"""Deterministic reversal-exit committee agent."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.agents.committee._common import baseline_side, policy_state, uncertainty_score
from fxstack.orchestration.contracts import AgentProposal


class ReversalExitAgent(DeterministicAgent):
    agent_id = "committee.reversal_exit"
    phase = "committee"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        state = policy_state(inputs)
        position_open = bool(state.get("position_open", False))
        lifecycle_action = str(state.get("lifecycle_action") or "").strip().lower()
        reversal_should_exit = bool(state.get("reversal_should_exit", False))
        reversal_ready = bool(state.get("reversal_ready", False))
        if not position_open:
            intent = "hold"
            rationale = "no open position to exit or reduce"
        elif lifecycle_action == "exit" or reversal_should_exit:
            intent = "exit"
            rationale = "reversal or lifecycle logic calls for a full exit"
        elif lifecycle_action in {"reduce", "partial_tp"}:
            intent = "reduce"
            rationale = "lifecycle logic calls for a reduction"
        else:
            intent = "hold"
            rationale = "reversal exit conditions are not active"
        exit_bonus = 100.0 if intent == "exit" else 50.0 if intent == "reduce" else 0.0
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=baseline_side(inputs),
            confidence=_safe_float(state.get("exit_action_score"), 0.95 if intent in {"exit", "reduce"} else 0.40),
            expected_edge_bps=0.0,
            uncertainty=uncertainty_score(inputs),
            risk_cost=0.0,
            evidence_refs=[f"committee://reversal_exit/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "lifecycle_action": lifecycle_action,
                "lifecycle_reason": str(state.get("lifecycle_reason") or ""),
                "reversal_should_exit": reversal_should_exit,
                "reversal_ready": reversal_ready,
            },
            proposal_role="lifecycle_exit",
            score_components={
                "uncertainty_penalty": max(0.0, uncertainty_score(inputs) * 10.0),
                "spread_penalty": 0.0,
                "portfolio_penalty": 0.0,
                "exit_priority_bonus": exit_bonus,
            },
            blocking_reasons=[],
            rationale=rationale,
        )
