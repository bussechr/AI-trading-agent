from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from fxstack.belief.candidate_builder import (
    HYPOTHESIS_SCENARIOS,
    HYPOTHESIS_SIDES,
    build_hypothesis_candidates,
)


def _base_frame(**overrides: object) -> pd.DataFrame:
    row = {
        "pair": "EURUSD",
        "ts": "2026-03-26T12:00:00Z",
        "row_idx": 0,
        "session_bucket": "london_open",
        "regime_bucket": "trend",
        "scenario_bucket": "trend_continuation",
        "spread_bps": 0.9,
        "mid_close": 1.1025,
        "ret_1": 0.0002,
        "ret_5": 0.0006,
        "vol_20": 0.00015,
        "vol_60": 0.00018,
        "pullback_depth_20": 0.0017,
        "pushup_depth_20": 0.0016,
        "trend_slope_20": 0.0011,
        "trend_slope_60": 0.0014,
        "trend_strength_20": 0.85,
        "trend_strength_60": 0.75,
        "bar_imbalance": 0.20,
        "micro_pressure": 0.25,
        "edge_decay_12": 0.0001,
        "vol_term_ratio": 1.05,
        "hostility_score": 0.10,
        "macro_coherence_score": 0.70,
        "position_count_pair": 0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_build_hypothesis_candidates_enumerates_all_sides_and_scenarios() -> None:
    out = build_hypothesis_candidates(
        _base_frame(),
        settings=SimpleNamespace(blocked_entry_sessions=[], max_allowed_spread_bps=2.5, pairs=["EURUSD"]),
        local_feasible_only=True,
    )

    assert len(out) == len(HYPOTHESIS_SCENARIOS) * len(HYPOTHESIS_SIDES)
    assert set(out["scenario"].astype(str)) == set(HYPOTHESIS_SCENARIOS)
    assert set(out["side"].astype(str)) == set(HYPOTHESIS_SIDES)
    assert set(out["row_idx"].astype(int)) == {0}
    assert set(out["query_id"].astype(str)) == {"EURUSD|2026-03-26T12:00:00Z"}
    assert out["local_feasible"].astype(bool).all()


def test_build_hypothesis_candidates_applies_local_entry_feasibility() -> None:
    frame = _base_frame(session_bucket="asia", position_count_pair=1)

    filtered = build_hypothesis_candidates(
        frame,
        settings=SimpleNamespace(blocked_entry_sessions=["asia"], max_allowed_spread_bps=2.5, pairs=["EURUSD"]),
        local_feasible_only=True,
    )
    assert filtered.empty

    unfiltered = build_hypothesis_candidates(
        frame,
        settings=SimpleNamespace(blocked_entry_sessions=["asia"], max_allowed_spread_bps=2.5, pairs=["EURUSD"]),
        local_feasible_only=False,
    )
    assert len(unfiltered) == len(HYPOTHESIS_SCENARIOS) * len(HYPOTHESIS_SIDES)
    assert not unfiltered["local_feasible"].astype(bool).any()
