# AGENT: ROLE: Dependency-free playbook, sleeve, and campaign state identities.
# AGENT: CALLED BY: adaptive policy, allocator, campaign, orchestration, managed state, and runtime.
# AGENT: STATE / SIDE EFFECTS: immutable names plus pure sleeve normalization.
# AGENT: SEE: `docs/agents/model-stack-and-feature-flow.md` -> `docs/agents/runtime-loop.md`
"""Lightweight shared playbook and campaign state identities."""

from __future__ import annotations


PLAYBOOK_TREND_PULLBACK = "trend_pullback"
PLAYBOOK_RANGE_MEAN_REVERSION = "range_mean_reversion"
PLAYBOOK_BREAKOUT_EXPANSION = "breakout_expansion"
PLAYBOOK_FAILED_BREAKOUT_REVERSAL = "failed_breakout_reversal"
PLAYBOOK_NO_TRADE = "no_trade"

CAMPAIGN_STATE_INACTIVE = "inactive"
CAMPAIGN_STATE_PROBE = "probe"
CAMPAIGN_STATE_CONFIRMED = "confirmed"
CAMPAIGN_STATE_PRESS = "press"
CAMPAIGN_STATE_HARVEST = "harvest"
CAMPAIGN_STATE_REATTACK_READY = "re_attack_ready"
CAMPAIGN_STATE_ABANDONED = "abandoned"

CAMPAIGN_ENABLED_SLEEVES = {
    PLAYBOOK_TREND_PULLBACK,
    PLAYBOOK_RANGE_MEAN_REVERSION,
    PLAYBOOK_BREAKOUT_EXPANSION,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
}
CAMPAIGN_ACTIVE_STATES = {
    CAMPAIGN_STATE_PROBE,
    CAMPAIGN_STATE_CONFIRMED,
    CAMPAIGN_STATE_PRESS,
    CAMPAIGN_STATE_HARVEST,
}
CAMPAIGN_MEMORY_STATES = {
    CAMPAIGN_STATE_INACTIVE,
    CAMPAIGN_STATE_REATTACK_READY,
    CAMPAIGN_STATE_ABANDONED,
}


def playbook_to_sleeve(playbook: str) -> str:
    """Return the canonical one-to-one sleeve identity for a playbook."""

    return str(playbook or "").strip() or PLAYBOOK_NO_TRADE
