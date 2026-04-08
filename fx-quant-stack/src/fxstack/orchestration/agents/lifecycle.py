"""Deterministic lifecycle agent for the Phase 2 shadow graph."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.contracts import AgentProposal


class LifecycleAgent(DeterministicAgent):
    agent_id = "lifecycle"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        context = inputs.context
        policy_state = dict(context.policy_state or {})
        position_open = bool(policy_state.get("position_open", False))
        lifecycle_action = str(policy_state.get("lifecycle_action") or "").strip().lower()
        if not position_open:
            intent = "hold"
        elif lifecycle_action == "exit":
            intent = "exit"
        elif lifecycle_action in {"reduce", "partial_tp"}:
            intent = "reduce"
        else:
            intent = "hold"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=str(policy_state.get("position_side") or inputs.baseline_action.get("side") or "FLAT"),
            confidence=_safe_float(policy_state.get("exit_action_score"), 0.65 if intent in {"exit", "reduce"} else 0.5),
            expected_edge_bps=0.0,
            uncertainty=_safe_float(context.live_signal.get("uncertainty_score"), 0.0),
            risk_cost=_safe_float(policy_state.get("position_profit"), 0.0),
            evidence_refs=[f"lifecycle://{context.pair}/{context.cycle_id}"],
            constraints={
                "position_open": position_open,
                "lifecycle_action": lifecycle_action,
                "lifecycle_reason": str(policy_state.get("lifecycle_reason") or ""),
            },
        )
