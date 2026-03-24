from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from fxstack.live.policy import normalize_spread_bps
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


app = FastAPI(title="fx-quant-stack bridge", version="0.1.0")
settings = get_settings()

from fxstack.api.auth import add_api_key_middleware  # noqa: E402
add_api_key_middleware(app, settings.bridge_api_key)

service = RuntimeService(
    database_url=settings.database_url,
    default_session_id=settings.default_session_id,
    command_ttl_secs=settings.command_ttl_secs,
    requeue_age_secs=settings.startup_requeue_age_secs,
    db_connect_retries=settings.db_connect_retries,
)

_reports_cache: list[dict[str, Any]] = []
_visuals: dict[str, Any] = {}
_market_ticks_mem: dict[str, dict[str, Any]] = {}
_market_tick_history: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=50000))
_workflow_status_cache: tuple[float, dict[str, Any]] | None = None
_WORKFLOW_STATUS_CACHE_TTL_SECS = 1.0


@app.on_event("startup")
async def _bridge_startup() -> None:
    _bridge_bootstrap_reset()


def _utc_now_ts() -> float:
    return float(datetime.now(timezone.utc).timestamp())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _parse_ts(value: Any) -> float:
    if value is None:
        return _utc_now_ts()
    if isinstance(value, (int, float)):
        v = float(value)
        # Heuristic: milliseconds epoch.
        if v > 1e12:
            return v / 1000.0
        return v
    txt = str(value).strip()
    if not txt:
        return _utc_now_ts()
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        return _utc_now_ts()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


def _heartbeat_age_secs(state: dict[str, Any]) -> float | None:
    hb = (state or {}).get("last_heartbeat")
    if hb is None:
        return None
    ts = _parse_ts(hb)
    if ts <= 0:
        return None
    return max(0.0, _utc_now_ts() - ts)


def _prune_tick_memory(*, now_ts: float | None = None) -> None:
    now = float(now_ts if now_ts is not None else _utc_now_ts())
    stale_after = max(30.0, float(settings.bridge_stale_tick_secs) * 10.0)
    drop_symbols: list[str] = []
    for sym, row in list(_market_ticks_mem.items()):
        ts = _safe_float((row or {}).get("ts_epoch"), 0.0)
        if ts <= 0.0 or (now - ts) > stale_after:
            drop_symbols.append(str(sym).upper())
    for sym in drop_symbols:
        _market_ticks_mem.pop(sym, None)
        _market_tick_history.pop(sym, None)


def _fresh_market_ticks() -> dict[str, dict[str, Any]]:
    _prune_tick_memory()
    stale_after = max(1.0, float(settings.bridge_stale_tick_secs))
    now = _utc_now_ts()
    out: dict[str, dict[str, Any]] = {}
    for sym, row in list(_market_ticks_mem.items()):
        item = dict(row or {})
        ts = _safe_float(item.get("ts_epoch"), 0.0)
        if ts <= 0.0:
            continue
        if (now - ts) > stale_after:
            continue
        out[str(sym).upper()] = item
    return out


def _tick_liveness() -> dict[str, Any]:
    _prune_tick_memory()
    stale_after = max(1.0, float(settings.bridge_stale_tick_secs))
    if not _market_ticks_mem:
        return {
            "ticks_present": False,
            "ticks_fresh": False,
            "tick_symbols_count": 0,
            "tick_max_age_secs": None,
            "tick_stale_after_secs": float(stale_after),
            "tick_status": "missing",
            "tick_reason": "no_live_ticks",
        }
    now = _utc_now_ts()
    ages: list[float] = []
    for row in _market_ticks_mem.values():
        ts = _safe_float((row or {}).get("ts_epoch"), 0.0)
        if ts > 0.0:
            ages.append(max(0.0, now - ts))
    if not ages:
        return {
            "ticks_present": True,
            "ticks_fresh": False,
            "tick_symbols_count": int(len(_market_ticks_mem)),
            "tick_max_age_secs": None,
            "tick_stale_after_secs": float(stale_after),
            "tick_status": "invalid",
            "tick_reason": "tick_timestamp_missing",
        }
    max_age = float(max(ages))
    fresh = bool(max_age <= stale_after)
    return {
        "ticks_present": True,
        "ticks_fresh": fresh,
        "tick_symbols_count": int(len(_market_ticks_mem)),
        "tick_max_age_secs": float(max_age),
        "tick_stale_after_secs": float(stale_after),
        "tick_status": "fresh" if fresh else "stale",
        "tick_reason": "ok" if fresh else "tick_feed_stale",
    }


def _runtime_cycle_age_secs(state: dict[str, Any]) -> float | None:
    ts = (state or {}).get("runtime_last_cycle_ts")
    if ts is None:
        return None
    parsed = _parse_ts(ts)
    if parsed <= 0:
        return None
    return max(0.0, _utc_now_ts() - parsed)


def _state_with_liveness(raw: dict[str, Any]) -> dict[str, Any]:
    state = dict(raw or {})
    stale_after = max(1.0, float(settings.bridge_stale_heartbeat_secs))
    age = _heartbeat_age_secs(state)
    runtime_cycle_age_secs = _runtime_cycle_age_secs(state)
    runtime_startup_progress_stale_secs = max(1.0, float(settings.runtime_startup_progress_stale_secs))
    status = str(state.get("system_status", "unknown") or "unknown").strip().lower()
    if age is None:
        if status in {"connected", "stale", "disconnected"}:
            status = "disconnected"
        elif status in {"", "unknown"}:
            status = "starting"
    elif age > stale_after:
        status = "stale"
    elif status in {"", "unknown", "starting", "stale", "disconnected"}:
        status = "connected"
    state["system_status"] = status
    state["heartbeat_age_secs"] = None if age is None else float(age)
    state["heartbeat_stale_after_secs"] = float(stale_after)
    state["runtime_cycle_age_secs"] = runtime_cycle_age_secs
    state["runtime_cycle_stale_after_secs"] = 30.0
    raw_runtime_startup = dict(state.get("runtime_startup") or {})
    runtime_phase = str(raw_runtime_startup.get("phase") or "").strip().lower()
    runtime_phase_pair = str(raw_runtime_startup.get("phase_pair") or "").strip().upper()
    runtime_phase_index = int(raw_runtime_startup.get("phase_index", 0) or 0)
    runtime_phase_total = int(raw_runtime_startup.get("phase_total", 0) or 0)
    runtime_boot_id = str(raw_runtime_startup.get("boot_id") or "").strip()
    runtime_booted_at = raw_runtime_startup.get("booted_at")
    runtime_failure_reason = str(raw_runtime_startup.get("failure_reason") or "").strip()
    runtime_failed_at = raw_runtime_startup.get("failed_at")
    runtime_last_progress_ts = raw_runtime_startup.get("last_progress_ts")
    runtime_last_progress_age_secs = None
    if runtime_last_progress_ts not in (None, ""):
        parsed_progress_ts = _parse_ts(runtime_last_progress_ts)
        if parsed_progress_ts > 0.0:
            runtime_last_progress_age_secs = max(0.0, _utc_now_ts() - parsed_progress_ts)
    normalized_runtime_startup = {
        "boot_id": runtime_boot_id,
        "booted_at": runtime_booted_at,
        "runtime_pid": raw_runtime_startup.get("runtime_pid"),
        "phase": runtime_phase,
        "phase_pair": runtime_phase_pair,
        "phase_index": runtime_phase_index,
        "phase_total": runtime_phase_total,
        "last_progress_ts": runtime_last_progress_ts,
        "last_progress_age_secs": runtime_last_progress_age_secs,
        "failure_reason": runtime_failure_reason,
        "failed_at": runtime_failed_at,
        "pending_command_policy": str(raw_runtime_startup.get("pending_command_policy") or "").strip(),
    }
    state["runtime_startup"] = normalized_runtime_startup
    state["runtime_phase"] = runtime_phase
    state["runtime_phase_pair"] = runtime_phase_pair
    state["runtime_phase_index"] = runtime_phase_index
    state["runtime_phase_total"] = runtime_phase_total
    state["runtime_boot_id"] = runtime_boot_id
    state["runtime_booted_at"] = runtime_booted_at
    state["runtime_last_progress_age_secs"] = runtime_last_progress_age_secs
    state["runtime_failure_reason"] = runtime_failure_reason
    state["runtime_failed_at"] = runtime_failed_at
    tick_state = _tick_liveness()
    state.update(tick_state)
    raw_runtime_status = str(state.get("runtime_status") or "unknown").strip().lower()
    if raw_runtime_status == "running" and (runtime_cycle_age_secs is None or float(runtime_cycle_age_secs) > 30.0):
        state["runtime_status"] = "stale"
    elif raw_runtime_status == "starting" and (
        runtime_last_progress_age_secs is not None
        and float(runtime_last_progress_age_secs) > runtime_startup_progress_stale_secs
    ):
        state["runtime_status"] = "stalled"
    else:
        state["runtime_status"] = raw_runtime_status
    configured_pairs = [
        str(pair).strip().upper()
        for pair in list(state.get("configured_pairs") or settings.pairs)
        if str(pair).strip()
    ]
    state["configured_pairs"] = configured_pairs
    state["active_pair_count"] = int(len(configured_pairs))

    runtime_diag = dict(state.get("runtime_diag") or {})
    activation_consistency = dict(runtime_diag.get("activation_consistency") or {})
    startup_inference = {
        str(pair).strip().upper(): dict(item or {})
        for pair, item in dict(runtime_diag.get("startup_inference") or {}).items()
        if str(pair).strip()
    }
    state["activation_consistency"] = activation_consistency
    state["startup_inference"] = startup_inference
    state["startup_inference_failures"] = int(runtime_diag.get("startup_inference_failures", 0) or 0)

    symbol_readiness = {
        str(pair).strip().upper(): dict(item or {})
        for pair, item in dict(state.get("symbol_readiness") or {}).items()
        if str(pair).strip()
    }
    state["symbol_readiness"] = symbol_readiness
    if symbol_readiness:
        broker_symbol_failures = sorted(
            [pair for pair in configured_pairs if not bool(dict(symbol_readiness.get(pair) or {}).get("supported"))]
        )
        broker_symbol_ready_count = int(
            sum(1 for pair in configured_pairs if bool(dict(symbol_readiness.get(pair) or {}).get("supported")))
        )
    else:
        tick_pairs = {str(pair).upper() for pair in _market_ticks_mem.keys()}
        broker_symbol_failures = sorted([pair for pair in configured_pairs if pair not in tick_pairs])
        broker_symbol_ready_count = int(sum(1 for pair in configured_pairs if pair in tick_pairs))
    state["broker_symbol_ready_count"] = broker_symbol_ready_count
    state["broker_symbol_failures"] = broker_symbol_failures
    state["broker_symbol_failure_count"] = int(len(broker_symbol_failures))
    state["activation_mismatch_pairs"] = list(activation_consistency.get("activation_mismatch_pairs", []) or [])
    state["activation_mismatch_count"] = int(len(state["activation_mismatch_pairs"]))
    state["active_registry_root"] = str(activation_consistency.get("active_registry_root") or "")
    state["bridge_booted_at"] = state.get("bridge_booted_at")

    mt4_fresh = bool(status == "connected" and age is not None and age <= stale_after)
    runtime_signal_fresh = bool(
        str(state.get("runtime_status") or "").strip().lower() == "running"
        and runtime_cycle_age_secs is not None
        and float(runtime_cycle_age_secs) <= 30.0
    )
    state["positions_fresh"] = mt4_fresh
    state["runtime_signal_fresh"] = runtime_signal_fresh
    state["symbol_readiness_fresh"] = mt4_fresh
    state["transport_fresh"] = mt4_fresh
    state["positions_stale"] = bool(not mt4_fresh and list(state.get("positions") or []))
    state["agent_decisions_stale"] = bool(not runtime_signal_fresh and list(state.get("agent_decisions") or []))
    if not mt4_fresh:
        state["positions"] = []
        state["symbol_readiness"] = {}
        state["symbol_ready_count"] = 0
        state["unsupported_pairs"] = []
        state["broker_symbol_ready_count"] = 0
        state["broker_symbol_failures"] = list(configured_pairs)
        state["broker_symbol_failure_count"] = int(len(configured_pairs))
        if state.get("transport_mode") is not None:
            state["transport_mode_raw"] = str(state.get("transport_mode") or "")
        state["transport_mode"] = ""
    if not runtime_signal_fresh:
        state["agent_decisions"] = []
        state["agent_diagnostics"] = {}
    if not mt4_fresh:
        state["equity"] = 0.0
        if state.get("equity_source") in {"runtime_seed", "runtime_constant", "seed"} or not state.get("equity_source"):
            state["equity_source"] = "stale_or_missing_heartbeat"

    bridge_state = "bridge_up"
    status_tier = "bridge_up_mt4_stale"
    runtime_status = str(state.get("runtime_status") or "").strip().lower()
    if mt4_fresh and bool(tick_state.get("ticks_fresh")):
        if runtime_signal_fresh:
            status_tier = "bridge_up_mt4_live"
        elif runtime_status == "failed":
            status_tier = "bridge_up_runtime_failed"
        elif runtime_status == "stalled":
            status_tier = "bridge_up_runtime_stalled"
        elif runtime_status in {"starting", "unknown", "stopped"}:
            status_tier = "bridge_up_runtime_starting"
        else:
            status_tier = "bridge_up_runtime_stale"
    state["bridge_state"] = bridge_state
    state["status_tier"] = status_tier
    state["signal_data_fresh"] = bool(mt4_fresh and bool(tick_state.get("ticks_fresh")) and runtime_signal_fresh)
    return state


def _bridge_bootstrap_reset() -> None:
    _reports_cache.clear()
    _visuals.clear()
    _market_ticks_mem.clear()
    _market_tick_history.clear()
    service.patch_state(
        {
            "system_status": "starting",
            "last_heartbeat": None,
            "positions": [],
            "symbol_readiness": {},
            "symbol_ready_count": 0,
            "unsupported_pairs": [],
            "transport_mode": "",
            "bridge_booted_at": _iso(_utc_now_ts()),
            "__prune_stale__": True,
        }
    )


def _ready_payload() -> dict[str, Any]:
    state = _state_with_liveness(service.get_state())
    health = dict(service.get_health() or {})

    runtime_status = str(state.get("runtime_status") or "unknown").strip().lower()
    runtime_cycle_age_secs = _runtime_cycle_age_secs(state)
    runtime_ready = bool(runtime_status == "running" and runtime_cycle_age_secs is not None and runtime_cycle_age_secs <= 30.0)

    mt4_status = str(state.get("system_status") or "unknown").strip().lower()
    heartbeat_age_secs = state.get("heartbeat_age_secs")
    heartbeat_stale_after_secs = state.get("heartbeat_stale_after_secs")
    mt4_fresh = bool(mt4_status == "connected" and heartbeat_age_secs is not None and float(heartbeat_age_secs) <= float(heartbeat_stale_after_secs or 30.0))
    ticks_fresh = bool(state.get("ticks_fresh", False))
    database_ok = bool(health.get("tables_ok"))

    if not database_ok:
        status_tier = "bridge_up_db_unhealthy"
        reason = "database_unhealthy"
    elif not runtime_ready:
        if runtime_status == "failed":
            status_tier = "bridge_up_runtime_failed"
            reason = "runtime_startup_failed"
        elif runtime_status == "stalled":
            status_tier = "bridge_up_runtime_stalled"
            reason = "runtime_startup_stalled"
        elif runtime_status == "starting":
            status_tier = "bridge_up_runtime_starting"
            reason = "runtime_starting"
        else:
            status_tier = "bridge_up_runtime_starting"
            reason = "runtime_cycle_stale" if runtime_status == "running" else "runtime_not_running"
    elif mt4_fresh and ticks_fresh:
        status_tier = "bridge_up_mt4_live"
        reason = "ok"
    else:
        status_tier = "bridge_up_runtime_ready_mt4_stale"
        reason = "mt4_heartbeat_stale" if not mt4_fresh else "tick_feed_stale"

    return {
        "status": "ok" if database_ok else "degraded",
        "reason": reason,
        "bridge_up": True,
        "database_ok": database_ok,
        "database_status": str(health.get("database") or ("up" if database_ok else "degraded")),
        "runtime_status": runtime_status,
        "runtime_ready": runtime_ready,
        "runtime_cycle_age_secs": runtime_cycle_age_secs,
        "runtime_phase": str(state.get("runtime_phase") or ""),
        "runtime_phase_pair": str(state.get("runtime_phase_pair") or ""),
        "runtime_phase_index": int(state.get("runtime_phase_index") or 0),
        "runtime_phase_total": int(state.get("runtime_phase_total") or 0),
        "runtime_last_progress_age_secs": state.get("runtime_last_progress_age_secs"),
        "runtime_failure_reason": str(state.get("runtime_failure_reason") or ""),
        "runtime_boot_id": str(state.get("runtime_boot_id") or ""),
        "mt4_status": mt4_status,
        "heartbeat_age_secs": heartbeat_age_secs,
        "heartbeat_stale_after_secs": heartbeat_stale_after_secs,
        "mt4_fresh": mt4_fresh,
        "ticks_fresh": ticks_fresh,
        "tick_status": str(state.get("tick_status") or "unknown"),
        "tick_reason": str(state.get("tick_reason") or "unknown"),
        "status_tier": status_tier,
    }


def _parse_positions_text(msg: str) -> list[dict[str, Any]]:
    tail = str(msg or "")[len("POSITIONS") :].strip()
    if not tail or tail == "NONE":
        return []

    out: list[dict[str, Any]] = []
    chunks = tail.split(" symbol=")
    for i, chunk in enumerate(chunks):
        txt = chunk if i == 0 else f"symbol={chunk}"
        txt = txt.strip()
        if not txt:
            continue
        pos: dict[str, Any] = {}
        for kv in txt.split(","):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k in {"lots", "profit", "open_price", "open_time"}:
                pos[k] = _safe_float(v)
            elif k in {"type", "magic"}:
                try:
                    pos[k] = int(v)
                except Exception:
                    pos[k] = -1
            else:
                pos[k] = v
        if pos:
            out.append(pos)
    return out


def _state_patch_from_heartbeat_text(msg: str) -> dict[str, Any]:
    patch: dict[str, Any] = {
        "system_status": "connected",
        "last_heartbeat": _iso(_utc_now_ts()),
    }
    for tok in str(msg).split():
        if tok.startswith("eq="):
            patch["equity"] = _safe_float(tok.split("=", 1)[1])
        elif tok.startswith("margin="):
            patch["margin"] = _safe_float(tok.split("=", 1)[1])
        elif tok.startswith("freemargin="):
            patch["freemargin"] = _safe_float(tok.split("=", 1)[1])
        elif tok.startswith("lev="):
            patch["leverage"] = _safe_float(tok.split("=", 1)[1])
        elif tok.startswith("transport="):
            patch["transport_mode"] = str(tok.split("=", 1)[1]).strip().lower()
    return patch


def _state_patch_from_report_json(payload: dict[str, Any]) -> dict[str, Any]:
    p = dict(payload or {})
    patch: dict[str, Any] = {
        "system_status": "connected",
        "last_heartbeat": _iso(_utc_now_ts()),
    }
    if p.get("equity") is not None:
        patch["equity"] = _safe_float(p.get("equity"))
    if p.get("margin") is not None:
        patch["margin"] = _safe_float(p.get("margin"))
    if p.get("freemargin") is not None:
        patch["freemargin"] = _safe_float(p.get("freemargin"))
    if p.get("leverage") is not None:
        patch["leverage"] = _safe_float(p.get("leverage"))

    if isinstance(p.get("positions"), list):
        patch["positions"] = list(p.get("positions") or [])
    if p.get("transport_mode") is not None:
        patch["transport_mode"] = str(p.get("transport_mode"))
    if isinstance(p.get("configured_pairs"), list):
        patch["configured_pairs"] = [str(x).strip().upper() for x in list(p.get("configured_pairs") or []) if str(x).strip()]
    if isinstance(p.get("symbol_readiness"), dict):
        readiness: dict[str, dict[str, Any]] = {}
        for pair, raw in dict(p.get("symbol_readiness") or {}).items():
            pair_u = str(pair).strip().upper()
            item = dict(raw or {})
            readiness[pair_u] = {
                "broker_symbol": str(item.get("broker_symbol") or ""),
                "supported": bool(item.get("supported")),
                "selected": bool(item.get("selected")),
            }
        patch["symbol_readiness"] = readiness
        patch["symbol_ready_count"] = int(sum(1 for item in readiness.values() if bool(item.get("supported"))))
        patch["unsupported_pairs"] = sorted([pair for pair, item in readiness.items() if not bool(item.get("supported"))])
    return patch


def _apply_report(msg: str, payload: dict[str, Any] | None) -> None:
    text = str(msg or "").strip()

    if isinstance(payload, dict) and payload:
        p = _state_patch_from_report_json(payload)
        service.patch_state(p)

    if not text:
        return

    if text.startswith("HEARTBEAT"):
        service.patch_state(_state_patch_from_heartbeat_text(text))
        return

    if text.startswith("POSITIONS"):
        service.patch_state({"positions": _parse_positions_text(text), "last_update": _utc_now_ts()})
        return

    if text.startswith("CYCLE_START"):
        patch: dict[str, Any] = {"cycle_active": True}
        for tok in text.split():
            if tok.startswith("eq="):
                patch["cycle_start_equity"] = _safe_float(tok.split("=", 1)[1])
            elif tok.startswith("target="):
                patch["cycle_target"] = _safe_float(tok.split("=", 1)[1])
        service.patch_state(patch)
        return

    if text.startswith("CYCLE_TARGET_HIT"):
        service.patch_state({"cycle_active": False})
        return


def _aggregate_bars(symbol: str, timeframe: str, limit: int) -> list[dict[str, Any]]:
    tf = str(timeframe).strip().upper()
    tf_sec = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "H1": 3600,
        "H4": 14400,
        "D": 86400,
    }.get(tf)
    if tf_sec is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    history = list(_market_tick_history.get(symbol, deque()))
    if not history:
        return []

    buckets: dict[int, dict[str, Any]] = {}
    for row in history:
        ts = _safe_float(row.get("ts_epoch"), 0.0)
        if ts <= 0.0:
            continue
        bid = _safe_float(row.get("bid"), 0.0)
        ask = _safe_float(row.get("ask"), 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
        if mid <= 0.0:
            continue
        spread_px = max(0.0, ask - bid) if bid > 0 and ask > 0 else 0.0

        bucket = int(ts // tf_sec) * tf_sec
        bar = buckets.get(bucket)
        if bar is None:
            buckets[bucket] = {
                "time": _iso(float(bucket)),
                "open": float(mid),
                "high": float(mid),
                "low": float(mid),
                "close": float(mid),
                "mid_open": float(mid),
                "mid_high": float(mid),
                "mid_low": float(mid),
                "mid_close": float(mid),
                "bid_open": float(bid),
                "bid_high": float(bid),
                "bid_low": float(bid),
                "bid_close": float(bid),
                "ask_open": float(ask),
                "ask_high": float(ask),
                "ask_low": float(ask),
                "ask_close": float(ask),
                "spread": float(spread_px),
                "_spread_sum": float(spread_px),
                "_spread_count": 1,
                "volume": 1,
            }
            continue

        bar["high"] = max(float(bar["high"]), float(mid))
        bar["low"] = min(float(bar["low"]), float(mid))
        bar["close"] = float(mid)
        bar["mid_high"] = max(float(bar["mid_high"]), float(mid))
        bar["mid_low"] = min(float(bar["mid_low"]), float(mid))
        bar["mid_close"] = float(mid)
        bar["bid_high"] = max(float(bar["bid_high"]), float(bid))
        bar["bid_low"] = min(float(bar["bid_low"]), float(bid))
        bar["bid_close"] = float(bid)
        bar["ask_high"] = max(float(bar["ask_high"]), float(ask))
        bar["ask_low"] = min(float(bar["ask_low"]), float(ask))
        bar["ask_close"] = float(ask)
        bar["_spread_sum"] = float(bar.get("_spread_sum", 0.0)) + float(spread_px)
        bar["_spread_count"] = int(bar.get("_spread_count", 0)) + 1
        bar["volume"] = int(bar.get("volume", 0)) + 1

    out: list[dict[str, Any]] = []
    for k in sorted(buckets.keys()):
        bar = dict(buckets[k])
        spread_count = max(1, int(bar.pop("_spread_count", 1)))
        spread_sum = float(bar.pop("_spread_sum", bar.get("spread", 0.0)))
        bar["spread"] = float(spread_sum / spread_count)
        out.append(bar)
    lim = max(1, min(int(limit), 2000))
    return out[-lim:]


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _resolve_repo_path(raw: str) -> Path | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    variants = [txt]
    normalized = txt.replace("\\", "/")
    if normalized != txt:
        variants.append(normalized)
    for value in variants:
        path = Path(value).expanduser()
        for candidate in (path, settings.project_root / path, settings.project_root.parent / path):
            if candidate.exists():
                return candidate.resolve()
    return None


def _latest_registry_file_for_pair(pair: str) -> Path | None:
    registry_root = _resolve_repo_path(settings.registry_root)
    if registry_root is None or not registry_root.exists():
        return None
    pair_u = str(pair).upper().strip()
    candidates: list[tuple[float, Path]] = []
    for path in registry_root.glob("*.json"):
        try:
            payload = _load_json_file(path)
        except Exception:
            continue
        if str(payload.get("pair") or "").strip().upper() != pair_u:
            continue
        try:
            mtime = float(path.stat().st_mtime)
        except Exception:
            mtime = 0.0
        candidates.append((mtime, path.resolve()))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _caps_from_registry_meta(registry_meta: dict[str, Any]) -> dict[str, Any]:
    artifacts = dict(registry_meta.get("artifacts") or {})
    capabilities = dict(registry_meta.get("capabilities") or {})
    has_exit_model = bool(capabilities.get("has_exit_model")) or bool(artifacts.get("exit_policy"))
    has_reversal_models = bool(capabilities.get("has_reversal_models")) or bool(
        artifacts.get("reversal_failure") and artifacts.get("reversal_opportunity")
    )
    warnings = list(registry_meta.get("activation_warnings", []) or registry_meta.get("warnings", []) or [])
    return {
        "has_exit_model": has_exit_model,
        "has_reversal_models": has_reversal_models,
        "activation_mode": "runtime_soft",
        "warnings": warnings,
        "activation_warnings": warnings,
        "warning": ", ".join(warnings) if warnings else "",
        "registry_path": "",
    }


def _derive_training_workflow_status(
    *,
    pair: str,
    caps: dict[str, Any],
    registry_meta: dict[str, Any],
    promotion: dict[str, Any],
    report_refs: list[str],
) -> str:
    promotion_status = str(
        promotion.get("status")
        or registry_meta.get("promotion_status")
        or ""
    ).strip().lower()
    if promotion_status:
        return promotion_status
    if report_refs:
        return "reported"
    has_exit_model = bool(caps.get("has_exit_model"))
    has_reversal_models = bool(caps.get("has_reversal_models"))
    lifecycle_complete = bool(
        registry_meta.get("lifecycle_complete")
        or (has_exit_model and has_reversal_models)
    )
    tier = str(registry_meta.get("tier") or settings.pair_tier(pair)).strip().lower()
    if tier == "tier1" and not lifecycle_complete:
        return "lifecycle_gap"
    if lifecycle_complete:
        return "lifecycle_complete"
    if str(caps.get("registry_path") or "").strip():
        return "active_unrated"
    return "unknown"


def _active_lifecycle_capabilities() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pair, row in service.get_active_model_sets(enabled_only=True).items():
        artifacts = dict((row or {}).get("artifacts_json") or {})
        metadata = dict((row or {}).get("metadata_json") or {})
        capabilities = dict(metadata.get("capabilities") or {})
        activation_warnings = list(metadata.get("activation_warnings", []) or [])
        out[str(pair).upper()] = {
            "has_exit_model": bool(capabilities.get("has_exit_model")) or bool(artifacts.get("exit_policy")),
            "has_reversal_models": bool(capabilities.get("has_reversal_models")) or bool(
                artifacts.get("reversal_failure") and artifacts.get("reversal_opportunity")
            ),
            "activation_mode": "runtime_soft",
            "warnings": activation_warnings,
            # Compatibility aliases for mixed frontend payload readers.
            "activation_warnings": activation_warnings,
            "warning": ", ".join(activation_warnings) if activation_warnings else "",
            "registry_path": str((row or {}).get("registry_path") or ""),
        }
    return out


def _compute_workflow_status() -> dict[str, Any]:
    state = _state_with_liveness(service.get_state())
    runtime_diag = dict(state.get("runtime_diag") or {})
    activation_consistency = dict(runtime_diag.get("activation_consistency") or {})
    startup_inference = {
        str(pair).upper(): dict(item or {})
        for pair, item in dict(runtime_diag.get("startup_inference") or {}).items()
        if str(pair).strip()
    }
    symbol_readiness = {
        str(pair).upper(): dict(item or {})
        for pair, item in dict(state.get("symbol_readiness") or {}).items()
        if str(pair).strip()
    }
    active_capabilities = _active_lifecycle_capabilities()
    capabilities: dict[str, dict[str, Any]] = {}
    workflows: list[dict[str, Any]] = []
    training_eval_reports: list[str] = []
    for pair, active_caps in active_capabilities.items():
        report_refs: list[str] = []
        promotion: dict[str, Any] = {}
        updated_at = _iso(_utc_now_ts())
        registry_path = str(active_caps.get("registry_path") or "")
        registry_meta: dict[str, Any] = {}
        active_registry_file = _resolve_repo_path(registry_path)
        latest_registry_file = _latest_registry_file_for_pair(pair)
        # Active model sets are the source of truth for the live stack. Only
        # fall back to the default registry root when an active registry file
        # is unavailable.
        registry_file = active_registry_file or latest_registry_file
        if registry_file is not None:
            registry_meta = _load_json_file(registry_file)
            if str(registry_meta.get("trained_at") or "").strip():
                updated_at = str(registry_meta.get("trained_at"))
            report_refs_raw = registry_meta.get("training_eval_reports")
            if isinstance(report_refs_raw, dict):
                for value in report_refs_raw.values():
                    txt = str(value or "").strip()
                    if txt:
                        report_refs.append(txt)
                        training_eval_reports.append(txt)
            elif isinstance(report_refs_raw, list):
                for value in report_refs_raw:
                    txt = str(value or "").strip()
                    if txt:
                        report_refs.append(txt)
                        training_eval_reports.append(txt)
            if not promotion:
                promotion = dict(registry_meta.get("promotion") or {})

        registry_caps = _caps_from_registry_meta(registry_meta) if registry_meta else {}
        caps = {
            **active_caps,
            **registry_caps,
            "has_exit_model": bool(active_caps.get("has_exit_model")) or bool(registry_caps.get("has_exit_model")),
            "has_reversal_models": bool(active_caps.get("has_reversal_models")) or bool(registry_caps.get("has_reversal_models")),
            "registry_path": str(registry_file or registry_path),
        }
        capabilities[pair] = caps

        if registry_file is not None:
            report_dir = registry_file.resolve().parents[1] / str(pair).lower() / "reports"
            if report_dir.exists():
                for name in [
                    "training_report.json",
                    "promotion_decision.json",
                    "scenario_matrix.json",
                    "reliability_by_segment.json",
                ]:
                    p = report_dir / name
                    if p.exists():
                        p_txt = str(p)
                        if p_txt not in report_refs:
                            report_refs.append(p_txt)
                        if p_txt not in training_eval_reports:
                            training_eval_reports.append(p_txt)
                        updated_at = _iso(float(p.stat().st_mtime))
                        if name == "promotion_decision.json":
                            promotion = _load_json_file(p)
        status = _derive_training_workflow_status(
            pair=pair,
            caps=caps,
            registry_meta=registry_meta,
            promotion=promotion,
            report_refs=report_refs,
        )
        workflows.append(
            {
                "workflow_id": f"{pair.lower()}-training-eval",
                "workflow_type": "training_eval",
                "status": status,
                "updated_at": updated_at,
                "startup_inference_ok": bool((startup_inference.get(pair) or {}).get("ok", False)),
                "broker_symbol_ready": bool((symbol_readiness.get(pair) or {}).get("supported", False)),
                "details_json": {
                    "promotion": promotion,
                    "training_eval_reports": report_refs,
                    "lifecycle_capabilities": caps,
                    "registry_meta": registry_meta,
                    "startup_inference": dict(startup_inference.get(pair) or {}),
                    "broker_symbol_readiness": dict(symbol_readiness.get(pair) or {}),
                },
            }
        )
    return {
        "workflows": workflows,
        "lifecycle_capabilities": capabilities,
        "training_eval_reports": training_eval_reports,
        "failure_cluster_summary": {},
        "drift_explainability": {},
        "activation_consistency": activation_consistency,
    }


def _workflow_status(limit: int) -> dict[str, Any]:
    global _workflow_status_cache
    now = _utc_now_ts()
    cached = _workflow_status_cache
    if cached and (now - cached[0]) <= _WORKFLOW_STATUS_CACHE_TTL_SECS:
        base = cached[1]
    else:
        base = _compute_workflow_status()
        _workflow_status_cache = (now, base)

    max_limit = max(1, min(int(limit), 5000))
    return {
        **base,
        "workflows": list(base.get("workflows", []))[:max_limit],
    }


@app.post("/v2/thought")
async def v2_thought(payload: dict[str, Any]) -> dict[str, Any]:
    thought = str(payload.get("thought", ""))
    service.patch_state({"current_thought": thought})
    return {"status": "ok", "thought": thought}


@app.get("/v2/monitor")
async def v2_monitor() -> dict[str, Any]:
    state = _state_with_liveness(service.get_state())
    mon = dict(state.get("monitor", {}) or {})
    return {
        "bridge": {"system_status": state.get("system_status", "unknown")},
        "account": {
            "equity": float(state.get("equity", 0.0) or 0.0),
            "margin": float(state.get("margin", 0.0) or 0.0),
            "freemargin": float(state.get("freemargin", 0.0) or 0.0),
        },
        "monitor": {
            "entry": dict(mon.get("entry", {}) or {}),
            "close": dict(mon.get("close", {}) or {}),
        },
    }


@app.post("/v2/visuals")
async def v2_post_visuals(payload: dict[str, Any]) -> dict[str, Any]:
    _visuals.clear()
    _visuals.update(dict(payload or {}))
    return {"status": "ok"}


@app.get("/v2/visuals")
async def v2_get_visuals() -> dict[str, Any]:
    return dict(_visuals)


@app.get("/v2/visuals/tap")
async def v2_tap_visuals() -> dict[str, Any]:
    return dict(_visuals)


@app.get("/v2/market/ticks")
async def v2_get_ticks() -> dict[str, Any]:
    return _fresh_market_ticks()


@app.get("/v2/market/bars")
async def v2_get_bars(symbol: str = Query(...), timeframe: str = Query("H1"), limit: int = Query(400)) -> dict[str, Any]:
    sym = str(symbol).strip().upper()
    try:
        bars = _aggregate_bars(sym, timeframe, limit)
    except ValueError as exc:
        return {"symbol": sym, "timeframe": timeframe, "bars": [], "error": str(exc)}
    return {"symbol": sym, "timeframe": timeframe, "bars": bars, "limit": int(limit)}


@app.post("/v2/commands")
async def v2_post_command(payload: dict[str, Any]) -> JSONResponse:
    out, code = service.submit_command(payload, proto="v2")
    return JSONResponse(content=out, status_code=code)


@app.get("/v2/commands/poll")
async def v2_poll_command(format: str = Query("json")) -> Response:
    as_line = str(format).lower() == "line"
    out, code = service.poll_command(as_line=as_line)
    if as_line:
        return PlainTextResponse(content=str(out), status_code=code)
    return JSONResponse(content=dict(out), status_code=code)


@app.get("/v2/commands/history")
async def v2_commands_history(limit: int = Query(200)) -> dict[str, Any]:
    return {"commands": service.get_commands(limit=limit)}


@app.get("/v2/commands/events")
async def v2_commands_events(limit: int = Query(500), command_id: str | None = Query(default=None)) -> dict[str, Any]:
    return {"events": service.get_command_events(limit=limit, command_id=command_id)}


@app.post("/v2/commands/ack")
async def v2_ack_command(payload: dict[str, Any]) -> JSONResponse:
    out, code = service.ack_command(payload)
    return JSONResponse(content=out, status_code=code)


@app.post("/v2/market/tick")
async def v2_tick(payload: dict[str, Any]) -> dict[str, Any]:
    sym = str(payload.get("symbol", "")).strip().upper()
    if sym:
        ts_epoch = _parse_ts(payload.get("time") or payload.get("ts") or payload.get("timestamp"))
        bid = _safe_float(payload.get("bid"), 0.0)
        ask = _safe_float(payload.get("ask"), 0.0)
        mid = ((bid + ask) / 2.0) if bid > 0 and ask > 0 else _safe_float(payload.get("mid"), 0.0)
        spread_points = _safe_float(payload.get("spread_points", payload.get("spread_pts")), 0.0)
        spread_pips = _safe_float(payload.get("spread_pips"), 0.0)
        spread_legacy = _safe_float(payload.get("spread"), 0.0)
        spread_bps_raw = _safe_float(payload.get("spread_bps"), 0.0)
        spread_bps, spread_source = normalize_spread_bps(
            tick={
                "symbol": sym,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "digits": payload.get("digits"),
                "spread_bps": spread_bps_raw if spread_bps_raw > 0 else None,
                "spread_points": spread_points if spread_points > 0 else None,
                "spread_pips": spread_pips if spread_pips > 0 else None,
                "spread": spread_legacy if spread_legacy > 0 else None,
            },
            pair=sym,
        )
        tick = {
            "symbol": sym,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread": spread_legacy,  # legacy alias
            "spread_points": spread_points,
            "spread_pips": spread_pips,
            "spread_bps": float(spread_bps),
            "spread_unit_source": str(spread_source),
            "digits": int(_safe_float(payload.get("digits"), 0.0)) if payload.get("digits") is not None else None,
            "ts_epoch": ts_epoch,
            "time": _iso(ts_epoch),
        }
        # Keep the legacy DB spread column in legacy units and store normalized bps in raw_json.
        spread_for_store = spread_legacy if spread_legacy > 0 else (spread_pips if spread_pips > 0 else 0.0)
        service.record_tick(
            {
                "symbol": sym,
                "bid": bid,
                "ask": ask,
                "spread": float(spread_for_store),
                "time": tick["time"],
                "raw": {
                    **dict(payload or {}),
                    "spread_bps": float(spread_bps),
                    "spread_unit_source": str(spread_source),
                },
            }
        )
        _market_ticks_mem[sym] = tick
        _market_tick_history[sym].append(tick)
    else:
        service.record_tick(payload)
    return {"status": "ok"}


@app.post("/v2/reports")
async def v2_reports_post(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    text = body.decode("utf-8", errors="ignore")
    parsed: dict[str, Any] | None = None
    if "application/json" in content_type and body.strip():
        try:
            decoded = json.loads(body.decode("utf-8", errors="ignore"))
            if isinstance(decoded, dict):
                parsed = decoded
        except Exception:
            parsed = None
        if parsed is not None:
            text = json.dumps(parsed, separators=(",", ":"), sort_keys=True)

    service.record_report(text, parsed)
    _reports_cache.append({"time": _iso(_utc_now_ts()), "message": text})
    if len(_reports_cache) > 2000:
        del _reports_cache[:-2000]

    _apply_report(text, parsed)
    return {"status": "ok"}


@app.get("/v2/reports")
async def v2_reports_get(limit: int = Query(200)) -> dict[str, Any]:
    return {"reports": service.get_reports(limit=limit)}


@app.get("/v2/governance/events")
async def v2_governance_events_get(limit: int = Query(200)) -> dict[str, Any]:
    return {"events": service.get_governance_events(limit=limit)}


@app.get("/v2/ops/events")
async def v2_ops_events_get(limit: int = Query(200)) -> dict[str, Any]:
    reports = service.get_reports(limit=limit)
    events = [
        {
            "time": item.get("ts"),
            "message": item.get("report_text", ""),
            "payload": item.get("report_json", {}) or {},
        }
        for item in reports
    ]
    return {"status": "ok", "events": events}


@app.get("/v2/ops/workflows/status")
async def v2_ops_workflows_status(limit: int = Query(200)) -> dict[str, Any]:
    return _workflow_status(limit)


@app.get("/v2/state")
async def v2_state() -> dict[str, Any]:
    return _state_with_liveness(service.get_state())


@app.post("/v2/state/decisions")
async def v2_state_decisions(payload: dict[str, Any]) -> dict[str, Any]:
    decisions = list(payload.get("decisions", []) or [])
    diagnostics = dict(payload.get("diagnostics", {}) or {})
    vol = float(payload.get("vol", 0.0) or 0.0)
    service.store_decisions(decisions=decisions, vol=vol, diagnostics=diagnostics)
    return {"status": "ok", "stored": len(decisions)}


@app.get("/v2/metrics")
async def v2_metrics() -> dict[str, Any]:
    out = dict(service.get_metrics() or {})
    state = _state_with_liveness(service.get_state())
    out["signals_sent"] = int(state.get("signals_sent") or 0)
    out["trades_executed"] = int(state.get("trades_executed") or 0)
    out["system_status"] = str(state.get("system_status") or "unknown")
    out["last_heartbeat"] = state.get("last_heartbeat")
    out["heartbeat_age_secs"] = state.get("heartbeat_age_secs")
    out["heartbeat_stale_after_secs"] = state.get("heartbeat_stale_after_secs")
    out["ticks_fresh"] = bool(state.get("ticks_fresh", False))
    out["tick_status"] = str(state.get("tick_status") or "unknown")
    out["tick_reason"] = str(state.get("tick_reason") or "unknown")
    out["tick_max_age_secs"] = state.get("tick_max_age_secs")
    out["tick_symbols_count"] = int(state.get("tick_symbols_count") or 0)
    return out


@app.get("/v2/health")
async def v2_health() -> dict[str, Any]:
    state = _state_with_liveness(service.get_state())
    out = service.get_health()
    out["last_heartbeat"] = state.get("last_heartbeat")
    out["system_status"] = state.get("system_status", "unknown")
    out["heartbeat_age_secs"] = state.get("heartbeat_age_secs")
    out["heartbeat_stale_after_secs"] = state.get("heartbeat_stale_after_secs")
    out["ticks_fresh"] = bool(state.get("ticks_fresh", False))
    out["tick_status"] = str(state.get("tick_status") or "unknown")
    out["tick_reason"] = str(state.get("tick_reason") or "unknown")
    out["tick_max_age_secs"] = state.get("tick_max_age_secs")
    out["tick_symbols_count"] = int(state.get("tick_symbols_count") or 0)
    system_status = str(out.get("system_status", "")).lower()
    heartbeat_age = out.get("heartbeat_age_secs")
    if system_status in {"stale", "disconnected"} and heartbeat_age is not None:
        out["status"] = "degraded"
    return out


@app.get("/v2/ready")
async def v2_ready() -> dict[str, Any]:
    return _ready_payload()


@app.get("/v2/ping")
async def v2_ping() -> dict[str, Any]:
    return {"status": "ok"}
