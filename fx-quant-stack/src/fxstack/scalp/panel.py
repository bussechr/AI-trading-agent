# AGENT: ROLE: Time-aligned multi-pair bar panel -- the substrate for cross-sectional signals and for running every pair simultaneously.
# AGENT: ENTRYPOINT: `load_panel`, `usd_factor_series`, `cross_features`.
# AGENT: PRIMARY INPUTS: Dukascopy bid/ask M1 CSVs for many symbols.
# AGENT: PRIMARY OUTPUTS: PanelBar rows keyed by epoch with every pair present, plus derived cross-sectional state.
# AGENT: CALLED BY: `fxstack/scalp/screen_panel.py`, research loop.
"""A time-aligned panel across all pairs, and what it makes measurable.

Single-pair features were screened to exhaustion: information exists at
~0.1-0.8bps while the round trip costs 1.2-2.6bps. Cross-sectional structure
is a genuinely different information class, but that makes it a hypothesis,
not an edge. The original residual implementation was invalid for crosses;
the corrected leave-one-pair-out/synthetic-cross formula is retained here so
screening can reject or confirm it without reintroducing target leakage.

Two things are needed for that, and both are the same requirement the live
scalper has when it trades every pair at once:

1. TIME ALIGNMENT. A cross-sectional statement ("the dollar moved, EURUSD
   did not") is only true if the quotes are simultaneous. Bars are keyed by
   epoch and retained only when every required pair has every consecutive M1
   member of the aggregate. The first M1 bid/ask open is retained separately
   from the final M1 close so delayed execution uses synchronous quotes.
2. A CURRENCY MODEL. Each pair is base/quote; a USD move shows up with
   opposite sign in EURUSD and USDJPY. The factor is built from
   USD-direction-normalised returns so the sign convention cannot silently
   invert -- the single most likely bug in cross-sectional FX work.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from fxstack.providers.ig_mt4_catalog import IG_MT4_PAIR_LEGS

#: Pair -> (base asset, quote currency). Every supported symbol uses the
#: venue's six-character base/quote convention, including crypto-vs-USD CFDs.
#: Copying the production catalog prevents the portfolio gate from silently
#: narrowing a symbol that the data loop is watching.
PAIR_LEGS: dict[str, tuple[str, str]] = dict(IG_MT4_PAIR_LEGS)


@dataclass(slots=True)
class PanelBar:
    epoch: int
    bid: float
    ask: float
    prev_mid: float
    volume: float
    bid_open: float
    ask_open: float

    @property
    def valid(self) -> bool:
        values = (
            self.bid_open,
            self.ask_open,
            self.bid,
            self.ask,
            self.prev_mid,
            self.volume,
        )
        close_mid = self.bid / 2.0 + self.ask / 2.0
        return_bps = (
            (close_mid - self.prev_mid) / self.prev_mid * 1e4
            if self.prev_mid > 0.0
            else math.nan
        )
        return (
            all(math.isfinite(value) for value in values)
            and self.bid_open > 0.0
            and self.ask_open >= self.bid_open
            and self.bid > 0.0
            and self.ask >= self.bid
            and self.prev_mid > 0.0
            and self.volume >= 0.0
            and math.isfinite(close_mid)
            and math.isfinite(return_bps)
        )

    @property
    def open_mid(self) -> float:
        return self.bid_open / 2.0 + self.ask_open / 2.0 if self.valid else 0.0

    @property
    def mid(self) -> float:
        return self.bid / 2.0 + self.ask / 2.0 if self.valid else 0.0

    @property
    def ret_bps(self) -> float:
        """This bar's mid return in bps -- the panel's unit of movement."""
        if not self.valid:
            return 0.0
        value = (self.mid - self.prev_mid) / self.prev_mid * 1e4
        return value if math.isfinite(value) else 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        value = (self.ask - self.bid) / mid * 1e4 if mid > 0 else 0.0
        return value if math.isfinite(value) else 0.0


@dataclass(frozen=True, slots=True)
class _M1Quote:
    epoch: int
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    volume: float

    @property
    def valid(self) -> bool:
        quotes = (
            self.bid_open,
            self.bid_high,
            self.bid_low,
            self.bid_close,
            self.ask_open,
            self.ask_high,
            self.ask_low,
            self.ask_close,
        )
        return (
            self.epoch % 60 == 0
            and all(math.isfinite(value) and value > 0.0 for value in quotes)
            and math.isfinite(self.volume)
            and self.volume >= 0.0
            and self.bid_low <= min(self.bid_open, self.bid_close)
            and max(self.bid_open, self.bid_close) <= self.bid_high
            and self.ask_low <= min(self.ask_open, self.ask_close)
            and max(self.ask_open, self.ask_close) <= self.ask_high
            and self.ask_open >= self.bid_open
            and self.ask_close >= self.bid_close
        )


def _complete_bucket(
    *, bucket_key: int, rows: list[_M1Quote | None], bar_minutes: int
) -> tuple[_M1Quote, _M1Quote, float] | None:
    """Return the executable open, close, and volume for one exact M1 bucket."""
    expected = tuple(bucket_key + 60 * i for i in range(bar_minutes))
    if len(rows) != bar_minutes or any(row is None for row in rows):
        return None
    quotes = [row for row in rows if row is not None]
    if tuple(row.epoch for row in quotes) != expected:
        return None
    if any(not row.valid for row in quotes):
        return None
    try:
        volume = math.fsum(row.volume for row in quotes)
    except OverflowError:
        return None
    if not math.isfinite(volume):
        return None
    return quotes[0], quotes[-1], volume


def _load_symbol(
    csv_path: Path, *, bar_minutes: int, start: int | None, end: int | None
) -> dict[int, PanelBar]:
    minutes = int(bar_minutes)
    if minutes < 1:
        return {}
    out: dict[int, PanelBar] = {}
    step = minutes * 60
    bucket_key: int | None = None
    bucket_rows: list[_M1Quote | None] = []
    complete: dict[int, tuple[_M1Quote, _M1Quote, float]] = {}
    last_epoch: int | None = None
    earliest_bucket = (start // step) * step - step if start is not None else None

    def finish_bucket() -> None:
        if bucket_key is None:
            return
        aggregate = _complete_bucket(
            bucket_key=bucket_key, rows=bucket_rows, bar_minutes=minutes
        )
        if aggregate is not None:
            complete[bucket_key] = aggregate

    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            try:
                epoch = int(
                    dt.datetime.fromisoformat(row[0].replace("Z", "+00:00")).timestamp()
                )
            except (ValueError, IndexError):
                # Without a timestamp the bad row cannot be assigned to a
                # bucket safely, so the symbol cannot prove exact membership.
                return {}
            if last_epoch is not None and epoch <= last_epoch:
                # Duplicates and out-of-order rows make bucket membership
                # ambiguous; fail the complete symbol rather than guessing.
                return {}
            last_epoch = epoch
            if earliest_bucket is not None and epoch < earliest_bucket:
                continue
            if end is not None and epoch >= end:
                break
            key = (epoch // step) * step
            if bucket_key is None or key != bucket_key:
                finish_bucket()
                bucket_key = key
                bucket_rows = []
            try:
                quote = _M1Quote(
                    epoch=epoch,
                    bid_open=float(row[1]),
                    bid_high=float(row[2]),
                    bid_low=float(row[3]),
                    bid_close=float(row[4]),
                    ask_open=float(row[5]),
                    ask_high=float(row[6]),
                    ask_low=float(row[7]),
                    ask_close=float(row[8]),
                    volume=float(row[9]),
                )
            except (ValueError, IndexError):
                quote = None
            bucket_rows.append(quote)
    finish_bucket()

    # A return is valid only when its preceding aggregate is complete and
    # exactly one bar earlier. This prevents weekend/outage returns from being
    # relabelled as a normal aggregate feature observation.
    for key in sorted(complete):
        previous = complete.get(key - step)
        if previous is None or (start is not None and key < start):
            continue
        first, last, volume = complete[key]
        _previous_first, previous_last, _previous_volume = previous
        prev_mid = previous_last.bid_close / 2.0 + previous_last.ask_close / 2.0
        bar = PanelBar(
            epoch=key,
            bid=last.bid_close,
            ask=last.ask_close,
            prev_mid=prev_mid,
            volume=volume,
            bid_open=first.bid_open,
            ask_open=first.ask_open,
        )
        if bar.valid:
            out[key] = bar
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
            # A partial universe changes the factor definition.  Returning an
            # empty panel makes that failure visible to every caller instead
            # of silently producing a different research experiment.
            return [], {}
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
    epoch: int,
    per_symbol: dict[str, dict[int, PanelBar]],
    *,
    exclude_symbol: str | None = None,
) -> tuple[float, float, int]:
    """(factor, coherence, n) for one timestamp.

    factor: mean USD-normalised return across USD pairs, in bps, expressed as
        "how much the DOLLAR moved" (positive = dollar stronger).
    coherence: fraction of USD pairs agreeing with the factor's sign -- a
        broad, aligned move is a different state from one pair dragging the
        average, and only the former is evidence about the dollar.
    """
    contributions: list[float] = []
    excluded = str(exclude_symbol or "").upper()
    for symbol, bars in per_symbol.items():
        if symbol.upper() == excluded:
            continue
        direction = usd_direction(symbol)
        if direction == 0.0:
            continue
        bar = bars.get(epoch)
        if bar is None:
            continue
        if not bar.valid:
            return 0.0, 0.0, 0
        # -direction converts "pair up" into "dollar up".
        contributions.append(-direction * bar.ret_bps)
    if len(contributions) < 3:
        return 0.0, 0.0, len(contributions)
    factor = statistics.fmean(contributions)
    if factor == 0.0:
        return 0.0, 0.0, len(contributions)
    agree = sum(1 for c in contributions if (c > 0) == (factor > 0))
    return factor, agree / len(contributions), len(contributions)


def _currency_vs_usd_return_bps(
    currency: str,
    *,
    epoch: int,
    per_symbol: dict[str, dict[int, PanelBar]],
) -> float | None:
    """Return of ``currency`` versus USD from its directly quoted USD leg.

    Every currency in the configured 18-pair FX universe has exactly one
    direct USD leg.  Normalising its orientation here lets a cross such as
    EURGBP inherit a fair move of ``EURUSD - GBPUSD`` instead of silently
    treating its own return as a supposedly cross-sectional residual.
    """
    ccy = str(currency).upper()
    if ccy == "USD":
        return 0.0
    for symbol, bars in per_symbol.items():
        base, quote = PAIR_LEGS.get(symbol.upper(), ("", ""))
        bar = bars.get(epoch)
        if bar is None:
            continue
        if not bar.valid:
            return None
        if base == ccy and quote == "USD":
            return bar.ret_bps
        if base == "USD" and quote == ccy:
            return -bar.ret_bps
    return None


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
    # A target may never help manufacture its own prediction.  For a direct
    # USD pair the broad factor is therefore leave-one-pair-out; otherwise an
    # extreme target return mechanically drags the factor toward itself and
    # shrinks the apparent residual.
    factor, coherence, n = usd_factor_bps(
        epoch, per_symbol, exclude_symbol=symbol
    )
    bar = per_symbol.get(symbol, {}).get(epoch)
    if bar is None or not bar.valid or n < 3:
        return {"usd_factor": 0.0, "usd_coherence": 0.0, "residual": 0.0}
    base, quote = PAIR_LEGS.get(symbol.upper(), ("", ""))
    direction = usd_direction(symbol)
    if direction:
        implied = -direction * factor  # what this USD pair should have done
    else:
        base_vs_usd = _currency_vs_usd_return_bps(
            base, epoch=epoch, per_symbol=per_symbol
        )
        quote_vs_usd = _currency_vs_usd_return_bps(
            quote, epoch=epoch, per_symbol=per_symbol
        )
        if base_vs_usd is None or quote_vs_usd is None:
            return {"usd_factor": 0.0, "usd_coherence": 0.0, "residual": 0.0}
        # Cross-rate identity: A/B = (A/USD) - (B/USD), in log-return
        # approximation.  Neither component is the target cross itself.
        implied = base_vs_usd - quote_vs_usd
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
