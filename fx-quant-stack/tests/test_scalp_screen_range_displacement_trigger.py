"""Synthetic-only tests for the causal Range-Displacement Trigger screen."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import json
import statistics
from pathlib import Path

import pytest

import fxstack.scalp.screen_range_displacement_trigger as rdt


BASE_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z.
PROVENANCE = {
    "venue_id": "synthetic-venue",
    "source_id": "synthetic-frozen-proxy-v1",
    "as_of_utc": "2024-01-01T00:00:00Z",
    "source_cutoff_utc": "2023-12-31T23:59:00Z",
    "method": "synthetic-test-only",
    "units": "bps",
    "source_snapshot_sha256": "a" * 64,
    "frozen": True,
}
EVALUATION_START = "2024-01-01T00:00:00Z"
EVALUATION_END = "2025-01-01T00:00:00Z"
EVALUATION_END_EPOCH = 1_735_689_600
PROXY_SCHEMA = rdt.PROXY_CONTRACT_SCHEMA_VERSION


def _bar(
    index: int,
    *,
    mid_o: float = 100.0,
    mid_h: float = 100.10,
    mid_l: float = 99.90,
    mid_c: float = 100.0,
    spread_bps: float = 0.20,
    volume: float = 1.0,
    epoch_shift: int = 0,
) -> rdt.QuoteBar:
    def quotes(mid: float) -> tuple[float, float]:
        half = mid * spread_bps / 2e4
        return mid - half, mid + half

    bid_o, ask_o = quotes(mid_o)
    bid_h, ask_h = quotes(mid_h)
    bid_l, ask_l = quotes(mid_l)
    bid_c, ask_c = quotes(mid_c)
    return rdt.QuoteBar(
        epoch=BASE_EPOCH + index * 60 + epoch_shift,
        bid_o=bid_o,
        bid_h=bid_h,
        bid_l=bid_l,
        bid_c=bid_c,
        ask_o=ask_o,
        ask_h=ask_h,
        ask_l=ask_l,
        ask_c=ask_c,
        volume=volume,
    )


def _baseline(
    count: int = 900, *, spread_bps: float = 0.20
) -> list[rdt.QuoteBar]:
    return [_bar(index, spread_bps=spread_bps) for index in range(count)]


def _write_m1_csv(path: Path, bars: list[rdt.QuoteBar]) -> None:
    lines = [",".join(rdt.CSV_HEADER)]
    for bar in bars:
        timestamp = dt.datetime.fromtimestamp(
            bar.epoch,
            dt.timezone.utc,
        ).isoformat().replace("+00:00", "Z")
        values = (
            bar.bid_o,
            bar.bid_h,
            bar.bid_l,
            bar.bid_c,
            bar.ask_o,
            bar.ask_h,
            bar.ask_l,
            bar.ask_c,
            bar.volume,
        )
        lines.append(",".join((timestamp, *(format(value, ".12g") for value in values))))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _plant_buy(
    bars: list[rdt.QuoteBar],
    index: int,
    *,
    spread_bps: float = 0.20,
    target: bool = True,
) -> None:
    bars[index] = _bar(
        index,
        mid_o=100.0,
        mid_h=100.22,
        mid_l=99.98,
        mid_c=100.20,
        spread_bps=spread_bps,
    )
    bars[index + 1] = _bar(
        index + 1,
        mid_o=100.20,
        mid_h=100.25,
        mid_l=100.15,
        mid_c=100.20,
        spread_bps=spread_bps,
    )
    if target:
        bars[index + 2] = _bar(
            index + 2,
            mid_o=100.20,
            mid_h=100.70,
            mid_l=100.15,
            mid_c=100.50,
            spread_bps=spread_bps,
        )


def _plant_sell(
    bars: list[rdt.QuoteBar],
    index: int,
    *,
    spread_bps: float = 0.20,
    target: bool = True,
) -> None:
    bars[index] = _bar(
        index,
        mid_o=100.0,
        mid_h=100.02,
        mid_l=99.78,
        mid_c=99.80,
        spread_bps=spread_bps,
    )
    bars[index + 1] = _bar(
        index + 1,
        mid_o=99.80,
        mid_h=99.85,
        mid_l=99.75,
        mid_c=99.80,
        spread_bps=spread_bps,
    )
    if target:
        bars[index + 2] = _bar(
            index + 2,
            mid_o=99.80,
            mid_h=99.85,
            mid_l=99.30,
            mid_c=99.50,
            spread_bps=spread_bps,
        )


STRICT_CONFIG = rdt.RDTConfig(24, 0.75, 0.85)


def _evaluate(
    bars: list[rdt.QuoteBar],
    index: int,
    side: str,
    *,
    config: rdt.RDTConfig = STRICT_CONFIG,
    budget: float = 1.0,
) -> tuple[rdt.RDTSignal | None, str]:
    return rdt.evaluate_signal(
        prepared=rdt.prepare_series(bars),
        signal_index=index,
        symbol="EURUSD",
        side=side,
        config=config,
        proxy_spread_budget_bps=budget,
    )


def _signal(
    bars: list[rdt.QuoteBar],
    index: int,
    side: str,
    *,
    config: rdt.RDTConfig = STRICT_CONFIG,
    budget: float = 1.0,
) -> rdt.RDTSignal:
    signal, reason = _evaluate(
        bars,
        index,
        side,
        config=config,
        budget=budget,
    )
    assert signal is not None, reason
    return signal


def test_future_outcomes_cannot_change_frozen_context_or_signal() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    before_prepared = rdt.prepare_series(bars)
    before_context = rdt.baseline_context_at(
        before_prepared, signal_index=300, config=STRICT_CONFIG
    )
    before_signal, reason = rdt.evaluate_signal(
        prepared=before_prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert before_signal is not None, reason

    changed = list(bars)
    # The t+1 open is the only future execution input.  Everything after that
    # is outcome data and cannot alter a frozen signal object.
    for index in range(302, len(changed)):
        changed[index] = _bar(
            index,
            mid_o=130.0,
            mid_h=131.0,
            mid_l=129.0,
            mid_c=130.5,
            spread_bps=4.0,
        )
    after_prepared = rdt.prepare_series(changed)
    after_context = rdt.baseline_context_at(
        after_prepared, signal_index=300, config=STRICT_CONFIG
    )
    after_signal, reason = rdt.evaluate_signal(
        prepared=after_prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert after_signal is not None, reason
    assert after_context == before_context
    assert after_signal == before_signal


def test_volume_is_validated_source_metadata_but_never_a_signal_or_exit_input() -> None:
    bars = _planted_buy_baseline()
    baseline_prepared = rdt.prepare_series(bars)
    baseline_context = rdt.baseline_context_at(
        baseline_prepared,
        signal_index=300,
        config=STRICT_CONFIG,
    )
    baseline_signal = _signal(bars, 300, "BUY")
    baseline_trade = rdt.simulate_trade(bars, signal=baseline_signal)

    changed = [
        dataclasses.replace(bar, volume=0.0 if index % 2 else 1_000_000.0)
        for index, bar in enumerate(bars)
    ]
    changed_prepared = rdt.prepare_series(changed)
    changed_context = rdt.baseline_context_at(
        changed_prepared,
        signal_index=300,
        config=STRICT_CONFIG,
    )
    changed_signal = _signal(changed, 300, "BUY")
    changed_trade = rdt.simulate_trade(changed, signal=changed_signal)
    assert changed_context == baseline_context
    assert changed_signal == baseline_signal
    assert changed_trade == baseline_trade


def test_context_is_exactly_240_bars_ending_t_minus_1() -> None:
    bars = _baseline(500)
    _plant_buy(bars, 300)
    context = rdt.baseline_context_at(
        rdt.prepare_series(bars), signal_index=300, config=STRICT_CONFIG
    )
    assert context is not None
    expected_tr = statistics.median(
        rdt._true_range_bps(bars[index - 1], bars[index])
        for index in range(60, 300)
    )
    expected_q25 = rdt._quantile_sorted(
        sorted(bars[index].spread_close_bps for index in range(60, 300)),
        0.25,
    )
    assert context.volatility_bps == pytest.approx(expected_tr)
    assert context.spread_q25_bps == pytest.approx(expected_q25)
    assert context.prior_bid_high == max(
        bars[index].bid_h for index in range(276, 300)
    )
    assert context.prior_ask_low == min(
        bars[index].ask_l for index in range(276, 300)
    )

    changed = list(bars)
    changed[300] = _bar(
        300,
        mid_o=80.0,
        mid_h=130.0,
        mid_l=70.0,
        mid_c=125.0,
    )
    changed_context = rdt.baseline_context_at(
        rdt.prepare_series(changed), signal_index=300, config=STRICT_CONFIG
    )
    assert changed_context == context


def test_context_and_fill_require_strict_m1_continuity() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)

    broken_baseline = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 100 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_baseline, 300, "BUY")
    assert candidate is None
    assert reason == "strict_pre_signal_context_unavailable"

    broken_signal = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 300 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_signal, 300, "BUY")
    assert candidate is None
    assert reason == "strict_pre_signal_context_unavailable"

    broken_fill = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_fill, 300, "BUY")
    assert candidate is None
    assert reason == "exact_next_open_gap"


def test_buy_and_sell_formulas_are_quote_side_mirrors() -> None:
    buy_bars = _baseline()
    sell_bars = _baseline()
    _plant_buy(buy_bars, 300)
    _plant_sell(sell_bars, 300)
    buy = _signal(buy_bars, 300, "BUY")
    sell = _signal(sell_bars, 300, "SELL")

    assert buy.displacement_bps == pytest.approx(sell.displacement_bps, rel=2e-3)
    assert buy.displacement_vol_units == pytest.approx(
        sell.displacement_vol_units, rel=2e-3
    )
    assert buy.close_location == pytest.approx(sell.close_location, rel=2e-3)
    assert buy.risk_bps == pytest.approx(sell.risk_bps, rel=5e-3)
    assert buy.p_star == pytest.approx(sell.p_star, rel=5e-3)
    assert buy.target_price > buy.entry_price > buy.stop_price
    assert sell.target_price < sell.entry_price < sell.stop_price


def test_range_cross_displacement_and_close_location_are_all_required() -> None:
    not_crossed = _baseline()
    _plant_buy(not_crossed, 300)
    not_crossed[300] = _bar(
        300,
        mid_o=100.15,
        mid_h=100.35,
        mid_l=100.14,
        mid_c=100.32,
    )
    candidate, reason = _evaluate(not_crossed, 300, "BUY")
    assert candidate is None
    assert reason == "prior_range_not_crossed_from_inside"

    weak_displacement = _baseline()
    _plant_buy(weak_displacement, 300)
    weak_displacement[300] = _bar(
        300,
        mid_o=100.09,
        mid_h=100.115,
        mid_l=100.085,
        mid_c=100.11,
    )
    candidate, reason = _evaluate(weak_displacement, 300, "BUY")
    assert candidate is None
    assert reason == "displacement_too_small"

    weak_location = _baseline()
    _plant_buy(weak_location, 300)
    weak_location[300] = _bar(
        300,
        mid_o=100.0,
        mid_h=100.50,
        mid_l=99.98,
        mid_c=100.20,
    )
    candidate, reason = _evaluate(weak_location, 300, "BUY")
    assert candidate is None
    assert reason == "close_location_too_weak"


def test_fill_is_exact_t_plus_1_quote_open_without_fallback() -> None:
    next_bar = _bar(1, mid_o=101.0, mid_h=101.2, mid_l=100.8, mid_c=101.0)
    assert rdt.FILL_DELAY_M1_BARS == 1
    assert rdt._entry_price(next_bar, side="BUY") == next_bar.ask_o
    assert rdt._entry_price(next_bar, side="SELL") == next_bar.bid_o

    bars = _baseline()
    _plant_buy(bars, 300)
    bars[301] = _bar(
        301,
        mid_o=100.18,
        mid_h=100.25,
        mid_l=100.14,
        mid_c=100.20,
    )
    signal = _signal(bars, 300, "BUY")
    assert signal.entry_price == bars[301].ask_o
    assert signal.entry_price != bars[300].ask_c


def test_q25_and_proxy_cap_both_signal_and_entry_spreads() -> None:
    signal_wide = _baseline()
    _plant_buy(signal_wide, 300, spread_bps=0.30)
    candidate, reason = _evaluate(signal_wide, 300, "BUY")
    assert candidate is None
    assert reason == "signal_spread_above_q25_or_proxy"

    entry_wide = _baseline()
    _plant_buy(entry_wide, 300)
    entry_wide[301] = _bar(
        301,
        mid_o=100.20,
        mid_h=100.25,
        mid_l=100.15,
        mid_c=100.20,
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


def test_risk_cost_and_break_even_gates_are_frozen() -> None:
    assert rdt._floored_risk_bps(2.0) == rdt.FROZEN_STOP_FLOOR_BPS == 4.5
    assert rdt._floored_risk_bps(6.0) == 6.0
    assert (
        rdt._bracket_p_star(4.5, execution_cost_debit_bps=1.0)
        > rdt.P_STAR_MAX
    )
    assert (
        rdt._bracket_p_star(24.0, execution_cost_debit_bps=1.0)
        < rdt.P_STAR_MAX
    )

    excessive_risk = _baseline()
    _plant_buy(excessive_risk, 300)
    excessive_risk[300] = _bar(
        300,
        mid_o=100.0,
        mid_h=100.22,
        mid_l=99.70,
        mid_c=100.20,
    )
    candidate, reason = _evaluate(excessive_risk, 300, "BUY")
    assert candidate is None
    assert reason == "risk_above_25bps"

    proxy_stress_dead = _baseline()
    _plant_buy(proxy_stress_dead, 300)
    candidate, reason = _evaluate(proxy_stress_dead, 300, "BUY", budget=6.0)
    assert candidate is None
    assert reason == "bracket_cost_dead"

    signal = _signal(_planted_buy_baseline(), 300, "BUY")
    assert signal.risk_bps <= 25.0
    assert signal.p_star <= 0.55
    assert signal.quote_target_bps == pytest.approx(signal.risk_bps)
    assert signal.quote_target_bps >= 4.0 * signal.recorded_cost_bps
    assert signal.recorded_cost_bps == pytest.approx(2.0)
    assert signal.incremental_spread_stress_bps == pytest.approx(0.8)
    assert signal.execution_cost_debit_bps == pytest.approx(1.8)
    assert signal.extra_round_trip_cost_bps == 1.0


def test_proxy_excess_changes_pstar_and_is_debited_from_scored_pnl() -> None:
    bars = _planted_buy_baseline()
    low_stress = _signal(bars, 300, "BUY", budget=0.5)
    high_stress = _signal(bars, 300, "BUY", budget=1.5)
    low_trade = rdt.simulate_trade(bars, signal=low_stress)
    high_trade = rdt.simulate_trade(bars, signal=high_stress)
    assert low_trade is not None and high_trade is not None
    assert high_stress.execution_cost_debit_bps > low_stress.execution_cost_debit_bps
    assert high_stress.p_star > low_stress.p_star
    assert high_trade.pnl_bps < low_trade.pnl_bps
    assert low_trade.pnl_bps - high_trade.pnl_bps == pytest.approx(1.0)


def _planted_buy_baseline() -> list[rdt.QuoteBar]:
    bars = _baseline()
    _plant_buy(bars, 300)
    return bars


def test_complete_horizon_is_prevalidated_before_early_target_read() -> None:
    bars = _planted_buy_baseline()
    gapped = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 305 else 0))
        for index, bar in enumerate(bars)
    ]
    signal = _signal(gapped, 300, "BUY")
    assert gapped[302].bid_h >= signal.target_price
    assert rdt.outcome_horizon_is_complete(gapped, entry_index=301) is False
    assert rdt.simulate_trade(gapped, signal=signal) is None


def test_missing_exact_fill_reserves_before_later_same_day_signal() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_buy(bars, 600)
    missing_fill = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    cell = rdt.screen_cell(
        prepared=rdt.prepare_series(missing_fill),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["closed_signal_events"] >= 2
    assert cell["eligible_events"] == 1
    assert cell["entry_day_reservations"] == 1
    assert cell["unresolved_reservations"] == 1
    assert cell["scored_trades"] == 0
    assert cell["reasons"]["exact_next_open_gap"] == 1
    assert cell["reasons"]["entry_day_already_reserved"] >= 1
    reservation = cell["reservation_ledger"][0]
    assert reservation["reservation_status"] == "unresolved"
    assert reservation["outcome_reason"] == "exact_next_open_gap"
    assert reservation["entry_price"] is None
    assert reservation["gate_treatment"] == (
        "unresolved_missing_fill_as_adverse_stop_for_discovery_gate"
    )
    assert reservation["gate_pnl_r"] < -1.0
    assert cell["trade_ledger"] == []


def test_first_unresolved_reservation_blocks_same_day_substitution() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_buy(bars, 600)
    gapped = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 305 else 0))
        for index, bar in enumerate(bars)
    ]
    cell = rdt.screen_cell(
        prepared=rdt.prepare_series(gapped),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["closed_signal_events"] >= 2
    assert cell["eligible_events"] == 1
    assert cell["entry_day_reservations"] == 1
    assert cell["unresolved_reservations"] == 1
    assert cell["scored_trades"] == 0
    assert cell["reasons"]["entry_day_already_reserved"] >= 1
    assert len(cell["reservation_ledger"]) == 1
    reservation = cell["reservation_ledger"][0]
    assert reservation["reservation_status"] == "unresolved"
    assert reservation["outcome_reason"] == "incomplete_outcome_horizon"
    assert reservation["gate_treatment"] == (
        "unresolved_as_adverse_stop_for_discovery_gate"
    )
    assert reservation["gate_pnl_r"] < -1.0
    assert cell["gate_mean_r"] < -1.0
    assert cell["trade_ledger"] == []


def test_unresolved_gate_recomputes_missing_fill_and_incomplete_cost_algebra() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    missing_fill_bars = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    incomplete_bars = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 305 else 0))
        for index, bar in enumerate(bars)
    ]
    missing = rdt.screen_cell(
        prepared=rdt.prepare_series(missing_fill_bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )["reservation_ledger"][0]
    incomplete = rdt.screen_cell(
        prepared=rdt.prepare_series(incomplete_bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )["reservation_ledger"][0]
    key = (STRICT_CONFIG.config_id, "EURUSD", "BUY")
    assert rdt._unresolved_reservation_is_adverse(missing, key=key) is True
    assert rdt._unresolved_reservation_is_adverse(incomplete, key=key) is True
    assert rdt._row_is_within_evaluation_window(
        missing,
        start_epoch=BASE_EPOCH,
        end_epoch=missing["entry_epoch"],
    ) is True
    assert rdt._row_is_within_evaluation_window(
        incomplete,
        start_epoch=BASE_EPOCH,
        end_epoch=incomplete["entry_epoch"],
    ) is False

    mutations = (
        (missing, "execution_cost_debit_bps", missing["execution_cost_debit_bps"] + 0.1),
        (missing, "gate_risk_basis_bps", missing["gate_risk_basis_bps"] + 0.1),
        (missing, "proxy_budget_bps", "1.0"),
        (incomplete, "execution_cost_debit_bps", incomplete["execution_cost_debit_bps"] + 0.1),
        (incomplete, "raw_risk_bps", -1.0),
        (incomplete, "p_star", 0.999),
    )
    for original, field, value in mutations:
        forged = dict(original)
        forged[field] = value
        assert rdt._unresolved_reservation_is_adverse(forged, key=key) is False

    forged_extra = dict(missing)
    forged_extra["unexpected"] = True
    assert rdt._unresolved_reservation_is_adverse(forged_extra, key=key) is False


def test_reservation_day_is_exact_utc_entry_day_across_midnight() -> None:
    bars = _baseline(1_600)
    _plant_buy(bars, 1_439)
    cell = rdt.screen_cell(
        prepared=rdt.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    reservation = cell["reservation_ledger"][0]
    assert rdt._utc_day(reservation["signal_epoch"]) == "2024-01-01"
    assert reservation["entry_day"] == "2024-01-02"
    assert rdt._utc_day(reservation["entry_epoch"]) == "2024-01-02"


def test_adverse_gap_target_cap_and_ambiguous_stop_ordering() -> None:
    bars = _planted_buy_baseline()
    signal = _signal(bars, 300, "BUY")

    adverse_gap = list(bars)
    gap_mid = signal.stop_price - 0.10
    adverse_gap[302] = _bar(
        302,
        mid_o=gap_mid,
        mid_h=gap_mid + 0.05,
        mid_l=gap_mid - 0.05,
        mid_c=gap_mid,
    )
    gap_trade = rdt.simulate_trade(adverse_gap, signal=signal)
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
    ambiguous_trade = rdt.simulate_trade(ambiguous, signal=signal)
    assert ambiguous_trade is not None
    assert ambiguous_trade.exit_reason == "sl_double_touch"
    assert ambiguous_trade.full_target_win is False
    assert ambiguous_trade.pnl_r < -1.0

    favorable_gap = list(bars)
    target_gap_mid = signal.target_price + 0.10
    favorable_gap[302] = _bar(
        302,
        mid_o=target_gap_mid,
        mid_h=target_gap_mid + 0.05,
        mid_l=target_gap_mid - 0.05,
        mid_c=target_gap_mid,
    )
    target_trade = rdt.simulate_trade(favorable_gap, signal=signal)
    assert target_trade is not None
    assert target_trade.exit_reason == "tp_gap_open"
    assert target_trade.exit_price == signal.target_price
    assert target_trade.full_target_win is True


def test_positive_time_stop_is_not_a_full_target_win() -> None:
    bars = _baseline()
    _plant_buy(bars, 300, target=False)
    for index in range(301, 313):
        close = 100.32 if index == 312 else 100.20
        bars[index] = _bar(
            index,
            mid_o=100.20,
            mid_h=100.34,
            mid_l=100.15,
            mid_c=close,
        )
    cell = rdt.screen_cell(
        prepared=rdt.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["scored_trades"] == 1
    assert cell["positive_outcomes"] == 1
    assert cell["full_target_wins"] == 0
    assert cell["full_target_trade_win_rate"] == 0.0
    assert cell["full_target_reservation_rate"] == 0.0
    assert cell["exit_mix"] == {"time_stop": 1}


def test_planted_buy_and_sell_produce_separate_positive_trade_rows() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_sell(bars, 600)
    prepared = rdt.prepare_series(bars)
    buy = rdt.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    sell = rdt.screen_cell(
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
        assert cell["reservation_ledger"][0]["reservation_status"] == "scored"


def test_all_288_zero_cells_are_retained_and_fail_closed() -> None:
    budgets = {
        symbol: 0.6 + index / 100.0
        for index, symbol in enumerate(rdt.FX_SYMBOLS)
    }
    result = rdt.screen_universe(
        bars_by_symbol={},
        proxy_spread_budgets_bps=budgets,
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    assert len(result["cells"]) == 288
    assert all(cell["scored_trades"] == 0 for cell in result["cells"])
    assert all(cell["gate_ledger_consistent"] is True for cell in result["cells"])
    assert all(
        cell["gate_proxy_contract_binding_consistent"] is True
        for cell in result["cells"]
    )
    assert all(cell["source_ready"] is False for cell in result["cells"])
    assert all(cell["proxy_budget_ready"] is True for cell in result["cells"])
    assert all(
        cell["cost_mode"] == "pair_specific_frozen_proxy_spread_stress"
        for cell in result["cells"]
    )
    assert len(result["cost_readiness"]["source_failure_symbols"]) == 18
    assert result["economic_claim_ready"] is False
    assert result["economics_claim_ready"] is False
    assert result["success_claim_authorized"] is False
    assert result["activation_authorized"] is False
    assert result["registry_write_authorized"] is False
    assert result["order_authorized"] is False
    assert result["discovery_gate"]["passed"] is False
    assert result["discovery_gate"]["passing_config_ids"] == []
    assert all(
        cell["passes_discovery_cell_gate"] is False for cell in result["cells"]
    )
    assert result["reservation_ledger"] == []
    assert result["trade_ledger"] == []

    invalid_contract = rdt.screen_universe(
        bars_by_symbol={},
        proxy_spread_budgets_bps=budgets,
        proxy_provenance=PROVENANCE | {"frozen": False},
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    assert all(
        cell["proxy_budget_ready"] is False for cell in invalid_contract["cells"]
    )
    assert all(
        cell["cost_mode"] == "cost_unavailable"
        for cell in invalid_contract["cells"]
    )


def test_discovery_gate_rejects_forged_unresolved_wins_for_all_288_cells() -> None:
    cells: list[dict[str, object]] = []
    reservations: list[dict[str, object]] = []
    for config in rdt.GRID:
        for symbol in rdt.FX_SYMBOLS:
            for side in ("BUY", "SELL"):
                event_ids: list[str] = []
                for offset in range(rdt.MIN_ALL_WIN_RESERVATIONS_FOR_WILSON):
                    event_id = f"forged-{config.config_id}-{symbol}-{side}-{offset}"
                    event_ids.append(event_id)
                    reservations.append(
                        {
                            "event_id": event_id,
                            "config_id": config.config_id,
                            "symbol": symbol,
                            "side": side,
                            "entry_day": (
                                dt.date(2024, 1, 1) + dt.timedelta(days=offset)
                            ).isoformat(),
                            "reservation_status": "unresolved",
                            "outcome_reason": "incomplete_outcome_horizon",
                            "gate_treatment": (
                                "unresolved_as_adverse_stop_for_discovery_gate"
                            ),
                            "gate_pnl_r": 1.0,
                            "pnl_bps": None,
                            "pnl_r": None,
                            "exit_epoch": None,
                            "exit_price": None,
                            "bars_held": None,
                            "full_target_win": True,
                            "positive_outcome": True,
                        }
                    )
                cells.append(
                    {
                        "config_id": config.config_id,
                        "symbol": symbol,
                        "side": side,
                        "entry_day_reservations": len(event_ids),
                        "unresolved_reservations": len(event_ids),
                        "scored_trades": rdt.MIN_TRADES_PER_CELL,
                        "full_target_wins": len(event_ids),
                        "full_target_trade_win_rate": 1.0,
                        "full_target_reservation_rate": 1.0,
                        "positive_outcomes": len(event_ids),
                        "total_r": 0.0,
                        "mean_r": 0.0,
                        "gate_total_r": float(len(event_ids)),
                        "gate_mean_r": 1.0,
                        "exit_mix": {},
                        "reservation_event_ids": event_ids,
                        "trade_event_ids": [],
                        "source_ready": True,
                        "proxy_budget_ready": True,
                    }
                )

    gate = rdt._apply_discovery_gate(
        cells=cells,
        reservation_ledger=reservations,
        trade_ledger=[],
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=EVALUATION_END_EPOCH,
        expected_proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        prepared_series_by_symbol={},
    )
    assert gate["full_canonical_universe_present"] is True
    assert gate["scored_reservation_trade_ids_match"] is True
    assert gate["passed"] is False
    assert gate["passing_config_ids"] == []
    assert all(cell["gate_ledger_consistent"] is False for cell in cells)
    assert all(cell["passes_discovery_cell_gate"] is False for cell in cells)


def test_discovery_gate_rejects_full_family_scored_rows_missing_frozen_semantics() -> None:
    cells: list[dict[str, object]] = []
    reservations: list[dict[str, object]] = []
    trades: list[dict[str, object]] = []
    observations = rdt.MIN_ALL_WIN_RESERVATIONS_FOR_WILSON
    for config in rdt.GRID:
        for symbol in rdt.FX_SYMBOLS:
            for side in ("BUY", "SELL"):
                event_ids: list[str] = []
                for offset in range(observations):
                    signal_epoch = BASE_EPOCH + offset * 86_400
                    entry_epoch = signal_epoch + 60
                    event_id = rdt._event_id_fields(
                        config_id=config.config_id,
                        symbol=symbol,
                        side=side,
                        signal_epoch=signal_epoch,
                        entry_epoch=entry_epoch,
                    )
                    event_ids.append(event_id)
                    entry_price = 1.0
                    stop_price = 0.999 if side == "BUY" else 1.001
                    target_price = 1.001 if side == "BUY" else 0.999
                    pnl_bps = 9.0
                    pnl_r = 0.9
                    trade = {
                        "event_id": event_id,
                        "config_id": config.config_id,
                        "symbol": symbol,
                        "side": side,
                        "signal_epoch": signal_epoch,
                        "entry_epoch": entry_epoch,
                        "entry_day": rdt._utc_day(entry_epoch),
                        "exit_epoch": entry_epoch,
                        "entry_price": entry_price,
                        "exit_price": target_price,
                        "risk_bps": 10.0,
                        "pnl_bps": pnl_bps,
                        "pnl_r": pnl_r,
                        "bars_held": 1,
                        "exit_reason": "tp",
                        "full_target_win": True,
                        "positive_outcome": True,
                    }
                    trades.append(trade)
                    # This is the formerly accepted minimal scored schema.  It
                    # deliberately omits indices, raw risk, signal geometry,
                    # spread observations/stress, recorded cost, and p-star.
                    reservations.append(
                        {
                            **trade,
                            "stop_price": stop_price,
                            "target_price": target_price,
                            "execution_cost_debit_bps": 1.0,
                            "reservation_status": "scored",
                            "outcome_reason": "tp",
                            "gate_pnl_r": pnl_r,
                            "gate_treatment": "observed_trade",
                        }
                    )
                total_r = observations * 0.9
                cells.append(
                    {
                        "config_id": config.config_id,
                        "channel_lookback": config.channel_lookback,
                        "displacement_vol": config.displacement_vol,
                        "close_location_floor": config.close_location_floor,
                        "symbol": symbol,
                        "side": side,
                        "eligible_events": observations,
                        "entry_day_reservations": observations,
                        "unresolved_reservations": 0,
                        "scored_trades": observations,
                        "full_target_wins": observations,
                        "full_target_trade_win_rate": 1.0,
                        "full_target_reservation_rate": 1.0,
                        "positive_outcomes": observations,
                        "positive_outcome_rate": 1.0,
                        "total_r": total_r,
                        "mean_r": 0.9,
                        "gate_total_r": total_r,
                        "gate_mean_r": 0.9,
                        "exit_mix": {"tp": observations},
                        "reservation_event_ids": event_ids,
                        "trade_event_ids": event_ids,
                        "source_ready": True,
                        "source_error": None,
                        "proxy_budget_bps": 1.0,
                        "proxy_budget_ready": True,
                        "proxy_contract_ready": True,
                        "cost_mode": "pair_specific_frozen_proxy_spread_stress",
                    }
                )

    gate = rdt._apply_discovery_gate(
        cells=cells,
        reservation_ledger=reservations,
        trade_ledger=trades,
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=EVALUATION_END_EPOCH,
        expected_proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        prepared_series_by_symbol={},
    )
    assert gate["full_canonical_universe_present"] is True
    assert gate["passed"] is False
    assert gate["passing_config_ids"] == []
    assert all(cell["gate_row_semantics_consistent"] is False for cell in cells)
    assert all(cell["passes_discovery_cell_gate"] is False for cell in cells)


def test_flagged_or_short_source_cannot_emit_evidence() -> None:
    bars = _planted_buy_baseline()
    budgets = {symbol: 1.0 for symbol in rdt.FX_SYMBOLS}
    flagged = rdt.screen_universe(
        bars_by_symbol={"EURUSD": bars},
        proxy_spread_budgets_bps=budgets,
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        source_errors={"EURUSD": "caller_flagged_source_failure"},
    )
    assert flagged["reservation_ledger"] == []
    assert flagged["trade_ledger"] == []
    eurusd_cells = [cell for cell in flagged["cells"] if cell["symbol"] == "EURUSD"]
    assert all(cell["source_ready"] is False for cell in eurusd_cells)

    short = _baseline(253)
    _plant_buy(short, 243)
    short_result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": short},
        proxy_spread_budgets_bps=budgets,
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    short_cells = [
        cell for cell in short_result["cells"] if cell["symbol"] == "EURUSD"
    ]
    assert short_result["reservation_ledger"] == []
    assert short_result["trade_ledger"] == []
    assert all(
        cell["source_error"] == "no_complete_254_bar_m1_run"
        for cell in short_cells
    )


def test_evaluation_start_is_bound_to_actual_bar_epochs() -> None:
    result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": _planted_buy_baseline()},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc="2030-01-01T00:00:00Z",
        evaluation_end_utc="2031-01-01T00:00:00Z",
    )
    eurusd_cells = [
        cell for cell in result["cells"] if cell["symbol"] == "EURUSD"
    ]
    assert all(cell["source_ready"] is False for cell in eurusd_cells)
    assert all(
        cell["source_error"] == "bar_before_evaluation_start"
        for cell in eurusd_cells
    )
    assert result["reservation_ledger"] == []
    assert result["trade_ledger"] == []


def test_grid_trial_ledger_and_bonferroni_student_t_threshold_are_immutable() -> None:
    assert len(rdt.GRID) == 8
    assert {config.channel_lookback for config in rdt.GRID} == {12, 24}
    assert {config.displacement_vol for config in rdt.GRID} == {0.50, 0.75}
    assert {config.close_location_floor for config in rdt.GRID} == {0.70, 0.85}
    assert rdt.trial_accounting() == {
        "grid_configurations": 8,
        "directions": 2,
        "symbols": 18,
        "current_attempted_cells": 288,
        "prior_attempted_cells": 3_666,
        "cumulative_attempted_cells": 3_954,
        "expected_full_universe_cells": 288,
    }
    assert rdt.TWO_SIDED_BONFERRONI_FAMILY_ALPHA == 0.05
    assert rdt.BONFERRONI_STUDENT_T_MIN_DF == 99
    assert rdt.MIN_INDEPENDENT_ENTRY_DAYS == 100
    assert rdt.MIN_INDEPENDENT_ENTRY_DAYS - 1 == rdt.BONFERRONI_STUDENT_T_MIN_DF
    assert (
        rdt.TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        == 4.597560439065422
    )
    assert rdt.SIMULTANEOUS_WILSON_FAMILY_CELLS == 288
    assert rdt.MIN_ALL_WIN_RESERVATIONS_FOR_WILSON == 116


def test_discovery_statistics_are_simultaneous_and_fail_small_samples() -> None:
    assert rdt._one_sided_wilson_lower_bound(0, 0) == 0.0
    assert rdt._one_sided_wilson_lower_bound(90, 100) < 0.90
    assert rdt._one_sided_wilson_lower_bound(100, 100) < 0.90
    assert rdt._one_sided_wilson_lower_bound(115, 115) < 0.90
    assert rdt._one_sided_wilson_lower_bound(116, 116) >= 0.90
    assert (
        rdt._one_sided_wilson_lower_bound(
            159,
            159,
            simultaneous_cells=3_954,
        )
        < 0.90
    )
    assert (
        rdt._one_sided_wilson_lower_bound(
            160,
            160,
            simultaneous_cells=3_954,
        )
        >= 0.90
    )
    assert rdt._one_sided_wilson_lower_bound(1_000, 1_000) > 0.90
    assert rdt._finite_one_sample_t([]) == 0.0
    assert rdt._finite_one_sample_t([1.0]) == 0.0
    assert rdt._finite_one_sample_t([1.0, 1.0]) == 1e12
    assert rdt._finite_one_sample_t([-1.0, -1.0]) == -1e12


def test_bonferroni_student_t_output_topology_and_reserved_day_value() -> None:
    result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": _planted_buy_baseline()},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    search_accounting = result["search_accounting"]
    assert set(search_accounting) == {
        "grid_configurations",
        "directions",
        "symbols",
        "current_attempted_cells",
        "prior_attempted_cells",
        "cumulative_attempted_cells",
        "expected_full_universe_cells",
        "family_alpha",
        "two_sided_bonferroni_student_t_min_df99_abs_threshold",
    }
    assert search_accounting["family_alpha"] == 0.05
    assert (
        search_accounting[
            "two_sided_bonferroni_student_t_min_df99_abs_threshold"
        ]
        == 4.597560439065422
    )

    gate = result["discovery_gate"]
    assert set(gate) == {
        "passed",
        "passing_config_ids",
        "unchanged_single_configuration_required",
        "full_canonical_universe_required",
        "full_canonical_universe_present",
        "evaluation_window_valid",
        "evaluation_start_epoch",
        "evaluation_end_epoch",
        "expected_proxy_mapping_complete",
        "prepared_source_mapping_complete",
        "pair_direction_cells_per_configuration",
        "simultaneous_wilson_family_cells",
        "minimum_scored_trades_per_cell",
        "minimum_independent_entry_days_per_cell",
        "minimum_observed_full_target_reservation_rate",
        "minimum_simultaneous_wilson_lower_bound",
        "minimum_all_win_reservations_for_wilson",
        "wilson_method",
        "wilson_scope",
        "minimum_gate_mean_r",
        "reserved_utc_day_observation_unit",
        "reserved_utc_day_one_sample_t_multiplicity_method",
        "reserved_utc_day_bonferroni_family_cells",
        "reserved_utc_day_bonferroni_family_alpha",
        "reserved_utc_day_one_sample_t_degrees_of_freedom_floor",
        "minimum_reserved_utc_day_one_sample_t",
        "temporal_thirds_role",
        "temporal_thirds_partition_formula",
        "temporal_thirds_half_open_boundary_epochs",
        "temporal_thirds_include_all_exact_reservations",
        "temporal_thirds_include_adverse_unresolved",
        "temporal_thirds_pooling_or_selection_allowed",
        "temporal_thirds_minimum_reservations_per_segment",
        "temporal_thirds_minimum_full_target_rate_numerator",
        "temporal_thirds_minimum_full_target_rate_denominator",
        "temporal_thirds_minimum_gate_total_r_exclusive",
        "temporal_thirds_inferential_claim_authorized",
        "unresolved_treatment",
        "scored_reservation_trade_ids_match",
        "success_claim_authorized",
        "activation_authorized",
        "order_authorized",
    }
    assert gate["minimum_independent_entry_days_per_cell"] == 100
    assert gate["reserved_utc_day_observation_unit"] == (
        "one_gate_pnl_r_per_unique_reserved_utc_entry_day"
    )
    assert gate["reserved_utc_day_one_sample_t_multiplicity_method"] == (
        "dependence_robust_two_sided_bonferroni_student_t"
    )
    assert gate["reserved_utc_day_bonferroni_family_cells"] == 3_954
    assert gate["reserved_utc_day_bonferroni_family_alpha"] == 0.05
    assert gate["reserved_utc_day_one_sample_t_degrees_of_freedom_floor"] == 99
    assert gate["minimum_reserved_utc_day_one_sample_t"] == 4.597560439065422
    assert gate["temporal_thirds_role"] == (
        "deterministic_discovery_only_robustness_veto_no_inferential_claim"
    )
    assert gate["temporal_thirds_partition_formula"] == (
        "k=min(2,floor(3*(entry_epoch-evaluation_start_epoch)/"
        "(evaluation_end_epoch-evaluation_start_epoch)))"
    )
    duration = EVALUATION_END_EPOCH - BASE_EPOCH
    assert gate["temporal_thirds_half_open_boundary_epochs"] == [
        BASE_EPOCH,
        BASE_EPOCH + duration // 3,
        BASE_EPOCH + 2 * duration // 3,
        EVALUATION_END_EPOCH,
    ]
    assert gate["temporal_thirds_include_all_exact_reservations"] is True
    assert gate["temporal_thirds_include_adverse_unresolved"] is True
    assert gate["temporal_thirds_pooling_or_selection_allowed"] is False
    assert gate["temporal_thirds_minimum_reservations_per_segment"] == 30
    assert gate["temporal_thirds_minimum_full_target_rate_numerator"] == 9
    assert gate["temporal_thirds_minimum_full_target_rate_denominator"] == 10
    assert gate["temporal_thirds_minimum_gate_total_r_exclusive"] == 0.0
    assert gate["temporal_thirds_inferential_claim_authorized"] is False
    assert gate["success_claim_authorized"] is False
    assert result["success_claim_authorized"] is False

    active = next(cell for cell in result["cells"] if cell["entry_day_reservations"])
    assert set(active) == rdt.RDT_CELL_PRE_GATE_FIELD_NAMES | rdt.RDT_CELL_GATE_FIELD_NAMES
    assert "reserved_utc_day_one_sample_t" in active
    active_key = (active["config_id"], active["symbol"], active["side"])
    reservations = [
        row
        for row in result["reservation_ledger"]
        if (row["config_id"], row["symbol"], row["side"]) == active_key
    ]
    assert len(reservations) == len({row["entry_day"] for row in reservations})
    assert active["reserved_utc_day_one_sample_t"] == rdt._finite_one_sample_t(
        [row["gate_pnl_r"] for row in reservations]
    )
    assert len(active["temporal_thirds_reservation_counts"]) == 3
    assert len(active["temporal_thirds_full_target_wins"]) == 3
    assert len(active["temporal_thirds_full_target_rates"]) == 3
    assert len(active["temporal_thirds_gate_total_rs"]) == 3
    assert len(active["temporal_thirds_gate_mean_rs"]) == 3
    assert active["gate_temporal_thirds_stable"] is False


TEMPORAL_TEST_DAY_SECONDS = 86_400
TEMPORAL_TEST_START = 0
TEMPORAL_TEST_END = 300 * TEMPORAL_TEST_DAY_SECONDS


def _temporal_test_rows(
    segment: int,
    *,
    count: int,
    wins: int,
    winning_return: float = 1.0,
    losing_return: float = -0.25,
) -> list[dict[str, object]]:
    segment_start_day = segment * 100
    return [
        {
            "entry_epoch": (
                segment_start_day + index
            ) * TEMPORAL_TEST_DAY_SECONDS,
            "gate_pnl_r": winning_return if index < wins else losing_return,
            "full_target_win": index < wins,
            "reservation_status": "scored" if index < wins else "unresolved",
        }
        for index in range(count)
    ]


def _temporal_diagnostics(
    reservations: list[dict[str, object]],
) -> dict[str, object]:
    return rdt._temporal_thirds_diagnostics(
        reservations,
        evaluation_start_epoch=TEMPORAL_TEST_START,
        evaluation_end_epoch=TEMPORAL_TEST_END,
    )


def test_temporal_third_partition_uses_exact_half_open_boundaries_and_end_cap() -> None:
    assert rdt._temporal_thirds_boundary_epochs(
        evaluation_start_epoch=0,
        evaluation_end_epoch=300,
    ) == [0, 100, 200, 300]
    expected = {
        0: 0,
        99: 0,
        100: 1,
        199: 1,
        200: 2,
        299: 2,
        300: 2,
    }
    for epoch, segment in expected.items():
        assert (
            rdt._temporal_third_index(
                epoch,
                evaluation_start_epoch=0,
                evaluation_end_epoch=300,
            )
            == segment
        )
    assert (
        rdt._temporal_third_index(
            -1,
            evaluation_start_epoch=0,
            evaluation_end_epoch=300,
        )
        is None
    )
    assert (
        rdt._temporal_third_index(
            301,
            evaluation_start_epoch=0,
            evaluation_end_epoch=300,
        )
        is None
    )


def test_temporal_thirds_reject_sparse_89_percent_and_nonpositive_segments() -> None:
    stable_rows = [
        row
        for segment in range(3)
        for row in _temporal_test_rows(segment, count=30, wins=30)
    ]
    stable = _temporal_diagnostics(stable_rows)
    assert stable["temporal_thirds_reservation_counts"] == [30, 30, 30]
    assert stable["gate_temporal_thirds_stable"] is True

    sparse = _temporal_diagnostics(stable_rows[1:])
    assert sparse["temporal_thirds_reservation_counts"] == [29, 30, 30]
    assert sparse["gate_temporal_thirds_stable"] is False

    eighty_nine_percent_rows = (
        _temporal_test_rows(0, count=100, wins=89)
        + _temporal_test_rows(1, count=30, wins=30)
        + _temporal_test_rows(2, count=30, wins=30)
    )
    eighty_nine_percent = _temporal_diagnostics(eighty_nine_percent_rows)
    assert eighty_nine_percent["temporal_thirds_full_target_wins"][0] == 89
    assert eighty_nine_percent["temporal_thirds_full_target_rates"][0] == 0.89
    assert eighty_nine_percent["gate_temporal_thirds_stable"] is False

    nonpositive_rows = (
        _temporal_test_rows(
            0,
            count=30,
            wins=30,
            winning_return=0.0,
        )
        + _temporal_test_rows(1, count=30, wins=30)
        + _temporal_test_rows(2, count=30, wins=30)
    )
    nonpositive = _temporal_diagnostics(nonpositive_rows)
    assert nonpositive["temporal_thirds_gate_total_rs"][0] == 0.0
    assert nonpositive["gate_temporal_thirds_stable"] is False


def test_temporal_thirds_include_adverse_unresolved_reservations() -> None:
    rows = [
        row
        for segment in range(3)
        for row in _temporal_test_rows(segment, count=30, wins=30)
    ]
    for segment in range(3):
        rows.extend(
            _temporal_test_rows(
                segment,
                count=31,
                wins=30,
                losing_return=-1.0,
            )[30:]
        )
    diagnostics = _temporal_diagnostics(rows)
    assert diagnostics["temporal_thirds_reservation_counts"] == [31, 31, 31]
    assert diagnostics["temporal_thirds_full_target_wins"] == [30, 30, 30]
    assert diagnostics["temporal_thirds_full_target_rates"] == [30 / 31] * 3
    assert diagnostics["temporal_thirds_gate_total_rs"] == [29.0, 29.0, 29.0]
    assert diagnostics["temporal_thirds_gate_mean_rs"] == [29 / 31] * 3
    assert diagnostics["gate_temporal_thirds_stable"] is True


def test_temporal_thirds_recompute_mutation_and_omission_failures() -> None:
    rows = [
        row
        for segment in range(3)
        for row in _temporal_test_rows(segment, count=30, wins=27)
    ]
    baseline = _temporal_diagnostics(rows)
    assert baseline["temporal_thirds_full_target_rates"] == [0.9, 0.9, 0.9]
    assert baseline["gate_temporal_thirds_stable"] is True

    mutated = copy.deepcopy(rows)
    mutated[0]["full_target_win"] = False
    mutation_diagnostics = _temporal_diagnostics(mutated)
    assert mutation_diagnostics["temporal_thirds_full_target_wins"] == [26, 27, 27]
    assert mutation_diagnostics["gate_temporal_thirds_stable"] is False

    omission_diagnostics = _temporal_diagnostics(rows[1:])
    assert omission_diagnostics["temporal_thirds_reservation_counts"] == [29, 30, 30]
    assert omission_diagnostics["gate_temporal_thirds_stable"] is False


def test_csv_requires_exact_header_explicit_utc_whole_minute_and_monotonicity(
    tmp_path: Path,
) -> None:
    bad_header = tmp_path / "bad_header.csv"
    bad_header.write_text(
        "timestamp,bid_o,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close,volume\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected M1 header"):
        rdt.load_m1_csv(bad_header)

    def one_row(path: Path, timestamp: str, *, volume: str = "1") -> None:
        path.write_text(
            ",".join(rdt.CSV_HEADER)
            + f"\n{timestamp},1,1,1,1,1.0001,1.0001,1.0001,1.0001,{volume}\n",
            encoding="utf-8",
        )

    for name, timestamp in (
        ("naive", "2024-01-01T00:00:00"),
        ("non_utc", "2024-01-01T01:00:00+01:00"),
        ("fractional", "2024-01-01T00:00:00.500Z"),
    ):
        path = tmp_path / f"{name}.csv"
        one_row(path, timestamp)
        with pytest.raises(ValueError, match="malformed M1 row"):
            rdt.load_m1_csv(path)

    off_minute = tmp_path / "off_minute.csv"
    one_row(off_minute, "2024-01-01T00:00:01Z")
    with pytest.raises(ValueError, match="invalid bid/ask OHLC"):
        rdt.load_m1_csv(off_minute)

    duplicate = tmp_path / "duplicate.csv"
    duplicate.write_text(
        ",".join(rdt.CSV_HEADER)
        + "\n2024-01-01T00:00:00Z,1,1,1,1,1.0001,1.0001,1.0001,1.0001,1"
        + "\n2024-01-01T00:00:00+00:00,1,1,1,1,1.0001,1.0001,1.0001,1.0001,1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-monotonic timestamp"):
        rdt.load_m1_csv(duplicate, start="2024-02-01T00:00:00Z")

    valid = tmp_path / "valid.csv"
    one_row(valid, "2024-01-01T00:00:00Z", volume="0")
    assert len(rdt.load_m1_csv(valid)) == 1

    for name, volume in (("negative", "-1"), ("nan", "nan"), ("inf", "inf")):
        invalid_volume = tmp_path / f"{name}_volume.csv"
        one_row(
            invalid_volume,
            "2024-01-01T00:00:00Z",
            volume=volume,
        )
        with pytest.raises(ValueError, match="invalid bid/ask OHLC"):
            rdt.load_m1_csv(invalid_volume)


def test_proxy_contract_requires_frozen_strict_utc_provenance(tmp_path: Path) -> None:
    for name, provenance in (
        ("unfrozen", PROVENANCE | {"frozen": False}),
        ("naive", PROVENANCE | {"as_of_utc": "2024-01-01T00:00:00"}),
        (
            "non_utc",
            PROVENANCE | {"as_of_utc": "2024-01-01T01:00:00+01:00"},
        ),
        (
            "future_source_cutoff",
            PROVENANCE | {"source_cutoff_utc": "2024-01-01T00:01:00Z"},
        ),
        ("wrong_units", PROVENANCE | {"units": "pips"}),
        ("bad_snapshot_hash", PROVENANCE | {"source_snapshot_sha256": "abc"}),
        ("unexpected_field", PROVENANCE | {"unbound_note": "not-allowed"}),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": PROXY_SCHEMA,
                    "provenance": provenance,
                    "budgets_bps": {"EURUSD": 0.7},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="missing frozen proxy provenance"):
            rdt._load_proxy_contract(
                path,
                evaluation_start_utc=EVALUATION_START,
            )

    valid = tmp_path / "valid_proxy.json"
    valid.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {"eurusd": 0.7},
            }
        ),
        encoding="utf-8",
    )
    budgets, provenance, schema_version = rdt._load_proxy_contract(
        valid,
        evaluation_start_utc=EVALUATION_START,
    )
    assert budgets == {"EURUSD": 0.7}
    assert provenance == PROVENANCE
    assert schema_version == PROXY_SCHEMA

    wrong_version = tmp_path / "wrong_version.json"
    wrong_version.write_text(
        json.dumps(
            {
                "schema_version": "wrong.v1",
                "provenance": PROVENANCE,
                "budgets_bps": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected proxy contract version"):
        rdt._load_proxy_contract(
            wrong_version,
            evaluation_start_utc=EVALUATION_START,
        )

    duplicate = tmp_path / "duplicate_budget.json"
    duplicate.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {"EURUSD": 0.7, "eurusd": 0.8},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate normalized proxy-budget"):
        rdt._load_proxy_contract(
            duplicate,
            evaluation_start_utc=EVALUATION_START,
        )

    unknown = tmp_path / "unknown_budget.json"
    unknown.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {"XAUUSD": 1.0},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="noncanonical proxy-budget symbol"):
        rdt._load_proxy_contract(
            unknown,
            evaluation_start_utc=EVALUATION_START,
        )


@pytest.mark.parametrize("budget", [True, "0.7", float("nan"), float("inf")])
def test_proxy_contract_rejects_non_numeric_or_nonfinite_budget_tokens(
    tmp_path: Path,
    budget: object,
) -> None:
    path = tmp_path / "invalid_budget_token.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {"EURUSD": budget},
            }
        ),
        encoding="utf-8",
    )
    expected = "non-finite JSON token" if isinstance(budget, float) else "invalid proxy budget"
    with pytest.raises(ValueError, match=expected):
        rdt._load_proxy_contract(path, evaluation_start_utc=EVALUATION_START)


def test_proxy_contract_rejects_exact_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate_exact_key.json"
    path.write_text(
        "{"
        f'"schema_version":{json.dumps(PROXY_SCHEMA)},'
        f'"provenance":{json.dumps(PROVENANCE)},'
        '"budgets_bps":{"EURUSD":0.7,"EURUSD":0.8}'
        "}",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate JSON key: EURUSD"):
        rdt._load_proxy_contract(path, evaluation_start_utc=EVALUATION_START)


def test_screen_and_both_ledgers_serialize_without_nonfinite_values() -> None:
    result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": _planted_buy_baseline()},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    assert result["search_accounting"]["prior_attempted_cells"] == 3_666
    assert len(result["reservation_ledger"]) == sum(
        len(cell["reservation_event_ids"]) for cell in result["cells"]
    )
    assert len(result["trade_ledger"]) == sum(
        len(cell["trade_event_ids"]) for cell in result["cells"]
    )
    active_cells = [
        cell for cell in result["cells"] if cell["entry_day_reservations"] > 0
    ]
    assert active_cells
    assert all(cell["gate_ledger_consistent"] is True for cell in active_cells)
    assert all(cell["gate_row_semantics_consistent"] is True for cell in active_cells)
    assert all(cell["gate_scored_trade_ids_match"] is True for cell in active_cells)
    assert all(cell["gate_ledger_consistent"] is True for cell in result["cells"])
    json.dumps(result, allow_nan=False)


def test_scored_reservation_pnl_must_match_trade_geometry() -> None:
    result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": _planted_buy_baseline()},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    reservation = next(
        row
        for row in result["reservation_ledger"]
        if row["reservation_status"] == "scored"
    )
    reservation["pnl_bps"] += 0.25
    gate = rdt._apply_discovery_gate(
        cells=result["cells"],
        reservation_ledger=result["reservation_ledger"],
        trade_ledger=result["trade_ledger"],
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=EVALUATION_END_EPOCH,
        expected_proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        prepared_series_by_symbol={
            symbol: rdt.prepare_series(
                _planted_buy_baseline() if symbol == "EURUSD" else []
            )
            for symbol in rdt.FX_SYMBOLS
        },
    )
    key = (
        reservation["config_id"],
        reservation["symbol"],
        reservation["side"],
    )
    affected = next(
        cell
        for cell in result["cells"]
        if (cell["config_id"], cell["symbol"], cell["side"]) == key
    )
    assert gate["passed"] is False
    assert affected["gate_row_semantics_consistent"] is False
    assert affected["gate_ledger_consistent"] is False


def test_scored_reservation_gate_recomputes_every_frozen_admission_family() -> None:
    base = rdt.screen_universe(
        bars_by_symbol={"EURUSD": _planted_buy_baseline()},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    scored_index = next(
        index
        for index, row in enumerate(base["reservation_ledger"])
        if row["reservation_status"] == "scored"
    )

    def set_value(field: str, value: object):
        return lambda row: row.__setitem__(field, value)

    mutations = (
        ("missing_field", lambda row: row.pop("signal_index")),
        ("unexpected_field", set_value("not_in_schema", 1)),
        ("entry_index", lambda row: row.__setitem__("entry_index", row["entry_index"] + 2)),
        ("raw_risk", set_value("raw_risk_bps", -1.0)),
        ("risk_cap", set_value("risk_bps", 100.0)),
        (
            "incremental_stress",
            lambda row: row.__setitem__(
                "incremental_spread_stress_bps",
                row["incremental_spread_stress_bps"] + 0.5,
            ),
        ),
        (
            "execution_debit",
            lambda row: row.__setitem__(
                "execution_cost_debit_bps",
                row["execution_cost_debit_bps"] + 0.5,
            ),
        ),
        (
            "recorded_cost",
            lambda row: row.__setitem__(
                "recorded_cost_bps",
                row["recorded_cost_bps"] + 0.5,
            ),
        ),
        ("p_star", set_value("p_star", 0.999)),
        (
            "signal_spread_cap",
            lambda row: row.__setitem__(
                "signal_spread_bps",
                row["proxy_budget_bps"] + 1.0,
            ),
        ),
        (
            "entry_spread_cap",
            lambda row: row.__setitem__(
                "entry_spread_bps",
                row["proxy_budget_bps"] + 1.0,
            ),
        ),
        ("displacement", set_value("displacement_vol_units", 0.0)),
        ("close_location", set_value("close_location", 0.0)),
        ("cost_pad", set_value("extra_round_trip_cost_bps", 0.0)),
    )
    for name, mutate in mutations:
        result = copy.deepcopy(base)
        reservation = result["reservation_ledger"][scored_index]
        mutate(reservation)
        gate = rdt._apply_discovery_gate(
            cells=result["cells"],
            reservation_ledger=result["reservation_ledger"],
            trade_ledger=result["trade_ledger"],
            evaluation_start_epoch=BASE_EPOCH,
            evaluation_end_epoch=EVALUATION_END_EPOCH,
            expected_proxy_spread_budgets_bps={
                symbol: 1.0 for symbol in rdt.FX_SYMBOLS
            },
            prepared_series_by_symbol={
                symbol: rdt.prepare_series(
                    _planted_buy_baseline() if symbol == "EURUSD" else []
                )
                for symbol in rdt.FX_SYMBOLS
            },
        )
        key = (
            reservation["config_id"],
            reservation["symbol"],
            reservation["side"],
        )
        affected = next(
            cell
            for cell in result["cells"]
            if (cell["config_id"], cell["symbol"], cell["side"]) == key
        )
        assert gate["passed"] is False, name
        assert affected["gate_row_semantics_consistent"] is False, name
        assert affected["gate_ledger_consistent"] is False, name


def test_scored_semantics_reject_case_drift_zero_q25_and_impossible_time_stop() -> None:
    result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": _planted_buy_baseline()},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    reservation = next(
        row
        for row in result["reservation_ledger"]
        if row["reservation_status"] == "scored"
    )
    trade = next(
        row
        for row in result["trade_ledger"]
        if row["event_id"] == reservation["event_id"]
    )
    key = (
        reservation["config_id"],
        reservation["symbol"],
        reservation["side"],
    )
    assert rdt._scored_reservation_matches_trade(reservation, trade, key=key) is True

    zero_q25 = dict(reservation)
    zero_q25["spread_q25_bps"] = 0.0
    assert rdt._scored_reservation_matches_trade(zero_q25, trade, key=key) is False

    case_drift = dict(reservation)
    case_drift_trade = dict(trade)
    case_drift["symbol"] = reservation["symbol"].lower()
    case_drift["side"] = reservation["side"].lower()
    case_drift_trade["symbol"] = trade["symbol"].lower()
    case_drift_trade["side"] = trade["side"].lower()
    assert (
        rdt._scored_reservation_matches_trade(
            case_drift,
            case_drift_trade,
            key=key,
        )
        is False
    )

    structural_forgery = dict(reservation)
    structural_forgery["structural_stop"] += 0.01
    assert (
        rdt._scored_reservation_matches_trade(structural_forgery, trade, key=key)
        is False
    )

    impossible = dict(reservation)
    impossible_trade = dict(trade)
    buy = key[2] == "BUY"
    exit_price = (
        reservation["target_price"] * 1.001
        if buy
        else reservation["target_price"] * 0.999
    )
    gross_bps = (
        (exit_price - reservation["entry_price"]) / reservation["entry_price"] * 1e4
        if buy
        else (reservation["entry_price"] - exit_price) / reservation["entry_price"] * 1e4
    )
    pnl_bps = gross_bps - reservation["execution_cost_debit_bps"]
    pnl_r = pnl_bps / reservation["risk_bps"]
    impossible.update(
        {
            "outcome_reason": "time_stop",
            "exit_epoch": reservation["entry_epoch"] + 11 * 60,
            "exit_price": exit_price,
            "bars_held": 12,
            "pnl_bps": pnl_bps,
            "pnl_r": pnl_r,
            "gate_pnl_r": pnl_r,
            "full_target_win": False,
            "positive_outcome": pnl_bps > 0.0,
        }
    )
    impossible_trade.update(
        {
            "exit_epoch": impossible["exit_epoch"],
            "exit_price": exit_price,
            "bars_held": 12,
            "pnl_bps": pnl_bps,
            "pnl_r": pnl_r,
            "exit_reason": "time_stop",
            "full_target_win": False,
            "positive_outcome": pnl_bps > 0.0,
        }
    )
    assert (
        rdt._scored_reservation_matches_trade(impossible, impossible_trade, key=key)
        is False
    )


def test_gate_binds_every_ledger_row_to_declared_evaluation_window() -> None:
    result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": _planted_buy_baseline()},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    gate = rdt._apply_discovery_gate(
        cells=result["cells"],
        reservation_ledger=result["reservation_ledger"],
        trade_ledger=result["trade_ledger"],
        evaluation_start_epoch=1_672_531_200,
        evaluation_end_epoch=1_688_169_600,
        expected_proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        prepared_series_by_symbol={
            symbol: rdt.prepare_series(
                _planted_buy_baseline() if symbol == "EURUSD" else []
            )
            for symbol in rdt.FX_SYMBOLS
        },
    )
    assert gate["passed"] is False
    active_cells = [
        cell for cell in result["cells"] if cell["entry_day_reservations"] > 0
    ]
    assert active_cells
    assert all(cell["gate_temporal_scope_consistent"] is False for cell in active_cells)
    assert all(cell["passes_discovery_cell_gate"] is False for cell in active_cells)


def test_gate_recomputes_exact_cell_contract_and_loaded_proxy_binding() -> None:
    bars = _planted_buy_baseline()
    base = rdt.screen_universe(
        bars_by_symbol={"EURUSD": bars},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    active = next(cell for cell in base["cells"] if cell["entry_day_reservations"])
    active_key = (active["config_id"], active["symbol"], active["side"])
    prepared = {
        symbol: rdt.prepare_series(bars if symbol == "EURUSD" else [])
        for symbol in rdt.FX_SYMBOLS
    }
    mutations = (
        ("one_trade_per_cell_entry_day", False),
        ("outcome_horizon_bars", 999),
        ("reward_risk", 999.0),
        ("stop_floor_bps", 0.0),
        ("max_risk_bps", 999.0),
        ("p_star_max", 1.0),
        ("min_target_cost_ratio", 0.0),
        ("extra_round_trip_cost_bps", 0.0),
        ("economic_claim_ready", True),
        ("economics_claim_ready", True),
        ("closed_signal_events", 0),
    )
    for field, value in mutations:
        result = copy.deepcopy(base)
        cell = next(
            row
            for row in result["cells"]
            if (row["config_id"], row["symbol"], row["side"]) == active_key
        )
        cell[field] = value
        gate = rdt._apply_discovery_gate(
            cells=result["cells"],
            reservation_ledger=result["reservation_ledger"],
            trade_ledger=result["trade_ledger"],
            evaluation_start_epoch=BASE_EPOCH,
            evaluation_end_epoch=EVALUATION_END_EPOCH,
            expected_proxy_spread_budgets_bps={
                symbol: 1.0 for symbol in rdt.FX_SYMBOLS
            },
            prepared_series_by_symbol=prepared,
        )
        assert gate["passed"] is False, field
        assert cell["gate_cell_contract_consistent"] is False, field
        assert cell["gate_ledger_consistent"] is False, field

    selective = copy.deepcopy(base)
    selective_cell = next(
        row
        for row in selective["cells"]
        if (row["config_id"], row["symbol"], row["side"]) == active_key
    )
    selective_cell["proxy_budget_bps"] = 0.5
    for reservation in selective["reservation_ledger"]:
        if (
            reservation["config_id"],
            reservation["symbol"],
            reservation["side"],
        ) == active_key:
            reservation["proxy_budget_bps"] = 0.5
    rdt._apply_discovery_gate(
        cells=selective["cells"],
        reservation_ledger=selective["reservation_ledger"],
        trade_ledger=selective["trade_ledger"],
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=EVALUATION_END_EPOCH,
        expected_proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        prepared_series_by_symbol=prepared,
    )
    assert selective_cell["gate_proxy_contract_binding_consistent"] is False
    assert selective_cell["gate_ledger_consistent"] is False


def test_complete_source_replay_rejects_an_internally_consistent_omission() -> None:
    bars = _planted_buy_baseline()
    result = rdt.screen_universe(
        bars_by_symbol={"EURUSD": bars},
        proxy_spread_budgets_bps={symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
    )
    active = next(
        cell
        for cell in result["cells"]
        if cell["entry_day_reservations"] and cell["scored_trades"]
    )
    active_key = (active["config_id"], active["symbol"], active["side"])
    omitted_ids = set(active["reservation_event_ids"])
    result["reservation_ledger"] = [
        row
        for row in result["reservation_ledger"]
        if row["event_id"] not in omitted_ids
    ]
    result["trade_ledger"] = [
        row for row in result["trade_ledger"] if row["event_id"] not in omitted_ids
    ]
    active.update(
        {
            "eligible_events": 0,
            "entry_day_reservations": 0,
            "scored_trades": 0,
            "unresolved_reservations": 0,
            "full_target_wins": 0,
            "positive_outcomes": 0,
            "full_target_trade_win_rate": 0.0,
            "full_target_reservation_rate": 0.0,
            "positive_outcome_rate": 0.0,
            "total_r": 0.0,
            "mean_r": 0.0,
            "gate_total_r": 0.0,
            "gate_mean_r": 0.0,
            "exit_mix": {},
            "reservation_event_ids": [],
            "trade_event_ids": [],
        }
    )

    gate = rdt._apply_discovery_gate(
        cells=result["cells"],
        reservation_ledger=result["reservation_ledger"],
        trade_ledger=result["trade_ledger"],
        evaluation_start_epoch=BASE_EPOCH,
        evaluation_end_epoch=EVALUATION_END_EPOCH,
        expected_proxy_spread_budgets_bps={
            symbol: 1.0 for symbol in rdt.FX_SYMBOLS
        },
        prepared_series_by_symbol={
            symbol: rdt.prepare_series(bars if symbol == "EURUSD" else [])
            for symbol in rdt.FX_SYMBOLS
        },
    )
    affected = next(
        cell
        for cell in result["cells"]
        if (cell["config_id"], cell["symbol"], cell["side"]) == active_key
    )
    assert gate["passed"] is False
    assert affected["gate_source_replay_consistent"] is False
    assert affected["gate_ledger_consistent"] is False


def test_cli_writes_distinct_hashed_ledgers_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    proxy = tmp_path / "proxy.json"
    proxy.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {},
            }
        ),
        encoding="utf-8",
    )
    reservation = tmp_path / "reservations.json"
    trades = tmp_path / "trades.json"
    cells = tmp_path / "cells.json"
    assert (
        rdt.main(
            [
                "--csv-root",
                str(tmp_path / "empty-input"),
                "--start",
                EVALUATION_START,
                "--end",
                EVALUATION_END,
                "--proxy-budgets-json",
                str(proxy),
                "--reservation-ledger-out",
                str(reservation),
                "--trade-ledger-out",
                str(trades),
                "--json-out",
                str(cells),
            ]
        )
        == 2
    )
    result = json.loads(cells.read_text(encoding="utf-8"))
    assert len(result["cost_readiness"]["source_failure_symbols"]) == 18
    assert len(result["cost_readiness"]["missing_proxy_budget_symbols"]) == 18
    assert result["reservation_ledger_evidence"]["file_sha256"] == hashlib.sha256(
        reservation.read_bytes()
    ).hexdigest()
    assert result["trade_ledger_evidence"]["file_sha256"] == hashlib.sha256(
        trades.read_bytes()
    ).hexdigest()
    assert reservation.resolve() != trades.resolve() != cells.resolve()

    with pytest.raises(SystemExit) as preexisting:
        rdt.main(
            [
                "--csv-root",
                str(tmp_path / "empty-input"),
                "--start",
                EVALUATION_START,
                "--end",
                EVALUATION_END,
                "--proxy-budgets-json",
                str(proxy),
                "--reservation-ledger-out",
                str(reservation),
                "--trade-ledger-out",
                str(tmp_path / "new-trades.json"),
                "--json-out",
                str(tmp_path / "new-cells.json"),
            ]
        )
    assert preexisting.value.code == 2

    shared = tmp_path / "shared.json"
    with pytest.raises(SystemExit) as collision:
        rdt.main(
            [
                "--csv-root",
                str(tmp_path / "empty-input"),
                "--start",
                EVALUATION_START,
                "--end",
                EVALUATION_END,
                "--proxy-budgets-json",
                str(proxy),
                "--reservation-ledger-out",
                str(shared),
                "--trade-ledger-out",
                str(shared),
                "--json-out",
                str(tmp_path / "another-cells.json"),
            ]
        )
    assert collision.value.code == 2


def test_cli_returns_nonzero_for_all_parsed_but_insufficient_sources(
    tmp_path: Path,
) -> None:
    csv_root = tmp_path / "short-input"
    csv_root.mkdir()
    for symbol in rdt.FX_SYMBOLS:
        _write_m1_csv(csv_root / f"{symbol}_M1.csv", [_bar(0, volume=0.0)])
    proxy = tmp_path / "proxy.json"
    proxy.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
            }
        ),
        encoding="utf-8",
    )
    cells = tmp_path / "cells.json"
    assert (
        rdt.main(
            [
                "--csv-root",
                str(csv_root),
                "--start",
                EVALUATION_START,
                "--end",
                EVALUATION_END,
                "--proxy-budgets-json",
                str(proxy),
                "--reservation-ledger-out",
                str(tmp_path / "reservations.json"),
                "--trade-ledger-out",
                str(tmp_path / "trades.json"),
                "--json-out",
                str(cells),
            ]
        )
        == 2
    )
    result = json.loads(cells.read_text(encoding="utf-8"))
    assert len(result["cost_readiness"]["source_failure_symbols"]) == 18
    assert result["cost_readiness"]["missing_proxy_budget_symbols"] == []
    assert all(cell["source_ready"] is False for cell in result["cells"])
    assert all(
        cell["source_error"] == "no_complete_254_bar_m1_run"
        for cell in result["cells"]
    )
    assert result["discovery_gate"]["passed"] is False


def test_cli_returns_nonzero_for_incomplete_proxy_and_zero_only_when_ready(
    tmp_path: Path,
) -> None:
    csv_root = tmp_path / "ready-input"
    csv_root.mkdir()
    bars = _baseline(254)
    for symbol in rdt.FX_SYMBOLS:
        _write_m1_csv(csv_root / f"{symbol}_M1.csv", bars)

    missing_symbol = rdt.FX_SYMBOLS[-1]
    incomplete_proxy = tmp_path / "incomplete-proxy.json"
    incomplete_proxy.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {
                    symbol: 1.0
                    for symbol in rdt.FX_SYMBOLS
                    if symbol != missing_symbol
                },
            }
        ),
        encoding="utf-8",
    )
    incomplete_cells = tmp_path / "incomplete-cells.json"
    assert (
        rdt.main(
            [
                "--csv-root",
                str(csv_root),
                "--start",
                EVALUATION_START,
                "--end",
                EVALUATION_END,
                "--proxy-budgets-json",
                str(incomplete_proxy),
                "--reservation-ledger-out",
                str(tmp_path / "incomplete-reservations.json"),
                "--trade-ledger-out",
                str(tmp_path / "incomplete-trades.json"),
                "--json-out",
                str(incomplete_cells),
            ]
        )
        == 2
    )
    incomplete = json.loads(incomplete_cells.read_text(encoding="utf-8"))
    assert incomplete["cost_readiness"]["source_failure_symbols"] == []
    assert incomplete["cost_readiness"]["missing_proxy_budget_symbols"] == [
        missing_symbol
    ]
    missing_cells = [
        cell for cell in incomplete["cells"] if cell["symbol"] == missing_symbol
    ]
    assert all(cell["proxy_budget_ready"] is False for cell in missing_cells)
    assert incomplete["discovery_gate"]["passed"] is False

    complete_proxy = tmp_path / "complete-proxy.json"
    complete_proxy.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {symbol: 1.0 for symbol in rdt.FX_SYMBOLS},
            }
        ),
        encoding="utf-8",
    )
    complete_cells = tmp_path / "complete-cells.json"
    assert (
        rdt.main(
            [
                "--csv-root",
                str(csv_root),
                "--start",
                EVALUATION_START,
                "--end",
                EVALUATION_END,
                "--proxy-budgets-json",
                str(complete_proxy),
                "--reservation-ledger-out",
                str(tmp_path / "complete-reservations.json"),
                "--trade-ledger-out",
                str(tmp_path / "complete-trades.json"),
                "--json-out",
                str(complete_cells),
            ]
        )
        == 0
    )
    complete = json.loads(complete_cells.read_text(encoding="utf-8"))
    assert complete["cost_readiness"]["source_failure_symbols"] == []
    assert complete["cost_readiness"]["missing_proxy_budget_symbols"] == []
    assert all(cell["source_ready"] is True for cell in complete["cells"])
    assert all(cell["proxy_budget_ready"] is True for cell in complete["cells"])
