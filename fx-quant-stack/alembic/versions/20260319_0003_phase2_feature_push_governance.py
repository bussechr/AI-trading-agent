"""Phase 2 feature push governance tables.

Revision ID: 20260319_0003
Revises: 20260318_0002
Create Date: 2026-03-19 00:03:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260319_0003"
down_revision = "20260318_0002"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return bool(inspector.has_table(name))


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
    if not _has_table("feature_push_outbox"):
        op.create_table(
            "feature_push_outbox",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("outbox_key", sa.String(length=128), nullable=False),
            sa.Column("pair", sa.String(length=16), nullable=False),
            sa.Column("feature_service", sa.String(length=128), nullable=False),
            sa.Column("entity_key", sa.String(length=128), nullable=False),
            sa.Column("event_timestamp", sa.Float(), nullable=False),
            sa.Column("feature_version", sa.String(length=128), nullable=True),
            sa.Column("checksum", sa.String(length=128), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("claimed_by", sa.String(length=128), nullable=True),
            sa.Column("claimed_at", sa.Float(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.Column("delivered_at", sa.Float(), nullable=True),
        )

    if not _has_table("feature_push_audit"):
        op.create_table(
            "feature_push_audit",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("outbox_key", sa.String(length=128), nullable=False),
            sa.Column("pair", sa.String(length=16), nullable=False),
            sa.Column("feature_service", sa.String(length=128), nullable=False),
            sa.Column("entity_key", sa.String(length=128), nullable=False),
            sa.Column("event_timestamp", sa.Float(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("worker_id", sa.String(length=128), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    if not _has_table("feature_parity_audit"):
        op.create_table(
            "feature_parity_audit",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("pair", sa.String(length=16), nullable=False),
            sa.Column("feature_service", sa.String(length=128), nullable=False),
            sa.Column("entity_key", sa.String(length=128), nullable=False),
            sa.Column("event_timestamp", sa.Float(), nullable=False),
            sa.Column("source", sa.String(length=32), nullable=False),
            sa.Column("parity_ok", sa.Integer(), nullable=False),
            sa.Column("drift_score", sa.Float(), nullable=True),
            sa.Column("message", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.Float(), nullable=False),
        )

    _safe_create_index("ix_feature_push_outbox_status", "feature_push_outbox", ["status"])
    _safe_create_index("ix_feature_push_outbox_key", "feature_push_outbox", ["outbox_key"], unique=True)
    _safe_create_index("ix_feature_push_outbox_pair", "feature_push_outbox", ["pair"])
    _safe_create_index("ix_feature_push_outbox_created_at", "feature_push_outbox", ["created_at"])
    _safe_create_index("ix_feature_push_outbox_entity_key", "feature_push_outbox", ["entity_key"])

    _safe_create_index("ix_feature_push_audit_outbox_key", "feature_push_audit", ["outbox_key"])
    _safe_create_index("ix_feature_push_audit_status", "feature_push_audit", ["status"])
    _safe_create_index("ix_feature_push_audit_created_at", "feature_push_audit", ["created_at"])

    _safe_create_index("ix_feature_parity_audit_pair", "feature_parity_audit", ["pair"])
    _safe_create_index("ix_feature_parity_audit_service", "feature_parity_audit", ["feature_service"])
    _safe_create_index("ix_feature_parity_audit_created_at", "feature_parity_audit", ["created_at"])


def downgrade() -> None:
    for idx, tbl in [
        ("ix_feature_parity_audit_created_at", "feature_parity_audit"),
        ("ix_feature_parity_audit_service", "feature_parity_audit"),
        ("ix_feature_parity_audit_pair", "feature_parity_audit"),
        ("ix_feature_push_audit_created_at", "feature_push_audit"),
        ("ix_feature_push_audit_status", "feature_push_audit"),
        ("ix_feature_push_audit_outbox_key", "feature_push_audit"),
        ("ix_feature_push_outbox_entity_key", "feature_push_outbox"),
        ("ix_feature_push_outbox_created_at", "feature_push_outbox"),
        ("ix_feature_push_outbox_pair", "feature_push_outbox"),
        ("ix_feature_push_outbox_key", "feature_push_outbox"),
        ("ix_feature_push_outbox_status", "feature_push_outbox"),
    ]:
        if _has_table(tbl):
            _safe_drop_index(idx, tbl)

    for table in ["feature_parity_audit", "feature_push_audit", "feature_push_outbox"]:
        if _has_table(table):
            op.drop_table(table)
