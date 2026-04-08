from __future__ import annotations

import pytest
import pandas as pd

from fxstack.live.policy import (
    compute_structure_timing_diagnostics,
    compute_live_uncertainty_score,
    compute_shadow_entry_diagnostics,
    gate_decision,
    is_entry_session_blocked,
    session_bucket_from_ts,
)
from fxstack.runtime.runner import (
    _apply_adaptive_shadow_ranking,
    _apply_rl_lifecycle_router,
    _challenger_conflict_payload,
    _challenger_conflict_can_gate,
    _finalize_entry_submissions,
    _apply_shadow_entry_ranking,
    _build_lifecycle_row,
    _partial_close_guard,
    _position_side,
    _position_signature,
    _reversal_blocking_reasons,
    _score_binary_lifecycle_model,
    _score_exit_policy_model,
    _shadow_entry_safety_reasons,
)


def test_directional_short_swing_gate_uses_directional_confidence() -> None:
    out = gate_decision(
        swing_prob=0.35,
        entry_prob=0.72,
        trade_prob=0.62,
        spread_bps=1.08,
        expected_edge_bps=9.99,
        side="short",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.6,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
    )
    assert out.allowed is True
    assert out.reason == "approved"


def test_reversal_blocking_reasons_ignore_exposure_caps() -> None:
    assert _reversal_blocking_reasons(["pair_exposure_cap"]) == []
    assert _reversal_blocking_reasons(["portfolio_exposure_cap"]) == []
    assert _reversal_blocking_reasons(["pair_exposure_cap", "weak_swing"]) == ["weak_swing"]


def test_position_side_recognizes_type_and_side_variants() -> None:
    assert _position_side([{"type": 0, "symbol": "EURAUD"}]) == "long"
    assert _position_side([{"type": "1", "symbol": "EURAUD"}]) == "short"
    assert _position_side([{"side": "BUY", "symbol": "EURAUD"}]) == "long"
    assert _position_side([{"position_side": "short", "symbol": "EURAUD"}]) == "short"


def test_reversal_context_requires_opposite_open_side() -> None:
    desired_side = "long"
    pos_side = "long"
    reversal_context_active = desired_side != "flat" and pos_side != "flat" and desired_side != pos_side
    reversal_ready = reversal_context_active and True and len(_reversal_blocking_reasons(["pair_exposure_cap"])) == 0
    assert reversal_context_active is False
    assert reversal_ready is False


def test_build_lifecycle_row_injects_live_position_state() -> None:
    row = pd.DataFrame([{"ts": "2026-03-24T10:00:00Z", "edge_decay_12": 0.25, "h1_ret_1": 0.01}])
    out = _build_lifecycle_row(
        row=row,
        positions=[{"open_time": 1_800.0}],
        total_position_count=2,
        loop_ts=2_400.0,
        timeframe="M5",
    )
    assert float(out.iloc[0]["time_in_trade_bars"]) == 2.0
    assert float(out.iloc[0]["open_position_count"]) == 2.0
    assert float(out.iloc[0]["live_edge_decay"]) == 0.25
    assert float(out.iloc[0]["h1_available"]) == 1.0


def test_score_exit_policy_model_maps_class_ids_to_actions() -> None:
    class DummyExitModel:
        def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame([{"p0": 0.1, "p1": 0.7, "p2": 0.2}], index=X.index)

    out = _score_exit_policy_model(
        DummyExitModel(),
        pd.DataFrame([{"x": 1.0}]),
        action_labels={0: "hold", 1: "partial_tp", 2: "exit"},
    )
    assert out["selected"] == "partial_tp"
    assert out["score"] == 0.7
    assert out["probs"]["partial_tp"] == 0.7


def test_score_binary_lifecycle_model_returns_p1() -> None:
    class DummyBinaryModel:
        def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame([{"p0": 0.35, "p1": 0.65}], index=X.index)

    assert _score_binary_lifecycle_model(DummyBinaryModel(), pd.DataFrame([{"x": 1.0}])) == 0.65


def test_position_signature_stable_across_partial_close_lot_changes() -> None:
    first = {
        "symbol": "EURAUD",
        "type": 1,
        "open_time": 1_774_350_120.0,
        "open_price": 1.66167,
        "lots": 0.31,
        "magic": 246810,
    }
    second = dict(first)
    second["lots"] = 0.08
    assert _position_signature(first) == _position_signature(second)


def test_partial_close_guard_blocks_during_cooldown() -> None:
    class Settings:
        partial_close_cooldown_secs = 1800.0
        max_partial_closes_per_position = 2

    allowed, reason, remaining = _partial_close_guard(
        tracker_state={"count": 1, "last_partial_ts": 1_000.0},
        loop_ts=2_000.0,
        settings=Settings(),
    )
    assert allowed is False
    assert reason == "partial_tp_cooldown_active"
    assert remaining == 800.0


def test_partial_close_guard_blocks_after_max_partials() -> None:
    class Settings:
        partial_close_cooldown_secs = 1800.0
        max_partial_closes_per_position = 2

    allowed, reason, remaining = _partial_close_guard(
        tracker_state={"count": 2, "last_partial_ts": 0.0},
        loop_ts=5_000.0,
        settings=Settings(),
    )
    assert allowed is False
    assert reason == "partial_tp_limit_reached"
    assert remaining == 0.0


def test_partial_close_guard_allows_first_partial() -> None:
    class Settings:
        partial_close_cooldown_secs = 1800.0
        max_partial_closes_per_position = 2

    allowed, reason, remaining = _partial_close_guard(
        tracker_state={},
        loop_ts=5_000.0,
        settings=Settings(),
    )
    assert allowed is True
    assert reason == ""
    assert remaining == 0.0


def test_compute_shadow_entry_diagnostics_penalizes_uncertainty_and_disagreement() -> None:
    out = compute_shadow_entry_diagnostics(
        row={},
        swing_prob=0.68,
        entry_prob=0.72,
        trade_prob=0.66,
        regime_prob=0.61,
        expected_edge_bps=9.0,
        spread_bps=1.0,
        uncertainty_score=0.30,
        side="long",
        pair_tier="tier2",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        min_expected_edge_bps=3.0,
        use_uncertainty_gate=True,
        max_entry_uncertainty=0.25,
        use_structure_timing_shadow=True,
        structure_timing_rescue_min_score=0.66,
        structure_timing_entry_rescue_margin=0.05,
        structure_timing_max_chase_risk=0.78,
        entry_hysteresis_margin_bps=1.0,
    )
    assert out.directional_swing_confidence == 0.68
    assert out.entry_margin > 0.0
    assert out.meta_margin > 0.0
    assert out.model_disagreement_score >= 0.0
    assert out.calibrated_ev_bps > 7.5
    assert out.entry_quality_score < out.calibrated_ev_bps
    assert out.floor_ok is False
    assert out.floor_rejection_reason == "shadow_uncertainty_gate"


def test_challenger_conflict_payload_supports_telemetry_mode() -> None:
    out = _challenger_conflict_payload(
        disagreement={"swing_patchtst_vs_live": 0.24, "intraday_patchtst_vs_live": 0.11},
        report_refs={"swing_patchtst": {"training_report": "t"}, "intraday_patchtst": {"training_report": "t"}},
        mode="telemetry",
    )
    assert out["mode"] == "telemetry"
    assert out["active"] is True
    assert out["gate_level"] == "telemetry"
    assert out["verdict"] == "telemetry"
    assert out["gate_ready"] is False
    assert out["sign_flip"] is False
    assert _challenger_conflict_can_gate(out) is False


def test_challenger_conflict_payload_soft_and_hard_gate_modes() -> None:
    soft = _challenger_conflict_payload(
        disagreement={"swing_patchtst_vs_live": 0.24, "intraday_patchtst_vs_live": 0.11},
        report_refs={"swing_patchtst": {"training_report": "t"}, "intraday_patchtst": {"training_report": "t"}},
        mode="soft_gate",
    )
    hard = _challenger_conflict_payload(
        disagreement={"swing_patchtst_vs_live": 0.41, "intraday_patchtst_vs_live": 0.11},
        report_refs={"swing_patchtst": {"training_report": "t"}, "intraday_patchtst": {"training_report": "t"}},
        mode="hard_gate",
    )
    off = _challenger_conflict_payload(
        disagreement={"swing_patchtst_vs_live": 0.41},
        report_refs={"swing_patchtst": {"training_report": "t"}},
        mode="off",
    )

    assert soft["mode"] == "soft_gate"
    assert soft["active"] is True
    assert soft["gate_level"] == "soft"
    assert soft["verdict"] == "soft_conflict"
    assert soft["gate_ready"] is True
    assert _challenger_conflict_can_gate(soft) is True

    assert hard["mode"] == "hard_gate"
    assert hard["active"] is True
    assert hard["gate_level"] == "hard"
    assert hard["verdict"] == "hard_conflict"
    assert hard["gate_ready"] is True
    assert _challenger_conflict_can_gate(hard) is True

    assert off["mode"] == "off"
    assert off["active"] is False
    assert off["gate_level"] == "none"
    assert off["verdict"] == "clear"
    assert off["gate_ready"] is False
    assert _challenger_conflict_can_gate(off) is False


def test_challenger_conflict_payload_requires_full_coverage_to_gate_entries() -> None:
    partial = _challenger_conflict_payload(
        disagreement={"swing_patchtst_vs_live": 0.24, "intraday_patchtst_vs_live": 0.11},
        report_refs={"swing_patchtst": {"training_report": "t"}},
        mode="soft_gate",
    )
    assert partial["verdict"] == "soft_conflict"
    assert partial["active"] is True
    assert partial["coverage_count"] == 2
    assert partial["evidence_count"] == 1
    assert partial["gate_ready"] is False
    assert partial["gate_reason"] == "insufficient_evidence"
    assert _challenger_conflict_can_gate(partial) is False


def test_apply_adaptive_shadow_ranking_surfaces_campaign_metadata() -> None:
    class Settings:
        adaptive_shadow_enabled = True
        adaptive_shadow_allow_adaptive_only = False
        adaptive_shadow_playbooks = "trend_pullback,range_mean_reversion,breakout_expansion,failed_breakout_reversal"
        adaptive_execution_enabled = False
        max_total_positions = 6
        max_pair_positions = 1
        max_spread_bps = 2.5
        min_expected_edge_bps = 3.0
        adaptive_entry_quality_floor = 0.52
        adaptive_aggressive_fallback_margin = 0.08
        campaign_manager_enabled = True
        campaign_shadow_only = True
        campaign_abandon_cooldown_bars = 8
        campaign_press_protected_bars = 4
        campaign_reattack_cooldown_scale = 0.5

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "metadata": {
                "pair": "EURUSD",
                "ts": "2026-03-20T10:00:00Z",
                "position_count_pair": 0,
                "strict_entry_ready": True,
                "strict_rejection_reason": "",
                "entry_blocking_reasons": [],
                "uncertainty_score": 0.10,
                "spread_bps": 1.0,
                "session_bucket": "london",
            },
        }
    ]
    adaptive_rows_by_pair = {
        "EURUSD": {
            "pair": "EURUSD",
            "signal_side": "long",
            "playbook": "trend_pullback",
            "environment_state": "CorrectiveTrend",
            "playbook_score": 0.74,
            "location_score": 0.72,
            "trigger_score": 0.67,
            "macro_coherence_score": 0.63,
            "hostility_score": 0.08,
            "extension_penalty_score": 0.25,
            "uncertainty_score": 0.10,
            "spread_bps": 1.0,
            "session_bucket": "london",
            "calibrated_ev_bps_shadow": 9.0,
        }
    }
    state = {"equity": 12_500.0, "positions": []}
    out = _apply_adaptive_shadow_ranking(
        decisions,
        settings=Settings(),
        open_position_count=0,
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        adaptive_position_registry={},
        recent_exit_registry={},
        pair_bar_index={"EURUSD": 10},
        sleeve_health_snapshots={},
        campaign_registry={},
        state=state,
        current_equity=12_500.0,
    )
    meta = decisions[0]["metadata"]
    assert "campaign_state" in meta
    assert "thesis_id" in meta
    assert "campaign_priority_boost" in meta
    assert isinstance(out.get("campaign_state_counts", {}), dict)


def test_apply_adaptive_shadow_ranking_uses_live_state_for_open_positions() -> None:
    class Settings:
        adaptive_shadow_enabled = True
        adaptive_shadow_allow_adaptive_only = False
        adaptive_shadow_playbooks = "trend_pullback,range_mean_reversion,breakout_expansion,failed_breakout_reversal"
        adaptive_execution_enabled = False
        max_total_positions = 6
        max_pair_positions = 1
        max_spread_bps = 2.5
        min_expected_edge_bps = 3.0
        adaptive_entry_quality_floor = 0.52
        adaptive_aggressive_fallback_margin = 0.08
        campaign_manager_enabled = True
        campaign_shadow_only = True
        campaign_abandon_cooldown_bars = 8
        campaign_press_protected_bars = 4
        campaign_reattack_cooldown_scale = 0.5

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "metadata": {
                "pair": "EURUSD",
                "ts": "2026-03-20T10:00:00Z",
                "position_count_pair": 1,
                "position_signature": "EURUSD:long:1",
                "position_side": "long",
                "strict_entry_ready": True,
                "strict_rejection_reason": "",
                "entry_blocking_reasons": [],
                "entry_ready": True,
                "rejection_reason": "none",
                "session_bucket": "london",
                "spread_bps": 1.0,
                "calibrated_ev_bps_shadow": 9.0,
            },
        }
    ]
    adaptive_rows_by_pair = {
        "EURUSD": {
            "pair": "EURUSD",
            "signal_side": "long",
            "playbook": "trend_pullback",
            "environment_state": "CorrectiveTrend",
            "playbook_score": 0.74,
            "location_score": 0.72,
            "trigger_score": 0.67,
            "macro_coherence_score": 0.63,
            "hostility_score": 0.08,
            "extension_penalty_score": 0.25,
            "uncertainty_score": 0.10,
            "spread_bps": 1.0,
            "session_bucket": "london",
            "calibrated_ev_bps_shadow": 9.0,
        }
    }
    state = {
        "equity": 12_500.0,
        "positions": [
            {"symbol": "EURUSD", "lots": 0.25, "side": "long", "time_in_trade_bars": 8},
        ],
    }

    diag = _apply_adaptive_shadow_ranking(
        decisions,
        settings=Settings(),
        open_position_count=1,
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        adaptive_position_registry={},
        recent_exit_registry={},
        pair_bar_index={"EURUSD": 10},
        sleeve_health_snapshots={},
        campaign_registry={},
        state=state,
        current_equity=12_500.0,
    )

    assert diag["adaptive_shadow_candidate_count"] == 0
    assert diag["adaptive_shadow_live_divergence_counts"]["open_position"] == 1
    assert decisions[0]["metadata"]["adaptive_shadow_live_divergence"] == "open_position"
    assert decisions[0]["metadata"]["adaptive_shadow_would_trade"] is False
    assert decisions[0]["metadata"]["adaptive_shadow_rejection_reason"] == "adaptive_position_open"


def test_structure_timing_prefers_aligned_pullback_over_late_extension() -> None:
    good = compute_structure_timing_diagnostics(
        {
            "h1_trend_slope_20": 0.0021,
            "h4_trend_slope_20": 0.0035,
            "d_trend_slope_20": 0.0040,
            "h1_trend_strength_20": 1.4,
            "h4_trend_strength_20": 1.6,
            "trend_strength_20": 0.8,
            "trend_strength_60": 0.7,
            "pullback_depth_20": 0.0018,
            "ret_1": 0.0005,
            "ret_5": -0.0002,
            "ret_20": 0.0015,
            "bar_imbalance": 0.35,
            "micro_pressure": 0.30,
            "edge_decay_12": 0.0004,
            "vol_20": 0.0005,
            "vol_60": 0.0006,
        },
        side="long",
    )
    bad = compute_structure_timing_diagnostics(
        {
            "h1_trend_slope_20": 0.0018,
            "h4_trend_slope_20": 0.0030,
            "d_trend_slope_20": 0.0032,
            "h1_trend_strength_20": 2.6,
            "h4_trend_strength_20": 2.8,
            "trend_strength_20": 2.7,
            "trend_strength_60": 2.4,
            "pullback_depth_20": 0.0001,
            "ret_1": 0.0007,
            "ret_5": 0.0030,
            "ret_20": 0.0090,
            "bar_imbalance": 0.10,
            "micro_pressure": 0.15,
            "edge_decay_12": -0.0001,
            "vol_20": 0.0005,
            "vol_60": 0.0006,
        },
        side="long",
    )
    assert good.structure_timing_score > bad.structure_timing_score
    assert good.extension_penalty_score < bad.extension_penalty_score


def test_structure_timing_can_rescue_borderline_entry_without_chase() -> None:
    out = compute_shadow_entry_diagnostics(
        row={
            "h1_trend_slope_20": 0.0019,
            "h4_trend_slope_20": 0.0031,
            "d_trend_slope_20": 0.0034,
            "h1_trend_strength_20": 1.35,
            "h4_trend_strength_20": 1.45,
            "trend_strength_20": 0.95,
            "trend_strength_60": 0.85,
            "pullback_depth_20": 0.0019,
            "ret_1": 0.0004,
            "ret_5": -0.0001,
            "ret_20": 0.0018,
            "bar_imbalance": 0.28,
            "micro_pressure": 0.24,
            "edge_decay_12": 0.0003,
            "vol_20": 0.0005,
            "vol_60": 0.0006,
        },
        swing_prob=0.69,
        entry_prob=0.59,
        trade_prob=0.64,
        regime_prob=0.63,
        expected_edge_bps=4.0,
        spread_bps=1.0,
        uncertainty_score=0.18,
        side="long",
        pair_tier="tier1",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        min_expected_edge_bps=3.0,
        use_uncertainty_gate=True,
        max_entry_uncertainty=0.25,
        use_structure_timing_shadow=True,
        structure_timing_rescue_min_score=0.66,
        structure_timing_entry_rescue_margin=0.05,
        structure_timing_max_chase_risk=0.78,
        entry_hysteresis_margin_bps=1.0,
    )
    assert out.structure_rescue_active is True
    assert out.floor_ok is True
    assert out.floor_rejection_reason == "structure_timing_rescue"


def test_compute_live_uncertainty_score_uses_model_ambiguity_and_feature_anomaly() -> None:
    row = {
        "spread_z20": 2.4,
        "normalized_spread": 1.6,
        "vol_term_ratio": 1.8,
        "bar_imbalance": 0.4,
        "h1_available": 0.0,
    }
    out = compute_live_uncertainty_score(
        row,
        regime_prob=0.56,
        swing_prob=0.52,
        entry_prob=0.51,
        trade_prob=0.54,
        side="long",
    )
    assert 0.0 < out <= 1.0
    assert out > 0.5


def test_session_bucket_and_entry_block_detection() -> None:
    assert session_bucket_from_ts("2026-03-24T21:25:00Z") == "pacific"
    assert is_entry_session_blocked(session_bucket="pacific", blocked_sessions=["pacific"]) is True
    assert is_entry_session_blocked(session_bucket="london_open", blocked_sessions=["pacific"]) is False


def test_shadow_safety_reasons_treat_session_block_as_hard_guard() -> None:
    assert _shadow_entry_safety_reasons(["session_blocked:pacific", "weak_entry"]) == ["session_blocked:pacific"]


def test_shadow_entry_ranking_prefers_higher_quality_and_tracks_divergence() -> None:
    class Settings:
        shadow_policy_enabled = True
        max_total_positions = 6
        max_new_entries_per_cycle = 2
        use_portfolio_ranking = True
        tier1_pairs = ["EURUSD", "GBPUSD"]

    decisions = [
        {
            "symbol": "EURUSD",
            "reasons": [],
            "metadata": {
                "entry_ready": True,
                "position_count_pair": 0,
                "position_signature": "",
                "entry_blocking_reasons": [],
                "shadow_floor_ok": True,
                "shadow_floor_rejection_reason": "approved",
                "entry_quality_score_shadow": 10.0,
                "calibrated_ev_bps_shadow": 8.0,
                "trade_prob": 0.71,
                "expected_edge_bps": 9.5,
            },
        },
        {
            "symbol": "GBPUSD",
            "reasons": ["weak_entry"],
            "metadata": {
                "entry_ready": False,
                "position_count_pair": 0,
                "position_signature": "",
                "entry_blocking_reasons": ["weak_entry"],
                "shadow_floor_ok": True,
                "shadow_floor_rejection_reason": "approved",
                "entry_quality_score_shadow": 9.0,
                "calibrated_ev_bps_shadow": 7.0,
                "trade_prob": 0.69,
                "expected_edge_bps": 8.5,
            },
        },
        {
            "symbol": "AUDUSD",
            "reasons": [],
            "metadata": {
                "entry_ready": True,
                "position_count_pair": 0,
                "position_signature": "",
                "entry_blocking_reasons": [],
                "shadow_floor_ok": True,
                "shadow_floor_rejection_reason": "approved",
                "entry_quality_score_shadow": 6.0,
                "calibrated_ev_bps_shadow": 5.0,
                "trade_prob": 0.63,
                "expected_edge_bps": 6.5,
            },
        },
    ]

    diag = _apply_shadow_entry_ranking(decisions, settings=Settings(), open_position_count=0)

    assert diag["shadow_candidate_count"] == 3
    assert diag["shadow_would_trade_count"] == 2
    assert decisions[0]["metadata"]["portfolio_rank_shadow"] == 1
    assert decisions[0]["metadata"]["shadow_would_trade"] is True
    assert decisions[0]["metadata"]["shadow_live_divergence"] == "agree_ready"
    assert decisions[1]["metadata"]["portfolio_rank_shadow"] == 2
    assert decisions[1]["metadata"]["shadow_would_trade"] is True
    assert decisions[1]["metadata"]["shadow_rejection_reason"] == "none"
    assert decisions[1]["metadata"]["shadow_live_divergence"] == "shadow_only"
    assert decisions[2]["metadata"]["portfolio_rank_shadow"] == 3
    assert decisions[2]["metadata"]["shadow_rejection_reason"] == "shadow_ranked_out"
    assert decisions[2]["metadata"]["shadow_live_divergence"] == "live_only"
    assert diag["shadow_dominant_rejection_reason"] == "shadow_ranked_out"
    assert diag["shadow_rejection_reason_counts"]["shadow_ranked_out"] == 1
    assert diag["shadow_tier_summary"]["tier1"]["candidates"] == 2
    assert diag["shadow_tier_summary"]["tier1"]["would_trade"] == 2
    assert diag["shadow_tier_summary"]["tier2"]["candidates"] == 1
    assert diag["shadow_tier_summary"]["tier2"]["blocked"] == 1


def test_shadow_entry_ranking_tracks_spread_rejects_by_pair_and_session() -> None:
    class Settings:
        shadow_policy_enabled = True
        max_total_positions = 4
        max_new_entries_per_cycle = 2
        use_portfolio_ranking = True
        tier1_pairs = ["EURUSD"]

    decisions = [
        {
            "symbol": "EURUSD",
            "reasons": ["spread_too_wide"],
            "metadata": {
                "ts": "2026-03-24T13:05:00Z",
                "entry_ready": False,
                "position_count_pair": 0,
                "position_signature": "",
                "entry_blocking_reasons": ["spread_too_wide"],
                "shadow_floor_ok": False,
                "shadow_floor_rejection_reason": "spread_too_wide",
                "spread_bps": 3.4,
                "max_spread_bps": 2.5,
            },
        },
        {
            "symbol": "GBPUSD",
            "reasons": ["spread_too_wide"],
            "metadata": {
                "ts": "2026-03-24T13:10:00Z",
                "entry_ready": False,
                "position_count_pair": 0,
                "position_signature": "",
                "entry_blocking_reasons": ["spread_too_wide"],
                "shadow_floor_ok": False,
                "shadow_floor_rejection_reason": "spread_too_wide",
                "spread_bps": 3.1,
                "max_spread_bps": 2.5,
            },
        },
    ]

    diag = _apply_shadow_entry_ranking(decisions, settings=Settings(), open_position_count=0)

    spread_diag = diag["shadow_spread_diagnostics"]
    assert spread_diag["reject_count"] == 2
    assert spread_diag["dominant_session"] == "london_ny_overlap"
    assert spread_diag["dominant_pair"] == "EURUSD"
    assert spread_diag["by_pair"]["EURUSD"]["count"] == 1
    assert spread_diag["by_pair"]["EURUSD"]["avg_excess_bps"] == pytest.approx(0.9)
    assert spread_diag["by_session"]["london_ny_overlap"]["count"] == 2
    assert spread_diag["by_session"]["london_ny_overlap"]["pairs"] == ["EURUSD", "GBPUSD"]


def test_shadow_entry_ranking_keeps_secondary_spread_diag_under_session_block() -> None:
    class Settings:
        shadow_policy_enabled = True
        max_total_positions = 4
        max_new_entries_per_cycle = 2
        use_portfolio_ranking = True
        tier1_pairs = ["EURUSD"]

    decisions = [
        {
            "symbol": "EURUSD",
            "reasons": ["session_blocked:pacific", "spread_too_wide"],
            "metadata": {
                "ts": "2026-03-24T21:25:00Z",
                "entry_ready": False,
                "position_count_pair": 0,
                "position_signature": "",
                "entry_blocking_reasons": ["session_blocked:pacific", "spread_too_wide"],
                "shadow_floor_ok": False,
                "shadow_floor_rejection_reason": "spread_too_wide",
                "spread_bps": 5.2,
                "max_spread_bps": 2.5,
            },
        }
    ]

    diag = _apply_shadow_entry_ranking(decisions, settings=Settings(), open_position_count=0)

    assert diag["shadow_dominant_rejection_reason"] == "session_blocked:pacific"
    assert diag["shadow_spread_diagnostics"]["reject_count"] == 0
    secondary = diag["shadow_secondary_spread_diagnostics"]
    assert secondary["reject_count"] == 1
    assert secondary["dominant_pair"] == "EURUSD"
    assert secondary["dominant_session"] == "pacific"
    assert secondary["by_pair"]["EURUSD"]["avg_excess_bps"] == pytest.approx(2.7)


def test_adaptive_shadow_ranking_tracks_fallback_and_divergence() -> None:
    class Settings:
        adaptive_shadow_enabled = True
        max_total_positions = 4
        max_new_entries_per_cycle = 2
        use_portfolio_ranking = True
        min_expected_edge_bps = 3.0
        max_allowed_spread_bps = 2.5

    decisions = [
        {
            "symbol": "EURUSD",
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "entry_ready": True,
                "position_count_pair": 0,
                "position_signature": "",
                "spread_bps": 1.0,
                "session_bucket": "london_open",
                "session_entry_blocked": False,
                "session_entry_block_reason": "",
                "rejection_reason": "none",
                "calibrated_ev_bps_shadow": 7.5,
            },
        },
        {
            "symbol": "GBPUSD",
            "reasons": [],
            "metadata": {
                "pair": "GBPUSD",
                "entry_ready": True,
                "position_count_pair": 0,
                "position_signature": "",
                "spread_bps": 1.1,
                "session_bucket": "london_open",
                "session_entry_blocked": False,
                "session_entry_block_reason": "",
                "rejection_reason": "none",
                "calibrated_ev_bps_shadow": 4.0,
            },
        },
    ]
    adaptive_rows_by_pair = {
        "EURUSD": {
            "pair": "EURUSD",
            "signal_side": "long",
            "session_bucket": "london_open",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.0,
            "environment_state": "CorrectiveTrend",
            "playbook": "trend_pullback",
            "playbook_score": 0.72,
            "location_score": 0.66,
            "trigger_score": 0.61,
            "macro_coherence_score": 0.69,
            "pair_strength_score": 0.18,
            "trend_persistence_score": 0.74,
            "compression_score": 0.24,
            "expansion_score": 0.38,
            "range_score": 0.22,
            "hostility_score": 0.11,
            "uncertainty_score": 0.09,
            "calibrated_ev_bps_shadow": 7.5,
        },
        "GBPUSD": {
            "pair": "GBPUSD",
            "signal_side": "long",
            "session_bucket": "london_open",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.1,
            "environment_state": "BalancedRange",
            "playbook": "no_trade",
            "playbook_score": 0.0,
            "location_score": 0.45,
            "trigger_score": 0.46,
            "macro_coherence_score": 0.60,
            "pair_strength_score": 0.08,
            "trend_persistence_score": 0.42,
            "compression_score": 0.48,
            "expansion_score": 0.32,
            "range_score": 0.62,
            "hostility_score": 0.18,
            "uncertainty_score": 0.10,
            "calibrated_ev_bps_shadow": 4.0,
        },
    }

    diag = _apply_adaptive_shadow_ranking(
        decisions,
        settings=Settings(),
        open_position_count=0,
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        state={"equity": 10_000.0, "positions": []},
        current_equity=10_000.0,
    )

    assert diag["adaptive_shadow_candidate_count"] == 1
    assert diag["adaptive_shadow_would_trade_count"] == 1
    assert diag["adaptive_shadow_aggressive_fallback_count"] == 1
    assert decisions[0]["metadata"]["adaptive_playbook"] == "trend_pullback"
    assert decisions[0]["metadata"]["adaptive_sleeve"] == "trend_pullback"
    assert decisions[0]["metadata"]["adaptive_shadow_would_trade"] is True
    assert decisions[0]["metadata"]["adaptive_shadow_live_divergence"] == "agree_ready"
    assert decisions[0]["metadata"]["conviction_band"] == "medium"
    assert float(decisions[0]["metadata"]["allocator_score"]) > 0.0
    assert int(decisions[0]["metadata"]["allocator_rank"]) == 1
    assert decisions[0]["metadata"]["allocator_selected"] is True
    assert decisions[1]["metadata"]["adaptive_playbook"] == "range_mean_reversion"
    assert decisions[1]["metadata"]["adaptive_aggressive_fallback_used"] is True
    assert decisions[1]["metadata"]["adaptive_shadow_would_trade"] is False
    assert decisions[1]["metadata"]["adaptive_shadow_rejection_reason"] == "overlay_stand_down"
    assert decisions[1]["metadata"]["thesis_stage"] == "stand_down"
    assert decisions[1]["metadata"]["adaptive_shadow_live_divergence"] == "live_only"
    assert float(diag["allocator_candidate_count"]) == 1
    assert int(diag["allocator_selected_count"]) == 1
    assert diag["adaptive_shadow_playbook_counts"]["trend_pullback"] == 1
    assert diag["adaptive_shadow_playbook_counts"]["no_trade"] == 1
    assert diag["adaptive_shadow_environment_counts"]["BalancedRange"] == 1


def test_finalize_entry_submissions_can_switch_to_adaptive_mode() -> None:
    class Settings:
        adaptive_execution_enabled = True
        adaptive_shadow_enabled = True

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(self, payload, proto="v2"):
            self.payloads.append(dict(payload))
            return {"status": "queued", "action": payload.get("action"), "command_id": payload.get("command_id")}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "adaptive_shadow_would_trade": False,
                "adaptive_shadow_rejection_reason": "low_playbook_score",
                "lifecycle_action": "entry",
                "lifecycle_reason": "entry_approved",
            },
        }
    ]
    svc = DummyService()
    diag = _finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {"command_id": "abc", "action": "entry", "symbol": "EURUSD"},
            }
        ],
        svc=svc,
        last_action_key={},
        settings=Settings(),
    )

    assert diag["execution_mode"] == "adaptive_multi_playbook"
    assert diag["approved_entry_count"] == 0
    assert diag["blocked_entry_count"] == 1
    assert svc.payloads == []
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["reasons"] == ["low_playbook_score"]
    assert decisions[0]["metadata"]["entry_ready"] is False
    assert decisions[0]["metadata"]["execution_rejection_reason"] == "low_playbook_score"
    assert decisions[0]["metadata"]["enqueue"]["status"] == "skipped"


def test_finalize_entry_submissions_rl_primary_falls_back_when_checkpoint_proposal_is_unsupported() -> None:
    class Settings:
        adaptive_execution_enabled = True
        adaptive_shadow_enabled = True
        strategy_engine_mode = "rl_primary"
        rl_supervised_fallback_required = True
        min_order_lots = 0.01
        order_lot_step = 0.01
        max_order_lots = 0.0

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(self, payload, proto="v2"):
            self.payloads.append(dict(payload))
            return {"status": "queued", "action": payload.get("action"), "command_id": payload.get("command_id")}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "adaptive_shadow_would_trade": True,
                "adaptive_shadow_rejection_reason": "none",
                "lifecycle_action": "entry",
                "lifecycle_reason": "entry_approved",
            },
        }
    ]
    svc = DummyService()
    diag = _finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "lots": 0.50},
                "approved_order": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "cmd": "BUY", "side": "BUY", "lots": 0.50},
            }
        ],
        svc=svc,
        last_action_key={},
        settings=Settings(),
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {
                        "target_position": -0.70,
                        "close_position": False,
                        "metadata": {"entry_supported": False},
                    },
                }
            },
        },
    )

    assert diag["execution_mode"] == "rl_primary"
    assert diag["rl_routed_entry_count"] == 0
    assert diag["rl_fallback_entry_count"] == 1
    assert diag["rl_blocked_entry_count"] == 0
    assert len(svc.payloads) == 1
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["reasons"] == []
    assert decisions[0]["metadata"]["rl_router_reason"] == "rl_primary_supervised_fallback"
    assert decisions[0]["metadata"]["enqueue"]["status"] == "queued"


def test_finalize_entry_submissions_rl_primary_falls_back_when_checkpoint_is_unavailable() -> None:
    class Settings:
        adaptive_execution_enabled = True
        adaptive_shadow_enabled = True
        strategy_engine_mode = "rl_primary"
        rl_supervised_fallback_required = False
        min_order_lots = 0.01
        order_lot_step = 0.01
        max_order_lots = 0.0

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(self, payload, proto="v2"):
            self.payloads.append(dict(payload))
            return {"status": "queued", "action": payload.get("action"), "command_id": payload.get("command_id")}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "adaptive_shadow_would_trade": True,
                "adaptive_shadow_rejection_reason": "none",
                "lifecycle_action": "entry",
                "lifecycle_reason": "entry_approved",
            },
        }
    ]
    svc = DummyService()
    diag = _finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "lots": 0.50},
                "approved_order": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "cmd": "BUY", "side": "BUY", "lots": 0.50},
            }
        ],
        svc=svc,
        last_action_key={},
        settings=Settings(),
        rl_portfolio_proposal={
            "source": "supervised_fallback",
            "checkpoint_loaded": False,
            "supervised_fallback_used": True,
            "fallback_reason": "checkpoint_unavailable",
            "proposals_by_pair": {},
        },
    )

    assert diag["execution_mode"] == "rl_primary"
    assert diag["rl_fallback_entry_count"] == 1
    assert diag["rl_blocked_entry_count"] == 0
    assert len(svc.payloads) == 1
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["reasons"] == []
    assert decisions[0]["metadata"]["rl_router_reason"] == "checkpoint_unavailable"
    assert decisions[0]["metadata"]["enqueue"]["status"] == "queued"


def test_finalize_entry_submissions_hybrid_candidate_falls_back_when_checkpoint_proposal_is_unsupported() -> None:
    class Settings:
        adaptive_execution_enabled = True
        adaptive_shadow_enabled = True
        strategy_engine_mode = "hybrid_candidate"
        min_order_lots = 0.01
        order_lot_step = 0.01
        max_order_lots = 0.0

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(self, payload, proto="v2"):
            self.payloads.append(dict(payload))
            return {"status": "queued", "action": payload.get("action"), "command_id": payload.get("command_id")}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "adaptive_shadow_would_trade": True,
                "adaptive_shadow_rejection_reason": "none",
                "lifecycle_action": "entry",
                "lifecycle_reason": "entry_approved",
            },
        }
    ]
    svc = DummyService()
    diag = _finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "lots": 0.50},
                "approved_order": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "cmd": "BUY", "side": "BUY", "lots": 0.50},
            }
        ],
        svc=svc,
        last_action_key={},
        settings=Settings(),
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {
                        "target_position": -0.70,
                        "close_position": False,
                        "metadata": {"entry_supported": False},
                    },
                }
            },
        },
    )

    assert diag["execution_mode"] == "hybrid_candidate"
    assert diag["rl_routed_entry_count"] == 0
    assert diag["rl_fallback_entry_count"] == 1
    assert diag["rl_blocked_entry_count"] == 0
    assert len(svc.payloads) == 1
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["reasons"] == []
    assert decisions[0]["metadata"]["rl_router_reason"] == "hybrid_candidate_supervised_fallback"
    assert decisions[0]["metadata"]["enqueue"]["status"] == "queued"


def test_finalize_entry_submissions_rl_primary_uses_supervised_lot_size_when_rl_scale_underflows_min_lot() -> None:
    class Settings:
        adaptive_execution_enabled = True
        adaptive_shadow_enabled = True
        strategy_engine_mode = "rl_primary"
        rl_supervised_fallback_required = True
        min_order_lots = 0.01
        order_lot_step = 0.01
        max_order_lots = 0.0

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(self, payload, proto="v2"):
            self.payloads.append(dict(payload))
            return {"status": "queued", "action": payload.get("action"), "command_id": payload.get("command_id")}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "adaptive_shadow_would_trade": True,
                "adaptive_shadow_rejection_reason": "none",
                "lifecycle_action": "entry",
                "lifecycle_reason": "entry_approved",
                "decision_source_chain": ["strategy_engine_mode:rl_primary"],
            },
        }
    ]
    svc = DummyService()
    diag = _finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "lots": 0.06},
                "approved_order": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "cmd": "BUY", "side": "BUY", "lots": 0.06},
            }
        ],
        svc=svc,
        last_action_key={},
        settings=Settings(),
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {"target_position": 0.10, "close_position": False},
                }
            },
        },
    )

    assert diag["execution_mode"] == "rl_primary"
    assert diag["rl_routed_entry_count"] == 1
    assert diag["rl_fallback_entry_count"] == 1
    assert diag["rl_scaled_entry_count"] == 0
    assert len(svc.payloads) == 1
    assert float(svc.payloads[0]["lots"]) == 0.06
    assert decisions[0]["execution_ready"] is True
    assert decisions[0]["reasons"] == []
    assert decisions[0]["metadata"]["rl_supervised_fallback_used"] is True
    assert decisions[0]["metadata"]["rl_fallback_reason"] == "rl_target_below_min_lot"
    assert decisions[0]["metadata"]["enqueue"]["status"] == "queued"


def test_finalize_entry_submissions_rl_primary_can_scale_approved_entry() -> None:
    class Settings:
        adaptive_execution_enabled = True
        adaptive_shadow_enabled = True
        strategy_engine_mode = "rl_primary"
        rl_supervised_fallback_required = True
        min_order_lots = 0.01
        order_lot_step = 0.01
        max_order_lots = 0.0

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(self, payload, proto="v2"):
            self.payloads.append(dict(payload))
            return {"status": "queued", "action": payload.get("action"), "command_id": payload.get("command_id")}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "adaptive_shadow_would_trade": True,
                "adaptive_shadow_rejection_reason": "none",
                "lifecycle_action": "entry",
                "lifecycle_reason": "entry_approved",
                "decision_source_chain": ["strategy_engine_mode:rl_primary"],
            },
        }
    ]
    svc = DummyService()
    diag = _finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "lots": 0.50},
                "approved_order": {"command_id": "abc", "action": "entry", "symbol": "EURUSD", "cmd": "BUY", "side": "BUY", "lots": 0.50},
            }
        ],
        svc=svc,
        last_action_key={},
        settings=Settings(),
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {"target_position": 0.50, "close_position": False},
                }
            },
        },
    )

    assert diag["execution_mode"] == "rl_primary"
    assert diag["rl_routed_entry_count"] == 1
    assert diag["rl_scaled_entry_count"] == 1
    assert len(svc.payloads) == 1
    assert float(svc.payloads[0]["lots"]) == 0.25
    assert decisions[0]["metadata"]["rl_scaled_lots"] == 0.25
    assert decisions[0]["metadata"]["rl_router_reason"] == "rl_primary_confirmed"
    assert "rl_router:rl_primary_confirmed" in decisions[0]["metadata"]["decision_source_chain"]


def test_rl_lifecycle_router_preserves_cross_pair_context_in_metadata() -> None:
    class Settings:
        strategy_engine_mode = "rl_primary"

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "SELL",
            "metadata": {
                "pair": "EURUSD",
                "side": "SELL",
                "lifecycle_action": "hold",
                "lifecycle_reason": "hold",
                "position_side": "short",
                "decision_source_chain": ["strategy_engine_mode:rl_primary"],
            },
        }
    ]
    pending_position_actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "lifecycle_action": "hold",
            "position_side": "short",
            "lots_open": 1.0,
        }
    ]
    rl_portfolio_proposal = {
        "source": "rl_checkpoint",
        "checkpoint_loaded": True,
        "proposals_by_pair": {
            "EURUSD": {
                "source": "rl_checkpoint",
                "supervised_fallback_used": False,
                "action": {"target_position": -0.70, "close_position": False},
                "cross_pair_rank_position": 1,
                "cross_pair_influence_score": 0.93,
                "cross_pair_recommendation_strength": 0.95,
                "cross_pair_influenced_by_pairs": ["GBPUSD", "USDJPY"],
                "cross_pair_reason_codes": ["local_edge", "peer_confluence"],
                "cross_pair_soft_block": False,
                "cross_pair_hard_block": False,
            }
        },
    }

    diag = _apply_rl_lifecycle_router(
        decisions=decisions,
        pending_position_actions=pending_position_actions,
        rl_portfolio_proposal=rl_portfolio_proposal,
        settings=Settings(),
    )

    meta = decisions[0]["metadata"]
    assert diag["rl_lifecycle_reviewed_count"] == 1
    assert meta["cross_pair_rank_position"] == 1
    assert meta["cross_pair_influence_score"] == 0.93
    assert meta["cross_pair_reason_codes"] == ["local_edge", "peer_confluence"]
    assert meta["rl_cross_pair_rank_position"] == 1
    assert meta["rl_cross_pair_influenced_by_pairs"] == ["GBPUSD", "USDJPY"]
    assert meta["rl_lifecycle_source"] == "rl_checkpoint"
    assert meta["rl_lifecycle_reason"] == "rl_primary_resize_down"
    assert "rl_lifecycle:rl_primary_resize_down" in meta["decision_source_chain"]


def test_apply_rl_lifecycle_router_can_force_exit_in_rl_primary() -> None:
    class Settings:
        strategy_engine_mode = "rl_primary"

    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "position_side": "long",
                "decision_source_chain": ["strategy_engine_mode:rl_primary"],
                "lifecycle_action": "hold",
                "lifecycle_reason": "position_open_hold",
            },
        }
    ]
    pending_position_actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-03-25T10:00:00Z",
            "position_side": "long",
            "lifecycle_action": "hold",
            "lifecycle_reason": "position_open_hold",
            "lifecycle_action_score": 0.1,
            "close_lots": 0.0,
            "sl_price": 0.0,
        }
    ]

    diag = _apply_rl_lifecycle_router(
        decisions=decisions,
        pending_position_actions=pending_position_actions,
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {"target_position": 0.0, "close_position": True},
                }
            },
        },
        settings=Settings(),
    )

    assert diag["rl_lifecycle_applied_count"] == 1
    assert diag["rl_lifecycle_exit_count"] == 1
    assert diag["rl_lifecycle_flip_exit_count"] == 0
    assert pending_position_actions[0]["lifecycle_action"] == "exit"
    assert decisions[0]["metadata"]["rl_lifecycle_applied"] is True
    assert decisions[0]["metadata"]["lifecycle_reason"] == "rl_primary_close_position"


def test_apply_rl_lifecycle_router_preserves_supervised_exit() -> None:
    class Settings:
        strategy_engine_mode = "hybrid_candidate"

    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "position_side": "long",
                "decision_source_chain": ["strategy_engine_mode:hybrid_candidate"],
                "lifecycle_action": "exit",
                "lifecycle_reason": "exit_model_exit",
            },
        }
    ]
    pending_position_actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-03-25T10:00:00Z",
            "position_side": "long",
            "lifecycle_action": "exit",
            "lifecycle_reason": "exit_model_exit",
            "lifecycle_action_score": 0.8,
            "close_lots": 0.0,
            "sl_price": 0.0,
        }
    ]

    diag = _apply_rl_lifecycle_router(
        decisions=decisions,
        pending_position_actions=pending_position_actions,
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {"target_position": 1.0, "close_position": False},
                }
            },
        },
        settings=Settings(),
    )

    assert diag["rl_lifecycle_preserved_exit_count"] == 1
    assert pending_position_actions[0]["lifecycle_action"] == "exit"
    assert decisions[0]["metadata"]["rl_lifecycle_reason"] == "supervised_exit_preserved"


def test_apply_rl_lifecycle_router_can_resize_down_same_direction_position() -> None:
    class Settings:
        strategy_engine_mode = "rl_primary"
        min_order_lots = 0.01
        order_lot_step = 0.01

    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "position_side": "long",
                "decision_source_chain": ["strategy_engine_mode:rl_primary"],
                "lifecycle_action": "hold",
                "lifecycle_reason": "position_open_hold",
            },
        }
    ]
    pending_position_actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-03-25T10:00:00Z",
            "position_side": "long",
            "lots_open": 0.50,
            "lifecycle_action": "hold",
            "lifecycle_reason": "position_open_hold",
            "lifecycle_action_score": 0.1,
            "close_lots": 0.0,
            "sl_price": 0.0,
        }
    ]

    diag = _apply_rl_lifecycle_router(
        decisions=decisions,
        pending_position_actions=pending_position_actions,
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {"target_position": 0.40, "close_position": False},
                }
            },
        },
        settings=Settings(),
    )

    assert diag["rl_lifecycle_applied_count"] == 1
    assert diag["rl_lifecycle_resize_count"] == 1
    assert pending_position_actions[0]["lifecycle_action"] == "partial_tp"
    assert pending_position_actions[0]["close_lots"] == 0.3
    assert decisions[0]["metadata"]["lifecycle_reason"] == "rl_primary_resize_down"


def test_apply_rl_lifecycle_router_tracks_flip_exit_intent() -> None:
    class Settings:
        strategy_engine_mode = "rl_primary"

    decisions = [
        {
            "symbol": "EURUSD",
            "metadata": {
                "pair": "EURUSD",
                "position_side": "long",
                "decision_source_chain": ["strategy_engine_mode:rl_primary"],
                "lifecycle_action": "hold",
                "lifecycle_reason": "position_open_hold",
            },
        }
    ]
    pending_position_actions = [
        {
            "index": 0,
            "pair": "EURUSD",
            "ts_value": "2026-03-25T10:00:00Z",
            "position_side": "long",
            "lifecycle_action": "hold",
            "lifecycle_reason": "position_open_hold",
            "lifecycle_action_score": 0.1,
            "close_lots": 0.0,
            "sl_price": 0.0,
        }
    ]

    diag = _apply_rl_lifecycle_router(
        decisions=decisions,
        pending_position_actions=pending_position_actions,
        rl_portfolio_proposal={
            "source": "rl_checkpoint",
            "checkpoint_loaded": True,
            "proposals_by_pair": {
                "EURUSD": {
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "action": {"target_position": -0.9, "close_position": False},
                }
            },
        },
        settings=Settings(),
    )

    assert diag["rl_lifecycle_exit_count"] == 1
    assert diag["rl_lifecycle_flip_exit_count"] == 1
    assert decisions[0]["metadata"]["lifecycle_reason"] == "rl_primary_flip_exit"
    assert decisions[0]["metadata"]["rl_flip_intent_active"] is True
    assert decisions[0]["metadata"]["rl_flip_intent_side"] == "SELL"
