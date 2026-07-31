"""Regression tests for exit-label conviction monotonicity.

The bug these lock out: ``best >= partial_tp_r`` was tested before
``best >= tighten_stop_r`` and produced ``partial_tp`` unconditionally, where
``best`` is the maximum FORWARD FAVOURABLE excursion. The runtime executes
``partial_tp`` as CLOSE_PARTIAL, so the strongest possible forecast of a
favourable move produced the most defensive action -- the model was trained to
sell winners exactly when the forward move was best.

The invariant: at equal adverse risk, a larger favourable excursion must never
produce a more defensive action than a smaller one.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from fxstack.labels.exit_labels import EXIT_ACTIONS, ExitLabelConfig, build_exit_labels

# How much POSITION each action surrenders. This -- not "defensiveness" -- is the
# quantity that must not increase with a better forecast. ``tighten_stop`` ranks
# 0 alongside ``hold``: it protects profit without giving up any exposure, so
# escalating hold -> tighten_stop as the favourable move grows is correct.
LIQUIDATION = {"hold": 0, "tighten_stop": 0, "partial_tp": 1, "reduce": 2, "exit": 3}


def _frame(path: list[float], *, atr: float = 1.0) -> pd.DataFrame:
    """Build a long-side frame whose forward mid path is exactly ``path``."""

    closes = [100.0, *[100.0 + p * atr for p in path]]
    n = len(closes)
    return pd.DataFrame(
        {
            "mid_close": closes,
            "mid_high": closes,
            "mid_low": closes,
            "atr_14": [atr] * n,
            "side": ["long"] * n,
            "spread_bps": [1.0] * n,
            "vol_20": [0.5] * n,
        }
    )


def _first_action(path: list[float], cfg: ExitLabelConfig | None = None) -> str:
    cfg = cfg or ExitLabelConfig(horizon_bars=len(path))
    out = build_exit_labels(_frame(path), cfg)
    return str(out["exit_action"].iloc[0])


def test_actions_cover_known_taxonomy():
    assert set(LIQUIDATION) == set(EXIT_ACTIONS)


def test_strong_favourable_move_that_holds_does_not_liquidate():
    """+2R and still there at the horizon: protect, do not cut."""

    action = _first_action([0.5, 1.2, 2.0, 2.0, 2.0])
    assert action == "tighten_stop", (
        f"a favourable move that is still holding must not be cut, got {action!r}"
    )
    assert LIQUIDATION[action] < LIQUIDATION["partial_tp"]


def test_strong_favourable_move_that_fades_banks_profit():
    """+2R that gives most of it back: banking part is rational."""

    action = _first_action([0.5, 1.2, 2.0, 1.0, 0.2])
    assert action == "partial_tp", f"a faded winner should bank, got {action!r}"


def test_monotone_in_conviction_at_equal_adverse_risk():
    """The core invariant, swept across excursion sizes."""

    previous = None
    for peak in (0.2, 0.6, 1.0, 1.4, 1.8, 2.5, 4.0):
        # Path rises to `peak` and stays there -- no adverse excursion at all.
        action = _first_action([peak * 0.5, peak, peak, peak])
        current = LIQUIDATION[action]
        if previous is not None:
            assert current <= previous, (
                f"non-monotone: peak={peak} gave {action!r} (surrenders {current}) "
                f"which gives up MORE position than the weaker signal ({previous})"
            )
        previous = current


def test_adverse_excursion_still_exits():
    """The defensive side must keep working."""

    assert _first_action([-0.3, -0.7, -1.2, -1.5]) == "exit"


def test_moderate_drawdown_reduces():
    action = _first_action([-0.2, -0.5, -0.8, -0.85])
    assert action in {"reduce", "exit"}


def test_flat_path_holds():
    assert _first_action([0.0, 0.0, 0.0, 0.0]) == "hold"


def test_fade_fraction_is_configurable():
    path = [1.0, 2.0, 1.4, 1.4]  # gives back 30%
    lenient = ExitLabelConfig(horizon_bars=len(path), partial_tp_fade_frac=0.5)
    strict = ExitLabelConfig(horizon_bars=len(path), partial_tp_fade_frac=0.8)
    # Under a 0.5 threshold 1.4 > 0.5*2.0 -> still holding -> protect.
    assert _first_action(path, lenient) == "tighten_stop"
    # Under 0.8, 1.4 <= 0.8*2.0 -> counts as fading -> bank.
    assert _first_action(path, strict) == "partial_tp"


def test_wide_spread_samples_are_down_weighted_not_up_weighted():
    """Spread is a cost. Training hardest on expensive bars is backwards."""

    cheap = _frame([0.1, 0.1, 0.1, 0.1])
    expensive = _frame([0.1, 0.1, 0.1, 0.1])
    cheap["spread_bps"] = 0.5
    expensive["spread_bps"] = 8.0
    cfg = ExitLabelConfig(horizon_bars=3)
    w_cheap = float(build_exit_labels(cheap, cfg)["sample_weight"].iloc[0])
    w_expensive = float(build_exit_labels(expensive, cfg)["sample_weight"].iloc[0])
    assert w_expensive < w_cheap, (
        f"wide-spread sample weighted {w_expensive} vs cheap {w_cheap} -- the model "
        "would learn most from the least tradeable regime"
    )
    assert w_cheap > 0.0 and w_expensive > 0.0


def test_higher_volatility_still_carries_more_weight():
    quiet = _frame([0.1, 0.1, 0.1, 0.1])
    lively = _frame([0.1, 0.1, 0.1, 0.1])
    quiet["vol_20"] = 0.1
    lively["vol_20"] = 2.0
    cfg = ExitLabelConfig(horizon_bars=3)
    assert float(build_exit_labels(lively, cfg)["sample_weight"].iloc[0]) > float(
        build_exit_labels(quiet, cfg)["sample_weight"].iloc[0]
    )


def test_no_lookahead_beyond_horizon():
    """A move after the horizon must not influence the label."""

    cfg = ExitLabelConfig(horizon_bars=2)
    near = _first_action([0.1, 0.1, 5.0, 5.0], cfg)
    assert near == "hold", f"label leaked a post-horizon move, got {near!r}"
