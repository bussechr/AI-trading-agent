from __future__ import annotations

from math import exp, isfinite
from typing import Any

from fxstack.belief.candidate_builder import HYPOTHESIS_SCENARIOS, regime_fit_prior
from fxstack.belief.types import DirectionalBelief

SCENARIOS = [*HYPOTHESIS_SCENARIOS, "no_edge"]
TRADEABLE_SCENARIOS = list(HYPOTHESIS_SCENARIOS)
SCENARIO_HORIZON_WEIGHTS = {
    "trend_pullback": (0.20, 0.45, 0.35),
    "range_mean_reversion": (0.55, 0.30, 0.15),
    "breakout_expansion": (0.50, 0.35, 0.15),
    "failed_breakout_reversal": (0.45, 0.35, 0.20),
    "no_edge": (0.0, 0.0, 0.0),
}
CONFIRMATION_RULES = {
    "trend_pullback": (3, "pullback_then_resume", "trigger_score_lt_0.35_or_trade_prob_lt_0.50"),
    "range_mean_reversion": (2, "snapback_then_decay", "short_horizon_flip_against_thesis_2bars"),
    "breakout_expansion": (2, "immediate_follow_through", "short_horizon_flip_or_regime_fit_collapse"),
    "failed_breakout_reversal": (3, "impulse_reversal", "structural_horizon_realigns_with_breakout"),
    "no_edge": (0, "no_trade", "no_trade"),
}


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, _safe_float(value, 0.0)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        out = float(default)
    if isfinite(out):
        return out
    fallback = float(default)
    return fallback if isfinite(fallback) else 0.0


def regime_fit_score(environment_state: str, scenario: str) -> float:
    return float(regime_fit_prior(str(environment_state or ""), str(scenario or "")))


# AGENT: Legacy v1 composer retained so old artifacts stay readable while v2 rolls out.
def scenario_blend_score(*, scenario: str, scenario_head_prob: float, adaptive_playbook: str, playbook_score: float) -> float:
    adaptive_prior = float(playbook_score) if str(adaptive_playbook or "").strip() == str(scenario or "").strip() else 0.0
    return _clip01((0.65 * _safe_float(scenario_head_prob, 0.0)) + (0.35 * adaptive_prior))


def side_horizon_score(*, scenario: str, side: str, short_up_prob: float, trade_up_prob: float, structural_up_prob: float) -> float:
    weights = SCENARIO_HORIZON_WEIGHTS.get(str(scenario or ""), (0.0, 0.0, 0.0))
    if str(scenario or "") == "no_edge":
        return 0.0
    short_p = _clip01(_safe_float(short_up_prob, 0.5))
    trade_p = _clip01(_safe_float(trade_up_prob, 0.5))
    structural_p = _clip01(_safe_float(structural_up_prob, 0.5))
    if str(side or "").strip().lower() == "short":
        short_p = 1.0 - short_p
        trade_p = 1.0 - trade_p
        structural_p = 1.0 - structural_p
    return _clip01((weights[0] * short_p) + (weights[1] * trade_p) + (weights[2] * structural_p))


def horizon_alignment_score(*, short_up_prob: float, trade_up_prob: float, structural_up_prob: float) -> float:
    short_p = _clip01(_safe_float(short_up_prob, 0.5))
    trade_p = _clip01(_safe_float(trade_up_prob, 0.5))
    structural_p = _clip01(_safe_float(structural_up_prob, 0.5))
    return _clip01(1.0 - ((abs(short_p - trade_p) + abs(trade_p - structural_p)) / 2.0))


def fragility_score(
    *,
    uncertainty_score: float,
    model_disagreement_score: float,
    short_up_prob: float,
    trade_up_prob: float,
    structural_up_prob: float,
    extension_penalty_score: float,
    hostility_score: float,
) -> float:
    short_p = _clip01(_safe_float(short_up_prob, 0.5))
    trade_p = _clip01(_safe_float(trade_up_prob, 0.5))
    structural_p = _clip01(_safe_float(structural_up_prob, 0.5))
    horizon_disagreement = max(abs(short_p - trade_p), abs(trade_p - structural_p))
    return _clip01(
        (0.35 * _clip01(_safe_float(uncertainty_score, 0.0)))
        + (0.25 * _clip01(_safe_float(model_disagreement_score, 0.0)))
        + (0.20 * _clip01(float(horizon_disagreement)))
        + (0.10 * _clip01(_safe_float(extension_penalty_score, 0.0)))
        + (0.10 * _clip01(_safe_float(hostility_score, 0.0)))
    )


def compose_directional_belief(
    *,
    pair: str,
    ts: str,
    signal: Any,
    adaptive_meta: dict[str, Any],
    scenario_probs: dict[str, float],
    short_up_prob: float,
    trade_up_prob: float,
    structural_up_prob: float,
    model_version: str,
    source_mode: str,
) -> DirectionalBelief:
    scenario_prob_map = {scenario: _clip01(_safe_float(scenario_probs.get(scenario, 0.0), 0.0)) for scenario in SCENARIOS}
    environment_state = str(adaptive_meta.get("environment_state") or adaptive_meta.get("adaptive_environment_state") or "")
    adaptive_playbook = str(adaptive_meta.get("adaptive_playbook") or adaptive_meta.get("playbook") or "")
    playbook_score = _safe_float(adaptive_meta.get("playbook_score"), 0.0)
    fragility = fragility_score(
        uncertainty_score=_safe_float(adaptive_meta.get("uncertainty_score", getattr(signal, "uncertainty_score", 0.0)), 0.0),
        model_disagreement_score=_safe_float(adaptive_meta.get("model_disagreement_score", getattr(signal, "model_disagreement_score", 0.0)), 0.0),
        short_up_prob=short_up_prob,
        trade_up_prob=trade_up_prob,
        structural_up_prob=structural_up_prob,
        extension_penalty_score=_safe_float(adaptive_meta.get("extension_penalty_score", getattr(signal, "extension_penalty_score", 0.0)), 0.0),
        hostility_score=_safe_float(adaptive_meta.get("hostility_score", 0.0), 0.0),
    )
    alignment = horizon_alignment_score(short_up_prob=short_up_prob, trade_up_prob=trade_up_prob, structural_up_prob=structural_up_prob)

    thesis_rows: list[tuple[float, str, str, float, float, float]] = []
    for scenario in TRADEABLE_SCENARIOS:
        blend = scenario_blend_score(
            scenario=scenario,
            scenario_head_prob=scenario_prob_map.get(scenario, 0.0),
            adaptive_playbook=adaptive_playbook,
            playbook_score=playbook_score,
        )
        regime_fit = regime_fit_score(environment_state, scenario)
        for side in ("long", "short"):
            side_score = side_horizon_score(
                scenario=scenario,
                side=side,
                short_up_prob=short_up_prob,
                trade_up_prob=trade_up_prob,
                structural_up_prob=structural_up_prob,
            )
            score = _clip01(blend * side_score * regime_fit * (1.0 - fragility))
            thesis_rows.append((score, scenario, side, blend, regime_fit, side_score))

    thesis_rows.sort(key=lambda item: item[0], reverse=True)
    primary = thesis_rows[0] if thesis_rows else (0.0, "", "", 0.0, 0.0, 0.0)
    opposition = thesis_rows[1] if len(thesis_rows) > 1 else (0.0, "", "", 0.0, 0.0, 0.0)
    primary_scenario = str(primary[1])
    primary_side = str(primary[2])
    primary_regime_fit = regime_fit_score(environment_state, primary_scenario) if primary_scenario else 0.0
    confirm_bars, path_shape, invalidation = CONFIRMATION_RULES.get(primary_scenario, (0, "", ""))

    return DirectionalBelief(
        pair=str(pair),
        ts=str(ts),
        primary_side=primary_side,
        primary_scenario=primary_scenario,
        primary_thesis=f"{primary_scenario}:{primary_side}" if primary_scenario and primary_side else "",
        primary_score=float(primary[0]),
        opposing_side=str(opposition[2]),
        opposing_scenario=str(opposition[1]),
        opposing_thesis=f"{opposition[1]}:{opposition[2]}" if opposition[1] and opposition[2] else "",
        opposing_score=float(opposition[0]),
        belief_gap=float(max(0.0, float(primary[0]) - float(opposition[0]))),
        fragility_score=float(fragility),
        horizon_alignment_score=float(alignment),
        short_up_prob=float(_clip01(_safe_float(short_up_prob, 0.5))),
        trade_up_prob=float(_clip01(_safe_float(trade_up_prob, 0.5))),
        structural_up_prob=float(_clip01(_safe_float(structural_up_prob, 0.5))),
        scenario_probs=dict(sorted(scenario_prob_map.items())),
        regime_fit_score=float(primary_regime_fit),
        expected_confirmation_window_bars=int(confirm_bars),
        expected_path_shape=str(path_shape),
        invalidation_reason=str(invalidation),
        model_version=str(model_version),
        source_mode=str(source_mode),
    )


def _softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    max_val = max(values)
    exp_vals = [exp(v - max_val) for v in values]
    denom = sum(exp_vals) or 1.0
    return [float(v / denom) for v in exp_vals]


def compose_ranked_directional_belief(
    *,
    pair: str,
    ts: str,
    hypotheses: list[dict[str, Any]],
    model_version: str,
    source_mode: str,
) -> DirectionalBelief:
    if not hypotheses:
        confirm_bars, path_shape, invalidation = CONFIRMATION_RULES["no_edge"]
        return DirectionalBelief(
            pair=str(pair),
            ts=str(ts),
            primary_scenario="no_edge",
            primary_score=0.0,
            expected_confirmation_window_bars=int(confirm_bars),
            expected_path_shape=str(path_shape),
            invalidation_reason=str(invalidation),
            no_edge=True,
            model_version=str(model_version),
            source_mode=str(source_mode),
            hypotheses=[],
        )

    rank_shares = _softmax([_safe_float(item.get("rank_margin"), 0.0) for item in hypotheses])
    scored: list[dict[str, Any]] = []
    for item, rank_share in zip(hypotheses, rank_shares):
        regime_fit = _clip01(_safe_float(item.get("scenario_regime_fit_prior"), 0.0))
        uncertainty = _clip01(_safe_float(item.get("uncertainty_score"), 0.0))
        ev_prob = _clip01(_safe_float(item.get("p_ev_above_hurdle"), 0.0))
        expected_ev = _safe_float(item.get("expected_net_ev_bps"), 0.0)
        confirm_prob = _clip01(_safe_float(item.get("p_confirm_success"), 0.0))
        fail_fast_prob = _clip01(_safe_float(item.get("p_fail_fast"), 0.0))
        score_raw = (
            (0.40 * rank_share)
            + (0.25 * ev_prob)
            + (0.20 * _clip01(expected_ev / 12.0))
            + (0.10 * confirm_prob)
            - (0.15 * fail_fast_prob)
        )
        score = _clip01(score_raw * regime_fit * (1.0 - (0.20 * uncertainty)))
        horizon_alignment = _clip01(1.0 - abs(confirm_prob - (1.0 - fail_fast_prob)))
        scored.append(
            {
                **dict(item),
                "rank_softmax_share": float(rank_share),
                "score_raw": float(score_raw),
                "score": float(score),
                "regime_fit_score": float(regime_fit),
                "horizon_alignment_score": float(horizon_alignment),
            }
        )
    scored.sort(key=lambda item: (-float(item.get("score", 0.0)), str(item.get("scenario") or ""), str(item.get("side") or "")))
    primary = dict(scored[0])
    primary_side = str(primary.get("side") or "")
    opposition = next((dict(item) for item in scored if str(item.get("side") or "") != primary_side), {})
    belief_gap = max(0.0, float(primary.get("score", 0.0)) - _safe_float(opposition.get("score"), 0.0))
    primary_score = float(primary.get("score", 0.0))
    primary_ev_prob = _clip01(_safe_float(primary.get("p_ev_above_hurdle"), 0.0))
    primary_fail_fast = _clip01(_safe_float(primary.get("p_fail_fast"), 0.0))
    no_edge = bool(primary_score < 0.18 or primary_ev_prob < 0.45 or primary_fail_fast > 0.55)
    scenario_name = "no_edge" if no_edge else str(primary.get("scenario") or "")
    side_name = "" if no_edge else str(primary.get("side") or "")
    confirm_bars, path_shape, invalidation = CONFIRMATION_RULES.get(scenario_name or "no_edge", CONFIRMATION_RULES["no_edge"])
    fragility = _clip01(
        (0.55 * primary_fail_fast)
        + (0.20 * _clip01(_safe_float(primary.get("uncertainty_score"), 0.0)))
        + (0.15 * _clip01(_safe_float(primary.get("model_disagreement_score"), 0.0)))
        + (0.10 * _clip01(_safe_float(primary.get("extension_penalty_score"), 0.0)))
    )
    return DirectionalBelief(
        pair=str(pair),
        ts=str(ts),
        primary_side=side_name,
        primary_scenario=scenario_name,
        primary_thesis=f"{scenario_name}:{side_name}" if scenario_name not in {"", "no_edge"} and side_name else "",
        primary_score=0.0 if no_edge else primary_score,
        primary_rank_score=0.0 if no_edge else float(primary.get("rank_softmax_share", 0.0)),
        primary_ev_above_hurdle_prob=0.0 if no_edge else primary_ev_prob,
        primary_expected_net_ev_bps=0.0 if no_edge else _safe_float(primary.get("expected_net_ev_bps"), 0.0),
        primary_confirm_prob=0.0 if no_edge else _clip01(_safe_float(primary.get("p_confirm_success"), 0.0)),
        primary_fail_fast_prob=0.0 if no_edge else primary_fail_fast,
        opposing_side=str(opposition.get("side") or ""),
        opposing_scenario=str(opposition.get("scenario") or ""),
        opposing_thesis=f"{str(opposition.get('scenario') or '')}:{str(opposition.get('side') or '')}" if opposition else "",
        opposing_score=_safe_float(opposition.get("score"), 0.0),
        belief_gap=float(0.0 if no_edge else belief_gap),
        fragility_score=float(fragility),
        horizon_alignment_score=float(primary.get("horizon_alignment_score", 0.0)) if not no_edge else 0.0,
        short_up_prob=0.0,
        trade_up_prob=0.0,
        structural_up_prob=0.0,
        scenario_probs={},
        regime_fit_score=0.0 if no_edge else _safe_float(primary.get("regime_fit_score"), 0.0),
        expected_confirmation_window_bars=int(confirm_bars),
        expected_path_shape=str(path_shape),
        invalidation_reason=str(invalidation),
        no_edge=bool(no_edge),
        hypotheses=scored,
        model_version=str(model_version),
        source_mode=str(source_mode),
    )
