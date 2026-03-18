from __future__ import annotations

from pathlib import Path

import pandas as pd

from fxstack.data.provider_migration import migrate_provider_partitions
from fxstack.io.parquet_store import ParquetStore


def _sample_bars() -> pd.DataFrame:
    ts = pd.date_range("2024-01-01 00:00:00", periods=6, freq="5min", tz="UTC")
    out = pd.DataFrame(
        {
            "pair": "EURUSD",
            "ts": ts,
            "timeframe": "M5",
            "bid_open": [1.1 + i * 0.0001 for i in range(6)],
            "bid_high": [1.1002 + i * 0.0001 for i in range(6)],
            "bid_low": [1.0998 + i * 0.0001 for i in range(6)],
            "bid_close": [1.1001 + i * 0.0001 for i in range(6)],
            "ask_open": [1.1 + i * 0.0001 for i in range(6)],
            "ask_high": [1.1002 + i * 0.0001 for i in range(6)],
            "ask_low": [1.0998 + i * 0.0001 for i in range(6)],
            "ask_close": [1.1001 + i * 0.0001 for i in range(6)],
            "mid_open": [1.1 + i * 0.0001 for i in range(6)],
            "mid_high": [1.1002 + i * 0.0001 for i in range(6)],
            "mid_low": [1.0998 + i * 0.0001 for i in range(6)],
            "mid_close": [1.1001 + i * 0.0001 for i in range(6)],
            "spread": [0.0] * 6,
            "volume": [100 + i for i in range(6)],
            "date": [d.strftime("%Y-%m-%d") for d in ts],
        }
    )
    return out


def test_provider_migration_dry_run_then_apply(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    src_df = _sample_bars()
    store.write_partitioned(src_df, provider="oanda", pair="EURUSD", timeframe="M5")

    dry = migrate_provider_partitions(
        store_root=tmp_path,
        source_provider="oanda",
        target_provider="dukascopy",
        dry_run=True,
        remove_source=False,
    )
    assert dry["dry_run"] is True
    assert int(dry["files_scanned"]) > 0
    assert int(dry["rows_written"]) == 0
    assert not (tmp_path / "provider=dukascopy").exists()

    apply_one = migrate_provider_partitions(
        store_root=tmp_path,
        source_provider="oanda",
        target_provider="dukascopy",
        dry_run=False,
        remove_source=False,
    )
    assert apply_one["dry_run"] is False
    out_df = store.read_pair_timeframe(provider="dukascopy", pair="EURUSD", timeframe="M5")
    assert len(out_df) == len(src_df)

    apply_two = migrate_provider_partitions(
        store_root=tmp_path,
        source_provider="oanda",
        target_provider="dukascopy",
        dry_run=False,
        remove_source=True,
    )
    out_df_2 = store.read_pair_timeframe(provider="dukascopy", pair="EURUSD", timeframe="M5")
    assert len(out_df_2) == len(src_df)
    assert int(apply_two["rows_scanned"]) == int(apply_two["rows_written"])
    assert apply_two["removed_source"] is True
    assert not (tmp_path / "provider=oanda").exists()
