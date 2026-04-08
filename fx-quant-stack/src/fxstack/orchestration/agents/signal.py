"""Deterministic signal agent for the Phase 2 shadow graph."""

from __future__ import annotations

from fxstack.orchestration.agents.base import AgentInputs, DeterministicAgent, _normalize_side, _safe_float
from fxstack.orchestration.contracts import AgentProposal


class SignalAgent(DeterministicAgent):
    agent_id = "signal"

    def propose(self, inputs: AgentInputs) -> AgentProposal:
        context = inputs.context
        live_signal = dict(context.live_signal or {})
        policy_state = dict(context.policy_state or {})
        reasons = [str(item) for item in list(policy_state.get("reasons") or []) if str(item).strip()]
        position_open = bool(policy_state.get("position_open", False))
        side = _normalize_side(live_signal.get("side") or policy_state.get("position_side"))
        if position_open:
            intent = "hold"
        elif bool(policy_state.get("execution_ready", False)) and not reasons:
            intent = "enter"
        elif reasons:
            intent = "no_trade"
            side = "FLAT"
        else:
            intent = "hold"
        confidence = max(
            _safe_float(live_signal.get("confidence"), 0.0),
            _safe_float(live_signal.get("trade_prob"), 0.0),
            min(1.0, _safe_float(live_signal.get("score"), 0.0)),
        )
        return self.make_proposal(
            inputs=inputs,
            intent=intent,
            side=side if intent == "enter" else ("FLAT" if intent == "no_trade" else side),
            confidence=confidence,
            expected_edge_bps=_safe_float(live_signal.get("expected_edge_bps"), 0.0),
            uncertainty=_safe_float(live_signal.get("uncertainty_score"), 0.0),
            risk_cost=0.0,
            evidence_refs=[f"signal://{context.pair}/{context.cycle_id}"],
            constraints={
                "execution_ready": bool(policy_state.get("execution_ready", False)),
                "position_open": position_open,
                "blocking_reasons": reasons,
            },
        )
