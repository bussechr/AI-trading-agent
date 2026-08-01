"""Dislocation mean-reversion signal at M1 -- the seed signal family.

Proposes; never sizes. The bracket carries the panel's per-entry viability
arithmetic: an intent whose breakeven win rate p* = (SL + cost) / (TP + SL)
exceeds the configured ceiling at the LIVE spread is rejected before any gate
sees it -- geometry that cannot pay is not a signal.

This is deliberately one simple, falsifiable family. Richer families arrive
through the research loop with validation attached, not by editing this file
into a zoo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from fxstack.scalp.bars import M1Bar, atr_bps, ema
from fxstack.scalp.config import ScalpConfig


@dataclass(slots=True)
class ScalpIntent:
    symbol: str
    side: str  # BUY | SELL
    minute_epoch: int
    ref_mid: float
    entry_price: float  # ask for BUY, bid for SELL -- spread paid at entry
    sl_price: float
    tp_price: float
    atr_bps: float
    stop_bps: float
    disp_z: float
    spread_bps: float
    p_star: float
    time_stop_bars: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_dislocation(
    *,
    bars: list[M1Bar],
    config: ScalpConfig,
    spread_bps: float,
) -> tuple[ScalpIntent | None, str]:
    """Return (intent, "") or (None, reason). ``bars`` must be the unbroken
    valid run ending at the just-closed bar."""

    if len(bars) < config.min_history_bars:
        return None, "insufficient_valid_history"
    last = bars[-1]
    closes = [b.close for b in bars]
    atr = atr_bps(bars, periods=config.atr_bars)
    if atr < config.atr_floor_bps:
        # Includes the partially-frozen-feed case: near-zero ATR history makes
        # the first real move look like an enormous z -- refuse to trade it.
        return None, "no_volatility_estimate"
    mean = ema(closes, periods=config.ema_bars)
    if mean <= 0.0 or last.close <= 0.0:
        return None, "degenerate_prices"

    disp_bps = (last.close - mean) / mean * 1e4
    disp_z = disp_bps / atr
    if abs(disp_z) < config.z_entry:
        return None, "no_dislocation"

    bar_dir = last.close - last.open
    if config.signal_mode == "momentum":
        # Continuation: join the dislocation only while the just-closed bar
        # still pushes WITH it -- never chase a move that already stalled.
        if disp_z > 0 and bar_dir <= 0:
            return None, "no_continuation_trigger"
        if disp_z < 0 and bar_dir >= 0:
            return None, "no_continuation_trigger"
        side = "BUY" if disp_z > 0 else "SELL"
    else:
        # Reversion: the just-closed bar must already lean back toward the
        # mean -- fade exhaustion, never a moving train.
        if disp_z > 0 and bar_dir >= 0:
            return None, "no_reversion_trigger"
        if disp_z < 0 and bar_dir <= 0:
            return None, "no_reversion_trigger"
        side = "SELL" if disp_z > 0 else "BUY"
    stop_bps = max(config.sl_atr_mult * atr, config.min_stop_bps)
    tp_bps = config.tp_atr_mult * atr
    cost = max(0.0, float(spread_bps))
    p_star = (stop_bps + cost) / (tp_bps + stop_bps)
    if p_star > config.p_star_max:
        return None, "bracket_cost_dead"

    mid = last.close
    entry = last.ask_close if side == "BUY" else last.bid_close
    if entry <= 0.0:
        return None, "no_entry_quote"
    stop_px = stop_bps / 1e4 * mid
    tp_px = tp_bps / 1e4 * mid
    if side == "BUY":
        sl_price, tp_price = entry - stop_px, entry + tp_px
    else:
        sl_price, tp_price = entry + stop_px, entry - tp_px

    return (
        ScalpIntent(
            symbol=last.symbol,
            side=side,
            minute_epoch=last.minute_epoch,
            ref_mid=mid,
            entry_price=entry,
            sl_price=sl_price,
            tp_price=tp_price,
            atr_bps=atr,
            stop_bps=stop_bps,
            disp_z=disp_z,
            spread_bps=cost,
            p_star=p_star,
            time_stop_bars=config.time_stop_bars,
        ),
        "",
    )
