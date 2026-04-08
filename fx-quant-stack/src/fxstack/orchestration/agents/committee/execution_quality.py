"""Deterministic execution-quality committee agent."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.agents.committee._common import (
    baseline_side,
    entry_quality_penalties,
    expected_edge_bps,
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
        blocking_reasons: list[str] = []
        if baseline_action != "enter":
            intent = "hold"
            rationale = "execution quality is only actionable for entry candidates"
        elif entry_margin < 0.0 or meta_margin < 0.0:
            intent = "no_trade"
            blocking_reasons.append("negative_execution_margin")
            rationale = "entry or meta margin is negative"
        elif entry_quality and entry_quality < 0.52:
            intent = "no_trade"
            blocking_reasons.append("low_execution_quality")
            rationale = "execution quality score is below the entry floor"
        else:
            intent = "enter"
            rationale = "execution quality, margins, and edge support entry"
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=baseline_side(inputs) if intent == "enter" else ("FLAT" if intent == "no_trade" else baseline_side(inputs)),
            confidence=max(0.0, min(1.0, max(entry_quality, 0.55))),
            expected_edge_bps=expected_edge_bps(inputs),
            uncertainty=uncertainty_score(inputs),
            risk_cost=0.0,
            evidence_refs=[f"committee://execution_quality/{inputs.context.pair}/{inputs.context.cycle_id}"],
            constraints={
                "entry_margin": entry_margin,
                "meta_margin": meta_margin,
                "entry_quality": entry_quality,
                "command_preview": dict(inputs.baseline_action.get("command_preview") or {}),
            },
            proposal_role="execution_quality",
            score_components=entry_quality_penalties(inputs),
            blocking_reasons=blocking_reasons,
            rationale=rationale,
        )
