"""Deterministic risk agent for the Phase 2 shadow graph."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _safe_float
from fxstack.orchestration.contracts import AgentProposal


LIFECYCLE_HARD_BLOCK_REASONS = {
    "capital_paused",
    "governance_paused",
    "latency_breach",
    "latency_budget_exceeded",
    "parity_breach",
    "proposal_budget_exceeded",
    "rollout_breach",
    "shadow_alignment",
    "stale_features",
}


def _is_lifecycle_hard_block_reason(reason: str) -> bool:
    token = str(reason or "").strip().lower()
    if not token:
        return False
    if token in LIFECYCLE_HARD_BLOCK_REASONS:
        return True
    return token.endswith("_error")


class RiskAgent(DeterministicAgent):
    agent_id = "risk"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        context = inputs.context
        policy_state = dict(context.policy_state or {})
        governance = dict((context.risk_envelope or {}).get("governance") or {})
        baseline_action = dict(inputs.baseline_action or {})
        baseline_intent = str(baseline_action.get("intent") or baseline_action.get("action") or "hold").strip().lower()
        if baseline_intent not in {"enter", "exit", "reduce", "hold", "no_trade"}:
            baseline_intent = "hold"
        is_protective_lifecycle = baseline_intent in {"exit", "reduce"}
        reasons = [str(item) for item in list(policy_state.get("reasons") or []) if str(item).strip()]
        if bool(governance.get("paused", False)):
            reasons.append("capital_paused")
        normalized_reasons = list(dict.fromkeys(reasons))
        lifecycle_hard_blocking_reasons = [
            reason for reason in normalized_reasons if _is_lifecycle_hard_block_reason(reason)
        ]
        blocking_reasons = list(dict.fromkeys(lifecycle_hard_blocking_reasons if is_protective_lifecycle else normalized_reasons))
        intent = (
            baseline_intent
            if is_protective_lifecycle and not blocking_reasons
            else ("no_trade" if blocking_reasons else baseline_intent)
        )
        side = "FLAT" if intent == "no_trade" else str(baseline_action.get("side") or "FLAT")
        risk_cost = max(
            _safe_float(governance.get("budget_scale"), 0.0),
            _safe_float(context.portfolio_state.get("replacement_pressure"), 0.0),
        )
        return self.make_proposal(
            inputs=inputs,
            intent=intent if intent in {"enter", "exit", "reduce", "hold", "no_trade"} else "hold",
            side=side,
            confidence=1.0 if blocking_reasons else 0.7,
            expected_edge_bps=_safe_float(context.live_signal.get("expected_edge_bps"), 0.0),
            uncertainty=_safe_float(context.live_signal.get("uncertainty_score"), 0.0),
            risk_cost=risk_cost,
            evidence_refs=[f"risk://{context.pair}/{context.cycle_id}"],
            constraints={"governance": governance},
            blocking_reasons=blocking_reasons,
        )
