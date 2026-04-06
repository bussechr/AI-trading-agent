from __future__ import annotations

import pandas as pd

from fxstack.belief.outcome_labels import label_hypothesis_outcomes


def test_label_hypothesis_outcomes_respects_confirmation_and_fail_fast() -> None:
    ts = pd.date_range("2026-03-26T12:00:00Z", periods=13, freq="5min")
    base = pd.DataFrame(
        {
            "ts": ts.astype(str),
            "mid_close": [1.1000, 1.1003, 1.1007, 1.1010, 1.1013, 1.1016, 1.1018, 1.1020, 1.1021, 1.1022, 1.1023, 1.1024, 1.1025],
            "spread_bps": [0.5] * 13,
            "vol_20": [0.0001] * 13,
            "vol_60": [0.0001] * 13,
        }
    )
    candidates = pd.DataFrame(
        [
            {"pair": "EURUSD", "ts": str(ts[0]), "row_idx": 0, "scenario": "trend_pullback", "side": "long"},
            {"pair": "EURUSD", "ts": str(ts[0]), "row_idx": 0, "scenario": "trend_pullback", "side": "short"},
        ]
    )

    labeled = label_hypothesis_outcomes(candidates, base_frame=base, slippage_bps=0.25, min_expected_edge_bps=3.0)
    long_row = labeled.loc[labeled["side"].eq("long")].iloc[0]
    short_row = labeled.loc[labeled["side"].eq("short")].iloc[0]

    assert float(long_row["net_ev_bps"]) > 12.0
    assert int(long_row["confirm_success"]) == 1
    assert int(long_row["fail_fast"]) == 0
    assert int(long_row["relevance"]) == 4
    assert int(long_row["ev_above_hurdle"]) == 1

    assert float(short_row["net_ev_bps"]) < 0.0
    assert int(short_row["confirm_success"]) == 0
    assert int(short_row["fail_fast"]) == 1
    assert int(short_row["relevance"]) == 0
