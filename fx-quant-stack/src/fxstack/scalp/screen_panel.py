# AGENT: ROLE: Cross-sectional feature screen across ALL pairs simultaneously, parallelised, with search-size-honest verdicts.
# AGENT: ENTRYPOINT: CLI `python -m fxstack.scalp.screen_panel`.
# AGENT: PRIMARY INPUTS: aligned multi-pair panel (fxstack/scalp/panel.py).
# AGENT: PRIMARY OUTPUTS: per (feature, horizon, pair) information + tradable expectancy, day-clustered, Sidak-corrected.
# AGENT: CALLED BY: research loop.
"""Screen cross-sectional features on every pair at once.

Single-pair features are exhausted: ~60 cells, nothing monetizable. The
remaining untested information class needs the whole universe observed
SIMULTANEOUSLY -- a dollar move is a statement about the cross-section, not
about any pair's own history.

The screen keeps every honesty property of the single-pair one (tradable
bid/ask arithmetic with the short leg priced from quotes, day-clustered
t-stats, a 30-day floor, Sidak correction for the honest search size) and
adds the two that only matter across pairs:

- The cross-section must be COMPLETE at each timestamp, or breadth is fake.
- Trades are counted per (pair, day). Seven pairs reacting to one dollar
  move on one day is ONE observation, and the clustered statistics are what
  stop that from looking like seven.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fxstack.scalp.panel import (
    PAIR_LEGS,
    cross_features,
    load_panel,
    usd_factor_bps,
)
from fxstack.scalp.screen import ScreenResult, _clustered_t, search_corrected_threshold


@dataclass(slots=True)
class PanelCell:
    feature: str
    symbol: str
    horizon: int
    n_obs: int
    n_days: int
    ic: float
    ic_t: float
    mid_bps: float
    tradable_bps: float
    tradable_t: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature, "symbol": self.symbol, "horizon": self.horizon,
            "n_obs": self.n_obs, "n_days": self.n_days, "ic": self.ic,
            "ic_t": self.ic_t, "mid_bps": self.mid_bps,
            "tradable_bps": self.tradable_bps, "tradable_t": self.tradable_t,
        }


def _day(epoch: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def _corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da > 1e-12 and db > 1e-12 else 0.0


def screen_panel_symbol(args: tuple) -> list[dict[str, Any]]:
    """Worker: screen every cross-sectional feature for ONE pair.

    Takes/returns plain data so it can cross a process boundary; the panel is
    loaded per worker, which is cheaper than pickling the whole universe.
    """
    (symbol, symbols, csv_root, bar_minutes, horizons, start, end,
     coherence_floor) = args
    epochs, per_symbol = load_panel(
        symbols=symbols, csv_root=Path(csv_root), bar_minutes=bar_minutes,
        start=start, end=end,
    )
    if symbol not in per_symbol or len(epochs) < 500:
        return []

    # Precompute the cross-section once per timestamp.
    feats: dict[str, list[tuple[int, float]]] = {
        "usd_factor": [], "residual": [], "residual_x_coherent": [],
        "factor_x_coherent": [],
    }
    for epoch in epochs:
        cf = cross_features(symbol=symbol, epoch=epoch, per_symbol=per_symbol)
        factor, coherence, n = usd_factor_bps(epoch, per_symbol)
        if n < 3:
            continue
        feats["usd_factor"].append((epoch, cf["usd_factor"]))
        feats["residual"].append((epoch, cf["residual"]))
        # Conditioned variants: the panel judged breadth to be the genuinely
        # new information, so the interaction is the hypothesis worth testing.
        coherent = coherence >= coherence_floor
        feats["residual_x_coherent"].append(
            (epoch, cf["residual"] if coherent else 0.0)
        )
        feats["factor_x_coherent"].append(
            (epoch, cf["usd_factor"] if coherent else 0.0)
        )

    bars = per_symbol[symbol]
    index = {e: i for i, e in enumerate(epochs)}
    out: list[dict[str, Any]] = []
    for feature, series in feats.items():
        for horizon in horizons:
            rows: list[tuple[str, float, float, float, float]] = []
            for epoch, value in series:
                i = index.get(epoch)
                if i is None or value == 0.0 or i + horizon >= len(epochs):
                    continue
                now = bars.get(epoch)
                later = bars.get(epochs[i + horizon])
                if now is None or later is None or now.mid <= 0:
                    continue
                mid_ret = (later.mid - now.mid) / now.mid * 1e4
                long_ret = (later.bid - now.ask) / now.mid * 1e4
                short_ret = (now.bid - later.ask) / now.mid * 1e4
                rows.append((_day(epoch), value, mid_ret, long_ret, short_ret))
            if len(rows) < 300:
                continue
            fs = [r[1] for r in rows]
            ms = [r[2] for r in rows]
            ic = _corr(fs, ms)
            fm, fsd = statistics.fmean(fs), statistics.pstdev(fs)
            mm, msd = statistics.fmean(ms), statistics.pstdev(ms)
            if fsd < 1e-12 or msd < 1e-12:
                continue
            ic_t = _clustered_t(
                [(r[0], ((r[1] - fm) / fsd) * ((r[2] - mm) / msd)) for r in rows]
            )
            ordered = sorted(rows, key=lambda r: r[1])
            k = max(30, len(ordered) // 10)
            bottom, top = ordered[:k], ordered[-k:]
            long_side, short_side = (top, bottom) if ic >= 0 else (bottom, top)
            trades = [(r[0], r[3]) for r in long_side] + [
                (r[0], r[4]) for r in short_side
            ]
            mid_only = [r[2] for r in long_side] + [-r[2] for r in short_side]
            out.append(
                PanelCell(
                    feature=feature, symbol=symbol, horizon=horizon,
                    n_obs=len(rows), n_days=len({r[0] for r in rows}),
                    ic=ic, ic_t=ic_t, mid_bps=statistics.fmean(mid_only),
                    tradable_bps=statistics.fmean([t[1] for t in trades]),
                    tradable_t=_clustered_t(trades),
                ).to_dict()
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(sorted(PAIR_LEGS)))
    ap.add_argument("--csv-root", default="fx-quant-stack/data/dukascopy")
    ap.add_argument("--bar-minutes", type=int, default=5)
    ap.add_argument("--horizons", default="3,6,12")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--coherence-floor", type=float, default=0.75)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--prior-tests", type=int, default=60,
                    help="cells already examined in earlier screens; the "
                         "correction must cover the WHOLE search, not one run")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    print(f"panel: {len(symbols)} pairs simultaneously, M{args.bar_minutes}, "
          f"horizons {horizons}, {args.workers} workers")

    jobs = [
        (s, symbols, str(args.csv_root), args.bar_minutes, horizons,
         args.start, args.end, args.coherence_floor)
        for s in symbols
    ]
    cells: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(screen_panel_symbol, jobs):
            cells.extend(result)

    total_tests = len(cells) + int(args.prior_tests)
    threshold = search_corrected_threshold(total_tests)
    print(f"\n{len(cells)} cells measured; search size {total_tests} "
          f"-> corrected |t| threshold {threshold:.2f}\n")

    monetizable = [
        c for c in cells
        if c["n_days"] >= 30 and c["tradable_t"] >= threshold and c["tradable_bps"] > 0
    ]
    informative = [
        c for c in cells if c["n_days"] >= 30 and abs(c["ic_t"]) >= threshold
    ]
    print(f"{'feature':<22}{'pair':<8}{'H':>3}{'n':>7}{'days':>6}{'IC':>8}"
          f"{'IC_t':>7}{'mid_bps':>9}{'trade_bps':>11}{'trade_t':>9}")
    for c in sorted(cells, key=lambda c: -c["tradable_t"])[:15]:
        print(f"{c['feature']:<22}{c['symbol']:<8}{c['horizon']:>3}{c['n_obs']:>7}"
              f"{c['n_days']:>6}{c['ic']:>8.4f}{c['ic_t']:>7.2f}"
              f"{c['mid_bps']:>9.3f}{c['tradable_bps']:>11.3f}{c['tradable_t']:>9.2f}")
    print(f"\nMONETIZABLE after correction: {len(monetizable)}")
    for c in monetizable:
        print(f"  {c['feature']} {c['symbol']} H{c['horizon']}: "
              f"{c['tradable_bps']:+.3f}bps t={c['tradable_t']:.2f} "
              f"({c['n_days']} days)")
    print(f"INFORMATIVE (mid) after correction: {len(informative)}")
    print("  (max_cost = the round-trip cost at which this edge breaks even;"
          " implied = what the data actually charged)")
    for c in sorted(informative, key=lambda c: -abs(c["ic_t"]))[:10]:
        # tradable = mid - cost  =>  cost = mid - tradable. The edge is
        # monetizable only at a venue cheaper than its own gross edge.
        implied_cost = c["mid_bps"] - c["tradable_bps"]
        print(f"  {c['feature']} {c['symbol']} H{c['horizon']}: "
              f"IC={c['ic']:+.4f} t={c['ic_t']:.2f} mid={c['mid_bps']:+.3f}bps "
              f"tradable={c['tradable_bps']:+.3f}bps | "
              f"max_cost={c['mid_bps']:.3f}bps implied={implied_cost:.3f}bps "
              f"({implied_cost / max(1e-9, c['mid_bps']):.1f}x too expensive)")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(cells, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
