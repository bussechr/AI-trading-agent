"""Selection-bias corrections: deflated Sharpe and probability of overfitting.

The self-improvement loop searches a configuration space and reports the winner.
The winner's Sharpe is therefore a *maximum over trials*, and the maximum of N
noisy draws is upward-biased even when every candidate is worthless. Reporting
that number as if it were a single pre-registered test is the single easiest way
to manufacture an edge that does not exist.

Two complementary corrections:

  * ``deflated_sharpe_ratio`` (Bailey & Lopez de Prado 2014) asks whether the
    observed Sharpe exceeds what the *best of N random trials* would produce,
    while also correcting the Sharpe's own sampling distribution for skew and
    fat tails. It needs an honest ``n_trials``.
  * ``probability_of_backtest_overfitting`` (CSCV) asks a different question that
    needs no trial count: across many symmetric in-sample/out-of-sample splits,
    how often does the in-sample winner land below the out-of-sample median?
    A PBO near 0.5 means selection is indistinguishable from noise.

Both take non-annualized, per-observation Sharpe values. Annualizing before
applying these formulas silently corrupts the variance term, so
``deannualize_sharpe`` is provided and the argument names say ``per_period``.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
from scipy.stats import norm

EULER_MASCHERONI = 0.5772156649015329


def deannualize_sharpe(annualized: float, *, periods_per_year: float) -> float:
    """Convert an annualized Sharpe back to per-observation units."""

    ppy = float(max(periods_per_year, 1e-12))
    return float(annualized) / math.sqrt(ppy)


def expected_max_sharpe(*, n_trials: int, sharpe_variance: float) -> float:
    """E[max Sharpe] over ``n_trials`` independent worthless strategies.

    Uses the Gumbel-limit approximation from Bailey & Lopez de Prado: with
    ``V = Var(SR)`` across trials,
    ``E[max] ~ sqrt(V) * ((1-g) * z(1 - 1/N) + g * z(1 - 1/(N*e)))``.
    """

    n = int(max(int(n_trials), 1))
    var = float(max(sharpe_variance, 0.0))
    if n <= 1 or var <= 0.0:
        return 0.0
    z1 = float(norm.ppf(1.0 - 1.0 / n))
    z2 = float(norm.ppf(1.0 - 1.0 / (n * math.e)))
    out = math.sqrt(var) * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)
    return float(out) if math.isfinite(out) else 0.0


def probabilistic_sharpe_ratio(
    *,
    sharpe_per_period: float,
    benchmark_sharpe_per_period: float,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """P(true Sharpe > benchmark), corrected for skew and fat tails.

    ``PSR = Phi( (SR - SR*) * sqrt(n-1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2) )``
    where ``g4`` is non-excess kurtosis (3.0 for a normal sample).
    """

    n = int(n_obs)
    if n < 3:
        return 0.0
    sr = float(sharpe_per_period)
    benchmark = float(benchmark_sharpe_per_period)
    g3 = float(skew)
    g4 = float(kurtosis)
    variance_term = 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * (sr**2)
    if not math.isfinite(variance_term) or variance_term <= 0.0:
        return 0.0
    numerator = (sr - benchmark) * math.sqrt(float(n - 1))
    z = numerator / math.sqrt(variance_term)
    if not math.isfinite(z):
        return 0.0
    return float(norm.cdf(z))


def deflated_sharpe_ratio(
    *,
    sharpe_per_period: float,
    n_obs: int,
    n_trials: int,
    sharpe_variance_across_trials: float,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> dict[str, float]:
    """Probability the strategy is real once the search is accounted for.

    Interpretation: ``dsr`` is P(true Sharpe > best-of-N-noise). Below ~0.95 the
    result is not distinguishable from the best of a lucky search.
    """

    benchmark = expected_max_sharpe(n_trials=n_trials, sharpe_variance=sharpe_variance_across_trials)
    dsr = probabilistic_sharpe_ratio(
        sharpe_per_period=sharpe_per_period,
        benchmark_sharpe_per_period=benchmark,
        n_obs=n_obs,
        skew=skew,
        kurtosis=kurtosis,
    )
    psr_zero = probabilistic_sharpe_ratio(
        sharpe_per_period=sharpe_per_period,
        benchmark_sharpe_per_period=0.0,
        n_obs=n_obs,
        skew=skew,
        kurtosis=kurtosis,
    )
    return {
        "dsr": float(dsr),
        "psr_vs_zero": float(psr_zero),
        "expected_max_sharpe": float(benchmark),
        "sharpe_per_period": float(sharpe_per_period),
        "n_trials": float(int(n_trials)),
        "n_obs": float(int(n_obs)),
    }


def probability_of_backtest_overfitting(
    returns_matrix: np.ndarray,
    *,
    n_splits: int = 10,
    max_combinations: int = 512,
) -> dict[str, float]:
    """CSCV probability of backtest overfitting.

    ``returns_matrix`` is ``(n_obs, n_configs)`` of per-period returns, one
    column per configuration tried. Rows are split into ``n_splits`` contiguous
    blocks; every balanced train/test assignment of those blocks is evaluated.
    For each split the in-sample best config's out-of-sample relative rank is
    recorded; PBO is the fraction of splits where it falls below the OOS median.

    Contiguous blocks (not random rows) keep each block's serial dependence
    intact, which matters for the same reason the block bootstrap does.
    """

    mat = np.asarray(returns_matrix, dtype=float)
    if mat.ndim != 2 or mat.shape[1] < 2 or mat.shape[0] < 2 * n_splits:
        return {"insufficient_data": 1.0, "pbo": float("nan"), "n_configs": float(mat.shape[1] if mat.ndim == 2 else 0)}

    n_obs, n_configs = mat.shape
    s = int(n_splits)
    if s % 2 != 0:
        s -= 1
    if s < 2:
        return {"insufficient_data": 1.0, "pbo": float("nan"), "n_configs": float(n_configs)}

    bounds = np.linspace(0, n_obs, s + 1, dtype=int)
    blocks = [np.arange(bounds[i], bounds[i + 1], dtype=int) for i in range(s)]

    def _sharpe_cols(rows: np.ndarray) -> np.ndarray:
        sub = mat[rows, :]
        if sub.shape[0] < 2:
            return np.zeros(n_configs, dtype=float)
        mean = sub.mean(axis=0)
        sd = sub.std(axis=0, ddof=1)
        out = np.divide(mean, sd, out=np.zeros_like(mean), where=sd > 0.0)
        return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)

    all_combos = list(combinations(range(s), s // 2))
    if len(all_combos) > int(max_combinations):
        step = max(1, len(all_combos) // int(max_combinations))
        all_combos = all_combos[::step][: int(max_combinations)]

    logits: list[float] = []
    for is_blocks in all_combos:
        is_rows = np.concatenate([blocks[i] for i in is_blocks])
        oos_rows = np.concatenate([blocks[i] for i in range(s) if i not in set(is_blocks)])
        is_perf = _sharpe_cols(is_rows)
        oos_perf = _sharpe_cols(oos_rows)
        best = int(np.argmax(is_perf))
        # Relative rank of the IS winner within the OOS distribution.
        rank = float(np.count_nonzero(oos_perf <= oos_perf[best]))
        omega = rank / float(n_configs + 1)
        omega = min(max(omega, 1.0 / float(n_configs + 1)), 1.0 - 1.0 / float(n_configs + 1))
        logits.append(math.log(omega / (1.0 - omega)))

    if not logits:
        return {"insufficient_data": 1.0, "pbo": float("nan"), "n_configs": float(n_configs)}

    arr = np.asarray(logits, dtype=float)
    return {
        "pbo": float(np.mean(arr < 0.0)),
        "mean_logit": float(np.mean(arr)),
        "median_logit": float(np.median(arr)),
        "n_combinations": float(arr.size),
        "n_splits": float(s),
        "n_configs": float(n_configs),
        "n_obs": float(n_obs),
        "insufficient_data": 0.0,
    }


def sharpe_variance_across_trials(sharpes: np.ndarray | list[float]) -> float:
    """Var(SR) across searched configs -- the ``V`` term for the DSR.

    Must be computed over ALL trials attempted, not the survivors: variance over
    survivors alone understates the search and inflates the deflated Sharpe.
    """

    arr = np.asarray(sharpes, dtype=float).ravel()
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return 0.0
    return float(np.var(arr, ddof=1))
