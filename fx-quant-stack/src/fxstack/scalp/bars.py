"""M1 bar aggregation from the live tick stream, with gap invalidation.

The EA broadcasts ~1 tick/sec/symbol to the bridge; this folds them into M1
OHLC (mid) with bid/ask at close and the max spread seen inside the bar. A bar
built from too few ticks, or a minute with no ticks at all, is recorded as
INVALID -- signals require an unbroken run of valid bars, so a laptop sleep or
feed outage silently produces "no signal" instead of fake bars.

Finalized bars append to a per-symbol JSONL file so restarts keep history and
the ledger's bars are auditable after the fact.
"""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
        mid = (bid_f + ask_f) / 2.0
        spread = max(0.0, _finite(spread_bps))
        minute = int(ts_epoch // 60) * 60
        cur = self._current.get(sym)
        finalized: list[M1Bar] = []
        if cur is not None and minute > cur["minute"]:
            finalized.extend(self._finalize(sym, up_to_minute=minute))
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
        return finalized

    def flush_stale(self, *, now_epoch: float) -> list[M1Bar]:
        """Finalize any bucket whose minute has fully elapsed with no new tick."""
        out: list[M1Bar] = []
        boundary = int(now_epoch // 60) * 60
        for sym in list(self._current):
            if self._current[sym]["minute"] < boundary:
                out.extend(self._finalize(sym, up_to_minute=boundary))
        return out

    def _finalize(self, sym: str, *, up_to_minute: int) -> list[M1Bar]:
        cur = self._current.pop(sym, None)
        if cur is None:
            return []
        out: list[M1Bar] = []
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
            tick_count=int(cur["ticks"]),
            valid=int(cur["ticks"]) >= self.min_ticks,
            invalid_reason="" if int(cur["ticks"]) >= self.min_ticks else "too_few_ticks",
        )
        out.append(bar)
        # Explicit invalid markers for every fully-missed minute in between, so
        # the history NEVER silently bridges a feed outage.
        for missed in range(int(cur["minute"]) + 60, int(up_to_minute), 60):
            out.append(
                M1Bar(
                    symbol=sym,
                    minute_epoch=missed,
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
                    invalid_reason="no_ticks_in_minute",
                )
            )
        for bar_out in out:
            self._history[sym].append(bar_out)
            self._persist(bar_out)
        return out

    def _persist(self, bar: M1Bar) -> None:
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
