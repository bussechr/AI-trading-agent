from __future__ import annotations

from types import SimpleNamespace

import numpy as np
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


def test_build_hypothesis_candidates_preserves_cross_pair_context_columns() -> None:
    out = build_hypothesis_candidates(
        _base_frame(
            usd_strength_basket_ret_1=0.00021,
            cross_pair_dispersion=0.17,
        ),
        settings=SimpleNamespace(blocked_entry_sessions=[], max_allowed_spread_bps=2.5, pairs=["EURUSD"]),
        local_feasible_only=True,
    )

    assert "usd_strength_basket_ret_1" in out.columns
    assert "cross_pair_dispersion" in out.columns
    assert set(out["usd_strength_basket_ret_1"].astype(float)) == {0.00021}
    assert set(out["cross_pair_dispersion"].astype(float)) == {0.17}


def test_build_hypothesis_candidates_preserves_supplied_risk_diagnostics() -> None:
    signal = SimpleNamespace(
        uncertainty_score=1.0,
        model_disagreement_score=1.0,
        extension_penalty_score=1.0,
        structure_timing_score=0.0,
    )
    out = build_hypothesis_candidates(
        _base_frame(extension_penalty_score=0.12, structure_timing_score=0.72),
        signal=signal,
        settings=SimpleNamespace(blocked_entry_sessions=[], max_allowed_spread_bps=2.5, pairs=["EURUSD"]),
        local_feasible_only=True,
    )

    assert set(out["uncertainty_score"].astype(float)) == {1.0}
    assert set(out["model_disagreement_score"].astype(float)) == {1.0}
    assert set(out["extension_penalty_score"].astype(float)) == {1.0}
    assert set(out["structure_timing_score"].astype(float)) == {0.0}


def test_build_hypothesis_candidates_falls_back_to_frame_when_signal_is_missing() -> None:
    out = build_hypothesis_candidates(
        _base_frame(
            extension_penalty_score=0.12,
            structure_timing_score=0.72,
            uncertainty_score=0.34,
            model_disagreement_score=0.29,
            directional_swing_confidence=0.66,
        ),
        signal=None,
        adaptive_meta=None,
        settings=SimpleNamespace(blocked_entry_sessions=[], max_allowed_spread_bps=2.5, pairs=["EURUSD"]),
        local_feasible_only=True,
    )

    assert set(out["extension_penalty_score"].astype(float)) == {0.12}
    assert set(out["structure_timing_score"].astype(float)) == {0.72}
    assert set(out["uncertainty_score"].astype(float)) == {0.34}
    assert set(out["model_disagreement_score"].astype(float)) == {0.29}
    assert set(out["directional_swing_confidence"].astype(float)) == {0.66}
    assert np.isfinite(out.select_dtypes(include=[np.number]).to_numpy()).all()
