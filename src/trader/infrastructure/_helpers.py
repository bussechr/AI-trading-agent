"""Shared helpers for the infrastructure layer."""

from __future__ import annotations

import json
import time
from typing import Any


def _now() -> float:
    return float(time.time())


def _jdump(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def _jload(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _percentile_triplet(values: list[float]) -> dict[str, float]:
    clean = sorted(float(v) for v in values if isinstance(v, (int, float)))
    if not clean:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    n = len(clean)

    def _pick(pct: float) -> float:
        idx = int(round((max(0.0, min(100.0, pct)) / 100.0) * (n - 1)))
        return float(clean[max(0, min(n - 1, idx))])

    return {"p50": _pick(50.0), "p95": _pick(95.0), "p99": _pick(99.0)}


def _default_state() -> dict[str, Any]:
    return {
        "system_status": "starting",
        "last_heartbeat": None,
        "equity": 0.0,
        "margin": 0.0,
        "freemargin": 0.0,
        "leverage": 0.0,
        "positions": [],
        "signals_sent": 0,
        "trades_executed": 0,
        "last_signal": None,
        "last_ack": None,
        "agent_decisions": [],
        "agent_diagnostics": {},
        "monitor": {},
        "vol": 0.0,
        "governance": {},
        "governance_last_event": None,
        "risk_envelope": {},
        "last_update": None,
        "current_thought": "",
    }
