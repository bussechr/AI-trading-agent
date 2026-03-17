#!/usr/bin/env python3
"""
HTTP bridge server for MT4 EA communication.
Provides reliable signal transport with idempotent IDs, ACK handling, and retry tracking.
"""

from __future__ import annotations

from flask import Flask, request, jsonify
from flask_cors import CORS
from collections import deque
import threading
import logging
from datetime import datetime
import json
import os
import sys
import time
import re
import random
from pathlib import Path

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from src.audit.interop_efficiency import parse_latency_buckets, percentile_triplet, bucketize
except Exception:
    def parse_latency_buckets(raw):
        vals = str(raw or "").replace(";", ",").split(",")
        out = []
        for v in vals:
            try:
                iv = int(float(v))
            except Exception:
                continue
            if iv > 0:
                out.append(iv)
        return sorted(set(out)) if out else [25, 50, 100, 250, 500, 1000, 1600, 2500, 5000]

    def percentile_triplet(values):
        clean = sorted(float(v) for v in values if isinstance(v, (int, float)))
        if not clean:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        n = len(clean)
        def _pick(p):
            i = int(round((max(0.0, min(100.0, p)) / 100.0) * (n - 1)))
            return float(clean[max(0, min(n - 1, i))])
        return {"p50": _pick(50.0), "p95": _pick(95.0), "p99": _pick(99.0)}

    def bucketize(values, buckets_ms):
        buckets = parse_latency_buckets(buckets_ms)
        counts = {str(b): 0 for b in buckets}
        counts["inf"] = 0
        for v in values:
            try:
                x = float(v)
            except Exception:
                continue
            placed = False
            for b in buckets:
                if x <= float(b):
                    counts[str(b)] += 1
                    placed = True
                    break
            if not placed:
                counts["inf"] += 1
        return counts

try:
    from src.trader.application.runtime_service import RuntimeService
    from src.trader.interfaces.config import load_trader_config
    _runtime_import_error = ""
except Exception as exc:
    RuntimeService = None
    load_trader_config = None
    _runtime_import_error = f"{exc.__class__.__name__}: {exc}"

def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _resolve_default_trader_config_path() -> str:
    raw = str(os.getenv("TRADER_CONFIG_PATH", "")).strip()
    if raw:
        return raw
    return str(PROJECT_ROOT / "src" / "config" / "fx_el_minis.yaml")


INTEROP_AUDIT_ENABLED = _env_bool("MT4_INTEROP_AUDIT_ENABLED", False)
INTEROP_AUDIT_TRACE_PATH = str(
    os.getenv("MT4_INTEROP_AUDIT_TRACE_PATH", "data/state/audit/interop/transport_trace.jsonl")
).strip()
try:
    INTEROP_AUDIT_SAMPLE_RATE = float(max(0.0, min(1.0, float(os.getenv("MT4_INTEROP_AUDIT_SAMPLE_RATE", "1.0")))))
except Exception:
    INTEROP_AUDIT_SAMPLE_RATE = 1.0
INTEROP_AUDIT_MODE = str(os.getenv("MT4_INTEROP_AUDIT_MODE", "live_shadow")).strip().lower()
if INTEROP_AUDIT_MODE not in {"live_shadow", "replay_live_like", "replay_offline"}:
    INTEROP_AUDIT_MODE = "live_shadow"
INTEROP_LATENCY_BUCKETS_MS = parse_latency_buckets(
    os.getenv("MT4_INTEROP_LATENCY_BUCKETS_MS", "25,50,100,250,500,1000,1600,2500,5000")
)
LEGACY_V1_COMPAT_ENABLED = _env_bool("MT4_BRIDGE_ENABLE_V1_COMPAT", False)

_runtime_service = None
_runtime_service_init_error = ""
if RuntimeService is not None and load_trader_config is not None:
    try:
        _runtime_service = RuntimeService(load_trader_config(_resolve_default_trader_config_path()))
    except Exception as exc:
        _runtime_service_init_error = str(exc)
        app.logger.exception("RuntimeService init failed, v2 endpoints disabled: %s", exc)
else:
    detail = str(_runtime_import_error or "").strip()
    if detail:
        _runtime_service_init_error = f"RuntimeService imports unavailable: {detail}"
    else:
        _runtime_service_init_error = "RuntimeService imports unavailable"

if LEGACY_V1_COMPAT_ENABLED:
    app.logger.warning("Legacy v1 bridge compatibility mode enabled (MT4_BRIDGE_ENABLE_V1_COMPAT=1)")
if _runtime_service is None:
    app.logger.error("v2 runtime service unavailable: %s", _runtime_service_init_error or "unknown")

# Report storage (from EA back to Python)
reports = deque(maxlen=2000)
report_lock = threading.Lock()

interop_lock = threading.Lock()
interop_latency_samples = {
    "signal_post_to_ack_ms": deque(maxlen=20000),
    "bridge_queue_wait_ms": deque(maxlen=20000),
    "poll_delivery_lag_ms": deque(maxlen=20000),
    "ea_handle_to_ack_ms": deque(maxlen=20000),
}
interop_error_budget: dict[str, int] = {}

# Trading state tracking
trading_state = {
    "last_heartbeat": None,
    "equity": 0.0,
    "positions": [],
    "cycle_active": False,
    "cycle_start_equity": 0.0,
    "cycle_target": 0.0,
    "signals_sent": 0,
    "trades_executed": 0,
    "last_signal": None,
    "last_ack": None,
    "agent_decisions": [],
    "agent_diagnostics": {},
    "monitor": {},
    "vol": 0.0,
    "system_status": "starting",
}
state_lock = threading.Lock()

# Visuals Store (for Indicator)
visuals = {}
visual_lock = threading.Lock()

# Market Data Store (Last known tick)
market_data = {}
market_tick_history: dict[str, deque] = {}
MAX_TICK_HISTORY_PER_SYMBOL = int(os.getenv("BRIDGE_MAX_TICK_HISTORY_PER_SYMBOL", "50000"))
md_lock = threading.Lock()

# -------- Helpers --------

def _now_iso() -> str:
    return datetime.now().isoformat()


def _safe_int(val, default: int = 0) -> int:
    try:
        return int(val)
    except Exception:
        return int(default)


def _safe_float(val, default: float = 0.0) -> float:
    try:
        return float(val)
    except Exception:
        return float(default)


def _iso_to_epoch(ts: str, default: float) -> float:
    try:
        return datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        return float(default)


def _interop_error(reason: str) -> None:
    key = str(reason or "unknown")
    with interop_lock:
        interop_error_budget[key] = int(interop_error_budget.get(key, 0)) + 1


def _interop_record_latency(stage: str, value_ms: float | None) -> None:
    if value_ms is None:
        return
    try:
        v = float(value_ms)
    except Exception:
        return
    if v < 0 or v != v:
        return
    if stage not in interop_latency_samples:
        return
    with interop_lock:
        interop_latency_samples[stage].append(v)


def _interop_stage_stats() -> dict:
    out = {}
    with interop_lock:
        for stage, vals in interop_latency_samples.items():
            arr = list(vals)
            out[stage] = {
                "count": int(len(arr)),
                "percentiles": dict(percentile_triplet(arr)),
                "buckets": dict(bucketize(arr, INTEROP_LATENCY_BUCKETS_MS)),
            }
    return out


def _interop_emit_trace(row: dict) -> None:
    if (not INTEROP_AUDIT_ENABLED) or (not INTEROP_AUDIT_TRACE_PATH):
        return
    if INTEROP_AUDIT_SAMPLE_RATE < 1.0 and random.random() > INTEROP_AUDIT_SAMPLE_RATE:
        return
    try:
        p = Path(INTEROP_AUDIT_TRACE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(row), sort_keys=True) + "\n")
    except Exception as exc:
        app.logger.debug("Interop trace write failed: %s", exc)


def _parse_positions_text(msg: str) -> list[dict]:
    tail = str(msg or "")[len("POSITIONS") :].strip()
    if not tail or tail == "NONE":
        return []

    out: list[dict] = []
    # Split at each new position token start.
    chunks = re.split(r"\s+(?=symbol=)", tail)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or chunk == "NONE":
            continue
        pos: dict = {}
        for kv in chunk.split(","):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            if k in {"lots", "profit", "open_price"}:
                pos[k] = _safe_float(v)
            elif k in {"type", "magic"}:
                pos[k] = _safe_int(v)
            elif k == "open_time":
                pos[k] = _safe_float(v)
            else:
                pos[k] = v
        if pos:
            out.append(pos)
    return out


def _apply_heartbeat_fields(msg: str) -> None:
    patch = {"last_heartbeat": _now_iso(), "system_status": "connected"}
    with state_lock:
        trading_state["last_heartbeat"] = _now_iso()
        trading_state["system_status"] = "connected"
        try:
            for tok in str(msg).split():
                if tok.startswith("eq="):
                    eq = _safe_float(tok.split("=", 1)[1], 0.0)
                    trading_state["equity"] = eq
                    patch["equity"] = eq
                elif tok.startswith("margin="):
                    margin = _safe_float(tok.split("=", 1)[1], 0.0)
                    trading_state["margin"] = margin
                    patch["margin"] = margin
                elif tok.startswith("freemargin="):
                    free = _safe_float(tok.split("=", 1)[1], 0.0)
                    trading_state["freemargin"] = free
                    patch["freemargin"] = free
                elif tok.startswith("lev="):
                    lev = _safe_float(tok.split("=", 1)[1], 0.0)
                    trading_state["leverage"] = lev
                    patch["leverage"] = lev
        except Exception as exc:
            app.logger.warning("Failed to parse heartbeat text: %s", exc)

    if _runtime_service is not None:
        try:
            _runtime_service.patch_state(patch)
        except Exception as exc:
            app.logger.debug("v2 heartbeat mirror skipped: %s", exc)


def _record_report(msg: str) -> None:
    with report_lock:
        reports.append({"time": _now_iso(), "message": msg})


def _ack_v2_payload(payload: dict) -> tuple[dict, int]:
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return out, code

    data = dict(payload or {})
    out, code = _runtime_service.ack_command(data)
    status = str((out or {}).get("status", "")).strip().lower()
    if status in {"acked", "failed"}:
        command_id = str(data.get("command_id") or data.get("signal_id") or (out or {}).get("command_id") or "").strip()
        cmd_row = None
        if command_id:
            try:
                cmd_row = _runtime_service.get_command(command_id)
            except Exception as exc:
                app.logger.debug("v2 ack trace command fetch failed: %s", exc)
        trace_payload = dict(data)
        if command_id and not str(trace_payload.get("command_id", "")).strip():
            trace_payload["command_id"] = command_id
        reason = str(data.get("message") or data.get("status_reason") or "")
        _emit_v2_ack_trace(trace_payload, outcome=status, rejection_reason=reason, command=cmd_row)
    return out, code


def _emit_v2_ack_trace(
    payload: dict,
    *,
    outcome: str,
    rejection_reason: str = "",
    command: dict | None = None,
) -> None:
    merged = dict(payload or {})
    cmd_payload: dict = {}
    if command:
        cmd_payload = dict((command or {}).get("payload", {}) or {})
        for key in ("command_id", "trace_id", "cmd", "symbol", "session_id", "intent", "proto"):
            if str(merged.get(key, "")).strip():
                continue
            val = (command or {}).get(key)
            if val not in (None, ""):
                merged[key] = val
        for key in ("delivered_count", "cycle_id"):
            if _safe_int(merged.get(key), 0) > 0:
                continue
            val = _safe_int((command or {}).get(key), 0)
            if val > 0:
                merged[key] = val
        for key in ("t_bridge_queued", "t_bridge_delivered", "t_bridge_ack_finalized"):
            if _safe_float(merged.get(key), 0.0) > 0.0:
                continue
            val = _safe_float((command or {}).get(key), 0.0)
            if val > 0.0:
                merged[key] = val

    if not str(merged.get("audit_session_id", "")).strip() and cmd_payload.get("audit_session_id"):
        merged["audit_session_id"] = cmd_payload.get("audit_session_id")
    if not str(merged.get("audit_profile", "")).strip() and cmd_payload.get("audit_profile"):
        merged["audit_profile"] = cmd_payload.get("audit_profile")
    if not str(merged.get("interop_mode", "")).strip() and cmd_payload.get("interop_mode"):
        merged["interop_mode"] = cmd_payload.get("interop_mode")
    if not str(merged.get("thought", "")).strip() and cmd_payload.get("thought"):
        merged["thought"] = cmd_payload.get("thought")

    if _safe_float(merged.get("t_py_signal_post_start"), 0.0) <= 0.0:
        py_start = _safe_float(cmd_payload.get("t_py_signal_post_start"), 0.0)
        if py_start <= 0.0:
            py_start = _safe_float((command or {}).get("created_at"), 0.0)
        if py_start > 0.0:
            merged["t_py_signal_post_start"] = py_start

    now_ts = float(time.time())
    signal_id = str(merged.get("command_id") or merged.get("signal_id") or "").strip()
    if not signal_id:
        return
    trace_id = str(merged.get("trace_id") or signal_id)
    mode = str(merged.get("interop_mode") or INTEROP_AUDIT_MODE)
    cmd = str(merged.get("cmd", ""))
    symbol = str(merged.get("symbol", ""))

    t_py_start = _safe_float(merged.get("t_py_signal_post_start"), 0.0)
    t_q = _safe_float(merged.get("t_bridge_queued"), 0.0)
    t_d = _safe_float(merged.get("t_bridge_delivered"), 0.0)
    t_ea_received = _safe_float(merged.get("t_ea_received"), 0.0)
    t_ea_exec_start = _safe_float(merged.get("t_ea_exec_start"), 0.0)
    t_ea_exec_end = _safe_float(merged.get("t_ea_exec_end"), 0.0)
    t_ea_ack_post = _safe_float(merged.get("t_ea_ack_post"), 0.0)
    ea_handle_to_ack_ms = _safe_float(merged.get("ea_handle_to_ack_ms"), 0.0)

    stage_lat: dict[str, float] = {}
    if t_py_start > 0:
        stage_lat["signal_post_to_ack_ms"] = max(0.0, (now_ts - t_py_start) * 1000.0)
        _interop_record_latency("signal_post_to_ack_ms", stage_lat["signal_post_to_ack_ms"])
    if t_q > 0 and t_d > 0:
        lag = max(0.0, (t_d - t_q) * 1000.0)
        stage_lat["bridge_queue_wait_ms"] = lag
        stage_lat["poll_delivery_lag_ms"] = lag
        _interop_record_latency("bridge_queue_wait_ms", lag)
        _interop_record_latency("poll_delivery_lag_ms", lag)
    if ea_handle_to_ack_ms > 0:
        stage_lat["ea_handle_to_ack_ms"] = float(ea_handle_to_ack_ms)
        _interop_record_latency("ea_handle_to_ack_ms", ea_handle_to_ack_ms)
    elif t_ea_received > 0 and t_ea_ack_post > 0:
        e2e = max(0.0, (t_ea_ack_post - t_ea_received) * 1000.0)
        stage_lat["ea_handle_to_ack_ms"] = float(e2e)
        _interop_record_latency("ea_handle_to_ack_ms", e2e)

    _interop_emit_trace(
        {
            "ts": float(now_ts),
            "phase": "transport",
            "mode": mode,
                "signal_id": signal_id,
                "trace_id": trace_id,
                "symbol": symbol,
                "cmd": cmd,
                "cycle_id": int(merged.get("cycle_id", 0) or 0),
                "audit_session_id": str(merged.get("audit_session_id", "")),
                "audit_profile": str(merged.get("audit_profile", "")),
                "t_py_signal_post_start": float(t_py_start if t_py_start > 0 else 0.0),
                "t_py_signal_post_end": float(t_q if t_q > 0 else 0.0),
                "t_bridge_queued": float(t_q),
                "t_bridge_delivered": float(t_d),
                "t_ea_received": float(t_ea_received),
                "t_ea_exec_start": float(t_ea_exec_start),
                "t_ea_exec_end": float(t_ea_exec_end),
                "t_ea_ack_post": float(t_ea_ack_post),
                "t_bridge_ack_finalized": float(now_ts),
                "stage_latencies_ms": stage_lat,
                "retries": int(merged.get("delivered_count", 0) or 0),
                "rejection_reason": str(rejection_reason or ""),
                "outcome": str(outcome),
            }
        )


def _decode_json_body() -> tuple[str, dict | None]:
    raw_bytes = request.data.replace(b"\x00", b"")
    msg = raw_bytes.decode("utf-8", errors="ignore").strip()
    payload = None
    if msg.startswith("{"):
        try:
            candidate = json.loads(msg)
            if isinstance(candidate, dict):
                payload = candidate
        except Exception:
            payload = None
    return msg, payload


def _ingest_tick_payload(data: dict) -> None:
    payload = dict(data or {})
    if not payload:
        raise ValueError("Empty tick data")

    symbol = str(payload.get("symbol", "")).strip()
    if not symbol:
        raise ValueError("Missing symbol")

    now_ts = time.time()
    now_iso = _now_iso()
    raw_tick_time = payload.get("time")
    if raw_tick_time:
        parsed = _iso_to_epoch(str(raw_tick_time), now_ts)
        if parsed > 0:
            now_ts = float(parsed)
            try:
                now_iso = datetime.fromtimestamp(now_ts).isoformat()
            except Exception:
                now_iso = _now_iso()
    else:
        ts_raw = payload.get("ts")
        if ts_raw is not None:
            ts_val = _safe_float(ts_raw, now_ts)
            if ts_val > 0:
                now_ts = float(ts_val)
                try:
                    now_iso = datetime.fromtimestamp(now_ts).isoformat()
                except Exception:
                    now_iso = _now_iso()

    bid = float(payload.get("bid", 0))
    ask = float(payload.get("ask", 0))
    spread = float(payload.get("spread", 0))

    with md_lock:
        market_data[symbol] = {
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "time": now_iso,
        }
        hist = market_tick_history.get(symbol)
        if hist is None:
            hist = deque(maxlen=MAX_TICK_HISTORY_PER_SYMBOL)
            market_tick_history[symbol] = hist
        hist.append(
            {
                "ts": float(now_ts),
                "time": str(now_iso),
                "bid": float(bid),
                "ask": float(ask),
                "spread": float(spread),
            }
        )

    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"[{now_str}] TICK {symbol} {payload.get('bid')}", flush=True)
    if _runtime_service is not None:
        try:
            mirror = dict(payload)
            mirror.setdefault("ts", float(now_ts))
            mirror.setdefault("time", str(now_iso))
            _runtime_service.record_tick(mirror)
        except Exception as exc:
            app.logger.debug("v2 tick mirror skipped: %s", exc)


def _process_report_message(msg: str, payload: dict | None) -> None:
    if payload is not None and isinstance(payload, dict):
        event_type = str(payload.get("type", "")).upper().strip()
        _record_report(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        if _runtime_service is not None:
            try:
                _runtime_service.record_report(msg, payload)
            except Exception as exc:
                app.logger.debug("v2 report mirror skipped: %s", exc)

        if event_type == "HEARTBEAT":
            with state_lock:
                trading_state["last_heartbeat"] = _now_iso()
                trading_state["system_status"] = "connected"
                trading_state["equity"] = _safe_float(payload.get("equity", 0.0), 0.0)
                trading_state["margin"] = _safe_float(payload.get("margin", 0.0), 0.0)
                trading_state["freemargin"] = _safe_float(payload.get("freemargin", 0.0), 0.0)
                if payload.get("leverage") is not None:
                    trading_state["leverage"] = _safe_float(payload.get("leverage", 0.0), 0.0)
        elif event_type == "POSITIONS":
            positions = list(payload.get("positions", []) or [])
            with state_lock:
                trading_state["positions"] = positions
                trading_state["last_pos_update"] = _now_iso()
        elif event_type == "ACK":
            out, _ = _ack_v2_payload(payload)
            app.logger.info("[REPORT_ACK] %s", out)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] REPORT: {msg}", flush=True)
        app.logger.info("[REPORT] %s", msg)
        return

    # Legacy text report path
    _record_report(msg)
    if _runtime_service is not None:
        try:
            _runtime_service.record_report(msg, None)
        except Exception as exc:
            app.logger.debug("v2 report mirror skipped: %s", exc)

    if "HEARTBEAT" in msg:
        _apply_heartbeat_fields(msg)

    elif "CYCLE_START" in msg:
        with state_lock:
            trading_state["cycle_active"] = True
            if "eq=" in msg:
                try:
                    eq_str = msg.split("eq=", 1)[1].split()[0]
                    eq = _safe_float(eq_str, 0.0)
                    trading_state["cycle_start_equity"] = eq
                    trading_state["cycle_target"] = eq * 0.01
                except Exception:
                    pass

    elif "CYCLE_TARGET_HIT" in msg:
        with state_lock:
            trading_state["cycle_active"] = False

    elif msg.startswith("OK BUY") or msg.startswith("OK SELL"):
        with state_lock:
            trading_state["trades_executed"] = int(trading_state.get("trades_executed", 0)) + 1

    elif msg.startswith("POSITIONS"):
        positions = _parse_positions_text(msg)
        with state_lock:
            trading_state["positions"] = positions
            trading_state["last_pos_update"] = _now_iso()
        if _runtime_service is not None:
            try:
                _runtime_service.patch_state({"positions": list(positions), "last_pos_update": _now_iso()})
            except Exception as exc:
                app.logger.debug("v2 position mirror skipped: %s", exc)

    elif msg.startswith("ACK "):
        # Optional text ACK fallback: ACK signal_id=<id> status=ACKED ...
        ack_payload: dict = {}
        for tok in msg.split()[1:]:
            if "=" not in tok:
                continue
            k, v = tok.split("=", 1)
            ack_payload[k.strip()] = v.strip()
        _ack_v2_payload(ack_payload)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] REPORT: {msg}", flush=True)
    app.logger.info("[REPORT] %s", msg)


# -------- Routes --------

@app.route('/v2/thought', methods=['POST'])
def v2_thought():
    """Agent posts its current thought process/status."""
    try:
        data = request.get_json(silent=True) or {}
        text = str(data.get("thought", ""))
        if len(text) > 4000:
            text = text[:4000]
        with state_lock:
            trading_state["current_thought"] = text

        print(f"[{datetime.now().strftime('%H:%M:%S')}] THOUGHT: {text}", flush=True)
        return jsonify({"status": "ok"}), 200
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


def _build_monitor_payload() -> dict:
    with state_lock:
        monitor_payload = dict(trading_state.get("monitor", {}) or {})
        if not monitor_payload:
            monitor_payload = dict((trading_state.get("agent_diagnostics", {}) or {}).get("monitor", {}) or {})
        entry = dict(monitor_payload.get("entry", {}) or {})
        close = dict(monitor_payload.get("close", {}) or {})
        close_positions = list(close.get("positions", []) or [])
        out = {
            "status": "ok",
            "time": _now_iso(),
            "bridge": {
                "system_status": str(trading_state.get("system_status", "unknown")),
                "last_heartbeat": trading_state.get("last_heartbeat"),
                "last_update": trading_state.get("last_update"),
            },
            "account": {
                "equity": _safe_float(trading_state.get("equity", 0.0), 0.0),
                "margin": _safe_float(trading_state.get("margin", 0.0), 0.0),
                "freemargin": _safe_float(trading_state.get("freemargin", 0.0), 0.0),
                "leverage": _safe_float(trading_state.get("leverage", 0.0), 0.0),
            },
            "positions": list(trading_state.get("positions", []) or []),
            "monitor": {
                "updated_ts": _safe_float(monitor_payload.get("updated_ts", 0.0), 0.0),
                "cycle_id": _safe_int(monitor_payload.get("cycle_id", 0), 0),
                "warmup_mode": bool(monitor_payload.get("warmup_mode", False)),
                "starvation_mode": bool(monitor_payload.get("starvation_mode", False)),
                "relax_level": _safe_float(monitor_payload.get("relax_level", 0.0), 0.0),
                "daily_breaker_active": bool(monitor_payload.get("daily_breaker_active", False)),
                "entry": entry,
                "close": {
                    "close_proximity_pct": _safe_float(close.get("close_proximity_pct", 0.0), 0.0),
                    "dominant_close_reason": str(close.get("dominant_close_reason", "none")),
                    "positions_open": _safe_int(close.get("positions_open", len(close_positions)), len(close_positions)),
                    "positions": close_positions,
                },
            },
        }
    return out


@app.route('/v2/monitor', methods=['GET'])
def v2_monitor():
    """Compact runtime monitor payload for CLI/BAT HUD consumers."""
    return jsonify(_build_monitor_payload()), 200


def _store_decisions_payload(data: dict) -> None:
    with state_lock:
        trading_state["agent_decisions"] = list(data.get("decisions", []) or [])
        trading_state["agent_diagnostics"] = dict(data.get("diagnostics", {}) or {})
        monitor_payload = data.get("monitor")
        if not monitor_payload:
            monitor_payload = dict((trading_state.get("agent_diagnostics", {}) or {}).get("monitor", {}) or {})
        trading_state["monitor"] = dict(monitor_payload or {})
        try:
            trading_state["vol"] = float(data.get("vol", 0.0))
        except Exception:
            trading_state["vol"] = 0.0
        trading_state["last_update"] = _now_iso()

    if _runtime_service is not None:
        try:
            _runtime_service.store_decisions(
                decisions=list(data.get("decisions", []) or []),
                vol=float(data.get("vol", 0.0) or 0.0),
                diagnostics=dict(data.get("diagnostics", {}) or {}),
            )
        except Exception as exc:
            app.logger.debug("v2 decision mirror skipped: %s", exc)


@app.errorhandler(404)
def not_found(err):
    del err
    app.logger.warning("[404] %s %s", request.method, request.path)
    legacy_v1_paths = {
        "/poll",
        "/report",
        "/tick",
        "/indicator",
        "/health",
        "/state",
        "/metrics",
        "/monitor",
    }
    hint = "Check bridge endpoint path/version."
    if str(request.path) in legacy_v1_paths:
        hint = (
            "Legacy v1 endpoint detected. Recompile/reattach MT4 files from this repo (v2 endpoints only), "
            "or set MT4_BRIDGE_ENABLE_V1_COMPAT=1 for temporary compatibility."
        )
    return (
        f"ENDPOINT_NOT_FOUND {request.path}\n{hint}",
        404,
        {"Content-Type": "text/plain; charset=utf-8"},
    )


@app.route('/v2/visuals', methods=['POST'])
def v2_post_visuals():
    """Agent posts drawing commands for specific charts."""
    try:
        data = request.get_json(silent=True) or {}
        symbol = data.get("symbol")
        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400

        with visual_lock:
            if symbol not in visuals:
                visuals[symbol] = []
            visuals[symbol].append(data)
            if len(visuals[symbol]) > 50:
                visuals[symbol].pop(0)

        return jsonify({"status": "queued"}), 200
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route('/v2/visuals', methods=['GET'])
def v2_get_visuals():
    """Indicator polls for drawing commands for its symbol and consumes them."""
    symbol = request.args.get("symbol")
    if not symbol:
        return jsonify({"error": "Missing symbol param"}), 400

    with visual_lock:
        cmds = visuals.get(symbol, [])
        if cmds:
            visuals[symbol] = []
            return jsonify(cmds), 200
    return jsonify([]), 200


@app.route('/v2/visuals/tap', methods=['GET'])
def v2_tap_visuals():
    """Dashboard taps recent drawing commands for a symbol without consuming queue state."""
    symbol = str(request.args.get("symbol", "")).strip()
    if not symbol:
        return jsonify({"error": "Missing symbol param"}), 400
    try:
        limit = int(request.args.get("limit", "20"))
    except Exception:
        limit = 20
    limit = max(1, min(limit, 200))

    with visual_lock:
        cmds = list(visuals.get(symbol, []) or [])
        if not cmds:
            for k, v in visuals.items():
                if str(k).upper() == symbol.upper():
                    cmds = list(v or [])
                    break
    if not cmds:
        return jsonify([]), 200
    return jsonify(cmds[-limit:]), 200


@app.route('/v2/market/ticks', methods=['GET'])
def v2_get_ticks():
    """Agent gets latest market data."""
    with md_lock:
        return jsonify(market_data), 200


@app.route('/v2/market/bars', methods=['GET'])
def v2_get_bars():
    """
    Aggregate bridge tick history into OHLC bars.
    Query params:
    - symbol: required
    - timeframe: optional, currently only H1 supported (default H1)
    - limit: optional, default 400
    """
    symbol = str(request.args.get("symbol", "")).strip()
    if not symbol:
        return jsonify({"status": "error", "message": "Missing symbol"}), 400
    timeframe = str(request.args.get("timeframe", "H1")).strip().upper()
    if timeframe not in {"H1", "1H", "60M"}:
        return jsonify({"status": "error", "message": f"Unsupported timeframe {timeframe}"}), 400
    try:
        limit = int(request.args.get("limit", "400"))
    except Exception:
        limit = 400
    limit = max(1, min(limit, 2000))

    with md_lock:
        ticks = list(market_tick_history.get(symbol, deque()))
        if not ticks:
            # Case-insensitive fallback lookup.
            for k, v in market_tick_history.items():
                if str(k).upper() == symbol.upper():
                    ticks = list(v)
                    symbol = str(k)
                    break

    if not ticks:
        return jsonify({"status": "ok", "symbol": symbol, "timeframe": "H1", "bars": []}), 200

    try:
        import pandas as pd

        df = pd.DataFrame(ticks)
        if df.empty:
            return jsonify({"status": "ok", "symbol": symbol, "timeframe": "H1", "bars": []}), 200
        ts = pd.to_datetime(df.get("time"), errors="coerce", utc=True)
        if ts.isna().all():
            ts = pd.to_datetime(df.get("ts"), unit="s", errors="coerce", utc=True)
        df = df.loc[ts.notna()].copy()
        if df.empty:
            return jsonify({"status": "ok", "symbol": symbol, "timeframe": "H1", "bars": []}), 200
        df["time"] = ts.loc[ts.notna()]
        df = df.sort_values("time")
        df["mid"] = (df["bid"].astype(float) + df["ask"].astype(float)) / 2.0
        df = df.set_index("time")
        ohlc = df["mid"].resample("1h").ohlc().dropna()
        vol = df["mid"].resample("1h").size().astype(float)
        if ohlc.empty:
            return jsonify({"status": "ok", "symbol": symbol, "timeframe": "H1", "bars": []}), 200
        out_df = ohlc.join(vol.rename("volume"), how="left").fillna(0.0).tail(limit)
        bars = []
        for idx, row in out_df.iterrows():
            bars.append(
                {
                    "time": idx.isoformat(),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0.0)),
                }
            )
        return jsonify({"status": "ok", "symbol": symbol, "timeframe": "H1", "bars": bars}), 200
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


def _v2_unavailable() -> tuple[dict, int]:
    out = {"status": "error", "message": "v2 runtime service unavailable"}
    if _runtime_service_init_error:
        out["reason"] = str(_runtime_service_init_error)
    return out, 503


def _legacy_response_headers(extra: dict | None = None) -> dict:
    out = {
        "X-Bridge-Legacy-Compat": "1",
        "X-Bridge-Upgrade": "Use /v2/* endpoints",
    }
    if isinstance(extra, dict):
        out.update(extra)
    return out


def _v2_metrics_payload() -> dict:
    base = dict((_runtime_service.get_metrics() if _runtime_service is not None else {}) or {})
    interop_latency = _interop_stage_stats()
    with interop_lock:
        error_budget = dict(interop_error_budget)
    with report_lock:
        reports_stored = int(len(reports))
    with md_lock:
        tracked_symbols = int(len(market_data))
    base["interop"] = {
        "enabled": bool(INTEROP_AUDIT_ENABLED),
        "mode": str(INTEROP_AUDIT_MODE),
        "trace_path": str(INTEROP_AUDIT_TRACE_PATH),
        "sample_rate": float(INTEROP_AUDIT_SAMPLE_RATE),
        "latency_buckets_ms": list(INTEROP_LATENCY_BUCKETS_MS),
        "latency": interop_latency,
        "error_budget": error_budget,
    }
    base["bridge_runtime"] = {
        "reports_stored": reports_stored,
        "tracked_symbols": tracked_symbols,
    }
    return base


@app.route('/v2/commands', methods=['POST'])
def v2_post_command():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    data = dict(request.get_json(silent=True) or {})
    out, code = _runtime_service.submit_command(data, proto="v2")
    return jsonify(out), code


@app.route('/v2/commands/poll', methods=['GET'])
def v2_poll_command():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    fmt = str(request.args.get("format", "json")).strip().lower()
    as_line = fmt in {"line", "text", "plain"}
    out, code = _runtime_service.poll_command(as_line=as_line)
    if as_line:
        return str(out), code, {"Content-Type": "text/plain; charset=utf-8"}
    return jsonify(out), code


@app.route('/v2/commands/history', methods=['GET'])
def v2_commands_history():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    rows = _runtime_service.get_commands(limit=max(1, min(limit, 5000)))
    return jsonify({"status": "ok", "commands": rows}), 200


@app.route('/v2/commands/events', methods=['GET'])
def v2_commands_events():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    try:
        limit = int(request.args.get("limit", "500"))
    except Exception:
        limit = 500
    command_id = str(request.args.get("command_id", "")).strip()
    rows = _runtime_service.get_command_events(
        command_id=(command_id or None),
        limit=max(1, min(limit, 10000)),
    )
    return jsonify({"status": "ok", "events": rows}), 200


@app.route('/v2/commands/ack', methods=['POST'])
def v2_ack_command():
    data = dict(request.get_json(silent=True) or {})
    out, code = _ack_v2_payload(data)
    return jsonify(out), code


@app.route('/v2/market/tick', methods=['POST'])
def v2_tick():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    data = request.get_json(silent=True)
    if data is None:
        raw = request.data.replace(b"\x00", b"").decode("utf-8", errors="ignore").strip()
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = {}
    data = dict(data or {})
    _ingest_tick_payload(data)
    return jsonify({"status": "ok"}), 200


@app.route('/v2/reports', methods=['POST'])
def v2_reports_post():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    msg, payload = _decode_json_body()
    _process_report_message(msg, payload)
    return jsonify({"status": "ok"}), 200


@app.route('/v2/reports', methods=['GET'])
def v2_reports_get():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    rows = _runtime_service.get_reports(limit=max(1, min(limit, 2000)))
    return jsonify({"status": "ok", "reports": rows}), 200


@app.route('/v2/governance/events', methods=['GET'])
def v2_governance_events_get():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    rows = _runtime_service.get_governance_events(limit=max(1, min(limit, 2000)))
    return jsonify({"status": "ok", "events": rows}), 200


@app.route('/v2/state', methods=['GET'])
def v2_state():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    return jsonify(_runtime_service.get_state()), 200


@app.route('/v2/state/decisions', methods=['POST'])
def v2_state_decisions():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    data = dict(request.get_json(silent=True) or {})
    _store_decisions_payload(data)
    _runtime_service.store_decisions(
        decisions=list(data.get("decisions", []) or []),
        vol=float(data.get("vol", 0.0) or 0.0),
        diagnostics=dict(data.get("diagnostics", {}) or {}),
    )
    return jsonify({"status": "ok"}), 200


@app.route('/v2/metrics', methods=['GET'])
def v2_metrics():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    return jsonify(_v2_metrics_payload()), 200


@app.route('/v2/health', methods=['GET'])
def v2_health():
    if _runtime_service is None:
        out, code = _v2_unavailable()
        return jsonify(out), code
    return jsonify(_runtime_service.get_health()), 200


if LEGACY_V1_COMPAT_ENABLED:
    @app.route('/poll', methods=['GET'])
    def legacy_v1_poll():
        if _runtime_service is None:
            app.logger.warning("legacy /poll served empty because runtime service is unavailable")
            return (
                "",
                200,
                _legacy_response_headers({"Content-Type": "text/plain; charset=utf-8"}),
            )
        out, code = _runtime_service.poll_command(as_line=True)
        return (
            str(out),
            code,
            _legacy_response_headers({"Content-Type": "text/plain; charset=utf-8"}),
        )


    @app.route('/report', methods=['POST'])
    def legacy_v1_report():
        msg, payload = _decode_json_body()
        _process_report_message(msg, payload)
        return (
            "OK",
            200,
            _legacy_response_headers({"Content-Type": "text/plain; charset=utf-8"}),
        )


    @app.route('/tick', methods=['POST'])
    def legacy_v1_tick():
        data = request.get_json(silent=True)
        if data is None:
            raw = request.data.replace(b"\x00", b"").decode("utf-8", errors="ignore").strip()
            if raw:
                try:
                    data = json.loads(raw)
                except Exception:
                    data = {}
        try:
            _ingest_tick_payload(dict(data or {}))
        except Exception as exc:
            return (
                f"ERROR {exc}",
                400,
                _legacy_response_headers({"Content-Type": "text/plain; charset=utf-8"}),
            )
        return (
            "OK",
            200,
            _legacy_response_headers({"Content-Type": "text/plain; charset=utf-8"}),
        )


    @app.route('/indicator', methods=['GET'])
    def legacy_v1_indicator():
        symbol = str(request.args.get("symbol", "")).strip()
        if not symbol:
            resp = jsonify({"error": "Missing symbol param"})
            resp.headers.update(_legacy_response_headers())
            return resp, 400

        with visual_lock:
            cmds = list(visuals.get(symbol, []) or [])
            if cmds:
                visuals[symbol] = []

        # Legacy indicator parsers typically only understand arrow/label items.
        # Convert modern HUD rows into a label so old .mq4 builds still render.
        out_cmds: list[dict] = []
        with md_lock:
            md_row = dict(market_data.get(symbol, {}) or {})
        bid = _safe_float(md_row.get("bid", 0.0), 0.0)
        ask = _safe_float(md_row.get("ask", 0.0), 0.0)
        fallback_price = (bid + ask) / 2.0 if (bid > 0.0 and ask > 0.0) else max(bid, ask, 0.0)
        for row in cmds:
            cmd = dict(row or {})
            cmd_type = str(cmd.get("type", "")).strip().lower()
            if cmd_type == "hud":
                action = str(cmd.get("action", "Scanning")).strip()
                score = _safe_float(cmd.get("score", 0.0), 0.0)
                trend = _safe_float(cmd.get("trend", 0.5), 0.5)
                sharpe = _safe_float(cmd.get("sharpe", 0.0), 0.0)
                out_cmds.append(
                    {
                        "type": "label",
                        "symbol": symbol,
                        "time": int(time.time()),
                        "price": float(
                            _safe_float(
                                cmd.get("price", fallback_price),
                                fallback_price,
                            )
                        ),
                        "text": f"{action} Sc {score:.2f} Tr {trend:.2f} Sh {sharpe:.2f}",
                        "color": "White",
                    }
                )
                continue
            out_cmds.append(cmd)

        resp = jsonify(out_cmds)
        resp.headers.update(_legacy_response_headers())
        return resp, 200


if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("MT4 Bridge Server")
    print("=" * 60)
    print("STATUS:  [RUNNING] (Green)")
    print("ADDRESS: http://127.0.0.1:58710")
    print("\nEndpoints:")
    print("  POST /v2/commands, GET /v2/commands/poll, GET /v2/commands/history, GET /v2/commands/events, POST /v2/commands/ack")
    print("  POST /v2/market/tick, POST /v2/reports, GET /v2/reports")
    print("  GET  /v2/state, /v2/metrics, /v2/health, /v2/governance/events, /v2/monitor")
    print("  POST /v2/thought")
    print("  GET  /v2/market/ticks, /v2/market/bars")
    print("  POST/GET /v2/visuals, GET /v2/visuals/tap")
    print("  POST /v2/state/decisions - Agent diagnostics")

    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    print("\nLogs will appear below...")
    print("=" * 60 + "\n")

    import time as _time

    def _heartbeat():
        while True:
            _time.sleep(30)
            now_str = datetime.now().strftime('%H:%M:%S')
            with md_lock:
                syms = list(market_data.keys())
            if syms:
                print(
                    f"[{now_str}] HEARTBEAT: Bridge alive | Tracking: {', '.join(syms)}",
                    flush=True,
                )
            else:
                print(f"[{now_str}] HEARTBEAT: Bridge alive | Waiting for MT4 ticks...", flush=True)

    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()

    app.run(host='127.0.0.1', port=58710, debug=False)
