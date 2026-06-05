"""Deterministic backtest evaluation for candidate configs.

Operates over a *scored-signals* dataset (one row per candidate entry with its
swing/entry/trade probabilities, expected edge, spread, and the realized forward
return). The live scorer can emit such a parquet; for offline runs and CI a
deterministic synthetic generator stands in, with a realistic structure where
stronger signals carry more edge -- so tightening gates trades volume for quality
and the loop has a genuine landscape to climb.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.backtest.costs import all_in_cost_bps

REQUIRED_COLUMNS = (
    "swing_prob",
    "entry_prob",
    "trade_prob",
    "expected_edge_bps",
    "spread_bps",
    "fwd_ret_bps",
)


def build_synthetic_dataset(
    *,
    rows: int = 4000,
    seed: int = 1729,
    pairs: tuple[str, ...] = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD"),
) -> pd.DataFrame:
    """Generate a deterministic scored-signals frame with signal->edge structure."""

    rng = np.random.default_rng(int(seed))
    n = int(max(1, rows))
    swing = np.clip(rng.beta(5.0, 4.0, n), 0.0, 1.0)
    entry = np.clip(0.5 * swing + 0.5 * rng.beta(5.0, 4.0, n), 0.0, 1.0)
    trade = np.clip(0.4 * entry + 0.6 * rng.beta(5.0, 4.0, n), 0.0, 1.0)
    # Composite signal strength drives the *mean* of the realized forward return.
    strength = (swing + entry + trade) / 3.0
    expected_edge = 2.0 + 12.0 * (strength - 0.5) + rng.normal(0.0, 1.5, n)
    spread = np.clip(0.8 + rng.gamma(2.0, 0.6, n), 0.1, 8.0)
    # Realized return: edge signal plus heavy noise (markets are mostly noise).
    fwd = 10.0 * (strength - 0.5) + rng.normal(0.0, 14.0, n)
    pair_col = np.array(pairs)[rng.integers(0, len(pairs), n)]
    ts = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "ts": ts,
            "pair": pair_col,
            "swing_prob": swing,
            "entry_prob": entry,
            "trade_prob": trade,
            "expected_edge_bps": expected_edge.astype(float),
            "spread_bps": spread.astype(float),
            "fwd_ret_bps": fwd.astype(float),
        }
    )


def load_parquet_dataset(path: str | Path) -> pd.DataFrame:
    """Load a pre-scored signals parquet/dir. Validates required columns."""

    p = Path(path)
    if p.is_dir():
        frames = [pd.read_parquet(f) for f in sorted(p.glob("**/*.parquet"))]
        if not frames:
            raise FileNotFoundError(f"no parquet files under {p}")
        df = pd.concat(frames, ignore_index=True)
    else:
        df = pd.read_parquet(p)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"scored-signals dataset {p} missing columns {missing}; "
            "emit swing/entry/trade probs + expected_edge_bps + spread_bps + fwd_ret_bps"
        )
    return df


def _gate(config: dict[str, Any]) -> dict[str, float]:
    gates = dict(config.get("gates") or {})
    cost = dict(config.get("cost_model") or {})
    return {
        "min_swing_prob": float(gates.get("min_swing_prob", 0.58)),
        "min_entry_prob": float(gates.get("min_entry_prob", 0.62)),
        "min_trade_prob": float(gates.get("min_trade_prob", 0.60)),
        "min_expected_edge_bps": float(gates.get("min_expected_edge_bps", 3.0)),
        "rescue_margin_bps": float(gates.get("min_expected_edge_rescue_margin_bps", 0.5)),
        "max_allowed_spread_bps": float(gates.get("max_allowed_spread_bps", 3.0)),
        "slippage_bps": float(cost.get("slippage_bps", 0.25)),
    }


def evaluate_config(config: dict[str, Any], dataset: pd.DataFrame) -> dict[str, float]:
    """Apply the config's gates to ``dataset`` and return backtest metrics."""

    if dataset is None or dataset.empty:
        return {"trades": 0.0, "win_rate": 0.0, "mean_net_bps": 0.0,
                "total_net_bps": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0}

    g = _gate(config)
    df = dataset
    all_in_cost = df["spread_bps"].astype(float) + float(g["slippage_bps"])
    net_edge_signal = df["expected_edge_bps"].astype(float) - all_in_cost
    hurdle = float(g["min_expected_edge_bps"]) - float(g["rescue_margin_bps"])

    take = (
        (df["swing_prob"].astype(float) >= g["min_swing_prob"])
        & (df["entry_prob"].astype(float) >= g["min_entry_prob"])
        & (df["trade_prob"].astype(float) >= g["min_trade_prob"])
        & (df["spread_bps"].astype(float) <= g["max_allowed_spread_bps"])
        & (net_edge_signal >= hurdle)
    )

    taken = df[take]
    trades = int(len(taken))
    if trades == 0:
        return {"trades": 0.0, "win_rate": 0.0, "mean_net_bps": 0.0,
                "total_net_bps": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0}

    realized_cost = taken["spread_bps"].astype(float).to_numpy() + float(g["slippage_bps"])
    net_bps = taken["fwd_ret_bps"].astype(float).to_numpy() - realized_cost

    mean_net = float(np.mean(net_bps))
    total_net = float(np.sum(net_bps))
    std = float(np.std(net_bps, ddof=0))
    sharpe = float(mean_net / std * np.sqrt(trades)) if std > 1e-9 else 0.0
    win_rate = float(np.mean(net_bps > 0.0))

    # Compounding equity curve on a 100-unit notional for a realistic drawdown.
    equity = 100.0 * np.cumprod(1.0 + net_bps / 10000.0)
    peak = np.maximum.accumulate(equity)
    drawdown_pct = float(np.max((peak - equity) / peak) * 100.0) if len(equity) else 0.0

    return {
        "trades": float(trades),
        "win_rate": win_rate,
        "mean_net_bps": mean_net,
        "total_net_bps": total_net,
        "sharpe": sharpe,
        "max_drawdown_pct": drawdown_pct,
    }


def cost_of(spread_bps: float, slippage_bps: float) -> float:
    """Thin wrapper around the shared cost model (kept for parity/testing)."""

    return float(all_in_cost_bps(spread_bps=float(spread_bps), slippage_bps=float(slippage_bps)))
