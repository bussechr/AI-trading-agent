"""Tests for label-overlap correction.

The property that matters: overlapping labels must NOT count as independent
observations. Every promotion decision in this stack is currently made on
statistics that assume they do, which inflates every t-statistic by roughly
sqrt(nominal_N / effective_N).
"""

from __future__ import annotations

import numpy as np
import pytest

from fxstack.validation.uniqueness import (
    average_uniqueness,
    effective_sample_size,
    num_concurrent_events,
    overlap_report,
    purged_train_indices,
    sample_weights,
    sequential_bootstrap,
)


def _non_overlapping(n: int) -> np.ndarray:
    """Label i resolves on its own bar -- no overlap at all."""
    return np.arange(n, dtype=np.int64)


def _overlapping(n: int, horizon: int) -> np.ndarray:
    """Label i resolves `horizon` bars later -- heavy overlap."""
    return np.minimum(np.arange(n) + horizon, n - 1).astype(np.int64)


# ------------------------------------------------------------------ concurrency


def test_non_overlapping_labels_have_concurrency_one():
    c = num_concurrent_events(_non_overlapping(50))
    assert c.min() == 1 and c.max() == 1


def test_overlapping_labels_raise_concurrency():
    c = num_concurrent_events(_overlapping(200, 10))
    # A 10-bar horizon means ~11 labels alive in steady state.
    assert c.max() >= 10
    assert c.mean() > 5.0


def test_concurrency_length_matches_bars():
    assert num_concurrent_events(_overlapping(64, 8)).size == 64


# ------------------------------------------------------------------ uniqueness


def test_non_overlapping_labels_are_fully_unique():
    u = average_uniqueness(_non_overlapping(40))
    assert np.allclose(u, 1.0)
    assert effective_sample_size(_non_overlapping(40)) == pytest.approx(40.0)


def test_overlapping_labels_are_not_unique():
    n, horizon = 300, 24
    u = average_uniqueness(_overlapping(n, horizon))
    assert u.max() <= 1.0
    assert u.mean() < 0.25, f"a {horizon}-bar horizon should be far from unique, got {u.mean():.3f}"


def test_effective_sample_size_collapses_with_overlap():
    """THE headline: 790 rows of D labels are not 790 observations."""

    n, horizon = 790, 24
    eff = effective_sample_size(_overlapping(n, horizon))
    assert eff < n / 5.0, f"effective N {eff:.1f} should be a small fraction of {n}"
    rep = overlap_report(_overlapping(n, horizon))
    # Audit claim was ~8x overstatement for this shape; assert the same order.
    assert rep["overstatement_factor"] > 5.0
    assert rep["t_stat_inflation"] > 2.0


def test_uniqueness_is_bounded_and_positive():
    u = average_uniqueness(_overlapping(120, 40))
    assert np.all(u > 0.0) and np.all(u <= 1.0)


def test_unresolved_t1_is_treated_as_single_bar():
    t1 = np.array([0.0, np.nan, 2.0, np.inf], dtype=float)
    u = average_uniqueness(t1, n_bars=4)
    assert u.size == 4 and np.all(np.isfinite(u))


def test_t1_before_start_is_clamped():
    """A malformed t1 pointing backwards must not produce a negative span."""

    u = average_uniqueness(np.array([5, 0, 1, 2, 3], dtype=np.int64), n_bars=5)
    assert u.size == 5 and np.all(u > 0.0)


# --------------------------------------------------------------- sample weights


def test_weights_normalize_to_mean_one():
    w = sample_weights(_overlapping(150, 12))
    assert w.mean() == pytest.approx(1.0, rel=1e-9)


def test_weights_downweight_overlapping_samples_relative_to_unique_ones():
    # First 20 labels resolve immediately; the rest overlap heavily.
    t1 = np.concatenate([np.arange(20), np.minimum(np.arange(20, 200) + 30, 199)]).astype(np.int64)
    w = sample_weights(t1, n_bars=200, normalize=False)
    assert w[:20].mean() > w[20:].mean() * 3.0


def test_magnitude_scaling_rewards_larger_moves():
    t1 = _non_overlapping(10)
    rets = np.array([0.001] * 5 + [0.01] * 5)
    w = sample_weights(t1, returns=rets, normalize=False)
    assert w[5:].mean() > w[:5].mean()


# ---------------------------------------------------------------------- purging


def test_purge_removes_labels_that_reach_into_the_test_window():
    n, horizon = 200, 20
    t1 = _overlapping(n, horizon)
    train = purged_train_indices(n_samples=n, t1=t1, test_start=100, test_end=120, embargo_frac=0.0)
    # Any train row whose span touches [100,120] must be gone.
    for i in train:
        assert not (t1[i] >= 100 and i <= 120), f"row {i} (t1={t1[i]}) leaks into the test window"


def test_purge_beats_a_fixed_row_distance_embargo():
    """A long-horizon label far from the test set still leaks; row distance misses it."""

    n = 300
    t1 = np.arange(n, dtype=np.int64)
    t1[50] = 160          # row 50's label resolves INSIDE the test window
    train = purged_train_indices(n_samples=n, t1=t1, test_start=150, test_end=170, embargo_frac=0.0)
    assert 50 not in set(train.tolist()), "a distant row with an overlapping label must be purged"


def test_test_window_itself_is_never_training():
    n = 100
    train = set(purged_train_indices(
        n_samples=n, t1=_non_overlapping(n), test_start=40, test_end=50, embargo_frac=0.0
    ).tolist())
    assert train.isdisjoint(set(range(40, 51)))


def test_embargo_drops_rows_after_the_test_window():
    n = 1000
    t1 = _non_overlapping(n)
    no_emb = set(purged_train_indices(n_samples=n, t1=t1, test_start=400, test_end=500, embargo_frac=0.0).tolist())
    with_emb = set(purged_train_indices(n_samples=n, t1=t1, test_start=400, test_end=500, embargo_frac=0.05).tolist())
    assert with_emb < no_emb
    assert 505 in no_emb and 505 not in with_emb


def test_purge_handles_degenerate_windows():
    n = 50
    assert purged_train_indices(n_samples=n, t1=_non_overlapping(n), test_start=10, test_end=5).size == n
    assert purged_train_indices(n_samples=0, t1=[], test_start=0, test_end=0).size == 0


# ---------------------------------------------------------- sequential bootstrap


def test_sequential_bootstrap_returns_requested_size():
    picks = sequential_bootstrap(_overlapping(40, 5), size=25, seed=1)
    assert picks.size == 25
    assert picks.min() >= 0 and picks.max() < 40


def test_sequential_bootstrap_is_more_unique_than_iid():
    """The whole point: draws should overlap less than random draws do."""

    n, horizon = 60, 10
    t1 = _overlapping(n, horizon)
    seq = sequential_bootstrap(t1, size=n, seed=3)
    rng = np.random.default_rng(3)
    iid = rng.integers(0, n, size=n)
    # Distinct draws is a direct proxy for information content.
    assert len(set(seq.tolist())) >= len(set(iid.tolist()))


def test_sequential_bootstrap_deterministic_under_seed():
    a = sequential_bootstrap(_overlapping(30, 4), size=15, seed=9)
    b = sequential_bootstrap(_overlapping(30, 4), size=15, seed=9)
    assert np.array_equal(a, b)


def test_overlap_report_on_unique_labels_is_a_noop():
    rep = overlap_report(_non_overlapping(30))
    assert rep["overstatement_factor"] == pytest.approx(1.0, rel=1e-9)
    assert rep["t_stat_inflation"] == pytest.approx(1.0, rel=1e-9)
