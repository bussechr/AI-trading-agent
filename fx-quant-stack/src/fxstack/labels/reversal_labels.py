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
    horizon = int(cfg.horizon_bars)
    if horizon < 1:
        raise ValueError("horizon_bars must be at least 1")
    x = df.copy().reset_index(drop=True)
    px = x["mid_close"].astype(float)
    atr = x.get("atr_14", pd.Series(0.0, index=x.index)).astype(float).replace(0.0, pd.NA).ffill().fillna(1e-6)
    side = _infer_side(x)

    failure: list[int] = []
    opportunity: list[int] = []
    timing_quality: list[int] = []

    for i in range(len(x)):
        end = min(len(x), i + horizon + 1)
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

        # FAILURE is an excursion event: the original thesis is invalidated the
        # moment price touches -failure_r at ANY point in the horizon.
        failure_hit = int((future <= cfg.failure_r).any())

        # OPPORTUNITY must be PATH-ORDERED, not excursion-based. It used to be
        # `(opposite >= opportunity_r).any()`, but `opposite == -future`, so with
        # the default symmetric thresholds that predicate is ALGEBRAICALLY
        # IDENTICAL to the failure predicate: opposite >= 1.0 <=> future <= -1.0.
        # Both heads trained on the same (X, y, w), and deterministic XGB then
        # wrote byte-identical model.json files -- the "two independent committee
        # opinions" were one opinion counted twice, in every bundle ever built
        # (verified by md5 across three bundles on 2026-07-31).
        #
        # The distinction that actually matters: FLIPPING PAYS only if the
        # reversed trade reaches its target (opposite >= opportunity_r) BEFORE
        # its own stop (opposite <= failure_r). A path that runs +1.2R in the
        # original direction and then crashes to -1.5R fails the thesis, but the
        # flip's stop (entry +1R in original coordinates) is hit first, so the
        # reversal never pays. Excursion semantics called that an opportunity;
        # first-touch semantics correctly does not.
        opp_target_mask = (opposite >= cfg.opportunity_r).to_numpy()
        opp_stop_mask = (opposite <= cfg.failure_r).to_numpy()
        t_target = int(opp_target_mask.argmax()) if opp_target_mask.any() else -1
        t_stop = int(opp_stop_mask.argmax()) if opp_stop_mask.any() else -1
        opp_hit = int(t_target >= 0 and (t_stop < 0 or t_target < t_stop))
        # Bar offset of the winning target touch, 1-based from the entry bar.
        opp_idx = (t_target + 1) if opp_hit else 0
        timing = int(opp_hit and opp_idx <= int(cfg.timing_window))
        failure.append(failure_hit)
        opportunity.append(opp_hit)
        timing_quality.append(timing)

    x["thesis_failure"] = failure
    x["opposite_opportunity"] = opportunity
    x["reversal_timing_quality"] = timing_quality
    x["sample_weight"] = 1.0 + x.get("spread_bps", pd.Series(0.0, index=x.index)).astype(float).abs()
    return x.iloc[: max(0, len(x) - horizon)].reset_index(drop=True)
