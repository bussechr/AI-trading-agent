"""Bind persisted market ticks to an authenticated broker/terminal source.

Revision ID: 20260803_0007
Revises: 20260408_0006
Create Date: 2026-08-03 00:07:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260803_0007"
down_revision = "20260408_0006"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return bool(sa.inspect(op.get_bind()).has_table(name))


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {
        str(column.get("name") or "") for column in inspector.get_columns(table_name)
    }


def _safe_add_column(table_name: str, column: sa.Column) -> None:
    if _has_table(table_name) and not _has_column(table_name, str(column.name)):
        op.add_column(table_name, column)


def _safe_drop_column(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and _has_column(table_name, column_name):
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
        "market_ticks",
        sa.Column("market_source_schema", sa.String(length=96), nullable=True),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column("market_source_id", sa.String(length=64), nullable=True),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column(
            "market_source_authenticated",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column("broker_account_scope", sa.String(length=128), nullable=True),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column("broker_venue_id", sa.String(length=64), nullable=True),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column("producer_identity", sa.String(length=128), nullable=True),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column("terminal_lease_scope", sa.String(length=128), nullable=True),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column("credential_generation_id", sa.String(length=128), nullable=True),
    )
    _safe_add_column(
        "market_ticks",
        sa.Column("bridge_protocol_version", sa.String(length=32), nullable=True),
    )
    _safe_create_index(
        "ix_market_ticks_source_symbol_ts",
        "market_ticks",
        ["market_source_id", "symbol", "ts"],
    )


def downgrade() -> None:
    _safe_drop_index("ix_market_ticks_source_symbol_ts", "market_ticks")
    for column_name in (
        "bridge_protocol_version",
        "credential_generation_id",
        "terminal_lease_scope",
        "producer_identity",
        "broker_venue_id",
        "broker_account_scope",
        "market_source_authenticated",
        "market_source_id",
        "market_source_schema",
    ):
        _safe_drop_column("market_ticks", column_name)
