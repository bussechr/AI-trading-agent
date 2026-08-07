"""Persist typed execution-ACK safety evidence on command rows.

Revision ID: 20260803_0009
Revises: 20260803_0008
Create Date: 2026-08-03 00:09:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260803_0009"
down_revision = "20260803_0008"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return bool(sa.inspect(op.get_bind()).has_table(name))


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return column_name in {
        str(column.get("name") or "")
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _safe_add_column(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, str(column.name)):
        op.add_column(table_name, column)


def _safe_drop_column(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name)


def _safe_create_index(name: str, table_name: str, columns: list[str]) -> None:
    if not _has_table(table_name):
        return
    existing = {
        str(index.get("name") or "")
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    if name not in existing:
        op.create_index(name, table_name, columns, unique=False)


def _safe_drop_index(name: str, table_name: str) -> None:
    if not _has_table(table_name):
        return
    existing = {
        str(index.get("name") or "")
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }
    if name in existing:
        op.drop_index(name, table_name=table_name)


def upgrade() -> None:
    _safe_add_column(
        "commands",
        sa.Column("ack_policy_scope", sa.String(length=64), nullable=True),
    )
    _safe_add_column(
        "commands",
        sa.Column("ack_attestation_schema", sa.String(length=96), nullable=True),
    )
    _safe_add_column(
        "commands",
        sa.Column(
            "ack_terminal_safe",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    _safe_add_column(
        "commands",
        sa.Column("ack_ticket", sa.Integer(), nullable=True),
    )
    _safe_add_column(
        "commands",
        sa.Column("ack_mutation_state", sa.String(length=32), nullable=True),
    )
    _safe_create_index(
        "ix_commands_status_ack_terminal_safe",
        "commands",
        ["status", "ack_terminal_safe"],
    )


def downgrade() -> None:
    _safe_drop_index("ix_commands_status_ack_terminal_safe", "commands")
    for column_name in (
        "ack_mutation_state",
        "ack_ticket",
        "ack_terminal_safe",
        "ack_attestation_schema",
        "ack_policy_scope",
    ):
        _safe_drop_column("commands", column_name)
