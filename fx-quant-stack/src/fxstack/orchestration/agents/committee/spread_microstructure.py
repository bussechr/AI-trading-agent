"""Deterministic spread and microstructure committee agent."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent
from fxstack.orchestration.agents.committee._common import (
    action_score_components,
    baseline_side,
    intelligent_decision,
    max_allowed_spread_bps,
    spread_bps,
    uncertainty_score,
)
from fxstack.orchestration.contracts import AgentProposal


class SpreadMicrostructureAgent(DeterministicAgent):
    agent_id = "committee.spread_microstructure"
    phase = "committee"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        spread = spread_bps(inputs)
        max_spread = max_allowed_spread_bps(inputs)
        blocking_reasons: list[str] = []
        baseline_action = str(inputs.baseline_action.get("action") or "").strip().lower()
        decision = intelligent_decision(inputs)
        if baseline_action == "enter":
            # Deliberately provide the zero-return abstention benchmark.  Entry
            # specialists must beat it after spread, uncertainty, and portfolio
            # costs are applied by the arbiter.
            intent = "no_trade"
            rationale = "microstructure supplied the abstention benchmark"
        else:
            intent = "hold"
            rationale = "microstructure has no active entry candidate"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=baseline_side(inputs) if intent == "enter" else ("FLAT" if intent == "no_trade" else baseline_side(inputs)),
            confidence=max(
                0.0,
                min(1.0, float(decision.get("no_trade_score", uncertainty_score(inputs)))),
            ),
            expected_edge_bps=0.0,
            uncertainty=uncertainty_score(inputs),
            risk_cost=0.0,
            evidence_refs=[f"committee://spread_microstructure/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "spread_bps": spread,
                "max_allowed_spread_bps": max_spread,
                "spread_quality_ok": max_spread <= 0.0 or spread <= max_spread,
                "hard_block": False,
            },
            proposal_role="microstructure_evidence",
            score_components=action_score_components(inputs, intent=intent),
            blocking_reasons=blocking_reasons,
            rationale=rationale,
        )
