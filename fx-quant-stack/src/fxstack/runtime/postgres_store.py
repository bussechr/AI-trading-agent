# AGENT: ROLE: DB-backed runtime store for commands, ticks, reports, decisions, governance events, and bridge state patches.
# AGENT: ENTRYPOINT: constructed by `fxstack/runtime/service.py`.
# AGENT: PRIMARY INPUTS: `ExecutionCommand`, `ExecutionAck`, state patch dicts, decision payloads, tick/report payloads.
# AGENT: PRIMARY OUTPUTS: durable queue rows, command events, state snapshots, tick history, reports.
# AGENT: DEPENDS ON: `fxstack/runtime/dto.py`, `fxstack/settings.py`, SQLAlchemy/Alembic.
# AGENT: CALLED BY: `fxstack/runtime/service.py`.
# AGENT: STATE / SIDE EFFECTS: owns command queue tables and the bridge-visible runtime state store.
# AGENT: HANDSHAKES: command enqueue/poll/ack, runtime patch persistence, readiness/state reads used by bridge and ops.
# AGENT: SEE: `docs/agents/runtime-loop.md` -> `fxstack/runtime/service.py` -> `docs/agents/bridge-and-api-handshakes.md`
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    func,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine

from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.sqlite_url import ensure_sqlite_database_dir
from fxstack.settings import get_settings


def _now() -> float:
    return float(time.time())


class PostgresRuntimeStore:
    def __init__(
        self,
        database_url: str,
        *,
        requeue_age_secs: float = 90.0,
        connect_retries: int = 5,
    ) -> None:
        self.database_url = ensure_sqlite_database_dir(database_url, base_dir=Path.cwd())
        self.requeue_age_secs = float(max(5.0, requeue_age_secs))
        self.engine: Engine = create_engine(
            self.database_url,
            future=True,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        self.meta = MetaData()
        self._lock = threading.RLock()

        # AGENT STATE: Table definitions here form the durable contract that backs bridge `/v2/state`, `/v2/commands`, and runtime recovery.
        self.commands = Table(
            "commands",
            self.meta,
            Column("command_id", String(128), primary_key=True),
            Column("session_id", String(64), nullable=False),
            Column("proto", String(16), nullable=False, default="v2"),
            Column("cmd", String(32), nullable=False),
            Column("symbol", String(16), nullable=True),
            Column("lots", Float, nullable=True),
            Column("tp_cash", Float, nullable=True),
            Column("tp_price", Float, nullable=True),
            Column("sl_price", Float, nullable=True),
            Column("magic", Integer, nullable=True),
            Column("intent", String(32), nullable=True),
            Column("trace_id", String(128), nullable=True),
            Column("status", String(32), nullable=False),
            Column("created_at", Float, nullable=False),
            Column("updated_at", Float, nullable=False),
            Column("expires_at", Float, nullable=False),
            Column("delivered_count", Integer, nullable=False, default=0),
            Column("reason", Text, nullable=True),
            Column("payload_json", JSON, nullable=True),
            Column("ack_json", JSON, nullable=True),
        )
        Index("ix_commands_status", self.commands.c.status)
        Index("ix_commands_created", self.commands.c.created_at)
        Index("ix_commands_expires", self.commands.c.expires_at)

        self.command_events = Table(
            "command_events",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("command_id", String(128), nullable=False),
            Column("event_status", String(32), nullable=False),
            Column("reason", Text, nullable=True),
            Column("ts", Float, nullable=False),
            Column("event_json", JSON, nullable=True),
        )
        Index("ix_command_events_command_id", self.command_events.c.command_id)
        Index("ix_command_events_ts", self.command_events.c.ts)

        self.market_ticks = Table(
            "market_ticks",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("symbol", String(16), nullable=False),
            Column("bid", Float, nullable=True),
            Column("ask", Float, nullable=True),
            Column("spread", Float, nullable=True),
            Column("ts", Float, nullable=False),
            Column("raw_json", JSON, nullable=True),
        )
        Index("ix_market_ticks_symbol", self.market_ticks.c.symbol)
        Index("ix_market_ticks_ts", self.market_ticks.c.ts)

        self.reports = Table(
            "reports",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", Float, nullable=False),
            Column("report_text", Text, nullable=True),
            Column("report_json", JSON, nullable=True),
        )
        Index("ix_reports_ts", self.reports.c.ts)

        self.decision_snapshots = Table(
            "decision_snapshots",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", Float, nullable=False),
            Column("vol", Float, nullable=True),
            Column("decisions_json", JSON, nullable=True),
            Column("diagnostics_json", JSON, nullable=True),
        )
        Index("ix_decision_snapshots_ts", self.decision_snapshots.c.ts)

        self.governance_events = Table(
            "governance_events",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", Float, nullable=False),
            Column("event_type", String(64), nullable=False),
            Column("reason", Text, nullable=True),
            Column("payload_json", JSON, nullable=True),
        )
        Index("ix_governance_events_ts", self.governance_events.c.ts)
        Index("ix_governance_events_type", self.governance_events.c.event_type)

        self.feature_push_outbox = Table(
            "feature_push_outbox",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("outbox_key", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("feature_service", String(128), nullable=False),
            Column("entity_key", String(128), nullable=False),
            Column("event_timestamp", Float, nullable=False),
            Column("feature_version", String(128), nullable=True),
            Column("checksum", String(128), nullable=True),
            Column("payload_json", JSON, nullable=False),
            Column("status", String(32), nullable=False),
            Column("attempt_count", Integer, nullable=False, default=0),
            Column("claimed_by", String(128), nullable=True),
            Column("claimed_at", Float, nullable=True),
            Column("last_error", Text, nullable=True),
            Column("created_at", Float, nullable=False),
            Column("updated_at", Float, nullable=False),
            Column("delivered_at", Float, nullable=True),
        )
        Index("ix_feature_push_outbox_key", self.feature_push_outbox.c.outbox_key, unique=True)
        Index("ix_feature_push_outbox_status", self.feature_push_outbox.c.status)
        Index("ix_feature_push_outbox_pair", self.feature_push_outbox.c.pair)
        Index("ix_feature_push_outbox_created_at", self.feature_push_outbox.c.created_at)
        Index("ix_feature_push_outbox_entity_key", self.feature_push_outbox.c.entity_key)

        self.feature_push_audit = Table(
            "feature_push_audit",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("outbox_key", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("feature_service", String(128), nullable=False),
            Column("entity_key", String(128), nullable=False),
            Column("event_timestamp", Float, nullable=False),
            Column("status", String(32), nullable=False),
            Column("worker_id", String(128), nullable=True),
            Column("message", Text, nullable=True),
            Column("payload_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_feature_push_audit_outbox_key", self.feature_push_audit.c.outbox_key)
        Index("ix_feature_push_audit_status", self.feature_push_audit.c.status)
        Index("ix_feature_push_audit_created_at", self.feature_push_audit.c.created_at)

        self.feature_parity_audit = Table(
            "feature_parity_audit",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("pair", String(16), nullable=False),
            Column("feature_service", String(128), nullable=False),
            Column("entity_key", String(128), nullable=False),
            Column("event_timestamp", Float, nullable=False),
            Column("source", String(32), nullable=False),
            Column("parity_ok", Integer, nullable=False),
            Column("drift_score", Float, nullable=True),
            Column("message", Text, nullable=True),
            Column("payload_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_feature_parity_audit_pair", self.feature_parity_audit.c.pair)
        Index("ix_feature_parity_audit_service", self.feature_parity_audit.c.feature_service)
        Index("ix_feature_parity_audit_created_at", self.feature_parity_audit.c.created_at)

        self.runtime_state = Table(
            "runtime_state",
            self.meta,
            Column("id", Integer, primary_key=True),
            Column("snapshot_json", JSON, nullable=False),
            Column("updated_at", Float, nullable=False),
        )

        self.model_runs = Table(
            "model_runs",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("run_id", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("timeframe", String(16), nullable=True),
            Column("model_family", String(64), nullable=False),
            Column("artifact_path", Text, nullable=False),
            Column("metadata_json", JSON, nullable=True),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_model_runs_run_id", self.model_runs.c.run_id, unique=True)
        Index("ix_model_runs_pair", self.model_runs.c.pair)

        self.model_artifacts = Table(
            "model_artifacts",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("model_set_id", String(128), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("artifact_type", String(64), nullable=False),
            Column("artifact_path", Text, nullable=False),
            Column("checksum", String(128), nullable=True),
            Column("metadata_json", JSON, nullable=True),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_model_artifacts_set", self.model_artifacts.c.model_set_id)

        self.active_model_sets = Table(
            "active_model_sets",
            self.meta,
            Column("pair", String(16), primary_key=True),
            Column("model_set_id", String(128), nullable=False),
            Column("registry_path", Text, nullable=False),
            Column("artifacts_json", JSON, nullable=False),
            Column("metadata_json", JSON, nullable=True),
            Column("enabled", Integer, nullable=False, default=1),
            Column("updated_at", Float, nullable=False),
        )
        Index("ix_active_model_sets_enabled", self.active_model_sets.c.enabled)

        self._connect_with_retry(max(1, int(connect_retries)))
        self._bootstrap_schema()
        self._ensure_state_row()
        self.cleanup_expired_commands()

    def _bootstrap_schema(self) -> None:
        s = get_settings()
        allow_create_all = bool(getattr(s, "runtime_allow_create_all", False))
        check = self.verify_required_tables()
        missing = list(check.get("missing_tables", check.get("missing", [])) or [])
        if missing and allow_create_all:
            self.meta.create_all(self.engine)
            check = self.verify_required_tables()
            missing = list(check.get("missing_tables", check.get("missing", [])) or [])
        if not bool(check.get("ok")):
            migration = dict(check.get("migration") or {})
            migration_error = str(migration.get("error") or "")
            raise RuntimeError(
                "runtime schema verification failed: "
                + f"missing_tables={sorted(missing)} "
                + f"migration_ok={bool(migration.get('ok'))} "
                + (f"migration_error={migration_error} " if migration_error else "")
                + "Run `trader db migrate` before starting runtime/bridge."
            )

    def _connect_with_retry(self, retries: int) -> None:
        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return
            except Exception as exc:  # pragma: no cover - environment dependent
                last_exc = exc
                if attempt >= retries:
                    break
                time.sleep(min(5.0, 0.5 * attempt))
        if last_exc is not None:
            raise last_exc

    def verify_required_tables(self) -> dict[str, Any]:
        required = {
            "commands",
            "command_events",
            "runtime_state",
            "market_ticks",
            "reports",
            "decision_snapshots",
            "governance_events",
            "feature_push_outbox",
            "feature_push_audit",
            "feature_parity_audit",
            "model_runs",
            "model_artifacts",
            "active_model_sets",
        }
        inspector = inspect(self.engine)
        present = set(inspector.get_table_names())
        missing = sorted(required - present)
        expected_heads: list[str] = []
        current_revisions: list[str] = []
        migration_error = ""
        migration_ok = False
        try:
            repo_root = Path(__file__).resolve().parents[3]
            ini = repo_root / "alembic.ini"
            cfg = Config(str(ini))
            cfg.set_main_option("script_location", str(repo_root / "alembic"))
            script = ScriptDirectory.from_config(cfg)
            expected_heads = sorted(str(h) for h in script.get_heads())
            if "alembic_version" in present:
                with self.engine.connect() as conn:
                    rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                current_revisions = sorted({str(r[0]) for r in rows if r and r[0]})
            migration_ok = bool(expected_heads) and set(current_revisions) == set(expected_heads)
        except Exception as exc:
            migration_error = f"{type(exc).__name__}: {exc}"

        ok = len(missing) == 0 and bool(migration_ok)
        return {
            "required": sorted(required),
            "present": sorted(present),
            "missing": missing,
            "missing_tables": missing,
            "migration": {
                "ok": bool(migration_ok),
                "expected_heads": expected_heads,
                "current_revisions": current_revisions,
                "error": migration_error,
            },
            "ok": ok,
        }

    def _ensure_state_row(self) -> None:
        with self.engine.begin() as conn:
            row = conn.execute(select(self.runtime_state.c.id).where(self.runtime_state.c.id == 1)).fetchone()
            if row is None:
                conn.execute(
                    self.runtime_state.insert().values(
                        id=1,
                        snapshot_json={
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
                            "risk_envelope": {},
                            "current_thought": "",
                            "last_update": _now(),
                        },
                        updated_at=_now(),
                    )
                )

    def _append_command_event(
        self,
        *,
        command_id: str,
        event_status: str,
        reason: str,
        payload: dict[str, Any] | None = None,
        conn=None,
    ) -> None:
        if conn is not None:
            conn.execute(
                self.command_events.insert().values(
                    command_id=command_id,
                    event_status=event_status,
                    reason=reason,
                    ts=_now(),
                    event_json=payload or {},
                )
            )
            return
        with self.engine.begin() as _conn:
            _conn.execute(
                self.command_events.insert().values(
                    command_id=command_id,
                    event_status=event_status,
                    reason=reason,
                    ts=_now(),
                    event_json=payload or {},
                )
            )

    def cleanup_expired_commands(self) -> int:
        now = _now()
        expired_rows: list[dict[str, Any]] = []
        with self._lock:
            with self.engine.begin() as conn:
                rows = conn.execute(
                    select(self.commands)
                    .where(self.commands.c.status.in_(["queued", "delivered"]))
                    .where(self.commands.c.expires_at < now)
                ).mappings().all()
                if not rows:
                    return 0

                for row in rows:
                    cid = str(row.get("command_id") or "")
                    if not cid:
                        continue
                    conn.execute(
                        update(self.commands)
                        .where(self.commands.c.command_id == cid)
                        .values(status="expired", updated_at=now, reason="ttl_expired")
                    )
                    self._append_command_event(
                        command_id=cid,
                        event_status="expired",
                        reason="ttl_expired",
                        payload={"expired_at": now},
                        conn=conn,
                    )
                    expired_rows.append(dict(row))
        return len(expired_rows)

    def requeue_stale_delivered(self, *, age_secs: float) -> int:
        now = _now()
        cutoff = now - max(1.0, float(age_secs))
        updated = 0
        with self._lock:
            with self.engine.begin() as conn:
                rows = conn.execute(
                    select(self.commands)
                    .where(self.commands.c.status == "delivered")
                    .where(self.commands.c.updated_at <= cutoff)
                    .where(self.commands.c.expires_at >= now)
                ).mappings().all()
                for row in rows:
                    cid = str(row.get("command_id") or "")
                    if not cid:
                        continue
                    conn.execute(
                        update(self.commands)
                        .where(self.commands.c.command_id == cid)
                        .values(status="queued", updated_at=now, reason="requeue_after_restart")
                    )
                    self._append_command_event(
                        command_id=cid,
                        event_status="queued",
                        reason="requeue_after_restart",
                        payload={"requeued_at": now},
                        conn=conn,
                    )
                    updated += 1
        return updated

    def purge_pending_commands(self, *, reason: str, intents: set[str] | None = None) -> int:
        now = _now()
        normalized_reason = str(reason or "runtime_restart_purged").strip() or "runtime_restart_purged"
        normalized_intents = {str(item or "").strip().upper() for item in (intents or set()) if str(item or "").strip()}
        updated = 0
        with self._lock:
            with self.engine.begin() as conn:
                stmt = select(self.commands).where(self.commands.c.status.in_(["queued", "delivered"]))
                if normalized_intents:
                    stmt = stmt.where(func.upper(func.coalesce(self.commands.c.intent, "")).in_(sorted(normalized_intents)))
                rows = conn.execute(stmt).mappings().all()
                for row in rows:
                    cid = str(row.get("command_id") or "")
                    if not cid:
                        continue
                    conn.execute(
                        update(self.commands)
                        .where(self.commands.c.command_id == cid)
                        .values(status="expired", updated_at=now, reason=normalized_reason)
                    )
                    self._append_command_event(
                        command_id=cid,
                        event_status="expired",
                        reason=normalized_reason,
                        payload={
                            "purged_at": now,
                            "purge_reason": normalized_reason,
                            "previous_status": str(row.get("status") or ""),
                            "intent": str(row.get("intent") or ""),
                        },
                        conn=conn,
                    )
                    updated += 1
        return updated

    def record_runtime_boot_state(self, *, boot: dict[str, Any], patch: dict[str, Any] | None = None, prune_state: bool = False) -> None:
        payload = dict(patch or {})
        payload["runtime_startup"] = dict(boot or {})
        if prune_state:
            payload["__prune_stale__"] = True
        self.update_state_patch(payload)

    def record_runtime_boot_failure(
        self,
        *,
        boot: dict[str, Any],
        failure_reason: str,
        failed_at: Any | None = None,
        patch: dict[str, Any] | None = None,
        prune_state: bool = False,
    ) -> None:
        failure_ts = float(_now()) if failed_at is None else None
        payload = dict(patch or {})
        boot_state = dict(boot or {})
        boot_state["failure_reason"] = str(failure_reason or "")
        if failed_at is None:
            boot_state["failed_at"] = float(failure_ts)
        else:
            boot_state["failed_at"] = failed_at
        payload["runtime_startup"] = boot_state
        if prune_state:
            payload["__prune_stale__"] = True
        self.update_state_patch(payload)
        self.record_governance_event(
            event_type="runtime_startup_failed",
            reason=str(failure_reason or ""),
            payload=boot_state,
            ts=failure_ts,
        )

    def record_governance_event(
        self,
        *,
        event_type: str,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        event_name = str(event_type or "").strip()
        if not event_name:
            raise ValueError("event_type is required")
        event_ts = float(_now() if ts is None else ts)
        with self.engine.begin() as conn:
            conn.execute(
                self.governance_events.insert().values(
                    ts=event_ts,
                    event_type=event_name,
                    reason=str(reason or ""),
                    payload_json=dict(payload or {}),
                )
            )

    def enqueue_feature_push(self, payload: dict[str, Any]) -> dict[str, Any]:
        outbox_key = str(payload.get("outbox_key") or "").strip()
        pair = str(payload.get("pair") or "").upper().strip()
        feature_service = str(payload.get("feature_service") or "").strip()
        entity_key = str(payload.get("entity_key") or "").strip()
        if not outbox_key or not pair or not feature_service or not entity_key:
            raise ValueError("outbox_key, pair, feature_service, and entity_key are required")
        now = _now()
        row = {
            "outbox_key": outbox_key,
            "pair": pair,
            "feature_service": feature_service,
            "entity_key": entity_key,
            "event_timestamp": float(payload.get("event_timestamp") or now),
            "feature_version": str(payload.get("feature_version") or ""),
            "checksum": str(payload.get("checksum") or ""),
            "payload_json": dict(payload.get("payload_json") or payload),
            "status": str(payload.get("status") or "queued"),
            "attempt_count": int(payload.get("attempt_count") or 0),
            "claimed_by": payload.get("claimed_by"),
            "claimed_at": payload.get("claimed_at"),
            "last_error": str(payload.get("last_error") or ""),
            "created_at": now,
            "updated_at": now,
            "delivered_at": payload.get("delivered_at"),
        }
        with self._lock:
            with self.engine.begin() as conn:
                existing = conn.execute(
                    select(self.feature_push_outbox.c.id).where(self.feature_push_outbox.c.outbox_key == outbox_key)
                ).first()
                if existing is None:
                    conn.execute(self.feature_push_outbox.insert().values(**row))
                else:
                    conn.execute(
                        update(self.feature_push_outbox)
                        .where(self.feature_push_outbox.c.outbox_key == outbox_key)
                        .values(**{k: v for k, v in row.items() if k != "created_at"})
                    )
        return dict(row)

    def claim_feature_push_batch(
        self,
        *,
        worker_id: str,
        limit: int = 50,
        statuses: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        worker = str(worker_id or "").strip()
        if not worker:
            raise ValueError("worker_id is required")
        allowed = {str(item or "").strip().lower() for item in (statuses or {"queued", "retry"}) if str(item or "").strip()}
        now = _now()
        claimed: list[dict[str, Any]] = []
        with self._lock:
            with self.engine.begin() as conn:
                stmt = select(self.feature_push_outbox).where(self.feature_push_outbox.c.status.in_(sorted(allowed)))
                stmt = stmt.order_by(self.feature_push_outbox.c.created_at.asc()).limit(max(1, min(limit, 500)))
                rows = conn.execute(stmt).mappings().all()
                for row in rows:
                    outbox_key = str(row.get("outbox_key") or "")
                    if not outbox_key:
                        continue
                    conn.execute(
                        update(self.feature_push_outbox)
                        .where(self.feature_push_outbox.c.outbox_key == outbox_key)
                        .values(
                            status="claimed",
                            claimed_by=worker,
                            claimed_at=now,
                            updated_at=now,
                            attempt_count=int(row.get("attempt_count") or 0) + 1,
                        )
                    )
                    claimed_row = dict(row)
                    claimed_row.update(
                        {
                            "status": "claimed",
                            "claimed_by": worker,
                            "claimed_at": now,
                            "updated_at": now,
                            "attempt_count": int(row.get("attempt_count") or 0) + 1,
                        }
                    )
                    claimed.append(claimed_row)
        return claimed

    def record_feature_push_audit(
        self,
        *,
        outbox_key: str,
        pair: str,
        feature_service: str,
        entity_key: str,
        event_timestamp: float,
        status: str,
        payload: dict[str, Any],
        worker_id: str | None = None,
        message: str | None = None,
        conn=None,
    ) -> dict[str, Any]:
        now = _now()
        row = {
            "outbox_key": str(outbox_key),
            "pair": str(pair).upper().strip(),
            "feature_service": str(feature_service),
            "entity_key": str(entity_key),
            "event_timestamp": float(event_timestamp),
            "status": str(status).lower().strip(),
            "worker_id": str(worker_id) if worker_id else None,
            "message": str(message or ""),
            "payload_json": dict(payload or {}),
            "created_at": now,
        }
        if conn is not None:
            conn.execute(self.feature_push_audit.insert().values(**row))
        else:
            with self.engine.begin() as inner:
                inner.execute(self.feature_push_audit.insert().values(**row))
        return row

    def mark_feature_push_success(
        self,
        *,
        outbox_key: str,
        worker_id: str | None = None,
        payload: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(self.feature_push_outbox).where(self.feature_push_outbox.c.outbox_key == str(outbox_key))
                ).mappings().first()
                if row is None:
                    raise KeyError(f"unknown outbox_key: {outbox_key}")
                conn.execute(
                    update(self.feature_push_outbox)
                    .where(self.feature_push_outbox.c.outbox_key == str(outbox_key))
                    .values(status="succeeded", delivered_at=now, updated_at=now, last_error="")
                )
                self.record_feature_push_audit(
                    outbox_key=str(outbox_key),
                    pair=str(row.get("pair") or ""),
                    feature_service=str(row.get("feature_service") or ""),
                    entity_key=str(row.get("entity_key") or ""),
                    event_timestamp=float(row.get("event_timestamp") or now),
                    status="succeeded",
                    payload=payload or dict(row.get("payload_json") or {}),
                    worker_id=worker_id,
                    message=message,
                    conn=conn,
                )
                result = dict(row)
                result.update({"status": "succeeded", "delivered_at": now, "updated_at": now})
                return result

    def mark_feature_push_failure(
        self,
        *,
        outbox_key: str,
        worker_id: str | None = None,
        message: str,
        payload: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> dict[str, Any]:
        now = _now()
        next_status = "retry" if retryable else "failed"
        with self._lock:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(self.feature_push_outbox).where(self.feature_push_outbox.c.outbox_key == str(outbox_key))
                ).mappings().first()
                if row is None:
                    raise KeyError(f"unknown outbox_key: {outbox_key}")
                conn.execute(
                    update(self.feature_push_outbox)
                    .where(self.feature_push_outbox.c.outbox_key == str(outbox_key))
                    .values(status=next_status, updated_at=now, last_error=str(message or ""))
                )
                self.record_feature_push_audit(
                    outbox_key=str(outbox_key),
                    pair=str(row.get("pair") or ""),
                    feature_service=str(row.get("feature_service") or ""),
                    entity_key=str(row.get("entity_key") or ""),
                    event_timestamp=float(row.get("event_timestamp") or now),
                    status=next_status,
                    payload=payload or dict(row.get("payload_json") or {}),
                    worker_id=worker_id,
                    message=message,
                    conn=conn,
                )
                result = dict(row)
                result.update({"status": next_status, "updated_at": now, "last_error": str(message or "")})
                return result

    def record_feature_parity_audit(
        self,
        *,
        pair: str,
        feature_service: str,
        entity_key: str,
        event_timestamp: float,
        source: str,
        parity_ok: bool,
        payload: dict[str, Any],
        drift_score: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        row = {
            "pair": str(pair).upper().strip(),
            "feature_service": str(feature_service),
            "entity_key": str(entity_key),
            "event_timestamp": float(event_timestamp),
            "source": str(source),
            "parity_ok": 1 if bool(parity_ok) else 0,
            "drift_score": drift_score,
            "message": str(message or ""),
            "payload_json": dict(payload or {}),
            "created_at": now,
        }
        with self.engine.begin() as conn:
            conn.execute(self.feature_parity_audit.insert().values(**row))
        return row

    def get_feature_push_outbox(self, *, limit: int = 200, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        stmt = select(self.feature_push_outbox)
        if statuses:
            stmt = stmt.where(self.feature_push_outbox.c.status.in_(sorted({str(s).lower().strip() for s in statuses if str(s).strip()})))
        stmt = stmt.order_by(self.feature_push_outbox.c.id.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_feature_push_audit(self, *, limit: int = 200, statuses: set[str] | None = None) -> list[dict[str, Any]]:
        stmt = select(self.feature_push_audit)
        if statuses:
            stmt = stmt.where(self.feature_push_audit.c.status.in_(sorted({str(s).lower().strip() for s in statuses if str(s).strip()})))
        stmt = stmt.order_by(self.feature_push_audit.c.id.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_feature_parity_audit(self, *, limit: int = 200, pair: str | None = None) -> list[dict[str, Any]]:
        stmt = select(self.feature_parity_audit)
        if pair:
            stmt = stmt.where(self.feature_parity_audit.c.pair == str(pair).upper().strip())
        stmt = stmt.order_by(self.feature_parity_audit.c.id.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_feature_push_rollup(self) -> dict[str, Any]:
        with self.engine.begin() as conn:
            outbox_counts = conn.execute(
                select(self.feature_push_outbox.c.status, func.count()).group_by(self.feature_push_outbox.c.status)
            ).all()
            audit_counts = conn.execute(
                select(self.feature_push_audit.c.status, func.count()).group_by(self.feature_push_audit.c.status)
            ).all()
            parity_counts = conn.execute(
                select(self.feature_parity_audit.c.parity_ok, func.count()).group_by(self.feature_parity_audit.c.parity_ok)
            ).all()
        pending = {"queued", "claimed", "retry"}
        return {
            "outbox": {
                "count": int(sum(int(v) for _, v in outbox_counts)),
                "pending": int(sum(int(v) for k, v in outbox_counts if str(k) in pending)),
                "by_status": {str(k): int(v) for k, v in outbox_counts},
            },
            "audit": {
                "count": int(sum(int(v) for _, v in audit_counts)),
                "by_status": {str(k): int(v) for k, v in audit_counts},
            },
            "parity": {
                "count": int(sum(int(v) for _, v in parity_counts)),
                "ok": int(sum(int(v) for k, v in parity_counts if int(k or 0) == 1)),
                "drift": int(sum(int(v) for k, v in parity_counts if int(k or 0) == 0)),
            },
        }

    def record_model_run(
        self,
        *,
        run_id: str,
        pair: str,
        timeframe: str,
        model_family: str,
        artifact_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "run_id": str(run_id),
            "pair": str(pair).upper(),
            "timeframe": str(timeframe).upper(),
            "model_family": str(model_family),
            "artifact_path": str(artifact_path),
            "metadata_json": dict(metadata or {}),
            "created_at": _now(),
        }
        with self.engine.begin() as conn:
            existing = conn.execute(select(self.model_runs.c.id).where(self.model_runs.c.run_id == payload["run_id"]))
            if existing.first() is None:
                conn.execute(self.model_runs.insert().values(**payload))

    def upsert_active_model_set(
        self,
        *,
        pair: str,
        model_set_id: str,
        registry_path: str,
        artifacts: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> None:
        symbol = str(pair).upper().strip()
        if not symbol:
            raise ValueError("pair is required")
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(select(self.active_model_sets.c.pair).where(self.active_model_sets.c.pair == symbol)).first()
            payload = {
                "pair": symbol,
                "model_set_id": str(model_set_id),
                "registry_path": str(registry_path),
                "artifacts_json": dict(artifacts or {}),
                "metadata_json": dict(metadata or {}),
                "enabled": 1 if enabled else 0,
                "updated_at": now,
            }
            if row is None:
                conn.execute(self.active_model_sets.insert().values(**payload))
            else:
                conn.execute(
                    update(self.active_model_sets)
                    .where(self.active_model_sets.c.pair == symbol)
                    .values(**payload)
                )

    def get_active_model_set(self, pair: str) -> dict[str, Any] | None:
        symbol = str(pair).upper().strip()
        with self.engine.begin() as conn:
            row = conn.execute(select(self.active_model_sets).where(self.active_model_sets.c.pair == symbol)).mappings().first()
        return dict(row) if row else None

    def get_active_model_sets(self, *, enabled_only: bool = True) -> dict[str, dict[str, Any]]:
        stmt = select(self.active_model_sets)
        if enabled_only:
            stmt = stmt.where(self.active_model_sets.c.enabled == 1)
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            pair = str(row.get("pair") or "").upper()
            if not pair:
                continue
            out[pair] = dict(row)
        return out

    def enqueue_command(self, cmd: ExecutionCommand) -> tuple[bool, str]:
        state_patch: dict[str, Any] | None = None
        with self._lock:
            with self.engine.begin() as conn:
                existing = conn.execute(select(self.commands.c.status).where(self.commands.c.command_id == cmd.command_id)).fetchone()
                if existing is not None:
                    return False, str(existing[0])

                conn.execute(
                    self.commands.insert().values(
                        command_id=cmd.command_id,
                        session_id=cmd.session_id,
                        proto=cmd.proto,
                        cmd=cmd.cmd,
                        symbol=cmd.symbol,
                        lots=cmd.lots,
                        tp_cash=cmd.tp_cash,
                        tp_price=cmd.tp_price,
                        sl_price=cmd.sl_price,
                        magic=cmd.magic,
                        intent=cmd.intent,
                        trace_id=cmd.trace_id,
                        status="queued",
                        created_at=cmd.created_at,
                        updated_at=cmd.updated_at,
                        expires_at=cmd.expires_at,
                        delivered_count=0,
                        reason="",
                        payload_json=cmd.payload,
                        ack_json={},
                    )
                )
                self._append_command_event(
                    command_id=cmd.command_id,
                    event_status="queued",
                    reason="queued",
                    payload=cmd.to_dict(),
                    conn=conn,
                )
                state_patch = {"command_id": cmd.command_id, "cmd": cmd.cmd, "symbol": cmd.symbol}

            if state_patch is not None:
                state = self.get_state()
                state["signals_sent"] = int(state.get("signals_sent", 0)) + 1
                state["last_signal"] = {
                    "command_id": str(state_patch["command_id"]),
                    "cmd": str(state_patch["cmd"]),
                    "symbol": str(state_patch["symbol"]),
                    "ts": _now(),
                }
                self.update_state_patch(state)
            return True, "queued"

    def poll_next_command(self) -> ExecutionCommand | None:
        now = _now()
        with self._lock:
            self.cleanup_expired_commands()
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(self.commands)
                    .where(and_(self.commands.c.status == "queued", self.commands.c.expires_at >= now))
                    .order_by(self.commands.c.created_at.asc())
                    .limit(1)
                ).mappings().first()
                if row is None:
                    return None

                conn.execute(
                    update(self.commands)
                    .where(self.commands.c.command_id == row["command_id"])
                    .values(
                        status="delivered",
                        updated_at=now,
                        delivered_count=int(row.get("delivered_count", 0)) + 1,
                    )
                )
                self._append_command_event(
                    command_id=str(row["command_id"]),
                    event_status="delivered",
                    reason="polled",
                    conn=conn,
                )

                row = dict(row)
                row["status"] = "delivered"
                row["updated_at"] = now
                row["delivered_count"] = int(row.get("delivered_count", 0)) + 1
                return ExecutionCommand(
                    command_id=str(row["command_id"]),
                    session_id=str(row["session_id"]),
                    proto=str(row["proto"]),
                    cmd=str(row["cmd"]),
                    symbol=str(row.get("symbol") or ""),
                    lots=float(row.get("lots") or 0.0),
                    tp_cash=row.get("tp_cash"),
                    tp_price=row.get("tp_price"),
                    sl_price=row.get("sl_price"),
                    close_lots=float((dict(row.get("payload_json") or {})).get("close_lots", 0.0) or 0.0),
                    magic=int(row.get("magic") or 246810),
                    intent=str(row.get("intent") or "UNKNOWN"),
                    trace_id=str(row.get("trace_id") or ""),
                    action=str((dict(row.get("payload_json") or {})).get("action") or ""),
                    action_score=float((dict(row.get("payload_json") or {})).get("action_score", 0.0) or 0.0),
                    reversal_token=str((dict(row.get("payload_json") or {})).get("reversal_token") or ""),
                    status="delivered",
                    created_at=float(row.get("created_at") or now),
                    updated_at=now,
                    expires_at=float(row.get("expires_at") or now),
                    delivered_count=int(row.get("delivered_count") or 1),
                    payload=dict(row.get("payload_json") or {}),
                )

    def ack_command(self, ack: ExecutionAck) -> tuple[dict[str, Any], int]:
        if not ack.command_id:
            return {"status": "error", "reason": "missing_command_id"}, 400

        status = str(ack.status).lower().strip()
        if status not in {"delivered", "acked", "failed", "duplicate"}:
            status = "failed"

        state_patch: dict[str, Any] | None = None
        with self._lock:
            with self.engine.begin() as conn:
                row = conn.execute(select(self.commands).where(self.commands.c.command_id == ack.command_id)).mappings().first()
                if row is None:
                    return {"status": "not_found", "command_id": ack.command_id}, 404

                cur = str(row["status"])
                if cur in {"acked", "failed", "expired", "duplicate"}:
                    return {"status": cur, "command_id": ack.command_id, "idempotent": True}, 200

                if status in {"acked", "failed", "duplicate"} and cur != "delivered":
                    return {
                        "status": "invalid_transition",
                        "command_id": ack.command_id,
                        "current": cur,
                        "requested": status,
                        "allowed": ["delivered"] if cur == "queued" else ["delivered", "acked", "failed"],
                    }, 409

                conn.execute(
                    update(self.commands)
                    .where(self.commands.c.command_id == ack.command_id)
                    .values(status=status, updated_at=ack.updated_at, ack_json=ack.to_dict(), reason=ack.message)
                )
                self._append_command_event(
                    command_id=ack.command_id,
                    event_status=status,
                    reason=ack.message,
                    payload=ack.to_dict(),
                    conn=conn,
                )
                state_patch = {"last_ack": ack.to_dict(), "inc_trades": 1 if bool(ack.count_as_trade) else 0}

            if state_patch is not None:
                state = self.get_state()
                state["last_ack"] = dict(state_patch["last_ack"])
                if int(state_patch.get("inc_trades", 0)) > 0:
                    state["trades_executed"] = int(state.get("trades_executed", 0)) + 1
                self.update_state_patch(state)
            return {"status": status, "command_id": ack.command_id}, 200

    def record_tick(self, payload: dict[str, Any]) -> None:
        sym = str(payload.get("symbol", "")).strip().upper()
        if not sym:
            return
        with self.engine.begin() as conn:
            conn.execute(
                self.market_ticks.insert().values(
                    symbol=sym,
                    bid=float(payload.get("bid", 0.0) or 0.0),
                    ask=float(payload.get("ask", 0.0) or 0.0),
                    spread=float(payload.get("spread", 0.0) or 0.0),
                    ts=_now(),
                    raw_json=dict(payload),
                )
            )

    def record_report(self, report_text: str, report_json: dict[str, Any] | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(self.reports.insert().values(ts=_now(), report_text=report_text, report_json=report_json or {}))

    def store_decisions(self, *, decisions: list[dict[str, Any]], vol: float, diagnostics: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                self.decision_snapshots.insert().values(
                    ts=_now(),
                    vol=float(vol),
                    decisions_json=list(decisions or []),
                    diagnostics_json=dict(diagnostics or {}),
                )
            )

        state = self.get_state()
        state["agent_decisions"] = list(decisions or [])
        state["agent_diagnostics"] = dict(diagnostics or {})
        state["vol"] = float(vol)
        self.update_state_patch(state)

    def update_state_patch(self, patch: dict[str, Any]) -> None:
        incoming = dict(patch or {})
        force_prune = bool(incoming.pop("__prune_stale__", False))
        with self._lock:
            with self.engine.begin() as conn:
                row = (
                    conn.execute(
                        select(self.runtime_state.c.snapshot_json)
                        .where(self.runtime_state.c.id == 1)
                        .with_for_update()
                    ).first()
                )
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                previous_profile = str(merged.get("runtime_profile", "") or "")
                merged.update(incoming)
                next_profile = str(merged.get("runtime_profile", "") or "")
                s = get_settings()
                should_prune = bool(force_prune) or (
                    bool(s.runtime_state_prune_stale_keys) and bool(next_profile) and next_profile != previous_profile
                )
                if should_prune:
                    for stale_key in s.runtime_state_stale_keys:
                        if stale_key and stale_key not in incoming and stale_key in merged:
                            merged.pop(stale_key, None)
                merged["last_update"] = _now()
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(snapshot_json=merged, updated_at=float(merged["last_update"]))
                    )

    def get_state(self) -> dict[str, Any]:
        with self.engine.begin() as conn:
            row = conn.execute(select(self.runtime_state.c.snapshot_json).where(self.runtime_state.c.id == 1)).first()
            return dict(row[0] if row else {})

    def get_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = conn.execute(select(self.reports).order_by(self.reports.c.id.desc()).limit(max(1, min(limit, 5000)))).mappings().all()
        return [dict(r) for r in rows]

    def get_decision_snapshots(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(self.decision_snapshots)
                .order_by(self.decision_snapshots.c.id.desc())
                .limit(max(1, min(limit, 5000)))
            ).mappings().all()
        return [dict(r) for r in rows]

    def get_closed_trade_reports(self, limit: int = 200) -> list[dict[str, Any]]:
        stmt = (
            select(self.reports)
            .where(
                or_(
                    self.reports.c.report_text.like('%"report_type":"closed_trade"%'),
                    self.reports.c.report_text.like('%"report_type": "closed_trade"%'),
                )
            )
            .order_by(self.reports.c.id.desc())
            .limit(max(1, min(limit, 5000)))
        )
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_commands(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = conn.execute(select(self.commands).order_by(self.commands.c.created_at.desc()).limit(max(1, min(limit, 5000)))).mappings().all()
        return [dict(r) for r in rows]

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = conn.execute(select(self.commands).where(self.commands.c.command_id == command_id)).mappings().first()
        return dict(row) if row else None

    def get_command_events(self, *, command_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        stmt = select(self.command_events)
        if command_id:
            stmt = stmt.where(self.command_events.c.command_id == command_id)
        stmt = stmt.order_by(self.command_events.c.id.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_governance_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.engine.begin() as conn:
            rows = conn.execute(select(self.governance_events).order_by(self.governance_events.c.id.desc()).limit(max(1, min(limit, 5000)))).mappings().all()
        return [dict(r) for r in rows]

    def get_metrics(self) -> dict[str, Any]:
        with self.engine.begin() as conn:
            by_status = conn.execute(select(self.commands.c.status, func.count()).group_by(self.commands.c.status)).all()
            pending = conn.execute(select(func.count()).select_from(self.commands).where(self.commands.c.status.in_(["queued", "delivered"]))).scalar_one()
            snapshots = conn.execute(select(func.count()).select_from(self.decision_snapshots)).scalar_one()
            events = conn.execute(select(func.count()).select_from(self.command_events)).scalar_one()
            active_sets = conn.execute(select(func.count()).select_from(self.active_model_sets).where(self.active_model_sets.c.enabled == 1)).scalar_one()
            state_row = conn.execute(select(self.runtime_state.c.snapshot_json).where(self.runtime_state.c.id == 1)).first()
            push_by_status = conn.execute(
                select(self.feature_push_outbox.c.status, func.count()).group_by(self.feature_push_outbox.c.status)
            ).all()
            push_backlog = conn.execute(
                select(func.count()).select_from(self.feature_push_outbox).where(self.feature_push_outbox.c.status.in_(["queued", "retry", "claimed"]))
            ).scalar_one()
            push_audit = conn.execute(select(func.count()).select_from(self.feature_push_audit)).scalar_one()
            parity_total = conn.execute(select(func.count()).select_from(self.feature_parity_audit)).scalar_one()
            parity_breaches = conn.execute(
                select(func.count()).select_from(self.feature_parity_audit).where(self.feature_parity_audit.c.parity_ok == 0)
            ).scalar_one()
        state = dict(state_row[0] if state_row and isinstance(state_row[0], dict) else {})
        runtime_diag = dict(state.get("runtime_diag") or {})
        rollout_summary = dict(runtime_diag.get("rollout_summary") or dict(runtime_diag.get("risk_cycle_summary") or {}).get("rollout") or {})
        rollout_policy = dict(runtime_diag.get("rollout_policy") or runtime_diag.get("canary_rollout_policy") or {})
        provider_health = dict(runtime_diag.get("provider_health") or {})
        provider_roles = dict(runtime_diag.get("provider_roles") or {})
        portfolio_intelligence = dict(runtime_diag.get("portfolio_intelligence") or {})
        capital_governance = dict(runtime_diag.get("capital_governance") or {})
        return {
            "commands": {str(k): int(v) for k, v in by_status},
            "pending": {"count": int(pending)},
            "decision_pipeline": {
                "snapshots_5m": int(snapshots),
                "stage_attribution": {"pipeline_rows": []},
            },
            "command_events": {"count": int(events)},
            "models": {"active_sets": int(active_sets)},
            "feature_push": {
                "outbox": {str(k): int(v) for k, v in push_by_status},
                "backlog": int(push_backlog),
                "audit_rows": int(push_audit),
            },
            "feature_parity": {
                "total": int(parity_total),
                "breaches": int(parity_breaches),
            },
            "rollout": {
                **rollout_summary,
                "policy": rollout_policy,
            },
            "provider_health": dict(provider_health),
            "provider_roles": dict(provider_roles),
            "portfolio_intelligence": dict(portfolio_intelligence),
            "capital_governance": dict(capital_governance),
        }
