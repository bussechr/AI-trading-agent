from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from fxstack.runtime.runner import _prepare_pair_rows_for_scoring
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings


class _Model:
    def __init__(self, feature_columns: list[str] | None = None) -> None:
        self.feature_columns = list(feature_columns or [])


def _bars(pair: str, timeframe: str, rows: int = 600) -> pd.DataFrame:
    base = 1.10 if pair == "EURUSD" else 145.0
    step = 0.0001 if pair == "EURUSD" else 0.01
    tf_minutes = {"M5": 5}[timeframe]
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


def test_prepare_pair_rows_for_scoring_enriches_nan_meta_fields_from_raw_contract(tmp_path) -> None:
    provider = get_settings().normalized_data_provider
    store = ParquetStore(tmp_path)
    store.write_partitioned(_bars("EURUSD", "M5", rows=4000), provider=provider, pair="EURUSD", timeframe="M5")
    store.write_partitioned(_bars("USDJPY", "M5", rows=4000), provider=provider, pair="USDJPY", timeframe="M5")

    row = store.read_pair_timeframe(provider=provider, pair="EURUSD", timeframe="M5").sort_values("ts").tail(1).copy()
    row["m15_ret_1"] = float("nan")
    row["cross_pair_dispersion"] = float("nan")

    loaded = SimpleNamespace(
        scorer=SimpleNamespace(
            swing_model=_Model([]),
            intraday_model=_Model([]),
            meta_model=_Model(["m15_ret_1", "cross_pair_dispersion"]),
        ),
        exit_model=None,
        reversal_failure_model=None,
        reversal_opportunity_model=None,
    )

    prepared = _prepare_pair_rows_for_scoring(
        raw_store=store,
        pair="EURUSD",
        loaded=loaded,
        pair_rows={"M5": row},
        swing_timeframe="H1",
        intraday_timeframe="M5",
        all_pairs=["EURUSD", "USDJPY"],
    )

    out = prepared["M5"].reset_index(drop=True).iloc[0]
    assert pd.notna(out["m15_ret_1"])
    assert pd.notna(out["cross_pair_dispersion"])
