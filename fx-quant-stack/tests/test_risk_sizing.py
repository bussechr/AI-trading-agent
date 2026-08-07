"""Tests for risk-based position sizing.

The property that matters most: stop width must be RISK-NEUTRAL. Under the
incumbent ``equity * equity_lots_per_usd`` path, widening the stop multiplies
money at risk by the same factor -- which is why the bracket geometry cannot be
changed until this lands. These tests pin that down explicitly.
"""

from __future__ import annotations

import pytest

from fxstack.risk.sizing import (
    STANDARD_LOT_UNITS,
    kelly_fraction,
    lots_for_risk,
    quote_value_per_price_unit,
    risk_fraction_for_lots,
)

PIP = 0.0001


def test_basic_risk_budget_is_respected():
    # $10k, risk 1%, 20-pip stop -> $100 / (0.0020 * 100000) = 0.5 lots
    out = lots_for_risk(equity=10_000.0, risk_fraction=0.01, stop_distance_price=20 * PIP, max_lots=1.0)
    assert out.ok
    assert out.lots == pytest.approx(0.5, abs=1e-9)
    assert out.money_at_risk == pytest.approx(100.0, rel=1e-6)


def test_wider_stop_is_risk_neutral():
    """THE point of this module: 2.5x the stop must not mean 2.5x the risk."""

    tight = lots_for_risk(equity=10_000.0, risk_fraction=0.01, stop_distance_price=5 * PIP, max_lots=10.0)
    wide = lots_for_risk(equity=10_000.0, risk_fraction=0.01, stop_distance_price=12.5 * PIP, max_lots=10.0)
    assert tight.ok and wide.ok
    # Position shrinks in proportion...
    assert wide.lots == pytest.approx(tight.lots / 2.5, rel=1e-6)
    # ...so money at risk is unchanged.
    assert wide.money_at_risk == pytest.approx(tight.money_at_risk, rel=1e-6)


def test_incumbent_path_is_not_risk_neutral():
    """Demonstrates the bug this replaces, using the real production constants."""

    equity = 10_000.0
    incumbent_lots = equity * 1e-5  # runner.py _entry_order_lots, _env.bat:94
    tight = risk_fraction_for_lots(equity=equity, lots=incumbent_lots, stop_distance_price=5 * PIP)
    wide = risk_fraction_for_lots(equity=equity, lots=incumbent_lots, stop_distance_price=12.5 * PIP)
    assert wide == pytest.approx(tight * 2.5, rel=1e-6), (
        "fixed-lot sizing scales risk with stop width -- widening the bracket "
        "silently multiplies money at risk"
    )
    # And the absolute scale is economically invisible: ~0.05% of equity.
    assert tight < 0.001


def test_rounds_down_so_realized_risk_never_exceeds_budget():
    out = lots_for_risk(
        equity=10_000.0, risk_fraction=0.01, stop_distance_price=7 * PIP, lot_step=0.01, max_lots=10.0
    )
    assert out.ok
    # $100 budget / (0.0007 * 100_000) = 1.428571... lots exactly.
    # Must round DOWN to the 0.01 step, never up past the budget.
    assert out.lots == pytest.approx(1.42, abs=1e-9)
    assert out.money_at_risk == pytest.approx(99.40, rel=1e-6)
    assert out.money_at_risk <= 100.0 + 1e-9


def test_refuses_rather_than_rounding_up_below_min_lot():
    """A budget too small to express must fail loudly, not silently over-risk."""

    out = lots_for_risk(
        equity=500.0, risk_fraction=0.001, stop_distance_price=50 * PIP, min_lots=0.01, max_lots=1.0
    )
    assert not out.ok
    assert out.lots == 0.0
    assert "risk_budget_below_min_lot" in out.reason


def test_max_lots_cap_applies():
    out = lots_for_risk(
        equity=1_000_000.0, risk_fraction=0.02, stop_distance_price=5 * PIP, max_lots=0.10
    )
    assert out.lots == pytest.approx(0.10, abs=1e-9)


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"equity": 0.0}, "non_positive_equity"),
        ({"risk_fraction": 0.0}, "non_positive_risk_fraction"),
        ({"stop_distance_price": 0.0}, "non_positive_stop_distance"),
        ({"value_per_price_unit": 0.0}, "non_positive_contract_value"),
    ],
)
def test_degenerate_inputs_fail_closed(kwargs, reason):
    base = dict(
        equity=10_000.0, risk_fraction=0.01, stop_distance_price=10 * PIP,
        value_per_price_unit=STANDARD_LOT_UNITS,
    )
    base.update(kwargs)
    out = lots_for_risk(**base)
    assert out.lots == 0.0
    assert out.reason == reason


def test_nonfinite_inputs_fail_closed():
    out = lots_for_risk(equity=float("nan"), risk_fraction=0.01, stop_distance_price=10 * PIP)
    assert out.lots == 0.0


# ------------------------------------------------------------------ Kelly


def test_kelly_zero_when_no_edge():
    # p=0.20 at 4R is exactly breakeven -> no edge -> no size.
    assert kelly_fraction(win_probability=0.20, reward_risk_ratio=4.0) == 0.0
    assert kelly_fraction(win_probability=0.10, reward_risk_ratio=4.0) == 0.0


def test_kelly_positive_with_edge_and_capped():
    f = kelly_fraction(win_probability=0.40, reward_risk_ratio=4.0, fraction_of_kelly=0.25, max_fraction=0.02)
    assert 0.0 < f <= 0.02
    # Full Kelly here is (0.4*5-1)/4 = 0.25; a quarter of that is 0.0625, capped to 0.02.
    assert f == pytest.approx(0.02)


def test_kelly_monotone_in_probability():
    a = kelly_fraction(win_probability=0.25, reward_risk_ratio=4.0, max_fraction=1.0)
    b = kelly_fraction(win_probability=0.35, reward_risk_ratio=4.0, max_fraction=1.0)
    assert b > a > 0.0


def test_kelly_rejects_degenerate_probability():
    for p in (0.0, 1.0, -0.1, 1.5, float("nan")):
        assert kelly_fraction(win_probability=p, reward_risk_ratio=2.0) == 0.0


def test_kelly_declines_the_deployed_geometry_at_measured_hit_rate():
    """Measured: 14.03% hit rate at 4R. Kelly must size that at zero."""

    assert kelly_fraction(win_probability=0.1403, reward_risk_ratio=4.0) == 0.0


def test_quote_value_helper():
    assert quote_value_per_price_unit(lots=1.0) == pytest.approx(STANDARD_LOT_UNITS)
    assert quote_value_per_price_unit(lots=0.1) == pytest.approx(10_000.0)
    assert quote_value_per_price_unit(lots=-1.0) == 0.0


# ------------------------------------------------------- volatility targeting


def test_ewma_vol_tracks_regime():
    import numpy as np
    from fxstack.risk.sizing import ewma_volatility

    rng = np.random.default_rng(0)
    quiet = ewma_volatility(list(rng.normal(0, 0.002, 300)))
    wild = ewma_volatility(list(rng.normal(0, 0.010, 300)))
    assert wild > quiet > 0.0
    assert wild / quiet > 2.0


def test_ewma_vol_degenerate_inputs():
    from fxstack.risk.sizing import ewma_volatility

    assert ewma_volatility([]) == 0.0
    assert ewma_volatility([0.01]) == 0.0
    assert ewma_volatility([float("nan"), float("inf")]) == 0.0


def test_vol_targeting_holds_risk_constant_across_regimes():
    """Higher forecast volatility must buy a proportionally smaller position."""

    from fxstack.risk.sizing import volatility_targeted_fraction

    quiet = volatility_targeted_fraction(base_fraction=0.005, forecast_volatility=0.002, target_volatility=0.005)
    wild = volatility_targeted_fraction(base_fraction=0.005, forecast_volatility=0.010, target_volatility=0.005)
    assert wild < quiet
    # 5x the volatility -> ~1/5 the risk fraction (within the clamps).
    assert quiet / wild == pytest.approx(5.0, rel=0.05)


def test_vol_targeting_clamps_prevent_explosive_sizing():
    """A near-zero vol estimate must not size on a measurement artifact."""

    from fxstack.risk.sizing import volatility_targeted_fraction

    f = volatility_targeted_fraction(
        base_fraction=0.005, forecast_volatility=1e-9, target_volatility=0.005, max_scale=3.0
    )
    assert f == pytest.approx(0.015)  # base * max_scale, not base * 5_000_000


def test_vol_targeting_keeps_strategy_alive_in_turbulence():
    from fxstack.risk.sizing import volatility_targeted_fraction

    f = volatility_targeted_fraction(
        base_fraction=0.005, forecast_volatility=10.0, target_volatility=0.005, min_scale=0.25
    )
    assert f == pytest.approx(0.00125)  # floored, not switched off entirely


# --------------------------------------------------- drawdown risk scaling


def test_drawdown_scaling_is_inert_at_no_drawdown():
    from fxstack.risk.sizing import drawdown_scaled_fraction

    f = drawdown_scaled_fraction(base_fraction=0.005, drawdown_pct=0.0, max_drawdown_pct=20.0)
    assert f == pytest.approx(0.005)


def test_drawdown_scaling_ramps_down_linearly():
    """Halfway to the hard limit must be roughly half size, not full size."""

    from fxstack.risk.sizing import drawdown_scaled_fraction

    f = drawdown_scaled_fraction(base_fraction=0.005, drawdown_pct=10.0, max_drawdown_pct=20.0)
    assert f == pytest.approx(0.0025)


def test_drawdown_scaling_is_monotone_non_increasing():
    from fxstack.risk.sizing import drawdown_scaled_fraction

    prev = float("inf")
    for dd in range(0, 25):
        f = drawdown_scaled_fraction(base_fraction=0.005, drawdown_pct=float(dd), max_drawdown_pct=20.0)
        assert f <= prev + 1e-12, f"risk increased as drawdown deepened at dd={dd}"
        prev = f


def test_drawdown_scaling_floors_rather_than_switching_off():
    """At/over the limit the kernel blocks entries; sizing must not also hit 0."""

    from fxstack.risk.sizing import drawdown_scaled_fraction

    f = drawdown_scaled_fraction(
        base_fraction=0.005, drawdown_pct=50.0, max_drawdown_pct=20.0, min_scale=0.25
    )
    assert f == pytest.approx(0.00125)
    assert f > 0.0


def test_drawdown_scaling_never_scales_up_on_missing_config():
    """No configured limit, or junk input, must not become a risk INCREASE."""

    from fxstack.risk.sizing import drawdown_scaled_fraction

    for limit in (0.0, -5.0, float("nan")):
        assert drawdown_scaled_fraction(
            base_fraction=0.005, drawdown_pct=10.0, max_drawdown_pct=limit
        ) == pytest.approx(0.005)
    for dd in (-3.0, float("nan")):
        assert drawdown_scaled_fraction(
            base_fraction=0.005, drawdown_pct=dd, max_drawdown_pct=20.0
        ) == pytest.approx(0.005)


def test_drawdown_scaling_composes_with_stop_width_without_double_counting():
    """Account-state scaling and instrument-vol scaling must stay orthogonal.

    Money at risk should fall by exactly the drawdown factor, and remain
    invariant to stop width at any fixed drawdown.
    """

    from fxstack.risk.sizing import drawdown_scaled_fraction

    flat = drawdown_scaled_fraction(base_fraction=0.005, drawdown_pct=0.0, max_drawdown_pct=20.0)
    hurt = drawdown_scaled_fraction(base_fraction=0.005, drawdown_pct=10.0, max_drawdown_pct=20.0)
    for stop in (5 * PIP, 30 * PIP):
        a = lots_for_risk(equity=10_000.0, risk_fraction=flat, stop_distance_price=stop, max_lots=100.0)
        b = lots_for_risk(equity=10_000.0, risk_fraction=hurt, stop_distance_price=stop, max_lots=100.0)
        assert b.money_at_risk == pytest.approx(a.money_at_risk * 0.5, rel=1e-3)


# ------------------------------------ contract value in ACCOUNT currency
#
# The 100,000 default is only correct when the quote currency IS the account
# currency -- 4 of the 18 configured pairs. Measured at a 30-pip stop on $10k at
# 0.5% BEFORE this landed: EURGBP +22% over budget, USDCHF +7% over, USDCAD -29%
# under, and all six JPY pairs REFUSED because the size fell below 0.01 lots.

RATES = {"EURUSD": 1.10, "GBPUSD": 1.27, "USDJPY": 150.0,
         "USDCHF": 0.90, "USDCAD": 1.35, "AUDUSD": 0.65}


@pytest.mark.parametrize(
    "pair,expected",
    [
        ("EURUSD", 100_000.0),   # quote IS the account currency
        ("USDJPY", 100_000 / 150.0),
        ("EURJPY", 100_000 / 150.0),   # cross: depends only on the QUOTE ccy
        ("GBPJPY", 100_000 / 150.0),
        ("USDCHF", 100_000 / 0.90),
        ("USDCAD", 100_000 / 1.35),
        ("EURGBP", 100_000 * 1.27),    # inverse leg: USD per GBP
        ("EURAUD", 100_000 * 0.65),
    ],
)
def test_contract_value_converts_to_account_currency(pair, expected):
    from fxstack.risk.sizing import account_value_per_price_unit

    got = account_value_per_price_unit(pair=pair, rates=RATES)
    assert got == pytest.approx(expected, rel=1e-9)


def test_contract_value_fails_closed_when_rate_is_unavailable():
    """0.0 means 'do not risk-size' -- never silently fall back to 100k."""

    from fxstack.risk.sizing import account_value_per_price_unit

    assert account_value_per_price_unit(pair="EURNOK", rates=RATES) == 0.0
    assert account_value_per_price_unit(pair="", rates=RATES) == 0.0
    assert account_value_per_price_unit(pair="EURJPY", rates={}) == 0.0
    # And lots_for_risk must refuse it rather than divide by zero.
    out = lots_for_risk(equity=10_000.0, risk_fraction=0.005,
                        stop_distance_price=30 * PIP, value_per_price_unit=0.0)
    assert out.lots == 0.0
    assert out.reason == "non_positive_contract_value"


@pytest.mark.parametrize(
    "pair,pip",
    [("EURUSD", 1e-4), ("USDJPY", 1e-2), ("EURJPY", 1e-2), ("GBPJPY", 1e-2),
     ("USDCHF", 1e-4), ("USDCAD", 1e-4), ("EURGBP", 1e-4)],
)
def test_every_pair_lands_within_a_lot_step_of_the_risk_budget(pair, pip):
    """The property the 100k default broke: risk is the SAME across pairs."""

    from fxstack.risk.sizing import account_value_per_price_unit

    equity, frac = 10_000.0, 0.005
    budget = equity * frac
    stop = 30 * pip
    vpu = account_value_per_price_unit(pair=pair, rates=RATES)
    out = lots_for_risk(equity=equity, risk_fraction=frac,
                        stop_distance_price=stop, value_per_price_unit=vpu, max_lots=100.0)
    assert out.ok, f"{pair} could not be sized at all"
    money = out.lots * stop * vpu
    # Never over budget, and within one lot step of it.
    assert money <= budget + 1e-9
    assert money >= budget - (0.01 * stop * vpu) - 1e-9


def test_jpy_pairs_are_sizeable_at_all():
    """Regression: every JPY pair used to be refused as below the lot minimum."""

    from fxstack.risk.sizing import account_value_per_price_unit

    for pair in ("USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY"):
        vpu = account_value_per_price_unit(pair=pair, rates=RATES)
        out = lots_for_risk(equity=10_000.0, risk_fraction=0.005,
                            stop_distance_price=30 * 1e-2,
                            value_per_price_unit=vpu, max_lots=100.0)
        assert out.ok, f"{pair} still unsizeable: {out.reason}"


def test_account_currency_is_a_declared_setting_not_a_silent_default():
    """A wrong account currency mis-sizes every non-matching-quote pair."""

    from fxstack.settings import Settings

    s = Settings()
    assert s.account_currency == "USD"
    assert "FXSTACK_ACCOUNT_CURRENCY" in str(Settings.model_fields["account_currency"])


@pytest.mark.parametrize(
    "account,pair,expected",
    [
        ("USD", "EURUSD", 100_000.0),
        ("USD", "USDJPY", 100_000 / 150.0),
        ("JPY", "USDJPY", 100_000.0),        # quote IS the account currency
        ("JPY", "EURUSD", 100_000 * 150.0),  # USD contract valued in JPY
        ("GBP", "EURUSD", 100_000 / 1.27),
    ],
)
def test_contract_value_follows_the_account_currency(account, pair, expected):
    from fxstack.risk.sizing import account_value_per_price_unit

    got = account_value_per_price_unit(pair=pair, rates=RATES, account_currency=account)
    assert got == pytest.approx(expected, rel=1e-9)


def test_unavailable_cross_rate_fails_closed_for_non_usd_accounts():
    """GBP account + USDJPY needs a GBP/JPY leg that is not in the map."""

    from fxstack.risk.sizing import account_value_per_price_unit

    assert account_value_per_price_unit(pair="USDJPY", rates=RATES, account_currency="GBP") == 0.0


def test_runner_supplies_contract_value_to_the_kernel():
    """Wiring: without this the kernel silently uses the wrong 100k default."""

    import inspect

    from fxstack.runtime import runner

    src = inspect.getsource(runner._evaluate_runtime_risk_kernel)
    assert '"value_per_price_unit": float(entry_contract_value)' in src
    # ...and sizing must not engage when it cannot be resolved.
    assert "entry_contract_value > 0.0" in src


# ------------------------------------------- the WIRING, not just the maths
#
# The repeated failure mode in this stack is a correct primitive that nothing
# calls (t1_index, the swap field, target_cap). These tests fail if the runtime
# entry-sizing path stops consulting the drawdown ramp, which a unit test on
# sizing.py alone would never notice.


def test_runtime_entry_risk_fraction_actually_consults_drawdown():
    from types import SimpleNamespace

    from fxstack.runtime.runner import _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.005, risk_max_drawdown_pct=20.0)
    flat = _entry_risk_fraction(settings=settings, drawdown_pct=0.0)
    hurt = _entry_risk_fraction(settings=settings, drawdown_pct=10.0)
    assert flat == pytest.approx(0.005)
    assert hurt < flat, "runtime sizing ignores drawdown -- the ramp is unwired"
    assert hurt == pytest.approx(0.0025)


def test_runtime_entry_risk_fraction_keeps_its_hard_ceiling():
    """A mis-set env var must still not be able to risk the account."""

    from types import SimpleNamespace

    from fxstack.runtime.runner import _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.99, risk_max_drawdown_pct=20.0)
    assert _entry_risk_fraction(settings=settings, drawdown_pct=0.0) == pytest.approx(0.02)


def test_runtime_entry_risk_fraction_is_inert_without_a_configured_limit():
    from types import SimpleNamespace

    from fxstack.runtime.runner import _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.005, risk_max_drawdown_pct=0.0)
    assert _entry_risk_fraction(settings=settings, drawdown_pct=15.0) == pytest.approx(0.005)


def test_runtime_sizing_handoff_passes_every_scaling_input(monkeypatch):
    """The kernel must receive the SCALED fraction, not the raw setting.

    Each keyword corresponds to one of the three orthogonal multipliers. A
    missing one is not a style regression -- it silently drops a whole axis of
    risk control while everything still passes.
    """

    import inspect

    from fxstack.runtime import runner

    src = inspect.getsource(runner._evaluate_runtime_risk_kernel)
    handoff = src.split("_entry_risk_fraction(", 1)
    assert len(handoff) == 2, "the target_risk_pct handoff no longer sizes from the stop"
    # The call spans nested parens, so take a generous window rather than trying
    # to balance brackets by hand.
    call = handoff[1][:1200]

    # account state -- otherwise risk runs flat into the kernel's hard limit
    assert "drawdown_pct=drawdown_pct" in call
    # edge -- otherwise every setup sizes identically regardless of conviction
    assert "win_probability=" in call
    assert "reward_risk_ratio=" in call
    # Instrument volatility is deliberately NOT a term here -- lots_for_risk
    # already divides by an ATR-scaled stop, and normalising twice made realised
    # risk 184% more erratic. See test_entry_risk_fraction_does_not_double_normalise.


def test_entry_risk_fraction_scales_down_on_weak_edge():
    """Kelly cuts size when the edge is thin, and never below the floor.

    Note the operating band this actually has at a 0.5% base: quarter-Kelly on a
    healthy edge computes a fraction far above 0.5%, so the ceiling is inactive
    and size is simply the configured base. It only binds as the edge approaches
    breakeven -- which is precisely where sizing down matters.
    """

    from types import SimpleNamespace

    from fxstack.runtime.runner import MIN_KELLY_SIZE_SCALE, _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.005, risk_max_drawdown_pct=0.0)
    strong = _entry_risk_fraction(settings=settings, win_probability=0.62, reward_risk_ratio=2.0)
    thin = _entry_risk_fraction(settings=settings, win_probability=0.505, reward_risk_ratio=1.0)

    assert thin < strong
    assert thin >= MIN_KELLY_SIZE_SCALE * 0.005
    # Kelly is a ceiling on the configured size, never an amplifier above it.
    assert strong == pytest.approx(0.005)


def test_entry_risk_fraction_never_zeroes_an_approved_entry():
    """Sizing scales a decision; entry-versus-abstain is not its call."""

    from types import SimpleNamespace

    from fxstack.runtime.runner import MIN_KELLY_SIZE_SCALE, _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.005, risk_max_drawdown_pct=0.0)
    # A bet with no positive expectancy at all: Kelly proper would say zero.
    out = _entry_risk_fraction(settings=settings, win_probability=0.20, reward_risk_ratio=0.5)

    assert out == pytest.approx(MIN_KELLY_SIZE_SCALE * 0.005)
    assert out > 0.0


def test_entry_risk_fraction_matches_legacy_base_without_optional_inputs():
    """Callers that supply no edge or volatility get exactly the old behaviour."""

    from types import SimpleNamespace

    from fxstack.runtime.runner import _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.005, risk_max_drawdown_pct=0.0)
    assert _entry_risk_fraction(settings=settings) == pytest.approx(0.005)
    assert _entry_risk_fraction(settings=settings, drawdown_pct=0.0) == pytest.approx(0.005)


def test_entry_risk_fraction_does_not_double_normalise_volatility():
    """Instrument volatility must be normalised exactly ONCE, downstream.

    `lots_for_risk` sizes as equity * risk_fraction / (stop * vpu) where the stop
    is an ATR multiple, so it already holds money-at-risk constant across
    instrument volatility. A vol-targeting multiplier on `risk_fraction` divides
    by volatility a second time.

    Measured on 3,100 real EURUSD M15 samples when this was briefly shipped:
    realised risk per stop-out went mean $49.07 / CV 0.0215 -> mean $11.58 /
    CV 0.0610, i.e. 184% MORE erratic and 76% smaller. This test pins that the
    risk fraction is now blind to the return series.
    """

    from types import SimpleNamespace

    from fxstack.runtime.runner import _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.005, risk_max_drawdown_pct=0.0)
    calm = [0.0002 * sign for sign in (1, -1) for _ in range(24)]
    wild = [0.0040 * sign for sign in (1, -1) for _ in range(24)]

    assert _entry_risk_fraction(settings=settings, realized_returns=calm) == pytest.approx(
        _entry_risk_fraction(settings=settings, realized_returns=wild)
    ), "risk_fraction must not respond to instrument volatility -- the ATR stop owns that"
    assert _entry_risk_fraction(settings=settings, realized_returns=wild) == pytest.approx(0.005)


def test_volatility_targeting_still_exists_for_non_atr_sizing_paths():
    """The function is correct; it was the COMPOSITION that was wrong.

    It belongs on a sizing path whose stop is not itself volatility-scaled, so
    it stays available and tested in risk/sizing.py.
    """

    from fxstack.risk.sizing import volatility_targeted_fraction

    calm = volatility_targeted_fraction(
        base_fraction=0.005, forecast_volatility=0.0005, target_volatility=0.001
    )
    wild = volatility_targeted_fraction(
        base_fraction=0.005, forecast_volatility=0.004, target_volatility=0.001
    )
    assert wild < calm


def test_entry_risk_fraction_ignores_unusable_return_series():
    """Junk in the return series must read as "no estimate", not as a number."""

    from types import SimpleNamespace

    from fxstack.runtime.runner import _entry_risk_fraction

    settings = SimpleNamespace(entry_risk_fraction=0.005, risk_max_drawdown_pct=0.0)
    for junk in (None, [], ["x", "y"], [float("nan"), float("inf")], object()):
        assert _entry_risk_fraction(settings=settings, realized_returns=junk) == pytest.approx(0.005)


def test_vol_targeting_does_not_scale_up_on_unknown_volatility():
    """Missing estimate -> unscaled base, never an optimistic guess."""

    from fxstack.risk.sizing import volatility_targeted_fraction

    for bad in (0.0, -1.0, float("nan")):
        assert volatility_targeted_fraction(
            base_fraction=0.005, forecast_volatility=bad, target_volatility=0.005
        ) == pytest.approx(0.005)
