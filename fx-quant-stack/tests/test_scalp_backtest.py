"""Fill-model honesty tests for the offline scalp backtester.

Signal logic is covered by test_scalp_core.py; here we pin the replay
semantics that keep the backtest from flattering itself: adverse entry,
TP-at-level, SL-first on double-touch, gap refusal, breaker, cooldown, eod.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from fxstack.scalp.backtest import (
    BacktestRunner,
    BtBar,
    load_bt_bars,
    summarize,
)
from fxstack.scalp.bars import M1Bar
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.signals import ScalpIntent

T0 = 1_753_999_200  # 2025-07-31-ish Thursday 22:00 UTC -> weekday minutes


def _config() -> ScalpConfig:
    cfg = ScalpConfig()
    cfg.min_history_bars = 5
    cfg.cooldown_bars = 2
    cfg.daily_loss_stop_r = -3.0
    return cfg


def _bt_bar(
    minute: int,
    *,
    mid_o: float = 1.1000,
    mid_h: float | None = None,
    mid_l: float | None = None,
    mid_c: float | None = None,
    spread: float = 0.0001,
    symbol: str = "EURUSD",
    valid: bool = True,
) -> BtBar:
    h = mid_h if mid_h is not None else mid_o + 0.0001
    lo = mid_l if mid_l is not None else mid_o - 0.0001
    c = mid_c if mid_c is not None else mid_o
    half = spread / 2.0
    bar = M1Bar(
        symbol=symbol,
        minute_epoch=minute,
        open=mid_o, high=h, low=lo, close=c,
        bid_close=c - half, ask_close=c + half,
        spread_max_bps=spread / c * 1e4,
        spread_close_bps=spread / c * 1e4,
        tick_count=3, valid=valid,
        invalid_reason="" if valid else "frozen_quotes",
        quote_changes=1 if valid else 0,
    )
    return BtBar(
        bar=bar,
        bid_open=mid_o - half, bid_high=h - half, bid_low=lo - half,
        ask_open=mid_o + half, ask_high=h + half, ask_low=lo + half,
    )


def _intent(minute: int, *, side: str = "BUY", entry: float = 1.10005) -> ScalpIntent:
    stop_bps, tp_bps = 5.0, 7.5
    mid = 1.1000
    stop_px, tp_px = stop_bps / 1e4 * mid, tp_bps / 1e4 * mid
    if side == "BUY":
        sl, tp = entry - stop_px, entry + tp_px
    else:
        sl, tp = entry + stop_px, entry - tp_px
    return ScalpIntent(
        symbol="EURUSD", side=side, minute_epoch=minute, ref_mid=mid,
        entry_price=entry, sl_price=sl, tp_price=tp, atr_bps=5.0,
        stop_bps=stop_bps, disp_z=-2.5, spread_bps=0.9, p_star=0.47,
        time_stop_bars=20,
    )


def _arm_pending(runner: BacktestRunner, intent: ScalpIntent, minute: int) -> None:
    """Stage an intent as the engine would: the fill lands on the NEXT M1 bar
    after the engine window that produced it."""
    runner._pending = intent
    runner._pending_fill_minute = minute + 60


def _open_position(runner: BacktestRunner, minute: int, *, side: str = "BUY"):
    _arm_pending(runner, _intent(minute, side=side), minute)
    runner.process(_bt_bar(minute + 60))
    assert runner._pos is not None, runner.stats.reasons
    return runner._pos


def test_entry_fills_adverse_of_signal_and_next_open():
    runner = BacktestRunner(config=_config())
    _arm_pending(runner, _intent(T0, entry=1.10005), T0)
    # Next bar opens HIGHER: a BUY must pay the worse (higher) price.
    runner.process(_bt_bar(T0 + 60, mid_o=1.1002, spread=0.0001))
    pos = runner._pos
    assert pos is not None
    assert pos.entry_price == pytest.approx(1.10025)  # ask_open, worse than signal
    # Bracket re-anchors to the actual entry.
    assert pos.sl_price == pytest.approx(pos.entry_price - 5.0 / 1e4 * 1.1000)


def test_entry_never_improves_on_signal_price():
    runner = BacktestRunner(config=_config())
    _arm_pending(runner, _intent(T0, entry=1.10005), T0)
    # Next bar opens LOWER (better for a BUY) -- fill stays at the signal touch.
    runner.process(_bt_bar(T0 + 60, mid_o=1.0998, spread=0.0001))
    assert runner._pos is not None
    assert runner._pos.entry_price == pytest.approx(1.10005)


def test_gap_after_signal_refuses_entry():
    runner = BacktestRunner(config=_config())
    for i in range(6):
        runner.process(_bt_bar(T0 + 60 * i))
    _arm_pending(runner, _intent(T0 + 300), T0 + 300)
    runner.process(_bt_bar(T0 + 300 + 180))  # 3-minute jump
    assert runner._pos is None
    assert runner.stats.reasons.get("entry_refused_gap") == 1
    assert len(runner._run) == 1  # gap also broke the valid run


def test_tp_fills_at_level_never_the_extreme():
    runner = BacktestRunner(config=_config())
    pos = _open_position(runner, T0)
    tp = pos.tp_price
    # Bar blasts far beyond TP; the fill must be AT the level.
    runner.process(_bt_bar(T0 + 120, mid_o=1.1010, mid_h=1.1030, mid_l=1.1009))
    assert runner._pos is None
    fill = runner.stats.fills[-1]
    assert fill.exit_reason == "tp"
    assert fill.exit_price == pytest.approx(tp)


def test_both_touched_in_one_bar_books_the_stop():
    runner = BacktestRunner(config=_config())
    pos = _open_position(runner, T0)
    # One wide bar spans both SL and TP with the OPEN inside the bracket --
    # genuinely unknowable ordering books the SL.
    runner.process(_bt_bar(T0 + 120, mid_o=1.1000, mid_h=1.1030, mid_l=1.0980))
    fill = runner.stats.fills[-1]
    assert fill.exit_reason == "sl"
    assert fill.exit_price == pytest.approx(pos.sl_price)
    assert fill.pnl_r == pytest.approx(-1.0)
    assert runner.stats.reasons.get("sl_double_touch") == 1


def test_bar_opening_through_stop_books_the_gap_fill_not_the_level():
    runner = BacktestRunner(config=_config())
    _open_position(runner, T0)
    # The bar OPENS far below the stop: a real stop fills at the open, and the
    # ledger must carry the full gap loss, never a truncated -1R.
    runner.process(_bt_bar(T0 + 120, mid_o=1.0970, mid_h=1.0972, mid_l=1.0968))
    fill = runner.stats.fills[-1]
    assert fill.exit_reason == "sl"
    assert fill.exit_price == pytest.approx(1.0970 - 0.00005)  # bid_open
    assert fill.pnl_r < -5.0
    assert runner.stats.reasons.get("sl_gap_open") == 1


def test_bar_opening_through_tp_books_the_tp_even_if_sl_swept_later():
    runner = BacktestRunner(config=_config())
    pos = _open_position(runner, T0)
    tp = pos.tp_price
    # Opens ABOVE the TP (first quote fills the limit), then sweeps down
    # through the stop intrabar: ordering is knowable -- TP wins.
    runner.process(_bt_bar(T0 + 120, mid_o=1.1012, mid_h=1.1013, mid_l=1.0980))
    fill = runner.stats.fills[-1]
    assert fill.exit_reason == "tp"
    assert fill.exit_price == pytest.approx(tp)
    assert fill.pnl_r > 0
    assert runner.stats.reasons.get("tp_gap_open") == 1
    assert "sl_double_touch" not in runner.stats.reasons


def test_sl_extra_slip_worsens_the_stop_fill():
    runner = BacktestRunner(config=_config(), sl_extra_slip_bps=1.0)
    pos = _open_position(runner, T0)
    runner.process(_bt_bar(T0 + 120, mid_o=1.0990, mid_h=1.0991, mid_l=1.0985))
    fill = runner.stats.fills[-1]
    assert fill.exit_reason == "sl"
    assert fill.exit_price < pos.sl_price
    assert fill.pnl_r < -1.0


def test_time_stop_exits_on_the_adverse_close():
    cfg = _config()
    runner = BacktestRunner(config=cfg)
    _open_position(runner, T0)
    runner._pos.intent.time_stop_bars = 3
    for i in range(2, 6):
        if runner._pos is None:
            break
        runner.process(_bt_bar(T0 + 60 * i, mid_o=1.1000, mid_h=1.1001, mid_l=1.0999))
    fill = runner.stats.fills[-1]
    assert fill.exit_reason == "time_stop"
    # Long exits at BID close: spread is paid on the way out.
    assert fill.exit_price == pytest.approx(1.1000 - 0.00005)


def test_daily_breaker_blocks_and_resets_next_day():
    cfg = _config()
    cfg.daily_loss_stop_r = -1.5
    runner = BacktestRunner(config=cfg)
    _open_position(runner, T0)
    # Bar OPENS inside the bracket, then sweeps the stop intrabar: -1R at level.
    runner.process(_bt_bar(T0 + 120, mid_o=1.0998, mid_h=1.0999, mid_l=1.0980))
    assert runner.stats.fills[-1].pnl_r == pytest.approx(-1.0)
    for i in (3, 4, 5):  # keep the minute stream unbroken -- no gap refusal
        runner.process(_bt_bar(T0 + 60 * i))
    _open_position(runner, T0 + 300)
    runner.process(_bt_bar(T0 + 420, mid_o=1.0998, mid_h=1.0999, mid_l=1.0975))
    assert runner._day_r <= -1.5
    # Same day: pipeline refuses at the breaker even with a valid history run.
    for i in range(8, 20):
        runner.process(_bt_bar(T0 + 60 * i))
    assert runner.stats.reasons.get("daily_loss_breaker", 0) > 0
    assert runner._pos is None
    # Next UTC day: breaker resets.
    runner.process(_bt_bar(T0 + 86_400))
    assert runner._day_r == 0.0


def test_cooldown_arms_after_every_fill():
    runner = BacktestRunner(config=_config())
    _open_position(runner, T0)
    runner.process(_bt_bar(T0 + 120, mid_o=1.1010, mid_h=1.1030, mid_l=1.1009))
    assert runner.stats.fills, "expected a fill"
    assert runner._cooldown == _config().cooldown_bars


def test_finish_closes_open_position_as_eod():
    runner = BacktestRunner(config=_config())
    _open_position(runner, T0)
    runner.process(_bt_bar(T0 + 120, mid_o=1.1001, mid_h=1.1002, mid_l=1.1000))
    assert runner._pos is not None
    runner.finish()
    assert runner._pos is None
    assert runner.stats.fills[-1].exit_reason == "eod"


def test_loader_parses_real_quotes_and_flags_frozen(tmp_path: Path):
    csv_text = (
        "timestamp,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close,volume\n"
        "2026-07-01T10:00:00Z,1.1000,1.1002,1.0999,1.1001,1.1001,1.1003,1.1000,1.1002,10\n"
        "2026-07-01T10:01:00Z,1.1001,1.1001,1.1001,1.1001,1.1002,1.1002,1.1002,1.1002,0\n"
    )
    path = tmp_path / "EURUSD_M1.csv"
    path.write_text(csv_text, encoding="utf-8")
    bars = list(load_bt_bars(path, symbol="EURUSD"))
    assert len(bars) == 2
    first, second = bars
    assert first.bar.valid
    assert first.bar.bid_close == pytest.approx(1.1001)
    assert first.bar.spread_close_bps == pytest.approx(
        0.0001 / ((1.1001 + 1.1002) / 2) * 1e4
    )
    assert not second.bar.valid
    assert second.bar.invalid_reason == "frozen_quotes"


def test_loader_extra_spread_widens_both_sides(tmp_path: Path):
    csv_text = (
        "timestamp,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close,volume\n"
        "2026-07-01T10:00:00Z,1.1000,1.1002,1.0999,1.1001,1.1001,1.1003,1.1000,1.1002,10\n"
    )
    path = tmp_path / "EURUSD_M1.csv"
    path.write_text(csv_text, encoding="utf-8")
    plain = next(iter(load_bt_bars(path, symbol="EURUSD")))
    padded = next(iter(load_bt_bars(path, symbol="EURUSD", extra_spread_bps=1.0)))
    assert padded.bar.ask_close > plain.bar.ask_close
    assert padded.bar.bid_close < plain.bar.bid_close
    assert padded.bar.spread_close_bps == pytest.approx(
        plain.bar.spread_close_bps + 1.0, abs=0.01
    )
    # Mid is unchanged: padding is symmetric.
    assert padded.bar.close == pytest.approx(plain.bar.close)


def test_momentum_mode_joins_the_dislocation():
    """Same bars, opposite hypothesis: momentum BUYs what revert SELLs."""
    from fxstack.scalp.signals import evaluate_dislocation

    cfg = _config()
    cfg.min_history_bars = 10
    cfg.min_stop_bps = 4.5
    cfg.p_star_max = 0.95  # direction semantics under test, not viability
    bars = []
    px = 1.1000
    for i in range(24):
        prev = px
        if i >= 20:
            px += 0.00060  # four-bar upward dislocation, still pushing
        else:
            px += 0.00002 * (1 if i % 2 == 0 else -1)
        bar = _bt_bar(T0 + 60 * i, mid_o=prev, mid_c=px,
                      mid_h=max(prev, px) + 0.00002,
                      mid_l=min(prev, px) - 0.00002).bar
        bars.append(bar)
    cfg.signal_mode = "revert"
    revert_intent, revert_reason = evaluate_dislocation(
        bars=bars, config=cfg, spread_bps=0.9
    )
    cfg.signal_mode = "momentum"
    momo_intent, momo_reason = evaluate_dislocation(
        bars=bars, config=cfg, spread_bps=0.9
    )
    # The last bar pushes WITH the +z dislocation: revert refuses (no
    # exhaustion), momentum joins it long.
    assert revert_intent is None and revert_reason == "no_reversion_trigger"
    assert momo_intent is not None, momo_reason
    assert momo_intent.side == "BUY"
    assert momo_intent.tp_price > momo_intent.entry_price


def test_xs_residual_runner_consumes_the_aligned_feature_for_that_bar():
    cfg = _config()
    cfg.signal_family = "xs_residual"
    cfg.xs_coherence_floor = 0.75
    cfg.xs_residual_entry_bps = 1.0
    cfg.p_star_max = 0.99
    cfg.min_stop_bps = 0.1
    start = int(dt.datetime(2025, 7, 31, 10, 0, tzinfo=dt.timezone.utc).timestamp())
    bars = [
        _bt_bar(
            start + 60 * i,
            mid_o=1.1000 + i * 0.00002,
            mid_h=1.1002 + i * 0.00002,
            mid_l=1.0998 + i * 0.00002,
            mid_c=1.1000 + i * 0.00002,
        ).bar
        for i in range(cfg.min_history_bars)
    ]
    last = bars[-1]
    runner = BacktestRunner(
        config=cfg,
        xs_features={
            last.minute_epoch: {
                "usd_factor": 0.0,
                "usd_coherence": 1.0,
                "residual": 5.0,
            }
        },
    )
    runner._run = bars
    runner._last_m1_minute = last.minute_epoch
    runner._maybe_enter(last)
    assert runner._pending is not None, runner.stats.reasons
    # Positive residuals are faded: the target pair outran fair value.
    assert runner._pending.side == "SELL"


def test_summarize_reports_expectancy_and_ci():
    runner = BacktestRunner(config=_config())
    _open_position(runner, T0)
    runner.process(_bt_bar(T0 + 120, mid_o=1.1010, mid_h=1.1030, mid_l=1.1009))
    summary = summarize("EURUSD", runner.stats)
    assert summary["trades"] == 1
    assert summary["win_rate"] == 1.0
    assert summary["exit_mix"] == {"tp": 1}
    lo, hi = summary["mean_r_ci95"]
    assert lo <= summary["mean_r"] <= hi


def test_summarize_emits_standard_json_when_profit_factor_is_undefined():
    runner = BacktestRunner(config=_config())
    summary = summarize("EURUSD", runner.stats)
    assert summary["profit_factor"] is None
    # Release evidence cannot contain Python's non-standard Infinity token.
    json.dumps(summary, allow_nan=False)
