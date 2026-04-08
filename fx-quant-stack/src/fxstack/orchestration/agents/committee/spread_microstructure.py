"""Deterministic spread and microstructure committee agent."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent
from fxstack.orchestration.agents.committee._common import (
    baseline_side,
    entry_quality_penalties,
    expected_edge_bps,
    max_allowed_spread_bps,
    policy_state,
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
        if max_spread > 0.0 and spread > max_spread:
            intent = "no_trade"
            blocking_reasons.append("spread_too_wide")
            rationale = "spread exceeds the configured maximum allowed spread"
        elif baseline_action == "enter":
            intent = "enter"
            rationale = "spread is within tolerance for entry"
        else:
            intent = "hold"
            rationale = "microstructure is acceptable but no entry is active"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=baseline_side(inputs) if intent == "enter" else ("FLAT" if intent == "no_trade" else baseline_side(inputs)),
            confidence=1.0 if intent == "no_trade" else 0.65,
            expected_edge_bps=expected_edge_bps(inputs),
            uncertainty=uncertainty_score(inputs),
            risk_cost=0.0,
            evidence_refs=[f"committee://spread_microstructure/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "spread_bps": spread,
                "max_allowed_spread_bps": max_spread,
                "spread_quality_ok": not blocking_reasons,
                "hard_block": bool(blocking_reasons),
            },
            proposal_role="microstructure_gate",
            score_components=entry_quality_penalties(inputs),
            blocking_reasons=blocking_reasons,
            rationale=rationale,
        )
