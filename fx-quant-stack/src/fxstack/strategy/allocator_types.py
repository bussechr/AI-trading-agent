# AGENT: ROLE: Typed allocator and sleeve-governance records shared by twin replay and live runtime.
# AGENT: ENTRYPOINT: imported by `fxstack/strategy/allocator.py`, `fxstack/strategy/sleeve_governance.py`, twin, and runtime.
# AGENT: PRIMARY INPUTS: candidate diagnostics, open-position keep scores, rolling sleeve metrics.
# AGENT: PRIMARY OUTPUTS: stable dataclass contracts for ranking, replacement, and telemetry.
# AGENT: DEPENDS ON: stdlib dataclasses and typing only.
# AGENT: CALLED BY: `fxstack/strategy/allocator.py`, `fxstack/strategy/sleeve_governance.py`, `tools/fxstack_digital_twin_backtest.py`, `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: pure data definitions only.
# AGENT: HANDSHAKES: runtime/twin allocator telemetry contract.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `fxstack/strategy/allocator.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SleeveHealthSnapshot:
    sleeve: str
    score: float = 0.5
    state: str = "healthy"
    trades: int = 0
    win_rate: float = 0.0
    expectancy_usd: float = 0.0
    profit_factor: float = 1.0
    avg_holding_bars: float = 0.0
    partial_frequency: float = 0.0
    replacement_exit_share: float = 0.0
    drawdown_contribution_usd: float = 0.0
    live_shadow_divergence_rate: float = 0.0
    session_pnl_mix: dict[str, float] = field(default_factory=dict)
    pair_contribution: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class AllocatorConfig:
    max_total_positions: int
    max_pair_positions: int
    max_new_entries: int
    max_spread_bps: float
    min_expected_edge_bps: float
    replacement_margin: float = 0.06
    tempo_gap_replacement_margin: float = 0.03
    protected_hold_window_bars: float = 3.0


@dataclass(slots=True)
class AllocatorCandidate:
    candidate_id: str
    index: int
    pair: str
    ts: str
    side: str
    sleeve: str
    environment_state: str
    session_bucket: str
    baseline_allowed: bool
    adaptive_allowed: bool
    playbook_score: float
    location_score: float
    trigger_score: float
    adaptive_entry_quality: float
    expected_edge_bps: float
    uncertainty_score: float
    spread_bps: float
    max_spread_bps: float
    macro_coherence_score: float
    currency_crowding_penalty: float
    playbook_diversification_penalty: float
    sleeve_health_score: float = 0.5
    sleeve_health_state: str = "healthy"
    thesis_id: str = ""
    campaign_seq: int = 0
    campaign_entry_kind: str = ""
    campaign_state: str = "inactive"
    campaign_state_reason: str = ""
    campaign_priority_boost: float = 0.0
    campaign_proof_score: float = 0.0
    campaign_maturity_score: float = 0.0
    campaign_reset_quality: float = 0.0
    campaign_reentry_blocked: bool = False
    conviction_score: float = 0.0
    conviction_band: str = "blocked"
    thesis_stage: str = "stand_down"
    portfolio_posture: str = "balanced_probe"
    replacement_urgency: float = 0.0
    portfolio_pair_pressure: float = 0.0
    portfolio_session_pressure: float = 0.0
    portfolio_sleeve_pressure: float = 0.0
    portfolio_correlation_pressure: float = 0.0
    portfolio_risk_pressure: float = 0.0
    sleeve_budget_target: int = 0
    sleeve_budget_used: int = 0
    sleeve_budget_pressure: float = 0.0
    cross_pair_rank_position: int = 0
    cross_pair_influence_score: float = 0.5
    cross_pair_recommendation_strength: float = 0.5
    cross_pair_soft_block: bool = False
    cross_pair_hard_block: bool = False
    cross_pair_influenced_by_pairs: list[str] = field(default_factory=list)
    cross_pair_reason_codes: list[str] = field(default_factory=list)
    replacement_pressure_score: float = 0.0
    spread_cost_penalty: float = 0.0
    allocator_score: float = 0.0
    allocator_rank: int | None = None
    allocator_selected: bool = False
    allocator_rejection_reason: str = ""
    replacement_value: float = 0.0
    replacement_target_pair: str = ""
    numeric_inputs_valid: bool = True
    numeric_input_errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AllocatorOpenPosition:
    position_id: str
    pair: str
    side: str
    sleeve: str
    session_bucket: str
    keep_score: float
    age_bars: float
    protected_hold: bool
    replaceable_hold: bool
    thesis_id: str = ""
    campaign_seq: int = 0
    campaign_entry_kind: str = ""
    campaign_state: str = "inactive"
    exposure_crowding_burden: float = 0.0
    macro_coherence_decay: float = 0.0
    rolling_profit_decay: float = 0.0
    thesis_stage: str = "core"
    replacement_urgency: float = 0.0


@dataclass(slots=True)
class AllocatorSelection:
    candidate_id: str
    selected: bool
    rank: int | None
    rejection_reason: str
    replacement_target_pair: str = ""
    replacement_value: float = 0.0


@dataclass(slots=True)
class AllocatorCycleSummary:
    candidate_count: int = 0
    selected_count: int = 0
    ranked_out_count: int = 0
    replacement_exit_count: int = 0
    replacement_candidate_count: int = 0
    remaining_slots: int = 0
    weakest_keep_score: float = 0.0
    replacement_margin: float = 0.0
    sleeve_candidate_counts: dict[str, int] = field(default_factory=dict)
    sleeve_selected_counts: dict[str, int] = field(default_factory=dict)
    sleeve_budget_targets: dict[str, int] = field(default_factory=dict)
    sleeve_budget_used: dict[str, int] = field(default_factory=dict)
    campaign_state_counts: dict[str, int] = field(default_factory=dict)
    rejection_counts: dict[str, int] = field(default_factory=dict)
    pair_pressure_avg: float = 0.0
    pair_pressure_max: float = 0.0
    session_pressure_avg: float = 0.0
    session_pressure_max: float = 0.0
    sleeve_pressure_avg: float = 0.0
    sleeve_pressure_max: float = 0.0
    correlation_pressure_avg: float = 0.0
    correlation_pressure_max: float = 0.0
    risk_pressure_avg: float = 0.0
    risk_pressure_max: float = 0.0
