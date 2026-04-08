"""Shared helpers for deterministic committee agents."""

from __future__ import annotations

from typing import Any

from fxstack.orchestration.agents.base import AgentInputs, _normalize_side, _safe_float


def policy_state(inputs: AgentInputs) -> dict[str, Any]:
    return dict(inputs.context.policy_state or {})


def portfolio_state(inputs: AgentInputs) -> dict[str, Any]:
    return dict(inputs.context.portfolio_state or {})


def live_signal(inputs: AgentInputs) -> dict[str, Any]:
    return dict(inputs.context.live_signal or {})


def tick(inputs: AgentInputs) -> dict[str, Any]:
    return dict(inputs.context.tick or {})


def is_position_open(inputs: AgentInputs) -> bool:
    return bool(policy_state(inputs).get("position_open", False))


def baseline_side(inputs: AgentInputs) -> str:
    return _normalize_side(inputs.baseline_action.get("side") or policy_state(inputs).get("position_side"))


def playbook_name(inputs: AgentInputs) -> str:
    state = policy_state(inputs)
    return str(state.get("adaptive_playbook") or state.get("playbook") or "").strip().lower()


def adaptive_scores(inputs: AgentInputs) -> tuple[float, float, float]:
    state = policy_state(inputs)
    return (
        _safe_float(state.get("adaptive_playbook_score"), 0.0),
        _safe_float(state.get("adaptive_location_score"), 0.0),
        _safe_float(state.get("adaptive_trigger_score"), 0.0),
    )


def max_allowed_spread_bps(inputs: AgentInputs) -> float:
    return _safe_float(policy_state(inputs).get("max_allowed_spread_bps"), 0.0)


def spread_bps(inputs: AgentInputs) -> float:
    return _safe_float(tick(inputs).get("spread_bps"), _safe_float(policy_state(inputs).get("spread_bps"), 0.0))


def expected_edge_bps(inputs: AgentInputs) -> float:
    return _safe_float(live_signal(inputs).get("expected_edge_bps"), 0.0)


def uncertainty_score(inputs: AgentInputs) -> float:
    return _safe_float(live_signal(inputs).get("uncertainty_score"), 0.0)


def entry_quality_penalties(inputs: AgentInputs) -> dict[str, float]:
    pstate = policy_state(inputs)
    spread_penalty = max(0.0, spread_bps(inputs) - max_allowed_spread_bps(inputs))
    portfolio_penalty = max(
        _safe_float(portfolio_state(inputs).get("replacement_pressure"), 0.0),
        2.0 if (not bool(pstate.get("allocator_selected", False)) and str(inputs.baseline_action.get("action") or "") == "enter") else 0.0,
    )
    return {
        "uncertainty_penalty": max(0.0, uncertainty_score(inputs) * 10.0),
        "spread_penalty": spread_penalty,
        "portfolio_penalty": portfolio_penalty,
        "exit_priority_bonus": 0.0,
    }
