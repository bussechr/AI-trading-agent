"""Tests for the scalper core (fxstack.scalp) -- the rebuild's seed.

Pin the properties that make the shadow ledger trustworthy: bars never bridge
feed gaps, the signal refuses cost-dead geometry, gates fail closed, sizing
refuses what it cannot honestly size, and shadow fills always pay the spread.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fxstack.scalp.bars import M1Aggregator
from fxstack.scalp.config import (
    CONFIGURED_CRYPTO_SYMBOLS,
    CONFIGURED_FX_SYMBOLS,
    CONFIGURED_SYMBOLS,
    DEFAULT_SESSION_WINDOWS_UTC,
    DEFAULT_SPREAD_BUDGETS_BPS,
    ScalpConfig,
)
from fxstack.scalp.gates import CRYPTO_SYMBOLS, SpreadSentinel, session_veto_reason
from fxstack.scalp.panel import PAIR_LEGS
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
    """Feed one minute of synthetic ticks (open==mid, close==close_mid or a
    tenth-pip drift so the bar contains real quote movement)."""
    close = close_mid if close_mid is not None else mid + 1e-05
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
    cfg = _cfg(min_history_bars=20, z_entry=2.0, p_star_max=0.80)
    run = _dislocated_run(cfg, direction=+1.0)
    intent, reason = evaluate_dislocation(bars=run, config=cfg, spread_bps=0.6)
    assert reason == "" and intent is not None
    assert intent.side == "SELL"  # fade the upward dislocation
    assert intent.sl_price > intent.entry_price > intent.tp_price
    assert 0.0 < intent.p_star <= cfg.p_star_max
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


def test_sentinel_and_backtest_share_the_scaled_spread_budget():
    cfg = _cfg()
    cfg.spread_budget_scale = 0.5
    assert cfg.budget_for("EURUSD") == pytest.approx(0.6)
    sentinel = SpreadSentinel(cfg)
    sentinel.observe(symbol="EURUSD", spread_bps=0.8, ts_epoch=1000.0)
    assert sentinel.veto_reason(symbol="EURUSD", now_epoch=1001.0) == "spread_over_budget"


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


def _btc_intent():
    from fxstack.scalp.signals import ScalpIntent

    return ScalpIntent(
        symbol="BTCUSD",
        side="BUY",
        minute_epoch=0,
        ref_mid=60_000.0,
        entry_price=60_010.0,
        sl_price=59_710.0,  # 300 USD stop
        tp_price=60_460.0,
        atr_bps=30.0,
        stop_bps=50.0,
        disp_z=-2.2,
        spread_bps=5.0,
        p_star=0.5,
        time_stop_bars=20,
    )


def test_broker_spec_makes_crypto_sizeable_with_real_contract():
    # IG crypto CFD: 1 unit per lot. $100 risk / $300 stop = 0.33 lots.
    specs = {"BTCUSD": {"lot_size": 1.0, "min_lot": 0.01, "lot_step": 0.01}}
    sized = size_intent(
        intent=_btc_intent(), equity=10_000.0, config=_cfg(risk_fraction=0.01),
        specs=specs,
    )
    assert sized.sizeable, sized.reason
    assert abs(sized.lots - 0.33) < 1e-9
    assert sized.money_at_risk <= 100.0 + 1e-6
    # Without the spec the same intent stays honestly unsizeable.
    bare = size_intent(intent=_btc_intent(), equity=10_000.0, config=_cfg())
    assert not bare.sizeable and bare.reason == "contract_size_unknown"


def test_broker_spec_enforces_min_stop_distance():
    # Broker demands 500 points * 0.01 = 5.0 price units; intent has a 300
    # unit stop -> geometry is refused, never silently widened.
    specs = {"BTCUSD": {"lot_size": 1.0, "point": 0.01, "stop_level_points": 50_000.0}}
    sized = size_intent(
        intent=_btc_intent(), equity=10_000.0, config=_cfg(), specs=specs
    )
    assert not sized.sizeable
    assert sized.reason == "stop_below_broker_minimum"


def test_broker_spec_margin_caps_lots_visibly():
    # Margin allows only 0.1 lots at 25% utilization: 10k * 0.25 / 25k = 0.1.
    specs = {"BTCUSD": {"lot_size": 1.0, "margin_required": 25_000.0}}
    sized = size_intent(
        intent=_btc_intent(), equity=10_000.0, config=_cfg(risk_fraction=0.01),
        specs=specs,
    )
    assert sized.sizeable and sized.margin_capped
    assert abs(sized.lots - 0.10) < 1e-9
    assert sized.money_at_risk < 100.0  # risk shrank with the clip, honestly


def test_broker_spec_margin_refuses_below_min_lot():
    specs = {
        "BTCUSD": {"lot_size": 1.0, "min_lot": 0.5, "margin_required": 25_000.0}
    }
    sized = size_intent(
        intent=_btc_intent(), equity=10_000.0, config=_cfg(risk_fraction=0.01),
        specs=specs,
    )
    assert not sized.sizeable
    # The verified sizer refuses at the broker's min lot before margin is
    # even consulted -- either refusal is honest, both carry a reason.
    assert sized.reason == "margin_infeasible" or sized.reason.startswith(
        "risk_budget_below_min_lot"
    )


def test_broker_spec_fx_matches_legacy_contract_math():
    # EURUSD spec with the standard 100k contract must agree with the
    # assumption path exactly -- the spec route is a refinement, not a fork.
    specs = {"EURUSD": {"lot_size": 100_000.0, "min_lot": 0.01, "lot_step": 0.01}}
    with_spec = size_intent(
        intent=_intent(), equity=10_000.0, config=_cfg(risk_fraction=0.01),
        specs=specs,
    )
    legacy = size_intent(
        intent=_intent(), equity=10_000.0, config=_cfg(risk_fraction=0.01)
    )
    assert with_spec.sizeable and legacy.sizeable
    assert abs(with_spec.lots - legacy.lots) < 1e-9


# --------------------------------------------------------------------- shadow


def test_shadow_tp_fills_at_the_level_never_the_overshoot():
    from fxstack.scalp.sizing import SizedIntent

    book = ShadowBook(max_concurrent=4)
    sized = SizedIntent(
        intent=_intent(), lots=0.22, risk_fraction=0.01, money_at_risk=100.0, sizeable=True
    )
    book.open_from(sized)
    # Long entered at the ASK; a bid rally THROUGH TP fills at the TP level --
    # crediting the observed overshoot would flatter every winner.
    fill = book.on_tick(symbol="EURUSD", bid=1.10095, ask=1.10105, day_key="d")
    assert fill is not None and fill.exit_reason == "tp"
    assert fill.entry_price == 1.10005 and fill.exit_price == 1.10080  # == tp_price
    assert fill.pnl_r > 0
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


def test_sl_exits_keep_the_through_price_against_the_book():
    """SL fills stay at the observed through-price (slippage against us),
    asymmetric to TP's fill-at-level -- the book never wins the ambiguity."""
    from fxstack.scalp.sizing import SizedIntent

    book = ShadowBook(max_concurrent=4)
    book.open_from(
        SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01, money_at_risk=50.0, sizeable=True)
    )
    # Bid gaps THROUGH the 1.09955 stop to 1.09940: fill at 1.09940, not the level.
    fill = book.on_tick(symbol="EURUSD", bid=1.09940, ask=1.09950, day_key="d")
    assert fill is not None and fill.exit_reason == "sl"
    assert fill.exit_price == 1.09940
    assert fill.pnl_r < -1.0  # worse than -1R: the slip is ours to keep


def test_bar_close_reconciles_missed_sl_wick_sl_first():
    """A wick that pierced the stop between 1s polls books the stop at bar
    close -- even though every sampled tick missed it and the bar closed back
    'safe'. Unknowable intrabar ordering must never resolve in our favor."""
    from fxstack.scalp.sizing import SizedIntent

    book = ShadowBook(max_concurrent=4)
    book.open_from(
        SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01, money_at_risk=50.0, sizeable=True)
    )
    fill = book.on_bar_close(
        symbol="EURUSD",
        bid_close=1.10000,
        ask_close=1.10010,  # closed comfortably above the stop...
        day_key="d",
        minute_epoch=60,  # after opened_minute=0
        high=1.10020,
        low=1.09950,  # ...but the mid low, minus half the worst spread, pierced it
        spread_max_bps=1.5,
    )
    assert fill is not None and fill.exit_reason == "sl_wick"
    assert fill.exit_price == 1.09955  # the stop level, not better
    assert fill.pnl_r < 0


def test_bar_predating_the_position_is_not_reconciled():
    from fxstack.scalp.sizing import SizedIntent

    book = ShadowBook(max_concurrent=4)
    book.open_from(
        SizedIntent(intent=_intent(), lots=0.1, risk_fraction=0.01, money_at_risk=50.0, sizeable=True)
    )
    # minute_epoch == opened_minute: this bar closed as the entry was made.
    fill = book.on_bar_close(
        symbol="EURUSD",
        bid_close=1.10000,
        ask_close=1.10010,
        day_key="d",
        minute_epoch=0,
        high=1.20000,
        low=1.00000,
        spread_max_bps=1.0,
    )
    assert fill is None
    assert book.positions["EURUSD"].bars_held == 0


# ------------------------------------------------- aggregator honesty (review)


def test_flush_then_resume_emits_gap_markers():
    """The critical review finding: silence spanning a flush boundary must
    break the consecutive-valid run when ticks resume."""
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=2)
    _feed_bar(agg, symbol="EURUSD", minute=100, mid=1.1000)
    agg.flush_stale(now_epoch=101 * 60 + 1)  # bar 100 finalized
    assert len(agg.consecutive_valid("EURUSD")) == 1
    # 30 minutes of silence, then ticks resume at minute 131.
    out = agg.ingest_tick(
        symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=131 * 60
    )
    assert out, "gap markers must be emitted on resume"
    assert all(not b.valid for b in out)
    assert any("no_ticks" in b.invalid_reason for b in out)
    # The run is broken: only bars after the gap can ever count again.
    assert agg.consecutive_valid("EURUSD") == []


def test_long_gaps_collapse_into_a_summary_marker():
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=2)
    _feed_bar(agg, symbol="EURUSD", minute=100, mid=1.1000)
    agg.flush_stale(now_epoch=101 * 60 + 1)
    out = agg.ingest_tick(  # ~16 hours later
        symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=1100 * 60
    )
    assert 1 <= len(out) <= 4  # capped markers + summary, not ~1000 rows
    assert any("no_ticks_gap_" in b.invalid_reason for b in out)


def test_duplicate_polls_of_the_same_tick_are_dropped():
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=3)
    for _ in range(10):  # same ts re-polled -> one tick, not ten
        agg.ingest_tick(
            symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=6000.0
        )
    agg.ingest_tick(symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=6060.0)
    bar = agg.history("EURUSD")[0]
    assert bar.tick_count == 1
    assert not bar.valid and bar.invalid_reason == "too_few_ticks"


def test_frozen_quotes_with_advancing_timestamps_are_invalid():
    """A stale feed re-broadcasting one quote with fresh timestamps must not
    manufacture valid bars (the Monday-morning stale-feed case)."""
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=3)
    for i in range(10):
        agg.ingest_tick(
            symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=6000.0 + i * 5
        )
    agg.ingest_tick(symbol="EURUSD", bid=1.0999, ask=1.1001, spread_bps=1.0, ts_epoch=6060.0)
    bar = agg.history("EURUSD")[0]
    assert bar.tick_count == 10
    assert not bar.valid and bar.invalid_reason == "frozen_quotes"


def test_late_ticks_for_finalized_minutes_never_rebuild_history():
    agg = M1Aggregator(symbols=["EURUSD"], min_ticks_per_bar=2)
    _feed_bar(agg, symbol="EURUSD", minute=100, mid=1.1000)
    agg.flush_stale(now_epoch=101 * 60 + 1)
    n_before = len(agg.history("EURUSD"))
    out = agg.ingest_tick(  # late tick for the already-finalized minute 100
        symbol="EURUSD", bid=1.1, ask=1.1002, spread_bps=1.0, ts_epoch=100 * 60 + 59
    )
    assert out == [] and len(agg.history("EURUSD")) == n_before


# ------------------------------------------------------ loop honesty (review)


def test_parse_tick_epoch_uses_bridge_fields_and_drops_unparseable():
    from fxstack.scalp.loop import parse_tick_epoch

    assert parse_tick_epoch({"ts_epoch": 1785600000.0}) == 1785600000.0
    expected = dt.datetime(2026, 8, 1, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    assert parse_tick_epoch({"time": "2026-08-01T12:00:00+00:00"}) == expected
    # No parseable timestamp -> DROP, never stamp with wall clock.
    assert parse_tick_epoch({}) is None
    assert parse_tick_epoch({"time": "not-a-time"}) is None


def _loop(tmp_path):
    from fxstack.scalp.loop import ScalpLoop

    cfg = _cfg()
    cfg.data_root = str(tmp_path / "scalp")
    cfg.api_key_file = str(tmp_path / "missing_key.txt")
    return ScalpLoop(cfg)


def test_cooldown_arms_on_every_fill_path(tmp_path):
    loop = _loop(tmp_path)
    from fxstack.scalp.shadow import ShadowFill

    fill = ShadowFill(
        symbol="EURUSD",
        side="BUY",
        entry_price=1.1,
        exit_price=1.099,
        exit_reason="sl",  # tick-path exit, the review's fail-open case
        bars_held=2,
        pnl_r=-1.0,
        pnl_bps=-9.0,
        lots=0.1,
    )
    now = 7_200.0
    loop._record_fill(fill, epoch=now)
    until = loop._cooldown_until_minute["EURUSD"]
    assert until == int(now // 60) * 60 + loop.config.cooldown_bars * 60


def test_loop_wires_configured_trailing_distance_into_shadow_book(tmp_path):
    from fxstack.scalp.loop import ScalpLoop

    cfg = _cfg(trail_atr_mult=0.75)
    cfg.data_root = str(tmp_path / "scalp")
    cfg.api_key_file = str(tmp_path / "missing_key.txt")
    loop = ScalpLoop(cfg)
    assert loop.book.trail_atr_mult == pytest.approx(0.75)


def test_freshen_entry_refuses_stale_quotes_and_reanchors(tmp_path):
    loop = _loop(tmp_path)
    intent = _intent()
    # No quote at all -> refuse.
    block, _ = loop._freshen_entry(intent, now_epoch=1000.0)
    assert block == "no_fresh_entry_quote"
    # Fresh quote, market moved AGAINST the signal entry: fill at the worse ask.
    loop._fresh_quote["EURUSD"] = {"bid": 1.10015, "ask": 1.10025, "spread": 0.9, "ts": 999.5}
    intent2 = _intent()
    block2, slip = loop._freshen_entry(intent2, now_epoch=1000.0)
    assert block2 == "" and slip > 0
    assert intent2.entry_price == 1.10025  # adverse of (signal 1.10005, current ask)
    # Bracket re-anchored: stop distance preserved from stop_bps.
    assert intent2.sl_price < intent2.entry_price < intent2.tp_price


def test_same_minute_candidates_rank_independently_of_symbol_order(
    tmp_path, monkeypatch
):
    from fxstack.scalp.bars import M1Bar
    import fxstack.scalp.loop as loop_module
    from fxstack.scalp.loop import ScalpLoop
    from fxstack.scalp.signals import ScalpIntent
    from fxstack.scalp.sizing import SizedIntent

    minute = int(
        dt.datetime(2026, 8, 3, 10, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    evidence = {
        "EURUSD": {"p_star": 0.52, "strength": 2.2},
        "AUDUSD": {"p_star": 0.45, "strength": 2.8},
    }

    def fake_signal(*, bars, config, spread_bps):
        del config
        last = bars[-1]
        item = evidence[last.symbol]
        return ScalpIntent(
            symbol=last.symbol,
            side="BUY",
            minute_epoch=last.minute_epoch,
            ref_mid=last.close,
            entry_price=last.ask_close,
            sl_price=last.ask_close - 0.0005,
            tp_price=last.ask_close + 0.0010,
            atr_bps=5.0,
            stop_bps=5.0,
            disp_z=item["strength"],
            spread_bps=spread_bps,
            p_star=item["p_star"],
            time_stop_bars=20,
        ), ""

    def fake_size(*, intent, **_kwargs):
        return SizedIntent(
            intent=intent,
            lots=0.1,
            risk_fraction=0.01,
            money_at_risk=10.0,
            sizeable=True,
        )

    monkeypatch.setattr(loop_module, "evaluate_signal", fake_signal)
    monkeypatch.setattr(loop_module, "size_intent", fake_size)

    def run(order, root):
        cfg = _cfg(symbols=list(order), max_concurrent=1)
        cfg.data_root = str(root)
        cfg.api_key_file = str(tmp_path / "missing_key.txt")
        loop = ScalpLoop(cfg)
        bars = {}
        for index, symbol in enumerate(order):
            mid = 1.10 + index * 0.01
            bar = M1Bar(
                symbol=symbol,
                minute_epoch=minute,
                open=mid,
                high=mid + 0.0002,
                low=mid - 0.0002,
                close=mid,
                bid_close=mid - 0.00005,
                ask_close=mid + 0.00005,
                spread_max_bps=0.9,
                spread_close_bps=0.9,
                tick_count=30,
                valid=True,
                quote_changes=5,
            )
            bars[symbol] = bar
            loop.sentinel.observe(
                symbol=symbol, spread_bps=0.9, ts_epoch=minute + 60.0
            )
            loop._fresh_quote[symbol] = {
                "bid": bar.bid_close,
                "ask": bar.ask_close,
                "spread": 0.9,
                "ts": minute + 60.0,
            }
        loop.aggregator.consecutive_valid = lambda symbol: [bars[symbol]]
        loop._equity = 10_000.0
        loop._equity_fetched = minute + 60.0
        loop._equity_attested = minute + 60.0
        loop._process_finalized_bars(
            [bars[symbol] for symbol in order],
            now_epoch=minute + 60.0,
            day_key="20260803",
        )
        return set(loop.book.positions)

    assert run(["EURUSD", "AUDUSD"], tmp_path / "forward") == {"AUDUSD"}
    assert run(["AUDUSD", "EURUSD"], tmp_path / "reverse") == {"AUDUSD"}


def test_restart_replays_day_r_and_orphans_open_positions(tmp_path):
    import json as _json

    from fxstack.scalp.ledger import ScalpLedger

    cfg = _cfg()
    cfg.data_root = str(tmp_path / "scalp")
    ledger_dir = tmp_path / "scalp" / "ledger"
    ledger_dir.mkdir(parents=True)
    import time as _time

    day = ScalpLedger.day_key(_time.time())
    rows = [
        {"kind": "fill", "symbol": "EURUSD", "pnl_r": -1.0},
        {"kind": "fill", "symbol": "EURUSD", "pnl_r": -1.5},
        {"kind": "decision", "symbol": "GBPUSD", "opened": True, "minute": 123456},
    ]
    (ledger_dir / f"ledger_{day}.jsonl").write_text(
        "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    from fxstack.scalp.loop import ScalpLoop

    cfg.api_key_file = str(tmp_path / "missing_key.txt")
    loop = ScalpLoop(cfg)
    assert loop.book.day_r == -2.5  # breaker state survives restart
    content = (ledger_dir / f"ledger_{day}.jsonl").read_text(encoding="utf-8")
    assert "position_orphaned" in content and "GBPUSD" in content


def test_sentinel_vetoes_unresolved_spread():
    cfg = _cfg()
    sentinel = SpreadSentinel(cfg)
    sentinel.observe(symbol="EURUSD", spread_bps=0.0, ts_epoch=1000.0)
    assert sentinel.veto_reason(symbol="EURUSD", now_epoch=1001.0) == "spread_unresolved"


def test_usdjpy_sizes_with_live_rates_and_refuses_without():
    from fxstack.scalp.signals import ScalpIntent

    intent = ScalpIntent(
        symbol="USDJPY",
        side="BUY",
        minute_epoch=0,
        ref_mid=150.00,
        entry_price=150.010,
        sl_price=149.935,  # ~7.5 pips
        tp_price=150.120,
        atr_bps=5.0,
        stop_bps=5.0,
        disp_z=-2.1,
        spread_bps=1.0,
        p_star=0.5,
        time_stop_bars=20,
    )
    without = size_intent(intent=intent, equity=10_000.0, config=_cfg())
    assert not without.sizeable and without.reason == "conversion_unresolvable"
    with_rates = size_intent(
        intent=intent, equity=10_000.0, config=_cfg(), quote_rates={"USDJPY": 150.0}
    )
    assert with_rates.sizeable and with_rates.lots > 0


# --------------------------------------------------------------- config guard


def test_default_universe_budgets_and_portfolio_legs_cover_all_22_symbols():
    expected = (
        "EURUSD",
        "USDJPY",
        "AUDUSD",
        "GBPUSD",
        "USDCAD",
        "USDCHF",
        "EURGBP",
        "EURJPY",
        "NZDUSD",
        "AUDJPY",
        "CADJPY",
        "CHFJPY",
        "EURAUD",
        "EURCAD",
        "EURCHF",
        "GBPCAD",
        "GBPCHF",
        "GBPJPY",
        "BTCUSD",
        "ETHUSD",
        "AUDCAD",
        "NZDJPY",
    )
    assert CONFIGURED_SYMBOLS == expected
    assert CONFIGURED_FX_SYMBOLS == tuple(
        symbol for symbol in expected if symbol not in CONFIGURED_CRYPTO_SYMBOLS
    )
    assert len(CONFIGURED_FX_SYMBOLS) == 20
    assert len(CONFIGURED_CRYPTO_SYMBOLS) == 2
    assert tuple(ScalpConfig().symbols) == expected
    assert tuple(DEFAULT_SPREAD_BUDGETS_BPS) == expected
    assert tuple(PAIR_LEGS) == expected
    assert set(CRYPTO_SYMBOLS) == set(CONFIGURED_CRYPTO_SYMBOLS)
    assert all(PAIR_LEGS[symbol] == (symbol[:3], symbol[3:]) for symbol in expected)
    assert DEFAULT_SPREAD_BUDGETS_BPS["AUDCAD"] == pytest.approx(3.4)
    assert DEFAULT_SESSION_WINDOWS_UTC["AUDCAD"] == [(7, 21)]
    assert "LTCUSD" not in DEFAULT_SPREAD_BUDGETS_BPS
    assert "LTCUSD" not in DEFAULT_SESSION_WINDOWS_UTC


def test_standalone_scalp_mode_is_shadow_only():
    live_errors = _cfg(mode="live").validate()
    assert any("must be shadow" in error for error in live_errors)
    assert any("must be shadow" in error for error in _cfg(mode="yolo").validate())


def test_standalone_loop_fails_closed_for_research_only_limit_entry(tmp_path):
    from fxstack.scalp.loop import ScalpLoop

    cfg = _cfg(entry_mode="limit")
    cfg.data_root = str(tmp_path / "scalp")
    cfg.api_key_file = str(tmp_path / "missing_key.txt")
    with pytest.raises(SystemExit, match="limit remains research-only"):
        ScalpLoop(cfg)


def test_unqualified_symbol_is_refused():
    cfg = _cfg(symbols=["EURUSD", "USDMXN"])
    assert any("USDMXN" in e for e in cfg.validate())


def test_config_rejects_duplicate_and_unsupported_symbols_explicitly():
    duplicate_errors = _cfg(symbols=["EURUSD", "eurusd"]).validate()
    assert "FXSCALP_SYMBOLS contains duplicate symbols: EURUSD" in duplicate_errors

    unsupported = _cfg(symbols=["EURUSD", "XAUUSD"])
    # A caller cannot extend the universe merely by supplying a spread budget.
    unsupported.spread_budgets_bps["XAUUSD"] = 1.0
    unsupported_errors = unsupported.validate()
    assert "FXSCALP_SYMBOLS contains unsupported symbols: XAUUSD" in unsupported_errors


@pytest.mark.parametrize("scale", [0.0, -0.1, 1.01, float("inf"), float("nan")])
def test_spread_budget_scale_must_only_tighten(scale: float):
    cfg = _cfg()
    cfg.spread_budget_scale = scale
    assert any("spread_budget_scale" in e for e in cfg.validate())
