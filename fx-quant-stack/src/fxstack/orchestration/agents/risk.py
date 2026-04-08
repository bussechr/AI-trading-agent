"""Deterministic risk agent for the Phase 2 shadow graph."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.contracts import AgentProposal


class RiskAgent(DeterministicAgent):
    agent_id = "risk"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        context = inputs.context
        policy_state = dict(context.policy_state or {})
        governance = dict((context.risk_envelope or {}).get("governance") or {})
        baseline_action = dict(inputs.baseline_action or {})
        reasons = [str(item) for item in list(policy_state.get("reasons") or []) if str(item).strip()]
        if bool(governance.get("paused", False)):
            reasons.append("capital_paused")
        if bool(governance.get("entries_only", False)) and str(baseline_action.get("action") or "") in {"exit", "reduce"}:
            reasons.append("entries_only_runtime")
        intent = "no_trade" if reasons else str(baseline_action.get("intent") or baseline_action.get("action") or "hold")
        side = "FLAT" if reasons else str(baseline_action.get("side") or "FLAT")
        risk_cost = max(
            _safe_float(governance.get("budget_scale"), 0.0),
            _safe_float(context.portfolio_state.get("replacement_pressure"), 0.0),
        )
        return self.make_proposal(
            inputs=inputs,
            intent=intent if intent in {"enter", "exit", "reduce", "hold", "no_trade"} else "hold",
            side=side,
            confidence=1.0 if reasons else 0.7,
            expected_edge_bps=_safe_float(context.live_signal.get("expected_edge_bps"), 0.0),
            uncertainty=_safe_float(context.live_signal.get("uncertainty_score"), 0.0),
            risk_cost=risk_cost,
            evidence_refs=[f"risk://{context.pair}/{context.cycle_id}"],
            constraints={"blocking_reasons": reasons, "governance": governance},
        )
