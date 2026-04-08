# AGENT: ROLE: Bridge HTTP app exposing runtime readiness, state, ticks/bars, commands, reports, and decision history.
# AGENT: ENTRYPOINT: ASGI app for `src.trader.cli bridge serve`.
# AGENT: PRIMARY INPUTS: MT4 heartbeat/report text, runtime state patches, command payloads, dashboard/ops HTTP reads.
# AGENT: PRIMARY OUTPUTS: `/v2/ready`, `/v2/state`, `/v2/commands`, `/v2/decision-snapshots`, tick/bar responses.
# AGENT: DEPENDS ON: `fxstack/runtime/service.py`, `fxstack/live/policy.py`, `fxstack/settings.py`.
# AGENT: CALLED BY: bridge server process, dashboard route proxies, ops scripts, runtime watchers, digital twin validation.
# AGENT: STATE / SIDE EFFECTS: mutates in-memory tick/report caches and writes queue/state/report data through `RuntimeService`.
# AGENT: HANDSHAKES: bridge readiness/state routes, command queue API, report ingest from MT4, decision snapshot reads for twin parity checks.
# AGENT: SEE: `docs/agents/bridge-and-api-handshakes.md` -> `fxstack/runtime/service.py` -> `docs/agents/dashboard-dataflow.md`
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


# AGENT HANDSHAKE: Startup primes bridge-local caches so `/v2/ready` and `/v2/state` have a stable baseline before MT4 or runtime patch traffic arrives.
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


def _report_payload(msg: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(payload, dict) and payload:
        return dict(payload)
    txt = str(msg or "").strip()
    if not txt.startswith("{"):
        return {}
    try:
        decoded = json.loads(txt)
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


def _normalize_closed_trade_report(row: dict[str, Any]) -> dict[str, Any] | None:
    payload = _report_payload(str(row.get("report_text", "") or ""), row.get("report_json"))
    if str(payload.get("report_type") or "").strip().lower() != "closed_trade":
        return None
    close_time = _parse_ts(payload.get("close_time") or payload.get("closed_at"))
    open_time = _parse_ts(payload.get("open_time"))
    ticket = int(_safe_float(payload.get("ticket"), -1))
    lots = float(_safe_float(payload.get("lots"), 0.0))
    profit = float(_safe_float(payload.get("profit"), 0.0))
    swap = float(_safe_float(payload.get("swap"), 0.0))
    commission = float(_safe_float(payload.get("commission"), 0.0))
    net_profit = float(_safe_float(payload.get("net_profit"), profit + swap + commission))
    return {
        "ticket": ticket,
        "symbol": str(payload.get("symbol") or "").strip().upper(),
        "broker_symbol": str(payload.get("broker_symbol") or payload.get("symbol") or "").strip(),
        "side": str(payload.get("side") or "").strip().upper(),
        "type": int(_safe_float(payload.get("type"), -1)),
        "lots": lots,
        "open_price": float(_safe_float(payload.get("open_price"), 0.0)),
        "close_price": float(_safe_float(payload.get("close_price"), 0.0)),
        "open_time": _iso(open_time) if open_time > 0 else None,
        "close_time": _iso(close_time) if close_time > 0 else None,
        "close_time_epoch": close_time if close_time > 0 else None,
        "profit": profit,
        "swap": swap,
        "commission": commission,
        "net_profit": net_profit,
        "duration_secs": max(0.0, close_time - open_time) if close_time > 0 and open_time > 0 else None,
        "report_ts": float(_safe_float(row.get("ts"), 0.0)),
    }


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


def _feature_serving_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    telemetry = dict(runtime_diag.get("feature_serving") or {})
    if not telemetry:
        telemetry = {
            "source": "",
            "source_chain": ["feast_online", "parquet_fallback", "raw_contract_fallback"],
            "feature_service": "",
            "cache_hit": False,
            "freshness_secs": None,
            "stale": False,
            "reason": "",
            "details": {},
        }
    telemetry["by_pair"] = dict(runtime_diag.get("feature_serving_by_pair") or {})
    return telemetry


def _provider_health_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    provider_health = dict(runtime_diag.get("provider_health") or {})
    provider_roles = dict(runtime_diag.get("provider_roles") or {})
    history_provider = dict(provider_health.get("history_provider") or {})
    market_data_provider = dict(provider_health.get("market_data_provider") or {})
    execution_provider = dict(provider_health.get("execution_provider") or {})
    history_provider_name = str(history_provider.get("provider") or provider_roles.get("history_provider") or "")
    market_data_provider_name = str(market_data_provider.get("provider") or provider_roles.get("market_data_provider") or "")
    execution_provider_name = str(execution_provider.get("provider") or provider_roles.get("execution_provider") or "")
    source_chain = [item for item in [history_provider_name, market_data_provider_name, execution_provider_name] if item]
    return {
        "roles": dict(provider_roles),
        "history_provider": history_provider,
        "market_data_provider": market_data_provider,
        "execution_provider": execution_provider,
        "history_provider_name": history_provider_name,
        "market_data_provider_name": market_data_provider_name,
        "execution_provider_name": execution_provider_name,
        "primary_provider": market_data_provider_name or history_provider_name or execution_provider_name,
        "source_chain": list(source_chain),
    }


def _portfolio_intelligence_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    telemetry = dict(runtime_diag.get("portfolio_intelligence") or {})
    if not telemetry:
        telemetry = {
            "open_position_count": 0,
            "pending_entry_count": 0,
            "gross_exposure": 0.0,
            "net_exposure": 0.0,
            "per_symbol_exposure": {},
            "per_currency_exposure": {},
            "per_asset_class_exposure": {},
            "session_counts": {},
            "sleeve_counts": {},
            "concentration": {},
            "correlation": {},
            "budget": {},
            "stress": {},
            "governance": {},
        }
    telemetry.setdefault("budget_targets", dict(telemetry.get("budget") or {}))
    telemetry.setdefault("budget_used", dict(telemetry.get("budget") or {}))
    telemetry.setdefault("by_symbol", dict(telemetry.get("per_symbol_exposure") or {}))
    return telemetry


def _capital_governance_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    telemetry = dict(runtime_diag.get("capital_governance") or {})
    if not telemetry:
        telemetry = {
            "capital_band": str(getattr(settings, "capital_band_mode", "paper") or "paper"),
            "mode": "shadow_only" if bool(getattr(settings, "provider_shadow_only", False)) else "normal",
            "paused": False,
            "entries_only": bool(getattr(settings, "capital_entries_only", False)),
            "shadow_only": bool(getattr(settings, "provider_shadow_only", False)),
            "budget_scale": 1.0,
            "reasons": [],
            "eligible_for_upgrade": False,
            "rollback_actions": [],
            "metrics": {},
        }
    rollback_actions = list(telemetry.get("rollback_actions") or [])
    rollback_armed = any(bool(dict(item or {}).get("armed", False)) for item in rollback_actions)
    rollback_reason = ""
    for item in rollback_actions:
        row = dict(item or {})
        if bool(row.get("armed", False)):
            rollback_reason = str(row.get("reason") or "")
            break
    if not rollback_reason:
        rollback_reason = str((list(telemetry.get("reasons") or []) or [""])[0] or "")
    metrics = dict(telemetry.get("metrics") or {})
    telemetry.setdefault("release_mode", str(telemetry.get("mode") or "normal"))
    telemetry.setdefault("risk_scale", float(telemetry.get("budget_scale", 1.0) or 1.0))
    telemetry.setdefault("rollback_armed", bool(rollback_armed))
    telemetry.setdefault("rollback_reason", rollback_reason)
    telemetry.setdefault("active_triggers", list(telemetry.get("reasons") or []))
    telemetry.setdefault(
        "breach_counts",
        {
            "rollout": int(metrics.get("rollout_breach_count", 0) or 0),
            "feature_parity": int(metrics.get("feature_parity_breaches", 0) or 0),
            "stale_features": int(metrics.get("stale_feature_count", 0) or 0),
        },
    )
    return telemetry


def _runtime_startup_summary(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    runtime_startup = dict((state or {}).get("runtime_startup") or {})
    runtime_status = str((state or {}).get("runtime_status") or "unknown").strip().lower()
    runtime_failure_reason = str(runtime_startup.get("failure_reason") or "").strip()
    model_load_errors = int(runtime_diag.get("model_load_errors", 0) or 0)
    model_load_timeouts = int(runtime_diag.get("model_load_timeouts", 0) or 0)
    startup_inference_failures = int(runtime_diag.get("startup_inference_failures", 0) or 0)
    startup_disabled_pairs = [
        str(pair).strip().upper()
        for pair in list(runtime_diag.get("startup_disabled_pairs") or [])
        if str(pair).strip()
    ]
    warning_count = int(
        (1 if model_load_errors > 0 else 0)
        + (1 if model_load_timeouts > 0 else 0)
        + (1 if startup_inference_failures > 0 else 0)
        + (1 if startup_disabled_pairs else 0)
    )
    if runtime_failure_reason:
        status = "failed"
    elif runtime_status == "stalled":
        status = "stalled"
    elif runtime_status == "starting":
        status = "starting"
    elif runtime_status == "running" and warning_count > 0:
        status = "recovered_with_warnings"
    elif runtime_status == "running":
        status = "ready"
    else:
        status = runtime_status or "unknown"
    return {
        "boot_id": str(runtime_startup.get("boot_id") or "").strip(),
        "booted_at": runtime_startup.get("booted_at"),
        "runtime_pid": runtime_startup.get("runtime_pid"),
        "phase": str(runtime_startup.get("phase") or "").strip().lower(),
        "phase_pair": str(runtime_startup.get("phase_pair") or "").strip().upper(),
        "phase_index": int(runtime_startup.get("phase_index", 0) or 0),
        "phase_total": int(runtime_startup.get("phase_total", 0) or 0),
        "last_progress_ts": runtime_startup.get("last_progress_ts"),
        "last_progress_age_secs": runtime_startup.get("last_progress_age_secs"),
        "failure_component": str(runtime_startup.get("failure_component") or "").strip(),
        "failure_pair": str(runtime_startup.get("failure_pair") or "").strip(),
        "failure_reason": runtime_failure_reason,
        "failed_at": runtime_startup.get("failed_at"),
        "pending_command_policy": str(runtime_startup.get("pending_command_policy") or "").strip(),
        "model_load_errors": model_load_errors,
        "model_load_timeouts": model_load_timeouts,
        "startup_inference_failures": startup_inference_failures,
        "startup_disabled_pairs": startup_disabled_pairs,
        "warning_count": warning_count,
        "status": status,
        "recovered": bool(runtime_status == "running" and not runtime_failure_reason),
    }


def _latest_runtime_startup_failure(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in list(events or []):
        row = dict(event or {})
        event_type = str(row.get("event_type") or row.get("eventType") or "").strip().lower()
        if event_type != "runtime_startup_failed":
            continue
        payload = row.get("payload_json")
        if not isinstance(payload, dict):
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        failed_at_raw = payload.get("failed_at") or row.get("failed_at") or row.get("ts")
        failed_at = _parse_ts(failed_at_raw)
        return {
            "eventType": event_type,
            "reason": str(row.get("reason") or payload.get("failure_reason") or ""),
            "bootId": str(payload.get("boot_id") or ""),
            "phase": str(payload.get("phase") or ""),
            "phasePair": str(payload.get("phase_pair") or "").upper(),
            "failedAt": _iso(failed_at) if failed_at > 0 else None,
            "failedAgeSecs": max(0.0, _utc_now_ts() - failed_at) if failed_at > 0 else None,
        }
    return None


def _should_suppress_runtime_startup_failure(
    *,
    runtime_startup_summary: dict[str, Any],
    last_runtime_startup_failure: dict[str, Any] | None,
) -> bool:
    summary = dict(runtime_startup_summary or {})
    failure = dict(last_runtime_startup_failure or {})
    if not failure:
        return False
    if bool(summary.get("recovered", False)):
        return True
    runtime_status = str(summary.get("status") or "").strip().lower()
    current_boot_id = str(summary.get("boot_id") or "").strip()
    failed_boot_id = str(failure.get("bootId") or "").strip()
    current_failure_reason = str(summary.get("failure_reason") or "").strip()
    has_progress = bool(summary.get("last_progress_ts")) or int(summary.get("phase_index", 0) or 0) > 0
    if (
        current_boot_id
        and failed_boot_id
        and current_boot_id != failed_boot_id
        and runtime_status in {"starting", "ready", "recovered_with_warnings"}
        and not current_failure_reason
        and has_progress
    ):
        return True
    return False


# AGENT FLOW: `_state_with_liveness` is the bridge-side normalization boundary that merges persisted runtime state with freshness and cache diagnostics.
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
    runtime_failure_component = str(raw_runtime_startup.get("failure_component") or "").strip()
    runtime_failure_pair = str(raw_runtime_startup.get("failure_pair") or "").strip()
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
        "failure_component": runtime_failure_component,
        "failure_pair": runtime_failure_pair,
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
    state["runtime_failure_component"] = runtime_failure_component
    state["runtime_failure_pair"] = runtime_failure_pair
    state["runtime_last_progress_age_secs"] = runtime_last_progress_age_secs
    state["runtime_failure_reason"] = runtime_failure_reason
    state["runtime_failed_at"] = runtime_failed_at
    runtime_diag = dict(state.get("runtime_diag") or {})
    runtime_startup_summary = _runtime_startup_summary(state)
    state["runtime_startup_summary"] = dict(runtime_startup_summary)
    state["runtimeStartupSummary"] = dict(runtime_startup_summary)
    state["runtimeStartup"] = dict(runtime_startup_summary)
    state["runtime_startup_status"] = str(runtime_startup_summary.get("status") or "")
    state["runtimeStartupStatus"] = str(runtime_startup_summary.get("status") or "")
    state["runtime_startup_warning_count"] = int(runtime_startup_summary.get("warning_count") or 0)
    state["runtimeStartupWarningCount"] = int(runtime_startup_summary.get("warning_count") or 0)
    state["model_load_errors"] = int(runtime_startup_summary.get("model_load_errors") or 0)
    state["model_load_timeouts"] = int(runtime_startup_summary.get("model_load_timeouts") or 0)
    state["startup_inference_failures"] = int(runtime_startup_summary.get("startup_inference_failures") or 0)
    state["startup_disabled_pairs"] = list(runtime_startup_summary.get("startup_disabled_pairs") or [])
    feature_serving = _feature_serving_telemetry(state)
    model_load = dict(runtime_diag.get("model_load") or {})
    provider_health = _provider_health_telemetry(state)
    portfolio_intelligence = _portfolio_intelligence_telemetry(state)
    capital_governance = _capital_governance_telemetry(state)
    state["feature_serving"] = dict(feature_serving)
    state["feature_serving_source"] = str(feature_serving.get("source") or "")
    state["feature_serving_reason"] = str(feature_serving.get("reason") or "")
    state["feature_serving_cache_hit"] = bool(feature_serving.get("cache_hit", False))
    state["feature_serving_stale"] = bool(feature_serving.get("stale", False))
    state["feature_serving_feature_service"] = str(feature_serving.get("feature_service") or "")
    state["provider_health"] = dict(provider_health)
    state["provider_roles"] = dict(provider_health.get("roles") or {})
    state["portfolio_intelligence"] = dict(portfolio_intelligence)
    state["capital_governance"] = dict(capital_governance)
    state["runtime_model_load"] = model_load
    state["runtime_model_load_failures"] = int(len(model_load.get("failed_pairs") or []))
    state["runtime_model_load_failed_pairs"] = list(model_load.get("failed_pairs") or [])
    state["runtime_model_load_degraded_pairs"] = list(model_load.get("degraded_pairs") or [])
    state["providerHealth"] = dict(provider_health)
    state["providerRoles"] = dict(provider_health.get("roles") or {})
    state["portfolioTelemetry"] = dict(portfolio_intelligence)
    state["capitalGovernance"] = dict(capital_governance)
    state["capital_band"] = str(capital_governance.get("capital_band") or "")
    state["governance_mode"] = str(capital_governance.get("mode") or "")
    state["capitalBand"] = str(capital_governance.get("capital_band") or "")
    state["governanceMode"] = str(capital_governance.get("mode") or "")
    state["governance_paused"] = bool(capital_governance.get("paused", False))
    state["entries_only_mode"] = bool(capital_governance.get("entries_only", False))
    state["shadow_only_mode"] = bool(capital_governance.get("shadow_only", False))
    state["entriesOnlyMode"] = bool(capital_governance.get("entries_only", False))
    state["shadowOnlyMode"] = bool(capital_governance.get("shadow_only", False))
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

    rollout_policy = dict(runtime_diag.get("rollout_policy") or {})
    risk_cycle_summary = dict(runtime_diag.get("risk_cycle_summary") or {})
    rollout_runtime = dict(risk_cycle_summary.get("rollout") or {})
    activation_consistency = dict(runtime_diag.get("activation_consistency") or {})
    startup_inference = {
        str(pair).strip().upper(): dict(item or {})
        for pair, item in dict(runtime_diag.get("startup_inference") or {}).items()
        if str(pair).strip()
    }
    state["activation_consistency"] = activation_consistency
    state["startup_inference"] = startup_inference
    state["startup_inference_failures"] = int(runtime_diag.get("startup_inference_failures", 0) or 0)
    state["rollout_policy"] = rollout_policy
    state["rollout_runtime"] = rollout_runtime
    state["canary_active"] = bool(
        int(rollout_policy.get("active_count") or 0) > 0 or int(risk_cycle_summary.get("rollout_active_count") or 0) > 0
    )
    state["canary_pairs"] = list(rollout_runtime.get("active_pairs") or rollout_policy.get("active_pairs") or [])
    state["canary_breach_count"] = int(
        rollout_runtime.get("breach_count")
        or risk_cycle_summary.get("rollout_breach_count")
        or 0
    )

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


# AGENT HANDSHAKE: `/v2/ready` is the ops/runtime startup contract; scripts and dashboards depend on these field names remaining stable.
def _ready_payload() -> dict[str, Any]:
    state = _state_with_liveness(service.get_state())
    health = dict(service.get_health() or {})
    metrics = dict(service.get_metrics() or {})
    runtime_startup_summary = dict(state.get("runtime_startup_summary") or {})
    last_runtime_startup_failure = _latest_runtime_startup_failure(service.get_governance_events(limit=50))
    if _should_suppress_runtime_startup_failure(
        runtime_startup_summary=runtime_startup_summary,
        last_runtime_startup_failure=last_runtime_startup_failure,
    ):
        last_runtime_startup_failure = None

    runtime_status = str(state.get("runtime_status") or "unknown").strip().lower()
    runtime_cycle_age_secs = _runtime_cycle_age_secs(state)
    runtime_ready = bool(runtime_status == "running" and runtime_cycle_age_secs is not None and runtime_cycle_age_secs <= 30.0)

    mt4_status = str(state.get("system_status") or "unknown").strip().lower()
    heartbeat_age_secs = state.get("heartbeat_age_secs")
    heartbeat_stale_after_secs = state.get("heartbeat_stale_after_secs")
    mt4_fresh = bool(mt4_status == "connected" and heartbeat_age_secs is not None and float(heartbeat_age_secs) <= float(heartbeat_stale_after_secs or 30.0))
    ticks_fresh = bool(state.get("ticks_fresh", False))
    database_ok = bool(health.get("tables_ok"))
    feature_serving = dict(state.get("feature_serving") or {})
    feature_push_metrics = dict(metrics.get("feature_push") or {})
    feature_parity_metrics = dict(metrics.get("feature_parity") or {})
    feature_push_backlog = int(feature_push_metrics.get("backlog") or 0)
    feature_parity_breaches = int(feature_parity_metrics.get("breaches") or 0)
    feature_online_ready = bool(str(state.get("feature_serving_source") or "").strip())
    feature_data_fresh = bool(feature_online_ready and not bool(state.get("feature_serving_stale", False)))
    feature_push_backlog_ok = bool(
        not bool(settings.feature_push_enabled) or feature_push_backlog <= int(settings.feature_push_backlog_warn)
    )
    feature_parity_ok = bool(feature_parity_breaches == 0)
    rollout_policy = dict(dict(state.get("runtime_diag") or {}).get("rollout_policy") or {})
    rollout_runtime = dict(dict(dict(state.get("runtime_diag") or {}).get("risk_cycle_summary") or {}).get("rollout") or {})
    provider_health = dict(state.get("provider_health") or {})
    capital_governance = dict(state.get("capital_governance") or {})

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
        "runtime_failure_component": str(state.get("runtime_failure_component") or ""),
        "runtime_failure_pair": str(state.get("runtime_failure_pair") or ""),
        "runtime_failure_reason": str(state.get("runtime_failure_reason") or ""),
        "runtime_boot_id": str(state.get("runtime_boot_id") or ""),
        "runtime_startup_summary": dict(state.get("runtime_startup_summary") or {}),
        "runtimeStartupSummary": dict(state.get("runtimeStartupSummary") or {}),
        "last_runtime_startup_failure": last_runtime_startup_failure,
        "lastRuntimeStartupFailure": last_runtime_startup_failure,
        "runtime_startup_status": str(state.get("runtime_startup_status") or ""),
        "runtime_startup_warning_count": int(state.get("runtime_startup_warning_count") or 0),
        "runtime_model_load": dict(state.get("runtime_model_load") or {}),
        "runtime_model_load_failures": int(state.get("runtime_model_load_failures") or 0),
        "runtime_model_load_failed_pairs": list(state.get("runtime_model_load_failed_pairs") or []),
        "runtime_model_load_degraded_pairs": list(state.get("runtime_model_load_degraded_pairs") or []),
        "model_load_errors": int(state.get("model_load_errors") or 0),
        "model_load_timeouts": int(state.get("model_load_timeouts") or 0),
        "startup_inference_failures": int(state.get("startup_inference_failures") or 0),
        "startup_disabled_pairs": list(state.get("startup_disabled_pairs") or []),
        "mt4_status": mt4_status,
        "heartbeat_age_secs": heartbeat_age_secs,
        "heartbeat_stale_after_secs": heartbeat_stale_after_secs,
        "mt4_fresh": mt4_fresh,
        "ticks_fresh": ticks_fresh,
        "tick_status": str(state.get("tick_status") or "unknown"),
        "tick_reason": str(state.get("tick_reason") or "unknown"),
        "feature_serving": feature_serving,
        "feature_serving_source": str(state.get("feature_serving_source") or ""),
        "feature_serving_reason": str(state.get("feature_serving_reason") or ""),
        "feature_serving_cache_hit": bool(state.get("feature_serving_cache_hit", False)),
        "feature_serving_stale": bool(state.get("feature_serving_stale", False)),
        "feature_serving_feature_service": str(state.get("feature_serving_feature_service") or ""),
        "feature_online_ready": feature_online_ready,
        "feature_data_fresh": feature_data_fresh,
        "feature_push_backlog_ok": feature_push_backlog_ok,
        "feature_parity_ok": feature_parity_ok,
        "feature_push_backlog": feature_push_backlog,
        "feature_parity_breaches": feature_parity_breaches,
        "providerHealth": provider_health,
        "provider_health": provider_health,
        "providerRoles": dict(state.get("provider_roles") or {}),
        "provider_roles": dict(state.get("provider_roles") or {}),
        "portfolioTelemetry": dict(state.get("portfolio_intelligence") or {}),
        "portfolio_intelligence": dict(state.get("portfolio_intelligence") or {}),
        "capitalGovernance": capital_governance,
        "capitalBand": str(state.get("capital_band") or ""),
        "governanceMode": str(state.get("governance_mode") or ""),
        "entriesOnlyMode": bool(state.get("entries_only_mode", False)),
        "shadowOnlyMode": bool(state.get("shadow_only_mode", False)),
        "canary_active": bool(state.get("canary_active", False)),
        "canary_pairs": list(state.get("canary_pairs") or []),
        "canary_breach_count": int(state.get("canary_breach_count") or 0),
        "rollout_policy": rollout_policy,
        "rollout_runtime": rollout_runtime,
        "provider_health": provider_health,
        "capital_governance": capital_governance,
        "capital_band": str(state.get("capital_band") or ""),
        "governance_mode": str(state.get("governance_mode") or ""),
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


def _coerce_known_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts <= 0.0:
            return None
        return ts / 1000.0 if ts > 1e12 else ts
    txt = str(value).strip()
    if not txt:
        return None
    try:
        if txt.replace(".", "", 1).isdigit():
            num = float(txt)
            if num <= 0.0:
                return None
            return num / 1000.0 if num > 1e12 else num
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _path_mtime(path: Path | None) -> float:
    if path is None:
        return 0.0
    try:
        return float(path.stat().st_mtime)
    except Exception:
        return 0.0


def _registry_candidate_rank(path: Path | None, *, registry_meta: dict[str, Any] | None = None) -> float:
    if path is None:
        return 0.0
    meta = registry_meta if registry_meta is not None else _load_json_file(path)
    trained_at = _coerce_known_ts((meta or {}).get("trained_at")) or 0.0
    return max(_path_mtime(path), trained_at)


def _latest_shadow_registry_files() -> dict[str, Path]:
    shadow_root = _resolve_repo_path("fx-quant-stack/artifacts_shadow")
    if shadow_root is None or not shadow_root.exists():
        return {}

    out: dict[str, tuple[float, Path]] = {}
    for path in shadow_root.glob("registry_full_*/*.json"):
        payload = _load_json_file(path)
        pair = str(payload.get("pair") or "").strip().upper()
        if not pair:
            continue
        rank = _registry_candidate_rank(path, registry_meta=payload)
        prev = out.get(pair)
        if prev is None or rank >= prev[0]:
            out[pair] = (rank, path.resolve())
    return {pair: item[1] for pair, item in out.items()}


def _latest_shadow_registry_file_for_pair(pair: str) -> Path | None:
    shadow_root = _resolve_repo_path("fx-quant-stack/artifacts_shadow")
    if shadow_root is None or not shadow_root.exists():
        return None

    pair_u = str(pair).upper().strip()
    candidates: list[tuple[float, Path]] = []
    for path in shadow_root.glob("registry_full_*/*.json"):
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


def _load_registry_report_details(
    *,
    pair: str,
    registry_file: Path,
    registry_meta: dict[str, Any],
) -> tuple[list[str], dict[str, Any], float]:
    report_refs: list[str] = []
    promotion = dict(registry_meta.get("promotion") or {})
    if not promotion and str(registry_meta.get("promotion_status") or "").strip():
        promotion = {"status": str(registry_meta.get("promotion_status") or "").strip().lower()}
    seen_refs: set[str] = set()
    latest_report_ts = 0.0

    def _add_report_ref(raw: str, *, expand_related: bool = True) -> None:
        nonlocal promotion, latest_report_ts
        txt = str(raw or "").strip()
        if not txt or txt in seen_refs:
            return
        seen_refs.add(txt)
        report_refs.append(txt)
        resolved = _resolve_repo_path(txt)
        if resolved is None or not resolved.exists():
            return
        latest_report_ts = max(latest_report_ts, _path_mtime(resolved))
        if resolved.name == "promotion_decision.json":
            payload = _load_json_file(resolved)
            if payload:
                promotion = payload
        if expand_related and resolved.name == "training_report.json":
            for sibling_name in ("promotion_decision.json", "scenario_matrix.json", "reliability_by_segment.json"):
                sibling = resolved.with_name(sibling_name)
                if sibling.exists():
                    _add_report_ref(str(sibling), expand_related=False)

    report_refs_raw = registry_meta.get("training_eval_reports")
    if isinstance(report_refs_raw, dict):
        for value in report_refs_raw.values():
            _add_report_ref(str(value or ""))
    elif isinstance(report_refs_raw, list):
        for value in report_refs_raw:
            _add_report_ref(str(value or ""))

    if registry_file.parent.name == "registry":
        report_dir = registry_file.resolve().parents[1] / str(pair).lower() / "reports"
        if report_dir.exists():
            for name in ("training_report.json", "promotion_decision.json", "scenario_matrix.json", "reliability_by_segment.json"):
                path = report_dir / name
                if path.exists():
                    _add_report_ref(str(path), expand_related=False)

    return report_refs, promotion, latest_report_ts


def _latest_shadow_training_event() -> dict[str, Any] | None:
    shadow_root = _resolve_repo_path("fx-quant-stack/artifacts_shadow")
    if shadow_root is None or not shadow_root.exists():
        return None

    latest: tuple[float, Path] | None = None
    for pattern in ("full_*/*/reports/*.json", "full_*/*/*/reports/*.json"):
        for path in shadow_root.glob(pattern):
            if path.name not in {"training_report.json", "promotion_decision.json"}:
                continue
            try:
                ts = float(path.stat().st_mtime)
            except Exception:
                continue
            if latest is None or ts > latest[0]:
                latest = (ts, path.resolve())

    if latest is None:
        return None

    ts, report_path = latest
    try:
        rel = report_path.relative_to(shadow_root)
        parts = rel.parts
    except Exception:
        parts = report_path.parts

    run_name = str(parts[0]) if len(parts) >= 1 else ""
    pair = str(parts[1]).upper() if len(parts) >= 2 else ""
    model = "primary_stack"
    if len(parts) >= 5 and parts[-2] == "reports":
        model = str(parts[-3])
    age_secs = max(0.0, _utc_now_ts() - ts)
    status = "running" if age_secs <= 7200.0 else "completed"
    return {
        "event_type": "training_shadow_update",
        "status": status,
        "time": _iso(ts),
        "reason": f"Latest shadow retrain update: {pair or 'unknown'} {model} in {run_name or 'artifacts_shadow'}",
        "message": f"Latest shadow retrain update: {pair or 'unknown'} {model}",
        "payload": {
            "pair": pair,
            "model": model,
            "run_name": run_name,
            "report_path": str(report_path),
            "shadow": True,
        },
    }


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
        "activation_mode": "model_driven" if (has_exit_model or has_reversal_models) else "runtime_soft",
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
        component_feature_services = {
            str(name): {
                "feature_service_name": str((value or {}).get("feature_service_name") or ""),
                "feature_service_version": str((value or {}).get("feature_service_version") or ""),
                "feature_contract_hash": str((value or {}).get("feature_contract_hash") or ""),
                "feature_view_names": list((value or {}).get("feature_view_names") or []),
            }
            for name, value in artifacts.items()
            if isinstance(value, dict)
            and (
                str((value or {}).get("feature_service_name") or "").strip()
                or str((value or {}).get("feature_contract_hash") or "").strip()
            )
        }
        component_feature_services.update(
            {
                str(name): dict(value or {})
                for name, value in dict(metadata.get("component_feature_contracts") or {}).items()
                if str(name).strip() and str((value or {}).get("feature_service_name") or "").strip()
            }
        )
        active_feature_services = sorted(
            {
                str(item.get("feature_service_name") or "").strip()
                for item in component_feature_services.values()
                if str(item.get("feature_service_name") or "").strip()
            }
        )
        component_versions = {
            str(name): dict(value or {})
            for name, value in artifacts.items()
            if isinstance(value, dict) and (value.get("model_version") or value.get("model_uri"))
        }
        component_model_uris = {
            str(name): str((value or {}).get("model_uri") or "")
            for name, value in artifacts.items()
            if isinstance(value, dict) and str((value or {}).get("model_uri") or "").strip()
        }
        out[str(pair).upper()] = {
            "has_exit_model": bool(capabilities.get("has_exit_model")) or bool(artifacts.get("exit_policy")),
            "has_reversal_models": bool(capabilities.get("has_reversal_models")) or bool(
                artifacts.get("reversal_failure") and artifacts.get("reversal_opportunity")
            ),
            "activation_mode": "model_driven"
            if (
                bool(capabilities.get("has_exit_model")) or bool(artifacts.get("exit_policy"))
                or bool(capabilities.get("has_reversal_models"))
                or bool(artifacts.get("reversal_failure") and artifacts.get("reversal_opportunity"))
            )
            else "runtime_soft",
            "warnings": activation_warnings,
            # Compatibility aliases for mixed frontend payload readers.
            "activation_warnings": activation_warnings,
            "warning": ", ".join(activation_warnings) if activation_warnings else "",
            "registry_path": str((row or {}).get("registry_path") or ""),
            "bundle_run_id": str(metadata.get("bundle_run_id") or ""),
            "mlflow": dict(metadata.get("mlflow") or {}),
            "component_versions": component_versions,
            "component_model_uris": component_model_uris,
            "component_feature_services": component_feature_services,
            "active_feature_services": active_feature_services,
            "activation_alias": str((metadata.get("mlflow") or {}).get("activated_alias") or metadata.get("intended_alias") or ""),
            "phase3_execution_required": bool(metadata.get("phase3_execution_required", False)),
            "phase3_evidence": dict(metadata.get("phase3_evidence") or {}),
            "phase4_shadow_only": bool(metadata.get("phase4_shadow_only", False)),
            "phase4_sequence_dataset_manifests": dict(metadata.get("phase4_sequence_dataset_manifests") or {}),
            "phase4_portfolio_reports": dict(metadata.get("phase4_portfolio_reports") or {}),
            "phase4_challenger_reports": dict(metadata.get("phase4_challenger_reports") or {}),
            "phase5_gates": dict(metadata.get("phase5_gates") or {}),
            "phase5_gate_bundle": dict(metadata.get("phase5_gate_bundle") or {}),
            "release_status": str(metadata.get("release_status") or ""),
            "rollback_target": dict(metadata.get("rollback_target") or {}),
            "operator_signoff": dict(metadata.get("operator_signoff") or {}),
            "canary_plan": dict(metadata.get("canary_plan") or {}),
            "canary_prep": dict(metadata.get("canary_prep") or {}),
            "promotion_gates": list(metadata.get("promotion_gates") or []),
            "phase5_gate_summary": dict(metadata.get("phase5_gate_summary") or {}),
            "shadow_acceptance_summary": dict(metadata.get("shadow_acceptance_summary") or {}),
            "release_notes": list(metadata.get("release_notes") or []),
            "activation_package": dict(metadata.get("activation_package") or {}),
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
    feature_serving_by_pair = {
        str(key).upper(): dict(item or {})
        for key, item in dict(runtime_diag.get("feature_serving_by_pair") or {}).items()
        if str(key).strip()
    }
    symbol_readiness = {
        str(pair).upper(): dict(item or {})
        for pair, item in dict(state.get("symbol_readiness") or {}).items()
        if str(pair).strip()
    }
    active_capabilities = _active_lifecycle_capabilities()
    shadow_registry_files = _latest_shadow_registry_files()
    pairs = sorted(set(active_capabilities.keys()) | set(shadow_registry_files.keys()))
    capabilities: dict[str, dict[str, Any]] = {}
    workflows: list[dict[str, Any]] = []
    training_eval_reports: list[str] = []
    for pair in pairs:
        active_caps = dict(active_capabilities.get(pair) or {})
        updated_at = _iso(_utc_now_ts())
        registry_path = str(active_caps.get("registry_path") or "")
        registry_meta: dict[str, Any] = {}
        active_registry_file = _resolve_repo_path(registry_path)
        latest_registry_file = _latest_registry_file_for_pair(pair)
        shadow_registry_file = shadow_registry_files.get(pair) or _latest_shadow_registry_file_for_pair(pair)
        live_registry_file = active_registry_file or latest_registry_file
        registry_candidates = [path for path in (active_registry_file, latest_registry_file, shadow_registry_file) if path is not None]
        # Prefer the newest registry we can see, including shadow runs that
        # have not yet been activated into the live manifest.
        registry_file = max(registry_candidates, key=lambda path: _registry_candidate_rank(path)) if registry_candidates else None
        report_refs: list[str] = []
        promotion: dict[str, Any] = {}
        if registry_file is not None:
            registry_meta = _load_json_file(registry_file)
            report_refs, promotion, latest_report_ts = _load_registry_report_details(
                pair=pair,
                registry_file=registry_file,
                registry_meta=registry_meta,
            )
            updated_at_ts = max(_registry_candidate_rank(registry_file, registry_meta=registry_meta), latest_report_ts)
            updated_at = _iso(updated_at_ts) if updated_at_ts > 0.0 else updated_at
            for txt in report_refs:
                if txt not in training_eval_reports:
                    training_eval_reports.append(txt)

        registry_caps = _caps_from_registry_meta(registry_meta) if registry_meta else {}
        caps = {
            **active_caps,
            **registry_caps,
            "has_exit_model": bool(active_caps.get("has_exit_model")) or bool(registry_caps.get("has_exit_model")),
            "has_reversal_models": bool(active_caps.get("has_reversal_models")) or bool(registry_caps.get("has_reversal_models")),
            "registry_path": str(registry_file or registry_path),
        }
        capabilities[pair] = caps

        live_rank = _registry_candidate_rank(live_registry_file)
        shadow_rank = _registry_candidate_rank(shadow_registry_file)
        selected_source = "shadow" if (
            registry_file is not None and shadow_registry_file is not None and registry_file.resolve() == shadow_registry_file.resolve()
        ) else "live"
        shadow_pending_activation = bool(
            shadow_registry_file is not None
            and (
                live_registry_file is None
                or shadow_registry_file.resolve() != live_registry_file.resolve()
            )
            and shadow_rank >= live_rank
        )
        feature_serving_pair = dict(
            feature_serving_by_pair.get(f"{pair}:M5")
            or feature_serving_by_pair.get(f"{pair}:D")
            or feature_serving_by_pair.get(f"{pair}:H4")
            or state.get("feature_serving")
            or {}
        )
        status = _derive_training_workflow_status(
            pair=pair,
            caps=caps,
            registry_meta=registry_meta,
            promotion=promotion,
            report_refs=report_refs,
        )
        registry_artifacts = dict(registry_meta.get("artifacts") or {})
        challenger_components = {
            key: dict(value or {})
            for key, value in registry_artifacts.items()
            if str(key).strip().lower() in {"swing_patchtst", "intraday_patchtst"}
        }
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
                    "registry_source": selected_source,
                    "active_registry_path": str(active_registry_file or registry_path),
                    "shadow_registry_path": str(shadow_registry_file or ""),
                    "shadow_pending_activation": shadow_pending_activation,
                    "bundle_run_id": str(registry_meta.get("bundle_run_id") or active_caps.get("bundle_run_id") or ""),
                    "mlflow": dict(registry_meta.get("mlflow") or active_caps.get("mlflow") or {}),
                    "component_versions": dict(
                        (registry_meta.get("mlflow") or {}).get("component_versions")
                        or active_caps.get("component_versions")
                        or {}
                    ),
                    "component_model_uris": dict(active_caps.get("component_model_uris") or {}),
                    "component_feature_services": dict(active_caps.get("component_feature_services") or {}),
                    "active_feature_services": list(active_caps.get("active_feature_services") or []),
                    "activation_alias": str(active_caps.get("activation_alias") or registry_meta.get("intended_alias") or ""),
                    "phase3_execution_required": bool(active_caps.get("phase3_execution_required", registry_meta.get("phase3_execution_required", False))),
                    "phase3_evidence": dict(active_caps.get("phase3_evidence") or registry_meta.get("phase3_evidence") or {}),
                    "phase4_shadow_only": bool(active_caps.get("phase4_shadow_only", registry_meta.get("phase4_shadow_only", False))),
                    "phase4_sequence_dataset_manifests": dict(
                        active_caps.get("phase4_sequence_dataset_manifests")
                        or registry_meta.get("phase4_sequence_dataset_manifests")
                        or {}
                    ),
                    "phase4_portfolio_reports": dict(
                        active_caps.get("phase4_portfolio_reports")
                        or registry_meta.get("phase4_portfolio_reports")
                        or {}
                    ),
                    "phase4_challenger_reports": dict(
                        active_caps.get("phase4_challenger_reports")
                        or registry_meta.get("phase4_challenger_reports")
                        or {}
                    ),
                    "phase5_gates": dict(active_caps.get("phase5_gates") or registry_meta.get("phase5_gates") or {}),
                    "phase5_gate_bundle": dict(active_caps.get("phase5_gate_bundle") or registry_meta.get("phase5_gate_bundle") or {}),
                    "release_status": str(active_caps.get("release_status") or registry_meta.get("release_status") or ""),
                    "rollback_target": dict(active_caps.get("rollback_target") or registry_meta.get("rollback_target") or {}),
                    "operator_signoff": dict(active_caps.get("operator_signoff") or registry_meta.get("operator_signoff") or {}),
                    "canary_plan": dict(active_caps.get("canary_plan") or registry_meta.get("canary_plan") or {}),
                    "canary_prep": dict(active_caps.get("canary_prep") or registry_meta.get("canary_prep") or {}),
                    "promotion_gates": list(active_caps.get("promotion_gates") or registry_meta.get("promotion_gates") or []),
                    "phase5_gate_summary": dict(
                        active_caps.get("phase5_gate_summary")
                        or registry_meta.get("phase5_gate_summary")
                        or {}
                    ),
                    "shadow_acceptance_summary": dict(
                        active_caps.get("shadow_acceptance_summary")
                        or registry_meta.get("shadow_acceptance_summary")
                        or {}
                    ),
                    "release_notes": list(active_caps.get("release_notes") or registry_meta.get("release_notes") or []),
                    "activation_package": dict(active_caps.get("activation_package") or registry_meta.get("activation_package") or {}),
                    "provider_roles": dict(runtime_diag.get("provider_roles") or {}),
                    "provider_health": dict(state.get("provider_health") or {}),
                    "portfolio_intelligence": dict(state.get("portfolio_intelligence") or {}),
                    "capital_governance": dict(state.get("capital_governance") or {}),
                    "challenger_components": challenger_components,
                    "feature_serving": feature_serving_pair,
                    "feature_serving_source": str(feature_serving_pair.get("source") or state.get("feature_serving_source") or ""),
                    "feature_serving_reason": str(feature_serving_pair.get("reason") or state.get("feature_serving_reason") or ""),
                    "feature_serving_feature_service": str(feature_serving_pair.get("feature_service") or state.get("feature_serving_feature_service") or ""),
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


@app.get("/v2/decision-snapshots")
async def v2_decision_snapshots_get(limit: int = Query(200)) -> dict[str, Any]:
    return {"items": service.get_decision_snapshots(limit=max(1, min(int(limit), 5000)))}


@app.get("/v2/closed-trades")
async def v2_closed_trades_get(limit: int = Query(200)) -> dict[str, Any]:
    rows = service.get_closed_trade_reports(limit=max(1, min(int(limit), 2000)))
    trades: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        trade = _normalize_closed_trade_report(dict(row or {}))
        if not trade:
            continue
        key = "|".join(
            [
                str(trade.get("ticket")),
                str(trade.get("close_time_epoch")),
                str(trade.get("lots")),
                str(trade.get("close_price")),
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        trades.append(trade)
    trades.sort(key=lambda item: float(item.get("close_time_epoch") or 0.0), reverse=True)
    return {"trades": trades[: max(1, min(int(limit), 2000))]}


@app.get("/v2/governance/events")
async def v2_governance_events_get(limit: int = Query(200)) -> dict[str, Any]:
    return {"events": service.get_governance_events(limit=limit)}


@app.get("/v2/ops/events")
async def v2_ops_events_get(limit: int = Query(200)) -> dict[str, Any]:
    reports = service.get_reports(limit=limit)
    events = [
        {
            "time": item.get("ts"),
            "event_type": "report",
            "status": "info",
            "message": item.get("report_text", ""),
            "payload": item.get("report_json", {}) or {},
        }
        for item in reports
    ]
    governance_events = [
        {
            "time": item.get("ts"),
            "event_type": str(item.get("event_type") or ""),
            "status": (
                "error"
                if str(item.get("event_type") or "").strip().lower() in {"runtime_startup_failed", "feature_push_failed", "feature_parity_breach"}
                else "warning"
            ),
            "message": str(item.get("reason") or item.get("event_type") or ""),
            "payload": item.get("payload_json", {}) or {},
        }
        for item in service.get_governance_events(limit=limit)
    ]
    events = governance_events + events
    shadow_event = _latest_shadow_training_event()
    if shadow_event is not None:
        events.insert(0, shadow_event)
    lim = max(1, min(int(limit), 5000))
    events.sort(key=lambda item: _parse_ts(item.get("time")) or 0.0, reverse=True)
    return {"status": "ok", "events": events[:lim]}


@app.get("/v2/ops/workflows/status")
async def v2_ops_workflows_status(limit: int = Query(200)) -> dict[str, Any]:
    return _workflow_status(limit)


@app.get("/v2/state")
async def v2_state() -> dict[str, Any]:
    state = _state_with_liveness(service.get_state())
    runtime_startup_summary = dict(state.get("runtime_startup_summary") or {})
    last_runtime_startup_failure = _latest_runtime_startup_failure(service.get_governance_events(limit=50))
    if _should_suppress_runtime_startup_failure(
        runtime_startup_summary=runtime_startup_summary,
        last_runtime_startup_failure=last_runtime_startup_failure,
    ):
        last_runtime_startup_failure = None
    state["last_runtime_startup_failure"] = last_runtime_startup_failure
    state["lastRuntimeStartupFailure"] = last_runtime_startup_failure
    return state


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
    out["feature_serving"] = dict(state.get("feature_serving") or {})
    out["feature_serving_source"] = str(state.get("feature_serving_source") or "")
    out["feature_serving_reason"] = str(state.get("feature_serving_reason") or "")
    out["feature_serving_cache_hit"] = bool(state.get("feature_serving_cache_hit", False))
    out["feature_serving_stale"] = bool(state.get("feature_serving_stale", False))
    out["feature_serving_feature_service"] = str(state.get("feature_serving_feature_service") or "")
    out["feature_online_ready"] = bool(str(state.get("feature_serving_source") or "").strip())
    out["feature_data_fresh"] = bool(out["feature_online_ready"] and not bool(state.get("feature_serving_stale", False)))
    out["feature_push_backlog_ok"] = bool(
        int(dict(out.get("feature_push") or {}).get("backlog") or 0) <= int(settings.feature_push_backlog_warn)
    )
    out["feature_parity_ok"] = bool(int(dict(out.get("feature_parity") or {}).get("breaches") or 0) == 0)
    out["risk_cycle_summary"] = dict(dict(state.get("runtime_diag") or {}).get("risk_cycle_summary") or {})
    out["rollout_policy"] = dict(dict(state.get("runtime_diag") or {}).get("rollout_policy") or {})
    out["provider_health"] = dict(state.get("provider_health") or {})
    out["provider_roles"] = dict(state.get("provider_roles") or {})
    out["portfolio_intelligence"] = dict(state.get("portfolio_intelligence") or {})
    out["capital_governance"] = dict(state.get("capital_governance") or {})
    out["capital_band"] = str(state.get("capital_band") or "")
    out["governance_mode"] = str(state.get("governance_mode") or "")
    out["providerHealth"] = dict(state.get("provider_health") or {})
    out["providerRoles"] = dict(state.get("provider_roles") or {})
    out["provider_roles"] = dict(state.get("provider_roles") or {})
    out["portfolioTelemetry"] = dict(state.get("portfolio_intelligence") or {})
    out["capitalGovernance"] = dict(state.get("capital_governance") or {})
    out["capitalBand"] = str(state.get("capital_band") or "")
    out["governanceMode"] = str(state.get("governance_mode") or "")
    out["entriesOnlyMode"] = bool(state.get("entries_only_mode", False))
    out["shadowOnlyMode"] = bool(state.get("shadow_only_mode", False))
    out["canary_active"] = bool(state.get("canary_active", False))
    out["canary_pairs"] = list(state.get("canary_pairs") or [])
    out["canary_breach_count"] = int(state.get("canary_breach_count") or 0)
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
    out["feature_serving"] = dict(state.get("feature_serving") or {})
    out["feature_serving_source"] = str(state.get("feature_serving_source") or "")
    out["feature_serving_reason"] = str(state.get("feature_serving_reason") or "")
    out["feature_serving_cache_hit"] = bool(state.get("feature_serving_cache_hit", False))
    out["feature_serving_stale"] = bool(state.get("feature_serving_stale", False))
    out["feature_serving_feature_service"] = str(state.get("feature_serving_feature_service") or "")
    out["feature_online_ready"] = bool(str(state.get("feature_serving_source") or "").strip())
    out["feature_data_fresh"] = bool(out["feature_online_ready"] and not bool(state.get("feature_serving_stale", False)))
    out["feature_push_backlog_ok"] = bool(
        int(dict(out.get("feature_push") or {}).get("backlog") or 0) <= int(settings.feature_push_backlog_warn)
    )
    out["feature_parity_ok"] = bool(int(dict(out.get("feature_parity") or {}).get("breaches") or 0) == 0)
    out["risk_cycle_summary"] = dict(dict(state.get("runtime_diag") or {}).get("risk_cycle_summary") or {})
    out["provider_health"] = dict(state.get("provider_health") or {})
    out["provider_roles"] = dict(state.get("provider_roles") or {})
    out["portfolio_intelligence"] = dict(state.get("portfolio_intelligence") or {})
    out["capital_governance"] = dict(state.get("capital_governance") or {})
    out["capital_band"] = str(state.get("capital_band") or "")
    out["governance_mode"] = str(state.get("governance_mode") or "")
    out["providerHealth"] = dict(state.get("provider_health") or {})
    out["providerRoles"] = dict(state.get("provider_roles") or {})
    out["provider_roles"] = dict(state.get("provider_roles") or {})
    out["portfolioTelemetry"] = dict(state.get("portfolio_intelligence") or {})
    out["capitalGovernance"] = dict(state.get("capital_governance") or {})
    out["capitalBand"] = str(state.get("capital_band") or "")
    out["governanceMode"] = str(state.get("governance_mode") or "")
    out["entriesOnlyMode"] = bool(state.get("entries_only_mode", False))
    out["shadowOnlyMode"] = bool(state.get("shadow_only_mode", False))
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
