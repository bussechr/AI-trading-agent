from __future__ import annotations

from pathlib import Path

import pandas as pd

from fxstack.features.multi_tf_contract import build_multi_tf_rows
from fxstack.io.parquet_store import ParquetStore


def _bars(pair: str, timeframe: str, rows: int = 600) -> pd.DataFrame:
    base = 1.10 if pair == "EURUSD" else 145.0
    step = 0.0001 if pair == "EURUSD" else 0.01
    tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "H4": 240, "D": 1440}[timeframe]
    out = []
    for i in range(rows):
        px = base + (i * step)
        out.append(
            {
                "pair": pair,
                "ts": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=tf_minutes * i),
                "timeframe": timeframe,
                "bid_open": px - step,
                "bid_high": px + (2 * step),
                "bid_low": px - (2 * step),
                "bid_close": px - (0.5 * step),
                "ask_open": px + step,
                "ask_high": px + (3 * step),
                "ask_low": px,
                "ask_close": px + (0.5 * step),
                "mid_open": px,
                "mid_high": px + (2 * step),
                "mid_low": px - (2 * step),
                "mid_close": px + (0.25 * step),
                "spread": step,
                "volume": 100.0 + i,
                "date": (pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=tf_minutes * i)).strftime("%Y-%m-%d"),
            }
        )
    return pd.DataFrame(out)


def test_build_multi_tf_rows_resamples_h1_and_joins_pit(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    provider = "dukascopy"
    store.write_partitioned(_bars("EURUSD", "M5", rows=4000), provider=provider, pair="EURUSD", timeframe="M5")
    store.write_partitioned(_bars("USDJPY", "M5", rows=4000), provider=provider, pair="USDJPY", timeframe="M5")
    store.write_partitioned(_bars("EURUSD", "M15", rows=1400), provider=provider, pair="EURUSD", timeframe="M15")
    store.write_partitioned(_bars("EURUSD", "H4", rows=120), provider=provider, pair="EURUSD", timeframe="H4")
    store.write_partitioned(_bars("EURUSD", "D", rows=80), provider=provider, pair="EURUSD", timeframe="D")

    feats, report = build_multi_tf_rows(
        pair="EURUSD",
        raw_store_root=tmp_path,
        provider=provider,
        anchor_timeframe="M5",
        context_timeframes=["M15", "H1", "H4", "D"],
        all_pairs=["EURUSD", "USDJPY"],
    )

    assert not feats.empty
    assert "h1_ret_1" in feats.columns
    assert "usd_strength_basket_ret_1" in feats.columns
    assert "context_frame_profile" in feats.columns
    assert (feats["anchor_close_ts"] >= feats["h1_close_ts"]).all()
    assert report["join_integrity"]["joined_contexts"] == ["M15", "H1", "H4", "D"]
