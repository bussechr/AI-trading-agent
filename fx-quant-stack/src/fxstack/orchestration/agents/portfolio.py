"""Deterministic portfolio agent for the Phase 2 shadow graph."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.contracts import AgentProposal


class PortfolioAgent(DeterministicAgent):
    agent_id = "portfolio"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        context = inputs.context
        portfolio_state = dict(context.portfolio_state or {})
        policy_state = dict(context.policy_state or {})
        baseline_action = dict(inputs.baseline_action or {})
        allocator_selected = bool(policy_state.get("allocator_selected", False))
        position_open = bool(policy_state.get("position_open", False))
        replacement_pressure = _safe_float(portfolio_state.get("replacement_pressure"), 0.0)
        posture = str(portfolio_state.get("portfolio_posture") or policy_state.get("portfolio_posture") or "").strip()
        blocking_reasons: list[str] = []
        if not position_open and str(baseline_action.get("action") or "") == "enter" and not allocator_selected:
            blocking_reasons.append(str(policy_state.get("allocator_rejection_reason") or "portfolio_ranked_out"))
        if replacement_pressure >= 1.0:
            blocking_reasons.append("replacement_pressure_high")
        if posture.lower() == "paused":
            blocking_reasons.append("portfolio_paused")
        intent = "no_trade" if blocking_reasons else ("enter" if str(baseline_action.get("action") or "") == "enter" else "hold")
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=str(baseline_action.get("side") or "FLAT"),
            confidence=0.85 if not blocking_reasons else 0.95,
            expected_edge_bps=_safe_float(context.live_signal.get("expected_edge_bps"), 0.0),
            uncertainty=_safe_float(context.live_signal.get("uncertainty_score"), 0.0),
            risk_cost=replacement_pressure,
            evidence_refs=[f"portfolio://{context.pair}/{context.cycle_id}"],
            constraints={
                "allocator_selected": allocator_selected,
                "portfolio_posture": posture,
                "blocking_reasons": blocking_reasons,
            },
        )
