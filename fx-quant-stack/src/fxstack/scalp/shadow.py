"""Shadow book: paper positions filled from LIVE ticks with the spread paid.

Fill honesty rules (adversarial review 2026-08-01):
- Entries fill at the touch (ask for BUY, bid for SELL) using a FRESH quote --
  the loop refuses to open from a stale one.
- TP exits fill AT the TP level, never at the observed overshoot: with 1s
  sampling the first quote beyond TP virtually always overshoots, and crediting
  the overshoot flatters every winner. A real limit fills at its level.
- SL exits fill at the observed through-price (worse than the level) -- real
  stops slip; the shadow book keeps that against itself.
- At each bar close the bar's high/low (padded by the worst spread seen in the
  bar) is reconciled SL-FIRST: a wick that pierced the stop between polls books
  the stop even if the position later "recovered". When both TP and SL were
  touched inside one bar, the SL wins -- unknowable intrabar ordering must
  never resolve in the book's favor.

PnL is tracked in R (risk units) and bps -- venue-agnostic, so crypto exercises
the machinery even while unsizeable in lots.
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
    exit_reason: str  # tp | sl | sl_wick | time_stop
    bars_held: int
    pnl_r: float
    pnl_bps: float
    lots: float
    opened_minute: int = 0
    exit_epoch: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

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
        self,
        *,
        symbol: str,
        bid: float,
        ask: float,
        day_key: str,
        now_epoch: float = 0.0,
    ) -> ShadowFill | None:
        """Check TP/SL against the adverse quote; returns a fill if closed."""
        self._roll_day(day_key)
        pos = self.positions.get(str(symbol).upper())
        if pos is None or bid <= 0.0 or ask <= 0.0:
            return None
        if pos.side == "BUY":
            # Exits happen at the BID for a long. SL first: if both triggered
            # on one quote something is degenerate -- take the loss.
            if bid <= pos.sl_price:
                return self._close(pos, exit_price=bid, reason="sl", epoch=now_epoch)
            if bid >= pos.tp_price:
                # A limit fills at its level, not at the observed overshoot.
                return self._close(pos, exit_price=pos.tp_price, reason="tp", epoch=now_epoch)
        else:
            if ask >= pos.sl_price:
                return self._close(pos, exit_price=ask, reason="sl", epoch=now_epoch)
            if ask <= pos.tp_price:
                return self._close(pos, exit_price=pos.tp_price, reason="tp", epoch=now_epoch)
        return None

    def on_bar_close(
        self,
        *,
        symbol: str,
        bid_close: float,
        ask_close: float,
        day_key: str,
        minute_epoch: int | None = None,
        high: float | None = None,
        low: float | None = None,
        spread_max_bps: float = 0.0,
        now_epoch: float = 0.0,
    ) -> ShadowFill | None:
        """Advance the bar clock; reconcile intrabar wicks SL-first; time stop."""
        self._roll_day(day_key)
        pos = self.positions.get(str(symbol).upper())
        if pos is None:
            return None
        if minute_epoch is not None and int(minute_epoch) <= int(pos.opened_minute):
            # This bar predates the position being live; nothing to reconcile.
            return None
        # Intrabar wick reconciliation: 1s tick sampling misses wicks between
        # polls, and for mean-reversion the missed touches are asymmetrically
        # stop touches. Pad the mid extreme by half the worst spread seen in
        # the bar to approximate the adverse quote.
        if high is not None and low is not None and high > 0.0 and low > 0.0:
            mid_ref = (bid_close + ask_close) / 2.0 if bid_close > 0 and ask_close > 0 else low
            half_spread_px = max(0.0, spread_max_bps) / 1e4 * max(mid_ref, 0.0) / 2.0
            if pos.side == "BUY":
                worst_bid = low - half_spread_px
                if worst_bid <= pos.sl_price:
                    return self._close(
                        pos, exit_price=pos.sl_price, reason="sl_wick", epoch=now_epoch
                    )
            else:
                worst_ask = high + half_spread_px
                if worst_ask >= pos.sl_price:
                    return self._close(
                        pos, exit_price=pos.sl_price, reason="sl_wick", epoch=now_epoch
                    )
        pos.bars_held += 1
        if pos.bars_held >= pos.time_stop_bars:
            exit_price = bid_close if pos.side == "BUY" else ask_close
            if exit_price > 0.0:
                return self._close(pos, exit_price=exit_price, reason="time_stop", epoch=now_epoch)
        return None

    def _close(
        self, pos: ShadowPosition, *, exit_price: float, reason: str, epoch: float = 0.0
    ) -> ShadowFill:
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
            opened_minute=pos.opened_minute,
            exit_epoch=float(epoch),
            meta=dict(pos.meta),
        )
        self.positions.pop(pos.symbol, None)
        self.fills.append(fill)
        self.day_r += pnl_r
        return fill

    def _roll_day(self, day_key: str) -> None:
        if day_key != self._day_key:
            self._day_key = day_key
            self.day_r = 0.0
