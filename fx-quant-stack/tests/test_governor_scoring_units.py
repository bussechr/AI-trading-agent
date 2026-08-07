"""Pins that the governor's ranking keeps every term in basis points.

The defect this guards against was structural, not marginal. The score was

    edge_bps * confidence - uncertainty * 10

which subtracts incommensurable units: ``expected_edge_bps`` is basis points
(order 1-10 for FX intraday) while ``uncertainty`` is a 0-1 probability scaled
by an arbitrary 10. An abstaining proposal carries no edge and therefore scored
exactly 0.0, while any entry proposal had to overcome a ~3.4 penalty it could
not reach. Abstention won on every cycle regardless of the evidence.

Measured live on EURUSD before the fix: three ``enter`` proposals at 3.29 bps
scored -1.19, -1.49 and -1.69, all losing to a ``no_trade`` at 0.0.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest

from fxstack.orchestration.contracts import AgentProposal, DecisionContext, VersionBundle
from fxstack.orchestration.governor import enrich_proposal_scores


RUN_ID = uuid.UUID("7499eab3-6fd3-588e-9057-312ec952c40f")


#: The exact uncertainty observed on the live EURUSD cycle that exposed this.
LIVE_UNCERTAINTY = 0.3383730559436981
LIVE_EDGE_BPS = 3.28651597371074


def _context() -> DecisionContext:
    return DecisionContext(
        run_id=RUN_ID,
        cycle_id="1785478331882",
        thread_id="EURUSD:1785478331882:live",
        correlation_id="EURUSD:1785478331882:live",
        ts_utc=datetime(2026, 7, 31, 6, 12, 11, tzinfo=UTC),
        pair="EURUSD",
        runtime_mode="live",
        tick={"spread_bps": 0.52},
        feature_refs={},
        live_signal={},
        policy_state={"max_allowed_spread_bps": 3.0},
        portfolio_state={},
        risk_envelope={},
        runtime_state={},
        version_bundle=VersionBundle(
            schema_version="orchestration.phase2.v1",
            policy_version="fxstack_policy_v1",
            model_bundle_version="8d6d36ab-4bee-483a-8a2f-a32fdc86bdd4",
            orchestrator_version="orchestration.phase4.v1",
        ),
    )


def _proposal(
    *,
    agent_id: str,
    intent: str,
    edge_bps: float,
    confidence: float,
    uncertainty: float = LIVE_UNCERTAINTY,
) -> AgentProposal:
    return AgentProposal(
        proposal_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"{agent_id}:{intent}:{edge_bps}:{confidence}"),
        run_id=RUN_ID,
        agent_id=agent_id,
        phase="committee",
        intent=intent,
        side="SELL",
        confidence=float(confidence),
        expected_edge_bps=float(edge_bps),
        uncertainty=float(uncertainty),
        risk_cost=0.0,
        ttl_ms=1000,
        evidence_refs=[],
        constraints={},
    )


def _scored(proposals: list[AgentProposal]) -> list[AgentProposal]:
    return enrich_proposal_scores(context=_context(), proposals=proposals)


def test_a_real_edge_outranks_abstention():
    """The live case. An entry with genuine edge must beat doing nothing."""
    ranked = _scored(
        [
            _proposal(agent_id="committee.spread_microstructure", intent="no_trade", edge_bps=0.0, confidence=0.4527),
            _proposal(agent_id="committee.portfolio_risk", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.70),
        ]
    )
    assert ranked[0].intent == "enter", (
        "an abstention at zero edge must not outrank a 3.29 bps entry at 0.70 confidence"
    )
    assert ranked[0].normalized_score > 0.0


def test_uncertainty_can_never_drive_a_score_negative_on_its_own():
    """Uncertainty shrinks edge toward zero; it does not subtract past it."""
    ranked = _scored(
        [_proposal(agent_id="a", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.70, uncertainty=0.99)]
    )
    assert ranked[0].normalized_score >= 0.0
    # Total uncertainty removes the edge entirely rather than inverting it.
    certain = _scored(
        [_proposal(agent_id="a", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.70, uncertainty=1.0)]
    )
    assert certain[0].normalized_score == pytest.approx(0.0)


def test_abstention_remains_the_benchmark_to_beat():
    """Abstention scores exactly 0.0, so an entry needs positive edge to win.

    Note what this does and does not claim. The governor's ranking asks only
    "is there positive risk-adjusted edge?" -- a *tiny* positive edge does
    outrank abstention here, and correctly so, because economic sufficiency is
    not this function's job. Whether the edge covers the crossing is enforced
    upstream by the conjunctive cost gate in ``strategy/adaptive_policy.py``
    (``edge_below_cost_floor``), which requires a multiple of the round-trip
    spread. Duplicating that here would double-count it.
    """
    ranked = _scored(
        [
            _proposal(agent_id="hold", intent="no_trade", edge_bps=0.0, confidence=0.9),
            _proposal(agent_id="no_edge", intent="enter", edge_bps=0.0, confidence=0.95),
        ]
    )
    assert ranked[0].normalized_score == pytest.approx(0.0)
    assert all(p.normalized_score == pytest.approx(0.0) for p in ranked), (
        "an entry with no edge must not outscore doing nothing, however confident it is"
    )

    # And a real edge does clear it, by a margin that scales with the edge.
    cleared = _scored(
        [
            _proposal(agent_id="hold", intent="no_trade", edge_bps=0.0, confidence=0.9),
            _proposal(agent_id="real", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.70),
        ]
    )
    assert cleared[0].intent == "enter"
    assert cleared[0].normalized_score > 1.0


def test_score_is_monotone_in_edge_and_in_certainty():
    def score(edge: float, uncertainty: float) -> float:
        return _scored(
            [_proposal(agent_id="a", intent="enter", edge_bps=edge, confidence=0.7, uncertainty=uncertainty)]
        )[0].normalized_score

    assert score(6.0, 0.3) > score(3.0, 0.3), "more edge must score higher"
    assert score(3.0, 0.2) > score(3.0, 0.6), "less uncertainty must score higher"


def test_score_stays_in_basis_points():
    """The whole expression must remain commensurate with edge_bps.

    A 3.29 bps edge cannot produce a score of order 10 -- that was the symptom
    of mixing a probability-scaled penalty into a basis-point quantity.
    """
    ranked = _scored(
        [_proposal(agent_id="a", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.70)]
    )
    assert 0.0 < ranked[0].normalized_score <= LIVE_EDGE_BPS
    components = dict(ranked[0].score_components or {})
    assert components["risk_adjusted_edge_bps"] <= LIVE_EDGE_BPS
    assert 0.0 <= components["uncertainty_shrink"] <= 1.0


def test_exits_still_outrank_entries():
    """The exit priority bonus must survive the rescaling."""
    ranked = _scored(
        [
            _proposal(agent_id="entry", intent="enter", edge_bps=50.0, confidence=1.0, uncertainty=0.0),
            _proposal(agent_id="exit", intent="exit", edge_bps=0.0, confidence=0.1, uncertainty=0.9),
        ]
    )
    assert ranked[0].intent == "exit", "an exit must outrank any entry in the same cycle"


def test_the_exact_live_ranking_now_selects_enter():
    """Full replay of the committee set captured from the live EURUSD cycle."""
    ranked = _scored(
        [
            _proposal(agent_id="committee.trend_pullback", intent="hold", edge_bps=0.0, confidence=0.6076),
            _proposal(agent_id="committee.spread_microstructure", intent="no_trade", edge_bps=0.0, confidence=0.4527),
            _proposal(agent_id="committee.breakout_expansion", intent="hold", edge_bps=0.0, confidence=0.6076),
            _proposal(agent_id="committee.portfolio_risk", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.70),
            _proposal(agent_id="committee.range_mean_reversion", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.6076),
            _proposal(agent_id="committee.execution_quality", intent="enter", edge_bps=LIVE_EDGE_BPS, confidence=0.5473),
            _proposal(agent_id="committee.reversal_exit", intent="hold", edge_bps=0.0, confidence=0.0),
        ]
    )
    assert ranked[0].agent_id == "committee.portfolio_risk"
    assert ranked[0].intent == "enter"
    # Every entry proposal must now clear abstention, not just the best one.
    entries = [p for p in ranked if p.intent == "enter"]
    assert len(entries) == 3
    assert all(p.normalized_score > 0.0 for p in entries)
