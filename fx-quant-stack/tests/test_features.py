from __future__ import annotations

import pandas as pd

from fxstack.features.build import build_features, leakage_guard


def test_feature_build_is_deterministic():
    rows = []
    px = 1.1
    for i in range(300):
        px += 0.0001
        rows.append(
            {
                "pair": "EURUSD",
                "ts": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
                "timeframe": "M5",
                "mid_open": px - 0.0001,
                "mid_high": px + 0.0002,
                "mid_low": px - 0.0002,
                "mid_close": px,
                "spread": 0.00005,
            }
        )
    df = pd.DataFrame(rows)
    f1 = build_features(df)
    f2 = build_features(df)
    leakage_guard(f1)
    assert len(f1) == len(f2)
    assert f1.iloc[-1]["ret_1"] == f2.iloc[-1]["ret_1"]
