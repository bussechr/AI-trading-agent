from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest

from fxstack.features.multi_tf_contract import (
    _attach_point_in_time_cross_pair_context,
    _merge_context_asof,
    build_latest_multi_tf_row,
    build_multi_tf_rows,
    raw_multi_tf_source_contract,
)
from fxstack.features.session_contract import MULTI_TF_CONTRACT_VERSION
from fxstack.io.parquet_store import ParquetStore


def _bars(pair: str, timeframe: str, rows: int = 600) -> pd.DataFrame:
    base = 1.10 if pair == "EURUSD" else 145.0
    step = 0.0001 if pair == "EURUSD" else 0.01
    tf_minutes = {"M1": 1, "M5": 5, "M15": 15, "H1": 60, "H4": 240, "D": 1440}[timeframe]
    out = []
    for i in range(rows):
        px = base + (i * step)
        out.append(
            {
                "pair": pair,
                "ts": pd.Timestamp("2026-01-01T00:00:00Z")
                + pd.Timedelta(minutes=tf_minutes * i),
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
                "date": (
                    pd.Timestamp("2026-01-01T00:00:00Z")
                    + pd.Timedelta(minutes=tf_minutes * i)
                ).strftime("%Y-%m-%d"),
            }
        )
    return pd.DataFrame(out)


def _bars_with_contract_columns(
    pair: str, timeframe: str, rows: int = 600
) -> pd.DataFrame:
    df = _bars(pair, timeframe, rows=rows)
    close_ts = pd.to_datetime(df["ts"], utc=True) + pd.Timedelta(minutes=5)
    prefix = str(timeframe).lower()
    df["close_ts"] = close_ts
    df["anchor_close_ts"] = close_ts
    df[f"{prefix}_ts"] = pd.to_datetime(df["ts"], utc=True)
    df[f"{prefix}_close_ts"] = close_ts
    df["context_frame_profile"] = "preexisting"
    df["h1_available"] = 0
    df["cross_pair_dispersion"] = 999.0
    return df


def _latest_log_return(frame: pd.DataFrame) -> float:
    close = frame["mid_close"].astype(float)
    return math.log(float(close.iloc[-1]) / float(close.iloc[-2]))


def _attach_cross_context(
    *,
    anchor_ts: pd.Timestamp,
    frames: dict[str, pd.DataFrame],
) -> tuple[pd.DataFrame, dict[str, object]]:
    return _attach_point_in_time_cross_pair_context(
        pd.DataFrame([{"ts": anchor_ts}]),
        pair_set=["EURUSD", "USDJPY"],
        anchor_timeframe="M5",
        load_raw=lambda symbol: frames.get(symbol, pd.DataFrame()),
    )


def test_cross_pair_context_uses_signed_log_returns() -> None:
    eurusd = _bars("EURUSD", "M5", rows=80)
    usdjpy = _bars("USDJPY", "M5", rows=80)

    out, report = _attach_cross_context(
        anchor_ts=pd.Timestamp(eurusd["ts"].iloc[-1]),
        frames={"EURUSD": eurusd, "USDJPY": usdjpy},
    )

    expected = (-_latest_log_return(eurusd) + _latest_log_return(usdjpy)) / 2.0
    assert float(out["usd_strength_basket_ret_1"].iloc[0]) == pytest.approx(expected)
    assert int(out["usd_strength_observed_count"].iloc[0]) == 2
    assert float(out["usd_strength_coverage"].iloc[0]) == pytest.approx(1.0)
    assert report["return_convention"] == "signed_log_return"


@pytest.mark.parametrize(
    ("peer_rows", "expected_count", "expected_coverage", "expected_age_secs"),
    [
        (79, 2, 1.0, 300.0),
        (78, 1, 0.5, 0.0),
    ],
)
def test_cross_pair_context_bounds_backward_alignment_to_one_bar(
    peer_rows: int,
    expected_count: int,
    expected_coverage: float,
    expected_age_secs: float,
) -> None:
    eurusd = _bars("EURUSD", "M5", rows=80)
    usdjpy = _bars("USDJPY", "M5", rows=peer_rows)

    out, _ = _attach_cross_context(
        anchor_ts=pd.Timestamp(eurusd["ts"].iloc[-1]),
        frames={"EURUSD": eurusd, "USDJPY": usdjpy},
    )

    assert int(out["cross_pair_observed_count"].iloc[0]) == expected_count
    assert float(out["cross_pair_coverage"].iloc[0]) == pytest.approx(expected_coverage)
    assert float(out["cross_pair_max_age_secs"].iloc[0]) == pytest.approx(
        expected_age_secs
    )


def test_cross_pair_context_missing_peer_uses_observed_denominator() -> None:
    eurusd = _bars("EURUSD", "M5", rows=80)

    out, report = _attach_cross_context(
        anchor_ts=pd.Timestamp(eurusd["ts"].iloc[-1]),
        frames={"EURUSD": eurusd},
    )

    # EURUSD is USD-quoted, so its log return contributes with a negative sign.
    assert float(out["usd_strength_basket_ret_1"].iloc[0]) == pytest.approx(
        -_latest_log_return(eurusd)
    )
    assert int(out["cross_pair_observed_count"].iloc[0]) == 1
    assert float(out["cross_pair_coverage"].iloc[0]) == pytest.approx(0.5)
    assert report["aligned_symbols"] == ["EURUSD"]


def test_build_multi_tf_rows_resamples_h1_and_joins_pit(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    provider = "dukascopy"
    store.write_partitioned(
        _bars("EURUSD", "M5", rows=4000),
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )
    store.write_partitioned(
        _bars("USDJPY", "M5", rows=4000),
        provider=provider,
        pair="USDJPY",
        timeframe="M5",
    )
    store.write_partitioned(
        _bars("EURUSD", "M15", rows=1400),
        provider=provider,
        pair="EURUSD",
        timeframe="M15",
    )
    store.write_partitioned(
        _bars("EURUSD", "H4", rows=120),
        provider=provider,
        pair="EURUSD",
        timeframe="H4",
    )
    store.write_partitioned(
        _bars("EURUSD", "D", rows=80), provider=provider, pair="EURUSD", timeframe="D"
    )

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
    for prefix in ("m15", "h1", "h4", "d"):
        assert {f"{prefix}_available", f"{prefix}_fresh", f"{prefix}_age_secs"} <= set(feats.columns)
        assert feats[f"{prefix}_available"].eq(1).all()
        assert feats[f"{prefix}_fresh"].eq(1).all()
    assert report["join_integrity"]["joined_contexts"] == ["M15", "H1", "H4", "D"]


def test_build_multi_tf_rows_retries_when_raw_stream_changes_during_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "dukascopy"
    store = ParquetStore(tmp_path)
    anchor = _bars("EURUSD", "M5", rows=800)
    context = _bars("EURUSD", "H1", rows=80)
    store.write_partitioned(
        anchor,
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )
    store.write_partitioned(
        context,
        provider=provider,
        pair="EURUSD",
        timeframe="H1",
    )
    original_read = ParquetStore.read_pair_timeframe
    mutated = False

    def _read_with_one_concurrent_mutation(
        self: ParquetStore,
        **kwargs: object,
    ) -> pd.DataFrame:
        nonlocal mutated
        out = original_read(self, **kwargs)
        if (
            not mutated
            and str(kwargs.get("pair")) == "EURUSD"
            and str(kwargs.get("timeframe")) == "M5"
        ):
            mutated = True
            revised = context.copy()
            revised.loc[revised.index[-1], "mid_close"] += 0.01
            ParquetStore(tmp_path).write_partitioned(
                revised,
                provider=provider,
                pair="EURUSD",
                timeframe="H1",
            )
        return out

    monkeypatch.setattr(
        ParquetStore,
        "read_pair_timeframe",
        _read_with_one_concurrent_mutation,
    )

    feats, report = build_multi_tf_rows(
        pair="EURUSD",
        raw_store_root=tmp_path,
        provider=provider,
        anchor_timeframe="M5",
        context_timeframes=["H1"],
        all_pairs=["EURUSD"],
    )
    verified = raw_multi_tf_source_contract(
        raw_store_root=tmp_path,
        provider=provider,
        pair="EURUSD",
        anchor_timeframe="M5",
        context_timeframes=["H1"],
        all_pairs=["EURUSD"],
    )

    assert mutated is True
    assert not feats.empty
    assert report["raw_source_snapshot_attempts"] == 2
    assert report["raw_source_contract"]["fingerprint"] == verified["fingerprint"]
    assert feats["raw_source_fingerprint"].eq(verified["fingerprint"]).all()


def test_build_latest_multi_tf_row_retries_when_raw_stream_changes_during_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = "dukascopy"
    store = ParquetStore(tmp_path)
    anchor = _bars("EURUSD", "M5", rows=800)
    context = _bars("EURUSD", "H1", rows=80)
    store.write_partitioned(
        anchor,
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )
    store.write_partitioned(
        context,
        provider=provider,
        pair="EURUSD",
        timeframe="H1",
    )
    original_read = ParquetStore.read_recent_rows
    mutated = False

    def _read_recent_with_one_concurrent_mutation(
        self: ParquetStore,
        **kwargs: object,
    ) -> pd.DataFrame:
        nonlocal mutated
        out = original_read(self, **kwargs)
        if (
            not mutated
            and str(kwargs.get("pair")) == "EURUSD"
            and str(kwargs.get("timeframe")) == "M5"
        ):
            mutated = True
            revised = context.copy()
            revised.loc[revised.index[-1], "mid_close"] += 0.01
            ParquetStore(tmp_path).write_partitioned(
                revised,
                provider=provider,
                pair="EURUSD",
                timeframe="H1",
            )
        return out

    monkeypatch.setattr(
        ParquetStore,
        "read_recent_rows",
        _read_recent_with_one_concurrent_mutation,
    )

    latest, report = build_latest_multi_tf_row(
        pair="EURUSD",
        raw_store_root=tmp_path,
        provider=provider,
        anchor_timeframe="M5",
        context_timeframes=["H1"],
        all_pairs=["EURUSD"],
    )

    assert mutated is True
    assert not latest.empty
    assert report["raw_source_snapshot_attempts"] == 2
    assert report["raw_source_contract"]["partition_scope"] == "tail:14"
    assert latest["raw_source_fingerprint"].eq(
        report["raw_source_contract"]["fingerprint"]
    ).all()


def test_multi_tf_contract_builders_ignore_existing_contract_columns(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    provider = "dukascopy"
    store.write_partitioned(
        _bars_with_contract_columns("EURUSD", "M5", rows=4000),
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )
    store.write_partitioned(
        _bars_with_contract_columns("USDJPY", "M5", rows=4000),
        provider=provider,
        pair="USDJPY",
        timeframe="M5",
    )
    store.write_partitioned(
        _bars_with_contract_columns("EURUSD", "M15", rows=1400),
        provider=provider,
        pair="EURUSD",
        timeframe="M15",
    )
    store.write_partitioned(
        _bars_with_contract_columns("EURUSD", "H4", rows=120),
        provider=provider,
        pair="EURUSD",
        timeframe="H4",
    )

    feats, report = build_multi_tf_rows(
        pair="EURUSD",
        raw_store_root=tmp_path,
        provider=provider,
        anchor_timeframe="M5",
        context_timeframes=["M15", "H1", "H4"],
        all_pairs=["EURUSD", "USDJPY"],
    )
    latest, latest_report = build_latest_multi_tf_row(
        pair="EURUSD",
        raw_store_root=tmp_path,
        provider=provider,
        anchor_timeframe="M5",
        context_timeframes=["M15", "H1", "H4"],
        all_pairs=["EURUSD", "USDJPY"],
    )

    assert not feats.empty
    assert not latest.empty
    assert feats.columns.is_unique
    assert latest.columns.is_unique
    assert "anchor_close_ts" in feats.columns
    assert "m15_close_ts" in feats.columns
    assert feats["context_frame_profile"].iloc[0] == MULTI_TF_CONTRACT_VERSION
    assert latest["context_frame_profile"].iloc[0] == MULTI_TF_CONTRACT_VERSION
    assert report["join_integrity"]["joined_contexts"] == ["M15", "H1", "H4"]
    assert latest_report["join_integrity"]["joined_contexts"] == ["M15", "H1", "H4"]
    assert report["raw_source_contract"]["partition_scope"] == "all"
    assert latest_report["raw_source_contract"]["partition_scope"] == "tail:14"
    assert (
        report["raw_source_contract"]["fingerprint"]
        != latest_report["raw_source_contract"]["fingerprint"]
    )
    cross_columns = [
        "usd_strength_basket_ret_1",
        "cross_pair_dispersion",
        "cross_pair_available",
        "cross_pair_observed_count",
        "cross_pair_coverage",
        "cross_pair_max_age_secs",
        "usd_strength_available",
        "usd_strength_observed_count",
        "usd_strength_coverage",
    ]
    for column in cross_columns:
        assert float(latest[column].iloc[0]) == pytest.approx(
            float(feats[column].iloc[-1])
        )
    for prefix in ("m15", "h1", "h4"):
        for suffix in ("available", "fresh", "age_secs"):
            column = f"{prefix}_{suffix}"
            assert float(latest[column].iloc[0]) == pytest.approx(
                float(feats[column].iloc[-1])
            )
    assert report["cross_pair_context"]["return_convention"] == "signed_log_return"
    assert (
        latest_report["cross_pair_context"]["return_convention"] == "signed_log_return"
    )


def test_stale_h1_rows_are_rejected_before_inference(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    provider = "dukascopy"
    store.write_partitioned(
        _bars("EURUSD", "M5", rows=4000),
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )
    # A real H1 stream exists, but it ends many days before the latest M5 row.
    store.write_partitioned(
        _bars("EURUSD", "H1", rows=80),
        provider=provider,
        pair="EURUSD",
        timeframe="H1",
    )

    feats, report = build_multi_tf_rows(
        pair="EURUSD",
        raw_store_root=tmp_path,
        provider=provider,
        anchor_timeframe="M5",
        context_timeframes=["H1"],
    )
    latest, latest_report = build_latest_multi_tf_row(
        pair="EURUSD",
        raw_store_root=tmp_path,
        provider=provider,
        anchor_timeframe="M5",
        context_timeframes=["H1"],
    )

    assert not feats.empty
    assert latest.empty
    assert "h1_ret_1" in feats.columns
    assert feats["h1_available"].eq(1).all()
    assert feats["h1_fresh"].eq(1).all()
    assert int(report["join_integrity"]["H1"]["stale_rows"]) > 0
    assert int(report["join_integrity"]["stale_context_rows_rejected"]) > 0
    assert int(latest_report["join_integrity"]["H1"]["fresh_rows"]) == 0
    assert int(latest_report["join_integrity"]["stale_context_rows_rejected"]) == 1
    assert float(report["join_integrity"]["H1"]["expected_interval_secs"]) == 3600.0


def test_merge_context_masks_stale_values_and_keeps_diagnostics() -> None:
    anchor = pd.DataFrame(
        {"anchor_close_ts": [pd.Timestamp("2026-01-01T03:00:00Z")]}
    )
    context = pd.DataFrame(
        {
            "h1_close_ts": [pd.Timestamp("2026-01-01T01:00:00Z")],
            "h1_ret_1": [0.012],
            "h1_trend_slope_20": [0.004],
        }
    )

    merged, report = _merge_context_asof(anchor, context, timeframe="H1")

    assert int(merged.loc[0, "h1_available"]) == 1
    assert int(merged.loc[0, "h1_fresh"]) == 0
    assert float(merged.loc[0, "h1_age_secs"]) == 7200.0
    assert pd.isna(merged.loc[0, "h1_ret_1"])
    assert pd.isna(merged.loc[0, "h1_trend_slope_20"])
    assert int(report["stale_rows"]) == 1
