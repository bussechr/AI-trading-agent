"""Direct unit tests for :mod:`fxstack.runtime.feature_freshness`.

Distinct from ``test_runtime_live_refresh.py`` (which integration-tests via
the runner) — this pins the carved-out helpers' input/output contract so
future moves or refactors of the freshness logic surface failures
immediately.
"""

from __future__ import annotations

import pandas as pd
import pytest

from fxstack.runtime.feature_freshness import (
    feature_bar_freshness,
    feature_row_is_stale,
    timeframe_to_seconds,
)


@pytest.mark.parametrize(
    ("timeframe", "expected"),
    [
        ("M1", 60),
        ("M5", 300),
        ("M15", 900),
        ("M30", 1800),
        ("H1", 3600),
        ("H4", 14_400),
        ("D", 86_400),
        ("W", 604_800),
        ("MN", 2_592_000),
        ("MN1", 2_592_000),
    ],
)
def test_timeframe_to_seconds_known_units(timeframe: str, expected: int) -> None:
    assert timeframe_to_seconds(timeframe) == expected


@pytest.mark.parametrize("bad", ["", "X", "ABC", "Z5", None])
def test_timeframe_to_seconds_unknown_returns_zero(bad: object) -> None:
    """Unknown unit letters or empty input return 0 (the "unknown" sentinel).

    Out-of-range numeric magnitudes (e.g. ``"M-1"``) are intentionally NOT
    tested here — the parser is permissive about the magnitude as long as
    ``int()`` can read it. Callers treat the freshness layer's output as
    advisory; defensive sanitization happens in the runner before the
    timeframe is used. If that contract ever changes, add a stricter
    validator and update the test.
    """
    assert timeframe_to_seconds(bad) == 0  # type: ignore[arg-type]


def test_feature_bar_freshness_fresh_bar_reports_ok() -> None:
    ts = pd.Timestamp("2026-04-08T12:15:00Z")
    loop_ts = ts.timestamp() + 60.0  # one minute later
    out = feature_bar_freshness(ts_value=ts, loop_ts=loop_ts, timeframe="M5")
    assert out["stale"] is False
    assert out["reason"] == "ok"
    assert out["age_secs"] == pytest.approx(60.0)


def test_feature_bar_freshness_stale_bar_reports_stale() -> None:
    ts = pd.Timestamp("2026-04-08T12:00:00Z")
    # 30 minutes for an M5 bar — well past the 10-minute floor + 2× window
    loop_ts = ts.timestamp() + 1800.0
    out = feature_bar_freshness(ts_value=ts, loop_ts=loop_ts, timeframe="M5")
    assert out["stale"] is True
    assert out["reason"] == "stale_feature_bar"


def test_feature_bar_freshness_missing_ts_is_always_stale() -> None:
    out = feature_bar_freshness(ts_value=None, loop_ts=1_775_650_538.0, timeframe="M5")
    assert out["stale"] is True
    assert out["reason"] == "missing_feature_ts"
    assert out["age_secs"] is None


def test_feature_bar_freshness_uses_600s_floor_for_short_timeframes() -> None:
    """Sub-5-minute timeframes still get the 10-minute freshness floor.

    Without this, a 1-minute bar could be flagged stale just for being 2-3
    minutes old, which is normal for the live loop's tick cadence.
    """
    ts = pd.Timestamp("2026-04-08T12:00:00Z")
    # 5 minutes for an M1 bar — would be stale by 2× window (120s) but the
    # 600s floor keeps it fresh.
    loop_ts = ts.timestamp() + 300.0
    out = feature_bar_freshness(ts_value=ts, loop_ts=loop_ts, timeframe="M1")
    assert out["stale"] is False
    assert out["stale_after_secs"] >= 600.0


def test_feature_row_is_stale_empty_frame() -> None:
    assert feature_row_is_stale(row=pd.DataFrame(), loop_ts=0.0, timeframe="M5") is True


def test_feature_row_is_stale_fresh_row() -> None:
    ts = pd.Timestamp("2026-04-08T12:15:00Z")
    row = pd.DataFrame([{"ts": ts}])
    assert feature_row_is_stale(row=row, loop_ts=ts.timestamp() + 60.0, timeframe="M5") is False


def test_feature_row_is_stale_old_row() -> None:
    ts = pd.Timestamp("2026-04-08T12:00:00Z")
    row = pd.DataFrame([{"ts": ts}])
    # 1 hour past an M5 bar — well stale.
    assert feature_row_is_stale(row=row, loop_ts=ts.timestamp() + 3600.0, timeframe="M5") is True
