"""Synthetic-only adversarial tests for the causal RSTS screen."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import json
import math
import statistics
from pathlib import Path

import pytest

import fxstack.scalp.screen_rolling_sign_transition_state as rsts


BASE_EPOCH = 1_672_531_200  # 2023-01-01T00:00:00Z.
EVALUATION_START = "2023-01-01T00:00:00Z"
EVALUATION_END = "2023-07-01T00:00:00Z"
PREREGISTRATION_LOCK = "2023-01-01T00:00:00Z"
PROVENANCE = {
    "venue_id": "synthetic-venue",
    "source_id": "synthetic-frozen-proxy-v1",
    "as_of_utc": PREREGISTRATION_LOCK,
    "source_cutoff_utc": "2022-12-31T23:59:00Z",
    "method": "synthetic-test-only",
    "units": "bps",
    "source_snapshot_sha256": "a" * 64,
    "frozen": True,
}
PROXY_SCHEMA = rsts.PROXY_CONTRACT_SCHEMA_VERSION
THETA_05 = rsts.RSTSConfig(0.05)


def _bar(
    index: int,
    *,
    mid_o: float,
    mid_c: float,
    low_pad: float = 0.04,
    high_pad: float = 0.04,
    spread_bps: float = 0.20,
    epoch_shift: int = 0,
    volume: float = 1.0,
) -> rsts.QuoteBar:
    def quotes(mid: float) -> tuple[float, float]:
        half = mid * spread_bps / 2e4
        return mid - half, mid + half

    mid_h = max(mid_o, mid_c) + high_pad
    mid_l = min(mid_o, mid_c) - low_pad
    bid_o, ask_o = quotes(mid_o)
    bid_h, ask_h = quotes(mid_h)
    bid_l, ask_l = quotes(mid_l)
    bid_c, ask_c = quotes(mid_c)
    return rsts.QuoteBar(
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


def _series(
    count: int = 700,
    *,
    alternating: bool = False,
    spread_bps: float = 0.20,
    sign_overrides: dict[int, int] | None = None,
) -> list[rsts.QuoteBar]:
    rows: list[rsts.QuoteBar] = []
    previous = 100.0
    for index in range(count):
        sign = (-1 if index % 2 else 1) if alternating else 1
        sign = (sign_overrides or {}).get(index, sign)
        close = previous if sign == 0 else previous * math.exp(sign * 0.20 / 1e4)
        rows.append(
            _bar(
                index,
                mid_o=previous,
                mid_c=close,
                spread_bps=spread_bps,
            )
        )
        previous = close
    return rows


def _replace_spread(bar: rsts.QuoteBar, spread_bps: float) -> rsts.QuoteBar:
    return _bar(
        (bar.epoch - BASE_EPOCH) // 60,
        mid_o=(bar.bid_o + bar.ask_o) / 2.0,
        mid_c=(bar.bid_c + bar.ask_c) / 2.0,
        low_pad=(bar.bid_o + bar.ask_o) / 2.0 - (bar.bid_l + bar.ask_l) / 2.0,
        high_pad=(bar.bid_h + bar.ask_h) / 2.0 - (bar.bid_o + bar.ask_o) / 2.0,
        spread_bps=spread_bps,
        volume=bar.volume,
    )


def _plant_signal(
    bars: list[rsts.QuoteBar],
    index: int,
    *,
    side: str,
    return_sign: int,
    signal_spread_bps: float = 0.15,
    entry_spread_bps: float = 0.15,
    target: bool = True,
) -> None:
    previous = bars[index - 1].mid_c
    close = previous * math.exp(return_sign * 1.0 / 1e4)
    if side == "BUY":
        bars[index] = _bar(
            index,
            mid_o=previous,
            mid_c=close,
            low_pad=previous * 0.00205,
            high_pad=previous * 0.00001,
            spread_bps=signal_spread_bps,
        )
    else:
        bars[index] = _bar(
            index,
            mid_o=previous,
            mid_c=close,
            low_pad=previous * 0.00001,
            high_pad=previous * 0.00205,
            spread_bps=signal_spread_bps,
        )
    bars[index + 1] = _bar(
        index + 1,
        mid_o=close,
        mid_c=close,
        low_pad=close * 0.00005,
        high_pad=close * 0.00005,
        spread_bps=entry_spread_bps,
    )
    if not target:
        return
    target_close = close * (1.004 if side == "BUY" else 0.996)
    bars[index + 2] = _bar(
        index + 2,
        mid_o=close,
        mid_c=target_close,
        low_pad=close * 0.00001,
        high_pad=close * 0.00001,
        spread_bps=entry_spread_bps,
    )


def _signal(
    bars: list[rsts.QuoteBar],
    index: int,
    side: str,
    *,
    config: rsts.RSTSConfig = THETA_05,
    budget: float = 1.0,
) -> rsts.RSTSSignal:
    signal, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(bars),
        signal_index=index,
        symbol="EURUSD",
        side=side,
        config=config,
        proxy_spread_budget_bps=budget,
    )
    assert signal is not None, reason
    return signal


def _budgets(value: float = 1.0) -> dict[str, float]:
    return {symbol: value for symbol in rsts.FX_SYMBOLS}


def _screen(
    bars_by_symbol: dict[str, list[rsts.QuoteBar]] | None = None,
    *,
    budgets: dict[str, float] | None = None,
    provenance: dict[str, object] | None = None,
) -> dict[str, object]:
    return rsts.screen_universe(
        bars_by_symbol=bars_by_symbol or {},
        proxy_spread_budgets_bps=budgets or _budgets(),
        proxy_provenance=PROVENANCE if provenance is None else provenance,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
    )


def _write_m1_csv(path: Path, bars: list[rsts.QuoteBar]) -> None:
    lines = [",".join(rsts.CSV_HEADER)]
    for bar in bars:
        timestamp = (
            dt.datetime.fromtimestamp(bar.epoch, dt.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
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
        lines.append(
            ",".join((timestamp, *(format(value, ".17g") for value in values)))
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_proxy(path: Path, budgets: dict[str, float] | None = None) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": budgets or _budgets(),
            },
            allow_nan=False,
        ),
        encoding="utf-8",
    )


def test_exact_pre_signal_windows_nearest_rank_and_no_lookahead() -> None:
    bars = _series(500)
    # At t=300 the exact baseline is [60, 300), so the nearest-rank Q25
    # of these 240 deliberately unique spreads is the 60th sorted value.
    for offset, index in enumerate(range(60, 300), start=1):
        bars[index] = _replace_spread(bars[index], offset / 100.0)
    prepared = rsts.prepare_series(bars)
    context = rsts.baseline_context_at(prepared, signal_index=300)
    assert context is not None
    expected_tr = statistics.median(
        rsts._true_range_bps(bars[index - 1], bars[index]) for index in range(60, 300)
    )
    assert context.volatility_bps == pytest.approx(expected_tr)
    assert context.spread_q25_bps == pytest.approx(0.60)
    assert rsts._nearest_rank_quantile_sorted(list(range(1, 241)), 0.25) == 60.0

    changed = list(bars)
    changed[300] = _replace_spread(changed[300], 50.0)
    for index in range(301, len(changed)):
        changed[index] = dataclasses.replace(changed[index], volume=1_000_000.0)
    changed_context = rsts.baseline_context_at(
        rsts.prepare_series(changed), signal_index=300
    )
    assert changed_context == context


def test_zero_spread_q25_is_a_valid_inclusive_cost_cap() -> None:
    bars = _series(300, spread_bps=0.0)
    prepared = rsts.prepare_series(bars)

    context = rsts.baseline_context_at(prepared, signal_index=241)

    assert context is not None
    assert context.spread_q25_bps == 0.0

    _plant_signal(
        bars,
        241,
        side="BUY",
        return_sign=1,
        signal_spread_bps=0.0,
        entry_spread_bps=0.0,
    )
    cell = rsts.screen_cell(
        prepared=rsts.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    trade = cell["trade_ledger"][0]
    assert reservation["spread_q25_bps"] == 0.0
    assert rsts._scored_reservation_semantics_are_valid(
        reservation,
        trade=trade,
        key=(THETA_05.config_id, "EURUSD", "BUY"),
    )


def test_future_outcomes_cannot_change_context_or_frozen_signal() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    prepared = rsts.prepare_series(bars)
    before_context = rsts.baseline_context_at(prepared, signal_index=300)
    before = _signal(bars, 300, "BUY")

    changed = list(bars)
    for index in range(302, len(changed)):
        bar = changed[index]
        changed[index] = dataclasses.replace(bar, volume=bar.volume + index)
    after_prepared = rsts.prepare_series(changed)
    after_context = rsts.baseline_context_at(after_prepared, signal_index=300)
    after = _signal(changed, 300, "BUY")
    assert after_context == before_context
    assert after == before


def test_zero_signs_are_excluded_without_repairing_transitions() -> None:
    overrides = {
        180 + index: (0 if index in {30, 60, 90} else 1) for index in range(120)
    }
    bars = _series(400, sign_overrides=overrides)
    context = rsts.baseline_context_at(rsts.prepare_series(bars), signal_index=300)
    assert context is not None
    assert context.active_transitions == 113
    assert context.same_sign_transitions == 113
    assert context.opposite_sign_transitions == 0
    assert context.transition_state == 1.0

    sparse = {180 + index: (0 if index % 3 == 1 else 1) for index in range(120)}
    sparse_context = rsts.baseline_context_at(
        rsts.prepare_series(_series(400, sign_overrides=sparse)),
        signal_index=300,
    )
    assert sparse_context is None


@pytest.mark.parametrize(
    ("alternating", "side", "return_sign", "expected_state", "expected_score"),
    [
        (False, "BUY", 1, 1.0, 1.0),
        (False, "SELL", -1, 1.0, -1.0),
        (True, "BUY", -1, -1.0, 1.0),
        (True, "SELL", 1, -1.0, -1.0),
    ],
)
def test_all_state_return_quadrants_are_symmetric(
    alternating: bool,
    side: str,
    return_sign: int,
    expected_state: float,
    expected_score: float,
) -> None:
    bars = _series(400, alternating=alternating)
    _plant_signal(bars, 300, side=side, return_sign=return_sign)
    signal = _signal(bars, 300, side)
    trade = rsts.simulate_trade(bars, signal=signal)
    assert signal.transition_state == expected_state
    assert signal.signal_sign == return_sign
    assert signal.forecast_score == expected_score
    assert signal.p_star <= rsts.P_STAR_MAX
    assert trade is not None
    assert trade.exit_reason == "tp"
    assert trade.full_target_win is True


def test_forecast_threshold_boundaries_are_inclusive() -> None:
    # 81 nonzero signs followed by 39 zero signs yield 80 active
    # transitions: 42 same and 38 opposite, so a=0.05 exactly.
    transition_steps = [1] * 42 + [-1] * 38
    signs = [1]
    for transition in transition_steps:
        signs.append(signs[-1] if transition == 1 else -signs[-1])
    signs.extend([0] * 39)
    assert len(signs) == 120
    overrides = {180 + index: sign for index, sign in enumerate(signs)}
    bars = _series(400, sign_overrides=overrides)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    prepared = rsts.prepare_series(bars)
    context = rsts.baseline_context_at(prepared, signal_index=300)
    assert context is not None
    assert (context.same_sign_transitions, context.opposite_sign_transitions) == (
        42,
        38,
    )
    assert context.transition_state == pytest.approx(0.05)

    admitted, reason = rsts.evaluate_signal(
        prepared=prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert admitted is not None, reason
    rejected, reason = rsts.evaluate_signal(
        prepared=prepared,
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=rsts.RSTSConfig(0.10),
        proxy_spread_budget_bps=1.0,
    )
    assert rejected is None
    assert reason == "forecast_score_below_buy_threshold"


def test_signal_return_true_range_and_both_spread_caps_are_causal() -> None:
    bars = _series(400)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    assert _signal(bars, 300, "BUY")

    tiny = list(bars)
    previous = tiny[299].mid_c
    tiny[300] = _bar(
        300,
        mid_o=previous,
        mid_c=previous * math.exp(0.10 / 1e4),
        low_pad=previous * 0.00205,
        high_pad=previous * 0.00001,
        spread_bps=0.15,
    )
    candidate, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(tiny),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert candidate is None
    assert reason == "signal_return_too_small"

    too_wide = list(bars)
    too_wide[300] = _replace_spread(too_wide[300], 0.21)
    candidate, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(too_wide),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert candidate is None
    assert reason == "signal_spread_above_q25_or_proxy"

    wide_entry = list(bars)
    wide_entry[301] = _replace_spread(wide_entry[301], 0.21)
    candidate, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(wide_entry),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert candidate is None
    assert reason == "entry_spread_above_q25_or_proxy"

    oversized = list(bars)
    prior = oversized[299].mid_c
    close = prior * math.exp(1.0 / 1e4)
    oversized[300] = _bar(
        300,
        mid_o=prior,
        mid_c=close,
        low_pad=prior * 0.003,
        high_pad=prior * 0.00001,
        spread_bps=0.15,
    )
    candidate, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(oversized),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert candidate is None
    assert reason == "signal_true_range_too_large"


def test_stop_uses_t_minus_2_through_t_and_cost_algebra_is_exact() -> None:
    bars = _series(400)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    low_outside_swing = dataclasses.replace(
        bars[297],
        # Preserve the midpoint low (and therefore baseline TR) while changing
        # the executable bid extreme outside the frozen t-2:t stop slice.
        bid_l=bars[297].bid_l - 0.02,
        ask_l=bars[297].ask_l + 0.02,
    )
    outside = list(bars)
    outside[297] = low_outside_swing
    baseline = _signal(bars, 300, "BUY", budget=1.0)
    unchanged = _signal(outside, 300, "BUY", budget=1.0)
    assert unchanged.structural_stop == baseline.structural_stop
    assert unchanged.risk_bps == baseline.risk_bps

    stressed = _signal(bars, 300, "BUY", budget=1.0)
    cheap = _signal(bars, 300, "BUY", budget=0.20)
    assert stressed.incremental_spread_stress_bps == pytest.approx(0.85)
    assert stressed.execution_cost_debit_bps == pytest.approx(1.85)
    assert stressed.recorded_cost_bps == pytest.approx(2.0)
    assert stressed.p_star == pytest.approx(
        (stressed.risk_bps + stressed.execution_cost_debit_bps)
        / (2.0 * stressed.risk_bps)
    )
    assert stressed.p_star > cheap.p_star
    stressed_trade = rsts.simulate_trade(bars, signal=stressed)
    cheap_trade = rsts.simulate_trade(bars, signal=cheap)
    assert stressed_trade is not None and cheap_trade is not None
    assert cheap_trade.pnl_bps - stressed_trade.pnl_bps == pytest.approx(0.80)


def test_known_entry_rejections_do_not_reserve_but_missing_fills_do() -> None:
    rejected = _series(370)
    _plant_signal(
        rejected,
        300,
        side="BUY",
        return_sign=1,
        entry_spread_bps=0.21,
        target=False,
    )
    _plant_signal(rejected, 340, side="BUY", return_sign=1)
    cell = rsts.screen_cell(
        prepared=rsts.prepare_series(rejected),
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["reservation_ledger"][0]["signal_index"] == 340
    assert cell["reasons"]["entry_spread_above_q25_or_proxy"] >= 1

    missing = _series(570)
    _plant_signal(missing, 300, side="BUY", return_sign=1, target=False)
    _plant_signal(missing, 542, side="BUY", return_sign=1)
    missing = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(missing)
    ]
    cell = rsts.screen_cell(
        prepared=rsts.prepare_series(missing),
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    reservation = cell["reservation_ledger"][0]
    assert reservation["reservation_status"] == "unresolved"
    assert reservation["outcome_reason"] == "exact_next_open_gap"
    assert reservation["full_target_win"] is False
    assert reservation["gate_pnl_r"] < -1.0
    assert cell["reasons"]["entry_day_already_reserved"] >= 1


def test_incomplete_horizon_reserves_before_outcome_check() -> None:
    bars = _series(310)
    _plant_signal(bars, 300, side="BUY", return_sign=1, target=False)
    cell = rsts.screen_cell(
        prepared=rsts.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["scored_trades"] == 0
    row = cell["reservation_ledger"][0]
    assert row["outcome_reason"] == "incomplete_outcome_horizon"
    assert row["gate_treatment"] == "unresolved_as_adverse_stop_for_discovery_gate"
    assert row["gate_pnl_r"] == pytest.approx(
        -(row["risk_bps"] + row["execution_cost_debit_bps"]) / row["risk_bps"]
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exit_quotes_gaps_target_cap_and_stop_first_ambiguity(side: str) -> None:
    bars = _series(400)
    sign = 1 if side == "BUY" else -1
    _plant_signal(bars, 300, side=side, return_sign=sign)
    signal = _signal(bars, 300, side)
    entry_bar = bars[signal.entry_index]

    adverse = list(bars)
    if side == "BUY":
        gap_open = signal.stop_price * 0.999
        adverse[signal.entry_index] = dataclasses.replace(
            entry_bar,
            bid_o=gap_open,
            bid_h=max(gap_open, entry_bar.bid_h),
            bid_l=min(gap_open, entry_bar.bid_l),
        )
    else:
        gap_open = signal.stop_price * 1.001
        adverse[signal.entry_index] = dataclasses.replace(
            entry_bar,
            ask_o=gap_open,
            ask_h=max(gap_open, entry_bar.ask_h),
            ask_l=min(gap_open, entry_bar.ask_l),
        )
    trade = rsts.simulate_trade(adverse, signal=signal)
    assert trade is not None
    assert trade.exit_reason == "sl_gap_open"
    assert trade.exit_price == gap_open

    favorable = list(bars)
    if side == "BUY":
        favorable[signal.entry_index] = dataclasses.replace(
            entry_bar,
            bid_o=signal.target_price * 1.001,
            bid_h=signal.target_price * 1.002,
        )
    else:
        favorable[signal.entry_index] = dataclasses.replace(
            entry_bar,
            ask_o=signal.target_price * 0.999,
            ask_l=signal.target_price * 0.998,
        )
    trade = rsts.simulate_trade(favorable, signal=signal)
    assert trade is not None
    assert trade.exit_reason == "tp_gap_open"
    assert trade.exit_price == signal.target_price

    ambiguous = list(bars)
    if side == "BUY":
        ambiguous[signal.entry_index] = dataclasses.replace(
            entry_bar,
            bid_l=signal.stop_price * 0.999,
            bid_h=signal.target_price * 1.001,
        )
    else:
        ambiguous[signal.entry_index] = dataclasses.replace(
            entry_bar,
            ask_l=signal.target_price * 0.999,
            ask_h=signal.stop_price * 1.001,
        )
    trade = rsts.simulate_trade(ambiguous, signal=signal)
    assert trade is not None
    assert trade.exit_reason == "sl_double_touch"
    assert trade.full_target_win is False


def test_horizon_is_exactly_20_bars_including_entry() -> None:
    bars = _series(400)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    signal = _signal(bars, 300, "BUY")
    neutral = list(bars)
    mid = signal.entry_price
    for index in range(signal.entry_index, signal.entry_index + 21):
        neutral[index] = _bar(
            index,
            mid_o=mid,
            mid_c=mid,
            low_pad=mid * 0.00001,
            high_pad=mid * 0.00001,
            spread_bps=0.15,
        )
    twentieth = signal.entry_index + 19
    neutral[twentieth] = dataclasses.replace(
        neutral[twentieth], bid_h=signal.target_price * 1.001
    )
    trade = rsts.simulate_trade(neutral, signal=signal)
    assert trade is not None
    assert (trade.exit_reason, trade.bars_held) == ("tp", 20)

    outside = list(neutral)
    outside[twentieth] = _bar(
        twentieth,
        mid_o=mid,
        mid_c=mid,
        low_pad=mid * 0.00001,
        high_pad=mid * 0.00001,
        spread_bps=0.15,
    )
    outside[signal.entry_index + 20] = dataclasses.replace(
        outside[signal.entry_index + 20], bid_h=signal.target_price * 1.001
    )
    trade = rsts.simulate_trade(outside, signal=signal)
    assert trade is not None
    assert (trade.exit_reason, trade.bars_held) == ("time_stop", 20)


def test_risk_pstar_and_target_cost_rejections_are_exact() -> None:
    over_risk = _series(400)
    _plant_signal(over_risk, 300, side="BUY", return_sign=1)
    bar = over_risk[298]
    over_risk[298] = dataclasses.replace(
        bar,
        bid_l=bar.bid_l - 0.30,
        ask_l=bar.ask_l - 0.30,
    )
    candidate, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(over_risk),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert candidate is None
    assert reason == "risk_above_25bps"

    dead = _series(400)
    previous = dead[299].mid_c
    close = previous * math.exp(1.0 / 1e4)
    dead[300] = _bar(
        300,
        mid_o=previous,
        mid_c=close,
        low_pad=previous * 0.00010,
        high_pad=previous * 0.00001,
        spread_bps=0.15,
    )
    dead[301] = _bar(
        301,
        mid_o=close,
        mid_c=close,
        low_pad=close * 0.00001,
        high_pad=close * 0.00001,
        spread_bps=0.15,
    )
    candidate, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(dead),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    assert candidate is None
    assert reason == "bracket_cost_dead"

    target_dead = _series(400, spread_bps=3.0)
    previous = target_dead[299].mid_c
    close = previous * math.exp(1.0 / 1e4)
    target_dead[300] = _bar(
        300,
        mid_o=previous,
        mid_c=close,
        low_pad=previous * 0.00110,
        high_pad=previous * 0.00001,
        spread_bps=2.9,
    )
    target_dead[301] = _bar(
        301,
        mid_o=close,
        mid_c=close,
        low_pad=close * 0.00001,
        high_pad=close * 0.00001,
        spread_bps=2.9,
    )
    candidate, reason = rsts.evaluate_signal(
        prepared=rsts.prepare_series(target_dead),
        signal_index=300,
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=3.0,
    )
    assert candidate is None
    assert reason == "target_too_small_vs_cost"


def test_minimum_consecutive_run_is_exactly_262_bars() -> None:
    assert rsts._has_scorable_run(_series(261)) is False
    assert rsts._has_scorable_run(_series(262)) is True
    gapped = _series(400)
    gapped = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 200 else 0))
        for index, bar in enumerate(gapped)
    ]
    assert rsts._has_scorable_run(gapped) is False


def test_all_72_cells_are_retained_and_have_no_authority() -> None:
    result = _screen()
    cells = result["cells"]
    assert isinstance(cells, list)
    assert len(cells) == 72
    assert {(cell["config_id"], cell["symbol"], cell["side"]) for cell in cells} == {
        (config.config_id, symbol, side)
        for config in rsts.GRID
        for symbol in rsts.FX_SYMBOLS
        for side in ("BUY", "SELL")
    }
    assert all(cell["scored_trades"] == 0 for cell in cells)
    assert all(cell["source_ready"] is False for cell in cells)
    assert all(cell["passes_discovery_cell_gate"] is False for cell in cells)
    assert result["reservation_ledger"] == []
    assert result["trade_ledger"] == []
    assert result["discovery_gate"]["passed"] is False
    for field in (
        "success_claim_authorized",
        "activation_authorized",
        "registry_write_authorized",
        "order_authorized",
    ):
        assert result[field] is False

    invalid_proxy = _screen(provenance=PROVENANCE | {"frozen": False})
    assert all(cell["proxy_budget_ready"] is False for cell in invalid_proxy["cells"])
    assert all(
        cell["cost_mode"] == "cost_unavailable" for cell in invalid_proxy["cells"]
    )


def test_direct_screen_call_rejects_prestart_and_postend_bars() -> None:
    start = BASE_EPOCH
    end = BASE_EPOCH + 262 * 60

    def iso(epoch: int) -> str:
        return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()

    def direct(
        bars: list[rsts.QuoteBar], start_epoch: int = start
    ) -> dict[str, object]:
        return rsts.screen_universe(
            bars_by_symbol={"EURUSD": bars},
            proxy_spread_budgets_bps=_budgets(),
            proxy_provenance=PROVENANCE,
            proxy_contract_schema_version=PROXY_SCHEMA,
            evaluation_start_utc=iso(start_epoch),
            evaluation_end_utc=iso(end),
            preregistration_lock_utc=PREREGISTRATION_LOCK,
        )

    ready = direct(_series(262))
    eurusd = [cell for cell in ready["cells"] if cell["symbol"] == "EURUSD"]
    assert all(cell["source_ready"] is True for cell in eurusd)

    post_end = direct(_series(263))
    eurusd = [cell for cell in post_end["cells"] if cell["symbol"] == "EURUSD"]
    assert all(
        cell["source_error"] == "bar_outside_evaluation_window" for cell in eurusd
    )
    assert post_end["reservation_ledger"] == []

    pre_start = direct(_series(262), start_epoch=BASE_EPOCH + 60)
    eurusd = [cell for cell in pre_start["cells"] if cell["symbol"] == "EURUSD"]
    assert all(
        cell["source_error"] == "bar_outside_evaluation_window" for cell in eurusd
    )
    assert pre_start["trade_ledger"] == []


def test_short_or_explicitly_flagged_source_cannot_emit_evidence() -> None:
    short = _screen({"EURUSD": _series(261)})
    eurusd = [cell for cell in short["cells"] if cell["symbol"] == "EURUSD"]
    assert all(cell["source_error"] == "no_complete_262_bar_m1_run" for cell in eurusd)
    assert short["reservation_ledger"] == []

    bars = _series(400)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    flagged = rsts.screen_universe(
        bars_by_symbol={"EURUSD": bars},
        proxy_spread_budgets_bps=_budgets(),
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
        source_errors={"EURUSD": "caller_flagged_source_failure"},
    )
    assert flagged["reservation_ledger"] == []
    assert flagged["trade_ledger"] == []


def test_trial_ledger_wilson_and_student_t_constants_are_frozen() -> None:
    assert [config.config_id for config in rsts.GRID] == ["theta05", "theta10"]
    assert rsts.trial_accounting() == {
        "grid_configurations": 2,
        "directions": 2,
        "symbols": 18,
        "current_attempted_cells": 72,
        "prior_attempted_cells": 4_242,
        "cumulative_attempted_cells": 4_314,
        "expected_full_universe_cells": 72,
    }
    assert rsts.TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD == (
        4.619583862948160
    )
    assert rsts._one_sided_wilson_lower_bound(91, 91) < 0.90
    assert rsts._one_sided_wilson_lower_bound(92, 92) >= 0.90
    assert rsts._one_sided_wilson_lower_bound(90, 100) < 0.90
    assert rsts._finite_one_sample_t([]) == 0.0
    assert rsts._finite_one_sample_t([1.0] * 100) == 1e12
    assert rsts._finite_one_sample_t([-1.0] * 100) == -1e12


def test_unique_day_counts_do_not_claim_within_cell_independence() -> None:
    result = _screen()
    gate = result["discovery_gate"]

    assert gate["minimum_unique_reserved_utc_entry_days_per_cell"] == 100
    assert (
        "does not establish statistical independence"
        in gate["unique_reserved_utc_entry_days_definition"]
    )
    assert gate["cross_cell_bonferroni_requires_cross_cell_independence"] is False
    assert gate["within_cell_serial_dependence_adjustment_applied"] is False
    assert gate["wilson_within_cell_serial_dependence_robust"] is False
    assert gate["student_t_within_cell_serial_dependence_robust"] is False
    assert gate["inferential_calibration_authorized"] is False
    assert all(
        "unique_reserved_utc_entry_days" in cell
        and "independent_entry_days" not in cell
        for cell in result["cells"]
    )


def test_emitted_baseline_contract_distinguishes_median_tr_from_q25_spread() -> None:
    contract = _screen()["fixed_contract"]
    baseline = contract["baseline"]

    assert "median of exactly 240 M1 midpoint true ranges" in baseline
    assert "nearest-rank Q25 (sorted index 59)" in baseline
    known_rejects = contract["known_entry_rejection_treatment"]
    assert "all known completion rejects" in known_rejects
    assert "degenerate stop or bracket" in known_rejects


def test_global_gate_requires_one_unchanged_theta_across_all_36_cells() -> None:
    config_id = THETA_05.config_id
    complete = [
        {
            "config_id": config_id,
            "symbol": symbol,
            "side": side,
            "passes_discovery_cell_gate": True,
        }
        for symbol in rsts.FX_SYMBOLS
        for side in ("BUY", "SELL")
    ]
    assert rsts._passing_global_configurations(complete) == [config_id]
    changed = [dict(row) for row in complete]
    changed[-1]["config_id"] = "theta10"
    assert rsts._passing_global_configurations(changed) == []
    assert rsts._passing_global_configurations(complete[:-1]) == []


def test_temporal_thirds_use_half_open_boundaries_and_strict_vetoes() -> None:
    start = rsts.CALENDAR_MONTH_BOUNDARY_EPOCHS[0]
    end = rsts.CALENDAR_MONTH_BOUNDARY_EPOCHS[-1]
    boundaries = rsts._temporal_thirds_boundary_epochs(
        evaluation_start_epoch=start,
        evaluation_end_epoch=end,
    )
    assert [
        rsts._temporal_third_index(
            boundary,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )
        for boundary in boundaries
    ] == [0, 1, 2, 2]

    rows: list[dict[str, object]] = []
    for segment in range(3):
        for index in range(30):
            win = index < 27
            rows.append(
                {
                    "entry_epoch": boundaries[segment] + index * 60,
                    "full_target_win": win,
                    "gate_pnl_r": 1.0 if win else -1.0,
                }
            )
    diagnostics = rsts._temporal_thirds_diagnostics(
        rows,
        evaluation_start_epoch=start,
        evaluation_end_epoch=end,
    )
    assert diagnostics["temporal_thirds_reservation_counts"] == [30, 30, 30]
    assert diagnostics["temporal_thirds_full_target_rates"] == [0.9, 0.9, 0.9]
    assert diagnostics["gate_temporal_thirds_stable"] is True

    sparse = rows[:-1]
    assert (
        rsts._temporal_thirds_diagnostics(
            sparse,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_temporal_thirds_stable"]
        is False
    )
    low_rate = copy.deepcopy(rows)
    low_rate[0]["full_target_win"] = False
    assert (
        rsts._temporal_thirds_diagnostics(
            low_rate,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_temporal_thirds_stable"]
        is False
    )
    nonpositive = copy.deepcopy(rows)
    for row in nonpositive[:30]:
        row["gate_pnl_r"] = 0.0
    assert (
        rsts._temporal_thirds_diagnostics(
            nonpositive,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_temporal_thirds_stable"]
        is False
    )


def test_calendar_months_use_exact_boundaries_end_cap_and_vetoes() -> None:
    boundaries = rsts.CALENDAR_MONTH_BOUNDARY_EPOCHS
    assert boundaries == (
        1_672_531_200,
        1_675_209_600,
        1_677_628_800,
        1_680_307_200,
        1_682_899_200,
        1_685_577_600,
        1_688_169_600,
    )
    start, end = boundaries[0], boundaries[-1]
    assert [
        rsts._calendar_month_index(
            epoch,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )
        for epoch in boundaries
    ] == [0, 1, 2, 3, 4, 5, 5]

    rows: list[dict[str, object]] = []
    for month in range(6):
        for day in range(20):
            win = day < 18
            rows.append(
                {
                    "entry_epoch": boundaries[month] + day * 86_400,
                    "full_target_win": win,
                    "gate_pnl_r": 1.0 if win else -0.25,
                    "reservation_status": "scored" if win else "unresolved",
                }
            )
    diagnostics = rsts._calendar_month_diagnostics(
        rows,
        evaluation_start_epoch=start,
        evaluation_end_epoch=end,
    )
    assert diagnostics["calendar_month_reservation_counts"] == [20] * 6
    assert diagnostics["calendar_month_full_target_rates"] == [0.9] * 6
    assert diagnostics["gate_calendar_months_stable"] is True

    mutated = copy.deepcopy(rows)
    mutated[0]["full_target_win"] = False
    assert (
        rsts._calendar_month_diagnostics(
            mutated,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_calendar_months_stable"]
        is False
    )
    wrong_window = rsts._calendar_month_diagnostics(
        rows,
        evaluation_start_epoch=start + 60,
        evaluation_end_epoch=end,
    )
    assert wrong_window["gate_calendar_months_stable"] is False


def test_exact_row_schemas_ids_and_semantic_recomputation() -> None:
    bars = _series(400)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    cell = rsts.screen_cell(
        prepared=rsts.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    trade = cell["trade_ledger"][0]
    key = (THETA_05.config_id, "EURUSD", "BUY")
    assert set(reservation) == rsts.RSTS_RESERVATION_FIELD_NAMES
    assert set(trade) == rsts.RSTS_TRADE_FIELD_NAMES
    assert rsts._scored_reservation_semantics_are_valid(
        reservation,
        trade=trade,
        key=key,
    )
    assert reservation["event_id"] == rsts._event_id_fields(
        config_id=key[0],
        symbol=key[1],
        side=key[2],
        signal_epoch=reservation["signal_epoch"],
        entry_epoch=reservation["entry_epoch"],
    )

    for field, value in (
        ("forecast_score", 0.0),
        ("active_transitions", 118),
        ("recorded_cost_bps", 0.0),
        ("event_id", "forged"),
        ("pnl_bps", 999.0),
    ):
        forged = dict(reservation)
        forged[field] = value
        assert not rsts._scored_reservation_semantics_are_valid(
            forged,
            trade=trade,
            key=key,
        )
    extra = dict(reservation)
    extra["unexpected"] = True
    assert not rsts._scored_reservation_semantics_are_valid(extra, trade=trade, key=key)
    impossible_trade = dict(trade)
    impossible_trade["exit_reason"] = "time_stop"
    assert not rsts._trade_semantics_are_valid(impossible_trade, key=key)


def test_complete_source_replay_rejects_consistent_ledger_omission() -> None:
    bars = _series(400)
    _plant_signal(bars, 300, side="BUY", return_sign=1)
    result = _screen({"EURUSD": bars})
    cells = copy.deepcopy(result["cells"])
    reservations = copy.deepcopy(result["reservation_ledger"])
    trades = copy.deepcopy(result["trade_ledger"])
    target = next(
        cell
        for cell in cells
        if (cell["config_id"], cell["symbol"], cell["side"])
        == ("theta05", "EURUSD", "BUY")
    )
    removed_ids = set(target["reservation_event_ids"])
    assert removed_ids
    reservations = [row for row in reservations if row["event_id"] not in removed_ids]
    trades = [row for row in trades if row["event_id"] not in removed_ids]
    target.update(
        {
            "eligible_events": 0,
            "entry_day_reservations": 0,
            "unresolved_reservations": 0,
            "scored_trades": 0,
            "full_target_wins": 0,
            "full_target_trade_win_rate": 0.0,
            "full_target_reservation_rate": 0.0,
            "positive_outcomes": 0,
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
    prepared = {symbol: rsts.prepare_series([]) for symbol in rsts.FX_SYMBOLS}
    prepared["EURUSD"] = rsts.prepare_series(bars)
    gate = rsts._apply_discovery_gate(
        cells=cells,
        reservation_ledger=reservations,
        trade_ledger=trades,
        evaluation_start_epoch=rsts.CALENDAR_MONTH_BOUNDARY_EPOCHS[0],
        evaluation_end_epoch=rsts.CALENDAR_MONTH_BOUNDARY_EPOCHS[-1],
        expected_proxy_spread_budgets_bps=_budgets(),
        prepared_series_by_symbol=prepared,
    )
    assert gate["full_canonical_universe_present"] is True
    assert gate["passed"] is False
    assert target["gate_source_replay_consistent"] is False
    assert target["gate_ledger_consistent"] is False


def test_forged_unresolved_win_and_missing_fill_schema_fail_closed() -> None:
    bars = _series(302)
    _plant_signal(bars, 300, side="BUY", return_sign=1, target=False)
    bars = bars[:301]
    cell = rsts.screen_cell(
        prepared=rsts.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=THETA_05,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    key = (THETA_05.config_id, "EURUSD", "BUY")
    assert set(reservation) == rsts.RSTS_MISSING_FILL_RESERVATION_FIELD_NAMES
    assert rsts._unresolved_reservation_semantics_are_valid(reservation, key=key)
    forged = dict(reservation)
    forged["full_target_win"] = True
    forged["positive_outcome"] = True
    forged["gate_pnl_r"] = 1.0
    assert not rsts._unresolved_reservation_semantics_are_valid(forged, key=key)
    omitted = dict(reservation)
    omitted.pop("gate_risk_basis_bps")
    assert not rsts._unresolved_reservation_semantics_are_valid(omitted, key=key)


@pytest.mark.parametrize(
    "mutation",
    ["header", "naive_utc", "subminute", "duplicate_epoch", "nonfinite"],
)
def test_csv_loader_requires_exact_schema_utc_m1_and_finite_quotes(
    tmp_path: Path,
    mutation: str,
) -> None:
    path = tmp_path / "EURUSD_M1.csv"
    _write_m1_csv(path, _series(3))
    lines = path.read_text(encoding="utf-8").splitlines()
    if mutation == "header":
        lines[0] = lines[0] + ",extra"
    elif mutation == "naive_utc":
        lines[1] = lines[1].replace("2023-01-01T00:00:00Z", "2023-01-01T00:00:00")
    elif mutation == "subminute":
        lines[1] = lines[1].replace("2023-01-01T00:00:00Z", "2023-01-01T00:00:30Z")
    elif mutation == "duplicate_epoch":
        lines[2] = ",".join((lines[1].split(",", 1)[0], lines[2].split(",", 1)[1]))
    elif mutation == "nonfinite":
        fields = lines[1].split(",")
        fields[1] = "NaN"
        lines[1] = ",".join(fields)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        rsts.load_m1_csv(path)


def test_csv_loader_filters_a_half_open_interval(tmp_path: Path) -> None:
    path = tmp_path / "EURUSD_M1.csv"
    _write_m1_csv(path, _series(4))
    rows = rsts.load_m1_csv(
        path,
        start="2023-01-01T00:01:00Z",
        end="2023-01-01T00:03:00Z",
    )
    assert [row.epoch for row in rows] == [BASE_EPOCH + 60, BASE_EPOCH + 120]


def test_proxy_contract_is_strict_frozen_and_data_blind(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    _write_proxy(valid)
    budgets, provenance, schema = rsts._load_proxy_contract(
        valid,
        evaluation_start_utc=EVALUATION_START,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
    )
    assert budgets == _budgets()
    assert provenance == PROVENANCE
    assert schema == PROXY_SCHEMA

    invalid_payloads = {
        "duplicate.json": (
            '{"schema_version":"x","schema_version":"x",'
            '"provenance":{},"budgets_bps":{}}'
        ),
        "nan.json": (
            '{"schema_version":"fxstack.scalp.pair_proxy_spread_contract.v1",'
            '"provenance":{},"budgets_bps":{"EURUSD":NaN}}'
        ),
        "duplicate_symbol.json": json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": {"EURUSD": 1.0, " eurusd ": 1.0},
            }
        ),
        "unfrozen.json": json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE | {"frozen": False},
                "budgets_bps": _budgets(),
            }
        ),
        "late_cutoff.json": json.dumps(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE
                | {"source_cutoff_utc": "2023-01-01T00:01:00Z"},
                "budgets_bps": _budgets(),
            }
        ),
    }
    for name, payload in invalid_payloads.items():
        path = tmp_path / name
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError):
            rsts._load_proxy_contract(
                path,
                evaluation_start_utc=EVALUATION_START,
                preregistration_lock_utc=PREREGISTRATION_LOCK,
            )
    with pytest.raises(ValueError):
        rsts._normalize_proxy_budgets({"EURUSD": True})


def _cli_args(
    *,
    csv_root: Path,
    proxy: Path,
    reservation: Path,
    trade: Path,
    screen: Path,
) -> list[str]:
    return [
        "--csv-root",
        str(csv_root),
        "--start",
        EVALUATION_START,
        "--end",
        EVALUATION_END,
        "--preregistration-lock-utc",
        PREREGISTRATION_LOCK,
        "--proxy-budgets-json",
        str(proxy),
        "--reservation-ledger-out",
        str(reservation),
        "--trade-ledger-out",
        str(trade),
        "--json-out",
        str(screen),
    ]


def test_cli_writes_distinct_nonfinite_safe_evidence_and_fails_readiness(
    tmp_path: Path,
) -> None:
    proxy = tmp_path / "proxy.json"
    _write_proxy(proxy)
    reservation = tmp_path / "out" / "reservations.json"
    trade = tmp_path / "out" / "trades.json"
    screen = tmp_path / "out" / "screen.json"
    args = _cli_args(
        csv_root=tmp_path / "missing-csv-root",
        proxy=proxy,
        reservation=reservation,
        trade=trade,
        screen=screen,
    )
    assert rsts.main(args) == 2
    assert reservation.exists() and trade.exists() and screen.exists()
    assert (
        len({rsts._sha256(reservation), rsts._sha256(trade), rsts._sha256(screen)}) == 3
    )
    for path in (reservation, trade, screen):
        text = path.read_text(encoding="utf-8")
        assert "NaN" not in text
        assert "Infinity" not in text
        json.loads(text)
    reservation_payload = json.loads(reservation.read_text(encoding="utf-8"))
    trade_payload = json.loads(trade.read_text(encoding="utf-8"))
    screen_payload = json.loads(screen.read_text(encoding="utf-8"))
    assert reservation_payload["schema_version"].endswith("reservation_ledger.v1")
    assert trade_payload["schema_version"].endswith("trade_ledger.v1")
    assert screen_payload["success_claim_authorized"] is False
    assert len(screen_payload["cost_readiness"]["source_failure_symbols"]) == 18

    with pytest.raises(SystemExit):
        rsts.main(args)
    collision = _cli_args(
        csv_root=tmp_path,
        proxy=proxy,
        reservation=tmp_path / "same.json",
        trade=tmp_path / "same.json",
        screen=tmp_path / "other.json",
    )
    with pytest.raises(SystemExit):
        rsts.main(collision)


def test_cli_returns_zero_only_when_all_sources_and_proxy_budgets_are_ready(
    tmp_path: Path,
) -> None:
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    bars = _series(262)
    for symbol in rsts.FX_SYMBOLS:
        _write_m1_csv(csv_root / f"{symbol}_M1.csv", bars)
    proxy = tmp_path / "proxy.json"
    _write_proxy(proxy)
    reservation = tmp_path / "ready" / "reservations.json"
    trade = tmp_path / "ready" / "trades.json"
    screen = tmp_path / "ready" / "screen.json"
    assert (
        rsts.main(
            _cli_args(
                csv_root=csv_root,
                proxy=proxy,
                reservation=reservation,
                trade=trade,
                screen=screen,
            )
        )
        == 0
    )
    payload = json.loads(screen.read_text(encoding="utf-8"))
    assert payload["cost_readiness"]["source_failure_symbols"] == []
    assert payload["cost_readiness"]["missing_proxy_budget_symbols"] == []
    assert len(payload["input_metadata"]["m1_csv_sha256_by_symbol"]) == 18
    assert len(payload["cells"]) == 72
    assert payload["discovery_gate"]["passed"] is False

    incomplete_proxy = tmp_path / "incomplete-proxy.json"
    _write_proxy(incomplete_proxy, {"EURUSD": 1.0})
    reservation = tmp_path / "incomplete" / "reservations.json"
    trade = tmp_path / "incomplete" / "trades.json"
    screen = tmp_path / "incomplete" / "screen.json"
    assert (
        rsts.main(
            _cli_args(
                csv_root=csv_root,
                proxy=incomplete_proxy,
                reservation=reservation,
                trade=trade,
                screen=screen,
            )
        )
        == 2
    )
    payload = json.loads(screen.read_text(encoding="utf-8"))
    assert len(payload["cost_readiness"]["missing_proxy_budget_symbols"]) == 17
