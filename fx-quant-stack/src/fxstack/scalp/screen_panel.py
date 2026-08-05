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
adds the controls that matter across pairs:

- The cross-section must be COMPLETE at each timestamp, or breadth is fake.
- Entry uses the synchronous first M1 open after a signal bar, and a missing
  epoch invalidates the entire forward horizon.
- Side policy and tail quantiles are explicit inputs. Outcome IC is reported,
  never used to choose momentum versus reversion on the same observations.
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
    PanelBar,
    cross_features,
    load_panel,
)
from fxstack.scalp.screen import _clustered_t, search_corrected_threshold


BASE_FEATURE_NAMES: tuple[str, ...] = (
    "usd_factor",
    "residual",
    "residual_x_coherent",
    "factor_x_coherent",
)

# Bounded, preregistered continuation of the original one-bar panel screen.
# For source x at the current closed bar t, each feature is
#
#   sum(x[t-k+1:t]) / (pstdev(x[t-24:t-1]) * sqrt(k))
#
# The numerator may use t because its outcome begins strictly after t.  The
# scale deliberately excludes t, making volatility normalization strictly
# trailing and preventing a current shock from shrinking its own signal.
LAGGED_FEATURE_SPECS: tuple[tuple[str, str, int, int], ...] = (
    ("residual_sum3_volnorm24", "residual", 3, 24),
    ("residual_sum6_volnorm24", "residual", 6, 24),
    ("factor_sum3_volnorm24", "usd_factor", 3, 24),
    ("factor_sum6_volnorm24", "usd_factor", 6, 24),
)
PANEL_FEATURE_NAMES: tuple[str, ...] = BASE_FEATURE_NAMES + tuple(
    spec[0] for spec in LAGGED_FEATURE_SPECS
)
SCREEN_FILL_DELAY_BARS = 1
SIDE_POLICIES: tuple[str, ...] = ("reversion", "momentum")
DEFAULT_SIDE_POLICIES: tuple[str, ...] = ("reversion",)
DEFAULT_LOWER_TAIL_QUANTILE = 0.10
DEFAULT_UPPER_TAIL_QUANTILE = 0.90


# Process-local cache populated by ``ProcessPoolExecutor.initializer``.  The
# synchronized panel is the expensive input; re-reading it once per target
# symbol multiplies the same CSV work by the size of the universe.
_PANEL_WORKER_CACHE: tuple[
    tuple[tuple[str, ...], str, int, str | None, str | None],
    tuple[list[int], dict[str, dict[int, PanelBar]]],
] | None = None


def _init_panel_worker(
    symbols: list[str],
    csv_root: str,
    bar_minutes: int,
    start: str | None,
    end: str | None,
) -> None:
    """Load one immutable synchronized panel per screening worker process."""
    global _PANEL_WORKER_CACHE
    key = (tuple(symbols), str(Path(csv_root)), int(bar_minutes), start, end)
    _PANEL_WORKER_CACHE = (
        key,
        load_panel(
            symbols=symbols,
            csv_root=Path(csv_root),
            bar_minutes=int(bar_minutes),
            start=start,
            end=end,
        ),
    )


def _worker_panel(
    *,
    symbols: list[str],
    csv_root: str,
    bar_minutes: int,
    start: str | None,
    end: str | None,
) -> tuple[list[int], dict[str, dict[int, PanelBar]]]:
    """Return the initializer-owned panel, with a direct-call fallback."""
    key = (tuple(symbols), str(Path(csv_root)), int(bar_minutes), start, end)
    if _PANEL_WORKER_CACHE is not None and _PANEL_WORKER_CACHE[0] == key:
        return _PANEL_WORKER_CACHE[1]
    return load_panel(
        symbols=symbols,
        csv_root=Path(csv_root),
        bar_minutes=int(bar_minutes),
        start=start,
        end=end,
    )


@dataclass(slots=True)
class PanelCell:
    feature: str
    symbol: str
    horizon: int
    side_policy: str
    lower_tail_quantile: float
    upper_tail_quantile: float
    lower_cutpoint: float
    upper_cutpoint: float
    n_obs: int
    n_days: int
    ic: float
    ic_t: float
    mid_bps: float
    tradable_bps: float
    tradable_t: float
    long_n_days: int
    long_bps: float
    long_t: float
    long_win_rate: float
    short_n_days: int
    short_bps: float
    short_t: float
    short_win_rate: float
    extra_round_trip_cost_bps: float

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "feature": self.feature, "symbol": self.symbol, "horizon": self.horizon,
            "side_policy": self.side_policy,
            "lower_tail_quantile": self.lower_tail_quantile,
            "upper_tail_quantile": self.upper_tail_quantile,
            "lower_cutpoint": self.lower_cutpoint,
            "upper_cutpoint": self.upper_cutpoint,
            "n_obs": self.n_obs, "n_days": self.n_days, "ic": self.ic,
            "ic_t": self.ic_t, "mid_bps": self.mid_bps,
            "tradable_bps": self.tradable_bps, "tradable_t": self.tradable_t,
            "long_n_days": self.long_n_days, "long_bps": self.long_bps,
            "long_t": self.long_t, "long_win_rate": self.long_win_rate,
            "short_n_days": self.short_n_days, "short_bps": self.short_bps,
            "short_t": self.short_t, "short_win_rate": self.short_win_rate,
            "extra_round_trip_cost_bps": self.extra_round_trip_cost_bps,
        }
        if self.side_policy not in SIDE_POLICIES:
            raise ValueError(f"unsupported side policy: {self.side_policy!r}")
        if not (
            0.0 < self.lower_tail_quantile < self.upper_tail_quantile < 1.0
            and self.lower_cutpoint < self.upper_cutpoint
            and self.extra_round_trip_cost_bps >= 0.0
            and 0.0 <= self.long_win_rate <= 1.0
            and 0.0 <= self.short_win_rate <= 1.0
        ):
            raise ValueError("invalid panel cell policy or metric bounds")
        for name, value in payload.items():
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f"non-finite panel metric {name}={value!r}")
        return payload


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


def _normalized_trailing_sum(
    values: list[float],
    i: int,
    *,
    sum_bars: int,
    vol_bars: int,
) -> float:
    """Vol-normalized multi-bar sum known at closed bar ``i``.

    The volatility slice ends at ``i - 1``.  The numerator ends at ``i``;
    callers must pair it only with outcomes beginning after this closed bar.
    """
    if sum_bars < 1 or vol_bars < 2 or i < vol_bars or i + 1 < sum_bars:
        return 0.0
    trailing = values[i - vol_bars:i]
    scale = statistics.pstdev(trailing)
    if not math.isfinite(scale) or scale <= 1e-12:
        return 0.0
    numerator = sum(values[i - sum_bars + 1:i + 1])
    value = numerator / (scale * math.sqrt(sum_bars))
    return value if math.isfinite(value) else 0.0


def _is_contiguous(
    epochs: list[int], *, start_index: int, end_index: int, bar_seconds: int
) -> bool:
    """True only when every epoch in the inclusive slice is one exact bar apart."""
    if bar_seconds < 60 or start_index < 0 or end_index >= len(epochs):
        return False
    return all(
        epochs[index] - epochs[index - 1] == bar_seconds
        for index in range(start_index + 1, end_index + 1)
    )


def build_feature_series(
    *,
    symbol: str,
    epochs: list[int],
    per_symbol: dict[str, dict[int, PanelBar]],
    coherence_floor: float,
    bar_seconds: int,
) -> dict[str, list[tuple[int, float]]]:
    """Build every panel feature using information available by each epoch.

    Keeping construction separate from outcome scoring makes the causal seam
    testable: changing bars after epoch ``t`` cannot alter any feature at or
    before ``t``.
    """
    feats: dict[str, list[tuple[int, float]]] = {
        name: [] for name in PANEL_FEATURE_NAMES
    }
    raw_epochs: list[int] = []
    raw: dict[str, list[float]] = {"usd_factor": [], "residual": []}
    coherences: list[float] = []

    for epoch in epochs:
        cf = cross_features(symbol=symbol, epoch=epoch, per_symbol=per_symbol)
        factor = float(cf.get("usd_factor", 0.0))
        residual = float(cf.get("residual", 0.0))
        coherence = float(cf.get("usd_coherence", 0.0))
        if not all(math.isfinite(v) for v in (factor, residual, coherence)):
            continue
        raw_epochs.append(epoch)
        raw["usd_factor"].append(factor)
        raw["residual"].append(residual)
        coherences.append(coherence)

    for i, epoch in enumerate(raw_epochs):
        factor = raw["usd_factor"][i]
        residual = raw["residual"][i]
        coherent = coherences[i] >= coherence_floor
        feats["usd_factor"].append((epoch, factor))
        feats["residual"].append((epoch, residual))
        feats["residual_x_coherent"].append(
            (epoch, residual if coherent else 0.0)
        )
        feats["factor_x_coherent"].append(
            (epoch, factor if coherent else 0.0)
        )
        for name, source, sum_bars, vol_bars in LAGGED_FEATURE_SPECS:
            first_required = i - max(vol_bars, sum_bars - 1)
            contiguous = _is_contiguous(
                raw_epochs,
                start_index=first_required,
                end_index=i,
                bar_seconds=bar_seconds,
            )
            feats[name].append(
                (
                    epoch,
                    (
                        _normalized_trailing_sum(
                            raw[source], i, sum_bars=sum_bars, vol_bars=vol_bars
                        )
                        if contiguous
                        else 0.0
                    ),
                )
            )
    return feats


def panel_trial_accounting(
    *,
    n_symbols: int,
    n_horizons: int,
    prior_tests: int,
    n_side_policies: int = 1,
) -> dict[str, int]:
    """Count attempted hypotheses, including cells with insufficient rows."""
    symbol_count = int(n_symbols)
    horizon_count = int(n_horizons)
    prior = int(prior_tests)
    orientation_multiplier = int(n_side_policies)
    if (
        symbol_count < 0
        or horizon_count < 0
        or prior < 0
        or orientation_multiplier < 1
    ):
        raise ValueError("trial counts must be non-negative with at least one policy")
    multiplier = symbol_count * horizon_count * orientation_multiplier
    base_cells = len(BASE_FEATURE_NAMES) * multiplier
    new_lagged_cells = len(LAGGED_FEATURE_SPECS) * multiplier
    current_cells = base_cells + new_lagged_cells
    return {
        "base_cells": base_cells,
        "new_lagged_cells": new_lagged_cells,
        "orientation_multiplier": orientation_multiplier,
        "current_cells": current_cells,
        "prior_tests": prior,
        "cumulative_tests": prior + current_cells,
    }


def _delayed_trade_returns(
    *,
    bars: dict[int, PanelBar],
    epochs: list[int],
    signal_index: int,
    horizon: int,
    bar_seconds: int,
) -> tuple[float, float, float] | None:
    """Return mid/long/short bps from next-M1-open through the horizon close.

    A feature at closed bar ``t`` is not allowed to transact at that bar's
    already-observed quote. Entry is the first executable M1 open of ``t + 1``
    and an H1 trade exits at that bar's close. Bid/ask arithmetic charges both
    observed legs. Every intervening aligned epoch must be exactly one bar
    apart, so an outage or weekend can never masquerade as a normal horizon.
    """
    if horizon < 1 or SCREEN_FILL_DELAY_BARS != 1 or bar_seconds < 60:
        return None
    entry_index = signal_index + SCREEN_FILL_DELAY_BARS
    exit_index = signal_index + horizon
    if signal_index < 0 or exit_index >= len(epochs):
        return None
    if not _is_contiguous(
        epochs,
        start_index=signal_index,
        end_index=exit_index,
        bar_seconds=bar_seconds,
    ):
        return None
    entry = bars.get(epochs[entry_index])
    exit_bar = bars.get(epochs[exit_index])
    if entry is None or exit_bar is None or not entry.valid or not exit_bar.valid:
        return None
    entry_mid = entry.open_mid
    values = (
        (exit_bar.mid - entry_mid) / entry_mid * 1e4,
        (exit_bar.bid - entry.ask_open) / entry_mid * 1e4,
        (entry.bid_open - exit_bar.ask) / entry_mid * 1e4,
    )
    return values if all(math.isfinite(value) for value in values) else None


def _tail_rows(
    rows: list[tuple[str, float, float, float, float]],
    *,
    lower_quantile: float,
    upper_quantile: float,
) -> tuple[
    list[tuple[str, float, float, float, float]],
    list[tuple[str, float, float, float, float]],
    float,
    float,
] | None:
    """Resolve deterministic, outcome-independent feature cutpoints and tails."""
    if not 0.0 < lower_quantile < upper_quantile < 1.0:
        return None
    ordered = sorted(rows, key=lambda row: row[1])
    n = len(ordered)
    if n < 300:
        return None
    lower_index = max(0, math.ceil(n * lower_quantile) - 1)
    upper_index = min(n - 1, math.floor(n * upper_quantile))
    if lower_index >= upper_index:
        return None
    lower_cutpoint = ordered[lower_index][1]
    upper_cutpoint = ordered[upper_index][1]
    if (
        not math.isfinite(lower_cutpoint)
        or not math.isfinite(upper_cutpoint)
        or lower_cutpoint >= upper_cutpoint
    ):
        return None
    lower = [row for row in ordered if row[1] <= lower_cutpoint]
    upper = [row for row in ordered if row[1] >= upper_cutpoint]
    if len(lower) < 30 or len(upper) < 30:
        return None
    return lower, upper, lower_cutpoint, upper_cutpoint


def _ic_supports_policy(cell: dict[str, Any], *, threshold: float) -> bool:
    ic_t = float(cell["ic_t"])
    if cell["side_policy"] == "momentum":
        return ic_t >= threshold
    return ic_t <= -threshold


def screen_panel_symbol(args: tuple) -> list[dict[str, Any]]:
    """Worker: screen every cross-sectional feature for ONE pair.

    Takes/returns plain data so it can cross a process boundary; the panel is
    loaded per worker, which is cheaper than pickling the whole universe.
    """
    (
        symbol,
        symbols,
        csv_root,
        bar_minutes,
        horizons,
        start,
        end,
        coherence_floor,
        extra_round_trip_cost_bps,
        side_policies,
        lower_tail_quantile,
        upper_tail_quantile,
    ) = args
    try:
        bar_minutes = int(bar_minutes)
        horizons = tuple(int(horizon) for horizon in horizons)
        coherence_floor = float(coherence_floor)
        extra_round_trip_cost_bps = float(extra_round_trip_cost_bps)
        lower_tail_quantile = float(lower_tail_quantile)
        upper_tail_quantile = float(upper_tail_quantile)
        policies = tuple(str(policy).lower() for policy in side_policies)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid panel screening policy") from exc
    if (
        bar_minutes < 1
        or not horizons
        or len(set(horizons)) != len(horizons)
        or any(horizon < 1 for horizon in horizons)
        or not math.isfinite(coherence_floor)
        or not 0.0 <= coherence_floor <= 1.0
        or not math.isfinite(extra_round_trip_cost_bps)
        or extra_round_trip_cost_bps < 0.0
        or not policies
        or len(set(policies)) != len(policies)
        or any(policy not in SIDE_POLICIES for policy in policies)
        or not math.isfinite(lower_tail_quantile)
        or not math.isfinite(upper_tail_quantile)
        or not 0.0 < lower_tail_quantile < upper_tail_quantile < 1.0
    ):
        raise ValueError("invalid panel screening policy")
    bar_seconds = bar_minutes * 60
    epochs, per_symbol = _worker_panel(
        symbols=symbols,
        csv_root=csv_root,
        bar_minutes=bar_minutes,
        start=start,
        end=end,
    )
    if symbol not in per_symbol or len(epochs) < 500:
        return []

    # Precompute features once per timestamp. Every series value at t is made
    # only from cross_features outputs at epochs <= t.
    feats = build_feature_series(
        symbol=symbol,
        epochs=epochs,
        per_symbol=per_symbol,
        coherence_floor=coherence_floor,
        bar_seconds=bar_seconds,
    )

    bars = per_symbol[symbol]
    index = {e: i for i, e in enumerate(epochs)}
    out: list[dict[str, Any]] = []
    for feature, series in feats.items():
        for horizon in horizons:
            if horizon < 1:
                continue
            rows: list[tuple[str, float, float, float, float]] = []
            for epoch, value in series:
                i = index.get(epoch)
                if i is None or value == 0.0:
                    continue
                returns = _delayed_trade_returns(
                    bars=bars,
                    epochs=epochs,
                    signal_index=i,
                    horizon=horizon,
                    bar_seconds=bar_seconds,
                )
                if returns is None:
                    continue
                mid_ret, long_ret, short_ret = returns
                long_ret -= extra_round_trip_cost_bps
                short_ret -= extra_round_trip_cost_bps
                if not all(
                    math.isfinite(number)
                    for number in (value, mid_ret, long_ret, short_ret)
                ):
                    continue
                entry_epoch = epoch + bar_seconds * SCREEN_FILL_DELAY_BARS
                rows.append(
                    (_day(entry_epoch), value, mid_ret, long_ret, short_ret)
                )
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
            tails = _tail_rows(
                rows,
                lower_quantile=lower_tail_quantile,
                upper_quantile=upper_tail_quantile,
            )
            if tails is None:
                continue
            lower, upper, lower_cutpoint, upper_cutpoint = tails
            for side_policy in policies:
                if side_policy == "momentum":
                    long_side, short_side = upper, lower
                else:
                    long_side, short_side = lower, upper
                long_trades = [(row[0], row[3]) for row in long_side]
                short_trades = [(row[0], row[4]) for row in short_side]
                trades = long_trades + short_trades
                mid_only = [row[2] for row in long_side] + [
                    -row[2] for row in short_side
                ]
                out.append(
                    PanelCell(
                        feature=feature,
                        symbol=symbol,
                        horizon=horizon,
                        side_policy=side_policy,
                        lower_tail_quantile=lower_tail_quantile,
                        upper_tail_quantile=upper_tail_quantile,
                        lower_cutpoint=lower_cutpoint,
                        upper_cutpoint=upper_cutpoint,
                        n_obs=len(rows),
                        n_days=len({row[0] for row in rows}),
                        ic=ic,
                        ic_t=ic_t,
                        mid_bps=statistics.fmean(mid_only),
                        tradable_bps=statistics.fmean(value for _, value in trades),
                        tradable_t=_clustered_t(trades),
                        long_n_days=len({day for day, _value in long_trades}),
                        long_bps=statistics.fmean(value for _, value in long_trades),
                        long_t=_clustered_t(long_trades),
                        long_win_rate=sum(value > 0.0 for _, value in long_trades)
                        / len(long_trades),
                        short_n_days=len({day for day, _value in short_trades}),
                        short_bps=statistics.fmean(value for _, value in short_trades),
                        short_t=_clustered_t(short_trades),
                        short_win_rate=sum(value > 0.0 for _, value in short_trades)
                        / len(short_trades),
                        extra_round_trip_cost_bps=extra_round_trip_cost_bps,
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
    ap.add_argument("--min-days", type=int, default=30)
    ap.add_argument(
        "--side-policies",
        default=",".join(DEFAULT_SIDE_POLICIES),
        help="comma-separated preregistered direction policies: reversion,momentum",
    )
    ap.add_argument(
        "--lower-tail-quantile",
        type=float,
        default=DEFAULT_LOWER_TAIL_QUANTILE,
        help="preregistered lower feature-distribution cutpoint",
    )
    ap.add_argument(
        "--upper-tail-quantile",
        type=float,
        default=DEFAULT_UPPER_TAIL_QUANTILE,
        help="preregistered upper feature-distribution cutpoint",
    )
    ap.add_argument(
        "--extra-round-trip-cost-bps",
        type=float,
        default=0.0,
        help="adverse commission/slippage pad charged in addition to both "
        "observed bid/ask legs",
    )
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--prior-tests", type=int, default=60,
                    help="cells already examined in earlier screens; the "
                         "correction must cover the WHOLE search, not one run")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    horizons = [int(h) for h in args.horizons.split(",") if h.strip()]
    side_policies = tuple(
        policy.strip().lower()
        for policy in args.side_policies.split(",")
        if policy.strip()
    )
    if not symbols or len(set(symbols)) != len(symbols):
        ap.error("--symbols must contain a non-empty set of unique pairs")
    if any(symbol not in PAIR_LEGS for symbol in symbols):
        ap.error("--symbols contains an unsupported pair")
    if args.bar_minutes < 1:
        ap.error("--bar-minutes must be positive")
    if not horizons or any(horizon < 1 for horizon in horizons):
        ap.error("--horizons must contain only positive integers")
    if len(set(horizons)) != len(horizons):
        ap.error("--horizons must not contain duplicates")
    if args.min_days < 3:
        ap.error("--min-days must be at least 3")
    if (
        not math.isfinite(args.coherence_floor)
        or not 0.0 <= args.coherence_floor <= 1.0
    ):
        ap.error("--coherence-floor must be finite and within [0, 1]")
    if (
        not side_policies
        or len(set(side_policies)) != len(side_policies)
        or any(policy not in SIDE_POLICIES for policy in side_policies)
    ):
        ap.error("--side-policies must contain unique reversion/momentum values")
    if (
        not math.isfinite(args.lower_tail_quantile)
        or not math.isfinite(args.upper_tail_quantile)
        or not 0.0
        < args.lower_tail_quantile
        < args.upper_tail_quantile
        < 1.0
    ):
        ap.error("tail quantiles must be finite and satisfy 0 < lower < upper < 1")
    if (
        not math.isfinite(args.extra_round_trip_cost_bps)
        or args.extra_round_trip_cost_bps < 0.0
    ):
        ap.error("--extra-round-trip-cost-bps must be finite and non-negative")
    if args.prior_tests < 0:
        ap.error("--prior-tests must be non-negative")
    if args.workers < 1:
        ap.error("--workers must be positive")
    print(f"panel: {len(symbols)} pairs simultaneously, M{args.bar_minutes}, "
          f"horizons {horizons}, policies {list(side_policies)}, "
          f"{args.workers} workers")

    jobs = [
        (s, symbols, str(args.csv_root), args.bar_minutes, horizons,
         args.start, args.end, args.coherence_floor,
         args.extra_round_trip_cost_bps, side_policies,
         args.lower_tail_quantile, args.upper_tail_quantile)
        for s in symbols
    ]
    cells: list[dict[str, Any]] = []
    with ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=_init_panel_worker,
        initargs=(
            symbols,
            args.csv_root,
            args.bar_minutes,
            args.start,
            args.end,
        ),
    ) as pool:
        for result in pool.map(screen_panel_symbol, jobs):
            cells.extend(result)

    accounting = panel_trial_accounting(
        n_symbols=len(symbols),
        n_horizons=len(horizons),
        prior_tests=args.prior_tests,
        n_side_policies=len(side_policies),
    )
    total_tests = accounting["cumulative_tests"]
    threshold = search_corrected_threshold(total_tests)
    print(
        f"\n{len(cells)} cells reported from {accounting['current_cells']} attempted "
        f"({accounting['base_cells']} existing + "
        f"{accounting['new_lagged_cells']} new lagged across "
        f"{accounting['orientation_multiplier']} side policy/policies); "
        f"cumulative search size "
        f"{total_tests} = {accounting['prior_tests']} prior + "
        f"{accounting['current_cells']} current -> corrected |t| threshold "
        f"{threshold:.2f}\n"
    )

    monetizable = [
        c for c in cells
        if c["long_n_days"] >= args.min_days
        and c["short_n_days"] >= args.min_days
        and c["long_t"] >= threshold
        and c["short_t"] >= threshold
        and c["long_bps"] > 0
        and c["short_bps"] > 0
        and _ic_supports_policy(c, threshold=threshold)
    ]
    informative = [
        c for c in cells
        if c["n_days"] >= args.min_days
        and _ic_supports_policy(c, threshold=threshold)
    ]
    print(f"{'feature':<28}{'policy':<10}{'pair':<8}{'H':>3}{'n':>7}{'days':>6}{'IC':>8}"
          f"{'IC_t':>7}{'mid_bps':>9}{'trade_bps':>11}{'trade_t':>9}")
    for c in sorted(cells, key=lambda c: -c["tradable_t"])[:15]:
        print(f"{c['feature']:<28}{c['side_policy']:<10}{c['symbol']:<8}"
              f"{c['horizon']:>3}{c['n_obs']:>7}"
              f"{c['n_days']:>6}{c['ic']:>8.4f}{c['ic_t']:>7.2f}"
              f"{c['mid_bps']:>9.3f}{c['tradable_bps']:>11.3f}{c['tradable_t']:>9.2f}")
    print(f"\nBOTH-DIRECTION MONETIZABLE after correction: {len(monetizable)}")
    for c in monetizable:
        print(f"  {c['feature']} {c['side_policy']} {c['symbol']} H{c['horizon']}: "
              f"{c['tradable_bps']:+.3f}bps t={c['tradable_t']:.2f} "
              f"BUY={c['long_bps']:+.3f}bps/t={c['long_t']:.2f}/"
              f"wr={c['long_win_rate']:.1%}; "
              f"SELL={c['short_bps']:+.3f}bps/t={c['short_t']:.2f}/"
              f"wr={c['short_win_rate']:.1%} ({c['n_days']} days)")
    print(f"INFORMATIVE (mid) after correction: {len(informative)}")
    print("  (max_cost = the round-trip cost at which this edge breaks even;"
          " implied = what the data actually charged)")
    for c in sorted(informative, key=lambda c: -abs(c["ic_t"]))[:10]:
        # tradable = mid - cost  =>  cost = mid - tradable. The edge is
        # monetizable only at a venue cheaper than its own gross edge.
        implied_cost = c["mid_bps"] - c["tradable_bps"]
        print(f"  {c['feature']} {c['side_policy']} {c['symbol']} H{c['horizon']}: "
              f"IC={c['ic']:+.4f} t={c['ic_t']:.2f} mid={c['mid_bps']:+.3f}bps "
              f"tradable={c['tradable_bps']:+.3f}bps | "
              f"max_cost={c['mid_bps']:.3f}bps implied={implied_cost:.3f}bps "
              f"({implied_cost / max(1e-9, c['mid_bps']):.1f}x too expensive)")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(cells, indent=1, allow_nan=False), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
