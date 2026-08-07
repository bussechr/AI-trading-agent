"""Dependency-free identities shared by risk and runtime admission."""

from __future__ import annotations


# Permission to execute and entry-budget throttling are separate contracts:
# graduated ``live`` remains executable but no longer receives canary sizing.
ROLLOUT_EXECUTION_MODES = frozenset({"canary", "live"})
ROLLOUT_BUDGET_THROTTLED_MODES = frozenset({"canary"})
