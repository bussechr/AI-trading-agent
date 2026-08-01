"""Offline M1 backtest of the LIVE scalp engine over bid/ask history.

This is not a parallel implementation of the strategy: signals come from
``fxstack.scalp.signals.evaluate_dislocation`` (the exact function the live
loop calls), sessions from ``fxstack.scalp.gates.session_veto_reason``, and
the fill model mirrors ``fxstack.scalp.shadow.ShadowBook``'s honesty rules at
bar granularity. What differs from live is only what history forces:

- Entry fills at the NEXT bar's open quote, taken ADVERSE against the signal
  bar's close quote (the bar-granularity analogue of the live loop's
  ``_freshen_entry``); a gap after the signal bar refuses the entry.
- A bar's OPEN is its first quote, so ordering at the open is knowable: a bar
  opening through the SL books the OPEN price (full gap loss, never a
  truncated -1R); a bar opening through the TP books the TP (the limit filled
  before any path to the stop existed).
- Otherwise TP exits fill AT the level (never the intrabar extreme) and SL
  exits fill AT the level plus ``sl_extra_slip_bps`` (live books the observed
  through-price; the shadow ledger measures that slippage).
- When one bar's EXTREMES touch both levels with the open inside the bracket,
  the SL wins -- genuinely unknowable ordering never resolves in our favor.
- Exits price off the REAL adverse side (bid extremes for longs, ask extremes
  for shorts) -- the data carries true bid/ask OHLC, so spread cost is
  intrinsic to every fill, not an assumption.
- History honesty matches the aggregator: a missing minute breaks the
  consecutive-valid run; a frozen bar (no quote movement) is invalid.

VENUE COSTS: the data is interbank; IG retail spreads are wider (EURUSD
~1.2bps budget vs ~0.3bps interbank median). Runs default to raw interbank
quotes and are labeled ``venue=interbank_raw`` in the output -- GO/NO-GO
decisions require a venue-realistic ``extra_spread_bps`` derived from the
live sentinel's measured IG spreads (per-pair, once FX is open). Commission
is zero on IG CFD FX; swap is irrelevant at 5-30min holds.

Run:  python -m fxstack.scalp.backtest --symbols EURUSD,USDJPY \
          --csv-root fx-quant-stack/data/dukascopy [--start 2024-01-01]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from fxstack.scalp.bars import M1Bar
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.gates import session_veto_reason
from fxstack.scalp.shadow import ShadowFill
from fxstack.scalp.signals import ScalpIntent, evaluate_dislocation

#: Signal window cap passed to evaluate_dislocation. Live evaluates over the
#: unbroken valid run (deque max 600); EMA20's weight on bars older than 120
#: is (1-2/21)^120 < 1e-5, so a 120-bar cap is numerically identical and ~5x
#: faster over multi-year files.
RUN_WINDOW_BARS = 120


@dataclass(slots=True)
class BtBar:
    """One historical M1 bar: the engine-facing M1Bar plus real quote extremes."""

    bar: M1Bar
    bid_open: float
    bid_high: float
    bid_low: float
    ask_open: float
    ask_high: float
    ask_low: float


@dataclass(slots=True)
class BtPosition:
    intent: ScalpIntent
    entry_price: float
    sl_price: float
    tp_price: float
    entry_minute: int
    bars_held: int = 0


@dataclass(slots=True)
class BtStats:
    bars_total: int = 0
    bars_valid: int = 0
    gaps: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    fills: list[ShadowFill] = field(default_factory=list)

    def count(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def _mid(bid: float, ask: float) -> float:
    return (bid + ask) / 2.0


def load_bt_bars(
    csv_path: Path,
    *,
    symbol: str,
    start_epoch: float | None = None,
    end_epoch: float | None = None,
    extra_spread_bps: float = 0.0,
) -> Iterator[BtBar]:
    """Yield BtBars from a Dukascopy-style bid/ask OHLC CSV, oldest first.

    Validity mirrors the live aggregator: a bar with no quote movement at all
    is ``frozen_quotes``; degenerate quotes are invalid. Missing minutes are
    NOT filled in -- the runner detects the epoch jump and breaks the run.
    """
    sym = str(symbol).upper()
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header or header[0] != "timestamp":
            raise ValueError(f"{csv_path}: unexpected header {header!r}")
        for row in reader:
            try:
                ts = dt.datetime.fromisoformat(row[0].replace("Z", "+00:00"))
                epoch = ts.timestamp()
                bo, bh, bl, bc = (float(row[1]), float(row[2]), float(row[3]), float(row[4]))
                ao, ah, al, ac = (float(row[5]), float(row[6]), float(row[7]), float(row[8]))
            except (ValueError, IndexError):
                continue
            if start_epoch is not None and epoch < start_epoch:
                continue
            if end_epoch is not None and epoch >= end_epoch:
                break
            if extra_spread_bps > 0.0:
                pad = extra_spread_bps / 1e4 * _mid(bc, ac) / 2.0
                bo, bh, bl, bc = bo - pad, bh - pad, bl - pad, bc - pad
                ao, ah, al, ac = ao + pad, ah + pad, al + pad, ac + pad
            minute = int(epoch // 60) * 60
            valid = True
            why = ""
            if not all(map(math.isfinite, (bo, bh, bl, bc, ao, ah, al, ac))):
                valid, why = False, "degenerate_quotes"
            elif min(bo, bh, bl, bc, ao, ah, al, ac) <= 0.0 or ac < bc or ah < bh or al < bl:
                valid, why = False, "degenerate_quotes"
            elif bh == bl and ah == al and bo == bc:
                valid, why = False, "frozen_quotes"
            mid_c = _mid(bc, ac)
            spreads = (ao - bo, ah - bh, al - bl, ac - bc)
            spread_close = max(0.0, (ac - bc) / mid_c * 1e4) if mid_c > 0 else 0.0
            spread_max = max(0.0, max(spreads) / mid_c * 1e4) if mid_c > 0 else 0.0
            bar = M1Bar(
                symbol=sym,
                minute_epoch=minute,
                open=_mid(bo, ao),
                high=_mid(bh, ah),
                low=_mid(bl, al),
                close=mid_c,
                bid_close=bc,
                ask_close=ac,
                spread_max_bps=spread_max,
                spread_close_bps=spread_close,
                tick_count=3 if valid else 0,
                valid=valid,
                invalid_reason=why,
                quote_changes=1 if valid else 0,
            )
            yield BtBar(
                bar=bar, bid_open=bo, bid_high=bh, bid_low=bl,
                ask_open=ao, ask_high=ah, ask_low=al,
            )


class BacktestRunner:
    """Single-symbol replay with ShadowBook-equivalent fill honesty."""

    def __init__(self, *, config: ScalpConfig, sl_extra_slip_bps: float = 0.0) -> None:
        self.config = config
        self.sl_extra_slip_bps = max(0.0, float(sl_extra_slip_bps))
        self.stats = BtStats()
        self._run: list[M1Bar] = []
        self._pos: BtPosition | None = None
        self._pending: ScalpIntent | None = None
        self._cooldown = 0
        self._day_key = ""
        self._day_r = 0.0

    # ------------------------------------------------------------------ flow

    def process(self, bt: BtBar) -> None:
        bar = bt.bar
        self.stats.bars_total += 1
        fills_before = len(self.stats.fills)

        gap = bool(self._run) and bar.minute_epoch > self._run[-1].minute_epoch + 60
        if gap:
            self.stats.gaps += 1
            self._run.clear()
            # An entry signalled just before a data gap cannot honestly fill.
            if self._pending is not None:
                self._pending = None
                self.stats.count("entry_refused_gap")

        self._roll_day(bar.minute_epoch)

        # 1) Fill a pending entry at THIS bar's open quote, adverse vs signal.
        if self._pending is not None:
            self._fill_pending(bt)

        # 2) Manage any open position against this bar's real adverse extremes.
        if self._pos is not None:
            self._manage_position(bt)

        # 3) Extend or break the valid run; entry pipeline runs at bar close.
        if bar.valid:
            self.stats.bars_valid += 1
            self._run.append(bar)
            if len(self._run) > RUN_WINDOW_BARS:
                del self._run[0]
            self._maybe_enter(bar)
        else:
            self._run.clear()

        # Cooldown ticks at end of bar and NEVER on a bar that produced a
        # fill -- the full cooldown_bars must elapse after every exit.
        if self._cooldown > 0 and len(self.stats.fills) == fills_before:
            self._cooldown -= 1

    def _maybe_enter(self, bar: M1Bar) -> None:
        # Entry pipeline at bar close -- live loop's veto order.
        if self._pos is not None or self._pending is not None:
            self.stats.count("position_already_open" if self._pos else "entry_pending")
            return
        if self._day_r <= self.config.daily_loss_stop_r:
            self.stats.count("daily_loss_breaker")
            return
        if self._cooldown > 0:
            self.stats.count("cooldown")
            return
        session_block = session_veto_reason(
            symbol=bar.symbol, now_epoch=float(bar.minute_epoch + 60), config=self.config
        )
        if session_block:
            self.stats.count(session_block)
            return
        spread = bar.spread_close_bps
        budget = float(self.config.spread_budgets_bps.get(bar.symbol, 0.0))
        if budget <= 0.0:
            self.stats.count("symbol_unqualified")
            return
        if spread <= 0.0:
            self.stats.count("spread_unresolved")
            return
        if spread > budget:
            self.stats.count("spread_over_budget")
            return
        if len(self._run) < self.config.min_history_bars:
            self.stats.count("insufficient_valid_history")
            return
        intent, reason = evaluate_dislocation(
            bars=list(self._run), config=self.config, spread_bps=spread
        )
        if intent is None:
            self.stats.count(reason)
            return
        self.stats.count("intent")
        self._pending = intent

    def finish(self) -> None:
        """End of data: an open position is closed at the last known close
        quote and recorded as ``eod`` (it is NOT dropped -- dropping losers
        at the boundary flatters the ledger)."""
        if self._pos is not None and self._run:
            last = self._run[-1]
            px = last.bid_close if self._pos.intent.side == "BUY" else last.ask_close
            if px > 0:
                self._close(px, "eod", minute=last.minute_epoch)

    # ------------------------------------------------------------- internals

    def _fill_pending(self, bt: BtBar) -> None:
        intent = self._pending
        assert intent is not None
        self._pending = None
        bar = bt.bar
        if bar.minute_epoch != intent.minute_epoch + 60:
            self.stats.count("entry_refused_gap")
            return
        current = bt.ask_open if intent.side == "BUY" else bt.bid_open
        if current <= 0.0:
            self.stats.count("no_fresh_entry_quote")
            return
        # Adverse of {signal-bar touch, next-open touch} -- _freshen_entry.
        if intent.side == "BUY":
            entry = max(intent.entry_price, current)
        else:
            entry = min(intent.entry_price, current)
        stop_px = intent.stop_bps / 1e4 * intent.ref_mid
        tp_px = abs(intent.tp_price - intent.entry_price)
        if intent.side == "BUY":
            sl_price, tp_price = entry - stop_px, entry + tp_px
        else:
            sl_price, tp_price = entry + stop_px, entry - tp_px
        self._pos = BtPosition(
            intent=intent, entry_price=entry, sl_price=sl_price, tp_price=tp_price,
            entry_minute=bar.minute_epoch,
        )
        self.stats.count("opened")

    def _manage_position(self, bt: BtBar) -> None:
        pos = self._pos
        assert pos is not None
        bar = bt.bar
        if bar.minute_epoch < pos.entry_minute:
            return
        buy = pos.intent.side == "BUY"
        slip = self.sl_extra_slip_bps / 1e4 * pos.entry_price

        # The bar's OPEN is its first quote, so ordering at the open is
        # KNOWABLE: a bar that opens beyond a level hit that level before any
        # intrabar path existed (adversarial review 2026-08-01, both lenses).
        open_adverse = bt.bid_open if buy else bt.ask_open
        opened_through_sl = (
            (open_adverse <= pos.sl_price) if buy else (open_adverse >= pos.sl_price)
        )
        if opened_through_sl:
            # Gap through the stop: the honest fill is the open, which is
            # strictly worse than the level -- a weekend/news gap books its
            # full loss, never a truncated -1R.
            self.stats.count("sl_gap_open")
            px = open_adverse - slip if buy else open_adverse + slip
            self._close(px, "sl", minute=bar.minute_epoch)
            return
        opened_through_tp = (
            (open_adverse >= pos.tp_price) if buy else (open_adverse <= pos.tp_price)
        )
        if opened_through_tp:
            # Gap through the TP: the limit filled at the bar's first quote;
            # booking the later intrabar stop would punish knowable ordering.
            self.stats.count("tp_gap_open")
            self._close(pos.tp_price, "tp", minute=bar.minute_epoch)
            return

        # Exits happen on the adverse side: bid for longs, ask for shorts.
        worst = bt.bid_low if buy else bt.ask_high
        best = bt.bid_high if buy else bt.ask_low
        sl_hit = (worst <= pos.sl_price) if buy else (worst >= pos.sl_price)
        tp_hit = (best >= pos.tp_price) if buy else (best <= pos.tp_price)
        if sl_hit:
            if tp_hit:
                # Genuinely ambiguous double-touch (open inside the bracket):
                # SL-first -- intrabar ordering never resolves in our favor.
                self.stats.count("sl_double_touch")
            px = pos.sl_price - slip if buy else pos.sl_price + slip
            self._close(px, "sl", minute=bar.minute_epoch)
            return
        if tp_hit:
            self._close(pos.tp_price, "tp", minute=bar.minute_epoch)
            return
        pos.bars_held += 1
        if pos.bars_held >= pos.intent.time_stop_bars:
            px = bt.bar.bid_close if buy else bt.bar.ask_close
            if px > 0:
                self._close(px, "time_stop", minute=bar.minute_epoch)

    def _close(self, exit_price: float, reason: str, *, minute: int) -> None:
        pos = self._pos
        assert pos is not None
        self._pos = None
        direction = 1.0 if pos.intent.side == "BUY" else -1.0
        pnl_px = (exit_price - pos.entry_price) * direction
        risk_px = abs(pos.entry_price - pos.sl_price)
        pnl_r = pnl_px / risk_px if risk_px > 0 else 0.0
        pnl_bps = pnl_px / pos.entry_price * 1e4 if pos.entry_price > 0 else 0.0
        fill = ShadowFill(
            symbol=pos.intent.symbol,
            side=pos.intent.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            exit_reason=reason,
            bars_held=pos.bars_held,
            pnl_r=pnl_r,
            pnl_bps=pnl_bps,
            lots=0.0,
            opened_minute=pos.entry_minute,
            exit_epoch=float(minute + 60),
            meta=dict(
                p_star=pos.intent.p_star, disp_z=pos.intent.disp_z,
                atr_bps=pos.intent.atr_bps, spread_bps=pos.intent.spread_bps,
            ),
        )
        self.stats.fills.append(fill)
        self._day_r += pnl_r
        self._cooldown = self.config.cooldown_bars

    def _roll_day(self, minute_epoch: int) -> None:
        key = dt.datetime.fromtimestamp(minute_epoch, dt.timezone.utc).strftime("%Y-%m-%d")
        if key != self._day_key:
            self._day_key = key
            self._day_r = 0.0


# ------------------------------------------------------------------ reporting

def bootstrap_ci_mean(
    values: list[float], *, n_boot: int = 5000, seed: int = 1337
) -> tuple[float, float]:
    """Percentile bootstrap 95% CI of the mean; deterministic."""
    if not values:
        return 0.0, 0.0
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(values[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot)
    )
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot)]


def summarize(symbol: str, stats: BtStats) -> dict[str, Any]:
    fills = stats.fills
    rs = [f.pnl_r for f in fills]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    total_r = sum(rs)
    mean_r = total_r / n if n else 0.0
    ci_lo, ci_hi = bootstrap_ci_mean(rs)
    # Max drawdown on the cumulative R curve.
    peak = acc = 0.0
    max_dd = 0.0
    for r in rs:
        acc += r
        peak = max(peak, acc)
        max_dd = max(max_dd, peak - acc)
    by_year: dict[str, dict[str, float]] = {}
    for f in fills:
        year = dt.datetime.fromtimestamp(f.exit_epoch, dt.timezone.utc).strftime("%Y")
        y = by_year.setdefault(year, {"trades": 0, "r": 0.0})
        y["trades"] += 1
        y["r"] += f.pnl_r
    exit_mix = {
        reason: sum(1 for f in fills if f.exit_reason == reason)
        for reason in sorted({f.exit_reason for f in fills})
    }
    return {
        "symbol": symbol,
        "bars_total": stats.bars_total,
        "bars_valid": stats.bars_valid,
        "gaps": stats.gaps,
        "trades": n,
        "win_rate": len(wins) / n if n else 0.0,
        "avg_p_star": sum(f.meta.get("p_star", 0.0) for f in fills) / n if n else 0.0,
        "total_r": total_r,
        "mean_r": mean_r,
        "mean_r_ci95": [ci_lo, ci_hi],
        "mean_pnl_bps": sum(f.pnl_bps for f in fills) / n if n else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_win_r": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss_r": sum(losses) / len(losses) if losses else 0.0,
        "max_drawdown_r": max_dd,
        "avg_bars_held": sum(f.bars_held for f in fills) / n if n else 0.0,
        "exit_mix": exit_mix,
        "by_year": by_year,
        "reasons": dict(sorted(stats.reasons.items(), key=lambda kv: -kv[1])),
    }


def run_symbol(
    *,
    symbol: str,
    csv_root: Path,
    config: ScalpConfig,
    start_epoch: float | None,
    end_epoch: float | None,
    extra_spread_bps: float,
    sl_extra_slip_bps: float,
    trades_out: Path | None = None,
) -> dict[str, Any]:
    csv_path = csv_root / f"{symbol}_M1.csv"
    if not csv_path.exists():
        return {"symbol": symbol, "error": f"missing {csv_path}"}
    runner = BacktestRunner(config=config, sl_extra_slip_bps=sl_extra_slip_bps)
    for bt in load_bt_bars(
        csv_path,
        symbol=symbol,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        extra_spread_bps=extra_spread_bps,
    ):
        runner.process(bt)
    runner.finish()
    summary = summarize(symbol, runner.stats)
    # Cost provenance: a run at raw interbank quotes must never be mistaken
    # for a venue-realistic one when reading the JSON later.
    summary["extra_spread_bps"] = float(extra_spread_bps)
    summary["sl_extra_slip_bps"] = float(sl_extra_slip_bps)
    summary["venue"] = (
        "interbank_raw" if extra_spread_bps <= 0.0 else f"interbank+{extra_spread_bps}bps"
    )
    if trades_out is not None:
        # Per-trade dump for fxstack.scalp.validate -- the arming battery
        # slices by time and side, which aggregates cannot support.
        with trades_out.open("a", encoding="utf-8") as fh:
            for f in runner.stats.fills:
                fh.write(
                    json.dumps(
                        {
                            "symbol": f.symbol,
                            "side": f.side,
                            "r": f.pnl_r,
                            "bps": f.pnl_bps,
                            "epoch": f.exit_epoch,
                            "exit_reason": f.exit_reason,
                            "p_star": f.meta.get("p_star"),
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
    return summary


def _parse_date(text: str | None) -> float | None:
    if not text:
        return None
    return dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", required=True, help="comma-separated, e.g. EURUSD,USDJPY")
    ap.add_argument("--csv-root", default="fx-quant-stack/data/dukascopy")
    ap.add_argument("--start", default=None, help="UTC date, e.g. 2024-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--extra-spread-bps", type=float, default=0.0)
    ap.add_argument("--sl-extra-slip-bps", type=float, default=0.0)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--trades-out", default=None,
                    help="append per-trade JSONL here (input to scalp.validate)")
    args = ap.parse_args(argv)

    config = ScalpConfig()
    errors = [e for e in config.validate() if "shadow" not in e]
    if errors:
        raise SystemExit(f"config invalid: {errors}")

    results = []
    for symbol in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
        result = run_symbol(
            symbol=symbol,
            csv_root=Path(args.csv_root),
            config=config,
            start_epoch=_parse_date(args.start),
            end_epoch=_parse_date(args.end),
            extra_spread_bps=args.extra_spread_bps,
            sl_extra_slip_bps=args.sl_extra_slip_bps,
            trades_out=Path(args.trades_out) if args.trades_out else None,
        )
        results.append(result)
        print(json.dumps(result, separators=(",", ":"), default=str))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, indent=1, default=str), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
