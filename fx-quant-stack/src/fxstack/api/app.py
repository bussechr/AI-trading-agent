from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import json
from typing import Any

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


app = FastAPI(title="fx-quant-stack bridge", version="0.1.0")
settings = get_settings()
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

        bucket = int(ts // tf_sec) * tf_sec
        bar = buckets.get(bucket)
        if bar is None:
            buckets[bucket] = {
                "time": _iso(float(bucket)),
                "open": float(mid),
                "high": float(mid),
                "low": float(mid),
                "close": float(mid),
                "volume": 1,
            }
            continue

        bar["high"] = max(float(bar["high"]), float(mid))
        bar["low"] = min(float(bar["low"]), float(mid))
        bar["close"] = float(mid)
        bar["volume"] = int(bar.get("volume", 0)) + 1

    out = [buckets[k] for k in sorted(buckets.keys())]
    lim = max(1, min(int(limit), 2000))
    return out[-lim:]


@app.post("/v2/thought")
async def v2_thought(payload: dict[str, Any]) -> dict[str, Any]:
    thought = str(payload.get("thought", ""))
    service.patch_state({"current_thought": thought})
    return {"status": "ok", "thought": thought}


@app.get("/v2/monitor")
async def v2_monitor() -> dict[str, Any]:
    state = service.get_state()
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
    return dict(_market_ticks_mem)


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
    service.record_tick(payload)
    sym = str(payload.get("symbol", "")).strip().upper()
    if sym:
        ts_epoch = _parse_ts(payload.get("time") or payload.get("ts") or payload.get("timestamp"))
        tick = {
            "symbol": sym,
            "bid": _safe_float(payload.get("bid"), 0.0),
            "ask": _safe_float(payload.get("ask"), 0.0),
            "spread": _safe_float(payload.get("spread"), 0.0),
            "ts_epoch": ts_epoch,
            "time": _iso(ts_epoch),
        }
        _market_ticks_mem[sym] = tick
        _market_tick_history[sym].append(tick)
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


@app.get("/v2/state")
async def v2_state() -> dict[str, Any]:
    return service.get_state()


@app.post("/v2/state/decisions")
async def v2_state_decisions(payload: dict[str, Any]) -> dict[str, Any]:
    decisions = list(payload.get("decisions", []) or [])
    diagnostics = dict(payload.get("diagnostics", {}) or {})
    vol = float(payload.get("vol", 0.0) or 0.0)
    service.store_decisions(decisions=decisions, vol=vol, diagnostics=diagnostics)
    return {"status": "ok", "stored": len(decisions)}


@app.get("/v2/metrics")
async def v2_metrics() -> dict[str, Any]:
    return service.get_metrics()


@app.get("/v2/health")
async def v2_health() -> dict[str, Any]:
    state = service.get_state()
    out = service.get_health()
    out["last_heartbeat"] = state.get("last_heartbeat")
    out["system_status"] = state.get("system_status", "unknown")
    return out


@app.get("/v2/ping")
async def v2_ping() -> dict[str, Any]:
    return {"status": "ok"}
