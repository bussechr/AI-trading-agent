"""Tests for the feature screener.

A screener that cannot detect a planted edge is useless, and one that finds
edges in noise is dangerous. Both directions are pinned here, along with the
property that separates this tool from the mistakes it exists to prevent:
information is measured on the mid, monetizability on the bid/ask.
"""

from __future__ import annotations

import datetime as dt
import math
import random

import pytest

from fxstack.scalp.screen import (
    Obs,
    _clustered_t,
    screen_feature,
)

BASE = int(dt.datetime(2024, 1, 2, 8, 0, tzinfo=dt.timezone.utc).timestamp())


def _series(
    n: int, *, spread_bps: float, drift_from_feature: float, seed: int = 7
) -> tuple[list[Obs], list[float]]:
    """Build bars where a hidden feature predicts the NEXT bar's mid move.

    ``drift_from_feature`` scales how much of the next move the feature
    explains: 0.0 is pure noise, larger is a stronger planted edge.
    """
    rng = random.Random(seed)
    obs: list[Obs] = []
    planted: list[float] = []
    mid = 1.1000
    bars_per_day = 60  # a 5-hour session of M5 bars, like a real one
    for i in range(n):
        signal = rng.gauss(0, 1)
        planted.append(signal)
        # The move that follows this bar is partly explained by `signal`.
        move_bps = drift_from_feature * signal + rng.gauss(0, 3.0)
        half = mid * spread_bps / 1e4 / 2.0
        obs.append(
            Obs(
                epoch=BASE + (i // bars_per_day) * 86_400 + (i % bars_per_day) * 300,
                bid_o=mid - half, bid_h=mid - half + 1e-5, bid_l=mid - half - 1e-5,
                bid_c=mid - half,
                ask_o=mid + half, ask_h=mid + half + 1e-5, ask_l=mid + half - 1e-5,
                ask_c=mid + half,
                volume=100.0 + signal * 10.0,
            )
        )
        mid *= 1.0 + move_bps / 1e4
    return obs, planted


def test_screener_detects_a_planted_edge_that_clears_the_spread():
    obs, planted = _series(4000, spread_bps=0.2, drift_from_feature=6.0)
    result = screen_feature(
        obs, name="planted", fn=lambda o, i: planted[i], horizon=1
    )
    assert result.n_obs > 1000
    assert result.has_information, result.ic_t_clustered
    assert result.ic > 0
    # With a tiny spread the edge is also monetizable.
    assert result.is_monetizable, (result.tradable_bps, result.tradable_t_clustered)


def test_screener_separates_information_from_monetizability():
    """The panel's killer case: real information, eaten by the spread.

    Same planted edge, same data -- only the quoted spread changes. The
    screener must keep saying "information" while refusing to call it
    tradable. Conflating these two is how an unmonetizable mid artifact gets
    built into a strategy.
    """
    obs, planted = _series(4000, spread_bps=12.0, drift_from_feature=6.0)
    result = screen_feature(
        obs, name="planted", fn=lambda o, i: planted[i], horizon=1
    )
    assert result.has_information
    assert not result.is_monetizable
    assert result.mid_bps > 0.0  # the mid move is real...
    assert result.tradable_bps < 0.0  # ...and unreachable through the quotes


def test_inverted_signal_is_monetized_in_the_right_direction():
    """A negative-IC feature is traded by shorting its top decile.

    Always going long the top would report a genuinely tradable inverted
    edge as a loss -- the screener would hide exactly what it exists to find.
    """
    obs, planted = _series(4000, spread_bps=0.2, drift_from_feature=6.0)
    straight = screen_feature(
        obs, name="straight", fn=lambda o, i: planted[i], horizon=1
    )
    inverted = screen_feature(
        obs, name="inverted", fn=lambda o, i: -planted[i], horizon=1
    )
    assert straight.ic > 0 and inverted.ic < 0
    # Same underlying edge, opposite feature sign: both must be monetizable
    # and worth the same, because the portfolio follows the IC.
    assert inverted.is_monetizable
    assert inverted.tradable_bps == pytest.approx(straight.tradable_bps, rel=0.05)


def test_search_size_raises_the_verdict_bar():
    from fxstack.scalp.screen import search_corrected_threshold

    one = search_corrected_threshold(1)
    sixty = search_corrected_threshold(60)
    many = search_corrected_threshold(1000)
    assert one >= 2.5
    assert sixty > one and many > sixty
    # A borderline hit that passes alone must fail inside a 60-cell search --
    # the exact situation that produced this repo's only "MONETIZABLE" flag.
    obs, planted = _series(4000, spread_bps=0.2, drift_from_feature=6.0)
    r = screen_feature(obs, name="planted", fn=lambda o, i: planted[i], horizon=1)
    r.t_threshold = 2.5
    assert r.is_monetizable
    r.tradable_t_clustered = 2.62  # the observed borderline value
    r.t_threshold = sixty
    assert not r.is_monetizable


def test_screener_finds_nothing_in_noise():
    obs, planted = _series(4000, spread_bps=1.0, drift_from_feature=0.0)
    result = screen_feature(
        obs, name="noise", fn=lambda o, i: planted[i], horizon=1
    )
    assert not result.has_information, result.ic_t_clustered
    assert not result.is_monetizable


def test_clustered_t_discounts_correlated_within_day_outcomes():
    """The real-world case: outcomes inside a day share a common shock.

    An iid t-stat over 200 such observations treats them as 200 independent
    facts; the clustered t sees 20 days of common shocks and reports a far
    smaller number. This is the property that stops a handful of good
    sessions from arming a strategy.
    """
    rng = random.Random(11)
    independent: list[tuple[str, float]] = []
    correlated: list[tuple[str, float]] = []
    for day in range(20):
        shock = rng.gauss(0.4, 1.0)  # one common shock per day
        for k in range(10):
            independent.append((f"2024-01-{day + 1:02d}", rng.gauss(0.4, 1.0)))
            # Same mean, but the day's shock dominates each observation.
            correlated.append((f"2024-01-{day + 1:02d}", shock + rng.gauss(0, 0.05)))
    iid_like = _iid_t([v for _d, v in correlated])
    clustered = abs(_clustered_t(correlated))
    # Clustering must strip the fake precision out of correlated data.
    assert clustered < iid_like * 0.75
    # And it must NOT punish genuinely independent within-day observations.
    assert abs(_clustered_t(independent)) > clustered * 0.5


def _iid_t(values: list[float]) -> float:
    import statistics as st

    n = len(values)
    sd = st.stdev(values)
    return abs(st.fmean(values) / (sd / math.sqrt(n))) if sd > 1e-12 else 0.0


def test_verdicts_require_enough_distinct_days():
    # A strong effect seen on only a few days must not earn a verdict: with
    # few clusters the t-statistic inflates rather than informs.
    obs, planted = _series(2000, spread_bps=0.2, drift_from_feature=8.0)
    squeezed = [
        Obs(
            epoch=BASE + (i % 120) * 300,  # everything inside ~5 days
            bid_o=o.bid_o, bid_h=o.bid_h, bid_l=o.bid_l, bid_c=o.bid_c,
            ask_o=o.ask_o, ask_h=o.ask_h, ask_l=o.ask_l, ask_c=o.ask_c,
            volume=o.volume,
        )
        for i, o in enumerate(obs)
    ]
    result = screen_feature(
        squeezed, name="planted_few_days", fn=lambda o, i: planted[i], horizon=1
    )
    assert result.n_days < result.MIN_DAYS
    assert not result.has_information
    assert not result.is_monetizable


def test_short_leg_is_priced_from_quotes_not_mirrored():
    """A short must pay its own spread.

    If the short leg were inferred by negating the long leg, a wide market
    would look like a free lunch on one side. Here both legs of a pure-noise
    series must be negative once the spread is paid.
    """
    obs, planted = _series(3000, spread_bps=8.0, drift_from_feature=0.0)
    result = screen_feature(
        obs, name="noise_wide", fn=lambda o, i: planted[i], horizon=1
    )
    # Long-top + short-bottom on noise: strictly loses the spread.
    assert result.tradable_bps < 0.0
    assert math.isfinite(result.tradable_t_clustered)
