"""Synthetic-only tests for the causal Median-Stretch Reversal screen."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import statistics
from pathlib import Path

import pytest

import fxstack.scalp.screen_median_stretch_reversal as msr


BASE_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z.
PROVENANCE = {
    "source_id": "synthetic-frozen-proxy-v1",
    "as_of_utc": "2024-01-01T00:00:00Z",
    "method": "synthetic-test-only",
    "frozen": True,
}


def _bar(
    index: int,
    *,
    mid_o: float = 100.0,
    mid_h: float = 100.05,
    mid_l: float = 99.95,
    mid_c: float = 100.0,
    spread_bps: float = 0.20,
    epoch_shift: int = 0,
) -> msr.QuoteBar:
    def quotes(mid: float) -> tuple[float, float]:
        half = mid * spread_bps / 2e4
        return mid - half, mid + half

    bid_o, ask_o = quotes(mid_o)
    bid_h, ask_h = quotes(mid_h)
    bid_l, ask_l = quotes(mid_l)
    bid_c, ask_c = quotes(mid_c)
    return msr.QuoteBar(
        epoch=BASE_EPOCH + index * 60 + epoch_shift,
        bid_o=bid_o,
        bid_h=bid_h,
        bid_l=bid_l,
        bid_c=bid_c,
        ask_o=ask_o,
        ask_h=ask_h,
        ask_l=ask_l,
        ask_c=ask_c,
    )


def _baseline(count: int = 900, *, spread_bps: float = 0.20) -> list[msr.QuoteBar]:
    return [_bar(index, spread_bps=spread_bps) for index in range(count)]


def _plant_buy(
    bars: list[msr.QuoteBar],
    index: int,
    *,
    spread_bps: float = 0.20,
    target: bool = True,
) -> None:
    bars[index - 1] = _bar(
        index - 1,
        mid_o=99.88,
        mid_h=99.90,
        mid_l=99.78,
        mid_c=99.84,
        spread_bps=spread_bps,
    )
    bars[index] = _bar(
        index,
        mid_o=99.83,
        mid_h=99.88,
        mid_l=99.82,
        mid_c=99.87,
        spread_bps=spread_bps,
    )
    bars[index + 1] = _bar(
        index + 1,
        mid_o=99.87,
        mid_h=99.91,
        mid_l=99.82,
        mid_c=99.87,
        spread_bps=spread_bps,
    )
    if target:
        bars[index + 2] = _bar(
            index + 2,
            mid_o=99.88,
            mid_h=100.10,
            mid_l=99.82,
            mid_c=100.02,
            spread_bps=spread_bps,
        )


def _plant_sell(
    bars: list[msr.QuoteBar],
    index: int,
    *,
    spread_bps: float = 0.20,
    target: bool = True,
) -> None:
    bars[index - 1] = _bar(
        index - 1,
        mid_o=100.12,
        mid_h=100.22,
        mid_l=100.10,
        mid_c=100.16,
        spread_bps=spread_bps,
    )
    bars[index] = _bar(
        index,
        mid_o=100.17,
        mid_h=100.18,
        mid_l=100.12,
        mid_c=100.13,
        spread_bps=spread_bps,
    )
    bars[index + 1] = _bar(
        index + 1,
        mid_o=100.13,
        mid_h=100.18,
        mid_l=100.09,
        mid_c=100.13,
        spread_bps=spread_bps,
    )
    if target:
        bars[index + 2] = _bar(
            index + 2,
            mid_o=100.12,
            mid_h=100.18,
            mid_l=99.90,
            mid_c=99.98,
            spread_bps=spread_bps,
        )


STRICT_CONFIG = msr.MSRConfig(24, 1.25, 0.25)


def _evaluate(
    bars: list[msr.QuoteBar],
    index: int,
    side: str,
    *,
    config: msr.MSRConfig = STRICT_CONFIG,
    budget: float = 1.0,
) -> tuple[msr.MSRSignal | None, str]:
    return msr.evaluate_signal(
        prepared=msr.prepare_series(bars),
        signal_index=index,
        symbol="EURUSD",
        side=side,
        config=config,
        proxy_spread_budget_bps=budget,
    )


def _signal(
    bars: list[msr.QuoteBar],
    index: int,
    side: str,
    *,
    config: msr.MSRConfig = STRICT_CONFIG,
    budget: float = 1.0,
) -> msr.MSRSignal:
    signal, reason = _evaluate(bars, index, side, config=config, budget=budget)
    assert signal is not None, reason
    return signal


def test_future_perturbations_cannot_change_frozen_context_or_signal() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    before_prepared = msr.prepare_series(bars)
    before_context = msr.baseline_context_at(
        before_prepared, signal_index=300, config=STRICT_CONFIG
    )
    before_signal, before_reason = msr.evaluate_signal(
        prepared=before_prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert before_signal is not None, before_reason

    changed = list(bars)
    # t+1 is the sole future execution input. Everything after it is outcome.
    for index in range(302, len(changed)):
        changed[index] = _bar(
            index,
            mid_o=130.0,
            mid_h=131.0,
            mid_l=129.0,
            mid_c=130.5,
            spread_bps=4.0,
        )
    after_prepared = msr.prepare_series(changed)
    after_context = msr.baseline_context_at(
        after_prepared, signal_index=300, config=STRICT_CONFIG
    )
    after_signal, after_reason = msr.evaluate_signal(
        prepared=after_prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert after_signal is not None, after_reason
    assert after_context == before_context
    assert after_signal == before_signal


def test_context_uses_exact_240_bars_ending_t_minus_2_and_excludes_event() -> None:
    bars = _baseline(500)
    _plant_buy(bars, 300)
    context = msr.baseline_context_at(
        msr.prepare_series(bars), signal_index=300, config=STRICT_CONFIG
    )
    assert context is not None
    assert context.event_start_index == 299
    expected_tr = statistics.median(
        msr._true_range_bps(bars[index - 1], bars[index])
        for index in range(59, 299)
    )
    expected_q25 = msr._quantile_sorted(
        sorted(bars[index].spread_close_bps for index in range(59, 299)), 0.25
    )
    assert context.volatility_bps == pytest.approx(expected_tr)
    assert context.spread_q25_bps == pytest.approx(expected_q25)
    assert context.fair_bid == pytest.approx(
        statistics.median(bars[index].bid_c for index in range(275, 299))
    )
    assert context.fair_ask == pytest.approx(
        statistics.median(bars[index].ask_c for index in range(275, 299))
    )

    changed = list(bars)
    changed[299] = _bar(299, mid_o=80.0, mid_h=120.0, mid_l=70.0, mid_c=75.0)
    changed[300] = _bar(300, mid_o=120.0, mid_h=130.0, mid_l=80.0, mid_c=125.0)
    changed_context = msr.baseline_context_at(
        msr.prepare_series(changed), signal_index=300, config=STRICT_CONFIG
    )
    assert changed_context == context


def test_strict_m1_continuity_covers_baseline_event_and_exact_next_open() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)

    broken_baseline = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 100 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_baseline, 300, "BUY")
    assert candidate is None
    assert reason == "strict_pre_event_context_unavailable"

    broken_event = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 300 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_event, 300, "BUY")
    assert candidate is None
    assert reason == "strict_pre_event_context_unavailable"

    broken_fill = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_fill, 300, "BUY")
    assert candidate is None
    assert reason == "exact_next_open_gap"


def test_buy_and_sell_signal_formulas_are_quote_side_mirrors() -> None:
    buy_bars = _baseline()
    sell_bars = _baseline()
    _plant_buy(buy_bars, 300)
    _plant_sell(sell_bars, 300)
    buy = _signal(buy_bars, 300, "BUY")
    sell = _signal(sell_bars, 300, "SELL")

    assert buy.stretch_vol_units == pytest.approx(sell.stretch_vol_units, rel=2e-3)
    assert buy.reversal_vol_units == pytest.approx(sell.reversal_vol_units, rel=2e-3)
    assert buy.close_location == pytest.approx(sell.close_location, rel=2e-3)
    assert buy.risk_bps == pytest.approx(sell.risk_bps, rel=5e-3)
    assert buy.p_star == pytest.approx(sell.p_star, rel=5e-3)
    assert buy.target_price > buy.entry_price > buy.stop_price
    assert sell.target_price < sell.entry_price < sell.stop_price


def test_stretch_reversal_size_and_reversal_bar_shape_are_enforced() -> None:
    shallow_stretch = _baseline()
    _plant_buy(shallow_stretch, 300)
    shallow_stretch[299] = _bar(
        299, mid_o=99.96, mid_h=99.98, mid_l=99.92, mid_c=99.94
    )
    shallow_stretch[300] = _bar(
        300, mid_o=99.94, mid_h=99.99, mid_l=99.93, mid_c=99.98
    )
    candidate, reason = _evaluate(shallow_stretch, 300, "BUY")
    assert candidate is None
    assert reason == "stretch_too_small"

    weak_reversal = _baseline()
    _plant_buy(weak_reversal, 300)
    weak_reversal[300] = _bar(
        300, mid_o=99.84, mid_h=99.86, mid_l=99.82, mid_c=99.85
    )
    candidate, reason = _evaluate(weak_reversal, 300, "BUY")
    assert candidate is None
    assert reason == "reversal_too_small"

    wrong_shape = _baseline()
    _plant_buy(wrong_shape, 300)
    wrong_shape[300] = _bar(
        300, mid_o=99.88, mid_h=99.95, mid_l=99.82, mid_c=99.87
    )
    candidate, reason = _evaluate(wrong_shape, 300, "BUY")
    assert candidate is None
    assert reason == "reversal_bar_missing"


def test_fill_is_exactly_next_open_with_no_signal_close_fallback() -> None:
    next_bar = _bar(1, mid_o=101.0, mid_h=101.2, mid_l=100.8, mid_c=101.0)
    assert msr.FILL_DELAY_M1_BARS == 1
    assert msr._entry_price(next_bar, side="BUY") == next_bar.ask_o
    assert msr._entry_price(next_bar, side="SELL") == next_bar.bid_o

    bars = _baseline()
    _plant_buy(bars, 300)
    bars[301] = _bar(
        301, mid_o=99.88, mid_h=99.92, mid_l=99.82, mid_c=99.88
    )
    signal = _signal(bars, 300, "BUY")
    assert signal.entry_price == bars[301].ask_o
    assert signal.entry_price != bars[300].ask_c


def test_pre_event_q25_and_pair_proxy_gate_signal_and_entry_spreads() -> None:
    signal_wide = _baseline()
    _plant_buy(signal_wide, 300, spread_bps=0.30)
    candidate, reason = _evaluate(signal_wide, 300, "BUY")
    assert candidate is None
    assert reason == "signal_spread_above_q25_or_proxy"

    entry_wide = _baseline()
    _plant_buy(entry_wide, 300)
    entry_wide[301] = _bar(
        301,
        mid_o=99.87,
        mid_h=99.91,
        mid_l=99.82,
        mid_c=99.87,
        spread_bps=0.30,
    )
    candidate, reason = _evaluate(entry_wide, 300, "BUY")
    assert candidate is None
    assert reason == "entry_spread_above_q25_or_proxy"

    proxy_tight = _baseline()
    _plant_buy(proxy_tight, 300)
    candidate, reason = _evaluate(proxy_tight, 300, "BUY", budget=0.10)
    assert candidate is None
    assert reason == "signal_spread_above_q25_or_proxy"


def test_stop_floor_max_risk_pstar_and_four_times_cost_gates_are_frozen() -> None:
    assert msr._floored_risk_bps(2.0) == msr.FROZEN_STOP_FLOOR_BPS == 4.5
    assert msr._floored_risk_bps(6.0) == 6.0

    excessive_risk = _baseline()
    _plant_buy(excessive_risk, 300)
    excessive_risk[299] = _bar(
        299, mid_o=99.88, mid_h=99.90, mid_l=99.50, mid_c=99.84
    )
    candidate, reason = _evaluate(excessive_risk, 300, "BUY")
    assert candidate is None
    assert reason == "risk_above_25bps"

    cost_dead = _baseline()
    _plant_buy(cost_dead, 300)
    cost_dead[299] = _bar(
        299, mid_o=99.88, mid_h=99.90, mid_l=99.83, mid_c=99.84
    )
    candidate, reason = _evaluate(cost_dead, 300, "BUY")
    assert candidate is None
    assert reason == "bracket_cost_dead"

    target_cost_dead = _baseline(spread_bps=2.0)
    _plant_buy(target_cost_dead, 300, spread_bps=2.0)
    target_cost_dead[301] = _bar(
        301,
        mid_o=99.86,
        mid_h=99.91,
        mid_l=99.82,
        mid_c=99.86,
        spread_bps=2.0,
    )
    candidate, reason = _evaluate(target_cost_dead, 300, "BUY", budget=3.0)
    assert candidate is None
    assert reason == "target_too_small_vs_cost"

    accepted = _baseline()
    _plant_buy(accepted, 300)
    signal = _signal(accepted, 300, "BUY")
    assert signal.risk_bps <= 25.0
    assert signal.p_star <= 0.55
    assert signal.p_star == pytest.approx(
        (signal.risk_bps + 1.0) / (signal.risk_bps + signal.quote_target_bps)
    )
    assert signal.quote_target_bps >= 4.0 * signal.recorded_cost_bps
    assert signal.extra_round_trip_cost_bps == 1.0


def test_fixed_target_must_remain_on_the_fair_value_side() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    bars[301] = _bar(
        301, mid_o=99.93, mid_h=99.96, mid_l=99.90, mid_c=99.93
    )
    candidate, reason = _evaluate(bars, 300, "BUY")
    assert candidate is None
    assert reason == "target_beyond_frozen_fair_value"


def test_complete_horizon_is_required_before_an_early_target_is_inspected() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    gapped = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 305 else 0))
        for index, bar in enumerate(bars)
    ]
    signal = _signal(gapped, 300, "BUY")
    assert gapped[302].bid_h >= signal.target_price
    assert msr.outcome_horizon_is_complete(gapped, entry_index=301) is False
    assert msr.simulate_trade(gapped, signal=signal) is None


def test_unresolved_first_reservation_blocks_later_same_day_substitution() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_buy(bars, 600)
    gapped = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 305 else 0))
        for index, bar in enumerate(bars)
    ]
    cell = msr.screen_cell(
        prepared=msr.prepare_series(gapped),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["raw_events"] == 2
    assert cell["entry_day_reservations"] == 1
    assert cell["unresolved_reservations"] == 1
    assert cell["scored_trades"] == 0
    assert cell["reasons"]["entry_day_already_reserved"] == 1
    assert len(cell["event_ledger"]) == 1
    assert cell["event_ledger"][0]["reservation_status"] == "unresolved"
    assert cell["event_ledger"][0]["outcome_reason"] == "incomplete_outcome_horizon"
    assert cell["event_ledger"][0]["gate_treatment"] == (
        "unresolved_as_adverse_stop_for_discovery_gate"
    )
    assert cell["event_ledger"][0]["gate_pnl_r"] < -1.0
    assert cell["gate_mean_r"] < -1.0
    assert cell["trade_ledger"] == []


def test_daily_reservation_uses_entry_day_even_when_signal_crosses_midnight() -> None:
    bars = _baseline(1_600)
    _plant_buy(bars, 1_439)
    cell = msr.screen_cell(
        prepared=msr.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    event = cell["event_ledger"][0]
    assert msr._utc_day(event["signal_epoch"]) == "2024-01-01"
    assert event["entry_day"] == "2024-01-02"
    assert msr._utc_day(event["entry_epoch"]) == "2024-01-02"


def test_adverse_gap_and_ambiguous_bar_use_conservative_exit_ordering() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    signal = _signal(bars, 300, "BUY")

    gap_bars = list(bars)
    gap_mid = signal.stop_price - 0.10
    gap_bars[302] = _bar(
        302,
        mid_o=gap_mid,
        mid_h=gap_mid + 0.05,
        mid_l=gap_mid - 0.05,
        mid_c=gap_mid,
    )
    gap_trade = msr.simulate_trade(gap_bars, signal=signal)
    assert gap_trade is not None
    assert gap_trade.exit_reason == "sl_gap_open"
    assert gap_trade.exit_price < signal.stop_price
    assert gap_trade.pnl_r < -1.0

    ambiguous = list(bars)
    ambiguous[302] = _bar(
        302,
        mid_o=signal.entry_price,
        mid_h=signal.target_price + 0.10,
        mid_l=signal.stop_price - 0.10,
        mid_c=signal.entry_price,
    )
    ambiguous_trade = msr.simulate_trade(ambiguous, signal=signal)
    assert ambiguous_trade is not None
    assert ambiguous_trade.exit_reason == "sl_double_touch"
    assert ambiguous_trade.full_target_win is False
    assert ambiguous_trade.pnl_r < -1.0

    favorable_gap = list(bars)
    gap_target_mid = signal.target_price + 0.10
    favorable_gap[302] = _bar(
        302,
        mid_o=gap_target_mid,
        mid_h=gap_target_mid + 0.05,
        mid_l=gap_target_mid - 0.05,
        mid_c=gap_target_mid,
    )
    target_trade = msr.simulate_trade(favorable_gap, signal=signal)
    assert target_trade is not None
    assert target_trade.exit_reason == "tp_gap_open"
    assert target_trade.exit_price == signal.target_price
    assert target_trade.full_target_win is True


def test_positive_time_stop_is_separate_from_full_target_trade_win() -> None:
    bars = _baseline()
    _plant_buy(bars, 300, target=False)
    for index in range(301, 313):
        close = 99.90 if index == 312 else 99.87
        bars[index] = _bar(
            index,
            mid_o=99.87,
            mid_h=99.94,
            mid_l=99.82,
            mid_c=close,
        )
    cell = msr.screen_cell(
        prepared=msr.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["scored_trades"] == 1
    assert cell["positive_outcomes"] == 1
    assert cell["positive_time_stops"] == 1
    assert cell["full_target_wins"] == 0
    assert cell["full_target_trade_win_rate"] == 0.0
    assert cell["full_target_reservation_rate"] == 0.0
    assert cell["exit_mix"] == {"time_stop": 1}


def test_planted_buy_and_sell_are_positive_net_full_target_ledger_rows() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_sell(bars, 600)
    prepared = msr.prepare_series(bars)
    buy = msr.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    sell = msr.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="SELL",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    for cell in (buy, sell):
        assert cell["entry_day_reservations"] == 1
        assert cell["scored_trades"] == cell["full_target_wins"] == 1
        assert cell["full_target_trade_win_rate"] == 1.0
        assert cell["full_target_reservation_rate"] == 1.0
        assert cell["exit_mix"] == {"tp": 1}
        assert cell["trade_ledger"][0]["full_target_win"] is True
        assert cell["trade_ledger"][0]["pnl_bps"] > 0.0
        assert cell["event_ledger"][0]["reservation_status"] == "scored"


def test_zero_cells_require_source_and_frozen_pair_proxy_provenance() -> None:
    budgets = {
        symbol: 0.6 + index / 100.0 for index, symbol in enumerate(msr.FX_SYMBOLS)
    }
    result = msr.screen_universe(
        bars_by_symbol={},
        proxy_spread_budgets_bps=budgets,
        proxy_provenance=PROVENANCE,
    )
    assert len(result["cells"]) == 288
    assert all(cell["scored_trades"] == 0 for cell in result["cells"])
    assert all(cell["source_ready"] is False for cell in result["cells"])
    assert all(cell["proxy_budget_ready"] is True for cell in result["cells"])
    assert all(
        cell["cost_mode"] == "pair_specific_frozen_proxy_discovery_stress"
        for cell in result["cells"]
    )
    assert all(
        cell["proxy_budget_bps"] == budgets[cell["symbol"]]
        for cell in result["cells"]
    )
    assert len(result["cost_readiness"]["source_failure_symbols"]) == 18
    assert result["economic_claim_ready"] is False
    assert result["economics_claim_ready"] is False
    assert result["success_claim_authorized"] is False
    assert result["activation_authorized"] is False
    assert result["registry_write_authorized"] is False
    assert result["order_authorized"] is False
    assert result["event_ledger"] == []
    assert result["trade_ledger"] == []

    invalid_contract = msr.screen_universe(
        bars_by_symbol={},
        symbols=("EURUSD",),
        proxy_spread_budgets_bps={"EURUSD": 0.7},
        proxy_provenance=PROVENANCE | {"frozen": False},
    )
    assert all(cell["proxy_budget_ready"] is False for cell in invalid_contract["cells"])
    assert all(cell["cost_mode"] == "cost_unavailable" for cell in invalid_contract["cells"])

    flagged_bars = _baseline()
    _plant_buy(flagged_bars, 300)
    flagged_source = msr.screen_universe(
        bars_by_symbol={"EURUSD": flagged_bars},
        symbols=("EURUSD",),
        proxy_spread_budgets_bps={"EURUSD": 1.0},
        proxy_provenance=PROVENANCE,
        source_errors={"EURUSD": "caller_flagged_source_failure"},
    )
    assert flagged_source["event_ledger"] == []
    assert flagged_source["trade_ledger"] == []
    assert all(cell["source_ready"] is False for cell in flagged_source["cells"])

    short_bars = _baseline(250)
    _plant_buy(short_bars, 243)
    short_source = msr.screen_universe(
        bars_by_symbol={"EURUSD": short_bars},
        symbols=("EURUSD",),
        proxy_spread_budgets_bps={"EURUSD": 1.0},
        proxy_provenance=PROVENANCE,
    )
    assert short_source["event_ledger"] == []
    assert short_source["trade_ledger"] == []
    assert all(
        cell["source_error"] == "no_complete_255_bar_m1_run"
        for cell in short_source["cells"]
    )


def test_trial_accounting_is_immutable_3090_prior_and_3378_cumulative() -> None:
    assert len(msr.GRID) == 8
    assert {config.fair_lookback for config in msr.GRID} == {12, 24}
    assert {config.stretch_vol for config in msr.GRID} == {0.75, 1.25}
    assert {config.reversal_vol for config in msr.GRID} == {0.10, 0.25}
    assert msr.trial_accounting() == {
        "grid_configurations": 8,
        "directions": 2,
        "symbols": 18,
        "current_attempted_cells": 288,
        "prior_attempted_cells": 3_090,
        "cumulative_attempted_cells": 3_378,
        "expected_full_universe_cells": 288,
    }
    assert msr.search_corrected_threshold(3_378) == pytest.approx(
        4.325996250203701,
        abs=1e-12,
    )


def test_global_gate_rejects_35_of_36_and_mixed_config_assembly() -> None:
    expected = [
        {
            "config_id": config.config_id,
            "symbol": symbol,
            "side": side,
            "passes_discovery_cell_gate": False,
        }
        for config in msr.GRID
        for symbol in msr.FX_SYMBOLS
        for side in ("BUY", "SELL")
    ]
    first_config = msr.GRID[0].config_id
    first_rows = [row for row in expected if row["config_id"] == first_config]
    for row in first_rows[:-1]:
        row["passes_discovery_cell_gate"] = True
    assert msr._passing_global_configurations(
        expected,
        symbols=msr.FX_SYMBOLS,
    ) == []

    for row in expected:
        row["passes_discovery_cell_gate"] = False
    for index, key in enumerate(
        (symbol, side) for symbol in msr.FX_SYMBOLS for side in ("BUY", "SELL")
    ):
        config_id = msr.GRID[index % len(msr.GRID)].config_id
        next(
            row
            for row in expected
            if row["config_id"] == config_id
            and (row["symbol"], row["side"]) == key
        )["passes_discovery_cell_gate"] = True
    assert msr._passing_global_configurations(
        expected,
        symbols=msr.FX_SYMBOLS,
    ) == []


def test_csv_loader_rejects_near_miss_header_and_proxy_contract_is_strict(
    tmp_path: Path,
) -> None:
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(
        "timestamp,bid_open,bid_h,bid_l,bid_c,ask_o,ask_h,ask_l,ask_c\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected M1 header"):
        msr.load_m1_csv(bad_csv)

    fractional_csv = tmp_path / "fractional.csv"
    fractional_csv.write_text(
        ",".join(msr.CSV_HEADER)
        + "\n2024-01-01T00:00:00.500Z,1,1,1,1,1.0001,1.0001,1.0001,1.0001,0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed M1 row"):
        msr.load_m1_csv(fractional_csv)

    invalid_proxy = tmp_path / "invalid_proxy.json"
    invalid_proxy.write_text(
        json.dumps(
            {
                "provenance": PROVENANCE | {"frozen": False},
                "budgets_bps": {"EURUSD": 0.7},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing frozen proxy provenance"):
        msr._load_proxy_contract(invalid_proxy)

    naive_proxy = tmp_path / "naive_proxy.json"
    naive_proxy.write_text(
        json.dumps(
            {
                "provenance": PROVENANCE | {"as_of_utc": "2024-01-01T00:00:00"},
                "budgets_bps": {"EURUSD": 0.7},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing frozen proxy provenance"):
        msr._load_proxy_contract(naive_proxy)

    non_utc_proxy = tmp_path / "non_utc_proxy.json"
    non_utc_proxy.write_text(
        json.dumps(
            {
                "provenance": PROVENANCE
                | {"as_of_utc": "2024-01-01T01:00:00+01:00"},
                "budgets_bps": {"EURUSD": 0.7},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing frozen proxy provenance"):
        msr._load_proxy_contract(non_utc_proxy)

    valid_proxy = tmp_path / "valid_proxy.json"
    valid_proxy.write_text(
        json.dumps(
            {
                "provenance": PROVENANCE,
                "budgets_bps": {"eurusd": 0.7},
            }
        ),
        encoding="utf-8",
    )
    budgets, provenance = msr._load_proxy_contract(valid_proxy)
    assert budgets == {"EURUSD": 0.7}
    assert provenance == PROVENANCE


def test_screen_and_ledgers_serialize_without_nonfinite_values() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    result = msr.screen_universe(
        bars_by_symbol={"EURUSD": bars},
        symbols=("EURUSD",),
        proxy_spread_budgets_bps={"EURUSD": 1.0},
        proxy_provenance=PROVENANCE,
    )
    assert result["search_accounting"]["prior_attempted_cells"] == 3_090
    assert len(result["event_ledger"]) == sum(
        len(cell["event_ids"]) for cell in result["cells"]
    )
    assert len(result["trade_ledger"]) == sum(
        len(cell["trade_event_ids"]) for cell in result["cells"]
    )
    scored_event_ids = {
        row["event_id"]
        for row in result["event_ledger"]
        if row["reservation_status"] == "scored"
    }
    assert scored_event_ids == {
        row["event_id"] for row in result["trade_ledger"]
    }
    assert result["discovery_gate"]["passed"] is False
    assert result["discovery_gate"]["success_claim_authorized"] is False
    duplicate_trade_gate = msr._apply_discovery_gate(
        cells=result["cells"],
        event_ledger=result["event_ledger"],
        trade_ledger=[*result["trade_ledger"], result["trade_ledger"][0]],
        symbols=("EURUSD",),
        corrected_t_threshold=msr.search_corrected_threshold(3_378),
    )
    assert duplicate_trade_gate["scored_reservation_trade_ids_match"] is False
    json.dumps(result, allow_nan=False)


def test_cli_refuses_colliding_or_preexisting_evidence_outputs(tmp_path: Path) -> None:
    proxy = tmp_path / "proxy.json"
    proxy.write_text(
        json.dumps({"provenance": PROVENANCE, "budgets_bps": {}}),
        encoding="utf-8",
    )
    shared = tmp_path / "shared.json"
    with pytest.raises(SystemExit) as collision:
        msr.main(
            [
                "--csv-root",
                str(tmp_path),
                "--proxy-budgets-json",
                str(proxy),
                "--event-ledger-out",
                str(shared),
                "--trade-ledger-out",
                str(shared),
                "--json-out",
                str(tmp_path / "cells.json"),
            ]
        )
    assert collision.value.code == 2

    shared.write_text("do-not-replace", encoding="utf-8")
    with pytest.raises(SystemExit) as preexisting:
        msr.main(
            [
                "--csv-root",
                str(tmp_path),
                "--proxy-budgets-json",
                str(proxy),
                "--event-ledger-out",
                str(shared),
                "--trade-ledger-out",
                str(tmp_path / "trades.json"),
                "--json-out",
                str(tmp_path / "cells.json"),
            ]
        )
    assert preexisting.value.code == 2
    assert shared.read_text(encoding="utf-8") == "do-not-replace"


def test_cli_returns_nonzero_for_parsed_but_insufficient_sources(
    tmp_path: Path,
) -> None:
    proxy = tmp_path / "proxy.json"
    proxy.write_text(
        json.dumps(
            {
                "provenance": PROVENANCE,
                "budgets_bps": {symbol: 1.0 for symbol in msr.FX_SYMBOLS},
            }
        ),
        encoding="utf-8",
    )
    event_path = tmp_path / "events.json"
    trade_path = tmp_path / "trades.json"
    cells_path = tmp_path / "cells.json"
    csv_root = tmp_path / "short-input"
    csv_root.mkdir()
    short_payload = (
        ",".join(msr.CSV_HEADER)
        + "\n2024-01-01T00:00:00Z,1,1,1,1,1.0001,1.0001,1.0001,1.0001,0\n"
    )
    for symbol in msr.FX_SYMBOLS:
        (csv_root / f"{symbol}_M1.csv").write_text(short_payload, encoding="utf-8")
    assert (
        msr.main(
            [
                "--csv-root",
                str(csv_root),
                "--proxy-budgets-json",
                str(proxy),
                "--event-ledger-out",
                str(event_path),
                "--trade-ledger-out",
                str(trade_path),
                "--json-out",
                str(cells_path),
            ]
        )
        == 2
    )
    cells = json.loads(cells_path.read_text(encoding="utf-8"))
    assert len(cells["cost_readiness"]["source_failure_symbols"]) == 18
    assert all(cell["source_ready"] is False for cell in cells["cells"])
    assert all(
        cell["source_error"] == "no_complete_255_bar_m1_run"
        for cell in cells["cells"]
    )
    assert cells["discovery_gate"]["passed"] is False


def test_cli_writes_distinct_hash_bound_cell_and_ledger_artifacts(
    tmp_path: Path,
) -> None:
    bars = _baseline(330)
    _plant_buy(bars, 300)
    csv_root = tmp_path / "input"
    csv_root.mkdir()
    lines = [",".join(msr.CSV_HEADER)]
    for bar in bars:
        timestamp = dt.datetime.fromtimestamp(
            bar.epoch,
            dt.timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        lines.append(
            ",".join(
                [
                    timestamp,
                    *(repr(value) for value in dataclasses.astuple(bar)[1:]),
                    "0",
                ]
            )
        )
    csv_payload = "\n".join(lines) + "\n"
    for symbol in msr.FX_SYMBOLS:
        (csv_root / f"{symbol}_M1.csv").write_text(csv_payload, encoding="utf-8")
    proxy = tmp_path / "proxy.json"
    proxy.write_text(
        json.dumps(
            {
                "provenance": PROVENANCE,
                "budgets_bps": {symbol: 1.0 for symbol in msr.FX_SYMBOLS},
            }
        ),
        encoding="utf-8",
    )
    event_path = tmp_path / "event_ledger.json"
    trade_path = tmp_path / "trade_ledger.json"
    cells_path = tmp_path / "cells.json"

    assert (
        msr.main(
            [
                "--csv-root",
                str(csv_root),
                "--proxy-budgets-json",
                str(proxy),
                "--event-ledger-out",
                str(event_path),
                "--trade-ledger-out",
                str(trade_path),
                "--json-out",
                str(cells_path),
            ]
        )
        == 0
    )
    cells = json.loads(cells_path.read_text(encoding="utf-8"))
    events = json.loads(event_path.read_text(encoding="utf-8"))["events"]
    trades = json.loads(trade_path.read_text(encoding="utf-8"))["trades"]
    assert events
    assert trades
    assert cells["event_ledger_evidence"]["file_sha256"] == hashlib.sha256(
        event_path.read_bytes()
    ).hexdigest()
    assert cells["trade_ledger_evidence"]["file_sha256"] == hashlib.sha256(
        trade_path.read_bytes()
    ).hexdigest()
    assert {
        event["event_id"]
        for event in events
        if event["reservation_status"] == "scored"
    } == {trade["event_id"] for trade in trades}
