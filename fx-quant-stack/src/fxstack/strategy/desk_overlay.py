from __future__ import annotations

from dataclasses import replace
from typing import Any

from fxstack.strategy.desk_overlay_types import (
    ConvictionBand,
    DeskOverlayInputs,
    DeskOverlayOutput,
    DeskOverlayTraceStage,
    PortfolioPosture,
    SleevePolicyProfile,
    SleeveBudgetGuidance,
    ThesisStage,
)


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _num(mapping: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in mapping:
            try:
                return float(mapping.get(key, default) or default)
            except Exception:
                return float(default)
    return float(default)


def _text(mapping: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = str(mapping.get(key, "") or "").strip()
        if value:
            return value
    return default


def _band(score: float) -> ConvictionBand:
    if score >= 0.85:
        return "extreme"
    if score >= 0.65:
        return "high"
    if score >= 0.40:
        return "medium"
    return "low"


def _profile_for_sleeve(sleeve: str) -> SleevePolicyProfile:
    sleeve_key = str(sleeve or "").strip()
    profile = SleevePolicyProfile(sleeve=sleeve_key or "unknown")
    if sleeve_key == "trend_pullback":
        return SleevePolicyProfile(
            sleeve=sleeve_key,
            aggression_bias=0.44,
            press_min_conviction=0.80,
            press_min_confirmation=0.72,
            harvest_maturity=0.72,
            stand_down_fail_fast=0.55,
        )
    if sleeve_key == "range_mean_reversion":
        return SleevePolicyProfile(
            sleeve=sleeve_key,
            aggression_bias=0.40,
            press_min_conviction=0.78,
            press_min_confirmation=0.70,
            harvest_maturity=0.58,
            stand_down_fail_fast=0.52,
        )
    if sleeve_key == "breakout_expansion":
        return SleevePolicyProfile(
            sleeve=sleeve_key,
            aggression_bias=0.72,
            press_min_conviction=0.72,
            press_min_confirmation=0.62,
            harvest_maturity=0.70,
            stand_down_fail_fast=0.62,
        )
    if sleeve_key == "failed_breakout_reversal":
        return SleevePolicyProfile(
            sleeve=sleeve_key,
            aggression_bias=0.68,
            press_min_conviction=0.70,
            press_min_confirmation=0.60,
            harvest_maturity=0.64,
            stand_down_fail_fast=0.60,
        )
    return profile


def _stage_from_state(
    campaign_state: str,
    *,
    sleeve: str,
    conviction: float,
    recent_edge: float,
    confirm_prob: float,
    fail_fast_risk: float,
    hostility: float,
) -> ThesisStage:
    state = str(campaign_state or "").strip().lower()
    profile = _profile_for_sleeve(sleeve)
    if state in {"abandoned"}:
        return "stand_down"
    if fail_fast_risk >= profile.stand_down_fail_fast or hostility >= 0.72:
        return "stand_down"
    if state in {"harvest"}:
        return "harvest"
    if (
        state in {"press"}
        and conviction >= profile.press_min_conviction
        and confirm_prob >= profile.press_min_confirmation
        and hostility <= 0.38
    ):
        return "press"
    if state in {"confirmed", "re_attack_ready"} and conviction >= 0.48:
        return "core"
    if recent_edge >= 0.50 or conviction >= 0.48 or confirm_prob >= 0.54:
        return "scout"
    return "stand_down"


def _posture(
    *,
    sleeve: str,
    conviction: float,
    sleeve_health: float,
    crowding: float,
    recent_pnl: float,
    confirm_prob: float,
    hostility: float,
    portfolio_capacity: float,
) -> PortfolioPosture:
    fast_press_sleeve = str(sleeve or "") in {"breakout_expansion", "failed_breakout_reversal"}
    if conviction < 0.35 or sleeve_health < 0.40 or recent_pnl < -0.15 or hostility >= 0.60:
        return "capital_preservation"
    if (
        fast_press_sleeve
        and conviction >= 0.72
        and confirm_prob >= 0.62
        and sleeve_health >= 0.62
        and crowding <= 0.35
        and hostility <= 0.32
        and portfolio_capacity >= 0.45
    ):
        return "selective_press"
    if conviction >= 0.55 and sleeve_health >= 0.50:
        return "constructive_rotation"
    return "balanced_probe"


def _budget_tilt(stage: str, conviction: float, sleeve_health: float, crowding: float, hostility: float) -> str:
    if stage == "stand_down" or sleeve_health < 0.35:
        return "reduce"
    if stage == "harvest" or crowding >= 0.70 or hostility >= 0.60:
        return "reduce"
    if stage == "press" and conviction >= 0.78 and crowding <= 0.30 and hostility <= 0.30:
        return "concentrate"
    if conviction >= 0.80 and crowding <= 0.30:
        return "concentrate"
    if conviction >= 0.55:
        return "add"
    return "neutral"


def build_desk_overlay(inputs: DeskOverlayInputs) -> DeskOverlayOutput:
    belief = dict(inputs.belief_metrics or {})
    playbook = dict(inputs.adaptive_playbook_metrics or {})
    campaign = dict(inputs.campaign_state or {})
    sleeve = dict(inputs.sleeve_health or {})
    crowding = dict(inputs.crowding or {})
    perf = dict(inputs.recent_performance or {})
    portfolio = dict(inputs.portfolio or {})
    sleeve_name = _text(inputs.sleeve_health, "sleeve", default=_text(playbook, "sleeve", default="unknown"))
    profile = _profile_for_sleeve(sleeve_name)

    belief_gap = _clip01(_num(belief, "belief_gap"))
    confirm_prob = _clip01(_num(belief, "confirm_prob", default=_num(belief, "confidence")))
    ev_score = _clip01((_num(belief, "expected_net_ev_bps", default=0.0) + 4.0) / 12.0)
    fail_fast_risk = _clip01(_num(belief, "fail_fast_risk"))
    hostility_score = _clip01(_num(playbook, "hostility_score"))
    portfolio_capacity = _clip01(1.0 - _num(crowding, "portfolio_concentration"))

    belief_score = _clip01(
        0.26 * _clip01(_num(belief, "directional_belief", "belief_score"))
        + 0.18 * confirm_prob
        + 0.16 * belief_gap
        + 0.14 * ev_score
        + 0.14 * _clip01(_num(belief, "model_agreement"))
        + 0.12 * _clip01(_num(belief, "signal_quality"))
    )
    playbook_score = _clip01(
        0.45 * _clip01(_num(playbook, "adaptive_entry_quality", "entry_quality"))
        + 0.25 * _clip01(_num(playbook, "playbook_score"))
        + 0.15 * _clip01(_num(playbook, "location_score"))
        + 0.15 * _clip01(_num(playbook, "trigger_score"))
    )
    sleeve_health_score = _clip01(_num(sleeve, "score", default=0.5))
    crowding_penalty = _clip01(
        0.50 * _clip01(_num(crowding, "currency_crowding", "crowding"))
        + 0.30 * _clip01(_num(crowding, "pair_crowding"))
        + 0.20 * _clip01(_num(crowding, "portfolio_concentration"))
    )
    perf_score = _clip01(
        0.45 * _clip01(_num(perf, "win_rate"))
        + 0.35 * _clip01((_num(perf, "expectancy_usd", default=0.0) + 50.0) / 100.0)
        + 0.20 * _clip01(_num(perf, "profit_factor", default=1.0) / 2.0)
    )
    campaign_boost = _clip01(
        0.40 * _clip01(_num(campaign, "proof_score"))
        + 0.30 * _clip01(_num(campaign, "maturity_score"))
        + 0.20 * _clip01(_num(campaign, "reset_quality"))
        + 0.10 * _clip01(_num(campaign, "priority_boost"))
    )
    fail_fast_penalty = _clip01((0.65 * fail_fast_risk) + (0.35 * hostility_score))
    replacement_pressure = _clip01(
        0.45 * crowding_penalty
        + 0.30 * _clip01(_num(portfolio, "replacement_pressure"))
        + 0.25 * _clip01(1.0 - sleeve_health_score)
    )

    conviction = _clip01(
        0.24 * belief_score
        + 0.24 * playbook_score
        + 0.14 * sleeve_health_score
        + 0.10 * perf_score
        + 0.14 * campaign_boost
        + 0.08 * confirm_prob
        + 0.06 * portfolio_capacity
        + 0.04 * profile.aggression_bias
        - 0.22 * crowding_penalty
        - 0.16 * fail_fast_penalty
    )
    if str(campaign.get("state", "")).strip().lower() == "abandoned":
        conviction = min(conviction, 0.25)

    stage = _stage_from_state(
        str(campaign.get("state", "")),
        sleeve=sleeve_name,
        conviction=conviction,
        recent_edge=playbook_score,
        confirm_prob=confirm_prob,
        fail_fast_risk=fail_fast_risk,
        hostility=hostility_score,
    )
    posture = _posture(
        sleeve=sleeve_name,
        conviction=conviction,
        sleeve_health=sleeve_health_score,
        crowding=crowding_penalty,
        recent_pnl=_clip01(_num(perf, "recent_pnl_trend", "pnl_trend", default=0.0) / 2.0 + 0.5),
        confirm_prob=confirm_prob,
        hostility=hostility_score,
        portfolio_capacity=portfolio_capacity,
    )
    budget_tilt = _budget_tilt(stage, conviction, sleeve_health_score, crowding_penalty, hostility_score)

    target_share = _clip01(
        0.10
        + (0.22 * conviction)
        + (0.08 * sleeve_health_score)
        + (0.06 * portfolio_capacity)
        - (0.10 * crowding_penalty)
    )
    max_share = _clip01(target_share + 0.12 + (0.05 if posture == "selective_press" else 0.0))
    min_share = _clip01(max(0.0, target_share - 0.08))

    if budget_tilt == "reduce":
        target_share = _clip01(target_share * 0.70)
        max_share = _clip01(max_share * 0.80)
    elif budget_tilt == "concentrate":
        target_share = _clip01(min(0.40, target_share * 1.20))
        max_share = _clip01(min(0.55, max_share * 1.15))

    trace = [
        DeskOverlayTraceStage(
            stage="belief",
            score=belief_score,
            note="directional conviction seed",
            details={
                **dict(belief),
                "belief_gap": belief_gap,
                "confirm_prob": confirm_prob,
                "expected_net_ev_score": ev_score,
                "fail_fast_risk": fail_fast_risk,
            },
        ),
        DeskOverlayTraceStage(
            stage="playbook",
            score=playbook_score,
            note="adaptive playbook quality",
            details={**dict(playbook), "hostility_score": hostility_score},
        ),
        DeskOverlayTraceStage(
            stage="campaign",
            score=campaign_boost,
            note="thesis lifecycle momentum",
            details=dict(campaign),
        ),
        DeskOverlayTraceStage(
            stage="sleeve_health",
            score=sleeve_health_score,
            note="sleeve governance overlay",
            details=dict(sleeve),
        ),
        DeskOverlayTraceStage(
            stage="crowding",
            score=1.0 - crowding_penalty,
            note="portfolio congestion drag",
            details=dict(crowding),
        ),
        DeskOverlayTraceStage(
            stage="performance",
            score=perf_score,
            note="recent desk feedback",
            details=dict(perf),
        ),
        DeskOverlayTraceStage(
            stage="final",
            score=conviction,
            note="combined shadow-first overlay",
            details={
                "conviction_band": _band(conviction),
                "thesis_stage": stage,
                "portfolio_posture": posture,
                "budget_tilt": budget_tilt,
                "replacement_pressure": replacement_pressure,
                "portfolio_capacity": portfolio_capacity,
                "profile": {
                    "sleeve": profile.sleeve,
                    "aggression_bias": profile.aggression_bias,
                    "press_min_conviction": profile.press_min_conviction,
                    "press_min_confirmation": profile.press_min_confirmation,
                },
            },
        ),
    ]

    guidance = {
        sleeve_name: SleeveBudgetGuidance(
            sleeve=sleeve_name,
            tilt=budget_tilt,
            target_share=float(target_share),
            max_share=float(max_share),
            min_share=float(min_share),
            reason=f"{stage}:{posture}:{budget_tilt}",
        )
    }
    if _text(portfolio, "secondary_sleeve"):
        sec = _text(portfolio, "secondary_sleeve")
        guidance[sec] = replace(
            guidance[sleeve_name],
            sleeve=sec,
            tilt="neutral" if budget_tilt == "concentrate" else budget_tilt,
            target_share=float(_clip01(target_share * 0.75)),
            max_share=float(_clip01(max_share * 0.75)),
            min_share=float(_clip01(min_share * 0.50)),
            reason=f"spillover:{stage}",
        )

    return DeskOverlayOutput(
        conviction_score=float(conviction),
        conviction_band=str(_band(conviction)),
        thesis_stage=str(stage),
        portfolio_posture=str(posture),
        sleeve_budget_guidance=guidance,
        replacement_urgency=float(replacement_pressure),
        policy_profile=profile,
        trace=trace,
    )
