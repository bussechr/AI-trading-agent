"""Grouped read-only views over the flat ``Settings`` object.

``fxstack.settings.Settings`` is intentionally flat — every threshold lives at
the top level so a single env-var maps to a single attribute. That makes the
contract with shell scripts and ``_env.bat`` simple, but it leaves call sites
juggling ten parameters where the grouping is implicit ("everything starting
with ``capital_`` is one config").

This module surfaces those implicit groups as **frozen dataclasses**. They are
projections from the flat fields — no separate state, no env-var aliases of
their own. Old code keeps working (``settings.min_swing_prob``); new code can
ask for the group it actually needs (``settings.gates.min_swing_prob``).

Adding a new group:
1. Add a frozen ``@dataclass`` here with the fields you want exposed.
2. Add a ``@property`` on :class:`fxstack.settings.Settings` that constructs
   it from the flat attributes.
3. Do not duplicate validation here — the source of truth remains the
   pydantic ``Settings`` declaration.

Constructing a group is cheap (a handful of attribute reads + a dataclass
constructor); call ``settings.gates`` per cycle if you like.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GatesConfig:
    """Per-intent decision gates — probability hurdles and edge floors.

    These determine whether a signal is allowed to become an order. The
    runtime evaluates them before risk-cap checks, so a low-probability
    signal is rejected here before it ever reaches the risk kernel.
    """

    min_swing_prob: float
    min_entry_prob: float
    min_trade_prob: float
    min_expected_edge_bps: float
    min_expected_edge_rescue_margin_bps: float
    entry_hysteresis_margin_bps: float
    reversal_hysteresis_margin_bps: float
    reversal_failure_min_prob: float
    reversal_opportunity_min_prob: float
    max_entry_uncertainty: float
    uncertainty_threshold: float
    use_uncertainty_gate: bool
    blocked_entry_sessions_csv: str
    lifecycle_model_action_min_prob: float


@dataclass(frozen=True, slots=True)
class RiskCapsConfig:
    """Hard caps the risk kernel enforces on sizing and exposure.

    Distinct from ``GatesConfig`` (which is binary entry-or-not) and from
    ``CapitalGovernanceConfig`` (which is portfolio-level pauses). These are
    the bright lines that no single trade may cross.
    """

    max_drawdown_pct: float
    max_gross_exposure: float
    max_net_exposure: float
    max_pair_positions: int
    max_total_positions: int
    max_allowed_spread_bps: float
    max_new_entries_per_cycle: int
    max_partial_closes_per_position: int
    hard_time_stop_secs: float
    default_order_lots: float
    equity_lots_per_usd: float
    min_order_lots: float
    order_lot_step: float
    max_order_lots: float
    partial_close_cooldown_secs: float
    partial_close_fraction: float


@dataclass(frozen=True, slots=True)
class CapitalGovernanceConfig:
    """Capital-band governance: pause, entries-only, budget scaling.

    Aggregates the ``capital_*`` thresholds. The governance evaluator (in
    ``fxstack.runtime.governance``) consumes a snapshot of this each cycle
    to decide whether to pause trading or scale down budgets.
    """

    enabled: bool
    band_mode: str
    entries_only: bool
    max_calibration_drift: float
    max_concentration_share: float
    max_drawdown_full_risk_pct: float
    max_drawdown_low_risk_pct: float
    max_drawdown_micro_live_pct: float
    max_latency_breach_count: int
    max_operational_fault_count: int
    max_realized_corr_share: float
    max_stale_feature_count: int
    max_tail_loss_pct: float
    min_shadow_alignment_share: float
    rollout_budget_scale_full_risk: float
    rollout_budget_scale_low_risk: float
    rollout_budget_scale_micro_live: float


@dataclass(frozen=True, slots=True)
class FeastConfig:
    """Online feature-serving and feature-push configuration."""

    enabled: bool
    online_latency_budget_ms: float
    online_stale_secs: float
    repo_root: str
    parity_tolerance: float
    push_backlog_warn: int
    push_batch_size: int
    push_claim_timeout_secs: float
    push_enabled: bool
    push_max_retries: int
    push_worker_id: str


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    """Orchestration agent runtime knobs (mode, durability, allowlists)."""

    mode: str
    runtime: str
    durability: str
    enable_otel: bool
    otel_exporter: str
    allow_external_tools: bool
    allow_remote_llm: bool
    require_human_approval: bool
    decision_timeout_ms: int
    max_node_ms: int
    max_parallel_proposals: int
    trace_retention_days: int
    live_intent_allowlist_csv: str
    live_pair_allowlist_csv: str
    live_sleeve_allowlist_csv: str
    paper_intent_allowlist_csv: str
    paper_pair_allowlist_csv: str
    paper_sleeve_allowlist_csv: str
    shadow_pair_allowlist_csv: str


@dataclass(frozen=True, slots=True)
class RLConfig:
    """Reinforcement-learning runtime configuration."""

    artifact_root: str
    online_worker_count: int
    stress_root: str
    supervised_fallback_required: bool
    transition_dataset_root: str


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    """Bridge HTTP API + EA-facing knobs.

    The values flow out through ``GET /v2/handshake`` so the EA and dashboard
    can pick them up; the bridge is the Python-side source of truth.
    """

    api_key: str
    auth_required: bool
    stale_heartbeat_secs: float
    stale_tick_secs: float
    basket_tp_pct: float
    command_ttl_secs: float


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    """Portfolio allocation and correlation configuration."""

    corr_mode: str
    realized_corr_max_age_secs: float
    realized_corr_min_obs: int
    realized_corr_window_bars: int
    use_portfolio_ranking: bool
    enable_pair_quality_prior: bool


@dataclass(frozen=True, slots=True)
class CanaryConfig:
    """Phase 6B canary-deployment overhead and entry-quality gates."""

    p95_overhead_ms: float
    p99_overhead_ms: float
    ack_success_floor: float
    orphan_command_limit: int
    entry_ratio_floor: float
    slot_utilisation_floor: float
    drawdown_deterioration_pct: float
    ramp_steps_pct: tuple[int, ...]
    alert_window_minutes: int


__all__ = [
    "AgentRuntimeConfig",
    "BridgeConfig",
    "CanaryConfig",
    "CapitalGovernanceConfig",
    "FeastConfig",
    "GatesConfig",
    "PortfolioConfig",
    "RLConfig",
    "RiskCapsConfig",
]
