"""Label overlap: concurrency, average uniqueness, purging, sequential bootstrap.

Every statistic this stack uses to decide what is "better" assumes its rows are
independent observations. They are not. A triple-barrier label spans from its
entry bar to its ``t1`` (barrier touch or horizon), and consecutive labels overlap
heavily -- on bar-level FX data a 24-bar horizon means ~24 labels are alive at any
moment, all reading overlapping future returns. Treating N overlapping rows as N
independent draws overstates the effective sample size by roughly the average
concurrency, which inflates every t-statistic, shrinks every confidence interval,
and is the mechanism by which a search promotes noise.

The stack already computes what is needed and throws it away: ``t1_index`` is
written at ``labels/triple_barrier.py:57`` and then explicitly DROPPED in all
three training paths (``tasks.py:695``, ``scripts/train_intraday_xgb.py:37``,
``scripts/train_swing_xgb.py:33``). This module consumes it.

Three uses, following Lopez de Prado (Advances in Financial Machine Learning):

  * ``average_uniqueness`` -> per-sample weights, so overlapping labels stop
    voting multiple times;
  * ``purged_train_indices`` -> cross-validation that drops training rows whose
    label span overlaps the test window (a fixed-percentage embargo does not:
    it purges by row count, not by how long the label actually lives);
  * ``sequential_bootstrap`` -> resampling that prefers low-overlap draws, so a
    bootstrap confidence interval reflects the real information content.

``t1`` is expressed as POSITIONAL end indices, matching ``t1_index``: entry ``i``
has its label resolved at bar ``t1[i]``, inclusive.
"""

from __future__ import annotations

import numpy as np


def _clean_t1(t1: np.ndarray | list[int], n: int) -> np.ndarray:
    """Positional label ends, clipped into range and never before their start."""

    arr = np.asarray(t1, dtype=float).ravel()
    out = np.empty(arr.size, dtype=np.int64)
    for i in range(arr.size):
        value = arr[i]
        if not np.isfinite(value):
            out[i] = i          # unresolved label spans only its own bar
        else:
            out[i] = int(value)
    out = np.clip(out, 0, max(n - 1, 0))
    starts = np.arange(arr.size, dtype=np.int64)
    return np.maximum(out, np.minimum(starts, max(n - 1, 0)))


def num_concurrent_events(t1: np.ndarray | list[int], *, n_bars: int | None = None) -> np.ndarray:
    """Number of labels alive at each bar.

    Computed with a difference array rather than a per-label loop over its span,
    so it stays linear in bars regardless of horizon length.
    """

    t1_arr = np.asarray(t1, dtype=float).ravel()
    n = int(n_bars) if n_bars is not None else int(t1_arr.size)
    if n <= 0:
        return np.zeros(0, dtype=np.int64)
    ends = _clean_t1(t1_arr, n)
    delta = np.zeros(n + 1, dtype=np.int64)
    for i in range(ends.size):
        start = min(i, n - 1)
        delta[start] += 1
        delta[int(ends[i]) + 1] -= 1
    return np.cumsum(delta)[:n]


def average_uniqueness(t1: np.ndarray | list[int], *, n_bars: int | None = None) -> np.ndarray:
    """Per-label average uniqueness in (0, 1].

    A label alone over its whole span scores 1.0. A label sharing every bar with
    k-1 others scores ~1/k. This is the weight a sample deserves.
    """

    t1_arr = np.asarray(t1, dtype=float).ravel()
    n = int(n_bars) if n_bars is not None else int(t1_arr.size)
    if t1_arr.size == 0 or n <= 0:
        return np.zeros(0, dtype=float)
    ends = _clean_t1(t1_arr, n)
    concurrency = num_concurrent_events(t1_arr, n_bars=n).astype(float)
    safe = np.where(concurrency > 0.0, concurrency, 1.0)
    inv = 1.0 / safe
    cumulative = np.concatenate(([0.0], np.cumsum(inv)))
    out = np.empty(ends.size, dtype=float)
    for i in range(ends.size):
        start = min(i, n - 1)
        stop = int(ends[i])
        span = stop - start + 1
        out[i] = (cumulative[stop + 1] - cumulative[start]) / float(span) if span > 0 else 1.0
    return np.clip(out, 1e-12, 1.0)


def effective_sample_size(t1: np.ndarray | list[int], *, n_bars: int | None = None) -> float:
    """Sum of average uniqueness -- the honest N for any t-statistic.

    Compare against ``len(t1)``. The ratio is how badly a naive statistic on this
    label set overstates its own significance.
    """

    weights = average_uniqueness(t1, n_bars=n_bars)
    return float(weights.sum()) if weights.size else 0.0


def sample_weights(
    t1: np.ndarray | list[int],
    *,
    n_bars: int | None = None,
    returns: np.ndarray | list[float] | None = None,
    normalize: bool = True,
) -> np.ndarray:
    """Training weights from uniqueness, optionally scaled by label magnitude.

    Magnitude scaling (``returns``) is the second half of the standard recipe:
    a label whose realized move was large carries more information than one that
    barely moved. Weights are normalized to mean 1.0 so they do not change the
    effective learning rate of a gradient booster.
    """

    weights = average_uniqueness(t1, n_bars=n_bars)
    if weights.size == 0:
        return weights
    if returns is not None:
        mag = np.abs(np.asarray(returns, dtype=float).ravel()[: weights.size])
        mag = np.where(np.isfinite(mag), mag, 0.0)
        if mag.sum() > 0.0:
            weights = weights * mag
    if normalize:
        mean = float(weights.mean())
        if mean > 0.0:
            weights = weights / mean
    return weights


def purged_train_indices(
    *,
    n_samples: int,
    t1: np.ndarray | list[int],
    test_start: int,
    test_end: int,
    embargo_frac: float = 0.01,
) -> np.ndarray:
    """Training indices with t1-overlap purged and a forward embargo applied.

    A training sample leaks if its label span reaches into the test window, even
    if the sample itself sits far before it. Purging by ROW DISTANCE -- a fixed
    percentage embargo, which is what this repo does -- cannot see that: a
    long-horizon label two hundred rows before the test set still overlaps it.
    Purging by ``t1`` can.

    The embargo additionally drops rows just AFTER the test window, where serial
    correlation still carries test-period information backwards into training.
    """

    n = int(n_samples)
    if n <= 0:
        return np.zeros(0, dtype=np.int64)
    lo = max(0, int(test_start))
    hi = min(n - 1, int(test_end))
    if hi < lo:
        return np.arange(n, dtype=np.int64)

    ends = _clean_t1(np.asarray(t1, dtype=float).ravel(), n)
    embargo = int(max(0.0, float(embargo_frac)) * n)
    embargo_end = min(n - 1, hi + embargo)

    starts = np.arange(min(ends.size, n), dtype=np.int64)
    span_ends = ends[: starts.size]
    # Overlap iff the label span [i, t1_i] intersects the test window [lo, hi].
    overlaps = (span_ends >= lo) & (starts <= hi)
    in_embargo = (starts > hi) & (starts <= embargo_end)
    drop = overlaps | in_embargo

    keep = np.ones(n, dtype=bool)
    keep[: starts.size] = ~drop
    keep[lo : hi + 1] = False  # the test window itself is never training
    return np.flatnonzero(keep).astype(np.int64)


def sequential_bootstrap(
    t1: np.ndarray | list[int],
    *,
    size: int | None = None,
    n_bars: int | None = None,
    seed: int = 12345,
) -> np.ndarray:
    """Draw indices favouring samples that overlap little with those already drawn.

    A plain i.i.d. bootstrap over overlapping labels re-draws the same underlying
    information repeatedly, so its confidence intervals are too tight. This
    reweights after every draw by how much uniqueness remains, which is the
    correction that makes a bootstrap CI on overlapping labels meaningful.
    """

    t1_arr = np.asarray(t1, dtype=float).ravel()
    n_labels = int(t1_arr.size)
    if n_labels == 0:
        return np.zeros(0, dtype=np.int64)
    n = int(n_bars) if n_bars is not None else n_labels
    draws = int(size) if size is not None else n_labels
    ends = _clean_t1(t1_arr, n)
    rng = np.random.default_rng(int(seed))

    concurrency = np.zeros(n, dtype=float)
    picked = np.empty(draws, dtype=np.int64)
    for k in range(draws):
        avg = np.empty(n_labels, dtype=float)
        for i in range(n_labels):
            start, stop = min(i, n - 1), int(ends[i])
            window = concurrency[start : stop + 1] + 1.0
            avg[i] = float(np.mean(1.0 / window)) if window.size else 1.0
        total = float(avg.sum())
        probs = avg / total if total > 0.0 else np.full(n_labels, 1.0 / n_labels)
        choice = int(rng.choice(n_labels, p=probs))
        picked[k] = choice
        start, stop = min(choice, n - 1), int(ends[choice])
        concurrency[start : stop + 1] += 1.0
    return picked


def overlap_report(t1: np.ndarray | list[int], *, n_bars: int | None = None) -> dict[str, float]:
    """Diagnostic: how much independence does this label set actually have?"""

    t1_arr = np.asarray(t1, dtype=float).ravel()
    n_labels = int(t1_arr.size)
    if n_labels == 0:
        return {"n_labels": 0.0, "effective_n": 0.0, "overstatement_factor": 1.0}
    weights = average_uniqueness(t1_arr, n_bars=n_bars)
    concurrency = num_concurrent_events(t1_arr, n_bars=n_bars)
    eff = float(weights.sum())
    return {
        "n_labels": float(n_labels),
        "effective_n": eff,
        "overstatement_factor": float(n_labels / eff) if eff > 0.0 else float("inf"),
        "mean_uniqueness": float(weights.mean()),
        "min_uniqueness": float(weights.min()),
        "mean_concurrency": float(concurrency.mean()) if concurrency.size else 0.0,
        "max_concurrency": float(concurrency.max()) if concurrency.size else 0.0,
        # A t-stat computed on nominal N is inflated by ~sqrt(N/effective_N).
        "t_stat_inflation": float(np.sqrt(n_labels / eff)) if eff > 0.0 else float("inf"),
    }
