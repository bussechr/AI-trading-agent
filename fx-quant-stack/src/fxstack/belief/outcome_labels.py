"""# AGENT: ROLE: Action-conditioned outcome labels for directional-belief v2 hypothesis rows.
# AGENT: ENTRYPOINT: `label_hypothesis_outcomes()`.
# AGENT: PRIMARY INPUTS: normalized candidate rows and source feature frame with forward prices.
# AGENT: PRIMARY OUTPUTS: relevance, net EV, confirm-success, fail-fast, MFE, MAE labels.
# AGENT: DEPENDS ON: pandas, numpy, backtest cost helpers.
# AGENT: CALLED BY: `fxstack/belief/dataset.py` and belief-v2 training.
# AGENT: STATE / SIDE EFFECTS: pure label generation only.
# AGENT: HANDSHAKES: shared label kernel for twin benchmark and training artifact metadata.
# AGENT: SEE: `fxstack/belief/candidate_builder.py` -> `fxstack/training/belief.py` -> `docs/agents/model-stack-and-feature-flow.md`"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from fxstack.backtest.costs import all_in_cost_bps

SCENARIO_WINDOWS = {
    "trend_pullback": {"confirm_window": 3, "eval_horizon": 12, "confirm_mult": 0.35, "fail_fast_mult": 0.30},
    "range_mean_reversion": {"confirm_window": 2, "eval_horizon": 6, "confirm_mult": 0.25, "fail_fast_mult": 0.25},
    "breakout_expansion": {"confirm_window": 2, "eval_horizon": 8, "confirm_mult": 0.40, "fail_fast_mult": 0.30},
    "failed_breakout_reversal": {"confirm_window": 3, "eval_horizon": 6, "confirm_mult": 0.35, "fail_fast_mult": 0.30},
}
MAX_EVAL_HORIZON = max(int(cfg["eval_horizon"]) for cfg in SCENARIO_WINDOWS.values())


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _side_return_bps_series(mid: pd.Series, *, horizon: int) -> pd.Series:
    horizon = int(max(1, horizon))
    mid = pd.to_numeric(mid, errors="coerce").replace(0.0, np.nan)
    return (((mid.shift(-horizon) / mid) - 1.0) * 10000.0).fillna(0.0).astype(float)


def label_hypothesis_outcomes(
    candidates: pd.DataFrame,
    *,
    base_frame: pd.DataFrame,
    slippage_bps: float = 0.25,
    min_expected_edge_bps: float = 3.0,
) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    source = base_frame.sort_values("ts").reset_index(drop=True).copy()
    if "row_idx" not in candidates.columns:
        raise ValueError("candidate rows must include row_idx for outcome labeling")
    mid = pd.to_numeric(source.get("mid_close"), errors="coerce").replace(0.0, np.nan)
    if mid.isna().all():
        raise RuntimeError("outcome label kernel requires mid_close")
    spread = pd.to_numeric(source.get("spread_bps"), errors="coerce").fillna(0.0).astype(float)
    vol_ref_bps = np.maximum(
        np.maximum(pd.to_numeric(source.get("vol_20"), errors="coerce").fillna(0.0).abs(), pd.to_numeric(source.get("vol_60"), errors="coerce").fillna(0.0).abs()) * 10000.0,
        2.0,
    )
    base_ret = {h: _side_return_bps_series(mid, horizon=h).to_numpy(dtype=float) for h in range(1, MAX_EVAL_HORIZON + 1)}

    labeled = candidates.copy().reset_index(drop=True)
    row_idx = pd.to_numeric(labeled["row_idx"], errors="coerce").fillna(-1).astype(int).to_numpy(dtype=int)
    valid_idx = (row_idx >= 0) & (row_idx < len(source))
    side_sign = np.where(labeled["side"].astype(str).str.lower().eq("short"), -1.0, 1.0)
    scenario = labeled["scenario"].astype(str).to_numpy(dtype=object)
    spread_at_row = np.where(valid_idx, spread.to_numpy(dtype=float)[np.clip(row_idx, 0, len(source) - 1)], 0.0)
    vol_ref_at_row = np.where(valid_idx, np.asarray(vol_ref_bps, dtype=float)[np.clip(row_idx, 0, len(source) - 1)], 2.0)
    all_in_cost = np.array([all_in_cost_bps(spread_bps=float(sp), slippage_bps=float(slippage_bps)) for sp in spread_at_row], dtype=float)

    net_ev = np.zeros(len(labeled), dtype=float)
    confirm_success = np.zeros(len(labeled), dtype=int)
    fail_fast = np.zeros(len(labeled), dtype=int)
    mfe = np.zeros(len(labeled), dtype=float)
    mae = np.zeros(len(labeled), dtype=float)

    for scenario_name, cfg in SCENARIO_WINDOWS.items():
        mask = scenario == scenario_name
        if not bool(mask.any()):
            continue
        idx = np.where(mask)[0]
        eval_h = int(cfg["eval_horizon"])
        confirm_w = int(cfg["confirm_window"])
        confirm_thresh = float(cfg["confirm_mult"]) * vol_ref_at_row[idx]
        fail_thresh = float(cfg["fail_fast_mult"]) * vol_ref_at_row[idx]
        eval_moves = np.array([side_sign[idx] * base_ret[eval_h][np.clip(row_idx[idx], 0, len(source) - 1)]], dtype=float).reshape(-1)
        net_ev[idx] = eval_moves - all_in_cost[idx]

        first_confirm = np.full(len(idx), np.inf, dtype=float)
        first_fail = np.full(len(idx), np.inf, dtype=float)
        max_favor = np.full(len(idx), -np.inf, dtype=float)
        max_adverse = np.full(len(idx), -np.inf, dtype=float)
        for horizon in range(1, eval_h + 1):
            move = side_sign[idx] * base_ret[horizon][np.clip(row_idx[idx], 0, len(source) - 1)]
            max_favor = np.maximum(max_favor, move)
            max_adverse = np.maximum(max_adverse, -move)
            if horizon <= confirm_w:
                hit_confirm = (move >= confirm_thresh) & np.isinf(first_confirm)
                hit_fail = (move <= (-fail_thresh)) & np.isinf(first_fail)
                first_confirm = np.where(hit_confirm, horizon, first_confirm)
                first_fail = np.where(hit_fail, horizon, first_fail)
        confirm_success[idx] = ((first_confirm < first_fail) & np.isfinite(first_confirm)).astype(int)
        fail_fast[idx] = ((first_fail < first_confirm) & np.isfinite(first_fail)).astype(int)
        mfe[idx] = np.where(np.isfinite(max_favor), max_favor, 0.0)
        mae[idx] = np.where(np.isfinite(max_adverse), max_adverse, 0.0)

    relevance = np.zeros(len(labeled), dtype=int)
    relevance = np.where((net_ev >= 12.0) & (confirm_success == 1) & (fail_fast == 0), 4, relevance)
    relevance = np.where((relevance == 0) & (net_ev >= 6.0) & (fail_fast == 0), 3, relevance)
    relevance = np.where((relevance == 0) & (net_ev >= 3.0), 2, relevance)
    relevance = np.where((relevance == 0) & (net_ev > 0.0) & (net_ev < 3.0), 1, relevance)

    labeled["all_in_cost_bps"] = all_in_cost.astype(float)
    labeled["net_ev_bps"] = net_ev.astype(float)
    labeled["confirm_success"] = confirm_success.astype(int)
    labeled["fail_fast"] = fail_fast.astype(int)
    labeled["mfe_bps"] = mfe.astype(float)
    labeled["mae_bps"] = mae.astype(float)
    labeled["relevance"] = relevance.astype(int)
    labeled["ev_above_hurdle"] = (labeled["net_ev_bps"] >= float(min_expected_edge_bps)).astype(int)
    return labeled
