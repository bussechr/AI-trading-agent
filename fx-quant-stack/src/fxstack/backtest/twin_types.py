from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TwinRunConfig:
    twin_version: str
    policy_version: str
    pairs: list[str]
    feature_root: str
    start_ts: str
    end_ts: str
    start_equity: float
    slippage_bps: float
    validate_live_overlap: bool
    validation_limit: int
    emit_decision_history: bool
    max_decision_history_rows: int
    recommendations: bool
    exec_mode: str = "strict_live_mirror"
    adaptive_compare_baseline: bool = True
    adaptive_playbooks: list[str] = field(default_factory=list)
    adaptive_entry_ratio_floor: float = 0.90
    adaptive_entry_ratio_cap: float = 1.35
    adaptive_slot_util_floor: float = 0.90
    adaptive_slot_util_cap: float = 1.20
    adaptive_aggressive_fallback_margin: float = 0.08
    adaptive_use_risk_multipliers: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TwinValidationResult:
    status: str
    compared_rows: int
    exact_match_rate: float
    side_match_rate: float
    allowed_match_rate: float
    rejection_reason_match_rate: float
    lifecycle_action_match_rate: float
    mismatch_reasons: dict[str, int] = field(default_factory=dict)
    mismatch_examples: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class TwinDecisionRecord:
    pair: str
    ts: str
    side: str
    allowed: bool
    rejection_reason: str
    rejection_reasons: list[str]
    expected_edge_bps: float
    spread_bps: float
    regime_prob: float
    swing_prob: float
    entry_prob: float
    trade_prob: float
    uncertainty_score: float
    model_disagreement_score: float
    directional_swing_confidence: float
    entry_margin: float
    meta_margin: float
    session_bucket: str
    session_entry_blocked: bool
    session_entry_block_reason: str
    htf_alignment_score: float
    pullback_quality_score: float
    resume_trigger_score: float
    extension_penalty_score: float
    structure_timing_score: float
    structure_bonus_bps: float
    chase_penalty_bps: float
    calibrated_ev_bps_shadow: float
    entry_quality_score_shadow: float
    structure_rescue_active: bool
    shadow_floor_ok: bool
    shadow_floor_rejection_reason: str
    portfolio_rank_shadow: int | None
    shadow_would_trade: bool
    shadow_rejection_reason: str
    pair_tier: str
    position_side: str
    position_count_pair: int
    total_open_positions: int
    lifecycle_action: str
    lifecycle_reason: str
    exit_action_selected: str
    reversal_context_active: bool
    reversal_ready: bool
    reversal_failure_prob: float
    reversal_opportunity_prob: float
    baseline_allowed: bool = False
    baseline_rejection_reason: str = "none"
    exec_mode: str = "strict_live_mirror"
    environment_state: str = ""
    trend_persistence_score: float = 0.0
    compression_score: float = 0.0
    expansion_score: float = 0.0
    range_score: float = 0.0
    hostility_score: float = 0.0
    macro_coherence_score: float = 0.0
    pair_strength_score: float = 0.0
    playbook: str = ""
    playbook_score: float = 0.0
    location_score: float = 0.0
    trigger_score: float = 0.0
    adaptive_entry_quality: float = 0.0
    currency_crowding_penalty: float = 0.0
    playbook_diversification_penalty: float = 0.0
    aggressive_fallback_used: bool = False
    adaptive_allowed: bool = False
    adaptive_rejection_reason: str = ""
    scenario_bucket: str = ""
    regime_bucket: str = ""


@dataclass(slots=True)
class TwinRecommendation:
    category: str
    severity: str
    finding: str
    evidence: list[str]
    proposed_change: str
    validation_plan: str


@dataclass(slots=True)
class TwinAggregateMetrics:
    run_status: str
    start_equity_usd: float
    end_equity_usd: float
    total_return_pct: float
    net_pnl_usd: float
    trades: int
    entries: int
    wins: int
    losses: int
    flats: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    max_drawdown_usd: float
    max_drawdown_duration_bars: int
    ulcer_index: float
    sharpe_like: float
    recovery_factor: float
    avg_open_positions: float
    peak_open_positions: int
    slot_utilization_rate: float
    expectancy_per_trade_usd: float
    partial_exit_events: int
    reversal_exit_events: int
    forced_final_close_share: float
    rejection_counts: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TwinOpenPosition:
    pair: str
    side: str
    lots: float
    entry_lots: float
    entry_price: float
    open_ts: str
    open_equity_usd: float
    entry_trade_prob: float
    entry_session_bucket: str
    entry_scenario_bucket: str
    entry_regime_bucket: str
    entry_uncertainty_score: float
    entry_structure_timing_score: float
    pair_tier: str
    playbook: str = "trend_pullback"
    environment_state_at_entry: str = ""
    entry_location_score: float = 0.0
    entry_trigger_score: float = 0.0
    entry_macro_coherence_score: float = 0.0
    aggressive_fallback_used: bool = False
    partial_count: int = 0
    last_partial_bar_index: int | None = None
    realized_pnl_usd: float = 0.0
    partial_exit_events: int = 0


@dataclass(slots=True)
class TwinClosedTrade:
    pair: str
    side: str
    open_ts: str
    close_ts: str
    entry_price: float
    exit_price: float
    lots: float
    realized_pnl_usd: float
    holding_bars: int
    partial_exit_events: int
    close_reason: str
    entry_trade_prob: float
    exit_action_selected: str
    reversal_failure_prob: float
    reversal_opportunity_prob: float
    entry_session_bucket: str
    entry_scenario_bucket: str
    entry_regime_bucket: str
    entry_uncertainty_score: float
    entry_structure_timing_score: float
    pair_tier: str
    playbook: str = "trend_pullback"
    environment_state_at_entry: str = ""
    environment_state_at_exit: str = ""
    lifecycle_exit_reason: str = ""
    aggressive_fallback_used: bool = False
