from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(slots=True)
class ReversalLabelConfig:
    horizon_bars: int = 24
    failure_r: float = -1.0
    opportunity_r: float = 1.0
    timing_window: int = 6


def _infer_side(df: pd.DataFrame) -> pd.Series:
    if "side" in df.columns:
        side = df["side"].astype(str).str.lower()
        return side.map({"long": 1.0, "buy": 1.0, "short": -1.0, "sell": -1.0}).fillna(1.0)
    swing = df.get("swing_prob")
    if swing is not None:
        return swing.astype(float).apply(lambda v: 1.0 if v >= 0.5 else -1.0)
    return df.get("ret_1", pd.Series(0.0, index=df.index)).astype(float).apply(lambda v: 1.0 if v >= 0.0 else -1.0)


def build_reversal_labels(df: pd.DataFrame, cfg: ReversalLabelConfig | None = None) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    cfg = cfg or ReversalLabelConfig()
    x = df.copy().reset_index(drop=True)
    px = x["mid_close"].astype(float)
    atr = x.get("atr_14", pd.Series(0.0, index=x.index)).astype(float).replace(0.0, pd.NA).ffill().fillna(1e-6)
    side = _infer_side(x)

    failure: list[int] = []
    opportunity: list[int] = []
    timing_quality: list[int] = []

    for i in range(len(x)):
        end = min(len(x), i + int(cfg.horizon_bars) + 1)
        if end <= i + 1:
            failure.append(0)
            opportunity.append(0)
            timing_quality.append(0)
            continue
        entry = float(px.iloc[i])
        vol = max(float(atr.iloc[i]), 1e-6)
        direction = float(side.iloc[i])
        future = ((px.iloc[i + 1 : end] - entry) * direction) / vol
        opposite = ((px.iloc[i + 1 : end] - entry) * -direction) / vol
        failure_hit = int((future <= cfg.failure_r).any())
        opp_hit = int((opposite >= cfg.opportunity_r).any())
        failure_idx = int((future <= cfg.failure_r).idxmax() - i) if failure_hit else 0
        opp_idx = int((opposite >= cfg.opportunity_r).idxmax() - i) if opp_hit else 0
        timing = int(opp_hit and opp_idx <= int(cfg.timing_window))
        failure.append(failure_hit)
        opportunity.append(opp_hit)
        timing_quality.append(timing)

    x["thesis_failure"] = failure
    x["opposite_opportunity"] = opportunity
    x["reversal_timing_quality"] = timing_quality
    x["sample_weight"] = 1.0 + x.get("spread_bps", pd.Series(0.0, index=x.index)).astype(float).abs()
    return x
