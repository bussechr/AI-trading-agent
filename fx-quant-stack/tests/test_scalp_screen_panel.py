"""Causality and planted-signal tests for the cross-sectional panel screen."""

from __future__ import annotations

import json
import math
import random

import pytest

import fxstack.scalp.screen_panel as screen_panel
from fxstack.scalp.panel import PanelBar


EPOCH_0 = 1_704_067_200  # 2024-01-01T00:00:00Z
BAR_MINUTES = 60
BAR_SECONDS = BAR_MINUTES * 60


def _bars_from_returns(
    epochs: list[int],
    returns_bps: list[float],
    *,
    first_mid: float,
    spread_bps: float = 0.20,
) -> dict[int, PanelBar]:
    previous = first_mid
    bars: dict[int, PanelBar] = {}
    for epoch, return_bps in zip(epochs, returns_bps):
        open_mid = previous
        mid = previous * (1.0 + return_bps / 1e4)
        half_spread = mid * spread_bps / 2e4
        open_half_spread = open_mid * spread_bps / 2e4
        bars[epoch] = PanelBar(
            epoch=epoch,
            bid=mid - half_spread,
            ask=mid + half_spread,
            prev_mid=previous,
            volume=100.0,
            bid_open=open_mid - open_half_spread,
            ask_open=open_mid + open_half_spread,
        )
        previous = mid
    return bars


def _planted_lagged_factor_panel(
    n: int = 1_000,
) -> tuple[list[int], dict[str, dict[int, PanelBar]]]:
    """Plant EURUSD's next return in three prior cross-pair factor bars."""
    rng = random.Random(71)
    epochs = [EPOCH_0 + BAR_SECONDS * i for i in range(n)]
    cross_pair_move = [rng.gauss(0.0, 1.25) for _ in epochs]

    # Signal at closed bar i enters at bar i+1's executable M1 open and H1
    # exits at that same complete bar's close, so the planted target return
    # belongs to bar i+1.
    target_returns = [rng.gauss(0.0, 0.25)]
    for target_i in range(1, n):
        signal_i = target_i - 1
        lagged_cross_pair_sum = sum(
            cross_pair_move[max(0, signal_i - 2):signal_i + 1]
        )
        target_returns.append(
            2.5 * lagged_cross_pair_sum + rng.gauss(0.0, 0.35)
        )

    # For EURUSD, the corrected leave-target-out factor implied move is
    # exactly cross_pair_move: quote-USD pairs move with it and USDJPY moves
    # against it. EURUSD itself therefore cannot manufacture the predictor.
    per_symbol = {
        "EURUSD": _bars_from_returns(
            epochs, target_returns, first_mid=1.10
        ),
        "GBPUSD": _bars_from_returns(
            epochs, cross_pair_move, first_mid=1.25
        ),
        "AUDUSD": _bars_from_returns(
            epochs, cross_pair_move, first_mid=0.65
        ),
        "USDJPY": _bars_from_returns(
            epochs, [-value for value in cross_pair_move], first_mid=150.0
        ),
    }
    return epochs, per_symbol


def test_screen_detects_planted_lagged_cross_pair_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epochs, per_symbol = _planted_lagged_factor_panel()
    monkeypatch.setattr(
        screen_panel,
        "load_panel",
        lambda **_kwargs: (epochs, per_symbol),
    )

    rows = screen_panel.screen_panel_symbol(
        (
            "EURUSD",
            list(per_symbol),
            "unused",
            BAR_MINUTES,
            [1],
            None,
            None,
            0.75,
            0.0,
            ("momentum",),
            0.10,
            0.90,
        )
    )
    by_feature = {row["feature"]: row for row in rows}
    planted = by_feature["factor_sum3_volnorm24"]
    one_bar = by_feature["usd_factor"]

    assert planted["n_days"] >= 30
    assert planted["ic"] > 0.90
    assert planted["ic"] > one_bar["ic"] + 0.20
    assert planted["tradable_bps"] > 0.0
    assert planted["tradable_t"] > 5.0
    assert planted["long_bps"] > 0.0
    assert planted["long_t"] > 5.0
    assert planted["short_bps"] > 0.0
    assert planted["short_t"] > 5.0
    assert planted["side_policy"] == "momentum"
    assert planted["lower_tail_quantile"] == 0.10
    assert planted["upper_tail_quantile"] == 0.90
    assert planted["lower_cutpoint"] < planted["upper_cutpoint"]
    assert 0.0 <= planted["long_win_rate"] <= 1.0
    assert 0.0 <= planted["short_win_rate"] <= 1.0
    # The planted gross edge is screened through actual bid/ask legs.
    assert planted["tradable_bps"] < planted["mid_bps"]

    padded_rows = screen_panel.screen_panel_symbol(
        (
            "EURUSD",
            list(per_symbol),
            "unused",
            BAR_MINUTES,
            [1],
            None,
            None,
            0.75,
            1.0,
            ("momentum",),
            0.10,
            0.90,
        )
    )
    padded = {row["feature"]: row for row in padded_rows}[
        "factor_sum3_volnorm24"
    ]
    assert padded["extra_round_trip_cost_bps"] == 1.0
    assert padded["long_bps"] == pytest.approx(planted["long_bps"] - 1.0)
    assert padded["short_bps"] == pytest.approx(planted["short_bps"] - 1.0)
    json.dumps(padded_rows, allow_nan=False)


def test_future_bar_perturbations_cannot_change_past_features() -> None:
    epochs, per_symbol = _planted_lagged_factor_panel(n=90)
    cutoff = epochs[55]
    before = screen_panel.build_feature_series(
        symbol="EURUSD",
        epochs=epochs,
        per_symbol=per_symbol,
        coherence_floor=0.75,
        bar_seconds=BAR_SECONDS,
    )

    perturbed = {symbol: dict(bars) for symbol, bars in per_symbol.items()}
    for symbol, bars in perturbed.items():
        for epoch in epochs[56:]:
            old = bars[epoch]
            direction = -1.0 if symbol == "USDJPY" else 1.0
            mid = old.mid * (1.0 + direction * 250.0 / 1e4)
            half_spread = mid * 0.20 / 2e4
            bars[epoch] = PanelBar(
                epoch=epoch,
                bid=mid - half_spread,
                ask=mid + half_spread,
                prev_mid=old.prev_mid,
                volume=old.volume * 10.0,
                bid_open=old.bid_open,
                ask_open=old.ask_open,
            )

    after = screen_panel.build_feature_series(
        symbol="EURUSD",
        epochs=epochs,
        per_symbol=perturbed,
        coherence_floor=0.75,
        bar_seconds=BAR_SECONDS,
    )
    for name in screen_panel.PANEL_FEATURE_NAMES:
        before_past = [(epoch, value) for epoch, value in before[name] if epoch <= cutoff]
        after_past = [(epoch, value) for epoch, value in after[name] if epoch <= cutoff]
        assert after_past == before_past

    assert any(
        abs(left[1] - right[1]) > 1e-9
        for left, right in zip(
            before["factor_sum3_volnorm24"],
            after["factor_sum3_volnorm24"],
        )
        if left[0] > cutoff
    )


def test_lagged_scale_excludes_the_current_closed_bar() -> None:
    values = [-1.0, 1.0] * 12 + [100.0]
    assert screen_panel._normalized_trailing_sum(
        values, 24, sum_bars=1, vol_bars=24
    ) == pytest.approx(100.0)


def test_lagged_features_restart_after_an_aligned_epoch_gap() -> None:
    epochs, per_symbol = _planted_lagged_factor_panel(n=90)
    del epochs[50]
    features = screen_panel.build_feature_series(
        symbol="EURUSD",
        epochs=epochs,
        per_symbol=per_symbol,
        coherence_floor=0.75,
        bar_seconds=BAR_SECONDS,
    )
    lagged = dict(features["factor_sum3_volnorm24"])
    # Twenty-four complete trailing observations plus the current bar are
    # required after the gap before lagged state is admitted again.
    assert all(lagged[epoch] == 0.0 for epoch in epochs[50:74])
    assert any(abs(lagged[epoch]) > 1e-12 for epoch in epochs[74:])


def test_side_policy_is_explicit_and_each_orientation_is_a_separate_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epochs, per_symbol = _planted_lagged_factor_panel()
    monkeypatch.setattr(
        screen_panel,
        "load_panel",
        lambda **_kwargs: (epochs, per_symbol),
    )
    rows = screen_panel.screen_panel_symbol(
        (
            "EURUSD",
            list(per_symbol),
            "unused",
            BAR_MINUTES,
            [1],
            None,
            None,
            0.75,
            0.0,
            ("momentum", "reversion"),
            0.10,
            0.90,
        )
    )
    assert len(rows) == 2 * len(screen_panel.PANEL_FEATURE_NAMES)
    by_cell = {(row["feature"], row["side_policy"]): row for row in rows}
    momentum = by_cell[("factor_sum3_volnorm24", "momentum")]
    reversion = by_cell[("factor_sum3_volnorm24", "reversion")]
    assert momentum["ic"] == reversion["ic"]
    assert momentum["lower_cutpoint"] == reversion["lower_cutpoint"]
    assert momentum["upper_cutpoint"] == reversion["upper_cutpoint"]
    assert momentum["tradable_bps"] > 0.0
    assert reversion["tradable_bps"] < 0.0
    assert screen_panel._ic_supports_policy(momentum, threshold=0.0)
    assert not screen_panel._ic_supports_policy(reversion, threshold=0.0)


def test_trade_returns_use_next_bar_fill_and_charge_both_spreads() -> None:
    epochs = [EPOCH_0 + BAR_SECONDS * i for i in range(3)]
    bars = {
        epochs[0]: PanelBar(
            epochs[0], 99.0, 101.0, 100.0, 1.0, 99.0, 101.0
        ),
        # The next bar opens at 110 and closes at 120. H1 must enter at the
        # former and exit at the latter, not wait for another aggregate close.
        epochs[1]: PanelBar(
            epochs[1], 119.0, 121.0, 100.0, 1.0, 109.0, 111.0
        ),
        epochs[2]: PanelBar(
            epochs[2], 129.0, 131.0, 120.0, 1.0, 124.0, 126.0
        ),
    }

    assert screen_panel.SCREEN_FILL_DELAY_BARS == 1
    mid_ret, long_ret, short_ret = screen_panel._delayed_trade_returns(
        bars=bars,
        epochs=epochs,
        signal_index=0,
        horizon=1,
        bar_seconds=BAR_SECONDS,
    )
    # Entry is bar 1's open (mid=110), never either observed close.
    assert mid_ret == pytest.approx((120.0 - 110.0) / 110.0 * 1e4)
    assert long_ret == pytest.approx((119.0 - 111.0) / 110.0 * 1e4)
    assert short_ret == pytest.approx((109.0 - 121.0) / 110.0 * 1e4)


def test_trade_returns_fail_closed_across_epoch_gap() -> None:
    epochs = [EPOCH_0, EPOCH_0 + 2 * BAR_SECONDS]
    bars = {
        epochs[0]: PanelBar(
            epochs[0], 99.9, 100.1, 100.0, 1.0, 99.9, 100.1
        ),
        epochs[1]: PanelBar(
            epochs[1], 100.9, 101.1, 100.0, 1.0, 100.4, 100.6
        ),
    }
    assert screen_panel._delayed_trade_returns(
        bars=bars,
        epochs=epochs,
        signal_index=0,
        horizon=1,
        bar_seconds=BAR_SECONDS,
    ) is None

    internal_gap_epochs = [
        EPOCH_0,
        EPOCH_0 + BAR_SECONDS,
        EPOCH_0 + 3 * BAR_SECONDS,
    ]
    internal_gap_bars = {
        epoch: PanelBar(epoch, 99.9, 100.1, 100.0, 1.0, 99.9, 100.1)
        for epoch in internal_gap_epochs
    }
    assert screen_panel._delayed_trade_returns(
        bars=internal_gap_bars,
        epochs=internal_gap_epochs,
        signal_index=0,
        horizon=2,
        bar_seconds=BAR_SECONDS,
    ) is None


def test_trade_returns_reject_nonfinite_executable_quotes() -> None:
    epochs = [EPOCH_0, EPOCH_0 + BAR_SECONDS]
    bars = {
        epochs[0]: PanelBar(
            epochs[0], 99.9, 100.1, 100.0, 1.0, 99.9, 100.1
        ),
        epochs[1]: PanelBar(
            epochs[1], 100.9, 101.1, 100.0, 1.0, math.nan, 100.6
        ),
    }
    assert screen_panel._delayed_trade_returns(
        bars=bars,
        epochs=epochs,
        signal_index=0,
        horizon=1,
        bar_seconds=BAR_SECONDS,
    ) is None


def test_default_trial_accounting_includes_every_attempted_cell() -> None:
    assert tuple(spec[0] for spec in screen_panel.LAGGED_FEATURE_SPECS) == (
        "residual_sum3_volnorm24",
        "residual_sum6_volnorm24",
        "factor_sum3_volnorm24",
        "factor_sum6_volnorm24",
    )
    assert screen_panel.panel_trial_accounting(
        n_symbols=18, n_horizons=3, prior_tests=60
    ) == {
        "base_cells": 216,
        "new_lagged_cells": 216,
        "orientation_multiplier": 1,
        "current_cells": 432,
        "prior_tests": 60,
        "cumulative_tests": 492,
    }
    assert screen_panel.panel_trial_accounting(
        n_symbols=18,
        n_horizons=3,
        prior_tests=60,
        n_side_policies=2,
    )["cumulative_tests"] == 924
    with pytest.raises(ValueError, match="trial counts"):
        screen_panel.panel_trial_accounting(
            n_symbols=18, n_horizons=3, prior_tests=-1
        )


def test_worker_reuses_initializer_owned_panel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ([EPOCH_0], {"EURUSD": {}})
    calls = 0

    def fake_load_panel(**_kwargs):
        nonlocal calls
        calls += 1
        return expected

    monkeypatch.setattr(screen_panel, "load_panel", fake_load_panel)
    monkeypatch.setattr(screen_panel, "_PANEL_WORKER_CACHE", None)
    screen_panel._init_panel_worker(
        ["EURUSD"], "unused", BAR_MINUTES, None, None
    )

    actual = screen_panel._worker_panel(
        symbols=["EURUSD"],
        csv_root="unused",
        bar_minutes=BAR_MINUTES,
        start=None,
        end=None,
    )

    assert actual is expected
    assert calls == 1


@pytest.mark.parametrize("extra_cost", [math.nan, math.inf, -0.01])
def test_worker_rejects_nonfinite_or_negative_extra_cost(extra_cost: float) -> None:
    with pytest.raises(ValueError, match="invalid panel screening policy"):
        screen_panel.screen_panel_symbol(
            (
                "EURUSD",
                ["EURUSD"],
                "unused",
                BAR_MINUTES,
                [1],
                None,
                None,
                0.75,
                extra_cost,
                ("reversion",),
                0.10,
                0.90,
            )
        )


@pytest.mark.parametrize(
    ("side_policies", "lower", "upper"),
    [
        (("auto",), 0.10, 0.90),
        (("reversion", "reversion"), 0.10, 0.90),
        (("reversion",), math.nan, 0.90),
        (("reversion",), 0.90, 0.10),
    ],
)
def test_worker_requires_explicit_unique_policy_and_valid_cutpoints(
    side_policies: tuple[str, ...], lower: float, upper: float
) -> None:
    with pytest.raises(ValueError, match="invalid panel screening policy"):
        screen_panel.screen_panel_symbol(
            (
                "EURUSD",
                ["EURUSD"],
                "unused",
                BAR_MINUTES,
                [1],
                None,
                None,
                0.75,
                0.0,
                side_policies,
                lower,
                upper,
            )
        )
