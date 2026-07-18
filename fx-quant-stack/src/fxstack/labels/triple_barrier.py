from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(slots=True)
class TripleBarrierConfig:
    horizon_bars: int
    tp_atr_mult: float
    sl_atr_mult: float


def triple_barrier_labels(df: pd.DataFrame, cfg: TripleBarrierConfig) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    horizon = int(cfg.horizon_bars)
    if horizon < 1:
        raise ValueError("horizon_bars must be at least 1")

    x = df.copy().reset_index(drop=True)
    px = x["mid_close"].astype(float).to_numpy()
    atr = x["atr_14"].astype(float).to_numpy()

    labels = np.zeros(len(x), dtype=int)
    t1_idx = np.full(len(x), -1, dtype=int)

    for i in range(len(x)):
        entry = px[i]
        if i + horizon >= len(x):
            break
        vol = max(float(atr[i]), 1e-6)
        tp = entry + cfg.tp_atr_mult * vol
        sl = entry - cfg.sl_atr_mult * vol

        end = min(len(x), i + horizon + 1)
        path = px[i + 1 : end]
        hit = 0
        hit_idx = end - 1
        for j, p in enumerate(path, start=i + 1):
            if p >= tp:
                hit = 1
                hit_idx = j
                break
            if p <= sl:
                hit = -1
                hit_idx = j
                break

        labels[i] = hit
        t1_idx[i] = hit_idx

    out = x[["pair", "ts", "timeframe", "mid_close"]].copy()
    out["label"] = labels
    out["t1_index"] = t1_idx
    # Rows in the final horizon do not yet have a fully observable outcome.
    # Dropping them prevents future-unknown samples from being trained as flat.
    return out.iloc[: max(0, len(out) - horizon)].reset_index(drop=True)
