from __future__ import annotations

import pandas as pd

from fxstack.labels.triple_barrier import TripleBarrierConfig, triple_barrier_labels


def test_triple_barrier_hits_take_profit():
    rows = []
    px = 1.1000
    for i in range(40):
        px += 0.0005
        rows.append(
            {
                "pair": "EURUSD",
                "ts": pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
                "timeframe": "M5",
                "mid_open": px - 0.0001,
                "mid_high": px + 0.0001,
                "mid_low": px - 0.0002,
                "mid_close": px,
                "atr_14": 0.0002,
            }
        )
    df = pd.DataFrame(rows)
    out = triple_barrier_labels(
        df,
        TripleBarrierConfig(horizon_bars=10, tp_atr_mult=1.0, sl_atr_mult=1.0),
    )
    assert len(out) == len(df)
    assert int(out["label"].iloc[0]) == 1
