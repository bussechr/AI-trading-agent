# AGENT: ROLE: Research-only sparse liquidity-sweep/reclaim screen.
# AGENT: ENTRYPOINT: `screen_universe`; CLI `python -m fxstack.scalp.screen_liquidity_sweep`.
# AGENT: PRIMARY INPUTS: immutable Dukascopy-style bid/ask M1 CSV snapshots.
# AGENT: PRIMARY OUTPUTS: every grid x pair x direction cell, including zero-event cells.
# AGENT ISOLATION: advisory discovery evidence only; never authorizes success or activation.
"""Causal screen for a rolling-liquidity-level sweep and reclaim (LSR).

This is deliberately one bounded hypothesis, not a feature factory.  A closed
M1 bar must sweep a level made only from prior complete M15 bars, reclaim it,
and close with a large rejection wick.  Volatility is made only from prior
complete M5 bars, while both the signal close and delayed next-open fill must
occur in the cheapest quartile of the pair's strictly trailing spreads.

The screen is intentionally harder on the strategy than an ordinary OHLC
backtest: entry is delayed one M1 bar and uses the adverse of the signal touch
and next open, exits use the adverse quote side, ambiguous bars stop first,
and an explicit adverse round-trip cost is subtracted from every outcome.

Research output is descriptive.  Missing measured venue budgets may be
replaced by an explicitly labelled proxy discovery stress, but that always
sets ``economic_claim_ready=false``.  No result produced here can authorize a
success claim, model activation, registry write, or live order.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import hashlib
import json
import math
import statistics
from collections import Counter, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


FX_SYMBOLS: tuple[str, ...] = (
    "AUDJPY",
    "AUDUSD",
    "CADJPY",
    "CHFJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "EURGBP",
    "EURJPY",
    "EURUSD",
    "GBPCAD",
    "GBPCHF",
    "GBPJPY",
    "GBPUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "USDJPY",
)

VOL_LOOKBACK_M5 = 24
SPREAD_LOOKBACK_M1 = 240
RECLAIM_ATR5 = 0.05
STOP_BUFFER_ATR5 = 0.10
COOLDOWN_M1_BARS = 30
TIME_STOP_M1_BARS = 8
FILL_DELAY_M1_BARS = 1
REWARD_RISK = 1.0
P_STAR_MAX = 0.55
MIN_TP_COST_RATIO = 4.0
DEFAULT_PRIOR_TESTS = 1_074


@dataclass(frozen=True, slots=True)
class SweepConfig:
    """One member of the locked 2 x 2 x 2 parameter grid."""

    level_bars_m15: int
    penetration_atr5: float
    wick_floor: float

    @property
    def config_id(self) -> str:
        delta = int(round(self.penetration_atr5 * 100.0))
        wick = int(round(self.wick_floor * 100.0))
        return f"n{self.level_bars_m15}_d{delta:02d}_w{wick:02d}"


GRID: tuple[SweepConfig, ...] = tuple(
    SweepConfig(level_bars_m15=n, penetration_atr5=delta, wick_floor=wick)
    for n in (8, 16)
    for delta in (0.10, 0.25)
    for wick in (0.60, 0.75)
)


@dataclass(frozen=True, slots=True)
class QuoteBar:
    """One valid minute of source bid/ask OHLC."""

    epoch: int
    bid_o: float
    bid_h: float
    bid_l: float
    bid_c: float
    ask_o: float
    ask_h: float
    ask_l: float
    ask_c: float

    @property
    def mid_o(self) -> float:
        return (self.bid_o + self.ask_o) / 2.0

    @property
    def mid_h(self) -> float:
        return (self.bid_h + self.ask_h) / 2.0

    @property
    def mid_l(self) -> float:
        return (self.bid_l + self.ask_l) / 2.0

    @property
    def mid_c(self) -> float:
        return (self.bid_c + self.ask_c) / 2.0

    @property
    def spread_close_bps(self) -> float:
        return _spread_bps(self.bid_c, self.ask_c)

    @property
    def spread_open_bps(self) -> float:
        return _spread_bps(self.bid_o, self.ask_o)


@dataclass(frozen=True, slots=True)
class SignalContext:
    """Strictly prior information available at one closed M1 bar."""

    volatility_bps_m5: float
    support_bid: float
    resistance_ask: float
    spread_q25_bps: float


@dataclass(frozen=True, slots=True)
class SweepSignal:
    config_id: str
    symbol: str
    side: str
    signal_index: int
    signal_epoch: int
    entry_index: int
    entry_price: float
    stop_price: float
    target_price: float
    risk_bps: float
    total_cost_bps: float
    extra_round_trip_cost_bps: float
    p_star: float
    penetration_atr5: float
    reclaim_atr5: float
    wick_fraction: float
    volatility_bps_m5: float
    spread_q25_bps: float
    signal_spread_bps: float
    entry_spread_bps: float


@dataclass(frozen=True, slots=True)
class SweepTrade:
    symbol: str
    side: str
    config_id: str
    signal_epoch: int
    entry_epoch: int
    exit_epoch: int
    entry_price: float
    exit_price: float
    risk_bps: float
    pnl_bps: float
    pnl_r: float
    bars_held: int
    exit_reason: str


@dataclass(slots=True)
class PreparedSeries:
    bars: list[QuoteBar]
    spread_q25_by_index: list[float | None]
    volatility_by_m5_bucket: dict[int, float]
    levels_by_n_and_m15_bucket: dict[int, dict[int, tuple[float, float]]]


def _spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0.0 or ask < bid:
        return 0.0
    return (ask - bid) / mid * 1e4


def _valid_number(value: Any, *, allow_zero: bool = False) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number < 0.0 or (number == 0.0 and not allow_zero):
        return None
    return number


def validate_quote_bar(bar: QuoteBar) -> bool:
    """Return whether a bar is safe to admit to a strict research run."""

    values = (
        bar.bid_o,
        bar.bid_h,
        bar.bid_l,
        bar.bid_c,
        bar.ask_o,
        bar.ask_h,
        bar.ask_l,
        bar.ask_c,
    )
    if bar.epoch % 60 != 0 or not all(math.isfinite(value) and value > 0.0 for value in values):
        return False
    if not (
        bar.bid_h >= max(bar.bid_o, bar.bid_c)
        and bar.bid_l <= min(bar.bid_o, bar.bid_c)
        and bar.ask_h >= max(bar.ask_o, bar.ask_c)
        and bar.ask_l <= min(bar.ask_o, bar.ask_c)
    ):
        return False
    if any(
        ask < bid
        for bid, ask in (
            (bar.bid_o, bar.ask_o),
            (bar.bid_h, bar.ask_h),
            (bar.bid_l, bar.ask_l),
            (bar.bid_c, bar.ask_c),
        )
    ):
        return False
    return True


def _parse_epoch(text: str | None) -> int | None:
    if not text:
        return None
    parsed = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return int(parsed.timestamp())


def load_m1_csv(
    path: Path,
    *,
    start: str | None = None,
    end: str | None = None,
) -> list[QuoteBar]:
    """Load a strict Dukascopy-style bid/ask M1 file.

    Gaps are retained as gaps and later break every rolling context. Duplicate
    or non-monotonic rows fail the source rather than being silently sorted.
    """

    start_epoch = _parse_epoch(start)
    end_epoch = _parse_epoch(end)
    bars: list[QuoteBar] = []
    last_epoch: int | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header or header[0] != "timestamp" or len(header) < 9:
            raise ValueError(f"{path}: unexpected M1 header {header!r}")
        for row_number, row in enumerate(reader, start=2):
            try:
                epoch = _parse_epoch(row[0])
                assert epoch is not None
                values = [float(value) for value in row[1:9]]
            except (AssertionError, IndexError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{row_number}: malformed M1 row") from exc
            if start_epoch is not None and epoch < start_epoch:
                continue
            if end_epoch is not None and epoch >= end_epoch:
                break
            if last_epoch is not None and epoch <= last_epoch:
                raise ValueError(f"{path}:{row_number}: timestamps are not strictly increasing")
            bar = QuoteBar(epoch, *values)
            if not validate_quote_bar(bar):
                raise ValueError(f"{path}:{row_number}: invalid bid/ask OHLC")
            bars.append(bar)
            last_epoch = epoch
    return bars


def _aggregate_complete(bars: Sequence[QuoteBar], *, minutes: int) -> list[QuoteBar]:
    """Aggregate only exact, UTC-aligned, consecutive M1 buckets."""

    step = int(minutes) * 60
    if step <= 0 or 60 % int(minutes) != 0:
        raise ValueError("aggregation minutes must be a positive divisor of 60")
    buckets: dict[int, list[QuoteBar]] = {}
    for bar in bars:
        key = (bar.epoch // step) * step
        buckets.setdefault(key, []).append(bar)
    out: list[QuoteBar] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        expected = [key + offset * 60 for offset in range(minutes)]
        if len(bucket) != minutes or [bar.epoch for bar in bucket] != expected:
            continue
        first, last = bucket[0], bucket[-1]
        out.append(
            QuoteBar(
                epoch=key,
                bid_o=first.bid_o,
                bid_h=max(bar.bid_h for bar in bucket),
                bid_l=min(bar.bid_l for bar in bucket),
                bid_c=last.bid_c,
                ask_o=first.ask_o,
                ask_h=max(bar.ask_h for bar in bucket),
                ask_l=min(bar.ask_l for bar in bucket),
                ask_c=last.ask_c,
            )
        )
    return out


def _is_consecutive(bars: Sequence[QuoteBar], *, seconds: int) -> bool:
    return bool(bars) and all(
        right.epoch == left.epoch + seconds for left, right in zip(bars, bars[1:])
    )


def _build_volatility_map(m5: Sequence[QuoteBar]) -> dict[int, float]:
    """Map current M5 bucket -> median of 24 strictly prior M5 TR values."""

    needed = VOL_LOOKBACK_M5 + 1
    result: dict[int, float] = {}
    for end in range(needed, len(m5) + 1):
        tail = list(m5[end - needed : end])
        if not _is_consecutive(tail, seconds=300):
            continue
        true_ranges_bps: list[float] = []
        for previous, current in zip(tail, tail[1:]):
            close = current.mid_c
            if close <= 0.0:
                true_ranges_bps = []
                break
            true_range = max(
                current.mid_h - current.mid_l,
                abs(current.mid_h - previous.mid_c),
                abs(current.mid_l - previous.mid_c),
            )
            true_ranges_bps.append(true_range / close * 1e4)
        if len(true_ranges_bps) != VOL_LOOKBACK_M5:
            continue
        volatility = statistics.median(true_ranges_bps)
        if math.isfinite(volatility) and volatility > 0.0:
            # The key is the bucket AFTER the final contributing M5 bar. Any
            # signal inside that bucket therefore cannot affect its own scale.
            result[tail[-1].epoch + 300] = volatility
    return result


def _build_level_map(
    m15: Sequence[QuoteBar], *, level_bars: int
) -> dict[int, tuple[float, float]]:
    """Map current M15 bucket -> prior rolling bid support / ask resistance."""

    result: dict[int, tuple[float, float]] = {}
    for end in range(level_bars, len(m15) + 1):
        tail = list(m15[end - level_bars : end])
        if not _is_consecutive(tail, seconds=900):
            continue
        result[tail[-1].epoch + 900] = (
            min(bar.bid_l for bar in tail),
            max(bar.ask_h for bar in tail),
        )
    return result


def _quantile_from_sorted(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(values[lower])
    weight = position - lower
    return float(values[lower] * (1.0 - weight) + values[upper] * weight)


def _rolling_prior_spread_q25(bars: Sequence[QuoteBar]) -> list[float | None]:
    """Q25 of the preceding 240 consecutive closes, strictly excluding t."""

    result: list[float | None] = [None] * len(bars)
    ordered: list[float] = []
    queue: deque[float] = deque()
    for index, bar in enumerate(bars):
        if index == 0:
            continue
        previous = bars[index - 1]
        if bar.epoch != previous.epoch + 60:
            ordered.clear()
            queue.clear()
            continue
        value = previous.spread_close_bps
        bisect.insort(ordered, value)
        queue.append(value)
        if len(queue) > SPREAD_LOOKBACK_M1:
            expired = queue.popleft()
            position = bisect.bisect_left(ordered, expired)
            if position >= len(ordered) or ordered[position] != expired:
                raise RuntimeError("rolling spread multiset lost synchronization")
            ordered.pop(position)
        if len(queue) == SPREAD_LOOKBACK_M1:
            result[index] = _quantile_from_sorted(ordered, 0.25)
    return result


def prepare_series(bars: Iterable[QuoteBar]) -> PreparedSeries:
    """Validate and precompute every strictly prior multi-timeframe input."""

    rows = list(bars)
    for index, bar in enumerate(rows):
        if not validate_quote_bar(bar):
            raise ValueError(f"invalid quote bar at index {index}")
        if index and bar.epoch <= rows[index - 1].epoch:
            raise ValueError("M1 timestamps must be strictly increasing")
    m5 = _aggregate_complete(rows, minutes=5)
    m15 = _aggregate_complete(rows, minutes=15)
    return PreparedSeries(
        bars=rows,
        spread_q25_by_index=_rolling_prior_spread_q25(rows),
        volatility_by_m5_bucket=_build_volatility_map(m5),
        levels_by_n_and_m15_bucket={
            n: _build_level_map(m15, level_bars=n) for n in (8, 16)
        },
    )


def signal_context_at(
    prepared: PreparedSeries, *, signal_index: int, level_bars_m15: int
) -> SignalContext | None:
    """Return the causal context at t, or None when any strict window is absent."""

    if signal_index < 0 or signal_index >= len(prepared.bars):
        return None
    q25 = prepared.spread_q25_by_index[signal_index]
    if q25 is None or q25 <= 0.0:
        return None
    epoch = prepared.bars[signal_index].epoch
    m5_bucket = (epoch // 300) * 300
    m15_bucket = (epoch // 900) * 900
    volatility = prepared.volatility_by_m5_bucket.get(m5_bucket)
    levels = prepared.levels_by_n_and_m15_bucket.get(level_bars_m15, {}).get(
        m15_bucket
    )
    if volatility is None or levels is None:
        return None
    support, resistance = levels
    if not all(
        math.isfinite(value) and value > 0.0
        for value in (volatility, support, resistance)
    ):
        return None
    return SignalContext(
        volatility_bps_m5=volatility,
        support_bid=support,
        resistance_ask=resistance,
        spread_q25_bps=q25,
    )


def _entry_price(signal_bar: QuoteBar, next_bar: QuoteBar, *, side: str) -> float:
    """Mandatory next-open fill, adverse against the already-observed touch."""

    if side == "BUY":
        return max(signal_bar.ask_c, next_bar.ask_o)
    if side == "SELL":
        return min(signal_bar.bid_c, next_bar.bid_o)
    raise ValueError("side must be BUY or SELL")


def evaluate_signal(
    *,
    prepared: PreparedSeries,
    signal_index: int,
    symbol: str,
    side: str,
    config: SweepConfig,
    effective_spread_budget_bps: float | None,
    stop_floor_bps: float | None = None,
    extra_round_trip_cost_bps: float = 0.0,
) -> tuple[SweepSignal | None, str]:
    """Evaluate one quote-side LSR event with no information after t+1 open."""

    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    extra_cost = _valid_number(extra_round_trip_cost_bps, allow_zero=True)
    if extra_cost is None:
        raise ValueError("extra round-trip cost must be finite and non-negative")
    budget = _valid_number(effective_spread_budget_bps)
    if budget is None:
        return None, "cost_unavailable"
    floor = _valid_number(stop_floor_bps, allow_zero=True)
    if stop_floor_bps is not None and floor is None:
        return None, "stop_floor_invalid"
    floor = float(floor or 0.0)
    if signal_index < 0 or signal_index + FILL_DELAY_M1_BARS >= len(prepared.bars):
        return None, "delayed_fill_unavailable"
    signal_bar = prepared.bars[signal_index]
    next_bar = prepared.bars[signal_index + FILL_DELAY_M1_BARS]
    if next_bar.epoch != signal_bar.epoch + 60 * FILL_DELAY_M1_BARS:
        return None, "delayed_fill_gap"
    context = signal_context_at(
        prepared, signal_index=signal_index, level_bars_m15=config.level_bars_m15
    )
    if context is None:
        return None, "strict_context_unavailable"

    signal_spread = signal_bar.spread_close_bps
    entry_spread = next_bar.spread_open_bps
    spread_cap = min(context.spread_q25_bps, budget)
    if spread_cap <= 0.0:
        return None, "spread_cap_unavailable"
    if signal_spread > spread_cap + 1e-12:
        return None, "signal_spread_above_q25_or_budget"
    if entry_spread > spread_cap + 1e-12:
        return None, "entry_spread_above_q25_or_budget"

    mid = signal_bar.mid_c
    volatility = context.volatility_bps_m5
    if mid <= 0.0 or volatility <= 0.0:
        return None, "volatility_unavailable"
    if side == "BUY":
        quote_range = signal_bar.bid_h - signal_bar.bid_l
        if quote_range <= 0.0:
            return None, "degenerate_signal_range"
        penetration = (
            (context.support_bid - signal_bar.bid_l) / mid * 1e4 / volatility
        )
        reclaim = (
            (signal_bar.bid_c - context.support_bid) / mid * 1e4 / volatility
        )
        wick = (
            min(signal_bar.bid_o, signal_bar.bid_c) - signal_bar.bid_l
        ) / quote_range
        directional_close = signal_bar.bid_c > signal_bar.bid_o
    else:
        quote_range = signal_bar.ask_h - signal_bar.ask_l
        if quote_range <= 0.0:
            return None, "degenerate_signal_range"
        penetration = (
            (signal_bar.ask_h - context.resistance_ask) / mid * 1e4 / volatility
        )
        reclaim = (
            (context.resistance_ask - signal_bar.ask_c) / mid * 1e4 / volatility
        )
        wick = (
            signal_bar.ask_h - max(signal_bar.ask_o, signal_bar.ask_c)
        ) / quote_range
        directional_close = signal_bar.ask_c < signal_bar.ask_o

    if penetration < config.penetration_atr5:
        return None, "no_level_penetration"
    if reclaim < RECLAIM_ATR5:
        return None, "level_not_reclaimed"
    if wick < config.wick_floor:
        return None, "wick_too_small"
    if not directional_close:
        return None, "directional_close_missing"

    entry = _entry_price(signal_bar, next_bar, side=side)
    buffer_price = STOP_BUFFER_ATR5 * volatility / 1e4 * mid
    if side == "BUY":
        raw_stop = signal_bar.bid_l - buffer_price
        raw_risk_bps = (entry - raw_stop) / entry * 1e4
    else:
        raw_stop = signal_bar.ask_h + buffer_price
        raw_risk_bps = (raw_stop - entry) / entry * 1e4
    if not math.isfinite(raw_risk_bps) or raw_risk_bps <= 0.0:
        return None, "degenerate_stop"
    risk_bps = max(raw_risk_bps, floor)
    total_cost_bps = entry_spread + extra_cost
    target_bps = risk_bps * REWARD_RISK
    p_star = (risk_bps + total_cost_bps) / (risk_bps + target_bps)
    if not math.isfinite(p_star) or p_star > P_STAR_MAX:
        return None, "bracket_cost_dead"
    if total_cost_bps > 0.0 and target_bps < MIN_TP_COST_RATIO * total_cost_bps:
        return None, "target_too_small_vs_cost"

    if side == "BUY":
        stop_price = entry * (1.0 - risk_bps / 1e4)
        target_price = entry * (1.0 + target_bps / 1e4)
    else:
        stop_price = entry * (1.0 + risk_bps / 1e4)
        target_price = entry * (1.0 - target_bps / 1e4)
    return (
        SweepSignal(
            config_id=config.config_id,
            symbol=str(symbol).upper(),
            side=side,
            signal_index=signal_index,
            signal_epoch=signal_bar.epoch,
            entry_index=signal_index + FILL_DELAY_M1_BARS,
            entry_price=entry,
            stop_price=stop_price,
            target_price=target_price,
            risk_bps=risk_bps,
            total_cost_bps=total_cost_bps,
            extra_round_trip_cost_bps=extra_cost,
            p_star=p_star,
            penetration_atr5=penetration,
            reclaim_atr5=reclaim,
            wick_fraction=wick,
            volatility_bps_m5=volatility,
            spread_q25_bps=context.spread_q25_bps,
            signal_spread_bps=signal_spread,
            entry_spread_bps=entry_spread,
        ),
        "",
    )


def _trade_result(
    signal: SweepSignal,
    *,
    exit_bar: QuoteBar,
    exit_price: float,
    bars_held: int,
    reason: str,
) -> SweepTrade:
    if signal.side == "BUY":
        gross_bps = (exit_price - signal.entry_price) / signal.entry_price * 1e4
    else:
        gross_bps = (signal.entry_price - exit_price) / signal.entry_price * 1e4
    pnl_bps = gross_bps - signal.extra_round_trip_cost_bps
    pnl_r = pnl_bps / signal.risk_bps
    return SweepTrade(
        symbol=signal.symbol,
        side=signal.side,
        config_id=signal.config_id,
        signal_epoch=signal.signal_epoch,
        entry_epoch=exit_bar.epoch - (bars_held - 1) * 60,
        exit_epoch=exit_bar.epoch,
        entry_price=signal.entry_price,
        exit_price=exit_price,
        risk_bps=signal.risk_bps,
        pnl_bps=pnl_bps,
        pnl_r=pnl_r,
        bars_held=bars_held,
        exit_reason=reason,
    )


def simulate_trade(
    bars: Sequence[QuoteBar], *, signal: SweepSignal
) -> SweepTrade | None:
    """Replay an eight-M1-bar 1R bracket using the adverse quote side.

    Every bar must be consecutive from the delayed fill. Stops win any
    ambiguous double touch. A gap through a stop fills at the adverse open;
    targets remain limit fills at their level.
    """

    entry_index = signal.entry_index
    for offset in range(TIME_STOP_M1_BARS):
        index = entry_index + offset
        if index >= len(bars):
            return None
        bar = bars[index]
        expected_epoch = bars[entry_index].epoch + offset * 60
        if bar.epoch != expected_epoch:
            return None
        buy = signal.side == "BUY"
        adverse_open = bar.bid_o if buy else bar.ask_o
        stop_gap = adverse_open <= signal.stop_price if buy else adverse_open >= signal.stop_price
        if stop_gap:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=adverse_open,
                bars_held=offset + 1,
                reason="sl_gap_open",
            )
        target_gap = (
            adverse_open >= signal.target_price if buy else adverse_open <= signal.target_price
        )
        if target_gap:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=signal.target_price,
                bars_held=offset + 1,
                reason="tp_gap_open",
            )
        adverse_low = bar.bid_l if buy else bar.ask_l
        adverse_high = bar.bid_h if buy else bar.ask_h
        stop_hit = adverse_low <= signal.stop_price if buy else adverse_high >= signal.stop_price
        target_hit = (
            adverse_high >= signal.target_price if buy else adverse_low <= signal.target_price
        )
        if stop_hit:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=signal.stop_price,
                bars_held=offset + 1,
                reason="sl_double_touch" if target_hit else "sl",
            )
        if target_hit:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=signal.target_price,
                bars_held=offset + 1,
                reason="tp",
            )
        if offset == TIME_STOP_M1_BARS - 1:
            exit_price = bar.bid_c if buy else bar.ask_c
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=exit_price,
                bars_held=TIME_STOP_M1_BARS,
                reason="time_stop",
            )
    return None


def _day(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def _day_statistics(trades: Sequence[SweepTrade]) -> dict[str, Any]:
    by_day: dict[str, list[SweepTrade]] = {}
    for trade in trades:
        by_day.setdefault(_day(trade.signal_epoch), []).append(trade)
    day_means = [
        statistics.fmean(trade.pnl_r for trade in day_trades)
        for day_trades in by_day.values()
    ]
    days = len(day_means)
    if days >= 2:
        day_mean = statistics.fmean(day_means)
        day_sd = statistics.stdev(day_means)
        if day_sd > 1e-12:
            standard_error = day_sd / math.sqrt(days)
            day_t = day_mean / standard_error
            lower = day_mean - 1.6448536269514722 * standard_error
        else:
            # A zero-variance planted fixture is not independent evidence.
            day_t = 0.0
            lower = None
    else:
        day_t = 0.0
        lower = None
    all_target_win_days = sum(
        1
        for day_trades in by_day.values()
        if day_trades and all(_is_full_target_win(trade) for trade in day_trades)
    )
    all_positive_days = sum(
        1
        for day_trades in by_day.values()
        if day_trades and all(trade.pnl_r > 0.0 for trade in day_trades)
    )
    return {
        "independent_days": days,
        "day_clustered_t": day_t,
        "day_mean_r_lcb95_descriptive": lower,
        "all_trades_full_target_win_days": all_target_win_days,
        "full_target_day_win_rate": all_target_win_days / days if days else 0.0,
        "all_trades_positive_outcome_days": all_positive_days,
        "positive_outcome_day_rate": all_positive_days / days if days else 0.0,
    }


def _is_full_target_win(trade: SweepTrade) -> bool:
    """The 90% objective means reaching the frozen 1R target, not any +PnL."""

    return trade.exit_reason in {"tp", "tp_gap_open"} and trade.pnl_r > 0.0


def screen_cell(
    *,
    prepared: PreparedSeries,
    symbol: str,
    side: str,
    config: SweepConfig,
    effective_spread_budget_bps: float | None,
    stop_floor_bps: float | None,
    extra_round_trip_cost_bps: float,
) -> dict[str, Any]:
    """Screen one config x pair x direction cell, retaining zero outcomes."""

    trades: list[SweepTrade] = []
    reasons: Counter[str] = Counter()
    raw_events = 0
    admitted_events = 0
    last_admitted_index: int | None = None
    for index in range(len(prepared.bars)):
        signal, reason = evaluate_signal(
            prepared=prepared,
            signal_index=index,
            symbol=symbol,
            side=side,
            config=config,
            effective_spread_budget_bps=effective_spread_budget_bps,
            stop_floor_bps=stop_floor_bps,
            extra_round_trip_cost_bps=extra_round_trip_cost_bps,
        )
        if signal is None:
            reasons[reason] += 1
            continue
        raw_events += 1
        if (
            last_admitted_index is not None
            and index - last_admitted_index < COOLDOWN_M1_BARS
        ):
            reasons["cooldown"] += 1
            continue
        last_admitted_index = index
        admitted_events += 1
        trade = simulate_trade(prepared.bars, signal=signal)
        if trade is None:
            reasons["outcome_unresolved"] += 1
            continue
        trades.append(trade)

    pnl_rs = [trade.pnl_r for trade in trades]
    wins = sum(_is_full_target_win(trade) for trade in trades)
    positive_outcomes = sum(value > 0.0 for value in pnl_rs)
    day_stats = _day_statistics(trades)
    return {
        "config_id": config.config_id,
        "level_bars_m15": config.level_bars_m15,
        "penetration_atr5": config.penetration_atr5,
        "wick_floor": config.wick_floor,
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "raw_events": raw_events,
        "events_after_cooldown": admitted_events,
        "trades": len(trades),
        "wins": wins,
        "win_rate": wins / len(trades) if trades else 0.0,
        "full_target_wins": wins,
        "full_target_win_rate": wins / len(trades) if trades else 0.0,
        "positive_outcomes": positive_outcomes,
        "positive_outcome_rate": positive_outcomes / len(trades) if trades else 0.0,
        "total_r": sum(pnl_rs),
        "mean_r": statistics.fmean(pnl_rs) if pnl_rs else 0.0,
        **day_stats,
        "exit_mix": dict(Counter(trade.exit_reason for trade in trades)),
        "reasons": dict(sorted(reasons.items())),
        "fill_delay_bars": FILL_DELAY_M1_BARS,
        "time_stop_bars": TIME_STOP_M1_BARS,
        "cooldown_bars": COOLDOWN_M1_BARS,
        "reward_risk": REWARD_RISK,
        "p_star_max": P_STAR_MAX,
        "min_tp_cost_ratio": MIN_TP_COST_RATIO,
        "extra_round_trip_cost_bps": extra_round_trip_cost_bps,
    }


def trial_accounting(*, n_symbols: int = 18, prior_tests: int = DEFAULT_PRIOR_TESTS) -> dict[str, int]:
    """Honest attempted-cell count; zero-event cells are still attempts."""

    configurations = len(GRID)
    directions = 2
    current = configurations * max(0, int(n_symbols)) * directions
    prior = max(0, int(prior_tests))
    return {
        "grid_configurations": configurations,
        "directions": directions,
        "symbols": max(0, int(n_symbols)),
        "current_attempted_cells": current,
        "prior_attempted_cells": prior,
        "cumulative_attempted_cells": prior + current,
        "expected_full_universe_cells": len(GRID) * len(FX_SYMBOLS) * directions,
    }


def search_corrected_threshold(n_tests: int, *, alpha: float = 0.05) -> float:
    """Two-sided Sidak normal threshold over the cumulative search."""

    tests = max(1, int(n_tests))
    per_test = 1.0 - (1.0 - alpha) ** (1.0 / tests)
    tail_target = max(1e-15, per_test / 2.0)
    low, high = 0.0, 12.0
    for _ in range(200):
        midpoint = (low + high) / 2.0
        tail = 0.5 * math.erfc(midpoint / math.sqrt(2.0))
        if tail > tail_target:
            low = midpoint
        else:
            high = midpoint
    return max(2.5, (low + high) / 2.0)


def _table_value(table: Mapping[str, float] | None, symbol: str) -> float | None:
    if table is None:
        return None
    return _valid_number(table.get(symbol.upper()), allow_zero=True)


def screen_universe(
    *,
    bars_by_symbol: Mapping[str, Sequence[QuoteBar]],
    symbols: Sequence[str] = FX_SYMBOLS,
    venue_spread_budgets_bps: Mapping[str, float] | None = None,
    proxy_spread_budgets_bps: Mapping[str, float] | None = None,
    stop_floors_bps: Mapping[str, float] | None = None,
    extra_round_trip_cost_bps: float = 0.0,
    prior_tests: int = DEFAULT_PRIOR_TESTS,
    source_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Screen the fixed grid and retain every config x pair x side cell."""

    requested = tuple(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    if len(set(requested)) != len(requested):
        raise ValueError("symbols must be unique")
    extra_cost = _valid_number(extra_round_trip_cost_bps, allow_zero=True)
    if extra_cost is None:
        raise ValueError("extra round-trip cost must be finite and non-negative")
    errors = {str(key).upper(): str(value) for key, value in (source_errors or {}).items()}
    cells: list[dict[str, Any]] = []
    missing_budgets: list[str] = []
    missing_sources: list[str] = []

    for symbol in requested:
        rows = list(bars_by_symbol.get(symbol, ()))
        if symbol in errors:
            missing_sources.append(symbol)
        try:
            prepared = prepare_series(rows)
        except ValueError as exc:
            prepared = prepare_series([])
            errors[symbol] = str(exc)
            missing_sources.append(symbol)

        venue_budget = _table_value(venue_spread_budgets_bps, symbol)
        venue_ready = venue_budget is not None and venue_budget > 0.0
        proxy_budget = _table_value(proxy_spread_budgets_bps, symbol)
        proxy_ready = proxy_budget is not None and proxy_budget > 0.0
        if not venue_ready:
            missing_budgets.append(symbol)
        effective_budget = venue_budget if venue_ready else proxy_budget
        if venue_ready:
            cost_mode = "venue_measured"
        elif proxy_ready:
            cost_mode = "pair_specific_proxy_discovery_stress"
        else:
            cost_mode = "cost_unavailable"
        stop_floor = _table_value(stop_floors_bps, symbol)

        for config in GRID:
            for side in ("BUY", "SELL"):
                cell = screen_cell(
                    prepared=prepared,
                    symbol=symbol,
                    side=side,
                    config=config,
                    effective_spread_budget_bps=effective_budget,
                    stop_floor_bps=stop_floor,
                    extra_round_trip_cost_bps=extra_cost,
                )
                cell.update(
                    {
                        "venue_budget_bps": venue_budget,
                        "proxy_budget_bps": proxy_budget,
                        "effective_spread_budget_bps": effective_budget,
                        "stop_floor_bps": stop_floor,
                        "cost_mode": cost_mode,
                        "venue_cost_ready": venue_ready,
                        "economic_claim_ready": venue_ready and symbol not in errors,
                        "economics_claim_ready": venue_ready and symbol not in errors,
                        "source_error": errors.get(symbol),
                    }
                )
                cells.append(cell)

    accounting = trial_accounting(n_symbols=len(requested), prior_tests=prior_tests)
    full_universe = requested == FX_SYMBOLS
    missing_budget_set = sorted(set(missing_budgets))
    missing_source_set = sorted(set(missing_sources))
    economic_claim_ready = (
        full_universe and not missing_budget_set and not missing_source_set
    )
    return {
        "schema_version": "fxstack.scalp.liquidity_sweep_screen.v1",
        "family": "rolling_liquidity_level_sweep_reclaim",
        "research_only": True,
        "future_data_access": "forbidden",
        "success_claim_authorized": False,
        "activation_authorized": False,
        "economic_passed": False,
        "economic_claim_ready": economic_claim_ready,
        "economics_claim_ready": economic_claim_ready,
        "economic_claim_scope": "cost_inputs_only_not_strategy_success",
        "proxy_discovery_stress": any(
            _table_value(proxy_spread_budgets_bps, symbol) is not None
            and _table_value(venue_spread_budgets_bps, symbol) is None
            for symbol in requested
        ),
        "symbols": list(requested),
        "grid": [asdict(config) | {"config_id": config.config_id} for config in GRID],
        "fixed_contract": {
            "volatility": "median TR bps of prior 24 complete M5 bars; signal bucket excluded",
            "levels": "prior N complete M15 bid-low support and ask-high resistance; signal bucket excluded",
            "reclaim_atr5": RECLAIM_ATR5,
            "spread": "Q25 of prior 240 M1 closes plus signal-close and delayed-next-open gates",
            "fill_delay_m1_bars": FILL_DELAY_M1_BARS,
            "fill": "BUY=max(signal ask close,next ask open); SELL=min(signal bid close,next bid open)",
            "cooldown_m1_bars_per_pair_side": COOLDOWN_M1_BARS,
            "stop_buffer_atr5": STOP_BUFFER_ATR5,
            "reward_risk": REWARD_RISK,
            "p_star_max": P_STAR_MAX,
            "min_tp_cost_ratio": MIN_TP_COST_RATIO,
            "time_stop_m1_bars": TIME_STOP_M1_BARS,
            "ambiguous_bar": "stop_loss_first",
            "exit_quote": "bid_for_BUY_ask_for_SELL",
        },
        "cost_readiness": {
            "observed_source_bid_ask_charged": True,
            "extra_adverse_round_trip_cost_bps": extra_cost,
            "missing_venue_budget_symbols": missing_budget_set,
            "missing_source_symbols": missing_source_set,
            "venue_budgets_bps": {
                symbol: _table_value(venue_spread_budgets_bps, symbol)
                for symbol in requested
            },
            "proxy_budgets_bps": {
                symbol: _table_value(proxy_spread_budgets_bps, symbol)
                for symbol in requested
            },
            "effective_budgets_bps": {
                symbol: (
                    _table_value(venue_spread_budgets_bps, symbol)
                    if _table_value(venue_spread_budgets_bps, symbol) is not None
                    else _table_value(proxy_spread_budgets_bps, symbol)
                )
                for symbol in requested
            },
        },
        "search_accounting": accounting
        | {
            "family_alpha": 0.05,
            "two_sided_sidak_abs_t_threshold": search_corrected_threshold(
                accounting["cumulative_attempted_cells"]
            ),
        },
        "cells": cells,
    }


def _load_number_table(path: Path | None, *, value_key: str) -> dict[str, float]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected an object keyed by symbol")
    table: dict[str, float] = {}
    for raw_symbol, raw_value in payload.items():
        symbol = str(raw_symbol).strip().upper()
        value = raw_value.get(value_key) if isinstance(raw_value, dict) else raw_value
        parsed = _valid_number(value, allow_zero=True)
        if symbol and parsed is not None:
            table[symbol] = parsed
    return table


def _sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-root", required=True)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--venue-budgets-json", default=None)
    parser.add_argument("--proxy-budgets-json", default=None)
    parser.add_argument("--stop-floors-json", default=None)
    parser.add_argument("--extra-round-trip-cost-bps", type=float, default=0.0)
    parser.add_argument("--prior-tests", type=int, default=DEFAULT_PRIOR_TESTS)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args(argv)

    csv_root = Path(args.csv_root)
    venue_path = Path(args.venue_budgets_json) if args.venue_budgets_json else None
    proxy_path = Path(args.proxy_budgets_json) if args.proxy_budgets_json else None
    floor_path = Path(args.stop_floors_json) if args.stop_floors_json else None
    venue_budgets = _load_number_table(venue_path, value_key="budget_bps")
    proxy_budgets = _load_number_table(proxy_path, value_key="budget_bps")
    stop_floors = _load_number_table(floor_path, value_key="stop_floor_bps")
    bars_by_symbol: dict[str, list[QuoteBar]] = {}
    source_errors: dict[str, str] = {}
    for symbol in FX_SYMBOLS:
        path = csv_root / f"{symbol}_M1.csv"
        if not path.exists():
            bars_by_symbol[symbol] = []
            source_errors[symbol] = f"missing:{path.name}"
            continue
        try:
            bars_by_symbol[symbol] = load_m1_csv(path, start=args.start, end=args.end)
        except (OSError, ValueError) as exc:
            bars_by_symbol[symbol] = []
            # Evidence stays path-scrubbed when transferred out of isolation.
            source_errors[symbol] = (
                f"invalid_or_unreadable:{path.name}:{type(exc).__name__}"
            )

    result = screen_universe(
        bars_by_symbol=bars_by_symbol,
        venue_spread_budgets_bps=venue_budgets,
        proxy_spread_budgets_bps=proxy_budgets,
        stop_floors_bps=stop_floors,
        extra_round_trip_cost_bps=args.extra_round_trip_cost_bps,
        prior_tests=args.prior_tests,
        source_errors=source_errors,
    )
    result["input_metadata"] = {
        "csv_root_recorded": False,
        "start": args.start,
        "end_exclusive": args.end,
        "venue_budget_sha256": _sha256(venue_path),
        "proxy_budget_sha256": _sha256(proxy_path),
        "stop_floor_sha256": _sha256(floor_path),
    }
    output = Path(args.json_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=1, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    accounting = result["search_accounting"]
    print(
        f"LSR: {len(result['cells'])} cells; "
        f"{accounting['current_attempted_cells']} current / "
        f"{accounting['cumulative_attempted_cells']} cumulative; "
        f"economic_claim_ready={result['economic_claim_ready']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
