"""Synthetic-only tests for the sparse liquidity-sweep/reclaim screen."""

from __future__ import annotations

import dataclasses
import json

import pytest

import fxstack.scalp.screen_liquidity_sweep as lsr


BASE_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z, UTC-aligned.


def _bar(
    index: int,
    *,
    mid_o: float = 100.0,
    mid_h: float = 100.10,
    mid_l: float = 99.90,
    mid_c: float = 100.0,
    spread_bps: float = 0.20,
) -> lsr.QuoteBar:
    def quotes(mid: float) -> tuple[float, float]:
        half = mid * spread_bps / 2e4
        return mid - half, mid + half

    bid_o, ask_o = quotes(mid_o)
    bid_h, ask_h = quotes(mid_h)
    bid_l, ask_l = quotes(mid_l)
    bid_c, ask_c = quotes(mid_c)
    return lsr.QuoteBar(
        epoch=BASE_EPOCH + index * 60,
        bid_o=bid_o,
        bid_h=bid_h,
        bid_l=bid_l,
        bid_c=bid_c,
        ask_o=ask_o,
        ask_h=ask_h,
        ask_l=ask_l,
        ask_c=ask_c,
    )


def _baseline(count: int = 900, *, spread_bps: float = 0.20) -> list[lsr.QuoteBar]:
    return [_bar(index, spread_bps=spread_bps) for index in range(count)]


def _plant_buy(bars: list[lsr.QuoteBar], index: int, *, spread_bps: float = 0.20) -> None:
    bars[index] = _bar(
        index,
        mid_o=100.0,
        mid_h=100.05,
        mid_l=99.55,
        mid_c=100.03,
        spread_bps=spread_bps,
    )
    bars[index + 2] = _bar(
        index + 2,
        mid_o=100.0,
        mid_h=100.70,
        mid_l=99.90,
        mid_c=100.20,
        spread_bps=spread_bps,
    )


def _plant_sell(bars: list[lsr.QuoteBar], index: int, *, spread_bps: float = 0.20) -> None:
    bars[index] = _bar(
        index,
        mid_o=100.0,
        mid_h=100.45,
        mid_l=99.95,
        mid_c=99.97,
        spread_bps=spread_bps,
    )
    bars[index + 2] = _bar(
        index + 2,
        mid_o=100.0,
        mid_h=100.10,
        mid_l=99.30,
        mid_c=99.80,
        spread_bps=spread_bps,
    )


STRICT_CONFIG = lsr.SweepConfig(16, 0.25, 0.75)


def _signal(
    bars: list[lsr.QuoteBar],
    index: int,
    side: str,
    *,
    extra_cost: float = 0.0,
    budget: float = 1.0,
) -> lsr.SweepSignal:
    prepared = lsr.prepare_series(bars)
    signal, reason = lsr.evaluate_signal(
        prepared=prepared,
        signal_index=index,
        symbol="EURUSD",
        side=side,
        config=STRICT_CONFIG,
        effective_spread_budget_bps=budget,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=extra_cost,
    )
    assert signal is not None, reason
    return signal


def test_future_perturbations_cannot_change_past_context_or_signal() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    before_prepared = lsr.prepare_series(bars)
    before_context = lsr.signal_context_at(
        before_prepared, signal_index=300, level_bars_m15=16
    )
    before_signal, before_reason = lsr.evaluate_signal(
        prepared=before_prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
    )
    assert before_signal is not None, before_reason

    changed = list(bars)
    # t+1 is the only future value the execution contract may read. Everything
    # after that is outcome data and must not alter the frozen signal.
    for index in range(302, len(changed)):
        changed[index] = _bar(
            index,
            mid_o=130.0,
            mid_h=131.0,
            mid_l=129.0,
            mid_c=130.5,
            spread_bps=4.0,
        )
    after_prepared = lsr.prepare_series(changed)
    after_context = lsr.signal_context_at(
        after_prepared, signal_index=300, level_bars_m15=16
    )
    after_signal, after_reason = lsr.evaluate_signal(
        prepared=after_prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
    )
    assert after_signal is not None, after_reason
    assert after_context == before_context
    assert after_signal == before_signal


def test_m5_and_m15_context_excludes_the_entire_bucket_containing_t() -> None:
    bars = _baseline(600)
    baseline = lsr.prepare_series(bars)
    before = lsr.signal_context_at(baseline, signal_index=302, level_bars_m15=16)
    assert before is not None

    changed = list(bars)
    # Bars 300 and 301 are earlier than t=302, but belong to the same current
    # M5 and M15 buckets. Neither may enter volatility or level construction.
    changed[300] = _bar(300, mid_h=120.0, mid_l=80.0)
    changed[301] = _bar(301, mid_h=115.0, mid_l=85.0)
    after = lsr.signal_context_at(
        lsr.prepare_series(changed), signal_index=302, level_bars_m15=16
    )
    assert after == before

    incomplete = list(bars)
    del incomplete[299]
    missing = lsr.signal_context_at(
        lsr.prepare_series(incomplete), signal_index=301, level_bars_m15=16
    )
    assert missing is None


def test_buy_and_sell_signal_formulas_are_quote_side_mirrors() -> None:
    buy_bars = _baseline()
    sell_bars = _baseline()
    _plant_buy(buy_bars, 300)
    _plant_sell(sell_bars, 300)

    buy = _signal(buy_bars, 300, "BUY")
    sell = _signal(sell_bars, 300, "SELL")
    assert buy.penetration_atr5 == pytest.approx(sell.penetration_atr5, rel=2e-3)
    assert buy.reclaim_atr5 == pytest.approx(sell.reclaim_atr5, rel=2e-3)
    assert buy.wick_fraction == pytest.approx(sell.wick_fraction, rel=2e-3)
    assert buy.risk_bps == pytest.approx(sell.risk_bps, rel=3e-3)
    assert buy.target_price > buy.entry_price > buy.stop_price
    assert sell.target_price < sell.entry_price < sell.stop_price


def test_entry_is_exactly_one_bar_delayed_and_adverse_to_both_touches() -> None:
    signal = _bar(0, mid_c=100.0)
    favorable_buy_gap = _bar(1, mid_o=99.0, mid_h=99.2, mid_l=98.8, mid_c=99.0)
    adverse_buy_gap = _bar(1, mid_o=101.0, mid_h=101.2, mid_l=100.8, mid_c=101.0)

    assert lsr.FILL_DELAY_M1_BARS == 1
    assert lsr._entry_price(signal, favorable_buy_gap, side="BUY") == signal.ask_c
    assert lsr._entry_price(signal, adverse_buy_gap, side="BUY") == adverse_buy_gap.ask_o
    assert lsr._entry_price(signal, favorable_buy_gap, side="SELL") == favorable_buy_gap.bid_o
    assert lsr._entry_price(signal, adverse_buy_gap, side="SELL") == signal.bid_c


def test_spread_q25_excludes_t_and_gates_signal_and_next_open() -> None:
    prior = _baseline(242, spread_bps=1.0)
    prior[240] = _bar(240, spread_bps=8.0)
    q25 = lsr.prepare_series(prior).spread_q25_by_index
    assert q25[240] == pytest.approx(1.0)

    signal_wide = _baseline()
    _plant_buy(signal_wide, 300, spread_bps=0.30)
    _candidate, reason = lsr.evaluate_signal(
        prepared=lsr.prepare_series(signal_wide),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
    )
    assert reason == "signal_spread_above_q25_or_budget"

    entry_wide = _baseline()
    _plant_buy(entry_wide, 300)
    entry_wide[301] = _bar(301, spread_bps=0.30)
    _candidate, reason = lsr.evaluate_signal(
        prepared=lsr.prepare_series(entry_wide),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
    )
    assert reason == "entry_spread_above_q25_or_budget"


def test_ambiguous_bar_exits_at_stop_before_target() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    signal = _signal(bars, 300, "BUY")
    ambiguous = list(bars)
    ambiguous[301] = _bar(
        301,
        mid_o=100.0,
        mid_h=101.0,
        mid_l=99.0,
        mid_c=100.0,
    )
    trade = lsr.simulate_trade(ambiguous, signal=signal)
    assert trade is not None
    assert trade.exit_reason == "sl_double_touch"
    assert trade.pnl_r == pytest.approx(-1.0)


def test_extra_round_trip_cost_is_charged_in_r() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    without_cost = _signal(bars, 300, "BUY", extra_cost=0.0)
    with_cost = _signal(bars, 300, "BUY", extra_cost=1.0)
    gross_trade = lsr.simulate_trade(bars, signal=without_cost)
    net_trade = lsr.simulate_trade(bars, signal=with_cost)
    assert gross_trade is not None and net_trade is not None
    assert gross_trade.exit_reason == net_trade.exit_reason == "tp"
    assert gross_trade.pnl_r - net_trade.pnl_r == pytest.approx(
        1.0 / with_cost.risk_bps
    )
    assert with_cost.total_cost_bps == pytest.approx(with_cost.entry_spread_bps + 1.0)


def test_positive_time_stop_is_not_a_full_target_win() -> None:
    bars = _baseline()
    # Plant only the sweep. It never reaches the 1R target, then exits the
    # eighth held M1 bar above entry with positive PnL.
    bars[300] = _bar(
        300,
        mid_o=100.0,
        mid_h=100.05,
        mid_l=99.55,
        mid_c=100.03,
    )
    bars[308] = _bar(
        308,
        mid_o=100.0,
        mid_h=100.30,
        mid_l=99.90,
        mid_c=100.20,
    )
    cell = lsr.screen_cell(
        prepared=lsr.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=0.0,
    )
    assert cell["trades"] == 1
    assert cell["positive_outcomes"] == 1
    assert cell["positive_outcome_rate"] == 1.0
    assert cell["wins"] == cell["full_target_wins"] == 0
    assert cell["win_rate"] == cell["full_target_win_rate"] == 0.0
    assert cell["exit_mix"] == {"time_stop": 1}


def test_cooldown_is_per_pair_and_side_for_thirty_m1_bars() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_buy(bars, 310)
    cell = lsr.screen_cell(
        prepared=lsr.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=0.0,
    )
    assert cell["raw_events"] == 2
    assert cell["events_after_cooldown"] == 1
    assert cell["reasons"]["cooldown"] == 1


def test_zero_event_cells_are_retained_and_proxy_costs_never_claim_readiness() -> None:
    proxy_budgets = {symbol: 1.0 + index / 100.0 for index, symbol in enumerate(lsr.FX_SYMBOLS)}
    result = lsr.screen_universe(
        bars_by_symbol={},
        proxy_spread_budgets_bps=proxy_budgets,
        prior_tests=1_074,
    )
    assert len(result["cells"]) == 288
    assert all(cell["trades"] == 0 for cell in result["cells"])
    assert all(cell["cost_mode"] == "pair_specific_proxy_discovery_stress" for cell in result["cells"])
    assert all(
        cell["effective_spread_budget_bps"] == proxy_budgets[cell["symbol"]]
        for cell in result["cells"]
    )
    assert result["economic_claim_ready"] is False
    assert result["economics_claim_ready"] is False
    assert result["success_claim_authorized"] is False
    assert result["activation_authorized"] is False
    json.dumps(result, allow_nan=False)


def test_trial_accounting_is_exactly_288_new_cells() -> None:
    assert len(lsr.GRID) == 8
    assert {(config.level_bars_m15) for config in lsr.GRID} == {8, 16}
    assert {(config.penetration_atr5) for config in lsr.GRID} == {0.10, 0.25}
    assert {(config.wick_floor) for config in lsr.GRID} == {0.60, 0.75}
    assert lsr.trial_accounting(n_symbols=18, prior_tests=1_074) == {
        "grid_configurations": 8,
        "directions": 2,
        "symbols": 18,
        "current_attempted_cells": 288,
        "prior_attempted_cells": 1_074,
        "cumulative_attempted_cells": 1_362,
        "expected_full_universe_cells": 288,
    }


def test_planted_buy_and_sell_sweeps_clear_the_spread() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_sell(bars, 600)
    prepared = lsr.prepare_series(bars)
    buy = lsr.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=1.0,
    )
    sell = lsr.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="SELL",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=1.0,
    )
    assert buy["trades"] == buy["wins"] == 1
    assert sell["trades"] == sell["wins"] == 1
    assert buy["mean_r"] > 0.0
    assert sell["mean_r"] > 0.0


def test_optional_stop_floor_only_widens_risk_and_preserves_one_to_one_target() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    raw = _signal(bars, 300, "BUY")
    prepared = lsr.prepare_series(bars)
    floored, reason = lsr.evaluate_signal(
        prepared=prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=raw.risk_bps + 10.0,
    )
    assert floored is not None, reason
    assert floored.risk_bps == pytest.approx(raw.risk_bps + 10.0)
    target_bps = (floored.target_price - floored.entry_price) / floored.entry_price * 1e4
    assert target_bps == pytest.approx(floored.risk_bps)


def test_signal_dataclass_serializes_without_nonfinite_values() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    signal = _signal(bars, 300, "BUY")
    assert all(
        not isinstance(value, float) or value == value
        for value in dataclasses.asdict(signal).values()
    )
