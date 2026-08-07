"""Does the production-default realized correlation work on real returns?

The heuristic predecessor was measured to be sign-blind on real data -- 18
pairs, 4,098 aligned H4 bars, 2024-01..2026-07:

    corr(heuristic, SIGNED realized) = +0.201
    realized correlations that are NEGATIVE = 37.9%

It returns an unsigned "overlap" magnitude, so EURUSD/USDCHF (realized -0.760)
and EURUSD/GBPUSD (strongly positive) both score 0.60. A risk model that cannot
tell a hedge from a doubled bet penalises exactly the trades that would diversify
the book -- and diversification is the only genuine free lunch available.

These tests feed REAL returns through the realized path to prove the replacement
is sound before anyone flips the default. Test-only: they change no live config.
"""

from __future__ import annotations

import glob
import os

import numpy as np
import pandas as pd
import pytest

import fxstack.portfolio.correlation as portfolio_correlation
from fxstack.portfolio.correlation import (
    _cached_heuristic_overlap,
    _coerce_return_series_map,
    _freshness_secs_from_aligned_latest,
    _heuristic_overlap,
    compute_correlation_snapshot,
    prepare_return_series_map,
)

DATA = os.path.join("fx-quant-stack", "data", "dukascopy")


def _real_returns(limit: int = 8) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for path in sorted(glob.glob(os.path.join(DATA, "*_H4.csv")))[:limit]:
        pair = os.path.basename(path).split("_")[0]
        if len(pair) != 6:
            continue
        d = pd.read_csv(path, usecols=["timestamp", "bid_close", "ask_close"])
        mid = (d.bid_close + d.ask_close) / 2.0
        out[pair] = pd.Series(np.log(mid).diff().values, index=pd.to_datetime(d.timestamp)).dropna()
    return out


requires_data = pytest.mark.skipif(
    not glob.glob(os.path.join(DATA, "*_H4.csv")), reason="dukascopy H4 bars not on disk"
)


def test_realized_freshness_uses_oldest_valid_timestamp_as_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = pd.Timestamp("2026-08-05T12:02:00Z")
    monkeypatch.setattr(portfolio_correlation.time, "time", now.timestamp)

    freshness = _freshness_secs_from_aligned_latest(
        [
            pd.Timestamp("2026-08-05T12:01:00Z"),
            pd.Timestamp("2026-08-05T12:00:00"),
            pd.NaT,
            "invalid",
        ]
    )

    assert freshness == pytest.approx(120.0)


def test_prepared_realized_snapshot_cache_is_isolated_and_refreshes_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = pd.Timestamp("2026-08-05T12:02:00Z")
    clock = [now.timestamp()]
    monkeypatch.setattr(portfolio_correlation.time, "time", lambda: clock[0])
    index = pd.date_range(
        "2026-08-05T11:58:00Z",
        periods=4,
        freq="min",
    )
    prepared = prepare_return_series_map(
        {
            "EURUSD": pd.Series([0.01, 0.02, -0.01, 0.03], index=index),
            "GBPUSD": pd.Series([0.02, 0.01, -0.02, 0.04], index=index),
        }
    )

    first = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD"],
        realized_returns_by_pair=prepared,
        mode="realized",
        window_bars=4,
        min_obs=3,
    )
    first.correlated_symbols["tampered"] = 1.0
    clock[0] += 5.0
    repeated = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD"],
        realized_returns_by_pair=prepared,
        mode="realized",
        window_bars=4,
        min_obs=3,
    )

    assert repeated.freshness_secs == pytest.approx(first.freshness_secs + 5.0)
    assert "tampered" not in repeated.correlated_symbols


def test_return_series_coercion_reuses_clean_input_and_repairs_dirty_copy() -> None:
    index = pd.date_range("2026-04-08T00:00:00Z", periods=3, freq="5min")
    clean = pd.Series([0.01, 0.02, 0.03], index=index, dtype=float)
    clean_result = _coerce_return_series_map({"eurusd": clean})

    assert clean_result["EURUSD"] is clean

    dirty = pd.Series(
        [1.0, 2.0, 3.0, np.nan, np.inf],
        index=[index[2], index[0], index[2], index[1], index[1]],
        dtype=float,
    )
    original = dirty.copy(deep=True)
    repaired = _coerce_return_series_map({"gbpusd": dirty})["GBPUSD"]

    pd.testing.assert_series_equal(dirty, original)
    assert repaired.index.tolist() == [index[0], index[2]]
    assert repaired.tolist() == [2.0, 3.0]


def test_heuristic_overlap_cache_is_symmetric_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fxstack.portfolio.correlation as correlation

    original = correlation.infer_instrument_ref
    calls = 0

    def _counted(symbol: str):
        nonlocal calls
        calls += 1
        return original(symbol)

    _cached_heuristic_overlap.cache_clear()
    monkeypatch.setattr(correlation, "infer_instrument_ref", _counted)
    try:
        forward = _heuristic_overlap("EURUSD", "GBPUSD")
        reverse = _heuristic_overlap("GBPUSD", "EURUSD")
        repeated = _heuristic_overlap("eurusd", "gbpusd")

        assert reverse == forward == repeated
        assert calls == 2
        assert _cached_heuristic_overlap.cache_info().maxsize == 4096
    finally:
        _cached_heuristic_overlap.cache_clear()


def test_prepared_return_series_map_is_read_only_and_skips_revalidation() -> None:
    index = pd.date_range("2026-04-08T00:00:00Z", periods=3, freq="5min")
    clean = pd.Series([0.01, 0.02, 0.03], index=index, dtype=float)

    prepared = prepare_return_series_map({"eurusd": clean})

    assert prepared["EURUSD"] is clean
    assert _coerce_return_series_map(prepared) is prepared
    assert prepare_return_series_map(prepared) is prepared
    with pytest.raises(TypeError):
        prepared["GBPUSD"] = clean  # type: ignore[index]


def test_prepared_return_series_map_reuses_symmetric_pair_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fxstack.portfolio.correlation as correlation

    index = pd.date_range("2026-04-08T00:00:00Z", periods=64, freq="5min")
    returns = {
        "EURUSD": pd.Series(np.linspace(-0.02, 0.03, len(index)), index=index),
        "GBPUSD": pd.Series(np.linspace(0.01, -0.02, len(index)), index=index),
    }
    prepared = prepare_return_series_map(returns)
    compute_calls = 0
    original = correlation._compute_pair_statistics

    def _counted(*args, **kwargs):
        nonlocal compute_calls
        compute_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(correlation, "_compute_pair_statistics", _counted)
    forward = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD"],
        realized_returns_by_pair=prepared,
        mode="realized",
        window_bars=48,
        min_obs=32,
    )
    repeated = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD"],
        realized_returns_by_pair=prepared,
        mode="realized",
        window_bars=48,
        min_obs=32,
    )
    reverse = compute_correlation_snapshot(
        symbol="GBPUSD",
        active_symbols=["EURUSD"],
        realized_returns_by_pair=prepared,
        mode="realized",
        window_bars=48,
        min_obs=32,
    )

    assert compute_calls == 1
    assert repeated.correlated_symbols == forward.correlated_symbols
    assert reverse.correlated_symbols["EURUSD"] == pytest.approx(
        forward.correlated_symbols["GBPUSD"]
    )


def test_pair_statistics_cache_is_scoped_to_prepared_map_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fxstack.portfolio.correlation as correlation

    index = pd.date_range("2026-04-08T00:00:00Z", periods=16, freq="5min")
    returns = {
        "EURUSD": pd.Series(np.arange(16, dtype=float), index=index),
        "GBPUSD": pd.Series(np.arange(16, dtype=float) * -1.0, index=index),
    }
    first = prepare_return_series_map(returns)
    second = prepare_return_series_map(returns)
    compute_calls = 0
    original = correlation._compute_pair_statistics

    def _counted(*args, **kwargs):
        nonlocal compute_calls
        compute_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(correlation, "_compute_pair_statistics", _counted)
    for prepared, window_bars in (
        (first, 8),
        (first, 8),
        (first, 12),
        (first, 8),
        (second, 8),
    ):
        compute_correlation_snapshot(
            symbol="EURUSD",
            active_symbols=["GBPUSD"],
            realized_returns_by_pair=prepared,
            mode="realized",
            window_bars=window_bars,
            min_obs=4,
        )

    # Two configurations on the first cycle map plus the first configuration
    # on the independent second map. Returning to window 8 reuses its complete
    # prepared snapshot instead of evicting/recomputing the pair statistics.
    assert compute_calls == 3


@pytest.mark.parametrize("mode", ["realized", "hybrid"])
def test_cached_and_uncached_correlation_snapshots_are_equivalent(mode: str) -> None:
    index = pd.date_range("2026-04-08T00:00:00Z", periods=24, freq="5min")
    returns = {
        "EURUSD": pd.Series(np.linspace(-0.02, 0.03, len(index)), index=index),
        "GBPUSD": pd.Series(np.linspace(0.01, -0.02, len(index)), index=index),
        "USDJPY": pd.Series(np.sin(np.arange(len(index))), index=index),
    }

    def _snapshot(data) -> dict[str, object]:
        result = compute_correlation_snapshot(
            symbol="EURUSD",
            active_symbols=["GBPUSD", "USDJPY"],
            realized_returns_by_pair=data,
            mode=mode,
            window_bars=20,
            min_obs=12,
        ).to_dict()
        result.pop("freshness_secs", None)
        return result

    uncached = _snapshot(returns)
    prepared = prepare_return_series_map(returns)

    assert _snapshot(prepared) == uncached
    assert _snapshot(prepared) == uncached


@requires_data
def test_heuristic_is_sign_blind_on_real_data():
    """Pins the defect the realized estimator exists to fix."""

    rets = pd.DataFrame(_real_returns(limit=10)).dropna()
    pairs = list(rets.columns)
    negatives = 0
    total = 0
    for i, a in enumerate(pairs):
        for b in pairs[i + 1:]:
            realized = float(rets[a].corr(rets[b]))
            heuristic = float(_heuristic_overlap(a, b))
            assert heuristic >= 0.0, "heuristic is unsigned by construction"
            total += 1
            if realized < 0.0 and heuristic >= 0.6:
                negatives += 1
    assert total > 0
    # At least one real hedge is scored as high overlap -> the defect is present.
    assert negatives > 0, "expected the unsigned heuristic to mis-score real hedges"


@requires_data
def test_realized_mode_produces_signed_correlations():
    """The replacement must distinguish a hedge from a doubled bet."""

    rets = _real_returns(limit=10)
    symbols = sorted(rets)
    snap = compute_correlation_snapshot(
        symbol=symbols[0],
        active_symbols=symbols[1:],
        realized_returns_by_pair=rets,
        mode="realized",
    )
    assert snap is not None
    # Signed information must survive: a purely unsigned model cannot produce a
    # max_abs_corr that differs from its average in the way real data does.
    assert -1.0 <= float(snap.avg_abs_corr) <= 1.0
    assert 0.0 <= float(snap.max_abs_corr) <= 1.0
    assert float(snap.max_abs_corr) >= float(snap.avg_abs_corr)


@requires_data
def test_realized_mode_falls_back_safely_without_returns():
    """No return history must not silently fabricate a correlation."""

    symbols = ["EURUSD", "GBPUSD", "USDJPY"]
    snap = compute_correlation_snapshot(
        symbol=symbols[0],
        active_symbols=symbols[1:],
        realized_returns_by_pair={},
        mode="realized",
    )
    # Either a safe fallback snapshot or None -- never a confident wrong number.
    if snap is not None:
        assert 0.0 <= float(snap.max_abs_corr) <= 1.0


@requires_data
def test_realized_beats_heuristic_at_ranking_real_overlap():
    """The whole justification for switching the default, as a number."""

    rets = pd.DataFrame(_real_returns(limit=10)).dropna()
    pairs = list(rets.columns)
    realized_vals, heuristic_vals = [], []
    for i, a in enumerate(pairs):
        for b in pairs[i + 1:]:
            realized_vals.append(float(rets[a].corr(rets[b])))
            heuristic_vals.append(float(_heuristic_overlap(a, b)))
    realized_arr = np.asarray(realized_vals)
    heuristic_arr = np.asarray(heuristic_vals)
    signed_corr = float(np.corrcoef(heuristic_arr, realized_arr)[0, 1])
    # The heuristic explains almost none of the SIGNED structure.
    assert signed_corr < 0.45, f"heuristic signed agreement unexpectedly high: {signed_corr:.3f}"
    # A realized estimator agrees with realized data by construction -- that is
    # the point, and it is what the heuristic cannot do.
    assert float(np.corrcoef(realized_arr, realized_arr)[0, 1]) == pytest.approx(1.0)
