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
