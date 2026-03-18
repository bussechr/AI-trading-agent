from __future__ import annotations

import pandas as pd


def summarize_backtest(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return {"trades": 0, "mean_net_edge_bps": 0.0, "positive_share": 0.0}
    take = df[df["take_trade"] == True]
    if take.empty:
        return {"trades": 0, "mean_net_edge_bps": 0.0, "positive_share": 0.0}
    mean_edge = float(take["net_edge_bps"].mean())
    pos = float((take["net_edge_bps"] > 0).mean())
    return {"trades": float(len(take)), "mean_net_edge_bps": mean_edge, "positive_share": pos}
