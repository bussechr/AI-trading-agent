from __future__ import annotations

import requests
import os
import time
import logging
import uuid
import json
import random
from pathlib import Path

logger = logging.getLogger(__name__)

API = os.environ.get("MT4_BRIDGE_URL", "http://127.0.0.1:58710")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _interop_enabled() -> bool:
    return _env_bool("MT4_INTEROP_AUDIT_ENABLED", False)


def _interop_trace_path() -> str:
    return str(
        os.environ.get(
            "MT4_INTEROP_AUDIT_TRACE_PATH",
            "data/state/audit/interop/transport_trace.jsonl",
        )
    ).strip()


def _interop_sample_rate() -> float:
    try:
        return float(max(0.0, min(1.0, float(os.environ.get("MT4_INTEROP_AUDIT_SAMPLE_RATE", "1.0")))))
    except Exception:
        return 1.0


def _interop_mode() -> str:
    mode = str(os.environ.get("MT4_INTEROP_AUDIT_MODE", "live_shadow")).strip().lower()
    if mode not in {"live_shadow", "replay_live_like", "replay_offline"}:
        return "live_shadow"
    return mode


def _interop_emit(row: dict) -> None:
    if not _interop_enabled():
        return
    if _interop_sample_rate() < 1.0 and random.random() > _interop_sample_rate():
        return
    path = _interop_trace_path()
    if not path:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(row), sort_keys=True) + "\n")
    except Exception:
        pass


def _post_command_v2(
    payload: dict,
    *,
    max_retries: int = 3,
    timeout: float = 2.0,
) -> tuple[bool, str, dict | None]:
    command_id = str(payload.get("command_id") or payload.get("signal_id") or "").strip() or str(uuid.uuid4())
    trace_id = str(payload.get("trace_id") or "").strip() or str(uuid.uuid4())
    payload = dict(payload)
    payload["command_id"] = command_id
    payload["trace_id"] = trace_id
    payload["interop_mode"] = str(payload.get("interop_mode") or _interop_mode())
    if _interop_enabled():
        payload["t_py_signal_post_start"] = float(payload.get("t_py_signal_post_start", time.time()))
    payload["session_id"] = str(payload.get("session_id") or os.environ.get("TRADER_SESSION_ID", "default"))
    payload["created_at"] = float(payload.get("created_at", time.time()) or time.time())

    for attempt in range(max_retries):
        req_start = time.time()
        try:
            r = requests.post(f"{API}/v2/commands", json=payload, timeout=timeout)
            body = dict(r.json() or {}) if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200:
                t_end = time.time()
                _interop_emit(
                    {
                        "ts": float(t_end),
                        "phase": "py_signal_post",
                        "signal_id": command_id,
                        "trace_id": trace_id,
                        "mode": str(payload.get("interop_mode", _interop_mode())),
                        "cmd": str(payload.get("cmd", "")),
                        "symbol": str(payload.get("symbol", "")),
                        "t_py_signal_post_start": float(req_start),
                        "t_py_signal_post_end": float(t_end),
                        "retries": int(attempt),
                        "outcome": "queued",
                        "rejection_reason": "",
                        "stage_latencies_ms": {
                            "py_signal_post_ms": float((t_end - req_start) * 1000.0),
                        },
                    }
                )
                return True, command_id, body
            if r.status_code in (400, 404, 409, 429):
                logger.warning("Bridge v2 rejected command %s (%s): %s", command_id, r.status_code, body)
                t_end = time.time()
                _interop_emit(
                    {
                        "ts": float(t_end),
                        "phase": "py_signal_post",
                        "signal_id": command_id,
                        "trace_id": trace_id,
                        "mode": str(payload.get("interop_mode", _interop_mode())),
                        "cmd": str(payload.get("cmd", "")),
                        "symbol": str(payload.get("symbol", "")),
                        "t_py_signal_post_start": float(req_start),
                        "t_py_signal_post_end": float(t_end),
                        "retries": int(attempt),
                        "outcome": "rejected",
                        "rejection_reason": str((body or {}).get("reason", f"http_{r.status_code}")),
                        "http_status": int(r.status_code),
                        "stage_latencies_ms": {
                            "py_signal_post_ms": float((t_end - req_start) * 1000.0),
                        },
                    }
                )
                return False, command_id, body
            r.raise_for_status()
            t_end = time.time()
            _interop_emit(
                {
                    "ts": float(t_end),
                    "phase": "py_signal_post",
                    "signal_id": command_id,
                    "trace_id": trace_id,
                    "mode": str(payload.get("interop_mode", _interop_mode())),
                    "cmd": str(payload.get("cmd", "")),
                    "symbol": str(payload.get("symbol", "")),
                    "t_py_signal_post_start": float(req_start),
                    "t_py_signal_post_end": float(t_end),
                    "retries": int(attempt),
                    "outcome": "queued",
                    "rejection_reason": "",
                    "stage_latencies_ms": {
                        "py_signal_post_ms": float((t_end - req_start) * 1000.0),
                    },
                }
            )
            return True, command_id, body
        except requests.exceptions.Timeout as exc:
            t_end = time.time()
            _interop_emit(
                {
                    "ts": float(t_end),
                    "phase": "py_signal_post",
                    "signal_id": command_id,
                    "trace_id": trace_id,
                    "mode": str(payload.get("interop_mode", _interop_mode())),
                    "cmd": str(payload.get("cmd", "")),
                    "symbol": str(payload.get("symbol", "")),
                    "t_py_signal_post_start": float(req_start),
                    "t_py_signal_post_end": float(t_end),
                    "retries": int(attempt),
                    "outcome": "timeout",
                    "rejection_reason": "bridge_timeout",
                    "stage_latencies_ms": {
                        "py_signal_post_ms": float((t_end - req_start) * 1000.0),
                    },
                }
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
                continue
            logger.error("Bridge v2 command timeout id=%s err=%s", command_id, exc)
            return False, command_id, None
        except requests.exceptions.ConnectionError as exc:
            t_end = time.time()
            _interop_emit(
                {
                    "ts": float(t_end),
                    "phase": "py_signal_post",
                    "signal_id": command_id,
                    "trace_id": trace_id,
                    "mode": str(payload.get("interop_mode", _interop_mode())),
                    "cmd": str(payload.get("cmd", "")),
                    "symbol": str(payload.get("symbol", "")),
                    "t_py_signal_post_start": float(req_start),
                    "t_py_signal_post_end": float(t_end),
                    "retries": int(attempt),
                    "outcome": "connection_error",
                    "rejection_reason": "bridge_connection_error",
                    "stage_latencies_ms": {
                        "py_signal_post_ms": float((t_end - req_start) * 1000.0),
                    },
                }
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
                continue
            logger.error("Bridge v2 command connection error id=%s err=%s", command_id, exc)
            return False, command_id, None
        except Exception as exc:
            t_end = time.time()
            _interop_emit(
                {
                    "ts": float(t_end),
                    "phase": "py_signal_post",
                    "signal_id": command_id,
                    "trace_id": trace_id,
                    "mode": str(payload.get("interop_mode", _interop_mode())),
                    "cmd": str(payload.get("cmd", "")),
                    "symbol": str(payload.get("symbol", "")),
                    "t_py_signal_post_start": float(req_start),
                    "t_py_signal_post_end": float(t_end),
                    "retries": int(attempt),
                    "outcome": "error",
                    "rejection_reason": str(type(exc).__name__),
                    "stage_latencies_ms": {
                        "py_signal_post_ms": float((t_end - req_start) * 1000.0),
                    },
                }
            )
            if attempt < max_retries - 1:
                time.sleep(2**attempt)
                continue
            logger.error("Bridge v2 command failed id=%s err=%s", command_id, exc)
            return False, command_id, None
    return False, command_id, None


def send(
    side: str,
    symbol: str,
    *,
    lots: float = 0.0,
    tp_cash: float | None = None,
    sl_price: float | None = None,
    tp_price: float | None = None,
    magic: int = 246810,
    max_retries: int = 3,
) -> None:
    """
    Send entry signal to MT4 bridge.

    lots=0.0 -> EA enforces minimum lot.
    tp_cash  -> EA converts cash TP to price TP (legacy).
    sl_price -> absolute stop-loss.
    tp_price -> absolute take-profit.
    """
    side_up = str(side).upper().strip()
    payload = {
        "cmd": side_up,
        "symbol": str(symbol),
        "lots": float(lots),
        "magic": int(magic),
        "intent": "ENTRY" if side_up in {"BUY", "SELL"} else "UNKNOWN",
    }
    if tp_cash is not None:
        payload["tp_cash"] = float(tp_cash)
    if sl_price is not None:
        payload["sl_price"] = float(sl_price)
    if tp_price is not None:
        payload["tp_price"] = float(tp_price)

    ok, signal_id, body = _post_command_v2(payload, max_retries=max_retries, timeout=2.0)
    if ok:
        logger.debug("Signal queued id=%s %s %s lots=%.2f", signal_id, side_up, symbol, float(lots))
    else:
        logger.error("Signal rejected/failed id=%s %s %s payload=%s", signal_id, side_up, symbol, body)


def close_all(max_retries: int = 3) -> None:
    """Close all EA-managed positions."""
    payload = {
        "cmd": "CLOSE_ALL",
        "intent": "CLOSE_ALL",
    }
    ok, signal_id, body = _post_command_v2(payload, max_retries=max_retries, timeout=2.0)
    if ok:
        logger.info("CLOSE_ALL queued id=%s", signal_id)
    else:
        logger.error("CLOSE_ALL rejected/failed id=%s payload=%s", signal_id, body)


def close_position(symbol: str, magic: int = 246810, max_retries: int = 3) -> bool:
    """Close EA-managed position(s) for one symbol."""
    payload = {
        "cmd": "CLOSE",
        "symbol": str(symbol),
        "magic": int(magic),
        "intent": "EXIT",
    }
    ok, signal_id, body = _post_command_v2(payload, max_retries=max_retries, timeout=2.0)
    if not ok:
        logger.error("CLOSE rejected/failed id=%s symbol=%s payload=%s", signal_id, symbol, body)
    return bool(ok)


def update_thought(thought: str, max_retries: int = 3) -> None:
    """Send thought process to on-chart dashboard."""
    for _ in range(max_retries):
        try:
            r = requests.post(f"{API}/v2/thought", json={"thought": thought}, timeout=1)
            r.raise_for_status()
            return
        except Exception:
            pass


def get_ticks(max_retries: int = 1) -> dict:
    """Fetch latest market data from bridge."""
    for _ in range(max_retries):
        try:
            r = requests.get(f"{API}/v2/market/ticks", timeout=1)
            r.raise_for_status()
            return r.json()
        except Exception:
            pass
    return {}


def get_recent_bars(symbol: str, timeframe: str = "H1", limit: int = 400, max_retries: int = 1) -> list[dict]:
    """Fetch aggregated OHLC bars from bridge tick history."""
    sym = str(symbol or "").strip()
    if not sym:
        return []
    tf = str(timeframe or "H1").strip().upper()
    lim = int(max(1, min(int(limit), 2000)))
    for _ in range(max_retries):
        try:
            r = requests.get(
                f"{API}/v2/market/bars",
                params={"symbol": sym, "timeframe": tf, "limit": lim},
                timeout=2,
            )
            r.raise_for_status()
            body = dict(r.json() or {})
            return list(body.get("bars", []) or [])
        except Exception:
            pass
    return []


def get_positions(max_retries: int = 1) -> list:
    """Fetch current open positions from bridge."""
    state_paths = ["/v2/state"]
    for _ in range(max_retries):
        for path in state_paths:
            try:
                r = requests.get(f"{API}{path}", timeout=1)
                r.raise_for_status()
                data = r.json()
                return data.get("positions", [])
            except Exception:
                continue
    return []


def get_account_info(max_retries: int = 1) -> dict:
    """Fetch current account equity and margin from bridge."""
    state_paths = ["/v2/state"]
    for _ in range(max_retries):
        for path in state_paths:
            try:
                r = requests.get(f"{API}{path}", timeout=1)
                r.raise_for_status()
                return r.json()
            except Exception:
                continue
    return {}


def post_visuals(visual_data: dict, max_retries: int = 1) -> None:
    """Send visual command to bridge (fire-and-forget)."""
    attempts = 1
    try:
        attempts = max(1, int(max_retries))
    except Exception:
        attempts = 1
    for _ in range(attempts):
        try:
            r = requests.post(f"{API}/v2/visuals", json=visual_data, timeout=1)
            r.raise_for_status()
            return
        except Exception:
            pass


def post_decisions(decisions: list[dict], vol: float, diagnostics: dict, max_retries: int = 1) -> None:
    """Optional dashboard diagnostics endpoint."""
    diag_dict = dict(diagnostics or {})
    payload = {
        "decisions": list(decisions or []),
        "vol": float(vol),
        "diagnostics": diag_dict,
    }
    if "runtime_metrics" in diag_dict:
        payload["runtime_metrics"] = dict(diag_dict.get("runtime_metrics", {}) or {})
    paths = ["/v2/state/decisions"]

    for _ in range(max_retries):
        for path in paths:
            try:
                r = requests.post(f"{API}{path}", json=payload, timeout=1)
                r.raise_for_status()
                return
            except Exception:
                continue


def get_metrics(max_retries: int = 1) -> dict:
    """Optional bridge lifecycle metrics."""
    for _ in range(max_retries):
        try:
            r = requests.get(f"{API}/v2/metrics", timeout=1)
            r.raise_for_status()
            return dict(r.json() or {})
        except Exception:
            pass
    return {}


def get_state_meta() -> dict:
    """Compatibility helper for stale-state gating."""
    return {
        "ok": True,
        "fetch_ok": True,
        "positions_ok": True,
        "account_ok": True,
        "age_secs": 0.0,
        "heartbeat_age_secs": 0.0,
        "positions_age_secs": 0.0,
        "stale_reasons": [],
        "error": "",
    }


def get_cached_signal_ids() -> set[str]:
    """Compatibility helper: HTTP bridge client is stateless by default."""
    return set()


def check_connection_details(retries: int = 3) -> tuple[bool, str]:
    """Verify bridge server readiness and include failure reason when unavailable."""
    last_err = ""
    for attempt in range(retries):
        try:
            r = requests.get(f"{API}/v2/health", timeout=3)
            if r.status_code == 200:
                return True, ""
            try:
                payload = r.json()
                reason = str(payload.get("reason") or payload.get("message") or "").strip()
            except Exception:
                reason = ""
            if reason:
                last_err = f"HTTP {int(r.status_code)}: {reason}"
            else:
                last_err = f"HTTP {int(r.status_code)}"
        except Exception as exc:
            last_err = str(exc)
        if attempt < retries - 1:
            time.sleep(1)
    return False, str(last_err or "unreachable")


def check_connection(retries: int = 3) -> bool:
    """Verify bridge server is running and reachable."""
    ok, _ = check_connection_details(retries=retries)
    return bool(ok)
