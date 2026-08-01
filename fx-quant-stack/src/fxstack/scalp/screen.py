# AGENT: ROLE: Cheap, statistically honest screening of candidate features BEFORE any family is built around them.
# AGENT: ENTRYPOINT: `screen_symbol`; CLI `python -m fxstack.scalp.screen`.
# AGENT: PRIMARY INPUTS: Dukascopy bid/ask M1 CSVs; feature functions defined here.
# AGENT: PRIMARY OUTPUTS: per-feature information (mid IC) AND monetizability (tradable expectancy) with day-clustered t-stats.
# AGENT: CALLED BY: research loop. Nothing in the hot path depends on this.
"""Feature screening: does this information exist, and can it be monetized?

Every family this stack has built cost ~900k bars of replay to discover that
its feature carried no directional information. That is the wrong order of
operations. A feature can be screened in seconds, and screening answers two
DIFFERENT questions that must never be conflated:

1. INFORMATION: does the feature correlate with the forward MID move? This is
   the honest test of predictive content, measured with no cost assumption.
2. MONETIZABILITY: does acting on it clear the bid/ask? Measured in TRADABLE
   terms -- buy the ask, sell the bid -- because an effect that lives inside
   the spread is unmonetizable no matter how significant it is.

The adversarial design panel killed a candidate that looked strong on (1) and
was pure artifact on (2): asymmetric dealer quote-widening moves the mid with
zero transactions, so the "reversion" was the ask retracting toward a bid that
never moved. Screening in tradable terms makes that class of mistake
impossible to make quietly.

STATISTICS. Scalp observations are not independent: same-session outcomes
correlate, so t-stats are computed on DAY means (each day one observation).
An iid t-stat over bars overstates significance by roughly the square root of
the cluster size, which is how a 6-day effect passes as n=42.

BRACKET REALITY. A forward-horizon return is NOT what a bracketed trade earns.
With a stop at k sigma the position is absorbed early, capturing only a
fraction of terminal drift (optional stopping: E[X_tau] = mu * E[tau]). The
screen reports terminal drift and flags the bracket requirement separately --
never credit a stop-and-target strategy with the full horizon move.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class Obs:
    """One engine bar with the quotes needed for tradable arithmetic."""

    epoch: int
    bid_o: float
    bid_h: float
    bid_l: float
    bid_c: float
    ask_o: float
    ask_h: float
    ask_l: float
    ask_c: float
    volume: float

    @property
    def mid_c(self) -> float:
        return (self.bid_c + self.ask_c) / 2.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_c
        return (self.ask_c - self.bid_c) / mid * 1e4 if mid > 0 else 0.0


@dataclass(slots=True)
class ScreenResult:
    feature: str
    horizon_bars: int
    n_obs: int
    n_days: int
    # Information: correlation with the forward MID move (no cost).
    ic: float
    ic_t_clustered: float
    # Monetizability: mean TRADABLE bps of the long-top/short-bottom decile
    # portfolio, i.e. net of paying the spread on both sides.
    tradable_bps: float
    tradable_t_clustered: float
    mid_bps: float
    decile_n: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    #: Minimum distinct days before ANY verdict is trustworthy. With few
    #: clusters the day-mean variance is small and the t-statistic inflates,
    #: while the critical value it must clear grows (t(3) needs ~3.2, not 2).
    #: Requiring clusters is simpler and stricter than chasing the df table.
    MIN_DAYS: int = 30

    #: t-threshold, raised by the caller to account for how many cells the
    #: search examined. A screen of 60 cells expects ~1 hit at |t|>2.5 from
    #: noise alone, so an uncorrected verdict is a coin flip wearing a suit.
    t_threshold: float = 2.5

    @property
    def has_information(self) -> bool:
        return (
            self.n_days >= self.MIN_DAYS
            and abs(self.ic_t_clustered) >= self.t_threshold
        )

    @property
    def is_monetizable(self) -> bool:
        return (
            self.n_days >= self.MIN_DAYS
            and self.tradable_t_clustered >= self.t_threshold
            and self.tradable_bps > 0.0
        )


def search_corrected_threshold(n_tests: int, *, alpha: float = 0.05) -> float:
    """Two-sided t-threshold controlling family-wise error over ``n_tests``.

    Sidak: per-test alpha' = 1 - (1-alpha)^(1/n). Approximated with the normal
    quantile, which is the right regime here (hundreds of clustered days).
    A screen is a SEARCH, and a search's best cell is not a discovery until
    it clears the bar the search itself raised.
    """
    n = max(1, int(n_tests))
    per_test = 1.0 - (1.0 - alpha) ** (1.0 / n)
    p = max(1e-12, per_test / 2.0)
    # Acklam-style rational approximation of the normal quantile is overkill;
    # bisection on erfc is exact enough and dependency-free.
    lo, hi = 0.0, 12.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        tail = 0.5 * math.erfc(mid / math.sqrt(2.0))
        if tail > p:
            lo = mid
        else:
            hi = mid
    return max(2.5, (lo + hi) / 2.0)


def load_obs(
    csv_path: Path, *, bar_minutes: int = 5, start: str | None = None, end: str | None = None
) -> list[Obs]:
    """Load M1 bid/ask rows and fold to ``bar_minutes`` (hour-aligned)."""
    start_epoch = _epoch(start)
    end_epoch = _epoch(end)
    out: list[Obs] = []
    bucket: list[tuple[int, list[float]]] = []
    step = max(1, int(bar_minutes))
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            try:
                epoch = int(
                    dt.datetime.fromisoformat(row[0].replace("Z", "+00:00")).timestamp()
                )
                vals = [float(x) for x in row[1:9]]
                vol = float(row[9]) if len(row) > 9 else 0.0
            except (ValueError, IndexError):
                continue
            if start_epoch and epoch < start_epoch:
                continue
            if end_epoch and epoch >= end_epoch:
                break
            if min(vals) <= 0:
                continue
            minute_index = epoch // 60
            if bucket and (minute_index // step) != (bucket[0][0] // step):
                folded = _fold(bucket)
                if folded is not None:
                    out.append(folded)
                bucket = []
            bucket.append((minute_index, vals + [vol]))
    folded = _fold(bucket)
    if folded is not None:
        out.append(folded)
    return out


def _fold(bucket: list[tuple[int, list[float]]]) -> Obs | None:
    if not bucket:
        return None
    first, last = bucket[0][1], bucket[-1][1]
    return Obs(
        epoch=bucket[0][0] * 60,
        bid_o=first[0],
        bid_h=max(b[1][1] for b in bucket),
        bid_l=min(b[1][2] for b in bucket),
        bid_c=last[3],
        ask_o=first[4],
        ask_h=max(b[1][5] for b in bucket),
        ask_l=min(b[1][6] for b in bucket),
        ask_c=last[7],
        volume=sum(b[1][8] for b in bucket),
    )


def _epoch(text: str | None) -> int | None:
    if not text:
        return None
    return int(
        dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp()
    )


# ------------------------------------------------------------------ features
# Each returns a value at index i using ONLY bars <= i (causality is the whole
# point; a feature that peeks is worse than useless because it looks amazing).


def _zscore(values: list[float]) -> float:
    if len(values) < 8:
        return 0.0
    mean = statistics.fmean(values)
    sd = statistics.pstdev(values)
    return (values[-1] - mean) / sd if sd > 1e-12 else 0.0


def f_trailing_return(obs: list[Obs], i: int, lookback: int = 12) -> float:
    """The CONTROL: trailing mid return. Every falsified family reduces to
    this, so it must appear in the screen as the null to beat."""
    if i < lookback:
        return 0.0
    past = obs[i - lookback].mid_c
    return (obs[i].mid_c - past) / past * 1e4 if past > 0 else 0.0


def f_spread_z(obs: list[Obs], i: int, lookback: int = 48) -> float:
    """Dealer widening: cost itself as a state variable, not a price move."""
    if i < lookback:
        return 0.0
    return _zscore([o.spread_bps for o in obs[i - lookback: i + 1]])


def f_activity_z(obs: list[Obs], i: int, lookback: int = 48) -> float:
    """Tick activity (Dukascopy volume proxy) -- NON-price information."""
    if i < lookback:
        return 0.0
    return _zscore([o.volume for o in obs[i - lookback: i + 1]])


def f_range_compression(obs: list[Obs], i: int, lookback: int = 24) -> float:
    """Current bar range vs trailing: expansion/compression regime state."""
    if i < lookback:
        return 0.0
    ranges = [
        (o.ask_h - o.bid_l) / o.mid_c * 1e4 if o.mid_c > 0 else 0.0
        for o in obs[i - lookback: i + 1]
    ]
    return _zscore(ranges)


def f_close_location(obs: list[Obs], i: int, lookback: int = 1) -> float:
    """Where the close sits inside the bar's range -- pressure at the close,
    a microstructure statement rather than a return."""
    o = obs[i]
    hi, lo = o.ask_h, o.bid_l
    if hi <= lo:
        return 0.0
    return ((o.mid_c - lo) / (hi - lo) - 0.5) * 2.0


def f_signed_activity(obs: list[Obs], i: int, lookback: int = 24) -> float:
    """Activity signed by the bar's direction: the closest proxy this data
    allows for aggressive order flow (no L2, no true tick rule)."""
    if i < lookback:
        return 0.0
    act = f_activity_z(obs, i, lookback)
    o = obs[i]
    direction = 1.0 if o.mid_c > (o.bid_o + o.ask_o) / 2.0 else -1.0
    return act * direction


FEATURES: dict[str, Callable[[list[Obs], int], float]] = {
    "trailing_return(CONTROL)": lambda o, i: f_trailing_return(o, i),
    "spread_z": lambda o, i: f_spread_z(o, i),
    "activity_z": lambda o, i: f_activity_z(o, i),
    "range_compression": lambda o, i: f_range_compression(o, i),
    "close_location": lambda o, i: f_close_location(o, i),
    "signed_activity": lambda o, i: f_signed_activity(o, i),
}


# ------------------------------------------------------------------ scoring


def _day(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def _clustered_t(pairs: list[tuple[str, float]]) -> float:
    """t-stat of the mean, one observation per DAY."""
    by_day: dict[str, list[float]] = {}
    for day, value in pairs:
        by_day.setdefault(day, []).append(value)
    day_means = [statistics.fmean(v) for v in by_day.values()]
    n = len(day_means)
    if n < 3:
        return 0.0
    mean = statistics.fmean(day_means)
    sd = statistics.stdev(day_means)
    return mean / (sd / math.sqrt(n)) if sd > 1e-12 else 0.0


def screen_feature(
    obs: list[Obs], *, name: str, fn: Callable[[list[Obs], int], float], horizon: int
) -> ScreenResult:
    values: list[tuple[int, float]] = []
    for i in range(len(obs) - horizon):
        v = fn(obs, i)
        if v == 0.0 or not math.isfinite(v):
            continue
        values.append((i, v))
    if len(values) < 200:
        return ScreenResult(name, horizon, len(values), 0, 0, 0, 0, 0, 0, 0)

    # Forward returns, both framings.
    # day, feature, mid_bps, tradable_long_bps, tradable_short_bps
    rows: list[tuple[str, float, float, float, float]] = []
    for i, v in values:
        now, later = obs[i], obs[i + horizon]
        mid_now = now.mid_c
        if mid_now <= 0:
            continue
        mid_ret = (later.mid_c - mid_now) / mid_now * 1e4
        # Tradable LONG: pay the ask now, sell the bid later.
        long_ret = (later.bid_c - now.ask_c) / mid_now * 1e4
        # Tradable SHORT: sell the bid now, cover the ask later. Computed
        # from quotes, never inferred from the long leg -- the spread is not
        # necessarily symmetric around the mid, and that asymmetry is exactly
        # the artifact that killed a panel candidate.
        short_ret = (now.bid_c - later.ask_c) / mid_now * 1e4
        rows.append((_day(now.epoch), v, mid_ret, long_ret, short_ret))
    if len(rows) < 200:
        return ScreenResult(name, horizon, len(rows), 0, 0, 0, 0, 0, 0, 0)

    feats = [r[1] for r in rows]
    mids = [r[2] for r in rows]
    ic = _corr(feats, mids)
    # Day-clustered t for the IC: use per-observation products, standardized.
    fmean, fsd = statistics.fmean(feats), statistics.pstdev(feats)
    mmean, msd = statistics.fmean(mids), statistics.pstdev(mids)
    if fsd < 1e-12 or msd < 1e-12:
        return ScreenResult(name, horizon, len(rows), 0, 0, 0, 0, 0, 0, 0)
    prod = [(r[0], ((r[1] - fmean) / fsd) * ((r[2] - mmean) / msd)) for r in rows]
    ic_t = _clustered_t(prod)

    # Monetizability: long the top decile, short the bottom decile, in
    # TRADABLE terms (a short earns the negative of the long round trip,
    # paying its own spread: sell bid now, cover ask later).
    ordered = sorted(rows, key=lambda r: r[1])
    k = max(20, len(ordered) // 10)
    bottom, top = ordered[:k], ordered[-k:]
    # Trade the direction the measured IC implies. A negative-IC feature is
    # monetized by shorting its top decile; always going long the top would
    # report a real inverted edge as a loss and hide it.
    long_side, short_side = (top, bottom) if ic >= 0 else (bottom, top)
    trades: list[tuple[str, float]] = []
    mid_only: list[float] = []
    for day, _v, mid_ret, long_ret, _short in long_side:
        trades.append((day, long_ret))
        mid_only.append(mid_ret)
    for day, _v, mid_ret, _long, short_ret in short_side:
        trades.append((day, short_ret))
        mid_only.append(-mid_ret)
    tradable = statistics.fmean([t[1] for t in trades])
    return ScreenResult(
        feature=name,
        horizon_bars=horizon,
        n_obs=len(rows),
        n_days=len({r[0] for r in rows}),
        ic=ic,
        ic_t_clustered=ic_t,
        tradable_bps=tradable,
        tradable_t_clustered=_clustered_t(trades),
        mid_bps=statistics.fmean(mid_only),
        decile_n=k,
    )


def _corr(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 3:
        return 0.0
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return num / (da * db) if da > 1e-12 and db > 1e-12 else 0.0


def screen_symbol(
    *,
    csv_path: Path,
    bar_minutes: int,
    horizons: list[int],
    start: str | None,
    end: str | None,
) -> list[ScreenResult]:
    obs = load_obs(csv_path, bar_minutes=bar_minutes, start=start, end=end)
    out: list[ScreenResult] = []
    for horizon in horizons:
        for name, fn in FEATURES.items():
            out.append(screen_feature(obs, name=name, fn=fn, horizon=horizon))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--csv-root", default="fx-quant-stack/data/dukascopy")
    ap.add_argument("--bar-minutes", type=int, default=5)
    ap.add_argument("--horizons", default="3,6")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--total-tests", type=int, default=0,
                    help="HONEST total cells examined across the whole search "
                         "(not just this invocation); raises the verdict bar")
    args = ap.parse_args(argv)

    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    all_rows: list[dict[str, Any]] = []
    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        path = Path(args.csv_root) / f"{symbol}_M1.csv"
        if not path.exists():
            print(f"{symbol}: missing {path}")
            continue
        results = screen_symbol(
            csv_path=path, bar_minutes=args.bar_minutes, horizons=horizons,
            start=args.start, end=args.end,
        )
        # The verdict bar rises with the size of the search that produced it.
        n_tests = args.total_tests or len([r for r in results if r.n_obs])
        threshold = search_corrected_threshold(n_tests)
        for r in results:
            r.t_threshold = threshold
        print(f"\n=== {symbol} (M{args.bar_minutes}) ===")
        print(f"search size {n_tests} cells -> corrected |t| threshold {threshold:.2f}")
        print(f"{'feature':<26}{'H':>3}{'n':>7}{'days':>6}{'IC':>8}{'IC_t':>8}"
              f"{'mid_bps':>9}{'trade_bps':>11}{'trade_t':>9}  verdict")
        for r in results:
            if r.n_obs == 0:
                continue
            verdict = (
                "MONETIZABLE" if r.is_monetizable
                else ("information, not monetizable" if r.has_information else "-")
            )
            print(f"{r.feature:<26}{r.horizon_bars:>3}{r.n_obs:>7}{r.n_days:>6}"
                  f"{r.ic:>8.4f}{r.ic_t_clustered:>8.2f}{r.mid_bps:>9.3f}"
                  f"{r.tradable_bps:>11.3f}{r.tradable_t_clustered:>9.2f}  {verdict}")
            all_rows.append({"symbol": symbol} | r.to_dict())
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(all_rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
