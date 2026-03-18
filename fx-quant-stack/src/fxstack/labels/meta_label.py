from __future__ import annotations

import pandas as pd


def build_meta_labels(candidate_df: pd.DataFrame, pnl_col: str = "realized_edge_bps") -> pd.DataFrame:
    if candidate_df.empty:
        return pd.DataFrame()
    x = candidate_df.copy()
    x["meta_label"] = (x[pnl_col].astype(float) > 0.0).astype(int)
    return x
