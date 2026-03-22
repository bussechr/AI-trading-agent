from __future__ import annotations

import pandas as pd


def build_meta_labels(
    candidate_df: pd.DataFrame,
    pnl_col: str = "realized_edge_bps",
    *,
    cost_stress_levels: tuple[float, ...] = (1.0, 1.25, 1.5),
) -> pd.DataFrame:
    if candidate_df.empty:
        return pd.DataFrame()
    x = candidate_df.copy()
    base_pnl = x[pnl_col].astype(float)
    spread_cost = x.get("spread_bps", pd.Series(0.0, index=x.index)).astype(float)
    x["realized_edge_after_costs"] = base_pnl
    stress_cols: list[str] = []
    for level in cost_stress_levels:
        suffix = str(level).replace(".", "_")
        col = f"realized_edge_after_costs_{suffix}"
        x[col] = base_pnl - (spread_cost * max(float(level) - 1.0, 0.0))
        stress_cols.append(col)
    x["meta_label"] = (x["realized_edge_after_costs"] > 0.0).astype(int)
    x["meta_label_stressed"] = (x[stress_cols].min(axis=1) > 0.0).astype(int)
    vol = x.get("vol_20", pd.Series(0.0, index=x.index)).astype(float).abs()
    spread = spread_cost.abs()
    mae_proxy = x.get("mae_proxy_12", pd.Series(0.0, index=x.index)).astype(float).abs()
    x["sample_weight"] = (1.0 + spread + vol.fillna(0.0) + mae_proxy.fillna(0.0)).clip(lower=0.1)
    return x
