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

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError
from sqlalchemy import func, select

from fxstack.api.auth import add_api_key_middleware
from fxstack.api.middleware import (
    REQUEST_ID_HEADER,
    add_request_id_middleware,
    configure_structured_logging,
    current_request_id,
)
from fxstack.api.observability import PROMETHEUS_CONTENT_TYPE, collect_and_render
from fxstack.api.schemas import (
    CommandAckRequest,
    CommandRequest,
    MarketTickRequest,
    PositionReconcileResponse,
    PositionView,
    ReportRequest,
    StateDecisionsRequest,
)
from fxstack.api.wire import (
    BRIDGE_PROTOCOL_MIN_COMPATIBLE,
    BRIDGE_PROTOCOL_VERSION,
    BridgeError,
    BridgeErrorEnvelope,
    HandshakeResponse,
)
from fxstack.live.policy import normalize_spread_bps
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


settings = get_settings()
_bridge_logger = logging.getLogger("fxstack.api.app")


# AGENT HANDSHAKE: Combined startup + shutdown lifespan for the bridge ASGI app.
# Startup: install structured logging, prime bridge caches via the bootstrap
# reset, and emit a startup banner with the protocol version and auth state so
# operators can spot misconfiguration immediately. Shutdown: best-effort drain
# of any in-flight service work (``RuntimeService.drain`` is currently a no-op
# explicit hook). Names referenced inside the body (``service``,
# ``_bridge_bootstrap_reset``, ``_handshake_build``) are resolved lazily at
# lifespan invocation time, which is after the module finishes loading.
@asynccontextmanager
async def _bridge_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_structured_logging()
    # Validate cross-field invariants before anything else binds resources.
    # If the config is broken, fail fast with a clear list of problems so
    # the operator sees them in the launch log instead of crashing deep in
    # a runtime loop two minutes later. Skipped only when the operator
    # explicitly opts out via FXSTACK_SKIP_STARTUP_VALIDATION=true (intended
    # for emergency boot-into-degraded-state scenarios only).
    # Schema-version protection lives in
    # ``fxstack.runtime.postgres_store._bootstrap_schema`` and fires when the
    # module-level ``service = RuntimeService(...)`` initializer runs (well
    # before this lifespan body). It refuses to come up if required tables
    # are missing or the alembic head mismatches.
    if (os.environ.get("FXSTACK_SKIP_STARTUP_VALIDATION") or "").strip().lower() not in {"true", "1", "yes"}:
        config_errors = settings.validate_for_startup()
        if config_errors:
            for err in config_errors:
                _bridge_logger.error("startup config error: %s", err)
            raise RuntimeError(
                f"fxstack bridge startup aborted: {len(config_errors)} config error(s); "
                "see preceding log lines for details"
            )
    _bridge_bootstrap_reset()
    _bridge_logger.info(
        "fxstack bridge startup: protocol=%s build=%s auth_required=%s",
        BRIDGE_PROTOCOL_VERSION,
        _handshake_build(),
        settings.bridge_auth_required,
    )
    try:
        yield
    finally:
        _bridge_logger.info("fxstack bridge shutdown: signaling service drain")
        drain = getattr(service, "drain", None)
        if callable(drain):
            try:
                result = drain()
                if hasattr(result, "__await__"):
                    await result  # type: ignore[misc]
            except Exception:
                _bridge_logger.exception("fxstack bridge shutdown: service.drain() raised")
        # Give in-flight HTTP handlers a brief, bounded window to finish after
        # the drain fence flips. Sync handlers complete on their own thread;
        # the sleep just yields the loop so any pending callbacks fire before
        # ASGI tears the socket down. Bounded by FXSTACK_SHUTDOWN_GRACE_SECS
        # so SIGTERM never hangs.
        grace_secs = _bridge_shutdown_grace_secs()
        if grace_secs > 0:
            try:
                await asyncio.wait_for(asyncio.sleep(grace_secs), timeout=grace_secs + 1.0)
            except asyncio.TimeoutError:  # pragma: no cover - belt-and-suspenders
                _bridge_logger.warning("fxstack bridge shutdown: grace window timed out")
        _bridge_logger.info("fxstack bridge shutdown complete")


def _bridge_shutdown_grace_secs() -> float:
    """Read the configured shutdown grace window (seconds). Clamped to [0, 30]."""
    raw = os.environ.get("FXSTACK_SHUTDOWN_GRACE_SECS", "0.5")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0.5
    return max(0.0, min(30.0, value))


app = FastAPI(
    title="fx-quant-stack bridge",
    version="0.1.0",
    lifespan=_bridge_lifespan,
)

# Middleware registration order matters: FastAPI invokes middleware in *reverse*
# order of registration, so the LAST registered runs FIRST. We want
# request-id to run first (outermost) so every downstream — including auth — sees
# the correlation id. Therefore register auth FIRST, then request-id.
add_api_key_middleware(
    app,
    settings.bridge_api_key,
    required=settings.bridge_auth_required,
)
add_request_id_middleware(app)


def _error_envelope(
    *,
    code: str,
    message: str,
    request_id: str | None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical error envelope dict."""
    return BridgeErrorEnvelope(
        error=BridgeError(
            code=code,
            message=message,
            request_id=request_id,
            detail=detail,
        )
    ).model_dump(exclude_none=True)


@app.exception_handler(HTTPException)
async def _bridge_http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    rid = getattr(request.state, "request_id", None) or current_request_id.get()
    detail_payload: dict[str, Any] | None = None
    if isinstance(exc.detail, dict):
        detail_payload = dict(exc.detail)
        message = str(detail_payload.pop("message", None) or detail_payload.pop("detail", None) or f"HTTP {exc.status_code}")
    else:
        message = str(exc.detail) if exc.detail is not None else f"HTTP {exc.status_code}"
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(
            _error_envelope(
                code=f"http_{exc.status_code}",
                message=message,
                request_id=rid,
                detail=detail_payload,
            )
        ),
        headers={REQUEST_ID_HEADER: rid} if rid else None,
    )


@app.exception_handler(RequestValidationError)
async def _bridge_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = getattr(request.state, "request_id", None) or current_request_id.get()
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            _error_envelope(
                code="validation_error",
                message="Request payload failed schema validation",
                request_id=rid,
                detail={"errors": exc.errors()},
            )
        ),
        headers={REQUEST_ID_HEADER: rid} if rid else None,
    )

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


def _handshake_build() -> str:
    """Return the build identifier we expose on /v2/handshake."""
    from fxstack.api.wire import _build_revision as _rev  # local import to avoid cycle warnings

    return _rev()


def _utc_now_ts() -> float:
    return float(datetime.now(timezone.utc).timestamp())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = float(default)
    if math.isfinite(number):
        return number
    try:
        fallback = float(default)
    except (TypeError, ValueError, OverflowError):
        fallback = 0.0
    return fallback if math.isfinite(fallback) else 0.0


def _parse_ts(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return 0.0

    def _normalize_epoch(raw: float) -> float:
        if not math.isfinite(raw) or raw <= 0.0:
            return 0.0
        # Accept seconds, milliseconds, microseconds, or nanoseconds while
        # rejecting magnitudes that cannot plausibly be an epoch timestamp.
        for _ in range(3):
            if raw <= 1e11:
                break
            raw /= 1000.0
        return float(raw) if math.isfinite(raw) and 0.0 < raw <= 1e11 else 0.0

    if isinstance(value, (int, float)):
        return _normalize_epoch(float(value))
    txt = str(value).strip()
    if not txt:
        return 0.0
    try:
        return _normalize_epoch(float(txt))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return _normalize_epoch(parsed.timestamp())
    except (OverflowError, OSError, ValueError):
        return 0.0


def _timestamp_age_secs(
    value: Any,
    *,
    now_ts: float | None = None,
    max_future_skew_secs: float = 5.0,
) -> float | None:
    parsed = _parse_ts(value)
    now = float(_utc_now_ts() if now_ts is None else now_ts)
    if parsed <= 0.0 or not math.isfinite(now):
        return None
    age = now - parsed
    if age < -max(0.0, float(max_future_skew_secs)):
        return None
    return max(0.0, float(age))


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
    return _timestamp_age_secs(hb)


def _prune_tick_memory(*, now_ts: float | None = None) -> None:
    now = float(now_ts if now_ts is not None else _utc_now_ts())
    stale_after = max(30.0, float(settings.bridge_stale_tick_secs) * 10.0)
    drop_symbols: list[str] = []
    for sym, row in list(_market_ticks_mem.items()):
        ts = _safe_float((row or {}).get("ts_epoch"), 0.0)
        age = _timestamp_age_secs(ts, now_ts=now)
        if age is None or age > stale_after:
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
        age = _timestamp_age_secs(ts, now_ts=now)
        if age is None:
            continue
        if age > stale_after:
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
        age = _timestamp_age_secs(ts, now_ts=now)
        if age is not None:
            ages.append(float(age))
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
    return _timestamp_age_secs(ts)


def _feature_serving_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    telemetry = dict((state or {}).get("feature_serving") or runtime_diag.get("feature_serving") or {})
    feature_serving_by_pair = dict((state or {}).get("feature_serving_by_pair") or runtime_diag.get("feature_serving_by_pair") or {})
    if not telemetry:
        telemetry = {
            "source": "",
            "source_chain": ["feast_online", "parquet_fallback", "raw_contract_fallback"],
            "feature_service": "",
            "cache_hit": False,
            "freshness_secs": None,
            "stale": False,
            "reason": "feature_worker_absent",
            "details": {},
        }
    telemetry["by_pair"] = feature_serving_by_pair
    return telemetry


def _feature_observability_telemetry(state: dict[str, Any], *, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    feature_serving = dict((state or {}).get("feature_serving") or runtime_diag.get("feature_serving") or {})
    feature_serving_details = dict(feature_serving.get("details") or {})
    feature_serving_by_pair = {
        str(key).upper(): dict(item or {})
        for key, item in dict((state or {}).get("feature_serving_by_pair") or runtime_diag.get("feature_serving_by_pair") or {}).items()
        if str(key).strip()
    }
    feature_push = dict((metrics or {}).get("feature_push") or (state or {}).get("feature_push") or runtime_diag.get("feature_push") or {})
    feature_push_outbox = dict(feature_push.get("outbox") or {})
    feature_push_backlog = int(feature_push.get("backlog") or 0)
    feature_push_warn = int(settings.feature_push_backlog_warn)
    feature_push_enabled = bool(settings.feature_push_enabled)
    feature_push_backlog_ok = bool(not feature_push_enabled or feature_push_backlog <= feature_push_warn)
    feature_online_ready = bool(
        str((state or {}).get("feature_serving_source") or feature_serving.get("source") or "").strip()
    )
    feature_serving_stale = bool((state or {}).get("feature_serving_stale", feature_serving.get("stale", False)))
    feature_serving_reason = str(feature_serving.get("reason") or "")
    feature_data_fresh = bool(feature_online_ready and not feature_serving_stale)
    feature_serving_by_pair_keys = sorted(feature_serving_by_pair)
    feature_serving_by_pair_pairs = sorted({key.split(":", 1)[0] for key in feature_serving_by_pair_keys})
    feature_serving_by_pair_stale_keys = sorted(key for key, item in feature_serving_by_pair.items() if bool(item.get("stale", False)))
    feature_serving_by_pair_stale_pairs = sorted({key.split(":", 1)[0] for key in feature_serving_by_pair_stale_keys})
    feature_serving_summary = {
        "source": str(feature_serving.get("source") or ""),
        "reason": str(feature_serving.get("reason") or ""),
        "source_chain": list(feature_serving.get("source_chain") or []),
        "selection_policy": str(feature_serving_details.get("selection_policy") or ""),
        "selected_pairs_count": int(feature_serving_details.get("selected_pairs_count") or 0),
        "selected_pairs": list(feature_serving_details.get("selected_pairs") or []),
        "selected_timeframes": dict(feature_serving_details.get("selected_timeframes") or {}),
        "selected_stale_count": int(feature_serving_details.get("selected_stale_count") or 0),
        "all_stale_count": int(feature_serving_details.get("all_stale_count") or 0),
        "all_stale_pairs": list(feature_serving_details.get("all_stale_pairs") or []),
        "all_stale_timeframes": list(feature_serving_details.get("all_stale_timeframes") or []),
        "by_pair_count": int(len(feature_serving_by_pair_keys)),
        "by_pair_pairs": list(feature_serving_by_pair_pairs),
        "by_pair_stale_count": int(len(feature_serving_by_pair_stale_keys)),
        "by_pair_stale_pairs": list(feature_serving_by_pair_stale_pairs),
    }
    feature_push_summary = {
        "backlog": feature_push_backlog,
        "warn": feature_push_warn,
        "ok": feature_push_backlog_ok,
        "overage": max(0, feature_push_backlog - feature_push_warn) if feature_push_enabled else 0,
        "outbox": dict(feature_push_outbox),
        "outbox_count": int(sum(int(v or 0) for v in feature_push_outbox.values())),
    }
    blocker_reasons: list[str] = []
    if not feature_online_ready:
        if feature_serving and not str(feature_serving.get("source") or "").strip():
            blocker_reasons.append("feature_serving:missing_source")
        elif feature_serving_by_pair:
            blocker_reasons.append("feature_serving:missing_snapshot")
        elif feature_serving_reason == "feature_worker_absent":
            blocker_reasons.append("feature_serving:worker_absent")
        elif not feature_serving:
            blocker_reasons.append("feature_serving:worker_absent")
        else:
            blocker_reasons.append("feature_serving:missing")
    elif feature_serving_stale:
        blocker_reasons.append("feature_serving:stale")
    if feature_push_enabled and not feature_push_backlog_ok:
        blocker_reasons.append("feature_push:backlog")
    feature_bar_status = "fresh" if feature_data_fresh else ("stale" if feature_online_ready else "missing")
    return {
        "feature_push": feature_push,
        "feature_push_backlog": feature_push_backlog,
        "feature_push_backlog_warn": feature_push_warn,
        "feature_push_backlog_ok": feature_push_backlog_ok,
        "feature_push_backlog_overage": max(0, feature_push_backlog - feature_push_warn) if feature_push_enabled else 0,
        "feature_online_ready": feature_online_ready,
        "feature_data_fresh": feature_data_fresh,
        "feature_bar_status": feature_bar_status,
        "feature_blocker_reasons": list(blocker_reasons),
        "feature_blocker_reason": str(blocker_reasons[0] if blocker_reasons else ""),
        "feature_blocker_source": str("feature_serving" if blocker_reasons and blocker_reasons[0].startswith("feature_serving") else "feature_push" if blocker_reasons else ""),
        "feature_serving_stale": feature_serving_stale,
        "feature_serving": feature_serving,
        "feature_serving_summary": feature_serving_summary,
        "feature_push_summary": feature_push_summary,
    }


def _rl_portfolio_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    rl_portfolio_proposal = dict(
        (state or {}).get("rl_portfolio_proposal")
        or runtime_diag.get("rl_portfolio_proposal")
        or {}
    )
    entry_execution_policy = dict(runtime_diag.get("entry_execution_policy") or {})
    proposal_diagnostics = dict(rl_portfolio_proposal.get("diagnostics") or {})
    proposals_by_pair = {
        str(key).upper(): dict(value or {})
        for key, value in dict(rl_portfolio_proposal.get("proposals_by_pair") or {}).items()
        if str(key).strip()
    }
    pair_universe = [str(pair).upper() for pair in list(rl_portfolio_proposal.get("pair_universe") or []) if str(pair).strip()]
    checkpoint_loaded = bool(entry_execution_policy.get("rl_checkpoint_loaded", rl_portfolio_proposal.get("checkpoint_loaded", False)))
    checkpoint_path = str(
        rl_portfolio_proposal.get("checkpoint_path")
        or entry_execution_policy.get("rl_checkpoint_path")
        or ""
    )
    proposal_source = str(entry_execution_policy.get("rl_proposal_source") or rl_portfolio_proposal.get("source") or "")
    fallback_reason = str(entry_execution_policy.get("rl_fallback_reason") or rl_portfolio_proposal.get("fallback_reason") or "")
    summary = {
        "checkpoint_loaded": checkpoint_loaded,
        "checkpoint_path": checkpoint_path,
        "proposal_source": proposal_source,
        "supervised_fallback_used": bool(
            entry_execution_policy.get("rl_fallback_entry_count", 0)
            or rl_portfolio_proposal.get("supervised_fallback_used", False)
        ),
        "fallback_reason": fallback_reason,
        "routed_entry_count": int(entry_execution_policy.get("rl_routed_entry_count") or 0),
        "blocked_entry_count": int(entry_execution_policy.get("rl_blocked_entry_count") or 0),
        "fallback_entry_count": int(entry_execution_policy.get("rl_fallback_entry_count") or 0),
        "scaled_entry_count": int(entry_execution_policy.get("rl_scaled_entry_count") or 0),
        "lifecycle_reviewed_count": int(entry_execution_policy.get("rl_lifecycle_reviewed_count") or 0),
        "lifecycle_applied_count": int(entry_execution_policy.get("rl_lifecycle_applied_count") or 0),
        "lifecycle_exit_count": int(entry_execution_policy.get("rl_lifecycle_exit_count") or 0),
        "lifecycle_flip_exit_count": int(entry_execution_policy.get("rl_lifecycle_flip_exit_count") or 0),
        "lifecycle_resize_count": int(entry_execution_policy.get("rl_lifecycle_resize_count") or 0),
        "lifecycle_tighten_stop_count": int(entry_execution_policy.get("rl_lifecycle_tighten_stop_count") or 0),
        "lifecycle_preserved_exit_count": int(entry_execution_policy.get("rl_lifecycle_preserved_exit_count") or 0),
        "lifecycle_fallback_count": int(entry_execution_policy.get("rl_lifecycle_fallback_count") or 0),
        "lifecycle_pairs": [str(pair).upper() for pair in list(entry_execution_policy.get("rl_lifecycle_pairs") or []) if str(pair).strip()],
        "execution_mode": str(entry_execution_policy.get("execution_mode") or ""),
        "strategy_engine_mode": str(
            entry_execution_policy.get("strategy_engine_mode")
            or runtime_diag.get("strategy_engine_mode")
            or "supervised_legacy"
        ),
        "proposal_count": int(proposal_diagnostics.get("decision_count") or len(proposals_by_pair)),
        "candidate_count": int(proposal_diagnostics.get("candidate_count") or 0),
        "pair_universe": pair_universe,
    }
    if proposals_by_pair:
        summary["proposals_by_pair"] = proposals_by_pair
    summary["diagnostics"] = proposal_diagnostics
    summary["source"] = proposal_source
    return summary


def _rl_lifecycle_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    rl_portfolio_proposal = dict((state or {}).get("rl_portfolio_proposal") or runtime_diag.get("rl_portfolio_proposal") or {})
    entry_execution_policy = dict(runtime_diag.get("entry_execution_policy") or (state or {}).get("rl_execution_policy") or (state or {}).get("rlExecutionPolicy") or {})
    proposal_diagnostics = dict(rl_portfolio_proposal.get("diagnostics") or {})
    checkpoint_summary = dict(rl_portfolio_proposal.get("checkpoint_summary") or proposal_diagnostics.get("checkpoint_summary") or {})
    artifact_discovery = dict(proposal_diagnostics.get("artifact_discovery") or {})
    proposals_by_pair = {
        str(key).upper(): dict(value or {})
        for key, value in dict(rl_portfolio_proposal.get("proposals_by_pair") or {}).items()
        if str(key).strip()
    }
    pair_universe = [str(pair).upper() for pair in list(rl_portfolio_proposal.get("pair_universe") or []) if str(pair).strip()]

    close_intent_count = 0
    tighten_stop_intent_count = 0
    non_flat_target_count = 0
    for proposal in proposals_by_pair.values():
        action = dict(proposal.get("action") or {})
        target_position = float(action.get("target_position") or 0.0)
        if abs(target_position) > 0.0:
            non_flat_target_count += 1
        if bool(action.get("close_position", False)):
            close_intent_count += 1
        if bool(action.get("tighten_stop", False)):
            tighten_stop_intent_count += 1

    lifecycle_summary = {
        "checkpoint_loaded": bool(entry_execution_policy.get("rl_lifecycle_checkpoint_loaded", rl_portfolio_proposal.get("checkpoint_loaded", False))),
        "proposal_source": str(entry_execution_policy.get("rl_lifecycle_proposal_source") or rl_portfolio_proposal.get("source") or ""),
        "reviewed_count": int(entry_execution_policy.get("rl_lifecycle_reviewed_count") or 0),
        "applied_count": int(entry_execution_policy.get("rl_lifecycle_applied_count") or 0),
        "exit_count": int(entry_execution_policy.get("rl_lifecycle_exit_count") or 0),
        "resize_count": int(entry_execution_policy.get("rl_lifecycle_resize_count") or 0),
        "tighten_stop_count": int(entry_execution_policy.get("rl_lifecycle_tighten_stop_count") or 0),
        "preserved_exit_count": int(entry_execution_policy.get("rl_lifecycle_preserved_exit_count") or 0),
        "fallback_count": int(entry_execution_policy.get("rl_lifecycle_fallback_count") or 0),
        "pairs": list(entry_execution_policy.get("rl_lifecycle_pairs") or []),
        "strategy_engine_mode": str(entry_execution_policy.get("strategy_engine_mode") or runtime_diag.get("strategy_engine_mode") or "supervised_legacy"),
    }
    return {
        "checkpoint_loaded": bool(rl_portfolio_proposal.get("checkpoint_loaded", False)),
        "checkpoint_path": str(rl_portfolio_proposal.get("checkpoint_path") or ""),
        "checkpoint_summary": checkpoint_summary,
        "artifact_readiness": {
            "ready": bool(
                bool(rl_portfolio_proposal.get("checkpoint_loaded", False))
                or bool(str(rl_portfolio_proposal.get("checkpoint_path") or "").strip())
            ),
            "checkpoint_loaded": bool(artifact_discovery.get("checkpoint_loaded", rl_portfolio_proposal.get("checkpoint_loaded", False))),
            "checkpoint_path": str(artifact_discovery.get("checkpoint_path") or rl_portfolio_proposal.get("checkpoint_path") or ""),
            "fallback_reason": str(artifact_discovery.get("fallback_reason") or rl_portfolio_proposal.get("fallback_reason") or ""),
            "source": str(rl_portfolio_proposal.get("source") or ""),
        },
        "flip_intent": {
            "pair_universe": pair_universe,
            "proposal_count": int(len(proposals_by_pair)),
            "non_flat_target_count": int(non_flat_target_count),
            "close_intent_count": int(close_intent_count),
            "tighten_stop_intent_count": int(tighten_stop_intent_count),
        },
        "rebalance_summary": {
            "reviewed_count": lifecycle_summary["reviewed_count"],
            "applied_count": lifecycle_summary["applied_count"],
            "exit_count": lifecycle_summary["exit_count"],
            "resize_count": lifecycle_summary["resize_count"],
            "tighten_stop_count": lifecycle_summary["tighten_stop_count"],
            "preserved_exit_count": lifecycle_summary["preserved_exit_count"],
            "fallback_count": lifecycle_summary["fallback_count"],
            "pairs": list(lifecycle_summary["pairs"]),
        },
        "lifecycle_summary": lifecycle_summary,
        "reviewed_count": lifecycle_summary["reviewed_count"],
        "applied_count": lifecycle_summary["applied_count"],
        "exit_count": lifecycle_summary["exit_count"],
        "resize_count": lifecycle_summary["resize_count"],
        "tighten_stop_count": lifecycle_summary["tighten_stop_count"],
        "preserved_exit_count": lifecycle_summary["preserved_exit_count"],
        "fallback_count": lifecycle_summary["fallback_count"],
        "pairs": list(lifecycle_summary["pairs"]),
        "strategy_engine_mode": lifecycle_summary["strategy_engine_mode"],
        "proposal_source": str(rl_portfolio_proposal.get("source") or lifecycle_summary["proposal_source"]),
        "supervised_fallback_used": bool(rl_portfolio_proposal.get("supervised_fallback_used", False)),
        "fallback_reason": str(rl_portfolio_proposal.get("fallback_reason") or ""),
        "proposal_count": int(len(proposals_by_pair)),
        "pair_universe": pair_universe,
    }


def _pair_feature_serving_snapshot(
    *,
    pair: str,
    feature_serving_by_pair: dict[str, Any],
) -> dict[str, Any]:
    pair_key = str(pair).upper().strip()
    if not pair_key:
        return {}
    for timeframe in ("M5", "D", "H4"):
        entry = dict(feature_serving_by_pair.get(f"{pair_key}:{timeframe}") or {})
        if entry:
            entry.setdefault("timeframe", timeframe)
            return entry
    return {}


def _pair_readiness_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    startup_inference = {
        str(pair).upper(): dict(item or {})
        for pair, item in dict(runtime_diag.get("startup_inference") or {}).items()
        if str(pair).strip()
    }
    feature_serving_by_pair = {
        str(key).upper(): dict(item or {})
        for key, item in dict((state.get("feature_serving_by_pair") or runtime_diag.get("feature_serving_by_pair") or {})).items()
        if str(key).strip()
    }
    symbol_readiness = {
        str(pair).upper(): dict(item or {})
        for pair, item in dict((state or {}).get("symbol_readiness") or {}).items()
        if str(pair).strip()
    }
    model_load = dict(runtime_diag.get("model_load") or {})
    pair_keys = sorted(
        set(startup_inference)
        | {str(key).split(":", 1)[0] for key in feature_serving_by_pair}
        | set(symbol_readiness)
        | {str(key).upper() for key in dict(model_load.get("pairs") or {}) if str(key).strip()}
    )
    out: dict[str, dict[str, Any]] = {}
    for pair in pair_keys:
        startup = dict(startup_inference.get(pair) or {})
        feature_serving = _pair_feature_serving_snapshot(pair=pair, feature_serving_by_pair=feature_serving_by_pair)
        symbol = dict(symbol_readiness.get(pair) or {})
        model_pair = dict(dict(model_load.get("pairs") or {}).get(pair) or {})
        blockers: list[str] = []
        if not startup:
            blockers.append("startup_inference:missing")
        elif not bool(startup.get("ok", False)):
            blockers.append(f"startup_inference:{str(startup.get('reason') or 'blocked')}")
        if feature_serving:
            if not str(feature_serving.get("source") or "").strip():
                blockers.append("feature_serving:missing_source")
            if bool(feature_serving.get("stale", False)):
                blockers.append("feature_serving:stale")
        elif pair in feature_serving_by_pair:
            blockers.append("feature_serving:missing")
        if symbol and not bool(symbol.get("supported", True)):
            blockers.append(f"symbol_readiness:{str(symbol.get('broker_symbol') or 'unsupported')}")
        if not symbol and pair in symbol_readiness:
            blockers.append("symbol_readiness:missing")
        if str(model_pair.get("failure_reason") or "").strip():
            blockers.append(f"model_load:{str(model_pair.get('failure_reason') or 'error')}")
        out[pair] = {
            "pair": pair,
            "startup_inference": startup,
            "feature_serving": feature_serving,
            "symbol_readiness": symbol,
            "model_load": model_pair,
            "ready": bool(not blockers),
            "status": "ready" if not blockers else "blocked",
            "blockers": blockers,
            "reason": "ok" if not blockers else blockers[0],
            "startup_inference_ok": bool(startup.get("ok", False)),
            "feature_serving_source": str(feature_serving.get("source") or ""),
            "feature_serving_stale": bool(feature_serving.get("stale", False)),
            "symbol_supported": bool(symbol.get("supported", True)) if symbol else True,
        }
    return out


def _provider_health_telemetry(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    provider_health = dict(runtime_diag.get("provider_health") or {})
    provider_roles = dict(runtime_diag.get("provider_roles") or {})
    if not provider_roles:
        provider_roles = {
            "history_provider": str(getattr(settings, "normalized_history_provider", "") or ""),
            "market_data_provider": str(getattr(settings, "normalized_market_data_provider", "") or ""),
            "execution_provider": str(getattr(settings, "normalized_execution_provider", "") or ""),
        }
    if not provider_health:
        heartbeat_age_secs = (state or {}).get("heartbeat_age_secs")
        heartbeat_stale_after_secs = (state or {}).get("heartbeat_stale_after_secs")
        mt4_status = str((state or {}).get("system_status") or "unknown").strip().lower()
        mt4_fresh = bool(
            mt4_status == "connected"
            and heartbeat_age_secs is not None
            and float(heartbeat_age_secs) <= float(heartbeat_stale_after_secs or 30.0)
        )
        ticks_fresh = bool((state or {}).get("ticks_fresh", False))
        history_provider_name = str(provider_roles.get("history_provider") or "")
        market_data_provider_name = str(provider_roles.get("market_data_provider") or "")
        execution_provider_name = str(provider_roles.get("execution_provider") or "")
        history_shadow_only = bool(
            getattr(settings, "provider_shadow_only", False)
            or str(history_provider_name).strip().lower() in {"parquet", "dukascopy"}
        )
        execution_shadow_only = bool(str(execution_provider_name).strip().lower() != "mt4")
        provider_health = {
            "history_provider": {
                "provider": history_provider_name,
                "role": "history",
                "status": "shadow_only" if history_shadow_only else "ok",
                "shadow_only": history_shadow_only,
                "provenance": "settings_fallback",
            },
            "market_data_provider": {
                "provider": market_data_provider_name,
                "role": "market_data",
                "status": "ok" if bool(mt4_fresh and ticks_fresh) else "degraded",
                "shadow_only": False,
                "provenance": "settings_fallback",
                "details": {
                    "mt4_fresh": bool(mt4_fresh),
                    "ticks_fresh": bool(ticks_fresh),
                },
            },
            "execution_provider": {
                "provider": execution_provider_name,
                "role": "execution",
                "status": "ok" if execution_shadow_only or bool(mt4_fresh) else "degraded",
                "shadow_only": execution_shadow_only,
                "provenance": "settings_fallback",
            },
        }
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


def _runtime_startup_failure_history(events: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    max_items = max(0, int(limit))
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
        out.append(
            {
                "eventType": event_type,
                "reason": str(row.get("reason") or payload.get("failure_reason") or ""),
                "bootId": str(payload.get("boot_id") or ""),
                "phase": str(payload.get("phase") or ""),
                "phasePair": str(payload.get("phase_pair") or "").upper(),
                "failedAt": _iso(failed_at) if failed_at > 0 else None,
                "failedAgeSecs": max(0.0, _utc_now_ts() - failed_at) if failed_at > 0 else None,
            }
        )
        if len(out) >= max_items:
            break
    return out


def _should_suppress_runtime_startup_failure(
    *,
    runtime_startup_summary: dict[str, Any],
    last_runtime_startup_failure: dict[str, Any] | None,
) -> bool:
    summary = dict(runtime_startup_summary or {})
    failure = dict(last_runtime_startup_failure or {})
    if not failure:
        return False
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
        runtime_last_progress_age_secs = _timestamp_age_secs(runtime_last_progress_ts)
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
    feature_serving_by_pair = {
        str(key).upper(): dict(item or {})
        for key, item in dict(runtime_diag.get("feature_serving_by_pair") or {}).items()
        if str(key).strip()
    }
    provider_health = _provider_health_telemetry(state)
    portfolio_intelligence = _portfolio_intelligence_telemetry(state)
    capital_governance = _capital_governance_telemetry(state)
    state["feature_serving"] = dict(feature_serving)
    state["feature_observability"] = dict(_feature_observability_telemetry(state, metrics=service.get_metrics()))
    state["featureObservability"] = dict(state["feature_observability"])
    state["feature_bar_status"] = str(state["feature_observability"].get("feature_bar_status") or "")
    state["featureBarStatus"] = str(state["feature_bar_status"] or "")
    state["feature_serving_by_pair"] = dict(feature_serving_by_pair)
    state["featureServingByPair"] = dict(feature_serving_by_pair)
    state["feature_serving_source"] = str(feature_serving.get("source") or "")
    state["feature_serving_reason"] = str(feature_serving.get("reason") or "")
    state["feature_serving_cache_hit"] = bool(feature_serving.get("cache_hit", False))
    state["feature_serving_stale"] = bool(feature_serving.get("stale", False))
    state["feature_serving_feature_service"] = str(feature_serving.get("feature_service") or "")
    state["provider_health"] = dict(provider_health)
    state["provider_roles"] = dict(provider_health.get("roles") or {})
    state["portfolio_intelligence"] = dict(portfolio_intelligence)
    state["capital_governance"] = dict(capital_governance)
    rl_portfolio_proposal = _rl_portfolio_telemetry(state)
    state["rl_portfolio_proposal"] = dict(rl_portfolio_proposal)
    state["rlPortfolioProposal"] = dict(rl_portfolio_proposal)
    state["rl_execution_policy"] = dict(rl_portfolio_proposal)
    state["rlExecutionPolicy"] = dict(rl_portfolio_proposal)
    state["rl_checkpoint_loaded"] = bool(rl_portfolio_proposal.get("checkpoint_loaded", False))
    state["rlCheckpointLoaded"] = bool(rl_portfolio_proposal.get("checkpoint_loaded", False))
    state["rl_checkpoint_path"] = str(rl_portfolio_proposal.get("checkpoint_path") or "")
    state["rlCheckpointPath"] = str(rl_portfolio_proposal.get("checkpoint_path") or "")
    state["rl_proposal_source"] = str(rl_portfolio_proposal.get("proposal_source") or "")
    state["rlProposalSource"] = str(rl_portfolio_proposal.get("proposal_source") or "")
    state["rl_supervised_fallback_used"] = bool(rl_portfolio_proposal.get("supervised_fallback_used", False))
    state["rlSupervisedFallbackUsed"] = bool(rl_portfolio_proposal.get("supervised_fallback_used", False))
    state["rl_fallback_reason"] = str(rl_portfolio_proposal.get("fallback_reason") or "")
    state["rlFallbackReason"] = str(rl_portfolio_proposal.get("fallback_reason") or "")
    state["rl_routed_entry_count"] = int(rl_portfolio_proposal.get("routed_entry_count") or 0)
    state["rlRoutedEntryCount"] = int(rl_portfolio_proposal.get("routed_entry_count") or 0)
    state["rl_blocked_entry_count"] = int(rl_portfolio_proposal.get("blocked_entry_count") or 0)
    state["rlBlockedEntryCount"] = int(rl_portfolio_proposal.get("blocked_entry_count") or 0)
    state["rl_fallback_entry_count"] = int(rl_portfolio_proposal.get("fallback_entry_count") or 0)
    state["rlFallbackEntryCount"] = int(rl_portfolio_proposal.get("fallback_entry_count") or 0)
    state["rl_scaled_entry_count"] = int(rl_portfolio_proposal.get("scaled_entry_count") or 0)
    state["rlScaledEntryCount"] = int(rl_portfolio_proposal.get("scaled_entry_count") or 0)
    rl_lifecycle_summary = dict(
        runtime_diag.get("rl_lifecycle_summary")
        or runtime_diag.get("rlLifecycleSummary")
        or {}
    )
    rl_rebalance_summary = dict(
        runtime_diag.get("rl_rebalance_summary")
        or runtime_diag.get("rlRebalanceSummary")
        or rl_lifecycle_summary.get("rebalance_summary")
        or rl_lifecycle_summary.get("rebalanceSummary")
        or {}
    )
    rl_flip_intent = dict(
        runtime_diag.get("rl_flip_intent")
        or runtime_diag.get("rlFlipIntent")
        or rl_lifecycle_summary.get("flip_intent")
        or rl_lifecycle_summary.get("flipIntent")
        or {}
    )
    rl_artifact_readiness = dict(
        runtime_diag.get("rl_artifact_readiness")
        or runtime_diag.get("rlArtifactReadiness")
        or rl_lifecycle_summary.get("artifact_readiness")
        or rl_lifecycle_summary.get("artifactReadiness")
        or {}
    )
    state["rl_lifecycle_summary"] = dict(rl_lifecycle_summary)
    state["rlLifecycleSummary"] = dict(rl_lifecycle_summary)
    state["rl_rebalance_summary"] = dict(rl_rebalance_summary)
    state["rlRebalanceSummary"] = dict(rl_rebalance_summary)
    state["rl_flip_intent"] = dict(rl_flip_intent)
    state["rlFlipIntent"] = dict(rl_flip_intent)
    state["rl_artifact_readiness"] = dict(rl_artifact_readiness)
    state["rlArtifactReadiness"] = dict(rl_artifact_readiness)
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
    pair_readiness = dict(runtime_diag.get("pair_readiness") or {})
    strategy_engine_mode = str(runtime_diag.get("strategy_engine_mode") or "supervised_legacy")
    supervised_fallback = dict(runtime_diag.get("supervised_fallback") or {})
    challenger_conflict = dict(runtime_diag.get("challenger_conflict") or {})
    entry_execution_policy = dict(runtime_diag.get("entry_execution_policy") or {})
    state["activation_consistency"] = activation_consistency
    state["startup_inference"] = startup_inference
    state["startup_inference_by_pair"] = dict(startup_inference)
    state["startupInferenceByPair"] = dict(startup_inference)
    state["startup_inference_failures"] = int(runtime_diag.get("startup_inference_failures", 0) or 0)
    state["pair_readiness"] = pair_readiness
    state["pairReadiness"] = pair_readiness
    state["strategy_engine_mode"] = strategy_engine_mode
    state["strategyEngineMode"] = strategy_engine_mode
    state["supervised_fallback"] = supervised_fallback
    state["supervisedFallback"] = supervised_fallback
    state["challenger_conflict"] = challenger_conflict
    state["challengerConflict"] = challenger_conflict
    state["entry_execution_policy"] = entry_execution_policy
    state["entryExecutionPolicy"] = entry_execution_policy
    rl_lifecycle = _rl_lifecycle_telemetry(state)
    state["rl_lifecycle_summary"] = dict(rl_lifecycle)
    state["rlLifecycleSummary"] = dict(rl_lifecycle)
    state["rl_rebalance_summary"] = dict(rl_lifecycle.get("rebalance_summary") or {})
    state["rlRebalanceSummary"] = dict(rl_lifecycle.get("rebalance_summary") or {})
    state["rl_flip_intent"] = dict(rl_lifecycle.get("flip_intent") or {})
    state["rlFlipIntent"] = dict(rl_lifecycle.get("flip_intent") or {})
    state["rl_artifact_readiness"] = dict(rl_lifecycle.get("artifact_readiness") or {})
    state["rlArtifactReadiness"] = dict(rl_lifecycle.get("artifact_readiness") or {})
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
    if not pair_readiness:
        pair_readiness = _pair_readiness_telemetry(state)
        state["pair_readiness"] = pair_readiness
        state["pairReadiness"] = pair_readiness
    state["bridge_booted_at"] = state.get("bridge_booted_at")

    mt4_fresh = bool(status == "connected" and age is not None and age <= stale_after)
    runtime_signal_fresh = bool(
        str(state.get("runtime_status") or "").strip().lower() == "running"
        and runtime_cycle_age_secs is not None
        and float(runtime_cycle_age_secs) <= 30.0
    )
    agent_decisions = list(state.get("agent_decisions") or [])
    if not agent_decisions and runtime_signal_fresh:
        try:
            snapshots = service.get_decision_snapshots(limit=1)
        except Exception:
            snapshots = []
        if snapshots:
            latest_snapshot = dict(snapshots[0] or {})
            snapshot_decisions = list(latest_snapshot.get("decisions_json") or [])
            if snapshot_decisions:
                agent_decisions = snapshot_decisions
                state["agent_decisions"] = list(agent_decisions)
                state["agent_decisions_source"] = "decision_snapshots"
                state["agent_decisions_ts"] = latest_snapshot.get("ts")
    state["decisions"] = list(agent_decisions)
    state["latest_decisions"] = list(agent_decisions)
    state["positions_fresh"] = mt4_fresh
    state["runtime_signal_fresh"] = runtime_signal_fresh
    state["symbol_readiness_fresh"] = mt4_fresh
    state["transport_fresh"] = mt4_fresh
    state["positions_stale"] = bool(not mt4_fresh and list(state.get("positions") or []))
    state["agent_decisions_stale"] = bool(not runtime_signal_fresh and agent_decisions)
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


def _paper_execution_summary(
    *,
    commands: list[dict[str, Any]],
    events: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    agent_mode = str(getattr(settings, "agent_mode", "off") or "").strip().lower()
    execution_provider = str(settings.normalized_execution_provider)

    def _paper_marker(row: dict[str, Any]) -> bool:
        command = dict(row or {})
        meta = dict(command.get("orchestration_meta_json") or {})
        ack = dict(command.get("ack_json") or {})
        ack_meta = dict(ack.get("orchestration_meta_json") or {})
        return bool(
            str(meta.get("agent_mode") or "").strip().lower() == "paper"
            or str(meta.get("execution_provider") or "").strip().lower() == "paper"
            or bool(meta.get("paper_simulated", False))
            or str(ack_meta.get("execution_provider") or "").strip().lower() == "paper"
            or bool(ack_meta.get("paper_simulated", False))
        )

    paper_commands = [dict(item or {}) for item in list(commands or []) if _paper_marker(dict(item or {}))]
    if not paper_commands and (agent_mode == "paper" or execution_provider == "paper"):
        paper_commands = [dict(item or {}) for item in list(commands or [])]

    status_counts: dict[str, int] = {}
    for item in paper_commands:
        status = str(dict(item or {}).get("status") or "").strip().lower()
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1

    enabled = bool(agent_mode == "paper" or execution_provider == "paper" or paper_commands)
    latest_command = dict(paper_commands[0] or {}) if paper_commands else {}
    command_id = str(latest_command.get("command_id") or "")
    latest_events = [
        dict(item or {})
        for item in list(events or [])
        if not command_id or str(dict(item or {}).get("command_id") or "") == command_id
    ]
    latest_event = dict(latest_events[0] or {}) if latest_events else {}
    latest_run = dict(runs[0] or {}) if runs else {}
    packet = dict(latest_run.get("packet_json") or {})
    governed = dict(packet.get("governed_decision") or {})
    event_payload = dict(latest_event.get("event_json") or latest_event.get("payload_json") or {})
    event_orchestration = dict(event_payload.get("orchestration_meta_json") or {})
    pending_count = sum(1 for item in paper_commands if str(dict(item or {}).get("status") or "").strip().lower() in {"queued", "delivered"})
    terminal_event_statuses = {"delivered", "acked", "duplicate", "failed", "expired"}
    event_statuses_by_command: dict[str, set[str]] = {}
    for item in list(events or []):
        event_command_id = str(dict(item or {}).get("command_id") or "")
        if not event_command_id:
            continue
        bucket = event_statuses_by_command.setdefault(event_command_id, set())
        bucket.add(str(dict(item or {}).get("event_status") or "").strip().lower())
    orphan_count = sum(
        1
        for item in paper_commands
        if str(dict(item or {}).get("status") or "").strip().lower() in {"queued", "delivered"}
        and not (event_statuses_by_command.get(str(dict(item or {}).get("command_id") or "")) or set()) & terminal_event_statuses
    )
    return {
        "enabled": bool(enabled),
        "execution_provider": str(settings.normalized_execution_provider),
        "agent_mode": str(getattr(settings, "agent_mode", "off") or "off"),
        "pending_command_count": int(pending_count),
        "orphan_command_count": int(orphan_count),
        "recent_command_count": int(len(paper_commands)),
        "status_counts": dict(status_counts),
        "governed_decision": {
            "run_id": str(latest_run.get("run_id") or governed.get("run_id") or ""),
            "selected_action": str(governed.get("selected_action") or ""),
            "allowed": bool(governed.get("allowed", False)),
            "approval_state": str(governed.get("approval_state") or "auto"),
            "blocking_reasons": list(governed.get("blocking_reasons") or []),
            "command_preview": dict(governed.get("command_preview") or {}),
            "winning_proposal_id": str(governed.get("winning_proposal_id") or ""),
        },
        "last_command": {
            "command_id": command_id,
            "status": str(latest_command.get("status") or ""),
            "symbol": str(latest_command.get("symbol") or ""),
            "cmd": str(latest_command.get("cmd") or ""),
            "intent": str(latest_command.get("intent") or ""),
            "correlation_id": str(latest_command.get("correlation_id") or ""),
            "thread_id": str(latest_command.get("thread_id") or ""),
            "run_id": str(dict(latest_command.get("orchestration_meta_json") or {}).get("run_id") or ""),
            "trace_id": str(dict(latest_command.get("orchestration_meta_json") or {}).get("trace_id") or ""),
            "created_at": latest_command.get("created_at"),
            "updated_at": latest_command.get("updated_at"),
        },
        "last_event": {
            "status": str(latest_event.get("event_status") or ""),
            "reason": str(latest_event.get("reason") or ""),
            "event_at": latest_event.get("created_at"),
            "fill_price": event_orchestration.get("paper_fill_price"),
            "fill_source": str(event_orchestration.get("paper_fill_source") or ""),
            "filled_lots": event_orchestration.get("paper_filled_lots"),
        },
        "event_flow": {
            "event_count": int(len(latest_events)),
            "statuses": [
                str(dict(item or {}).get("event_status") or "")
                for item in list(reversed(latest_events[-5:]))
            ],
        },
    }


def _orchestration_live_summary(
    *,
    state: dict[str, Any],
    commands: list[dict[str, Any]],
    events: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    active_release: dict[str, Any] | None,
) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    live_diag = dict(runtime_diag.get("orchestration_live") or {})
    runtime_agent_mode = (
        str(
            live_diag.get("agent_mode")
            or runtime_diag.get("agent_mode")
            or dict(runtime_diag.get("orchestration") or {}).get("agent_mode")
            or getattr(settings, "agent_mode", "off")
            or "off"
        )
        .strip()
        .lower()
    )
    release_meta = dict(dict(active_release or {}).get("metadata_json") or {})
    canary_prep = dict(release_meta.get("canary_prep") or {})
    canary_plan = dict(release_meta.get("canary_plan") or {})
    canary_metadata = dict(canary_plan.get("metadata") or {})
    rollout_policy = dict(runtime_diag.get("rollout_policy") or {})
    risk_cycle_summary = dict(runtime_diag.get("risk_cycle_summary") or {})
    rollout_runtime = dict(risk_cycle_summary.get("rollout") or {})
    active_pair_scope = [
        str(pair).strip().upper()
        for pair in list(
            live_diag.get("active_pair_scope")
            or canary_prep.get("live_pair_allowlist")
            or canary_prep.get("allowlisted_pairs")
            or rollout_runtime.get("active_pairs")
            or rollout_policy.get("active_pairs")
            or (state or {}).get("canary_pairs")
            or []
        )
        if str(pair).strip()
    ]
    enabled = (
        runtime_agent_mode == "live"
        or bool(live_diag.get("enabled", False))
        or str(canary_prep.get("mode") or canary_metadata.get("mode") or "").strip().lower() == "orchestration_live"
    )
    live_commands = [
        dict(item or {})
        for item in list(commands or [])
        if str((dict(item or {}).get("orchestration_meta_json") or {}).get("agent_mode") or "").strip().lower() == "live"
    ]
    latest_command = dict(live_commands[0] or {}) if live_commands else {}
    command_id = str(latest_command.get("command_id") or "")
    latest_events = [
        dict(item or {})
        for item in list(events or [])
        if not command_id or str(dict(item or {}).get("command_id") or "") == command_id
    ]
    latest_event = dict(latest_events[0] or {}) if latest_events else {}
    latest_run = dict(runs[0] or {}) if runs else {}
    packet = dict(latest_run.get("packet_json") or {})
    governed = dict(packet.get("governed_decision") or {})
    event_payload = dict(latest_event.get("event_json") or latest_event.get("payload_json") or {})
    event_orchestration = dict(event_payload.get("orchestration_meta_json") or {})
    pending_count = sum(1 for item in live_commands if str(dict(item or {}).get("status") or "").strip().lower() in {"queued", "delivered"})
    terminal_event_statuses = {"delivered", "acked", "duplicate", "failed", "expired"}
    event_statuses_by_command: dict[str, set[str]] = {}
    for item in list(events or []):
        event_command_id = str(dict(item or {}).get("command_id") or "")
        if not event_command_id:
            continue
        bucket = event_statuses_by_command.setdefault(event_command_id, set())
        bucket.add(str(dict(item or {}).get("event_status") or "").strip().lower())
    orphan_count = sum(
        1
        for item in live_commands
        if str(dict(item or {}).get("status") or "").strip().lower() in {"queued", "delivered"}
        and not (event_statuses_by_command.get(str(dict(item or {}).get("command_id") or "")) or set()) & terminal_event_statuses
    )
    return {
        "enabled": bool(enabled),
        "agent_mode": str(runtime_agent_mode or "off"),
        "execution_provider": str(settings.normalized_execution_provider),
        "release_status": str(release_meta.get("release_status") or ""),
        "bundle_run_id": str(dict(release_meta.get("activation_package") or {}).get("bundle_run_id") or ""),
        "active_pair_scope": active_pair_scope,
        "active_sleeve_scope": list(live_diag.get("active_sleeve_scope") or canary_prep.get("live_sleeve_allowlist") or []),
        "active_intent_scope": list(live_diag.get("active_intent_scope") or canary_prep.get("live_intent_allowlist") or []),
        "ramp_steps_pct": list(live_diag.get("ramp_steps_pct") or canary_prep.get("ramp_steps_pct") or []),
        "current_stage_index": int(live_diag.get("current_stage_index") or canary_prep.get("current_stage_index") or 0),
        "current_stage_pct": int(live_diag.get("current_stage_pct") or canary_prep.get("current_stage_pct") or 0),
        "budget_scale": float(live_diag.get("budget_scale") or canary_prep.get("budget_scale") or 0.0),
        "runtime_enabled": bool(live_diag.get("runtime_enabled", canary_prep.get("runtime_enabled", True))),
        "queue_kill_active": bool(live_diag.get("queue_kill_active", canary_prep.get("queue_kill_active", False))),
        "queue_kill_reason": str(live_diag.get("queue_kill_reason") or canary_prep.get("queue_kill_reason") or ""),
        "queue_killed_at": live_diag.get("queue_killed_at") or canary_prep.get("queue_killed_at") or 0.0,
        "promotion_pack_path": str(live_diag.get("promotion_pack_path") or canary_prep.get("promotion_pack_path") or ""),
        "signoff_records": list(live_diag.get("signoff_records") or canary_prep.get("signoff_records") or []),
        "pending_command_count": int(live_diag.get("pending_command_count") or pending_count),
        "orphan_command_count": int(live_diag.get("orphan_command_count") or orphan_count),
        "ack_success_rate": float(live_diag.get("ack_success_rate") or 0.0),
        "ack_timeout_rate": float(live_diag.get("ack_timeout_rate") or 0.0),
        "overhead_p95_ms": float(live_diag.get("p95_ms") or 0.0),
        "overhead_p99_ms": float(live_diag.get("p99_ms") or 0.0),
        "entry_ratio_vs_baseline": float(live_diag.get("entry_ratio_vs_baseline") or 0.0),
        "slot_utilisation_vs_baseline": float(live_diag.get("slot_utilisation_vs_baseline") or 0.0),
        "drawdown_deterioration_pct": float(live_diag.get("drawdown_deterioration_pct") or 0.0),
        "repeated_graph_fault_count": int(live_diag.get("repeated_graph_fault_count") or 0),
        "trace_persistence_failure_count": int(live_diag.get("trace_persistence_failure_count") or 0),
        "baseline_fallback_count": int(live_diag.get("baseline_fallback_count") or 0),
        "governed_decision": {
            "run_id": str(latest_run.get("run_id") or governed.get("run_id") or ""),
            "selected_action": str(governed.get("selected_action") or ""),
            "allowed": bool(governed.get("allowed", False)),
            "approval_state": str(governed.get("approval_state") or "auto"),
            "blocking_reasons": list(governed.get("blocking_reasons") or []),
            "command_preview": dict(governed.get("command_preview") or {}),
            "winning_proposal_id": str(governed.get("winning_proposal_id") or ""),
        },
        "last_command": {
            "command_id": command_id,
            "status": str(latest_command.get("status") or ""),
            "symbol": str(latest_command.get("symbol") or ""),
            "cmd": str(latest_command.get("cmd") or ""),
            "intent": str(latest_command.get("intent") or ""),
            "correlation_id": str(latest_command.get("correlation_id") or ""),
            "thread_id": str(latest_command.get("thread_id") or ""),
            "run_id": str(dict(latest_command.get("orchestration_meta_json") or {}).get("run_id") or ""),
            "trace_id": str(dict(latest_command.get("orchestration_meta_json") or {}).get("trace_id") or ""),
            "command_source": str(dict(latest_command.get("orchestration_meta_json") or {}).get("command_source") or ""),
            "baseline_fallback": bool(dict(latest_command.get("orchestration_meta_json") or {}).get("baseline_fallback", False)),
            "fallback_reason": str(dict(latest_command.get("orchestration_meta_json") or {}).get("fallback_reason") or ""),
            "created_at": latest_command.get("created_at"),
            "updated_at": latest_command.get("updated_at"),
        },
        "last_event": {
            "status": str(latest_event.get("event_status") or ""),
            "reason": str(latest_event.get("reason") or ""),
            "event_at": latest_event.get("created_at"),
            "ticket": latest_event.get("ticket"),
            "fill_price": event_orchestration.get("fill_price"),
            "fill_source": str(event_orchestration.get("fill_source") or ""),
        },
        "event_flow": {
            "event_count": int(len(latest_events)),
            "statuses": [
                str(dict(item or {}).get("event_status") or "")
                for item in list(reversed(latest_events[-5:]))
            ],
        },
    }


def _orchestration_live_health_summary(
    orchestration_live: dict[str, Any],
    *,
    runtime_status: str | None = None,
    runtime_ready: bool | None = None,
    status_tier: str | None = None,
) -> dict[str, Any]:
    live = dict(orchestration_live or {})
    runtime_status_text = str(runtime_status or live.get("runtime_status") or "").strip().lower()
    runtime_ready_bool = None if runtime_ready is None else bool(runtime_ready)
    runtime_enabled = bool(live.get("runtime_enabled", True))
    queue_kill_active = bool(live.get("queue_kill_active", False))
    pending_command_count = int(live.get("pending_command_count") or 0)
    orphan_command_count = int(live.get("orphan_command_count") or 0)
    ack_success_rate = float(live.get("ack_success_rate") or 0.0)
    ack_timeout_rate = float(live.get("ack_timeout_rate") or 0.0)
    repeated_graph_fault_count = int(live.get("repeated_graph_fault_count") or 0)
    trace_persistence_failure_count = int(live.get("trace_persistence_failure_count") or 0)
    baseline_fallback_count = int(live.get("baseline_fallback_count") or 0)
    blockers: list[str] = []
    warnings: list[str] = []

    if runtime_ready_bool is False:
        warnings.append("runtime_not_ready")
    elif runtime_status_text and runtime_status_text != "running":
        warnings.append(f"runtime_status:{runtime_status_text}")

    if not runtime_enabled:
        blockers.append("runtime_disabled")
    if queue_kill_active:
        blockers.append("queue_kill_active")
    if pending_command_count > 0 and orphan_command_count > 0:
        warnings.append("orphan_commands")
    if ack_timeout_rate > 0.0:
        warnings.append("ack_timeout_spike")
    if repeated_graph_fault_count > 0:
        warnings.append("graph_faults")
    if trace_persistence_failure_count > 0:
        warnings.append("trace_persistence_failures")
    if baseline_fallback_count > 0:
        warnings.append("baseline_fallbacks")

    reasons = list(blockers or warnings)
    if blockers:
        status = "blocked"
    elif warnings:
        status = "degraded"
    else:
        status = "healthy"
    reason = str(reasons[0] if reasons else "ok")
    return {
        "status": status,
        "reason": reason,
        "reasons": reasons,
        "warning_count": int(len(warnings)),
        "blocking_count": int(len(blockers)),
        "runtime_status": runtime_status_text,
        "runtime_ready": runtime_ready_bool,
        "status_tier": str(status_tier or ""),
        "runtime_enabled": runtime_enabled,
        "queue_kill_active": queue_kill_active,
        "pending_command_count": pending_command_count,
        "orphan_command_count": orphan_command_count,
        "ack_success_rate": ack_success_rate,
        "ack_timeout_rate": ack_timeout_rate,
        "ack_timeout_spike": bool(ack_timeout_rate > 0.0),
        "repeated_graph_fault_count": repeated_graph_fault_count,
        "trace_persistence_failure_count": trace_persistence_failure_count,
        "baseline_fallback_count": baseline_fallback_count,
    }


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
    feature_observability = _feature_observability_telemetry(state, metrics=metrics)
    governance_events = service.get_governance_events(limit=50)
    last_runtime_startup_failure = _latest_runtime_startup_failure(governance_events)
    runtime_startup_failure_history = _runtime_startup_failure_history(governance_events)

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
    startup_inference_by_pair = dict(state.get("startup_inference_by_pair") or state.get("startup_inference") or {})
    feature_serving_by_pair = dict(state.get("feature_serving_by_pair") or state.get("featureServingByPair") or {})
    pair_readiness = dict(state.get("pair_readiness") or state.get("pairReadiness") or {})
    strategy_engine_mode = str(state.get("strategy_engine_mode") or state.get("strategyEngineMode") or "supervised_legacy")
    supervised_fallback = dict(state.get("supervised_fallback") or state.get("supervisedFallback") or {})
    challenger_conflict = dict(state.get("challenger_conflict") or state.get("challengerConflict") or {})
    entry_execution_policy = dict(state.get("entry_execution_policy") or state.get("entryExecutionPolicy") or {})
    rl_portfolio_proposal = dict(state.get("rl_portfolio_proposal") or state.get("rlPortfolioProposal") or {})
    rl_execution_policy = dict(state.get("rl_execution_policy") or state.get("rlExecutionPolicy") or {})
    rl_lifecycle_summary = dict(state.get("rl_lifecycle_summary") or state.get("rlLifecycleSummary") or {})
    rl_rebalance_summary = dict(state.get("rl_rebalance_summary") or state.get("rlRebalanceSummary") or {})
    rl_flip_intent = dict(state.get("rl_flip_intent") or state.get("rlFlipIntent") or {})
    rl_artifact_readiness = dict(state.get("rl_artifact_readiness") or state.get("rlArtifactReadiness") or {})
    feature_push_backlog_ok = bool(
        not bool(settings.feature_push_enabled) or feature_push_backlog <= int(settings.feature_push_backlog_warn)
    )
    feature_parity_ok = bool(feature_parity_breaches == 0)
    rollout_policy = dict(dict(state.get("runtime_diag") or {}).get("rollout_policy") or {})
    rollout_runtime = dict(dict(dict(state.get("runtime_diag") or {}).get("risk_cycle_summary") or {}).get("rollout") or {})
    provider_health = dict(state.get("provider_health") or {})
    capital_governance = dict(state.get("capital_governance") or {})
    live_pair_candidates = [
        str(item).upper()
        for item in (
            list(state.get("canary_pairs") or [])
            or list(rollout_runtime.get("active_pairs") or [])
            or list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        )
        if str(item).strip()
    ]
    live_pair_key = str(live_pair_candidates[0]) if live_pair_candidates else ""
    paper_commands = service.get_commands(limit=100)
    paper_events = service.get_command_events(limit=200)
    paper_execution = _paper_execution_summary(
        commands=paper_commands,
        events=paper_events,
        runs=service.get_orchestration_runs(limit=20, runtime_mode="paper"),
    )
    live_commands = service.get_commands(limit=100)
    live_events = service.get_command_events(limit=200)
    live_runs = service.get_orchestration_runs(limit=20, runtime_mode="live")
    active_release = service.get_active_model_set(live_pair_key) if live_pair_key else {}
    orchestration_live = _orchestration_live_summary(
        state=state,
        commands=live_commands,
        events=live_events,
        runs=live_runs,
        active_release=active_release,
    )
    orchestration_evidence = _orchestration_evidence_summary()

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

    orchestration_live_health = _orchestration_live_health_summary(
        orchestration_live,
        runtime_status=runtime_status,
        runtime_ready=runtime_ready,
        status_tier=status_tier,
    )

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
        "runtime_startup_failure_history": runtime_startup_failure_history,
        "runtimeStartupFailureHistory": runtime_startup_failure_history,
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
        "startup_inference_by_pair": startup_inference_by_pair,
        "startupInferenceByPair": startup_inference_by_pair,
        "feature_serving_by_pair": feature_serving_by_pair,
        "featureServingByPair": feature_serving_by_pair,
        "pair_readiness": pair_readiness,
        "pairReadiness": pair_readiness,
        "strategy_engine_mode": strategy_engine_mode,
        "strategyEngineMode": strategy_engine_mode,
        "supervised_fallback": supervised_fallback,
        "supervisedFallback": supervised_fallback,
        "challenger_conflict": challenger_conflict,
        "challengerConflict": challenger_conflict,
        "entry_execution_policy": entry_execution_policy,
        "entryExecutionPolicy": entry_execution_policy,
        "rl_portfolio_proposal": rl_portfolio_proposal,
        "rlPortfolioProposal": rl_portfolio_proposal,
        "rl_execution_policy": rl_execution_policy,
        "rlExecutionPolicy": rl_execution_policy,
        "rl_lifecycle_summary": rl_lifecycle_summary,
        "rlLifecycleSummary": rl_lifecycle_summary,
        "rl_rebalance_summary": rl_rebalance_summary,
        "rlRebalanceSummary": rl_rebalance_summary,
        "rl_flip_intent": rl_flip_intent,
        "rlFlipIntent": rl_flip_intent,
        "rl_artifact_readiness": rl_artifact_readiness,
        "rlArtifactReadiness": rl_artifact_readiness,
        "rl_checkpoint_loaded": bool(state.get("rl_checkpoint_loaded", False)),
        "rlCheckpointLoaded": bool(state.get("rl_checkpoint_loaded", False)),
        "rl_checkpoint_path": str(state.get("rl_checkpoint_path") or ""),
        "rlCheckpointPath": str(state.get("rl_checkpoint_path") or ""),
        "rl_proposal_source": str(state.get("rl_proposal_source") or ""),
        "rlProposalSource": str(state.get("rl_proposal_source") or ""),
        "rl_supervised_fallback_used": bool(state.get("rl_supervised_fallback_used", False)),
        "rlSupervisedFallbackUsed": bool(state.get("rl_supervised_fallback_used", False)),
        "rl_fallback_reason": str(state.get("rl_fallback_reason") or ""),
        "rlFallbackReason": str(state.get("rl_fallback_reason") or ""),
        "rl_routed_entry_count": int(state.get("rl_routed_entry_count") or 0),
        "rlRoutedEntryCount": int(state.get("rl_routed_entry_count") or 0),
        "rl_blocked_entry_count": int(state.get("rl_blocked_entry_count") or 0),
        "rlBlockedEntryCount": int(state.get("rl_blocked_entry_count") or 0),
        "rl_fallback_entry_count": int(state.get("rl_fallback_entry_count") or 0),
        "rlFallbackEntryCount": int(state.get("rl_fallback_entry_count") or 0),
        "rl_scaled_entry_count": int(state.get("rl_scaled_entry_count") or 0),
        "rlScaledEntryCount": int(state.get("rl_scaled_entry_count") or 0),
        "tick_status": str(state.get("tick_status") or "unknown"),
        "tick_reason": str(state.get("tick_reason") or "unknown"),
        "feature_serving": feature_serving,
        "feature_observability": feature_observability,
        "featureObservability": feature_observability,
        "feature_serving_source": str(state.get("feature_serving_source") or ""),
        "feature_serving_reason": str(state.get("feature_serving_reason") or ""),
        "feature_serving_cache_hit": bool(state.get("feature_serving_cache_hit", False)),
        "feature_serving_stale": bool(state.get("feature_serving_stale", False)),
        "feature_serving_feature_service": str(state.get("feature_serving_feature_service") or ""),
        "feature_online_ready": feature_online_ready,
        "feature_data_fresh": feature_data_fresh,
        "feature_bar_status": str(state.get("feature_bar_status") or feature_observability.get("feature_bar_status") or ""),
        "featureBarStatus": str(state.get("featureBarStatus") or feature_observability.get("feature_bar_status") or ""),
        "feature_push_backlog_ok": feature_push_backlog_ok,
        "feature_parity_ok": feature_parity_ok,
        "feature_push_backlog": feature_push_backlog,
        "feature_parity_breaches": feature_parity_breaches,
        "feature_push_backlog_warn": int(feature_observability.get("feature_push_backlog_warn") or 0),
        "feature_push_backlog_overage": int(feature_observability.get("feature_push_backlog_overage") or 0),
        "feature_blocker_reason": str(feature_observability.get("feature_blocker_reason") or ""),
        "feature_blocker_reasons": list(feature_observability.get("feature_blocker_reasons") or []),
        "feature_blocker_source": str(feature_observability.get("feature_blocker_source") or ""),
        "paper_execution": paper_execution,
        "paperExecution": paper_execution,
        "providerHealth": provider_health,
        "provider_health": provider_health,
        "providerRoles": dict(provider_health.get("roles") or state.get("provider_roles") or {}),
        "provider_roles": dict(provider_health.get("roles") or state.get("provider_roles") or {}),
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
        "capital_governance": capital_governance,
        "capital_band": str(state.get("capital_band") or ""),
        "governance_mode": str(state.get("governance_mode") or ""),
        "status_tier": status_tier,
        "orchestration_live": orchestration_live,
        "orchestrationLive": orchestration_live,
        "orchestration_live_health": orchestration_live_health,
        "orchestrationLiveHealth": orchestration_live_health,
        "orchestration_evidence": orchestration_evidence,
        "orchestrationEvidence": orchestration_evidence,
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
        supplied_mid = _safe_float(row.get("mid"), 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else supplied_mid
        if mid <= 0.0:
            continue
        has_two_sided_quote = bid > 0.0 and ask > 0.0
        spread_px = max(0.0, ask - bid) if has_two_sided_quote else None
        bar_bid = bid if has_two_sided_quote else None
        bar_ask = ask if has_two_sided_quote else None

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
                "bid_open": float(bar_bid) if bar_bid is not None else None,
                "bid_high": float(bar_bid) if bar_bid is not None else None,
                "bid_low": float(bar_bid) if bar_bid is not None else None,
                "bid_close": float(bar_bid) if bar_bid is not None else None,
                "ask_open": float(bar_ask) if bar_ask is not None else None,
                "ask_high": float(bar_ask) if bar_ask is not None else None,
                "ask_low": float(bar_ask) if bar_ask is not None else None,
                "ask_close": float(bar_ask) if bar_ask is not None else None,
                "spread": float(spread_px) if spread_px is not None else None,
                "_spread_sum": float(spread_px) if spread_px is not None else 0.0,
                "_spread_count": 1 if spread_px is not None else 0,
                "volume": 1,
            }
            continue

        bar["high"] = max(float(bar["high"]), float(mid))
        bar["low"] = min(float(bar["low"]), float(mid))
        bar["close"] = float(mid)
        bar["mid_high"] = max(float(bar["mid_high"]), float(mid))
        bar["mid_low"] = min(float(bar["mid_low"]), float(mid))
        bar["mid_close"] = float(mid)
        if has_two_sided_quote:
            if bar.get("bid_open") is None:
                bar["bid_open"] = float(bar_bid)
                bar["bid_high"] = float(bar_bid)
                bar["bid_low"] = float(bar_bid)
            else:
                bar["bid_high"] = max(float(bar["bid_high"]), float(bar_bid))
                bar["bid_low"] = min(float(bar["bid_low"]), float(bar_bid))
            bar["bid_close"] = float(bar_bid)
            if bar.get("ask_open") is None:
                bar["ask_open"] = float(bar_ask)
                bar["ask_high"] = float(bar_ask)
                bar["ask_low"] = float(bar_ask)
            else:
                bar["ask_high"] = max(float(bar["ask_high"]), float(bar_ask))
                bar["ask_low"] = min(float(bar["ask_low"]), float(bar_ask))
            bar["ask_close"] = float(bar_ask)
            bar["_spread_sum"] = float(bar.get("_spread_sum", 0.0)) + float(spread_px)
            bar["_spread_count"] = int(bar.get("_spread_count", 0)) + 1
        bar["volume"] = int(bar.get("volume", 0)) + 1

    out: list[dict[str, Any]] = []
    for k in sorted(buckets.keys()):
        bar = dict(buckets[k])
        spread_count = int(bar.pop("_spread_count", 0))
        spread_sum = float(bar.pop("_spread_sum", 0.0))
        bar["spread"] = float(spread_sum / spread_count) if spread_count > 0 else None
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


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return {}
        try:
            decoded = json.loads(txt)
        except Exception:
            return value
        return decoded if isinstance(decoded, (dict, list)) else value
    return value


def _json_dict(value: Any) -> dict[str, Any]:
    raw = _json_value(value)
    return dict(raw) if isinstance(raw, dict) else {}


def _json_list(value: Any) -> list[Any]:
    raw = _json_value(value)
    return list(raw) if isinstance(raw, list) else []


def _table_rows(
    table,
    *,
    limit: int = 200,
    where: list[Any] | None = None,
    order_by: Any | None = None,
) -> list[dict[str, Any]]:
    store = getattr(service, "store", None)
    engine = getattr(store, "engine", None)
    if engine is None or table is None:
        return []
    try:
        stmt = select(table)
        for clause in list(where or []):
            stmt = stmt.where(clause)
        if order_by is None:
            if "created_at" in getattr(table, "c", {}):
                order_by = table.c.created_at.desc()
            elif "updated_at" in getattr(table, "c", {}):
                order_by = table.c.updated_at.desc()
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = stmt.limit(max(1, min(int(limit), 5000)))
        with engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _table_count(table, *, where: list[Any] | None = None) -> int:
    store = getattr(service, "store", None)
    engine = getattr(store, "engine", None)
    if engine is None or table is None:
        return 0
    try:
        stmt = select(func.count()).select_from(table)
        for clause in list(where or []):
            stmt = stmt.where(clause)
        with engine.connect() as conn:
            value = conn.execute(stmt).scalar_one_or_none()
        return int(value or 0)
    except Exception:
        return 0


def _approval_event_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row.get("event_id") or ""),
        "subject_type": str(row.get("subject_type") or ""),
        "subject_id": str(row.get("subject_id") or ""),
        "approver": str(row.get("approver") or ""),
        "decision": str(row.get("decision") or ""),
        "reason": str(row.get("reason") or ""),
        "created_at": float(_safe_float(row.get("created_at"), 0.0)),
    }


def _experiment_promotion_record(row: dict[str, Any]) -> dict[str, Any]:
    approval_records = _json_list(row.get("approval_records_json"))
    paper_results = _json_dict(row.get("paper_results_json"))
    canary_results = _json_dict(row.get("canary_results_json"))
    replay_results = _json_dict(row.get("replay_results_json"))
    rollback_metadata = _json_dict(row.get("rollback_metadata_json"))
    artefact_hashes = _json_dict(row.get("artefact_hashes_json"))
    config_diff = _json_dict(row.get("config_diff_json"))
    return {
        "promotion_id": str(row.get("promotion_id") or ""),
        "experiment_id": str(row.get("experiment_id") or ""),
        "prompt_hash": str(row.get("prompt_hash") or ""),
        "tool_trace_hash": str(row.get("tool_trace_hash") or ""),
        "model_id": str(row.get("model_id") or ""),
        "config_diff": config_diff,
        "replay_window": str(row.get("replay_window") or ""),
        "replay_results": replay_results,
        "approval_records": approval_records,
        "paper_results": paper_results,
        "canary_results": canary_results,
        "release_manifest_ref": str(row.get("release_manifest_ref") or ""),
        "rollback_metadata": rollback_metadata,
        "artefact_hashes": artefact_hashes,
        "status": str(row.get("status") or ""),
        "created_at": float(_safe_float(row.get("created_at"), 0.0)),
        "updated_at": float(_safe_float(row.get("updated_at"), 0.0)),
        "lineage": {
            "experiment_id": str(row.get("experiment_id") or ""),
            "promotion_id": str(row.get("promotion_id") or ""),
            "status": str(row.get("status") or ""),
            "approval_record_count": int(len(approval_records)),
            "paper_result_keys": sorted(paper_results.keys()),
            "canary_result_keys": sorted(canary_results.keys()),
            "release_manifest_ref": str(row.get("release_manifest_ref") or ""),
        },
    }


def _experiment_proposal_record(
    row: dict[str, Any],
    *,
    approval_records: list[dict[str, Any]] | None = None,
    promotions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    change_set = _json_dict(row.get("change_set_json"))
    evaluation_plan = _json_dict(row.get("evaluation_plan_json"))
    risk_notes = _json_dict(row.get("risk_notes_json"))
    evidence_refs = _json_dict(row.get("evidence_refs_json"))
    input_artefacts = _json_dict(row.get("input_artefact_refs_json"))
    config_diff = _json_dict(row.get("config_diff_json"))
    approvals = list(approval_records or [])
    promo_items = list(promotions or [])
    latest_promotion = dict(promo_items[0] or {}) if promo_items else {}
    experiment_lineage_ref = str(
        evidence_refs.get("experiment_lineage_ref")
        or evidence_refs.get("experiment_lineage")
        or input_artefacts.get("experiment_lineage_ref")
        or input_artefacts.get("experiment_lineage")
        or ""
    )
    lineage = {
        "experiment_id": str(row.get("experiment_id") or ""),
        "source_run_id": str(row.get("source_run_id") or ""),
        "latest_stage": str(row.get("latest_stage") or ""),
        "latest_promotion_id": str(row.get("latest_promotion_id") or ""),
        "approval_status": str(row.get("approval_status") or ""),
        "experiment_lineage_ref": experiment_lineage_ref,
        "change_set": change_set,
        "evaluation_plan": evaluation_plan,
        "risk_notes": risk_notes,
        "evidence_refs": evidence_refs,
        "input_artefact_refs": input_artefacts,
        "config_diff": config_diff,
        "approval_count": int(len(approvals)),
        "promotion_count": int(len(promo_items)),
        "latest_promotion_status": str(latest_promotion.get("status") or ""),
    }
    return {
        "experiment_id": str(row.get("experiment_id") or ""),
        "source_run_id": str(row.get("source_run_id") or ""),
        "hypothesis": str(row.get("hypothesis") or ""),
        "change_set": change_set,
        "evaluation_plan": evaluation_plan,
        "risk_notes": risk_notes,
        "evidence_refs": evidence_refs,
        "prompt_hash": str(row.get("prompt_hash") or ""),
        "tool_trace_hash": str(row.get("tool_trace_hash") or ""),
        "model_id": str(row.get("model_id") or ""),
        "decision_seed": int(_safe_float(row.get("decision_seed"), 0.0)),
        "input_artefact_refs": input_artefacts,
        "config_diff": config_diff,
        "replay_window": str(row.get("replay_window") or ""),
        "artifact_root": str(row.get("artifact_root") or ""),
        "latest_stage": str(row.get("latest_stage") or ""),
        "latest_promotion_id": str(row.get("latest_promotion_id") or ""),
        "approval_status": str(row.get("approval_status") or ""),
        "created_at": float(_safe_float(row.get("created_at"), 0.0)),
        "lineage": lineage,
        "approvals": approvals,
        "promotions": promo_items,
        "latest_promotion": latest_promotion,
        "summary": {
            "approval_count": int(len(approvals)),
            "promotion_count": int(len(promo_items)),
            "latest_promotion_status": str(latest_promotion.get("status") or ""),
            "latest_promotion_id": str(latest_promotion.get("promotion_id") or ""),
        },
    }


def _orchestration_evidence_summary() -> dict[str, Any]:
    store = getattr(service, "store", None)
    if store is None:
        return {
            "experiment_count": 0,
            "promotion_count": 0,
            "approval_event_count": 0,
            "latest_experiment_id": "",
            "latest_promotion_id": "",
            "latest_approval_event_id": "",
            "latest_promotion_status": "",
            "latest_approval_decision": "",
            "latest_lineage": {},
        }
    experiments = _table_rows(store.experiment_proposals, limit=10)
    promotions = _table_rows(store.experiment_promotions, limit=10)
    approvals = _table_rows(store.approval_events, limit=10)
    latest_experiment = dict(experiments[0] or {}) if experiments else {}
    latest_promotion = dict(promotions[0] or {}) if promotions else {}
    latest_approval = dict(approvals[0] or {}) if approvals else {}
    latest_experiment_payload = _experiment_proposal_record(
        latest_experiment,
        approval_records=[_approval_event_record(latest_approval)] if latest_approval else [],
        promotions=[_experiment_promotion_record(latest_promotion)] if latest_promotion else [],
    ) if latest_experiment else {}
    latest_lineage = dict(latest_experiment_payload.get("lineage") or {})
    latest_lineage.update(
        {
            "latest_promotion_status": str(latest_promotion.get("status") or latest_lineage.get("latest_promotion_status") or ""),
            "latest_approval_decision": str(latest_approval.get("decision") or ""),
            "latest_approval_event_id": str(latest_approval.get("event_id") or ""),
            "latest_promotion_id": str(latest_promotion.get("promotion_id") or latest_lineage.get("latest_promotion_id") or ""),
        }
    )
    return {
        "experiment_count": _table_count(store.experiment_proposals),
        "promotion_count": _table_count(store.experiment_promotions),
        "approval_event_count": _table_count(store.approval_events),
        "latest_experiment_id": str(latest_experiment.get("experiment_id") or ""),
        "latest_promotion_id": str(latest_promotion.get("promotion_id") or ""),
        "latest_approval_event_id": str(latest_approval.get("event_id") or ""),
        "latest_promotion_status": str(latest_promotion.get("status") or ""),
        "latest_approval_decision": str(latest_approval.get("decision") or ""),
        "latest_lineage": latest_lineage,
    }


def _workflow_lineage_summary(
    *,
    pair: str,
    registry_meta: dict[str, Any],
    active_caps: dict[str, Any],
    promotion: dict[str, Any],
    report_refs: list[str],
) -> dict[str, Any]:
    activation_package = dict(active_caps.get("activation_package") or registry_meta.get("activation_package") or {})
    active_lineage = dict(active_caps.get("lineage") or active_caps.get("lineage_snapshot") or {})
    registry_lineage = dict(registry_meta.get("lineage") or registry_meta.get("lineage_snapshot") or {})
    lineage = dict(active_lineage or registry_lineage)
    lineage.update(
        {
            "pair": str(pair).upper(),
            "bundle_run_id": str(
                active_caps.get("bundle_run_id")
                or registry_meta.get("bundle_run_id")
                or activation_package.get("bundle_run_id")
                or ""
            ),
            "experiment_id": str(
                activation_package.get("experiment_id")
                or promotion.get("experiment_id")
                or registry_meta.get("experiment_id")
                or lineage.get("experiment_id")
                or ""
            ),
            "promotion_id": str(
                activation_package.get("promotion_id")
                or promotion.get("promotion_id")
                or registry_meta.get("promotion_id")
                or lineage.get("promotion_id")
                or ""
            ),
            "experiment_lineage_ref": str(
                activation_package.get("experiment_lineage_ref")
                or promotion.get("experiment_lineage_ref")
                or registry_meta.get("experiment_lineage_ref")
                or registry_meta.get("lineage_ref")
                or lineage.get("experiment_lineage_ref")
                or ""
            ),
            "promotion_status": str(
                promotion.get("status")
                or active_caps.get("promotion_status")
                or registry_meta.get("promotion_status")
                or activation_package.get("promotion_status")
                or lineage.get("promotion_status")
                or ""
            ),
            "approval_status": str(
                promotion.get("approval_status")
                or active_caps.get("approval_status")
                or dict(active_caps.get("promotion_summary") or {}).get("approval_status")
                or registry_meta.get("approval_status")
                or activation_package.get("approval_status")
                or lineage.get("approval_status")
                or ""
            ),
            "report_refs": list(report_refs),
            "report_count": int(len(report_refs)),
            "approval_record_count": int(len(_json_list(promotion.get("approval_records")))),
            "paper_evidence": _json_dict(promotion.get("paper_results")),
            "canary_evidence": _json_dict(promotion.get("canary_results")),
            "replay_evidence": _json_dict(promotion.get("replay_results")),
            "release_manifest_ref": str(
                (active_caps.get("activation_package") or {}).get("release_manifest_ref")
                or promotion.get("release_manifest_ref")
                or registry_meta.get("release_manifest_ref")
                or ""
            ),
        }
    )
    return lineage


def _load_orchestration_experiment_rows(limit: int = 200) -> list[dict[str, Any]]:
    return _table_rows(getattr(service.store, "experiment_proposals", None), limit=limit)


def _load_orchestration_promotion_rows(limit: int = 200) -> list[dict[str, Any]]:
    return _table_rows(getattr(service.store, "experiment_promotions", None), limit=limit)


def _load_orchestration_approval_rows(limit: int = 200) -> list[dict[str, Any]]:
    return _table_rows(getattr(service.store, "approval_events", None), limit=limit)


def _load_orchestration_experiment_approvals(experiment_id: str, limit: int = 200) -> list[dict[str, Any]]:
    store = getattr(service, "store", None)
    table = getattr(store, "approval_events", None)
    if store is None or table is None:
        return []
    return _table_rows(
        table,
        limit=limit,
        where=[table.c.subject_type.in_(["experiment", "experiment_proposal"]), table.c.subject_id == str(experiment_id)],
    )


def _load_orchestration_experiment_promotions(experiment_id: str, limit: int = 200) -> list[dict[str, Any]]:
    store = getattr(service, "store", None)
    table = getattr(store, "experiment_promotions", None)
    if store is None or table is None:
        return []
    return _table_rows(table, limit=limit, where=[table.c.experiment_id == str(experiment_id)])


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
            "lineage": dict(metadata.get("lineage") or metadata.get("lineage_snapshot") or {}),
            "promotion_summary": dict(metadata.get("promotion_summary") or {}),
            "experiment_summary": dict(metadata.get("experiment_summary") or {}),
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
    strategy_engine_mode = str(runtime_diag.get("strategy_engine_mode") or "supervised_legacy")
    supervised_fallback = dict(runtime_diag.get("supervised_fallback") or {})
    challenger_conflict = dict(runtime_diag.get("challenger_conflict") or {})
    rl_portfolio_proposal = dict(state.get("rl_portfolio_proposal") or state.get("rlPortfolioProposal") or {})
    rl_execution_policy = dict(state.get("rl_execution_policy") or state.get("rlExecutionPolicy") or {})
    rl_lifecycle_summary = dict(state.get("rl_lifecycle_summary") or state.get("rlLifecycleSummary") or {})
    rl_rebalance_summary = dict(state.get("rl_rebalance_summary") or state.get("rlRebalanceSummary") or {})
    rl_flip_intent = dict(state.get("rl_flip_intent") or state.get("rlFlipIntent") or {})
    rl_artifact_readiness = dict(state.get("rl_artifact_readiness") or state.get("rlArtifactReadiness") or {})
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
        activation_package = dict(active_caps.get("activation_package") or registry_meta.get("activation_package") or {})
        promotion_summary = dict(active_caps.get("promotion_summary") or registry_meta.get("promotion_summary") or {})
        promotion_summary.update(
            {
                "status": str(
                    promotion.get("status")
                    or registry_meta.get("promotion_status")
                    or promotion_summary.get("status")
                    or ""
                ),
                "approval_status": str(
                    promotion.get("approval_status")
                    or registry_meta.get("approval_status")
                    or promotion_summary.get("approval_status")
                    or ""
                ),
                "report_refs": list(report_refs),
                "report_count": int(len(report_refs)),
                "experiment_id": str(
                    activation_package.get("experiment_id")
                    or registry_meta.get("experiment_id")
                    or promotion_summary.get("experiment_id")
                    or ""
                ),
                "promotion_id": str(
                    activation_package.get("promotion_id")
                    or registry_meta.get("promotion_id")
                    or promotion_summary.get("promotion_id")
                    or ""
                ),
            }
        )
        lineage = _workflow_lineage_summary(
            pair=pair,
            registry_meta=registry_meta,
            active_caps=active_caps,
            promotion=promotion,
            report_refs=report_refs,
        )
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
                    "promotion_summary": dict(promotion_summary),
                    "lineage": dict(lineage),
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
                    "pair_readiness": dict((state.get("pair_readiness") or {}).get(pair) or {}),
                    "broker_symbol_readiness": dict(symbol_readiness.get(pair) or {}),
                    "strategy_engine_mode": strategy_engine_mode,
                    "supervised_fallback": dict(supervised_fallback),
                    "challenger_conflict": dict(challenger_conflict),
                    "rl_portfolio_proposal": dict(rl_portfolio_proposal),
                    "rl_execution_policy": dict(rl_execution_policy),
                    "rl_lifecycle_summary": dict(rl_lifecycle_summary),
                    "rl_rebalance_summary": dict(rl_rebalance_summary),
                    "rl_flip_intent": dict(rl_flip_intent),
                    "rl_artifact_readiness": dict(rl_artifact_readiness),
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
async def v2_get_bars(
    symbol: str = Query(..., min_length=1, max_length=32),
    timeframe: str = Query("H1", min_length=1, max_length=8),
    limit: int = Query(400, ge=1, le=2000),
) -> dict[str, Any]:
    sym = str(symbol).strip().upper()
    try:
        bars = _aggregate_bars(sym, timeframe, limit)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "symbol": sym, "timeframe": timeframe},
        ) from exc
    return {"symbol": sym, "timeframe": timeframe, "bars": bars, "limit": int(limit)}


@app.post("/v2/commands")
async def v2_post_command(command: CommandRequest) -> JSONResponse:
    payload = command.model_dump(exclude_none=True)
    out, code = service.submit_command(payload, proto="v2")
    _bridge_logger.info(
        "command submit symbol=%s action=%s lots=%s status=%s code=%s",
        payload.get("symbol"),
        payload.get("action") or payload.get("cmd"),
        payload.get("lots"),
        out.get("status"),
        code,
    )
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
async def v2_ack_command(ack: CommandAckRequest) -> JSONResponse:
    payload = ack.model_dump(exclude_none=True)
    out, code = service.ack_command(payload)
    _bridge_logger.info(
        "command ack command_id=%s ticket=%s status=%s code=%s",
        payload.get("command_id") or payload.get("id"),
        payload.get("ticket"),
        payload.get("status"),
        code,
    )
    return JSONResponse(content=out, status_code=code)


@app.post("/v2/market/tick")
async def v2_tick(tick_in: MarketTickRequest) -> dict[str, Any]:
    payload = tick_in.model_dump(exclude_none=False)
    sym = str(payload.get("symbol") or "").strip().upper()
    if sym:
        received_at = _utc_now_ts()
        ts_epoch = _parse_ts(payload.get("time") or payload.get("ts") or payload.get("timestamp"))
        if ts_epoch <= 0.0 or ts_epoch > received_at + 5.0:
            ts_epoch = received_at
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

    # When the EA submits JSON, validate against the ReportRequest schema so
    # malformed payloads fail fast (422) before they pollute storage. The
    # plain-text legacy path bypasses validation by design.
    if parsed is not None:
        try:
            validated_report = ReportRequest.model_validate(parsed)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "Report payload failed schema validation",
                    "errors": exc.errors(include_input=False),
                },
            ) from exc
        parsed = validated_report.model_dump(exclude_none=True)
        text = json.dumps(parsed, separators=(",", ":"), sort_keys=True, allow_nan=False)

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


@app.get("/v2/orchestration/runs")
async def v2_orchestration_runs_get(
    limit: int = Query(200),
    pair: str = Query(""),
    runtime_mode: str = Query(""),
    cycle_id: str = Query(""),
) -> dict[str, Any]:
    return {
        "items": service.get_orchestration_runs(
            limit=max(1, min(int(limit), 5000)),
            pair=pair,
            runtime_mode=runtime_mode,
            cycle_id=cycle_id,
        )
    }


@app.get("/v2/orchestration/traces")
async def v2_orchestration_traces_get(
    limit: int = Query(200),
    run_id: str = Query(""),
    pair: str = Query(""),
) -> dict[str, Any]:
    return {
        "items": service.get_orchestration_traces(
            limit=max(1, min(int(limit), 5000)),
            run_id=run_id,
            pair=pair,
        )
    }


@app.get("/v2/orchestration/experiments")
async def v2_orchestration_experiments_get(limit: int = Query(200)) -> dict[str, Any]:
    lim = max(1, min(int(limit), 5000))
    experiment_rows = _load_orchestration_experiment_rows(limit=lim)
    promotion_rows = _load_orchestration_promotion_rows(limit=lim)
    approval_rows = _load_orchestration_approval_rows(limit=lim)
    items: list[dict[str, Any]] = []
    for row in experiment_rows:
        experiment_id = str(row.get("experiment_id") or "")
        items.append(
            _experiment_proposal_record(
                row,
                approval_records=[_approval_event_record(item) for item in _load_orchestration_experiment_approvals(experiment_id, limit=lim)] if experiment_id else [],
                promotions=[_experiment_promotion_record(item) for item in _load_orchestration_experiment_promotions(experiment_id, limit=lim)] if experiment_id else [],
            )
        )
    experiment_status_counts: dict[str, int] = defaultdict(int)
    promotion_status_counts: dict[str, int] = defaultdict(int)
    approval_decision_counts: dict[str, int] = defaultdict(int)
    for item in items:
        experiment_status_counts[str(item.get("approval_status") or "").strip().lower()] += 1
    for row in promotion_rows:
        promotion_status_counts[str(row.get("status") or "").strip().lower()] += 1
    for row in approval_rows:
        approval_decision_counts[str(row.get("decision") or "").strip().lower()] += 1
    latest_lineage = dict(items[0].get("lineage") or {}) if items else {}
    return {
        "items": items,
        "summary": {
            "experiment_count": _table_count(getattr(service.store, "experiment_proposals", None)),
            "promotion_count": _table_count(getattr(service.store, "experiment_promotions", None)),
            "approval_event_count": _table_count(getattr(service.store, "approval_events", None)),
            "latest_experiment_id": str(items[0].get("experiment_id") or "") if items else "",
            "latest_promotion_id": str(promotion_rows[0].get("promotion_id") or "") if promotion_rows else "",
            "latest_approval_event_id": str(approval_rows[0].get("event_id") or "") if approval_rows else "",
            "latest_approval_decision": str(approval_rows[0].get("decision") or "") if approval_rows else "",
            "latest_lineage": latest_lineage,
            "approval_status_counts": {k: v for k, v in experiment_status_counts.items() if k},
            "promotion_status_counts": {k: v for k, v in promotion_status_counts.items() if k},
            "approval_decision_counts": {k: v for k, v in approval_decision_counts.items() if k},
        },
    }


@app.get("/v2/orchestration/experiments/{experiment_id}")
async def v2_orchestration_experiment_get(experiment_id: str, limit: int = Query(200)) -> dict[str, Any]:
    store = getattr(service, "store", None)
    table = getattr(store, "experiment_proposals", None)
    if store is None or table is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    experiment_rows = _table_rows(table, limit=1, where=[table.c.experiment_id == str(experiment_id)])
    if not experiment_rows:
        raise HTTPException(status_code=404, detail="experiment not found")
    approvals = _load_orchestration_experiment_approvals(experiment_id, limit=limit)
    promotions = _load_orchestration_experiment_promotions(experiment_id, limit=limit)
    experiment = _experiment_proposal_record(
        experiment_rows[0],
        approval_records=[_approval_event_record(row) for row in approvals],
        promotions=[_experiment_promotion_record(row) for row in promotions],
    )
    summary = dict(experiment.get("summary") or {})
    summary.update(
        {
            "experiment_count": _table_count(table, where=[table.c.experiment_id == str(experiment_id)]),
            "approval_event_count": _table_count(
                getattr(store, "approval_events", None),
                where=[
                    store.approval_events.c.subject_type.in_(["experiment", "experiment_proposal"]),
                    store.approval_events.c.subject_id == str(experiment_id),
                ],
            ),
            "promotion_count": _table_count(
                getattr(store, "experiment_promotions", None),
                where=[store.experiment_promotions.c.experiment_id == str(experiment_id)],
            ),
        }
    )
    return {"experiment": experiment, "summary": summary}


@app.get("/v2/orchestration/promotions")
async def v2_orchestration_promotions_get(limit: int = Query(200)) -> dict[str, Any]:
    lim = max(1, min(int(limit), 5000))
    promotion_rows = _load_orchestration_promotion_rows(limit=lim)
    approval_rows = _load_orchestration_approval_rows(limit=lim)
    items = [_experiment_promotion_record(row) for row in promotion_rows]
    promotion_status_counts: dict[str, int] = defaultdict(int)
    approval_decision_counts: dict[str, int] = defaultdict(int)
    for row in promotion_rows:
        promotion_status_counts[str(row.get("status") or "").strip().lower()] += 1
    for row in approval_rows:
        approval_decision_counts[str(row.get("decision") or "").strip().lower()] += 1
    return {
        "items": items,
        "summary": {
            "promotion_count": _table_count(getattr(service.store, "experiment_promotions", None)),
            "approval_event_count": _table_count(getattr(service.store, "approval_events", None)),
            "latest_promotion_id": str(items[0].get("promotion_id") or "") if items else "",
            "latest_experiment_id": str(items[0].get("experiment_id") or "") if items else "",
            "latest_promotion_status": str(items[0].get("status") or "") if items else "",
            "promotion_status_counts": {k: v for k, v in promotion_status_counts.items() if k},
            "approval_decision_counts": {k: v for k, v in approval_decision_counts.items() if k},
        },
    }


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
    health = dict(service.get_health() or {})
    database_ok = bool(health.get("tables_ok"))
    state["database_ok"] = database_ok
    state["database_status"] = str(health.get("database") or ("up" if database_ok else "degraded"))
    if not database_ok:
        state["status_tier"] = "bridge_up_db_unhealthy"
    state.update(_feature_observability_telemetry(state, metrics=service.get_metrics()))
    governance_events = service.get_governance_events(limit=50)
    last_runtime_startup_failure = _latest_runtime_startup_failure(governance_events)
    runtime_startup_failure_history = _runtime_startup_failure_history(governance_events)
    live_pair_candidates = [
        str(item).upper()
        for item in (
            list(state.get("canary_pairs") or [])
            or list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        )
        if str(item).strip()
    ]
    live_pair_key = str(live_pair_candidates[0]) if live_pair_candidates else ""
    commands = service.get_commands(limit=100)
    events = service.get_command_events(limit=200)
    paper_execution = _paper_execution_summary(
        commands=commands,
        events=events,
        runs=service.get_orchestration_runs(limit=20, runtime_mode="paper"),
    )
    orchestration_live = _orchestration_live_summary(
        state=state,
        commands=commands,
        events=events,
        runs=service.get_orchestration_runs(limit=20, runtime_mode="live"),
        active_release=service.get_active_model_set(live_pair_key) if live_pair_key else {},
    )
    state_runtime_ready = bool(
        str(state.get("runtime_status") or "").strip().lower() == "running"
        and state.get("runtime_cycle_age_secs") is not None
        and float(state.get("runtime_cycle_age_secs") or 0.0) <= 30.0
    )
    orchestration_live_health = _orchestration_live_health_summary(
        orchestration_live,
        runtime_status=str(state.get("runtime_status") or ""),
        runtime_ready=state_runtime_ready,
        status_tier=str(state.get("status_tier") or ""),
    )
    orchestration_evidence = _orchestration_evidence_summary()
    state["last_runtime_startup_failure"] = last_runtime_startup_failure
    state["lastRuntimeStartupFailure"] = last_runtime_startup_failure
    state["runtime_startup_failure_history"] = runtime_startup_failure_history
    state["runtimeStartupFailureHistory"] = runtime_startup_failure_history
    state["paper_execution"] = paper_execution
    state["paperExecution"] = paper_execution
    state["orchestration_live"] = orchestration_live
    state["orchestrationLive"] = orchestration_live
    state["orchestration_live_health"] = orchestration_live_health
    state["orchestrationLiveHealth"] = orchestration_live_health
    state["orchestration_evidence"] = orchestration_evidence
    state["orchestrationEvidence"] = orchestration_evidence
    return state


@app.post("/v2/state/decisions")
async def v2_state_decisions(body: StateDecisionsRequest) -> dict[str, Any]:
    decisions = list(body.decisions or [])
    diagnostics = dict(body.diagnostics or {})
    vol = float(body.vol or 0.0)
    service.store_decisions(decisions=decisions, vol=vol, diagnostics=diagnostics)
    return {"status": "ok", "stored": len(decisions)}


@app.get("/v2/metrics")
async def v2_metrics() -> dict[str, Any]:
    out = dict(service.get_metrics() or {})
    state = _state_with_liveness(service.get_state())
    state.update(_feature_observability_telemetry(state, metrics=out))
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
    out["feature_bar_status"] = str(state.get("feature_bar_status") or "")
    out["feature_push_backlog_ok"] = bool(state.get("feature_push_backlog_ok", False))
    out["feature_push_backlog"] = int(state.get("feature_push_backlog") or 0)
    out["feature_push_backlog_warn"] = int(state.get("feature_push_backlog_warn") or 0)
    out["feature_push_backlog_overage"] = int(state.get("feature_push_backlog_overage") or 0)
    out["feature_blocker_reason"] = str(state.get("feature_blocker_reason") or "")
    out["feature_blocker_reasons"] = list(state.get("feature_blocker_reasons") or [])
    out["feature_blocker_source"] = str(state.get("feature_blocker_source") or "")
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
    state.update(_feature_observability_telemetry(state, metrics=service.get_metrics()))
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
    out["feature_bar_status"] = str(state.get("feature_bar_status") or "")
    out["feature_push_backlog_ok"] = bool(state.get("feature_push_backlog_ok", False))
    out["feature_push_backlog"] = int(state.get("feature_push_backlog") or 0)
    out["feature_push_backlog_warn"] = int(state.get("feature_push_backlog_warn") or 0)
    out["feature_push_backlog_overage"] = int(state.get("feature_push_backlog_overage") or 0)
    out["feature_blocker_reason"] = str(state.get("feature_blocker_reason") or "")
    out["feature_blocker_reasons"] = list(state.get("feature_blocker_reasons") or [])
    out["feature_blocker_source"] = str(state.get("feature_blocker_source") or "")
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
    out["paper_execution"] = dict(state.get("paper_execution") or state.get("paperExecution") or {})
    out["paperExecution"] = dict(out["paper_execution"])
    out["orchestration_live_health"] = _orchestration_live_health_summary(
        dict(state.get("orchestration_live") or {}),
        runtime_status=str(state.get("runtime_status") or ""),
        runtime_ready=bool(
            str(state.get("runtime_status") or "").strip().lower() == "running"
            and state.get("runtime_cycle_age_secs") is not None
            and float(state.get("runtime_cycle_age_secs") or 0.0) <= 30.0
        ),
        status_tier=str(state.get("status_tier") or ""),
    )
    out["orchestrationLiveHealth"] = dict(out["orchestration_live_health"])
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


# AGENT HANDSHAKE: Kubernetes-style operational probes. Distinct from the
# legacy fat /v2/health and /v2/ready payloads (which return 200 with telemetry
# regardless of state). These return proper status codes (200 ok, 503 not
# ready) and a minimal JSON body so a container orchestrator can decide
# restart-vs-route-traffic without parsing nested telemetry.
@app.get("/v2/livez")
async def v2_livez() -> dict[str, str]:
    """Liveness probe — the ASGI app is running.

    Returns 200 unconditionally. This is what an orchestrator hits to decide
    *whether to restart the container*. It deliberately does **not** check
    downstream dependencies; a DB blip should not cause container churn.
    For "should this instance serve traffic?", use ``/v2/readyz`` instead.
    """
    return {"status": "ok"}


@app.get("/v2/readyz")
async def v2_readyz(response: Response) -> dict[str, Any]:
    """Readiness probe — the bridge can serve traffic right now.

    Returns 200 + check breakdown when all of these are true:

    * ``database_ok`` — DB schema present and reachable
    * ``runtime_running`` — the runtime loop has reported a cycle within 30s
    * ``mt4_fresh`` — MT4 heartbeat received within the configured stale-after
    * ``not_draining`` — service has not entered shutdown drain

    Returns 503 with the same payload when any check fails — that's the
    contract Kubernetes-style probes expect, and what load balancers use to
    decide whether to send traffic to this instance.
    """
    state = _state_with_liveness(service.get_state())
    health = dict(service.get_health() or {})

    runtime_status = str(state.get("runtime_status") or "unknown").strip().lower()
    runtime_cycle_age = _runtime_cycle_age_secs(state)
    runtime_running = bool(
        runtime_status == "running"
        and runtime_cycle_age is not None
        and runtime_cycle_age <= 30.0
    )

    mt4_status = str(state.get("system_status") or "unknown").strip().lower()
    heartbeat_age = state.get("heartbeat_age_secs")
    heartbeat_stale_after = float(state.get("heartbeat_stale_after_secs") or 30.0)
    mt4_fresh = bool(
        mt4_status == "connected"
        and heartbeat_age is not None
        and float(heartbeat_age) <= heartbeat_stale_after
    )

    not_draining = not bool(getattr(service, "draining", False))

    checks = {
        "database_ok": bool(health.get("tables_ok")),
        "runtime_running": runtime_running,
        "mt4_fresh": mt4_fresh,
        "not_draining": not_draining,
    }
    ready = all(checks.values())
    if not ready:
        response.status_code = 503
    return {"ready": ready, "checks": checks}


# AGENT HANDSHAKE: Wire-protocol negotiation between bridge, MT4 EA, dashboard,
# and runtime. Public (no auth) so clients can verify the protocol version
# before they attempt authenticated calls — otherwise a stale key would mask a
# version mismatch behind a 401.
# AGENT HANDSHAKE: Prometheus-compatible operational metrics surface, distinct
# from the JSON `/v2/metrics` firehose. Intended to be scraped by Prometheus /
# Grafana. Requires the bridge API key (same as `/v2/metrics`); operators
# configure their scrape job with the `Authorization: X-API-Key` header.
@app.get("/v2/metrics/prometheus")
async def v2_metrics_prometheus() -> PlainTextResponse:
    body = collect_and_render(
        service=service,
        settings_obj=settings,
        state_with_liveness=_state_with_liveness,
    )
    return PlainTextResponse(content=body, media_type=PROMETHEUS_CONTENT_TYPE)


@app.get("/v2/handshake")
async def v2_handshake() -> HandshakeResponse:
    from fxstack.api.auth import _PUBLIC_PATHS

    return HandshakeResponse(
        protocol_version=BRIDGE_PROTOCOL_VERSION,
        min_compatible=BRIDGE_PROTOCOL_MIN_COMPATIBLE,
        server="fxstack-bridge",
        build=_handshake_build(),
        auth_required=bool(settings.bridge_auth_required),
        public_paths=sorted(_PUBLIC_PATHS),
        basket_tp_pct=float(settings.basket_tp_pct),
    )


def _reconcile_db_positions() -> list[PositionView]:
    """Return open positions as known to the bridge / runtime state.

    Delegates to :meth:`RuntimeService.get_open_positions` which normalizes the
    underlying state shape to a list of dicts.
    """
    out: list[PositionView] = []
    for pos in service.get_open_positions() or []:
        sym = str(pos.get("symbol") or "").strip().upper()
        if not sym:
            continue
        try:
            out.append(
                PositionView(
                    symbol=sym,
                    side=(str(pos.get("side") or "").upper() or None),
                    lots=(float(pos.get("lots") or 0.0) or None),
                    ticket=pos.get("ticket"),
                    source="db",
                )
            )
        except Exception:
            continue
    return out


def _reconcile_ea_positions() -> tuple[list[PositionView], float | None]:
    """Return EA-reported positions from the most-recent ``positions_snapshot`` report.

    The EA is expected to post a structured report with ``report_type=positions_snapshot``
    on its own cadence. Until the EA is updated to do so, this function returns
    an empty list and ``None`` snapshot age, and the reconcile endpoint flags
    ``ea_snapshot_available=False``.
    """
    reports = service.get_reports(limit=200) or []
    latest_ts: float | None = None
    latest_payload: dict[str, Any] | None = None
    for row in reports:
        payload = _report_payload(str(row.get("report_text", "") or ""), row.get("report_json"))
        if str(payload.get("report_type") or "").strip().lower() != "positions_snapshot":
            continue
        ts = _safe_float(row.get("ts"), 0.0)
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_payload = payload
    if not latest_payload:
        return [], None
    positions_raw = latest_payload.get("positions") or []
    out: list[PositionView] = []
    if isinstance(positions_raw, list):
        for pos in positions_raw:
            if not isinstance(pos, dict):
                continue
            try:
                sym = str(pos.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                out.append(
                    PositionView(
                        symbol=sym,
                        side=(str(pos.get("side") or "").upper() or None),
                        lots=(float(pos.get("lots") or 0.0) or None),
                        ticket=pos.get("ticket"),
                        source="ea",
                    )
                )
            except Exception:
                continue
    age: float | None = None
    if latest_ts is not None and latest_ts > 0:
        age = max(0.0, _utc_now_ts() - latest_ts)
    return out, age


# AGENT HANDSHAKE: Position-truth reconciliation between the bridge's DB view
# and the EA's reported snapshot. Informational only — does not mutate state.
# Runtime / ops should poll this on startup and on a slow cadence, alerting on
# any non-empty diff. Once the EA emits `positions_snapshot` reports, this
# endpoint provides the canonical broker-vs-runtime divergence view.
@app.get("/v2/positions/reconcile")
async def v2_positions_reconcile() -> PositionReconcileResponse:
    db_positions = _reconcile_db_positions()
    ea_positions, ea_age = _reconcile_ea_positions()

    db_syms = {p.symbol for p in db_positions}
    ea_syms = {p.symbol for p in ea_positions}
    only_db = sorted(db_syms - ea_syms)
    only_ea = sorted(ea_syms - db_syms)

    ea_by_sym = {p.symbol: p for p in ea_positions}
    lot_mismatches: list[dict[str, Any]] = []
    for db_pos in db_positions:
        ea_pos = ea_by_sym.get(db_pos.symbol)
        if ea_pos is None:
            continue
        db_lots = float(db_pos.lots or 0.0)
        ea_lots = float(ea_pos.lots or 0.0)
        if abs(db_lots - ea_lots) > 1e-6:
            lot_mismatches.append(
                {
                    "symbol": db_pos.symbol,
                    "db_lots": db_lots,
                    "ea_lots": ea_lots,
                }
            )

    response = PositionReconcileResponse(
        db_positions=db_positions,
        ea_positions=ea_positions,
        only_in_db=only_db,
        only_in_ea=only_ea,
        lot_mismatches=lot_mismatches,
        ea_snapshot_age_secs=ea_age,
        ea_snapshot_available=bool(ea_positions or ea_age is not None),
    )

    if only_db or only_ea or lot_mismatches:
        _bridge_logger.warning(
            "position reconcile divergence: only_db=%s only_ea=%s lot_mismatches=%d",
            only_db,
            only_ea,
            len(lot_mismatches),
        )
    return response
