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

from fxstack.scalp.authority import TRADE_EVIDENCE_SCHEMA
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
    initial_sl_price: float = 0.0
    breakeven_armed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def risk_px(self) -> float:
        """Distance from entry to the ORIGINAL stop -- the R unit.

        R is fixed at entry: moving the stop changes the outcome, never the
        yardstick. Recomputing R off a moved stop would turn a breakeven exit
        into a divide-by-zero and inflate every subsequent ratio.
        """
        base = self.initial_sl_price or self.sl_price
        return abs(self.entry_price - base)


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
    def __init__(
        self,
        *,
        max_concurrent: int,
        breakeven_at_r: float = 0.0,
        trail_atr_mult: float = 0.0,
    ) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.breakeven_at_r = max(0.0, float(breakeven_at_r))
        self.trail_atr_mult = max(0.0, float(trail_atr_mult))
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
        initial_risk_bps = (
            abs(intent.entry_price - intent.sl_price) / intent.entry_price * 1e4
            if intent.entry_price > 0.0
            else 0.0
        )
        initial_target_bps = (
            abs(intent.tp_price - intent.entry_price) / intent.entry_price * 1e4
            if intent.entry_price > 0.0
            else 0.0
        )
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
            initial_sl_price=intent.sl_price,
            meta={
                "disp_z": intent.disp_z,
                "p_star": intent.p_star,
                "atr_bps": intent.atr_bps,
                "trade_evidence_schema": TRADE_EVIDENCE_SCHEMA,
                "target_predeclared": True,
                "initial_risk_bps": initial_risk_bps,
                "initial_target_bps": initial_target_bps,
            },
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
                reason = "breakeven" if pos.breakeven_armed else "sl"
                return self._close(pos, exit_price=bid, reason=reason, epoch=now_epoch)
            if bid >= pos.tp_price:
                # A limit fills at its level, not at the observed overshoot.
                return self._close(pos, exit_price=pos.tp_price, reason="tp", epoch=now_epoch)
        else:
            if ask >= pos.sl_price:
                reason = "breakeven" if pos.breakeven_armed else "sl"
                return self._close(pos, exit_price=ask, reason=reason, epoch=now_epoch)
            if ask <= pos.tp_price:
                return self._close(pos, exit_price=pos.tp_price, reason="tp", epoch=now_epoch)
        # Arm breakeven only AFTER this quote's exits are resolved: the stop
        # may never move in a way that rescues a level already breached.
        self._maybe_arm_breakeven(pos, bid=bid, ask=ask)
        self._maybe_advance_trail(pos, bid=bid, ask=ask)
        return None

    def _maybe_arm_breakeven(self, pos: ShadowPosition, *, bid: float, ask: float) -> None:
        """Move the stop to entry once the trade is breakeven_at_r in front.

        Favorable excursion is measured on the EXIT side (bid for a long), so
        the spread must be genuinely cleared before the stop moves -- a trade
        that merely looks green on mid does not arm.
        """
        if self.breakeven_at_r <= 0.0 or pos.breakeven_armed:
            return
        risk = pos.risk_px()
        if risk <= 0.0:
            return
        favorable = (bid - pos.entry_price) if pos.side == "BUY" else (pos.entry_price - ask)
        if favorable / risk < self.breakeven_at_r:
            return
        pos.breakeven_armed = True
        pos.sl_price = pos.entry_price

    def _maybe_advance_trail(
        self, pos: ShadowPosition, *, bid: float, ask: float
    ) -> None:
        """Tighten behind the best observed executable exit-side quote."""
        if self.trail_atr_mult <= 0.0:
            return
        atr_bps = float(pos.meta.get("atr_bps") or 0.0)
        trail_px = self.trail_atr_mult * atr_bps / 1e4 * pos.entry_price
        if trail_px <= 0.0:
            return
        if pos.side == "BUY":
            candidate = bid - trail_px
            if candidate > pos.sl_price:
                pos.sl_price = candidate
        else:
            candidate = ask + trail_px
            if candidate < pos.sl_price:
                pos.sl_price = candidate

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
        engine_close: bool = True,
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
            wick_reason = "breakeven_wick" if pos.breakeven_armed else "sl_wick"
            if pos.side == "BUY":
                worst_bid = low - half_spread_px
                if worst_bid <= pos.sl_price:
                    return self._close(
                        pos, exit_price=pos.sl_price, reason=wick_reason, epoch=now_epoch
                    )
            else:
                worst_ask = high + half_spread_px
                if worst_ask >= pos.sl_price:
                    return self._close(
                        pos, exit_price=pos.sl_price, reason=wick_reason, epoch=now_epoch
                    )
        if not engine_close:
            return None
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
        risk_px = pos.risk_px()
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
