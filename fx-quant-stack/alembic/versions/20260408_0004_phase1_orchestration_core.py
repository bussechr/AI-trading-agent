"""Phase 1 orchestration bus tables and command metadata.

Revision ID: 20260408_0004
Revises: 20260319_0003
Create Date: 2026-04-08 00:04:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260408_0004"
down_revision = "20260319_0003"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return bool(inspector.has_table(name))


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column_name in {col.get("name") for col in inspector.get_columns(table_name)}


def _safe_add_column(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, str(column.name)):
        op.add_column(table_name, column)


def _safe_drop_column(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _safe_create_index(name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    idx = {item.get("name") for item in inspector.get_indexes(table_name)}
    if name not in idx:
        op.create_index(name, table_name, columns, unique=unique)


def _safe_drop_index(name: str, table_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    idx = {item.get("name") for item in inspector.get_indexes(table_name)}
    if name in idx:
        op.drop_index(name, table_name=table_name)


def upgrade() -> None:
    _safe_add_column("commands", sa.Column("correlation_id", sa.String(length=192), nullable=True))
    _safe_add_column("commands", sa.Column("thread_id", sa.String(length=192), nullable=True))
    _safe_add_column("commands", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    _safe_add_column("commands", sa.Column("schema_version", sa.String(length=64), nullable=True))
    _safe_add_column("commands", sa.Column("orchestration_meta_json", sa.JSON(), nullable=True))

    if not _has_table("orchestration_runs"):
        op.create_table(
            "orchestration_runs",
            sa.Column("run_id", sa.String(length=64), primary_key=True),
            sa.Column("cycle_id", sa.String(length=128), nullable=False),
            sa.Column("thread_id", sa.String(length=192), nullable=False),
            sa.Column("correlation_id", sa.String(length=192), nullable=False),
            sa.Column("pair", sa.String(length=16), nullable=False),
            sa.Column("ts_utc", sa.Float(), nullable=False),
            sa.Column("runtime_mode", sa.String(length=16), nullable=False),
            sa.Column("latency_ms", sa.Integer(), nullable=False),
            sa.Column("fallback_used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("version_bundle_json", sa.JSON(), nullable=False),
            sa.Column("packet_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    if not _has_table("agent_proposals"):
        op.create_table(
            "agent_proposals",
            sa.Column("proposal_id", sa.String(length=64), primary_key=True),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("agent_id", sa.String(length=128), nullable=False),
            sa.Column("phase", sa.String(length=64), nullable=False),
            sa.Column("intent", sa.String(length=32), nullable=False),
            sa.Column("side", sa.String(length=16), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("expected_edge_bps", sa.Float(), nullable=False),
            sa.Column("uncertainty", sa.Float(), nullable=False),
            sa.Column("risk_cost", sa.Float(), nullable=False),
            sa.Column("ttl_ms", sa.Integer(), nullable=False),
            sa.Column("evidence_json", sa.JSON(), nullable=False),
            sa.Column("constraints_json", sa.JSON(), nullable=False),
            sa.Column("advisory_only", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    if not _has_table("governed_decisions"):
        op.create_table(
            "governed_decisions",
            sa.Column("decision_id", sa.String(length=64), primary_key=True),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("runtime_mode", sa.String(length=16), nullable=False, server_default="shadow"),
            sa.Column("allowed", sa.Integer(), nullable=False),
            sa.Column("selected_action", sa.String(length=64), nullable=False),
            sa.Column("command_preview_json", sa.JSON(), nullable=True),
            sa.Column("blocking_reasons_json", sa.JSON(), nullable=False),
            sa.Column("approval_state", sa.String(length=32), nullable=False),
            sa.Column("governor_version", sa.String(length=128), nullable=False),
            sa.Column("version_bundle_json", sa.JSON(), nullable=True),
            sa.Column("invariants_ok", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
        )
    _safe_add_column("governed_decisions", sa.Column("version_bundle_json", sa.JSON(), nullable=True))
    _safe_add_column("governed_decisions", sa.Column("runtime_mode", sa.String(length=16), nullable=False, server_default="shadow"))

    if not _has_table("agent_traces"):
        op.create_table(
            "agent_traces",
            sa.Column("trace_id", sa.String(length=128), primary_key=True),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("pair", sa.String(length=16), nullable=True),
            sa.Column("trace_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    if not _has_table("approval_events"):
        op.create_table(
            "approval_events",
            sa.Column("event_id", sa.String(length=64), primary_key=True),
            sa.Column("subject_type", sa.String(length=64), nullable=False),
            sa.Column("subject_id", sa.String(length=128), nullable=False),
            sa.Column("approver", sa.String(length=128), nullable=False),
            sa.Column("decision", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    if not _has_table("experiment_proposals"):
        op.create_table(
            "experiment_proposals",
            sa.Column("experiment_id", sa.String(length=64), primary_key=True),
            sa.Column("source_run_id", sa.String(length=64), nullable=True),
            sa.Column("hypothesis", sa.Text(), nullable=False),
            sa.Column("change_set_json", sa.JSON(), nullable=False),
            sa.Column("evaluation_plan_json", sa.JSON(), nullable=False),
            sa.Column("risk_notes_json", sa.JSON(), nullable=False),
            sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
            sa.Column("approval_status", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    _safe_create_index("ix_orchestration_runs_pair", "orchestration_runs", ["pair"])
    _safe_create_index("ix_orchestration_runs_cycle_id", "orchestration_runs", ["cycle_id"])
    _safe_create_index("ix_orchestration_runs_thread_id", "orchestration_runs", ["thread_id"])
    _safe_create_index("ix_orchestration_runs_ts_utc", "orchestration_runs", ["ts_utc"])
    _safe_create_index("ix_orchestration_runs_runtime_mode", "orchestration_runs", ["runtime_mode"])
    _safe_create_index("ix_orchestration_runs_correlation_id", "orchestration_runs", ["correlation_id"], unique=True)

    _safe_create_index("ix_agent_proposals_run_id", "agent_proposals", ["run_id"])
    _safe_create_index("ix_agent_proposals_agent_id", "agent_proposals", ["agent_id"])
    _safe_create_index("ix_agent_proposals_phase", "agent_proposals", ["phase"])

    _safe_create_index("ix_governed_decisions_run_id", "governed_decisions", ["run_id"], unique=True)
    _safe_create_index("ix_governed_decisions_runtime_mode", "governed_decisions", ["runtime_mode"])

    _safe_create_index("ix_agent_traces_run_id", "agent_traces", ["run_id"])
    _safe_create_index("ix_agent_traces_created_at", "agent_traces", ["created_at"])

    _safe_create_index("ix_approval_events_subject_type", "approval_events", ["subject_type"])
    _safe_create_index("ix_approval_events_subject_id", "approval_events", ["subject_id"])
    _safe_create_index("ix_approval_events_created_at", "approval_events", ["created_at"])

    _safe_create_index("ix_experiment_proposals_approval_status", "experiment_proposals", ["approval_status"])
    _safe_create_index("ix_experiment_proposals_created_at", "experiment_proposals", ["created_at"])


def downgrade() -> None:
    for idx, tbl in [
        ("ix_experiment_proposals_created_at", "experiment_proposals"),
        ("ix_experiment_proposals_approval_status", "experiment_proposals"),
        ("ix_approval_events_created_at", "approval_events"),
        ("ix_approval_events_subject_id", "approval_events"),
        ("ix_approval_events_subject_type", "approval_events"),
        ("ix_agent_traces_created_at", "agent_traces"),
        ("ix_agent_traces_run_id", "agent_traces"),
        ("ix_governed_decisions_runtime_mode", "governed_decisions"),
        ("ix_governed_decisions_run_id", "governed_decisions"),
        ("ix_agent_proposals_phase", "agent_proposals"),
        ("ix_agent_proposals_agent_id", "agent_proposals"),
        ("ix_agent_proposals_run_id", "agent_proposals"),
        ("ix_orchestration_runs_correlation_id", "orchestration_runs"),
        ("ix_orchestration_runs_runtime_mode", "orchestration_runs"),
        ("ix_orchestration_runs_ts_utc", "orchestration_runs"),
        ("ix_orchestration_runs_thread_id", "orchestration_runs"),
        ("ix_orchestration_runs_cycle_id", "orchestration_runs"),
        ("ix_orchestration_runs_pair", "orchestration_runs"),
    ]:
        if _has_table(tbl):
            _safe_drop_index(idx, tbl)

    for table in [
        "experiment_proposals",
        "approval_events",
        "agent_traces",
        "governed_decisions",
        "agent_proposals",
        "orchestration_runs",
    ]:
        if _has_table(table):
            op.drop_table(table)

    for column in [
        "orchestration_meta_json",
        "schema_version",
        "idempotency_key",
        "thread_id",
        "correlation_id",
    ]:
        _safe_drop_column("commands", column)
    _safe_drop_column("governed_decisions", "version_bundle_json")
    _safe_drop_column("governed_decisions", "runtime_mode")
