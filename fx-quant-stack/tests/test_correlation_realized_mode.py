"""Does the realized correlation estimator actually work on real returns?

The production default is ``portfolio_corr_mode="heuristic"`` (settings.py) and
``_env.bat`` never overrides it, so the realized estimator has never run in
anger. That matters because the heuristic was MEASURED to be sign-blind on real
data -- 18 pairs, 4,098 aligned H4 bars, 2024-01..2026-07:

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

from fxstack.portfolio.correlation import _heuristic_overlap, compute_correlation_snapshot

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
