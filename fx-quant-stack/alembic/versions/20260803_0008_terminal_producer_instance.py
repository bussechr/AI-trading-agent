"""Bind authenticated market ticks to one durable MT4 terminal instance.

Revision ID: 20260803_0008
Revises: 20260803_0007
Create Date: 2026-08-03 00:08:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260803_0008"
down_revision = "20260803_0007"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return bool(sa.inspect(op.get_bind()).has_table(name))


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column_name in {
        str(column.get("name") or "") for column in inspector.get_columns(table_name)
    }


def upgrade() -> None:
    if _has_table("market_ticks") and not _has_column(
        "market_ticks", "producer_instance_id"
    ):
        op.add_column(
            "market_ticks",
            sa.Column("producer_instance_id", sa.String(length=128), nullable=True),
        )


def downgrade() -> None:
    if _has_table("market_ticks") and _has_column(
        "market_ticks", "producer_instance_id"
    ):
        op.drop_column("market_ticks", "producer_instance_id")
