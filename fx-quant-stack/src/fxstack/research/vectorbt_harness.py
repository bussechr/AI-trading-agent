"""Richer research metrics over a scored-signals frame.

This harness mirrors the gate logic used by the improvement loop's evaluator
(:mod:`fxstack.improve.evaluator`) so that "which rows are taken" stays in lock
step with the live objective, then computes a broader battery of research-grade
performance statistics (Sharpe, Sortino, Calmar, profit factor, exposure, ...).

The canonical compute path is pure numpy/pandas and carries no third-party
dependency. If the optional ``vectorbt`` library happens to be importable, an
equity-curve cross-check is *additionally* exposed, but it is never required:
all metrics, determinism, and the public contract hold without it.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from fxstack.improve.evaluator import REQUIRED_COLUMNS, _gate

# Trades-per-period assumed when annualising. The synthetic frame is hourly, but
# the metrics are scale-stable: we annualise Sharpe/Sortino with sqrt(N) (the same
# convention the evaluator uses) and keep Calmar on the per-run total so the values
# stay comparable to the loop's objective without smuggling in a calendar.
_EPS = 1e-9


def _empty_metrics(backend: str) -> dict[str, Any]:
    """Finite, zero-trade metric block (no div-by-zero, all bounded)."""

    return {
        "backend": backend,
        "trades": 0,
        "win_rate": 0.0,
        "total_net_bps": 0.0,
        "mean_net_bps": 0.0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "calmar": 0.0,
        "max_drawdown_pct": 0.0,
        "exposure": 0.0,
        "profit_factor": 0.0,
    }


def _validate(dataset: pd.DataFrame | None) -> pd.DataFrame | None:
    if dataset is None or len(dataset) == 0:
        return None
    missing = [c for c in REQUIRED_COLUMNS if c not in dataset.columns]
    if missing:
        raise ValueError(
            f"scored-signals dataset missing columns {missing}; "
            f"expected {list(REQUIRED_COLUMNS)}"
        )
    return dataset


def _take_mask(config: dict[str, Any], df: pd.DataFrame) -> pd.Series:
    """Replicate the evaluator's gate to decide which rows are *taken*.

    Kept structurally identical to ``evaluate_config`` so the trade count matches
    the loop's objective on any shared dataset.
    """

    g = _gate(config)
    all_in_cost = df["spread_bps"].astype(float) + float(g["slippage_bps"])
    net_edge_signal = df["expected_edge_bps"].astype(float) - all_in_cost
    hurdle = float(g["min_expected_edge_bps"]) - float(g["rescue_margin_bps"])
    return (
        (df["swing_prob"].astype(float) >= g["min_swing_prob"])
        & (df["entry_prob"].astype(float) >= g["min_entry_prob"])
        & (df["trade_prob"].astype(float) >= g["min_trade_prob"])
        & (df["spread_bps"].astype(float) <= g["max_allowed_spread_bps"])
        & (net_edge_signal >= hurdle)
    )


def _equity_curve(net_bps: np.ndarray) -> np.ndarray:
    """Compounding equity on a 100-unit notional (matches the evaluator)."""

    gross_factor = np.clip(1.0 + net_bps / 10000.0, 1e-6, None)
    return 100.0 * np.cumprod(gross_factor)


def _max_drawdown_pct(equity: np.ndarray) -> float:
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    safe_peak = np.where(peak > 0.0, peak, 1.0)
    dd = float(np.max((peak - equity) / safe_peak) * 100.0)
    return dd if np.isfinite(dd) else 0.0


def _vbt_equity_crosscheck(net_bps: np.ndarray) -> dict[str, Any] | None:
    """Optional vectorbt cross-check of the equity curve.

    Returns ``None`` when vectorbt is unavailable (the default in CI). Never
    raises into the caller: a flaky optional backend must not break research.
    """

    try:  # pragma: no cover - exercised only when vectorbt is installed
        import vectorbt as vbt  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - any import failure means "not available"
        return None

    try:  # pragma: no cover - optional path
        returns = net_bps / 10000.0
        acc = vbt.returns.accessors.ReturnsAccessor.from_returns(  # type: ignore[attr-defined]
            pd.Series(returns)
        )
        vbt_total_return = float(acc.total())
        vbt_max_dd = float(abs(acc.max_drawdown()) * 100.0)
        numpy_equity = _equity_curve(net_bps)
        numpy_total_return = float(numpy_equity[-1] / 100.0 - 1.0)
        return {
            "available": True,
            "vbt_total_return": vbt_total_return,
            "vbt_max_drawdown_pct": vbt_max_dd if np.isfinite(vbt_max_dd) else 0.0,
            "numpy_total_return": numpy_total_return,
            "agree": bool(abs(vbt_total_return - numpy_total_return) < 1e-6),
        }
    except Exception:  # noqa: BLE001 - degrade gracefully, never fail research
        return None


def run_vectorbt_research(
    config: dict[str, Any],
    dataset: pd.DataFrame | None,
) -> dict[str, Any]:
    """Compute research metrics for ``config`` over a scored-signals ``dataset``.

    The trade-selection gate is identical to the improvement loop's evaluator, so
    the reported ``trades`` count matches ``evaluate_config`` on the same frame.

    Returns a flat metric dict with a ``backend`` marker (``"numpy"`` by default,
    ``"vectorbt"`` when the optional cross-check ran). Empty or zero-trade frames
    yield finite, bounded zeros rather than NaNs/infs.
    """

    df = _validate(dataset)
    if df is None:
        return _empty_metrics("numpy")

    take = _take_mask(config, df)
    taken = df[take]
    trades = int(len(taken))
    n_total = int(len(df))
    if trades == 0:
        out = _empty_metrics("numpy")
        out["exposure"] = 0.0
        return out

    g = _gate(config)
    realized_cost = taken["spread_bps"].astype(float).to_numpy() + float(g["slippage_bps"])
    net_bps = taken["fwd_ret_bps"].astype(float).to_numpy() - realized_cost

    mean_net = float(np.mean(net_bps))
    total_net = float(np.sum(net_bps))
    wins = net_bps > 0.0
    win_rate = float(np.mean(wins))

    std = float(np.std(net_bps, ddof=0))
    sharpe = float(mean_net / std * np.sqrt(trades)) if std > _EPS else 0.0

    downside = net_bps[net_bps < 0.0]
    downside_std = float(np.sqrt(np.mean(np.square(downside)))) if downside.size else 0.0
    sortino = float(mean_net / downside_std * np.sqrt(trades)) if downside_std > _EPS else 0.0

    equity = _equity_curve(net_bps)
    max_dd = _max_drawdown_pct(equity)
    total_return_pct = float(equity[-1] / 100.0 - 1.0) * 100.0
    calmar = float(total_return_pct / max_dd) if max_dd > _EPS else 0.0

    exposure = float(trades / n_total) if n_total > 0 else 0.0

    gross_profit = float(np.sum(net_bps[wins]))
    gross_loss = float(-np.sum(net_bps[~wins]))
    if gross_loss > _EPS:
        profit_factor = float(gross_profit / gross_loss)
    elif gross_profit > _EPS:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    def _finite(x: float) -> float:
        return x if np.isfinite(x) else 0.0

    metrics: dict[str, Any] = {
        "backend": "numpy",
        "trades": trades,
        "win_rate": _finite(win_rate),
        "total_net_bps": _finite(total_net),
        "mean_net_bps": _finite(mean_net),
        "sharpe": _finite(sharpe),
        "sortino": _finite(sortino),
        "calmar": _finite(calmar),
        "max_drawdown_pct": _finite(max_dd),
        "exposure": _finite(exposure),
        # profit_factor may legitimately be +inf (no losing trades); keep it.
        "profit_factor": profit_factor if np.isfinite(profit_factor) else float("inf"),
    }

    crosscheck = _vbt_equity_crosscheck(net_bps)
    if crosscheck is not None:
        metrics["backend"] = "vectorbt"
        metrics["vbt_crosscheck"] = crosscheck

    return metrics
