"""Lifecycle/ops schema bridge revision placeholder.

Revision ID: 20260318_0002
Revises: 20260317_0001
Create Date: 2026-03-18 00:02:00
"""
from __future__ import annotations


revision = "20260318_0002"
down_revision = "20260317_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op by design.
    #
    # Some deployed databases were stamped to this revision while tables were
    # created idempotently by runtime bootstrap code. Keeping a no-op revision
    # here restores Alembic chain continuity and allows `upgrade head` to run.
    return


def downgrade() -> None:
    # No-op to match upgrade behavior.
    return

