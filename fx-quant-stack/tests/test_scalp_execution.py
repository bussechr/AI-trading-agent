"""Passive-entry and trailing-stop execution tests.

Limit fills are where a backtest lies most easily: it is trivially tempting
to fill every resting order at a perfect price and ignore the ones the market
never reached. These tests pin the opposite -- fills require the market to
come to the level, unfilled orders expire and are COUNTED, and the fill price
is the level rather than the overshoot.
"""

from __future__ import annotations

import pytest

from fxstack.scalp.backtest import BacktestRunner
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.signals import ScalpIntent
from test_scalp_backtest import T0, _bt_bar


def _cfg(**over) -> ScalpConfig:
    cfg = ScalpConfig()
    cfg.min_history_bars = 5
    cfg.cooldown_bars = 0
    cfg.entry_mode = "limit"
    cfg.limit_offset_atr = 1.0
    cfg.limit_valid_bars = 3
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _intent(side: str = "BUY", entry: float = 1.10000, atr_bps: float = 10.0):
    mid = 1.1000
    stop_px = 5.0 / 1e4 * mid
    tp_px = 7.5 / 1e4 * mid
    sl = entry - stop_px if side == "BUY" else entry + stop_px
    tp = entry + tp_px if side == "BUY" else entry - tp_px
    return ScalpIntent(
        symbol="EURUSD", side=side, minute_epoch=T0, ref_mid=mid, entry_price=entry,
        sl_price=sl, tp_price=tp, atr_bps=atr_bps, stop_bps=5.0, disp_z=-2.5,
        spread_bps=0.9, p_star=0.47, time_stop_bars=20,
    )


def _arm(runner: BacktestRunner, intent: ScalpIntent) -> float:
    runner._pending = intent
    runner._pending_fill_minute = T0 + 60
    offset = runner.config.limit_offset_atr * intent.atr_bps / 1e4 * intent.ref_mid
    runner._limit_price = (
        intent.entry_price - offset if intent.side == "BUY"
        else intent.entry_price + offset
    )
    runner._limit_deadline_secs = runner.config.limit_valid_bars * runner.step * 60
    return runner._limit_price


def test_buy_limit_rests_below_the_signal_price():
    runner = BacktestRunner(config=_cfg())
    level = _arm(runner, _intent("BUY", entry=1.10000, atr_bps=10.0))
    # 1.0 ATR of 10bps on 1.1000 = 0.0011 better than the signal price.
    assert level == pytest.approx(1.10000 - 0.0011)


def test_limit_does_not_fill_when_price_never_comes_back():
    runner = BacktestRunner(config=_cfg())
    _arm(runner, _intent("BUY"))
    # Market runs AWAY upward: the resting buy is never touched.
    runner.process(_bt_bar(T0 + 60, mid_o=1.1010, mid_h=1.1020, mid_l=1.1008))
    assert runner._pos is None
    assert runner.stats.reasons.get("limit_filled") is None


def test_limit_fills_at_the_level_not_the_overshoot():
    runner = BacktestRunner(config=_cfg())
    level = _arm(runner, _intent("BUY"))
    # Ask dips to the level (fills) without the bid reaching the stop. Keep
    # the high below the target so nothing exits on the entry bar.
    runner.process(_bt_bar(T0 + 60, mid_o=1.0992, mid_h=1.0993, mid_l=1.0986))
    assert runner._pos is not None, runner.stats.reasons
    assert runner._pos.entry_price == pytest.approx(level)
    assert runner.stats.reasons.get("limit_filled") == 1


def test_fill_bar_that_also_sweeps_the_stop_books_the_loss():
    """A limit filled by a dive that continues through the stop is a LOSS.

    The fill and the stop can both belong to one bar, and pretending the
    position survived to trade another day is the flattering version.
    """
    runner = BacktestRunner(config=_cfg())
    _arm(runner, _intent("BUY"))
    runner.process(_bt_bar(T0 + 60, mid_o=1.1000, mid_h=1.1001, mid_l=1.0960))
    assert runner._pos is None
    assert runner.stats.reasons.get("limit_filled") == 1
    assert runner.stats.fills and runner.stats.fills[-1].pnl_r < 0


def test_entry_bar_cannot_book_a_target_from_a_pre_fill_high():
    """The favorable extreme on the entry bar may PREDATE the fill.

    A limit fills at the low; the bar's high can have happened before that.
    Crediting a take-profit from it is lookahead -- the exact bug that makes
    passive-entry backtests look extraordinary. Only the stop may fire here.
    """
    runner = BacktestRunner(config=_cfg())
    level = _arm(runner, _intent("BUY"))
    # High is far above the target, low touches the resting order.
    runner.process(_bt_bar(T0 + 60, mid_o=1.1000, mid_h=1.1001, mid_l=1.0986))
    assert runner._pos is not None, runner.stats.reasons
    assert runner._pos.entry_price == pytest.approx(level)
    # No exit was booked from this bar's high.
    assert not runner.stats.fills
    # The NEXT bar can legitimately take the target.
    runner.process(_bt_bar(T0 + 120, mid_o=1.0995, mid_h=1.1002, mid_l=1.0994))
    assert runner.stats.fills and runner.stats.fills[-1].exit_reason == "tp"


def test_limit_expires_unfilled_and_is_counted():
    runner = BacktestRunner(config=_cfg(limit_valid_bars=2))
    _arm(runner, _intent("BUY"))
    for i in range(1, 6):
        runner.process(_bt_bar(T0 + 60 * i, mid_o=1.1010, mid_h=1.1020, mid_l=1.1009))
    assert runner._pos is None
    # Expiry must be visible: silent cancellation hides a bad fill rate.
    assert runner.stats.reasons.get("limit_expired_unfilled") == 1


def test_gap_through_the_level_fills_at_the_better_open():
    runner = BacktestRunner(config=_cfg())
    level = _arm(runner, _intent("BUY"))
    # Bar OPENS below the resting level -- a real order fills at the open,
    # which is better than its price. The only case where a limit improves.
    runner.process(_bt_bar(T0 + 60, mid_o=1.0987, mid_h=1.0988, mid_l=1.0986))
    assert runner._pos is not None, runner.stats.reasons
    assert runner._pos.entry_price < level
    assert runner.stats.reasons.get("limit_filled_on_gap") == 1


def test_sell_limit_needs_the_bid_to_rise_to_it():
    runner = BacktestRunner(config=_cfg())
    level = _arm(runner, _intent("SELL", entry=1.10000))
    assert level == pytest.approx(1.10000 + 0.0011)
    # Price falls: a resting sell above the market is never reached.
    runner.process(_bt_bar(T0 + 60, mid_o=1.0990, mid_h=1.0992, mid_l=1.0980))
    assert runner._pos is None
    # Now it rallies into the level (bid reaches it; ask stays under the stop).
    runner.process(_bt_bar(T0 + 120, mid_o=1.1005, mid_h=1.1012, mid_l=1.1004))
    assert runner._pos is not None, runner.stats.reasons
    assert runner._pos.entry_price == pytest.approx(level)


def test_passive_entry_beats_market_entry_on_the_same_bars():
    """The whole point: a filled passive entry has a better basis.

    Same signal, same bars -- the limit fill must be strictly better than the
    market fill, which is where the saved spread comes from.
    """
    bar = _bt_bar(T0 + 60, mid_o=1.1000, mid_h=1.1001, mid_l=1.0986)

    def _entry_price(cfg) -> float:
        runner = BacktestRunner(config=cfg)
        if cfg.entry_mode == "limit":
            _arm(runner, _intent("BUY"))
        else:
            runner._pending = _intent("BUY")
            runner._pending_fill_minute = T0 + 60
        runner._fill_pending(bar)  # fill only, before any exit management
        assert runner._pos is not None, runner.stats.reasons
        return runner._pos.entry_price

    market_entry = _entry_price(_cfg(entry_mode="market"))
    passive_entry = _entry_price(_cfg())
    assert passive_entry < market_entry
    # The saved basis IS the point: measure it in bps of the entry.
    saved_bps = (market_entry - passive_entry) / market_entry * 1e4
    assert saved_bps > 5.0


def test_trailing_stop_only_ever_tightens():
    cfg = _cfg(entry_mode="market", trail_atr_mult=0.5)
    runner = BacktestRunner(config=cfg)
    runner._pending = _intent("BUY", entry=1.10000, atr_bps=10.0)
    runner._pending_fill_minute = T0 + 60
    runner.process(_bt_bar(T0 + 60, mid_o=1.1000, mid_h=1.1001, mid_l=1.0999))
    pos = runner._pos
    assert pos is not None
    initial_sl = pos.sl_price
    # Price advances: the stop ratchets up.
    runner.process(_bt_bar(T0 + 120, mid_o=1.1004, mid_h=1.1006, mid_l=1.1003))
    advanced = pos.sl_price
    assert advanced > initial_sl
    assert runner.stats.reasons.get("trail_advanced", 0) >= 1
    # Price retreats (without hitting the stop): the trail must NOT loosen.
    runner.process(_bt_bar(T0 + 180, mid_o=1.1003, mid_h=1.10035, mid_l=1.1002))
    assert pos.sl_price == pytest.approx(advanced)
    # R stays measured from the ORIGINAL stop.
    assert pos.risk_px() == pytest.approx(abs(pos.entry_price - pos.initial_sl_price))
