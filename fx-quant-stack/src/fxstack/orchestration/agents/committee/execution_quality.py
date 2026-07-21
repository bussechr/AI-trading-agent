"""Deterministic execution-quality committee agent."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.agents.committee._common import (
    action_score_components,
    baseline_side,
    expected_edge_bps,
    intelligent_decision,
    policy_state,
    uncertainty_score,
)
from fxstack.orchestration.contracts import AgentProposal


class ExecutionQualityAgent(DeterministicAgent):
    agent_id = "committee.execution_quality"
    phase = "committee"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        state = policy_state(inputs)
        baseline_action = str(inputs.baseline_action.get("action") or "").strip().lower()
        entry_margin = _safe_float(state.get("entry_margin"), 0.0)
        meta_margin = _safe_float(state.get("meta_margin"), 0.0)
        entry_quality = _safe_float(state.get("adaptive_entry_quality"), 0.0)
        decision = intelligent_decision(inputs)
        enter_score = _safe_float(decision.get("enter_score"), entry_quality)
        no_trade_score = _safe_float(decision.get("no_trade_score"), 1.0 - entry_quality)
        blocking_reasons: list[str] = []
        if baseline_action != "enter":
            intent = "hold"
            rationale = "execution quality is only actionable for entry candidates"
        else:
            intent = "enter" if enter_score > no_trade_score else "no_trade"
            rationale = "execution specialist selected the higher-utility action"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=baseline_side(inputs) if intent == "enter" else ("FLAT" if intent == "no_trade" else baseline_side(inputs)),
            confidence=max(0.0, min(1.0, max(enter_score, no_trade_score))),
            expected_edge_bps=expected_edge_bps(inputs) if intent == "enter" else 0.0,
            uncertainty=uncertainty_score(inputs),
            risk_cost=0.0,
            evidence_refs=[f"committee://execution_quality/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "entry_margin": entry_margin,
                "meta_margin": meta_margin,
                "entry_quality": entry_quality,
                "enter_score": enter_score,
                "no_trade_score": no_trade_score,
                "command_preview": dict(inputs.baseline_action.get("command_preview") or {}),
            },
            proposal_role="execution_quality",
            score_components=action_score_components(inputs, intent=intent),
            blocking_reasons=blocking_reasons,
            rationale=rationale,
        )
