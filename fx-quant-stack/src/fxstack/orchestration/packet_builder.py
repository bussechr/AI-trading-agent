"""Decision-packet and trace builders for the shadow orchestration bus."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from typing import Any
from uuid import uuid4

from fxstack.orchestration.context_builder import canonical_json
from fxstack.orchestration.contracts import AgentProposal, AgentTrace, DecisionContext, DecisionPacket, GovernedDecision
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION


def _shadow_governed_allowed(*, selected_action: str, blocking_reasons: list[str], arbiter_stage: str) -> bool:
    action = str(selected_action or "").strip().lower()
    stage = str(arbiter_stage or "").strip().lower()
    reasons = [str(item) for item in list(blocking_reasons or []) if str(item).strip()]
    if stage in {"hard_policy_blocks", "portfolio_checks", "governor_final_decision"}:
        return False
    if stage == "lifecycle":
        return action in {"exit", "reduce"} and not bool(reasons)
    if stage == "entry_ranking":
        return action == "enter" and not bool(reasons)
    return action in {"enter", "exit", "reduce"} and not bool(reasons)


def build_governed_decision_from_baseline(
    *,
    context: DecisionContext,
    baseline_action: dict[str, Any],
    blocking_reasons: list[str] | None,
    command_preview: dict[str, Any] | None = None,
) -> GovernedDecision:
    reasons = [str(item) for item in list(blocking_reasons or []) if str(item).strip()]
    selected_action = str(baseline_action.get("action") or baseline_action.get("intent") or ("no_trade" if reasons else "hold"))
    return GovernedDecision(
        decision_id=uuid4(),
        run_id=context.run_id,
        allowed=not bool(reasons),
        selected_action=selected_action,
        command_preview=dict(command_preview or {}) or None,
        blocking_reasons=reasons,
        approval_state="auto",
        governor_version=context.version_bundle.orchestrator_version,
        invariants_ok=True,
    )


def build_governed_decision_from_shadow(
    *,
    context: DecisionContext,
    shadow_action: dict[str, Any],
    blocking_reasons: list[str] | None,
    command_preview: dict[str, Any] | None = None,
    winning_proposal: AgentProposal | None = None,
    ranked_proposals: list[AgentProposal] | None = None,
    arbiter_stage: str = "",
    arbiter_rationale: str = "",
    score_path: list[dict[str, Any]] | None = None,
    invariant_results: dict[str, Any] | None = None,
) -> GovernedDecision:
    reasons = [str(item) for item in list(blocking_reasons or []) if str(item).strip()]
    selected_action = str(shadow_action.get("action") or shadow_action.get("intent") or ("no_trade" if reasons else "hold"))
    invariants = dict(invariant_results or {})
    return GovernedDecision(
        decision_id=uuid4(),
        run_id=context.run_id,
        allowed=_shadow_governed_allowed(
            selected_action=selected_action,
            blocking_reasons=reasons,
            arbiter_stage=arbiter_stage,
        ),
        selected_action=selected_action,
        command_preview=dict(command_preview or {}) or None,
        blocking_reasons=reasons,
        approval_state="auto",
        governor_version=context.version_bundle.orchestrator_version,
        invariants_ok=all(bool(value) for value in invariants.values()) if invariants else True,
        winning_proposal_id=str(winning_proposal.proposal_id) if winning_proposal is not None else "",
        ranked_proposal_ids=[str(item.proposal_id) for item in list(ranked_proposals or [])],
        arbiter_stage=str(arbiter_stage or ""),
        arbiter_rationale=str(arbiter_rationale or ""),
        score_path=list(score_path or []),
        invariant_results=invariants,
    )


def build_decision_packet(
    *,
    context: DecisionContext,
    baseline_action: dict[str, Any],
    shadow_action: dict[str, Any],
    proposals: list[AgentProposal],
    divergence_reason: str,
    proposal_votes: dict[str, Any],
    fault_classification: str | None,
    governed_decision: GovernedDecision,
    latency_ms: int,
    trace_id: str,
    fallback_used: bool,
    winning_proposal: AgentProposal | None = None,
    ranked_proposals: list[AgentProposal] | None = None,
    arbiter_stage: str = "",
    arbiter_rationale: str = "",
    score_path: list[dict[str, Any]] | None = None,
    invariant_results: dict[str, Any] | None = None,
) -> DecisionPacket:
    return DecisionPacket(
        packet_id=uuid4(),
        run_id=context.run_id,
        pair=context.pair,
        ts_utc=context.ts_utc,
        baseline_action=dict(baseline_action or {}),
        shadow_action=dict(shadow_action or {}),
        divergence_reason=str(divergence_reason or "agree"),
        proposal_votes=dict(proposal_votes or {}),
        fault_classification=(str(fault_classification) if fault_classification else None),
        proposals=list(proposals or []),
        governed_decision=governed_decision,
        latency_ms=max(0, int(latency_ms)),
        fallback_used=bool(fallback_used),
        trace_id=str(trace_id),
        schema_version=ORCHESTRATION_SCHEMA_VERSION,
        winning_proposal_id=str(winning_proposal.proposal_id) if winning_proposal is not None else "",
        ranked_proposal_ids=[str(item.proposal_id) for item in list(ranked_proposals or [])],
        arbiter_stage=str(arbiter_stage or ""),
        arbiter_rationale=str(arbiter_rationale or ""),
        score_path=list(score_path or []),
        invariant_results=dict(invariant_results or {}),
    )


def build_agent_trace(
    *,
    context: DecisionContext,
    trace_id: str,
    node_spans: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    model_calls: list[dict[str, Any]] | None = None,
    persistence_refs: list[str] | None = None,
    prompt_hashes: list[str] | None = None,
    error_class: str | None = None,
) -> AgentTrace:
    node_spans_list = list(node_spans or [])
    tool_calls_list = list(tool_calls or [])
    model_calls_list = list(model_calls or [])
    input_hash = hashlib.sha256(canonical_json(context.model_dump(mode="json")).encode("utf-8")).hexdigest()
    output_payload = {
        "node_spans": node_spans_list,
        "tool_calls": tool_calls_list,
        "model_calls": model_calls_list,
    }
    output_hash = hashlib.sha256(canonical_json(output_payload).encode("utf-8")).hexdigest()
    return AgentTrace(
        trace_id=str(trace_id),
        run_id=context.run_id,
        node_spans=node_spans_list,
        tool_calls=tool_calls_list,
        model_calls=model_calls_list,
        persistence_refs=list(persistence_refs or []),
        prompt_hashes=list(prompt_hashes or []),
        input_hash=f"sha256:{input_hash}",
        output_hash=f"sha256:{output_hash}",
        error_class=(str(error_class) if error_class else None),
        created_at=datetime.now(UTC),
    )
