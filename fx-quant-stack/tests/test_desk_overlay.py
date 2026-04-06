from __future__ import annotations

from fxstack.strategy.desk_overlay import build_desk_overlay
from fxstack.strategy.desk_overlay_types import DeskOverlayInputs


def test_desk_overlay_keeps_trend_sleeves_constructive_before_pressing() -> None:
    out = build_desk_overlay(
        DeskOverlayInputs(
            belief_metrics={
                "directional_belief": 0.9,
                "confidence": 0.85,
                "model_agreement": 0.8,
                "signal_quality": 0.75,
            },
            adaptive_playbook_metrics={
                "adaptive_entry_quality": 0.82,
                "playbook_score": 0.8,
                "location_score": 0.72,
                "trigger_score": 0.68,
            },
            campaign_state={
                "state": "confirmed",
                "proof_score": 0.8,
                "maturity_score": 0.55,
                "reset_quality": 0.7,
                "priority_boost": 0.05,
            },
            sleeve_health={"sleeve": "trend_pullback", "score": 0.82},
            crowding={"currency_crowding": 0.18, "pair_crowding": 0.12, "portfolio_concentration": 0.2},
            recent_performance={"win_rate": 0.68, "expectancy_usd": 42.0, "profit_factor": 1.4, "recent_pnl_trend": 0.4},
            portfolio={"replacement_pressure": 0.15},
        )
    )

    assert out.conviction_score >= 0.65
    assert out.conviction_band in {"high", "extreme"}
    assert out.thesis_stage == "core"
    assert out.portfolio_posture == "constructive_rotation"
    assert out.replacement_urgency < 0.5
    assert "trend_pullback" in out.sleeve_budget_guidance
    assert out.trace[-1].stage == "final"


def test_desk_overlay_selectively_presses_breakout_sleeves_when_confirmation_is_strong() -> None:
    out = build_desk_overlay(
        DeskOverlayInputs(
            belief_metrics={
                "directional_belief": 0.92,
                "belief_gap": 0.74,
                "confidence": 0.88,
                "confirm_prob": 0.81,
                "model_agreement": 0.84,
                "signal_quality": 0.78,
                "expected_net_ev_bps": 9.5,
                "fail_fast_risk": 0.16,
            },
            adaptive_playbook_metrics={
                "sleeve": "breakout_expansion",
                "adaptive_entry_quality": 0.86,
                "playbook_score": 0.84,
                "location_score": 0.76,
                "trigger_score": 0.83,
                "hostility_score": 0.18,
            },
            campaign_state={
                "state": "press",
                "proof_score": 0.86,
                "maturity_score": 0.62,
                "reset_quality": 0.72,
                "priority_boost": 0.08,
            },
            sleeve_health={"sleeve": "breakout_expansion", "score": 0.78},
            crowding={"currency_crowding": 0.14, "pair_crowding": 0.12, "portfolio_concentration": 0.18},
            recent_performance={"win_rate": 0.64, "expectancy_usd": 38.0, "profit_factor": 1.36, "recent_pnl_trend": 0.32},
            portfolio={"replacement_pressure": 0.12},
        )
    )

    assert out.conviction_band in {"high", "extreme"}
    assert out.thesis_stage == "press"
    assert out.portfolio_posture == "selective_press"
    assert out.sleeve_budget_guidance["breakout_expansion"].tilt in {"add", "concentrate"}


def test_desk_overlay_reduces_budget_when_campaign_is_abandoned_and_crowded() -> None:
    out = build_desk_overlay(
        DeskOverlayInputs(
            belief_metrics={"directional_belief": 0.3, "confidence": 0.25, "model_agreement": 0.2},
            adaptive_playbook_metrics={"adaptive_entry_quality": 0.22, "playbook_score": 0.28, "location_score": 0.2, "trigger_score": 0.18},
            campaign_state={"state": "abandoned", "proof_score": 0.1, "maturity_score": 0.05, "reset_quality": 0.08},
            sleeve_health={"sleeve": "range_mean_reversion", "score": 0.32},
            crowding={"currency_crowding": 0.8, "pair_crowding": 0.7, "portfolio_concentration": 0.9},
            recent_performance={"win_rate": 0.22, "expectancy_usd": -18.0, "profit_factor": 0.72, "recent_pnl_trend": -0.6},
            portfolio={"replacement_pressure": 0.72},
        )
    )

    guidance = next(iter(out.sleeve_budget_guidance.values()))
    assert out.thesis_stage == "stand_down"
    assert out.portfolio_posture == "capital_preservation"
    assert out.conviction_band == "low"
    assert out.replacement_urgency >= 0.6
    assert guidance.tilt == "reduce"
    assert guidance.target_share < guidance.max_share


def test_desk_overlay_emits_trace_stages_for_runtime_integration() -> None:
    out = build_desk_overlay(
        DeskOverlayInputs(
            belief_metrics={"directional_belief": 0.6, "confidence": 0.55},
            adaptive_playbook_metrics={"adaptive_entry_quality": 0.5, "playbook_score": 0.55},
            campaign_state={"state": "re_attack_ready", "proof_score": 0.45, "maturity_score": 0.35, "reset_quality": 0.65},
            sleeve_health={"sleeve": "breakout_expansion", "score": 0.6},
            crowding={"currency_crowding": 0.4, "pair_crowding": 0.35},
            recent_performance={"win_rate": 0.5, "expectancy_usd": 8.0, "profit_factor": 1.05},
            portfolio={"secondary_sleeve": "failed_breakout_reversal"},
        )
    )

    stages = [stage.stage for stage in out.trace]
    assert stages == ["belief", "playbook", "campaign", "sleeve_health", "crowding", "performance", "final"]
    assert "failed_breakout_reversal" in out.sleeve_budget_guidance
    assert out.sleeve_budget_guidance["failed_breakout_reversal"].reason.startswith("spillover:")
