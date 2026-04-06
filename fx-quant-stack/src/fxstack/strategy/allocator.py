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
from typing import Any

from fxstack.strategy.allocator_types import (
    AllocatorCandidate,
    AllocatorConfig,
    AllocatorCycleSummary,
    AllocatorOpenPosition,
    SleeveHealthSnapshot,
)
from fxstack.strategy.sleeve_governance import sleeve_health_penalty


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def playbook_to_sleeve(playbook: str) -> str:
    txt = str(playbook or "").strip()
    return txt if txt else "no_trade"


def allocator_config_from_settings(settings: Any) -> AllocatorConfig:
    return AllocatorConfig(
        max_total_positions=max(0, int(getattr(settings, "max_total_positions", 0) or 0)),
        max_pair_positions=max(0, int(getattr(settings, "max_pair_positions", 0) or 0)),
        max_new_entries=max(1, int(getattr(settings, "max_new_entries_per_cycle", 1) or 1)),
        max_spread_bps=float(getattr(settings, "max_allowed_spread_bps", 0.0) or 0.0),
        min_expected_edge_bps=float(getattr(settings, "min_expected_edge_bps", 1.0) or 1.0),
    )


def spread_cost_penalty(*, spread_bps: float, max_spread_bps: float) -> float:
    max_spread = max(float(max_spread_bps), 1e-9)
    return _clip01(float(spread_bps) / max_spread)


def replacement_pressure_score(open_positions: list[AllocatorOpenPosition]) -> float:
    if not open_positions:
        return 0.0
    weakest_keep = min(float(item.keep_score) for item in open_positions)
    replaceable_share = sum(1 for item in open_positions if bool(item.replaceable_hold)) / max(1, len(open_positions))
    return _clip01((1.0 - weakest_keep) * 0.70 + replaceable_share * 0.30)


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
    max_edge = max(float(config.min_expected_edge_bps) * 3.0, 1e-9)
    ev_rank = _clip01(float(candidate.expected_edge_bps) / max_edge)
    spread_penalty = spread_cost_penalty(
        spread_bps=float(candidate.spread_bps),
        max_spread_bps=float(candidate.max_spread_bps or config.max_spread_bps),
    )
    pressure = replacement_pressure_score(open_positions)
    sleeve_snapshot = sleeve_health or SleeveHealthSnapshot(sleeve=str(candidate.sleeve))
    governance_penalty = sleeve_health_penalty(sleeve_snapshot)
    budget_pressure = _clip01(float(candidate.sleeve_budget_pressure))
    score = (
        (0.26 * float(candidate.adaptive_entry_quality))
        + (0.14 * float(candidate.conviction_score))
        + (0.16 * float(candidate.playbook_score))
        + (0.12 * float(candidate.location_score))
        + (0.08 * float(candidate.trigger_score))
        + (0.10 * float(ev_rank))
        + (0.05 * float(candidate.macro_coherence_score))
        + (0.05 * float(sleeve_snapshot.score))
        + (0.04 * float(pressure))
        + float(conviction_band_bonus(candidate.conviction_band))
        + float(thesis_stage_bonus(candidate.thesis_stage))
        + float(candidate.campaign_priority_boost)
        - (0.10 * float(candidate.uncertainty_score))
        - (0.08 * float(candidate.currency_crowding_penalty))
        - (0.05 * float(candidate.playbook_diversification_penalty))
        - (0.05 * float(spread_penalty))
        - (0.05 * float(budget_pressure))
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
    config: AllocatorConfig,
    open_positions: list[AllocatorOpenPosition],
    sleeve_health: SleeveHealthSnapshot | None,
) -> AllocatorCandidate:
    sleeve_snapshot = sleeve_health or SleeveHealthSnapshot(sleeve=str(sleeve or ""))
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
        playbook_score=float(playbook_score),
        location_score=float(location_score),
        trigger_score=float(trigger_score),
        adaptive_entry_quality=float(adaptive_entry_quality),
        expected_edge_bps=float(expected_edge_bps),
        uncertainty_score=float(uncertainty_score),
        spread_bps=float(spread_bps),
        max_spread_bps=float(max_spread_bps),
        macro_coherence_score=float(macro_coherence_score),
        currency_crowding_penalty=float(currency_crowding_penalty),
        playbook_diversification_penalty=float(playbook_diversification_penalty),
        sleeve_health_score=float(sleeve_snapshot.score),
        sleeve_health_state=str(sleeve_snapshot.state),
        thesis_id=str(thesis_id),
        campaign_seq=int(campaign_seq),
        campaign_entry_kind=str(campaign_entry_kind),
        campaign_state=str(campaign_state),
        campaign_state_reason=str(campaign_state_reason),
        campaign_priority_boost=float(campaign_priority_boost),
        campaign_proof_score=float(campaign_proof_score),
        campaign_maturity_score=float(campaign_maturity_score),
        campaign_reset_quality=float(campaign_reset_quality),
        campaign_reentry_blocked=bool(campaign_reentry_blocked),
        conviction_score=float(conviction_score),
        conviction_band=str(conviction_band),
        thesis_stage=str(thesis_stage),
        portfolio_posture=str(portfolio_posture),
        replacement_urgency=float(replacement_urgency),
        sleeve_budget_target=int(sleeve_budget_target),
        sleeve_budget_used=int(sleeve_budget_used),
        sleeve_budget_pressure=float(sleeve_budget_pressure),
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
            float(item.allocator_score),
            float(item.adaptive_entry_quality),
            float(item.playbook_score),
            float(item.expected_edge_bps),
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
    margin = float(config.tempo_gap_replacement_margin if tempo_gap_active else config.replacement_margin)
    budget_targets = {str(k): max(0, int(v)) for k, v in dict(sleeve_budget_targets or {}).items()}
    replaceable = sorted(
        [
            item
            for item in open_positions
            if bool(item.replaceable_hold)
            and (not bool(item.protected_hold))
            and float(item.age_bars) >= float(config.protected_hold_window_bars)
            and float(item.keep_score) < 0.62
        ],
        key=lambda item: float(item.keep_score),
    )
    selected_by_id: set[str] = set()
    replacement_exit_count = 0
    replacement_candidate_count = 0
    weakest_keep_score = float(replaceable[0].keep_score) if replaceable else 0.0
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
        selected = str(item.candidate_id) in budget_selected_ids
        rejection_reason = "allocator_ranked_out"
        replacement_target_pair = ""
        replacement_value = 0.0
        if selected:
            rejection_reason = "selected_budgeted"
        elif len(selected_by_id) < max(0, int(remaining_slots)):
            selected = True
            rejection_reason = "selected" if not budget_targets else "selected_global_fill"
        elif replaceable:
            weakest = replaceable[0]
            if float(item.allocator_score) >= (float(weakest.keep_score) + margin):
                selected = True
                replacement_candidate_count += 1
                replacement_exit_count += 1
                replacement_target_pair = str(weakest.pair)
                replacement_value = float(item.allocator_score) - float(weakest.keep_score)
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
    )
    return out_ranked, summary
