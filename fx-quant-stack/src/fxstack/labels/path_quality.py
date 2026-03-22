from __future__ import annotations

import pandas as pd


def build_path_quality_labels(
    df: pd.DataFrame,
    *,
    mae_col: str = "mae_r",
    mfe_col: str = "mfe_r",
    outcome_col: str = "realized_r",
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    x = df.copy()
    mae = x.get(mae_col, pd.Series(0.0, index=x.index)).astype(float)
    mfe = x.get(mfe_col, pd.Series(0.0, index=x.index)).astype(float)
    out = x.get(outcome_col, pd.Series(0.0, index=x.index)).astype(float)

    x["good_entry"] = ((mfe >= 1.0) & (mae > -0.5)).astype(int)
    x["bad_hold"] = ((out < 0.0) & (mfe >= 1.0)).astype(int)
    x["bad_exit"] = ((out > 0.0) & (mfe - out >= 0.75)).astype(int)
    x["false_reversal"] = ((mae <= -1.0) & (out > 0.0)).astype(int)
    return x
