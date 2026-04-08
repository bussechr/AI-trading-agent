from __future__ import annotations

import pandas as pd

from fxstack.io.parquet_store import ParquetStore
from fxstack.runtime.runner import (
    _bars_to_raw_frame,
    _bootstrap_pair_features_from_local_snapshot,
    _entry_venue_readiness_reasons,
    _feature_row_is_stale,
    _latest_feature_row,
    _refresh_pair_feature_tails_from_local_snapshot,
)
from fxstack.settings import get_settings


def test_feature_row_is_stale_when_missing() -> None:
    assert _feature_row_is_stale(row=pd.DataFrame(), loop_ts=1775650538.0, timeframe="M5") is True


def test_feature_row_is_stale_when_fresh() -> None:
    row = pd.DataFrame([{"ts": pd.Timestamp("2026-04-08T12:10:00Z")}])
    assert _feature_row_is_stale(row=row, loop_ts=pd.Timestamp("2026-04-08T12:15:30Z").timestamp(), timeframe="M5") is False


def test_feature_row_is_stale_when_old() -> None:
    row = pd.DataFrame([{"ts": pd.Timestamp("2026-04-08T11:55:00Z")}])
    assert _feature_row_is_stale(row=row, loop_ts=pd.Timestamp("2026-04-08T12:15:30Z").timestamp(), timeframe="M5") is True


def test_entry_venue_readiness_reasons_are_skipped_in_paper_mode() -> None:
    assert _entry_venue_readiness_reasons(paper_mode=True, mt4_fresh=False, ticks_fresh=False, tick_present=False) == []
    assert _entry_venue_readiness_reasons(paper_mode=False, mt4_fresh=False, ticks_fresh=False, tick_present=False) == [
        "mt4_stale",
        "tick_feed_stale",
        "missing_live_tick",
    ]


def _seed_raw_snapshot(store: ParquetStore, *, pair: str, timeframe: str, start: str, step: str, count: int) -> None:
    base = pd.Timestamp(start)
    delta = pd.Timedelta(step)
    bars = []
    for index in range(count):
        price = 1.05 + (index * 0.001)
        ts = base + (index * delta)
        bars.append(
            {
                "time": ts.isoformat(),
                "mid_open": price,
                "mid_high": price + 0.0004,
                "mid_low": price - 0.0004,
                "mid_close": price + 0.0002,
                "spread": 0.0002,
                "volume": 100 + index,
            }
        )
    provider = str(get_settings().normalized_data_provider)
    store.write_partitioned(
        _bars_to_raw_frame(pair=pair, timeframe=timeframe, bars=bars),
        provider=provider,
        pair=pair,
        timeframe=timeframe,
    )


def test_local_snapshot_bootstrap_populates_missing_feature_rows(tmp_path) -> None:
    feature_store = ParquetStore(tmp_path / "feature")
    raw_store = ParquetStore(tmp_path / "raw")
    provider = str(get_settings().normalized_data_provider)

    _seed_raw_snapshot(raw_store, pair="EURUSD", timeframe="M5", start="2026-04-08T12:00:00Z", step="5min", count=24)

    ok, detail = _bootstrap_pair_features_from_local_snapshot(
        feature_store=feature_store,
        raw_store=raw_store,
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )

    row = _latest_feature_row(store=feature_store, raw_store=raw_store, pair="EURUSD", timeframe="M5", all_pairs=["EURUSD"])
    assert ok is True
    assert detail.startswith("rows=")
    assert not row.empty


def test_local_snapshot_refresh_updates_feature_tails_without_live_bars(tmp_path) -> None:
    feature_store = ParquetStore(tmp_path / "feature")
    raw_store = ParquetStore(tmp_path / "raw")
    provider = str(get_settings().normalized_data_provider)

    _seed_raw_snapshot(raw_store, pair="EURUSD", timeframe="M5", start="2026-04-08T12:00:00Z", step="5min", count=24)
    _seed_raw_snapshot(raw_store, pair="EURUSD", timeframe="H4", start="2026-04-01T00:00:00Z", step="4h", count=12)
    _seed_raw_snapshot(raw_store, pair="EURUSD", timeframe="D", start="2026-03-01T00:00:00Z", step="1d", count=12)

    diag = _refresh_pair_feature_tails_from_local_snapshot(
        feature_store=feature_store,
        raw_store=raw_store,
        provider=provider,
        pair="EURUSD",
    )

    m5_row = _latest_feature_row(store=feature_store, raw_store=raw_store, pair="EURUSD", timeframe="M5", all_pairs=["EURUSD"])
    h4_row = _latest_feature_row(store=feature_store, raw_store=raw_store, pair="EURUSD", timeframe="H4", all_pairs=["EURUSD"])
    d_row = _latest_feature_row(store=feature_store, raw_store=raw_store, pair="EURUSD", timeframe="D", all_pairs=["EURUSD"])

    assert diag["ok"] is True
    assert diag["reason"] == "paper_local_snapshot"
    assert bool(diag["feature_refresh"]["M5"]["ok"]) is True
    assert bool(diag["feature_refresh"]["H4"]["ok"]) is True
    assert bool(diag["feature_refresh"]["D"]["ok"]) is True
    assert not m5_row.empty
    assert not h4_row.empty
    assert not d_row.empty


def test_latest_feature_row_enriches_parquet_fast_path_with_single_pair_cross_features(tmp_path) -> None:
    feature_store = ParquetStore(tmp_path / "feature")
    raw_store = ParquetStore(tmp_path / "raw")
    provider = str(get_settings().normalized_data_provider)

    _seed_raw_snapshot(raw_store, pair="EURUSD", timeframe="M5", start="2026-04-08T12:00:00Z", step="5min", count=24)
    _seed_raw_snapshot(raw_store, pair="EURUSD", timeframe="H4", start="2026-04-01T00:00:00Z", step="4h", count=12)
    _seed_raw_snapshot(raw_store, pair="EURUSD", timeframe="D", start="2026-03-01T00:00:00Z", step="1d", count=12)

    diag = _refresh_pair_feature_tails_from_local_snapshot(
        feature_store=feature_store,
        raw_store=raw_store,
        provider=provider,
        pair="EURUSD",
    )
    assert diag["ok"] is True

    row = _latest_feature_row(
        store=feature_store,
        raw_store=raw_store,
        pair="EURUSD",
        timeframe="M5",
        all_pairs=["EURUSD"],
    )

    assert not row.empty
    assert "usd_strength_basket_ret_1" in row.columns
    assert "cross_pair_dispersion" in row.columns
    assert float(row.iloc[0]["usd_strength_basket_ret_1"]) == 0.0
    assert float(row.iloc[0]["cross_pair_dispersion"]) == 0.0
