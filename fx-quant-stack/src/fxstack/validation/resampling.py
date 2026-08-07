"""Block bootstrap resampling for autocorrelated FX return series.

An i.i.d. bootstrap is the wrong tool here. FX bar returns carry volatility
clustering and short-horizon autocorrelation, and a strategy's equity path
inherits both. Shuffling observation-by-observation destroys that dependence and
produces confidence intervals that are far too tight -- which is exactly the
error that lets a curve-fit strategy look significant.

Two dependence-preserving schemes are provided:

  * ``stationary_bootstrap`` (Politis & Romano 1994) draws geometrically
    distributed block lengths, so the resampled series is strictly stationary
    and no single fixed block length imprints itself on the result.
  * ``circular_block_bootstrap`` uses fixed-length wrap-around blocks, which is
    easier to reason about when comparing against published block-bootstrap
    results.

Both wrap around the end of the series so every observation has equal
probability of selection -- without wrapping, the head and tail are
under-sampled.

Block length: ``optimal_block_length`` uses the AR(1) approximation to the
Politis & White (2004) rule. That approximation is deliberate and documented
rather than hidden: it captures first-order persistence, which dominates for
bar-level FX returns, and callers can always pass an explicit length.
"""

from __future__ import annotations

import math

import numpy as np

MIN_BLOCK = 1
MAX_BLOCK_FRACTION = 0.25  # never let a single block exceed a quarter of the sample


def _clean(values: np.ndarray | list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float).ravel()
    if arr.size == 0:
        return arr
    return arr[np.isfinite(arr)]


def lag1_autocorrelation(values: np.ndarray | list[float]) -> float:
    """First-order autocorrelation, clamped to (-0.99, 0.99)."""

    arr = _clean(values)
    if arr.size < 3:
        return 0.0
    centered = arr - float(np.mean(arr))
    denom = float(np.dot(centered, centered))
    if denom <= 0.0:
        return 0.0
    rho = float(np.dot(centered[:-1], centered[1:]) / denom)
    if not math.isfinite(rho):
        return 0.0
    return float(np.clip(rho, -0.99, 0.99))


def optimal_block_length(values: np.ndarray | list[float]) -> int:
    """AR(1)-approximated Politis-White optimal mean block length.

    For an AR(1) process the optimal block length for variance estimation scales
    as ``n**(1/3)`` inflated by the persistence term ``(2*rho^2/(1-rho^2)^2)``.
    Returns at least 1 and at most ``MAX_BLOCK_FRACTION * n``.
    """

    arr = _clean(values)
    n = int(arr.size)
    if n < 8:
        return MIN_BLOCK
    rho = abs(lag1_autocorrelation(arr))
    if rho <= 1e-6:
        return MIN_BLOCK
    denom = (1.0 - rho**2) ** 2
    if denom <= 1e-12:
        return max(MIN_BLOCK, int(round(n ** (1.0 / 3.0))))
    inflation = (2.0 * rho**2 / denom) ** (1.0 / 3.0)
    block = int(round((n ** (1.0 / 3.0)) * max(inflation, 1e-6)))
    ceiling = max(MIN_BLOCK, int(math.floor(MAX_BLOCK_FRACTION * n)))
    return int(min(max(block, MIN_BLOCK), ceiling))


def _block_index(
    *,
    n: int,
    block_lengths: np.ndarray,
    starts: np.ndarray,
) -> np.ndarray:
    """Concatenate wrap-around blocks into exactly ``n`` indices."""

    if n <= 0:
        return np.zeros(0, dtype=int)
    offsets = [np.arange(int(length), dtype=int) for length in block_lengths]
    pieces = [(int(start) + off) % n for start, off in zip(starts, offsets)]
    if not pieces:
        return np.zeros(0, dtype=int)
    idx = np.concatenate(pieces)
    return idx[:n]


def stationary_bootstrap_indices(
    n: int,
    *,
    mean_block: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Index draw for one Politis-Romano stationary bootstrap replicate."""

    if n <= 0:
        return np.zeros(0, dtype=int)
    mean_b = float(max(mean_block, 1.0))
    p = 1.0 / mean_b
    # Geometric block lengths; draw generously then truncate to n.
    budget = int(n + max(8, int(2.0 * mean_b)))
    lengths = rng.geometric(p, size=budget)
    keep = int(np.searchsorted(np.cumsum(lengths), n) + 1)
    lengths = lengths[: max(keep, 1)]
    starts = rng.integers(0, n, size=lengths.size)
    return _block_index(n=n, block_lengths=lengths, starts=starts)


def circular_block_bootstrap_indices(
    n: int,
    *,
    block_length: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Index draw for one fixed-length circular block bootstrap replicate."""

    if n <= 0:
        return np.zeros(0, dtype=int)
    b = int(min(max(int(block_length), MIN_BLOCK), n))
    n_blocks = int(math.ceil(n / b))
    starts = rng.integers(0, n, size=n_blocks)
    lengths = np.full(n_blocks, b, dtype=int)
    return _block_index(n=n, block_lengths=lengths, starts=starts)


def bootstrap_statistic(
    returns: np.ndarray | list[float],
    statistic,
    *,
    n_resamples: int = 1000,
    method: str = "stationary",
    block_length: int | None = None,
    seed: int = 12345,
) -> dict[str, float | list[float]]:
    """Bootstrap distribution of ``statistic`` over dependence-preserving draws.

    Returns the observed value, the resampled distribution, and percentile
    confidence bounds. The lower 5% bound is the number that matters for
    acceptance: a strategy whose Sharpe CI includes zero has not been shown to
    work, however high its point estimate.
    """

    arr = _clean(returns)
    n = int(arr.size)
    if n < 8:
        return {
            "observed": float(statistic(arr)) if n else 0.0,
            "n_obs": float(n),
            "n_resamples": 0.0,
            "insufficient_data": 1.0,
            "distribution": [],
        }

    mode = str(method or "stationary").strip().lower()
    if mode not in {"stationary", "circular"}:
        raise ValueError(f"unknown bootstrap method: {method!r}")

    b = int(block_length) if block_length else optimal_block_length(arr)
    rng = np.random.default_rng(int(seed))
    draws = np.empty(int(n_resamples), dtype=float)
    for i in range(int(n_resamples)):
        if mode == "stationary":
            idx = stationary_bootstrap_indices(n, mean_block=float(b), rng=rng)
        else:
            idx = circular_block_bootstrap_indices(n, block_length=b, rng=rng)
        value = float(statistic(arr[idx]))
        draws[i] = value if math.isfinite(value) else 0.0

    observed = float(statistic(arr))
    observed = observed if math.isfinite(observed) else 0.0
    return {
        "observed": observed,
        "mean": float(np.mean(draws)),
        "std": float(np.std(draws, ddof=1)) if draws.size > 1 else 0.0,
        "ci_lower_05": float(np.percentile(draws, 5.0)),
        "ci_lower_025": float(np.percentile(draws, 2.5)),
        "median": float(np.percentile(draws, 50.0)),
        "ci_upper_975": float(np.percentile(draws, 97.5)),
        "prob_positive": float(np.mean(draws > 0.0)),
        "block_length": float(b),
        "method": mode,
        "n_obs": float(n),
        "n_resamples": float(draws.size),
        "insufficient_data": 0.0,
        "distribution": [float(x) for x in draws],
    }


def trade_bootstrap(
    trade_pnl: np.ndarray | list[float],
    *,
    n_resamples: int = 2000,
    seed: int = 12345,
) -> dict[str, float]:
    """Trade-level i.i.d. bootstrap for drawdown and risk-of-ruin.

    Independent resampling IS defensible at trade level -- unlike bar returns,
    completed trades are closer to exchangeable -- and randomizing trade ORDER
    is what reveals how much of the observed drawdown was ordering luck.
    """

    arr = _clean(trade_pnl)
    n = int(arr.size)
    if n < 5:
        return {"n_trades": float(n), "insufficient_data": 1.0}

    rng = np.random.default_rng(int(seed))
    worst_dd = np.empty(int(n_resamples), dtype=float)
    finals = np.empty(int(n_resamples), dtype=float)
    for i in range(int(n_resamples)):
        draw = arr[rng.integers(0, n, size=n)]
        curve = np.cumsum(draw)
        peak = np.maximum.accumulate(np.concatenate(([0.0], curve)))[1:]
        worst_dd[i] = float(np.max(peak - curve)) if curve.size else 0.0
        finals[i] = float(curve[-1]) if curve.size else 0.0

    observed_curve = np.cumsum(arr)
    observed_peak = np.maximum.accumulate(np.concatenate(([0.0], observed_curve)))[1:]
    observed_dd = float(np.max(observed_peak - observed_curve)) if observed_curve.size else 0.0
    return {
        "n_trades": float(n),
        "observed_max_drawdown": observed_dd,
        "median_max_drawdown": float(np.percentile(worst_dd, 50.0)),
        "p95_max_drawdown": float(np.percentile(worst_dd, 95.0)),
        "p99_max_drawdown": float(np.percentile(worst_dd, 99.0)),
        "prob_final_negative": float(np.mean(finals <= 0.0)),
        "median_final": float(np.percentile(finals, 50.0)),
        "insufficient_data": 0.0,
    }
