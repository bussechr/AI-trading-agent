from __future__ import annotations

import argparse
import hashlib
import json
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.data.live_quotes import fetch_bridge_ready, fetch_bridge_ticks
from fxstack.io.parquet_store import ParquetStore
from fxstack.live.policy import EDGE_FORMULA_ID, infer_pip_size, normalize_spread_bps
from fxstack.live.scorer import LiveScorer
from fxstack.settings import get_settings


@dataclass(slots=True)
class LoadedModelSet:
    pair: str
    model_set_id: str
    scorer: LiveScorer
    swing_router: "_PolicyModelRouter"
    intraday_router: "_PolicyModelRouter"
    has_exit_model: bool
    has_reversal_models: bool


def _resolve_path(raw: str, project_root: Path) -> Path:
    txt = str(raw or "").strip()
    if not txt:
        raise FileNotFoundError("empty model artifact path")
    variants = [txt]
    # Activation payloads may contain Windows-style separators even when runtime is POSIX.
    normalized = txt.replace("\\", "/")
    if normalized != txt:
        variants.append(normalized)
    for value in variants:
        p = Path(value).expanduser()
        cands = [p, project_root / p, project_root.parent / p]
        for cand in cands:
            if cand.exists():
                return cand.resolve()
    raise FileNotFoundError(f"model artifact not found: {raw}")


def _resolve_optional_path(raw: str, project_root: Path) -> Path | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    variants = [txt]
    normalized = txt.replace("\\", "/")
    if normalized != txt:
        variants.append(normalized)
    for value in variants:
        p = Path(value).expanduser()
        for cand in (p, project_root / p, project_root.parent / p):
            if cand.exists():
                return cand.resolve()
    return None


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
        primary_error = ""

        if self.primary_model is not None:
            try:
                out = self.primary_model.predict_proba(X)
                self.last_selected_model = self.primary_name
                return out
            except Exception as exc:
                primary_error = f"{self.primary_name}_inference_error:{type(exc).__name__}"
                self.last_fallback_reason = primary_error

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

        if primary_error:
            raise RuntimeError(f"{self.family} routing failed: {primary_error}")
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
        s = get_settings()
        timeout_secs = max(0.0, float(getattr(s, "model_load_timeout_secs", 0.0) or 0.0))
        path = _resolve_path(value, project_root)
        if timeout_secs > 0.0 and hasattr(signal, "SIGALRM"):
            def _timeout_handler(_signum, _frame):
                raise TimeoutError("model_load_timeout")

            prev_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, timeout_secs)
            try:
                model = model_cls.load(path)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, prev_handler)
            return model, ""
        return model_cls.load(path), ""
    except Exception as exc:
        return None, f"load_error:{type(exc).__name__}"


def _load_model_sets(*, pairs: list[str], require_all: bool, project_root: Path) -> tuple[dict[str, LoadedModelSet], dict[str, int]]:
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
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
    load_diag: dict[str, int] = {"model_load_timeouts": 0, "model_load_errors": 0}

    def _track_load_error(err: str) -> None:
        if not err:
            return
        if "TimeoutError" in str(err):
            load_diag["model_load_timeouts"] = int(load_diag.get("model_load_timeouts", 0)) + 1
        else:
            load_diag["model_load_errors"] = int(load_diag.get("model_load_errors", 0)) + 1
    for pair in pairs:
        row = dict(active.get(pair, {}) or {})
        if not row:
            continue
        art = dict(row.get("artifacts_json") or {})
        meta_json = dict(row.get("metadata_json") or {})
        policy_json = dict(meta_json.get("policies") or {})

        configured_swing_policy = str(s.swing_model_policy or "").strip()
        configured_intraday_policy = str(s.intraday_model_policy or "").strip()
        manifest_swing_policy = str(policy_json.get("swing") or "").strip()
        manifest_intraday_policy = str(policy_json.get("intraday") or "").strip()

        # Allow the active ops profile to force lighter model policies, even if
        # the activated artifact metadata prefers deep primary models.
        swing_policy = configured_swing_policy or manifest_swing_policy
        intraday_policy = configured_intraday_policy or manifest_intraday_policy
        if str(configured_swing_policy).lower() != "xgb_only" and manifest_swing_policy:
            swing_policy = manifest_swing_policy
        if str(configured_intraday_policy).lower() != "xgb_only" and manifest_intraday_policy:
            intraday_policy = manifest_intraday_policy

        regime_path = _artifact_value(art, "regime")
        meta_path = _artifact_value(art, "meta")
        regime, regime_err = _safe_load(RegimeHMM, regime_path, project_root)
        meta, meta_err = _safe_load(MetaFilterXGB, meta_path, project_root)
        _track_load_error(regime_err)
        _track_load_error(meta_err)
        if regime is None or meta is None:
            if require_all:
                raise RuntimeError(
                    f"failed loading required models for {pair}: regime={regime_err or 'ok'},meta={meta_err or 'ok'}"
                )
            continue

        swing_tf = None
        swing_xgb = None
        intraday_tcn = None
        intraday_xgb = None

        if str(swing_policy).lower() == "transformer_primary_xgb_fallback":
            from fxstack.models.swing_transformer import SwingTransformer

            swing_tf, swing_tf_err = _safe_load(SwingTransformer, _artifact_value(art, "swing_transformer"), project_root)
            swing_xgb, swing_err = _safe_load(SwingXGB, _artifact_value(art, "swing_xgb", "swing"), project_root)
            _track_load_error(swing_tf_err)
            _track_load_error(swing_err)
        else:
            swing_xgb, swing_err = _safe_load(SwingXGB, _artifact_value(art, "swing_xgb", "swing"), project_root)
            _track_load_error(swing_err)

        if str(intraday_policy).lower() == "tcn_primary_xgb_fallback":
            from fxstack.models.intraday_tcn import IntradayTCN

            intraday_tcn, intraday_tcn_err = _safe_load(IntradayTCN, _artifact_value(art, "intraday_tcn"), project_root)
            intraday_xgb, intraday_xgb_err = _safe_load(IntradayXGB, _artifact_value(art, "intraday_xgb", "intraday"), project_root)
            _track_load_error(intraday_tcn_err)
            _track_load_error(intraday_xgb_err)
        else:
            intraday_xgb, intraday_xgb_err = _safe_load(IntradayXGB, _artifact_value(art, "intraday_xgb", "intraday"), project_root)
            _track_load_error(intraday_xgb_err)

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
            has_exit_model=bool(_artifact_value(art, "exit_policy", "exit", "exit_model")),
            has_reversal_models=bool(
                _artifact_value(art, "reversal_failure", "reversal_failure_xgb")
                and _artifact_value(art, "reversal_opportunity", "reversal_opportunity_xgb")
            ),
        )
    return out, load_diag


def _seed_active_model_sets_from_manifest(*, svc: Any, project_root: Path) -> dict[str, Any]:
    s = get_settings()
    existing = svc.get_active_model_sets(enabled_only=True)
    configured_pairs = {str(p).upper() for p in list(s.pairs)}
    existing_pairs = {str(p).upper() for p in list(existing.keys())}
    missing_pairs = sorted(list(configured_pairs - existing_pairs)) if configured_pairs else []
    if existing and not missing_pairs:
        return {"seeded": False, "reason": "already_present", "pairs": sorted(list(existing_pairs))}

    manifest_candidate = _resolve_optional_path(str(s.model_activation_manifest), project_root)
    if manifest_candidate is None:
        return {
            "seeded": False,
            "reason": "manifest_missing",
            "path": str(s.model_activation_manifest),
            "missing_pairs": missing_pairs,
        }

    try:
        payload = json.loads(manifest_candidate.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"seeded": False, "reason": f"manifest_parse_error:{type(exc).__name__}", "path": str(manifest_candidate)}

    active = dict((payload or {}).get("active_model_sets") or {})
    if not active:
        return {
            "seeded": False,
            "reason": "manifest_empty",
            "path": str(manifest_candidate),
            "missing_pairs": missing_pairs,
        }

    seeded_pairs: list[str] = []
    target_pairs = set(missing_pairs) if missing_pairs else {str(p).upper() for p in active.keys()}
    for pair, row in active.items():
        pair_up = str(pair).upper()
        if target_pairs and pair_up not in target_pairs:
            continue
        item = dict(row or {})
        enabled = bool(item.get("enabled", True))
        if not enabled:
            continue
        artifacts = dict(item.get("artifacts") or {})
        policies = dict(item.get("policies") or {})
        metadata = dict(item.get("metadata") or {})
        metadata["policies"] = policies
        metadata["seed_source"] = "activation_manifest"
        try:
            svc.upsert_active_model_set(
                pair=pair_up,
                model_set_id=str(item.get("model_set_id") or f"{str(pair).lower()}-manifest"),
                registry_path=str(item.get("registry_path") or ""),
                artifacts=artifacts,
                metadata=metadata,
                enabled=True,
            )
            seeded_pairs.append(pair_up)
        except Exception:
            continue

    post = svc.get_active_model_sets(enabled_only=True)
    post_pairs = {str(p).upper() for p in list(post.keys())}
    post_missing_pairs = sorted(list(configured_pairs - post_pairs)) if configured_pairs else []
    return {
        "seeded": bool(seeded_pairs),
        "reason": "seeded_partial" if (seeded_pairs and post_missing_pairs) else ("seeded" if seeded_pairs else "seed_failed"),
        "path": str(manifest_candidate),
        "pairs": sorted(seeded_pairs),
        "missing_pairs": post_missing_pairs,
    }


def _latest_feature_row(*, store: ParquetStore, pair: str, timeframe: str) -> pd.DataFrame:
    provider = get_settings().normalized_data_provider
    if hasattr(store, "read_latest_row"):
        row = store.read_latest_row(provider=provider, pair=pair, timeframe=timeframe, tail_files=3)
        if not row.empty:
            return row
    df = store.read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
    if df.empty:
        return pd.DataFrame()
    return df.sort_values("ts").tail(1).copy()


def _required_feature_timeframes() -> list[str]:
    s = get_settings()
    ordered: list[str] = []
    for tf in (str(s.intraday_timeframe).upper(), str(s.swing_timeframe).upper(), str(s.regime_timeframe).upper()):
        if tf and tf not in ordered:
            ordered.append(tf)
    return ordered


def _state_mt4_fresh(state: dict[str, Any]) -> bool:
    status = str(state.get("system_status") or "").strip().lower()
    try:
        age = float(state.get("heartbeat_age_secs")) if state.get("heartbeat_age_secs") is not None else None
    except Exception:
        age = None
    try:
        stale_after = float(state.get("heartbeat_stale_after_secs") or 30.0)
    except Exception:
        stale_after = 30.0
    return bool(status == "connected" and age is not None and age <= stale_after)


def _state_position_counts(state: dict[str, Any], *, pair: str) -> tuple[int, int]:
    positions = list(state.get("positions", []) or [])
    total = len(positions)
    pair_count = 0
    for p in positions:
        sym = str((p or {}).get("symbol", "")).upper()
        if sym == str(pair).upper():
            pair_count += 1
    return pair_count, total


def _pair_positions(state: dict[str, Any], *, pair: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos in list(state.get("positions", []) or []):
        symbol = str((pos or {}).get("symbol", "")).upper()
        if symbol == str(pair).upper():
            out.append(dict(pos or {}))
    return out


def _position_side(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "flat"
    first = dict(positions[0] or {})
    typ = int(float(first.get("type", -1) or -1))
    if typ == 0:
        return "long"
    if typ == 1:
        return "short"
    return "flat"


def _position_oldest_open_time(positions: list[dict[str, Any]]) -> float:
    out: list[float] = []
    for pos in positions:
        try:
            ts = float(pos.get("open_time", 0.0) or 0.0)
        except Exception:
            ts = 0.0
        if ts > 0.0:
            out.append(ts)
    return min(out) if out else 0.0


def _build_command_id(*, pair: str, ts_value: str, action_tag: str) -> str:
    ts_parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    if pd.isna(ts_parsed):
        # Keep fallback deterministic across processes and restarts.
        ts_key = hashlib.sha1(str(ts_value).encode("utf-8")).hexdigest()[:16]
    else:
        ts_key = str(int(ts_parsed.timestamp() * 1000.0))
    if str(action_tag).strip().lower() == "entry":
        return f"fxs-{pair.lower()}-{ts_key}"
    return f"fxs-{action_tag}-{pair.lower()}-{ts_key}"


def _resolve_dukascopy_csv(*, pair: str, timeframe: str) -> Path:
    s = get_settings()
    pattern = str(s.dukascopy_file_pattern or "{pair}_{granularity}.csv").strip()
    try:
        file_name = pattern.format(
            pair=str(pair).upper(),
            granularity=str(timeframe).upper(),
            timeframe=str(timeframe).upper(),
        )
    except Exception:
        file_name = f"{str(pair).upper()}_{str(timeframe).upper()}.csv"
    return Path(str(s.dukascopy_source_root)).expanduser() / file_name


def _bootstrap_pair_features_from_csv(*, store: ParquetStore, pair: str, timeframe: str) -> tuple[bool, str]:
    s = get_settings()
    provider = str(s.normalized_data_provider)
    existing = _latest_feature_row(store=store, pair=pair, timeframe=timeframe)
    if not existing.empty:
        return False, "already_present"

    csv_path = _resolve_dukascopy_csv(pair=pair, timeframe=timeframe)
    if not csv_path.exists():
        return False, f"csv_missing:{csv_path}"

    try:
        from fxstack.data.ingest import ingest_dukascopy_csv, load_silver_bars
        from fxstack.features.build import build_features, leakage_guard
    except Exception as exc:
        return False, f"bootstrap_import_error:{type(exc).__name__}"

    raw_root = Path(s.project_root) / "data" / "raw"
    try:
        ingest_dukascopy_csv(
            store_root=raw_root,
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
            csv_path=csv_path,
            provider=provider,
        )
        bars = load_silver_bars(
            store_root=raw_root,
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
            provider=provider,
        )
        if bars.empty:
            return False, "raw_empty_after_ingest"
        feats = build_features(bars)
        leakage_guard(feats)
        if feats.empty:
            return False, "features_empty_after_build"
        store.write_partitioned(
            feats,
            provider=provider,
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
        )
        return True, f"rows={len(feats)}"
    except Exception as exc:
        return False, f"bootstrap_failed:{type(exc).__name__}"


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
    manifest_seed_diag = _seed_active_model_sets_from_manifest(svc=svc, project_root=s.project_root)

    model_sets, model_load_diag = _load_model_sets(
        pairs=pairs,
        require_all=bool(s.require_active_models),
        project_root=s.project_root,
    )
    if bool(s.require_active_models) and len(model_sets) != len(pairs):
        missing = [p for p in pairs if p not in model_sets]
        raise RuntimeError(f"active model load failed for pairs: {','.join(missing)}")

    store = ParquetStore(Path(feature_root))
    regime_timeframe = str(s.regime_timeframe).upper()
    swing_timeframe = str(s.swing_timeframe).upper()
    intraday_timeframe = str(s.intraday_timeframe).upper()
    feature_timeframes = _required_feature_timeframes()
    last_action_key: dict[str, str] = {}
    feature_bootstrap: dict[str, dict[str, dict[str, Any]]] = {}
    for pair in pairs:
        pair_bootstrap = feature_bootstrap.setdefault(str(pair), {})
        for timeframe in feature_timeframes:
            row = _latest_feature_row(store=store, pair=pair, timeframe=timeframe)
            if row.empty:
                ok, detail = _bootstrap_pair_features_from_csv(store=store, pair=pair, timeframe=timeframe)
                pair_bootstrap[timeframe] = {"attempted": True, "ok": bool(ok), "detail": str(detail)}

    svc.patch_state(
        {
            "runtime_profile": str(s.policy_version),
            "runtime_status": "starting",
            "runtime_last_cycle_ts": float(time.time()),
            "__prune_stale__": True,
        }
    )

    while True:
        loop_ts = time.time()
        loop_t0 = time.perf_counter()
        bridge_ready = fetch_bridge_ready(s.mt4_bridge_url)
        ticks = fetch_bridge_ticks(s.mt4_bridge_url)
        state = svc.get_state()
        governance = dict(state.get("governance", {}) or {})
        paused = bool(governance.get("paused", False))
        mt4_fresh = bool(bridge_ready.get("mt4_fresh")) if bridge_ready else _state_mt4_fresh(state)
        ticks_fresh = bool(bridge_ready.get("ticks_fresh")) if bridge_ready else bool(ticks)

        decisions: list[dict[str, Any]] = []
        rejection_counts: dict[str, int] = {}
        pair_eval_time_ms: dict[str, float] = {}
        inference_errors = 0

        for pair in pairs:
            pair_t0 = time.perf_counter()
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
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue

            pair_rows: dict[str, pd.DataFrame] = {}
            pair_bootstrap = feature_bootstrap.setdefault(str(pair), {})
            missing_frames: list[str] = []
            for timeframe in feature_timeframes:
                row = _latest_feature_row(store=store, pair=pair, timeframe=timeframe)
                if row.empty and not bool((pair_bootstrap.get(timeframe) or {}).get("attempted")):
                    ok, detail = _bootstrap_pair_features_from_csv(store=store, pair=pair, timeframe=timeframe)
                    pair_bootstrap[timeframe] = {"attempted": True, "ok": bool(ok), "detail": str(detail)}
                    row = _latest_feature_row(store=store, pair=pair, timeframe=timeframe)
                if row.empty:
                    missing_frames.append(timeframe)
                else:
                    pair_rows[timeframe] = row
            if missing_frames:
                reason = f"no_features:{','.join(missing_frames)}"
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                meta = {"pair": pair, "runtime": "fxstack"}
                if pair_bootstrap:
                    meta["feature_bootstrap"] = dict(pair_bootstrap)
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": meta,
                    }
                )
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue

            regime_row = pair_rows[regime_timeframe]
            swing_row = pair_rows[swing_timeframe]
            intraday_row = pair_rows[intraday_timeframe]
            tick = dict((ticks.get(pair, {}) if isinstance(ticks, dict) else {}) or {})
            spread_bps, spread_unit_source = normalize_spread_bps(tick=tick, row=intraday_row.iloc[0], pair=pair)

            try:
                signal = loaded.scorer.score(
                    regime_row=regime_row,
                    swing_row=swing_row,
                    intraday_row=intraday_row,
                    meta_row=intraday_row,
                    spread_bps=float(spread_bps),
                    expected_edge_bps=None,
                    spread_unit_source=str(spread_unit_source),
                )
            except Exception as exc:
                reason = f"model_inference_error:{type(exc).__name__}"
                inference_errors += 1
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": {"pair": pair, "runtime": "fxstack", "error": str(exc)},
                    }
                )
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue
            expected_edge_bps = float(signal.expected_edge_bps)
            swing_route = loaded.swing_router.diagnostics()
            intraday_route = loaded.intraday_router.diagnostics()
            decision_reasons: list[str] = []
            if not bool(signal.allowed):
                decision_reasons.append(str(signal.rejection_reason))
            if str(spread_unit_source) == "missing":
                decision_reasons.append("missing_spread_input")

            positions = _pair_positions(state, pair=pair)
            pair_count, total_count = _state_position_counts(state, pair=pair)
            pos_side = _position_side(positions)
            if not positions and not mt4_fresh:
                decision_reasons.append("mt4_stale")
            if not positions and not ticks_fresh:
                decision_reasons.append("tick_feed_stale")
            if not positions and not bool(tick):
                decision_reasons.append("missing_live_tick")
            if paused:
                decision_reasons.append("governance_paused")
            if pair_count >= int(s.max_pair_positions):
                decision_reasons.append("pair_exposure_cap")
            if total_count >= int(s.max_total_positions):
                decision_reasons.append("portfolio_exposure_cap")

            # Keep reasons unique while preserving evaluation order.
            decision_reasons = list(dict.fromkeys(decision_reasons))
            ready = len(decision_reasons) == 0
            side = "BUY" if str(signal.side).lower() == "long" else "SELL"
            ts_value = str(intraday_row.iloc[0].get("ts", ""))
            desired_side = "long" if side == "BUY" else "short"
            lifecycle_soft_degrade_reasons: list[str] = []
            if not bool(loaded.has_exit_model):
                lifecycle_soft_degrade_reasons.append("no_exit_model_runtime_soft")
            if not bool(loaded.has_reversal_models):
                lifecycle_soft_degrade_reasons.append("no_reversal_model_runtime_soft")

            enqueue_out: dict[str, Any] = {"status": "skipped"}
            lifecycle_action = "hold"
            lifecycle_action_score = 0.0
            lifecycle_reason = "hold"
            action_tag = "hold"
            close_lots = 0.0

            # Action precedence:
            # 1) hard risk/time-stop emergency
            # 2) reversal-exit decision
            # 3) adjust/exit actions
            # 4) entry (flat only)
            if positions and float(s.hard_time_stop_secs) > 0.0:
                oldest_open_time = _position_oldest_open_time(positions)
                if oldest_open_time > 0.0 and (float(loop_ts) - float(oldest_open_time)) >= float(s.hard_time_stop_secs):
                    lifecycle_action = "exit"
                    lifecycle_action_score = 1.0
                    lifecycle_reason = "hard_time_stop"
                    action_tag = "exit"
            if positions and lifecycle_action == "hold" and bool(s.enable_lifecycle_actions):
                if desired_side != "flat" and str(pos_side) != "flat" and desired_side != str(pos_side) and bool(signal.allowed):
                    lifecycle_action = "exit"
                    lifecycle_action_score = 0.8
                    lifecycle_reason = "reversal_exit"
                    action_tag = "reversal_exit"
            if (
                positions
                and lifecycle_action == "hold"
                and bool(s.enable_lifecycle_actions)
                and bool(loaded.has_exit_model)
                and float(signal.trade_prob) < float(s.min_trade_prob * 0.8)
            ):
                first_pos = dict(positions[0] or {})
                lots_open = float(first_pos.get("lots", 0.0) or 0.0)
                close_lots = max(0.0, lots_open * float(s.partial_close_fraction))
                if close_lots > 0.0:
                    lifecycle_action = "partial_tp"
                    lifecycle_action_score = 0.6
                    lifecycle_reason = "exit_model_reduce"
                    action_tag = "close_partial"
            if (
                positions
                and lifecycle_action == "hold"
                and bool(s.enable_adjust_actions)
                and float(s.adjust_stop_buffer_pips) > 0.0
            ):
                bid = float(tick.get("bid", 0.0) or 0.0)
                ask = float(tick.get("ask", 0.0) or 0.0)
                if bid > 0.0 and ask > 0.0 and str(pos_side) in {"long", "short"}:
                    pip_size = infer_pip_size(pair=pair, digits=int(float(tick.get("digits", 0.0) or 0.0)) or None)
                    px_buffer = float(s.adjust_stop_buffer_pips) * float(pip_size)
                    sl_price = (bid - px_buffer) if str(pos_side) == "long" else (ask + px_buffer)
                    lifecycle_action = "tighten_stop"
                    lifecycle_action_score = 0.5
                    lifecycle_reason = "adjust_stop_buffer"
                    action_tag = "adjust_sl"
                else:
                    sl_price = 0.0
            else:
                sl_price = 0.0

            action_key = f"{action_tag}:{ts_value}"
            if lifecycle_action in {"exit", "tighten_stop", "partial_tp"}:
                if last_action_key.get(pair) != action_key:
                    cmd_id = _build_command_id(pair=pair, ts_value=ts_value, action_tag=action_tag)
                    if lifecycle_action == "tighten_stop":
                        payload = {
                            "command_id": cmd_id,
                            "cmd": "MODIFY_SL",
                            "symbol": pair,
                            "lots": 0.0,
                            "sl_price": float(sl_price),
                            "intent": "ADJUST_MODEL",
                            "trace_id": cmd_id,
                            "action": "tighten_stop",
                            "action_score": float(lifecycle_action_score),
                            "reversal_token": "",
                        }
                    elif lifecycle_action == "partial_tp":
                        payload = {
                            "command_id": cmd_id,
                            "cmd": "CLOSE_PARTIAL",
                            "symbol": pair,
                            "lots": float(close_lots),
                            "close_lots": float(close_lots),
                            "intent": "EXIT_MODEL",
                            "trace_id": cmd_id,
                            "action": "partial_tp",
                            "action_score": float(lifecycle_action_score),
                            "reversal_token": "",
                        }
                    else:
                        payload = {
                            "command_id": cmd_id,
                            "cmd": "CLOSE",
                            "symbol": pair,
                            "lots": 0.0,
                            "intent": "EXIT_MODEL" if lifecycle_reason != "reversal_exit" else "REVERSAL_EXIT",
                            "trace_id": cmd_id,
                            "action": "exit",
                            "action_score": float(lifecycle_action_score),
                            "reversal_token": cmd_id if lifecycle_reason == "reversal_exit" else "",
                        }
                    out, _ = svc.submit_command(payload, proto="v2")
                    enqueue_out = dict(out)
                    last_action_key[pair] = action_key
                else:
                    enqueue_out = {"status": "duplicate_action_skip", "ts": ts_value, "action": lifecycle_action}
            elif ready and not positions:
                lifecycle_action = "entry"
                lifecycle_action_score = float(signal.trade_prob)
                lifecycle_reason = "entry_approved"
                action_key = f"entry:{ts_value}"
                if last_action_key.get(pair) != action_key:
                    cmd_id = _build_command_id(pair=pair, ts_value=ts_value, action_tag="entry")
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
                        "action": "entry",
                        "action_score": float(signal.trade_prob),
                    }
                    out, _ = svc.submit_command(payload, proto="v2")
                    enqueue_out = dict(out)
                    last_action_key[pair] = action_key
                else:
                    enqueue_out = {"status": "duplicate_action_skip", "ts": ts_value, "action": "entry"}
            elif positions:
                lifecycle_reason = "position_open_hold"
                if not loaded.has_exit_model:
                    lifecycle_reason = "no_exit_model"
                    lifecycle_soft_degrade_reasons.append("no_exit_model")
                if not loaded.has_reversal_models:
                    lifecycle_soft_degrade_reasons.append("no_reversal_model")

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
                        "tick_available": bool(tick),
                        "mt4_fresh": bool(mt4_fresh),
                        "ticks_fresh": bool(ticks_fresh),
                        "expected_edge_bps": float(expected_edge_bps),
                        "policy_version": str(signal.policy_version),
                        "edge_formula_id": str(signal.edge_formula_id),
                        "threshold_snapshot": dict(signal.threshold_snapshot),
                        "spread_unit_source": str(signal.spread_unit_source),
                        "scenario_bucket": str(signal.scenario_bucket),
                        "context_frame_profile": str(signal.context_frame_profile or s.frame_profile),
                        "uncertainty_score": float(signal.uncertainty_score),
                        "swing_policy": swing_route.get("policy"),
                        "swing_model_selected": swing_route.get("selected_model"),
                        "swing_fallback_reason": swing_route.get("fallback_reason"),
                        "intraday_policy": intraday_route.get("policy"),
                        "intraday_model_selected": intraday_route.get("selected_model"),
                        "intraday_fallback_reason": intraday_route.get("fallback_reason"),
                        "feature_timeframes": {
                            "regime": regime_timeframe,
                            "swing": swing_timeframe,
                            "intraday": intraday_timeframe,
                            "meta": intraday_timeframe,
                        },
                        "position_side": pos_side,
                        "position_count_pair": int(pair_count),
                        "lifecycle_action": str(lifecycle_action),
                        "lifecycle_action_score": float(lifecycle_action_score),
                        "lifecycle_reason": str(lifecycle_reason),
                        "lifecycle_activation_mode": "runtime_soft",
                        "lifecycle_capabilities": {
                            "has_exit_model": bool(loaded.has_exit_model),
                            "has_reversal_models": bool(loaded.has_reversal_models),
                        },
                        "lifecycle_soft_degrade_reasons": list(dict.fromkeys(lifecycle_soft_degrade_reasons)),
                        "allowed": bool(ready),
                        "rejection_reason": "none" if ready else decision_reasons[0],
                        "enqueue": enqueue_out,
                    },
                }
            )
            pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)

        first = decisions[0] if decisions else {"symbol": "N/A", "side": "N/A"}
        monitor_entry = {"symbol": str(first.get("symbol", "N/A")), "side": str(first.get("side", "N/A"))}
        loop_latency_ms = round((time.perf_counter() - loop_t0) * 1000.0, 3)
        runtime_diag = {
            "loop_latency_ms": float(loop_latency_ms),
            "pair_eval_time_ms": dict(pair_eval_time_ms),
            "inference_errors": int(inference_errors),
            "model_load_timeouts": int(model_load_diag.get("model_load_timeouts", 0)),
            "model_load_errors": int(model_load_diag.get("model_load_errors", 0)),
            "feature_bootstrap": dict(feature_bootstrap),
            "manifest_seed": dict(manifest_seed_diag),
        }

        state_patch: dict[str, Any] = {
            "runtime_profile": str(s.policy_version),
            "runtime_last_cycle_ts": float(loop_ts),
            "runtime_status": "running",
            "runtime_equity_seed": float(equity),
            "runtime_diag": runtime_diag,
            "monitor": {
                "entry": monitor_entry,
                "close": {"dominant_close_reason": "none"},
            },
        }
        svc.patch_state(state_patch)

        svc.store_decisions(
            decisions=decisions,
            vol=0.0,
            diagnostics={
                "runtime": "fxstack",
                "pairs": pairs,
                "loop_ts": loop_ts,
                "rejection_stats": rejection_counts,
                "active_model_sets": sorted(list(model_sets.keys())),
                "policy_version": str(s.policy_version),
                "edge_formula_id": EDGE_FORMULA_ID,
                "runtime_diag": runtime_diag,
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
