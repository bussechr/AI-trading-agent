from __future__ import annotations

import numpy as np
import pandas as pd

from fxstack.features.fx_lifecycle import add_fx_lifecycle_features


def _safe_pct(x: pd.Series, periods: int = 1) -> pd.Series:
    out = x.pct_change(periods=periods)
    return out.replace([np.inf, -np.inf], np.nan)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return add_fx_lifecycle_features(df)


def leakage_guard(df: pd.DataFrame) -> None:
    if df.empty:
        return
    ts = pd.to_datetime(df["ts"], utc=True)
    if not ts.is_monotonic_increasing:
        raise ValueError("Feature frame timestamps are not strictly increasing")
    if ts.duplicated().any():
        raise ValueError("Feature frame contains duplicate timestamps")
