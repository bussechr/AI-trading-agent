from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fxstack.orchestration.agents.base import AgentInputs
from fxstack.orchestration.agents.committee import (
    BreakoutExpansionAgent,
    ExecutionQualityAgent,
    ReversalExitAgent,
    SpreadMicrostructureAgent,
)
from fxstack.orchestration.contracts import DecisionContext, VersionBundle
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION


def _context(*, pair: str = "EURUSD", live_signal=None, policy_state=None, portfolio_state=None, tick=None) -> DecisionContext:
    return DecisionContext(
        run_id=UUID("00000000-0000-0000-0000-000000000200"),
        cycle_id="cycle-scenario",
        thread_id=f"{pair}:cycle-scenario:shadow",
        correlation_id=f"{pair}:cycle-scenario:shadow",
        ts_utc=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
        pair=pair,
        runtime_mode="shadow",
        tick=dict(tick or {"spread_bps": 1.2}),
        feature_refs={},
        live_signal=dict(live_signal or {"expected_edge_bps": 4.0, "uncertainty_score": 0.1}),
        policy_state=dict(policy_state or {}),
        portfolio_state=dict(portfolio_state or {"replacement_pressure": 0.1}),
        risk_envelope={},
        runtime_state={"pair_tier": "tier1"},
        version_bundle=VersionBundle(
            schema_version=ORCHESTRATION_SCHEMA_VERSION,
            policy_version="fxstack_policy_v1",
            model_bundle_version="bundle-v1",
            orchestrator_version="orchestration.phase4.v1",
        ),
    )


def test_breakout_expansion_scenario_marks_entry_on_volatility_expansion() -> None:
    inputs = AgentInputs(
        context=_context(
            policy_state={
                "adaptive_playbook": "breakout_expansion",
                "adaptive_playbook_score": 0.82,
                "adaptive_location_score": 0.71,
                "adaptive_trigger_score": 0.76,
            }
        ),
        baseline_action={"action": "enter", "side": "BUY"},
    )
    proposal = BreakoutExpansionAgent().propose(inputs)
    assert proposal.intent == "enter"
    assert proposal.proposal_role == "playbook_entry"


def test_spread_widening_scenario_blocks_entry() -> None:
    inputs = AgentInputs(
        context=_context(
            tick={"spread_bps": 5.0},
            policy_state={"max_allowed_spread_bps": 2.5},
        ),
        baseline_action={"action": "enter", "side": "BUY"},
    )
    proposal = SpreadMicrostructureAgent().propose(inputs)
    assert proposal.intent == "no_trade"
    assert "spread_too_wide" in proposal.blocking_reasons


def test_missing_data_scenario_falls_back_to_no_trade_for_execution_quality() -> None:
    inputs = AgentInputs(
        context=_context(
            live_signal={},
            policy_state={"entry_margin": -0.1, "meta_margin": 0.0},
        ),
        baseline_action={"action": "enter", "side": "BUY"},
    )
    proposal = ExecutionQualityAgent().propose(inputs)
    assert proposal.intent == "no_trade"
    assert "negative_execution_margin" in proposal.blocking_reasons


def test_reversal_exit_scenario_prefers_exit() -> None:
    inputs = AgentInputs(
        context=_context(
            policy_state={
                "position_open": True,
                "position_side": "BUY",
                "lifecycle_action": "exit",
                "lifecycle_reason": "adaptive_reverse_ready",
                "exit_action_score": 0.87,
                "reversal_should_exit": True,
            }
        ),
        baseline_action={"action": "hold", "side": "BUY"},
    )
    proposal = ReversalExitAgent().propose(inputs)
    assert proposal.intent == "exit"
    assert proposal.proposal_role == "lifecycle_exit"
