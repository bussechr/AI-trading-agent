from __future__ import annotations

from pathlib import Path

import pandas as pd

from fxstack.feast.online_features import resolve_latest_feature_row
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings


def _bars(pair: str, timeframe: str, rows: int = 600) -> pd.DataFrame:
    base = 1.10 if pair == "EURUSD" else 145.0
    step = 0.0001 if pair == "EURUSD" else 0.01
    tf_minutes = {"M5": 5, "M15": 15, "H1": 60, "H4": 240, "D": 1440}[timeframe]
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


class _OnlineResult:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def to_df(self) -> pd.DataFrame:
        return self._df.copy()


class _OnlineStore:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def get_online_features(self, features, entity_rows):  # noqa: ANN001
        assert features
        assert isinstance(entity_rows, list)
        assert entity_rows
        assert entity_rows[0]["pair"] == "EURUSD"
        return _OnlineResult(self._df)


def test_resolve_latest_feature_row_prefers_feast_then_parquet_then_raw_contract(tmp_path: Path, monkeypatch) -> None:
    provider = get_settings().normalized_data_provider
    feature_store = ParquetStore(tmp_path / "feature")
    raw_store = ParquetStore(tmp_path / "raw")
    raw_store.write_partitioned(_bars("EURUSD", "M5", rows=600), provider=provider, pair="EURUSD", timeframe="M5")
    feature_store.write_partitioned(
        pd.DataFrame([{"pair": "EURUSD", "ts": "2026-01-01T00:00:00Z", "timeframe": "M5", "ret_5": 0.456, "h1_available": 1.0}]),
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )

    feast_row = pd.DataFrame(
        [
            {
                "pair": "EURUSD",
                "ts": "2026-01-01T00:00:00Z",
                "ret_1": 0.123,
                "source_marker": "feast",
            }
        ]
    )
    monkeypatch.setattr(
        "fxstack.feast.online_features._cached_feature_store_handle",
        lambda *args, **kwargs: _OnlineStore(feast_row),
    )
    row, telemetry = resolve_latest_feature_row(
        store=feature_store,
        raw_store=raw_store,
        pair="EURUSD",
        timeframe="M5",
        provider=provider,
        all_pairs=["EURUSD"],
    )
    assert not row.empty
    assert telemetry.source == "feast_online"
    assert telemetry.source_chain == ["feast_online", "parquet_fallback", "raw_contract_fallback"]
    assert telemetry.cache_hit is True
    assert float(row.iloc[0]["ret_1"]) == 0.123
    assert float(row.iloc[0]["ret_5"]) == 0.456
    assert "bid_open" in row.columns
    assert telemetry.details["parquet_enriched"] is True
    assert telemetry.details["raw_contract_enriched"] is True

    monkeypatch.setattr("fxstack.feast.online_features._cached_feature_store_handle", lambda *args, **kwargs: None)
    feature_store.write_partitioned(
        pd.DataFrame([{"pair": "EURUSD", "ts": "2026-01-01T01:00:00Z", "timeframe": "M5", "ret_1": 0.456}]),
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )
    row, telemetry = resolve_latest_feature_row(
        store=feature_store,
        raw_store=raw_store,
        pair="EURUSD",
        timeframe="M5",
        provider=provider,
        all_pairs=["EURUSD"],
    )
    assert not row.empty
    assert telemetry.source == "parquet_fallback"

    empty_feature_store = ParquetStore(tmp_path / "feature_empty")
    row, telemetry = resolve_latest_feature_row(
        store=empty_feature_store,
        raw_store=raw_store,
        pair="EURUSD",
        timeframe="M5",
        provider=provider,
        all_pairs=["EURUSD"],
    )
    assert not row.empty
    assert telemetry.source == "raw_contract_fallback"
