from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


SCENARIO_LABELS = [
    "trend_pullback",
    "range_mean_reversion",
    "breakout_expansion",
    "failed_breakout_reversal",
    "no_edge",
]
SCENARIO_TO_ID = {label: idx for idx, label in enumerate(SCENARIO_LABELS)}


def _safe_series(df: pd.DataFrame, key: str, default: float = 0.0) -> pd.Series:
    if key in df.columns:
        return pd.to_numeric(df[key], errors="coerce").fillna(default).astype(float)
    return pd.Series(default, index=df.index, dtype=float)


def build_directional_belief_labels(
    df: pd.DataFrame,
    *,
    short_horizon_bars: int = 3,
    trade_horizon_bars: int = 12,
    structural_horizon_bars: int = 48,
) -> pd.DataFrame:
    frame = df.sort_values("ts").reset_index(drop=True).copy()
    mid = pd.to_numeric(frame.get("mid_close"), errors="coerce").replace(0.0, np.nan)
    frame["future_ret_short"] = (mid.shift(-int(short_horizon_bars)) / mid) - 1.0
    frame["future_ret_trade"] = (mid.shift(-int(trade_horizon_bars)) / mid) - 1.0
    frame["future_ret_structural"] = (mid.shift(-int(structural_horizon_bars)) / mid) - 1.0
    frame["up_short"] = (frame["future_ret_short"] > 0.0).astype(float)
    frame["up_trade"] = (frame["future_ret_trade"] > 0.0).astype(float)
    frame["up_structural"] = (frame["future_ret_structural"] > 0.0).astype(float)

    regime = frame.get("regime_bucket", pd.Series("", index=frame.index)).astype(str)
    scenario_bucket = frame.get("scenario_bucket", pd.Series("", index=frame.index)).astype(str)
    abs_short = frame["future_ret_short"].abs().fillna(0.0)
    abs_trade = frame["future_ret_trade"].abs().fillna(0.0)
    abs_struct = frame["future_ret_structural"].abs().fillna(0.0)
    sign_short = np.sign(frame["future_ret_short"].fillna(0.0))
    sign_trade = np.sign(frame["future_ret_trade"].fillna(0.0))
    sign_struct = np.sign(frame["future_ret_structural"].fillna(0.0))
    vol_term = _safe_series(frame, "vol_term_ratio", 1.0)
    trend_strength = _safe_series(frame, "trend_strength_20", 0.0).abs()
    extension_penalty = ((trend_strength - 1.0) / 2.0).clip(lower=0.0, upper=1.0)

    label = pd.Series("no_edge", index=frame.index, dtype="object")
    hostile = regime.eq("stress") | scenario_bucket.isin(["high_spread_stress", "rollover_spread_shock"])
    label.loc[hostile] = "no_edge"

    breakout_like = scenario_bucket.isin(["breakout_initiation", "volatility_expansion"])
    breakout_mask = (~hostile) & breakout_like & (sign_short == sign_trade) & (abs_short >= 0.0006) & (abs_trade >= 0.0010)
    label.loc[breakout_mask] = "breakout_expansion"

    trend_mask = (
        (~hostile)
        & regime.eq("trend")
        & (sign_trade == sign_struct)
        & (abs_trade >= 0.0009)
        & (abs_struct >= 0.0015)
        & (extension_penalty < 0.85)
    )
    label.loc[trend_mask] = "trend_pullback"

    range_mask = (
        (~hostile)
        & regime.eq("range")
        & scenario_bucket.isin(["range_mean_reversion", "asia_low_liquidity"])
        & (sign_short != sign_struct)
        & (abs_short >= 0.0004)
        & (abs_struct <= 0.0012)
    )
    label.loc[range_mask] = "range_mean_reversion"

    reversal_mask = (
        (~hostile)
        & (scenario_bucket.isin(["breakout_initiation", "volatility_expansion"]) | regime.eq("trend"))
        & (sign_short != sign_trade)
        & (abs_short >= 0.0005)
        & (abs_trade >= 0.0008)
    )
    label.loc[reversal_mask] = "failed_breakout_reversal"

    no_edge_mask = (
        (sign_short == 0.0)
        | (sign_trade == 0.0)
        | (abs_short < 0.00025)
        | ((abs_trade < 0.0004) & (abs_struct < 0.0006))
        | (vol_term > 3.0)
    )
    label.loc[no_edge_mask] = "no_edge"

    frame["belief_scenario"] = label
    frame["belief_scenario_id"] = frame["belief_scenario"].map(SCENARIO_TO_ID).fillna(SCENARIO_TO_ID["no_edge"]).astype(int)
    return frame
