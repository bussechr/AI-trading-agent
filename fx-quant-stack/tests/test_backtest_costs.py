"""Tests for all-in cost accounting, including financing.

The bug these lock out: financing (swap/rollover) was charged NOWHERE in this
repository, while the EA reported it and the API stored it. Measured consequence
on the corrected M5 strategy -- 298.6 position-days, breakeven financing 0.340
bps/day against typical retail EURUSD swap of 0.5-2 bps/day -- the only positive
result the stack produced turns negative once carry is charged.
"""

from __future__ import annotations

import inspect

import pytest

from fxstack.backtest.costs import (
    all_in_cost_bps,
    financing_bps_for_holding,
    financing_bps_from_reported_swap,
)


def test_financing_is_a_required_argument():
    """No default. A 0.0 default is exactly how this cost went unnoticed."""

    params = inspect.signature(all_in_cost_bps).parameters
    assert params["financing_bps"].default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        all_in_cost_bps(spread_bps=1.0, slippage_bps=0.25)  # type: ignore[call-arg]


def test_all_in_sums_the_three_terms():
    assert all_in_cost_bps(spread_bps=1.0, slippage_bps=0.25, financing_bps=0.5) == pytest.approx(1.75)


def test_spread_and_slippage_clamp_non_negative():
    assert all_in_cost_bps(spread_bps=-5.0, slippage_bps=-2.0, financing_bps=0.0) == 0.0


def test_positive_carry_reduces_cost_and_is_not_clamped():
    """Carry can be EARNED. Clamping it away understates a carry strategy."""

    paid = all_in_cost_bps(spread_bps=1.0, slippage_bps=0.0, financing_bps=+0.8)
    earned = all_in_cost_bps(spread_bps=1.0, slippage_bps=0.0, financing_bps=-0.8)
    assert earned < paid
    assert earned == pytest.approx(0.2)


def test_financing_scales_with_holding_days():
    assert financing_bps_for_holding(cost_bps_per_day=1.5, holding_days=4.0) == pytest.approx(6.0)
    assert financing_bps_for_holding(cost_bps_per_day=1.5, holding_days=0.0) == 0.0


def test_financing_sign_is_preserved_over_holding():
    assert financing_bps_for_holding(cost_bps_per_day=-0.7, holding_days=3.0) == pytest.approx(-2.1)


def test_negative_holding_days_cannot_manufacture_income():
    assert financing_bps_for_holding(cost_bps_per_day=2.0, holding_days=-5.0) == 0.0


def test_reported_swap_paid_becomes_a_positive_cost():
    """Broker reports a DEBIT as negative; this module charges costs as positive."""

    # -$20 swap on $100,000 notional == you paid 2 bps.
    assert financing_bps_from_reported_swap(swap_amount=-20.0, notional=100_000.0) == pytest.approx(2.0)


def test_reported_swap_earned_becomes_a_negative_cost():
    assert financing_bps_from_reported_swap(swap_amount=15.0, notional=100_000.0) == pytest.approx(-1.5)


def test_reported_swap_handles_degenerate_notional():
    assert financing_bps_from_reported_swap(swap_amount=-20.0, notional=0.0) == 0.0
    assert financing_bps_from_reported_swap(swap_amount=-20.0, notional=-100_000.0) == pytest.approx(2.0)


# --------------------------------------------------------------- THE SEAM
#
# Each helper above was individually correct under its OWN reading of the sign,
# and every isolated test passed while the composition inverted carry. These
# tests exercise the boundary, which is the only place the bug was visible.


def test_paying_swap_increases_all_in_cost():
    """The bug: a swap-PAYING position used to REDUCE measured cost."""

    paid = financing_bps_from_reported_swap(swap_amount=-20.0, notional=100_000.0)
    total = all_in_cost_bps(spread_bps=1.0, slippage_bps=0.25, financing_bps=paid)
    baseline = all_in_cost_bps(spread_bps=1.0, slippage_bps=0.25, financing_bps=0.0)
    assert total > baseline, "paying financing must make a trade MORE expensive"
    assert total == pytest.approx(3.25)


def test_earning_swap_decreases_all_in_cost():
    earned = financing_bps_from_reported_swap(swap_amount=15.0, notional=100_000.0)
    total = all_in_cost_bps(spread_bps=1.0, slippage_bps=0.25, financing_bps=earned)
    baseline = all_in_cost_bps(spread_bps=1.0, slippage_bps=0.25, financing_bps=0.0)
    assert total < baseline, "earned carry must make a trade CHEAPER"
    assert total == pytest.approx(-0.25)


def test_carry_direction_survives_a_multi_day_hold():
    """Full path: broker swap -> per-day cost -> holding period -> all-in."""

    per_day = financing_bps_from_reported_swap(swap_amount=-2.0, notional=100_000.0)
    assert per_day > 0.0  # paying
    over_a_week = financing_bps_for_holding(cost_bps_per_day=per_day, holding_days=7.0)
    assert over_a_week == pytest.approx(1.4)
    assert all_in_cost_bps(spread_bps=1.0, slippage_bps=0.0, financing_bps=over_a_week) > 1.0


def test_breakeven_financing_arithmetic_matches_the_measurement():
    """The number that killed the +1.02%: 0.340 bps/day over 298.6 days."""

    net_bps = 102.0  # +1.02% expressed in bps
    holding_days = 298.6
    breakeven_per_day = net_bps / holding_days
    assert breakeven_per_day == pytest.approx(0.3416, abs=1e-3)
    # Typical retail EURUSD swap comfortably exceeds it -> result is negative.
    assert financing_bps_for_holding(cost_bps_per_day=0.5, holding_days=holding_days) > net_bps
