# AGENT: ROLE: Typed campaign-manager records shared by twin replay and live runtime thesis sequencing.
# AGENT: ENTRYPOINT: imported by `fxstack/strategy/campaign.py`, twin replay, and runtime adaptive paths.
# AGENT: PRIMARY INPUTS: pair/side/sleeve identifiers, lifecycle diagnostics, campaign registry state.
# AGENT: PRIMARY OUTPUTS: stable dataclass contracts for thesis state, registry entries, and transition events.
# AGENT: DEPENDS ON: stdlib dataclasses and typing only.
# AGENT: CALLED BY: `fxstack/strategy/campaign.py`, `tools/fxstack_digital_twin_backtest.py`, `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: pure data definitions only.
# AGENT: HANDSHAKES: runtime/twin campaign telemetry and registry contract.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `fxstack/strategy/campaign.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


CampaignState = Literal[
    "inactive",
    "probe",
    "confirmed",
    "press",
    "harvest",
    "re_attack_ready",
    "abandoned",
]


@dataclass(slots=True)
class CampaignConfig:
    enabled: bool = False
    shadow_only: bool = True
    abandon_cooldown_bars: int = 8
    press_protected_bars: int = 4
    reattack_cooldown_scale: float = 0.5


@dataclass(slots=True)
class CampaignSnapshot:
    thesis_id: str
    pair: str
    side: str
    sleeve: str
    campaign_seq: int = 0
    entry_kind: str = ""
    state: str = "inactive"
    state_reason: str = ""
    proof_score: float = 0.0
    maturity_score: float = 0.0
    reset_quality: float = 0.0
    abandon_score: float = 0.0
    priority_boost: float = 0.0
    reentry_blocked: bool = False
    reentry_block_reason: str = ""
    keep_adjustment: float = 0.0
    replacement_margin_delta: float = 0.0
    press_protected: bool = False


@dataclass(slots=True)
class CampaignTransition:
    thesis_id: str
    pair: str
    side: str
    sleeve: str
    prior_state: str
    new_state: str
    reason: str
    bar_idx: int
    ts: str
    campaign_seq: int = 0
    entry_kind: str = ""
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    holding_bars: float = 0.0
    trade_id: str = ""


@dataclass(slots=True)
class CampaignDecisionContext:
    pair: str
    side: str
    sleeve: str
    bar_idx: int
    ts: str
    playbook_score: float = 0.0
    location_score: float = 0.0
    trigger_score: float = 0.0
    macro_coherence_score: float = 0.0
    hostility_score: float = 0.0
    extension_penalty_score: float = 0.0
    environment_state: str = ""
    entry_trade_prob: float = 0.0
    unrealized_pnl_usd: float = 0.0
    age_bars: float = 0.0
    open_equity_usd: float = 0.0
    lifecycle_action: str = "hold"
    lifecycle_reason: str = "hold"
    reversal_ready: bool = False
    severe_invalidation: bool = False


@dataclass(slots=True)
class CampaignRegistryEntry:
    thesis_id: str
    pair: str
    side: str
    sleeve: str
    campaign_seq: int = 0
    campaign_active: bool = False
    campaign_start_bar: int = -1
    campaign_start_ts: str = ""
    entry_kind: str = ""
    completed_count: int = 0
    state: str = "inactive"
    state_reason: str = ""
    last_bar_idx: int = -1
    state_entered_bar: int = -1
    last_ts: str = ""
    active_position: bool = False
    harvest_count: int = 0
    reattack_count: int = 0
    abandoned_at_bar: int | None = None
    last_close_reason: str = ""
    last_realized_pnl_usd: float = 0.0
    last_campaign_pnl_usd: float = 0.0
