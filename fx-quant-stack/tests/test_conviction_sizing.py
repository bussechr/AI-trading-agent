"""Pins that position size actually expresses conviction, and that realized
sleeve expectancy governs allocation.

The prior Kelly implementation was near-inert: capped at the 0.5% base, it
exceeded that cap for any edge better than roughly p=0.51 at 1:1, so a 0.53
setup and a 0.75 setup were funded identically. Expressing conviction is the
entire point of Kelly.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fxstack.runtime.runner import (
    KELLY_REFERENCE_FRACTION,
    MIN_KELLY_SIZE_SCALE,
    _entry_risk_fraction,
)
from fxstack.strategy.allocator_types import SleeveHealthSnapshot
from fxstack.strategy.sleeve_governance import (
    EXPECTANCY_FULL_ALLOCATION_USD,
    MIN_EXPECTANCY_ALLOCATION_SCALE,
    MIN_EXPECTANCY_TRADES,
    sleeve_expectancy_allocation_scale,
)


BASE = 0.005


def _settings(max_scale: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        entry_risk_fraction=BASE,
        risk_max_drawdown_pct=0.0,
        max_conviction_size_scale=max_scale,
    )


# ---------------------------------------------------------------------------
# Conviction sizing
# ---------------------------------------------------------------------------


def test_size_is_monotone_in_conviction():
    """Better edge must never buy less size."""
    settings = _settings()
    ladder = [(0.505, 1.0), (0.52, 1.0), (0.55, 1.0), (0.55, 1.5), (0.60, 1.5), (0.65, 2.0)]
    sizes = [
        _entry_risk_fraction(settings=settings, win_probability=p, reward_risk_ratio=b)
        for p, b in ladder
    ]
    assert sizes == sorted(sizes), f"size must not decrease as edge improves: {sizes}"


def test_weak_and_strong_edges_are_no_longer_funded_identically():
    """The specific defect: the old ceiling pinned both to the base."""
    settings = _settings()
    weak = _entry_risk_fraction(settings=settings, win_probability=0.505, reward_risk_ratio=1.0)
    strong = _entry_risk_fraction(settings=settings, win_probability=0.60, reward_risk_ratio=1.5)

    assert weak < strong
    assert strong / weak >= 3.0, "the gradient must be economically meaningful, not a rounding difference"


def test_conviction_floor_is_respected():
    """A thin but APPROVED edge trades small, never zero -- sizing does not
    re-litigate an admission the entry gates already granted."""
    settings = _settings()
    out = _entry_risk_fraction(settings=settings, win_probability=0.5001, reward_risk_ratio=0.1)
    assert out == pytest.approx(MIN_KELLY_SIZE_SCALE * BASE)
    assert out > 0.0


def test_default_never_sizes_above_base():
    """Sizing up runs on uncalibrated probabilities, so the default cap is 1.0x."""
    settings = _settings(max_scale=1.0)
    for p, b in [(0.60, 1.5), (0.75, 2.0), (0.95, 5.0)]:
        assert _entry_risk_fraction(
            settings=settings, win_probability=p, reward_risk_ratio=b
        ) <= BASE + 1e-12


def test_upward_scaling_engages_only_when_explicitly_raised():
    """The mechanism is two-sided and ready; the cap is what holds it back."""
    capped = _entry_risk_fraction(
        settings=_settings(max_scale=1.0), win_probability=0.65, reward_risk_ratio=2.0
    )
    released = _entry_risk_fraction(
        settings=_settings(max_scale=2.0), win_probability=0.65, reward_risk_ratio=2.0
    )
    assert capped == pytest.approx(BASE)
    assert released > BASE
    assert released <= 2.0 * BASE


def test_reference_trade_sizes_at_base():
    """p=0.55 at 1.5:1 is the reference; it should fund at exactly 1.0x."""
    out = _entry_risk_fraction(
        settings=_settings(), win_probability=0.55, reward_risk_ratio=1.5
    )
    assert out == pytest.approx(BASE)
    assert KELLY_REFERENCE_FRACTION == pytest.approx(0.0625)


def test_absent_edge_inputs_leave_sizing_untouched():
    """Callers that cannot supply edge get exactly the configured base."""
    assert _entry_risk_fraction(settings=_settings()) == pytest.approx(BASE)


# ---------------------------------------------------------------------------
# Sleeve expectancy -> allocation
# ---------------------------------------------------------------------------


def _snapshot(*, trades: int, expectancy: float) -> SleeveHealthSnapshot:
    return SleeveHealthSnapshot(
        sleeve="trend_pullback",
        score=0.6,
        state="healthy",
        trades=int(trades),
        win_rate=0.5,
        expectancy_usd=float(expectancy),
        profit_factor=1.0,
        avg_holding_bars=10.0,
        partial_frequency=0.0,
        replacement_exit_share=0.0,
        drawdown_contribution_usd=0.0,
        session_pnl_mix={},
        pair_contribution={},
    )


def test_thin_history_does_not_move_allocation():
    """Below the evidence floor, expectancy is noise."""
    scale, reason = sleeve_expectancy_allocation_scale(
        _snapshot(trades=MIN_EXPECTANCY_TRADES - 1, expectancy=-50.0)
    )
    assert scale == 1.0
    assert "insufficient_trades" in reason


def test_a_paying_sleeve_keeps_full_allocation():
    scale, reason = sleeve_expectancy_allocation_scale(
        _snapshot(trades=40, expectancy=EXPECTANCY_FULL_ALLOCATION_USD + 5.0)
    )
    assert scale == 1.0
    assert reason == "expectancy_full"


def test_a_losing_sleeve_is_starved_but_not_switched_off():
    """Zero would remove the only source of evidence that it has recovered."""
    scale, reason = sleeve_expectancy_allocation_scale(
        _snapshot(trades=40, expectancy=-25.0)
    )
    assert scale == pytest.approx(MIN_EXPECTANCY_ALLOCATION_SCALE)
    assert scale > 0.0
    assert reason == "expectancy_non_positive"


def test_allocation_ramps_monotonically_with_expectancy():
    scales = [
        sleeve_expectancy_allocation_scale(_snapshot(trades=40, expectancy=e))[0]
        for e in (0.0, 2.5, 5.0, 7.5, 10.0)
    ]
    assert scales == sorted(scales)
    assert scales[0] == pytest.approx(MIN_EXPECTANCY_ALLOCATION_SCALE)
    assert scales[-1] == pytest.approx(1.0)


def test_unusable_snapshots_are_inert():
    """Never let a malformed snapshot silently resize the book."""
    assert sleeve_expectancy_allocation_scale(None)[0] == 1.0
    assert sleeve_expectancy_allocation_scale(
        _snapshot(trades=40, expectancy=float("nan"))
    )[0] == 1.0
