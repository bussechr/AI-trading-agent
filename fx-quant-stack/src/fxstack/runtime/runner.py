from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from fxstack.data.live_quotes import fetch_bridge_bars, fetch_bridge_ready, fetch_bridge_ticks
from fxstack.features.fx_lifecycle import add_fx_lifecycle_features
from fxstack.features.multi_tf_contract import build_multi_tf_rows, resample_bars
from fxstack.io.parquet_store import ParquetStore
from fxstack.live.policy import EDGE_FORMULA_ID, infer_pip_size, normalize_spread_bps
from fxstack.live.scorer import LiveScorer
from fxstack.settings import get_settings


@dataclass(slots=True)
class LoadedModelSet:
    pair: str
    model_set_id: str
    registry_path: str
    scorer: LiveScorer
    swing_router: "_PolicyModelRouter"
    intraday_router: "_PolicyModelRouter"
    exit_model: Any | None
    reversal_failure_model: Any | None
    reversal_opportunity_model: Any | None
    exit_action_labels: dict[int, str]
    lifecycle_activation_mode: str
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


def _load_artifact_meta(raw_path: str, project_root: Path) -> dict[str, Any]:
    path = _resolve_optional_path(str(raw_path or ""), project_root)
    if path is None:
        return {}
    meta_path = path / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return dict(json.loads(meta_path.read_text(encoding="utf-8")) or {})
    except Exception:
        return {}


def _required_model_feature_columns(*models: Any) -> list[str]:
    cols: list[str] = []
    for model in models:
        for col in list(getattr(model, "feature_columns", []) or []):
            txt = str(col or "").strip()
            if txt and txt not in cols:
                cols.append(txt)
    return cols


def _exit_action_labels(exit_meta: dict[str, Any], classes: list[int] | None) -> dict[int, str]:
    ordered = ["hold", "partial_tp", "exit"]
    class_ids = [int(x) for x in list(classes or [])] or [0, 1, 2]
    labels: dict[int, str] = {}
    for idx, class_id in enumerate(class_ids):
        labels[int(class_id)] = ordered[idx] if idx < len(ordered) else f"class_{class_id}"
    collapse = dict(exit_meta.get("exit_action_collapse") or {})
    collapse_actions = list((((collapse.get("class_balance_after") or {})).keys())) if collapse else []
    if collapse_actions and len(collapse_actions) == len(class_ids):
        for idx, class_id in enumerate(class_ids):
            labels[int(class_id)] = str(collapse_actions[idx])
    return labels


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _timeframe_to_seconds(timeframe: str) -> int:
    txt = str(timeframe or "").strip().upper()
    if not txt:
        return 0
    if txt == "D":
        return 86_400
    if txt == "W":
        return 604_800
    if txt in {"MN", "MN1"}:
        return 2_592_000
    unit = txt[:1]
    magnitude = txt[1:] or "1"
    try:
        value = int(magnitude)
    except Exception:
        return 0
    scale = {
        "S": 1,
        "M": 60,
        "H": 3_600,
        "D": 86_400,
    }.get(unit, 0)
    return int(value * scale) if scale > 0 else 0


def _feature_bar_freshness(*, ts_value: Any, loop_ts: float, timeframe: str) -> dict[str, Any]:
    parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    timeframe_secs = max(0, _timeframe_to_seconds(timeframe))
    stale_after_secs = max(float(timeframe_secs * 2), 600.0)
    if pd.isna(parsed):
        return {
            "ts": str(ts_value or ""),
            "age_secs": None,
            "stale": True,
            "stale_after_secs": stale_after_secs,
            "reason": "missing_feature_ts",
        }
    age_secs = max(0.0, float(loop_ts) - float(parsed.timestamp()))
    return {
        "ts": str(parsed),
        "age_secs": float(age_secs),
        "stale": bool(age_secs > stale_after_secs),
        "stale_after_secs": float(stale_after_secs),
        "reason": "ok" if age_secs <= stale_after_secs else "stale_feature_bar",
    }


def _bars_to_raw_frame(*, pair: str, timeframe: str, bars: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    tf = str(timeframe).upper()
    sym = str(pair).upper()
    for bar in list(bars or []):
        ts = pd.to_datetime(bar.get("time") or bar.get("ts"), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        spread = _safe_float(bar.get("spread"), 0.0)
        mid_open = _safe_float(bar.get("mid_open", bar.get("open")), 0.0)
        mid_high = _safe_float(bar.get("mid_high", bar.get("high")), 0.0)
        mid_low = _safe_float(bar.get("mid_low", bar.get("low")), 0.0)
        mid_close = _safe_float(bar.get("mid_close", bar.get("close")), 0.0)
        if min(mid_open, mid_high, mid_low, mid_close) <= 0.0:
            continue
        half_spread = spread / 2.0
        bid_open = _safe_float(bar.get("bid_open"), mid_open - half_spread)
        bid_high = _safe_float(bar.get("bid_high"), mid_high - half_spread)
        bid_low = _safe_float(bar.get("bid_low"), mid_low - half_spread)
        bid_close = _safe_float(bar.get("bid_close"), mid_close - half_spread)
        ask_open = _safe_float(bar.get("ask_open"), mid_open + half_spread)
        ask_high = _safe_float(bar.get("ask_high"), mid_high + half_spread)
        ask_low = _safe_float(bar.get("ask_low"), mid_low + half_spread)
        ask_close = _safe_float(bar.get("ask_close"), mid_close + half_spread)
        rows.append(
            {
                "pair": sym,
                "timeframe": tf,
                "ts": ts,
                "bid_open": float(bid_open),
                "bid_high": float(bid_high),
                "bid_low": float(bid_low),
                "bid_close": float(bid_close),
                "ask_open": float(ask_open),
                "ask_high": float(ask_high),
                "ask_low": float(ask_low),
                "ask_close": float(ask_close),
                "mid_open": float(mid_open),
                "mid_high": float(mid_high),
                "mid_low": float(mid_low),
                "mid_close": float(mid_close),
                "volume": int(_safe_float(bar.get("volume"), 0.0)),
                "spread": float(spread),
                "date": pd.to_datetime(ts, utc=True).strftime("%Y-%m-%d"),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("ts").drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")


def _feature_tail_spec(timeframe: str) -> tuple[int, int]:
    tf = str(timeframe).upper()
    if tf == "M5":
        return 14, 3000
    if tf == "H4":
        return 45, 400
    if tf == "D":
        return 120, 200
    return 30, 1000


def _refresh_feature_tail(
    *,
    feature_store: ParquetStore,
    raw_store: ParquetStore,
    provider: str,
    pair: str,
    timeframe: str,
) -> dict[str, Any]:
    tail_files, max_rows = _feature_tail_spec(timeframe)
    raw_recent = raw_store.read_recent_rows(
        provider=provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        tail_files=tail_files,
        max_rows=max_rows,
    )
    if raw_recent.empty:
        return {"ok": False, "reason": "raw_recent_empty"}
    feats = add_fx_lifecycle_features(raw_recent)
    if feats.empty:
        return {"ok": False, "reason": "feature_build_empty"}
    feature_store.write_partitioned(
        feats,
        provider=provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
    )
    latest_ts = str(feats.sort_values("ts").iloc[-1]["ts"])
    return {"ok": True, "reason": "refreshed", "latest_ts": latest_ts, "rows": int(len(feats))}


def _tick_bucket_start(*, tick: dict[str, Any], timeframe: str) -> int | None:
    ts = _safe_float(dict(tick or {}).get("ts_epoch"), 0.0)
    tf_secs = max(0, _timeframe_to_seconds(timeframe))
    if ts <= 0.0 or tf_secs <= 0:
        return None
    return int(ts // tf_secs) * tf_secs


def _refresh_live_pair_market_data(
    *,
    bridge_url: str,
    raw_store: ParquetStore,
    feature_store: ParquetStore,
    pair: str,
    provider: str,
    latest_bar_cache: dict[str, str],
) -> dict[str, Any]:
    bars = fetch_bridge_bars(bridge_url, symbol=pair, timeframe="M5", limit=1000)
    raw_m5 = _bars_to_raw_frame(pair=pair, timeframe="M5", bars=bars)
    if raw_m5.empty:
        return {"ok": False, "reason": "no_bridge_bars"}

    latest_ts = str(raw_m5.sort_values("ts").iloc[-1]["ts"])
    pair_key = str(pair).upper()
    if latest_bar_cache.get(pair_key) == latest_ts:
        return {"ok": True, "reason": "already_current", "latest_ts": latest_ts}

    raw_store.write_partitioned(raw_m5, provider=provider, pair=pair_key, timeframe="M5")
    for tf in ("M15", "H1", "H4", "D"):
        resampled = resample_bars(raw_m5, tf)
        if not resampled.empty:
            raw_store.write_partitioned(resampled, provider=provider, pair=pair_key, timeframe=tf)

    feature_diag: dict[str, Any] = {}
    for tf in ("M5", "H4", "D"):
        feature_diag[tf] = _refresh_feature_tail(
            feature_store=feature_store,
            raw_store=raw_store,
            provider=provider,
            pair=pair,
            timeframe=tf,
        )

    latest_bar_cache[pair_key] = latest_ts
    return {
        "ok": True,
        "reason": "refreshed",
        "latest_ts": latest_ts,
        "feature_refresh": feature_diag,
    }


def _round_lot_size(*, lots: float, min_lot: float, lot_step: float, max_lot: float) -> float:
    step = max(1e-9, float(lot_step))
    minimum = max(0.0, float(min_lot))
    maximum = max(0.0, float(max_lot))
    raw = max(0.0, float(lots))
    quantized = math.floor((raw / step) + 1e-9) * step
    quantized = max(minimum, quantized)
    if maximum > 0.0:
        quantized = min(maximum, quantized)
    decimals = max(0, int(round(-math.log10(step)))) if step < 1.0 else 0
    return round(float(quantized), decimals)


def _partial_close_plan(*, lots_open: float, fraction: float, settings: Any) -> tuple[str, float]:
    open_lots = max(0.0, float(lots_open))
    close_fraction = max(0.0, float(fraction))
    if open_lots <= 0.0 or close_fraction <= 0.0:
        return "hold", 0.0

    min_lot = max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01))
    lot_step = max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01))
    requested_close = open_lots * close_fraction
    rounded_close = _round_lot_size(
        lots=requested_close,
        min_lot=min_lot,
        lot_step=lot_step,
        max_lot=open_lots,
    )
    tolerance = max(1e-9, lot_step / 10.0)
    remaining_lots = max(0.0, open_lots - rounded_close)
    if rounded_close <= 0.0:
        return "hold", 0.0
    if rounded_close >= (open_lots - tolerance):
        return "exit", round(float(open_lots), 8)
    if 0.0 < remaining_lots < (min_lot - tolerance):
        return "exit", round(float(open_lots), 8)
    return "partial_tp", round(float(rounded_close), 8)


def _entry_order_lots(*, state: dict[str, Any], settings: Any, equity_seed: float) -> tuple[float, dict[str, Any]]:
    equity_live = _safe_float(state.get("equity", 0.0), 0.0)
    equity_value = equity_live if equity_live > 0.0 else _safe_float(equity_seed, 0.0)
    raw_lots = 0.0
    sizing_mode = "fixed_default"
    coefficient = max(0.0, _safe_float(getattr(settings, "equity_lots_per_usd", 0.0), 0.0))
    if equity_value > 0.0 and coefficient > 0.0:
        raw_lots = equity_value * coefficient
        sizing_mode = "equity_scaled"
    else:
        raw_lots = max(0.0, _safe_float(getattr(settings, "default_order_lots", 0.0), 0.0))
    rounded_lots = _round_lot_size(
        lots=raw_lots,
        min_lot=max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01)),
        lot_step=max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01)),
        max_lot=max(0.0, _safe_float(getattr(settings, "max_order_lots", 0.0), 0.0)),
    )
    return rounded_lots, {
        "mode": sizing_mode,
        "equity": float(equity_value),
        "coefficient": float(coefficient),
        "raw_lots": float(raw_lots),
        "rounded_lots": float(rounded_lots),
    }


def _startup_log(message: str) -> None:
    print(f"[runtime-startup] {str(message)}", flush=True)


def _runtime_startup_state(
    *,
    boot_id: str,
    booted_at: str,
    runtime_pid: int,
    phase: str,
    phase_pair: str = "",
    phase_index: int = 0,
    phase_total: int = 0,
    last_progress_ts: float | None = None,
    failure_reason: str = "",
    failed_at: str = "",
    pending_command_policy: str = "purge_and_mark_stale",
) -> dict[str, Any]:
    progress_ts = float(last_progress_ts if last_progress_ts is not None else time.time())
    return {
        "boot_id": str(boot_id),
        "booted_at": str(booted_at),
        "runtime_pid": int(runtime_pid),
        "phase": str(phase),
        "phase_pair": str(phase_pair or ""),
        "phase_index": int(phase_index),
        "phase_total": int(phase_total),
        "last_progress_ts": float(progress_ts),
        "failure_reason": str(failure_reason or ""),
        "failed_at": str(failed_at or ""),
        "pending_command_policy": str(pending_command_policy or "purge_and_mark_stale"),
    }


def _runtime_boot_reset_patch(
    *,
    runtime_profile: str,
    equity_seed: float,
    pairs: list[str],
    startup_state: dict[str, Any],
    runtime_diag: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "runtime_profile": str(runtime_profile),
        "runtime_status": "starting",
        "runtime_last_cycle_ts": 0.0,
        "runtime_equity_seed": float(equity_seed),
        "configured_pairs": list(pairs),
        "agent_decisions": [],
        "agent_diagnostics": {},
        "monitor": {},
        "vol": 0.0,
        "runtime_diag": dict(runtime_diag or {}),
        "runtime_startup": dict(startup_state),
        "__prune_stale__": True,
    }


def _touch_runtime_startup_progress(
    *,
    svc: Any,
    startup_state: dict[str, Any],
    phase: str,
    phase_pair: str = "",
    phase_index: int = 0,
    phase_total: int = 0,
    runtime_diag: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_state = _runtime_startup_state(
        boot_id=str(startup_state.get("boot_id") or ""),
        booted_at=str(startup_state.get("booted_at") or ""),
        runtime_pid=int(startup_state.get("runtime_pid") or 0),
        phase=str(phase),
        phase_pair=str(phase_pair or ""),
        phase_index=int(phase_index),
        phase_total=int(phase_total),
        last_progress_ts=float(time.time()),
        failure_reason="",
        failed_at="",
        pending_command_policy=str(startup_state.get("pending_command_policy") or "purge_and_mark_stale"),
    )
    patch = {
        "runtime_status": "starting",
        "runtime_last_cycle_ts": 0.0,
        "runtime_startup": dict(next_state),
    }
    if runtime_diag is not None:
        patch["runtime_diag"] = dict(runtime_diag)
    svc.record_runtime_boot_state(boot=next_state, patch=patch, prune_state=False)
    return next_state


def _touch_runtime_loop_progress(*, svc: Any, startup_state: dict[str, Any]) -> dict[str, Any]:
    next_state = _runtime_startup_state(
        boot_id=str(startup_state.get("boot_id") or ""),
        booted_at=str(startup_state.get("booted_at") or ""),
        runtime_pid=int(startup_state.get("runtime_pid") or 0),
        phase="main_loop",
        phase_pair="",
        phase_index=0,
        phase_total=0,
        last_progress_ts=float(time.time()),
        failure_reason="",
        failed_at="",
        pending_command_policy=str(startup_state.get("pending_command_policy") or "purge_and_mark_stale"),
    )
    svc.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": float(time.time()),
            "runtime_startup": dict(next_state),
        }
    )
    return next_state


def _record_runtime_startup_failure(
    *,
    svc: Any,
    startup_state: dict[str, Any],
    failure_reason: str,
    runtime_diag: dict[str, Any] | None = None,
) -> None:
    failure_ts = float(time.time())
    failed_iso = pd.Timestamp(failure_ts, unit="s", tz="UTC").isoformat()
    boot_state = dict(startup_state)
    boot_state["failure_reason"] = str(failure_reason or "")
    boot_state["failed_at"] = str(failed_iso)
    svc.record_runtime_boot_failure(
        boot=boot_state,
        failure_reason=str(failure_reason or ""),
        failed_at=failed_iso,
        patch={
            "runtime_status": "failed",
            "runtime_last_cycle_ts": 0.0,
            "agent_decisions": [],
            "agent_diagnostics": {},
            "monitor": {},
            "vol": 0.0,
            "runtime_diag": dict(runtime_diag or {}),
        },
        prune_state=True,
    )


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
    from fxstack.models.exit_policy_xgb import ExitPolicyXGB
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.reversal_failure_xgb import ReversalFailureXGB
    from fxstack.models.reversal_opportunity_xgb import ReversalOpportunityXGB
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
        exit_path = _artifact_value(art, "exit_policy", "exit", "exit_model")
        reversal_failure_path = _artifact_value(art, "reversal_failure", "reversal_failure_xgb")
        reversal_opportunity_path = _artifact_value(art, "reversal_opportunity", "reversal_opportunity_xgb")
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

        exit_model, exit_err = _safe_load(ExitPolicyXGB, exit_path, project_root)
        reversal_failure_model, reversal_failure_err = _safe_load(ReversalFailureXGB, reversal_failure_path, project_root)
        reversal_opportunity_model, reversal_opportunity_err = _safe_load(
            ReversalOpportunityXGB,
            reversal_opportunity_path,
            project_root,
        )
        _track_load_error(exit_err)
        _track_load_error(reversal_failure_err)
        _track_load_error(reversal_opportunity_err)

        if require_all and str(exit_path).strip() and exit_model is None:
            raise RuntimeError(f"failed loading exit model for {pair}: {exit_err or 'unknown'}")
        if require_all and str(reversal_failure_path).strip() and reversal_failure_model is None:
            raise RuntimeError(f"failed loading reversal failure model for {pair}: {reversal_failure_err or 'unknown'}")
        if require_all and str(reversal_opportunity_path).strip() and reversal_opportunity_model is None:
            raise RuntimeError(
                f"failed loading reversal opportunity model for {pair}: {reversal_opportunity_err or 'unknown'}"
            )

        exit_meta = _load_artifact_meta(exit_path, project_root) if str(exit_path).strip() else {}
        if exit_model is not None and not getattr(exit_model, "feature_columns", None):
            setattr(exit_model, "feature_columns", list(exit_meta.get("feature_columns") or []))
        exit_action_labels = _exit_action_labels(exit_meta, getattr(exit_model, "classes_", None))
        has_exit_model = bool(exit_model is not None)
        has_reversal_models = bool(reversal_failure_model is not None and reversal_opportunity_model is not None)
        lifecycle_activation_mode = "model_driven" if (has_exit_model or has_reversal_models) else "runtime_soft"

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
            registry_path=str(row.get("registry_path") or ""),
            scorer=LiveScorer(regime_model=regime, swing_model=swing_router, intraday_model=intraday_router, meta_model=meta),
            swing_router=swing_router,
            intraday_router=intraday_router,
            exit_model=exit_model,
            reversal_failure_model=reversal_failure_model,
            reversal_opportunity_model=reversal_opportunity_model,
            exit_action_labels=exit_action_labels,
            lifecycle_activation_mode=lifecycle_activation_mode,
            has_exit_model=has_exit_model,
            has_reversal_models=has_reversal_models,
        )
    return out, load_diag


def _seed_active_model_sets_from_manifest(*, svc: Any, project_root: Path) -> dict[str, Any]:
    s = get_settings()
    existing = svc.get_active_model_sets(enabled_only=True)
    configured_pairs = {str(p).upper() for p in list(s.pairs)}

    manifest_candidate = _resolve_optional_path(str(s.model_activation_manifest), project_root)
    if manifest_candidate is None:
        return {
            "seeded": False,
            "reason": "manifest_missing",
            "path": str(s.model_activation_manifest),
            "missing_pairs": sorted(list(configured_pairs)) if configured_pairs else [],
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
            "missing_pairs": sorted(list(configured_pairs)) if configured_pairs else [],
        }

    seeded_pairs: list[str] = []
    target_pairs = configured_pairs if configured_pairs else {str(p).upper() for p in active.keys()}
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


def _load_manifest_active_rows(*, project_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    s = get_settings()
    manifest_candidate = _resolve_optional_path(str(s.model_activation_manifest), project_root)
    if manifest_candidate is None:
        return {}, {"present": False, "path": str(s.model_activation_manifest)}
    try:
        payload = json.loads(manifest_candidate.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, {"present": True, "path": str(manifest_candidate), "error": f"manifest_parse_error:{type(exc).__name__}"}
    active = dict((payload or {}).get("active_model_sets") or {})
    out: dict[str, dict[str, Any]] = {}
    for pair, row in active.items():
        pair_up = str(pair).upper().strip()
        if not pair_up:
            continue
        item = dict(row or {})
        if not bool(item.get("enabled", True)):
            continue
        out[pair_up] = item
    return out, {"present": True, "path": str(manifest_candidate)}


def _normalized_registry_path(raw: str, *, project_root: Path) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    resolved = _resolve_optional_path(txt, project_root)
    if resolved is not None:
        return str(resolved)
    return txt.replace("\\", "/")


def _common_registry_root(paths: list[str]) -> str:
    roots = {str(Path(p).parent) for p in paths if str(p).strip()}
    if not roots:
        return ""
    if len(roots) == 1:
        return next(iter(roots))
    return "mixed"


def _activation_consistency(
    *,
    svc: Any,
    project_root: Path,
    configured_pairs: list[str],
    loaded_model_sets: dict[str, LoadedModelSet],
) -> dict[str, Any]:
    manifest_rows, manifest_meta = _load_manifest_active_rows(project_root=project_root)
    db_rows = svc.get_active_model_sets(enabled_only=True)
    configured = {str(pair).upper().strip() for pair in list(configured_pairs)}
    manifest_pairs = {pair for pair in manifest_rows.keys() if pair in configured}
    db_pairs = {str(pair).upper().strip() for pair in db_rows.keys() if str(pair).upper().strip() in configured}
    loaded_pairs = {str(pair).upper().strip() for pair in loaded_model_sets.keys() if str(pair).upper().strip() in configured}

    manifest_db_mismatch: list[str] = []
    runtime_db_mismatch: list[str] = []
    for pair in sorted(configured):
        manifest_row = dict(manifest_rows.get(pair) or {})
        db_row = dict(db_rows.get(pair) or {})
        manifest_path = _normalized_registry_path(str(manifest_row.get("registry_path") or ""), project_root=project_root)
        db_path = _normalized_registry_path(str(db_row.get("registry_path") or ""), project_root=project_root)
        if bool(manifest_row) != bool(db_row):
            manifest_db_mismatch.append(pair)
        elif manifest_row and db_row and manifest_path != db_path:
            manifest_db_mismatch.append(pair)

        loaded_row = loaded_model_sets.get(pair)
        if loaded_row is None:
            runtime_db_mismatch.append(pair)
            continue
        loaded_path = _normalized_registry_path(str(loaded_row.registry_path or ""), project_root=project_root)
        if not db_row or loaded_path != db_path:
            runtime_db_mismatch.append(pair)

    runtime_registry_paths = [
        _normalized_registry_path(str(item.registry_path or ""), project_root=project_root)
        for item in loaded_model_sets.values()
    ]
    return {
        "manifest": dict(manifest_meta),
        "active_manifest_matches_db": len(manifest_db_mismatch) == 0,
        "runtime_loaded_matches_db": len(runtime_db_mismatch) == 0,
        "activation_mismatch_pairs": sorted(list(set(manifest_db_mismatch) | set(runtime_db_mismatch))),
        "manifest_db_mismatch_pairs": sorted(manifest_db_mismatch),
        "runtime_db_mismatch_pairs": sorted(runtime_db_mismatch),
        "configured_pairs": sorted(list(configured)),
        "manifest_active_pairs": sorted(list(manifest_pairs)),
        "db_active_pairs": sorted(list(db_pairs)),
        "runtime_loaded_pairs": sorted(list(loaded_pairs)),
        "active_pair_count": int(len(configured)),
        "active_registry_root": _common_registry_root(runtime_registry_paths),
    }


def _startup_inference_dry_run(
    *,
    store: ParquetStore,
    raw_store: ParquetStore,
    pairs: list[str],
    model_sets: dict[str, LoadedModelSet],
    feature_timeframes: list[str],
    regime_timeframe: str,
    swing_timeframe: str,
    intraday_timeframe: str,
    progress_cb: Callable[[str, int, int], None] | None = None,
) -> tuple[dict[str, LoadedModelSet], dict[str, dict[str, Any]]]:
    ready_model_sets: dict[str, LoadedModelSet] = {}
    startup_results: dict[str, dict[str, Any]] = {}
    intraday_cache: dict[tuple[str, str, str], pd.DataFrame] = {}

    total_pairs = int(len(pairs))
    for index, pair in enumerate(pairs, start=1):
        if progress_cb is not None:
            progress_cb(str(pair), int(index), int(total_pairs))
        loaded = model_sets.get(pair)
        if loaded is None:
            startup_results[pair] = {
                "ok": False,
                "reason": "model_not_loaded",
                "model_set_id": "",
                "registry_path": "",
            }
            continue

        pair_rows: dict[str, pd.DataFrame] = {}
        missing_frames: list[str] = []
        for timeframe in feature_timeframes:
            row = _latest_feature_row(store=store, pair=pair, timeframe=timeframe)
            if row.empty:
                missing_frames.append(timeframe)
            else:
                pair_rows[timeframe] = row
        if missing_frames:
            startup_results[pair] = {
                "ok": False,
                "reason": f"missing_features:{','.join(missing_frames)}",
                "model_set_id": str(loaded.model_set_id),
                "registry_path": str(loaded.registry_path),
            }
            continue

        pair_rows = _prepare_pair_rows_for_scoring(
            raw_store=raw_store,
            pair=pair,
            loaded=loaded,
            pair_rows=pair_rows,
            swing_timeframe=swing_timeframe,
            intraday_timeframe=intraday_timeframe,
            all_pairs=pairs,
            intraday_cache=intraday_cache,
        )

        try:
            signal = loaded.scorer.score(
                regime_row=pair_rows[regime_timeframe],
                swing_row=pair_rows[swing_timeframe],
                intraday_row=pair_rows[intraday_timeframe],
                meta_row=pair_rows[intraday_timeframe],
                spread_bps=0.0,
                expected_edge_bps=0.0,
                spread_unit_source="startup_dry_run",
            )
            lifecycle_row = _build_lifecycle_row(
                row=pair_rows[intraday_timeframe],
                positions=[],
                total_position_count=0,
                loop_ts=time.time(),
                timeframe=str(intraday_timeframe),
            )
            exit_selected = "hold"
            exit_score = 0.0
            reversal_failure_prob = 0.0
            reversal_opportunity_prob = 0.0
            if loaded.exit_model is not None:
                exit_diag = _score_exit_policy_model(
                    loaded.exit_model,
                    lifecycle_row,
                    action_labels=loaded.exit_action_labels,
                )
                exit_selected = str(exit_diag.get("selected") or "hold")
                exit_score = float(exit_diag.get("score") or 0.0)
            if loaded.reversal_failure_model is not None:
                reversal_failure_prob = _score_binary_lifecycle_model(loaded.reversal_failure_model, lifecycle_row)
            if loaded.reversal_opportunity_model is not None:
                reversal_opportunity_prob = _score_binary_lifecycle_model(loaded.reversal_opportunity_model, lifecycle_row)
            startup_results[pair] = {
                "ok": True,
                "reason": "ok",
                "model_set_id": str(loaded.model_set_id),
                "registry_path": str(loaded.registry_path),
                "trade_prob": float(signal.trade_prob),
                "side": str(signal.side),
                "has_exit_model": bool(loaded.has_exit_model),
                "has_reversal_models": bool(loaded.has_reversal_models),
                "lifecycle_activation_mode": str(loaded.lifecycle_activation_mode),
                "exit_action_selected": str(exit_selected),
                "exit_action_score": float(exit_score),
                "reversal_failure_prob": float(reversal_failure_prob),
                "reversal_opportunity_prob": float(reversal_opportunity_prob),
            }
            ready_model_sets[pair] = loaded
        except Exception as exc:
            startup_results[pair] = {
                "ok": False,
                "reason": f"inference_error:{type(exc).__name__}",
                "error": str(exc),
                "model_set_id": str(loaded.model_set_id),
                "registry_path": str(loaded.registry_path),
                "has_exit_model": bool(loaded.has_exit_model),
                "has_reversal_models": bool(loaded.has_reversal_models),
            }

    return ready_model_sets, startup_results


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


def _merge_latest_row(base_row: pd.DataFrame, latest_row: pd.DataFrame) -> pd.DataFrame:
    if base_row.empty:
        return latest_row.copy()
    if latest_row.empty:
        return base_row.copy()
    merged = base_row.reset_index(drop=True).copy()
    src = latest_row.reset_index(drop=True).iloc[0]
    for col in latest_row.columns:
        merged.loc[0, col] = src.get(col)
    return merged


def _enrich_row_from_raw_lifecycle(
    *,
    raw_store: ParquetStore,
    pair: str,
    timeframe: str,
    row: pd.DataFrame,
    required_columns: list[str] | None,
) -> pd.DataFrame:
    required = [str(col) for col in list(required_columns or []) if str(col).strip()]
    if row.empty or not required:
        return row
    missing = [col for col in required if col not in row.columns]
    if not missing:
        return row

    provider = get_settings().normalized_data_provider
    raw_df = raw_store.read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
    if raw_df.empty:
        return row

    enriched = add_fx_lifecycle_features(raw_df)
    if enriched.empty:
        return row
    latest = enriched.sort_values("ts").tail(1).copy()
    return _merge_latest_row(row, latest)


def _enrich_intraday_row_from_raw_contract(
    *,
    raw_store: ParquetStore,
    pair: str,
    timeframe: str,
    row: pd.DataFrame,
    required_columns: list[str] | None,
    all_pairs: list[str],
    cache: dict[tuple[str, str, str], pd.DataFrame] | None = None,
) -> pd.DataFrame:
    required = [str(col) for col in list(required_columns or []) if str(col).strip()]
    if row.empty or not required:
        return row
    missing = [col for col in required if col not in row.columns]
    if not missing:
        return row

    ts_key = str(row.iloc[0].get("ts", "") or "")
    cache_key = (str(pair).upper(), str(timeframe).upper(), ts_key)
    if cache is not None and cache_key in cache:
        return _merge_latest_row(row, cache[cache_key])

    provider = get_settings().normalized_data_provider
    enriched, _ = build_multi_tf_rows(
        pair=str(pair).upper(),
        raw_store_root=Path(raw_store.root),
        provider=provider,
        anchor_timeframe=str(timeframe).upper(),
        context_timeframes=["M15", "H1", "H4", "D"],
        all_pairs=list(all_pairs),
    )
    if enriched.empty:
        return row
    latest = enriched.sort_values("ts").tail(1).copy()
    if cache is not None:
        cache[cache_key] = latest.copy()
    return _merge_latest_row(row, latest)


def _prepare_pair_rows_for_scoring(
    *,
    raw_store: ParquetStore,
    pair: str,
    loaded: LoadedModelSet,
    pair_rows: dict[str, pd.DataFrame],
    swing_timeframe: str,
    intraday_timeframe: str,
    all_pairs: list[str],
    intraday_cache: dict[tuple[str, str, str], pd.DataFrame] | None = None,
) -> dict[str, pd.DataFrame]:
    out = dict(pair_rows)
    swing_required = list(getattr(loaded.scorer.swing_model, "feature_columns", []) or [])
    if swing_timeframe in out:
        out[swing_timeframe] = _enrich_row_from_raw_lifecycle(
            raw_store=raw_store,
            pair=pair,
            timeframe=swing_timeframe,
            row=out[swing_timeframe],
            required_columns=swing_required,
        )
    intraday_required = _required_model_feature_columns(
        loaded.scorer.intraday_model,
        loaded.exit_model,
        loaded.reversal_failure_model,
        loaded.reversal_opportunity_model,
    )
    if intraday_timeframe in out:
        out[intraday_timeframe] = _enrich_intraday_row_from_raw_contract(
            raw_store=raw_store,
            pair=pair,
            timeframe=intraday_timeframe,
            row=out[intraday_timeframe],
            required_columns=intraday_required,
            all_pairs=all_pairs,
            cache=intraday_cache,
        )
    return out


def _build_lifecycle_row(
    *,
    row: pd.DataFrame,
    positions: list[dict[str, Any]],
    total_position_count: int,
    loop_ts: float,
    timeframe: str,
) -> pd.DataFrame:
    out = row.copy()
    timeframe_secs = max(1, _timeframe_to_seconds(timeframe))
    oldest_open_time = _position_oldest_open_time(positions)
    time_in_trade_bars = 0.0
    if positions and oldest_open_time > 0.0:
        time_in_trade_bars = max(0.0, (float(loop_ts) - float(oldest_open_time)) / float(timeframe_secs))
    out.loc[:, "time_in_trade_bars"] = float(time_in_trade_bars)
    out.loc[:, "open_position_count"] = float(max(0, int(total_position_count)))
    if "live_edge_decay" not in out.columns:
        out.loc[:, "live_edge_decay"] = float(_safe_float(out.iloc[0].get("edge_decay_12"), 0.0))
    if "h1_available" not in out.columns:
        out.loc[:, "h1_available"] = float(1.0 if any(str(col).startswith("h1_") for col in out.columns) else 0.0)
    return out


def _score_exit_policy_model(model: Any, row: pd.DataFrame, *, action_labels: dict[int, str]) -> dict[str, Any]:
    if model is None:
        return {"selected": "hold", "score": 0.0, "probs": {}}
    proba = model.predict_proba(row)
    if proba.empty:
        return {"selected": "hold", "score": 0.0, "probs": {}}
    probs: dict[str, float] = {}
    for col, value in dict(proba.iloc[0]).items():
        label = str(col)
        if str(col).startswith("p"):
            try:
                label = action_labels.get(int(str(col)[1:]), label)
            except Exception:
                label = str(col)
        probs[str(label)] = float(value)
    selected = max(probs.items(), key=lambda item: float(item[1]))[0] if probs else "hold"
    return {
        "selected": str(selected),
        "score": float(probs.get(selected, 0.0)),
        "probs": probs,
    }


def _score_binary_lifecycle_model(model: Any, row: pd.DataFrame) -> float:
    if model is None:
        return 0.0
    proba = model.predict_proba(row)
    if proba.empty:
        return 0.0
    return float(_safe_float(proba.iloc[0].get("p1"), 0.0))


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
    for raw in positions:
        pos = dict(raw or {})
        for key in ("type", "order_type", "position_type"):
            value = pos.get(key)
            if value is None or str(value).strip() == "":
                continue
            try:
                typ = int(float(value))
            except Exception:
                typ = -1
            if typ == 0:
                return "long"
            if typ == 1:
                return "short"
            txt = str(value).strip().lower()
            if txt in {"buy", "long", "op_buy"}:
                return "long"
            if txt in {"sell", "short", "op_sell"}:
                return "short"
        for key in ("side", "position_side", "direction", "cmd"):
            txt = str(pos.get(key) or "").strip().lower()
            if txt in {"buy", "long"}:
                return "long"
            if txt in {"sell", "short"}:
                return "short"
    return "flat"


def _reversal_blocking_reasons(reasons: list[str]) -> list[str]:
    blocked = []
    for reason in list(reasons or []):
        txt = str(reason or "").strip()
        if not txt:
            continue
        if txt in {"pair_exposure_cap", "portfolio_exposure_cap"}:
            continue
        blocked.append(txt)
    return list(dict.fromkeys(blocked))


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
    _startup_log(f"begin pairs={len(pairs)} bridge={s.mt4_bridge_url} db={s.database_url}")

    runtime_boot_id = str(uuid.uuid4())
    runtime_booted_at = pd.Timestamp.utcnow().isoformat()
    startup_state = _runtime_startup_state(
        boot_id=runtime_boot_id,
        booted_at=runtime_booted_at,
        runtime_pid=int(os.getpid()),
        phase="boot",
        pending_command_policy="purge_and_mark_stale",
    )
    manifest_seed_diag: dict[str, Any] = {}
    model_load_diag: dict[str, int] = {"model_load_timeouts": 0, "model_load_errors": 0}
    startup_inference: dict[str, dict[str, Any]] = {}
    startup_disabled_pairs: list[str] = []
    activation_consistency: dict[str, Any] = {}
    startup_runtime_diag: dict[str, Any] = {
        "pending_command_policy": "purge_and_mark_stale",
        "pending_commands_purged": 0,
        "manifest_seed": {},
        "feature_bootstrap": {},
        "live_feature_refresh": {},
        "startup_inference": {},
        "startup_inference_failures": 0,
        "startup_disabled_pairs": [],
        "activation_consistency": {},
    }
    runtime_running = False

    provider = str(s.normalized_data_provider)
    store = ParquetStore(Path(feature_root))
    raw_store = ParquetStore(Path(s.project_root) / "data" / "raw")
    regime_timeframe = str(s.regime_timeframe).upper()
    swing_timeframe = str(s.swing_timeframe).upper()
    intraday_timeframe = str(s.intraday_timeframe).upper()
    feature_timeframes = _required_feature_timeframes()
    last_action_key: dict[str, str] = {}
    intraday_enrichment_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
    feature_bootstrap: dict[str, dict[str, dict[str, Any]]] = {}
    live_bar_refresh_cache: dict[str, str] = {}
    live_refresh_diag: dict[str, dict[str, Any]] = {}
    try:
        svc = RuntimeService(
            database_url=s.database_url,
            default_session_id=s.default_session_id,
            command_ttl_secs=s.command_ttl_secs,
            requeue_age_secs=s.startup_requeue_age_secs,
            db_connect_retries=s.db_connect_retries,
        )
        _startup_log("runtime_service_ready")
        svc.patch_state(
            _runtime_boot_reset_patch(
                runtime_profile=str(s.policy_version),
                equity_seed=float(equity),
                pairs=pairs,
                startup_state=startup_state,
                runtime_diag=startup_runtime_diag,
            )
        )
        _startup_log("state_patched_boot")
        pending_purged = int(svc.purge_pending_commands(reason="runtime_restart_purged"))
        startup_runtime_diag["pending_commands_purged"] = int(pending_purged)
        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="boot",
            runtime_diag=startup_runtime_diag,
        )
        _startup_log(f"pending_commands_purged count={pending_purged}")

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="manifest_seed",
            runtime_diag=startup_runtime_diag,
        )
        manifest_seed_diag = _seed_active_model_sets_from_manifest(svc=svc, project_root=s.project_root)
        startup_runtime_diag["manifest_seed"] = dict(manifest_seed_diag)
        _startup_log(f"manifest_seed reason={manifest_seed_diag.get('reason')} seeded={manifest_seed_diag.get('seeded')}")

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="model_load",
            runtime_diag=startup_runtime_diag,
        )
        model_sets, model_load_diag = _load_model_sets(
            pairs=pairs,
            require_all=bool(s.require_active_models),
            project_root=s.project_root,
        )
        startup_runtime_diag["model_load_timeouts"] = int(model_load_diag.get("model_load_timeouts", 0))
        startup_runtime_diag["model_load_errors"] = int(model_load_diag.get("model_load_errors", 0))
        _startup_log(
            "model_load "
            + f"loaded={len(model_sets)} "
            + f"timeouts={model_load_diag.get('model_load_timeouts', 0)} "
            + f"errors={model_load_diag.get('model_load_errors', 0)}"
        )
        if bool(s.require_active_models) and len(model_sets) != len(pairs):
            missing = [p for p in pairs if p not in model_sets]
            raise RuntimeError(f"active model load failed for pairs: {','.join(missing)}")

        for index, pair in enumerate(pairs, start=1):
            startup_state = _touch_runtime_startup_progress(
                svc=svc,
                startup_state=startup_state,
                phase="initial_refresh",
                phase_pair=str(pair),
                phase_index=int(index),
                phase_total=int(len(pairs)),
                runtime_diag=startup_runtime_diag,
            )
            _startup_log(f"initial_refresh pair={pair}")
            pair_bootstrap = feature_bootstrap.setdefault(str(pair), {})
            for timeframe in feature_timeframes:
                row = _latest_feature_row(store=store, pair=pair, timeframe=timeframe)
                if row.empty:
                    ok, detail = _bootstrap_pair_features_from_csv(store=store, pair=pair, timeframe=timeframe)
                    pair_bootstrap[timeframe] = {"attempted": True, "ok": bool(ok), "detail": str(detail)}
            live_refresh_diag[pair] = _refresh_live_pair_market_data(
                bridge_url=s.mt4_bridge_url,
                raw_store=raw_store,
                feature_store=store,
                pair=pair,
                provider=provider,
                latest_bar_cache=live_bar_refresh_cache,
            )
            startup_runtime_diag["feature_bootstrap"] = dict(feature_bootstrap)
            startup_runtime_diag["live_feature_refresh"] = dict(live_refresh_diag)
            _startup_log(f"initial_refresh_done pair={pair} reason={live_refresh_diag[pair].get('reason')}")

        _startup_log("startup_inference_begin")

        def _startup_inference_progress(pair_name: str, pair_index: int, pair_total: int) -> None:
            nonlocal startup_state
            startup_state = _touch_runtime_startup_progress(
                svc=svc,
                startup_state=startup_state,
                phase="startup_inference",
                phase_pair=str(pair_name),
                phase_index=int(pair_index),
                phase_total=int(pair_total),
                runtime_diag=startup_runtime_diag,
            )

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="startup_inference",
            phase_total=int(len(pairs)),
            runtime_diag=startup_runtime_diag,
        )
        model_sets, startup_inference = _startup_inference_dry_run(
            store=store,
            raw_store=raw_store,
            pairs=pairs,
            model_sets=model_sets,
            feature_timeframes=feature_timeframes,
            regime_timeframe=regime_timeframe,
            swing_timeframe=swing_timeframe,
            intraday_timeframe=intraday_timeframe,
            progress_cb=_startup_inference_progress,
        )
        _startup_log("startup_inference_done")
        startup_disabled_pairs = sorted([pair for pair, result in startup_inference.items() if not bool(result.get("ok"))])
        startup_runtime_diag["startup_inference"] = dict(startup_inference)
        startup_runtime_diag["startup_inference_failures"] = int(len(startup_disabled_pairs))
        startup_runtime_diag["startup_disabled_pairs"] = list(startup_disabled_pairs)

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="activation_consistency",
            runtime_diag=startup_runtime_diag,
        )
        activation_consistency = _activation_consistency(
            svc=svc,
            project_root=s.project_root,
            configured_pairs=pairs,
            loaded_model_sets=model_sets,
        )
        startup_runtime_diag["activation_consistency"] = dict(activation_consistency)
        _startup_log(
            "activation_consistency "
            + f"manifest_db={activation_consistency.get('active_manifest_matches_db')} "
            + f"runtime_db={activation_consistency.get('runtime_loaded_matches_db')}"
        )

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="readying_state",
            runtime_diag=startup_runtime_diag,
        )
        _startup_log("state_patched_starting")
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}:{exc}" if str(exc) else str(type(exc).__name__)
        _startup_log(
            "startup_failed "
            + f"phase={startup_state.get('phase')} "
            + f"pair={startup_state.get('phase_pair')} "
            + f"reason={failure_reason}"
        )
        if "svc" in locals():
            try:
                _record_runtime_startup_failure(
                    svc=svc,
                    startup_state=startup_state,
                    failure_reason=failure_reason,
                    runtime_diag=startup_runtime_diag,
                )
            except Exception as record_exc:
                _startup_log(f"startup_failure_record_error {type(record_exc).__name__}:{record_exc}")
        raise

    while True:
        loop_ts = time.time()
        loop_t0 = time.perf_counter()
        if not runtime_running:
            _startup_log("main_loop_enter")
        progress_touch_t0 = time.perf_counter()
        if runtime_running:
            startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
        else:
            startup_state = _touch_runtime_startup_progress(
                svc=svc,
                startup_state=startup_state,
                phase="main_loop",
                runtime_diag=startup_runtime_diag,
            )
        bridge_ready = fetch_bridge_ready(s.mt4_bridge_url)
        ticks = fetch_bridge_ticks(s.mt4_bridge_url)
        for pair in pairs:
            if (time.perf_counter() - progress_touch_t0) >= 5.0:
                if runtime_running:
                    startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
                else:
                    startup_state = _touch_runtime_startup_progress(
                        svc=svc,
                        startup_state=startup_state,
                        phase="main_loop",
                        runtime_diag=startup_runtime_diag,
                    )
                progress_touch_t0 = time.perf_counter()
            tick = dict((ticks.get(pair, {}) if isinstance(ticks, dict) else {}) or {})
            bucket = _tick_bucket_start(tick=tick, timeframe=intraday_timeframe)
            if bucket is None:
                continue
            if live_bar_refresh_cache.get(str(pair).upper()) == str(pd.to_datetime(float(bucket), unit="s", utc=True)):
                continue
            live_refresh_diag[pair] = _refresh_live_pair_market_data(
                bridge_url=s.mt4_bridge_url,
                raw_store=raw_store,
                feature_store=store,
                pair=pair,
                provider=provider,
                latest_bar_cache=live_bar_refresh_cache,
            )
        state = svc.get_state()
        governance = dict(state.get("governance", {}) or {})
        paused = bool(governance.get("paused", False))
        mt4_fresh = bool(bridge_ready.get("mt4_fresh")) if bridge_ready else _state_mt4_fresh(state)
        ticks_fresh = bool(bridge_ready.get("ticks_fresh")) if bridge_ready else bool(ticks)

        decisions: list[dict[str, Any]] = []
        rejection_counts: dict[str, int] = {}
        pair_eval_time_ms: dict[str, float] = {}
        inference_errors = 0
        planned_entry_lots, lot_sizing_diag = _entry_order_lots(state=state, settings=s, equity_seed=float(equity))

        for pair in pairs:
            if (time.perf_counter() - progress_touch_t0) >= 5.0:
                if runtime_running:
                    startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
                else:
                    startup_state = _touch_runtime_startup_progress(
                        svc=svc,
                        startup_state=startup_state,
                        phase="main_loop",
                        runtime_diag=startup_runtime_diag,
                    )
                progress_touch_t0 = time.perf_counter()
            pair_t0 = time.perf_counter()
            loaded = model_sets.get(pair)
            startup_status = dict(startup_inference.get(pair) or {})
            if loaded is None:
                reason = str(startup_status.get("reason") or "missing_active_model_set")
                if startup_status and not bool(startup_status.get("ok")) and not str(reason).startswith("startup_"):
                    reason = f"startup_{reason}"
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": {"pair": pair, "runtime": "fxstack", "startup_inference": startup_status},
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

            pair_rows = _prepare_pair_rows_for_scoring(
                raw_store=raw_store,
                pair=pair,
                loaded=loaded,
                pair_rows=pair_rows,
                swing_timeframe=swing_timeframe,
                intraday_timeframe=intraday_timeframe,
                all_pairs=pairs,
                intraday_cache=intraday_enrichment_cache,
            )
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
            ts_value = str(intraday_row.iloc[0].get("ts", ""))
            feature_bar = _feature_bar_freshness(
                ts_value=ts_value,
                loop_ts=float(loop_ts),
                timeframe=str(intraday_timeframe),
            )
            if not positions and not mt4_fresh:
                decision_reasons.append("mt4_stale")
            if not positions and not ticks_fresh:
                decision_reasons.append("tick_feed_stale")
            if not positions and not bool(tick):
                decision_reasons.append("missing_live_tick")
            if not positions and bool(feature_bar.get("stale")):
                decision_reasons.append(str(feature_bar.get("reason") or "stale_feature_bar"))
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
            desired_side = "long" if side == "BUY" else "short"
            reversal_blocking_reasons = _reversal_blocking_reasons(decision_reasons)
            reversal_context_active = (
                desired_side != "flat" and str(pos_side) != "flat" and desired_side != str(pos_side)
            )
            lifecycle_soft_degrade_reasons: list[str] = []
            if not bool(loaded.has_exit_model):
                lifecycle_soft_degrade_reasons.append("no_exit_model")
            if not bool(loaded.has_reversal_models):
                lifecycle_soft_degrade_reasons.append("no_reversal_model")

            enqueue_out: dict[str, Any] = {"status": "skipped"}
            lifecycle_action = "hold"
            lifecycle_action_score = 0.0
            lifecycle_reason = "hold"
            action_tag = "hold"
            close_lots = 0.0
            sl_price = 0.0
            lifecycle_row = _build_lifecycle_row(
                row=intraday_row,
                positions=positions,
                total_position_count=total_count,
                loop_ts=float(loop_ts),
                timeframe=str(intraday_timeframe),
            )
            exit_action_selected = "hold"
            exit_action_score = 0.0
            exit_action_probs: dict[str, float] = {}
            reversal_failure_prob = 0.0
            reversal_opportunity_prob = 0.0
            lifecycle_inference_error = ""

            if positions and bool(s.enable_lifecycle_actions):
                try:
                    if loaded.exit_model is not None:
                        exit_diag = _score_exit_policy_model(
                            loaded.exit_model,
                            lifecycle_row,
                            action_labels=loaded.exit_action_labels,
                        )
                        exit_action_selected = str(exit_diag.get("selected") or "hold")
                        exit_action_score = float(exit_diag.get("score") or 0.0)
                        exit_action_probs = {
                            str(k): float(v) for k, v in dict(exit_diag.get("probs") or {}).items()
                        }
                    if loaded.reversal_failure_model is not None:
                        reversal_failure_prob = _score_binary_lifecycle_model(loaded.reversal_failure_model, lifecycle_row)
                    if loaded.reversal_opportunity_model is not None:
                        reversal_opportunity_prob = _score_binary_lifecycle_model(
                            loaded.reversal_opportunity_model,
                            lifecycle_row,
                        )
                except Exception as exc:
                    lifecycle_inference_error = f"{type(exc).__name__}:{exc}"
                    lifecycle_soft_degrade_reasons.append(f"lifecycle_inference_error:{type(exc).__name__}")

            if reversal_context_active and loaded.has_reversal_models:
                if float(reversal_failure_prob) < float(s.reversal_failure_min_prob):
                    reversal_blocking_reasons.append("reversal_failure_below_threshold")
                if float(reversal_opportunity_prob) < float(s.reversal_opportunity_min_prob):
                    reversal_blocking_reasons.append("reversal_opportunity_below_threshold")
            reversal_blocking_reasons = list(dict.fromkeys(reversal_blocking_reasons))
            reversal_ready = (
                bool(reversal_context_active)
                and bool(signal.allowed)
                and len(reversal_blocking_reasons) == 0
                and (
                    not loaded.has_reversal_models
                    or (
                        float(reversal_failure_prob) >= float(s.reversal_failure_min_prob)
                        and float(reversal_opportunity_prob) >= float(s.reversal_opportunity_min_prob)
                    )
                )
            )

            # Action precedence:
            # 1) hard risk/time-stop emergency
            # 2) reversal-exit decision
            # 3) exit-policy action
            # 4) adjust-stop action
            # 4) entry (flat only)
            if positions and float(s.hard_time_stop_secs) > 0.0:
                oldest_open_time = _position_oldest_open_time(positions)
                if oldest_open_time > 0.0 and (float(loop_ts) - float(oldest_open_time)) >= float(s.hard_time_stop_secs):
                    lifecycle_action = "exit"
                    lifecycle_action_score = 1.0
                    lifecycle_reason = "hard_time_stop"
                    action_tag = "exit"
            if positions and lifecycle_action == "hold" and bool(s.enable_lifecycle_actions):
                if bool(reversal_ready):
                    lifecycle_action = "exit"
                    lifecycle_action_score = float(
                        min(
                            1.0,
                            (float(reversal_failure_prob) + float(reversal_opportunity_prob) + float(signal.trade_prob)) / 3.0,
                        )
                    )
                    lifecycle_reason = "reversal_models_exit"
                    action_tag = "reversal_exit"
            if (
                positions
                and lifecycle_action == "hold"
                and bool(s.enable_lifecycle_actions)
                and bool(loaded.has_exit_model)
            ):
                if (
                    str(exit_action_selected) in {"partial_tp", "exit"}
                    and float(exit_action_score) >= float(s.lifecycle_model_action_min_prob)
                ):
                    first_pos = dict(positions[0] or {})
                    lots_open = float(first_pos.get("lots", 0.0) or 0.0)
                    if str(exit_action_selected) == "partial_tp":
                        lifecycle_action, close_lots = _partial_close_plan(
                            lots_open=lots_open,
                            fraction=float(s.partial_close_fraction),
                            settings=s,
                        )
                        if close_lots > 0.0 and lifecycle_action in {"partial_tp", "exit"}:
                            lifecycle_action_score = float(exit_action_score)
                            lifecycle_reason = (
                                "exit_model_reduce_to_flat" if lifecycle_action == "exit" else "exit_model_partial_tp"
                            )
                            action_tag = "exit" if lifecycle_action == "exit" else "close_partial"
                    elif str(exit_action_selected) == "exit":
                        lifecycle_action = "exit"
                        lifecycle_action_score = float(exit_action_score)
                        lifecycle_reason = "exit_model_exit"
                        action_tag = "exit"
            if (
                positions
                and lifecycle_action == "hold"
                and bool(s.enable_lifecycle_actions)
                and not bool(loaded.has_exit_model)
                and float(signal.trade_prob) < float(s.min_trade_prob * 0.8)
            ):
                first_pos = dict(positions[0] or {})
                lots_open = float(first_pos.get("lots", 0.0) or 0.0)
                lifecycle_action, close_lots = _partial_close_plan(
                    lots_open=lots_open,
                    fraction=float(s.partial_close_fraction),
                    settings=s,
                )
                if close_lots > 0.0 and lifecycle_action in {"partial_tp", "exit"}:
                    lifecycle_action_score = 0.6
                    lifecycle_reason = (
                        "exit_model_reduce_to_flat" if lifecycle_action == "exit" else "exit_model_reduce"
                    )
                    action_tag = "exit" if lifecycle_action == "exit" else "close_partial"
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
            if not positions:
                reversal_ready = False

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
                        "lots": float(planned_entry_lots),
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
                        "registry_path": loaded.registry_path,
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
                        "feature_bar": dict(feature_bar),
                        "entry_lot_sizing": dict(lot_sizing_diag),
                        "startup_inference": startup_status or {"ok": True, "reason": "ok"},
                        "position_side": pos_side,
                        "position_count_pair": int(pair_count),
                        "entry_ready": bool(ready),
                        "entry_blocking_reasons": list(decision_reasons),
                        "reversal_should_exit": bool(reversal_ready),
                        "reversal_context_active": bool(reversal_context_active),
                        "reversal_ready": bool(reversal_ready),
                        "reversal_blocking_reasons": list(reversal_blocking_reasons),
                        "reversal_failure_prob": float(reversal_failure_prob),
                        "reversal_opportunity_prob": float(reversal_opportunity_prob),
                        "reversal_reasons": list(reversal_blocking_reasons),
                        "exit_action_selected": str(exit_action_selected),
                        "exit_action_score": float(exit_action_score),
                        "exit_action_probs": dict(exit_action_probs),
                        "lifecycle_action": str(lifecycle_action),
                        "lifecycle_action_score": float(lifecycle_action_score),
                        "lifecycle_reason": str(lifecycle_reason),
                        "lifecycle_activation_mode": str(loaded.lifecycle_activation_mode),
                        "lifecycle_capabilities": {
                            "has_exit_model": bool(loaded.has_exit_model),
                            "has_reversal_models": bool(loaded.has_reversal_models),
                        },
                        "lifecycle_inference_error": str(lifecycle_inference_error),
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
            "live_feature_refresh": dict(live_refresh_diag),
            "entry_lot_sizing": dict(lot_sizing_diag),
            "startup_inference": dict(startup_inference),
            "startup_inference_failures": int(len(startup_disabled_pairs)),
            "startup_disabled_pairs": list(startup_disabled_pairs),
            "activation_consistency": dict(activation_consistency),
            "manifest_seed": dict(manifest_seed_diag),
        }

        state_patch: dict[str, Any] = {
            "runtime_profile": str(s.policy_version),
            "runtime_last_cycle_ts": float(loop_ts),
            "runtime_status": "running" if runtime_running else "starting",
            "runtime_equity_seed": float(equity),
            "runtime_diag": runtime_diag,
            "runtime_startup": dict(startup_state),
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

        startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
        if not runtime_running:
            runtime_running = True
            _startup_log("main_loop_ready")

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
