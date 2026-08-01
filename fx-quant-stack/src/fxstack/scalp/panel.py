# AGENT: ROLE: Time-aligned multi-pair bar panel -- the substrate for cross-sectional signals and for running every pair simultaneously.
# AGENT: ENTRYPOINT: `load_panel`, `usd_factor_series`, `cross_features`.
# AGENT: PRIMARY INPUTS: Dukascopy bid/ask M1 CSVs for many symbols.
# AGENT: PRIMARY OUTPUTS: PanelBar rows keyed by epoch with every pair present, plus derived cross-sectional state.
# AGENT: CALLED BY: `fxstack/scalp/screen_panel.py`, research loop.
"""A time-aligned panel across all pairs, and what it makes measurable.

Single-pair features were screened to exhaustion: information exists at
~0.1-0.8bps while the round trip costs 1.2-2.6bps. Everything tested was a
transform of ONE pair's own price, which is why the adversarial panel's only
surviving idea was cross-sectional -- a USD-common factor is not derivable
from any single pair's history, so it is genuinely new information rather
than trailing return in a new costume.

Two things are needed for that, and both are the same requirement the live
scalper has when it trades every pair at once:

1. TIME ALIGNMENT. A cross-sectional statement ("the dollar moved, EURUSD
   did not") is only true if the quotes are simultaneous. Bars are keyed by
   epoch and a timestamp is kept ONLY when every required pair has a bar --
   a factor computed from a partially-observed cross-section is noise
   masquerading as breadth.
2. A CURRENCY MODEL. Each pair is base/quote; a USD move shows up with
   opposite sign in EURUSD and USDJPY. The factor is built from
   USD-direction-normalised returns so the sign convention cannot silently
   invert -- the single most likely bug in cross-sectional FX work.
"""

from __future__ import annotations

import csv
import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

#: Pair -> (base, quote). Only pairs the venue publishes.
PAIR_LEGS: dict[str, tuple[str, str]] = {
    "EURUSD": ("EUR", "USD"), "GBPUSD": ("GBP", "USD"),
    "AUDUSD": ("AUD", "USD"), "NZDUSD": ("NZD", "USD"),
    "USDJPY": ("USD", "JPY"), "USDCHF": ("USD", "CHF"),
    "USDCAD": ("USD", "CAD"),
    "EURGBP": ("EUR", "GBP"), "EURJPY": ("EUR", "JPY"),
    "EURCHF": ("EUR", "CHF"), "EURAUD": ("EUR", "AUD"),
    "EURCAD": ("EUR", "CAD"), "GBPJPY": ("GBP", "JPY"),
    "GBPCHF": ("GBP", "CHF"), "GBPCAD": ("GBP", "CAD"),
    "AUDJPY": ("AUD", "JPY"), "CADJPY": ("CAD", "JPY"),
    "CHFJPY": ("CHF", "JPY"),
}


@dataclass(slots=True)
class PanelBar:
    epoch: int
    bid: float
    ask: float
    prev_mid: float
    volume: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def ret_bps(self) -> float:
        """This bar's mid return in bps -- the panel's unit of movement."""
        return (self.mid - self.prev_mid) / self.prev_mid * 1e4 if self.prev_mid > 0 else 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        return (self.ask - self.bid) / mid * 1e4 if mid > 0 else 0.0


def _load_symbol(
    csv_path: Path, *, bar_minutes: int, start: int | None, end: int | None
) -> dict[int, PanelBar]:
    out: dict[int, PanelBar] = {}
    step = max(1, int(bar_minutes)) * 60
    bucket_key = -1
    bid = ask = 0.0
    vol = 0.0
    prev_mid = 0.0
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            try:
                epoch = int(
                    dt.datetime.fromisoformat(row[0].replace("Z", "+00:00")).timestamp()
                )
                bid_c, ask_c = float(row[4]), float(row[8])
                v = float(row[9]) if len(row) > 9 else 0.0
            except (ValueError, IndexError):
                continue
            if bid_c <= 0 or ask_c <= 0 or ask_c < bid_c:
                continue
            if start and epoch < start:
                continue
            if end and epoch >= end:
                break
            key = (epoch // step) * step
            if key != bucket_key:
                if bucket_key >= 0 and prev_mid > 0:
                    out[bucket_key] = PanelBar(
                        epoch=bucket_key, bid=bid, ask=ask, prev_mid=prev_mid, volume=vol
                    )
                prev_mid = (bid + ask) / 2.0 if bucket_key >= 0 else 0.0
                bucket_key = key
                vol = 0.0
            bid, ask = bid_c, ask_c
            vol += v
    if bucket_key >= 0 and prev_mid > 0:
        out[bucket_key] = PanelBar(
            epoch=bucket_key, bid=bid, ask=ask, prev_mid=prev_mid, volume=vol
        )
    return out


def load_panel(
    *,
    symbols: list[str],
    csv_root: Path,
    bar_minutes: int = 5,
    start: str | None = None,
    end: str | None = None,
) -> tuple[list[int], dict[str, dict[int, PanelBar]]]:
    """Load every symbol and return (aligned_epochs, per-symbol bars).

    ``aligned_epochs`` contains only timestamps where EVERY symbol has a bar.
    A cross-section computed over a partial universe is not a cross-section.
    """
    start_epoch = _epoch(start)
    end_epoch = _epoch(end)
    per_symbol: dict[str, dict[int, PanelBar]] = {}
    for symbol in symbols:
        path = csv_root / f"{symbol}_M1.csv"
        if not path.exists():
            continue
        per_symbol[symbol] = _load_symbol(
            path, bar_minutes=bar_minutes, start=start_epoch, end=end_epoch
        )
    if not per_symbol:
        return [], {}
    common: set[int] | None = None
    for bars in per_symbol.values():
        keys = set(bars)
        common = keys if common is None else (common & keys)
    return sorted(common or set()), per_symbol


def _epoch(text: str | None) -> int | None:
    if not text:
        return None
    return int(
        dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc).timestamp()
    )


def usd_direction(symbol: str) -> float:
    """+1 if a rise in this pair means USD WEAKNESS, -1 if USD strength.

    EURUSD up = dollar down (+1). USDJPY up = dollar up (-1). Getting this
    backwards silently inverts every cross-sectional result, so it is a
    named function with its own test rather than an inline sign.
    """
    base, quote = PAIR_LEGS.get(symbol.upper(), ("", ""))
    if quote == "USD":
        return 1.0
    if base == "USD":
        return -1.0
    return 0.0


def usd_factor_bps(
    epoch: int, per_symbol: dict[str, dict[int, PanelBar]]
) -> tuple[float, float, int]:
    """(factor, coherence, n) for one timestamp.

    factor: mean USD-normalised return across USD pairs, in bps, expressed as
        "how much the DOLLAR moved" (positive = dollar stronger).
    coherence: fraction of USD pairs agreeing with the factor's sign -- a
        broad, aligned move is a different state from one pair dragging the
        average, and only the former is evidence about the dollar.
    """
    contributions: list[float] = []
    for symbol, bars in per_symbol.items():
        direction = usd_direction(symbol)
        if direction == 0.0:
            continue
        bar = bars.get(epoch)
        if bar is None:
            continue
        # -direction converts "pair up" into "dollar up".
        contributions.append(-direction * bar.ret_bps)
    if len(contributions) < 3:
        return 0.0, 0.0, len(contributions)
    factor = statistics.fmean(contributions)
    if factor == 0.0:
        return 0.0, 0.0, len(contributions)
    agree = sum(1 for c in contributions if (c > 0) == (factor > 0))
    return factor, agree / len(contributions), len(contributions)


def cross_features(
    *,
    symbol: str,
    epoch: int,
    per_symbol: dict[str, dict[int, PanelBar]],
) -> dict[str, float]:
    """Cross-sectional state for one pair at one timestamp.

    - ``usd_factor``: the dollar's move (bps), signed for THIS pair, i.e. the
      move this pair would make if it simply followed the dollar.
    - ``usd_coherence``: breadth of that move across the USD complex.
    - ``residual``: this pair's move MINUS the factor-implied move. A pair
      that ignored a broad dollar move is the cross-sectional statement --
      it cannot be computed from the pair's own history at all.
    """
    factor, coherence, n = usd_factor_bps(epoch, per_symbol)
    bar = per_symbol.get(symbol, {}).get(epoch)
    if bar is None or n < 3:
        return {"usd_factor": 0.0, "usd_coherence": 0.0, "residual": 0.0}
    direction = usd_direction(symbol)
    implied = -direction * factor  # what this pair "should" have done
    return {
        "usd_factor": implied,
        "usd_coherence": coherence,
        "residual": bar.ret_bps - implied,
    }


def iter_aligned(
    epochs: list[int], per_symbol: dict[str, dict[int, PanelBar]]
) -> Iterator[tuple[int, dict[str, PanelBar]]]:
    for epoch in epochs:
        yield epoch, {s: b[epoch] for s, b in per_symbol.items() if epoch in b}
