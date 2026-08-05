"""Synthetic-only adversarial tests for the frozen QSCC-v1 causal screen."""

from __future__ import annotations

import copy
import dataclasses
import inspect
import math
import random
import statistics
from pathlib import Path
from typing import Any

import pytest

import fxstack.scalp.screen_quote_side_convergence_continuation as qscc


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
PROXY_SCHEMA = qscc.PROXY_CONTRACT_SCHEMA_VERSION
CONFIG = qscc.GRID[0]


def _bar(
    index: int,
    *,
    mid_o: float = 100.0,
    mid_h: float = 100.05,
    mid_l: float = 99.95,
    mid_c: float = 100.0,
    spread_bps: float = 0.20,
    epoch_shift: int = 0,
    volume: float = 1.0,
) -> qscc.QuoteBar:
    def quotes(mid: float) -> tuple[float, float]:
        half = mid * spread_bps / 2e4
        return mid - half, mid + half

    bid_o, ask_o = quotes(mid_o)
    bid_h, ask_h = quotes(mid_h)
    bid_l, ask_l = quotes(mid_l)
    bid_c, ask_c = quotes(mid_c)
    return qscc.QuoteBar(
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


def _series(count: int = 1_100, *, spread_bps: float = 0.50) -> list[qscc.QuoteBar]:
    return [_bar(index, spread_bps=spread_bps) for index in range(count)]


def _replace_spread(bar: qscc.QuoteBar, spread_bps: float) -> qscc.QuoteBar:
    def mid(left: float, right: float) -> float:
        return (left + right) / 2.0

    def quotes(value: float) -> tuple[float, float]:
        half = value * spread_bps / 2e4
        return value - half, value + half

    bid_o, ask_o = quotes(mid(bar.bid_o, bar.ask_o))
    bid_h, ask_h = quotes(mid(bar.bid_h, bar.ask_h))
    bid_l, ask_l = quotes(mid(bar.bid_l, bar.ask_l))
    bid_c, ask_c = quotes(mid(bar.bid_c, bar.ask_c))
    return qscc.QuoteBar(
        epoch=bar.epoch,
        bid_o=bid_o,
        bid_h=bid_h,
        bid_l=bid_l,
        bid_c=bid_c,
        ask_o=ask_o,
        ask_h=ask_h,
        ask_l=ask_l,
        ask_c=ask_c,
        volume=bar.volume,
    )


def _plant_signal(
    bars: list[qscc.QuoteBar],
    index: int,
    *,
    side: str,
    prior_spread_bps: float = 0.90,
    signal_spread_bps: float = 0.45,
    entry_spread_bps: float = 0.45,
    target: bool = True,
) -> None:
    if side == "BUY":
        prior_close = 100.04
        signal_close = 100.06
        bars[index - 1] = _bar(
            index - 1,
            mid_o=100.0,
            mid_h=100.045,
            mid_l=99.995,
            mid_c=prior_close,
            spread_bps=prior_spread_bps,
        )
        bars[index] = _bar(
            index,
            mid_o=prior_close,
            mid_h=100.065,
            mid_l=100.035,
            mid_c=signal_close,
            spread_bps=signal_spread_bps,
        )
    elif side == "SELL":
        prior_close = 99.96
        signal_close = 99.94
        bars[index - 1] = _bar(
            index - 1,
            mid_o=100.0,
            mid_h=100.005,
            mid_l=99.955,
            mid_c=prior_close,
            spread_bps=prior_spread_bps,
        )
        bars[index] = _bar(
            index,
            mid_o=prior_close,
            mid_h=99.965,
            mid_l=99.935,
            mid_c=signal_close,
            spread_bps=signal_spread_bps,
        )
    else:
        raise ValueError(side)

    # Freeze a quiet, complete 30-bar outcome slice including the entry bar.
    for outcome_index in range(index + 1, index + 31):
        bars[outcome_index] = _bar(
            outcome_index,
            mid_o=signal_close,
            mid_h=signal_close + 0.002,
            mid_l=signal_close - 0.002,
            mid_c=signal_close,
            spread_bps=entry_spread_bps,
        )
    if target:
        if side == "BUY":
            bars[index + 2] = _bar(
                index + 2,
                mid_o=signal_close,
                mid_h=signal_close + 0.10,
                mid_l=signal_close - 0.002,
                mid_c=signal_close + 0.08,
                spread_bps=entry_spread_bps,
            )
        else:
            bars[index + 2] = _bar(
                index + 2,
                mid_o=signal_close,
                mid_h=signal_close + 0.002,
                mid_l=signal_close - 0.10,
                mid_c=signal_close - 0.08,
                spread_bps=entry_spread_bps,
            )


def _plant_exact_boundary_signal(
    bars: list[qscc.QuoteBar], index: int, *, side: str
) -> None:
    context = qscc.baseline_context_at(qscc.prepare_series(bars), signal_index=index)
    assert context is not None
    volatility_bps = context.volatility_bps
    direction = 1.0 if side == "BUY" else -1.0
    prior_close = 100.0 * math.exp(
        direction * qscc.PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD * volatility_bps / 1e4
    )
    signal_close = prior_close * math.exp(
        direction * qscc.CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD * volatility_bps / 1e4
    )
    if side == "SELL":
        prior_close = math.nextafter(prior_close, -math.inf)
        signal_close = prior_close * math.exp(
            -qscc.CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD * volatility_bps / 1e4
        )
        signal_close = math.nextafter(signal_close, -math.inf)
    if side == "BUY":
        bars[index - 1] = _bar(
            index - 1,
            mid_o=100.0,
            mid_h=prior_close + 0.005,
            mid_l=99.995,
            mid_c=prior_close,
            spread_bps=0.90,
        )
        bars[index] = _bar(
            index,
            mid_o=prior_close,
            mid_h=signal_close + 0.005,
            mid_l=prior_close - 0.005,
            mid_c=signal_close,
            spread_bps=0.45,
        )
    elif side == "SELL":
        bars[index - 1] = _bar(
            index - 1,
            mid_o=100.0,
            mid_h=100.005,
            mid_l=prior_close - 0.005,
            mid_c=prior_close,
            spread_bps=0.90,
        )
        bars[index] = _bar(
            index,
            mid_o=prior_close,
            mid_h=prior_close + 0.005,
            mid_l=signal_close - 0.005,
            mid_c=signal_close,
            spread_bps=0.45,
        )
    else:
        raise ValueError(side)
    for outcome_index in range(index + 1, index + 31):
        bars[outcome_index] = _bar(
            outcome_index,
            mid_o=signal_close,
            mid_h=signal_close + 0.002,
            mid_l=signal_close - 0.002,
            mid_c=signal_close,
            spread_bps=0.45,
        )


def _evaluate(
    bars: list[qscc.QuoteBar],
    index: int,
    side: str,
    *,
    budget: float = 1.0,
) -> tuple[qscc.QSCCSignal | None, str]:
    return qscc.evaluate_signal(
        prepared=qscc.prepare_series(bars),
        signal_index=index,
        symbol="EURUSD",
        side=side,
        config=CONFIG,
        proxy_spread_budget_bps=budget,
    )


def _signal(
    bars: list[qscc.QuoteBar], index: int, side: str, *, budget: float = 1.0
) -> qscc.QSCCSignal:
    signal, reason = _evaluate(bars, index, side, budget=budget)
    assert signal is not None, reason
    return signal


def _budgets(value: float = 1.0) -> dict[str, float]:
    return {symbol: value for symbol in qscc.FX_SYMBOLS}


def _screen(
    bars_by_symbol: dict[str, list[qscc.QuoteBar]] | None = None,
) -> dict[str, Any]:
    return qscc.screen_universe(
        bars_by_symbol=bars_by_symbol or {},
        proxy_spread_budgets_bps=_budgets(),
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
    )


def test_baseline_is_exactly_240_bars_ending_t_minus_2_with_nearest_rank_q25() -> None:
    bars = _series(500)
    # At t=300 the baseline is exactly indices 59..298; index 58 is the
    # additional predecessor needed by the first of the 240 true ranges.
    for offset, index in enumerate(range(59, 299), start=1):
        bars[index] = _replace_spread(bars[index], offset / 100.0)
    prepared = qscc.prepare_series(bars)
    context = qscc.baseline_context_at(prepared, signal_index=300)
    assert context is not None
    expected_tr = statistics.median(
        qscc._true_range_bps(bars[index - 1], bars[index]) for index in range(59, 299)
    )
    assert context.volatility_bps == pytest.approx(expected_tr)
    assert context.spread_q25_bps == pytest.approx(0.60)

    changed = list(bars)
    changed[299] = _bar(
        299,
        mid_o=80.0,
        mid_h=130.0,
        mid_l=70.0,
        mid_c=125.0,
        spread_bps=20.0,
    )
    for index in range(300, len(changed)):
        changed[index] = dataclasses.replace(changed[index], volume=1_000_000.0)
    assert (
        qscc.baseline_context_at(qscc.prepare_series(changed), signal_index=300)
        == context
    )

    inside = list(bars)
    for index in range(178, 299):
        inside[index] = _bar(
            index,
            mid_h=100.20,
            mid_l=99.80,
            spread_bps=bars[index].spread_close_bps,
        )
    assert (
        qscc.baseline_context_at(qscc.prepare_series(inside), signal_index=300)
        != context
    )


def test_rolling_baseline_matches_naive_recomputation_on_randomized_gapped_series() -> (
    None
):
    compared = 0
    for case in range(20):
        generator = random.Random(91_337 + case)
        gap_index = 390 + case
        bars: list[qscc.QuoteBar] = []
        prior_mid = 90.0 + case
        for index in range(750):
            current_mid = prior_mid * math.exp(generator.uniform(-1.5, 1.5) / 1e4)
            radius = generator.uniform(0.01, 0.08)
            bars.append(
                _bar(
                    index,
                    mid_o=prior_mid,
                    mid_h=max(prior_mid, current_mid) + radius,
                    mid_l=min(prior_mid, current_mid) - radius,
                    mid_c=current_mid,
                    spread_bps=generator.uniform(0.05, 2.5),
                    epoch_shift=60 if index >= gap_index else 0,
                    volume=generator.uniform(0.0, 100.0),
                )
            )
            prior_mid = current_mid
        prepared = qscc.prepare_series(bars)
        for signal_index in range(len(bars)):
            optimized = qscc.baseline_context_at(prepared, signal_index=signal_index)
            predecessor = signal_index - qscc.BASELINE_M1_BARS - 2
            if predecessor < 0 or any(
                bars[index].epoch != bars[index - 1].epoch + 60
                for index in range(predecessor + 1, signal_index + 1)
            ):
                assert optimized is None
                continue
            baseline_indices = range(signal_index - 241, signal_index - 1)
            true_ranges = sorted(
                qscc._true_range_bps(bars[index - 1], bars[index])
                for index in baseline_indices
            )
            spreads = sorted(bars[index].spread_close_bps for index in baseline_indices)
            expected = qscc.BaselineContext(
                volatility_bps=(true_ranges[119] + true_ranges[120]) / 2.0,
                spread_q25_bps=spreads[59],
            )
            assert optimized == expected
            compared += 1
    assert compared >= 5_000


def test_q_and_r_are_exact_completed_close_formulas_without_intraminute_claim() -> None:
    previous = _bar(
        10,
        mid_o=99.0,
        mid_h=105.0,
        mid_l=95.0,
        mid_c=100.0,
        spread_bps=0.40,
        volume=2.0,
    )
    current = _bar(
        11,
        mid_o=103.0,
        mid_h=110.0,
        mid_l=90.0,
        mid_c=100.03,
        spread_bps=0.90,
        volume=999.0,
    )
    expected_r = 1e4 * math.log(current.mid_c / previous.mid_c)
    expected_q = 1e4 * (
        math.log(current.ask_c / current.bid_c)
        - math.log(previous.ask_c / previous.bid_c)
    )
    assert qscc._mid_close_return_bps(previous, current) == expected_r
    assert qscc._quote_log_spread_change_bps(previous, current) == expected_q
    assert expected_q > 0.0

    path_only_change = dataclasses.replace(
        current,
        bid_o=current.bid_c,
        ask_o=current.ask_c,
        bid_h=current.bid_h + 5.0,
        ask_h=current.ask_h + 5.0,
        bid_l=current.bid_l - 5.0,
        ask_l=current.ask_l - 5.0,
        volume=0.0,
    )
    assert qscc._mid_close_return_bps(previous, path_only_change) == expected_r
    assert qscc._quote_log_spread_change_bps(previous, path_only_change) == expected_q


def test_future_outcomes_cannot_change_frozen_context_or_signal() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", target=False)
    before_prepared = qscc.prepare_series(bars)
    before_context = qscc.baseline_context_at(before_prepared, signal_index=300)
    before = _signal(bars, 300, "BUY")

    changed = list(bars)
    for index in range(302, len(changed)):
        changed[index] = _bar(
            index,
            mid_o=130.0,
            mid_h=140.0,
            mid_l=120.0,
            mid_c=135.0,
            spread_bps=8.0,
            volume=1_000_000.0,
        )
    assert (
        qscc.baseline_context_at(qscc.prepare_series(changed), signal_index=300)
        == before_context
    )
    assert _signal(changed, 300, "BUY") == before


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_buy_and_mirrored_sell_accept_every_inclusive_formula_boundary(
    side: str,
) -> None:
    bars = _series(500)
    _plant_exact_boundary_signal(bars, 300, side=side)
    signal = _signal(bars, 300, side)
    direction = 1.0 if side == "BUY" else -1.0
    assert direction * signal.prior_return_vol_units == pytest.approx(0.35)
    assert direction * signal.current_return_vol_units == pytest.approx(0.10)
    assert signal.prior_quote_change_bps >= 0.10 * (signal.proxy_budget_bps + 1.0)
    assert signal.signal_quote_change_bps <= -0.50 * signal.prior_quote_change_bps
    assert signal.prior_true_range_vol_units <= 2.5
    assert signal.signal_true_range_vol_units <= 2.5
    if side == "BUY":
        assert signal.target_price > signal.entry_price > signal.stop_price
    else:
        assert signal.target_price < signal.entry_price < signal.stop_price


@pytest.mark.parametrize(
    ("side", "mutation", "reason"),
    [
        ("BUY", "weak_widening", "prior_quote_widening_too_small"),
        ("BUY", "weak_convergence", "signal_quote_convergence_too_small"),
        ("BUY", "weak_prior_return", "prior_directional_return_too_small"),
        ("BUY", "weak_current_return", "current_directional_return_too_small"),
        ("BUY", "large_prior_tr", "prior_true_range_too_large"),
        ("BUY", "large_signal_tr", "signal_true_range_too_large"),
        ("SELL", "weak_widening", "prior_quote_widening_too_small"),
        ("SELL", "weak_convergence", "signal_quote_convergence_too_small"),
        ("SELL", "weak_prior_return", "prior_directional_return_too_small"),
        ("SELL", "weak_current_return", "current_directional_return_too_small"),
        ("SELL", "large_prior_tr", "prior_true_range_too_large"),
        ("SELL", "large_signal_tr", "signal_true_range_too_large"),
    ],
)
def test_quote_side_convergence_formula_fails_each_strict_component(
    side: str, mutation: str, reason: str
) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    context = qscc.baseline_context_at(qscc.prepare_series(bars), signal_index=300)
    assert context is not None
    direction = 1.0 if side == "BUY" else -1.0
    previous = bars[298]
    prior = bars[299]
    signal = bars[300]
    if mutation == "weak_widening":
        bars[299] = _replace_spread(prior, 0.60)
    elif mutation == "weak_convergence":
        bars[298] = _replace_spread(previous, 0.10)
        bars[299] = _replace_spread(prior, 0.70)
        bars[300] = _replace_spread(signal, 0.49)
    elif mutation == "weak_prior_return":
        close = previous.mid_c * math.exp(
            direction * 0.349 * context.volatility_bps / 1e4
        )
        bars[299] = _bar(
            299,
            mid_o=previous.mid_c,
            mid_h=max(prior.mid_h, close),
            mid_l=min(prior.mid_l, close),
            mid_c=close,
            spread_bps=0.90,
        )
    elif mutation == "weak_current_return":
        close = prior.mid_c * math.exp(direction * 0.099 * context.volatility_bps / 1e4)
        bars[300] = _bar(
            300,
            mid_o=prior.mid_c,
            mid_h=max(signal.mid_h, close),
            mid_l=min(signal.mid_l, close),
            mid_c=close,
            spread_bps=0.45,
        )
    elif mutation == "large_prior_tr":
        bars[299] = _bar(
            299,
            mid_o=prior.mid_o,
            mid_h=max(prior.mid_h, prior.mid_c + 0.30),
            mid_l=min(prior.mid_l, prior.mid_c - 0.30),
            mid_c=prior.mid_c,
            spread_bps=0.90,
        )
    else:
        bars[300] = _bar(
            300,
            mid_o=signal.mid_o,
            mid_h=max(signal.mid_h, signal.mid_c + 0.30),
            mid_l=min(signal.mid_l, signal.mid_c - 0.30),
            mid_c=signal.mid_c,
            spread_bps=0.45,
        )
    candidate, actual_reason = _evaluate(bars, 300, side)
    assert candidate is None
    assert actual_reason == reason


def test_strict_m1_context_signal_fill_and_horizon_continuity() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")

    broken_baseline = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 100 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_baseline, 300, "BUY")
    assert candidate is None
    assert reason == "strict_pre_signal_context_unavailable"

    broken_pair = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 300 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, _ = _evaluate(broken_pair, 300, "BUY")
    assert candidate is None

    broken_fill = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    candidate, reason = _evaluate(broken_fill, 300, "BUY")
    assert candidate is None
    assert reason == "exact_next_open_gap"

    invalid_present_fill = list(bars)
    invalid_present_fill[301] = dataclasses.replace(
        invalid_present_fill[301],
        ask_o=math.nextafter(invalid_present_fill[301].bid_o, -math.inf),
    )
    with pytest.raises(ValueError, match="invalid quote bar at index 301"):
        qscc.prepare_series(invalid_present_fill)

    broken_horizon = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 315 else 0))
        for index, bar in enumerate(bars)
    ]
    signal = _signal(broken_horizon, 300, "BUY")
    assert (
        qscc.outcome_horizon_is_complete(broken_horizon, entry_index=signal.entry_index)
        is False
    )
    assert qscc.simulate_trade(broken_horizon, signal=signal) is None


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exact_t_plus_1_executable_quote_and_all_four_spread_gates(side: str) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    bars[301] = _bar(
        301,
        mid_o=100.25,
        mid_h=100.252,
        mid_l=100.248,
        mid_c=100.25,
        spread_bps=0.20,
    )
    signal = _signal(bars, 300, side)
    expected = bars[301].ask_o if side == "BUY" else bars[301].bid_o
    assert signal.entry_index == 301
    assert signal.entry_price == expected
    assert signal.entry_price != (bars[300].ask_c if side == "BUY" else bars[300].bid_c)

    for index, width, expected_reason in (
        (298, 0.51, "baseline_end_spread_above_q25_or_proxy"),
        (299, 1.01, "prior_spread_above_proxy"),
        (300, 0.51, "signal_spread_above_q25_or_proxy"),
    ):
        too_wide = list(bars)
        too_wide[index] = _replace_spread(too_wide[index], width)
        candidate, reason = _evaluate(too_wide, 300, side)
        assert candidate is None
        assert reason == expected_reason

    too_wide_entry = list(bars)
    too_wide_entry[301] = _replace_spread(too_wide_entry[301], 0.51)
    candidate, reason = _evaluate(too_wide_entry, 300, side)
    assert candidate is None
    assert reason == "entry_spread_above_q25_or_proxy"


def test_each_spread_boundary_accepts_exact_and_rejects_the_next_float_outside(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def evaluate_with_frozen_features(
        bars: list[qscc.QuoteBar],
        *,
        q25: float,
        budget: float,
        complete: bool = False,
    ) -> tuple[qscc.QSCCClosedSignal | qscc.QSCCSignal | None, str]:
        prepared = qscc.prepare_series(bars)
        returns = iter((0.35, 0.10))
        quote_changes = iter((0.50, -0.30))
        with monkeypatch.context() as patcher:
            patcher.setattr(
                qscc,
                "baseline_context_at",
                lambda _prepared, *, signal_index: qscc.BaselineContext(1.0, q25),
            )
            patcher.setattr(qscc, "_true_range_bps", lambda *_: 1.0)
            patcher.setattr(qscc, "_mid_close_return_bps", lambda *_: next(returns))
            patcher.setattr(
                qscc,
                "_quote_log_spread_change_bps",
                lambda *_: next(quote_changes),
            )
            evaluator = (
                qscc.evaluate_signal if complete else qscc.evaluate_closed_signal
            )
            return evaluator(
                prepared=prepared,
                signal_index=300,
                symbol="EURUSD",
                side="BUY",
                config=CONFIG,
                proxy_spread_budget_bps=budget,
            )

    baseline_case = _series(500)
    _plant_signal(baseline_case, 300, side="BUY", target=False)
    baseline_spread = baseline_case[298].spread_close_bps
    admitted, reason = evaluate_with_frozen_features(
        baseline_case, q25=baseline_spread, budget=1.0
    )
    assert admitted is not None, reason
    rejected, reason = evaluate_with_frozen_features(
        baseline_case,
        q25=math.nextafter(baseline_spread, -math.inf),
        budget=1.0,
    )
    assert rejected is None
    assert reason == "baseline_end_spread_above_q25_or_proxy"

    prior_spread = baseline_case[299].spread_close_bps
    admitted, reason = evaluate_with_frozen_features(
        baseline_case, q25=1.0, budget=prior_spread
    )
    assert admitted is not None, reason
    rejected, reason = evaluate_with_frozen_features(
        baseline_case,
        q25=1.0,
        budget=math.nextafter(prior_spread, -math.inf),
    )
    assert rejected is None
    assert reason == "prior_spread_above_proxy"

    cap_case = list(baseline_case)
    cap_case[298] = _replace_spread(cap_case[298], 0.40)
    signal_spread = cap_case[300].spread_close_bps
    admitted, reason = evaluate_with_frozen_features(
        cap_case, q25=signal_spread, budget=1.0
    )
    assert admitted is not None, reason
    rejected, reason = evaluate_with_frozen_features(
        cap_case,
        q25=math.nextafter(signal_spread, -math.inf),
        budget=1.0,
    )
    assert rejected is None
    assert reason == "signal_spread_above_q25_or_proxy"

    entry_case = list(cap_case)
    entry_case[300] = _replace_spread(entry_case[300], 0.40)
    entry_spread = entry_case[301].spread_open_bps
    admitted, reason = evaluate_with_frozen_features(
        entry_case, q25=entry_spread, budget=1.0, complete=True
    )
    assert admitted is not None, reason
    rejected, reason = evaluate_with_frozen_features(
        entry_case,
        q25=math.nextafter(entry_spread, -math.inf),
        budget=1.0,
        complete=True,
    )
    assert rejected is None
    assert reason == "entry_spread_above_q25_or_proxy"


def test_next_float_outside_each_admission_inequality_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert qscc._valid_proxy_budget(qscc.MAX_PROXY_SPREAD_BPS) == (
        qscc.MAX_PROXY_SPREAD_BPS
    )
    assert (
        qscc._valid_proxy_budget(math.nextafter(qscc.MAX_PROXY_SPREAD_BPS, math.inf))
        is None
    )

    def evaluate_closed_with_exact_units(
        prepared: qscc.PreparedSeries,
        *,
        side: str,
        prior_return: float,
        current_return: float,
        prior_quote_change: float,
        signal_quote_change: float,
        prior_true_range: float,
        signal_true_range: float,
    ) -> tuple[qscc.QSCCClosedSignal | None, str]:
        returns = iter((prior_return, current_return))
        quote_changes = iter((prior_quote_change, signal_quote_change))
        true_ranges = iter((prior_true_range, signal_true_range))
        with monkeypatch.context() as patcher:
            patcher.setattr(
                qscc,
                "baseline_context_at",
                lambda _prepared, *, signal_index: qscc.BaselineContext(
                    volatility_bps=1.0,
                    spread_q25_bps=1.0,
                ),
            )
            patcher.setattr(qscc, "_mid_close_return_bps", lambda *_: next(returns))
            patcher.setattr(
                qscc,
                "_quote_log_spread_change_bps",
                lambda *_: next(quote_changes),
            )
            patcher.setattr(
                qscc,
                "_true_range_bps",
                lambda *_: next(true_ranges),
            )
            return qscc.evaluate_closed_signal(
                prepared=prepared,
                signal_index=300,
                symbol="EURUSD",
                side=side,
                config=CONFIG,
                proxy_spread_budget_bps=1.0,
            )

    for side in ("BUY", "SELL"):
        bars = _series(500)
        _plant_signal(bars, 300, side=side, target=False)
        prepared = qscc.prepare_series(bars)
        direction = 1.0 if side == "BUY" else -1.0
        exact = {
            "prior_return": direction * qscc.PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD,
            "current_return": direction * qscc.CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD,
            "prior_quote_change": qscc.QUOTE_WIDENING_RECORDED_COST_FRACTION * 2.0,
            "signal_quote_change": -qscc.QUOTE_CONVERGENCE_PRIOR_CHANGE_FRACTION
            * qscc.QUOTE_WIDENING_RECORDED_COST_FRACTION
            * 2.0,
            "prior_true_range": qscc.MAX_SIGNAL_TRUE_RANGE_VOL,
            "signal_true_range": qscc.MAX_SIGNAL_TRUE_RANGE_VOL,
        }
        closed, reason = evaluate_closed_with_exact_units(
            prepared,
            side=side,
            **exact,
        )
        assert closed is not None, reason
        cases: list[tuple[str, float, str]] = [
            (
                "prior_true_range",
                math.nextafter(qscc.MAX_SIGNAL_TRUE_RANGE_VOL, math.inf),
                "prior_true_range_too_large",
            ),
            (
                "signal_true_range",
                math.nextafter(qscc.MAX_SIGNAL_TRUE_RANGE_VOL, math.inf),
                "signal_true_range_too_large",
            ),
            (
                "prior_quote_change",
                math.nextafter(exact["prior_quote_change"], -math.inf),
                "prior_quote_widening_too_small",
            ),
            (
                "signal_quote_change",
                math.nextafter(exact["signal_quote_change"], math.inf),
                "signal_quote_convergence_too_small",
            ),
            (
                "prior_return",
                math.nextafter(exact["prior_return"], -direction * math.inf),
                "prior_directional_return_too_small",
            ),
            (
                "current_return",
                math.nextafter(exact["current_return"], -direction * math.inf),
                "current_directional_return_too_small",
            ),
        ]
        for field, outside, expected_reason in cases:
            values = dict(exact)
            values[field] = outside
            candidate, reason = evaluate_closed_with_exact_units(
                prepared,
                side=side,
                **values,
            )
            assert candidate is None
            assert reason == expected_reason

    epsilon_lines = [
        line.strip() for line in inspect.getsource(qscc).splitlines() if "1e-12" in line
    ]
    assert epsilon_lines == [
        "and math.isclose(left_value, right_value, rel_tol=1e-12, abs_tol=1e-12)"
    ]


def test_signal_true_range_cap_is_inclusive_and_rejects_above_2_5v() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", target=False)
    context = qscc.baseline_context_at(qscc.prepare_series(bars), signal_index=300)
    assert context is not None
    prior_close = bars[299].mid_c
    close = max((bars[299].mid_h + bars[299].mid_l) / 2.0, prior_close * 1.0005)
    # The raw decimal construction rounds one ULP above 2.5V. The next
    # representable price toward the close is the admitted-side fixture.
    low = math.nextafter(close * (1.0 - 25.0 / 1e4), close)
    high = close
    bars[300] = _bar(
        300,
        mid_o=prior_close,
        mid_h=max(high, close),
        mid_l=low,
        mid_c=close,
    )
    signal = _signal(bars, 300, "BUY")
    assert signal.signal_true_range_vol_units == pytest.approx(2.5, rel=2e-3)

    above = list(bars)
    above[300] = dataclasses.replace(
        above[300],
        bid_l=above[300].bid_l - 0.001,
        ask_l=above[300].ask_l - 0.001,
    )
    candidate, reason = _evaluate(above, 300, "BUY")
    assert candidate is None
    assert reason == "signal_true_range_too_large"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_s_c_d_target_risk_and_break_even_algebra_are_exact(side: str) -> None:
    bars = _series(500, spread_bps=0.80)
    _plant_signal(
        bars,
        300,
        side=side,
        prior_spread_bps=0.99,
        signal_spread_bps=0.70,
        entry_spread_bps=0.80 - 1e-12,
        target=False,
    )
    bars[298] = _replace_spread(bars[298], 0.70)
    signal = _signal(bars, 300, side, budget=1.0)
    assert signal.spread_stress_bps == pytest.approx(1.0)
    assert signal.recorded_cost_bps == pytest.approx(2.0)
    assert signal.execution_cost_debit_bps == pytest.approx(1.20)
    assert signal.gross_target_bps == pytest.approx(8.0)
    assert signal.gross_stop_bps == pytest.approx(16.0)
    normalized_p_star = (
        8.0 + signal.execution_cost_debit_bps / signal.recorded_cost_bps
    ) / 12.0
    standard_p_star = (signal.gross_stop_bps + signal.execution_cost_debit_bps) / (
        signal.gross_stop_bps + signal.gross_target_bps
    )
    assert signal.p_star == normalized_p_star
    assert qscc._numbers_match(normalized_p_star, standard_p_star)
    assert qscc._p_star_forms_agree(
        normalized_p_star=signal.p_star,
        gross_stop_bps=signal.gross_stop_bps,
        execution_cost_debit_bps=signal.execution_cost_debit_bps,
        gross_target_bps=signal.gross_target_bps,
    )
    assert signal.p_star <= qscc.MAX_P_STAR == pytest.approx(3.0 / 4.0)
    if side == "BUY":
        assert signal.target_price == pytest.approx(signal.entry_price * 1.0008)
        assert signal.stop_price == pytest.approx(signal.entry_price * 0.9984)
    else:
        assert signal.target_price == pytest.approx(signal.entry_price * 0.9992)
        assert signal.stop_price == pytest.approx(signal.entry_price * 1.0016)


def test_zero_entry_spread_attains_inclusive_pstar_boundary_and_max_risk_is_32bps() -> (
    None
):
    boundary = _series(500)
    _plant_signal(
        boundary,
        300,
        side="BUY",
        prior_spread_bps=0.90,
        signal_spread_bps=0.45,
        entry_spread_bps=0.0,
        target=False,
    )
    signal = _signal(boundary, 300, "BUY", budget=1.0)
    assert signal.execution_cost_debit_bps == signal.recorded_cost_bps == 2.0
    assert signal.p_star == qscc.MAX_P_STAR == 3.0 / 4.0

    for recorded_cost in (math.nextafter(0.0, math.inf), 1e-300, 2.0, 4.0):
        assert (
            qscc._normalized_p_star(
                recorded_cost_bps=recorded_cost,
                execution_cost_debit_bps=recorded_cost,
            )
            == qscc.MAX_P_STAR
        )
        assert (
            qscc._normalized_p_star(
                recorded_cost_bps=recorded_cost,
                execution_cost_debit_bps=math.nextafter(recorded_cost, 0.0),
            )
            <= qscc.MAX_P_STAR
        )

    maximum = _series(500, spread_bps=2.999)
    _plant_signal(
        maximum,
        300,
        side="BUY",
        prior_spread_bps=2.90,
        signal_spread_bps=1.80,
        entry_spread_bps=2.99,
        target=False,
    )
    maximum[298] = _replace_spread(maximum[298], 1.0)
    max_signal = _signal(maximum, 300, "BUY", budget=3.0)
    assert max_signal.spread_stress_bps <= max_signal.proxy_budget_bps
    assert max_signal.recorded_cost_bps == 4.0
    assert max_signal.gross_stop_bps == qscc.MAX_GROSS_STOP_BPS == 32.0


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_entry_bar_counts_and_stop_first_wins_ambiguity(side: str) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    signal = _signal(bars, 300, side)
    entry = bars[signal.entry_index]
    if side == "BUY":
        high = signal.target_price * 1.001
        low = signal.stop_price * 0.999
        bars[signal.entry_index] = dataclasses.replace(
            entry,
            bid_h=high,
            ask_h=max(entry.ask_h, high + entry.ask_h - entry.bid_h),
            bid_l=low,
        )
    else:
        high = signal.stop_price * 1.001
        low = signal.target_price * 0.999
        bars[signal.entry_index] = dataclasses.replace(
            entry,
            ask_h=high,
            ask_l=low,
            bid_l=min(entry.bid_l, low - (entry.ask_l - entry.bid_l)),
        )
    trade = qscc.simulate_trade(bars, signal=signal)
    assert trade is not None
    assert trade.bars_held == 1
    assert trade.exit_reason == "sl_double_touch"
    assert trade.full_target_win is False


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_adverse_gap_uses_observed_open_and_target_gap_is_capped(side: str) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    signal = _signal(bars, 300, side)
    adverse = list(bars)
    favorable = list(bars)
    gap_index = signal.entry_index + 1
    if side == "BUY":
        adverse[gap_index] = _bar(
            gap_index,
            mid_o=signal.stop_price * 0.999,
            mid_h=signal.stop_price * 0.999 + 0.002,
            mid_l=signal.stop_price * 0.999 - 0.002,
            mid_c=signal.stop_price * 0.999,
        )
        favorable[gap_index] = _bar(
            gap_index,
            mid_o=signal.target_price * 1.001,
            mid_h=signal.target_price * 1.002,
            mid_l=signal.target_price * 1.0005,
            mid_c=signal.target_price * 1.001,
        )
        adverse_open = adverse[gap_index].bid_o
    else:
        adverse[gap_index] = _bar(
            gap_index,
            mid_o=signal.stop_price * 1.001,
            mid_h=signal.stop_price * 1.001 + 0.002,
            mid_l=signal.stop_price * 1.001 - 0.002,
            mid_c=signal.stop_price * 1.001,
        )
        favorable[gap_index] = _bar(
            gap_index,
            mid_o=signal.target_price * 0.999,
            mid_h=signal.target_price * 0.9995,
            mid_l=signal.target_price * 0.998,
            mid_c=signal.target_price * 0.999,
        )
        adverse_open = adverse[gap_index].ask_o
    adverse_trade = qscc.simulate_trade(adverse, signal=signal)
    assert adverse_trade is not None
    assert adverse_trade.exit_reason == "sl_gap_open"
    assert adverse_trade.exit_price == adverse_open
    assert adverse_trade.full_target_win is False
    gap_diagnostics = qscc._adverse_gap_diagnostics([adverse_trade])
    assert gap_diagnostics["adverse_gap_stop_count"] == 1
    assert gap_diagnostics["adverse_gap_total_r"] == pytest.approx(adverse_trade.pnl_r)
    assert gap_diagnostics["adverse_gap_min_r"] == pytest.approx(adverse_trade.pnl_r)

    favorable_trade = qscc.simulate_trade(favorable, signal=signal)
    assert favorable_trade is not None
    assert favorable_trade.exit_reason == "tp_gap_open"
    assert favorable_trade.exit_price == signal.target_price
    assert favorable_trade.full_target_win is True


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exact_exit_quote_target_and_time_stop_only_full_target_wins(side: str) -> None:
    target_bars = _series(500)
    _plant_signal(target_bars, 300, side=side, target=True)
    target_signal = _signal(target_bars, 300, side)
    target_trade = qscc.simulate_trade(target_bars, signal=target_signal)
    assert target_trade is not None
    assert target_trade.exit_reason == "tp"
    assert target_trade.full_target_win is True
    assert target_trade.pnl_bps == pytest.approx(
        target_signal.gross_target_bps - target_signal.execution_cost_debit_bps
    )
    assert target_trade.pnl_r == pytest.approx(
        target_trade.pnl_bps / target_signal.gross_stop_bps
    )

    timeout_bars = _series(500)
    _plant_signal(timeout_bars, 300, side=side, target=False)
    timeout_signal = _signal(timeout_bars, 300, side)
    timeout_trade = qscc.simulate_trade(timeout_bars, signal=timeout_signal)
    assert timeout_trade is not None
    assert timeout_trade.exit_reason == "time_stop"
    assert timeout_trade.bars_held == 30
    assert timeout_trade.exit_epoch == timeout_bars[330].epoch
    expected_exit = (
        timeout_bars[330].bid_c if side == "BUY" else timeout_bars[330].ask_c
    )
    assert timeout_trade.exit_price == expected_exit
    assert timeout_trade.full_target_win is False


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_positive_time_stop_is_not_a_full_target_win(side: str) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side, target=False)
    signal = _signal(bars, 300, side)
    last_index = signal.entry_index + qscc.OUTCOME_HORIZON_M1_BARS - 1
    direction = 1.0 if side == "BUY" else -1.0
    close = signal.entry_price * (1.0 + direction * 4.0 / 1e4)
    open_mid = bars[last_index].mid_o
    bars[last_index] = _bar(
        last_index,
        mid_o=open_mid,
        mid_h=max(open_mid, close) + 0.001,
        mid_l=min(open_mid, close) - 0.001,
        mid_c=close,
        spread_bps=0.20,
    )
    trade = qscc.simulate_trade(bars, signal=signal)
    assert trade is not None
    assert trade.exit_reason == "time_stop"
    assert trade.positive_outcome is True
    assert trade.pnl_bps > 0.0
    assert trade.full_target_win is False


def test_known_entry_spread_rejection_does_not_reserve_or_block_later_signal() -> None:
    bars = _series(1_100)
    _plant_signal(
        bars,
        300,
        side="BUY",
        entry_spread_bps=0.51,
        target=False,
    )
    _plant_signal(bars, 900, side="BUY")
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["reservation_ledger"][0]["signal_index"] == 900
    assert cell["reasons"]["entry_spread_above_q25_or_proxy"] >= 1


def test_completion_dispositions_reserve_skip_or_raise_without_substitution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _series(1_100)
    _plant_signal(bars, 300, side="BUY", target=False)
    _plant_signal(bars, 900, side="BUY")
    prepared = qscc.prepare_series(bars)
    original_completion = qscc._complete_signal_at_exact_next_open

    for disposition in (
        "exact_next_open_unavailable",
        "exact_next_open_gap",
    ):

        def unavailable_completion(
            *,
            prepared: qscc.PreparedSeries,
            closed: qscc.QSCCClosedSignal,
            disposition: str = disposition,
        ) -> tuple[qscc.QSCCSignal | None, str]:
            if closed.signal_index == 300:
                return None, disposition
            return original_completion(prepared=prepared, closed=closed)

        monkeypatch.setattr(
            qscc,
            "_complete_signal_at_exact_next_open",
            unavailable_completion,
        )
        cell = qscc.screen_cell(
            prepared=prepared,
            symbol="EURUSD",
            side="BUY",
            config=CONFIG,
            proxy_spread_budget_bps=1.0,
        )
        assert cell["entry_day_reservations"] == 1
        assert cell["scored_trades"] == 0
        assert cell["reasons"]["entry_day_already_reserved"] >= 1
        reservation = cell["reservation_ledger"][0]
        assert reservation["signal_index"] == 300
        assert reservation["reservation_status"] == "unresolved"
        assert reservation["outcome_reason"] == disposition
        assert reservation["spread_stress_bps"] <= reservation["proxy_budget_bps"]
        assert reservation["gross_stop_bps"] <= qscc.MAX_GROSS_STOP_BPS

    def invalid_present_completion(
        *, prepared: qscc.PreparedSeries, closed: qscc.QSCCClosedSignal
    ) -> tuple[qscc.QSCCSignal | None, str]:
        if closed.signal_index == 300:
            return None, "exact_next_open_invalid"
        return original_completion(prepared=prepared, closed=closed)

    monkeypatch.setattr(
        qscc,
        "_complete_signal_at_exact_next_open",
        invalid_present_completion,
    )
    with pytest.raises(
        RuntimeError,
        match="QSCC completion readiness failure: exact_next_open_invalid",
    ):
        qscc.screen_cell(
            prepared=prepared,
            symbol="EURUSD",
            side="BUY",
            config=CONFIG,
            proxy_spread_budget_bps=1.0,
        )

    entry_spread = prepared.bars[301].spread_open_bps
    one_float_lower_cap = math.nextafter(entry_spread, -math.inf)
    assert math.nextafter(one_float_lower_cap, math.inf) == entry_spread

    def one_ulp_wide_entry(
        *, prepared: qscc.PreparedSeries, closed: qscc.QSCCClosedSignal
    ) -> tuple[qscc.QSCCSignal | None, str]:
        if closed.signal_index == 300:
            closed = dataclasses.replace(
                closed,
                spread_cap_bps=one_float_lower_cap,
            )
        return original_completion(prepared=prepared, closed=closed)

    monkeypatch.setattr(
        qscc,
        "_complete_signal_at_exact_next_open",
        one_ulp_wide_entry,
    )
    skipped = qscc.screen_cell(
        prepared=prepared,
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert skipped["entry_day_reservations"] == 1
    assert skipped["reservation_ledger"][0]["signal_index"] == 900
    assert skipped["reasons"]["entry_spread_above_q25_or_proxy"] >= 1
    admitted = skipped["reservation_ledger"][0]
    assert admitted["spread_stress_bps"] <= admitted["proxy_budget_bps"]
    assert admitted["gross_stop_bps"] <= qscc.MAX_GROSS_STOP_BPS

    completion_calls: list[int] = []
    reservation_calls: list[str] = []

    def impossible_completion(
        *, prepared: qscc.PreparedSeries, closed: qscc.QSCCClosedSignal
    ) -> tuple[qscc.QSCCSignal | None, str]:
        completion_calls.append(closed.signal_index)
        return None, "risk_above_32bps"

    def unexpected_reservation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        reservation_calls.append("called")
        return {}

    monkeypatch.setattr(
        qscc,
        "_complete_signal_at_exact_next_open",
        impossible_completion,
    )
    monkeypatch.setattr(qscc, "_reservation_row", unexpected_reservation)
    monkeypatch.setattr(qscc, "_missing_fill_reservation_row", unexpected_reservation)
    with pytest.raises(
        RuntimeError,
        match="QSCC completion readiness failure: risk_above_32bps",
    ):
        qscc.screen_cell(
            prepared=prepared,
            symbol="EURUSD",
            side="BUY",
            config=CONFIG,
            proxy_spread_budget_bps=1.0,
        )
    assert completion_calls == [300]
    assert reservation_calls == []


def test_missing_fill_reserves_conservative_9_over_8_adverse_and_blocks_day() -> None:
    bars = _series(1_100)
    _plant_signal(bars, 300, side="BUY", target=False)
    _plant_signal(bars, 900, side="BUY")
    gapped = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(gapped),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["scored_trades"] == 0
    assert cell["reasons"]["entry_day_already_reserved"] >= 1
    row = cell["reservation_ledger"][0]
    assert row["reservation_status"] == "unresolved"
    assert row["outcome_reason"] == "exact_next_open_gap"
    assert row["entry_epoch"] == row["signal_epoch"] + 60
    assert row["spread_stress_bps"] == row["proxy_budget_bps"] == 1.0
    assert row["recorded_cost_bps"] == row["execution_cost_debit_bps"] == 2.0
    assert row["gross_target_bps"] == 8.0
    assert row["gross_stop_bps"] == 16.0
    assert row["p_star"] == qscc.MAX_P_STAR == 3.0 / 4.0
    assert row["gate_pnl_r"] == pytest.approx(-9.0 / 8.0)
    assert row["full_target_win"] is False
    assert cell["maximum_drawdown_r"] == pytest.approx(9.0 / 8.0)


def test_incomplete_horizon_reserves_using_actual_cost_and_blocks_substitution() -> (
    None
):
    bars = _series(500, spread_bps=0.80)
    _plant_signal(
        bars,
        300,
        side="BUY",
        entry_spread_bps=0.80 - 1e-12,
        target=False,
    )
    bars[298] = _replace_spread(bars[298], 0.50)
    bars = bars[:320]
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["scored_trades"] == 0
    row = cell["reservation_ledger"][0]
    assert row["outcome_reason"] == "incomplete_outcome_horizon"
    assert row["execution_cost_debit_bps"] == pytest.approx(1.20)
    assert row["gate_pnl_r"] == pytest.approx(
        -(row["gross_stop_bps"] + row["execution_cost_debit_bps"])
        / row["gross_stop_bps"]
    )
    assert row["full_target_win"] is False


def test_first_admitted_signal_per_config_pair_side_utc_entry_day_is_reserved() -> None:
    bars = _series(1_100)
    _plant_signal(bars, 300, side="BUY")
    _plant_signal(bars, 900, side="BUY")
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    assert cell["entry_day_reservations"] == 1
    assert cell["reservation_ledger"][0]["signal_index"] == 300
    assert cell["reasons"]["entry_day_already_reserved"] >= 1


def test_wilson_and_cumulative_student_t_thresholds_are_frozen() -> None:
    assert qscc.SIMULTANEOUS_WILSON_FAMILY_CELLS == 36
    assert qscc.TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD == (
        4.6258224137931165
    )
    assert qscc._one_sided_wilson_lower_bound(80, 80) < 0.90
    assert qscc._one_sided_wilson_lower_bound(81, 81) >= 0.90
    assert qscc._one_sided_wilson_lower_bound(98, 100) < 0.90
    assert qscc._one_sided_wilson_lower_bound(99, 100) >= 0.90
    assert qscc._finite_one_sample_t([]) == 0.0
    assert qscc._finite_one_sample_t([1.0] * 100) == 1e12
    assert qscc._finite_one_sample_t([-1.0] * 100) == -1e12


def test_observed_rate_and_wilson_gate_thresholds_have_no_epsilon_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reservations: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for index in range(100):
        event_id = f"event-{index}"
        common = {
            "event_id": event_id,
            "config_id": "qscc_v1",
            "symbol": "EURUSD",
            "side": "BUY",
            "proxy_budget_bps": 1.0,
        }
        reservations.append(
            {
                **common,
                "entry_day": f"synthetic-day-{index}",
                "reservation_status": "scored",
                "full_target_win": index < 90,
                "gate_pnl_r": 1.0,
            }
        )
        trades.append(dict(common))

    profit_factor = qscc._profit_factor_diagnostics(reservations)
    base_cell = {
        "config_id": "qscc_v1",
        "symbol": "EURUSD",
        "side": "BUY",
        "proxy_budget_bps": 1.0,
        "source_ready": True,
        "proxy_contract_ready": True,
        "proxy_budget_ready": True,
        "scored_trades": 100,
        "unresolved_reservations": 0,
        "gate_total_r": 100.0,
        "gate_mean_r": 1.0,
        "maximum_drawdown_r": 0.0,
        **profit_factor,
    }
    monkeypatch.setattr(qscc, "validate_ledger_consistency", lambda **_: True)
    monkeypatch.setattr(qscc, "validate_reservation_row", lambda _: True)
    monkeypatch.setattr(qscc, "validate_trade_row", lambda _: True)
    monkeypatch.setattr(qscc, "_row_is_within_evaluation_window", lambda *_, **__: True)
    monkeypatch.setattr(qscc, "_cell_contract_is_valid", lambda *_, **__: True)
    monkeypatch.setattr(
        qscc,
        "_mapping_matches_exact_generated_row",
        lambda *_, **__: True,
    )
    monkeypatch.setattr(
        qscc,
        "screen_cell",
        lambda **_: {
            "reservation_ledger": reservations,
            "trade_ledger": trades,
        },
    )
    monkeypatch.setattr(
        qscc,
        "_temporal_thirds_diagnostics",
        lambda *_, **__: {"gate_temporal_thirds_stable": True},
    )
    monkeypatch.setattr(
        qscc,
        "_calendar_month_diagnostics",
        lambda *_, **__: {"gate_calendar_months_stable": True},
    )

    def passes_gate(*, rate_threshold: float, wilson_value: float) -> bool:
        monkeypatch.setattr(qscc, "MIN_OBSERVED_FULL_TARGET_RATE", rate_threshold)
        monkeypatch.setattr(
            qscc,
            "_one_sided_wilson_lower_bound",
            lambda *_: wilson_value,
        )
        cell = dict(base_cell)
        qscc._apply_discovery_gate(
            cells=[cell],
            reservation_ledger=reservations,
            trade_ledger=trades,
            evaluation_start_epoch=qscc.CALENDAR_MONTH_BOUNDARY_EPOCHS[0],
            evaluation_end_epoch=qscc.CALENDAR_MONTH_BOUNDARY_EPOCHS[-1],
            expected_proxy_spread_budgets_bps={"EURUSD": 1.0},
            prepared_series_by_symbol={"EURUSD": qscc.prepare_series([])},
        )
        return bool(cell["passes_discovery_cell_gate"])

    exact = qscc.MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
    assert passes_gate(rate_threshold=0.90, wilson_value=exact)
    assert not passes_gate(
        rate_threshold=math.nextafter(0.90, math.inf),
        wilson_value=exact,
    )
    assert not passes_gate(
        rate_threshold=0.90,
        wilson_value=math.nextafter(exact, -math.inf),
    )


def test_profit_factor_uses_every_gate_return_and_has_strict_zero_loss_policy() -> None:
    passing = qscc._profit_factor_diagnostics(
        [
            {"gate_pnl_r": 2.0, "reservation_status": "scored"},
            {
                "gate_pnl_r": -1.0,
                "reservation_status": "unresolved",
            },
        ]
    )
    assert passing == {
        "gross_profit_r": 2.0,
        "gross_loss_r": 1.0,
        "profit_factor": 2.0,
        "profit_factor_no_loss": False,
        "gate_profit_factor": True,
    }

    failing = qscc._profit_factor_diagnostics(
        [{"gate_pnl_r": 1.0}, {"gate_pnl_r": -1.0}]
    )
    assert failing["profit_factor"] == 1.0
    assert failing["gate_profit_factor"] is False

    exact_boundary = qscc._profit_factor_diagnostics(
        [
            {"gate_pnl_r": qscc.MIN_PROFIT_FACTOR},
            {"gate_pnl_r": -1.0},
        ]
    )
    assert exact_boundary["profit_factor"] == qscc.MIN_PROFIT_FACTOR
    assert exact_boundary["gate_profit_factor"] is True
    one_float_below = math.nextafter(qscc.MIN_PROFIT_FACTOR, -math.inf)
    strict_boundary = qscc._profit_factor_diagnostics(
        [{"gate_pnl_r": one_float_below}, {"gate_pnl_r": -1.0}]
    )
    assert strict_boundary["profit_factor"] == one_float_below
    assert strict_boundary["gate_profit_factor"] is False

    no_loss = qscc._profit_factor_diagnostics(
        [{"gate_pnl_r": 1.0}, {"gate_pnl_r": 2.0}]
    )
    assert no_loss["gross_loss_r"] == 0.0
    assert no_loss["profit_factor"] is None
    assert no_loss["profit_factor_no_loss"] is True
    assert no_loss["gate_profit_factor"] is True

    all_zero = qscc._profit_factor_diagnostics(
        [{"gate_pnl_r": 0.0}, {"gate_pnl_r": 0.0}]
    )
    assert all_zero["profit_factor"] == 0.0
    assert all_zero["profit_factor_no_loss"] is False
    assert all_zero["gate_profit_factor"] is False


def test_tail_risk_diagnostics_use_ordered_gate_returns_and_adverse_gap_trades() -> (
    None
):
    assert qscc._maximum_drawdown_r([1.0, -0.5, -1.0, 2.0, -3.0]) == pytest.approx(3.0)
    assert qscc._maximum_drawdown_r([]) == 0.0
    with pytest.raises(ValueError):
        qscc._maximum_drawdown_r([1.0, math.nan])
    assert qscc._adverse_gap_diagnostics([]) == {
        "adverse_gap_stop_count": 0,
        "adverse_gap_total_r": 0.0,
        "adverse_gap_min_r": None,
    }


def test_temporal_thirds_use_exact_half_open_boundaries_and_strict_vetoes() -> None:
    start = qscc.CALENDAR_MONTH_BOUNDARY_EPOCHS[0]
    end = qscc.CALENDAR_MONTH_BOUNDARY_EPOCHS[-1]
    boundaries = qscc._temporal_thirds_boundary_epochs(
        evaluation_start_epoch=start,
        evaluation_end_epoch=end,
    )
    assert list(boundaries) == [
        1_672_531_200,
        1_677_744_000,
        1_682_956_800,
        1_688_169_600,
    ]
    assert [
        qscc._temporal_third_index(
            epoch,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )
        for epoch in boundaries
    ] == [0, 1, 2, 2]

    rows: list[dict[str, Any]] = []
    for segment in range(3):
        for index in range(30):
            win = index < 27
            rows.append(
                {
                    "entry_epoch": boundaries[segment] + index * 60,
                    "full_target_win": win,
                    "gate_pnl_r": 1.0 if win else -0.25,
                    "reservation_status": "scored" if win else "unresolved",
                }
            )
    diagnostics = qscc._temporal_thirds_diagnostics(
        rows,
        evaluation_start_epoch=start,
        evaluation_end_epoch=end,
    )
    assert diagnostics["temporal_thirds_reservation_counts"] == [30, 30, 30]
    assert diagnostics["temporal_thirds_full_target_rates"] == [0.9, 0.9, 0.9]
    assert diagnostics["gate_temporal_thirds_stable"] is True

    sparse = rows[:-1]
    assert (
        qscc._temporal_thirds_diagnostics(
            sparse,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_temporal_thirds_stable"]
        is False
    )
    low_rate = copy.deepcopy(rows)
    low_rate[0]["full_target_win"] = False
    assert (
        qscc._temporal_thirds_diagnostics(
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
        qscc._temporal_thirds_diagnostics(
            nonpositive,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_temporal_thirds_stable"]
        is False
    )


def test_calendar_months_use_exact_boundaries_include_unresolved_and_veto() -> None:
    boundaries = qscc.CALENDAR_MONTH_BOUNDARY_EPOCHS
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
        qscc._calendar_month_index(
            epoch,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )
        for epoch in boundaries
    ] == [0, 1, 2, 3, 4, 5, 5]

    rows: list[dict[str, Any]] = []
    for month in range(6):
        for day in range(12):
            win = day < 11
            rows.append(
                {
                    "entry_epoch": boundaries[month] + day * 86_400,
                    "full_target_win": win,
                    "gate_pnl_r": 1.0 if win else -0.25,
                    "reservation_status": "scored" if win else "unresolved",
                }
            )
    diagnostics = qscc._calendar_month_diagnostics(
        rows,
        evaluation_start_epoch=start,
        evaluation_end_epoch=end,
    )
    assert diagnostics["calendar_month_reservation_counts"] == [12] * 6
    assert diagnostics["calendar_month_full_target_wins"] == [11] * 6
    assert diagnostics["gate_calendar_months_stable"] is True

    low_rate = copy.deepcopy(rows)
    low_rate[0]["full_target_win"] = False
    assert (
        qscc._calendar_month_diagnostics(
            low_rate,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_calendar_months_stable"]
        is False
    )
    nonpositive = copy.deepcopy(rows)
    for row in nonpositive[:12]:
        row["gate_pnl_r"] = 0.0
    assert (
        qscc._calendar_month_diagnostics(
            nonpositive,
            evaluation_start_epoch=start,
            evaluation_end_epoch=end,
        )["gate_calendar_months_stable"]
        is False
    )


def test_global_gate_requires_the_single_unchanged_config_for_all_36_cells() -> None:
    complete = [
        {
            "config_id": "qscc_v1",
            "symbol": symbol,
            "side": side,
            "passes_discovery_cell_gate": True,
        }
        for symbol in qscc.FX_SYMBOLS
        for side in ("BUY", "SELL")
    ]
    assert qscc._passing_global_configurations(complete) == ["qscc_v1"]
    assert qscc._passing_global_configurations(complete[:-1]) == []
    duplicate = copy.deepcopy(complete)
    duplicate[-1] = dict(duplicate[-2])
    assert qscc._passing_global_configurations(duplicate) == []
    unknown = copy.deepcopy(complete)
    unknown[-1]["config_id"] = "forged"
    assert qscc._passing_global_configurations(unknown) == []


def test_exact_row_schemas_and_semantic_validators_reject_forged_algebra() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    trade = cell["trade_ledger"][0]
    assert set(reservation) == qscc.QSCC_RESERVATION_FIELD_NAMES
    assert set(trade) == qscc.QSCC_TRADE_FIELD_NAMES
    assert qscc.validate_reservation_row(reservation)
    assert qscc.validate_trade_row(trade)

    for field, value in (
        ("event_id", "forged"),
        ("symbol", "eurusd"),
        ("side", "buy"),
        ("spread_stress_bps", 0.0),
        ("recorded_cost_bps", 999.0),
        ("execution_cost_debit_bps", 0.0),
        ("gross_target_bps", 999.0),
        ("gross_stop_bps", 1.0),
        ("p_star", 0.0),
        ("pnl_bps", 999.0),
        ("gate_pnl_r", 999.0),
        ("full_target_win", False),
    ):
        forged = dict(reservation)
        forged[field] = value
        assert not qscc.validate_reservation_row(forged), field

    for field, value in (
        ("event_id", "forged"),
        ("symbol", "eurusd"),
        ("side", "buy"),
        ("exit_reason", "time_stop"),
        ("pnl_bps", 999.0),
        ("pnl_r", 999.0),
        ("bars_held", 31),
        ("full_target_win", False),
    ):
        forged = dict(trade)
        forged[field] = value
        assert not qscc.validate_trade_row(forged), field

    extra = dict(reservation)
    extra["unexpected"] = True
    assert not qscc.validate_reservation_row(extra)
    wrong_type = dict(trade)
    wrong_type["bars_held"] = True
    assert not qscc.validate_trade_row(wrong_type)

    impossible_time_stop = dict(trade)
    impossible_time_stop["exit_reason"] = "time_stop"
    impossible_time_stop["full_target_win"] = False
    assert not qscc.validate_trade_row(impossible_time_stop)

    improved_target = dict(trade)
    direction = 1.0 if trade["side"] == "BUY" else -1.0
    improved_target["exit_price"] = (
        trade["entry_price"]
        * (1.0 + direction * trade["gross_target_bps"] / 1e4)
        * (1.0 + direction * 0.001)
    )
    gross = (
        (improved_target["exit_price"] - improved_target["entry_price"])
        / improved_target["entry_price"]
        * 1e4
        if trade["side"] == "BUY"
        else (improved_target["entry_price"] - improved_target["exit_price"])
        / improved_target["entry_price"]
        * 1e4
    )
    improved_target["pnl_bps"] = gross - improved_target["execution_cost_debit_bps"]
    improved_target["pnl_r"] = (
        improved_target["pnl_bps"] / improved_target["gross_stop_bps"]
    )
    improved_target["positive_outcome"] = True
    improved_target["full_target_win"] = True
    assert not qscc.validate_trade_row(improved_target)


def test_semantic_validators_reject_one_ulp_outside_every_numeric_hard_cap() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", target=False)
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    row = cell["reservation_ledger"][0]
    assert qscc.validate_reservation_row(row)

    hard_cap_mutations = (
        {"proxy_budget_bps": math.nextafter(qscc.MAX_PROXY_SPREAD_BPS, math.inf)},
        {"baseline_end_spread_bps": math.nextafter(row["spread_cap_bps"], math.inf)},
        {"prior_spread_bps": math.nextafter(row["proxy_budget_bps"], math.inf)},
        {"signal_spread_bps": math.nextafter(row["spread_cap_bps"], math.inf)},
        {"entry_spread_bps": math.nextafter(row["spread_cap_bps"], math.inf)},
        {
            "prior_true_range_vol_units": math.nextafter(
                qscc.MAX_SIGNAL_TRUE_RANGE_VOL, math.inf
            ),
            "prior_true_range_bps": math.nextafter(
                qscc.MAX_SIGNAL_TRUE_RANGE_VOL, math.inf
            )
            * row["volatility_bps"],
        },
        {
            "signal_true_range_vol_units": math.nextafter(
                qscc.MAX_SIGNAL_TRUE_RANGE_VOL, math.inf
            ),
            "signal_true_range_bps": math.nextafter(
                qscc.MAX_SIGNAL_TRUE_RANGE_VOL, math.inf
            )
            * row["volatility_bps"],
        },
        {
            "prior_return_vol_units": math.nextafter(
                qscc.PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD, -math.inf
            ),
            "prior_return_bps": math.nextafter(
                qscc.PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD, -math.inf
            )
            * row["volatility_bps"],
        },
        {
            "current_return_vol_units": math.nextafter(
                qscc.CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD, -math.inf
            ),
            "current_return_bps": math.nextafter(
                qscc.CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD, -math.inf
            )
            * row["volatility_bps"],
        },
        {
            "prior_quote_change_bps": math.nextafter(
                qscc.QUOTE_WIDENING_RECORDED_COST_FRACTION
                * (row["proxy_budget_bps"] + 1.0),
                -math.inf,
            ),
            "quote_convergence_ratio": row["signal_quote_change_bps"]
            / math.nextafter(
                qscc.QUOTE_WIDENING_RECORDED_COST_FRACTION
                * (row["proxy_budget_bps"] + 1.0),
                -math.inf,
            ),
        },
        {
            "signal_quote_change_bps": math.nextafter(
                -qscc.QUOTE_CONVERGENCE_PRIOR_CHANGE_FRACTION
                * row["prior_quote_change_bps"],
                math.inf,
            ),
            "quote_convergence_ratio": math.nextafter(
                -qscc.QUOTE_CONVERGENCE_PRIOR_CHANGE_FRACTION
                * row["prior_quote_change_bps"],
                math.inf,
            )
            / row["prior_quote_change_bps"],
        },
    )
    for mutation in hard_cap_mutations:
        forged = dict(row)
        forged.update(mutation)
        assert not qscc.validate_reservation_row(forged), mutation

    maximum = _series(500, spread_bps=2.999)
    _plant_signal(
        maximum,
        300,
        side="BUY",
        prior_spread_bps=2.90,
        signal_spread_bps=1.80,
        entry_spread_bps=2.99,
        target=False,
    )
    maximum[298] = _replace_spread(maximum[298], 1.0)
    maximum_cell = qscc.screen_cell(
        prepared=qscc.prepare_series(maximum),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=3.0,
    )
    maximum_row = maximum_cell["reservation_ledger"][0]
    assert maximum_row["spread_stress_bps"] <= maximum_row["proxy_budget_bps"]
    assert maximum_row["gross_stop_bps"] == qscc.MAX_GROSS_STOP_BPS
    assert qscc.validate_reservation_row(maximum_row)
    excessive_risk = dict(maximum_row)
    excessive_risk["gross_stop_bps"] = math.nextafter(qscc.MAX_GROSS_STOP_BPS, math.inf)
    assert qscc._numbers_match(
        excessive_risk["gross_stop_bps"], qscc.MAX_GROSS_STOP_BPS
    )
    assert not qscc.validate_reservation_row(excessive_risk)

    zero_spread = _series(500)
    _plant_signal(
        zero_spread,
        300,
        side="BUY",
        prior_spread_bps=0.90,
        signal_spread_bps=0.45,
        entry_spread_bps=0.0,
        target=False,
    )
    boundary_cell = qscc.screen_cell(
        prepared=qscc.prepare_series(zero_spread),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    boundary_row = boundary_cell["reservation_ledger"][0]
    assert boundary_row["execution_cost_debit_bps"] == boundary_row["recorded_cost_bps"]
    assert boundary_row["p_star"] == qscc.MAX_P_STAR
    assert qscc.validate_reservation_row(boundary_row)
    excessive_p_star = dict(boundary_row)
    excessive_p_star["p_star"] = math.nextafter(qscc.MAX_P_STAR, math.inf)
    assert qscc._numbers_match(excessive_p_star["p_star"], qscc.MAX_P_STAR)
    assert not qscc.validate_reservation_row(excessive_p_star)


def test_semantic_gates_use_raw_values_not_tolerant_derived_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", target=False)
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    row = cell["reservation_ledger"][0]
    assert qscc.validate_reservation_row(row)

    volatility = row["volatility_bps"]
    raw_gate_cases = (
        (
            "prior_true_range_bps",
            math.nextafter(qscc.MAX_SIGNAL_TRUE_RANGE_VOL * volatility, math.inf),
            "prior_true_range_vol_units",
            qscc.MAX_SIGNAL_TRUE_RANGE_VOL,
        ),
        (
            "signal_true_range_bps",
            math.nextafter(qscc.MAX_SIGNAL_TRUE_RANGE_VOL * volatility, math.inf),
            "signal_true_range_vol_units",
            qscc.MAX_SIGNAL_TRUE_RANGE_VOL,
        ),
        (
            "prior_return_bps",
            math.nextafter(
                qscc.PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD * volatility,
                -math.inf,
            ),
            "prior_return_vol_units",
            qscc.PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD,
        ),
        (
            "current_return_bps",
            math.nextafter(
                qscc.CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD * volatility,
                -math.inf,
            ),
            "current_return_vol_units",
            qscc.CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD,
        ),
    )
    for raw_name, raw_value, diagnostic_name, diagnostic_value in raw_gate_cases:
        forged = dict(row)
        forged[raw_name] = raw_value
        forged[diagnostic_name] = diagnostic_value
        assert qscc._numbers_match(diagnostic_value, raw_value / volatility)
        assert not qscc.validate_reservation_row(forged), raw_name

    true_cap = min(row["spread_q25_bps"], row["proxy_budget_bps"])
    forged_cap = math.nextafter(true_cap, math.inf)
    forged_spread = dict(row)
    forged_spread["spread_cap_bps"] = forged_cap
    forged_spread["baseline_end_spread_bps"] = forged_cap
    assert qscc._numbers_match(forged_cap, true_cap)
    assert not qscc.validate_reservation_row(forged_spread)

    assert qscc._completed_signal_semantics_are_valid(row, missing_fill=False)
    expected_stop = row["gross_stop_bps"]
    lowered_stop_cap = math.nextafter(expected_stop, -math.inf)
    forged_stop = dict(row)
    forged_stop["gross_stop_bps"] = lowered_stop_cap
    forged_stop["stop_price"] = row["entry_price"] * (1.0 - lowered_stop_cap / 1e4)
    assert qscc._numbers_match(lowered_stop_cap, expected_stop)
    with monkeypatch.context() as patcher:
        patcher.setattr(qscc, "MAX_GROSS_STOP_BPS", lowered_stop_cap)
        assert not qscc._completed_signal_semantics_are_valid(
            forged_stop, missing_fill=False
        )

    expected_p_star = row["p_star"]
    lowered_p_star_cap = math.nextafter(expected_p_star, -math.inf)
    forged_p_star = dict(row)
    forged_p_star["p_star"] = lowered_p_star_cap
    assert qscc._numbers_match(lowered_p_star_cap, expected_p_star)
    with monkeypatch.context() as patcher:
        patcher.setattr(qscc, "MAX_P_STAR", lowered_p_star_cap)
        assert not qscc._completed_signal_semantics_are_valid(
            forged_p_star, missing_fill=False
        )


def _recompute_trade_economics(row: dict[str, object]) -> None:
    entry = float(row["entry_price"])
    exit_price = float(row["exit_price"])
    debit = float(row["execution_cost_debit_bps"])
    stop = float(row["gross_stop_bps"])
    gross = (
        (exit_price - entry) / entry * 1e4
        if row["side"] == "BUY"
        else (entry - exit_price) / entry * 1e4
    )
    pnl_bps = gross - debit
    row["pnl_bps"] = pnl_bps
    row["pnl_r"] = pnl_bps / stop
    reason = row.get("exit_reason", row.get("outcome_reason"))
    row["full_target_win"] = reason in {"tp", "tp_gap_open"} and pnl_bps > 0.0
    row["positive_outcome"] = pnl_bps > 0.0
    if "gate_pnl_r" in row:
        row["gate_pnl_r"] = row["pnl_r"]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_trade_and_scored_reservation_reject_one_ulp_fixed_bracket_drift(
    side: str,
) -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side=side)
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side=side,
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    trade = cell["trade_ledger"][0]
    assert trade["exit_reason"] == "tp"
    assert qscc.validate_trade_row(trade)
    assert qscc.validate_reservation_row(reservation)

    for toward in (-math.inf, math.inf):
        forged_exit = dict(trade)
        forged_exit["exit_price"] = math.nextafter(trade["target_price"], toward)
        _recompute_trade_economics(forged_exit)
        assert not qscc.validate_trade_row(forged_exit)

        forged_target = dict(trade)
        forged_target["target_price"] = math.nextafter(trade["target_price"], toward)
        forged_target["exit_price"] = forged_target["target_price"]
        _recompute_trade_economics(forged_target)
        assert not qscc.validate_trade_row(forged_target)

        forged_reservation = dict(reservation)
        forged_reservation["target_price"] = math.nextafter(
            reservation["target_price"], toward
        )
        forged_reservation["exit_price"] = forged_reservation["target_price"]
        _recompute_trade_economics(forged_reservation)
        assert not qscc.validate_reservation_row(forged_reservation)

    nominal_stop = dict(trade)
    nominal_stop["exit_reason"] = "sl"
    nominal_stop["exit_price"] = nominal_stop["stop_price"]
    _recompute_trade_economics(nominal_stop)
    assert qscc.validate_trade_row(nominal_stop)
    for toward in (-math.inf, math.inf):
        forged_exit = dict(nominal_stop)
        forged_exit["exit_price"] = math.nextafter(nominal_stop["stop_price"], toward)
        _recompute_trade_economics(forged_exit)
        assert not qscc.validate_trade_row(forged_exit)

        forged_stop = dict(nominal_stop)
        forged_stop["stop_price"] = math.nextafter(nominal_stop["stop_price"], toward)
        forged_stop["exit_price"] = forged_stop["stop_price"]
        _recompute_trade_economics(forged_stop)
        assert not qscc.validate_trade_row(forged_stop)


def test_reservation_semantics_reject_optimistic_one_ulp_economic_drift() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    trade = cell["trade_ledger"][0]
    assert qscc.validate_reservation_row(reservation)
    assert qscc.validate_trade_row(trade)

    forged_trade = dict(trade)
    forged_trade["pnl_bps"] = math.nextafter(trade["pnl_bps"], math.inf)
    forged_trade["pnl_r"] = forged_trade["pnl_bps"] / forged_trade["gross_stop_bps"]
    assert not qscc.validate_trade_row(forged_trade)

    forged_gate = dict(reservation)
    forged_gate["gate_pnl_r"] = math.nextafter(reservation["gate_pnl_r"], math.inf)
    assert not qscc.validate_reservation_row(forged_gate)

    forged_debit = dict(reservation)
    forged_debit["execution_cost_debit_bps"] = math.nextafter(
        reservation["execution_cost_debit_bps"], -math.inf
    )
    forged_debit["p_star"] = (
        8.0
        + forged_debit["execution_cost_debit_bps"] / forged_debit["recorded_cost_bps"]
    ) / 12.0
    _recompute_trade_economics(forged_debit)
    assert not qscc.validate_reservation_row(forged_debit)

    true_cap = min(reservation["spread_q25_bps"], reservation["proxy_budget_bps"])
    forged_cap = dict(reservation)
    forged_cap["spread_cap_bps"] = math.nextafter(true_cap, math.inf)
    forged_cap["entry_spread_bps"] = forged_cap["spread_cap_bps"]
    forged_cap["execution_cost_debit_bps"] = (
        max(
            0.0,
            forged_cap["spread_stress_bps"] - forged_cap["entry_spread_bps"],
        )
        + qscc.FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    )
    forged_cap["p_star"] = (
        8.0 + forged_cap["execution_cost_debit_bps"] / forged_cap["recorded_cost_bps"]
    ) / 12.0
    _recompute_trade_economics(forged_cap)
    assert not qscc.validate_reservation_row(forged_cap)

    gapped = _series(500)
    _plant_signal(gapped, 300, side="BUY", target=False)
    gapped = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(gapped)
    ]
    missing = qscc.screen_cell(
        prepared=qscc.prepare_series(gapped),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )["reservation_ledger"][0]
    assert missing["outcome_reason"] == "exact_next_open_gap"
    assert qscc.validate_reservation_row(missing)
    forged_missing_gate = dict(missing)
    forged_missing_gate["gate_pnl_r"] = math.nextafter(missing["gate_pnl_r"], math.inf)
    assert not qscc.validate_reservation_row(forged_missing_gate)
    forged_missing_risk = dict(missing)
    forged_missing_risk["gate_risk_basis_bps"] = math.nextafter(
        missing["gate_risk_basis_bps"], -math.inf
    )
    assert not qscc.validate_reservation_row(forged_missing_risk)

    incomplete_bars = _series(500, spread_bps=0.80)
    _plant_signal(
        incomplete_bars,
        300,
        side="BUY",
        entry_spread_bps=0.80 - 1e-12,
        target=False,
    )
    incomplete_bars[298] = _replace_spread(incomplete_bars[298], 0.50)
    incomplete = qscc.screen_cell(
        prepared=qscc.prepare_series(incomplete_bars[:320]),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )["reservation_ledger"][0]
    assert incomplete["outcome_reason"] == "incomplete_outcome_horizon"
    assert qscc.validate_reservation_row(incomplete)
    forged_incomplete = dict(incomplete)
    forged_incomplete["gate_pnl_r"] = math.nextafter(incomplete["gate_pnl_r"], math.inf)
    assert not qscc.validate_reservation_row(forged_incomplete)


def test_missing_fill_schema_cannot_be_forged_into_a_win_or_observed_fill() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY", target=False)
    bars = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 301 else 0))
        for index, bar in enumerate(bars)
    ]
    cell = qscc.screen_cell(
        prepared=qscc.prepare_series(bars),
        symbol="EURUSD",
        side="BUY",
        config=CONFIG,
        proxy_spread_budget_bps=1.0,
    )
    reservation = cell["reservation_ledger"][0]
    assert set(reservation) == qscc.QSCC_MISSING_FILL_RESERVATION_FIELD_NAMES
    assert reservation["execution_cost_debit_bps"] == reservation["recorded_cost_bps"]
    assert reservation["p_star"] == qscc.MAX_P_STAR
    assert qscc.validate_reservation_row(reservation)

    forged_invalid_present_fill = dict(reservation)
    forged_invalid_present_fill["outcome_reason"] = "exact_next_open_invalid"
    assert not qscc.validate_reservation_row(forged_invalid_present_fill)

    excessive_p_star = dict(reservation)
    excessive_p_star["p_star"] = math.nextafter(qscc.MAX_P_STAR, math.inf)
    assert qscc._numbers_match(excessive_p_star["p_star"], qscc.MAX_P_STAR)
    assert not qscc.validate_reservation_row(excessive_p_star)

    for field, value in (
        ("entry_price", 100.0),
        ("entry_spread_bps", 0.0),
        ("stop_price", 99.0),
        ("target_price", 101.0),
        ("full_target_win", True),
        ("positive_outcome", True),
        ("gate_pnl_r", 1.0),
        ("gate_risk_basis_bps", 999.0),
    ):
        forged = dict(reservation)
        forged[field] = value
        assert not qscc.validate_reservation_row(forged), field
    omitted = dict(reservation)
    omitted.pop("gate_risk_basis_bps")
    assert not qscc.validate_reservation_row(omitted)


def test_strict_payload_and_bundle_validation_rejects_duplicates_omissions_and_authority() -> (
    None
):
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")
    result = _screen({"EURUSD": bars})
    assert qscc.validate_cells_payload(result["cells"])
    assert qscc.validate_reservation_ledger_payload(result["reservation_ledger"])
    assert qscc.validate_trade_ledger_payload(result["trade_ledger"])
    assert qscc.validate_ledger_consistency(
        cells=result["cells"],
        reservation_ledger=result["reservation_ledger"],
        trade_ledger=result["trade_ledger"],
    )
    assert qscc.validate_result_bundle(result)

    duplicated_cells = copy.deepcopy(result["cells"])
    duplicated_cells[-1] = copy.deepcopy(duplicated_cells[-2])
    assert not qscc.validate_cells_payload(duplicated_cells)

    duplicate_reservations = copy.deepcopy(result["reservation_ledger"])
    duplicate_reservations.append(copy.deepcopy(duplicate_reservations[0]))
    assert not qscc.validate_reservation_ledger_payload(duplicate_reservations)

    omitted_trade = copy.deepcopy(result)
    omitted_trade["trade_ledger"] = omitted_trade["trade_ledger"][:-1]
    assert not qscc.validate_result_bundle(omitted_trade)

    forged_cell = copy.deepcopy(result)
    active = next(
        cell for cell in forged_cell["cells"] if cell["entry_day_reservations"]
    )
    active["full_target_wins"] += 1
    assert not qscc.validate_result_bundle(forged_cell)

    forged_tail = copy.deepcopy(result)
    active_tail = next(
        cell for cell in forged_tail["cells"] if cell["entry_day_reservations"]
    )
    active_tail["maximum_drawdown_r"] = 999.0
    active_tail["adverse_gap_stop_count"] = 999
    assert not qscc.validate_result_bundle(forged_tail)

    for authority_field in (
        "success_claim_authorized",
        "holdout_access_authorized",
        "promotion_authorized",
        "activation_authorized",
        "registry_write_authorized",
        "order_authorized",
        "economic_passed",
        "economic_claim_ready",
        "economics_claim_ready",
    ):
        forged = copy.deepcopy(result)
        forged[authority_field] = True
        assert not qscc.validate_result_bundle(forged), authority_field


def test_complete_source_replay_rejects_even_an_internally_recounted_omission() -> None:
    bars = _series(500)
    _plant_signal(bars, 300, side="BUY")
    result = _screen({"EURUSD": bars})
    assert qscc.validate_source_replay(
        result=result,
        bars_by_symbol={"EURUSD": bars},
        proxy_spread_budgets_bps=_budgets(),
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
    )

    forged = copy.deepcopy(result)
    active = next(cell for cell in forged["cells"] if cell["entry_day_reservations"])
    removed = set(active["reservation_event_ids"])
    forged["reservation_ledger"] = [
        row for row in forged["reservation_ledger"] if row["event_id"] not in removed
    ]
    forged["trade_ledger"] = [
        row for row in forged["trade_ledger"] if row["event_id"] not in removed
    ]
    active.update(
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
            "gross_profit_r": 0.0,
            "gross_loss_r": 0.0,
            "profit_factor": 0.0,
            "profit_factor_no_loss": False,
            "gate_profit_factor": False,
            "exit_mix": {},
            "reservation_event_ids": [],
            "trade_event_ids": [],
        }
    )
    assert not qscc.validate_source_replay(
        result=forged,
        bars_by_symbol={"EURUSD": bars},
        proxy_spread_budgets_bps=_budgets(),
        proxy_provenance=PROVENANCE,
        proxy_contract_schema_version=PROXY_SCHEMA,
        evaluation_start_utc=EVALUATION_START,
        evaluation_end_utc=EVALUATION_END,
        preregistration_lock_utc=PREREGISTRATION_LOCK,
    )


def test_strict_json_rejects_duplicate_keys_and_nonfinite_numbers() -> None:
    with pytest.raises(ValueError):
        qscc.loads_strict_json('{"event_id":"a","event_id":"b"}')
    with pytest.raises(ValueError):
        qscc.loads_strict_json('{"gate_pnl_r":NaN}')
    with pytest.raises(ValueError):
        qscc.dumps_strict_json({"gate_pnl_r": math.inf})


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-03T01:30:24Z",
        "2026-08-03T01:30:24.000000Z",
    ],
)
def test_parse_epoch_accepts_canonical_whole_second_utc(timestamp: str) -> None:
    assert qscc._parse_epoch(timestamp) == 1_785_720_624


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-08-03T01:30:24.1597594Z",
        "2026-08-03T01:30:24.1Z",
        "2026-08-03T01:30:24.000001Z",
    ],
)
def test_parse_epoch_rejects_fractional_seconds(timestamp: str) -> None:
    with pytest.raises(ValueError):
        qscc._parse_epoch(timestamp)


def test_load_proxy_contract_accepts_canonical_six_digit_lock(
    tmp_path: Path,
) -> None:
    proxy_path = tmp_path / "proxy_contract.json"
    proxy_path.write_text(
        qscc.dumps_strict_json(
            {
                "schema_version": PROXY_SCHEMA,
                "provenance": PROVENANCE,
                "budgets_bps": _budgets(),
            }
        ),
        encoding="utf-8",
    )

    budgets, provenance, schema_version = qscc._load_proxy_contract(
        proxy_path,
        evaluation_start_utc=EVALUATION_START,
        preregistration_lock_utc="2026-08-03T01:30:24.000000Z",
    )

    assert budgets == _budgets()
    assert provenance == PROVENANCE
    assert schema_version == PROXY_SCHEMA


def test_trial_accounting_one_config_36_cells_and_no_authority_are_frozen() -> None:
    assert [config.config_id for config in qscc.GRID] == ["qscc_v1"]
    assert qscc.trial_accounting() == {
        "grid_configurations": 1,
        "directions": 2,
        "symbols": 18,
        "current_attempted_cells": 36,
        "prior_attempted_cells": 4_386,
        "cumulative_attempted_cells": 4_422,
        "expected_full_universe_cells": 36,
    }
    result = _screen()
    assert len(result["cells"]) == 36
    assert {
        (cell["config_id"], cell["symbol"], cell["side"]) for cell in result["cells"]
    } == {
        ("qscc_v1", symbol, side)
        for symbol in qscc.FX_SYMBOLS
        for side in ("BUY", "SELL")
    }
    assert result["success_claim_authorized"] is False
    assert result["holdout_access_authorized"] is False
    assert result["promotion_authorized"] is False
    assert result["activation_authorized"] is False
    assert result["registry_write_authorized"] is False
    assert result["order_authorized"] is False
    assert result["economic_passed"] is False
    assert result["economic_claim_ready"] is False
    assert result["economics_claim_ready"] is False
    assert result["research_only"] is True
    assert result["discovery_only"] is True
    assert result["economic_claim_scope"] == "none_proxy_discovery_only"
