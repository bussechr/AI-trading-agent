from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.data.live_quotes import fetch_bridge_ticks
from fxstack.io.parquet_store import ParquetStore
from fxstack.live.scorer import LiveScorer
from fxstack.settings import get_settings


@dataclass(slots=True)
class LoadedModelSet:
    pair: str
    model_set_id: str
    scorer: LiveScorer
    swing_router: "_PolicyModelRouter"
    intraday_router: "_PolicyModelRouter"


def _resolve_path(raw: str, project_root: Path) -> Path:
    p = Path(str(raw)).expanduser()
    if p.exists():
        return p.resolve()
    cands = [project_root / p, project_root.parent / p]
    for cand in cands:
        if cand.exists():
            return cand.resolve()
    raise FileNotFoundError(f"model artifact not found: {raw}")


def _artifact_path(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("path") or "")
    return ""


def _artifact_value(artifacts: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _artifact_path(artifacts.get(key))
        if value.strip():
            return value
    return ""


class _PolicyModelRouter:
    def __init__(
        self,
        *,
        policy: str,
        family: str,
        primary_name: str,
        primary_model: Any | None,
        fallback_name: str,
        fallback_model: Any | None,
    ) -> None:
        self.policy = str(policy)
        self.family = str(family)
        self.primary_name = str(primary_name)
        self.primary_model = primary_model
        self.fallback_name = str(fallback_name)
        self.fallback_model = fallback_model
        self.last_selected_model = ""
        self.last_fallback_reason = ""

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        self.last_selected_model = ""
        self.last_fallback_reason = ""

        if self.primary_model is not None:
            try:
                out = self.primary_model.predict_proba(X)
                self.last_selected_model = self.primary_name
                return out
            except Exception as exc:
                self.last_fallback_reason = f"{self.primary_name}_inference_error:{type(exc).__name__}"

        if self.fallback_model is not None:
            try:
                out = self.fallback_model.predict_proba(X)
                self.last_selected_model = self.fallback_name
                if not self.last_fallback_reason:
                    self.last_fallback_reason = f"{self.primary_name}_missing"
                return out
            except Exception as exc:
                detail = f"{self.fallback_name}_inference_error:{type(exc).__name__}"
                if self.last_fallback_reason:
                    detail = f"{self.last_fallback_reason};{detail}"
                raise RuntimeError(f"{self.family} routing failed: {detail}") from exc

        raise RuntimeError(f"{self.family} routing failed: no_available_model")

    def predict(self, X: pd.DataFrame) -> pd.Series:
        p = self.predict_proba(X)
        return (p["p1"] >= 0.5).astype(int)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "selected_model": self.last_selected_model,
            "used_fallback": bool(self.last_selected_model and self.last_selected_model != self.primary_name),
            "fallback_reason": self.last_fallback_reason if self.last_fallback_reason else "none",
        }


def _safe_load(model_cls: Any, raw_path: str, project_root: Path) -> tuple[Any | None, str]:
    value = str(raw_path or "").strip()
    if not value:
        return None, "missing_path"
    try:
        path = _resolve_path(value, project_root)
        return model_cls.load(path), ""
    except Exception as exc:
        return None, f"load_error:{type(exc).__name__}"


def _load_model_sets(*, pairs: list[str], require_all: bool, project_root: Path) -> dict[str, LoadedModelSet]:
    from fxstack.models.intraday_tcn import IntradayTCN
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.swing_transformer import SwingTransformer
    from fxstack.models.swing_xgb import SwingXGB
    from fxstack.runtime.service import RuntimeService

    s = get_settings()
    svc = RuntimeService(
        database_url=s.database_url,
        default_session_id=s.default_session_id,
        command_ttl_secs=s.command_ttl_secs,
        requeue_age_secs=s.startup_requeue_age_secs,
        db_connect_retries=s.db_connect_retries,
    )
    active = svc.get_active_model_sets(enabled_only=True)
    missing = [p for p in pairs if p not in active]
    if require_all and missing:
        raise RuntimeError(f"missing active model sets for pairs: {','.join(missing)}")

    out: dict[str, LoadedModelSet] = {}
    for pair in pairs:
        row = dict(active.get(pair, {}) or {})
        if not row:
            continue
        art = dict(row.get("artifacts_json") or {})
        meta_json = dict(row.get("metadata_json") or {})
        policy_json = dict(meta_json.get("policies") or {})

        swing_policy = str(policy_json.get("swing") or s.swing_model_policy)
        intraday_policy = str(policy_json.get("intraday") or s.intraday_model_policy)

        regime_path = _artifact_value(art, "regime")
        meta_path = _artifact_value(art, "meta")
        regime, regime_err = _safe_load(RegimeHMM, regime_path, project_root)
        meta, meta_err = _safe_load(MetaFilterXGB, meta_path, project_root)
        if regime is None or meta is None:
            if require_all:
                raise RuntimeError(
                    f"failed loading required models for {pair}: regime={regime_err or 'ok'},meta={meta_err or 'ok'}"
                )
            continue

        swing_tf, _ = _safe_load(SwingTransformer, _artifact_value(art, "swing_transformer"), project_root)
        swing_xgb, _ = _safe_load(SwingXGB, _artifact_value(art, "swing_xgb", "swing"), project_root)
        intraday_tcn, _ = _safe_load(IntradayTCN, _artifact_value(art, "intraday_tcn"), project_root)
        intraday_xgb, _ = _safe_load(IntradayXGB, _artifact_value(art, "intraday_xgb", "intraday"), project_root)

        swing_router = _PolicyModelRouter(
            policy=swing_policy,
            family="swing",
            primary_name="swing_transformer"
            if str(swing_policy).lower() == "transformer_primary_xgb_fallback"
            else "swing_xgb",
            primary_model=swing_tf if str(swing_policy).lower() == "transformer_primary_xgb_fallback" else swing_xgb,
            fallback_name="swing_xgb",
            fallback_model=swing_xgb if str(swing_policy).lower() == "transformer_primary_xgb_fallback" else None,
        )
        intraday_router = _PolicyModelRouter(
            policy=intraday_policy,
            family="intraday",
            primary_name="intraday_tcn"
            if str(intraday_policy).lower() == "tcn_primary_xgb_fallback"
            else "intraday_xgb",
            primary_model=intraday_tcn if str(intraday_policy).lower() == "tcn_primary_xgb_fallback" else intraday_xgb,
            fallback_name="intraday_xgb",
            fallback_model=intraday_xgb if str(intraday_policy).lower() == "tcn_primary_xgb_fallback" else None,
        )

        # Validate that at least one model is available per family.
        if swing_router.primary_model is None and swing_router.fallback_model is None:
            if require_all:
                raise RuntimeError(f"failed loading swing models for {pair} under policy={swing_policy}")
            continue
        if intraday_router.primary_model is None and intraday_router.fallback_model is None:
            if require_all:
                raise RuntimeError(f"failed loading intraday models for {pair} under policy={intraday_policy}")
            continue

        out[pair] = LoadedModelSet(
            pair=pair,
            model_set_id=str(row.get("model_set_id") or "unknown"),
            scorer=LiveScorer(regime_model=regime, swing_model=swing_router, intraday_model=intraday_router, meta_model=meta),
            swing_router=swing_router,
            intraday_router=intraday_router,
        )
    return out


def _latest_feature_row(*, store: ParquetStore, pair: str, timeframe: str) -> pd.DataFrame:
    provider = get_settings().normalized_data_provider
    df = store.read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
    if df.empty:
        return pd.DataFrame()
    row = df.sort_values("ts").tail(1).copy()
    return row


def _state_position_counts(state: dict[str, Any], *, pair: str) -> tuple[int, int]:
    positions = list(state.get("positions", []) or [])
    total = len(positions)
    pair_count = 0
    for p in positions:
        sym = str((p or {}).get("symbol", "")).upper()
        if sym == str(pair).upper():
            pair_count += 1
    return pair_count, total


def run_loop(*, equity: float, sleep_secs: int, feature_root: str) -> None:
    from fxstack.runtime.service import RuntimeService

    s = get_settings()
    pairs = list(s.pairs)
    if not pairs:
        raise RuntimeError("FXSTACK_PAIRS is empty")

    svc = RuntimeService(
        database_url=s.database_url,
        default_session_id=s.default_session_id,
        command_ttl_secs=s.command_ttl_secs,
        requeue_age_secs=s.startup_requeue_age_secs,
        db_connect_retries=s.db_connect_retries,
    )

    model_sets = _load_model_sets(
        pairs=pairs,
        require_all=bool(s.require_active_models),
        project_root=s.project_root,
    )
    if bool(s.require_active_models) and len(model_sets) != len(pairs):
        missing = [p for p in pairs if p not in model_sets]
        raise RuntimeError(f"active model load failed for pairs: {','.join(missing)}")

    store = ParquetStore(Path(feature_root))
    last_ts: dict[str, str] = {}

    while True:
        loop_ts = time.time()
        ticks = fetch_bridge_ticks(s.mt4_bridge_url)
        state = svc.get_state()
        governance = dict(state.get("governance", {}) or {})
        paused = bool(governance.get("paused", False))

        decisions: list[dict[str, Any]] = []
        rejection_counts: dict[str, int] = {}

        for pair in pairs:
            loaded = model_sets.get(pair)
            if loaded is None:
                reason = "missing_active_model_set"
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": {"pair": pair, "runtime": "fxstack"},
                    }
                )
                continue

            row = _latest_feature_row(store=store, pair=pair, timeframe=str(s.intraday_timeframe).upper())
            if row.empty:
                reason = "no_features"
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": {"pair": pair, "runtime": "fxstack"},
                    }
                )
                continue

            tick = dict((ticks.get(pair, {}) if isinstance(ticks, dict) else {}) or {})
            spread_bps = float(tick.get("spread", 0.0) or 0.0)
            expected_edge_bps = float(row.get("ret_1", pd.Series([0.0])).iloc[0] * 10000.0)

            signal = loaded.scorer.score(row, spread_bps=spread_bps, expected_edge_bps=expected_edge_bps)
            swing_route = loaded.swing_router.diagnostics()
            intraday_route = loaded.intraday_router.diagnostics()
            decision_reasons: list[str] = []
            if not bool(signal.allowed):
                decision_reasons.append(str(signal.rejection_reason))

            pair_count, total_count = _state_position_counts(state, pair=pair)
            if paused:
                decision_reasons.append("governance_paused")
            if pair_count >= int(s.max_pair_positions):
                decision_reasons.append("pair_exposure_cap")
            if total_count >= int(s.max_total_positions):
                decision_reasons.append("portfolio_exposure_cap")
            if float(spread_bps) > float(s.max_allowed_spread_bps):
                decision_reasons.append("spread_above_cap")
            if float(expected_edge_bps) < float(s.min_expected_edge_bps):
                decision_reasons.append("edge_below_hurdle")

            ready = len(decision_reasons) == 0
            side = "BUY" if str(signal.side).lower() == "long" else "SELL"
            ts_value = str(row.iloc[0].get("ts", ""))

            enqueue_out: dict[str, Any] = {"status": "skipped"}
            if ready:
                if last_ts.get(pair) != ts_value:
                    ts_parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
                    if pd.isna(ts_parsed):
                        ts_key = str(abs(hash(ts_value)))
                    else:
                        ts_key = str(int(ts_parsed.timestamp() * 1000.0))
                    cmd_id = f"fxs-{pair.lower()}-{ts_key}"
                    payload = {
                        "command_id": cmd_id,
                        "cmd": side,
                        "symbol": pair,
                        "lots": float(s.default_order_lots),
                        "intent": "ENTRY",
                        "trace_id": cmd_id,
                        "side": side,
                        "expected_edge_bps": float(expected_edge_bps),
                        "spread_bps": float(spread_bps),
                        "trade_prob": float(signal.trade_prob),
                        "swing_prob": float(signal.swing_prob),
                        "entry_prob": float(signal.entry_prob),
                        "regime_prob": float(signal.regime_prob),
                    }
                    out, _ = svc.submit_command(payload, proto="v2")
                    enqueue_out = dict(out)
                    last_ts[pair] = ts_value
                else:
                    enqueue_out = {"status": "duplicate_ts_skip", "ts": ts_value}

            if not ready:
                for reason in decision_reasons:
                    rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

            decisions.append(
                {
                    "symbol": pair,
                    "side": side,
                    "score": float(expected_edge_bps),
                    "confidence": float(max(0.0, min(100.0, signal.trade_prob * 100.0))),
                    "execution_ready": bool(ready),
                    "reasons": decision_reasons,
                    "metadata": {
                        "model_set_id": loaded.model_set_id,
                        "pair": pair,
                        "ts": ts_value,
                        "regime_prob": float(signal.regime_prob),
                        "swing_prob": float(signal.swing_prob),
                        "entry_prob": float(signal.entry_prob),
                        "trade_prob": float(signal.trade_prob),
                        "spread_bps": float(spread_bps),
                        "expected_edge_bps": float(expected_edge_bps),
                        "swing_policy": swing_route.get("policy"),
                        "swing_model_selected": swing_route.get("selected_model"),
                        "swing_fallback_reason": swing_route.get("fallback_reason"),
                        "intraday_policy": intraday_route.get("policy"),
                        "intraday_model_selected": intraday_route.get("selected_model"),
                        "intraday_fallback_reason": intraday_route.get("fallback_reason"),
                        "allowed": bool(ready),
                        "rejection_reason": "none" if ready else decision_reasons[0],
                        "enqueue": enqueue_out,
                    },
                }
            )

        first = decisions[0] if decisions else {"symbol": "N/A", "side": "N/A"}
        monitor_entry = {"symbol": str(first.get("symbol", "N/A")), "side": str(first.get("side", "N/A"))}

        svc.patch_state(
            {
                "system_status": "connected",
                "equity": float(equity),
                "last_heartbeat": loop_ts,
                "monitor": {
                    "entry": monitor_entry,
                    "close": {"dominant_close_reason": "none"},
                },
            }
        )

        svc.store_decisions(
            decisions=decisions,
            vol=0.0,
            diagnostics={
                "runtime": "fxstack",
                "pairs": pairs,
                "loop_ts": loop_ts,
                "rejection_stats": rejection_counts,
                "active_model_sets": sorted(list(model_sets.keys())),
            },
        )

        time.sleep(max(1, int(sleep_secs)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run fxstack runtime loop")
    ap.add_argument("--config", default="")
    ap.add_argument("--equity", type=float, required=True)
    ap.add_argument("--sleep", type=int, default=10)
    ap.add_argument("--feature-root", default="fx-quant-stack/data/features")
    _ = ap.parse_args()

    run_loop(equity=_.equity, sleep_secs=_.sleep, feature_root=_.feature_root)


if __name__ == "__main__":
    main()
