from __future__ import annotations

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
