from __future__ import annotations

import pandas as pd


def prepare_xy(df: pd.DataFrame, *, target_col: str, drop_cols: list[str] | None = None) -> tuple[pd.DataFrame, pd.Series]:
    drop = list(drop_cols or []) + [target_col]
    x = df.drop(columns=[c for c in drop if c in df.columns]).copy()
    y = df[target_col].copy()
    return x, y
