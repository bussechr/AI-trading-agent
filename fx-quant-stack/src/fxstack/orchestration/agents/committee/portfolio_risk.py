"""Deterministic portfolio-risk committee agent."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.agents.committee._common import (
    baseline_side,
    expected_edge_bps,
    policy_state,
    portfolio_state,
    uncertainty_score,
)
from fxstack.orchestration.contracts import AgentProposal


class PortfolioRiskAgent(DeterministicAgent):
    agent_id = "committee.portfolio_risk"
    phase = "committee"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        pstate = policy_state(inputs)
        portfolio = portfolio_state(inputs)
        posture = str(portfolio.get("portfolio_posture") or pstate.get("portfolio_posture") or "").strip().lower()
        replacement_pressure = _safe_float(portfolio.get("replacement_pressure"), 0.0)
        allocator_selected = bool(pstate.get("allocator_selected", False))
        blocking_reasons: list[str] = []
        if posture == "paused":
            blocking_reasons.append("portfolio_paused")
        if not allocator_selected and str(inputs.baseline_action.get("action") or "") == "enter":
            blocking_reasons.append(str(pstate.get("allocator_rejection_reason") or "portfolio_ranked_out"))
        if replacement_pressure >= 1.0:
            blocking_reasons.append("replacement_pressure_high")
        if blocking_reasons:
            intent = "no_trade"
            rationale = "portfolio slots, replacement pressure, or posture blocked entry"
        elif str(inputs.baseline_action.get("action") or "") == "enter":
            intent = "enter"
            rationale = "portfolio budget and slot checks allow the candidate"
        else:
            intent = "hold"
            rationale = "portfolio conditions are neutral for this cycle"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=baseline_side(inputs) if intent == "enter" else ("FLAT" if intent == "no_trade" else baseline_side(inputs)),
            confidence=0.95 if blocking_reasons else 0.70,
            expected_edge_bps=expected_edge_bps(inputs),
            uncertainty=uncertainty_score(inputs),
            risk_cost=replacement_pressure,
            evidence_refs=[f"committee://portfolio_risk/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "allocator_selected": allocator_selected,
                "allocator_rejection_reason": str(pstate.get("allocator_rejection_reason") or ""),
                "portfolio_posture": posture,
                "replacement_pressure": replacement_pressure,
            },
            proposal_role="portfolio_risk",
            score_components={
                "uncertainty_penalty": max(0.0, uncertainty_score(inputs) * 10.0),
                "spread_penalty": 0.0,
                "portfolio_penalty": replacement_pressure,
                "exit_priority_bonus": 0.0,
            },
            blocking_reasons=blocking_reasons,
            rationale=rationale,
        )
