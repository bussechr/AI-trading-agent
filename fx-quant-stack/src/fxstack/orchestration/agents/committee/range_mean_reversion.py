"""Deterministic range-mean-reversion committee agent."""

from __future__ import annotations

from fxstack.backtest.adaptive_policy import PLAYBOOK_RANGE_MEAN_REVERSION
from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent
from fxstack.orchestration.agents.committee._common import (
    adaptive_scores,
    baseline_side,
    entry_quality_penalties,
    expected_edge_bps,
    is_position_open,
    playbook_name,
    uncertainty_score,
)
from fxstack.orchestration.contracts import AgentProposal


class RangeMeanReversionAgent(DeterministicAgent):
    agent_id = "committee.range_mean_reversion"
    phase = "committee"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        playbook = playbook_name(inputs)
        playbook_score, location_score, trigger_score = adaptive_scores(inputs)
        blocking_reasons: list[str] = []
        intent = "hold"
        side = baseline_side(inputs)
        rationale = "range mean reversion is inactive"
        if is_position_open(inputs):
            rationale = "position already open, range reversion entry deferred"
        elif playbook != PLAYBOOK_RANGE_MEAN_REVERSION:
            pass
        elif playbook_score < 0.58:
            intent = "no_trade"
            blocking_reasons.append("low_playbook_score")
            rationale = "range mean reversion playbook score below floor"
        elif location_score < 0.32:
            intent = "no_trade"
            blocking_reasons.append("weak_location_score")
            rationale = "range mean reversion location score below floor"
        elif trigger_score < 0.45:
            intent = "no_trade"
            blocking_reasons.append("weak_trigger_score")
            rationale = "range mean reversion trigger score below floor"
        else:
            intent = "enter"
            rationale = "range mean reversion aligned on playbook, location, and trigger"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=side if intent == "enter" else ("FLAT" if intent == "no_trade" else side),
            confidence=max(playbook_score, location_score, trigger_score),
            expected_edge_bps=expected_edge_bps(inputs),
            uncertainty=uncertainty_score(inputs),
            risk_cost=0.0,
            evidence_refs=[f"committee://range_mean_reversion/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "playbook": PLAYBOOK_RANGE_MEAN_REVERSION,
                "playbook_score": playbook_score,
                "location_score": location_score,
                "trigger_score": trigger_score,
            },
            proposal_role="playbook_entry",
            score_components=entry_quality_penalties(inputs),
            blocking_reasons=blocking_reasons,
            rationale=rationale,
        )
