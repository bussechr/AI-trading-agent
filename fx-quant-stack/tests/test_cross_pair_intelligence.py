from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from fxstack.belief.cross_pair import build_cross_pair_influence_frame
from fxstack.training.belief import export_cross_pair_intelligence


def _belief_rows(*, b_shift: float = 0.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "pair": "EURUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "long",
                "belief_primary_scenario": "trend_pullback",
                "belief_primary_score": 0.66,
                "belief_primary_rank_score": 0.61,
                "belief_primary_ev_above_hurdle_prob": 0.72,
                "belief_gap": 0.32,
                "belief_horizon_alignment_score": 0.68,
                "belief_regime_fit_score": 0.64,
                "belief_fragility_score": 0.18,
                "usd_strength_basket_ret_1": 0.00025,
                "cross_pair_dispersion": 0.08,
            },
            {
                "pair": "GBPUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "long",
                "belief_primary_scenario": "breakout_expansion",
                "belief_primary_score": 0.66,
                "belief_primary_rank_score": 0.61,
                "belief_primary_ev_above_hurdle_prob": 0.72,
                "belief_gap": 0.32,
                "belief_horizon_alignment_score": 0.68,
                "belief_regime_fit_score": 0.64,
                "belief_fragility_score": 0.18,
                "usd_strength_basket_ret_1": 0.00010 + b_shift,
                "cross_pair_dispersion": 0.20 - (b_shift * 1000.0),
            },
            {
                "pair": "USDJPY",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "short",
                "belief_primary_scenario": "failed_breakout_reversal",
                "belief_primary_score": 0.52,
                "belief_primary_rank_score": 0.46,
                "belief_primary_ev_above_hurdle_prob": 0.58,
                "belief_gap": 0.18,
                "belief_horizon_alignment_score": 0.55,
                "belief_regime_fit_score": 0.50,
                "belief_fragility_score": 0.26,
                "usd_strength_basket_ret_1": -0.00012,
                "cross_pair_dispersion": 0.36,
            },
        ]
    )


def test_cross_pair_ranking_changes_with_cross_pair_inputs(tmp_path: Path) -> None:
    base = build_cross_pair_influence_frame(_belief_rows())
    assert list(base.sort_values("rank_position")["pair"]) == ["EURUSD", "GBPUSD", "USDJPY"]
    eurusd = base.loc[base["pair"] == "EURUSD"].iloc[0]
    assert eurusd["rank_position"] == 1
    assert base["recommendation_strength"].nunique() > 1
    assert "basket_alignment" in eurusd["cross_pair_reason_codes"]
    assert eurusd["influenced_by_pairs"]

    shifted = build_cross_pair_influence_frame(_belief_rows(b_shift=0.00035))
    assert list(shifted.sort_values("rank_position")["pair"]) == ["GBPUSD", "EURUSD", "USDJPY"]
    gbpusd = shifted.loc[shifted["pair"] == "GBPUSD"].iloc[0]
    assert gbpusd["rank_position"] == 1
    assert gbpusd["influence_score"] > shifted.loc[shifted["pair"] == "EURUSD", "influence_score"].iloc[0]

    out = export_cross_pair_intelligence(belief_rows=_belief_rows(b_shift=0.00035), out=tmp_path / "cross_pair_intelligence.json")
    assert out["model"] == "cross_pair_intelligence_v1"
    assert out["top_pairs"][0] == "GBPUSD"
    assert (tmp_path / "cross_pair_intelligence.json").exists()


def test_cross_pair_telemetry_only_for_small_universe_keeps_gating_off() -> None:
    frame = pd.DataFrame(
        [
            {
                "pair": "EURUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "long",
                "belief_primary_score": 0.14,
                "belief_primary_rank_score": 0.12,
                "belief_primary_ev_above_hurdle_prob": 0.09,
                "belief_gap": 0.03,
                "belief_horizon_alignment_score": 0.10,
                "belief_regime_fit_score": 0.08,
                "belief_fragility_score": 0.92,
                "usd_strength_basket_ret_1": 0.0,
                "cross_pair_dispersion": 0.98,
            },
            {
                "pair": "GBPUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "short",
                "belief_primary_score": 0.11,
                "belief_primary_rank_score": 0.09,
                "belief_primary_ev_above_hurdle_prob": 0.07,
                "belief_gap": 0.02,
                "belief_horizon_alignment_score": 0.06,
                "belief_regime_fit_score": 0.05,
                "belief_fragility_score": 0.95,
                "usd_strength_basket_ret_1": 0.0,
                "cross_pair_dispersion": 0.97,
            },
        ]
    )

    ranking = build_cross_pair_influence_frame(frame)
    assert set(ranking["source_mode"]) == {"telemetry_only"}
    assert all(ranking["recommendation_strength"] >= 0.35)
    assert all("telemetry_only" in codes for codes in ranking["cross_pair_reason_codes"])
    assert all("insufficient_universe_coverage" in codes for codes in ranking["cross_pair_reason_codes"])
    assert list(ranking.sort_values("rank_position")["pair"]) == ["EURUSD", "GBPUSD"]


def test_cross_pair_telemetry_only_for_noisy_large_universe_keeps_gating_off() -> None:
    frame = pd.DataFrame(
        [
            {
                "pair": "EURUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "long",
                "belief_primary_score": 0.19,
                "belief_primary_rank_score": 0.16,
                "belief_primary_ev_above_hurdle_prob": 0.12,
                "belief_gap": 0.04,
                "belief_horizon_alignment_score": 0.12,
                "belief_regime_fit_score": 0.09,
                "belief_fragility_score": 0.90,
                "usd_strength_basket_ret_1": 0.0,
                "cross_pair_dispersion": 0.96,
            },
            {
                "pair": "GBPUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "short",
                "belief_primary_score": 0.17,
                "belief_primary_rank_score": 0.15,
                "belief_primary_ev_above_hurdle_prob": 0.11,
                "belief_gap": 0.03,
                "belief_horizon_alignment_score": 0.10,
                "belief_regime_fit_score": 0.08,
                "belief_fragility_score": 0.94,
                "usd_strength_basket_ret_1": 0.0,
                "cross_pair_dispersion": 0.97,
            },
            {
                "pair": "USDJPY",
                "ts": "2026-04-07T12:00:00Z",
                "belief_primary_side": "long",
                "belief_primary_score": 0.18,
                "belief_primary_rank_score": 0.14,
                "belief_primary_ev_above_hurdle_prob": 0.10,
                "belief_gap": 0.03,
                "belief_horizon_alignment_score": 0.09,
                "belief_regime_fit_score": 0.07,
                "belief_fragility_score": 0.93,
                "usd_strength_basket_ret_1": 0.0,
                "cross_pair_dispersion": 0.95,
            },
        ]
    )

    ranking = build_cross_pair_influence_frame(frame)
    assert set(ranking["source_mode"]) == {"telemetry_only"}
    assert all(ranking["recommendation_strength"] >= 0.35)
    assert all("telemetry_only" in codes for codes in ranking["cross_pair_reason_codes"])
    assert all("low_signal_quality" in codes for codes in ranking["cross_pair_reason_codes"])


def test_cross_pair_ineligible_belief_rows_stay_neutral_and_do_not_contaminate_peers() -> None:
    frame = pd.DataFrame(
        [
            *_belief_rows().to_dict(orient="records"),
            {
                "pair": "AUDUSD",
                "ts": "2026-04-07T12:00:00Z",
                "belief_source_mode": "artifact_missing",
                "belief_primary_side": "long",
                "belief_primary_scenario": "trend_pullback",
                "belief_primary_score": 0.99,
                "belief_primary_rank_score": 0.99,
                "belief_primary_ev_above_hurdle_prob": 0.99,
                "belief_gap": 0.99,
                "belief_horizon_alignment_score": 0.99,
                "belief_regime_fit_score": 0.99,
                "belief_fragility_score": 0.01,
                "usd_strength_basket_ret_1": 0.0009,
                "cross_pair_dispersion": 0.01,
            },
        ]
    )

    ranking = build_cross_pair_influence_frame(frame)

    audusd = ranking.loc[ranking["pair"] == "AUDUSD"].iloc[0]
    assert audusd["source_mode"] == "artifact_missing"
    assert audusd["influence_score"] == 0.0
    assert audusd["recommendation_strength"] == 0.5
    assert audusd["influenced_by_pairs"] == []
    assert audusd["cross_pair_reason_codes"] == ["ineligible_belief_source_mode"]
    assert audusd["rank_position"] == int(ranking["rank_position"].max())

    eurusd = ranking.loc[ranking["pair"] == "EURUSD"].iloc[0]
    assert "AUDUSD" not in eurusd["influenced_by_pairs"]
    assert list(ranking.sort_values("rank_position")["pair"])[:3] == ["EURUSD", "GBPUSD", "USDJPY"]
    summary = export_cross_pair_intelligence(belief_rows=frame)
    assert "AUDUSD" not in summary["top_pairs"]


def test_cross_pair_missing_dispersion_stays_neutral_instead_of_max_consensus() -> None:
    frame = _belief_rows().copy()
    frame.loc[frame["pair"] == "EURUSD", "cross_pair_dispersion"] = float("nan")

    ranking = build_cross_pair_influence_frame(frame)
    eurusd = ranking.loc[ranking["pair"] == "EURUSD"].iloc[0]

    assert eurusd["consensus_score"] == 0.5


def test_cross_pair_nonfinite_belief_inputs_never_become_top_scores() -> None:
    frame = _belief_rows().copy()
    mask = frame["pair"] == "EURUSD"
    for column in [
        "belief_primary_score",
        "belief_primary_rank_score",
        "belief_primary_ev_above_hurdle_prob",
        "belief_gap",
        "belief_horizon_alignment_score",
        "belief_regime_fit_score",
    ]:
        frame.loc[mask, column] = float("nan")
    frame.loc[mask, "belief_fragility_score"] = float("inf")

    ranking = build_cross_pair_influence_frame(frame)
    eurusd = ranking.loc[ranking["pair"] == "EURUSD"].iloc[0]

    assert float(eurusd["local_belief_score"]) < 0.5
    for column in [
        "local_belief_score",
        "basket_alignment_score",
        "peer_confluence_score",
        "consensus_score",
        "influence_score",
        "recommendation_strength",
    ]:
        assert math.isfinite(float(eurusd[column]))
