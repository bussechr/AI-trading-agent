"""Synthetic-only tests for the impulse-pullback-continuation screen."""

from __future__ import annotations

import dataclasses
import json
import statistics
from pathlib import Path

import pytest

import fxstack.scalp.screen_impulse_pullback as ipc


BASE_EPOCH = 1_704_067_200  # 2024-01-01T00:00:00Z, UTC-aligned.


def _bar(
    index: int,
    *,
    mid_o: float = 100.0,
    mid_h: float = 100.05,
    mid_l: float = 99.95,
    mid_c: float = 100.0,
    spread_bps: float = 0.20,
    epoch_shift: int = 0,
) -> ipc.QuoteBar:
    def quotes(mid: float) -> tuple[float, float]:
        half = mid * spread_bps / 2e4
        return mid - half, mid + half

    bid_o, ask_o = quotes(mid_o)
    bid_h, ask_h = quotes(mid_h)
    bid_l, ask_l = quotes(mid_l)
    bid_c, ask_c = quotes(mid_c)
    return ipc.QuoteBar(
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


def _baseline(count: int = 900, *, spread_bps: float = 0.20) -> list[ipc.QuoteBar]:
    return [_bar(index, spread_bps=spread_bps) for index in range(count)]


def _plant_buy(
    bars: list[ipc.QuoteBar],
    index: int,
    *,
    spread_bps: float = 0.20,
    target: bool = True,
) -> None:
    bars[index - 3] = _bar(
        index - 3,
        mid_o=100.00,
        mid_h=100.12,
        mid_l=99.98,
        mid_c=100.10,
        spread_bps=spread_bps,
    )
    bars[index - 2] = _bar(
        index - 2,
        mid_o=100.10,
        mid_h=100.22,
        mid_l=100.08,
        mid_c=100.20,
        spread_bps=spread_bps,
    )
    bars[index - 1] = _bar(
        index - 1,
        mid_o=100.20,
        mid_h=100.32,
        mid_l=100.18,
        mid_c=100.30,
        spread_bps=spread_bps,
    )
    bars[index] = _bar(
        index,
        mid_o=100.25,
        mid_h=100.36,
        mid_l=100.22,
        mid_c=100.34,
        spread_bps=spread_bps,
    )
    bars[index + 1] = _bar(
        index + 1,
        mid_o=100.34,
        mid_h=100.38,
        mid_l=100.28,
        mid_c=100.34,
        spread_bps=spread_bps,
    )
    if target:
        bars[index + 2] = _bar(
            index + 2,
            mid_o=100.35,
            mid_h=100.65,
            mid_l=100.28,
            mid_c=100.50,
            spread_bps=spread_bps,
        )


def _plant_sell(
    bars: list[ipc.QuoteBar],
    index: int,
    *,
    spread_bps: float = 0.20,
    target: bool = True,
) -> None:
    bars[index - 3] = _bar(
        index - 3,
        mid_o=100.00,
        mid_h=100.02,
        mid_l=99.88,
        mid_c=99.90,
        spread_bps=spread_bps,
    )
    bars[index - 2] = _bar(
        index - 2,
        mid_o=99.90,
        mid_h=99.92,
        mid_l=99.78,
        mid_c=99.80,
        spread_bps=spread_bps,
    )
    bars[index - 1] = _bar(
        index - 1,
        mid_o=99.80,
        mid_h=99.82,
        mid_l=99.68,
        mid_c=99.70,
        spread_bps=spread_bps,
    )
    bars[index] = _bar(
        index,
        mid_o=99.75,
        mid_h=99.78,
        mid_l=99.64,
        mid_c=99.66,
        spread_bps=spread_bps,
    )
    bars[index + 1] = _bar(
        index + 1,
        mid_o=99.66,
        mid_h=99.72,
        mid_l=99.62,
        mid_c=99.66,
        spread_bps=spread_bps,
    )
    if target:
        bars[index + 2] = _bar(
            index + 2,
            mid_o=99.65,
            mid_h=99.72,
            mid_l=99.30,
            mid_c=99.45,
            spread_bps=spread_bps,
        )


STRICT_CONFIG = ipc.IPCConfig(3, 2.5, 0.40)


def _evaluate(
    bars: list[ipc.QuoteBar],
    index: int,
    side: str,
    *,
    config: ipc.IPCConfig = STRICT_CONFIG,
    budget: float = 1.0,
    stop_floor: float = 0.0,
    extra_cost: float = 0.0,
) -> tuple[ipc.IPCSignal | None, str]:
    return ipc.evaluate_signal(
        prepared=ipc.prepare_series(bars),
        signal_index=index,
        symbol="EURUSD",
        side=side,
        config=config,
        effective_spread_budget_bps=budget,
        stop_floor_bps=stop_floor,
        extra_round_trip_cost_bps=extra_cost,
    )


def _signal(
    bars: list[ipc.QuoteBar],
    index: int,
    side: str,
    **kwargs: float | ipc.IPCConfig,
) -> ipc.IPCSignal:
    signal, reason = _evaluate(bars, index, side, **kwargs)
    assert signal is not None, reason
    return signal


def test_future_perturbations_cannot_change_past_context_or_signal() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    before_prepared = ipc.prepare_series(bars)
    before_context = ipc.baseline_context_at(
        before_prepared, signal_index=300, impulse_bars=3
    )
    before_signal, before_reason = ipc.evaluate_signal(
        prepared=before_prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
    )
    assert before_signal is not None, before_reason

    changed = list(bars)
    # t+1 is a declared execution input. Later bars are outcome-only and must
    # not alter either the frozen baseline or the signal.
    for index in range(302, len(changed)):
        changed[index] = _bar(
            index,
            mid_o=130.0,
            mid_h=131.0,
            mid_l=129.0,
            mid_c=130.5,
            spread_bps=4.0,
        )
    after_prepared = ipc.prepare_series(changed)
    after_context = ipc.baseline_context_at(
        after_prepared, signal_index=300, impulse_bars=3
    )
    after_signal, after_reason = ipc.evaluate_signal(
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


def test_baseline_is_exactly_240_bars_and_excludes_the_whole_pattern() -> None:
    bars = _baseline(500)
    _plant_buy(bars, 300)
    prepared = ipc.prepare_series(bars)
    context = ipc.baseline_context_at(prepared, signal_index=300, impulse_bars=3)
    assert context is not None
    assert context.pattern_start_index == 297

    expected_tr = statistics.median(
        ipc._true_range_bps(bars[index - 1], bars[index])
        for index in range(57, 297)
    )
    expected_q25 = ipc._quantile_sorted(
        sorted(bars[index].spread_close_bps for index in range(57, 297)), 0.25
    )
    assert context.volatility_bps == pytest.approx(expected_tr)
    assert context.spread_q25_bps == pytest.approx(expected_q25)

    changed = list(bars)
    for index in range(297, 301):
        changed[index] = _bar(
            index,
            mid_o=120.0,
            mid_h=125.0,
            mid_l=80.0,
            mid_c=110.0,
            spread_bps=9.0,
        )
    changed_context = ipc.baseline_context_at(
        ipc.prepare_series(changed), signal_index=300, impulse_bars=3
    )
    assert changed_context == context


def test_strict_m1_continuity_is_required_for_context_and_delayed_fill() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)

    broken_history = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 100 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_history, 300, "BUY")
    assert candidate is None
    assert reason == "strict_prepattern_context_unavailable"

    broken_fill = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_fill, 300, "BUY")
    assert candidate is None
    assert reason == "delayed_fill_gap"


def test_csv_header_must_bind_every_positional_quote_column(tmp_path: Path) -> None:
    path = tmp_path / "EURUSD_M1.csv"
    path.write_text(
        "timestamp,ask_open,bid_high,bid_low,bid_close,"
        "bid_open,ask_high,ask_low,ask_close\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected M1 header"):
        ipc.load_m1_csv(path)

    fractional = tmp_path / "fractional_M1.csv"
    fractional.write_text(
        "timestamp,bid_open,bid_high,bid_low,bid_close,"
        "ask_open,ask_high,ask_low,ask_close\n"
        "2024-01-01T00:00:00.900Z,1,1,1,1,1.1,1.1,1.1,1.1\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed M1 row"):
        ipc.load_m1_csv(fractional)


def test_buy_and_sell_formulas_are_quote_side_mirrors() -> None:
    buy_bars = _baseline()
    sell_bars = _baseline()
    _plant_buy(buy_bars, 300)
    _plant_sell(sell_bars, 300)

    buy = _signal(buy_bars, 300, "BUY")
    sell = _signal(sell_bars, 300, "SELL")
    assert buy.impulse_bps == pytest.approx(sell.impulse_bps, rel=1e-2)
    assert buy.impulse_vol_units == pytest.approx(sell.impulse_vol_units, rel=1e-2)
    assert buy.retrace_fraction == pytest.approx(sell.retrace_fraction, rel=1e-2)
    assert buy.close_location == pytest.approx(sell.close_location, rel=1e-2)
    assert buy.risk_bps == pytest.approx(sell.risk_bps, rel=2e-2)
    assert buy.directional_impulse_bars == sell.directional_impulse_bars == 3
    assert buy.target_price > buy.entry_price > buy.stop_price
    assert sell.target_price < sell.entry_price < sell.stop_price


def test_impulse_threshold_breadth_and_pullback_bounds_are_enforced() -> None:
    too_small = _baseline()
    _plant_buy(too_small, 300)
    too_small[297] = _bar(
        297, mid_o=100.20, mid_h=100.24, mid_l=100.18, mid_c=100.22
    )
    too_small[298] = _bar(
        298, mid_o=100.22, mid_h=100.27, mid_l=100.20, mid_c=100.25
    )
    too_small[299] = _bar(
        299, mid_o=100.25, mid_h=100.32, mid_l=100.23, mid_c=100.30
    )
    candidate, reason = _evaluate(too_small, 300, "BUY")
    assert candidate is None
    assert reason == "impulse_too_small"

    low_breadth = _baseline()
    _plant_buy(low_breadth, 300)
    low_breadth[298] = _bar(
        298, mid_o=100.30, mid_h=100.32, mid_l=100.18, mid_c=100.20
    )
    low_breadth[299] = _bar(
        299, mid_o=100.30, mid_h=100.32, mid_l=100.28, mid_c=100.30
    )
    candidate, reason = _evaluate(low_breadth, 300, "BUY")
    assert candidate is None
    assert reason == "impulse_breadth_too_low"

    too_shallow = _baseline()
    _plant_buy(too_shallow, 300)
    too_shallow[300] = _bar(
        300, mid_o=100.29, mid_h=100.36, mid_l=100.28, mid_c=100.34
    )
    candidate, reason = _evaluate(too_shallow, 300, "BUY")
    assert candidate is None
    assert reason == "pullback_too_shallow"

    too_deep = _baseline()
    _plant_buy(too_deep, 300)
    too_deep[300] = _bar(
        300, mid_o=100.25, mid_h=100.36, mid_l=100.15, mid_c=100.34
    )
    candidate, reason = _evaluate(too_deep, 300, "BUY")
    assert candidate is None
    assert reason == "pullback_too_deep"

    poor_close = _baseline()
    _plant_buy(poor_close, 300)
    poor_close[300] = _bar(
        300, mid_o=100.25, mid_h=100.50, mid_l=100.22, mid_c=100.31
    )
    candidate, reason = _evaluate(poor_close, 300, "BUY")
    assert candidate is None
    assert reason == "pullback_rejection_missing"


def test_entry_is_one_bar_delayed_and_uses_the_exact_next_quote_open() -> None:
    signal_bar = _bar(0, mid_c=100.0)
    favorable_buy_gap = _bar(1, mid_o=99.0, mid_h=99.2, mid_l=98.8, mid_c=99.0)
    adverse_buy_gap = _bar(1, mid_o=101.0, mid_h=101.2, mid_l=100.8, mid_c=101.0)

    assert ipc.FILL_DELAY_M1_BARS == 1
    assert ipc._entry_price(signal_bar, favorable_buy_gap, side="BUY") == (
        favorable_buy_gap.ask_o
    )
    assert ipc._entry_price(signal_bar, adverse_buy_gap, side="BUY") == adverse_buy_gap.ask_o
    assert ipc._entry_price(signal_bar, favorable_buy_gap, side="SELL") == (
        favorable_buy_gap.bid_o
    )
    assert ipc._entry_price(signal_bar, adverse_buy_gap, side="SELL") == adverse_buy_gap.bid_o


def test_whole_horizon_is_validated_before_an_early_barrier_is_scored() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    signal = _signal(bars, 300, "BUY")

    # The planted target is hit on index 302, but a later timestamp gap makes
    # the fixed eight-bar outcome unobservable.  Neither a favorable nor an
    # adverse early barrier may escape that outcome-independent censoring.
    gapped_after_target = list(bars)
    gapped_after_target[306] = dataclasses.replace(
        gapped_after_target[306], epoch=gapped_after_target[306].epoch + 60
    )
    assert ipc.simulate_trade(gapped_after_target, signal=signal) is None

    truncated_after_target = bars[:306]
    assert ipc.simulate_trade(truncated_after_target, signal=signal) is None

    adverse = list(bars)
    adverse[301] = _bar(
        301,
        mid_o=signal.stop_price - 0.10,
        mid_h=signal.stop_price - 0.05,
        mid_l=signal.stop_price - 0.15,
        mid_c=signal.stop_price - 0.10,
    )
    adverse[306] = dataclasses.replace(adverse[306], epoch=adverse[306].epoch + 60)
    assert ipc.simulate_trade(adverse, signal=signal) is None


def test_prepattern_q25_and_pair_budget_gate_signal_and_next_open() -> None:
    signal_wide = _baseline()
    _plant_buy(signal_wide, 300, spread_bps=0.30)
    candidate, reason = _evaluate(signal_wide, 300, "BUY")
    assert candidate is None
    assert reason == "signal_spread_above_q25_or_budget"

    entry_wide = _baseline()
    _plant_buy(entry_wide, 300)
    entry_wide[301] = _bar(
        301,
        mid_o=100.34,
        mid_h=100.38,
        mid_l=100.28,
        mid_c=100.34,
        spread_bps=0.30,
    )
    candidate, reason = _evaluate(entry_wide, 300, "BUY")
    assert candidate is None
    assert reason == "entry_spread_above_q25_or_budget"

    budget_tight = _baseline()
    _plant_buy(budget_tight, 300)
    candidate, reason = _evaluate(budget_tight, 300, "BUY", budget=0.10)
    assert candidate is None
    assert reason == "signal_spread_above_q25_or_budget"


def test_cost_gates_and_optional_pair_stop_floor_are_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    raw = _signal(bars, 300, "BUY")

    costly, reason = _evaluate(bars, 300, "BUY", extra_cost=2.0)
    assert costly is None
    assert reason == "bracket_cost_dead"

    floored = _signal(bars, 300, "BUY", stop_floor=raw.risk_bps + 10.0)
    assert floored.risk_bps == pytest.approx(raw.risk_bps + 10.0)
    target_bps = (floored.target_price - floored.entry_price) / floored.entry_price * 1e4
    assert target_bps == pytest.approx(floored.risk_bps)
    assert floored.p_star <= 0.55

    # With fixed 1R, p* is the tighter ordinary guard. Temporarily relax it
    # solely to prove the independent four-times-recorded-cost guard exists.
    monkeypatch.setattr(ipc, "P_STAR_MAX", 1.0)
    too_small, reason = _evaluate(bars, 300, "BUY", extra_cost=4.0)
    assert too_small is None
    assert reason == "target_too_small_vs_cost"

    absurd_floor, reason = _evaluate(bars, 300, "BUY", stop_floor=10_001.0)
    assert absurd_floor is None
    assert reason == "stop_floor_above_sane_bound"


@pytest.mark.parametrize("side,gapped_open", [("BUY", 103.0), ("SELL", 97.0)])
def test_adverse_next_open_gap_cannot_create_a_non_scalp_bracket(
    side: str, gapped_open: float
) -> None:
    bars = _baseline()
    if side == "BUY":
        _plant_buy(bars, 300)
    else:
        _plant_sell(bars, 300)
    bars[301] = _bar(
        301,
        mid_o=gapped_open,
        mid_h=gapped_open + 0.05,
        mid_l=gapped_open - 0.05,
        mid_c=gapped_open,
    )
    signal, reason = _evaluate(bars, 300, side)
    assert signal is None
    assert reason == "initial_risk_above_sane_bound"


def test_adverse_gap_and_ambiguous_bar_use_conservative_exit_ordering() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    signal = _signal(bars, 300, "BUY")

    gap_bars = list(bars)
    gap_mid = signal.stop_price - 0.10
    gap_bars[301] = _bar(
        301,
        mid_o=gap_mid,
        mid_h=gap_mid + 0.05,
        mid_l=gap_mid - 0.05,
        mid_c=gap_mid,
    )
    gap_trade = ipc.simulate_trade(gap_bars, signal=signal)
    assert gap_trade is not None
    assert gap_trade.exit_reason == "sl_gap_open"
    assert gap_trade.exit_price < signal.stop_price
    assert gap_trade.pnl_r < -1.0

    ambiguous = list(bars)
    ambiguous[301] = _bar(
        301,
        mid_o=signal.entry_price,
        mid_h=signal.target_price + 0.10,
        mid_l=signal.stop_price - 0.10,
        mid_c=signal.entry_price,
    )
    ambiguous_trade = ipc.simulate_trade(ambiguous, signal=signal)
    assert ambiguous_trade is not None
    assert ambiguous_trade.exit_reason == "sl_double_touch"
    assert ambiguous_trade.pnl_r == pytest.approx(-1.0)


def test_positive_time_stop_is_reported_separately_from_full_target_win() -> None:
    bars = _baseline()
    _plant_buy(bars, 300, target=False)
    for index in range(301, 309):
        close = 100.40 if index == 308 else 100.34
        bars[index] = _bar(
            index,
            mid_o=100.34,
            mid_h=100.42,
            mid_l=100.27,
            mid_c=close,
        )
    cell = ipc.screen_cell(
        prepared=ipc.prepare_series(bars),
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


def test_cooldown_is_thirty_m1_bars_per_pair_side_and_cell() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_buy(bars, 310)
    cell = ipc.screen_cell(
        prepared=ipc.prepare_series(bars),
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


def test_only_first_eligible_trade_per_cell_entry_day_is_scored() -> None:
    bars = _baseline(1_600)
    _plant_buy(bars, 300)
    _plant_buy(bars, 340)
    cell = ipc.screen_cell(
        prepared=ipc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=0.0,
    )
    assert cell["raw_events"] == 2
    assert cell["events_after_cooldown"] == 1
    assert cell["trades"] == 1
    assert cell["reasons"]["one_trade_per_entry_day"] == 1
    assert cell["maximum_trades_per_cell_entry_day"] == 1


def test_independent_day_and_ledger_use_executable_entry_epoch() -> None:
    bars = _baseline(1_600)
    signal_index = 1_439  # 23:59 UTC; t+1 entry belongs to the next UTC day.
    _plant_buy(bars, signal_index)
    cell = ipc.screen_cell(
        prepared=ipc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=1.0,
    )
    assert cell["trades"] == 1
    assert cell["independent_days"] == 1
    trade = cell["trade_ledger"][0]
    assert trade["entry_day_utc"] == "2024-01-02"
    assert trade["entry_epoch"] == bars[signal_index + 1].epoch
    assert trade["stop_price"] < trade["entry_price"] < trade["target_price"]
    assert trade["extra_round_trip_cost_bps"] == 1.0
    assert trade["full_target_win"] is True


def test_zero_cells_pair_cost_provenance_and_permanent_guards_are_retained() -> None:
    proxy_budgets = {
        symbol: 1.0 + index / 100.0 for index, symbol in enumerate(ipc.FX_SYMBOLS)
    }
    result = ipc.screen_universe(
        bars_by_symbol={},
        proxy_spread_budgets_bps=proxy_budgets,
    )
    assert len(result["cells"]) == 288
    assert all(cell["trades"] == 0 for cell in result["cells"])
    assert all(cell["source_ready"] is False for cell in result["cells"])
    assert all(
        cell["cost_mode"] == "pair_specific_proxy_discovery_stress"
        for cell in result["cells"]
    )
    assert all(
        cell["effective_spread_budget_bps"] == proxy_budgets[cell["symbol"]]
        for cell in result["cells"]
    )
    assert result["proxy_discovery_stress"] is True
    assert result["economic_claim_ready"] is False
    assert result["economics_claim_ready"] is False
    assert result["economic_passed"] is False
    assert result["success_claim_authorized"] is False
    assert result["activation_authorized"] is False
    json.dumps(result, allow_nan=False)

    zero_venue = ipc.screen_universe(
        bars_by_symbol={},
        symbols=("EURUSD",),
        venue_spread_budgets_bps={"EURUSD": 0.0},
        proxy_spread_budgets_bps={"EURUSD": 0.7},
    )
    assert zero_venue["proxy_discovery_stress"] is True
    assert all(cell["effective_spread_budget_bps"] == 0.7 for cell in zero_venue["cells"])

    empty_with_named_venue = ipc.screen_universe(
        bars_by_symbol={},
        venue_spread_budgets_bps={symbol: 0.7 for symbol in ipc.FX_SYMBOLS},
        venue_cost_provenance={
            symbol: _venue_record(0.7) for symbol in ipc.FX_SYMBOLS
        },
        stop_floors_bps={symbol: 4.5 for symbol in ipc.FX_SYMBOLS},
    )
    assert empty_with_named_venue["economic_claim_ready"] is False
    assert empty_with_named_venue["cost_readiness"]["source_failure_symbols"] == list(
        ipc.FX_SYMBOLS
    )


def _venue_record(budget_bps: float) -> dict[str, object]:
    return {
        "budget_bps": budget_bps,
        "venue": "synthetic-test-venue",
        "method": "synthetic fixture only",
        "observed_start_utc": "2023-01-01T00:00:00Z",
        "observed_end_utc": "2023-02-01T00:00:00Z",
        "sample_count": 100,
        "source_sha256": "a" * 64,
    }


def test_readiness_requires_source_floor_and_identity_bound_venue_provenance() -> None:
    bars = _baseline(300)
    kwargs = {
        "bars_by_symbol": {"EURUSD": bars},
        "symbols": ("EURUSD",),
        "venue_spread_budgets_bps": {"EURUSD": 0.7},
        "stop_floors_bps": {"EURUSD": 4.5},
    }
    unverified = ipc.screen_universe(**kwargs)
    assert all(cell["source_ready"] is True for cell in unverified["cells"])
    assert all(cell["venue_provenance_ready"] is False for cell in unverified["cells"])
    assert all(
        cell["cost_mode"] == "unverified_venue_budget_discovery_stress"
        for cell in unverified["cells"]
    )
    assert all(cell["economic_claim_ready"] is False for cell in unverified["cells"])

    verified = ipc.screen_universe(
        **kwargs,
        venue_cost_provenance={"EURUSD": _venue_record(0.7)},
    )
    assert all(cell["venue_provenance_ready"] is True for cell in verified["cells"])
    assert all(cell["stop_floor_ready"] is True for cell in verified["cells"])
    assert all(cell["economic_claim_ready"] is True for cell in verified["cells"])
    # A subset may have complete inputs for its own cells but can never make a
    # claim for the canonical all-pair universe.
    assert verified["economic_claim_ready"] is False

    missing_floor = ipc.screen_universe(
        bars_by_symbol={"EURUSD": bars},
        symbols=("EURUSD",),
        venue_spread_budgets_bps={"EURUSD": 0.7},
        venue_cost_provenance={"EURUSD": _venue_record(0.7)},
    )
    assert all(cell["stop_floor_ready"] is False for cell in missing_floor["cells"])
    assert all(cell["economic_claim_ready"] is False for cell in missing_floor["cells"])


def test_trial_accounting_is_exactly_288_new_and_2802_cumulative_cells() -> None:
    assert len(ipc.GRID) == 8
    assert {config.impulse_bars for config in ipc.GRID} == {3, 5}
    assert {config.min_impulse_vol for config in ipc.GRID} == {1.5, 2.5}
    assert {config.max_retrace_fraction for config in ipc.GRID} == {0.40, 0.60}
    assert ipc.trial_accounting() == {
        "grid_configurations": 8,
        "directions": 2,
        "symbols": 18,
        "reported_current_cells": 288,
        "current_attempted_cells": 288,
        "prior_attempted_cells": 2_514,
        "cumulative_attempted_cells": 2_802,
        "expected_full_universe_cells": 288,
    }
    subset = ipc.trial_accounting(n_symbols=1)
    assert subset["reported_current_cells"] == 16
    assert subset["current_attempted_cells"] == 288
    assert subset["cumulative_attempted_cells"] == 2_802
    with pytest.raises(ValueError, match="frozen prior attempt count"):
        ipc.trial_accounting(prior_tests=0)


def test_cli_refuses_overlapping_cells_and_trade_ledger_paths(tmp_path: Path) -> None:
    same = tmp_path / "evidence.json"
    with pytest.raises(SystemExit) as raised:
        ipc.main(
            [
                "--csv-root",
                str(tmp_path),
                "--trade-ledger-out",
                str(same),
                "--json-out",
                str(same),
            ]
        )
    assert raised.value.code == 2
    assert not same.exists()


def test_planted_buy_and_sell_are_positive_net_full_target_wins() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    _plant_sell(bars, 600)
    prepared = ipc.prepare_series(bars)
    buy = ipc.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="BUY",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=1.0,
    )
    sell = ipc.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="SELL",
        config=STRICT_CONFIG,
        effective_spread_budget_bps=1.0,
        stop_floor_bps=0.0,
        extra_round_trip_cost_bps=1.0,
    )
    assert buy["trades"] == buy["wins"] == buy["full_target_wins"] == 1
    assert sell["trades"] == sell["wins"] == sell["full_target_wins"] == 1
    assert buy["exit_mix"] == {"tp": 1}
    assert sell["exit_mix"] == {"tp": 1}
    assert buy["mean_r"] > 0.0
    assert sell["mean_r"] > 0.0


def test_signal_dataclass_and_screen_json_contain_no_nonfinite_values() -> None:
    bars = _baseline()
    _plant_buy(bars, 300)
    signal = _signal(bars, 300, "BUY")
    assert all(
        not isinstance(value, float) or value == value
        for value in dataclasses.asdict(signal).values()
    )
    result = ipc.screen_universe(
        bars_by_symbol={"EURUSD": bars},
        symbols=("EURUSD",),
        venue_spread_budgets_bps={"EURUSD": 1.0},
    )
    json.dumps(result, allow_nan=False)
