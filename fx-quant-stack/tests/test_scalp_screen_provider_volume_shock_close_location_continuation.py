"""Synthetic-only adversarial tests for frozen PVSCLC-v1 research code."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import inspect
import json
import math
import random
from pathlib import Path
from typing import Any

import pytest

import fxstack.scalp.screen_provider_volume_shock_close_location_continuation as pvsclc


BASE_EPOCH = 1_672_531_200
EVALUATION_START = "2023-01-01T00:00:00Z"
EVALUATION_END = "2023-07-01T00:00:00Z"
PREREGISTRATION_LOCK = "2023-01-01T00:00:00Z"
SOURCE_DESIGN_LOCK = "2022-12-31T23:59:59Z"
PROXY_SCHEMA = pvsclc.PROXY_CONTRACT_SCHEMA_VERSION
PROXY_PROVENANCE = {
    "venue_id": "synthetic-venue",
    "source_id": "synthetic-frozen-proxy-v1",
    "as_of_utc": PREREGISTRATION_LOCK,
    "source_cutoff_utc": "2022-12-31T23:59:00Z",
    "method": "synthetic-test-only",
    "units": "bps",
    "source_snapshot_sha256": "a" * 64,
    "frozen": True,
}
CONFIG = pvsclc.GRID[0]
SOURCE_HASHES = {
    symbol: hashlib.sha256(f"source:{symbol}".encode()).hexdigest()
    for symbol in pvsclc.FX_SYMBOLS
}


def _source_integrity(*, rows: int = 175_000) -> dict[str, dict[str, Any]]:
    return {
        symbol: {
            "rows": rows,
            "bid_stream_sha256": hashlib.sha256(f"bid:{symbol}".encode()).hexdigest(),
            "ask_stream_sha256": hashlib.sha256(f"ask:{symbol}".encode()).hexdigest(),
            "bid_zero_count": 0,
            "ask_zero_count": 0,
            "first_timestamp": EVALUATION_START,
            "last_timestamp": (
                EVALUATION_START if rows == 1 else "2023-06-30T23:59:00Z"
            ),
            "timestamp_set_sha256": hashlib.sha256(
                f"timestamps:{symbol}:{rows}".encode()
            ).hexdigest(),
            "gap_count": 0 if rows == 1 else 26,
            "maximum_gap_seconds": 0 if rows == 1 else 259_200,
        }
        for symbol in pvsclc.FX_SYMBOLS
    }


def _activity_provenance(*, rows: int = 175_000) -> dict[str, Any]:
    manifest = pvsclc._canonical_input_manifest_bytes(SOURCE_HASHES)
    assert manifest is not None
    return {
        "schema_version": pvsclc.ACTIVITY_PROVENANCE_SCHEMA_VERSION,
        "activity_metric_id": pvsclc.ACTIVITY_METRIC_ID,
        "provider": "Dukascopy public historical feed",
        "acquisition_started_at_utc": SOURCE_DESIGN_LOCK,
        "as_of_utc": PREREGISTRATION_LOCK,
        "window": {
            "start_inclusive": EVALUATION_START,
            "end_exclusive": EVALUATION_END,
        },
        "request_contract": {
            "endpoint": pvsclc.DUKASCOPY_ENDPOINT,
            "timeframe": "1MIN",
            "bid_offer_side": "B",
            "ask_offer_side": "A",
            "separate_requests": True,
            "resume": False,
            "max_retries": 7,
            "limit": 5000,
            "provider_end_inclusive_adjustment_minutes": -1,
            "out_of_window_rows_policy": "reject",
        },
        "merge_contract": {
            "method": "exact_timestamp_inner_join_with_equal_side_sets",
            "unmatched_bid_rows": 0,
            "unmatched_ask_rows": 0,
        },
        "fill_contract": {
            "mid_only_fallback": False,
            "zero_fill": False,
            "synthetic_side": False,
        },
        "coverage_contract": {
            "rows_preserved_without_filtering": True,
            "minimum_rows_per_symbol": 175_000,
            "maximum_boundary_lag_seconds": 604_800,
            "maximum_gap_seconds": 345_600,
            "timestamp_set_hash_grammar": (
                "sha256_ascii_lf_header_timestamp_then_canonical_rows_v1"
            ),
        },
        "tool_contract": {
            "tool_path": "tools/fetch_pvsclc_dual_side_volume_snapshot.py",
            "tool_sha256": "b" * 64,
            "dukascopy_python_version": "4.0-test",
            "dukascopy_python_module_sha256": "c" * 64,
            "dukascopy_python_instruments_module_sha256": "d" * 64,
            "pandas_version": "2.3-test",
            "requests_version": "2.32-test",
            "python_version": "3.12-test",
        },
        "input_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "symbols": {
            symbol: {
                "instrument_id": f"{symbol[:3]}/{symbol[3:]}",
                "output_file": f"input/{symbol}_M1.csv",
                "output_sha256": SOURCE_HASHES[symbol],
                "rows": rows,
                "bid_stream_sha256": hashlib.sha256(
                    f"bid:{symbol}".encode()
                ).hexdigest(),
                "ask_stream_sha256": hashlib.sha256(
                    f"ask:{symbol}".encode()
                ).hexdigest(),
                "unmatched_bid_rows": 0,
                "unmatched_ask_rows": 0,
                "bid_volume": {
                    "rows": rows,
                    "finite": True,
                    "nonnegative": True,
                    "missing": 0,
                    "zero_count": 0,
                },
                "ask_volume": {
                    "rows": rows,
                    "finite": True,
                    "nonnegative": True,
                    "missing": 0,
                    "zero_count": 0,
                },
                "first_timestamp": EVALUATION_START,
                "last_timestamp": (
                    EVALUATION_START if rows == 1 else "2023-06-30T23:59:00Z"
                ),
                "timestamp_set_sha256": hashlib.sha256(
                    f"timestamps:{symbol}:{rows}".encode()
                ).hexdigest(),
                "gap_count": 0 if rows == 1 else 26,
                "maximum_gap_seconds": 0 if rows == 1 else 259_200,
            }
            for symbol in pvsclc.FX_SYMBOLS
        },
    }


def _quotes(mid: float, spread_bps: float) -> tuple[float, float]:
    half = mid * spread_bps / 2e4
    return mid - half, mid + half


def _bar(
    index: int,
    *,
    mid_o: float = 100.0,
    mid_h: float = 100.05,
    mid_l: float = 99.95,
    mid_c: float = 100.0,
    spread_bps: float = 0.50,
    bid_volume: float = 50.0,
    ask_volume: float = 50.0,
    epoch_shift: int = 0,
) -> pvsclc.QuoteBar:
    bid_o, ask_o = _quotes(mid_o, spread_bps)
    bid_h, ask_h = _quotes(mid_h, spread_bps)
    bid_l, ask_l = _quotes(mid_l, spread_bps)
    bid_c, ask_c = _quotes(mid_c, spread_bps)
    return pvsclc.QuoteBar(
        epoch=BASE_EPOCH + index * 60 + epoch_shift,
        bid_o=bid_o,
        bid_h=bid_h,
        bid_l=bid_l,
        bid_c=bid_c,
        ask_o=ask_o,
        ask_h=ask_h,
        ask_l=ask_l,
        ask_c=ask_c,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
    )


def _series(count: int = 1_100, *, spread_bps: float = 0.50) -> list[pvsclc.QuoteBar]:
    return [_bar(index, spread_bps=spread_bps) for index in range(count)]


def _replace_spread(bar: pvsclc.QuoteBar, spread_bps: float) -> pvsclc.QuoteBar:
    return _bar(
        (bar.epoch - BASE_EPOCH) // 60,
        mid_o=bar.mid_o,
        mid_h=bar.mid_h,
        mid_l=bar.mid_l,
        mid_c=bar.mid_c,
        spread_bps=spread_bps,
        bid_volume=bar.bid_volume,
        ask_volume=bar.ask_volume,
    )


def _signal_bar(
    index: int,
    *,
    side: str,
    body_bps: float = 2.5,
    close_location: float = 0.90,
    spread_bps: float = 0.45,
    bid_volume: float = 150.0,
    ask_volume: float = 150.0,
) -> pvsclc.QuoteBar:
    direction = 1.0 if side == "BUY" else -1.0
    bid_o, ask_o = _quotes(100.0, spread_bps)
    factor = math.exp(direction * body_bps / 1e4)
    bid_c = bid_o * factor
    ask_c = ask_o * factor
    quote_range = 0.10
    if side == "BUY":
        bid_l = bid_c - close_location * quote_range
        ask_l = ask_c - close_location * quote_range
        bid_h = bid_l + quote_range
        ask_h = ask_l + quote_range
    elif side == "SELL":
        bid_l = bid_c - (1.0 - close_location) * quote_range
        ask_l = ask_c - (1.0 - close_location) * quote_range
        bid_h = bid_l + quote_range
        ask_h = ask_l + quote_range
    else:
        raise ValueError(side)
    bar = pvsclc.QuoteBar(
        epoch=BASE_EPOCH + index * 60,
        bid_o=bid_o,
        bid_h=bid_h,
        bid_l=bid_l,
        bid_c=bid_c,
        ask_o=ask_o,
        ask_h=ask_h,
        ask_l=ask_l,
        ask_c=ask_c,
        bid_volume=bid_volume,
        ask_volume=ask_volume,
    )
    assert pvsclc.validate_quote_bar(bar)
    return bar


def _plant_signal(
    bars: list[pvsclc.QuoteBar],
    index: int,
    *,
    side: str,
    target: bool = True,
    body_bps: float = 2.5,
    close_location: float = 0.90,
    signal_spread_bps: float = 0.45,
    entry_spread_bps: float = 0.45,
) -> None:
    bars[index] = _signal_bar(
        index,
        side=side,
        body_bps=body_bps,
        close_location=close_location,
        spread_bps=signal_spread_bps,
    )
    signal_mid = bars[index].mid_c
    for outcome_index in range(index + 1, min(len(bars), index + 31)):
        bars[outcome_index] = _bar(
            outcome_index,
            mid_o=signal_mid,
            mid_h=signal_mid + 0.002,
            mid_l=signal_mid - 0.002,
            mid_c=signal_mid,
            spread_bps=entry_spread_bps,
        )
    if target:
        direction = 1.0 if side == "BUY" else -1.0
        bars[index + 2] = _bar(
            index + 2,
            mid_o=signal_mid,
            mid_h=signal_mid + (0.12 if side == "BUY" else 0.002),
            mid_l=signal_mid - (0.12 if side == "SELL" else 0.002),
            mid_c=signal_mid + direction * 0.08,
            spread_bps=entry_spread_bps,
        )


def _evaluate(
    bars: list[pvsclc.QuoteBar],
    index: int,
    side: str,
    *,
    budget: float = 1.0,
) -> tuple[pvsclc.PVSCLCSignal | None, str]:
    return pvsclc.evaluate_signal(
        prepared=pvsclc.prepare_series(bars),
        signal_index=index,
        symbol="EURUSD",
        side=side,
        config=CONFIG,
        proxy_spread_budget_bps=budget,
    )


def _signal(
    bars: list[pvsclc.QuoteBar], index: int, side: str, *, budget: float = 1.0
) -> pvsclc.PVSCLCSignal:
    signal, reason = _evaluate(bars, index, side, budget=budget)
    assert signal is not None, reason
    return signal


def _budgets(value: float = 1.0) -> dict[str, float]:
    return {symbol: value for symbol in pvsclc.FX_SYMBOLS}


def _screen(
    bars_by_symbol: dict[str, list[pvsclc.QuoteBar]] | None = None,
    *,
    activity_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return pvsclc.screen_universe(
        bars_by_symbol=bars_by_symbol or {},
        proxy_spread_budgets_bps=_budgets(),
        proxy_provenance=PROXY_PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        activity_provenance=(
            _activity_provenance()
            if activity_provenance is None
            else activity_provenance
        ),
        source_sha256_by_symbol=SOURCE_HASHES,
        source_activity_integrity_by_symbol=_source_integrity(),
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        source_design_lock_utc=SOURCE_DESIGN_LOCK,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
    )


def _valid_activity(payload: dict[str, Any] | None) -> bool:
    return _valid_activity_with_integrity(payload, _source_integrity())


def _valid_activity_with_integrity(
    payload: dict[str, Any] | None,
    integrity: dict[str, dict[str, Any]],
) -> bool:
    return pvsclc._valid_activity_provenance(
        payload,
        source_sha256_by_symbol=SOURCE_HASHES,
        source_activity_integrity_by_symbol=integrity,
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=1_688_169_600,
        source_design_lock_epoch=BASE_EPOCH - 1,
        preregistration_lock_epoch=BASE_EPOCH,
    )


def test_csv_schema_and_component_volume_contract_are_exact(tmp_path: Path) -> None:
    assert pvsclc.CSV_HEADER == (
        "timestamp",
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "ask_open",
        "ask_high",
        "ask_low",
        "ask_close",
        "bid_volume",
        "ask_volume",
    )
    valid = _bar(0)
    assert pvsclc.validate_quote_bar(valid)
    for mutation in (
        {"bid_volume": -0.0 - 1.0},
        {"ask_volume": math.inf},
        {"bid_volume": 1e308, "ask_volume": 1e308},
    ):
        assert not pvsclc.validate_quote_bar(dataclasses.replace(valid, **mutation))

    path = tmp_path / "EURUSD_M1.csv"
    row = [
        "2023-01-01T00:00:00Z",
        *[str(value) for value in dataclasses.astuple(valid)[1:]],
    ]
    path.write_bytes(
        (",".join(pvsclc.CSV_HEADER) + "\n" + ",".join(row) + "\n").encode()
    )
    loaded = pvsclc.load_m1_csv(path)
    assert loaded == [valid]
    integrity = pvsclc._source_activity_integrity(path)
    bid_stream = (
        "timestamp,open,high,low,close,volume\n"
        + ",".join((row[0], row[1], row[2], row[3], row[4], row[9]))
        + "\n"
    ).encode()
    ask_stream = (
        "timestamp,open,high,low,close,volume\n"
        + ",".join((row[0], row[5], row[6], row[7], row[8], row[10]))
        + "\n"
    ).encode()
    assert integrity == {
        "rows": 1,
        "bid_stream_sha256": hashlib.sha256(bid_stream).hexdigest(),
        "ask_stream_sha256": hashlib.sha256(ask_stream).hexdigest(),
        "bid_zero_count": 0,
        "ask_zero_count": 0,
        "first_timestamp": "2023-01-01T00:00:00Z",
        "last_timestamp": "2023-01-01T00:00:00Z",
        "timestamp_set_sha256": hashlib.sha256(
            b"timestamp\n2023-01-01T00:00:00Z\n"
        ).hexdigest(),
        "gap_count": 0,
        "maximum_gap_seconds": 0,
    }
    path.write_text(
        ",".join((*pvsclc.CSV_HEADER[:-2], "volume")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected M1 header"):
        pvsclc.load_m1_csv(path)


def test_cli_byte_loader_rejects_visible_rows_outside_half_open_window() -> None:
    bar = _bar(0)
    row = ["2022-12-31T23:59:00Z", *map(str, dataclasses.astuple(bar)[1:])]
    raw = (",".join(pvsclc.CSV_HEADER) + "\n" + ",".join(row) + "\n").encode()
    assert (
        pvsclc._load_m1_csv_bytes(
            raw,
            source_name="EURUSD_M1.csv",
            start=EVALUATION_START,
            end=EVALUATION_END,
        )
        == []
    )
    with pytest.raises(ValueError, match="row before evaluation window"):
        pvsclc._load_m1_csv_bytes(
            raw,
            source_name="EURUSD_M1.csv",
            start=EVALUATION_START,
            end=EVALUATION_END,
            reject_outside_window=True,
        )


@pytest.mark.parametrize(
    ("values", "probability", "expected"),
    [
        ([0.0, 10.0], 0.25, 2.5),
        ([0.0, 10.0], 0.90, 9.0),
        (list(range(240)), 0.90, 215.1),
        (list(range(240)), 0.25, 59.75),
    ],
)
def test_type7_quantile_exact_interpolation(
    values: list[float], probability: float, expected: float
) -> None:
    assert pvsclc._type7_quantile(values, probability) == pytest.approx(expected)


def test_baseline_is_exactly_t_minus_240_through_t_minus_1_and_type7() -> None:
    bars = _series(500)
    for offset, index in enumerate(range(60, 300)):
        activity = float(offset)
        bars[index] = dataclasses.replace(
            _replace_spread(bars[index], (offset + 1) / 100.0),
            bid_volume=activity / 2.0,
            ask_volume=activity / 2.0,
        )
    prepared = pvsclc.prepare_series(bars)
    context = pvsclc.baseline_context_at(prepared, signal_index=300)
    assert context == pvsclc.BaselineContext(
        activity_v90=pytest.approx(215.1),
        spread_q25_bps=pytest.approx(0.6075),
    )

    outside = list(bars)
    outside[59] = dataclasses.replace(outside[59], bid_volume=1e9, ask_volume=1e9)
    outside[300] = dataclasses.replace(outside[300], bid_volume=1e9, ask_volume=1e9)
    assert (
        pvsclc.baseline_context_at(pvsclc.prepare_series(outside), signal_index=300)
        == context
    )
    inside = list(bars)
    inside[275] = dataclasses.replace(inside[275], bid_volume=1e9, ask_volume=1e9)
    assert (
        pvsclc.baseline_context_at(pvsclc.prepare_series(inside), signal_index=300)
        != context
    )


def test_rolling_type7_context_matches_naive_randomized_gapped_recomputation() -> None:
    compared = 0
    for seed in range(8):
        generator = random.Random(seed + 91_337)
        gap_index = 360 + seed
        bars = [
            _bar(
                index,
                spread_bps=generator.uniform(0.05, 2.5),
                bid_volume=generator.uniform(0.0, 100.0),
                ask_volume=generator.uniform(0.0, 100.0),
                epoch_shift=60 if index >= gap_index else 0,
            )
            for index in range(700)
        ]
        prepared = pvsclc.prepare_series(bars)
        for signal_index in range(len(bars)):
            actual = pvsclc.baseline_context_at(prepared, signal_index=signal_index)
            start = signal_index - pvsclc.BASELINE_M1_BARS
            if start < 0 or any(
                bars[index].epoch != bars[index - 1].epoch + 60
                for index in range(start + 1, signal_index + 1)
            ):
                assert actual is None
                continue
            activities = sorted(bar.activity for bar in bars[start:signal_index])
            spreads = sorted(bar.spread_close_bps for bar in bars[start:signal_index])
            assert actual == pvsclc.BaselineContext(
                pvsclc._type7_quantile(activities, 0.90),
                pvsclc._type7_quantile(spreads, 0.25),
            )
            compared += 1
    assert compared > 1_700


def test_activity_v90_must_be_positive_and_signal_activity_strictly_above() -> None:
    zero = [
        dataclasses.replace(bar, bid_volume=0.0, ask_volume=0.0) for bar in _series(500)
    ]
    zero[300] = _signal_bar(300, side="BUY")
    closed, reason = pvsclc.evaluate_closed_signal(
        prepared=pvsclc.prepare_series(zero),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert closed is None and reason == "activity_v90_not_positive"

    equal = _series(500)
    equal[300] = _signal_bar(300, side="BUY", bid_volume=50.0, ask_volume=50.0)
    signal, reason = _evaluate(equal, 300, "BUY")
    assert signal is None and reason == "activity_not_strictly_above_v90"
    admitted = list(equal)
    admitted[300] = dataclasses.replace(equal[300], ask_volume=50.0000000001)
    assert _signal(admitted, 300, "BUY").signal_activity > 100.0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_buy_and_exact_mirror_sell_require_both_quote_bodies_and_locations(
    side: str,
) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side)
    signal = _signal(bars, 300, side)
    direction = 1.0 if side == "BUY" else -1.0
    assert direction * signal.bid_log_body_bps >= signal.recorded_cost_bps
    assert direction * signal.ask_log_body_bps >= signal.recorded_cost_bps
    assert signal.bid_close_location >= 0.80
    assert signal.ask_close_location >= 0.80
    assert signal.activity_metric_id == pvsclc.ACTIVITY_METRIC_ID
    assert signal.signal_activity == signal.signal_bid_volume + signal.signal_ask_volume
    assert signal.signal_activity_ratio == signal.signal_activity / signal.activity_v90


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("quote", ["bid", "ask"])
def test_each_quote_body_is_independently_required(side: str, quote: str) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side)
    signal_bar = bars[300]
    field = f"{quote}_o"
    close_value = getattr(signal_bar, f"{quote}_c")
    direction = 1.0 if side == "BUY" else -1.0
    below = close_value / math.exp(direction * 1.99 / 1e4)
    updates = {field: below}
    if quote == "bid" and below > signal_bar.ask_o:
        updates["ask_o"] = math.nextafter(below, math.inf)
    if quote == "ask" and below < signal_bar.bid_o:
        updates["bid_o"] = math.nextafter(below, -math.inf)
    bars[300] = dataclasses.replace(signal_bar, **updates)
    signal, reason = _evaluate(bars, 300, side)
    assert signal is None and reason == "bid_or_ask_log_body_below_1c"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("quote", ["bid", "ask"])
def test_each_quote_close_location_rejects_one_float_below_boundary(
    side: str, quote: str
) -> None:
    bars = _series(500)
    bars[300] = _signal_bar(300, side=side, close_location=0.80)
    bar = bars[300]
    high = getattr(bar, f"{quote}_h")
    low = getattr(bar, f"{quote}_l")
    close = getattr(bar, f"{quote}_c")
    location = (
        (close - low) / (high - low) if side == "BUY" else (high - close) / (high - low)
    )
    if location < 0.80:
        close = math.nextafter(close, math.inf if side == "BUY" else -math.inf)
        bars[300] = dataclasses.replace(bar, **{f"{quote}_c": close})
    signal, reason = _evaluate(bars, 300, side)
    assert signal is not None, reason
    bar = bars[300]
    bars[300] = dataclasses.replace(
        bar,
        **{
            f"{quote}_c": math.nextafter(
                getattr(bar, f"{quote}_c"),
                -math.inf if side == "BUY" else math.inf,
            )
        },
    )
    signal, reason = _evaluate(bars, 300, side)
    assert signal is None
    assert reason in {
        "bid_or_ask_close_location_below_0_80",
        "bid_or_ask_log_body_below_1c",
    }


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_zero_quote_range_rejects_signal_but_malformed_ohlc_is_readiness_fatal(
    side: str,
) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side)
    bar = bars[300]
    bid_flat = bar.bid_c
    ask_flat = bar.ask_c
    bars[300] = dataclasses.replace(
        bar,
        bid_o=bid_flat,
        bid_h=bid_flat,
        bid_l=bid_flat,
        bid_c=bid_flat,
        ask_o=ask_flat,
        ask_h=ask_flat,
        ask_l=ask_flat,
        ask_c=ask_flat,
    )
    assert pvsclc.validate_quote_bar(bars[300])
    signal, reason = _evaluate(bars, 300, side)
    assert signal is None and reason == "zero_quote_range"

    malformed = list(bars)
    malformed[300] = dataclasses.replace(bar, bid_h=bar.bid_l - 1.0)
    with pytest.raises(ValueError, match="invalid quote bar"):
        pvsclc.prepare_series(malformed)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_signal_and_entry_spread_caps_admit_inside_and_reject_outside(
    side: str,
) -> None:
    bars = _series(500, spread_bps=0.50)
    _plant_signal(
        bars,
        300,
        side=side,
        signal_spread_bps=0.45,
        entry_spread_bps=0.45,
    )
    signal = _signal(bars, 300, side)
    assert signal.spread_cap_bps == pytest.approx(0.50)
    assert signal.signal_spread_bps <= signal.spread_cap_bps
    assert signal.entry_spread_bps <= signal.spread_cap_bps

    signal_too_wide = list(bars)
    signal_too_wide[300] = _replace_spread(signal_too_wide[300], 0.51)
    rejected, reason = _evaluate(signal_too_wide, 300, side)
    assert rejected is None and reason == "signal_spread_above_q25_or_proxy"

    entry_too_wide = list(bars)
    entry_too_wide[301] = _replace_spread(entry_too_wide[301], 0.51)
    rejected, reason = _evaluate(entry_too_wide, 300, side)
    assert rejected is None and reason == "entry_spread_above_q25_or_proxy"


def test_closed_signal_does_not_read_t_plus_1_or_future_outcomes() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", target=False)
    prepared = pvsclc.prepare_series(bars)
    closed, reason = pvsclc.evaluate_closed_signal(
        prepared=prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert closed is not None, reason
    changed = list(bars)
    for index in range(301, len(changed)):
        changed[index] = _bar(
            index,
            mid_o=130.0,
            mid_h=140.0,
            mid_l=120.0,
            mid_c=135.0,
            spread_bps=3.0,
            bid_volume=1e6,
            ask_volume=1e6,
        )
    after, reason = pvsclc.evaluate_closed_signal(
        prepared=pvsclc.prepare_series(changed),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert after == closed, reason


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exact_t_plus_1_executable_quote_and_cost_algebra(side: str) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    signal = _signal(bars, 300, side)
    assert signal.entry_index == 301
    assert signal.entry_epoch == signal.signal_epoch + 60
    assert signal.entry_price == (bars[301].ask_o if side == "BUY" else bars[301].bid_o)
    assert signal.spread_stress_bps == signal.proxy_budget_bps == 1.0
    assert signal.recorded_cost_bps == 2.0
    assert signal.execution_cost_debit_bps == pytest.approx(1.55)
    assert signal.gross_target_bps == 8.0
    assert signal.gross_stop_bps == 16.0
    assert signal.p_star == (
        signal.gross_stop_bps + signal.execution_cost_debit_bps
    ) / (signal.gross_stop_bps + signal.gross_target_bps)
    assert signal.p_star <= pvsclc.MAX_P_STAR


def test_zero_entry_spread_hits_pstar_boundary_and_proxy_three_hits_32bp_risk() -> None:
    boundary = _series(500)
    _plant_signal(
        boundary,
        300,
        side="BUY",
        target=False,
        entry_spread_bps=0.0,
    )
    signal = _signal(boundary, 300, "BUY")
    assert signal.execution_cost_debit_bps == signal.recorded_cost_bps == 2.0
    assert signal.p_star == pvsclc.MAX_P_STAR == 0.75

    maximum = _series(500)
    _plant_signal(
        maximum,
        300,
        side="BUY",
        target=False,
        body_bps=4.5,
        signal_spread_bps=0.45,
        entry_spread_bps=0.45,
    )
    maximum_signal = _signal(maximum, 300, "BUY", budget=3.0)
    assert maximum_signal.recorded_cost_bps == 4.0
    assert maximum_signal.gross_stop_bps == pvsclc.MAX_GROSS_STOP_BPS == 32.0


def test_gap_resets_baseline_and_exact_fill_gap_reserves_adversely() -> None:
    bars = _series(700)
    gapped = list(bars)
    for index in range(300, len(gapped)):
        gapped[index] = dataclasses.replace(
            gapped[index], epoch=gapped[index].epoch + 60
        )
    prepared = pvsclc.prepare_series(gapped)
    assert pvsclc.baseline_context_at(prepared, signal_index=539) is None
    assert pvsclc.baseline_context_at(prepared, signal_index=540) is not None

    fill_gap = _series(500)
    _plant_signal(fill_gap, 300, side="BUY", target=False)
    for index in range(301, len(fill_gap)):
        fill_gap[index] = dataclasses.replace(
            fill_gap[index], epoch=fill_gap[index].epoch + 60
        )
    cell = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(fill_gap),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    assert reservation["outcome_reason"] == "exact_next_open_gap"
    assert reservation["reservation_status"] == "unresolved"
    assert reservation["gate_pnl_r"] == -9.0 / 8.0


def test_half_open_cutoff_does_not_manufacture_missing_fill_at_end() -> None:
    bars = _series(241)
    _plant_signal(bars, 240, side="BUY", target=False)
    evaluation_end = bars[240].epoch + 60
    cell = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
        evaluation_end_epoch=evaluation_end,
    )
    assert cell["closed_signal_events"] == 1
    assert cell["entry_day_reservations"] == 0
    assert cell["reasons"]["entry_outside_evaluation_window"] == 1
    assert not pvsclc._row_is_within_evaluation_window(
        {
            "signal_epoch": bars[240].epoch,
            "entry_epoch": evaluation_end,
            "exit_epoch": None,
        },
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=evaluation_end,
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_entry_bar_counts_and_ambiguous_touch_is_stop_first(side: str) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    signal = _signal(bars, 300, side)
    entry = bars[signal.entry_index]
    if side == "BUY":
        bars[signal.entry_index] = dataclasses.replace(
            entry,
            bid_h=signal.target_price * 1.001,
            bid_l=signal.stop_price * 0.999,
        )
    else:
        bars[signal.entry_index] = dataclasses.replace(
            entry,
            ask_h=signal.stop_price * 1.001,
            ask_l=signal.target_price * 0.999,
        )
    trade = pvsclc.simulate_trade(bars, signal=signal)
    assert trade is not None
    assert trade.exit_reason == "sl_double_touch"
    assert trade.bars_held == 1


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_adverse_gap_uses_observed_open_and_favorable_gap_is_target_capped(
    side: str,
) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    signal = _signal(bars, 300, side)
    index = signal.entry_index + 1
    adverse = list(bars)
    favorable = list(bars)
    if side == "BUY":
        adverse[index] = _bar(
            index,
            mid_o=signal.stop_price * 0.998,
            mid_h=signal.stop_price,
            mid_l=signal.stop_price * 0.997,
            mid_c=signal.stop_price * 0.998,
        )
        favorable[index] = _bar(
            index,
            mid_o=signal.target_price * 1.002,
            mid_h=signal.target_price * 1.003,
            mid_l=signal.target_price,
            mid_c=signal.target_price * 1.002,
        )
    else:
        adverse[index] = _bar(
            index,
            mid_o=signal.stop_price * 1.002,
            mid_h=signal.stop_price * 1.003,
            mid_l=signal.stop_price,
            mid_c=signal.stop_price * 1.002,
        )
        favorable[index] = _bar(
            index,
            mid_o=signal.target_price * 0.998,
            mid_h=signal.target_price,
            mid_l=signal.target_price * 0.997,
            mid_c=signal.target_price * 0.998,
        )
    adverse_trade = pvsclc.simulate_trade(adverse, signal=signal)
    favorable_trade = pvsclc.simulate_trade(favorable, signal=signal)
    assert adverse_trade is not None and adverse_trade.exit_reason == "sl_gap_open"
    assert adverse_trade.exit_price != signal.stop_price
    assert favorable_trade is not None and favorable_trade.exit_reason == "tp_gap_open"
    assert favorable_trade.exit_price == signal.target_price


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_only_exact_target_exit_is_full_target_win_and_timeout_is_not(
    side: str,
) -> None:
    target_bars = _series(500)
    _plant_signal(target_bars, 300, side=side, target=True)
    target_signal = _signal(target_bars, 300, side)
    target_trade = pvsclc.simulate_trade(target_bars, signal=target_signal)
    assert target_trade is not None
    assert target_trade.exit_reason == "tp"
    assert target_trade.full_target_win is True

    timeout_bars = _series(500)
    _plant_signal(timeout_bars, 300, side=side, target=False)
    timeout_signal = _signal(timeout_bars, 300, side)
    timeout_trade = pvsclc.simulate_trade(timeout_bars, signal=timeout_signal)
    assert timeout_trade is not None
    assert timeout_trade.exit_reason == "time_stop"
    assert timeout_trade.full_target_win is False
    assert timeout_trade.bars_held == pvsclc.OUTCOME_HORIZON_M1_BARS


def test_known_entry_spread_rejection_does_not_reserve_or_block_later_signal() -> None:
    bars = _series(1_100)
    _plant_signal(bars, 300, side="BUY", entry_spread_bps=0.80)
    _plant_signal(bars, 900, side="BUY")
    cell = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["reservation_ledger"][0]["signal_index"] == 900


def test_first_admitted_signal_per_cell_side_and_utc_entry_day_is_reserved() -> None:
    bars = _series(1_100)
    _plant_signal(bars, 300, side="BUY")
    _plant_signal(bars, 900, side="BUY")
    cell = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["reservation_ledger"][0]["signal_index"] == 300
    assert cell["reasons"]["entry_day_already_reserved"] >= 1


def test_incomplete_horizon_reserves_first_signal_and_blocks_same_day_substitute() -> (
    None
):
    bars = _series(340)
    _plant_signal(bars, 320, side="BUY", target=False)
    cell = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    row = cell["reservation_ledger"][0]
    assert row["reservation_status"] == "unresolved"
    assert row["outcome_reason"] == "incomplete_outcome_horizon"
    assert (
        row["gate_pnl_r"]
        == -(row["gross_stop_bps"] + row["execution_cost_debit_bps"])
        / row["gross_stop_bps"]
    )


def test_trial_accounting_threshold_config_and_authority_are_frozen() -> None:
    assert CONFIG.config_id == "pvsclc_v1_vq90_cl80_b1c_h30_t4c_s8c"
    assert pvsclc.trial_accounting() == {
        "grid_configurations": 1,
        "directions": 2,
        "symbols": 18,
        "current_attempted_cells": 36,
        "prior_attempted_cells": 4_422,
        "cumulative_attempted_cells": 4_458,
        "expected_full_universe_cells": 36,
    }
    assert (
        pvsclc.TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        == 4.627867074083506
    )
    source = inspect.getsource(pvsclc)
    for forbidden in (
        "RuntimeService",
        "requests.post",
        'order_authorized": True',
        'activation_authorized": True',
        'holdout_access_authorized": True',
    ):
        assert forbidden not in source


def test_wilson_and_profit_factor_use_conservative_full_reservation_family() -> None:
    assert pvsclc.SIMULTANEOUS_WILSON_FAMILY_CELLS == 36
    assert pvsclc._one_sided_wilson_lower_bound(100, 100) > 0.90
    assert pvsclc._one_sided_wilson_lower_bound(
        99, 100
    ) < pvsclc._one_sided_wilson_lower_bound(100, 100)
    passing = pvsclc._profit_factor_diagnostics(
        [
            {"gate_pnl_r": 2.0, "reservation_status": "scored"},
            {"gate_pnl_r": -1.0, "reservation_status": "unresolved"},
        ]
    )
    assert passing["profit_factor"] == 2.0
    assert passing["gate_profit_factor"] is True
    failing = pvsclc._profit_factor_diagnostics(
        [
            {"gate_pnl_r": 1.0, "reservation_status": "scored"},
            {"gate_pnl_r": -1.0, "reservation_status": "unresolved"},
        ]
    )
    assert failing["profit_factor"] == 1.0
    assert failing["gate_profit_factor"] is False


def test_temporal_thirds_and_calendar_months_include_unresolved_and_veto() -> None:
    thirds_rows: list[dict[str, Any]] = []
    boundaries = pvsclc._temporal_thirds_boundary_epochs(
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=1_688_169_600,
    )
    for segment in range(3):
        for offset in range(30):
            thirds_rows.append(
                {
                    "entry_epoch": boundaries[segment] + offset * 60,
                    "gate_pnl_r": 1.0 if offset < 27 else -1.0,
                    "full_target_win": offset < 27,
                }
            )
    diagnostics = pvsclc._temporal_thirds_diagnostics(
        thirds_rows,
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=1_688_169_600,
    )
    assert diagnostics["temporal_thirds_reservation_counts"] == [30, 30, 30]
    assert diagnostics["gate_temporal_thirds_stable"] is True
    thirds_rows[0]["full_target_win"] = False
    assert (
        pvsclc._temporal_thirds_diagnostics(
            thirds_rows,
            evaluation_start_epoch=BASE_EPOCH,
            evaluation_end_epoch=1_688_169_600,
        )["gate_temporal_thirds_stable"]
        is False
    )

    month_rows: list[dict[str, Any]] = []
    for month in range(6):
        for offset in range(12):
            month_rows.append(
                {
                    "entry_epoch": pvsclc.CALENDAR_MONTH_BOUNDARY_EPOCHS[month]
                    + offset * 60,
                    "gate_pnl_r": 1.0 if offset < 11 else -1.0,
                    "full_target_win": offset < 11,
                }
            )
    months = pvsclc._calendar_month_diagnostics(
        month_rows,
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=1_688_169_600,
    )
    assert months["calendar_month_reservation_counts"] == [12] * 6
    assert months["gate_calendar_months_stable"] is True


def test_global_gate_requires_one_unchanged_config_for_all_36_cells() -> None:
    cells = [
        {
            "config_id": CONFIG.config_id,
            "symbol": symbol,
            "side": side,
            "passes_discovery_cell_gate": True,
        }
        for symbol in pvsclc.FX_SYMBOLS
        for side in ("BUY", "SELL")
    ]
    assert pvsclc._passing_global_configurations(cells) == [CONFIG.config_id]
    cells[0]["passes_discovery_cell_gate"] = False
    assert pvsclc._passing_global_configurations(cells) == []


def test_exact_reservation_schema_and_activity_semantics_reject_forgery() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")
    cell = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    row = cell["reservation_ledger"][0]
    assert set(row) == pvsclc.PVSCLC_RESERVATION_FIELD_NAMES
    assert pvsclc.validate_reservation_row(row)
    exact_body_boundary = dict(row)
    exact_body_boundary["bid_log_body_bps"] = 2.0
    exact_body_boundary["ask_log_body_bps"] = 2.0
    assert pvsclc.validate_reservation_row(exact_body_boundary)
    one_ulp_below_body = dict(exact_body_boundary)
    one_ulp_below_body["bid_log_body_bps"] = math.nextafter(2.0, -math.inf)
    assert not pvsclc.validate_reservation_row(one_ulp_below_body)
    mutations = (
        {"activity_metric_id": "wrong"},
        {"activity_v90": 0.0},
        {"signal_bid_volume": -1.0},
        {"signal_activity": math.nextafter(row["signal_activity"], math.inf)},
        {
            "signal_activity_ratio": math.nextafter(
                row["signal_activity_ratio"], math.inf
            )
        },
        {"spread_cap_bps": math.nextafter(row["spread_cap_bps"], math.inf)},
        {"bid_log_body_bps": 1.99},
        {"ask_log_body_bps": 1.99},
        {"bid_close_location": math.nextafter(0.80, -math.inf)},
        {"ask_close_location": math.nextafter(0.80, -math.inf)},
        {"signal_spread_bps": math.nextafter(row["spread_cap_bps"], math.inf)},
    )
    for mutation in mutations:
        forged = dict(row)
        forged.update(mutation)
        assert not pvsclc.validate_reservation_row(forged), mutation
    extra = dict(row)
    extra["unexpected"] = 1
    assert not pvsclc.validate_reservation_row(extra)


def test_economic_semantics_reject_one_ulp_optimism_and_bracket_drift() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")
    row = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )["reservation_ledger"][0]
    assert pvsclc.validate_reservation_row(row)
    for field, toward in (
        ("spread_stress_bps", -math.inf),
        ("recorded_cost_bps", -math.inf),
        ("execution_cost_debit_bps", -math.inf),
        ("gross_target_bps", math.inf),
        ("gross_stop_bps", -math.inf),
        ("p_star", -math.inf),
        ("gate_pnl_r", math.inf),
    ):
        forged = dict(row)
        forged[field] = math.nextafter(float(row[field]), toward)
        assert not pvsclc.validate_reservation_row(forged), field
    forged = dict(row)
    forged["target_price"] = math.nextafter(row["target_price"], math.inf)
    forged["exit_price"] = forged["target_price"]
    assert not pvsclc.validate_reservation_row(forged)


def test_missing_fill_schema_cannot_be_forged_into_observed_win() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", target=False)
    for index in range(301, len(bars)):
        bars[index] = dataclasses.replace(bars[index], epoch=bars[index].epoch + 60)
    row = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )["reservation_ledger"][0]
    assert set(row) == pvsclc.PVSCLC_MISSING_FILL_RESERVATION_FIELD_NAMES
    assert pvsclc.validate_reservation_row(row)
    for mutation in (
        {"entry_price": 100.0},
        {"reservation_status": "scored"},
        {"full_target_win": True},
        {"gate_pnl_r": math.nextafter(row["gate_pnl_r"], math.inf)},
        {"gate_risk_basis_bps": math.nextafter(row["gross_stop_bps"], -math.inf)},
    ):
        forged = dict(row)
        forged.update(mutation)
        assert not pvsclc.validate_reservation_row(forged), mutation


def test_reservation_and_trade_payloads_reject_duplicates_and_wrong_family() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")
    cell = pvsclc.screen_cell(
        prepared=pvsclc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    reservations = {
        "schema_version": "fxstack.scalp.provider_volume_shock_close_location_continuation_reservation_ledger.v1",
        "family": "provider_volume_shock_close_location_continuation",
        "reservations": cell["reservation_ledger"],
    }
    trades = {
        "schema_version": "fxstack.scalp.provider_volume_shock_close_location_continuation_trade_ledger.v1",
        "family": "provider_volume_shock_close_location_continuation",
        "trades": cell["trade_ledger"],
    }
    assert pvsclc.validate_reservation_ledger_payload(reservations)
    assert pvsclc.validate_trade_ledger_payload(trades)
    duplicate = copy.deepcopy(reservations)
    duplicate["reservations"].append(copy.deepcopy(duplicate["reservations"][0]))
    assert not pvsclc.validate_reservation_ledger_payload(duplicate)
    wrong_family = copy.deepcopy(trades)
    wrong_family["family"] = "other"
    assert not pvsclc.validate_trade_ledger_payload(wrong_family)


def test_strict_payload_bundle_and_complete_source_replay() -> None:
    bars = _series(300)
    bars_by_symbol = {symbol: bars for symbol in pvsclc.FX_SYMBOLS}
    result = _screen(bars_by_symbol)
    assert len(result["cells"]) == 36
    assert result["reservation_ledger"] == []
    assert result["trade_ledger"] == []
    assert pvsclc.validate_result_bundle(result)
    assert pvsclc.validate_source_replay(
        result=result,
        bars_by_symbol=bars_by_symbol,
        proxy_spread_budgets_bps=_budgets(),
        proxy_provenance=PROXY_PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        activity_provenance=_activity_provenance(),
        source_sha256_by_symbol=SOURCE_HASHES,
        source_activity_integrity_by_symbol=_source_integrity(),
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        source_design_lock_utc=SOURCE_DESIGN_LOCK,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
    )
    for authority in (
        "success_claim_authorized",
        "holdout_access_authorized",
        "promotion_authorized",
        "activation_authorized",
        "registry_write_authorized",
        "order_authorized",
    ):
        forged = copy.deepcopy(result)
        forged[authority] = True
        assert not pvsclc.validate_result_bundle(forged), authority
    omitted = copy.deepcopy(result)
    omitted["cells"].pop()
    assert not pvsclc.validate_result_bundle(omitted)


def test_strict_json_rejects_duplicates_and_nonfinite_numbers() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        pvsclc.loads_strict_json('{"x":1,"x":2}')
    for token in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="non-finite"):
            pvsclc.loads_strict_json(f'{{"x":{token}}}')
    with pytest.raises(ValueError):
        pvsclc.dumps_strict_json({"x": math.nan})


@pytest.mark.parametrize(
    "timestamp",
    [
        "2023-01-01T00:00:00Z",
        "2023-01-01T00:00:00+00:00",
        "2023-01-01T00:00:00.000000Z",
    ],
)
def test_parse_epoch_accepts_canonical_whole_second_utc(timestamp: str) -> None:
    assert pvsclc._parse_epoch(timestamp) == BASE_EPOCH


@pytest.mark.parametrize(
    "timestamp",
    [
        "2023-01-01T00:00:00.1Z",
        "2023-01-01T00:00:00.0000001Z",
        "2023-01-01T01:00:00+01:00",
        "2023-01-01T00:00:00",
    ],
)
def test_parse_epoch_rejects_fractional_or_noncanonical_utc(timestamp: str) -> None:
    with pytest.raises(ValueError):
        pvsclc._parse_epoch(timestamp)


def test_activity_provenance_exact_contract_and_manifest_bytes() -> None:
    payload = _activity_provenance()
    assert _valid_activity(payload)
    manifest = pvsclc._canonical_input_manifest_bytes(SOURCE_HASHES)
    assert manifest is not None
    lines = manifest.decode("ascii").splitlines(keepends=True)
    assert len(lines) == 18
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1])
    assert all(line.endswith("\n") and "  input/" in line for line in lines)
    assert payload["input_manifest_sha256"] == hashlib.sha256(manifest).hexdigest()


def test_activity_provenance_two_lock_chronology_is_inclusive_and_canonical() -> None:
    lower_boundary = _activity_provenance()
    lower_boundary["as_of_utc"] = SOURCE_DESIGN_LOCK
    assert _valid_activity(lower_boundary)
    assert _valid_activity(_activity_provenance())

    before_source_lock = _activity_provenance()
    before_source_lock["acquisition_started_at_utc"] = "2022-12-31T23:59:58Z"
    assert not _valid_activity(before_source_lock)
    start_after_completion = _activity_provenance()
    start_after_completion["acquisition_started_at_utc"] = "2023-01-01T00:00:01Z"
    assert not _valid_activity(start_after_completion)
    after_final_lock = _activity_provenance()
    after_final_lock["as_of_utc"] = "2023-01-01T00:00:01Z"
    assert not _valid_activity(after_final_lock)
    noncanonical = _activity_provenance()
    noncanonical["as_of_utc"] = "2023-01-01T00:00:00.000000Z"
    assert not _valid_activity(noncanonical)
    assert not pvsclc._valid_activity_provenance(
        _activity_provenance(),
        source_sha256_by_symbol=SOURCE_HASHES,
        source_activity_integrity_by_symbol=_source_integrity(),
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=1_688_169_600,
        source_design_lock_epoch=BASE_EPOCH + 1,
        preregistration_lock_epoch=BASE_EPOCH,
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"bid_stream_sha256": "0" * 64},
        {"ask_stream_sha256": "0" * 64},
        {"rows": 499},
        {"bid_zero_count": 1},
        {"ask_zero_count": 1},
        {"first_timestamp": "2023-01-01T00:01:00Z"},
        {"last_timestamp": "2023-06-30T23:58:00Z"},
        {"timestamp_set_sha256": "0" * 64},
        {"gap_count": 25},
        {"maximum_gap_seconds": 259_140},
    ],
)
def test_activity_provenance_rejects_forged_actual_source_integrity(
    mutation: dict[str, Any],
) -> None:
    integrity = _source_integrity()
    integrity["EURUSD"].update(mutation)
    assert not pvsclc._valid_activity_provenance(
        _activity_provenance(),
        source_sha256_by_symbol=SOURCE_HASHES,
        source_activity_integrity_by_symbol=integrity,
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=1_688_169_600,
        source_design_lock_epoch=BASE_EPOCH - 1,
        preregistration_lock_epoch=BASE_EPOCH,
    )


@pytest.mark.parametrize(
    "raw_mutation",
    [
        lambda raw: raw.replace(b"\n", b"\r\n"),
        lambda raw: raw.replace(b"100.0", b'"100.0"', 1),
        lambda raw: raw.rstrip(b"\n"),
    ],
)
def test_source_stream_recomputation_rejects_noncanonical_bytes(
    tmp_path: Path, raw_mutation: Any
) -> None:
    bar = _bar(0)
    row = ["2023-01-01T00:00:00Z", *map(str, dataclasses.astuple(bar)[1:])]
    canonical = (",".join(pvsclc.CSV_HEADER) + "\n" + ",".join(row) + "\n").encode()
    path = tmp_path / "EURUSD_M1.csv"
    path.write_bytes(raw_mutation(canonical))
    with pytest.raises(ValueError, match="noncanonical CSV byte grammar"):
        pvsclc._source_activity_integrity(path)


def test_source_integrity_recomputes_timestamp_identity_and_gap_metadata() -> None:
    bar = _bar(0)
    values = list(map(str, dataclasses.astuple(bar)[1:]))
    timestamps = (
        "2023-01-01T00:00:00Z",
        "2023-01-01T00:01:00Z",
        "2023-01-01T00:03:00Z",
    )
    raw = (
        ",".join(pvsclc.CSV_HEADER)
        + "\n"
        + "".join(f"{timestamp},{','.join(values)}\n" for timestamp in timestamps)
    ).encode()
    integrity = pvsclc._source_activity_integrity_bytes(
        raw, source_name="EURUSD_M1.csv"
    )
    assert integrity["first_timestamp"] == timestamps[0]
    assert integrity["last_timestamp"] == timestamps[-1]
    assert (
        integrity["timestamp_set_sha256"]
        == hashlib.sha256(
            ("timestamp\n" + "\n".join(timestamps) + "\n").encode("ascii")
        ).hexdigest()
    )
    assert integrity["gap_count"] == 1
    assert integrity["maximum_gap_seconds"] == 120

    nonminute_gap = raw.replace(b"2023-01-01T00:03:00Z", b"2023-01-01T00:02:30Z")
    with pytest.raises(ValueError, match="invalid timestamp sequence"):
        pvsclc._source_activity_integrity_bytes(
            nonminute_gap, source_name="EURUSD_M1.csv"
        )
    noncanonical = raw.replace(b"2023-01-01T00:03:00Z", b"2023-01-01T00:03:00+00:00")
    with pytest.raises(ValueError, match="noncanonical timestamp"):
        pvsclc._source_activity_integrity_bytes(
            noncanonical, source_name="EURUSD_M1.csv"
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p.update(activity_metric_id="wrong"),
        lambda p: p.update(provider="Dukascopy"),
        lambda p: p.update(as_of_utc="2023-01-01T00:00:01Z"),
        lambda p: p.update(acquisition_started_at_utc="2022-12-31T23:59:58Z"),
        lambda p: p["request_contract"].update(endpoint="https://example.test/"),
        lambda p: p["request_contract"].update(separate_requests=False),
        lambda p: p["request_contract"].update(resume=True),
        lambda p: p["request_contract"].update(max_retries=6),
        lambda p: p["request_contract"].update(limit=5001),
        lambda p: p["request_contract"].update(
            provider_end_inclusive_adjustment_minutes=0
        ),
        lambda p: p["request_contract"].update(out_of_window_rows_policy="crop"),
        lambda p: p["merge_contract"].update(unmatched_bid_rows=1),
        lambda p: p["fill_contract"].update(mid_only_fallback=True),
        lambda p: p["fill_contract"].update(zero_fill=True),
        lambda p: p["fill_contract"].update(synthetic_side=True),
        lambda p: p["coverage_contract"].update(rows_preserved_without_filtering=False),
        lambda p: p["coverage_contract"].update(minimum_rows_per_symbol=174_999),
        lambda p: p["coverage_contract"].update(maximum_boundary_lag_seconds=1),
        lambda p: p["coverage_contract"].update(maximum_gap_seconds=60),
        lambda p: p["coverage_contract"].update(timestamp_set_hash_grammar="wrong"),
        lambda p: p["tool_contract"].update(tool_sha256="0" * 63),
        lambda p: p["tool_contract"].update(
            dukascopy_python_instruments_module_sha256="0" * 63
        ),
        lambda p: p["tool_contract"].update(tool_path="../fetch.py"),
        lambda p: p.update(input_manifest_sha256="0" * 64),
        lambda p: p["symbols"]["EURUSD"].update(output_sha256="0" * 64),
        lambda p: p["symbols"]["EURUSD"].update(instrument_id="USD/EUR"),
        lambda p: p["symbols"]["EURUSD"].update(bid_stream_sha256="0" * 64),
        lambda p: p["symbols"]["EURUSD"].update(ask_stream_sha256="0" * 64),
        lambda p: p["symbols"]["EURUSD"].update(first_timestamp="2023-01-08T00:00:00Z"),
        lambda p: p["symbols"]["EURUSD"].update(last_timestamp="2023-07-01T00:00:00Z"),
        lambda p: p["symbols"]["EURUSD"].update(timestamp_set_sha256="0" * 64),
        lambda p: p["symbols"]["EURUSD"].update(gap_count=27),
        lambda p: p["symbols"]["EURUSD"].update(maximum_gap_seconds=345_660),
        lambda p: p["symbols"]["EURUSD"].update(unmatched_ask_rows=1),
        lambda p: p["symbols"]["EURUSD"]["bid_volume"].update(finite=False),
        lambda p: p["symbols"]["EURUSD"]["ask_volume"].update(nonnegative=False),
        lambda p: p["symbols"]["EURUSD"]["ask_volume"].update(missing=1),
        lambda p: p["symbols"]["EURUSD"]["ask_volume"].update(zero_count=1),
    ],
)
def test_activity_provenance_rejects_every_optimistic_forgery(mutator: Any) -> None:
    payload = _activity_provenance()
    mutator(payload)
    assert not _valid_activity(payload)


def test_activity_provenance_rejects_missing_extra_symbol_and_schema_keys() -> None:
    missing = _activity_provenance()
    missing["symbols"].pop("EURUSD")
    assert not _valid_activity(missing)
    extra = _activity_provenance()
    extra["symbols"]["EURUSD"]["extra"] = False
    assert not _valid_activity(extra)
    root_extra = _activity_provenance()
    root_extra["authority"] = False
    assert not _valid_activity(root_extra)


def test_activity_provenance_coverage_boundaries_and_minimum_are_exact() -> None:
    assert pvsclc.MINIMUM_SOURCE_ROWS_PER_SYMBOL == 175_000
    assert _valid_activity(_activity_provenance())

    below_minimum = _activity_provenance(rows=174_999)
    assert not _valid_activity_with_integrity(
        below_minimum, _source_integrity(rows=174_999)
    )

    boundary = _activity_provenance()
    integrity = _source_integrity()
    for symbol in pvsclc.FX_SYMBOLS:
        boundary["symbols"][symbol]["first_timestamp"] = "2023-01-07T23:59:00Z"
        integrity[symbol]["first_timestamp"] = "2023-01-07T23:59:00Z"
        boundary["symbols"][symbol]["last_timestamp"] = "2023-06-24T00:00:00Z"
        integrity[symbol]["last_timestamp"] = "2023-06-24T00:00:00Z"
    assert _valid_activity_with_integrity(boundary, integrity)

    too_late_first = copy.deepcopy(boundary)
    too_late_first_integrity = copy.deepcopy(integrity)
    too_late_first["symbols"]["EURUSD"]["first_timestamp"] = "2023-01-08T00:00:00Z"
    too_late_first_integrity["EURUSD"]["first_timestamp"] = "2023-01-08T00:00:00Z"
    assert not _valid_activity_with_integrity(too_late_first, too_late_first_integrity)

    end_exclusive = copy.deepcopy(boundary)
    end_exclusive_integrity = copy.deepcopy(integrity)
    end_exclusive["symbols"]["EURUSD"]["last_timestamp"] = EVALUATION_END
    end_exclusive_integrity["EURUSD"]["last_timestamp"] = EVALUATION_END
    assert not _valid_activity_with_integrity(end_exclusive, end_exclusive_integrity)


def test_activity_provenance_loader_rejects_wrong_metric_and_duplicate_json(
    tmp_path: Path,
) -> None:
    path = tmp_path / "volume_provenance.json"
    payload = _activity_provenance()
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert (
        pvsclc._load_activity_provenance(
            path,
            source_sha256_by_symbol=SOURCE_HASHES,
            source_activity_integrity_by_symbol=_source_integrity(),
            evaluation_start_utc=EVALUATION_START,
            evaluation_end_utc=EVALUATION_END,
            source_design_lock_utc=SOURCE_DESIGN_LOCK,
            preregistration_lock_utc=PREREGISTRATION_LOCK,
        )
        == payload
    )
    payload["activity_metric_id"] = "wrong"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid two-sided activity provenance"):
        pvsclc._load_activity_provenance(
            path,
            source_sha256_by_symbol=SOURCE_HASHES,
            source_activity_integrity_by_symbol=_source_integrity(),
            evaluation_start_utc=EVALUATION_START,
            evaluation_end_utc=EVALUATION_END,
            source_design_lock_utc=SOURCE_DESIGN_LOCK,
            preregistration_lock_utc=PREREGISTRATION_LOCK,
        )
    path.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        pvsclc._load_activity_provenance(
            path,
            source_sha256_by_symbol=SOURCE_HASHES,
            source_activity_integrity_by_symbol=_source_integrity(),
            evaluation_start_utc=EVALUATION_START,
            evaluation_end_utc=EVALUATION_END,
            source_design_lock_utc=SOURCE_DESIGN_LOCK,
            preregistration_lock_utc=PREREGISTRATION_LOCK,
        )


def test_invalid_activity_provenance_fail_closes_every_cell() -> None:
    invalid = _activity_provenance()
    invalid["activity_metric_id"] = "wrong"
    result = _screen(activity_provenance=invalid)
    assert result["cost_readiness"]["activity_provenance_ready"] is False
    assert all(cell["source_ready"] is False for cell in result["cells"])
    assert all(cell["passes_discovery_cell_gate"] is False for cell in result["cells"])
    assert result["reservation_ledger"] == []
    assert result["trade_ledger"] == []
    assert not pvsclc.validate_result_bundle(result)


def test_cli_requires_activity_provenance_argument() -> None:
    with pytest.raises(SystemExit) as exc_info:
        pvsclc.main([])
    assert exc_info.value.code == 2


def test_cli_recomputes_manifest_streams_and_emits_research_only_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pvsclc, "MINIMUM_SOURCE_ROWS_PER_SYMBOL", 1)
    test_window_seconds = pvsclc._parse_epoch(EVALUATION_END) - BASE_EPOCH
    monkeypatch.setattr(pvsclc, "MAXIMUM_BOUNDARY_LAG_SECONDS", test_window_seconds)
    bundle = tmp_path / "bundle"
    input_root = bundle / "input"
    input_root.mkdir(parents=True)
    source_hashes: dict[str, str] = {}
    source_integrity: dict[str, dict[str, Any]] = {}
    for symbol in pvsclc.FX_SYMBOLS:
        bar = _bar(0)
        row = ["2023-01-01T00:00:00Z", *map(str, dataclasses.astuple(bar)[1:])]
        path = input_root / f"{symbol}_M1.csv"
        path.write_bytes(
            (",".join(pvsclc.CSV_HEADER) + "\n" + ",".join(row) + "\n").encode()
        )
        source_hashes[symbol] = pvsclc._sha256(path)
        source_integrity[symbol] = pvsclc._source_activity_integrity(path)
    manifest = pvsclc._canonical_input_manifest_bytes(source_hashes)
    assert manifest is not None
    (bundle / "input_sha256.txt").write_bytes(manifest)

    activity = _activity_provenance(rows=1)
    activity["coverage_contract"]["minimum_rows_per_symbol"] = 1
    activity["coverage_contract"]["maximum_boundary_lag_seconds"] = test_window_seconds
    activity["input_manifest_sha256"] = hashlib.sha256(manifest).hexdigest()
    for symbol in pvsclc.FX_SYMBOLS:
        identity = source_integrity[symbol]
        record = activity["symbols"][symbol]
        record["output_sha256"] = source_hashes[symbol]
        record["rows"] = identity["rows"]
        record["bid_stream_sha256"] = identity["bid_stream_sha256"]
        record["ask_stream_sha256"] = identity["ask_stream_sha256"]
        record["bid_volume"]["rows"] = identity["rows"]
        record["ask_volume"]["rows"] = identity["rows"]
        record["bid_volume"]["zero_count"] = identity["bid_zero_count"]
        record["ask_volume"]["zero_count"] = identity["ask_zero_count"]
        for field in (
            "first_timestamp",
            "last_timestamp",
            "timestamp_set_sha256",
            "gap_count",
            "maximum_gap_seconds",
        ):
            record[field] = identity[field]
    activity_path = bundle / "volume_provenance.json"
    activity_path.write_text(json.dumps(activity), encoding="utf-8")
    proxy_path = bundle / "proxy.json"
    proxy_path.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROXY_PROVENANCE,
                "budgets_bps": _budgets(),
            }
        ),
        encoding="utf-8",
    )
    reservations = bundle / "reservations.json"
    trades = bundle / "trades.json"
    cells = bundle / "cells.json"
    guarded_inputs = {
        *(path.resolve() for path in input_root.glob("*_M1.csv")),
        (bundle / "input_sha256.txt").resolve(),
        activity_path.resolve(),
        proxy_path.resolve(),
    }
    read_counts = {path: 0 for path in guarded_inputs}
    original_read_bytes = Path.read_bytes

    def tracked_read_bytes(path: Path) -> bytes:
        resolved = path.resolve()
        if resolved in read_counts:
            read_counts[resolved] += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tracked_read_bytes)
    exit_code = pvsclc.main(
        [
            "--csv-root",
            str(input_root),
            "--start",
            EVALUATION_START,
            "--end",
            EVALUATION_END,
            "--source-design-lock-utc",
            SOURCE_DESIGN_LOCK,
            "--preregistration-lock-utc",
            PREREGISTRATION_LOCK,
            "--proxy-budgets-json",
            str(proxy_path),
            "--activity-provenance-json",
            str(activity_path),
            "--reservation-ledger-out",
            str(reservations),
            "--trade-ledger-out",
            str(trades),
            "--json-out",
            str(cells),
        ]
    )
    assert exit_code == 2  # One row is intentionally not research-ready.
    result = pvsclc.loads_strict_json(cells.read_text(encoding="utf-8"))
    assert (
        result["input_metadata"]["input_manifest_sha256"]
        == hashlib.sha256(manifest).hexdigest()
    )
    assert result["input_metadata"]["activity_provenance_sha256"] == pvsclc._sha256(
        activity_path
    )
    assert result["success_claim_authorized"] is False
    assert result["order_authorized"] is False
    assert reservations.is_file() and trades.is_file()
    assert set(read_counts.values()) == {1}
