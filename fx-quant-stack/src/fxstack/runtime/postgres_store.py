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
import math
import threading
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

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

from fxstack.runtime.db_tools import load_migration_heads
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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


_DISABLED_SCALP_ENTRY_REASON = (
    "execution_egress_scalp_live_ingress_disabled_unvalidated_authority"
)


def _is_identifiable_scalp_entry(
    *,
    cmd: Any,
    command_id: Any,
    intent: Any,
    payload: Any,
) -> bool:
    """Identify the retired standalone-scalp BUY/SELL ingress fail closed.

    The authoritative client stamped both ``scalp:<...>`` command IDs and the
    ``scalp_live_entry`` intent, while older iterations also carried
    ``scalp_*`` certificate/config fields.  Restrict the fence to
    exposure-increasing verbs so a protective command can never be withheld
    merely because it retains scalp provenance.
    """

    if str(cmd or "").strip().upper() not in {"BUY", "SELL"}:
        return False
    normalized_intent = str(intent or "").strip().lower()
    normalized_command_id = str(command_id or "").strip().lower()
    raw_payload = dict(payload) if isinstance(payload, dict) else {}
    payload_intent = str(raw_payload.get("intent") or "").strip().lower()
    return bool(
        normalized_intent == "scalp_live_entry"
        or payload_intent == "scalp_live_entry"
        or normalized_command_id.startswith("scalp:")
        or any(
            str(key or "").strip().lower().startswith("scalp_")
            for key in raw_payload
        )
    )


def _timestamp_age_secs(
    value: Any,
    *,
    now_ts: float,
    max_future_skew_secs: float = 5.0,
) -> float | None:
    parsed = _parse_iso_ts(value)
    now = float(now_ts)
    if (
        not math.isfinite(parsed)
        or parsed <= 0.0
        or not math.isfinite(now)
    ):
        return None
    age = now - parsed
    if age < -max(0.0, float(max_future_skew_secs)):
        return None
    return max(0.0, float(age))


# These fields are written by operator/release transactions and must never be
# rolled back by a runtime cycle that started from an older state snapshot.
_ORCHESTRATION_LIVE_AUTHORITY_FIELDS = (
    "authority_revision",
    "enabled",
    "mode",
    "runtime_enabled",
    "queue_kill_active",
    "queue_kill_reason",
    "queue_killed_at",
    "active_pair_scope",
    "active_sleeve_scope",
    "active_intent_scope",
    "active_pair_scope_configured",
    "active_sleeve_scope_configured",
    "active_intent_scope_configured",
    "ramp_steps_pct",
    "current_stage_index",
    "current_stage_pct",
    "budget_scale",
    "promotion_pack_path",
    "signoff_records",
    "release_status",
    "bundle_run_id",
    "last_kill_reason",
    "last_kill_at",
    "purged_command_count",
)

_ORCHESTRATION_LIVE_REVISION_TRIGGER_FIELDS = tuple(
    field
    for field in _ORCHESTRATION_LIVE_AUTHORITY_FIELDS
    if field != "authority_revision"
)

_ORCHESTRATION_LIVE_STAGE_IDENTITY_FIELDS = (
    "current_stage_index",
    "current_stage_pct",
    "bundle_run_id",
)

_ORCHESTRATION_LIVE_ENTRY_EVIDENCE_FIELDS = (
    "entry_ratio_vs_baseline",
    "entry_ratio_evaluable",
    "entry_ratio_status",
    "entry_ratio_approved_count",
    "entry_ratio_submitted_count",
    "entry_ratio_accepted_count",
    "entry_ratio_observed_at",
    "entry_ratio_stage_index",
    "entry_ratio_stage_pct",
    "entry_evidence_by_pair",
)

_EXECUTION_EGRESS_SCHEMA = "fxstack_execution_egress_authority_v1"
_RELEASE_AUTHORITY_STATE_SCHEMA = "fxstack_live_release_authority_state_v1"
_RELEASE_AUTHORITY_REQUEST_SCHEMA = "fxstack_live_release_authority_request_v1"
_RELEASE_AUTHORITY_ACK_SCHEMA = "fxstack_live_release_authority_ack_v1"
_EXTERNAL_RELEASE_WITNESS_SCHEMA = "fxstack_external_release_witness_v1"
_EXECUTION_EGRESS_RUNNER_LEASE_SECS = 30.0
_RELEASE_EGRESS_IDENTITY_FIELDS = (
    "source_sha256",
    "package_merkle_sha256",
    "config_sha256",
    "manifest_file_sha256",
    "model_identity_sha256",
    "artifact_set_sha256",
    "model_set_id",
)


def _selected_state_fields(payload: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: payload.get(field) for field in fields}


def _preserve_selected_state_fields(
    *,
    incoming: dict[str, Any],
    current: dict[str, Any],
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if field in current:
            incoming[field] = current[field]
        else:
            incoming.pop(field, None)


class PostgresRuntimeStore:
    # Transaction-scoped PostgreSQL advisory lock shared by every API/runtime
    # process that can change command state. The per-instance RLock only
    # protects threads in one process and cannot make the reconciliation fence
    # atomic across multiple workers.
    _EXECUTION_QUEUE_ADVISORY_LOCK_KEY = 5068883552507806257

    def __init__(
        self,
        database_url: str,
        *,
        requeue_age_secs: float = 90.0,
        connect_retries: int = 5,
    ) -> None:
        self._migration_root, self._expected_migration_heads = load_migration_heads()
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
        migration = dict(check.get("migration") or {})
        if missing and allow_create_all and not str(migration.get("error") or ""):
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
                + "Run `python -I -m fxstack.runtime.db_tools migrate` with "
                + "FXSTACK_PROJECT_ROOT set before starting runtime/bridge."
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
        expected_heads = list(getattr(self, "_expected_migration_heads", []) or [])
        migration_error = ""
        if not expected_heads:
            try:
                _, expected_heads = load_migration_heads()
            except Exception as exc:
                migration_error = f"{type(exc).__name__}: {exc}"
                missing = sorted(required)
                return {
                    "required": missing,
                    "present": [],
                    "missing": missing,
                    "missing_tables": missing,
                    "migration": {
                        "ok": False,
                        "expected_heads": [],
                        "current_revisions": [],
                        "error": migration_error,
                    },
                    "ok": False,
                }

        inspector = inspect(self.engine)
        present = set(inspector.get_table_names())
        missing = sorted(required - present)
        current_revisions: list[str] = []
        migration_ok = False
        try:
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
            row = conn.execute(
                select(
                    self.runtime_state.c.id,
                    self.runtime_state.c.snapshot_json,
                ).where(self.runtime_state.c.id == 1)
            ).fetchone()
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
                            # Broker egress is fail-closed until an externally
                            # witnessed release generation is acknowledged by
                            # the exact runner boot and atomically activated.
                            "execution_egress_enabled": False,
                            "execution_egress_authority": {
                                "schema_version": _EXECUTION_EGRESS_SCHEMA,
                                "enabled": False,
                                "reason": "release_authority_not_active",
                                "generation_id": "",
                                "request_sha256": "",
                                "runtime_boot_id": "",
                                "updated_at": _now(),
                            },
                            "last_update": _now(),
                        },
                        updated_at=_now(),
                    )
                )
            else:
                snapshot = dict(row[1] if isinstance(row[1], dict) else {})
                if "execution_egress_enabled" not in snapshot:
                    snapshot["execution_egress_enabled"] = False
                    snapshot["execution_egress_authority"] = {
                        "schema_version": _EXECUTION_EGRESS_SCHEMA,
                        "enabled": False,
                        "reason": "legacy_state_fail_closed",
                        "generation_id": "",
                        "request_sha256": "",
                        "runtime_boot_id": "",
                        "updated_at": _now(),
                    }
                    snapshot["last_update"] = _now()
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(
                            snapshot_json=snapshot,
                            updated_at=float(snapshot["last_update"]),
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
                self._acquire_execution_queue_lock(conn)
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

    def quarantine_stale_delivered(self, *, age_secs: float) -> int:
        """Fence stale deliveries whose broker outcome is unknown.

        A delivered command may already have changed broker state even when
        its ACK never reached the bridge. Redelivering it would therefore risk
        repeating an OPEN, CLOSE, or CLOSE_ALL after a restart. Quarantined
        rows remain durable and ACK-reconcilable, but ``poll_next_command``
        cannot select them because their status is no longer ``queued``.
        """
        now = _now()
        cutoff = now - max(1.0, float(age_secs))
        updated = 0
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
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
                    result = conn.execute(
                        update(self.commands)
                        .where(
                            and_(
                                self.commands.c.command_id == cid,
                                self.commands.c.status == "delivered",
                                self.commands.c.updated_at <= cutoff,
                                self.commands.c.expires_at >= now,
                            )
                        )
                        .values(
                            status="reconcile_required",
                            updated_at=now,
                            reason="stale_delivery_outcome_unknown",
                        )
                    )
                    if int(result.rowcount or 0) != 1:
                        continue
                    self._append_command_event(
                        command_id=cid,
                        event_status="reconcile_required",
                        reason="stale_delivery_outcome_unknown",
                        payload={
                            "quarantined_at": now,
                            "previous_status": "delivered",
                            "delivered_count": int(row.get("delivered_count", 0) or 0),
                            "reconciliation_required": True,
                        },
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
                self._acquire_execution_queue_lock(conn)
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

    def disable_execution_egress(
        self,
        *,
        reason: str,
        revoke_release: bool = True,
    ) -> dict[str, Any]:
        """Atomically revoke broker egress and quarantine outstanding work."""

        now_ts = _now()
        normalized_reason = str(reason or "execution_egress_disabled").strip()
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(
                    row[0] if row and isinstance(row[0], dict) else {}
                )
                self._disable_execution_egress_in_state(
                    merged,
                    reason=normalized_reason,
                    now_ts=now_ts,
                )
                runtime_diag = dict(merged.get("runtime_diag") or {})
                live = dict(runtime_diag.get("orchestration_live") or {})
                live.update(
                    {
                        "runtime_enabled": False,
                        "queue_kill_active": True,
                        "queue_kill_reason": normalized_reason,
                        "queue_killed_at": now_ts,
                        "last_kill_reason": normalized_reason,
                        "last_kill_at": now_ts,
                        "authority_revision": max(
                            0,
                            _safe_int(live.get("authority_revision"), 0),
                        )
                        + 1,
                    }
                )
                runtime_diag["orchestration_live"] = live
                merged["runtime_diag"] = runtime_diag
                if revoke_release:
                    current = dict(merged.get("release_authority") or {})
                    current_status = str(
                        current.get("status") or ""
                    ).strip().lower()
                    if current_status in {"pending", "acknowledged", "active"}:
                        merged["release_authority"] = {
                            **current,
                            "status": "revoked",
                            "errors": list(
                                dict.fromkeys(
                                    [
                                        *list(current.get("errors") or []),
                                        normalized_reason,
                                    ]
                                )
                            ),
                            "updated_at": now_ts,
                        }
                quarantined = self._quarantine_execution_queue(
                    conn,
                    reason=normalized_reason,
                    now_ts=now_ts,
                )
                merged["last_update"] = now_ts
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=now_ts,
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(snapshot_json=merged, updated_at=now_ts)
                    )
        return {
            "execution_egress_enabled": False,
            "reason": normalized_reason,
            "quarantined_command_count": int(quarantined),
        }

    def enable_production_execution_egress(
        self,
        *,
        runtime_boot_id: str,
    ) -> dict[str, Any]:
        """Bind broker egress to the booted production runtime, never research."""

        boot_id = str(runtime_boot_id or "").strip()
        if not boot_id:
            raise ValueError("runtime_boot_id_required")
        now_ts = _now()
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = conn.execute(
                    select(self.runtime_state.c.snapshot_json)
                    .where(self.runtime_state.c.id == 1)
                    .with_for_update()
                ).first()
                merged = dict(
                    row[0] if row and isinstance(row[0], dict) else {}
                )
                runtime_diag = dict(merged.get("runtime_diag") or {})
                live = dict(runtime_diag.get("orchestration_live") or {})
                startup = dict(merged.get("runtime_startup") or {})
                if (
                    not bool(live.get("enabled", False))
                    or str(live.get("mode") or "").strip().lower() != "live"
                    or not bool(live.get("runtime_enabled", False))
                    or bool(live.get("queue_kill_active", False))
                ):
                    raise RuntimeError("production_live_authority_inactive")
                revision = max(0, _safe_int(live.get("authority_revision"), 0))
                if revision <= 0:
                    raise RuntimeError("production_live_authority_unattested")
                if str(startup.get("boot_id") or "").strip() != boot_id:
                    raise RuntimeError("production_runtime_boot_mismatch")
                if str(merged.get("runtime_status") or "").strip().lower() != "running":
                    raise RuntimeError("production_runtime_not_running")
                pair_scope = sorted(
                    {
                        str(item).strip().upper()
                        for item in list(live.get("active_pair_scope") or [])
                        if str(item).strip()
                    }
                )
                sleeve_scope = sorted(
                    {
                        str(item).strip().lower()
                        for item in list(live.get("active_sleeve_scope") or [])
                        if str(item).strip()
                    }
                )
                intent_scope = sorted(
                    {
                        str(item).strip().lower()
                        for item in list(live.get("active_intent_scope") or [])
                        if str(item).strip()
                    }
                )
                if not pair_scope or not sleeve_scope or not intent_scope:
                    raise RuntimeError("production_live_scope_incomplete")
                merged["execution_egress_enabled"] = True
                merged["execution_egress_authority"] = {
                    "schema_version": _EXECUTION_EGRESS_SCHEMA,
                    "enabled": True,
                    "source": "production_runtime",
                    "runtime_boot_id": boot_id,
                    "authority_revision": revision,
                    "pair_scope": pair_scope,
                    "sleeve_scope": sleeve_scope,
                    "intent_scope": intent_scope,
                    "reason": "operator_armed_production_runtime",
                    "updated_at": now_ts,
                }
                merged["last_update"] = now_ts
                conn.execute(
                    update(self.runtime_state)
                    .where(self.runtime_state.c.id == 1)
                    .values(snapshot_json=merged, updated_at=now_ts)
                )
        return {
            "execution_egress_enabled": True,
            "source": "production_runtime",
            "runtime_boot_id": boot_id,
            "authority_revision": revision,
        }

    def record_runtime_boot_state(self, *, boot: dict[str, Any], patch: dict[str, Any] | None = None, prune_state: bool = False) -> None:
        self.disable_execution_egress(
            reason="runtime_boot_requires_new_release_ack",
            revoke_release=True,
        )
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
        self.disable_execution_egress(
            reason="runtime_boot_failed",
            revoke_release=True,
        )
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

    def _execution_uncertainty_predicate(self):
        """Rows whose broker outcome cannot be proven terminal.

        ``expired`` is normally safe when a command was never delivered, but
        it remains execution-uncertain when ``delivered_count`` proves the EA
        received it before the queue lifetime ended. Unknown legacy statuses
        also fail closed instead of silently authorizing new exposure.
        """

        resolved_statuses = ("acked", "failed", "duplicate")
        known_statuses = ("queued", "delivered", "reconcile_required", "acked", "failed", "duplicate", "expired")
        mutates_broker_state = or_(
            self.commands.c.cmd.is_(None),
            func.upper(self.commands.c.cmd) != "INFO",
        )
        return and_(
            mutates_broker_state,
            or_(
                self.commands.c.status.in_(("delivered", "reconcile_required")),
                and_(
                    self.commands.c.delivered_count > 0,
                    ~self.commands.c.status.in_(resolved_statuses),
                ),
                ~self.commands.c.status.in_(known_statuses),
            ),
        )

    def _has_execution_uncertainty(self, conn) -> bool:
        row = conn.execute(
            select(self.commands.c.command_id)
            .where(self._execution_uncertainty_predicate())
            .limit(1)
        ).first()
        return row is not None

    def _acquire_execution_queue_lock(self, conn) -> None:
        """Serialize queue admission, broker delivery, and ACK transitions."""

        if str(conn.dialect.name).strip().lower() != "postgresql":
            return
        conn.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": self._EXECUTION_QUEUE_ADVISORY_LOCK_KEY},
        )

    @staticmethod
    def _release_egress_binding_error(authority: dict[str, Any]) -> str:
        """Return why a release state cannot authorize any broker command.

        Cryptographic and evidence validation happens before the release CAS
        in :class:`RuntimeService`. This is the transaction-local structural
        check: the exact witnessed request and exact runner boot ACK must still
        agree when the durable egress bit is changed.
        """

        state = dict(authority or {})
        if str(state.get("schema_version") or "") != _RELEASE_AUTHORITY_STATE_SCHEMA:
            return "release_authority_state_schema_invalid"
        if str(state.get("status") or "").strip().lower() != "active":
            return "release_authority_not_active"
        request = dict(state.get("request") or {})
        ack = dict(state.get("ack") or {})
        witness = dict(request.get("external_witness") or {})
        if str(request.get("schema_version") or "") != _RELEASE_AUTHORITY_REQUEST_SCHEMA:
            return "release_authority_request_schema_invalid"
        if str(ack.get("schema_version") or "") != _RELEASE_AUTHORITY_ACK_SCHEMA:
            return "release_authority_ack_schema_invalid"
        if str(witness.get("schema_version") or "") != _EXTERNAL_RELEASE_WITNESS_SCHEMA:
            return "release_witness_schema_invalid"
        if not str(witness.get("signature") or "").strip():
            return "release_witness_signature_missing"
        generation_id = str(request.get("generation_id") or "").strip()
        request_sha256 = str(request.get("request_sha256") or "").strip().lower()
        runtime_boot_id = str(ack.get("runtime_boot_id") or "").strip()
        if not generation_id or not request_sha256:
            return "release_authority_identity_missing"
        if str(ack.get("generation_id") or "").strip() != generation_id:
            return "release_authority_ack_generation_mismatch"
        if str(ack.get("request_sha256") or "").strip().lower() != request_sha256:
            return "release_authority_ack_request_mismatch"
        if not runtime_boot_id:
            return "release_authority_ack_boot_missing"
        for field in _RELEASE_EGRESS_IDENTITY_FIELDS:
            request_value = str(request.get(field) or "").strip()
            ack_value = str(ack.get(field) or "").strip()
            if not request_value or ack_value != request_value:
                return f"release_authority_ack_{field}_mismatch"
        return ""

    @staticmethod
    def _execution_egress_authorization_failure_from_state(
        state: dict[str, Any],
        *,
        now_ts: float | None = None,
        command: ExecutionCommand | None = None,
    ) -> str:
        snapshot = dict(state or {})
        now = float(_now() if now_ts is None else now_ts)
        if snapshot.get("execution_egress_enabled") is not True:
            return "execution_egress_disabled"
        egress = dict(snapshot.get("execution_egress_authority") or {})
        if (
            str(egress.get("schema_version") or "") != _EXECUTION_EGRESS_SCHEMA
            or egress.get("enabled") is not True
        ):
            return "execution_egress_authority_invalid"
        if str(egress.get("source") or "").strip().lower() == "production_runtime":
            runtime_startup = dict(snapshot.get("runtime_startup") or {})
            runtime_attestation = dict(snapshot.get("runtime_attestation") or {})
            runtime_boot_id = str(egress.get("runtime_boot_id") or "").strip()
            if (
                not runtime_boot_id
                or str(runtime_startup.get("boot_id") or "").strip()
                != runtime_boot_id
                or str(runtime_attestation.get("runtime_boot_id") or "").strip()
                != runtime_boot_id
            ):
                return "execution_egress_current_boot_mismatch"
            if str(snapshot.get("runtime_status") or "").strip().lower() != "running":
                return "execution_egress_runner_not_running"
            cycle_age = _timestamp_age_secs(
                snapshot.get("runtime_last_cycle_ts"),
                now_ts=now,
            )
            if cycle_age is None or cycle_age > _EXECUTION_EGRESS_RUNNER_LEASE_SECS:
                return "execution_egress_runner_lease_stale"
            runtime_diag = dict(snapshot.get("runtime_diag") or {})
            live = dict(runtime_diag.get("orchestration_live") or {})
            if (
                not bool(live.get("enabled", False))
                or str(live.get("mode") or "").strip().lower() != "live"
                or not bool(live.get("runtime_enabled", False))
                or bool(live.get("queue_kill_active", False))
            ):
                return "execution_egress_runtime_disabled"
            if _safe_int(live.get("authority_revision"), 0) != _safe_int(
                egress.get("authority_revision"),
                0,
            ):
                return "execution_egress_authority_revision_changed"
            pair_scope = {
                str(item).strip().upper()
                for item in list(egress.get("pair_scope") or [])
                if str(item).strip()
            }
            sleeve_scope = {
                str(item).strip().lower()
                for item in list(egress.get("sleeve_scope") or [])
                if str(item).strip()
            }
            intent_scope = {
                str(item).strip().lower()
                for item in list(egress.get("intent_scope") or [])
                if str(item).strip()
            }
            if pair_scope != {
                str(item).strip().upper()
                for item in list(live.get("active_pair_scope") or [])
                if str(item).strip()
            }:
                return "execution_egress_pair_scope_changed"
            if sleeve_scope != {
                str(item).strip().lower()
                for item in list(live.get("active_sleeve_scope") or [])
                if str(item).strip()
            }:
                return "execution_egress_sleeve_scope_changed"
            if intent_scope != {
                str(item).strip().lower()
                for item in list(live.get("active_intent_scope") or [])
                if str(item).strip()
            }:
                return "execution_egress_intent_scope_changed"
            if command is None:
                return ""
            cmd = str(command.cmd or "").strip().upper()
            symbol = str(command.symbol or "").strip().upper()
            required_intent = {
                "BUY": "enter",
                "SELL": "enter",
                "CLOSE": "exit",
                "CLOSE_ALL": "exit",
                "CLOSE_PARTIAL": "reduce",
                "MODIFY_SL": "tighten_stop",
            }.get(cmd, "")
            if required_intent and required_intent not in intent_scope:
                return "execution_egress_command_intent_blocked"
            if cmd not in {"INFO", "CLOSE_ALL"} and (
                not symbol or symbol not in pair_scope
            ):
                return "execution_egress_command_pair_blocked"
            if cmd in {"BUY", "SELL"}:
                meta = dict(command.orchestration_meta_json or {})
                payload = dict(command.payload or {})
                sleeve = str(
                    payload.get("sleeve")
                    or payload.get("adaptive_sleeve")
                    or meta.get("sleeve")
                    or meta.get("adaptive_sleeve")
                    or ""
                ).strip().lower()
                if not sleeve or sleeve not in sleeve_scope:
                    return "execution_egress_command_sleeve_blocked"
            return ""
        release = dict(snapshot.get("release_authority") or {})
        binding_error = PostgresRuntimeStore._release_egress_binding_error(release)
        if binding_error:
            return binding_error
        request = dict(release.get("request") or {})
        ack = dict(release.get("ack") or {})
        witness = dict(request.get("external_witness") or {})
        witness_expires_at = _parse_iso_ts(witness.get("expires_at"))
        if witness_expires_at <= now:
            return "execution_egress_witness_expired"
        if str(egress.get("generation_id") or "") != str(
            request.get("generation_id") or ""
        ):
            return "execution_egress_generation_mismatch"
        if str(egress.get("request_sha256") or "") != str(
            request.get("request_sha256") or ""
        ):
            return "execution_egress_request_mismatch"
        if str(egress.get("runtime_boot_id") or "") != str(
            ack.get("runtime_boot_id") or ""
        ):
            return "execution_egress_boot_mismatch"

        runtime_startup = dict(snapshot.get("runtime_startup") or {})
        runtime_attestation = dict(snapshot.get("runtime_attestation") or {})
        current_boot_id = str(runtime_startup.get("boot_id") or "").strip()
        acknowledged_boot_id = str(ack.get("runtime_boot_id") or "").strip()
        if (
            not current_boot_id
            or current_boot_id != acknowledged_boot_id
            or str(runtime_attestation.get("runtime_boot_id") or "").strip()
            != acknowledged_boot_id
        ):
            return "execution_egress_current_boot_mismatch"
        if str(snapshot.get("runtime_status") or "").strip().lower() != "running":
            return "execution_egress_runner_not_running"
        cycle_age = _timestamp_age_secs(
            snapshot.get("runtime_last_cycle_ts"),
            now_ts=now,
        )
        if cycle_age is None or cycle_age > _EXECUTION_EGRESS_RUNNER_LEASE_SECS:
            return "execution_egress_runner_lease_stale"

        execution = dict(request.get("authorized_execution") or {})
        signed_account_mode = str(execution.get("account_mode") or "").strip().lower()
        signed_account_scope = str(execution.get("account_scope") or "").strip()
        if (
            str(snapshot.get("broker_account_mode") or "").strip().lower()
            != signed_account_mode
        ):
            return "execution_egress_account_mode_changed"
        if (
            str(snapshot.get("broker_account_scope") or "").strip()
            != signed_account_scope
        ):
            return "execution_egress_account_scope_changed"

        runtime_diag = dict(snapshot.get("runtime_diag") or {})
        live = dict(runtime_diag.get("orchestration_live") or {})
        signed_pairs = {
            str(item).strip().upper()
            for item in list(execution.get("pair_scope") or [])
            if str(item).strip()
        }
        signed_sleeves = {
            str(item).strip().lower()
            for item in list(execution.get("sleeve_scope") or [])
            if str(item).strip()
        }
        signed_intents = {
            str(item).strip().lower()
            for item in list(execution.get("intent_scope") or [])
            if str(item).strip()
        }
        current_pairs = {
            str(item).strip().upper()
            for item in list(live.get("active_pair_scope") or [])
            if str(item).strip()
        }
        current_sleeves = {
            str(item).strip().lower()
            for item in list(live.get("active_sleeve_scope") or [])
            if str(item).strip()
        }
        current_intents = {
            str(item).strip().lower()
            for item in list(live.get("active_intent_scope") or [])
            if str(item).strip()
        }
        if (
            not current_pairs
            or not current_pairs.issubset(signed_pairs)
            or not current_sleeves
            or not current_sleeves.issubset(signed_sleeves)
            or not current_intents
            or not current_intents.issubset(signed_intents)
        ):
            return "execution_egress_runtime_scope_widened"

        if command is not None:
            cmd = str(command.cmd or "").strip().upper()
            symbol = str(command.symbol or "").strip().upper()
            command_meta = dict(command.orchestration_meta_json or {})
            command_release_expectations = {
                "release_generation_id": str(
                    request.get("generation_id") or ""
                ),
                "release_request_sha256": str(
                    request.get("request_sha256") or ""
                ),
                "release_model_identity_sha256": str(
                    request.get("model_identity_sha256") or ""
                ),
                "release_manifest_file_sha256": str(
                    request.get("manifest_file_sha256") or ""
                ),
                "release_runtime_boot_id": str(
                    ack.get("runtime_boot_id") or ""
                ),
            }
            for field_name, expected_value in command_release_expectations.items():
                if (
                    not expected_value
                    or str(command_meta.get(field_name) or "")
                    != expected_value
                ):
                    return f"execution_egress_command_{field_name}_mismatch"
            if cmd == "CLOSE_ALL":
                # Account-wide flatten is deliberately distinct from normal
                # singleton pair authority and must be present in the exact
                # externally signed execution contract.
                if execution.get("emergency_flatten_all") is not True:
                    return "execution_egress_emergency_flatten_unauthorized"
                return ""
            if symbol and symbol not in signed_pairs:
                return "execution_egress_command_pair_blocked"
            if cmd not in {"INFO"} and not symbol:
                return "execution_egress_command_pair_missing"
            if cmd == "INFO":
                return ""
            protective_intents = {
                str(item).strip().lower()
                for item in list(
                    execution.get("protective_intent_scope") or []
                )
                if str(item).strip()
            }
            if cmd in {"CLOSE", "CLOSE_PARTIAL"}:
                if "exit" not in protective_intents:
                    return "execution_egress_protective_exit_unauthorized"
                return ""
            if cmd == "MODIFY_SL":
                if "adjust" not in protective_intents:
                    return "execution_egress_protective_adjust_unauthorized"
                return ""
            if cmd not in {"BUY", "SELL"} or "enter" not in signed_intents:
                return "execution_egress_command_intent_blocked"
            payload = dict(command.payload or {})
            meta = command_meta
            command_sleeve = str(
                payload.get("sleeve")
                or payload.get("adaptive_sleeve")
                or meta.get("sleeve")
                or meta.get("adaptive_sleeve")
                or ""
            ).strip().lower()
            if cmd != "INFO" and (
                not command_sleeve or command_sleeve not in signed_sleeves
            ):
                return "execution_egress_command_sleeve_blocked"
        return ""

    def _execution_egress_authorization_failure(
        self,
        conn,
        *,
        now_ts: float | None = None,
        command: ExecutionCommand | None = None,
    ) -> str:
        state_row = conn.execute(
            select(self.runtime_state.c.snapshot_json)
            .where(self.runtime_state.c.id == 1)
            .with_for_update()
        ).first()
        state = dict(
            state_row[0]
            if state_row and isinstance(state_row[0], dict)
            else {}
        )
        return self._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now_ts,
            command=command,
        )

    def _quarantine_execution_queue(
        self,
        conn,
        *,
        reason: str,
        now_ts: float,
    ) -> int:
        """Expire undelivered work and quarantine unknown delivered outcomes."""

        rows = conn.execute(
            select(self.commands).where(
                self.commands.c.status.in_(["queued", "delivered"])
            )
        ).mappings().all()
        updated = 0
        normalized_reason = str(reason or "execution_egress_disabled")
        for raw_row in rows:
            row = dict(raw_row)
            command_id = str(row.get("command_id") or "")
            previous_status = str(row.get("status") or "")
            if not command_id:
                continue
            next_status = (
                "reconcile_required"
                if previous_status == "delivered"
                else "expired"
            )
            row_reason = (
                f"{normalized_reason}:broker_outcome_unknown"
                if previous_status == "delivered"
                else normalized_reason
            )
            result = conn.execute(
                update(self.commands)
                .where(
                    and_(
                        self.commands.c.command_id == command_id,
                        self.commands.c.status == previous_status,
                    )
                )
                .values(
                    status=next_status,
                    updated_at=float(now_ts),
                    reason=row_reason,
                )
            )
            if int(result.rowcount or 0) != 1:
                continue
            self._append_command_event(
                command_id=command_id,
                event_status=next_status,
                reason=row_reason,
                payload={
                    "execution_egress_enabled": False,
                    "previous_status": previous_status,
                    "delivery_attempted": previous_status == "delivered",
                },
                conn=conn,
            )
            updated += 1
        return updated

    def _quarantine_disabled_scalp_entries(
        self,
        conn,
        *,
        now_ts: float,
    ) -> int:
        """Fence recognizable standalone-scalp entries before broker poll.

        Queued rows were never handed to the EA and are terminally expired.
        Delivered rows may already have changed broker state, so they remain
        late-ACK-reconcilable under ``reconcile_required``.
        """

        rows = conn.execute(
            select(self.commands).where(
                self.commands.c.status.in_(["queued", "delivered"])
            )
        ).mappings().all()
        updated = 0
        for raw_row in rows:
            row = dict(raw_row)
            if not _is_identifiable_scalp_entry(
                cmd=row.get("cmd"),
                command_id=row.get("command_id"),
                intent=row.get("intent"),
                payload=row.get("payload_json"),
            ):
                continue
            command_id = str(row.get("command_id") or "")
            previous_status = str(row.get("status") or "")
            if not command_id or previous_status not in {"queued", "delivered"}:
                continue
            delivered = previous_status == "delivered"
            next_status = "reconcile_required" if delivered else "expired"
            reason = (
                f"poll_authority_revoked:{_DISABLED_SCALP_ENTRY_REASON}"
                + (":broker_outcome_unknown" if delivered else "")
            )
            result = conn.execute(
                update(self.commands)
                .where(
                    and_(
                        self.commands.c.command_id == command_id,
                        self.commands.c.status == previous_status,
                    )
                )
                .values(
                    status=next_status,
                    updated_at=float(now_ts),
                    reason=reason,
                )
            )
            if int(result.rowcount or 0) != 1:
                continue
            self._append_command_event(
                command_id=command_id,
                event_status=next_status,
                reason=reason,
                payload={
                    "authorization_failure": _DISABLED_SCALP_ENTRY_REASON,
                    "previous_status": previous_status,
                    "delivery_attempted": delivered,
                    "reconciliation_required": delivered,
                },
                conn=conn,
            )
            updated += 1
        return updated

    @staticmethod
    def _disable_execution_egress_in_state(
        state: dict[str, Any],
        *,
        reason: str,
        now_ts: float,
    ) -> None:
        snapshot = state
        snapshot["execution_egress_enabled"] = False
        snapshot["execution_egress_authority"] = {
            "schema_version": _EXECUTION_EGRESS_SCHEMA,
            "enabled": False,
            "reason": str(reason or "execution_egress_disabled"),
            "generation_id": "",
            "request_sha256": "",
            "runtime_boot_id": "",
            "updated_at": float(now_ts),
        }

    def _live_entry_authorization_failure(
        self,
        conn,
        *,
        pair: str,
        expected_account_mode: str,
        expected_account_scope: str,
        expected_authority_revision: int,
        now_ts: float,
        expected_release_generation_id: str = "",
        expected_release_request_sha256: str = "",
        expected_model_identity_sha256: str = "",
        expected_manifest_file_sha256: str = "",
        expected_runtime_boot_id: str = "",
    ) -> str:
        """Return the current reason an entry may not cross the broker edge.

        This check intentionally reads and locks the durable runtime state in
        the same transaction that either enqueues or delivers the command. A
        prior in-process approval is evidence of what was authorized, not a
        lease that survives a kill switch, broker identity drift, or stale
        transport data.
        """

        symbol = str(pair or "").strip().upper()
        expected_mode = str(expected_account_mode or "").strip().lower()
        expected_scope = str(expected_account_scope or "").strip()
        expected_revision = _safe_int(expected_authority_revision, 0)
        if expected_mode not in {"demo", "real"}:
            return "broker_account_mode_unattested"
        if not expected_scope:
            return "broker_account_scope_unattested"
        if expected_revision <= 0:
            return "live_authority_revision_unattested"
        if not symbol:
            return "live_pair_not_allowlisted"

        state_row = conn.execute(
            select(self.runtime_state.c.snapshot_json)
            .where(self.runtime_state.c.id == 1)
            .with_for_update()
        ).first()
        state = dict(
            state_row[0]
            if state_row and isinstance(state_row[0], dict)
            else {}
        )
        runtime_diag = dict(state.get("runtime_diag") or {})
        egress = dict(state.get("execution_egress_authority") or {})
        if str(egress.get("source") or "").strip().lower() != "production_runtime":
            release = dict(state.get("release_authority") or {})
            release_request = dict(release.get("request") or {})
            release_ack = dict(release.get("ack") or {})
            release_expectations = {
                "generation_id": (
                    str(expected_release_generation_id or ""),
                    str(release_request.get("generation_id") or ""),
                ),
                "request_sha256": (
                    str(expected_release_request_sha256 or ""),
                    str(release_request.get("request_sha256") or ""),
                ),
                "model_identity_sha256": (
                    str(expected_model_identity_sha256 or ""),
                    str(release_request.get("model_identity_sha256") or ""),
                ),
                "manifest_file_sha256": (
                    str(expected_manifest_file_sha256 or ""),
                    str(release_request.get("manifest_file_sha256") or ""),
                ),
                "runtime_boot_id": (
                    str(expected_runtime_boot_id or ""),
                    str(release_ack.get("runtime_boot_id") or ""),
                ),
            }
            for field_name, (expected_value, current_value) in release_expectations.items():
                if not expected_value:
                    return f"release_authority_{field_name}_unattested"
                if expected_value != current_value:
                    return f"release_authority_{field_name}_changed"
        live = dict(runtime_diag.get("orchestration_live") or {})
        admission = dict(runtime_diag.get("live_command_admission") or {})
        if not bool(live.get("enabled", False)):
            return "live_mode_disabled"
        if str(live.get("mode") or "").strip().lower() != "live":
            return "live_mode_disabled"
        if not bool(live.get("runtime_enabled", False)):
            return "live_runtime_killed"
        if bool(live.get("queue_kill_active", False)):
            return "live_queue_killed"
        if _safe_int(live.get("authority_revision"), 0) != expected_revision:
            return "live_authority_revision_changed"
        if not bool(admission.get("allowed", False)):
            return "live_command_admission_blocked"
        pair_admission = dict(
            dict(admission.get("pairs") or {}).get(symbol) or {}
        )
        if not bool(pair_admission.get("allowed", False)):
            return "live_rollout_pair_blocked"
        active_pairs = {
            str(item).strip().upper()
            for item in list(live.get("active_pair_scope") or [])
            if str(item).strip()
        }
        active_intents = {
            str(item).strip().lower()
            for item in list(live.get("active_intent_scope") or [])
            if str(item).strip()
        }
        if symbol not in active_pairs:
            return "live_pair_not_allowlisted"
        if "enter" not in active_intents:
            return "live_intent_not_allowlisted"
        if (
            str(state.get("broker_account_mode") or "").strip().lower()
            != expected_mode
        ):
            return "broker_account_mode_changed"
        if (
            str(state.get("broker_account_scope") or "").strip()
            != expected_scope
        ):
            return "broker_account_scope_changed"

        if str(state.get("system_status") or "").strip().lower() != "connected":
            return "broker_heartbeat_disconnected"
        settings = get_settings()
        heartbeat_age = _timestamp_age_secs(
            state.get("last_heartbeat"),
            now_ts=now_ts,
        )
        heartbeat_stale_after = max(
            1.0,
            float(settings.bridge_stale_heartbeat_secs),
        )
        if heartbeat_age is None:
            return "broker_heartbeat_invalid"
        if heartbeat_age > heartbeat_stale_after:
            return "broker_heartbeat_stale"

        tick = conn.execute(
            select(
                self.market_ticks.c.bid,
                self.market_ticks.c.ask,
                self.market_ticks.c.ts,
            )
            .where(self.market_ticks.c.symbol == symbol)
            .order_by(self.market_ticks.c.ts.desc())
            .limit(1)
        ).mappings().first()
        if tick is None:
            return "market_tick_missing"
        tick_age = _timestamp_age_secs(tick.get("ts"), now_ts=now_ts)
        tick_stale_after = max(1.0, float(settings.bridge_stale_tick_secs))
        if tick_age is None:
            return "market_tick_invalid"
        if tick_age > tick_stale_after:
            return "market_tick_stale"
        try:
            bid = float(tick.get("bid"))
            ask = float(tick.get("ask"))
        except (TypeError, ValueError, OverflowError):
            return "market_tick_invalid"
        if (
            not math.isfinite(bid)
            or not math.isfinite(ask)
            or bid <= 0.0
            or ask <= 0.0
            or ask < bid
        ):
            return "market_tick_invalid"
        return ""

    def _poll_entry_authorization_failure(
        self,
        conn,
        *,
        row: dict[str, Any],
        now_ts: float,
    ) -> str:
        """Reauthorize a durable BUY/SELL immediately before delivery."""

        payload = dict(row.get("payload_json") or {})
        return self._live_entry_authorization_failure(
            conn,
            pair=str(row.get("symbol") or payload.get("symbol") or ""),
            expected_account_mode=str(payload.get("expected_account_mode") or ""),
            expected_account_scope=str(payload.get("expected_account_scope") or ""),
            expected_authority_revision=_safe_int(
                payload.get("expected_authority_revision"),
                0,
            ),
            now_ts=now_ts,
            expected_release_generation_id=str(
                payload.get("expected_release_generation_id") or ""
            ),
            expected_release_request_sha256=str(
                payload.get("expected_release_request_sha256") or ""
            ),
            expected_model_identity_sha256=str(
                payload.get("expected_model_identity_sha256") or ""
            ),
            expected_manifest_file_sha256=str(
                payload.get("expected_manifest_file_sha256") or ""
            ),
            expected_runtime_boot_id=str(
                payload.get("expected_runtime_boot_id") or ""
            ),
        )

    def get_execution_uncertainty(self, *, limit: int = 20) -> dict[str, Any]:
        """Return a bounded diagnostic for unresolved broker outcomes.

        This is intentionally a read-only store query. Admission enforcement
        performs the same predicate again inside the enqueue transaction so a
        clear preflight cannot race a delivery transition.
        """

        bounded_limit = max(1, min(int(limit), 100))
        predicate = self._execution_uncertainty_predicate()
        with self._lock:
            with self.engine.begin() as conn:
                count = int(
                    conn.execute(
                        select(func.count())
                        .select_from(self.commands)
                        .where(predicate)
                    ).scalar_one()
                    or 0
                )
                status_rows = conn.execute(
                    select(self.commands.c.status, func.count())
                    .where(predicate)
                    .group_by(self.commands.c.status)
                ).all()
                rows = conn.execute(
                    select(
                        self.commands.c.command_id,
                        self.commands.c.cmd,
                        self.commands.c.symbol,
                        self.commands.c.status,
                        self.commands.c.delivered_count,
                        self.commands.c.updated_at,
                    )
                    .where(predicate)
                    .order_by(self.commands.c.updated_at.asc())
                    .limit(bounded_limit)
                ).mappings().all()
        return {
            "blocked": count > 0,
            "reason": "broker_execution_outcome_unresolved" if count > 0 else "",
            "count": count,
            "statuses": {str(status): int(total) for status, total in status_rows},
            "commands": [dict(row) for row in rows],
        }

    def enqueue_command(
        self,
        cmd: ExecutionCommand,
        *,
        require_resolved_execution: bool = False,
        required_live_admission: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        state_patch: dict[str, Any] | None = None
        now = _now()
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                if _is_identifiable_scalp_entry(
                    cmd=cmd.cmd,
                    command_id=cmd.command_id,
                    intent=cmd.intent,
                    payload=cmd.payload,
                ):
                    return False, _DISABLED_SCALP_ENTRY_REASON
                egress_failure = self._execution_egress_authorization_failure(
                    conn,
                    now_ts=now,
                    command=cmd,
                )
                if egress_failure:
                    return False, egress_failure
                existing = conn.execute(select(self.commands.c.status).where(self.commands.c.command_id == cmd.command_id)).fetchone()
                if existing is not None:
                    return False, str(existing[0])
                if str(cmd.idempotency_key or "").strip():
                    existing = conn.execute(
                        select(self.commands.c.command_id, self.commands.c.status)
                        .where(
                            and_(
                                self.commands.c.idempotency_key == str(cmd.idempotency_key),
                                self.commands.c.created_at <= now,
                                self.commands.c.expires_at >= now,
                            )
                        )
                        .order_by(self.commands.c.created_at.desc())
                        .limit(1)
                    ).fetchone()
                    if existing is not None:
                        return False, str(existing[1])

                # Duplicate checks deliberately precede the fence: an exact
                # idempotent retry cannot increase exposure and must retain
                # its existing command identity. Every genuinely new entry is
                # checked in this same lock/transaction as the insert.
                if bool(require_resolved_execution) and self._has_execution_uncertainty(conn):
                    return False, "reconciliation_required"

                if required_live_admission:
                    required = dict(required_live_admission or {})
                    expected_mode = str(
                        required.get("broker_account_mode") or ""
                    ).strip().lower()
                    expected_scope = str(
                        required.get("broker_account_scope") or ""
                    ).strip()
                    expected_revision = _safe_int(
                        required.get("authority_revision"),
                        0,
                    )
                    expected_release_generation_id = str(
                        required.get("release_generation_id") or ""
                    )
                    expected_release_request_sha256 = str(
                        required.get("release_request_sha256") or ""
                    )
                    expected_model_identity_sha256 = str(
                        required.get("model_identity_sha256") or ""
                    )
                    expected_manifest_file_sha256 = str(
                        required.get("manifest_file_sha256") or ""
                    )
                    expected_runtime_boot_id = str(
                        required.get("runtime_boot_id") or ""
                    )
                    pair = str(required.get("pair") or cmd.symbol or "").strip().upper()
                    payload = dict(cmd.payload or {})
                    if str(cmd.cmd or "").strip().upper() not in {"BUY", "SELL"}:
                        return False, "final_entry_approval_missing"
                    if str(cmd.symbol or "").strip().upper() != pair:
                        return False, "live_pair_not_allowlisted"
                    if (
                        str(payload.get("expected_account_mode") or "")
                        .strip()
                        .lower()
                        != expected_mode
                    ):
                        return False, "broker_account_mode_approval_mismatch"
                    if (
                        str(payload.get("expected_account_scope") or "").strip()
                        != expected_scope
                    ):
                        return False, "broker_account_scope_approval_mismatch"
                    if _safe_int(payload.get("expected_authority_revision"), 0) != expected_revision:
                        return False, "live_authority_revision_approval_mismatch"
                    admission_failure = self._live_entry_authorization_failure(
                        conn,
                        pair=pair,
                        expected_account_mode=expected_mode,
                        expected_account_scope=expected_scope,
                        expected_authority_revision=expected_revision,
                        now_ts=now,
                        expected_release_generation_id=expected_release_generation_id,
                        expected_release_request_sha256=expected_release_request_sha256,
                        expected_model_identity_sha256=expected_model_identity_sha256,
                        expected_manifest_file_sha256=expected_manifest_file_sha256,
                        expected_runtime_boot_id=expected_runtime_boot_id,
                    )
                    if admission_failure:
                        return False, admission_failure

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
                self.update_state_patch(
                    {
                        "signals_sent": int(state.get("signals_sent", 0)) + 1,
                        "last_signal": {
                            "command_id": str(state_patch["command_id"]),
                            "cmd": str(state_patch["cmd"]),
                            "symbol": str(state_patch["symbol"]),
                            "ts": _now(),
                        },
                    }
                )
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
                            self.commands.c.created_at <= now,
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
        if row is None:
            return None
        out = dict(row)
        raw = dict(out.get("raw_json") or {})
        nested_raw = dict(raw.get("raw") or {}) if isinstance(raw.get("raw"), dict) else {}
        for key in ("bid", "ask", "mid"):
            try:
                current = float(out.get(key))
            except (TypeError, ValueError, OverflowError):
                current = 0.0
            if math.isfinite(current) and current > 0.0:
                continue
            for source in (raw, nested_raw):
                try:
                    candidate = float(source.get(key))
                except (TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(candidate) and candidate > 0.0:
                    out[key] = candidate
                    break
        return out

    def poll_next_command(self) -> ExecutionCommand | None:
        now = _now()
        with self._lock:
            self.cleanup_expired_commands()
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                self._quarantine_disabled_scalp_entries(conn, now_ts=now)
                egress_failure = self._execution_egress_authorization_failure(
                    conn,
                    now_ts=now,
                )
                if egress_failure:
                    self._quarantine_execution_queue(
                        conn,
                        reason=f"poll_egress_revoked:{egress_failure}",
                        now_ts=now,
                    )
                    return None
                execution_uncertain = self._has_execution_uncertainty(conn)
                rows = conn.execute(
                    select(self.commands)
                    .where(
                        and_(
                            self.commands.c.status == "queued",
                            self.commands.c.created_at <= now,
                            self.commands.c.expires_at >= now,
                        )
                    )
                    .order_by(self.commands.c.created_at.asc())
                ).mappings().all()
                row: dict[str, Any] | None = None
                for queued_row in rows:
                    candidate = dict(queued_row)
                    candidate_command = ExecutionCommand(
                        command_id=str(candidate.get("command_id") or ""),
                        session_id=str(candidate.get("session_id") or ""),
                        proto=str(candidate.get("proto") or "v2"),
                        cmd=str(candidate.get("cmd") or ""),
                        symbol=str(candidate.get("symbol") or ""),
                        lots=float(candidate.get("lots") or 0.0),
                        intent=str(candidate.get("intent") or "UNKNOWN"),
                        orchestration_meta_json=dict(
                            candidate.get("orchestration_meta_json") or {}
                        ),
                        payload=dict(candidate.get("payload_json") or {}),
                    )
                    command_egress_failure = (
                        self._execution_egress_authorization_failure(
                            conn,
                            now_ts=now,
                            command=candidate_command,
                        )
                    )
                    if command_egress_failure:
                        reason = (
                            "poll_egress_revoked:"
                            f"{command_egress_failure}"
                        )
                        conn.execute(
                            update(self.commands)
                            .where(
                                and_(
                                    self.commands.c.command_id
                                    == candidate["command_id"],
                                    self.commands.c.status == "queued",
                                )
                            )
                            .values(
                                status="expired",
                                updated_at=now,
                                reason=reason,
                            )
                        )
                        self._append_command_event(
                            command_id=str(candidate["command_id"]),
                            event_status="expired",
                            reason=reason,
                            payload={
                                "authorization_failure": str(
                                    command_egress_failure
                                ),
                                "delivery_attempted": False,
                            },
                            conn=conn,
                        )
                        continue
                    exposure_increasing = (
                        str(candidate.get("cmd") or "").strip().upper()
                        in {"BUY", "SELL"}
                    )
                    if exposure_increasing:
                        try:
                            authorization_failure = (
                                self._poll_entry_authorization_failure(
                                    conn,
                                    row=candidate,
                                    now_ts=now,
                                )
                            )
                        except Exception:
                            # An unavailable authority check is itself a hard
                            # failure for new exposure. Expire the command so
                            # it cannot become executable after a later poll;
                            # protective commands behind it remain available.
                            authorization_failure = "poll_authority_check_failed"
                        if authorization_failure:
                            reason = (
                                "poll_authority_revoked:"
                                f"{authorization_failure}"
                            )
                            conn.execute(
                                update(self.commands)
                                .where(
                                    and_(
                                        self.commands.c.command_id
                                        == candidate["command_id"],
                                        self.commands.c.status == "queued",
                                    )
                                )
                                .values(
                                    status="expired",
                                    updated_at=now,
                                    reason=reason,
                                )
                            )
                            self._append_command_event(
                                command_id=str(candidate["command_id"]),
                                event_status="expired",
                                reason=reason,
                                payload={
                                    "authorization_failure": str(
                                        authorization_failure
                                    ),
                                    "delivery_attempted": False,
                                },
                                conn=conn,
                            )
                            continue
                        if execution_uncertain:
                            # A prior broker outcome is unresolved. The entry
                            # remains queued, but exits and protection may pass.
                            continue
                    row = candidate
                    break
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
                self._acquire_execution_queue_lock(conn)
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
                delivered_before = int(row.get("delivered_count", 0) or 0) > 0
                # An expired row is terminal only when it was never handed to
                # the EA. If it was delivered first, expiry does not prove the
                # broker outcome and a late durable ACK must still reconcile it.
                if cur in {"acked", "failed", "expired", "duplicate"} and not (
                    cur == "expired" and delivered_before
                ):
                    return {"status": cur, "command_id": command_id, "idempotent": True}, 200

                can_finalize = cur in {"delivered", "reconcile_required"} or (
                    cur in {"queued", "expired"} and delivered_before
                )
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
                ack_state_patch: dict[str, Any] = {
                    "last_ack": dict(state_patch["last_ack"])
                }
                if int(state_patch.get("inc_trades", 0)) > 0:
                    ack_state_patch["trades_executed"] = int(
                        state.get("trades_executed", 0)
                    ) + 1
                self.update_state_patch(ack_state_patch)
            out = {"status": status, "command_id": command_id}
            if idempotency_key and not command_id == str(ack.command_id or "").strip():
                out["idempotency_key"] = idempotency_key
            return out, 200

    def record_tick(self, payload: dict[str, Any]) -> None:
        sym = str(payload.get("symbol", "")).strip().upper()
        if not sym:
            return
        received_at = _now()
        observed_at = _parse_iso_ts(
            payload.get("time")
            or payload.get("ts")
            or payload.get("timestamp")
        )
        if (
            not math.isfinite(observed_at)
            or observed_at <= 0.0
            or observed_at > received_at + 5.0
        ):
            observed_at = received_at

        def _positive_or_none(value: Any) -> float | None:
            try:
                number = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return number if math.isfinite(number) and number > 0.0 else None

        with self.engine.begin() as conn:
            conn.execute(
                self.market_ticks.insert().values(
                    symbol=sym,
                    bid=_positive_or_none(payload.get("bid")),
                    ask=_positive_or_none(payload.get("ask")),
                    spread=float(payload.get("spread", 0.0) or 0.0),
                    ts=float(observed_at),
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

        self.update_state_patch(
            {
                "agent_decisions": list(decisions or []),
                "agent_diagnostics": dict(diagnostics or {}),
                "vol": float(vol),
            }
        )

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
        expected_live_authority = incoming.pop(
            "__expected_orchestration_live_authority__",
            None,
        )
        # These fields are release-CAS owned. A runner cycle, bridge caller,
        # or generic state repair may disable trading through the dedicated
        # safety path, but can never mint or re-enable broker egress by
        # overwriting JSON state.
        incoming.pop("release_authority", None)
        incoming.pop("release_witness_nonce_ledger", None)
        incoming.pop("execution_egress_enabled", None)
        incoming.pop("execution_egress_authority", None)
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
                if isinstance(incoming.get("runtime_diag"), dict):
                    current_runtime_diag = dict(merged.get("runtime_diag") or {})
                    current_live = dict(current_runtime_diag.get("orchestration_live") or {})
                    incoming_runtime_diag = dict(incoming.get("runtime_diag") or {})
                    if "orchestration_live" in incoming_runtime_diag:
                        incoming_live = dict(
                            incoming_runtime_diag.get("orchestration_live") or {}
                        )
                        release_is_active = str(
                            dict(merged.get("release_authority") or {}).get(
                                "status"
                            )
                            or ""
                        ).strip().lower() == "active"
                        if release_is_active or bool(
                            merged.get("execution_egress_enabled", False)
                        ):
                            # Live scopes are part of the signed release
                            # request. Runtime telemetry patches may update
                            # observations, never authority or scope.
                            _preserve_selected_state_fields(
                                incoming=incoming_live,
                                current=current_live,
                                fields=_ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                            )
                        if isinstance(expected_live_authority, dict):
                            current_authority = _selected_state_fields(
                                current_live,
                                _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                            )
                            expected_authority = _selected_state_fields(
                                expected_live_authority,
                                _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                            )
                            if current_authority != expected_authority:
                                _preserve_selected_state_fields(
                                    incoming=incoming_live,
                                    current=current_live,
                                    fields=_ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                                )
                                current_stage_identity = _selected_state_fields(
                                    current_live,
                                    _ORCHESTRATION_LIVE_STAGE_IDENTITY_FIELDS,
                                )
                                expected_stage_identity = _selected_state_fields(
                                    expected_live_authority,
                                    _ORCHESTRATION_LIVE_STAGE_IDENTITY_FIELDS,
                                )
                                if current_stage_identity != expected_stage_identity:
                                    _preserve_selected_state_fields(
                                        incoming=incoming_live,
                                        current=current_live,
                                        fields=_ORCHESTRATION_LIVE_ENTRY_EVIDENCE_FIELDS,
                                    )
                        current_revision = max(
                            0,
                            _safe_int(current_live.get("authority_revision"), 0),
                        )
                        authority_changed = _selected_state_fields(
                            incoming_live,
                            _ORCHESTRATION_LIVE_REVISION_TRIGGER_FIELDS,
                        ) != _selected_state_fields(
                            current_live,
                            _ORCHESTRATION_LIVE_REVISION_TRIGGER_FIELDS,
                        )
                        admission_changed = (
                            "live_command_admission" in incoming_runtime_diag
                            and dict(
                                incoming_runtime_diag.get("live_command_admission")
                                or {}
                            )
                            != dict(
                                current_runtime_diag.get("live_command_admission")
                                or {}
                            )
                        )
                        incoming_live["authority_revision"] = int(
                            current_revision
                            + (1 if authority_changed or admission_changed else 0)
                        )
                        incoming_runtime_diag["orchestration_live"] = incoming_live
                        incoming["runtime_diag"] = incoming_runtime_diag
                previous_profile = str(merged.get("runtime_profile", "") or "")
                merged.update(incoming)
                next_profile = str(merged.get("runtime_profile", "") or "")
                s = get_settings()
                should_prune = bool(force_prune) or (
                    bool(s.runtime_state_prune_stale_keys) and bool(next_profile) and next_profile != previous_profile
                )
                if should_prune:
                    protected_state_keys = {
                        "release_authority",
                        "release_witness_nonce_ledger",
                        "execution_egress_enabled",
                        "execution_egress_authority",
                        "runtime_attestation",
                    }
                    for stale_key in s.runtime_state_stale_keys:
                        if (
                            stale_key
                            and stale_key not in protected_state_keys
                            and stale_key not in incoming
                            and stale_key in merged
                        ):
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

    def claim_bridge_consumer_lease(
        self,
        *,
        consumer_identity: str,
        terminal_lease_scope: str,
        credential_generation_id: str,
        channel: str,
        lease_secs: float,
    ) -> dict[str, Any]:
        """Atomically admit exactly one EA consumer for the terminal scope."""

        identity = str(consumer_identity or "").strip()
        scope = str(terminal_lease_scope or "").strip()
        generation = str(credential_generation_id or "").strip()
        channel_name = str(channel or "").strip().lower()
        if not identity or not scope or not generation:
            return {"ok": False, "reason": "bridge_consumer_identity_incomplete"}
        if channel_name not in {"poll", "ack"}:
            return {"ok": False, "reason": "bridge_consumer_channel_invalid"}
        ttl = min(120.0, max(5.0, float(lease_secs)))
        now_ts = _now()
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
                current = dict(merged.get("bridge_consumer_lease") or {})
                current_fresh = float(current.get("expires_at") or 0.0) > now_ts
                if current_fresh and (
                    str(current.get("consumer_identity") or "") != identity
                    or str(current.get("terminal_lease_scope") or "") != scope
                    or str(current.get("credential_generation_id") or "") != generation
                ):
                    return {
                        "ok": False,
                        "reason": "bridge_consumer_lease_busy",
                        "expires_at": float(current.get("expires_at") or 0.0),
                    }
                lease = {
                    **current,
                    "schema_version": "fxstack_bridge_consumer_lease_v1",
                    "consumer_identity": identity,
                    "terminal_lease_scope": scope,
                    "credential_generation_id": generation,
                    "acquired_at": float(current.get("acquired_at") or now_ts),
                    "renewed_at": now_ts,
                    "expires_at": now_ts + ttl,
                    f"{channel_name}_authenticated_at": now_ts,
                }
                merged["bridge_consumer_lease"] = lease
                merged["last_update"] = now_ts
                if row is None:
                    conn.execute(
                        self.runtime_state.insert().values(
                            id=1,
                            snapshot_json=merged,
                            updated_at=now_ts,
                        )
                    )
                else:
                    conn.execute(
                        update(self.runtime_state)
                        .where(self.runtime_state.c.id == 1)
                        .values(snapshot_json=merged, updated_at=now_ts)
                    )
                return {"ok": True, "lease": lease}

    def compare_and_set_release_authority(
        self,
        *,
        next_authority: dict[str, Any],
        expected_generation_id: str = "",
        expected_status: str = "",
        safety_dominant: bool = False,
    ) -> dict[str, Any]:
        """Atomically replace release authority and its broker-egress lease.

        Witness/evidence verification is deliberately performed while the
        durable state row and execution queue are locked. A structurally
        plausible payload supplied by another in-process caller is not an
        authorization capability.
        """

        incoming = dict(next_authority or {})
        with self._lock:
            with self.engine.begin() as conn:
                self._acquire_execution_queue_lock(conn)
                row = (
                    conn.execute(
                        select(self.runtime_state.c.snapshot_json)
                        .where(self.runtime_state.c.id == 1)
                        .with_for_update()
                    ).first()
                )
                merged = dict(row[0] if row and isinstance(row[0], dict) else {})
                current = dict(merged.get("release_authority") or {})
                current_request = dict(current.get("request") or {})
                current_generation = str(current_request.get("generation_id") or "")
                current_status = str(current.get("status") or "").strip().lower()
                incoming_status = str(incoming.get("status") or "").strip().lower()
                incoming_request = dict(incoming.get("request") or {})
                incoming_generation = str(
                    incoming_request.get("generation_id") or ""
                )
                if safety_dominant:
                    # Safety dominance is monotonic. It can revoke authority
                    # despite a stale expected generation, but it can never
                    # publish, acknowledge, or activate one.
                    if (
                        str(incoming.get("schema_version") or "")
                        != _RELEASE_AUTHORITY_STATE_SCHEMA
                        or incoming_status not in {"revoked", "rejected"}
                    ):
                        return {
                            "updated": False,
                            "reason": "release_safety_transition_must_revoke",
                            "authority": current,
                        }
                else:
                    if expected_generation_id and current_generation != str(expected_generation_id):
                        return {"updated": False, "reason": "release_generation_changed", "authority": current}
                    if expected_status and current_status != str(expected_status).strip().lower():
                        return {"updated": False, "reason": "release_status_changed", "authority": current}
                    incoming_pair = str(incoming_request.get("pair") or "").strip().upper()
                    current_pair = str(current_request.get("pair") or "").strip().upper()
                    if (
                        current_status in {"pending", "acknowledged", "active"}
                        and current_generation
                        and current_generation != incoming_generation
                    ):
                        return {"updated": False, "reason": "release_authority_singleton_busy", "authority": current}
                    if current_pair and incoming_pair and current_pair != incoming_pair:
                        return {"updated": False, "reason": "release_authority_scope_conflict", "authority": current}
                    if incoming_status == "active" and current_status != "acknowledged":
                        return {
                            "updated": False,
                            "reason": "release_activation_requires_acknowledged",
                            "authority": current,
                        }
                    if incoming_status == "acknowledged" and current_status != "pending":
                        return {
                            "updated": False,
                            "reason": "release_ack_requires_pending",
                            "authority": current,
                        }
                    if incoming_status in {"acknowledged", "active"} and (
                        incoming_generation != current_generation
                        or str(incoming_request.get("request_sha256") or "")
                        != str(current_request.get("request_sha256") or "")
                    ):
                        return {
                            "updated": False,
                            "reason": "release_transition_request_changed",
                            "authority": current,
                        }
                    if incoming_status == "pending":
                        witness = dict(incoming_request.get("external_witness") or {})
                        nonce = str(witness.get("nonce") or "").strip()
                        expires_at = _parse_iso_ts(witness.get("expires_at"))
                        if not nonce or expires_at <= _now():
                            return {"updated": False, "reason": "release_witness_invalid", "authority": current}
                        ledger = {
                            str(key): float(value)
                            for key, value in dict(merged.get("release_witness_nonce_ledger") or {}).items()
                            if str(key).strip() and _parse_iso_ts(value) > _now()
                        }
                        if nonce in ledger:
                            return {"updated": False, "reason": "release_witness_replayed", "authority": current}
                        ledger[nonce] = float(expires_at)
                        merged["release_witness_nonce_ledger"] = ledger

                    if incoming_status not in {
                        "pending",
                        "acknowledged",
                        "active",
                        "revoked",
                        "rejected",
                    }:
                        return {
                            "updated": False,
                            "reason": "release_authority_status_invalid",
                            "authority": current,
                        }

                    if incoming_status in {"pending", "acknowledged", "active"}:
                        # Local import avoids a store/module import cycle while
                        # keeping cryptographic and immutable-evidence checks
                        # inseparable from this state/queue transaction.
                        from fxstack.runtime.release_authority import (
                            active_authority_errors,
                            authority_request_errors,
                        )

                        pair = str(incoming_request.get("pair") or "").strip().upper()
                        active_db_row = conn.execute(
                            select(self.active_model_sets).where(
                                self.active_model_sets.c.pair == pair
                            )
                        ).mappings().first()
                        if incoming_status == "active":
                            runtime_attestation = dict(
                                merged.get("runtime_attestation") or {}
                            )
                            pair_attestation = dict(
                                dict(runtime_attestation.get("pairs") or {}).get(pair)
                                or {}
                            )
                            if pair_attestation:
                                runtime_attestation = {
                                    **runtime_attestation,
                                    **pair_attestation,
                                }
                            runtime_boot_id = str(
                                runtime_attestation.get("runtime_boot_id")
                                or merged.get("runtime_boot_id")
                                or ""
                            )
                            validation_errors = active_authority_errors(
                                incoming,
                                active_db_row=(
                                    dict(active_db_row)
                                    if active_db_row is not None
                                    else None
                                ),
                                runtime_boot_id=runtime_boot_id,
                                runtime_attestation=runtime_attestation,
                                expected_generation_id=incoming_generation,
                                expected_request_sha256=str(
                                    incoming_request.get("request_sha256") or ""
                                ),
                                validate_evidence=True,
                            )
                        else:
                            validation_errors = authority_request_errors(
                                incoming_request,
                                active_db_row=(
                                    dict(active_db_row)
                                    if active_db_row is not None
                                    else None
                                ),
                                validate_evidence=True,
                            )
                            if incoming_status == "acknowledged":
                                binding_probe = {**incoming, "status": "active"}
                                binding_error = self._release_egress_binding_error(
                                    binding_probe
                                )
                                if binding_error:
                                    validation_errors.append(binding_error)
                        if validation_errors:
                            return {
                                "updated": False,
                                "reason": "release_authority_validation_failed",
                                "errors": list(dict.fromkeys(validation_errors)),
                                "authority": current,
                            }

                merged["release_authority"] = incoming
                now_ts = _now()
                if incoming_status == "active":
                    binding_error = self._release_egress_binding_error(incoming)
                    if binding_error:
                        return {
                            "updated": False,
                            "reason": binding_error,
                            "authority": current,
                        }
                    ack = dict(incoming.get("ack") or {})
                    merged["execution_egress_enabled"] = True
                    merged["execution_egress_authority"] = {
                        "schema_version": _EXECUTION_EGRESS_SCHEMA,
                        "enabled": True,
                        "reason": "externally_witnessed_runner_ack",
                        "generation_id": incoming_generation,
                        "request_sha256": str(
                            incoming_request.get("request_sha256") or ""
                        ),
                        "runtime_boot_id": str(ack.get("runtime_boot_id") or ""),
                        "updated_at": now_ts,
                    }
                else:
                    self._disable_execution_egress_in_state(
                        merged,
                        reason=f"release_authority_{incoming_status or 'invalid'}",
                        now_ts=now_ts,
                    )
                    self._quarantine_execution_queue(
                        conn,
                        reason=f"execution_egress_{incoming_status or 'disabled'}",
                        now_ts=now_ts,
                    )
                merged["last_update"] = now_ts
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
        return {
            "updated": True,
            "reason": "updated",
            "authority": incoming,
            "execution_egress_enabled": incoming_status == "active",
        }

    def patch_orchestration_live_state(
        self,
        *,
        updates: dict[str, Any],
        expected_live_authority: dict[str, Any] | None,
        safety_dominant: bool = False,
        allow_reenable: bool = False,
    ) -> dict[str, Any]:
        """Atomically mutate operator/release-owned live authority.

        Enabling/ramp mutations use optimistic authority matching. Safety
        mutations are deliberately dominant, but must leave entry authority
        disabled so an older enable/ramp operation can never resurrect it.
        """

        authority_updates = {
            str(key): value for key, value in dict(updates or {}).items()
        }
        if not safety_dominant and not isinstance(expected_live_authority, dict):
            raise ValueError("expected_live_authority_required")
        if safety_dominant:
            next_runtime_enabled = bool(
                authority_updates.get("runtime_enabled", True)
            )
            next_queue_kill = bool(
                authority_updates.get("queue_kill_active", False)
            )
            if next_runtime_enabled and not next_queue_kill:
                raise ValueError("safety_dominant_mutation_must_disable_entries")

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
                runtime_diag = dict(merged.get("runtime_diag") or {})
                current_live = dict(runtime_diag.get("orchestration_live") or {})
                if not safety_dominant:
                    current_authority = _selected_state_fields(
                        current_live,
                        _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                    )
                    expected_authority = _selected_state_fields(
                        dict(expected_live_authority or {}),
                        _ORCHESTRATION_LIVE_AUTHORITY_FIELDS,
                    )
                    if current_authority != expected_authority:
                        raise RuntimeError("orchestration_live_authority_conflict")
                    requests_enabled = bool(
                        authority_updates.get(
                            "runtime_enabled",
                            current_live.get("runtime_enabled", False),
                        )
                    ) and not bool(
                        authority_updates.get(
                            "queue_kill_active",
                            current_live.get("queue_kill_active", False),
                        )
                    )
                    currently_disabled = (
                        not bool(current_live.get("runtime_enabled", False))
                        or bool(current_live.get("queue_kill_active", False))
                    )
                    if requests_enabled and currently_disabled and not allow_reenable:
                        raise RuntimeError("orchestration_live_reenable_requires_start")
                current_revision = max(
                    0,
                    _safe_int(current_live.get("authority_revision"), 0),
                )
                current_live.update(authority_updates)
                current_live["authority_revision"] = int(current_revision + 1)
                runtime_diag["orchestration_live"] = current_live
                merged["runtime_diag"] = runtime_diag
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
                        .values(
                            snapshot_json=merged,
                            updated_at=float(merged["last_update"]),
                        )
                    )
                return dict(current_live)

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

    def get_command_window_summary(self, *, start_ts: float, end_ts: float) -> dict[str, Any]:
        """Aggregate the complete durable command queue for an exact time window.

        Unlike ``get_commands``, this proof endpoint is intentionally uncapped.
        The query returns bounded cardinality (command/status groups), while the
        database counts every matching row in one transaction snapshot.
        """

        start = float(start_ts)
        end = float(end_ts)
        query_started_at = _now()
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start <= 0.0
            or end < start
            or end > query_started_at + 5.0
        ):
            raise ValueError("invalid_command_window")
        window = and_(
            self.commands.c.created_at >= start,
            self.commands.c.created_at <= end,
        )
        normalized_cmd = func.upper(self.commands.c.cmd).label("normalized_cmd")
        grouped_stmt = (
            select(
                normalized_cmd,
                self.commands.c.status,
                func.count().label("row_count"),
            )
            .where(window)
            .group_by(normalized_cmd, self.commands.c.status)
        )
        bounds_stmt = select(
            func.count().label("total_commands"),
            func.min(self.commands.c.created_at).label("first_created_at"),
            func.max(self.commands.c.created_at).label("last_created_at"),
        ).where(window)
        with self.engine.begin() as conn:
            groups = conn.execute(grouped_stmt).mappings().all()
            bounds = dict(conn.execute(bounds_stmt).mappings().one())

        status_counts: dict[str, int] = {}
        entry_status_counts: dict[str, int] = {}
        control_status_counts: dict[str, int] = {}
        command_counts: dict[str, int] = {}
        for raw in groups:
            command = str(raw.get("normalized_cmd") or "").strip().upper()
            status = str(raw.get("status") or "").strip().lower()
            count = int(raw.get("row_count") or 0)
            command_counts[command] = command_counts.get(command, 0) + count
            status_counts[status] = status_counts.get(status, 0) + count
            target = entry_status_counts if command in {"BUY", "SELL"} else control_status_counts
            target[status] = target.get(status, 0) + count
        entry_commands = sum(command_counts.get(command, 0) for command in ("BUY", "SELL"))
        total_commands = int(bounds.get("total_commands") or 0)
        return {
            "schema_version": "fxstack_command_window_summary_v1",
            "start_ts": start,
            "end_ts": end,
            "queried_at": _now(),
            "window_complete": True,
            "total_commands": total_commands,
            "entry_commands": int(entry_commands),
            "control_commands": int(total_commands - entry_commands),
            "first_created_at": (
                float(bounds["first_created_at"])
                if bounds.get("first_created_at") is not None
                else None
            ),
            "last_created_at": (
                float(bounds["last_created_at"])
                if bounds.get("last_created_at") is not None
                else None
            ),
            "status_counts": dict(sorted(status_counts.items())),
            "entry_status_counts": dict(sorted(entry_status_counts.items())),
            "control_status_counts": dict(sorted(control_status_counts.items())),
            "command_counts": dict(sorted(command_counts.items())),
        }

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
