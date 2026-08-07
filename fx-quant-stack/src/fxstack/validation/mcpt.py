"""Monte Carlo permutation tests for trade timing.

The question this answers is narrow and important: *does the signal know when to
be in the market, or would any signal with the same shape have done as well?*

Choosing the null hypothesis correctly is the whole game, and the obvious choice
is wrong. Shuffling returns i.i.d. destroys volatility clustering and
autocorrelation, so the permuted world is easier to trade than the real one and
every strategy looks significant. Shuffling the signal i.i.d. destroys holding
periods and trade count, so the permuted strategy pays a completely different
amount of spread and is not comparable.

The null used here is a **random circular rotation of the position series
against the return series**. It preserves, exactly:

  * the realized return path, including vol clustering and fat tails;
  * the position series' own structure -- trade count, holding periods,
    long/short balance, and therefore transaction costs.

Only the *alignment* between signal and market is randomized. That isolates
timing skill, which is the only thing a directional strategy can claim.

A rotation test has a finite permutation universe of ``n - 1`` non-trivial
shifts, so when the requested replicate count meets or exceeds that, all shifts
are enumerated and the p-value is exact rather than sampled.

p-values use the add-one (Davison-Hinkley) estimator ``(1 + #{stat >= obs}) /
(1 + n)``: it can never report 0.0, which would be an unearned claim of
impossibility.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

from fxstack.validation.metrics import resolve_statistic, sharpe_ratio


def _clean_pair(positions: np.ndarray | list[float], returns: np.ndarray | list[float]) -> tuple[np.ndarray, np.ndarray]:
    pos = np.asarray(positions, dtype=float).ravel()
    ret = np.asarray(returns, dtype=float).ravel()
    size = int(min(pos.size, ret.size))
    pos, ret = pos[:size], ret[:size]
    if size == 0:
        return pos, ret
    mask = np.isfinite(pos) & np.isfinite(ret)
    return pos[mask], ret[mask]


def strategy_returns(
    positions: np.ndarray | list[float],
    returns: np.ndarray | list[float],
    *,
    cost_per_turn: float = 0.0,
) -> np.ndarray:
    """Per-period strategy returns with optional per-turnover cost.

    ALIGNMENT CONTRACT -- read before use. ``positions[i]`` is multiplied by
    ``returns[i]``, so the caller is responsible for having already lagged the
    position series. The position held during bar ``i`` must have been decided
    from information available at or before ``i-1``; passing a position derived
    from bar ``i``'s own close is lookahead bias and will produce a spectacular,
    entirely fictional edge that this test suite cannot detect for you -- a
    permutation test validates timing skill given the alignment it is handed, it
    cannot know your alignment was cheating.

    ``cost_per_turn`` is charged on absolute position change, so a flat->long
    entry and the later long->flat exit are each billed. Passing the real
    round-trip spread here is what makes a permutation test honest: a
    cost-blind test rewards hyperactive signals that could never be traded.
    """

    pos, ret = _clean_pair(positions, returns)
    if pos.size == 0:
        return np.zeros(0, dtype=float)
    gross = pos * ret
    cost = float(max(cost_per_turn, 0.0))
    if cost <= 0.0:
        return gross
    turnover = np.abs(np.diff(np.concatenate(([0.0], pos))))
    return gross - turnover * cost


def rotation_permutation_test(
    positions: np.ndarray | list[float],
    returns: np.ndarray | list[float],
    *,
    statistic: str | Callable[[np.ndarray], float] = "sharpe",
    n_permutations: int = 1000,
    cost_per_turn: float = 0.0,
    seed: int = 12345,
) -> dict[str, float]:
    """p-value for "this signal's timing beats a randomly aligned signal"."""

    pos, ret = _clean_pair(positions, returns)
    n = int(pos.size)
    stat_fn = resolve_statistic(statistic) if isinstance(statistic, str) else statistic

    if n < 16:
        return {"n_obs": float(n), "insufficient_data": 1.0, "p_value": 1.0}

    observed_series = strategy_returns(pos, ret, cost_per_turn=cost_per_turn)
    observed = float(stat_fn(observed_series))
    observed = observed if math.isfinite(observed) else 0.0

    available = n - 1  # exclude the identity shift
    exact = int(n_permutations) >= available
    if exact:
        shifts = np.arange(1, n, dtype=int)
    else:
        rng = np.random.default_rng(int(seed))
        shifts = rng.choice(np.arange(1, n, dtype=int), size=int(n_permutations), replace=False)

    draws = np.empty(shifts.size, dtype=float)
    for i, shift in enumerate(shifts):
        rotated = np.roll(pos, int(shift))
        value = float(stat_fn(strategy_returns(rotated, ret, cost_per_turn=cost_per_turn)))
        draws[i] = value if math.isfinite(value) else 0.0

    at_least_as_good = int(np.count_nonzero(draws >= observed))
    p_value = (1.0 + at_least_as_good) / (1.0 + float(draws.size))
    null_sd = float(np.std(draws, ddof=1)) if draws.size > 1 else 0.0
    return {
        "observed": observed,
        "p_value": float(min(max(p_value, 0.0), 1.0)),
        "null_mean": float(np.mean(draws)),
        "null_std": null_sd,
        "null_p95": float(np.percentile(draws, 95.0)),
        "z_score": float((observed - float(np.mean(draws))) / null_sd) if null_sd > 0.0 else 0.0,
        "n_permutations": float(draws.size),
        "exact": 1.0 if exact else 0.0,
        "n_obs": float(n),
        "cost_per_turn": float(cost_per_turn),
        "insufficient_data": 0.0,
    }


def block_permutation_test(
    positions: np.ndarray | list[float],
    returns: np.ndarray | list[float],
    *,
    block_length: int = 24,
    statistic: str | Callable[[np.ndarray], float] = "sharpe",
    n_permutations: int = 1000,
    cost_per_turn: float = 0.0,
    seed: int = 12345,
) -> dict[str, float]:
    """Permute contiguous position blocks instead of rotating.

    Rotation preserves the signal's global ordering; block permutation breaks it
    while still preserving local runs. Reporting both guards against a signal
    that scores well only because of one lucky sustained trend.
    """

    pos, ret = _clean_pair(positions, returns)
    n = int(pos.size)
    stat_fn = resolve_statistic(statistic) if isinstance(statistic, str) else statistic
    if n < 16:
        return {"n_obs": float(n), "insufficient_data": 1.0, "p_value": 1.0}

    b = int(min(max(int(block_length), 1), max(n // 2, 1)))
    observed = float(stat_fn(strategy_returns(pos, ret, cost_per_turn=cost_per_turn)))
    observed = observed if math.isfinite(observed) else 0.0

    starts = np.arange(0, n, b, dtype=int)
    blocks = [pos[s : s + b] for s in starts]
    rng = np.random.default_rng(int(seed))
    draws = np.empty(int(n_permutations), dtype=float)
    for i in range(int(n_permutations)):
        order = rng.permutation(len(blocks))
        shuffled = np.concatenate([blocks[j] for j in order])[:n]
        value = float(stat_fn(strategy_returns(shuffled, ret, cost_per_turn=cost_per_turn)))
        draws[i] = value if math.isfinite(value) else 0.0

    at_least_as_good = int(np.count_nonzero(draws >= observed))
    p_value = (1.0 + at_least_as_good) / (1.0 + float(draws.size))
    return {
        "observed": observed,
        "p_value": float(min(max(p_value, 0.0), 1.0)),
        "null_mean": float(np.mean(draws)),
        "null_std": float(np.std(draws, ddof=1)) if draws.size > 1 else 0.0,
        "block_length": float(b),
        "n_permutations": float(draws.size),
        "n_obs": float(n),
        "insufficient_data": 0.0,
    }


def cost_stress_curve(
    positions: np.ndarray | list[float],
    returns: np.ndarray | list[float],
    *,
    base_cost_per_turn: float,
    multipliers: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0),
    periods_per_year: float = 252.0,
) -> dict[str, float]:
    """Sharpe as transaction costs are scaled up.

    In retail FX the round-trip spread is frequently the same order of magnitude
    as the per-trade edge, so an edge that evaporates at 1.5x quoted spread is
    not tradeable -- slippage and widening at news alone will exceed that.
    """

    pos, ret = _clean_pair(positions, returns)
    out: dict[str, float] = {"base_cost_per_turn": float(base_cost_per_turn)}
    survives = True
    for mult in multipliers:
        cost = float(base_cost_per_turn) * float(mult)
        series = strategy_returns(pos, ret, cost_per_turn=cost)
        sr = sharpe_ratio(series, periods_per_year=periods_per_year)
        out[f"sharpe_x{mult:g}"] = float(sr)
        out[f"total_return_x{mult:g}"] = float(np.sum(series)) if series.size else 0.0
        if mult <= 2.0 and sr <= 0.0:
            survives = False
    out["survives_2x_costs"] = 1.0 if survives else 0.0
    return out
