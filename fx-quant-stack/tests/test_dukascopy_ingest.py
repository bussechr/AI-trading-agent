from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from fxstack.backtest.engine import evaluate_signals
from fxstack.backtest.reports import summarize_backtest
from fxstack.data.ingest import normalize_dukascopy_bars
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings
from fxstack.tasks import (
    build_features_task,
    build_labels_task,
    ingest_task,
    train_intraday_task,
)


def _write_mid_csv(path: Path, *, rows: int = 220) -> None:
    ts = pd.date_range("2024-01-01 00:00:00", periods=rows, freq="5min", tz="UTC")
    base = 1.10
    wave = [0.0015 * math.sin(i / 8.0) for i in range(rows)]
    drift = [0.00001 * i for i in range(rows)]
    px = [base + wave[i] + drift[i] for i in range(rows)]
    raw = pd.DataFrame(
        {
            "Gmt time": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "Open": px,
            "High": [x + 0.0003 for x in px],
            "Low": [x - 0.0002 for x in px],
            "Close": [x + (0.00005 if (i % 2 == 0) else -0.00005) for i, x in enumerate(px)],
            "Volume": [100 + i for i in range(rows)],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    raw.to_csv(path, index=False)


def _refresh_settings_cache() -> None:
    get_settings.cache_clear()


def test_normalize_dukascopy_mid_only_sorts_and_dedupes() -> None:
    raw = pd.DataFrame(
        {
            "Gmt time": ["2024-01-01 00:10:00", "2024-01-01 00:00:00", "2024-01-01 00:10:00"],
            "Open": [1.1002, 1.1000, 1.1003],
            "High": [1.1004, 1.1002, 1.1005],
            "Low": [1.1000, 1.0998, 1.1001],
            "Close": [1.1001, 1.1001, 1.1002],
            "Volume": [10, 11, 12],
        }
    )

    out = normalize_dukascopy_bars(raw=raw, pair="EURUSD", timeframe="M5")
    assert len(out) == 2
    assert list(out["ts"]) == sorted(list(out["ts"]))
    assert float(out["spread"].abs().max()) == 0.0
    assert {"bid_open", "ask_open", "mid_open", "date"}.issubset(set(out.columns))


def test_normalize_dukascopy_missing_required_columns_raises() -> None:
    raw = pd.DataFrame({"time": ["2024-01-01 00:00:00"], "open": [1.1]})
    with pytest.raises(ValueError, match="OHLC|required"):
        normalize_dukascopy_bars(raw=raw, pair="EURUSD", timeframe="M5")


def test_normalize_dukascopy_accepts_date_timestamp_column() -> None:
    raw = pd.DataFrame(
        {
            "date": ["2024-01-01T00:00:00Z", "2024-01-01T00:05:00Z"],
            "open": [1.1, 1.1001],
            "high": [1.1003, 1.1004],
            "low": [1.0999, 1.1],
            "close": [1.1002, 1.1001],
        }
    )
    out = normalize_dukascopy_bars(raw=raw, pair="EURUSD", timeframe="M5")
    assert len(out) == 2
    assert str(out.iloc[0]["timeframe"]) == "M5"


def test_ingest_task_writes_dukascopy_provider_partition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root = tmp_path / "dukascopy"
    csv_path = source_root / "EURUSD_M5.csv"
    _write_mid_csv(csv_path, rows=80)

    monkeypatch.setenv("FXSTACK_DATA_PROVIDER", "dukascopy")
    monkeypatch.setenv("FXSTACK_DUKASCOPY_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("FXSTACK_DUKASCOPY_FILE_PATTERN", "{pair}_{granularity}.csv")
    _refresh_settings_cache()

    store_root = tmp_path / "raw"
    out = ingest_task(
        pair="EURUSD",
        granularity="M5",
        store_root=str(store_root),
        source_root="",
        file_pattern="",
        csv_path="",
    )
    assert "provider=dukascopy" in str(out["path"])
    df = ParquetStore(store_root).read_pair_timeframe(provider="dukascopy", pair="EURUSD", timeframe="M5")
    assert not df.empty


def test_pipeline_ingest_features_labels_train_backtest_with_dukascopy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "dukascopy"
    csv_path = source_root / "EURUSD_M5.csv"
    _write_mid_csv(csv_path, rows=240)

    monkeypatch.setenv("FXSTACK_DATA_PROVIDER", "dukascopy")
    monkeypatch.setenv("FXSTACK_DUKASCOPY_SOURCE_ROOT", str(source_root))
    monkeypatch.setenv("FXSTACK_DUKASCOPY_FILE_PATTERN", "{pair}_{granularity}.csv")
    _refresh_settings_cache()

    raw_root = tmp_path / "raw"
    feat_root = tmp_path / "features"
    label_root = tmp_path / "labels"
    artifact_out = tmp_path / "artifacts" / "intraday_xgb"

    ingest_task(pair="EURUSD", granularity="M5", store_root=str(raw_root))
    build_features_task(pair="EURUSD", timeframe="M5", input_root=str(raw_root), output_root=str(feat_root))
    build_labels_task(
        pair="EURUSD",
        timeframe="M5",
        feature_root=str(feat_root),
        label_root=str(label_root),
        horizon_bars=8,
        tp_mult=1.2,
        sl_mult=1.0,
    )
    train_intraday_task(
        pair="EURUSD",
        timeframe="M5",
        feature_root=str(feat_root),
        label_root=str(label_root),
        out=str(artifact_out),
    )
    assert (artifact_out / "meta.json").exists()

    feats = ParquetStore(feat_root).read_pair_timeframe(provider="dukascopy", pair="EURUSD", timeframe="M5")
    signals = feats[["pair", "ts"]].copy()
    signals["expected_edge_bps"] = feats["ret_1"].astype(float) * 10000.0
    signals["spread_bps"] = feats.get("spread", 0.0).astype(float) * 10000.0
    signals["allowed"] = True
    scored = evaluate_signals(signals)
    summary = summarize_backtest(scored)
    assert "trades" in summary
