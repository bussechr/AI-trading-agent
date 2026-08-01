"""Shadow book: paper positions filled from LIVE ticks with the spread paid.

Entries fill at the touch (ask for BUY, bid for SELL). Exits fill at the
opposing touch. TP/SL trigger off the adverse quote, time stops off bar count.
PnL is tracked in R (risk units) and bps -- venue-agnostic, so crypto exercises
the machinery even while unsizeable in lots. This ledger is the falsification
dataset: it must never flatter (no mid fills, no free spread).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from fxstack.scalp.sizing import SizedIntent


@dataclass(slots=True)
class ShadowPosition:
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    tp_price: float
    lots: float
    opened_minute: int
    time_stop_bars: int
    stop_bps: float
    bars_held: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ShadowFill:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    exit_reason: str  # tp | sl | time_stop
    bars_held: int
    pnl_r: float
    pnl_bps: float
    lots: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ShadowBook:
    def __init__(self, *, max_concurrent: int) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.positions: dict[str, ShadowPosition] = {}
        self.fills: list[ShadowFill] = []
        self.day_r: float = 0.0
        self._day_key: str = ""

    def can_open(self, symbol: str) -> str:
        if str(symbol).upper() in self.positions:
            return "position_already_open"
        if len(self.positions) >= self.max_concurrent:
            return "max_concurrent"
        return ""

    def open_from(self, sized: SizedIntent) -> ShadowPosition:
        intent = sized.intent
        pos = ShadowPosition(
            symbol=intent.symbol,
            side=intent.side,
            entry_price=intent.entry_price,
            sl_price=intent.sl_price,
            tp_price=intent.tp_price,
            lots=sized.lots,
            opened_minute=intent.minute_epoch,
            time_stop_bars=intent.time_stop_bars,
            stop_bps=intent.stop_bps,
            meta={"disp_z": intent.disp_z, "p_star": intent.p_star, "atr_bps": intent.atr_bps},
        )
        self.positions[intent.symbol] = pos
        return pos

    def on_tick(
        self, *, symbol: str, bid: float, ask: float, day_key: str
    ) -> ShadowFill | None:
        """Check TP/SL against the adverse quote; returns a fill if closed."""
        self._roll_day(day_key)
        pos = self.positions.get(str(symbol).upper())
        if pos is None or bid <= 0.0 or ask <= 0.0:
            return None
        if pos.side == "BUY":
            # Exits happen at the BID for a long.
            if bid <= pos.sl_price:
                return self._close(pos, exit_price=bid, reason="sl")
            if bid >= pos.tp_price:
                return self._close(pos, exit_price=bid, reason="tp")
        else:
            # Exits happen at the ASK for a short.
            if ask >= pos.sl_price:
                return self._close(pos, exit_price=ask, reason="sl")
            if ask <= pos.tp_price:
                return self._close(pos, exit_price=ask, reason="tp")
        return None

    def on_bar_close(
        self, *, symbol: str, bid_close: float, ask_close: float, day_key: str
    ) -> ShadowFill | None:
        """Advance the bar clock; enforce the time stop at the closing touch."""
        self._roll_day(day_key)
        pos = self.positions.get(str(symbol).upper())
        if pos is None:
            return None
        pos.bars_held += 1
        if pos.bars_held >= pos.time_stop_bars:
            exit_price = bid_close if pos.side == "BUY" else ask_close
            if exit_price > 0.0:
                return self._close(pos, exit_price=exit_price, reason="time_stop")
        return None

    def _close(self, pos: ShadowPosition, *, exit_price: float, reason: str) -> ShadowFill:
        direction = 1.0 if pos.side == "BUY" else -1.0
        pnl_px = (exit_price - pos.entry_price) * direction
        pnl_bps = pnl_px / pos.entry_price * 1e4 if pos.entry_price > 0 else 0.0
        risk_px = abs(pos.entry_price - pos.sl_price)
        pnl_r = pnl_px / risk_px if risk_px > 0 else 0.0
        fill = ShadowFill(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            exit_reason=reason,
            bars_held=pos.bars_held,
            pnl_r=pnl_r,
            pnl_bps=pnl_bps,
            lots=pos.lots,
        )
        self.positions.pop(pos.symbol, None)
        self.fills.append(fill)
        self.day_r += pnl_r
        return fill

    def _roll_day(self, day_key: str) -> None:
        if day_key != self._day_key:
            self._day_key = day_key
            self.day_r = 0.0
