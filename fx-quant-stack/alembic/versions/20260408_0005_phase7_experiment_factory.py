"""Phase 7 experiment factory tables and proposal enrichment.

Revision ID: 20260408_0005
Revises: 20260408_0004
Create Date: 2026-04-08 00:05:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260408_0005"
down_revision = "20260408_0004"
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
    for column in [
        sa.Column("prompt_hash", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("tool_trace_hash", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("model_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("decision_seed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_artefact_refs_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("config_diff_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("replay_window", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("artifact_root", sa.Text(), nullable=False, server_default=""),
        sa.Column("latest_stage", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("latest_promotion_id", sa.String(length=64), nullable=False, server_default=""),
    ]:
        _safe_add_column("experiment_proposals", column)

    if not _has_table("experiment_promotions"):
        op.create_table(
            "experiment_promotions",
            sa.Column("promotion_id", sa.String(length=64), primary_key=True),
            sa.Column("experiment_id", sa.String(length=64), nullable=False),
            sa.Column("prompt_hash", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("tool_trace_hash", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("model_id", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("config_diff_json", sa.JSON(), nullable=False),
            sa.Column("replay_window", sa.String(length=128), nullable=False, server_default=""),
            sa.Column("replay_results_json", sa.JSON(), nullable=False),
            sa.Column("approval_records_json", sa.JSON(), nullable=False),
            sa.Column("paper_results_json", sa.JSON(), nullable=False),
            sa.Column("canary_results_json", sa.JSON(), nullable=False),
            sa.Column("release_manifest_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("rollback_metadata_json", sa.JSON(), nullable=False),
            sa.Column("artefact_hashes_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
        )

    if not _has_table("experiment_lineage"):
        op.create_table(
            "experiment_lineage",
            sa.Column("experiment_id", sa.String(length=64), primary_key=True),
            sa.Column("proposal_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("review_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("replay_refs_json", sa.JSON(), nullable=False),
            sa.Column("paper_pack_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("canary_pack_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("promotion_decision_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("rollback_plan_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("release_manifest_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("reflection_memory_ref", sa.Text(), nullable=False, server_default=""),
            sa.Column("latest_stage", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("latest_promotion_id", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("approval_status", sa.String(length=32), nullable=False, server_default=""),
            sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
            sa.Column("promotion_ids_json", sa.JSON(), nullable=False),
            sa.Column("approval_event_ids_json", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
        )

    _safe_create_index("ix_experiment_promotions_experiment_id", "experiment_promotions", ["experiment_id"])
    _safe_create_index("ix_experiment_promotions_status", "experiment_promotions", ["status"])
    _safe_create_index("ix_experiment_promotions_created_at", "experiment_promotions", ["created_at"])
    _safe_create_index("ix_experiment_lineage_latest_stage", "experiment_lineage", ["latest_stage"])
    _safe_create_index("ix_experiment_lineage_updated_at", "experiment_lineage", ["updated_at"])


def downgrade() -> None:
    for name, table_name in [
        ("ix_experiment_promotions_created_at", "experiment_promotions"),
        ("ix_experiment_promotions_status", "experiment_promotions"),
        ("ix_experiment_promotions_experiment_id", "experiment_promotions"),
        ("ix_experiment_lineage_updated_at", "experiment_lineage"),
        ("ix_experiment_lineage_latest_stage", "experiment_lineage"),
    ]:
        _safe_drop_index(name, table_name)
    if _has_table("experiment_lineage"):
        op.drop_table("experiment_lineage")
    if _has_table("experiment_promotions"):
        op.drop_table("experiment_promotions")
