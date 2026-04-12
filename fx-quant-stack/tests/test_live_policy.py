from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from fxstack.live.policy import (
    compute_model_intelligence_score,
    compute_expected_edge_bps,
    compute_heuristic_penalty_score,
    compute_shadow_entry_diagnostics,
    compute_structure_timing_diagnostics,
    gate_decision,
    is_entry_session_blocked,
    normalize_spread_bps,
    session_bucket_from_ts,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


def _load_twin_module():
    spec = importlib.util.spec_from_file_location("fxstack_digital_twin_backtest_policy_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_compute_expected_edge_bps_from_ret_1() -> None:
    out = compute_expected_edge_bps({"ret_1": 0.0012})
    assert round(float(out), 6) == 12.0


def test_structure_timing_uses_finite_htf_values_only() -> None:
    mod = _load_twin_module()
    row = {
        "trend_slope_60": 0.0030,
        "trend_strength_60": 1.5,
        "h1_trend_slope_20": float("nan"),
        "h4_trend_slope_20": float("nan"),
        "d_trend_slope_20": float("nan"),
        "h1_trend_strength_20": float("nan"),
        "h4_trend_strength_20": float("nan"),
        "d_trend_strength_20": float("nan"),
    }

    live = compute_structure_timing_diagnostics(row, side="long")
    twin = mod._htf_alignment_score_series(pd.DataFrame([row]), side_sign=np.array([1.0], dtype=float))

    assert float(live.htf_alignment_score) == 1.0
    assert float(twin.iloc[0]) == 1.0
    assert float(live.htf_alignment_score) == float(twin.iloc[0])


def test_normalize_spread_bps_from_price_units_eurusd() -> None:
    spread_bps, source = normalize_spread_bps(row={"pair": "EURUSD", "mid_close": 1.1000, "spread": 0.00011})
    assert source == "row.spread_price"
    assert round(float(spread_bps), 6) == 1.0


def test_normalize_spread_bps_from_tick_pips_usdjpy() -> None:
    spread_bps, source = normalize_spread_bps(
        tick={"symbol": "USDJPY", "bid": 150.0, "ask": 150.006, "spread_pips": 0.6, "digits": 3},
        pair="USDJPY",
    )
    assert source == "tick.spread_pips"
    assert float(spread_bps) > 0.0


def test_session_bucket_helpers_recognize_blocked_sessions() -> None:
    assert session_bucket_from_ts("2026-03-24T21:25:00Z") == "pacific"
    assert is_entry_session_blocked(session_bucket="pacific", blocked_sessions=["pacific"]) is True
    assert is_entry_session_blocked(session_bucket="london_open", blocked_sessions=["pacific"]) is False


def test_gate_decision_emits_threshold_snapshot() -> None:
    out = gate_decision(
        swing_prob=0.7,
        entry_prob=0.7,
        trade_prob=0.7,
        spread_bps=0.6,
        expected_edge_bps=6.0,
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
    )
    assert out.allowed is True
    assert out.reason == "approved"
    assert float(out.threshold_snapshot["max_spread_bps"]) == 2.5
    assert out.spread_unit_source == "tick.spread_bps"


def test_gate_decision_allows_small_edge_shortfall_with_rescue_margin() -> None:
    tolerated = gate_decision(
        swing_prob=0.74,
        entry_prob=0.76,
        trade_prob=0.75,
        spread_bps=0.6,
        expected_edge_bps=2.8,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        min_expected_edge_rescue_margin_bps=0.5,
        spread_unit_source="tick.spread_bps",
    )
    blocked = gate_decision(
        swing_prob=0.74,
        entry_prob=0.76,
        trade_prob=0.75,
        spread_bps=0.6,
        expected_edge_bps=2.3,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        min_expected_edge_rescue_margin_bps=0.5,
        spread_unit_source="tick.spread_bps",
    )

    assert tolerated.allowed is True
    assert tolerated.reason == "approved"
    assert float(tolerated.threshold_snapshot["min_expected_edge_rescue_margin_bps"]) == 0.5
    assert blocked.allowed is False
    assert blocked.reason == "edge_below_hurdle"


def test_gate_decision_uses_directional_confidence_for_short_side_minima() -> None:
    out = gate_decision(
        swing_prob=0.35,
        entry_prob=0.72,
        trade_prob=0.62,
        spread_bps=1.08,
        expected_edge_bps=9.99,
        side="short",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
    )

    assert out.allowed is True
    assert out.reason == "approved"
    assert float(out.threshold_snapshot["directional_swing_confidence"]) == 0.65


def test_gate_decision_does_not_veto_valid_core_model_minima_due_to_low_intelligence() -> None:
    out = gate_decision(
        swing_prob=0.74,
        entry_prob=0.76,
        trade_prob=0.75,
        spread_bps=0.6,
        expected_edge_bps=4.0,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
        model_intelligence_score=0.10,
        strategy_engine_mode="hybrid_candidate",
    )

    assert out.allowed is True
    assert out.reason == "approved"
    assert float(out.threshold_snapshot["model_intelligence_score"]) == 0.10
    assert out.strategy_engine_mode == "hybrid_candidate"


def test_gate_decision_still_blocks_low_intelligence_when_core_model_minima_are_not_met() -> None:
    out = gate_decision(
        swing_prob=0.52,
        entry_prob=0.53,
        trade_prob=0.51,
        spread_bps=0.6,
        expected_edge_bps=4.0,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
        model_intelligence_score=0.10,
    )

    assert out.allowed is False
    assert out.reason == "low_swing_prob"


def test_model_intelligence_score_rises_with_supervised_inputs() -> None:
    weak = compute_model_intelligence_score(
        regime_prob=0.52,
        swing_prob=0.53,
        entry_prob=0.51,
        trade_prob=0.52,
        expected_edge_bps=1.0,
        min_expected_edge_bps=3.0,
        side="long",
    )
    strong = compute_model_intelligence_score(
        regime_prob=0.74,
        swing_prob=0.79,
        entry_prob=0.77,
        trade_prob=0.76,
        expected_edge_bps=8.0,
        min_expected_edge_bps=3.0,
        side="long",
    )

    assert 0.0 <= weak < strong <= 1.0


def test_heuristic_penalty_score_increases_with_spread_uncertainty_and_disagreement() -> None:
    calm = compute_heuristic_penalty_score(
        spread_bps=0.6,
        max_spread_bps=2.5,
        uncertainty_score=0.10,
        model_disagreement_score=0.05,
        structure_timing_score=0.78,
        extension_penalty_score=0.10,
        session_blocked=False,
    )
    stressed = compute_heuristic_penalty_score(
        spread_bps=2.4,
        max_spread_bps=2.5,
        uncertainty_score=0.35,
        model_disagreement_score=0.30,
        structure_timing_score=0.32,
        extension_penalty_score=0.55,
        session_blocked=True,
    )

    assert 0.0 <= calm < stressed <= 1.0


def test_shadow_entry_diagnostics_uses_configured_spread_limit_for_heuristic_penalty() -> None:
    out_with_limit = compute_shadow_entry_diagnostics(
        row={},
        swing_prob=0.78,
        entry_prob=0.78,
        trade_prob=0.78,
        regime_prob=0.78,
        expected_edge_bps=7.0,
        spread_bps=1.5,
        uncertainty_score=0.0,
        side="long",
        pair_tier="tier1",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        min_expected_edge_bps=6.0,
        max_allowed_spread_bps=3.0,
        use_uncertainty_gate=False,
        max_entry_uncertainty=0.25,
        use_structure_timing_shadow=False,
        structure_timing_rescue_min_score=0.66,
        structure_timing_entry_rescue_margin=0.05,
        structure_timing_max_chase_risk=0.78,
        entry_hysteresis_margin_bps=1.0,
    )
    out_without_limit = compute_shadow_entry_diagnostics(
        row={},
        swing_prob=0.78,
        entry_prob=0.78,
        trade_prob=0.78,
        regime_prob=0.78,
        expected_edge_bps=7.0,
        spread_bps=1.5,
        uncertainty_score=0.0,
        side="long",
        pair_tier="tier1",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        min_expected_edge_bps=6.0,
        use_uncertainty_gate=False,
        max_entry_uncertainty=0.25,
        use_structure_timing_shadow=False,
        structure_timing_rescue_min_score=0.66,
        structure_timing_entry_rescue_margin=0.05,
        structure_timing_max_chase_risk=0.78,
        entry_hysteresis_margin_bps=1.0,
    )

    assert float(out_with_limit.heuristic_penalty_score) < float(out_without_limit.heuristic_penalty_score)

    expected_penalty = compute_heuristic_penalty_score(
        spread_bps=1.5,
        max_spread_bps=3.0,
        uncertainty_score=0.0,
        model_disagreement_score=float(out_with_limit.model_disagreement_score),
        structure_timing_score=float(out_with_limit.structure_timing_score),
        extension_penalty_score=float(out_with_limit.extension_penalty_score),
        session_blocked=False,
    )
    assert float(out_with_limit.heuristic_penalty_score) == expected_penalty


def test_gate_decision_reflects_rl_flip_and_rebalance_intents() -> None:
    flip = gate_decision(
        swing_prob=0.74,
        entry_prob=0.76,
        trade_prob=0.75,
        spread_bps=0.6,
        expected_edge_bps=4.5,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
        strategy_engine_mode="rl_primary",
        rl_target_position=0.5,
        rl_current_position_side="short",
        rl_current_position_size=0.4,
    )
    rebalance = gate_decision(
        swing_prob=0.74,
        entry_prob=0.76,
        trade_prob=0.75,
        spread_bps=0.6,
        expected_edge_bps=4.5,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
        strategy_engine_mode="rl_primary",
        rl_target_position=0.3,
        rl_current_position_side="long",
        rl_current_position_size=0.7,
    )

    assert flip.allowed is True
    assert flip.rl_lifecycle_intent == "flip_intent"
    assert flip.rl_flip_intent is True
    assert flip.rl_rebalance_intent is False
    assert flip.rl_lifecycle_reason == "rl_primary_flip_intent"
    assert rebalance.allowed is True
    assert rebalance.rl_lifecycle_intent == "rebalance_intent"
    assert rebalance.rl_flip_intent is False
    assert rebalance.rl_rebalance_intent is True
    assert rebalance.rl_lifecycle_reason == "rl_primary_rebalance_intent"


def test_shadow_entry_diagnostics_only_rescues_near_threshold_cases() -> None:
    rescued = compute_shadow_entry_diagnostics(
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
    blocked = compute_shadow_entry_diagnostics(
        row={
            "h1_trend_slope_20": 0.0019,
            "h4_trend_slope_20": 0.0031,
            "d_trend_slope_20": 0.0034,
            "h1_trend_strength_20": 1.35,
            "h4_trend_strength_20": 1.45,
            "trend_strength_20": 0.95,
            "trend_strength_60": 0.85,
            "pullback_depth_20": 0.0019,
            "ret_1": 0.0002,
            "ret_5": -0.0002,
            "ret_20": 0.0012,
            "bar_imbalance": 0.18,
            "micro_pressure": 0.16,
            "edge_decay_12": 0.0001,
            "vol_20": 0.0005,
            "vol_60": 0.0006,
        },
        swing_prob=0.41,
        entry_prob=0.44,
        trade_prob=0.43,
        regime_prob=0.42,
        expected_edge_bps=1.4,
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

    assert rescued.fallback_used is True
    assert rescued.fallback_reason == "structure_timing_rescue"
    assert rescued.decision_source_chain[-1] == "fallback:structure_timing_rescue"
    assert blocked.fallback_used is False
    assert blocked.fallback_reason == "none"
    assert blocked.floor_ok is False


def test_shadow_entry_diagnostics_exposes_non_legacy_lifecycle_fallback_reason() -> None:
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
        strategy_engine_mode="rl_primary",
    )

    assert out.fallback_used is True
    assert out.fallback_reason == "rl_primary:structure_timing_rescue"
    assert "lifecycle:rl_primary_structure_timing_rescue" in out.decision_source_chain
    assert out.decision_source_chain[-1] == "fallback:rl_primary:structure_timing_rescue"


def test_shadow_entry_diagnostics_reflects_non_legacy_strategy_engine_mode() -> None:
    out = compute_shadow_entry_diagnostics(
        row={},
        swing_prob=0.68,
        entry_prob=0.71,
        trade_prob=0.69,
        regime_prob=0.66,
        expected_edge_bps=7.0,
        spread_bps=1.0,
        uncertainty_score=0.10,
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
        strategy_engine_mode="hybrid_candidate",
    )

    assert out.strategy_engine_mode == "hybrid_candidate"
    assert out.fallback_reason == "hybrid_candidate:none"
    assert out.decision_source_chain[0] == "strategy_engine_mode:hybrid_candidate"
    assert "lifecycle:hybrid_candidate_entry_approved" in out.decision_source_chain
