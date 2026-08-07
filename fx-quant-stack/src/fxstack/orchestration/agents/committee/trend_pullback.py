"""Deterministic trend-pullback committee agent."""

from __future__ import annotations

from fxstack.strategy.adaptive_policy import PLAYBOOK_TREND_PULLBACK
from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent
from fxstack.orchestration.agents.committee._common import (
    action_score_components,
    adaptive_scores,
    baseline_side,
    expected_edge_bps,
    is_position_open,
    playbook_name,
    specialist_action_scores,
    uncertainty_score,
)
from fxstack.orchestration.contracts import AgentProposal


class TrendPullbackAgent(DeterministicAgent):
    agent_id = "committee.trend_pullback"
    phase = "committee"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        playbook = playbook_name(inputs)
        playbook_score, location_score, trigger_score = adaptive_scores(inputs)
        blocking_reasons: list[str] = []
        intent = "hold"
        side = baseline_side(inputs)
        rationale = "trend pullback is inactive"
        enter_score, no_trade_score = specialist_action_scores(inputs)
        if is_position_open(inputs):
            intent = "hold"
            rationale = "position already open, trend pullback entry deferred"
        elif playbook != PLAYBOOK_TREND_PULLBACK:
            intent = "hold"
        else:
            intent = "enter" if enter_score > no_trade_score else "no_trade"
            rationale = "trend specialist selected the higher-utility action"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=side if intent == "enter" else ("FLAT" if intent == "no_trade" else side),
            confidence=max(enter_score, no_trade_score),
            expected_edge_bps=expected_edge_bps(inputs) if intent == "enter" else 0.0,
            uncertainty=uncertainty_score(inputs),
            risk_cost=0.0,
            evidence_refs=[f"committee://trend_pullback/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "playbook": PLAYBOOK_TREND_PULLBACK,
                "playbook_score": playbook_score,
                "location_score": location_score,
                "trigger_score": trigger_score,
                "enter_score": enter_score,
                "no_trade_score": no_trade_score,
            },
            proposal_role="playbook_entry",
            score_components=action_score_components(inputs, intent=intent),
            blocking_reasons=blocking_reasons,
            rationale=rationale,
        )
