from __future__ import annotations

import pandas as pd

from src.run_fx import (
    _bridge_bars_to_df,
    _startup_backfill_mark_pending,
    _startup_backfill_mark_ready,
    _startup_backfill_retry_age_secs,
    _startup_backfill_retry_due,
    _startup_backfill_state_default,
    _startup_warmup_strategy,
    _synthetic_seed_close,
    _synthetic_fill_times,
    fetch_market_data,
)


def test_fetch_market_data_honors_lookback(tmp_path):
    n = 600
    df = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=n, freq="h"),
            "open": [1.10 + i * 1e-5 for i in range(n)],
            "high": [1.11 + i * 1e-5 for i in range(n)],
            "low": [1.09 + i * 1e-5 for i in range(n)],
            "close": [1.10 + i * 1e-5 for i in range(n)],
        }
    )
    p = tmp_path / "EURUSD.csv"
    df.to_csv(p, index=False)

    md_lookback_120 = fetch_market_data(str(tmp_path), ["EURUSD"], lookback=120)
    assert "EURUSD" in md_lookback_120
    assert len(md_lookback_120["EURUSD"]) == 120

    # Hard cap still applies at MAX_BARS (=500 in run_fx.py).
    md_lookback_900 = fetch_market_data(str(tmp_path), ["EURUSD"], lookback=900)
    assert len(md_lookback_900["EURUSD"]) == 500


def test_bridge_bars_to_df_parses_and_orders_rows():
    rows = [
        {
            "time": "2026-03-12T10:00:00+00:00",
            "open": 1.1000,
            "high": 1.1010,
            "low": 1.0995,
            "close": 1.1008,
            "volume": 10,
        },
        {
            "time": "2026-03-12T09:00:00+00:00",
            "open": 1.0990,
            "high": 1.1002,
            "low": 1.0987,
            "close": 1.1000,
            "volume": 8,
        },
    ]
    out = _bridge_bars_to_df(rows)
    assert not out.empty
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert len(out) == 2
    assert out.index.is_monotonic_increasing


def test_synthetic_fill_times_anchor_to_current_bar_tail_window():
    current = pd.Timestamp("2026-03-13 15:00:00")
    out = _synthetic_fill_times(current, 3)
    assert out == [
        pd.Timestamp("2026-03-13 13:00:00"),
        pd.Timestamp("2026-03-13 14:00:00"),
        pd.Timestamp("2026-03-13 15:00:00"),
    ]


def test_synthetic_seed_close_uses_live_mid_for_truncated_synthetic_recovery():
    seeded = _synthetic_seed_close(
        prev_close=1.15112,
        live_mid=1.14547,
        recovery_source="synthetic_capped",
        gap_fill_truncated=True,
    )
    assert seeded == 1.14547


def test_startup_warmup_strategy_parser_defaults_to_live_on_invalid_mode():
    assert _startup_warmup_strategy("backward_bridge") == "backward_bridge"
    assert _startup_warmup_strategy("live") == "live"
    assert _startup_warmup_strategy("invalid_mode") == "live"


def test_startup_backfill_state_pending_to_ready_transition():
    state = _startup_backfill_state_default()
    _startup_backfill_mark_pending(
        state=state,
        now_ts=100.0,
        gap_hours=382,
        bridge_bars=12,
        attempted_retry=True,
    )
    assert state["pending"] is True
    assert state["ready"] is False
    assert state["bridge_bars"] == 12
    assert state["gap_hours_original"] == 382
    assert _startup_backfill_retry_due(state, now_ts=105.0, retry_secs=10.0) is False
    assert _startup_backfill_retry_due(state, now_ts=111.0, retry_secs=10.0) is True
    assert _startup_backfill_retry_age_secs(state, now_ts=130.0) == 30.0

    _startup_backfill_mark_ready(
        state=state,
        now_ts=131.0,
        gap_hours=382,
        bridge_bars=96,
    )
    assert state["pending"] is False
    assert state["ready"] is True
    assert state["ready_processed"] is False
    assert state["bridge_bars"] == 96
    assert _startup_backfill_retry_age_secs(state, now_ts=200.0) == 0.0
