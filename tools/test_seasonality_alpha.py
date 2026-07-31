"""The last alpha family testable offline: calendar / session seasonality.

Every family measured so far is PRICE-DERIVED (momentum, breakout, reversion,
vol, dispersion) and every one is flat gross at every horizon. Carry is the
documented structurally-independent family but needs broker swap rates the EA
does not yet poll.

Calendar effects are the other structurally-independent family, and unlike carry
they need NOTHING but timestamps, which are already on disk:

  * month-end / quarter-end rebalancing flow (the documented "4pm fix" effect)
  * hour-of-day session structure (London open, NY overlap, Asia range)
  * day-of-week

These are not momentum in disguise -- the signal is a clock, not a price. If they
are flat too, the "no edge in this data" conclusion covers every family that can
be tested without new inputs, which is a materially stronger statement than
"price features are flat".

Every candidate is charged full costs and put through the SAME rotation-based
MCPT the activation gate uses. Positions are lagged one bar before evaluation
(the ALIGNMENT CONTRACT in mcpt.py) -- a position derived from bar i's own close
is lookahead and would manufacture a spectacular fake edge.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path("fx-quant-stack/src").resolve()))

from fxstack.validation.mcpt import rotation_permutation_test, strategy_returns  # noqa: E402

DATA = Path("fx-quant-stack/data/dukascopy/EURUSD_H4.csv")
N_PERM = 2000


def load(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, usecols=["timestamp", "bid_close", "ask_close"])
    d["ts"] = pd.to_datetime(d.timestamp, utc=True, errors="coerce")
    d = d.dropna(subset=["ts"]).reset_index(drop=True)
    d["mid"] = (d.bid_close + d.ask_close) / 2.0
    d["ret"] = np.log(d.mid).diff().fillna(0.0)
    # Round-trip cost as a fraction of price, from the REAL quoted spread.
    d["cost"] = ((d.ask_close - d.bid_close) / d.mid).clip(lower=0.0)
    return d


def evaluate(name: str, pos: np.ndarray, d: pd.DataFrame, cost_per_turn: float) -> dict:
    # ALIGNMENT: act on the NEXT bar, never the one the signal was computed from.
    pos = np.roll(np.asarray(pos, dtype=float), 1)
    pos[0] = 0.0
    ret = d.ret.values
    net = strategy_returns(pos, ret, cost_per_turn=cost_per_turn)
    turns = int(np.sum(np.abs(np.diff(np.concatenate([[0.0], pos]))) > 0))
    gross = strategy_returns(pos, ret, cost_per_turn=0.0)
    if net.std() == 0 or turns == 0:
        return {"name": name, "trades": turns, "gross_pct": 0.0, "net_pct": 0.0,
                "sharpe": 0.0, "p": 1.0}
    res = rotation_permutation_test(
        pos, ret, statistic="sharpe", n_permutations=N_PERM,
        cost_per_turn=cost_per_turn, seed=4242,
    )
    return {
        "name": name,
        "trades": turns,
        "gross_pct": float(np.expm1(gross.sum()) * 100.0),
        "net_pct": float(np.expm1(net.sum()) * 100.0),
        "sharpe": float(res.get("observed", 0.0)),
        "p": float(res.get("p_value", 1.0)),
    }


def main() -> None:
    d = load(DATA)
    cost = float(d.cost.mean())
    print(f"{DATA.name}: {len(d):,} bars  {d.ts.iloc[0].date()} -> {d.ts.iloc[-1].date()}")
    print(f"mean round-trip cost: {cost * 1e4:.3f} bps of price\n")

    ts = d.ts.dt
    results = []

    # ---- month-end / quarter-end rebalancing flow
    is_month_end = (ts.days_in_month - ts.day) <= 2
    is_quarter_end = is_month_end & ts.month.isin([3, 6, 9, 12])
    for label, mask in (("month_end_long", is_month_end), ("quarter_end_long", is_quarter_end)):
        results.append(evaluate(label, np.where(mask, 1.0, 0.0), d, cost))
        results.append(evaluate(label.replace("long", "short"), np.where(mask, -1.0, 0.0), d, cost))

    # ---- hour-of-day session structure (H4 buckets)
    for hour in sorted(ts.hour.unique()):
        mask = ts.hour == hour
        if int(mask.sum()) < 200:
            continue
        results.append(evaluate(f"hour_{hour:02d}_long", np.where(mask, 1.0, 0.0), d, cost))
        results.append(evaluate(f"hour_{hour:02d}_short", np.where(mask, -1.0, 0.0), d, cost))

    # ---- day-of-week
    for dow, nm in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"]):
        mask = ts.dayofweek == dow
        if int(mask.sum()) < 200:
            continue
        results.append(evaluate(f"{nm}_long", np.where(mask, 1.0, 0.0), d, cost))
        results.append(evaluate(f"{nm}_short", np.where(mask, -1.0, 0.0), d, cost))

    results.sort(key=lambda r: r["p"])
    print(f"{'candidate':<20}{'trades':>7}{'gross %':>10}{'net %':>9}{'Sharpe':>9}{'MCPT p':>9}")
    print("-" * 64)
    for r in results:
        print(f"{r['name']:<20}{r['trades']:>7}{r['gross_pct']:>10.2f}"
              f"{r['net_pct']:>9.2f}{r['sharpe']:>9.3f}{r['p']:>9.4f}")

    n = len(results)
    survivors = [r for r in results if r["p"] < 0.05 and r["net_pct"] > 0]
    print(f"\ncandidates tested: {n}")
    print(f"p<0.05 AND net-positive: {len(survivors)}")
    # Multiple-testing reality check: at alpha=0.05 you expect n*0.05 by luck alone.
    print(f"expected by luck at alpha=0.05: {n * 0.05:.1f}")
    bonf = 0.05 / max(1, n)
    strict = [r for r in survivors if r["p"] < bonf]
    print(f"survive Bonferroni (p<{bonf:.5f}): {len(strict)}")
    for r in strict:
        print(f"   -> {r['name']}: net {r['net_pct']:+.2f}%  p={r['p']:.5f}")
    if not strict:
        print("   -> none. Calendar seasonality is flat here too, after costs and")
        print("      after accounting for how many things were tried.")


if __name__ == "__main__":
    main()
