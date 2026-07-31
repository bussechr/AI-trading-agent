"""Summaries for signal-level backtests.

This module previously reported a statistic that could not fail. ``take_trade``
is defined in ``engine.py`` as ``allowed & (net_edge_bps > 0)``, and
``positive_share`` was then computed as ``(take.net_edge_bps > 0).mean()`` over
exactly that subset -- identically 1.0 for every dataset ever passed in. It read
like a hit rate and was consumed as evidence of quality, including in the chain
that supported a live activation.

Two changes fix that:

  * ``positive_share`` is now computed over ALL evaluated candidates, so it
    answers a real question -- what fraction of observed setups carried a
    positive expected net edge -- and can take any value in [0, 1].
  * Every expected-edge statistic is named ``expected_*`` and flagged by
    ``realized_available``. ``net_edge_bps`` is the MODEL'S OWN FORECAST minus an
    assumed cost; it is not a return. When the frame carries a realized outcome
    column, realized statistics are reported alongside and are the only ones
    that should ever gate a promotion.

Nothing here is a substitute for the resampling tests in ``fxstack.validation``:
a positive realized mean over one path says nothing about whether the edge is
distinguishable from luck.
"""

from __future__ import annotations

import math

import pandas as pd

#: Columns that, if present, are treated as a realized per-candidate outcome in
#: basis points. Signed in the direction actually traded.
REALIZED_BPS_COLUMNS = (
    "net_realized_bps",
    "realized_net_bps",
    "realized_edge_bps",
    "realized_bps",
    "fwd_ret_bps",
)

_EMPTY: dict[str, float] = {
    "trades": 0.0,
    "candidates": 0.0,
    "mean_net_edge_bps": 0.0,
    "positive_share": 0.0,
    "realized_available": 0.0,
}


def _first_realized_column(df: pd.DataFrame) -> str | None:
    for name in REALIZED_BPS_COLUMNS:
        if name in df.columns:
            return name
    return None


def _finite_mean(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return 0.0
    out = float(values.mean())
    return out if math.isfinite(out) else 0.0


def summarize_backtest(df: pd.DataFrame) -> dict[str, float]:
    """Summarize scored signals.

    ``mean_net_edge_bps`` and ``positive_share`` describe the model's EXPECTED
    edge. Treat them as a description of the signal distribution, never as
    performance. Check ``realized_available`` before drawing any P&L conclusion.
    """

    if df.empty or "net_edge_bps" not in df.columns:
        return dict(_EMPTY)

    candidates = int(len(df))
    take = df[df["take_trade"] == True] if "take_trade" in df.columns else df.iloc[0:0]

    # Over ALL candidates -- not the subset selected by this very predicate.
    expected_positive_share = float((pd.to_numeric(df["net_edge_bps"], errors="coerce") > 0).mean())
    out: dict[str, float] = {
        "trades": float(len(take)),
        "candidates": float(candidates),
        # Retained key names for existing consumers; both are EXPECTED, not realized.
        "mean_net_edge_bps": _finite_mean(take["net_edge_bps"]) if not take.empty else 0.0,
        "positive_share": expected_positive_share if math.isfinite(expected_positive_share) else 0.0,
        "expected_mean_net_edge_bps_taken": _finite_mean(take["net_edge_bps"]) if not take.empty else 0.0,
        "expected_positive_share_all": expected_positive_share if math.isfinite(expected_positive_share) else 0.0,
        "take_rate": float(len(take)) / float(candidates) if candidates else 0.0,
        "realized_available": 0.0,
    }

    realized_col = _first_realized_column(df)
    if realized_col is None:
        return out

    realized_all = pd.to_numeric(df[realized_col], errors="coerce")
    cost = (
        pd.to_numeric(df["all_in_cost_bps"], errors="coerce").fillna(0.0)
        if "all_in_cost_bps" in df.columns
        else pd.Series(0.0, index=df.index)
    )
    # Only subtract cost when the column is not already net of it.
    realized_net_all = realized_all if realized_col.startswith(("net_", "realized_net")) else realized_all - cost

    taken_mask = (df["take_trade"] == True) if "take_trade" in df.columns else pd.Series(True, index=df.index)
    realized_taken = realized_net_all[taken_mask].dropna()

    out["realized_available"] = 1.0
    out["realized_column"] = 0.0  # placeholder keeps the return type float-only
    out["realized_mean_net_bps_taken"] = float(realized_taken.mean()) if not realized_taken.empty else 0.0
    out["realized_hit_rate_taken"] = (
        float((realized_taken > 0).mean()) if not realized_taken.empty else 0.0
    )
    out["realized_total_net_bps_taken"] = float(realized_taken.sum()) if not realized_taken.empty else 0.0
    out["realized_mean_net_bps_all"] = _finite_mean(realized_net_all)
    # Did selection actually help? If taking trades is no better than taking
    # everything, the gate is not adding information.
    out["realized_selection_uplift_bps"] = float(
        out["realized_mean_net_bps_taken"] - out["realized_mean_net_bps_all"]
    )
    return out
