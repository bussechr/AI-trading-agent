"""Command queue operations: enqueue, poll, ack, expire."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.trader.interfaces.dto import CommandStatus, ExecutionAck, ExecutionCommand

from ._helpers import _jdump, _jload, _now


class CommandStoreMixin:
    """Mixin for command-queue CRUD on ``RuntimeStore``."""

    _conn: sqlite3.Connection
    _lock: Any  # threading.RLock

    @staticmethod
    def _normalize_ack_status(raw: str) -> str:
        status = str(raw or "").strip().lower()
        if status in {
            CommandStatus.DELIVERED.value,
            CommandStatus.ACKED.value,
            CommandStatus.FAILED.value,
            CommandStatus.DUPLICATE.value,
        }:
            return status
        return CommandStatus.FAILED.value

    @staticmethod
    def _is_legal_ack_transition(current_status: str, requested_status: str) -> bool:
        cur = str(current_status or "").strip().lower()
        nxt = str(requested_status or "").strip().lower()
        if nxt == CommandStatus.DELIVERED.value:
            return cur in {CommandStatus.QUEUED.value, CommandStatus.DELIVERED.value}
        if nxt in {CommandStatus.ACKED.value, CommandStatus.FAILED.value, CommandStatus.DUPLICATE.value}:
            return cur == CommandStatus.DELIVERED.value
        return False

    @staticmethod
    def _allowed_ack_statuses_for_current(current_status: str) -> list[str]:
        cur = str(current_status or "").strip().lower()
        if cur == CommandStatus.QUEUED.value:
            return [CommandStatus.DELIVERED.value]
        if cur == CommandStatus.DELIVERED.value:
            return [CommandStatus.DELIVERED.value, CommandStatus.ACKED.value, CommandStatus.FAILED.value, CommandStatus.DUPLICATE.value]
        return []

    @staticmethod
    def _row_to_command(row: sqlite3.Row) -> ExecutionCommand:
        payload = _jload(row["payload_json"], {})
        return ExecutionCommand(
            command_id=str(row["command_id"]),
            session_id=str(row["session_id"]),
            proto=str(row["proto"]),
            cmd=str(row["cmd"]),
            symbol=str(row["symbol"] or ""),
            lots=float(row["lots"] or 0.0),
            tp_cash=(None if row["tp_cash"] is None else float(row["tp_cash"])),
            tp_price=(None if row["tp_price"] is None else float(row["tp_price"])),
            sl_price=(None if row["sl_price"] is None else float(row["sl_price"])),
            close_lots=float((dict(payload or {})).get("close_lots", row["lots"] or 0.0) or 0.0),
            magic=int(row["magic"] or 246810),
            intent=str(row["intent"] or "UNKNOWN"),
            trace_id=str(row["trace_id"] or ""),
            action=str((dict(payload or {})).get("action") or ""),
            action_score=float((dict(payload or {})).get("action_score", 0.0) or 0.0),
            reversal_token=str((dict(payload or {})).get("reversal_token") or ""),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=float(row["expires_at"]),
            delivered_count=int(row["delivered_count"] or 0),
            payload=dict(payload or {}),
        )

    def _recover_pending_on_startup_locked(self) -> None:
        now_ts = _now()
        self._expire_stale_locked(now_ts)
        rows = self._conn.execute(
            """
            SELECT command_id FROM commands
            WHERE status = ? AND (expires_at <= 0 OR expires_at >= ?)
            """,
            (CommandStatus.DELIVERED.value, now_ts),
        ).fetchall()
        if not rows:
            self._conn.commit()
            return

        ids = [str(r["command_id"]) for r in rows]
        self._conn.executemany(
            "UPDATE commands SET status = ?, reason = ?, updated_at = ? WHERE command_id = ?",
            [(CommandStatus.QUEUED.value, "restart_requeued", now_ts, cid) for cid in ids],
        )
        self._conn.executemany(
            "INSERT INTO command_events(command_id, event_status, reason, ts, event_json) VALUES(?, ?, ?, ?, ?)",
            [(cid, CommandStatus.QUEUED.value, "restart_requeued", now_ts, "{}") for cid in ids],
        )
        st = self._get_state_locked()
        st["last_restart_requeue"] = {"time": float(now_ts), "count": int(len(ids))}
        st["last_update"] = float(now_ts)
        self._put_state_locked(st, now_ts)
        self._conn.commit()

    def enqueue_command(self, command: ExecutionCommand) -> tuple[bool, str]:
        with self._lock:
            now_ts = _now()
            try:
                self._conn.execute(
                    """
                    INSERT INTO commands(
                        command_id, session_id, proto, cmd, symbol, lots, tp_cash, tp_price, sl_price,
                        magic, intent, status, trace_id, created_at, updated_at, expires_at,
                        delivered_count, reason, payload_json, ack_json,
                        t_bridge_queued, t_bridge_delivered, t_bridge_ack_finalized
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        command.command_id,
                        command.session_id,
                        command.proto,
                        command.cmd,
                        command.symbol,
                        float(command.lots),
                        command.tp_cash,
                        command.tp_price,
                        command.sl_price,
                        int(command.magic),
                        command.intent,
                        CommandStatus.QUEUED.value,
                        command.trace_id,
                        float(command.created_at),
                        float(now_ts),
                        float(command.expires_at),
                        int(command.delivered_count),
                        "",
                        _jdump(command.payload),
                        "",
                        float(command.created_at),
                        None,
                        None,
                    ),
                )
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT status FROM commands WHERE command_id = ?",
                    (command.command_id,),
                ).fetchone()
                status = str(row["status"] if row else "duplicate")
                return False, status

            self._conn.execute(
                "INSERT INTO command_events(command_id, event_status, reason, ts, event_json) VALUES(?, ?, ?, ?, ?)",
                (command.command_id, CommandStatus.QUEUED.value, "", now_ts, _jdump(command.to_dict())),
            )
            st = self._get_state_locked()
            st["signals_sent"] = int(st.get("signals_sent", 0)) + 1
            st["last_signal"] = {
                "time": now_ts,
                "command_id": command.command_id,
                "cmd": command.cmd,
                "symbol": command.symbol,
            }
            st["last_update"] = now_ts
            self._put_state_locked(st, now_ts)
            self._conn.commit()
            return True, CommandStatus.QUEUED.value

    def _expire_stale_locked(self, now_ts: float) -> int:
        rows = self._conn.execute(
            """
            SELECT command_id FROM commands
            WHERE status IN (?, ?) AND expires_at > 0 AND expires_at < ?
            """,
            (CommandStatus.QUEUED.value, CommandStatus.DELIVERED.value, now_ts),
        ).fetchall()
        if not rows:
            return 0

        ids = [str(r["command_id"]) for r in rows]
        self._conn.executemany(
            "UPDATE commands SET status = ?, reason = ?, updated_at = ? WHERE command_id = ?",
            [(CommandStatus.EXPIRED.value, "ttl_expired", now_ts, cid) for cid in ids],
        )
        self._conn.executemany(
            "INSERT INTO command_events(command_id, event_status, reason, ts, event_json) VALUES(?, ?, ?, ?, ?)",
            [(cid, CommandStatus.EXPIRED.value, "ttl_expired", now_ts, "{}") for cid in ids],
        )
        return len(ids)

    def poll_next_command(self) -> ExecutionCommand | None:
        with self._lock:
            now_ts = _now()
            self._expire_stale_locked(now_ts)
            row = self._conn.execute(
                """
                SELECT * FROM commands
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (CommandStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None

            cid = str(row["command_id"])
            self._conn.execute(
                """
                UPDATE commands
                SET status = ?, delivered_count = delivered_count + 1, updated_at = ?, t_bridge_delivered = ?
                WHERE command_id = ?
                """,
                (CommandStatus.DELIVERED.value, now_ts, now_ts, cid),
            )
            self._conn.execute(
                "INSERT INTO command_events(command_id, event_status, reason, ts, event_json) VALUES(?, ?, ?, ?, ?)",
                (cid, CommandStatus.DELIVERED.value, "poll_delivery", now_ts, "{}"),
            )
            row2 = self._conn.execute("SELECT * FROM commands WHERE command_id = ?", (cid,)).fetchone()
            self._conn.commit()
            if row2 is None:
                return None
            return self._row_to_command(row2)

    def ack_command(self, ack: ExecutionAck) -> tuple[dict[str, Any], int]:
        if not ack.command_id:
            return {"status": "error", "message": "Missing command_id"}, 400

        with self._lock:
            now_ts = _now()
            self._expire_stale_locked(now_ts)
            row = self._conn.execute(
                "SELECT * FROM commands WHERE command_id = ?",
                (ack.command_id,),
            ).fetchone()
            if row is None:
                return {"status": "unknown_command_id", "command_id": ack.command_id}, 404

            cur_status = str(row["status"])
            if cur_status in {CommandStatus.ACKED.value, CommandStatus.FAILED.value, CommandStatus.EXPIRED.value, CommandStatus.DUPLICATE.value}:
                return {
                    "status": "already_finalized",
                    "command_id": ack.command_id,
                    "final_status": cur_status,
                }, 200

            next_status = self._normalize_ack_status(str(ack.status))
            if not self._is_legal_ack_transition(cur_status, next_status):
                self._conn.commit()
                return {
                    "status": "transition_conflict",
                    "command_id": ack.command_id,
                    "current_status": cur_status,
                    "requested_status": next_status,
                    "allowed_next": self._allowed_ack_statuses_for_current(cur_status),
                }, 409

            if cur_status == next_status:
                self._conn.commit()
                return {
                    "status": "already_applied",
                    "command_id": ack.command_id,
                    "current_status": cur_status,
                }, 200

            ack_json = _jdump(ack.to_dict())
            reason = str(ack.message or "")
            if next_status == CommandStatus.DELIVERED.value:
                self._conn.execute(
                    """
                    UPDATE commands
                    SET status = ?, reason = ?, ack_json = ?, updated_at = ?,
                        t_bridge_delivered = COALESCE(t_bridge_delivered, ?)
                    WHERE command_id = ?
                    """,
                    (
                        next_status,
                        reason,
                        ack_json,
                        now_ts,
                        now_ts,
                        ack.command_id,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE commands
                    SET status = ?, reason = ?, ack_json = ?, updated_at = ?, t_bridge_ack_finalized = ?
                    WHERE command_id = ?
                    """,
                    (
                        next_status,
                        reason,
                        ack_json,
                        now_ts,
                        now_ts,
                        ack.command_id,
                    ),
                )

            self._conn.execute(
                "INSERT INTO command_events(command_id, event_status, reason, ts, event_json) VALUES(?, ?, ?, ?, ?)",
                (ack.command_id, next_status, reason, now_ts, ack_json),
            )

            st = self._get_state_locked()
            st["last_ack"] = {
                "time": now_ts,
                "command_id": ack.command_id,
                "status": next_status,
                "ticket": int(ack.ticket),
                "symbol": str(ack.symbol),
                "error_code": int(ack.error_code),
                "message": str(ack.message or ""),
            }
            if bool(ack.count_as_trade):
                st["trades_executed"] = int(st.get("trades_executed", 0)) + 1
            st["last_update"] = now_ts
            self._put_state_locked(st, now_ts)
            self._conn.commit()

            return {
                "status": next_status,
                "command_id": ack.command_id,
                "ticket": int(ack.ticket),
            }, 200
