from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from src.trader.domain.decision_pipeline import DecisionPipeline
from src.trader.domain.risk_envelope import compute_adaptive_risk_envelope
from src.trader.interfaces.dto import CommandStatus, ExecutionAck, ExecutionCommand


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


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


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


class RuntimeStore:
    """Persistent command/state repository backed by sqlite."""

    def __init__(
        self,
        db_path: str,
        *,
        soft_band: tuple[float, float] = (0.06, 0.09),
        hard_band: tuple[float, float] = (0.10, 0.12),
        daily_band: tuple[float, float] = (0.02, 0.03),
    ) -> None:
        self.db_path = str(db_path)
        self.soft_band = (float(soft_band[0]), float(soft_band[1]))
        self.hard_band = (float(hard_band[0]), float(hard_band[1]))
        self.daily_band = (float(daily_band[0]), float(daily_band[1]))
        self._decision_pipeline = DecisionPipeline()

        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema_locked()
            self._recover_pending_on_startup_locked()

    @staticmethod
    def _normalize_ack_status(raw: str) -> str:
        status = str(raw or "").strip().lower()
        if status in {
            CommandStatus.DELIVERED.value,
            CommandStatus.ACKED.value,
            CommandStatus.FAILED.value,
        }:
            return status
        return CommandStatus.FAILED.value

    @staticmethod
    def _is_legal_ack_transition(current_status: str, requested_status: str) -> bool:
        cur = str(current_status or "").strip().lower()
        nxt = str(requested_status or "").strip().lower()
        if nxt == CommandStatus.DELIVERED.value:
            return cur in {CommandStatus.QUEUED.value, CommandStatus.DELIVERED.value}
        if nxt in {CommandStatus.ACKED.value, CommandStatus.FAILED.value}:
            return cur == CommandStatus.DELIVERED.value
        return False

    @staticmethod
    def _allowed_ack_statuses_for_current(current_status: str) -> list[str]:
        cur = str(current_status or "").strip().lower()
        if cur == CommandStatus.QUEUED.value:
            return [CommandStatus.DELIVERED.value]
        if cur == CommandStatus.DELIVERED.value:
            return [CommandStatus.DELIVERED.value, CommandStatus.ACKED.value, CommandStatus.FAILED.value]
        return []

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _table_exists_locked(self, table_name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (str(table_name),),
        ).fetchone()
        return row is not None

    def _table_columns_locked(self, table_name: str) -> set[str]:
        if not self._table_exists_locked(table_name):
            return set()
        rows = self._conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        out: set[str] = set()
        for row in rows:
            out.add(str(row["name"]))
        return out

    def _ensure_columns_locked(self, table_name: str, ddl_by_column: dict[str, str]) -> None:
        if not self._table_exists_locked(table_name):
            return
        existing = self._table_columns_locked(table_name)
        for col, ddl in ddl_by_column.items():
            c = str(col)
            if c in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {c} {ddl}")

    def _migrate_legacy_schema_locked(self) -> None:
        # Legacy DB files may miss columns added during the v2 refactor.
        # Backfill missing columns in-place so startup can recover cleanly.
        self._ensure_columns_locked(
            "commands",
            {
                "session_id": "TEXT NOT NULL DEFAULT 'default'",
                "proto": "TEXT NOT NULL DEFAULT 'v2'",
                "cmd": "TEXT NOT NULL DEFAULT 'HOLD'",
                "symbol": "TEXT DEFAULT ''",
                "lots": "REAL DEFAULT 0.0",
                "tp_cash": "REAL",
                "tp_price": "REAL",
                "sl_price": "REAL",
                "magic": "INTEGER DEFAULT 246810",
                "intent": "TEXT DEFAULT 'UNKNOWN'",
                "status": "TEXT NOT NULL DEFAULT 'queued'",
                "trace_id": "TEXT DEFAULT ''",
                "created_at": "REAL NOT NULL DEFAULT 0.0",
                "updated_at": "REAL NOT NULL DEFAULT 0.0",
                "expires_at": "REAL NOT NULL DEFAULT 0.0",
                "delivered_count": "INTEGER NOT NULL DEFAULT 0",
                "reason": "TEXT DEFAULT ''",
                "payload_json": "TEXT DEFAULT '{}'",
                "ack_json": "TEXT DEFAULT ''",
                "t_bridge_queued": "REAL",
                "t_bridge_delivered": "REAL",
                "t_bridge_ack_finalized": "REAL",
            },
        )
        self._ensure_columns_locked(
            "command_events",
            {
                "command_id": "TEXT",
                "event_status": "TEXT NOT NULL DEFAULT 'queued'",
                "reason": "TEXT DEFAULT ''",
                "ts": "REAL NOT NULL DEFAULT 0.0",
                "event_json": "TEXT DEFAULT '{}'",
            },
        )
        self._ensure_columns_locked(
            "market_ticks",
            {
                "symbol": "TEXT",
                "bid": "REAL",
                "ask": "REAL",
                "spread": "REAL DEFAULT 0.0",
                "ts": "REAL NOT NULL DEFAULT 0.0",
                "raw_json": "TEXT DEFAULT ''",
            },
        )
        self._ensure_columns_locked(
            "reports",
            {
                "ts": "REAL NOT NULL DEFAULT 0.0",
                "report_text": "TEXT",
                "report_json": "TEXT DEFAULT ''",
            },
        )
        self._ensure_columns_locked(
            "account_snapshots",
            {
                "ts": "REAL NOT NULL DEFAULT 0.0",
                "equity": "REAL",
                "margin": "REAL",
                "freemargin": "REAL",
                "leverage": "REAL",
                "source": "TEXT",
                "raw_json": "TEXT",
            },
        )
        self._ensure_columns_locked(
            "position_snapshots",
            {
                "ts": "REAL NOT NULL DEFAULT 0.0",
                "source": "TEXT",
                "positions_json": "TEXT",
            },
        )
        self._ensure_columns_locked(
            "decision_snapshots",
            {
                "ts": "REAL NOT NULL DEFAULT 0.0",
                "vol": "REAL",
                "decisions_json": "TEXT",
                "diagnostics_json": "TEXT",
                "rejection_json": "TEXT",
                "attribution_json": "TEXT",
            },
        )
        self._ensure_columns_locked(
            "governance_events",
            {
                "ts": "REAL NOT NULL DEFAULT 0.0",
                "event_type": "TEXT NOT NULL DEFAULT 'state_update'",
                "reason": "TEXT",
                "payload_json": "TEXT",
            },
        )
        self._ensure_columns_locked(
            "runtime_state",
            {
                "id": "INTEGER DEFAULT 1",
                "snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
                "updated_at": "REAL NOT NULL DEFAULT 0.0",
            },
        )

        now_ts = _now()
        if self._table_exists_locked("commands"):
            self._conn.execute(
                """
                UPDATE commands
                SET
                    session_id = COALESCE(NULLIF(TRIM(session_id), ''), 'default'),
                    proto = COALESCE(NULLIF(TRIM(proto), ''), 'v2'),
                    cmd = COALESCE(NULLIF(TRIM(cmd), ''), 'HOLD'),
                    status = COALESCE(NULLIF(TRIM(status), ''), 'queued'),
                    intent = COALESCE(NULLIF(TRIM(intent), ''), 'UNKNOWN'),
                    trace_id = COALESCE(trace_id, ''),
                    reason = COALESCE(reason, ''),
                    payload_json = CASE WHEN payload_json IS NULL OR payload_json = '' THEN '{}' ELSE payload_json END,
                    ack_json = COALESCE(ack_json, ''),
                    lots = COALESCE(lots, 0.0),
                    delivered_count = COALESCE(delivered_count, 0),
                    created_at = CASE WHEN created_at IS NULL OR created_at <= 0 THEN ? ELSE created_at END,
                    updated_at = CASE
                        WHEN updated_at IS NULL OR updated_at <= 0 THEN
                            CASE WHEN created_at IS NULL OR created_at <= 0 THEN ? ELSE created_at END
                        ELSE updated_at
                    END,
                    expires_at = COALESCE(expires_at, 0.0),
                    t_bridge_queued = COALESCE(t_bridge_queued, created_at, ?)
                """,
                (float(now_ts), float(now_ts), float(now_ts)),
            )

        if self._table_exists_locked("command_events"):
            self._conn.execute(
                """
                UPDATE command_events
                SET
                    event_status = COALESCE(NULLIF(TRIM(event_status), ''), 'queued'),
                    reason = COALESCE(reason, ''),
                    ts = CASE WHEN ts IS NULL OR ts <= 0 THEN ? ELSE ts END,
                    event_json = CASE WHEN event_json IS NULL OR event_json = '' THEN '{}' ELSE event_json END
                """,
                (float(now_ts),),
            )

        if self._table_exists_locked("runtime_state"):
            self._conn.execute(
                """
                UPDATE runtime_state
                SET
                    snapshot_json = CASE WHEN snapshot_json IS NULL OR snapshot_json = '' THEN '{}' ELSE snapshot_json END,
                    updated_at = CASE WHEN updated_at IS NULL OR updated_at <= 0 THEN ? ELSE updated_at END
                """,
                (float(now_ts),),
            )

        self._conn.commit()

    def _init_schema_locked(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS commands (
                command_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                proto TEXT NOT NULL,
                cmd TEXT NOT NULL,
                symbol TEXT,
                lots REAL,
                tp_cash REAL,
                tp_price REAL,
                sl_price REAL,
                magic INTEGER,
                intent TEXT,
                status TEXT NOT NULL,
                trace_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                delivered_count INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                payload_json TEXT,
                ack_json TEXT,
                t_bridge_queued REAL,
                t_bridge_delivered REAL,
                t_bridge_ack_finalized REAL
            );

            CREATE TABLE IF NOT EXISTS command_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_id TEXT NOT NULL,
                event_status TEXT NOT NULL,
                reason TEXT,
                ts REAL NOT NULL,
                event_json TEXT,
                FOREIGN KEY(command_id) REFERENCES commands(command_id)
            );

            CREATE TABLE IF NOT EXISTS market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                bid REAL,
                ask REAL,
                spread REAL,
                ts REAL NOT NULL,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                report_text TEXT,
                report_json TEXT
            );

            CREATE TABLE IF NOT EXISTS account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                equity REAL,
                margin REAL,
                freemargin REAL,
                leverage REAL,
                source TEXT,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS position_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                source TEXT,
                positions_json TEXT
            );

            CREATE TABLE IF NOT EXISTS decision_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                vol REAL,
                decisions_json TEXT,
                diagnostics_json TEXT,
                rejection_json TEXT,
                attribution_json TEXT
            );

            CREATE TABLE IF NOT EXISTS governance_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                reason TEXT,
                payload_json TEXT
            );

            CREATE TABLE IF NOT EXISTS runtime_state (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                snapshot_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            """
        )
        self._migrate_legacy_schema_locked()
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_commands_status_created
                ON commands(status, created_at);
            CREATE INDEX IF NOT EXISTS idx_command_events_cmd_ts
                ON command_events(command_id, ts);
            CREATE INDEX IF NOT EXISTS idx_market_ticks_symbol_ts
                ON market_ticks(symbol, ts);
            CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports(ts);
            CREATE INDEX IF NOT EXISTS idx_account_snapshots_ts ON account_snapshots(ts);
            CREATE INDEX IF NOT EXISTS idx_position_snapshots_ts ON position_snapshots(ts);
            CREATE INDEX IF NOT EXISTS idx_decision_snapshots_ts ON decision_snapshots(ts);
            CREATE INDEX IF NOT EXISTS idx_governance_events_ts ON governance_events(ts);
            """
        )
        row = self._conn.execute("SELECT id FROM runtime_state WHERE id = 1").fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO runtime_state(id, snapshot_json, updated_at) VALUES(1, ?, ?)",
                (_jdump(_default_state()), _now()),
            )
        self._conn.commit()

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
            magic=int(row["magic"] or 246810),
            intent=str(row["intent"] or "UNKNOWN"),
            trace_id=str(row["trace_id"] or ""),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            expires_at=float(row["expires_at"]),
            delivered_count=int(row["delivered_count"] or 0),
            payload=dict(payload or {}),
        )

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
            if cur_status in {CommandStatus.ACKED.value, CommandStatus.FAILED.value, CommandStatus.EXPIRED.value}:
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
            if next_status == CommandStatus.ACKED.value and str(row["cmd"]).upper() in {"BUY", "SELL"}:
                st["trades_executed"] = int(st.get("trades_executed", 0)) + 1
            st["last_update"] = now_ts
            self._put_state_locked(st, now_ts)
            self._conn.commit()

            return {
                "status": next_status,
                "command_id": ack.command_id,
                "ticket": int(ack.ticket),
            }, 200

    def record_tick(self, payload: dict[str, Any]) -> None:
        sym = str(payload.get("symbol", "")).strip()
        if not sym:
            return

        with self._lock:
            now_ts = _now()
            ts = payload.get("ts", payload.get("time", now_ts))
            try:
                ts_f = float(ts)
            except Exception:
                ts_f = now_ts

            bid = float(payload.get("bid", 0.0) or 0.0)
            ask = float(payload.get("ask", 0.0) or 0.0)
            spread = float(payload.get("spread", 0.0) or 0.0)
            self._conn.execute(
                "INSERT INTO market_ticks(symbol, bid, ask, spread, ts, raw_json) VALUES(?, ?, ?, ?, ?, ?)",
                (sym, bid, ask, spread, ts_f, _jdump(dict(payload or {}))),
            )
            self._conn.commit()

    def _insert_account_snapshot_locked(self, *, now_ts: float, source: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO account_snapshots(ts, equity, margin, freemargin, leverage, source, raw_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(now_ts),
                _safe_float(payload.get("equity", 0.0), 0.0),
                _safe_float(payload.get("margin", 0.0), 0.0),
                _safe_float(payload.get("freemargin", 0.0), 0.0),
                _safe_float(payload.get("leverage", 0.0), 0.0),
                str(source or "unknown"),
                _jdump(dict(payload or {})),
            ),
        )

    def _insert_position_snapshot_locked(self, *, now_ts: float, source: str, positions: list[dict[str, Any]]) -> None:
        self._conn.execute(
            "INSERT INTO position_snapshots(ts, source, positions_json) VALUES(?, ?, ?)",
            (
                float(now_ts),
                str(source or "unknown"),
                _jdump(list(positions or [])),
            ),
        )

    @staticmethod
    def _governance_view(gov: dict[str, Any]) -> dict[str, Any]:
        reasons = [str(x) for x in list(gov.get("reasons", []) or [])]
        return {
            "paused": bool(gov.get("paused", False)),
            "risk_scale": _safe_float(gov.get("risk_scale", 1.0), 1.0),
            "reasons": reasons,
            "drawdown_pct": _safe_float(gov.get("drawdown_pct", 0.0), 0.0),
            "daily_loss_pct": _safe_float(gov.get("daily_loss_pct", 0.0), 0.0),
        }

    @staticmethod
    def _plugin_cfg_from_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
        plugin_flags = dict((diagnostics or {}).get("plugin_flags", {}) or {})
        if plugin_flags:
            return plugin_flags
        last_diag = dict((diagnostics or {}).get("last_diag", {}) or {})
        return {
            "use_hawkes": bool(last_diag.get("hawkes_n", 0.0)),
            "use_lppls": bool(last_diag.get("lppls_hazard", 0.0)),
            "use_heston_guard": bool(abs(_safe_float(last_diag.get("heston_scale", 1.0), 1.0) - 1.0) > 1e-9),
            "use_ai_indicator_model": (
                bool(last_diag.get("ai_enabled", False))
                or bool(last_diag.get("direction_samples", 0))
                or bool(last_diag.get("direction_side_samples", 0))
            ),
        }

    @staticmethod
    def _stage_attribution(diagnostics: dict[str, Any]) -> dict[str, Any]:
        last_diag = dict((diagnostics or {}).get("last_diag", {}) or {})
        return {
            "feature_extraction": {
                "vol": _safe_float(last_diag.get("vol", 0.0), 0.0),
                "p_trend": _safe_float(last_diag.get("p_trend", 0.5), 0.5),
                "regime_bucket": str(last_diag.get("regime_bucket", "unknown")),
            },
            "model_scoring": {
                "score": _safe_float(last_diag.get("score", 0.0), 0.0),
                "score_effective": _safe_float(last_diag.get("score_effective", last_diag.get("score", 0.0)), 0.0),
                "raw_signal": _safe_float(last_diag.get("raw_signal", 0.0), 0.0),
                "momentum_component": _safe_float(last_diag.get("momentum_component", 0.0), 0.0),
                "micro_component": _safe_float(last_diag.get("micro_component", 0.0), 0.0),
                "gate_penalty": _safe_float(last_diag.get("gate_penalty", 1.0), 1.0),
            },
            "gating": {
                "entry_gate_mode": str((diagnostics or {}).get("entry_gate_mode", "unknown")),
                "execution_gate_mode": str((diagnostics or {}).get("execution_gate_mode", "unknown")),
                "utility_gate_mode": str((diagnostics or {}).get("utility_gate_mode", "off")),
            },
            "confidence_execution_readiness": {
                "predictive_sharpe": _safe_float(last_diag.get("predictive_sharpe", 0.0), 0.0),
                "predictive_sharpe_aligned": _safe_float(last_diag.get("predictive_sharpe_aligned", 0.0), 0.0),
                "horizon_confidence": _safe_float(last_diag.get("horizon_confidence", 0.0), 0.0),
            },
            "sizing_dispatch": {
                "portfolio_risk": dict((diagnostics or {}).get("portfolio_risk", {}) or {}),
                "governance": dict((diagnostics or {}).get("governance", {}) or {}),
                "risk_envelope": dict((diagnostics or {}).get("risk_envelope", {}) or {}),
            },
        }

    def record_report(self, report_text: str, report_json: dict[str, Any] | None = None) -> None:
        with self._lock:
            now_ts = _now()
            self._conn.execute(
                "INSERT INTO reports(ts, report_text, report_json) VALUES(?, ?, ?)",
                (now_ts, str(report_text or ""), _jdump(report_json) if report_json else ""),
            )

            state = self._get_state_locked()
            if report_json and isinstance(report_json, dict):
                typ = str(report_json.get("type", "")).upper().strip()
                if typ == "HEARTBEAT":
                    state["last_heartbeat"] = now_ts
                    state["system_status"] = "connected"
                    state["equity"] = float(report_json.get("equity", 0.0) or 0.0)
                    state["margin"] = float(report_json.get("margin", 0.0) or 0.0)
                    state["freemargin"] = float(report_json.get("freemargin", 0.0) or 0.0)
                    state["leverage"] = float(report_json.get("leverage", 0.0) or 0.0)
                    self._insert_account_snapshot_locked(now_ts=now_ts, source="heartbeat", payload=report_json)
                elif typ == "POSITIONS":
                    positions = list(report_json.get("positions", []) or [])
                    state["positions"] = positions
                    state["last_pos_update"] = now_ts
                    self._insert_position_snapshot_locked(now_ts=now_ts, source="positions", positions=positions)

            state["last_update"] = now_ts
            self._put_state_locked(state, now_ts)
            self._conn.commit()

    def store_decisions(self, *, decisions: list[dict[str, Any]], vol: float, diagnostics: dict[str, Any]) -> None:
        with self._lock:
            now_ts = _now()
            state = self._get_state_locked()

            plugin_cfg = self._plugin_cfg_from_diagnostics(diagnostics)
            execution_quality = dict((diagnostics or {}).get("execution_quality", {}) or {})
            batch = self._decision_pipeline.run_many(
                decisions=list(decisions or []),
                diagnostics=dict(diagnostics or {}),
                plugin_cfg=plugin_cfg,
                sizing_cfg={
                    "min_confidence": _safe_float(execution_quality.get("min_confidence", 35.0), 35.0),
                    "base_lot": 0.03,
                    "min_lot": 0.01,
                    "max_lot": 2.0,
                },
            )

            rejection = dict(batch.rejection_taxonomy or {})
            if not rejection:
                rejection = dict((diagnostics or {}).get("rejection_stats", {}) or {})
            attribution = {
                "pipeline_rows": [row.to_dict() for row in batch.rows],
                "plugin_errors": [dict(err) for err in batch.plugin_errors],
                "diagnostics_stage_attribution": self._stage_attribution(diagnostics),
            }
            self._conn.execute(
                """
                INSERT INTO decision_snapshots(ts, vol, decisions_json, diagnostics_json, rejection_json, attribution_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    float(now_ts),
                    float(vol or 0.0),
                    _jdump(list(decisions or [])),
                    _jdump(dict(diagnostics or {})),
                    _jdump(rejection),
                    _jdump(attribution),
                ),
            )

            gov = dict((diagnostics or {}).get("governance", {}) or {})
            prev_gov = dict(state.get("governance", {}) or {})
            prev_fp = str(state.get("_governance_fp", ""))
            next_view = self._governance_view(gov) if gov else {}
            next_fp = _jdump(next_view) if next_view else ""
            if gov and next_fp != prev_fp:
                prev_paused = bool(prev_gov.get("paused", False))
                next_paused = bool(gov.get("paused", False))
                if next_paused and not prev_paused:
                    event_type = "pause_on"
                elif prev_paused and not next_paused:
                    event_type = "pause_off"
                else:
                    event_type = "state_update"
                reasons = [str(x) for x in list(gov.get("reasons", []) or [])]
                reason = ",".join(reasons[:3]) if reasons else "governance_update"
                event_payload = {
                    "governance": dict(gov),
                    "vol": float(vol or 0.0),
                    "p_trend": _safe_float((diagnostics or {}).get("last_diag", {}).get("p_trend", 0.5), 0.5),
                }
                self._conn.execute(
                    "INSERT INTO governance_events(ts, event_type, reason, payload_json) VALUES(?, ?, ?, ?)",
                    (float(now_ts), str(event_type), str(reason), _jdump(event_payload)),
                )
                state["governance_last_event"] = {
                    "time": float(now_ts),
                    "event_type": str(event_type),
                    "reason": str(reason),
                }
                state["_governance_fp"] = str(next_fp)

            p_trend = _safe_float((diagnostics or {}).get("last_diag", {}).get("p_trend", 0.5), 0.5)
            env = compute_adaptive_risk_envelope(
                volatility=float(vol or 0.0),
                trend_prob=float(p_trend),
                soft_band=self.soft_band,
                hard_band=self.hard_band,
                daily_band=self.daily_band,
                now_ts=now_ts,
            )

            state["agent_decisions"] = list(decisions or [])
            state["agent_diagnostics"] = dict(diagnostics or {})
            state["monitor"] = dict((diagnostics or {}).get("monitor", {}) or {})
            state["vol"] = float(vol or 0.0)
            state["decision_pipeline_plugin_errors"] = [dict(err) for err in batch.plugin_errors[:20]]
            if gov:
                state["governance"] = dict(gov)
            state["risk_envelope"] = env.to_dict()
            state["last_update"] = now_ts

            self._put_state_locked(state, now_ts)
            self._conn.commit()

    def update_state_patch(self, patch: dict[str, Any]) -> None:
        if not patch:
            return
        with self._lock:
            now_ts = _now()
            state = self._get_state_locked()
            state.update(dict(patch))
            state["last_update"] = now_ts
            self._put_state_locked(state, now_ts)
            self._conn.commit()

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
