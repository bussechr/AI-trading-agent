from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from fxstack.orchestration.contracts import AgentProposal, DecisionContext, VersionBundle
from fxstack.orchestration.governor import enrich_proposal_scores, govern_shadow
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION


def _context(*, reasons: list[str] | None = None) -> DecisionContext:
    return DecisionContext(
        run_id=UUID("00000000-0000-0000-0000-000000000100"),
        cycle_id="cycle-1",
        thread_id="EURUSD:cycle-1:shadow",
        correlation_id="EURUSD:cycle-1:shadow",
        ts_utc=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
        pair="EURUSD",
        runtime_mode="shadow",
        tick={"spread_bps": 1.2},
        feature_refs={},
        live_signal={"expected_edge_bps": 5.0, "uncertainty_score": 0.1},
        policy_state={"reasons": list(reasons or []), "adaptive_playbook": "trend_pullback", "max_allowed_spread_bps": 3.0},
        portfolio_state={"replacement_pressure": 0.1},
        risk_envelope={},
        runtime_state={"pair_tier": "tier1"},
        version_bundle=VersionBundle(
            schema_version=ORCHESTRATION_SCHEMA_VERSION,
            policy_version="fxstack_policy_v1",
            model_bundle_version="bundle-v1",
            orchestrator_version="orchestration.phase4.v1",
        ),
    )


def _proposal(
    *,
    proposal_id: str,
    agent_id: str,
    intent: str,
    side: str,
    confidence: float,
    expected_edge_bps: float,
    uncertainty: float,
    proposal_role: str,
    blocking_reasons: list[str] | None = None,
    score_components: dict[str, float] | None = None,
) -> AgentProposal:
    return AgentProposal(
        proposal_id=UUID(proposal_id),
        run_id=UUID("00000000-0000-0000-0000-000000000100"),
        agent_id=agent_id,
        phase="committee",
        intent=intent,
        side=side,
        confidence=confidence,
        expected_edge_bps=expected_edge_bps,
        uncertainty=uncertainty,
        risk_cost=0.0,
        ttl_ms=250,
        evidence_refs=["unit://proposal"],
        constraints={"playbook": "trend_pullback"},
        proposal_role=proposal_role,
        score_components=dict(score_components or {}),
        blocking_reasons=list(blocking_reasons or []),
        rationale=f"{agent_id} rationale",
    )


@given(block_reason=st.text(min_size=1, max_size=12), edge=st.floats(min_value=0.5, max_value=10.0))
def test_hard_policy_block_always_suppresses_command_emission(block_reason: str, edge: float) -> None:
    context = _context(reasons=[block_reason])
    ranked = enrich_proposal_scores(
        context=context,
        proposals=[
            _proposal(
                proposal_id="00000000-0000-0000-0000-000000000101",
                agent_id="committee.trend_pullback",
                intent="enter",
                side="BUY",
                confidence=0.8,
                expected_edge_bps=edge,
                uncertainty=0.1,
                proposal_role="playbook_entry",
            )
        ],
    )
    outcome = govern_shadow(
        context=context,
        baseline_action={"action": "enter", "side": "BUY", "blocking_reasons": [block_reason]},
        ranked_proposals=ranked,
        summary_proposals={},
    )
    assert outcome.allowed is False
    assert outcome.selected_action == "no_trade"
    assert outcome.invariant_results["hard_policy_block_suppresses_command"] is True


@given(exit_edge=st.floats(min_value=0.0, max_value=1.0), entry_edge=st.floats(min_value=1.0, max_value=25.0))
def test_exit_outranks_entry_when_same_cycle_has_both(exit_edge: float, entry_edge: float) -> None:
    context = _context()
    ranked = enrich_proposal_scores(
        context=context,
        proposals=[
            _proposal(
                proposal_id="00000000-0000-0000-0000-000000000102",
                agent_id="committee.reversal_exit",
                intent="exit",
                side="BUY",
                confidence=0.7,
                expected_edge_bps=exit_edge,
                uncertainty=0.2,
                proposal_role="lifecycle_exit",
                score_components={"exit_priority_bonus": 100.0, "spread_penalty": 0.0, "portfolio_penalty": 0.0, "uncertainty_penalty": 2.0},
            ),
            _proposal(
                proposal_id="00000000-0000-0000-0000-000000000103",
                agent_id="committee.breakout_expansion",
                intent="enter",
                side="BUY",
                confidence=0.95,
                expected_edge_bps=entry_edge,
                uncertainty=0.01,
                proposal_role="playbook_entry",
            ),
        ],
    )
    outcome = govern_shadow(
        context=context,
        baseline_action={"action": "enter", "side": "BUY"},
        ranked_proposals=ranked,
        summary_proposals={},
    )
    assert outcome.selected_action in {"exit", "reduce"}
    assert outcome.invariant_results["exit_outranks_entry_same_cycle"] is True


@given(edge=st.floats(min_value=1.0, max_value=15.0), uncertainty=st.floats(min_value=0.0, max_value=0.5))
def test_identical_inputs_produce_same_winner_and_score_path(edge: float, uncertainty: float) -> None:
    context = _context()
    proposals = [
        _proposal(
            proposal_id="00000000-0000-0000-0000-000000000104",
            agent_id="committee.trend_pullback",
            intent="enter",
            side="BUY",
            confidence=0.8,
            expected_edge_bps=edge,
            uncertainty=uncertainty,
            proposal_role="playbook_entry",
        ),
        _proposal(
            proposal_id="00000000-0000-0000-0000-000000000105",
            agent_id="committee.execution_quality",
            intent="enter",
            side="BUY",
            confidence=0.75,
            expected_edge_bps=edge - 0.25,
            uncertainty=uncertainty + 0.02,
            proposal_role="execution_quality",
        ),
    ]
    first = govern_shadow(
        context=context,
        baseline_action={"action": "enter", "side": "BUY"},
        ranked_proposals=enrich_proposal_scores(context=context, proposals=proposals),
        summary_proposals={},
    )
    second = govern_shadow(
        context=context,
        baseline_action={"action": "enter", "side": "BUY"},
        ranked_proposals=enrich_proposal_scores(context=context, proposals=proposals),
        summary_proposals={},
    )
    assert [row["proposal_id"] for row in first.score_path] == [row["proposal_id"] for row in second.score_path]
    assert (first.winning_proposal.proposal_id if first.winning_proposal else "") == (
        second.winning_proposal.proposal_id if second.winning_proposal else ""
    )


@given(
    lower_spread=st.floats(min_value=0.0, max_value=1.0),
    higher_spread=st.floats(min_value=1.1, max_value=3.0),
)
def test_tie_break_prefers_lower_spread_when_scores_otherwise_equal(lower_spread: float, higher_spread: float) -> None:
    context = _context()
    ranked = enrich_proposal_scores(
        context=context,
        proposals=[
            _proposal(
                proposal_id="00000000-0000-0000-0000-000000000106",
                agent_id="committee.trend_pullback",
                intent="enter",
                side="BUY",
                confidence=0.8,
                expected_edge_bps=5.0,
                uncertainty=0.1,
                proposal_role="playbook_entry",
                score_components={"spread_penalty": lower_spread, "portfolio_penalty": 0.0, "uncertainty_penalty": 1.0, "exit_priority_bonus": 0.0},
            ),
            _proposal(
                proposal_id="00000000-0000-0000-0000-000000000107",
                agent_id="committee.range_mean_reversion",
                intent="enter",
                side="BUY",
                confidence=0.8,
                expected_edge_bps=5.0,
                uncertainty=0.1,
                proposal_role="playbook_entry",
                score_components={"spread_penalty": higher_spread, "portfolio_penalty": 0.0, "uncertainty_penalty": 1.0, "exit_priority_bonus": 0.0},
            ),
        ],
    )
    assert str(ranked[0].proposal_id) == "00000000-0000-0000-0000-000000000106"
