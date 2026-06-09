"""Evaluate step of the autonomous loop: compare digital-twin backtest runs.

Reads each run's aggregate.json (+ trades.csv if present) and prints the economics
that matter for the economic gate (net PnL, win rate, profit factor, max DD) plus
the pnl_by_close_reason breakdown, so a config A/B is judged at a glance.

Usage:
  python tools/compare_twin_runs.py <run_dir_1> [<run_dir_2> ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("D:/Development/Trading Agent")
BT = ROOT / "artifacts" / "reports" / "backtests"


def _load(run: str) -> dict:
    d = Path(run)
    if not d.is_absolute():
        d = BT / run
    agg_path = d / "aggregate.json"
    out: dict = {"run": d.name, "ok": False}
    if not agg_path.exists():
        out["error"] = f"no aggregate.json at {agg_path}"
        return out
    agg = json.loads(agg_path.read_text(encoding="utf-8"))
    if "aggregate" in agg and isinstance(agg["aggregate"], dict):
        agg = agg["aggregate"]
    out.update({
        "ok": True,
        "net_pnl": round(float(agg.get("net_pnl_usd", 0.0)), 2),
        "trades": int(agg.get("trades", 0) or 0),
        "entries": int(agg.get("entries", 0) or 0),
        "win_rate": round(float(agg.get("win_rate", 0.0) or 0.0), 3),
        "profit_factor": round(float(agg.get("profit_factor", 0.0) or 0.0), 3),
        "max_dd_pct": round(float(agg.get("max_drawdown_pct", 0.0) or 0.0), 2),
        "avg_holding_bars": round(float(agg.get("avg_holding_bars", 0.0) or 0.0), 1),
        "partial_exit_events": int(agg.get("partial_exit_events", 0) or 0),
    })
    # close-reason breakdown
    rows = agg.get("pnl_by_close_reason") or []
    out["by_close_reason"] = {
        str(r.get("close_reason")): {"n": int(r.get("trades", 0) or 0), "net": round(float(r.get("net_pnl_usd", 0.0) or 0.0), 2)}
        for r in rows
    }
    tr = d / "trades.csv"
    if tr.exists():
        try:
            tdf = pd.read_csv(tr)
            pnl = tdf["realized_pnl_usd"].astype(float)
            out["trades_csv"] = {
                "n": int(len(pnl)), "net": round(float(pnl.sum()), 2),
                "win_rate": round(float((pnl > 0).mean()), 3) if len(pnl) else 0.0,
            }
        except Exception as ex:
            out["trades_csv_err"] = str(ex)
    return out


def main() -> None:
    runs = sys.argv[1:] or ["iter2_base"]
    results = [_load(r) for r in runs]
    print(f"{'run':28} {'net_pnl':>10} {'trades':>7} {'win%':>6} {'PF':>6} {'maxDD%':>7} {'avgHold':>8}")
    for r in results:
        if not r.get("ok"):
            print(f"{r['run']:28} ERROR: {r.get('error')}")
            continue
        print(f"{r['run']:28} {r['net_pnl']:>10} {r['trades']:>7} {r['win_rate']*100:>5.1f} "
              f"{r['profit_factor']:>6} {r['max_dd_pct']:>7} {r['avg_holding_bars']:>8}")
    base = next((r for r in results if r.get("ok")), None)
    for r in results:
        if r.get("ok") and base and r is not base:
            print(f"  delta {r['run']} vs {base['run']}: net {base['net_pnl']} -> {r['net_pnl']}  (delta {round(r['net_pnl']-base['net_pnl'],2)})")
    print("\n=== close-reason breakdown ===")
    for r in results:
        if not r.get("ok"):
            continue
        print(f"  [{r['run']}]")
        for reason, v in sorted(r.get("by_close_reason", {}).items(), key=lambda kv: kv[1]["net"]):
            print(f"     {reason:30} n={v['n']:4} net={v['net']:10.2f}")


if __name__ == "__main__":
    main()
