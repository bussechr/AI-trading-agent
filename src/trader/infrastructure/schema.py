"""Database schema DDL, migration, and legacy backfill logic."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.trader.interfaces.dto import CommandStatus
from src.trader.utils import safe_float as _safe_float

from ._helpers import _jdump, _now


class SchemaMixin:
    """Mixin providing schema initialisation and legacy migration."""

    _conn: sqlite3.Connection

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
            from ._helpers import _default_state
            self._conn.execute(
                "INSERT INTO runtime_state(id, snapshot_json, updated_at) VALUES(1, ?, ?)",
                (_jdump(_default_state()), _now()),
            )
        self._conn.commit()
