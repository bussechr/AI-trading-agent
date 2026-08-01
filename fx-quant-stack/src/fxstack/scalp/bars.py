"""M1 bar aggregation from the live tick stream, with gap invalidation.

The EA broadcasts ~1 tick/sec/symbol to the bridge; this folds them into M1
OHLC (mid) with bid/ask at close and the max spread seen inside the bar.

Honesty invariants (adversarial review 2026-08-01 hardened all four):
- A minute with no ticks, or with too few, is an explicit INVALID bar. This
  includes flush-then-resume outages: resuming after silence first emits gap
  markers, so ``consecutive_valid`` can never splice history across an outage.
- Duplicate polls of the same tick (ts not advancing) are dropped, so
  ``tick_count`` counts market ticks, not poll iterations.
- A bar whose quotes never CHANGED is invalid (``frozen_quotes``): a stale
  feed re-broadcasting one quote with advancing timestamps cannot manufacture
  valid history.
- Ticks for minutes at or before the last finalized minute are dropped -- a
  finalized past is immutable, never rebuilt or churned.

Finalized bars append to a per-symbol JSONL file so restarts keep an audit
trail (in-memory history is NOT reloaded on restart; the loop replays the
day's ledger instead and the warmup requirement re-arms naturally).
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: At most this many individual gap markers are written per outage; longer
#: outages collapse into one summary marker (one invalid bar already breaks
#: the consecutive-valid run -- thousands of markers after a weekend add
#: nothing but noise).
_MAX_GAP_MARKERS = 3


@dataclass(slots=True)
class M1Bar:
    symbol: str
    minute_epoch: int  # UTC epoch of the minute START
    open: float
    high: float
    low: float
    close: float
    bid_close: float
    ask_close: float
    spread_max_bps: float
    spread_close_bps: float
    tick_count: int
    valid: bool
    invalid_reason: str = ""
    quote_changes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


class M1Aggregator:
    """Folds ticks into per-symbol M1 bars; emits finalized bars on rollover."""

    def __init__(
        self,
        *,
        symbols: list[str],
        min_ticks_per_bar: int = 3,
        history_bars: int = 600,
        persist_root: Path | None = None,
    ) -> None:
        self.symbols = [str(s).upper() for s in symbols]
        self.min_ticks = max(1, int(min_ticks_per_bar))
        self.persist_root = persist_root
        self._current: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque[M1Bar]] = {
            s: deque(maxlen=int(history_bars)) for s in self.symbols
        }
        self._last_ts: dict[str, float] = {}
        self._last_quote: dict[str, tuple[float, float]] = {}
        self._last_emitted_minute: dict[str, int] = {}
        if persist_root is not None:
            persist_root.mkdir(parents=True, exist_ok=True)

    def history(self, symbol: str) -> list[M1Bar]:
        return list(self._history.get(str(symbol).upper(), []))

    def consecutive_valid(self, symbol: str) -> list[M1Bar]:
        """The unbroken run of valid bars ending at the most recent bar."""
        run: list[M1Bar] = []
        for bar in reversed(self.history(symbol)):
            if not bar.valid:
                break
            run.append(bar)
        run.reverse()
        return run

    def ingest_tick(
        self, *, symbol: str, bid: float, ask: float, spread_bps: float, ts_epoch: float
    ) -> list[M1Bar]:
        """Feed one tick; returns bars finalized by this tick's arrival."""
        sym = str(symbol).upper()
        if sym not in self._history:
            return []
        bid_f = _finite(bid)
        ask_f = _finite(ask)
        if bid_f <= 0.0 or ask_f <= 0.0 or ask_f < bid_f:
            return []
        # Duplicate-poll guard: the loop re-reads the bridge's last-known tick
        # every second; only a strictly advancing timestamp is a new tick.
        if float(ts_epoch) <= self._last_ts.get(sym, 0.0):
            return []
        self._last_ts[sym] = float(ts_epoch)

        minute = int(ts_epoch // 60) * 60
        last_emitted = self._last_emitted_minute.get(sym)
        if last_emitted is not None and minute <= last_emitted:
            # The finalized past is immutable -- a late tick for an already
            # emitted minute is dropped, never allowed to churn history.
            return []

        finalized: list[M1Bar] = []
        cur = self._current.get(sym)
        if cur is not None and minute > cur["minute"]:
            finalized.extend(self._finalize(sym, up_to_minute=minute))
            cur = self._current.get(sym)
        elif cur is None and last_emitted is not None and minute > last_emitted + 60:
            # Flush-then-resume outage: mark the silent minutes BEFORE the new
            # bucket exists, so the valid run is broken at the gap.
            finalized.extend(
                self._gap_markers(sym, start_minute=last_emitted + 60, end_minute=minute)
            )

        quote = (bid_f, ask_f)
        prior_quote = self._last_quote.get(sym)
        # The very first observed quote is not a "change" -- a frozen feed's
        # first bar must be frozen, not granted one free movement.
        quote_changed = prior_quote is not None and quote != prior_quote
        self._last_quote[sym] = quote
        mid = (bid_f + ask_f) / 2.0
        spread = max(0.0, _finite(spread_bps))
        cur = self._current.get(sym)
        if cur is None or cur["minute"] != minute:
            self._current[sym] = {
                "minute": minute,
                "open": mid,
                "high": mid,
                "low": mid,
                "close": mid,
                "bid_close": bid_f,
                "ask_close": ask_f,
                "spread_max": spread,
                "spread_close": spread,
                "ticks": 1,
                "quote_changes": 1 if quote_changed else 0,
            }
        else:
            cur["high"] = max(cur["high"], mid)
            cur["low"] = min(cur["low"], mid)
            cur["close"] = mid
            cur["bid_close"] = bid_f
            cur["ask_close"] = ask_f
            cur["spread_max"] = max(cur["spread_max"], spread)
            cur["spread_close"] = spread
            cur["ticks"] += 1
            if quote_changed:
                cur["quote_changes"] += 1
        return finalized

    def flush_stale(self, *, now_epoch: float) -> list[M1Bar]:
        """Finalize any bucket whose minute has fully elapsed with no new tick."""
        out: list[M1Bar] = []
        boundary = int(now_epoch // 60) * 60
        for sym in list(self._current):
            if self._current[sym]["minute"] < boundary:
                out.extend(self._finalize(sym, up_to_minute=boundary))
        return out

    # ------------------------------------------------------------- internals

    def _finalize(self, sym: str, *, up_to_minute: int) -> list[M1Bar]:
        cur = self._current.pop(sym, None)
        if cur is None:
            return []
        ticks = int(cur["ticks"])
        changes = int(cur["quote_changes"])
        if ticks < self.min_ticks:
            valid, why = False, "too_few_ticks"
        elif changes < 1:
            valid, why = False, "frozen_quotes"
        else:
            valid, why = True, ""
        bar = M1Bar(
            symbol=sym,
            minute_epoch=int(cur["minute"]),
            open=float(cur["open"]),
            high=float(cur["high"]),
            low=float(cur["low"]),
            close=float(cur["close"]),
            bid_close=float(cur["bid_close"]),
            ask_close=float(cur["ask_close"]),
            spread_max_bps=float(cur["spread_max"]),
            spread_close_bps=float(cur["spread_close"]),
            tick_count=ticks,
            valid=valid,
            invalid_reason=why,
            quote_changes=changes,
        )
        out = [bar]
        self._emit(bar)
        out.extend(
            self._gap_markers(
                sym, start_minute=int(cur["minute"]) + 60, end_minute=int(up_to_minute)
            )
        )
        return out

    def _gap_markers(self, sym: str, *, start_minute: int, end_minute: int) -> list[M1Bar]:
        missed = list(range(int(start_minute), int(end_minute), 60))
        if not missed:
            return []
        out: list[M1Bar] = []
        summarised = len(missed) > _MAX_GAP_MARKERS
        emit_minutes = missed[:_MAX_GAP_MARKERS] if summarised else missed
        for minute in emit_minutes:
            out.append(self._invalid_marker(sym, minute, "no_ticks_in_minute"))
        if summarised:
            out.append(
                self._invalid_marker(
                    sym, missed[-1], f"no_ticks_gap_{len(missed)}_minutes"
                )
            )
        for marker in out:
            self._emit(marker)
        return out

    def _invalid_marker(self, sym: str, minute: int, reason: str) -> M1Bar:
        return M1Bar(
            symbol=sym,
            minute_epoch=int(minute),
            open=0.0,
            high=0.0,
            low=0.0,
            close=0.0,
            bid_close=0.0,
            ask_close=0.0,
            spread_max_bps=0.0,
            spread_close_bps=0.0,
            tick_count=0,
            valid=False,
            invalid_reason=reason,
        )

    def _emit(self, bar: M1Bar) -> None:
        self._history[bar.symbol].append(bar)
        self._last_emitted_minute[bar.symbol] = max(
            self._last_emitted_minute.get(bar.symbol, 0), int(bar.minute_epoch)
        )
        if self.persist_root is None:
            return
        path = self.persist_root / f"{bar.symbol}_M1.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(bar.to_dict(), separators=(",", ":")) + "\n")
        except OSError:
            # Persistence is best-effort; in-memory history stays authoritative
            # for signals, and the ledger records every decision regardless.
            pass


def window_is_complete(minute_epoch: int, *, bar_minutes: int) -> bool:
    """True when ``minute_epoch`` is the LAST minute of an aggregation window.

    Windows are aligned to the hour (bar_minutes divides 60), so an M15
    engine bar always closes at :14, :29, :44, :59 -- session boundaries and
    the opening-range family depend on that alignment.
    """
    step = max(1, int(bar_minutes))
    if step == 1:
        return True
    return ((int(minute_epoch) // 60) + 1) % step == 0


def aggregate_bars(bars: list[M1Bar], *, bar_minutes: int) -> list[M1Bar]:
    """Fold M1 bars into hour-aligned ``bar_minutes`` candles.

    Honesty rules carried from the aggregator: a window containing ANY
    invalid minute is dropped entirely (never silently stitched from partial
    data), and a window missing minutes is dropped too -- an engine bar must
    represent a fully observed interval. Spread fields keep the worst case
    seen inside the window, so cost gates read the window's true adversity.
    """
    step = max(1, int(bar_minutes))
    if step == 1:
        return list(bars)
    out: list[M1Bar] = []
    bucket: list[M1Bar] = []
    for bar in bars:
        minute_index = int(bar.minute_epoch) // 60
        bucket_start = (minute_index // step) * step
        if bucket and (int(bucket[0].minute_epoch) // 60) // step != bucket_start // step:
            out.extend(_fold(bucket, step))
            bucket = []
        bucket.append(bar)
    out.extend(_fold(bucket, step))
    return out


def _fold(bucket: list[M1Bar], step: int) -> list[M1Bar]:
    if len(bucket) != step:
        return []  # incomplete window -- not an observed interval
    if any(not b.valid for b in bucket):
        return []
    first, last = bucket[0], bucket[-1]
    return [
        M1Bar(
            symbol=first.symbol,
            minute_epoch=int(first.minute_epoch),
            open=first.open,
            high=max(b.high for b in bucket),
            low=min(b.low for b in bucket),
            close=last.close,
            bid_close=last.bid_close,
            ask_close=last.ask_close,
            spread_max_bps=max(b.spread_max_bps for b in bucket),
            spread_close_bps=last.spread_close_bps,
            tick_count=sum(b.tick_count for b in bucket),
            valid=True,
            invalid_reason="",
            quote_changes=sum(b.quote_changes for b in bucket),
        )
    ]


def atr_bps(bars: list[M1Bar], *, periods: int = 14) -> float:
    """Average true range of the last ``periods`` valid bars, in bps of close."""
    usable = [b for b in bars if b.valid and b.close > 0]
    if len(usable) < 2:
        return 0.0
    trs: list[float] = []
    for prev, cur in zip(usable, usable[1:]):
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr / cur.close * 1e4)
    tail = trs[-max(1, int(periods)):]
    return sum(tail) / len(tail)


def ema(values: list[float], *, periods: int) -> float:
    if not values:
        return 0.0
    k = 2.0 / (float(max(1, periods)) + 1.0)
    acc = values[0]
    for v in values[1:]:
        acc = v * k + acc * (1.0 - k)
    return acc
