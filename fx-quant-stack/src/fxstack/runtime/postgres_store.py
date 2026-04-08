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

from datetime import datetime
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    delete,
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


def _parse_iso_ts(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip()
    if not txt:
        return 0.0
    try:
        return float(datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0.0


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
            Column("correlation_id", String(192), nullable=True),
            Column("thread_id", String(192), nullable=True),
            Column("idempotency_key", String(128), nullable=True),
            Column("schema_version", String(64), nullable=True),
            Column("orchestration_meta_json", JSON, nullable=True),
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

        self.orchestration_runs = Table(
            "orchestration_runs",
            self.meta,
            Column("run_id", String(64), primary_key=True),
            Column("cycle_id", String(128), nullable=False),
            Column("thread_id", String(192), nullable=False),
            Column("correlation_id", String(192), nullable=False),
            Column("pair", String(16), nullable=False),
            Column("ts_utc", Float, nullable=False),
            Column("runtime_mode", String(16), nullable=False),
            Column("latency_ms", Integer, nullable=False),
            Column("fallback_used", Integer, nullable=False, default=0),
            Column("version_bundle_json", JSON, nullable=False),
            Column("packet_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_orchestration_runs_pair", self.orchestration_runs.c.pair)
        Index("ix_orchestration_runs_cycle_id", self.orchestration_runs.c.cycle_id)
        Index("ix_orchestration_runs_thread_id", self.orchestration_runs.c.thread_id)
        Index("ix_orchestration_runs_ts_utc", self.orchestration_runs.c.ts_utc)
        Index("ix_orchestration_runs_runtime_mode", self.orchestration_runs.c.runtime_mode)
        Index("ix_orchestration_runs_correlation_id", self.orchestration_runs.c.correlation_id, unique=True)

        self.agent_proposals = Table(
            "agent_proposals",
            self.meta,
            Column("proposal_id", String(64), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("agent_id", String(128), nullable=False),
            Column("phase", String(64), nullable=False),
            Column("intent", String(32), nullable=False),
            Column("side", String(16), nullable=False),
            Column("confidence", Float, nullable=False),
            Column("expected_edge_bps", Float, nullable=False),
            Column("uncertainty", Float, nullable=False),
            Column("risk_cost", Float, nullable=False),
            Column("ttl_ms", Integer, nullable=False),
            Column("evidence_json", JSON, nullable=False),
            Column("constraints_json", JSON, nullable=False),
            Column("advisory_only", Integer, nullable=False, default=1),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_agent_proposals_run_id", self.agent_proposals.c.run_id)
        Index("ix_agent_proposals_agent_id", self.agent_proposals.c.agent_id)
        Index("ix_agent_proposals_phase", self.agent_proposals.c.phase)

        self.governed_decisions = Table(
            "governed_decisions",
            self.meta,
            Column("decision_id", String(64), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("runtime_mode", String(16), nullable=False, default="shadow"),
            Column("allowed", Integer, nullable=False),
            Column("selected_action", String(64), nullable=False),
            Column("command_preview_json", JSON, nullable=True),
            Column("blocking_reasons_json", JSON, nullable=False),
            Column("approval_state", String(32), nullable=False),
            Column("governor_version", String(128), nullable=False),
            Column("version_bundle_json", JSON, nullable=True),
            Column("invariants_ok", Integer, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_governed_decisions_run_id", self.governed_decisions.c.run_id, unique=True)
        Index("ix_governed_decisions_runtime_mode", self.governed_decisions.c.runtime_mode)

        self.agent_traces = Table(
            "agent_traces",
            self.meta,
            Column("trace_id", String(128), primary_key=True),
            Column("run_id", String(64), nullable=False),
            Column("pair", String(16), nullable=True),
            Column("trace_json", JSON, nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_agent_traces_run_id", self.agent_traces.c.run_id)
        Index("ix_agent_traces_created_at", self.agent_traces.c.created_at)

        self.approval_events = Table(
            "approval_events",
            self.meta,
            Column("event_id", String(64), primary_key=True),
            Column("subject_type", String(64), nullable=False),
            Column("subject_id", String(128), nullable=False),
            Column("approver", String(128), nullable=False),
            Column("decision", String(32), nullable=False),
            Column("reason", Text, nullable=True),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_approval_events_subject_type", self.approval_events.c.subject_type)
        Index("ix_approval_events_subject_id", self.approval_events.c.subject_id)
        Index("ix_approval_events_created_at", self.approval_events.c.created_at)

        self.experiment_proposals = Table(
            "experiment_proposals",
            self.meta,
            Column("experiment_id", String(64), primary_key=True),
            Column("source_run_id", String(64), nullable=True),
            Column("hypothesis", Text, nullable=False),
            Column("change_set_json", JSON, nullable=False),
            Column("evaluation_plan_json", JSON, nullable=False),
            Column("risk_notes_json", JSON, nullable=False),
            Column("evidence_refs_json", JSON, nullable=False),
            Column("prompt_hash", String(128), nullable=False, default=""),
            Column("tool_trace_hash", String(128), nullable=False, default=""),
            Column("model_id", String(128), nullable=False, default=""),
            Column("decision_seed", Integer, nullable=False, default=0),
            Column("input_artefact_refs_json", JSON, nullable=False),
            Column("config_diff_json", JSON, nullable=False),
            Column("replay_window", String(128), nullable=False, default=""),
            Column("artifact_root", Text, nullable=False, default=""),
            Column("latest_stage", String(64), nullable=False, default=""),
            Column("latest_promotion_id", String(64), nullable=False, default=""),
            Column("approval_status", String(32), nullable=False),
            Column("created_at", Float, nullable=False),
        )
        Index("ix_experiment_proposals_approval_status", self.experiment_proposals.c.approval_status)
        Index("ix_experiment_proposals_created_at", self.experiment_proposals.c.created_at)
        Index("ix_experiment_proposals_source_run_id", self.experiment_proposals.c.source_run_id)

        self.experiment_promotions = Table(
            "experiment_promotions",
            self.meta,
            Column("promotion_id", String(64), primary_key=True),
            Column("experiment_id", String(64), nullable=False),
            Column("prompt_hash", String(128), nullable=False, default=""),
            Column("tool_trace_hash", String(128), nullable=False, default=""),
            Column("model_id", String(128), nullable=False, default=""),
            Column("config_diff_json", JSON, nullable=False),
            Column("replay_window", String(128), nullable=False, default=""),
            Column("replay_results_json", JSON, nullable=False),
            Column("approval_records_json", JSON, nullable=False),
            Column("paper_results_json", JSON, nullable=False),
            Column("canary_results_json", JSON, nullable=False),
            Column("release_manifest_ref", Text, nullable=False, default=""),
            Column("rollback_metadata_json", JSON, nullable=False),
            Column("artefact_hashes_json", JSON, nullable=False),
            Column("status", String(32), nullable=False),
            Column("created_at", Float, nullable=False),
            Column("updated_at", Float, nullable=False),
        )
        Index("ix_experiment_promotions_experiment_id", self.experiment_promotions.c.experiment_id)
        Index("ix_experiment_promotions_status", self.experiment_promotions.c.status)
        Index("ix_experiment_promotions_created_at", self.experiment_promotions.c.created_at)

        self.experiment_lineage = Table(
            "experiment_lineage",
            self.meta,
            Column("experiment_id", String(64), primary_key=True),
            Column("proposal_ref", Text, nullable=False, default=""),
            Column("review_ref", Text, nullable=False, default=""),
            Column("replay_refs_json", JSON, nullable=False),
            Column("paper_pack_ref", Text, nullable=False, default=""),
            Column("canary_pack_ref", Text, nullable=False, default=""),
            Column("promotion_decision_ref", Text, nullable=False, default=""),
            Column("rollback_plan_ref", Text, nullable=False, default=""),
            Column("release_manifest_ref", Text, nullable=False, default=""),
            Column("reflection_memory_ref", Text, nullable=False, default=""),
            Column("latest_stage", String(64), nullable=False, default=""),
            Column("latest_promotion_id", String(64), nullable=False, default=""),
            Column("approval_status", String(32), nullable=False, default=""),
            Column("evidence_refs_json", JSON, nullable=False),
            Column("promotion_ids_json", JSON, nullable=False),
            Column("approval_event_ids_json", JSON, nullable=False),
            Column("updated_at", Float, nullable=False),
        )
        Index("ix_experiment_lineage_latest_stage", self.experiment_lineage.c.latest_stage)
        Index("ix_experiment_lineage_updated_at", self.experiment_lineage.c.updated_at)

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
            "orchestration_runs",
            "agent_proposals",
            "governed_decisions",
            "agent_traces",
            "approval_events",
            "experiment_proposals",
            "experiment_promotions",
            "experiment_lineage",
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

    def purge_pending_commands(
        self,
        *,
        reason: str,
        intents: set[str] | None = None,
        include_delivered: bool = True,
    ) -> int:
        now = _now()
        normalized_reason = str(reason or "runtime_restart_purged").strip() or "runtime_restart_purged"
        normalized_intents = {str(item or "").strip().upper() for item in (intents or set()) if str(item or "").strip()}
        updated = 0
        with self._lock:
            with self.engine.begin() as conn:
                purge_statuses = ["queued", "delivered"] if include_delivered else ["queued"]
                stmt = select(self.commands).where(self.commands.c.status.in_(purge_statuses))
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

    def record_approval_event(
        self,
        *,
        subject_type: str,
        subject_id: str,
        approver: str,
        decision: str,
        reason: str = "",
        event_id: str | None = None,
        created_at: float | None = None,
    ) -> dict[str, Any]:
        row = {
            "event_id": str(event_id or uuid4()),
            "subject_type": str(subject_type or ""),
            "subject_id": str(subject_id or ""),
            "approver": str(approver or ""),
            "decision": str(decision or ""),
            "reason": str(reason or ""),
            "created_at": _parse_iso_ts(created_at) if created_at is not None else _now(),
        }
        if not row["subject_type"] or not row["subject_id"] or not row["approver"] or not row["decision"]:
            raise ValueError("subject_type, subject_id, approver, and decision are required")
        with self.engine.begin() as conn:
            conn.execute(self.approval_events.insert().values(**row))
        return dict(row)

    def upsert_experiment_proposal(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload or {})
        experiment_id = str(row.get("experiment_id") or "").strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        created_at = _parse_iso_ts(row.get("created_at")) or _now()
        stored = {
            "experiment_id": experiment_id,
            "source_run_id": str(row.get("source_run_id") or "") or None,
            "hypothesis": str(row.get("hypothesis") or ""),
            "change_set_json": list(row.get("change_set") or []),
            "evaluation_plan_json": dict(row.get("evaluation_plan") or {}),
            "risk_notes_json": list(row.get("risk_notes") or []),
            "evidence_refs_json": list(row.get("evidence_refs") or []),
            "prompt_hash": str(row.get("prompt_hash") or ""),
            "tool_trace_hash": str(row.get("tool_trace_hash") or ""),
            "model_id": str(row.get("model_id") or ""),
            "decision_seed": int(row.get("decision_seed") or 0),
            "input_artefact_refs_json": list(row.get("input_artefact_refs") or []),
            "config_diff_json": dict(row.get("config_diff") or {}),
            "replay_window": str(row.get("replay_window") or ""),
            "artifact_root": str(row.get("artifact_root") or ""),
            "latest_stage": str(row.get("latest_stage") or ""),
            "latest_promotion_id": str(row.get("latest_promotion_id") or ""),
            "approval_status": str(row.get("approval_status") or "draft"),
            "created_at": created_at,
        }
        with self.engine.begin() as conn:
            conn.execute(delete(self.experiment_proposals).where(self.experiment_proposals.c.experiment_id == experiment_id))
            conn.execute(self.experiment_proposals.insert().values(**stored))
        return self.get_experiment_proposal(experiment_id) or {}

    def get_experiment_proposal(self, experiment_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.experiment_proposals).where(self.experiment_proposals.c.experiment_id == str(experiment_id))
            ).mappings().first()
        return self._normalize_experiment_proposal_row(row) if row else None

    def get_experiment_proposals(
        self,
        *,
        limit: int = 200,
        approval_status: str = "",
        source_run_id: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.experiment_proposals)
        if str(approval_status).strip():
            stmt = stmt.where(self.experiment_proposals.c.approval_status == str(approval_status))
        if str(source_run_id).strip():
            stmt = stmt.where(self.experiment_proposals.c.source_run_id == str(source_run_id))
        stmt = stmt.order_by(self.experiment_proposals.c.created_at.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_experiment_proposal_row(row) for row in rows]

    def upsert_experiment_promotion(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload or {})
        promotion_id = str(row.get("promotion_id") or "").strip()
        experiment_id = str(row.get("experiment_id") or "").strip()
        if not promotion_id or not experiment_id:
            raise ValueError("promotion_id and experiment_id are required")
        created_at = _parse_iso_ts(row.get("created_at")) or _now()
        updated_at = _parse_iso_ts(row.get("updated_at")) or created_at
        stored = {
            "promotion_id": promotion_id,
            "experiment_id": experiment_id,
            "prompt_hash": str(row.get("prompt_hash") or ""),
            "tool_trace_hash": str(row.get("tool_trace_hash") or ""),
            "model_id": str(row.get("model_id") or ""),
            "config_diff_json": dict(row.get("config_diff") or {}),
            "replay_window": str(row.get("replay_window") or ""),
            "replay_results_json": dict(row.get("replay_results") or {}),
            "approval_records_json": list(row.get("approval_records") or []),
            "paper_results_json": dict(row.get("paper_results") or {}),
            "canary_results_json": dict(row.get("canary_results") or {}),
            "release_manifest_ref": str(row.get("release_manifest_ref") or ""),
            "rollback_metadata_json": dict(row.get("rollback_metadata") or {}),
            "artefact_hashes_json": {str(key): str(value) for key, value in dict(row.get("artefact_hashes") or {}).items()},
            "status": str(row.get("status") or ""),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        with self.engine.begin() as conn:
            conn.execute(delete(self.experiment_promotions).where(self.experiment_promotions.c.promotion_id == promotion_id))
            conn.execute(self.experiment_promotions.insert().values(**stored))
        return self.get_experiment_promotion(promotion_id) or {}

    def get_experiment_promotion(self, promotion_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.experiment_promotions).where(self.experiment_promotions.c.promotion_id == str(promotion_id))
            ).mappings().first()
        return self._normalize_experiment_promotion_row(row) if row else None

    def get_experiment_promotions(
        self,
        *,
        limit: int = 200,
        experiment_id: str = "",
        status: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.experiment_promotions)
        if str(experiment_id).strip():
            stmt = stmt.where(self.experiment_promotions.c.experiment_id == str(experiment_id))
        if str(status).strip():
            stmt = stmt.where(self.experiment_promotions.c.status == str(status))
        stmt = stmt.order_by(self.experiment_promotions.c.created_at.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_experiment_promotion_row(row) for row in rows]

    def get_approval_events(
        self,
        *,
        limit: int = 200,
        subject_type: str = "",
        subject_id: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.approval_events)
        if str(subject_type).strip():
            stmt = stmt.where(self.approval_events.c.subject_type == str(subject_type))
        if str(subject_id).strip():
            stmt = stmt.where(self.approval_events.c.subject_id == str(subject_id))
        stmt = stmt.order_by(self.approval_events.c.created_at.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(row) for row in rows]

    def upsert_experiment_lineage(self, payload: dict[str, Any]) -> dict[str, Any]:
        row = dict(payload or {})
        experiment_id = str(row.get("experiment_id") or "").strip()
        if not experiment_id:
            raise ValueError("experiment_id is required")
        updated_at = _parse_iso_ts(row.get("updated_at")) or _now()
        stored = {
            "experiment_id": experiment_id,
            "proposal_ref": str(row.get("proposal_ref") or ""),
            "review_ref": str(row.get("review_ref") or ""),
            "replay_refs_json": list(row.get("replay_refs") or []),
            "paper_pack_ref": str(row.get("paper_pack_ref") or ""),
            "canary_pack_ref": str(row.get("canary_pack_ref") or ""),
            "promotion_decision_ref": str(row.get("promotion_decision_ref") or ""),
            "rollback_plan_ref": str(row.get("rollback_plan_ref") or ""),
            "release_manifest_ref": str(row.get("release_manifest_ref") or ""),
            "reflection_memory_ref": str(row.get("reflection_memory_ref") or ""),
            "latest_stage": str(row.get("latest_stage") or ""),
            "latest_promotion_id": str(row.get("latest_promotion_id") or ""),
            "approval_status": str(row.get("approval_status") or ""),
            "evidence_refs_json": list(row.get("evidence_refs") or []),
            "promotion_ids_json": list(row.get("promotion_ids") or []),
            "approval_event_ids_json": list(row.get("approval_event_ids") or []),
            "updated_at": updated_at,
        }
        with self.engine.begin() as conn:
            conn.execute(delete(self.experiment_lineage).where(self.experiment_lineage.c.experiment_id == experiment_id))
            conn.execute(self.experiment_lineage.insert().values(**stored))
        return self.get_experiment_lineage(experiment_id) or {}

    def get_experiment_lineage(self, experiment_id: str) -> dict[str, Any] | None:
        with self.engine.begin() as conn:
            row = conn.execute(
                select(self.experiment_lineage).where(self.experiment_lineage.c.experiment_id == str(experiment_id))
            ).mappings().first()
        return self._normalize_experiment_lineage_row(row) if row else None

    def get_experiment_lineages(
        self,
        *,
        limit: int = 200,
        latest_stage: str = "",
        approval_status: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.experiment_lineage)
        if str(latest_stage).strip():
            stmt = stmt.where(self.experiment_lineage.c.latest_stage == str(latest_stage))
        if str(approval_status).strip():
            stmt = stmt.where(self.experiment_lineage.c.approval_status == str(approval_status))
        stmt = stmt.order_by(self.experiment_lineage.c.updated_at.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._normalize_experiment_lineage_row(row) for row in rows]

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
        direct_claimable = {item for item in allowed if item != "claimed"}
        settings = get_settings()
        now = _now()
        claim_timeout_secs = float(max(30.0, float(getattr(settings, "feature_push_claim_timeout_secs", 120.0) or 120.0)))
        reclaim_before = float(now - claim_timeout_secs)
        claimed: list[dict[str, Any]] = []
        with self._lock:
            with self.engine.begin() as conn:
                stmt = select(self.feature_push_outbox).where(
                    or_(
                        self.feature_push_outbox.c.status.in_(sorted(direct_claimable)),
                        and_(
                            self.feature_push_outbox.c.status == "claimed",
                            self.feature_push_outbox.c.claimed_at.is_not(None),
                            self.feature_push_outbox.c.claimed_at <= reclaim_before,
                        ),
                        and_(
                            self.feature_push_outbox.c.status == "claimed",
                            self.feature_push_outbox.c.claimed_at.is_(None),
                            self.feature_push_outbox.c.updated_at <= reclaim_before,
                        ),
                    )
                )
                stmt = stmt.order_by(self.feature_push_outbox.c.created_at.asc()).limit(max(1, min(limit, 500)))
                rows = conn.execute(stmt).mappings().all()
                for row in rows:
                    outbox_key = str(row.get("outbox_key") or "")
                    if not outbox_key:
                        continue
                    row_status = str(row.get("status") or "").strip().lower()
                    row_claimed_at = row.get("claimed_at")
                    row_updated_at = row.get("updated_at")
                    claim_stmt = (
                        update(self.feature_push_outbox)
                        .where(
                            self.feature_push_outbox.c.outbox_key == outbox_key,
                            self.feature_push_outbox.c.status == row_status,
                        )
                    )
                    if row_claimed_at is None:
                        claim_stmt = claim_stmt.where(
                            self.feature_push_outbox.c.claimed_at.is_(None),
                            self.feature_push_outbox.c.updated_at == float(row_updated_at or 0.0),
                        )
                    else:
                        claim_stmt = claim_stmt.where(self.feature_push_outbox.c.claimed_at == float(row_claimed_at))
                    result = conn.execute(
                        claim_stmt.values(
                            status="claimed",
                            claimed_by=worker,
                            claimed_at=now,
                            updated_at=now,
                            attempt_count=int(row.get("attempt_count") or 0) + 1,
                        )
                    )
                    if int(getattr(result, "rowcount", 0) or 0) <= 0:
                        continue
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
        now = _now()
        with self._lock:
            with self.engine.begin() as conn:
                existing = conn.execute(select(self.commands.c.status).where(self.commands.c.command_id == cmd.command_id)).fetchone()
                if existing is not None:
                    return False, str(existing[0])
                if str(cmd.idempotency_key or "").strip():
                    existing = conn.execute(
                        select(self.commands.c.command_id, self.commands.c.status)
                        .where(
                            and_(
                                self.commands.c.idempotency_key == str(cmd.idempotency_key),
                                self.commands.c.expires_at >= now,
                            )
                        )
                        .order_by(self.commands.c.created_at.desc())
                        .limit(1)
                    ).fetchone()
                    if existing is not None:
                        return False, str(existing[1])

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
                        correlation_id=cmd.correlation_id,
                        thread_id=cmd.thread_id,
                        idempotency_key=cmd.idempotency_key,
                        schema_version=cmd.schema_version,
                        orchestration_meta_json=dict(cmd.orchestration_meta_json or {}),
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

    def get_active_command_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        now = _now()
        with self._lock:
            with self.engine.begin() as conn:
                row = conn.execute(
                    select(self.commands)
                    .where(
                        and_(
                            self.commands.c.idempotency_key == key,
                            self.commands.c.expires_at >= now,
                        )
                    )
                    .order_by(self.commands.c.created_at.desc())
                    .limit(1)
                ).mappings().first()
                if row is None:
                    return None
                return dict(row)

    def get_latest_tick(self, symbol: str) -> dict[str, Any] | None:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        stmt = (
            select(self.market_ticks)
            .where(self.market_ticks.c.symbol == sym)
            .order_by(self.market_ticks.c.ts.desc())
            .limit(1)
        )
        with self.engine.begin() as conn:
            row = conn.execute(stmt).mappings().first()
        return dict(row) if row else None

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
                    correlation_id=str(row.get("correlation_id") or ""),
                    thread_id=str(row.get("thread_id") or ""),
                    idempotency_key=str(row.get("idempotency_key") or ""),
                    schema_version=str(row.get("schema_version") or ""),
                    orchestration_meta_json=dict(row.get("orchestration_meta_json") or {}),
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
        command_id = str(ack.command_id or "").strip()
        idempotency_key = str(ack.idempotency_key or "").strip()
        if not command_id and not idempotency_key:
            return {"status": "error", "reason": "missing_command_id"}, 400

        status = str(ack.status).lower().strip()
        if status not in {"delivered", "acked", "failed", "duplicate"}:
            status = "failed"

        state_patch: dict[str, Any] | None = None
        with self._lock:
            with self.engine.begin() as conn:
                row = None
                if command_id:
                    row = conn.execute(select(self.commands).where(self.commands.c.command_id == command_id)).mappings().first()
                if row is None and idempotency_key:
                    row = conn.execute(
                        select(self.commands)
                        .where(
                            and_(
                                self.commands.c.idempotency_key == idempotency_key,
                                self.commands.c.expires_at >= _now(),
                            )
                        )
                        .order_by(self.commands.c.created_at.desc())
                        .limit(1)
                    ).mappings().first()
                if row is None:
                    out = {"status": "not_found"}
                    if command_id:
                        out["command_id"] = command_id
                    if idempotency_key:
                        out["idempotency_key"] = idempotency_key
                    return out, 404

                command_id = str(row["command_id"])
                cur = str(row["status"])
                if cur in {"acked", "failed", "expired", "duplicate"}:
                    return {"status": cur, "command_id": command_id, "idempotent": True}, 200

                delivered_before = int(row.get("delivered_count", 0) or 0) > 0
                can_finalize = cur == "delivered" or (cur == "queued" and delivered_before)
                if status in {"acked", "failed", "duplicate"} and not can_finalize:
                    return {
                        "status": "invalid_transition",
                        "command_id": command_id,
                        "current": cur,
                        "requested": status,
                        "allowed": ["delivered"] if cur == "queued" else ["delivered", "acked", "failed"],
                    }, 409

                conn.execute(
                    update(self.commands)
                    .where(self.commands.c.command_id == command_id)
                    .values(
                        status=status,
                        updated_at=ack.updated_at,
                        ack_json=ack.to_dict(),
                        reason=ack.message,
                        delivered_count=(
                            int(row.get("delivered_count", 0) or 0) + 1
                            if status == "delivered"
                            else int(row.get("delivered_count", 0) or 0)
                        ),
                    )
                )
                self._append_command_event(
                    command_id=command_id,
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
            out = {"status": status, "command_id": command_id}
            if idempotency_key and not command_id == str(ack.command_id or "").strip():
                out["idempotency_key"] = idempotency_key
            return out, 200

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

    def store_orchestration_bundle(
        self,
        *,
        context: dict[str, Any],
        packet: dict[str, Any],
        trace: dict[str, Any],
        runtime_mode: str,
        fallback_used: bool,
    ) -> None:
        context_json = dict(context or {})
        packet_json = dict(packet or {})
        trace_json = dict(trace or {})
        packet_fallback_used = bool(packet_json.get("fallback_used", False) or fallback_used)
        packet_json["fallback_used"] = bool(packet_fallback_used)
        governed = dict(packet_json.get("governed_decision") or {})
        proposals = list(packet_json.get("proposals") or [])
        run_id = str(packet_json.get("run_id") or "")
        if not run_id:
            raise ValueError("packet run_id is required")
        version_bundle = dict(context_json.get("version_bundle") or {})
        trace_run_id = str(trace_json.get("run_id") or run_id)
        ts_utc = context_json.get("ts_utc") or packet_json.get("ts_utc")
        ts_value = _parse_iso_ts(ts_utc)
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(delete(self.agent_proposals).where(self.agent_proposals.c.run_id == run_id))
            conn.execute(delete(self.agent_traces).where(self.agent_traces.c.run_id == run_id))
            conn.execute(delete(self.governed_decisions).where(self.governed_decisions.c.run_id == run_id))
            conn.execute(delete(self.orchestration_runs).where(self.orchestration_runs.c.run_id == run_id))

            conn.execute(
                self.orchestration_runs.insert().values(
                    run_id=run_id,
                    cycle_id=str(context_json.get("cycle_id") or ""),
                    thread_id=str(context_json.get("thread_id") or ""),
                    correlation_id=str(context_json.get("correlation_id") or ""),
                    pair=str(context_json.get("pair") or packet_json.get("pair") or ""),
                    ts_utc=float(ts_value if ts_value > 0 else now),
                    runtime_mode=str(runtime_mode),
                    latency_ms=int(packet_json.get("latency_ms") or 0),
                    fallback_used=1 if bool(packet_fallback_used) else 0,
                    version_bundle_json=version_bundle,
                    packet_json=packet_json,
                    created_at=now,
                )
            )
            if governed:
                conn.execute(
                    self.governed_decisions.insert().values(
                        decision_id=str(governed.get("decision_id") or ""),
                        run_id=run_id,
                        runtime_mode=str(runtime_mode),
                        allowed=1 if bool(governed.get("allowed", False)) else 0,
                        selected_action=str(governed.get("selected_action") or ""),
                        command_preview_json=dict(governed.get("command_preview") or {}) or None,
                        blocking_reasons_json=list(governed.get("blocking_reasons") or []),
                        approval_state=str(governed.get("approval_state") or "auto"),
                        governor_version=str(governed.get("governor_version") or ""),
                        version_bundle_json=version_bundle or None,
                        invariants_ok=1 if bool(governed.get("invariants_ok", False)) else 0,
                        created_at=now,
                    )
                )
            for proposal in proposals:
                proposal_json = dict(proposal or {})
                conn.execute(
                    self.agent_proposals.insert().values(
                        proposal_id=str(proposal_json.get("proposal_id") or ""),
                        run_id=run_id,
                        agent_id=str(proposal_json.get("agent_id") or ""),
                        phase=str(proposal_json.get("phase") or ""),
                        intent=str(proposal_json.get("intent") or ""),
                        side=str(proposal_json.get("side") or ""),
                        confidence=float(proposal_json.get("confidence") or 0.0),
                        expected_edge_bps=float(proposal_json.get("expected_edge_bps") or 0.0),
                        uncertainty=float(proposal_json.get("uncertainty") or 0.0),
                        risk_cost=float(proposal_json.get("risk_cost") or 0.0),
                        ttl_ms=int(proposal_json.get("ttl_ms") or 0),
                        evidence_json=list(proposal_json.get("evidence_refs") or []),
                        constraints_json=dict(proposal_json.get("constraints") or {}),
                        advisory_only=1 if bool(proposal_json.get("advisory_only", True)) else 0,
                        created_at=now,
                    )
                )
            conn.execute(
                self.agent_traces.insert().values(
                    trace_id=str(trace_json.get("trace_id") or ""),
                    run_id=trace_run_id,
                    pair=str(context_json.get("pair") or packet_json.get("pair") or ""),
                    trace_json=trace_json,
                    created_at=now,
                )
            )

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

    def _normalize_experiment_proposal_row(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        return {
            "experiment_id": str(data.get("experiment_id") or ""),
            "source_run_id": str(data.get("source_run_id") or "") or None,
            "hypothesis": str(data.get("hypothesis") or ""),
            "change_set": list(data.get("change_set_json") or []),
            "evaluation_plan": dict(data.get("evaluation_plan_json") or {}),
            "risk_notes": list(data.get("risk_notes_json") or []),
            "evidence_refs": list(data.get("evidence_refs_json") or []),
            "prompt_hash": str(data.get("prompt_hash") or ""),
            "tool_trace_hash": str(data.get("tool_trace_hash") or ""),
            "model_id": str(data.get("model_id") or ""),
            "decision_seed": int(data.get("decision_seed") or 0),
            "input_artefact_refs": list(data.get("input_artefact_refs_json") or []),
            "config_diff": dict(data.get("config_diff_json") or {}),
            "replay_window": str(data.get("replay_window") or ""),
            "artifact_root": str(data.get("artifact_root") or ""),
            "latest_stage": str(data.get("latest_stage") or ""),
            "latest_promotion_id": str(data.get("latest_promotion_id") or ""),
            "approval_status": str(data.get("approval_status") or ""),
            "created_at": float(data.get("created_at") or 0.0),
        }

    def _normalize_experiment_promotion_row(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        return {
            "promotion_id": str(data.get("promotion_id") or ""),
            "experiment_id": str(data.get("experiment_id") or ""),
            "prompt_hash": str(data.get("prompt_hash") or ""),
            "tool_trace_hash": str(data.get("tool_trace_hash") or ""),
            "model_id": str(data.get("model_id") or ""),
            "config_diff": dict(data.get("config_diff_json") or {}),
            "replay_window": str(data.get("replay_window") or ""),
            "replay_results": dict(data.get("replay_results_json") or {}),
            "approval_records": list(data.get("approval_records_json") or []),
            "paper_results": dict(data.get("paper_results_json") or {}),
            "canary_results": dict(data.get("canary_results_json") or {}),
            "release_manifest_ref": str(data.get("release_manifest_ref") or ""),
            "rollback_metadata": dict(data.get("rollback_metadata_json") or {}),
            "artefact_hashes": {str(key): str(value) for key, value in dict(data.get("artefact_hashes_json") or {}).items()},
            "status": str(data.get("status") or ""),
            "created_at": float(data.get("created_at") or 0.0),
            "updated_at": float(data.get("updated_at") or 0.0),
        }

    def _normalize_experiment_lineage_row(self, row: Any) -> dict[str, Any]:
        data = dict(row or {})
        return {
            "experiment_id": str(data.get("experiment_id") or ""),
            "proposal_ref": str(data.get("proposal_ref") or ""),
            "review_ref": str(data.get("review_ref") or ""),
            "replay_refs": list(data.get("replay_refs_json") or []),
            "paper_pack_ref": str(data.get("paper_pack_ref") or ""),
            "canary_pack_ref": str(data.get("canary_pack_ref") or ""),
            "promotion_decision_ref": str(data.get("promotion_decision_ref") or ""),
            "rollback_plan_ref": str(data.get("rollback_plan_ref") or ""),
            "release_manifest_ref": str(data.get("release_manifest_ref") or ""),
            "reflection_memory_ref": str(data.get("reflection_memory_ref") or ""),
            "latest_stage": str(data.get("latest_stage") or ""),
            "latest_promotion_id": str(data.get("latest_promotion_id") or ""),
            "approval_status": str(data.get("approval_status") or ""),
            "evidence_refs": list(data.get("evidence_refs_json") or []),
            "promotion_ids": list(data.get("promotion_ids_json") or []),
            "approval_event_ids": list(data.get("approval_event_ids_json") or []),
            "updated_at": float(data.get("updated_at") or 0.0),
        }

    def get_orchestration_runs(
        self,
        *,
        limit: int = 200,
        pair: str = "",
        runtime_mode: str = "",
        cycle_id: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.orchestration_runs)
        if str(pair).strip():
            stmt = stmt.where(self.orchestration_runs.c.pair == str(pair).upper())
        if str(runtime_mode).strip():
            stmt = stmt.where(self.orchestration_runs.c.runtime_mode == str(runtime_mode))
        if str(cycle_id).strip():
            stmt = stmt.where(self.orchestration_runs.c.cycle_id == str(cycle_id))
        stmt = stmt.order_by(self.orchestration_runs.c.created_at.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def get_orchestration_traces(
        self,
        *,
        limit: int = 200,
        run_id: str = "",
        pair: str = "",
    ) -> list[dict[str, Any]]:
        stmt = select(self.agent_traces)
        if str(run_id).strip():
            stmt = stmt.where(self.agent_traces.c.run_id == str(run_id))
        if str(pair).strip():
            stmt = stmt.where(self.agent_traces.c.pair == str(pair).upper())
        stmt = stmt.order_by(self.agent_traces.c.created_at.desc()).limit(max(1, min(limit, 5000)))
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
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
