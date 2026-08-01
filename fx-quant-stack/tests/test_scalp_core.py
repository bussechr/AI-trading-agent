"""Tests for the scalper core (fxstack.scalp) -- the rebuild's seed.

Pin the properties that make the shadow ledger trustworthy: bars never bridge
feed gaps, the signal refuses cost-dead geometry, gates fail closed, sizing
refuses what it cannot honestly size, and shadow fills always pay the spread.
"""

from __future__ import annotations

import datetime as dt

from fxstack.scalp.bars import M1Aggregator, atr_bps
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.gates import SpreadSentinel, session_veto_reason
from fxstack.scalp.shadow import ShadowBook
from fxstack.scalp.signals import evaluate_dislocation
from fxstack.scalp.sizing import size_intent


def _cfg(**overrides) -> ScalpConfig:
    cfg = ScalpConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _feed_bar(
    agg: M1Aggregator,
    *,
    symbol: str,
    minute: int,
    mid: float,
    spread_bps: float = 1.0,
    ticks: int = 4,
    close_mid: float | None = None,
) -> None:
    """Feed one minute of synthetic ticks (open==mid, close==close_mid or mid)."""
    close = close_mid if close_mid is not None else mid
    half = mid * spread_bps / 1e4 / 2.0
    for i in range(ticks):
        px = mid if i < ticks - 1 else close
        agg.ingest_tick(
            symbol=symbol,
            bid=px - half,
            ask=px + half,
            spread_bps=spread_bps,
            ts_epoch=minute * 60 + i * 10,
        )


# ----------------------------------------------------------------- aggregator


def test_bars_finalize_on_rollover_and_mark_gaps():
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=3)
    _feed_bar(agg, symbol="EURUSD", minute=100, mid=1.1000)
    # Skip minutes 101 and 102 entirely, then tick in minute 103.
    out = agg.ingest_tick(
        symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=103 * 60
    )
    kinds = [(b.minute_epoch // 60, b.valid, b.invalid_reason) for b in out]
    assert kinds == [
        (100, True, ""),
        (101, False, "no_ticks_in_minute"),
        (102, False, "no_ticks_in_minute"),
    ]
    # The gap breaks the consecutive-valid run.
    assert agg.consecutive_valid("EURUSD") == []


def test_too_few_ticks_invalidates_bar():
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=3)
    _feed_bar(agg, symbol="EURUSD", minute=100, mid=1.1, ticks=2)
    out = agg.ingest_tick(
        symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=101 * 60
    )
    assert out[0].valid is False and out[0].invalid_reason == "too_few_ticks"


def test_flush_stale_finalizes_without_new_ticks():
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=3)
    _feed_bar(agg, symbol="EURUSD", minute=100, mid=1.1)
    out = agg.flush_stale(now_epoch=101 * 60 + 5)
    assert [b.minute_epoch // 60 for b in out] == [100]
    assert agg.consecutive_valid("EURUSD")[-1].minute_epoch == 100 * 60


# --------------------------------------------------------------------- signal


def _dislocated_run(cfg: ScalpConfig, *, direction: float = +1.0):
    """Valid history around 1.1000, last bar dislocated ``direction`` ATRs up
    (with a reverting close) so a SELL (direction>0) or BUY should propose."""
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=2)
    minute = 1000
    for i in range(cfg.min_history_bars + 5):
        wiggle = 0.0001 if i % 2 else -0.0001  # ~0.9 bps TR -> nonzero ATR
        _feed_bar(agg, symbol="EURUSD", minute=minute + i, mid=1.1000 + wiggle)
    # Dislocated bar: 40 bps from the mean, closing back toward it.
    last_minute = minute + cfg.min_history_bars + 5
    disp = direction * 0.0044
    _feed_bar(
        agg,
        symbol="EURUSD",
        minute=last_minute,
        mid=1.1000 + disp,
        close_mid=1.1000 + disp * 0.9,  # reverting close
    )
    agg.flush_stale(now_epoch=(last_minute + 1) * 60 + 1)
    return agg.consecutive_valid("EURUSD")


def test_dislocation_proposes_reversion_with_viable_bracket():
    cfg = _cfg(min_history_bars=20, z_entry=2.0, p_star_max=0.60)
    run = _dislocated_run(cfg, direction=+1.0)
    intent, reason = evaluate_dislocation(bars=run, config=cfg, spread_bps=0.6)
    assert reason == "" and intent is not None
    assert intent.side == "SELL"  # fade the upward dislocation
    assert intent.sl_price > intent.entry_price > intent.tp_price
    assert 0.0 < intent.p_star <= 0.60
    # Bracket respects the min-stop floor.
    assert intent.stop_bps >= cfg.min_stop_bps


def test_cost_dead_bracket_is_refused_at_signal_layer():
    cfg = _cfg(min_history_bars=20, z_entry=2.0, p_star_max=0.55)
    run = _dislocated_run(cfg)
    intent, reason = evaluate_dislocation(bars=run, config=cfg, spread_bps=60.0)
    assert intent is None and reason == "bracket_cost_dead"


def test_no_dislocation_means_no_signal():
    cfg = _cfg(min_history_bars=20)
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=2)
    for i in range(30):
        _feed_bar(agg, symbol="EURUSD", minute=2000 + i, mid=1.1 + (0.0001 if i % 2 else 0))
    # Flush immediately after the last bar's minute ends -- flushing later
    # would (correctly) insert a no_ticks_in_minute gap marker.
    agg.flush_stale(now_epoch=(2000 + 30) * 60 + 1)
    intent, reason = evaluate_dislocation(
        bars=agg.consecutive_valid("EURUSD"), config=cfg, spread_bps=0.6
    )
    assert intent is None and reason == "no_dislocation"


# ---------------------------------------------------------------------- gates


def test_sentinel_vetoes_over_budget_and_stale():
    cfg = _cfg()
    sentinel = SpreadSentinel(cfg)
    sentinel.observe(symbol="EURUSD", spread_bps=5.0, ts_epoch=1000.0)  # budget 1.2
    assert sentinel.veto_reason(symbol="EURUSD", now_epoch=1001.0) == "spread_over_budget"
    sentinel.observe(symbol="EURUSD", spread_bps=0.8, ts_epoch=1002.0)
    assert sentinel.veto_reason(symbol="EURUSD", now_epoch=1003.0) == ""
    assert (
        sentinel.veto_reason(symbol="EURUSD", now_epoch=1002.0 + cfg.tick_stale_secs + 1)
        == "tick_stale"
    )


def test_session_router_weekend_rollover_and_crypto():
    cfg = _cfg()
    saturday = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    assert session_veto_reason(symbol="EURUSD", now_epoch=saturday, config=cfg) == "fx_weekend_closed"
    assert session_veto_reason(symbol="BTCUSD", now_epoch=saturday, config=cfg) == ""
    rollover = dt.datetime(2026, 8, 3, 21, 0, tzinfo=dt.timezone.utc).timestamp()
    assert session_veto_reason(symbol="EURUSD", now_epoch=rollover, config=cfg) == "rollover_window"
    tokyo_only = dt.datetime(2026, 8, 3, 3, 0, tzinfo=dt.timezone.utc).timestamp()
    assert (
        session_veto_reason(symbol="GBPUSD", now_epoch=tokyo_only, config=cfg)
        == "outside_liquid_session"
    )


# --------------------------------------------------------------------- sizing


def _intent(symbol: str = "EURUSD"):
    from fxstack.scalp.signals import ScalpIntent

    return ScalpIntent(
        symbol=symbol,
        side="BUY",
        minute_epoch=0,
        ref_mid=1.1000,
        entry_price=1.10005,
        sl_price=1.09955,  # 5 pips
        tp_price=1.10080,
        atr_bps=5.0,
        stop_bps=4.5,
        disp_z=-2.2,
        spread_bps=0.8,
        p_star=0.5,
        time_stop_bars=20,
    )


def test_fx_sizing_uses_fail_closed_sizer():
    sized = size_intent(intent=_intent(), equity=10_000.0, config=_cfg(risk_fraction=0.01))
    assert sized.sizeable and sized.lots > 0
    # Money at risk never exceeds the budget (sizer rounds DOWN).
    assert sized.money_at_risk <= 10_000.0 * 0.01 + 1e-6


def test_crypto_is_honestly_unsizeable():
    sized = size_intent(intent=_intent("BTCUSD"), equity=10_000.0, config=_cfg())
    assert not sized.sizeable and sized.lots == 0.0
    assert sized.reason == "contract_size_unknown"


def test_zero_equity_refuses_sizing():
    sized = size_intent(intent=_intent(), equity=0.0, config=_cfg())
    assert not sized.sizeable and sized.reason == "equity_unattested"


# --------------------------------------------------------------------- shadow


def test_shadow_fills_pay_the_spread_and_track_r():
    from fxstack.scalp.sizing import SizedIntent

    book = ShadowBook(max_concurrent=4)
    sized = SizedIntent(
        intent=_intent(), lots=0.22, risk_fraction=0.01, money_at_risk=100.0, sizeable=True
    )
    book.open_from(sized)
    # Long entered at the ASK; a bid rally through TP exits at the BID.
    fill = book.on_tick(symbol="EURUSD", bid=1.10081, ask=1.10091, day_key="d")
    assert fill is not None and fill.exit_reason == "tp"
    assert fill.pnl_r > 0
    # Entry was at ask (1.10005), exit at bid: both touches paid.
    assert fill.entry_price == 1.10005 and fill.exit_price == 1.10081
    assert book.day_r == fill.pnl_r


def test_shadow_stop_and_time_stop():
    from fxstack.scalp.sizing import SizedIntent

    book = ShadowBook(max_concurrent=4)
    book.open_from(
        SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01, money_at_risk=50.0, sizeable=True)
    )
    fill = book.on_tick(symbol="EURUSD", bid=1.09950, ask=1.09960, day_key="d")
    assert fill is not None and fill.exit_reason == "sl"
    assert fill.pnl_r < 0

    book2 = ShadowBook(max_concurrent=4)
    book2.open_from(
        SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01, money_at_risk=50.0, sizeable=True)
    )
    fill2 = None
    for _ in range(25):
        fill2 = book2.on_bar_close(symbol="EURUSD", bid_close=1.10010, ask_close=1.10020, day_key="d")
        if fill2:
            break
    assert fill2 is not None and fill2.exit_reason == "time_stop"


def test_daily_breaker_resets_on_new_day():
    book = ShadowBook(max_concurrent=4)
    book.day_r = -3.5
    book._roll_day("20260802")
    assert book.day_r == 0.0


# --------------------------------------------------------------- config guard


def test_live_mode_is_refused_until_it_exists():
    cfg = _cfg(mode="live")
    assert any("not implemented" in e for e in cfg.validate())


def test_unqualified_symbol_is_refused():
    cfg = _cfg(symbols=["EURUSD", "USDMXN"])
    assert any("USDMXN" in e for e in cfg.validate())
