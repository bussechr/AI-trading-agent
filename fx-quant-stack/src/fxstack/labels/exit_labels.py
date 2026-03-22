from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from fxstack.labels.path_quality import build_path_quality_labels


EXIT_ACTIONS = ["hold", "reduce", "partial_tp", "tighten_stop", "exit"]
EXIT_ACTION_TO_ID = {name: idx for idx, name in enumerate(EXIT_ACTIONS)}


@dataclass(slots=True)
class ExitLabelConfig:
    horizon_bars: int = 24
    partial_tp_r: float = 1.5
    tighten_stop_r: float = 1.0
    reduce_drawdown_r: float = -0.75
    exit_drawdown_r: float = -1.0


def _infer_side(df: pd.DataFrame) -> pd.Series:
    if "side" in df.columns:
        side = df["side"].astype(str).str.lower()
        return side.map({"long": 1.0, "buy": 1.0, "short": -1.0, "sell": -1.0}).fillna(1.0)
    swing = df.get("swing_prob")
    if swing is not None:
        return swing.astype(float).apply(lambda v: 1.0 if v >= 0.5 else -1.0)
    return df.get("ret_1", pd.Series(0.0, index=df.index)).astype(float).apply(lambda v: 1.0 if v >= 0.0 else -1.0)


def build_exit_labels(df: pd.DataFrame, cfg: ExitLabelConfig | None = None, *, method: str = "trade_outcome") -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    cfg = cfg or ExitLabelConfig()
    x = df.copy().reset_index(drop=True)
    px = x["mid_close"].astype(float)
    hi = x["mid_high"].astype(float)
    lo = x["mid_low"].astype(float)
    atr = x.get("atr_14", pd.Series(0.0, index=x.index)).astype(float).replace(0.0, pd.NA).ffill().fillna(1e-6)
    side = _infer_side(x)

    actions: list[str] = []
    realized_r: list[float] = []
    mae_r: list[float] = []
    mfe_r: list[float] = []
    time_to_best: list[int] = []

    for i in range(len(x)):
        end = min(len(x), i + int(cfg.horizon_bars) + 1)
        if end <= i + 1:
            actions.append("hold")
            realized_r.append(0.0)
            mae_r.append(0.0)
            mfe_r.append(0.0)
            time_to_best.append(0)
            continue

        entry = float(px.iloc[i])
        vol = max(float(atr.iloc[i]), 1e-6)
        direction = float(side.iloc[i])
        future_close = ((px.iloc[i + 1 : end] - entry) * direction) / vol
        future_high = ((hi.iloc[i + 1 : end] - entry) * direction) / vol
        future_low = ((lo.iloc[i + 1 : end] - entry) * direction) / vol
        path = pd.concat([future_close, future_high, future_low], axis=1)
        path["best"] = path.max(axis=1)
        path["worst"] = path.min(axis=1)

        best = float(path["best"].max())
        worst = float(path["worst"].min())
        final = float(future_close.iloc[-1]) if not future_close.empty else 0.0
        best_idx = int(path["best"].idxmax() - i) if not path.empty else 0

        if method == "fixed_horizon":
            action = "partial_tp" if best >= cfg.partial_tp_r else "exit" if final <= cfg.exit_drawdown_r else "hold"
        elif method == "trailing_window":
            action = "tighten_stop" if best >= cfg.tighten_stop_r else "reduce" if worst <= cfg.reduce_drawdown_r else "hold"
        else:
            if worst <= cfg.exit_drawdown_r:
                action = "exit"
            elif best >= cfg.partial_tp_r:
                action = "partial_tp"
            elif best >= cfg.tighten_stop_r:
                action = "tighten_stop"
            elif worst <= cfg.reduce_drawdown_r or final < 0.0:
                action = "reduce"
            else:
                action = "hold"

        actions.append(action)
        realized_r.append(final)
        mae_r.append(worst)
        mfe_r.append(best)
        time_to_best.append(max(best_idx, 0))

    x["exit_action"] = actions
    x["exit_action_id"] = x["exit_action"].map(EXIT_ACTION_TO_ID).astype(int)
    x["realized_r"] = realized_r
    x["mae_r"] = mae_r
    x["mfe_r"] = mfe_r
    x["time_to_best_bars"] = time_to_best
    x["sample_weight"] = 1.0 + x["spread_bps"].astype(float).abs().fillna(0.0) + x["vol_20"].astype(float).abs().fillna(0.0)
    return build_path_quality_labels(x)
