"""Metrics, health, state, and query operations."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.trader.interfaces.dto import CommandStatus
from src.trader.utils import safe_float as _safe_float

from ._helpers import _default_state, _jdump, _jload, _now, _percentile_triplet


class MetricsStoreMixin:
    """Mixin for read-only metrics / state queries on ``RuntimeStore``."""

    _conn: sqlite3.Connection
    _lock: Any  # threading.RLock
    db_path: str

    def _get_state_locked(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT snapshot_json FROM runtime_state WHERE id = 1").fetchone()
        if row is None:
            return _default_state()
        return dict(_jload(row["snapshot_json"], _default_state()) or _default_state())

    def _put_state_locked(self, state: dict[str, Any], now_ts: float | None = None) -> None:
        ts = float(_now() if now_ts is None else now_ts)
        self._conn.execute(
            "UPDATE runtime_state SET snapshot_json = ?, updated_at = ? WHERE id = 1",
            (_jdump(dict(state or {})), ts),
        )

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            state = self._get_state_locked()
            pending = self._conn.execute(
                "SELECT COUNT(*) AS n FROM commands WHERE status IN (?, ?)",
                (CommandStatus.QUEUED.value, CommandStatus.DELIVERED.value),
            ).fetchone()
            state["pending_commands"] = int((pending["n"] if pending else 0) or 0)
            state.pop("_governance_fp", None)
            return state

    def get_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        lim = int(max(1, min(int(limit), 2000)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, report_text, report_json FROM reports ORDER BY id DESC LIMIT ?",
                (lim,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in reversed(rows):
            out.append(
                {
                    "time": float(r["ts"]),
                    "message": str(r["report_text"] or ""),
                    "json": _jload(str(r["report_json"] or ""), {}),
                }
            )
        return out

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        cid = str(command_id or "").strip()
        if not cid:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    command_id, session_id, proto, cmd, symbol, lots, tp_cash, tp_price, sl_price,
                    magic, intent, status, trace_id, created_at, updated_at, expires_at,
                    delivered_count, reason, payload_json, ack_json,
                    t_bridge_queued, t_bridge_delivered, t_bridge_ack_finalized
                FROM commands
                WHERE command_id = ?
                LIMIT 1
                """,
                (cid,),
            ).fetchone()
        if row is None:
            return None
        ack = _jload(str(row["ack_json"] or ""), {})
        payload = _jload(str(row["payload_json"] or ""), {})
        return {
            "command_id": str(row["command_id"]),
            "session_id": str(row["session_id"]),
            "proto": str(row["proto"]),
            "cmd": str(row["cmd"]),
            "symbol": str(row["symbol"] or ""),
            "lots": float(row["lots"] or 0.0),
            "tp_cash": (None if row["tp_cash"] is None else float(row["tp_cash"])),
            "tp_price": (None if row["tp_price"] is None else float(row["tp_price"])),
            "sl_price": (None if row["sl_price"] is None else float(row["sl_price"])),
            "magic": int(row["magic"] or 0),
            "intent": str(row["intent"] or ""),
            "status": str(row["status"]),
            "trace_id": str(row["trace_id"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
            "expires_at": float(row["expires_at"] or 0.0),
            "delivered_count": int(row["delivered_count"] or 0),
            "reason": str(row["reason"] or ""),
            "t_bridge_queued": float(row["t_bridge_queued"] or 0.0),
            "t_bridge_delivered": float(row["t_bridge_delivered"] or 0.0),
            "t_bridge_ack_finalized": float(row["t_bridge_ack_finalized"] or 0.0),
            "payload": dict(payload or {}),
            "ack": dict(ack or {}),
        }

    def get_commands(self, limit: int = 200) -> list[dict[str, Any]]:
        lim = int(max(1, min(int(limit), 5000)))
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    command_id, session_id, proto, cmd, symbol, lots, tp_cash, tp_price, sl_price,
                    magic, intent, status, trace_id, created_at, updated_at, expires_at,
                    delivered_count, reason, ack_json
                FROM commands
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (lim,),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for r in rows:
            ack = _jload(str(r["ack_json"] or ""), {})
            out.append(
                {
                    "command_id": str(r["command_id"]),
                    "session_id": str(r["session_id"]),
                    "proto": str(r["proto"]),
                    "cmd": str(r["cmd"]),
                    "symbol": str(r["symbol"] or ""),
                    "lots": float(r["lots"] or 0.0),
                    "tp_cash": (None if r["tp_cash"] is None else float(r["tp_cash"])),
                    "tp_price": (None if r["tp_price"] is None else float(r["tp_price"])),
                    "sl_price": (None if r["sl_price"] is None else float(r["sl_price"])),
                    "magic": int(r["magic"] or 0),
                    "intent": str(r["intent"] or ""),
                    "status": str(r["status"]),
                    "trace_id": str(r["trace_id"] or ""),
                    "created_at": float(r["created_at"] or 0.0),
                    "updated_at": float(r["updated_at"] or 0.0),
                    "expires_at": float(r["expires_at"] or 0.0),
                    "delivered_count": int(r["delivered_count"] or 0),
                    "reason": str(r["reason"] or ""),
                    "ack": dict(ack or {}),
                }
            )
        return out

    def get_command_events(self, *, command_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        lim = int(max(1, min(int(limit), 10000)))
        with self._lock:
            if command_id:
                rows = self._conn.execute(
                    """
                    SELECT command_id, event_status, reason, ts, event_json
                    FROM command_events
                    WHERE command_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (str(command_id), lim),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT command_id, event_status, reason, ts, event_json
                    FROM command_events
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (lim,),
                ).fetchall()

        out: list[dict[str, Any]] = []
        for r in reversed(rows):
            out.append(
                {
                    "command_id": str(r["command_id"]),
                    "status": str(r["event_status"]),
                    "reason": str(r["reason"] or ""),
                    "time": float(r["ts"] or 0.0),
                    "payload": _jload(str(r["event_json"] or ""), {}),
                }
            )
        return out

    def get_governance_events(self, limit: int = 200) -> list[dict[str, Any]]:
        lim = int(max(1, min(int(limit), 2000)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, event_type, reason, payload_json FROM governance_events ORDER BY id DESC LIMIT ?",
                (lim,),
            ).fetchall()

        out: list[dict[str, Any]] = []
        for r in reversed(rows):
            out.append(
                {
                    "time": float(r["ts"]),
                    "event_type": str(r["event_type"] or "state_update"),
                    "reason": str(r["reason"] or ""),
                    "payload": _jload(str(r["payload_json"] or ""), {}),
                }
            )
        return out

    def get_metrics(self) -> dict[str, Any]:
        with self._lock:
            now_ts = _now()
            self._expire_stale_locked(now_ts)
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM commands GROUP BY status"
            ).fetchall()
            counts = {str(r["status"]): int(r["n"]) for r in rows}
            pending_row = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM commands WHERE status IN (?, ?)",
                (CommandStatus.QUEUED.value, CommandStatus.DELIVERED.value),
            ).fetchone()
            pending_n = int((pending_row["n"] if pending_row else 0) or 0)
            oldest = float((pending_row["oldest"] if pending_row else 0.0) or 0.0)
            pending_oldest_secs = float(max(0.0, now_ts - oldest)) if oldest > 0 else 0.0

            term = self._conn.execute(
                """
                SELECT event_status, reason FROM command_events
                WHERE event_status IN (?, ?) AND ts >= ?
                """,
                (CommandStatus.ACKED.value, CommandStatus.FAILED.value, now_ts - 300.0),
            ).fetchall()
            terminal_n = len(term)
            acked_5m = 0
            timeout_n = 0
            for r in term:
                if str(r["event_status"] or "") == CommandStatus.ACKED.value:
                    acked_5m += 1
                reason = str(r["reason"] or "").lower()
                if "timeout" in reason or "retry_exhausted" in reason:
                    timeout_n += 1

            latency_rows = self._conn.execute(
                """
                SELECT t_bridge_queued, t_bridge_delivered, t_bridge_ack_finalized
                FROM commands
                WHERE status IN (?, ?) AND t_bridge_queued IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 2000
                """,
                (CommandStatus.ACKED.value, CommandStatus.FAILED.value),
            ).fetchall()

            queue_to_delivered_ms: list[float] = []
            delivered_to_terminal_ms: list[float] = []
            queue_to_terminal_ms: list[float] = []
            for r in latency_rows:
                tq = _safe_float(r["t_bridge_queued"], 0.0)
                td = _safe_float(r["t_bridge_delivered"], 0.0)
                tf = _safe_float(r["t_bridge_ack_finalized"], 0.0)
                if td > 0.0 and tq > 0.0 and td >= tq:
                    queue_to_delivered_ms.append((td - tq) * 1000.0)
                if tf > 0.0 and td > 0.0 and tf >= td:
                    delivered_to_terminal_ms.append((tf - td) * 1000.0)
                if tf > 0.0 and tq > 0.0 and tf >= tq:
                    queue_to_terminal_ms.append((tf - tq) * 1000.0)

            dec_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM decision_snapshots WHERE ts >= ?",
                (now_ts - 300.0,),
            ).fetchone()
            decision_snapshots_5m = int((dec_row["n"] if dec_row else 0) or 0)

            latest_dec = self._conn.execute(
                "SELECT rejection_json, attribution_json FROM decision_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            rejection_taxonomy = _jload(str((latest_dec["rejection_json"] if latest_dec else "") or ""), {})
            stage_attribution = _jload(str((latest_dec["attribution_json"] if latest_dec else "") or ""), {})

            gov_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM governance_events WHERE ts >= ?",
                (now_ts - 86400.0,),
            ).fetchone()
            gov_events_24h = int((gov_row["n"] if gov_row else 0) or 0)

            gov_last = self._conn.execute(
                "SELECT ts, event_type, reason, payload_json FROM governance_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if gov_last is None:
                governance_last_event: dict[str, Any] = {}
            else:
                governance_last_event = {
                    "time": float(gov_last["ts"]),
                    "event_type": str(gov_last["event_type"] or "state_update"),
                    "reason": str(gov_last["reason"] or ""),
                    "payload": _jload(str(gov_last["payload_json"] or ""), {}),
                }

            state = self._get_state_locked()
            risk_envelope = dict(state.get("risk_envelope", {}) or {})
            self._conn.commit()

        return {
            "counters": {
                "commands_total": int(sum(counts.values())),
                "queued": int(counts.get(CommandStatus.QUEUED.value, 0)),
                "delivered": int(counts.get(CommandStatus.DELIVERED.value, 0)),
                "acked": int(counts.get(CommandStatus.ACKED.value, 0)),
                "failed": int(counts.get(CommandStatus.FAILED.value, 0)),
                "expired": int(counts.get(CommandStatus.EXPIRED.value, 0)),
            },
            "pending": {
                "count": int(pending_n),
                "oldest_pending_secs": float(pending_oldest_secs),
            },
            "timeouts": {
                "window_secs": 300.0,
                "terminal_outcomes_5m": int(terminal_n),
                "acked_5m": int(acked_5m),
                "timeout_failures_5m": int(timeout_n),
                "ack_timeout_rate_5m": float(timeout_n / max(terminal_n, 1)),
            },
            "throughput": {
                "window_secs": 300.0,
                "executed_entries_5m": int(acked_5m),
            },
            "lifecycle_latency_ms": {
                "samples": {
                    "queue_to_delivered": int(len(queue_to_delivered_ms)),
                    "delivered_to_terminal": int(len(delivered_to_terminal_ms)),
                    "queue_to_terminal": int(len(queue_to_terminal_ms)),
                },
                "queue_to_delivered": _percentile_triplet(queue_to_delivered_ms),
                "delivered_to_terminal": _percentile_triplet(delivered_to_terminal_ms),
                "queue_to_terminal": _percentile_triplet(queue_to_terminal_ms),
            },
            "decision_pipeline": {
                "snapshots_5m": int(decision_snapshots_5m),
                "rejection_taxonomy": dict(rejection_taxonomy or {}),
                "stage_attribution": dict(stage_attribution or {}),
            },
            "governance": {
                "events_24h": int(gov_events_24h),
                "last_event": governance_last_event,
            },
            "risk_envelope": dict(risk_envelope or {}),
        }

    def get_health(self) -> dict[str, Any]:
        metrics = self.get_metrics()
        return {
            "status": "healthy",
            "db_path": self.db_path,
            "pending_commands": int((metrics.get("pending", {}) or {}).get("count", 0)),
            "queue_depth": int((metrics.get("pending", {}) or {}).get("count", 0)),
            "risk_regime": str((metrics.get("risk_envelope", {}) or {}).get("regime", "unknown")),
        }
