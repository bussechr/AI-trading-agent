from __future__ import annotations

import pandas as pd

from fxstack.belief.labels import build_directional_belief_labels


def test_directional_belief_labels_identify_trend_pullback() -> None:
    frame = pd.DataFrame(
        [
            {"ts": "2026-03-26T00:00:00Z", "mid_close": 1.0000, "regime_bucket": "trend", "scenario_bucket": "london_open"},
            {"ts": "2026-03-26T00:05:00Z", "mid_close": 1.0004, "regime_bucket": "trend", "scenario_bucket": "london_open"},
            {"ts": "2026-03-26T00:10:00Z", "mid_close": 1.0012, "regime_bucket": "trend", "scenario_bucket": "london_open"},
            {"ts": "2026-03-26T00:15:00Z", "mid_close": 1.0020, "regime_bucket": "trend", "scenario_bucket": "london_open"},
            {"ts": "2026-03-26T00:20:00Z", "mid_close": 1.0024, "regime_bucket": "trend", "scenario_bucket": "london_open"},
        ]
    )

    labeled = build_directional_belief_labels(frame, short_horizon_bars=1, trade_horizon_bars=2, structural_horizon_bars=3)
    assert labeled.loc[0, "belief_scenario"] == "trend_pullback"
    assert int(labeled.loc[0, "belief_scenario_id"]) >= 0


def test_directional_belief_labels_identify_range_and_breakout_and_no_edge() -> None:
    range_frame = pd.DataFrame(
        [
            {"ts": "2026-03-26T01:00:00Z", "mid_close": 1.0000, "regime_bucket": "range", "scenario_bucket": "range_mean_reversion"},
            {"ts": "2026-03-26T01:05:00Z", "mid_close": 1.0008, "regime_bucket": "range", "scenario_bucket": "range_mean_reversion"},
            {"ts": "2026-03-26T01:10:00Z", "mid_close": 1.0004, "regime_bucket": "range", "scenario_bucket": "range_mean_reversion"},
            {"ts": "2026-03-26T01:15:00Z", "mid_close": 0.9994, "regime_bucket": "range", "scenario_bucket": "range_mean_reversion"},
        ]
    )
    breakout_frame = pd.DataFrame(
        [
            {"ts": "2026-03-26T02:00:00Z", "mid_close": 1.0000, "regime_bucket": "vol_expansion", "scenario_bucket": "breakout_initiation"},
            {"ts": "2026-03-26T02:05:00Z", "mid_close": 1.0007, "regime_bucket": "vol_expansion", "scenario_bucket": "breakout_initiation"},
            {"ts": "2026-03-26T02:10:00Z", "mid_close": 1.0018, "regime_bucket": "vol_expansion", "scenario_bucket": "breakout_initiation"},
            {"ts": "2026-03-26T02:15:00Z", "mid_close": 1.0024, "regime_bucket": "vol_expansion", "scenario_bucket": "breakout_initiation"},
        ]
    )
    hostile_frame = pd.DataFrame(
        [
            {"ts": "2026-03-26T03:00:00Z", "mid_close": 1.0000, "regime_bucket": "stress", "scenario_bucket": "high_spread_stress"},
            {"ts": "2026-03-26T03:05:00Z", "mid_close": 1.0001, "regime_bucket": "stress", "scenario_bucket": "high_spread_stress"},
            {"ts": "2026-03-26T03:10:00Z", "mid_close": 1.0002, "regime_bucket": "stress", "scenario_bucket": "high_spread_stress"},
            {"ts": "2026-03-26T03:15:00Z", "mid_close": 1.0001, "regime_bucket": "stress", "scenario_bucket": "high_spread_stress"},
        ]
    )

    labeled_range = build_directional_belief_labels(range_frame, short_horizon_bars=1, trade_horizon_bars=2, structural_horizon_bars=3)
    labeled_breakout = build_directional_belief_labels(breakout_frame, short_horizon_bars=1, trade_horizon_bars=2, structural_horizon_bars=3)
    labeled_hostile = build_directional_belief_labels(hostile_frame, short_horizon_bars=1, trade_horizon_bars=2, structural_horizon_bars=3)

    assert labeled_range.loc[0, "belief_scenario"] == "range_mean_reversion"
    assert labeled_breakout.loc[0, "belief_scenario"] == "breakout_expansion"
    assert labeled_hostile.loc[0, "belief_scenario"] == "no_edge"
