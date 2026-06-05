"""Bridge real scored signals into the loop's evaluation dataset.

The improvement loop's evaluator consumes a scored-signals frame
(swing/entry/trade probabilities + expected_edge_bps + spread_bps + fwd_ret_bps).
The live scorer / belief stack already produces per-bar probabilities and an edge
estimate; this converts such a frame (whatever its column names) into the canonical
schema, with explicit, testable unit handling and clear errors when a required
column is missing. No model is run here -- it only reshapes existing columns.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.improve.evaluator import REQUIRED_COLUMNS


@dataclass(slots=True)
class ColumnMap:
    swing_prob: str = "swing_prob"
    entry_prob: str = "entry_prob"
    trade_prob: str = "trade_prob"
    spread: str = "spread"
    fwd_ret: str = "fwd_ret"
    pair: str = "pair"
    ts: str = "ts"
    expected_edge: str | None = None  # optional; derived from trade_prob when absent


def _to_bps(series: pd.Series, unit: str) -> pd.Series:
    unit = str(unit or "fraction").lower()
    values = series.astype(float)
    if unit in {"fraction", "frac", "ratio"}:
        return values * 10000.0
    if unit in {"bps", "bp"}:
        return values
    if unit in {"pct", "percent"}:
        return values * 100.0
    raise ValueError(f"unknown unit {unit!r}; use fraction|bps|pct")


def build_scored_signals(
    df: pd.DataFrame,
    *,
    columns: ColumnMap | None = None,
    spread_unit: str = "fraction",
    fwd_ret_unit: str = "fraction",
    edge_scale_bps: float = 12.0,
) -> pd.DataFrame:
    """Convert a scored feature frame into the canonical scored-signals schema."""

    cols = columns or ColumnMap()
    required = [cols.swing_prob, cols.entry_prob, cols.trade_prob, cols.spread, cols.fwd_ret]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"input frame missing columns {missing}; provide a ColumnMap to remap")

    out = pd.DataFrame()
    out["ts"] = df[cols.ts] if cols.ts in df.columns else pd.RangeIndex(len(df))
    out["pair"] = df[cols.pair].astype(str) if cols.pair in df.columns else "UNKNOWN"
    out["swing_prob"] = df[cols.swing_prob].astype(float).clip(0.0, 1.0)
    out["entry_prob"] = df[cols.entry_prob].astype(float).clip(0.0, 1.0)
    out["trade_prob"] = df[cols.trade_prob].astype(float).clip(0.0, 1.0)
    out["spread_bps"] = _to_bps(df[cols.spread], spread_unit).clip(lower=0.0)
    out["fwd_ret_bps"] = _to_bps(df[cols.fwd_ret], fwd_ret_unit)

    if cols.expected_edge and cols.expected_edge in df.columns:
        out["expected_edge_bps"] = _to_bps(df[cols.expected_edge], "bps").astype(float)
    else:
        # Ex-ante edge proxy from the trade probability (documented stand-in when the
        # scorer does not emit an explicit edge): centred at the 0.5 coin-flip.
        out["expected_edge_bps"] = (out["trade_prob"] - 0.5) * float(edge_scale_bps)

    # Canonical column order; guarantees evaluator.REQUIRED_COLUMNS are all present.
    ordered = ["ts", "pair", *list(REQUIRED_COLUMNS)]
    out = out[[c for c in ordered if c in out.columns]]
    out = out.replace([np.inf, -np.inf], np.nan).dropna(subset=list(REQUIRED_COLUMNS))
    return out.reset_index(drop=True)


def build_from_parquet(
    source: str | Path,
    *,
    columns: ColumnMap | None = None,
    spread_unit: str = "fraction",
    fwd_ret_unit: str = "fraction",
    edge_scale_bps: float = 12.0,
) -> pd.DataFrame:
    src = Path(source)
    if src.is_dir():
        frames = [pd.read_parquet(f) for f in sorted(src.glob("**/*.parquet"))]
        if not frames:
            raise FileNotFoundError(f"no parquet files under {src}")
        df = pd.concat(frames, ignore_index=True)
    else:
        df = pd.read_parquet(src)
    return build_scored_signals(
        df, columns=columns, spread_unit=spread_unit, fwd_ret_unit=fwd_ret_unit, edge_scale_bps=edge_scale_bps,
    )


def write_scored_signals(frame: pd.DataFrame, out_path: str | Path) -> dict[str, Any]:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(out)
    return {"ok": True, "path": str(out), "rows": int(len(frame)), "pairs": sorted(frame["pair"].unique().tolist())}
