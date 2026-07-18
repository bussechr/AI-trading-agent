# AGENT: ROLE: Deterministic alpha-sleeve allocator shared by twin replay and live runtime adaptive execution.
# AGENT: ENTRYPOINT: imported by `tools/fxstack_digital_twin_backtest.py` and `fxstack/runtime/runner.py`.
# AGENT: PRIMARY INPUTS: adaptive candidate diagnostics, open-position keep scores, sleeve-health snapshots, settings-derived caps.
# AGENT: PRIMARY OUTPUTS: ranked candidates, selected candidates, replacement plans, cycle summaries.
# AGENT: DEPENDS ON: `fxstack/strategy/allocator_types.py`, `fxstack/strategy/sleeve_governance.py`.
# AGENT: CALLED BY: `tools/fxstack_digital_twin_backtest.py`, `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: pure calculations; caller applies exits and submissions.
# AGENT: HANDSHAKES: portfolio allocator seam between adaptive context and live/twin execution.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `fxstack/backtest/adaptive_policy.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
from typing import Any

from fxstack.strategy.allocator_types import (
    AllocatorCandidate,
    AllocatorConfig,
    AllocatorCycleSummary,
    AllocatorOpenPosition,
    SleeveHealthSnapshot,
)
from fxstack.strategy.sleeve_governance import sleeve_health_penalty
from fxstack.portfolio.correlation import compute_correlation_snapshot


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        out = float(default)
    if math.isfinite(out):
        return out
    fallback = float(default)
    return fallback if math.isfinite(fallback) else 0.0


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _safe_int(value: Any, default: int = 0) -> int:
    return int(_finite_float(value, float(default)))


def _clip01(value: Any, default: float = 0.0) -> float:
    return max(0.0, min(1.0, _finite_float(value, default)))


def playbook_to_sleeve(playbook: str) -> str:
    txt = str(playbook or "").strip()
    return txt if txt else "no_trade"


def allocator_config_from_settings(settings: Any) -> AllocatorConfig:
    return AllocatorConfig(
        max_total_positions=max(0, _safe_int(getattr(settings, "max_total_positions", 0), 0)),
        max_pair_positions=max(0, _safe_int(getattr(settings, "max_pair_positions", 0), 0)),
        max_new_entries=max(1, _safe_int(getattr(settings, "max_new_entries_per_cycle", 1), 1)),
        max_spread_bps=max(0.0, _finite_float(getattr(settings, "max_allowed_spread_bps", 0.0), 0.0)),
        min_expected_edge_bps=_finite_float(getattr(settings, "min_expected_edge_bps", 1.0), 1.0),
    )


def spread_cost_penalty(*, spread_bps: float, max_spread_bps: float) -> float:
    if not _is_finite(spread_bps):
        return 1.0
    spread = max(0.0, float(spread_bps))
    max_spread = max(0.0, _finite_float(max_spread_bps, 0.0))
    if max_spread <= 0.0:
        return 0.0 if spread <= 0.0 else 1.0
    return _clip01(spread / max_spread)


def replacement_pressure_score(open_positions: list[AllocatorOpenPosition]) -> float:
    if not open_positions:
        return 0.0
    weakest_keep = min(_clip01(item.keep_score, 0.0) for item in open_positions)
    replaceable_share = sum(1 for item in open_positions if bool(item.replaceable_hold)) / max(1, len(open_positions))
    return _clip01((1.0 - weakest_keep) * 0.70 + replaceable_share * 0.30)


def portfolio_risk_pressure(
    *,
    pair: str,
    session_bucket: str,
    sleeve: str,
    open_positions: list[AllocatorOpenPosition],
    corr_mode: str = "heuristic",
    realized_returns_by_pair: Any = None,
    corr_window_bars: int = 0,
    corr_min_obs: int = 0,
) -> dict[str, float]:
    if not open_positions:
        return {
            "pair_pressure": 0.0,
            "session_pressure": 0.0,
            "sleeve_pressure": 0.0,
            "correlation_pressure": 0.0,
            "risk_pressure": 0.0,
        }

    total = max(1, len(open_positions))
    pair_key = str(pair or "").strip().upper()
    session_key = str(session_bucket or "").strip().lower()
    sleeve_key = str(sleeve or "").strip().lower()
    pair_pressure = sum(1 for item in open_positions if str(item.pair or "").strip().upper() == pair_key) / total if pair_key else 0.0
    session_pressure = (
        sum(1 for item in open_positions if str(item.session_bucket or "").strip().lower() == session_key) / total
        if session_key
        else 0.0
    )
    sleeve_pressure = (
        sum(1 for item in open_positions if str(item.sleeve or "").strip().lower() == sleeve_key) / total if sleeve_key else 0.0
    )
    active_pairs = [str(item.pair or "").strip().upper() for item in open_positions if str(item.pair or "").strip()]
    correlation_snapshot = compute_correlation_snapshot(
        symbol=pair_key,
        active_symbols=active_pairs,
        mode=str(corr_mode or "heuristic"),
        realized_returns_by_pair=realized_returns_by_pair,
        window_bars=int(corr_window_bars or 0),
        min_obs=int(corr_min_obs or 0),
    )
    same_pair_overlap = 0.5 + (0.5 * float(pair_pressure))
    correlation_pressure = _clip01(
        max(
            0.65 * float(correlation_snapshot.max_abs_corr) + 0.35 * float(correlation_snapshot.avg_abs_corr),
            same_pair_overlap,
        )
    )
    risk_pressure = _clip01(
        (0.40 * float(pair_pressure))
        + (0.25 * float(session_pressure))
        + (0.20 * float(sleeve_pressure))
        + (0.15 * float(correlation_pressure))
    )
    return {
        "pair_pressure": float(pair_pressure),
        "session_pressure": float(session_pressure),
        "sleeve_pressure": float(sleeve_pressure),
        "correlation_pressure": float(correlation_pressure),
        "risk_pressure": float(risk_pressure),
    }


def conviction_band_bonus(band: str) -> float:
    mapping = {
        "low": -0.05,
        "medium": 0.01,
        "high": 0.04,
        "extreme": 0.07,
    }
    return float(mapping.get(str(band or ""), 0.0))


def thesis_stage_bonus(stage: str) -> float:
    mapping = {
        "scout": 0.0,
        "core": 0.02,
        "press": 0.05,
        "harvest": -0.01,
        "stand_down": -0.05,
    }
    return float(mapping.get(str(stage or ""), 0.0))


def compute_allocator_score(
    candidate: AllocatorCandidate,
    *,
    config: AllocatorConfig,
    open_positions: list[AllocatorOpenPosition],
    sleeve_health: SleeveHealthSnapshot | None,
) -> float:
    max_edge = max(abs(_finite_float(config.min_expected_edge_bps, 1.0)) * 3.0, 1e-9)
    ev_rank = _clip01(_finite_float(candidate.expected_edge_bps, 0.0) / max_edge)
    spread_penalty = spread_cost_penalty(
        spread_bps=float(candidate.spread_bps),
        max_spread_bps=float(candidate.max_spread_bps or config.max_spread_bps),
    )
    pressure = replacement_pressure_score(open_positions)
    sleeve_snapshot = sleeve_health or SleeveHealthSnapshot(sleeve=str(candidate.sleeve))
    governance_penalty = _finite_float(sleeve_health_penalty(sleeve_snapshot), 0.12)
    budget_pressure = _clip01(candidate.sleeve_budget_pressure, 1.0)
    portfolio_headroom = 1.0 - _clip01(candidate.portfolio_risk_pressure, 1.0)
    cross_pair_strength = _clip01(candidate.cross_pair_recommendation_strength, 0.5)
    cross_pair_influence = _clip01(candidate.cross_pair_influence_score, 0.5)
    cross_pair_rank_signal = 0.0
    if int(candidate.cross_pair_rank_position or 0) > 0:
        cross_pair_rank_signal = 1.0 / float(candidate.cross_pair_rank_position)
    cross_pair_soft_penalty = 0.05 if bool(candidate.cross_pair_soft_block) else 0.0
    cross_pair_hard_penalty = 0.20 if bool(candidate.cross_pair_hard_block) else 0.0
    score = (
        (0.26 * _clip01(candidate.adaptive_entry_quality))
        + (0.14 * _clip01(candidate.conviction_score))
        + (0.16 * _clip01(candidate.playbook_score))
        + (0.12 * _clip01(candidate.location_score))
        + (0.08 * _clip01(candidate.trigger_score))
        + (0.10 * float(ev_rank))
        + (0.05 * _clip01(candidate.macro_coherence_score))
        + (0.05 * _clip01(sleeve_snapshot.score))
        + (0.04 * float(pressure))
        + (0.03 * _clip01(candidate.replacement_urgency))
        + (0.04 * float(portfolio_headroom))
        + float(conviction_band_bonus(candidate.conviction_band))
        + float(thesis_stage_bonus(candidate.thesis_stage))
        + _finite_float(candidate.campaign_priority_boost, 0.0)
        + (0.06 * float(cross_pair_strength - 0.5))
        + (0.05 * float(cross_pair_influence - 0.5))
        + (0.03 * float(cross_pair_rank_signal))
        - (0.10 * _clip01(candidate.uncertainty_score, 1.0))
        - (0.08 * _clip01(candidate.currency_crowding_penalty, 1.0))
        - (0.05 * _clip01(candidate.playbook_diversification_penalty, 1.0))
        - (0.15 * _clip01(candidate.portfolio_risk_pressure, 1.0))
        - (0.05 * float(spread_penalty))
        - (0.05 * float(budget_pressure))
        - float(cross_pair_soft_penalty)
        - float(cross_pair_hard_penalty)
        - float(governance_penalty)
    )
    return float(_clip01(score))


def build_allocator_candidate(
    *,
    candidate_id: str,
    index: int,
    pair: str,
    ts: str,
    side: str,
    sleeve: str,
    environment_state: str,
    session_bucket: str,
    baseline_allowed: bool,
    adaptive_allowed: bool,
    playbook_score: float,
    location_score: float,
    trigger_score: float,
    adaptive_entry_quality: float,
    expected_edge_bps: float,
    uncertainty_score: float,
    spread_bps: float,
    max_spread_bps: float,
    macro_coherence_score: float,
    currency_crowding_penalty: float,
    playbook_diversification_penalty: float,
    cross_pair_rank_position: int = 0,
    cross_pair_influence_score: float = 0.5,
    cross_pair_recommendation_strength: float = 0.5,
    cross_pair_soft_block: bool = False,
    cross_pair_hard_block: bool = False,
    cross_pair_influenced_by_pairs: list[str] | None = None,
    cross_pair_reason_codes: list[str] | None = None,
    thesis_id: str = "",
    campaign_seq: int = 0,
    campaign_entry_kind: str = "",
    campaign_state: str = "inactive",
    campaign_state_reason: str = "",
    campaign_priority_boost: float = 0.0,
    campaign_proof_score: float = 0.0,
    campaign_maturity_score: float = 0.0,
    campaign_reset_quality: float = 0.0,
    campaign_reentry_blocked: bool = False,
    conviction_score: float = 0.0,
    conviction_band: str = "blocked",
    thesis_stage: str = "stand_down",
    portfolio_posture: str = "balanced_probe",
    replacement_urgency: float = 0.0,
    sleeve_budget_target: int = 0,
    sleeve_budget_used: int = 0,
    sleeve_budget_pressure: float = 0.0,
    corr_mode: str = "heuristic",
    realized_returns_by_pair: Any = None,
    corr_window_bars: int = 0,
    corr_min_obs: int = 0,
    config: AllocatorConfig,
    open_positions: list[AllocatorOpenPosition],
    sleeve_health: SleeveHealthSnapshot | None,
) -> AllocatorCandidate:
    sleeve_snapshot = sleeve_health or SleeveHealthSnapshot(sleeve=str(sleeve or ""))
    normalized_fields = {
        "playbook_score": playbook_score,
        "location_score": location_score,
        "trigger_score": trigger_score,
        "adaptive_entry_quality": adaptive_entry_quality,
        "uncertainty_score": uncertainty_score,
        "macro_coherence_score": macro_coherence_score,
        "currency_crowding_penalty": currency_crowding_penalty,
        "playbook_diversification_penalty": playbook_diversification_penalty,
        "cross_pair_influence_score": cross_pair_influence_score,
        "cross_pair_recommendation_strength": cross_pair_recommendation_strength,
        "campaign_priority_boost": campaign_priority_boost,
        "campaign_proof_score": campaign_proof_score,
        "campaign_maturity_score": campaign_maturity_score,
        "campaign_reset_quality": campaign_reset_quality,
        "conviction_score": conviction_score,
        "replacement_urgency": replacement_urgency,
        "sleeve_budget_pressure": sleeve_budget_pressure,
        "sleeve_health_score": sleeve_snapshot.score,
    }
    numeric_errors = [
        f"nonfinite:{name}" for name, value in normalized_fields.items() if not _is_finite(value)
    ]
    numeric_errors.extend(
        f"out_of_range:{name}"
        for name, value in normalized_fields.items()
        if _is_finite(value) and not 0.0 <= float(value) <= 1.0
    )
    for name, value in {
        "expected_edge_bps": expected_edge_bps,
        "spread_bps": spread_bps,
        "max_spread_bps": max_spread_bps,
    }.items():
        if not _is_finite(value):
            numeric_errors.append(f"nonfinite:{name}")
    for name, value in {"spread_bps": spread_bps, "max_spread_bps": max_spread_bps}.items():
        if _is_finite(value) and float(value) < 0.0:
            numeric_errors.append(f"out_of_range:{name}")
    numeric_errors = sorted(set(numeric_errors))
    max_spread_value = max(0.0, _finite_float(max_spread_bps, _finite_float(config.max_spread_bps, 0.0)))
    spread_fallback = max(max_spread_value, 1.0)
    portfolio_pressure = portfolio_risk_pressure(
        pair=str(pair),
        session_bucket=str(session_bucket),
        sleeve=str(sleeve),
        open_positions=open_positions,
        corr_mode=str(corr_mode or "heuristic"),
        realized_returns_by_pair=realized_returns_by_pair,
        corr_window_bars=int(corr_window_bars or 0),
        corr_min_obs=int(corr_min_obs or 0),
    )
    candidate = AllocatorCandidate(
        candidate_id=str(candidate_id),
        index=int(index),
        pair=str(pair),
        ts=str(ts),
        side=str(side),
        sleeve=str(sleeve),
        environment_state=str(environment_state),
        session_bucket=str(session_bucket),
        baseline_allowed=bool(baseline_allowed),
        adaptive_allowed=bool(adaptive_allowed),
        playbook_score=_clip01(playbook_score),
        location_score=_clip01(location_score),
        trigger_score=_clip01(trigger_score),
        adaptive_entry_quality=_clip01(adaptive_entry_quality),
        expected_edge_bps=_finite_float(expected_edge_bps, 0.0),
        uncertainty_score=_clip01(uncertainty_score, 1.0),
        spread_bps=max(0.0, _finite_float(spread_bps, spread_fallback)),
        max_spread_bps=max_spread_value,
        macro_coherence_score=_clip01(macro_coherence_score),
        currency_crowding_penalty=_clip01(currency_crowding_penalty, 1.0),
        playbook_diversification_penalty=_clip01(playbook_diversification_penalty, 1.0),
        cross_pair_rank_position=max(0, _safe_int(cross_pair_rank_position, 0)),
        cross_pair_influence_score=_clip01(cross_pair_influence_score, 0.5),
        cross_pair_recommendation_strength=_clip01(cross_pair_recommendation_strength, 0.5),
        cross_pair_soft_block=bool(cross_pair_soft_block),
        cross_pair_hard_block=bool(cross_pair_hard_block),
        cross_pair_influenced_by_pairs=list(cross_pair_influenced_by_pairs or []),
        cross_pair_reason_codes=list(cross_pair_reason_codes or []),
        sleeve_health_score=_clip01(sleeve_snapshot.score),
        sleeve_health_state=str(sleeve_snapshot.state),
        thesis_id=str(thesis_id),
        campaign_seq=_safe_int(campaign_seq, 0),
        campaign_entry_kind=str(campaign_entry_kind),
        campaign_state=str(campaign_state),
        campaign_state_reason=str(campaign_state_reason),
        campaign_priority_boost=_clip01(campaign_priority_boost),
        campaign_proof_score=_clip01(campaign_proof_score),
        campaign_maturity_score=_clip01(campaign_maturity_score),
        campaign_reset_quality=_clip01(campaign_reset_quality),
        campaign_reentry_blocked=bool(campaign_reentry_blocked),
        conviction_score=_clip01(conviction_score),
        conviction_band=str(conviction_band),
        thesis_stage=str(thesis_stage),
        portfolio_posture=str(portfolio_posture),
        replacement_urgency=_clip01(replacement_urgency),
        portfolio_pair_pressure=float(portfolio_pressure["pair_pressure"]),
        portfolio_session_pressure=float(portfolio_pressure["session_pressure"]),
        portfolio_sleeve_pressure=float(portfolio_pressure["sleeve_pressure"]),
        portfolio_correlation_pressure=float(portfolio_pressure["correlation_pressure"]),
        portfolio_risk_pressure=float(portfolio_pressure["risk_pressure"]),
        sleeve_budget_target=max(0, _safe_int(sleeve_budget_target, 0)),
        sleeve_budget_used=max(0, _safe_int(sleeve_budget_used, 0)),
        sleeve_budget_pressure=_clip01(sleeve_budget_pressure, 1.0),
        numeric_inputs_valid=not numeric_errors,
        numeric_input_errors=numeric_errors,
    )
    candidate.spread_cost_penalty = spread_cost_penalty(
        spread_bps=float(candidate.spread_bps),
        max_spread_bps=float(candidate.max_spread_bps or config.max_spread_bps),
    )
    candidate.replacement_pressure_score = replacement_pressure_score(open_positions)
    candidate.allocator_score = compute_allocator_score(
        candidate,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_snapshot,
    )
    return candidate


def rank_allocator_candidates(candidates: list[AllocatorCandidate]) -> list[AllocatorCandidate]:
    ranked = sorted(
        list(candidates),
        key=lambda item: (
            _finite_float(item.allocator_score, 0.0),
            _finite_float(item.adaptive_entry_quality, 0.0),
            _finite_float(item.playbook_score, 0.0),
            _finite_float(item.expected_edge_bps, 0.0),
            _finite_float(item.cross_pair_recommendation_strength, 0.0),
            _finite_float(item.cross_pair_influence_score, 0.0),
        ),
        reverse=True,
    )
    out: list[AllocatorCandidate] = []
    for rank, item in enumerate(ranked, start=1):
        out.append(replace(item, allocator_rank=int(rank)))
    return out


def allocate_candidates(
    *,
    candidates: list[AllocatorCandidate],
    open_positions: list[AllocatorOpenPosition],
    remaining_slots: int,
    config: AllocatorConfig,
    tempo_gap_active: bool,
    sleeve_budget_targets: dict[str, int] | None = None,
) -> tuple[list[AllocatorCandidate], AllocatorCycleSummary]:
    ranked = rank_allocator_candidates(candidates)
    margin = max(0.0, _finite_float(config.tempo_gap_replacement_margin if tempo_gap_active else config.replacement_margin, 0.0))
    budget_targets = {str(k): max(0, int(v)) for k, v in dict(sleeve_budget_targets or {}).items()}
    replaceable = sorted(
        [
            item
            for item in open_positions
            if bool(item.replaceable_hold)
            and (not bool(item.protected_hold))
            and _finite_float(item.age_bars, 0.0) >= max(0.0, _finite_float(config.protected_hold_window_bars, 0.0))
            and _clip01(item.keep_score, 0.0) < 0.62
        ],
        key=lambda item: _clip01(item.keep_score, 0.0),
    )
    selected_by_id: set[str] = set()
    replacement_exit_count = 0
    replacement_candidate_count = 0
    weakest_keep_score = _clip01(replaceable[0].keep_score, 0.0) if replaceable else 0.0
    budget_selected_ids: set[str] = set()
    if budget_targets:
        budget_counts: Counter[str] = Counter()
        for item in ranked:
            if len(budget_selected_ids) >= max(0, int(remaining_slots)):
                break
            target = int(budget_targets.get(str(item.sleeve), int(item.sleeve_budget_target or 0)))
            if target <= 0:
                continue
            if int(budget_counts.get(str(item.sleeve), 0)) >= target:
                continue
            budget_selected_ids.add(str(item.candidate_id))
            budget_counts[str(item.sleeve)] += 1

    for idx, item in enumerate(ranked):
        hard_blocked = bool(item.cross_pair_hard_block)
        numeric_blocked = not bool(item.numeric_inputs_valid)
        adaptive_blocked = not bool(item.adaptive_allowed)
        selected = False
        if numeric_blocked:
            rejection_reason = "invalid_numeric_inputs"
        elif adaptive_blocked:
            rejection_reason = "adaptive_not_allowed"
        elif hard_blocked:
            rejection_reason = "cross_pair_hard_gate"
        else:
            rejection_reason = "allocator_ranked_out"
        replacement_target_pair = ""
        replacement_value = 0.0
        if not (numeric_blocked or adaptive_blocked or hard_blocked) and str(item.candidate_id) in budget_selected_ids:
            selected = True
            rejection_reason = "selected_budgeted"
        elif not (numeric_blocked or adaptive_blocked or hard_blocked) and len(selected_by_id) < max(0, int(remaining_slots)):
            selected = True
            rejection_reason = "selected" if not budget_targets else "selected_global_fill"
        elif not (numeric_blocked or adaptive_blocked or hard_blocked) and replaceable:
            weakest = replaceable[0]
            weakest_score = _clip01(weakest.keep_score, 0.0)
            if _clip01(item.allocator_score, 0.0) >= (weakest_score + margin):
                selected = True
                replacement_candidate_count += 1
                replacement_exit_count += 1
                replacement_target_pair = str(weakest.pair)
                replacement_value = _clip01(item.allocator_score, 0.0) - weakest_score
                rejection_reason = "selected_with_replacement"
                replaceable.pop(0)
        if selected:
            selected_by_id.add(str(item.candidate_id))
        ranked[idx] = replace(
            item,
            allocator_selected=bool(selected),
            allocator_rejection_reason=str(rejection_reason if not selected else rejection_reason),
            replacement_target_pair=str(replacement_target_pair),
            replacement_value=float(replacement_value),
        )

    selected_counts = Counter(item.sleeve for item in ranked if bool(item.allocator_selected))
    candidate_counts = Counter(item.sleeve for item in ranked)
    campaign_state_counts = Counter(str(item.campaign_state or "inactive") for item in ranked)
    rejection_counts = Counter(
        str(item.allocator_rejection_reason or "selected")
        for item in ranked
        if not bool(item.allocator_selected)
    )
    pair_pressures = [_clip01(item.portfolio_pair_pressure) for item in ranked]
    session_pressures = [_clip01(item.portfolio_session_pressure) for item in ranked]
    sleeve_pressures = [_clip01(item.portfolio_sleeve_pressure) for item in ranked]
    correlation_pressures = [_clip01(item.portfolio_correlation_pressure) for item in ranked]
    risk_pressures = [_clip01(item.portfolio_risk_pressure) for item in ranked]

    def _avg_max(values: list[float]) -> tuple[float, float]:
        if not values:
            return 0.0, 0.0
        return float(sum(values) / len(values)), float(max(values))

    pair_pressure_avg, pair_pressure_max = _avg_max(pair_pressures)
    session_pressure_avg, session_pressure_max = _avg_max(session_pressures)
    sleeve_pressure_avg, sleeve_pressure_max = _avg_max(sleeve_pressures)
    correlation_pressure_avg, correlation_pressure_max = _avg_max(correlation_pressures)
    risk_pressure_avg, risk_pressure_max = _avg_max(risk_pressures)
    normalized_budget_targets = {
        str(item.sleeve): int(budget_targets.get(str(item.sleeve), int(item.sleeve_budget_target or 0)))
        for item in ranked
    }
    out_ranked: list[AllocatorCandidate] = []
    for item in ranked:
        sleeve_key = str(item.sleeve)
        target = int(normalized_budget_targets.get(sleeve_key, int(item.sleeve_budget_target or 0)))
        used = int(selected_counts.get(sleeve_key, 0))
        pressure = _clip01(float(max(0, used - target)) / max(1.0, float(max(target, 1))))
        out_ranked.append(
            replace(
                item,
                sleeve_budget_target=int(target),
                sleeve_budget_used=int(used),
                sleeve_budget_pressure=float(pressure),
            )
        )
    summary = AllocatorCycleSummary(
        candidate_count=int(len(ranked)),
        selected_count=int(sum(1 for item in ranked if bool(item.allocator_selected))),
        ranked_out_count=int(sum(1 for item in ranked if not bool(item.allocator_selected))),
        replacement_exit_count=int(replacement_exit_count),
        replacement_candidate_count=int(replacement_candidate_count),
        remaining_slots=int(max(0, int(remaining_slots))),
        weakest_keep_score=float(weakest_keep_score),
        replacement_margin=float(margin),
        sleeve_candidate_counts={k: int(v) for k, v in sorted(candidate_counts.items())},
        sleeve_selected_counts={k: int(v) for k, v in sorted(selected_counts.items())},
        sleeve_budget_targets={k: int(v) for k, v in sorted(normalized_budget_targets.items()) if int(v) > 0},
        sleeve_budget_used={k: int(v) for k, v in sorted(selected_counts.items())},
        campaign_state_counts={k: int(v) for k, v in sorted(campaign_state_counts.items())},
        rejection_counts={k: int(v) for k, v in sorted(rejection_counts.items())},
        pair_pressure_avg=float(pair_pressure_avg),
        pair_pressure_max=float(pair_pressure_max),
        session_pressure_avg=float(session_pressure_avg),
        session_pressure_max=float(session_pressure_max),
        sleeve_pressure_avg=float(sleeve_pressure_avg),
        sleeve_pressure_max=float(sleeve_pressure_max),
        correlation_pressure_avg=float(correlation_pressure_avg),
        correlation_pressure_max=float(correlation_pressure_max),
        risk_pressure_avg=float(risk_pressure_avg),
        risk_pressure_max=float(risk_pressure_max),
    )
    return out_ranked, summary
