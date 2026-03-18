from __future__ import annotations

import numpy as np
import pandas as pd


def _safe_pct(x: pd.Series, periods: int = 1) -> pd.Series:
    out = x.pct_change(periods=periods)
    return out.replace([np.inf, -np.inf], np.nan)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    x = df.copy()
    x["ts"] = pd.to_datetime(x["ts"], utc=True)
    x = x.sort_values("ts").reset_index(drop=True)

    px = x["mid_close"].astype(float)
    hi = x["mid_high"].astype(float)
    lo = x["mid_low"].astype(float)

    x["ret_1"] = _safe_pct(px, 1)
    x["ret_5"] = _safe_pct(px, 5)
    x["ret_20"] = _safe_pct(px, 20)

    x["vol_20"] = x["ret_1"].rolling(20).std(ddof=0)
    x["vol_60"] = x["ret_1"].rolling(60).std(ddof=0)

    tr1 = (hi - lo).abs()
    tr2 = (hi - px.shift(1)).abs()
    tr3 = (lo - px.shift(1)).abs()
    x["atr_14"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(14).mean()

    x["trend_slope_20"] = (px - px.shift(20)) / px.shift(20)
    x["trend_slope_60"] = (px - px.shift(60)) / px.shift(60)

    spread = x.get("spread")
    if spread is not None:
        x["spread"] = spread.astype(float)
        x["spread_z20"] = (x["spread"] - x["spread"].rolling(20).mean()) / (x["spread"].rolling(20).std(ddof=0) + 1e-9)
    else:
        x["spread"] = 0.0
        x["spread_z20"] = 0.0

    x["bar_imbalance"] = (x["mid_close"] - x["mid_open"]) / (x["mid_high"] - x["mid_low"] + 1e-9)
    x["pullback_depth_20"] = (x["mid_close"].rolling(20).max() - x["mid_close"]) / (x["mid_close"].rolling(20).max() + 1e-9)

    hour = x["ts"].dt.hour
    x["session_asia"] = ((hour >= 0) & (hour < 8)).astype(int)
    x["session_london"] = ((hour >= 8) & (hour < 16)).astype(int)
    x["session_ny"] = ((hour >= 13) & (hour < 21)).astype(int)

    x = x.dropna().reset_index(drop=True)
    return x


def leakage_guard(df: pd.DataFrame) -> None:
    if df.empty:
        return
    ts = pd.to_datetime(df["ts"], utc=True)
    if not ts.is_monotonic_increasing:
        raise ValueError("Feature frame timestamps are not strictly increasing")
    if ts.duplicated().any():
        raise ValueError("Feature frame contains duplicate timestamps")
