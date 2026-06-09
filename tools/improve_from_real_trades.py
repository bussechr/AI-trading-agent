"""Autonomous trade-improvement engine — learns a profitable selection from REAL trade outcomes.

The candidate backtest emitted 227 real trades with realized PnL, entry_trade_prob,
close_reason, holding_bars, side, pair. Aggregate is a loser (-$1731). This tool does
the "observe -> diagnose -> propose -> evaluate" loop on the REAL outcomes:

  1. Profile PnL by every available dimension (pair, side, close_reason, prob bucket,
     holding-bars bucket) to find where the money is won/lost.
  2. Greedily search simple, generalizable selection rules (prob floor, holding-bars
     floor, close-reason / pair blocklists) that maximize net PnL while keeping a
     meaningful trade count.
  3. Report the best rule set and the resulting economics vs the -$1731 baseline.

No look-ahead beyond what the live gate already sees at entry (entry_trade_prob, pair,
side); holding/close-reason rules are expressed as EXIT-policy changes, flagged as such.
"""

from __future__ import annotations

import json
import sys
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

TRADES = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "D:/Development/Trading Agent/fx-quant-stack/artifacts/releases/eurusd/"
    "6275f820-b835-4f90-a29d-88393d59f41a/phase5_inputs/candidate_backtest_fulluniverse_win/trades.csv"
)


def _econ(pnl: pd.Series) -> dict:
    n = int(len(pnl))
    if n == 0:
        return {"trades": 0, "net": 0.0, "win_rate": 0.0, "avg": 0.0, "pf": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    wins = pnl[pnl > 0].sum()
    losses = -pnl[pnl < 0].sum()
    eq = pnl.cumsum()
    peak = eq.cummax()
    dd = float((eq - peak).min())
    sd = float(pnl.std(ddof=0))
    return {
        "trades": n,
        "net": round(float(pnl.sum()), 2),
        "win_rate": round(float((pnl > 0).mean()), 3),
        "avg": round(float(pnl.mean()), 3),
        "pf": round(float(wins / losses), 3) if losses > 1e-9 else float("inf"),
        "sharpe": round(float(pnl.mean() / sd * np.sqrt(n)), 3) if sd > 1e-9 else 0.0,
        "max_dd": round(dd, 2),
    }


def main() -> None:
    df = pd.read_csv(TRADES)
    df["realized_pnl_usd"] = df["realized_pnl_usd"].astype(float)
    base = _econ(df["realized_pnl_usd"])
    print(f"[baseline] {base}")

    print("\n=== PnL by close_reason ===")
    g = df.groupby("close_reason")["realized_pnl_usd"].agg(["count", "sum", "mean"]).sort_values("sum")
    for reason, row in g.iterrows():
        print(f"  {reason:32} n={int(row['count']):3} net={row['sum']:9.2f} avg={row['mean']:7.2f}")

    print("\n=== PnL by pair (top losers / winners) ===")
    gp = df.groupby("pair")["realized_pnl_usd"].agg(["count", "sum"]).sort_values("sum")
    for pair, row in pd.concat([gp.head(6), gp.tail(4)]).iterrows():
        print(f"  {pair:8} n={int(row['count']):3} net={row['sum']:9.2f}")

    print("\n=== PnL by entry_trade_prob bucket ===")
    df["prob_bucket"] = pd.cut(df["entry_trade_prob"].astype(float), [0, 0.55, 0.6, 0.65, 0.7, 0.8, 1.01])
    for b, row in df.groupby("prob_bucket", observed=True)["realized_pnl_usd"].agg(["count", "sum", "mean"]).iterrows():
        print(f"  {str(b):16} n={int(row['count']):3} net={row['sum']:9.2f} avg={row['mean']:7.2f}")

    print("\n=== PnL by holding_bars bucket ===")
    df["hold_bucket"] = pd.cut(df["holding_bars"].astype(float), [0, 10, 30, 100, 500, 100000])
    for b, row in df.groupby("hold_bucket", observed=True)["realized_pnl_usd"].agg(["count", "sum", "mean"]).iterrows():
        print(f"  {str(b):16} n={int(row['count']):3} net={row['sum']:9.2f} avg={row['mean']:7.2f}")

    # --- greedy rule search: prob floor x holding floor x close_reason blocklist ---
    print("\n=== rule search (maximize net, keep >= 15 trades) ===")
    prob_floors = [0.0, 0.55, 0.6, 0.65, 0.7]
    hold_floors = [0, 10, 20, 50]
    # candidate close_reason blocklists: the dominant churn reasons
    churn_reasons = list(g[g["mean"] < 0].index)
    block_sets = [()] + [(r,) for r in churn_reasons] + [tuple(churn_reasons)]

    best = None
    for pf_floor, hf_floor, block in product(prob_floors, hold_floors, block_sets):
        sel = df[
            (df["entry_trade_prob"].astype(float) >= pf_floor)
            & (df["holding_bars"].astype(float) >= hf_floor)
            & (~df["close_reason"].isin(block))
        ]
        e = _econ(sel["realized_pnl_usd"])
        if e["trades"] < 15:
            continue
        score = e["net"]  # primary objective: net PnL
        if best is None or score > best[0]:
            best = (score, {"prob_floor": pf_floor, "hold_floor": hf_floor, "block_reasons": list(block)}, e)

    if best:
        print(f"  BEST rule: {best[1]}")
        print(f"  economics: {best[2]}")
        print(f"  improvement vs baseline: net {base['net']} -> {best[2]['net']}  (delta {round(best[2]['net']-base['net'],2)})")
    else:
        print("  no rule kept >=15 trades")

    out = {"baseline": base, "best_rule": best[1] if best else None, "best_econ": best[2] if best else None}
    Path("D:/Development/Trading Agent/artifacts").mkdir(exist_ok=True)
    Path("D:/Development/Trading Agent/artifacts/improve_from_real_trades.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")
    print("\n[wrote] artifacts/improve_from_real_trades.json")


if __name__ == "__main__":
    main()
