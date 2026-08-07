"""Regression tests for the backtest summary.

The bug these lock out: ``positive_share`` was computed over the subset that
``take_trade`` had already filtered to ``net_edge_bps > 0``, so it was
identically 1.0 for every input and read like a hit rate. A metric that cannot
report failure is worse than no metric, because it certifies whatever it is
pointed at.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fxstack.backtest.engine import evaluate_signals
from fxstack.backtest.reports import summarize_backtest


def _signals() -> pd.DataFrame:
    # Deliberately mixed: some candidates clear cost, most do not.
    return pd.DataFrame(
        {
            "pair": ["EURUSD"] * 6,
            "expected_edge_bps": [12.0, 8.0, 1.0, 0.5, -3.0, 6.0],
            "spread_bps": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            "allowed": [True, True, True, True, True, True],
        }
    )


def test_positive_share_is_not_identically_one():
    """The headline metric must be capable of reporting failure."""

    summary = summarize_backtest(evaluate_signals(_signals()))
    assert summary["positive_share"] < 1.0, (
        "positive_share is tautological again -- it must be computed over ALL "
        f"candidates, not the take_trade subset (got {summary['positive_share']})"
    )
    assert 0.0 <= summary["positive_share"] <= 1.0


def test_all_negative_edge_reports_zero_positive_share():
    bad = _signals()
    bad["expected_edge_bps"] = [-5.0] * 6
    summary = summarize_backtest(evaluate_signals(bad))
    assert summary["positive_share"] == 0.0
    assert summary["trades"] == 0.0


def test_candidates_and_take_rate_reported():
    summary = summarize_backtest(evaluate_signals(_signals()))
    assert summary["candidates"] == 6.0
    assert 0.0 <= summary["take_rate"] <= 1.0
    assert summary["trades"] <= summary["candidates"]


def test_expected_metrics_are_flagged_as_not_realized():
    summary = summarize_backtest(evaluate_signals(_signals()))
    assert summary["realized_available"] == 0.0, (
        "a frame with no realized outcome column must not imply realized performance"
    )


def test_realized_metrics_computed_when_outcome_present():
    scored = evaluate_signals(_signals())
    # Realized outcome that disagrees with the forecast: the two best-forecast
    # candidates actually lost. An honest summary must show that.
    scored["realized_edge_bps"] = [-9.0, -4.0, 0.0, 0.0, 0.0, 11.0]
    summary = summarize_backtest(scored)
    assert summary["realized_available"] == 1.0
    assert "realized_mean_net_bps_taken" in summary
    assert "realized_hit_rate_taken" in summary
    assert summary["realized_hit_rate_taken"] < 1.0
    # Forecast said positive; realization says otherwise. The point of the fix.
    assert summary["realized_mean_net_bps_taken"] < summary["mean_net_edge_bps"]


def test_selection_uplift_detects_a_useless_gate():
    """If taking trades is no better than taking everything, say so."""

    scored = evaluate_signals(_signals())
    scored["realized_edge_bps"] = [2.0] * 6  # outcome independent of the forecast
    summary = summarize_backtest(scored)
    assert summary["realized_selection_uplift_bps"] == pytest.approx(0.0, abs=1e-9)


def test_empty_frame_is_safe():
    summary = summarize_backtest(pd.DataFrame())
    assert summary["trades"] == 0.0
    assert summary["realized_available"] == 0.0
