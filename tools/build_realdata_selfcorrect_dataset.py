"""Build a REAL scored-signals dataset for the self-improvement loop.

Joins model-scored signals (real probabilities on real Dukascopy data, from
`trader backtest full`) with a realized FORWARD return computed from the on-disk
M5 mid-close series (signed by trade side), then reshapes into the loop's
canonical scored-signals schema via `fxstack.improve.dataset_builder`.

This is the "observe on real data" link of the self-correcting loop — no synthetic.
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("D:/Development/Trading Agent/fx-quant-stack")
_RUN = sys.argv[1] if len(sys.argv) > 1 else "realdata_rich"
SIGNALS = ROOT / f"artifacts/reports/backtests/{_RUN}/signals_sample.csv"
FEATURE_ROOT = ROOT / "data/labels"   # feature frames carrying mid_close + ts + pair + timeframe
OUT = ROOT / f"artifacts/reports/backtests/{_RUN}/scored_signals_real.parquet"
FWD_BARS = 12  # 1h forward on M5

sys.path.insert(0, str(ROOT / "src"))


def _norm_ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True, errors="coerce")


def main() -> None:
    ss = pd.read_csv(SIGNALS)
    ss["ts"] = _norm_ts(ss["ts"])
    ss["pair"] = ss["pair"].astype(str).str.upper()
    ss["side"] = ss["side"].astype(str).str.lower()
    pairs = sorted(ss["pair"].unique())
    dmin, dmax = ss["ts"].min(), ss["ts"].max()
    print(f"[realdata] signals rows={len(ss)} pairs={pairs} ts=[{dmin}..{dmax}]")

    all_files = glob.glob(str(FEATURE_ROOT / "**/*.parquet"), recursive=True)
    # keep only M5 files for our pairs, within the signal date window (+2 days for the fwd horizon)
    lo = (dmin.tz_convert(None) - pd.Timedelta(days=2)).date()
    hi = (dmax.tz_convert(None) + pd.Timedelta(days=2)).date()

    def _keep(p: str) -> bool:
        pl = p.replace("\\", "/").lower()
        if "m5" not in pl:
            return False
        if not any(("pair=" + pr.lower()) in pl or ("/" + pr.lower() + "/") in pl for pr in pairs):
            return False
        return True

    sel = [p for p in all_files if _keep(p)]
    print(f"[realdata] candidate M5 feature files for pairs={len(sel)} (of {len(all_files)})")
    frames = []
    for f in sel:
        try:
            d = pd.read_parquet(f, columns=["pair", "ts", "timeframe", "mid_close"])
        except Exception:
            try:
                d = pd.read_parquet(f)
            except Exception:
                continue
            keep = [c for c in ("pair", "ts", "timeframe", "mid_close") if c in d.columns]
            d = d[keep]
        if "mid_close" not in d.columns or "ts" not in d.columns:
            continue
        if "pair" not in d.columns:
            part = next((seg for seg in Path(f).parts if seg.lower().startswith("pair=")), "")
            d["pair"] = part.split("=", 1)[1].upper() if part else "UNKNOWN"
        frames.append(d)
    if not frames:
        raise SystemExit("no M5 feature frames matched; inspect FEATURE_ROOT layout")
    px = pd.concat(frames, ignore_index=True)
    if "timeframe" in px.columns:
        px = px[px["timeframe"].astype(str).str.upper() == "M5"]
    px["pair"] = px["pair"].astype(str).str.upper()
    px["ts"] = _norm_ts(px["ts"])
    px = px.dropna(subset=["ts", "mid_close"]).drop_duplicates(["pair", "ts"]).sort_values(["pair", "ts"])
    px = px[(px["ts"].dt.date >= lo) & (px["ts"].dt.date <= hi)]
    print(f"[realdata] price rows={len(px)}")

    # forward return per pair: mid_close.shift(-H)/mid_close - 1
    px["fwd_ret_raw"] = px.groupby("pair")["mid_close"].transform(lambda s: s.shift(-FWD_BARS) / s - 1.0)
    px = px.dropna(subset=["fwd_ret_raw"])

    merged = ss.merge(px[["pair", "ts", "fwd_ret_raw"]], on=["pair", "ts"], how="inner")
    # sign by trade side (short profits on a fall)
    sgn = np.where(merged["side"].isin(["sell", "short"]), -1.0, 1.0)
    merged["fwd_ret"] = merged["fwd_ret_raw"].astype(float) * sgn
    print(f"[realdata] joined rows={len(merged)} ; fwd_ret(frac) range=({merged['fwd_ret'].min():.6f},{merged['fwd_ret'].max():.6f})")
    if merged.empty:
        raise SystemExit("0 joined rows — ts alignment failed")

    from fxstack.improve.dataset_builder import ColumnMap, build_scored_signals, write_scored_signals

    cmap = ColumnMap(
        swing_prob="swing_prob", entry_prob="entry_prob", trade_prob="trade_prob",
        spread="spread_bps", fwd_ret="fwd_ret", pair="pair", ts="ts",
        expected_edge="expected_edge_bps",
    )
    scored = build_scored_signals(merged, columns=cmap, spread_unit="bps", fwd_ret_unit="fraction")
    info = write_scored_signals(scored, OUT)
    print(f"[realdata] WROTE {info}")
    print(f"[realdata] scored cols={list(scored.columns)} ; mean fwd_ret_bps={scored['fwd_ret_bps'].mean():.3f}")


if __name__ == "__main__":
    main()
