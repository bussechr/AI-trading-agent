from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def timeframe_to_timedelta(timeframe: str) -> pd.Timedelta:
    mapping = {
        "M1": pd.Timedelta(minutes=1),
        "M5": pd.Timedelta(minutes=5),
        "M15": pd.Timedelta(minutes=15),
        "M30": pd.Timedelta(minutes=30),
        "H1": pd.Timedelta(hours=1),
        "H4": pd.Timedelta(hours=4),
        "D": pd.Timedelta(days=1),
    }
    key = str(timeframe or "").strip().upper()
    if key not in mapping:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    return mapping[key]


def infer_pip_size(pair: str) -> float:
    return 0.01 if str(pair).upper().endswith("JPY") else 0.0001


def _safe_pct(x: pd.Series, periods: int = 1) -> pd.Series:
    out = x.pct_change(periods=periods)
    return out.replace([np.inf, -np.inf], np.nan)


def _session_tag(ts: pd.Series) -> pd.Series:
    hour = pd.to_datetime(ts, utc=True).dt.hour
    out = pd.Series(index=ts.index, dtype="object")
    out.loc[(hour >= 0) & (hour < 8)] = "asia"
    out.loc[(hour >= 8) & (hour < 13)] = "london_open"
    out.loc[(hour >= 13) & (hour < 16)] = "ny_overlap"
    out.loc[(hour >= 16) & (hour < 21)] = "ny"
    out.loc[(hour >= 21) | (hour < 0)] = "rollover"
    return out.fillna("other")


def _regime_bucket(trend: pd.Series, vol_fast: pd.Series, vol_slow: pd.Series) -> pd.Series:
    ratio = (vol_fast / (vol_slow.abs() + 1e-9)).fillna(0.0)
    out = pd.Series("range", index=trend.index, dtype="object")
    out.loc[(trend.abs() >= 0.0015) & (ratio < 1.2)] = "trend"
    out.loc[(ratio >= 1.2) & (trend.abs() < 0.0015)] = "vol_expansion"
    out.loc[(ratio >= 1.5) & (trend.abs() >= 0.0015)] = "stress"
    return out


def infer_scenario_bucket(row: pd.Series | dict[str, Any]) -> str:
    src = row if isinstance(row, pd.Series) else pd.Series(dict(row or {}))
    session = str(src.get("session_tag", "other"))
    regime = str(src.get("regime_bucket", "range"))
    spread_bps = float(src.get("spread_bps", 0.0) or 0.0)
    if spread_bps >= 3.0:
        return "high_spread_stress"
    if session == "rollover":
        return "rollover_spread_shock"
    if regime == "stress":
        return "volatility_expansion"
    if regime == "trend":
        return "trend_continuation"
    if regime == "vol_expansion":
        return "breakout_initiation"
    if session == "asia":
        return "asia_low_liquidity"
    if session == "ny_overlap":
        return "ny_overlap"
    if session == "london_open":
        return "london_open"
    return "range_mean_reversion"


def add_fx_lifecycle_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    x = df.copy()
    x["ts"] = pd.to_datetime(x["ts"], utc=True)
    x = x.sort_values("ts").reset_index(drop=True)

    pair = str(x.iloc[0].get("pair", ""))
    pip_size = infer_pip_size(pair)

    px = x["mid_close"].astype(float)
    hi = x["mid_high"].astype(float)
    lo = x["mid_low"].astype(float)
    opn = x["mid_open"].astype(float)
    spread_px = x.get("spread", pd.Series(0.0, index=x.index)).astype(float)

    x["ret_1"] = _safe_pct(px, 1)
    x["ret_5"] = _safe_pct(px, 5)
    x["ret_20"] = _safe_pct(px, 20)
    x["ret_60"] = _safe_pct(px, 60)
    x["vol_20"] = x["ret_1"].rolling(20, min_periods=5).std(ddof=0)
    x["vol_60"] = x["ret_1"].rolling(60, min_periods=10).std(ddof=0)
    x["vol_term_ratio"] = x["vol_20"] / (x["vol_60"] + 1e-9)

    tr1 = (hi - lo).abs()
    tr2 = (hi - px.shift(1)).abs()
    tr3 = (lo - px.shift(1)).abs()
    x["atr_14"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).rolling(14, min_periods=5).mean()

    baseline_20 = px.shift(20).fillna(px.expanding(min_periods=2).mean())
    baseline_60 = px.shift(60).fillna(px.expanding(min_periods=2).mean())
    x["trend_slope_20"] = (px - baseline_20) / (baseline_20 + 1e-9)
    x["trend_slope_60"] = (px - baseline_60) / (baseline_60 + 1e-9)
    x["trend_strength_20"] = (px - px.rolling(20, min_periods=5).mean()) / (x["atr_14"] + 1e-9)
    x["trend_strength_60"] = (px - px.rolling(60, min_periods=10).mean()) / (x["atr_14"] + 1e-9)

    x["spread"] = spread_px
    x["spread_bps"] = (spread_px / (px.abs() + 1e-9)) * 10000.0
    x["normalized_spread"] = spread_px / (x["atr_14"] + 1e-9)
    x["spread_pips"] = spread_px / max(pip_size, 1e-9)
    x["spread_z20"] = (x["spread"] - x["spread"].rolling(20, min_periods=5).mean()) / (
        x["spread"].rolling(20, min_periods=5).std(ddof=0) + 1e-9
    )

    x["bar_imbalance"] = (px - opn) / ((hi - lo) + 1e-9)
    x["pullback_depth_20"] = (px.rolling(20, min_periods=5).max() - px) / (px.rolling(20, min_periods=5).max() + 1e-9)
    x["carry_proxy"] = (px / (px.rolling(1440, min_periods=60).mean() + 1e-9)) - 1.0
    x["mae_proxy_12"] = (px.rolling(12, min_periods=4).min() - px) / (x["atr_14"] + 1e-9)
    x["mfe_proxy_12"] = (px.rolling(12, min_periods=4).max() - px) / (x["atr_14"] + 1e-9)
    x["edge_decay_12"] = x["ret_1"].rolling(12, min_periods=4).mean() - x["ret_1"].rolling(48, min_periods=12).mean()
    x["vol_burst_12"] = x["ret_1"].abs().rolling(12, min_periods=4).mean() / (
        x["ret_1"].abs().rolling(48, min_periods=12).mean() + 1e-9
    )

    x["session_tag"] = _session_tag(x["ts"])
    x["session_asia"] = (x["session_tag"] == "asia").astype(int)
    x["session_london"] = x["session_tag"].isin({"london_open", "ny_overlap"}).astype(int)
    x["session_ny"] = x["session_tag"].isin({"ny_overlap", "ny"}).astype(int)
    x["session_rollover"] = (x["session_tag"] == "rollover").astype(int)

    x["regime_bucket"] = _regime_bucket(x["trend_slope_60"], x["vol_20"], x["vol_60"])
    x["scenario_bucket"] = x.apply(infer_scenario_bucket, axis=1)

    # Placeholder live-state features default to flat/offline-safe values.
    x["time_in_trade_bars"] = 0.0
    x["open_position_count"] = 0.0
    x["live_edge_decay"] = x["edge_decay_12"].fillna(0.0)
    x["micro_pressure"] = ((px - lo) - (hi - px)) / ((hi - lo) + 1e-9)

    x["close_ts"] = x["ts"] + timeframe_to_timedelta(str(x.iloc[0].get("timeframe", "M5")))
    x = x.replace([np.inf, -np.inf], np.nan)
    required_cols = ["ret_1", "vol_20", "vol_60", "atr_14", "trend_slope_20", "trend_slope_60"]
    x = x.dropna(subset=[c for c in required_cols if c in x.columns]).reset_index(drop=True)
    fill_zero_cols = [
        "carry_proxy",
        "mae_proxy_12",
        "mfe_proxy_12",
        "edge_decay_12",
        "vol_burst_12",
        "spread_z20",
        "normalized_spread",
    ]
    for col in fill_zero_cols:
        if col in x.columns:
            x[col] = x[col].fillna(0.0)
    return x.reset_index(drop=True)
