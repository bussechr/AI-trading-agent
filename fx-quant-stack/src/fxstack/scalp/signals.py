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
    """The dislocation family. ``bars`` must be the unbroken valid run.

    Delegates to :mod:`fxstack.scalp.families`, which owns every family and
    the shared bracket economics. Two implementations of one family would
    drift, and a control that no longer matches the engine's own code proves
    nothing -- so this is a thin alias, not a copy.
    """
    from fxstack.scalp.families import evaluate_dislocation_family

    return evaluate_dislocation_family(
        bars=bars, config=config, spread_bps=spread_bps
    )
