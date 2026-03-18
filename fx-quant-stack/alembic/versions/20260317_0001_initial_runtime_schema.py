"""Initial fxstack runtime and model activation schema.

Revision ID: 20260317_0001
Revises: 
Create Date: 2026-03-17 00:00:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0001"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return bool(inspector.has_table(name))


def _create_indexes() -> None:
    op.create_index("ix_commands_status", "commands", ["status"], unique=False)
    op.create_index("ix_commands_created", "commands", ["created_at"], unique=False)
    op.create_index("ix_commands_expires", "commands", ["expires_at"], unique=False)

    op.create_index("ix_command_events_command_id", "command_events", ["command_id"], unique=False)
    op.create_index("ix_command_events_ts", "command_events", ["ts"], unique=False)

    op.create_index("ix_market_ticks_symbol", "market_ticks", ["symbol"], unique=False)
    op.create_index("ix_market_ticks_ts", "market_ticks", ["ts"], unique=False)

    op.create_index("ix_reports_ts", "reports", ["ts"], unique=False)
    op.create_index("ix_decision_snapshots_ts", "decision_snapshots", ["ts"], unique=False)
    op.create_index("ix_governance_events_ts", "governance_events", ["ts"], unique=False)
    op.create_index("ix_governance_events_type", "governance_events", ["event_type"], unique=False)

    op.create_index("ix_model_runs_run_id", "model_runs", ["run_id"], unique=True)
    op.create_index("ix_model_runs_pair", "model_runs", ["pair"], unique=False)
    op.create_index("ix_model_artifacts_set", "model_artifacts", ["model_set_id"], unique=False)
    op.create_index("ix_active_model_sets_enabled", "active_model_sets", ["enabled"], unique=False)


def _safe_create_index(name: str, table_name: str, columns: list[str], *, unique: bool = False) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    idx = {i.get("name") for i in inspector.get_indexes(table_name)}
    if name in idx:
        return
    op.create_index(name, table_name, columns, unique=unique)


def _safe_drop_index(name: str, table_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    idx = {i.get("name") for i in inspector.get_indexes(table_name)}
    if name in idx:
        op.drop_index(name, table_name=table_name)


def upgrade() -> None:
    if not _has_table("commands"):
        op.create_table(
            "commands",
            sa.Column("command_id", sa.String(length=128), primary_key=True),
            sa.Column("session_id", sa.String(length=64), nullable=False),
            sa.Column("proto", sa.String(length=16), nullable=False),
            sa.Column("cmd", sa.String(length=32), nullable=False),
            sa.Column("symbol", sa.String(length=16), nullable=True),
            sa.Column("lots", sa.Float(), nullable=True),
            sa.Column("tp_cash", sa.Float(), nullable=True),
            sa.Column("tp_price", sa.Float(), nullable=True),
            sa.Column("sl_price", sa.Float(), nullable=True),
            sa.Column("magic", sa.Integer(), nullable=True),
            sa.Column("intent", sa.String(length=32), nullable=True),
            sa.Column("trace_id", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.Column("delivered_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=True),
            sa.Column("ack_json", sa.JSON(), nullable=True),
        )

    if not _has_table("command_events"):
        op.create_table(
            "command_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("command_id", sa.String(length=128), nullable=False),
            sa.Column("event_status", sa.String(length=32), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("ts", sa.Float(), nullable=False),
            sa.Column("event_json", sa.JSON(), nullable=True),
        )

    if not _has_table("market_ticks"):
        op.create_table(
            "market_ticks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("symbol", sa.String(length=16), nullable=False),
            sa.Column("bid", sa.Float(), nullable=True),
            sa.Column("ask", sa.Float(), nullable=True),
            sa.Column("spread", sa.Float(), nullable=True),
            sa.Column("ts", sa.Float(), nullable=False),
            sa.Column("raw_json", sa.JSON(), nullable=True),
        )

    if not _has_table("reports"):
        op.create_table(
            "reports",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ts", sa.Float(), nullable=False),
            sa.Column("report_text", sa.Text(), nullable=True),
            sa.Column("report_json", sa.JSON(), nullable=True),
        )

    if not _has_table("decision_snapshots"):
        op.create_table(
            "decision_snapshots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ts", sa.Float(), nullable=False),
            sa.Column("vol", sa.Float(), nullable=True),
            sa.Column("decisions_json", sa.JSON(), nullable=True),
            sa.Column("diagnostics_json", sa.JSON(), nullable=True),
        )

    if not _has_table("governance_events"):
        op.create_table(
            "governance_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("ts", sa.Float(), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=True),
        )

    if not _has_table("runtime_state"):
        op.create_table(
            "runtime_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("snapshot_json", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
        )

    if not _has_table("model_runs"):
        op.create_table(
            "model_runs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(length=128), nullable=False),
            sa.Column("pair", sa.String(length=16), nullable=False),
            sa.Column("timeframe", sa.String(length=16), nullable=True),
            sa.Column("model_family", sa.String(length=64), nullable=False),
            sa.Column("artifact_path", sa.Text(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    if not _has_table("model_artifacts"):
        op.create_table(
            "model_artifacts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("model_set_id", sa.String(length=128), nullable=False),
            sa.Column("pair", sa.String(length=16), nullable=False),
            sa.Column("artifact_type", sa.String(length=64), nullable=False),
            sa.Column("artifact_path", sa.Text(), nullable=False),
            sa.Column("checksum", sa.String(length=128), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    if not _has_table("active_model_sets"):
        op.create_table(
            "active_model_sets",
            sa.Column("pair", sa.String(length=16), primary_key=True),
            sa.Column("model_set_id", sa.String(length=128), nullable=False),
            sa.Column("registry_path", sa.Text(), nullable=False),
            sa.Column("artifacts_json", sa.JSON(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.Float(), nullable=False),
        )

    _safe_create_index("ix_commands_status", "commands", ["status"])
    _safe_create_index("ix_commands_created", "commands", ["created_at"])
    _safe_create_index("ix_commands_expires", "commands", ["expires_at"])

    _safe_create_index("ix_command_events_command_id", "command_events", ["command_id"])
    _safe_create_index("ix_command_events_ts", "command_events", ["ts"])

    _safe_create_index("ix_market_ticks_symbol", "market_ticks", ["symbol"])
    _safe_create_index("ix_market_ticks_ts", "market_ticks", ["ts"])

    _safe_create_index("ix_reports_ts", "reports", ["ts"])
    _safe_create_index("ix_decision_snapshots_ts", "decision_snapshots", ["ts"])
    _safe_create_index("ix_governance_events_ts", "governance_events", ["ts"])
    _safe_create_index("ix_governance_events_type", "governance_events", ["event_type"])

    _safe_create_index("ix_model_runs_run_id", "model_runs", ["run_id"], unique=True)
    _safe_create_index("ix_model_runs_pair", "model_runs", ["pair"])
    _safe_create_index("ix_model_artifacts_set", "model_artifacts", ["model_set_id"])
    _safe_create_index("ix_active_model_sets_enabled", "active_model_sets", ["enabled"])


def downgrade() -> None:
    for idx, tbl in [
        ("ix_active_model_sets_enabled", "active_model_sets"),
        ("ix_model_artifacts_set", "model_artifacts"),
        ("ix_model_runs_pair", "model_runs"),
        ("ix_model_runs_run_id", "model_runs"),
        ("ix_governance_events_type", "governance_events"),
        ("ix_governance_events_ts", "governance_events"),
        ("ix_decision_snapshots_ts", "decision_snapshots"),
        ("ix_reports_ts", "reports"),
        ("ix_market_ticks_ts", "market_ticks"),
        ("ix_market_ticks_symbol", "market_ticks"),
        ("ix_command_events_ts", "command_events"),
        ("ix_command_events_command_id", "command_events"),
        ("ix_commands_expires", "commands"),
        ("ix_commands_created", "commands"),
        ("ix_commands_status", "commands"),
    ]:
        if _has_table(tbl):
            _safe_drop_index(idx, tbl)

    for table in [
        "active_model_sets",
        "model_artifacts",
        "model_runs",
        "runtime_state",
        "governance_events",
        "decision_snapshots",
        "reports",
        "market_ticks",
        "command_events",
        "commands",
    ]:
        if _has_table(table):
            op.drop_table(table)
