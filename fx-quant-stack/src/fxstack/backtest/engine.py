from __future__ import annotations

import pandas as pd

from fxstack.backtest.costs import all_in_cost_bps


def evaluate_signals(df: pd.DataFrame, *, slippage_bps: float = 0.25) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["all_in_cost_bps"] = x.apply(
        lambda r: all_in_cost_bps(spread_bps=float(r.get("spread_bps", 0.0)), slippage_bps=float(slippage_bps)),
        axis=1,
    )
    x["net_edge_bps"] = x["expected_edge_bps"].astype(float) - x["all_in_cost_bps"].astype(float)
    x["take_trade"] = (x["allowed"] == True) & (x["net_edge_bps"] > 0)
    return x
