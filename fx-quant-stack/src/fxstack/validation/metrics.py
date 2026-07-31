"""Performance statistics computed one way, for every consumer.

Sharpe, drawdown and expectancy were previously recomputed ad hoc in
``research/vectorbt_harness.py``, ``tools/edge_audit.py`` and
``tools/fxstack_causal_research_backtest.py`` -- three implementations that can
disagree about annualization, degrees of freedom and zero-variance handling.
A resampling test that calls a different Sharpe than the backtest reports is
measuring the wrong thing, so the validation layer owns one definition and the
resampling modules consume it.

Conventions fixed here on purpose:
  * Sharpe uses the sample standard deviation (ddof=1) and no risk-free
    adjustment -- returns are assumed already excess/relative.
  * Zero-variance return series yield 0.0 rather than +/-inf, so a degenerate
    permutation draw cannot dominate a Monte Carlo null distribution.
  * Drawdown is reported as a positive fraction of the running peak.
"""

from __future__ import annotations

import math
from typing import Callable

import numpy as np

# Bars per year for the timeframes this stack trades. FX trades ~24h x 5d.
PERIODS_PER_YEAR: dict[str, float] = {
    "M1": 60.0 * 24.0 * 252.0,
    "M5": 12.0 * 24.0 * 252.0,
    "M15": 4.0 * 24.0 * 252.0,
    "M30": 2.0 * 24.0 * 252.0,
    "H1": 24.0 * 252.0,
    "H4": 6.0 * 252.0,
    "D1": 252.0,
}

DEFAULT_PERIODS_PER_YEAR = 252.0


def _as_returns(values: np.ndarray | list[float]) -> np.ndarray:
    arr = np.asarray(values, dtype=float).ravel()
    if arr.size == 0:
        return arr
    return arr[np.isfinite(arr)]


def sharpe_ratio(returns: np.ndarray | list[float], *, periods_per_year: float = DEFAULT_PERIODS_PER_YEAR) -> float:
    """Annualized Sharpe on per-period returns. 0.0 when undefined."""

    arr = _as_returns(returns)
    if arr.size < 2:
        return 0.0
    sd = float(np.std(arr, ddof=1))
    if not math.isfinite(sd) or sd <= 0.0:
        return 0.0
    mean = float(np.mean(arr))
    scale = math.sqrt(max(float(periods_per_year), 0.0))
    out = (mean / sd) * scale
    return float(out) if math.isfinite(out) else 0.0


def mean_return(returns: np.ndarray | list[float]) -> float:
    """Total-edge statistic that stays finite for degenerate draws."""

    arr = _as_returns(returns)
    return float(np.mean(arr)) if arr.size else 0.0


def total_return(returns: np.ndarray | list[float]) -> float:
    arr = _as_returns(returns)
    return float(np.sum(arr)) if arr.size else 0.0


def equity_curve(returns: np.ndarray | list[float], *, compound: bool = False) -> np.ndarray:
    arr = _as_returns(returns)
    if arr.size == 0:
        return np.asarray([1.0], dtype=float)
    if compound:
        return np.cumprod(1.0 + arr)
    return 1.0 + np.cumsum(arr)


def max_drawdown(returns: np.ndarray | list[float], *, compound: bool = False) -> float:
    """Peak-to-trough decline as a positive fraction (0.12 == 12%)."""

    curve = equity_curve(returns, compound=compound)
    if curve.size == 0:
        return 0.0
    peak = np.maximum.accumulate(curve)
    safe_peak = np.where(np.abs(peak) < 1e-12, 1e-12, peak)
    drawdown = (peak - curve) / np.abs(safe_peak)
    worst = float(np.max(drawdown)) if drawdown.size else 0.0
    return max(0.0, worst) if math.isfinite(worst) else 0.0


def cagr(returns: np.ndarray | list[float], *, periods_per_year: float = DEFAULT_PERIODS_PER_YEAR) -> float:
    arr = _as_returns(returns)
    if arr.size == 0 or periods_per_year <= 0.0:
        return 0.0
    final = float(np.prod(1.0 + arr))
    if final <= 0.0:
        return -1.0
    years = arr.size / float(periods_per_year)
    if years <= 0.0:
        return 0.0
    out = final ** (1.0 / years) - 1.0
    return float(out) if math.isfinite(out) else 0.0


def profit_factor(pnl: np.ndarray | list[float]) -> float:
    arr = _as_returns(pnl)
    if arr.size == 0:
        return 0.0
    gains = float(np.sum(arr[arr > 0.0]))
    losses = float(-np.sum(arr[arr < 0.0]))
    if losses <= 0.0:
        return float("inf") if gains > 0.0 else 0.0
    return gains / losses


def win_rate(pnl: np.ndarray | list[float]) -> float:
    arr = _as_returns(pnl)
    if arr.size == 0:
        return 0.0
    return float(np.count_nonzero(arr > 0.0)) / float(arr.size)


def expectancy(pnl: np.ndarray | list[float]) -> float:
    arr = _as_returns(pnl)
    return float(np.mean(arr)) if arr.size else 0.0


def skewness(returns: np.ndarray | list[float]) -> float:
    arr = _as_returns(returns)
    if arr.size < 3:
        return 0.0
    sd = float(np.std(arr, ddof=1))
    if sd <= 0.0:
        return 0.0
    out = float(np.mean(((arr - np.mean(arr)) / sd) ** 3))
    return out if math.isfinite(out) else 0.0


def kurtosis(returns: np.ndarray | list[float]) -> float:
    """Non-excess (Pearson) kurtosis; 3.0 for a normal sample."""

    arr = _as_returns(returns)
    if arr.size < 4:
        return 3.0
    sd = float(np.std(arr, ddof=1))
    if sd <= 0.0:
        return 3.0
    out = float(np.mean(((arr - np.mean(arr)) / sd) ** 4))
    return out if math.isfinite(out) else 3.0


def summarize(
    returns: np.ndarray | list[float],
    *,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
) -> dict[str, float]:
    arr = _as_returns(returns)
    return {
        "n_obs": float(arr.size),
        "sharpe": sharpe_ratio(arr, periods_per_year=periods_per_year),
        "mean_return": mean_return(arr),
        "total_return": total_return(arr),
        "max_drawdown": max_drawdown(arr),
        "cagr": cagr(arr, periods_per_year=periods_per_year),
        "profit_factor": profit_factor(arr),
        "win_rate": win_rate(arr),
        "skew": skewness(arr),
        "kurtosis": kurtosis(arr),
    }


#: Named statistics usable as a Monte Carlo test statistic.
STATISTICS: dict[str, Callable[[np.ndarray], float]] = {
    "sharpe": sharpe_ratio,
    "mean_return": mean_return,
    "total_return": total_return,
}


def resolve_statistic(name: str) -> Callable[[np.ndarray], float]:
    key = str(name or "").strip().lower()
    if key not in STATISTICS:
        raise ValueError(f"unknown statistic: {name!r} (known: {sorted(STATISTICS)})")
    return STATISTICS[key]
