"""Deterministic committee ranking and arbitration for Phase 4 shadow governance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fxstack.backtest.adaptive_policy import PLAYBOOK_ORDER
from fxstack.orchestration.agents.base import _safe_float
from fxstack.orchestration.contracts import AgentProposal, DecisionContext


EXIT_INTENTS = {"exit", "reduce"}
ENTRY_INTENTS = {"enter"}
NO_TRADE_INTENTS = {"no_trade"}
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


@dataclass(slots=True)
class ArbiterOutcome:
    ranked_proposals: list[AgentProposal]
    winning_proposal: AgentProposal | None
    selected_action: str
    allowed: bool
    arbiter_stage: str
    arbiter_rationale: str
    blocking_reasons: list[str]
    score_path: list[dict[str, Any]]
    invariant_results: dict[str, Any]


def _normalize_intent(value: Any) -> str:
    intent = str(value or "").strip().lower()
    if intent in {"enter", "exit", "reduce", "hold", "no_trade"}:
        return intent
    return "hold"


def _normalize_side(value: Any) -> str:
    side = str(value or "").strip().upper()
    if side in {"BUY", "SELL"}:
        return side
    return "FLAT"


def _playbook_rank(playbook: str) -> int:
    token = str(playbook or "").strip().lower()
    try:
        return PLAYBOOK_ORDER.index(token)
    except ValueError:
        return len(PLAYBOOK_ORDER)


def _pair_tier_rank(context: DecisionContext) -> int:
    tier = str((context.runtime_state or {}).get("pair_tier") or "").strip().lower()
    return 2 if tier == "tier1" else 1


def _spread_penalty(context: DecisionContext, proposal: AgentProposal) -> float:
    components = dict(proposal.score_components or {})
    if "spread_penalty" in components:
        return max(0.0, _safe_float(components.get("spread_penalty"), 0.0))
    tick = dict(context.tick or {})
    policy_state = dict(context.policy_state or {})
    spread_bps = _safe_float(tick.get("spread_bps"), _safe_float(policy_state.get("spread_bps"), 0.0))
    max_spread_bps = _safe_float(policy_state.get("max_allowed_spread_bps"), 0.0)
    return max(0.0, spread_bps - max_spread_bps) if max_spread_bps > 0.0 else max(0.0, spread_bps)


def _portfolio_penalty(context: DecisionContext, proposal: AgentProposal) -> float:
    components = dict(proposal.score_components or {})
    if "portfolio_penalty" in components:
        return max(0.0, _safe_float(components.get("portfolio_penalty"), 0.0))
    portfolio_state = dict(context.portfolio_state or {})
    return max(_safe_float(proposal.risk_cost, 0.0), _safe_float(portfolio_state.get("replacement_pressure"), 0.0))


def _uncertainty_penalty(proposal: AgentProposal) -> float:
    components = dict(proposal.score_components or {})
    if "uncertainty_penalty" in components:
        return max(0.0, _safe_float(components.get("uncertainty_penalty"), 0.0))
    return max(0.0, _safe_float(proposal.uncertainty, 0.0) * 10.0)


def _exit_priority_bonus(proposal: AgentProposal) -> float:
    components = dict(proposal.score_components or {})
    if "exit_priority_bonus" in components:
        return _safe_float(components.get("exit_priority_bonus"), 0.0)
    intent = _normalize_intent(proposal.intent)
    if intent == "exit":
        return 100.0
    if intent == "reduce":
        return 50.0
    return 0.0


def _proposal_playbook(context: DecisionContext, proposal: AgentProposal) -> str:
    constraints = dict(proposal.constraints or {})
    return str(
        constraints.get("playbook")
        or constraints.get("adaptive_playbook")
        or (context.policy_state or {}).get("adaptive_playbook")
        or (context.policy_state or {}).get("playbook")
        or ""
    ).strip().lower()


def _rank_sort_key(context: DecisionContext, proposal: AgentProposal) -> tuple[Any, ...]:
    components = dict(proposal.score_components or {})
    return (
        0 if _normalize_intent(proposal.intent) in EXIT_INTENTS else 1,
        max(0.0, _safe_float(components.get("spread_penalty"), 0.0)),
        max(0.0, _safe_float(proposal.uncertainty, 0.0)),
        -_pair_tier_rank(context),
        _playbook_rank(_proposal_playbook(context, proposal)),
        str(proposal.agent_id),
        str(proposal.proposal_id),
    )


def enrich_proposal_scores(*, context: DecisionContext, proposals: list[AgentProposal]) -> list[AgentProposal]:
    enriched: list[AgentProposal] = []
    for proposal in proposals:
        components = dict(proposal.score_components or {})
        components["uncertainty_penalty"] = _uncertainty_penalty(proposal)
        components["spread_penalty"] = _spread_penalty(context, proposal)
        components["portfolio_penalty"] = _portfolio_penalty(context, proposal)
        components["exit_priority_bonus"] = _exit_priority_bonus(proposal)
        normalized_score = (
            _safe_float(proposal.expected_edge_bps, 0.0) * max(0.0, _safe_float(proposal.confidence, 0.0))
            - _safe_float(components.get("uncertainty_penalty"), 0.0)
            - _safe_float(components.get("spread_penalty"), 0.0)
            - _safe_float(components.get("portfolio_penalty"), 0.0)
            + _safe_float(components.get("exit_priority_bonus"), 0.0)
        )
        enriched.append(
            proposal.model_copy(
                update={
                    "normalized_score": float(normalized_score),
                    "score_components": components,
                }
            )
        )
    return sorted(
        enriched,
        key=lambda proposal: (-float(proposal.normalized_score),) + _rank_sort_key(context, proposal),
    )


def build_proposal_votes(*, proposals: list[AgentProposal], winning_proposal: AgentProposal | None = None) -> dict[str, Any]:
    votes: dict[str, Any] = {
        "total": int(len(proposals)),
        "by_intent": {},
        "by_side": {},
        "by_agent": {},
        "by_role": {},
        "winning_proposal_id": str(winning_proposal.proposal_id) if winning_proposal is not None else "",
    }
    for proposal in proposals:
        intent = _normalize_intent(proposal.intent)
        side = _normalize_side(proposal.side)
        role = str(proposal.proposal_role or proposal.agent_id)
        votes["by_intent"][intent] = int(votes["by_intent"].get(intent, 0)) + 1
        votes["by_side"][side] = int(votes["by_side"].get(side, 0)) + 1
        votes["by_agent"][str(proposal.agent_id)] = {
            "intent": intent,
            "score": float(proposal.normalized_score),
            "role": role,
        }
        votes["by_role"][role] = int(votes["by_role"].get(role, 0)) + 1
    return votes


def _summary_blocking_reasons(summary_proposals: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for value in dict(summary_proposals or {}).values():
        proposal = AgentProposal.model_validate(value)
        reasons.extend(str(item) for item in list(proposal.blocking_reasons or []) if str(item).strip())
        reasons.extend(
            str(item)
            for item in list(dict(proposal.constraints or {}).get("blocking_reasons") or [])
            if str(item).strip()
        )
    return list(dict.fromkeys(reasons))


def _proposal_blocking_reasons(proposal: AgentProposal) -> list[str]:
    blocking_reasons = [str(item) for item in list(proposal.blocking_reasons or []) if str(item).strip()]
    blocking_reasons.extend(
        str(item)
        for item in list(dict(proposal.constraints or {}).get("blocking_reasons") or [])
        if str(item).strip()
    )
    return list(dict.fromkeys(blocking_reasons))


def _is_lifecycle_hard_block_reason(reason: Any) -> bool:
    token = str(reason or "").strip().lower()
    if not token:
        return False
    if token in LIFECYCLE_HARD_BLOCK_REASONS:
        return True
    return token.endswith("_error")


def _score_path(proposals: list[AgentProposal]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for rank, proposal in enumerate(proposals, start=1):
        rows.append(
            {
                "rank": int(rank),
                "proposal_id": str(proposal.proposal_id),
                "agent_id": str(proposal.agent_id),
                "proposal_role": str(proposal.proposal_role or proposal.agent_id),
                "intent": _normalize_intent(proposal.intent),
                "side": _normalize_side(proposal.side),
                "normalized_score": float(proposal.normalized_score),
                "score_components": dict(proposal.score_components or {}),
                "blocking_reasons": _proposal_blocking_reasons(proposal),
                "rationale": str(proposal.rationale or ""),
                "evidence_refs": list(proposal.evidence_refs or []),
            }
        )
    return rows


def govern_shadow(
    *,
    context: DecisionContext,
    baseline_action: dict[str, Any],
    ranked_proposals: list[AgentProposal],
    summary_proposals: dict[str, Any] | None = None,
    fault_classification: str | None = None,
) -> ArbiterOutcome:
    summary_map = dict(summary_proposals or {})
    baseline_blocking = [
        str(item)
        for item in list(dict(baseline_action or {}).get("blocking_reasons") or [])
        if str(item).strip()
    ]
    summary_blocking = _summary_blocking_reasons(summary_map)
    hard_block_reasons = list(dict.fromkeys([*baseline_blocking, *summary_blocking]))

    if fault_classification:
        hard_block_reasons = list(dict.fromkeys([*hard_block_reasons, str(fault_classification)]))

    lifecycle_candidates = [
        proposal
        for proposal in ranked_proposals
        if _normalize_intent(proposal.intent) in EXIT_INTENTS and not _proposal_blocking_reasons(proposal)
    ]
    entry_candidates = [
        proposal
        for proposal in ranked_proposals
        if _normalize_intent(proposal.intent) in ENTRY_INTENTS and not _proposal_blocking_reasons(proposal)
    ]
    no_trade_blockers = [proposal for proposal in ranked_proposals if _normalize_intent(proposal.intent) == "no_trade"]
    blocking_no_trade = [proposal for proposal in no_trade_blockers if list(_proposal_blocking_reasons(proposal))]
    portfolio_blockers = [
        proposal for proposal in blocking_no_trade if str(proposal.proposal_role or "") == "portfolio_risk"
    ]
    entry_gate_blockers = [
        proposal
        for proposal in blocking_no_trade
        if str(proposal.proposal_role or "") in {"microstructure_gate", "execution_quality"}
    ]
    safety_blockers = [
        proposal
        for proposal in blocking_no_trade
        if proposal not in portfolio_blockers and proposal not in entry_gate_blockers
    ]
    lifecycle_hard_block_reasons = [reason for reason in hard_block_reasons if _is_lifecycle_hard_block_reason(reason)]
    lifecycle_compatible_block_reasons = [
        reason for reason in hard_block_reasons if not _is_lifecycle_hard_block_reason(reason)
    ]

    if lifecycle_candidates and lifecycle_hard_block_reasons:
        winner = None
        selected_action = "no_trade"
        allowed = False
        arbiter_stage = "hard_policy_blocks"
        arbiter_rationale = f"hard block: {', '.join(lifecycle_hard_block_reasons)}"
        blocking_reasons = lifecycle_hard_block_reasons
    elif lifecycle_candidates:
        winner = lifecycle_candidates[0]
        selected_action = _normalize_intent(winner.intent)
        allowed = True
        arbiter_stage = "lifecycle"
        arbiter_rationale = str(winner.rationale or "lifecycle exit outranked entries")
        if lifecycle_compatible_block_reasons:
            arbiter_rationale = (
                f"{arbiter_rationale}; compatible blockers ignored for lifecycle: "
                f"{', '.join(lifecycle_compatible_block_reasons)}"
            )
        blocking_reasons = _proposal_blocking_reasons(winner)
    elif hard_block_reasons:
        winner = None
        selected_action = "no_trade"
        allowed = False
        arbiter_stage = "hard_policy_blocks"
        arbiter_rationale = f"hard block: {', '.join(hard_block_reasons)}"
        blocking_reasons = hard_block_reasons
    elif portfolio_blockers:
        winner = None
        selected_action = "no_trade"
        allowed = False
        arbiter_stage = "portfolio_checks"
        blocking_reasons = list(dict.fromkeys(item for proposal in portfolio_blockers for item in _proposal_blocking_reasons(proposal)))
        arbiter_rationale = "portfolio budget or slot checks blocked entry"
    elif entry_gate_blockers:
        winner = None
        selected_action = "no_trade"
        allowed = False
        arbiter_stage = "entry_ranking"
        blocking_reasons = list(dict.fromkeys(item for proposal in entry_gate_blockers for item in _proposal_blocking_reasons(proposal)))
        arbiter_rationale = "entry-quality or microstructure gates blocked the candidate"
    elif safety_blockers:
        winner = None
        selected_action = "no_trade"
        allowed = False
        arbiter_stage = "entry_ranking"
        blocking_reasons = list(dict.fromkeys(item for proposal in safety_blockers for item in _proposal_blocking_reasons(proposal)))
        arbiter_rationale = "additional safety gates blocked the candidate"
    elif entry_candidates:
        winner = entry_candidates[0]
        selected_action = "enter"
        allowed = True
        arbiter_stage = "entry_ranking"
        arbiter_rationale = str(winner.rationale or "highest ranked entry candidate")
        blocking_reasons = _proposal_blocking_reasons(winner)
    else:
        winner = ranked_proposals[0] if ranked_proposals else None
        selected_action = _normalize_intent(winner.intent) if winner is not None else "hold"
        if selected_action not in {"hold", "no_trade"}:
            selected_action = "hold"
        allowed = False
        arbiter_stage = "governor_final_decision"
        arbiter_rationale = str(
            winner.rationale if winner is not None and winner.rationale else "no committee candidate cleared arbitration"
        )
        blocking_reasons = _proposal_blocking_reasons(winner) if winner is not None else []

    invariant_results = {
        "hard_policy_block_suppresses_command": not (
            bool(lifecycle_hard_block_reasons if lifecycle_candidates else hard_block_reasons) and bool(allowed)
        ),
        "exit_outranks_entry_same_cycle": not bool(lifecycle_candidates and entry_candidates) or selected_action in EXIT_INTENTS,
        "stable_tie_break_order": True,
        "command_authority_remains_shadow_only": True,
    }

    return ArbiterOutcome(
        ranked_proposals=ranked_proposals,
        winning_proposal=winner,
        selected_action=selected_action,
        allowed=bool(allowed),
        arbiter_stage=arbiter_stage,
        arbiter_rationale=arbiter_rationale,
        blocking_reasons=list(dict.fromkeys(blocking_reasons)),
        score_path=_score_path(ranked_proposals),
        invariant_results=invariant_results,
    )
