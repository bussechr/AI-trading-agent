"""Shared helpers for deterministic Phase 2 orchestration agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fxstack.orchestration.contracts import AgentProposal, DecisionContext


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float(default)
    return float(num) if num == num else float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalize_side(value: Any) -> str:
    side = str(value or "").strip().upper()
    if side in {"BUY", "SELL"}:
        return side
    return "FLAT"


@dataclass(slots=True)
class AgentInputs:
    context: DecisionContext
    baseline_action: dict[str, Any]
    summary_proposals: dict[str, Any] | None = None


class DeterministicAgent:
    agent_id = "base"
    phase = "shadow"

    def proposal_uuid(self, run_id: UUID) -> UUID:
        return uuid5(NAMESPACE_URL, f"{run_id}:{self.agent_id}:{self.phase}")

    def make_proposal(
        self,
        *,
        inputs: AgentInputs,
        intent: str,
        side: str,
        confidence: float,
        expected_edge_bps: float,
        uncertainty: float,
        risk_cost: float,
        evidence_refs: list[str] | None = None,
        constraints: dict[str, Any] | None = None,
        proposal_role: str = "",
        normalized_score: float = 0.0,
        score_components: dict[str, Any] | None = None,
        blocking_reasons: list[str] | None = None,
        rationale: str = "",
    ) -> AgentProposal:
        return AgentProposal(
            proposal_id=self.proposal_uuid(inputs.context.run_id),
            run_id=inputs.context.run_id,
            agent_id=self.agent_id,
            phase=self.phase,
            intent=str(intent),
            side=_normalize_side(side),
            confidence=max(0.0, min(1.0, _safe_float(confidence))),
            expected_edge_bps=_safe_float(expected_edge_bps),
            uncertainty=max(0.0, _safe_float(uncertainty)),
            risk_cost=max(0.0, _safe_float(risk_cost)),
            ttl_ms=max(0, _safe_int(inputs.context.runtime_state.get("decision_timeout_ms"), 250)),
            evidence_refs=list(evidence_refs or []),
            constraints=dict(constraints or {}),
            advisory_only=True,
            proposal_role=str(proposal_role or self.agent_id),
            normalized_score=_safe_float(normalized_score, 0.0),
            score_components=dict(score_components or {}),
            blocking_reasons=[str(item) for item in list(blocking_reasons or []) if str(item).strip()],
            rationale=str(rationale or ""),
        )
