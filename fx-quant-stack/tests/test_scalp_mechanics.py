"""Tests for the post-blind-run mechanics: aggregation, exits, economics,
families, and measured costs.

Every test here pins a MECHANISM (does the machinery do what it claims),
never an OUTCOME (does the strategy make money). A test that asserted
profitability would be the exact pressure that bends a backtest into lying.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from fxstack.scalp.backtest import BacktestRunner, BtBar
from fxstack.scalp.bars import M1Bar, aggregate_bars, window_is_complete
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.costs import measure_from_bars, venue_pad_bps
from fxstack.scalp.families import evaluate_signal
from fxstack.scalp.shadow import ShadowBook
from fxstack.scalp.sizing import SizedIntent
from fxstack.scalp.signals import ScalpIntent

T0 = 1_753_999_200


def _cfg(**over) -> ScalpConfig:
    cfg = ScalpConfig()
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _bar(minute: int, *, o=1.1000, h=None, lo=None, c=None, spread=0.0001,
         valid=True, symbol="EURUSD") -> M1Bar:
    h = h if h is not None else max(o, c if c is not None else o) + 0.0001
    lo = lo if lo is not None else min(o, c if c is not None else o) - 0.0001
    c = c if c is not None else o
    half = spread / 2.0
    return M1Bar(
        symbol=symbol, minute_epoch=minute, open=o, high=h, low=lo, close=c,
        bid_close=c - half, ask_close=c + half,
        spread_max_bps=spread / c * 1e4, spread_close_bps=spread / c * 1e4,
        tick_count=4, valid=valid, invalid_reason="" if valid else "frozen_quotes",
        quote_changes=3 if valid else 0,
    )


def _bt(bar: M1Bar) -> BtBar:
    half = (bar.ask_close - bar.bid_close) / 2.0
    return BtBar(
        bar=bar, bid_open=bar.open - half, bid_high=bar.high - half,
        bid_low=bar.low - half, ask_open=bar.open + half,
        ask_high=bar.high + half, ask_low=bar.low + half,
    )


# ------------------------------------------------------------- aggregation


def test_window_completion_is_hour_aligned():
    # M15 windows must close at :14, :29, :44, :59 so session boundaries and
    # the opening-range family line up with real clock hours.
    base = int(dt.datetime(2026, 3, 2, 7, 0, tzinfo=dt.timezone.utc).timestamp())
    closes = [
        m for m in range(0, 60)
        if window_is_complete(base + m * 60, bar_minutes=15)
    ]
    assert closes == [14, 29, 44, 59]
    assert all(window_is_complete(base + m * 60, bar_minutes=1) for m in range(5))


def test_aggregate_folds_ohlc_and_keeps_worst_spread():
    bars = [
        _bar(T0 + 0, o=1.1000, h=1.1004, lo=1.0999, c=1.1002, spread=0.0001),
        _bar(T0 + 60, o=1.1002, h=1.1003, lo=1.0995, c=1.0997, spread=0.0003),
        _bar(T0 + 120, o=1.0997, h=1.1001, lo=1.0996, c=1.1001, spread=0.0002),
    ]
    out = aggregate_bars(bars, bar_minutes=3)
    assert len(out) == 1
    agg = out[0]
    assert agg.open == pytest.approx(1.1000)
    assert agg.close == pytest.approx(1.1001)
    assert agg.high == pytest.approx(1.1004)
    assert agg.low == pytest.approx(1.0995)
    # Worst in-window spread survives: cost gates must see the adversity.
    assert agg.spread_max_bps == pytest.approx(max(b.spread_max_bps for b in bars))
    assert agg.tick_count == 12


def test_aggregate_drops_windows_that_were_not_fully_observed():
    # One invalid minute poisons the whole window -- never stitch partials.
    bars = [_bar(T0, c=1.1001), _bar(T0 + 60, valid=False), _bar(T0 + 120, c=1.1002)]
    assert aggregate_bars(bars, bar_minutes=3) == []
    # A short window (missing minutes) is dropped too.
    assert aggregate_bars([_bar(T0), _bar(T0 + 60)], bar_minutes=3) == []


def test_engine_timeframe_decides_only_on_closed_windows():
    cfg = _cfg(bar_minutes=5, min_history_bars=5, signal_family="dislocation")
    runner = BacktestRunner(config=cfg)
    base = int(dt.datetime(2026, 3, 2, 8, 0, tzinfo=dt.timezone.utc).timestamp())
    for i in range(20):
        runner.process(_bt(_bar(base + i * 60, o=1.1000 + i * 1e-5, c=1.1000 + i * 1e-5)))
    # 20 M1 bars at M5 => 4 engine bars, so at most 4 decisions were made.
    decisions = sum(
        v for k, v in runner.stats.reasons.items() if k != "engine_window_incomplete"
    )
    assert decisions <= 4
    assert len(runner._run) <= 4


# ------------------------------------------------------------------- exits


def _intent(side="BUY", entry=1.10005, stop_bps=5.0) -> ScalpIntent:
    mid = 1.1000
    stop_px = stop_bps / 1e4 * mid
    tp_px = 7.5 / 1e4 * mid
    sl = entry - stop_px if side == "BUY" else entry + stop_px
    tp = entry + tp_px if side == "BUY" else entry - tp_px
    return ScalpIntent(
        symbol="EURUSD", side=side, minute_epoch=T0, ref_mid=mid, entry_price=entry,
        sl_price=sl, tp_price=tp, atr_bps=5.0, stop_bps=stop_bps, disp_z=-2.2,
        spread_bps=0.9, p_star=0.5, time_stop_bars=20,
    )


def test_shadow_breakeven_moves_stop_and_books_zero_not_minus_one():
    book = ShadowBook(max_concurrent=2, breakeven_at_r=0.5)
    pos = book.open_from(SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01,
                                     money_at_risk=50.0, sizeable=True))
    risk = pos.risk_px()
    # Move +0.6R in favor ON THE BID (exit side) -> breakeven arms.
    good_bid = pos.entry_price + 0.6 * risk
    assert book.on_tick(symbol="EURUSD", bid=good_bid, ask=good_bid + 0.00005,
                        day_key="2026-08-01") is None
    assert pos.breakeven_armed and pos.sl_price == pytest.approx(pos.entry_price)
    # Now it retraces to entry: booked at ~0R, not -1R.
    fill = book.on_tick(symbol="EURUSD", bid=pos.entry_price - 1e-9,
                        ask=pos.entry_price + 0.00005, day_key="2026-08-01")
    assert fill is not None
    assert fill.exit_reason == "breakeven"
    # ~0R (the fill takes the observed through-price, a hair below entry) --
    # the point is that it is NOT the -1R this trade would have booked.
    assert fill.pnl_r == pytest.approx(0.0, abs=1e-3)


def test_breakeven_does_not_arm_on_unrealized_mid_profit():
    # Favorable excursion is measured on the EXIT side: a trade that looks
    # green on mid but not on the bid must NOT move its stop.
    book = ShadowBook(max_concurrent=2, breakeven_at_r=0.5)
    pos = book.open_from(SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01,
                                     money_at_risk=50.0, sizeable=True))
    risk = pos.risk_px()
    bid = pos.entry_price + 0.49 * risk
    book.on_tick(symbol="EURUSD", bid=bid, ask=bid + 0.00005, day_key="2026-08-01")
    assert not pos.breakeven_armed
    assert pos.sl_price < pos.entry_price


def test_r_unit_stays_pinned_to_the_original_stop():
    # After the stop moves, R must still be measured from the ORIGINAL risk;
    # otherwise a breakeven exit divides by zero and ratios explode.
    book = ShadowBook(max_concurrent=2, breakeven_at_r=0.5)
    pos = book.open_from(SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01,
                                     money_at_risk=50.0, sizeable=True))
    original_risk = pos.risk_px()
    good_bid = pos.entry_price + 0.9 * original_risk
    book.on_tick(symbol="EURUSD", bid=good_bid, ask=good_bid + 0.00005,
                 day_key="2026-08-01")
    assert pos.sl_price == pytest.approx(pos.entry_price)
    assert pos.risk_px() == pytest.approx(original_risk)


def test_shadow_trailing_stop_tightens_from_executable_quote():
    book = ShadowBook(max_concurrent=2, trail_atr_mult=1.0)
    intent = _intent()
    # Keep the target beyond the quote used to advance the trail.
    intent.tp_price = intent.entry_price + 0.0030
    pos = book.open_from(
        SizedIntent(
            intent=intent,
            lots=0.1,
            risk_fraction=0.01,
            money_at_risk=50.0,
            sizeable=True,
        )
    )
    original_stop = pos.sl_price
    favorable_bid = pos.entry_price + 0.0007
    assert book.on_tick(
        symbol="EURUSD",
        bid=favorable_bid,
        ask=favorable_bid + 0.00005,
        day_key="2026-08-01",
    ) is None
    trail_distance = intent.atr_bps / 1e4 * pos.entry_price
    assert pos.sl_price == pytest.approx(favorable_bid - trail_distance)
    assert pos.sl_price > original_stop


def test_shadow_time_stop_counts_engine_closes_only():
    book = ShadowBook(max_concurrent=2)
    intent = _intent()
    intent.time_stop_bars = 2
    intent.tp_price = intent.entry_price + 0.0030
    pos = book.open_from(
        SizedIntent(
            intent=intent,
            lots=0.1,
            risk_fraction=0.01,
            money_at_risk=50.0,
            sizeable=True,
        )
    )
    for offset in (60, 120, 180):
        assert book.on_bar_close(
            symbol="EURUSD",
            bid_close=pos.entry_price,
            ask_close=pos.entry_price + 0.00005,
            day_key="2026-08-01",
            minute_epoch=T0 + offset,
            engine_close=False,
        ) is None
    assert pos.bars_held == 0
    assert book.on_bar_close(
        symbol="EURUSD",
        bid_close=pos.entry_price,
        ask_close=pos.entry_price + 0.00005,
        day_key="2026-08-01",
        minute_epoch=T0 + 240,
        engine_close=True,
    ) is None
    fill = book.on_bar_close(
        symbol="EURUSD",
        bid_close=pos.entry_price,
        ask_close=pos.entry_price + 0.00005,
        day_key="2026-08-01",
        minute_epoch=T0 + 300,
        engine_close=True,
    )
    assert fill is not None and fill.exit_reason == "time_stop"
    assert fill.bars_held == 2


def test_backtest_breakeven_matches_shadow_semantics():
    cfg = _cfg(breakeven_at_r=0.5, min_history_bars=5, cooldown_bars=0)
    runner = BacktestRunner(config=cfg)
    runner._pending = _intent()
    runner._pending_fill_minute = T0 + 60
    runner.process(_bt(_bar(T0 + 60)))
    pos = runner._pos
    assert pos is not None
    risk = pos.risk_px()
    # Bar runs +0.7R in favor without touching TP -> arms breakeven.
    good = pos.entry_price + 0.7 * risk
    runner.process(_bt(_bar(T0 + 120, o=pos.entry_price, h=good + 0.00005,
                            lo=pos.entry_price - 1e-6, c=good)))
    assert pos.breakeven_armed
    assert runner.stats.reasons.get("breakeven_armed") == 1
    # Retrace to entry books ~0R under the "breakeven" label.
    runner.process(_bt(_bar(T0 + 180, o=good, h=good, lo=pos.entry_price - 0.00002,
                            c=pos.entry_price)))
    fill = runner.stats.fills[-1]
    assert fill.exit_reason == "breakeven"
    assert fill.pnl_r == pytest.approx(0.0, abs=1e-3)


# --------------------------------------------------------------- economics


def test_min_tp_cost_ratio_refuses_thin_gross():
    from fxstack.scalp.families import build_intent

    last = _bar(T0, c=1.1000)
    # TP 3bps vs 1.2bps cost = 2.5x; a 4x floor must refuse it even though
    # p* alone would pass with a wide stop.
    intent, reason = build_intent(
        last=last, side="BUY", stop_bps=10.0, tp_bps=3.0, spread_bps=1.2,
        config=_cfg(min_tp_cost_ratio=4.0, min_stop_bps=1.0, p_star_max=0.95),
        atr=5.0, signal_strength=2.0,
    )
    assert intent is None and reason == "gross_too_small_vs_cost"
    # Same geometry with the gate off is admitted (gate is the only difference).
    ok, _ = build_intent(
        last=last, side="BUY", stop_bps=10.0, tp_bps=3.0, spread_bps=1.2,
        config=_cfg(min_tp_cost_ratio=0.0, min_stop_bps=1.0, p_star_max=0.95),
        atr=5.0, signal_strength=2.0,
    )
    assert ok is not None


def test_p_star_gate_still_binds_for_every_family():
    from fxstack.scalp.families import build_intent

    intent, reason = build_intent(
        last=_bar(T0, c=1.1000), side="BUY", stop_bps=5.0, tp_bps=2.0,
        spread_bps=3.0, config=_cfg(), atr=5.0, signal_strength=1.0,
    )
    assert intent is None and reason == "bracket_cost_dead"


# ---------------------------------------------------------------- families


def _session_bars(n: int, *, start_hour: int = 7, closes: list[float] | None = None):
    base = int(dt.datetime(2026, 3, 2, start_hour, 0, tzinfo=dt.timezone.utc).timestamp())
    bars = []
    px = 1.1000
    for i in range(n):
        px = closes[i] if closes else px + (1e-5 if i % 2 else -1e-5)
        bars.append(_bar(base + i * 60, o=px - 1e-5, c=px, spread=0.00012))
    return bars


def test_opening_range_waits_for_the_range_then_breaks_out():
    cfg = _cfg(signal_family="opening_range", min_history_bars=10, or_bars=15,
               or_valid_bars=45, or_buffer_frac=0.05, min_stop_bps=1.0,
               p_star_max=0.95, tp_atr_mult=3.0)
    forming = _session_bars(12)
    intent, reason = evaluate_signal(bars=forming, config=cfg, spread_bps=1.2)
    assert intent is None and reason in {"opening_range_forming", "insufficient_valid_history"}

    # 15 range bars, then a decisive break above the range high.
    closes = [1.1000 + (1e-5 if i % 2 else -1e-5) for i in range(15)]
    closes += [1.1006, 1.1008, 1.1010]
    bars = _session_bars(18, closes=closes)
    intent, reason = evaluate_signal(bars=bars, config=cfg, spread_bps=1.2)
    assert intent is not None, reason
    assert intent.side == "BUY"
    assert intent.sl_price < intent.entry_price < intent.tp_price


def test_opening_range_risks_a_fraction_of_the_range_not_all_of_it():
    """The bracket must be scaled to the RANGE, both sides.

    Risking the full range for an ATR-sized target put p* near 1 and refused
    99.5% of detected breakouts -- geometry incoherent with the hypothesis.
    """
    cfg = _cfg(signal_family="opening_range", min_history_bars=10, or_bars=15,
               or_valid_bars=45, or_buffer_frac=0.05, min_stop_bps=0.1,
               p_star_max=0.95, or_stop_range_frac=0.5, or_tp_range_mult=1.0)
    closes = [1.1000 + (1e-4 if i % 2 else -1e-4) for i in range(15)]
    closes += [1.1006, 1.1008, 1.1010]
    bars = _session_bars(18, closes=closes)
    intent, reason = evaluate_signal(bars=bars, config=cfg, spread_bps=1.2)
    assert intent is not None, reason
    hi = max(b.high for b in bars[:15])
    lo = min(b.low for b in bars[:15])
    range_bps = (hi - lo) / bars[-1].close * 1e4
    assert intent.stop_bps == pytest.approx(0.5 * range_bps, rel=1e-6)
    reward_bps = abs(intent.tp_price - intent.entry_price) / intent.entry_price * 1e4
    assert reward_bps == pytest.approx(range_bps, rel=1e-3)
    # Reward >= risk means p* is reachable rather than ~1 by construction.
    assert intent.p_star < 0.7


def test_opening_range_ignores_moves_inside_the_range():
    cfg = _cfg(signal_family="opening_range", min_history_bars=10, or_bars=15,
               or_valid_bars=45, min_stop_bps=1.0, p_star_max=0.95)
    closes = [1.1000 + (2e-5 if i % 2 else -2e-5) for i in range(15)]
    closes += [1.1000, 1.10001, 1.10002]  # still well inside
    bars = _session_bars(18, closes=closes)
    intent, reason = evaluate_signal(bars=bars, config=cfg, spread_bps=1.2)
    assert intent is None and reason == "inside_opening_range"


def test_opening_range_window_expires():
    cfg = _cfg(signal_family="opening_range", min_history_bars=10, or_bars=5,
               or_valid_bars=8, min_stop_bps=1.0, p_star_max=0.95)
    closes = [1.1000 + (1e-5 if i % 2 else -1e-5) for i in range(5)]
    closes += [1.1000] * 4 + [1.1015] * 3  # breakout arrives too late
    bars = _session_bars(12, closes=closes)
    intent, reason = evaluate_signal(bars=bars, config=cfg, spread_bps=1.2)
    assert intent is None and reason == "opening_range_window_closed"


def test_unknown_family_refuses_rather_than_defaulting():
    intent, reason = evaluate_signal(
        bars=_session_bars(40), config=_cfg(signal_family="wishful"), spread_bps=1.0
    )
    assert intent is None and reason.startswith("unknown_signal_family")


# --------------------------------------------------------------- edge math


def test_skill_requirement_arithmetic_and_cost_dilution():
    from fxstack.scalp.edge_math import skill_requirement

    # Reward:risk 1.5 -> a driftless path wins 40% of the time.
    tight = skill_requirement(symbol="EURUSD", stop_bps=4.0, target_bps=6.0, cost_bps=1.2)
    assert tight.zero_skill_win_rate == pytest.approx(0.4)
    assert tight.breakeven_win_rate == pytest.approx((4.0 + 1.2) / 10.0)
    assert tight.skill_gap_pp == pytest.approx(12.0)
    assert tight.cost_drag_r == pytest.approx(0.3)
    assert not tight.plausible

    # Widening the bracket does NOT create edge -- it dilutes cost. The
    # zero-skill rate is unchanged; only the gap shrinks.
    wide = skill_requirement(symbol="EURUSD", stop_bps=16.0, target_bps=24.0, cost_bps=1.2)
    assert wide.zero_skill_win_rate == pytest.approx(tight.zero_skill_win_rate)
    assert wide.skill_gap_pp == pytest.approx(3.0)
    assert wide.cost_drag_r == pytest.approx(0.075)
    assert wide.plausible


def test_zero_cost_needs_no_skill_at_all():
    from fxstack.scalp.edge_math import skill_requirement

    free = skill_requirement(symbol="EURUSD", stop_bps=5.0, target_bps=10.0, cost_bps=0.0)
    assert free.skill_gap_pp == pytest.approx(0.0)
    assert free.breakeven_win_rate == pytest.approx(free.zero_skill_win_rate)


# ------------------------------------------------------------------- costs


def test_measured_costs_from_live_bars_and_unmeasured_fails_closed(tmp_path: Path):
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    rows = []
    for i in range(250):
        bar = _bar(T0 + i * 60, c=1.1000, spread=0.00013 if i % 2 else 0.00011)
        rows.append(json.dumps(bar.to_dict()))
    (bars_dir / "EURUSD_M1.jsonl").write_text("\n".join(rows), encoding="utf-8")
    # Too few samples to price anything.
    (bars_dir / "GBPUSD_M1.jsonl").write_text(rows[0], encoding="utf-8")

    table = measure_from_bars(bars_dir)
    assert table["EURUSD"]["measured"] is True
    assert table["EURUSD"]["p75_bps"] >= table["EURUSD"]["median_bps"]
    assert table["GBPUSD"]["measured"] is False

    pad, why = venue_pad_bps(table, symbol="EURUSD", interbank_bps=0.3)
    assert why == "" and pad > 0.0
    # An unmeasured pair must refuse, never quietly return a zero pad.
    pad, why = venue_pad_bps(table, symbol="GBPUSD", interbank_bps=0.3)
    assert why == "unmeasured"
    pad, why = venue_pad_bps(table, symbol="NZDUSD", interbank_bps=0.3)
    assert why == "unmeasured"


def test_hour_conditional_pad_prices_the_hour_actually_traded():
    from fxstack.scalp.costs import worst_hour_pad_bps

    table = {
        "EURUSD": {
            "p75_bps": 1.0, "measured": True,
            # The open hour is twice as expensive as the quiet average.
            "by_hour_p75_bps": {"3": 0.8, "7": 2.4, "13": 2.0},
        }
    }
    quiet, _ = venue_pad_bps(table, symbol="EURUSD", interbank_bps=0.3, hour_utc=3)
    open_hour, _ = venue_pad_bps(table, symbol="EURUSD", interbank_bps=0.3, hour_utc=7)
    assert open_hour > quiet
    assert open_hour == pytest.approx(2.1)
    # A single-number pad must take the WORST measured hour, never the mean.
    worst, why = worst_hour_pad_bps(table, symbol="EURUSD", interbank_bps=0.3)
    assert why == "" and worst == pytest.approx(2.1)
    # Unmeasured hour falls back to the daily basis, never to zero.
    fallback, _ = venue_pad_bps(table, symbol="EURUSD", interbank_bps=0.3, hour_utc=22)
    assert fallback == pytest.approx(0.7)


def test_pad_subtracts_what_the_source_data_already_charges():
    table = {"EURUSD": {"p75_bps": 1.4, "measured": True}}
    pad, why = venue_pad_bps(table, symbol="EURUSD", interbank_bps=0.3)
    assert why == "" and pad == pytest.approx(1.1)
    # A source already at venue cost needs no pad -- never a negative one.
    pad, _ = venue_pad_bps(table, symbol="EURUSD", interbank_bps=2.0)
    assert pad == 0.0
