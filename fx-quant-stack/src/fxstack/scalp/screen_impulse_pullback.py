# AGENT: ROLE: Research-only sparse impulse-pullback-continuation screen.
# AGENT: ENTRYPOINT: `screen_universe`; CLI `python -m fxstack.scalp.screen_impulse_pullback`.
# AGENT: PRIMARY INPUTS: immutable bid/ask M1 CSV snapshots and frozen pair-cost tables.
# AGENT: PRIMARY OUTPUTS: every fixed-grid x pair x direction cell, including zeros.
# AGENT ISOLATION: advisory discovery evidence only; never authorizes success or activation.
"""Causal M1 screen for a symmetric Impulse-Pullback Continuation (IPC).

One bounded pattern is tested.  A directional k-bar impulse must exceed a
strictly prior volatility baseline, then the closed signal bar must make a
limited counter-directional sweep and reject it in the impulse direction.
The 240-bar volatility and spread baselines end *before* the entire impulse
plus pullback pattern, so the event cannot normalize or cheapen itself.

Execution is deliberately conservative: the signal fills one M1 bar later at
the observed next-open quote; source bid/ask is used on both legs; an
additional adverse round-trip cost is charged; stops win ambiguous bars; and
no breakeven, trail, partial close, or limit fill exists.

This module emits research-only descriptive evidence. Pair-specific proxy
budgets may make a discovery replay runnable but can never make economics
claim-ready. No output can authorize a success claim, activation, or order.
Its default search ledger starts after 2,514 frozen prior cells, so the eight
IPC configurations across 18 pairs and two directions advance it to 2,802.
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
from collections import Counter
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

M1_REQUIRED_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
)

BASELINE_M1_BARS = 240
MIN_RETRACE_FRACTION = 0.10
STOP_BUFFER_VOL = 0.10
MAX_STOP_FLOOR_BPS = 25.0
MAX_INITIAL_RISK_BPS = 25.0
COOLDOWN_M1_BARS = 30
TIME_STOP_M1_BARS = 8
FILL_DELAY_M1_BARS = 1
REWARD_RISK = 1.0
P_STAR_MAX = 0.55
MIN_TP_COST_RATIO = 4.0
DEFAULT_PRIOR_TESTS = 2_514


@dataclass(frozen=True, slots=True)
class IPCConfig:
    impulse_bars: int
    min_impulse_vol: float
    max_retrace_fraction: float

    @property
    def config_id(self) -> str:
        impulse = int(round(self.min_impulse_vol * 100.0))
        retrace = int(round(self.max_retrace_fraction * 100.0))
        return f"k{self.impulse_bars}_i{impulse:03d}_r{retrace:02d}"


GRID: tuple[IPCConfig, ...] = tuple(
    IPCConfig(impulse_bars=k, min_impulse_vol=minimum, max_retrace_fraction=retrace)
    for k in (3, 5)
    for minimum in (1.5, 2.5)
    for retrace in (0.40, 0.60)
)

MIN_SOURCE_CONSECUTIVE_BARS = (
    BASELINE_M1_BARS
    + 1  # previous close for the first true range
    + max(config.impulse_bars for config in GRID)
    + 1  # closed signal bar
    + TIME_STOP_M1_BARS  # includes the delayed-entry bar
)


@dataclass(frozen=True, slots=True)
class QuoteBar:
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
class BaselineContext:
    pattern_start_index: int
    volatility_bps: float
    spread_q25_bps: float


@dataclass(frozen=True, slots=True)
class IPCSignal:
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
    volatility_bps: float
    impulse_bps: float
    impulse_vol_units: float
    directional_impulse_bars: int
    retrace_fraction: float
    close_location: float
    spread_q25_bps: float
    signal_spread_bps: float
    entry_spread_bps: float


@dataclass(frozen=True, slots=True)
class IPCTrade:
    symbol: str
    side: str
    config_id: str
    signal_epoch: int
    entry_epoch: int
    exit_epoch: int
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    risk_bps: float
    total_cost_bps: float
    extra_round_trip_cost_bps: float
    p_star: float
    pnl_bps: float
    pnl_r: float
    bars_held: int
    exit_reason: str


@dataclass(slots=True)
class PreparedSeries:
    bars: list[QuoteBar]
    volatility_before_index: list[float | None]
    spread_q25_before_index: list[float | None]


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
    return not any(
        ask < bid
        for bid, ask in (
            (bar.bid_o, bar.ask_o),
            (bar.bid_h, bar.ask_h),
            (bar.bid_l, bar.ask_l),
            (bar.bid_c, bar.ask_c),
        )
    )


def _parse_epoch(text: str | None) -> int | None:
    if not text:
        return None
    parsed = dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    timestamp = parsed.timestamp()
    if parsed.microsecond != 0 or not math.isfinite(timestamp) or not timestamp.is_integer():
        raise ValueError("timestamp must resolve to an exact whole second")
    return int(timestamp)


def load_m1_csv(
    path: Path, *, start: str | None = None, end: str | None = None
) -> list[QuoteBar]:
    """Load strict monotonic bid/ask M1; gaps remain visible and break context."""

    start_epoch = _parse_epoch(start)
    end_epoch = _parse_epoch(end)
    rows: list[QuoteBar] = []
    last_epoch: int | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header or tuple(header[: len(M1_REQUIRED_COLUMNS)]) != M1_REQUIRED_COLUMNS:
            raise ValueError(f"{path.name}: unexpected M1 header")
        for row_number, row in enumerate(reader, start=2):
            try:
                epoch = _parse_epoch(row[0])
                assert epoch is not None
                values = [float(value) for value in row[1:9]]
            except (AssertionError, IndexError, TypeError, ValueError) as exc:
                raise ValueError(f"{path.name}:{row_number}: malformed M1 row") from exc
            if start_epoch is not None and epoch < start_epoch:
                continue
            if end_epoch is not None and epoch >= end_epoch:
                break
            if last_epoch is not None and epoch <= last_epoch:
                raise ValueError(f"{path.name}:{row_number}: non-monotonic timestamp")
            bar = QuoteBar(epoch, *values)
            if not validate_quote_bar(bar):
                raise ValueError(f"{path.name}:{row_number}: invalid bid/ask OHLC")
            rows.append(bar)
            last_epoch = epoch
    return rows


def _remove_sorted(values: list[float], value: float) -> None:
    index = bisect.bisect_left(values, value)
    if index >= len(values) or values[index] != value:
        raise RuntimeError("rolling multiset lost synchronization")
    values.pop(index)


def _quantile_sorted(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    position = (len(values) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(values[lower])
    weight = position - lower
    return float(values[lower] * (1.0 - weight) + values[upper] * weight)


def _true_range_bps(previous: QuoteBar, current: QuoteBar) -> float:
    close = current.mid_c
    if close <= 0.0:
        return 0.0
    true_range = max(
        current.mid_h - current.mid_l,
        abs(current.mid_h - previous.mid_c),
        abs(current.mid_l - previous.mid_c),
    )
    return true_range / close * 1e4


def _consecutive_runs(bars: Sequence[QuoteBar]) -> list[tuple[int, int]]:
    if not bars:
        return []
    runs: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(bars)):
        if bars[index].epoch != bars[index - 1].epoch + 60:
            runs.append((start, index))
            start = index
    runs.append((start, len(bars)))
    return runs


def _build_prepattern_baselines(
    bars: Sequence[QuoteBar],
) -> tuple[list[float | None], list[float | None]]:
    """Values keyed by pattern start; exactly 240 bars end before that start.

    A 241st prior bar supplies the previous close for the first of the 240
    true ranges. All 241 bars must belong to one exact consecutive M1 run.
    """

    volatility: list[float | None] = [None] * len(bars)
    q25: list[float | None] = [None] * len(bars)
    for run_start, run_end in _consecutive_runs(bars):
        first_pattern_start = run_start + BASELINE_M1_BARS + 1
        if first_pattern_start >= run_end:
            continue
        window_start = first_pattern_start - BASELINE_M1_BARS
        window_end = first_pattern_start
        spread_ordered = sorted(
            bars[index].spread_close_bps for index in range(window_start, window_end)
        )
        tr_ordered = sorted(
            _true_range_bps(bars[index - 1], bars[index])
            for index in range(window_start, window_end)
        )
        for pattern_start in range(first_pattern_start, run_end):
            median_tr = statistics.median(tr_ordered)
            if math.isfinite(median_tr) and median_tr > 0.0:
                volatility[pattern_start] = median_tr
                q25[pattern_start] = _quantile_sorted(spread_ordered, 0.25)
            next_pattern_start = pattern_start + 1
            if next_pattern_start >= run_end:
                continue
            expired_index = pattern_start - BASELINE_M1_BARS
            admitted_index = pattern_start
            _remove_sorted(spread_ordered, bars[expired_index].spread_close_bps)
            bisect.insort(spread_ordered, bars[admitted_index].spread_close_bps)
            _remove_sorted(
                tr_ordered,
                _true_range_bps(bars[expired_index - 1], bars[expired_index]),
            )
            bisect.insort(
                tr_ordered,
                _true_range_bps(bars[admitted_index - 1], bars[admitted_index]),
            )
    return volatility, q25


def prepare_series(bars: Iterable[QuoteBar]) -> PreparedSeries:
    rows = list(bars)
    for index, bar in enumerate(rows):
        if not validate_quote_bar(bar):
            raise ValueError(f"invalid quote bar at index {index}")
        if index and bar.epoch <= rows[index - 1].epoch:
            raise ValueError("M1 timestamps must be strictly increasing")
    volatility, q25 = _build_prepattern_baselines(rows)
    return PreparedSeries(rows, volatility, q25)


def baseline_context_at(
    prepared: PreparedSeries, *, signal_index: int, impulse_bars: int
) -> BaselineContext | None:
    pattern_start = signal_index - impulse_bars
    if pattern_start < 0 or signal_index >= len(prepared.bars):
        return None
    volatility = prepared.volatility_before_index[pattern_start]
    q25 = prepared.spread_q25_before_index[pattern_start]
    if volatility is None or q25 is None or volatility <= 0.0 or q25 <= 0.0:
        return None
    # The impulse and pullback themselves must also be an exact M1 sequence.
    pattern = prepared.bars[pattern_start : signal_index + 1]
    if len(pattern) != impulse_bars + 1 or any(
        right.epoch != left.epoch + 60 for left, right in zip(pattern, pattern[1:])
    ):
        return None
    return BaselineContext(pattern_start, volatility, q25)


def _entry_price(signal_bar: QuoteBar, next_bar: QuoteBar, *, side: str) -> float:
    del signal_bar  # The closed signal bar may not alter the executable t+1 fill.
    if side == "BUY":
        return next_bar.ask_o
    if side == "SELL":
        return next_bar.bid_o
    raise ValueError("side must be BUY or SELL")


def evaluate_signal(
    *,
    prepared: PreparedSeries,
    signal_index: int,
    symbol: str,
    side: str,
    config: IPCConfig,
    effective_spread_budget_bps: float | None,
    stop_floor_bps: float | None = None,
    extra_round_trip_cost_bps: float = 0.0,
) -> tuple[IPCSignal | None, str]:
    """Evaluate one IPC event using only the closed pattern and t+1 open."""

    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    budget = _valid_number(effective_spread_budget_bps)
    if budget is None:
        return None, "cost_unavailable"
    floor = _valid_number(stop_floor_bps, allow_zero=True)
    if stop_floor_bps is not None and floor is None:
        return None, "stop_floor_invalid"
    floor = float(floor or 0.0)
    if floor > MAX_STOP_FLOOR_BPS:
        return None, "stop_floor_above_sane_bound"
    extra_cost = _valid_number(extra_round_trip_cost_bps, allow_zero=True)
    if extra_cost is None:
        raise ValueError("extra round-trip cost must be finite and non-negative")
    if signal_index < 0 or signal_index + FILL_DELAY_M1_BARS >= len(prepared.bars):
        return None, "delayed_fill_unavailable"
    signal_bar = prepared.bars[signal_index]
    next_bar = prepared.bars[signal_index + FILL_DELAY_M1_BARS]
    if next_bar.epoch != signal_bar.epoch + 60:
        return None, "delayed_fill_gap"
    context = baseline_context_at(
        prepared, signal_index=signal_index, impulse_bars=config.impulse_bars
    )
    if context is None:
        return None, "strict_prepattern_context_unavailable"

    signal_spread = signal_bar.spread_close_bps
    entry_spread = next_bar.spread_open_bps
    spread_cap = min(context.spread_q25_bps, budget)
    if signal_spread > spread_cap + 1e-12:
        return None, "signal_spread_above_q25_or_budget"
    if entry_spread > spread_cap + 1e-12:
        return None, "entry_spread_above_q25_or_budget"

    impulse = prepared.bars[context.pattern_start_index : signal_index]
    first, last = impulse[0], impulse[-1]
    reference = signal_bar.mid_c
    volatility = context.volatility_bps
    if side == "BUY":
        impulse_price = last.bid_c - first.bid_o
        directional_bars = sum(bar.bid_c > bar.bid_o for bar in impulse)
        signal_range = signal_bar.bid_h - signal_bar.bid_l
        retrace_price = last.bid_c - signal_bar.bid_l
        close_location = (
            (signal_bar.bid_c - signal_bar.bid_l) / signal_range
            if signal_range > 0.0
            else 0.0
        )
        directional_signal = (
            signal_bar.bid_c > signal_bar.bid_o
            and signal_bar.bid_c > last.bid_c
            and close_location >= 0.75
        )
    else:
        impulse_price = first.ask_o - last.ask_c
        directional_bars = sum(bar.ask_c < bar.ask_o for bar in impulse)
        signal_range = signal_bar.ask_h - signal_bar.ask_l
        retrace_price = signal_bar.ask_h - last.ask_c
        close_location = (
            (signal_bar.ask_h - signal_bar.ask_c) / signal_range
            if signal_range > 0.0
            else 0.0
        )
        directional_signal = (
            signal_bar.ask_c < signal_bar.ask_o
            and signal_bar.ask_c < last.ask_c
            and close_location >= 0.75
        )
    if impulse_price <= 0.0 or reference <= 0.0:
        return None, "no_directional_impulse"
    impulse_bps = impulse_price / reference * 1e4
    impulse_units = impulse_bps / volatility
    if impulse_units < config.min_impulse_vol:
        return None, "impulse_too_small"
    if directional_bars < config.impulse_bars - 1:
        return None, "impulse_breadth_too_low"
    retrace_fraction = retrace_price / impulse_price
    if retrace_fraction < MIN_RETRACE_FRACTION:
        return None, "pullback_too_shallow"
    if retrace_fraction > config.max_retrace_fraction:
        return None, "pullback_too_deep"
    if not directional_signal:
        return None, "pullback_rejection_missing"

    entry = _entry_price(signal_bar, next_bar, side=side)
    buffer_price = STOP_BUFFER_VOL * volatility / 1e4 * reference
    if side == "BUY":
        raw_stop = signal_bar.bid_l - buffer_price
        raw_risk_bps = (entry - raw_stop) / entry * 1e4
    else:
        raw_stop = signal_bar.ask_h + buffer_price
        raw_risk_bps = (raw_stop - entry) / entry * 1e4
    if not math.isfinite(raw_risk_bps) or raw_risk_bps <= 0.0:
        return None, "degenerate_stop"
    risk_bps = max(raw_risk_bps, floor)
    if risk_bps > MAX_INITIAL_RISK_BPS:
        return None, "initial_risk_above_sane_bound"
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
    if not all(
        math.isfinite(value) and value > 0.0
        for value in (entry, stop_price, target_price)
    ):
        return None, "invalid_bracket_geometry"
    if (side == "BUY" and not stop_price < entry < target_price) or (
        side == "SELL" and not target_price < entry < stop_price
    ):
        return None, "invalid_bracket_geometry"
    return (
        IPCSignal(
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
            volatility_bps=volatility,
            impulse_bps=impulse_bps,
            impulse_vol_units=impulse_units,
            directional_impulse_bars=directional_bars,
            retrace_fraction=retrace_fraction,
            close_location=close_location,
            spread_q25_bps=context.spread_q25_bps,
            signal_spread_bps=signal_spread,
            entry_spread_bps=entry_spread,
        ),
        "",
    )


def _trade_result(
    signal: IPCSignal,
    *,
    exit_bar: QuoteBar,
    exit_price: float,
    bars_held: int,
    reason: str,
) -> IPCTrade:
    if signal.side == "BUY":
        gross_bps = (exit_price - signal.entry_price) / signal.entry_price * 1e4
    else:
        gross_bps = (signal.entry_price - exit_price) / signal.entry_price * 1e4
    pnl_bps = gross_bps - signal.extra_round_trip_cost_bps
    return IPCTrade(
        symbol=signal.symbol,
        side=signal.side,
        config_id=signal.config_id,
        signal_epoch=signal.signal_epoch,
        entry_epoch=exit_bar.epoch - (bars_held - 1) * 60,
        exit_epoch=exit_bar.epoch,
        entry_price=signal.entry_price,
        exit_price=exit_price,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        risk_bps=signal.risk_bps,
        total_cost_bps=signal.total_cost_bps,
        extra_round_trip_cost_bps=signal.extra_round_trip_cost_bps,
        p_star=signal.p_star,
        pnl_bps=pnl_bps,
        pnl_r=pnl_bps / signal.risk_bps,
        bars_held=bars_held,
        exit_reason=reason,
    )


def simulate_trade(bars: Sequence[QuoteBar], *, signal: IPCSignal) -> IPCTrade | None:
    """Eight-bar adverse-quote replay; gaps adverse and double touches stop first."""

    entry_index = signal.entry_index
    horizon_end = entry_index + TIME_STOP_M1_BARS
    if entry_index < 0 or horizon_end > len(bars):
        return None
    entry_epoch = bars[entry_index].epoch
    if any(
        bars[index].epoch != entry_epoch + offset * 60
        for offset, index in enumerate(range(entry_index, horizon_end))
    ):
        # Validate the whole fixed horizon before looking at any barrier.  An
        # early TP/SL must not survive merely because a later gap or truncated
        # file would have censored an otherwise unresolved path.
        return None
    for offset in range(TIME_STOP_M1_BARS):
        index = entry_index + offset
        bar = bars[index]
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
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=bar.bid_c if buy else bar.ask_c,
                bars_held=TIME_STOP_M1_BARS,
                reason="time_stop",
            )
    return None


def _is_full_target_win(trade: IPCTrade) -> bool:
    return trade.exit_reason in {"tp", "tp_gap_open"} and trade.pnl_r > 0.0


def _day(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def _day_statistics(trades: Sequence[IPCTrade]) -> dict[str, Any]:
    by_day: dict[str, list[IPCTrade]] = {}
    for trade in trades:
        by_day.setdefault(_day(trade.entry_epoch), []).append(trade)
    day_means = [
        statistics.fmean(trade.pnl_r for trade in day_trades)
        for day_trades in by_day.values()
    ]
    days = len(day_means)
    day_t = 0.0
    lower: float | None = None
    if days >= 2:
        mean = statistics.fmean(day_means)
        sd = statistics.stdev(day_means)
        if sd > 1e-12:
            standard_error = sd / math.sqrt(days)
            day_t = mean / standard_error
            lower = mean - 1.6448536269514722 * standard_error
    full_target_days = sum(
        1
        for day_trades in by_day.values()
        if day_trades and all(_is_full_target_win(trade) for trade in day_trades)
    )
    positive_days = sum(
        1
        for day_trades in by_day.values()
        if day_trades and all(trade.pnl_r > 0.0 for trade in day_trades)
    )
    return {
        "independent_days": days,
        "day_clustered_t": day_t,
        "day_mean_r_lcb95_descriptive": lower,
        "all_trades_full_target_win_days": full_target_days,
        "full_target_day_win_rate": full_target_days / days if days else 0.0,
        "all_trades_positive_outcome_days": positive_days,
        "positive_outcome_day_rate": positive_days / days if days else 0.0,
    }


def screen_cell(
    *,
    prepared: PreparedSeries,
    symbol: str,
    side: str,
    config: IPCConfig,
    effective_spread_budget_bps: float | None,
    stop_floor_bps: float | None,
    extra_round_trip_cost_bps: float,
) -> dict[str, Any]:
    trades: list[IPCTrade] = []
    reasons: Counter[str] = Counter()
    raw_events = 0
    admitted_events = 0
    last_admitted_index: int | None = None
    admitted_entry_days: set[str] = set()
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
        trade = simulate_trade(prepared.bars, signal=signal)
        if trade is None:
            reasons["outcome_unresolved"] += 1
            continue
        if last_admitted_index is not None and index - last_admitted_index < COOLDOWN_M1_BARS:
            reasons["cooldown"] += 1
            continue
        entry_day = _day(prepared.bars[signal.entry_index].epoch)
        if entry_day in admitted_entry_days:
            reasons["one_trade_per_entry_day"] += 1
            continue
        last_admitted_index = index
        admitted_entry_days.add(entry_day)
        admitted_events += 1
        trades.append(trade)

    pnl_rs = [trade.pnl_r for trade in trades]
    full_target_wins = sum(_is_full_target_win(trade) for trade in trades)
    positive_outcomes = sum(value > 0.0 for value in pnl_rs)
    return {
        "config_id": config.config_id,
        "impulse_bars": config.impulse_bars,
        "min_impulse_vol": config.min_impulse_vol,
        "min_retrace_fraction": MIN_RETRACE_FRACTION,
        "max_retrace_fraction": config.max_retrace_fraction,
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "raw_events": raw_events,
        "events_after_cooldown": admitted_events,
        "trades": len(trades),
        "wins": full_target_wins,
        "win_rate": full_target_wins / len(trades) if trades else 0.0,
        "full_target_wins": full_target_wins,
        "full_target_win_rate": full_target_wins / len(trades) if trades else 0.0,
        "positive_outcomes": positive_outcomes,
        "positive_outcome_rate": positive_outcomes / len(trades) if trades else 0.0,
        "total_r": sum(pnl_rs),
        "mean_r": statistics.fmean(pnl_rs) if pnl_rs else 0.0,
        **_day_statistics(trades),
        "exit_mix": dict(Counter(trade.exit_reason for trade in trades)),
        "trade_ledger": [
            asdict(trade)
            | {
                "entry_day_utc": _day(trade.entry_epoch),
                "full_target_win": _is_full_target_win(trade),
            }
            for trade in trades
        ],
        "reasons": dict(sorted(reasons.items())),
        "fill_delay_bars": FILL_DELAY_M1_BARS,
        "time_stop_bars": TIME_STOP_M1_BARS,
        "cooldown_bars": COOLDOWN_M1_BARS,
        "maximum_trades_per_cell_entry_day": 1,
        "reward_risk": REWARD_RISK,
        "p_star_max": P_STAR_MAX,
        "min_tp_cost_ratio": MIN_TP_COST_RATIO,
        "extra_round_trip_cost_bps": extra_round_trip_cost_bps,
    }


def trial_accounting(*, n_symbols: int = 18, prior_tests: int = DEFAULT_PRIOR_TESTS) -> dict[str, int]:
    symbols_reported = max(0, int(n_symbols))
    reported = len(GRID) * symbols_reported * 2
    prior = int(prior_tests)
    if prior != DEFAULT_PRIOR_TESTS:
        raise ValueError(
            f"IPC v1 requires the frozen prior attempt count {DEFAULT_PRIOR_TESTS}"
        )
    current = len(GRID) * len(FX_SYMBOLS) * 2
    return {
        "grid_configurations": len(GRID),
        "directions": 2,
        "symbols": symbols_reported,
        "reported_current_cells": reported,
        "current_attempted_cells": current,
        "prior_attempted_cells": prior,
        "cumulative_attempted_cells": prior + current,
        "expected_full_universe_cells": len(GRID) * len(FX_SYMBOLS) * 2,
    }


def search_corrected_threshold(n_tests: int, *, alpha: float = 0.05) -> float:
    tests = max(1, int(n_tests))
    per_test = 1.0 - (1.0 - alpha) ** (1.0 / tests)
    target = max(1e-15, per_test / 2.0)
    low, high = 0.0, 12.0
    for _ in range(200):
        midpoint = (low + high) / 2.0
        tail = 0.5 * math.erfc(midpoint / math.sqrt(2.0))
        if tail > target:
            low = midpoint
        else:
            high = midpoint
    return max(2.5, (low + high) / 2.0)


def _table_value(table: Mapping[str, float] | None, symbol: str) -> float | None:
    if table is None:
        return None
    return _valid_number(table.get(symbol.upper()), allow_zero=True)


def _has_sufficient_source(prepared: PreparedSeries) -> bool:
    return any(
        run_end - run_start >= MIN_SOURCE_CONSECUTIVE_BARS
        for run_start, run_end in _consecutive_runs(prepared.bars)
    )


def _venue_provenance_ready(
    record: Mapping[str, Any] | None, *, budget_bps: float | None
) -> bool:
    """Require one cost measurement to be identity-, time-, and hash-bound."""

    if not isinstance(record, Mapping) or budget_bps is None or budget_bps <= 0.0:
        return False
    recorded_budget = _valid_number(record.get("budget_bps"))
    if recorded_budget is None or not math.isclose(
        recorded_budget, budget_bps, rel_tol=0.0, abs_tol=1e-12
    ):
        return False
    if not str(record.get("venue", "")).strip() or not str(
        record.get("method", "")
    ).strip():
        return False
    try:
        start = _parse_epoch(str(record.get("observed_start_utc", "")))
        end = _parse_epoch(str(record.get("observed_end_utc", "")))
        sample_count = int(record.get("sample_count", 0))
    except (TypeError, ValueError):
        return False
    digest = str(record.get("source_sha256", "")).strip().lower()
    return bool(
        start is not None
        and end is not None
        and end > start
        and sample_count > 0
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def screen_universe(
    *,
    bars_by_symbol: Mapping[str, Sequence[QuoteBar]],
    symbols: Sequence[str] = FX_SYMBOLS,
    venue_spread_budgets_bps: Mapping[str, float] | None = None,
    venue_cost_provenance: Mapping[str, Mapping[str, Any]] | None = None,
    proxy_spread_budgets_bps: Mapping[str, float] | None = None,
    stop_floors_bps: Mapping[str, float] | None = None,
    extra_round_trip_cost_bps: float = 0.0,
    prior_tests: int = DEFAULT_PRIOR_TESTS,
    source_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    requested = tuple(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    if len(set(requested)) != len(requested):
        raise ValueError("symbols must be unique")
    extra_cost = _valid_number(extra_round_trip_cost_bps, allow_zero=True)
    if extra_cost is None:
        raise ValueError("extra round-trip cost must be finite and non-negative")
    errors = {str(key).upper(): str(value) for key, value in (source_errors or {}).items()}
    cells: list[dict[str, Any]] = []
    missing_venue_budgets: list[str] = []
    missing_venue_provenance: list[str] = []
    missing_stop_floors: list[str] = []
    source_failures: list[str] = []
    effective_budgets: dict[str, float | None] = {}

    for symbol in requested:
        try:
            prepared = prepare_series(bars_by_symbol.get(symbol, ()))
        except ValueError as exc:
            prepared = prepare_series([])
            errors[symbol] = str(exc)
        source_ready = symbol not in errors and _has_sufficient_source(prepared)
        if not source_ready and symbol not in errors:
            errors[symbol] = "insufficient_exact_consecutive_m1_history"
        if symbol in errors:
            source_failures.append(symbol)
        venue_budget = _table_value(venue_spread_budgets_bps, symbol)
        proxy_budget = _table_value(proxy_spread_budgets_bps, symbol)
        provenance = (
            venue_cost_provenance.get(symbol)
            if venue_cost_provenance is not None
            else None
        )
        venue_budget_present = venue_budget is not None and venue_budget > 0.0
        venue_ready = venue_budget_present and _venue_provenance_ready(
            provenance, budget_bps=venue_budget
        )
        proxy_ready = proxy_budget is not None and proxy_budget > 0.0
        if not venue_budget_present:
            missing_venue_budgets.append(symbol)
        if not venue_ready:
            missing_venue_provenance.append(symbol)
        effective_budget = (
            venue_budget
            if venue_budget_present
            else (proxy_budget if proxy_ready else None)
        )
        effective_budgets[symbol] = effective_budget
        if venue_ready:
            cost_mode = "venue_measured"
        elif venue_budget_present:
            cost_mode = "unverified_venue_budget_discovery_stress"
        elif proxy_ready:
            cost_mode = "pair_specific_proxy_discovery_stress"
        else:
            cost_mode = "cost_unavailable"
        stop_floor = _table_value(stop_floors_bps, symbol)
        stop_floor_ready = stop_floor is not None and stop_floor > 0.0
        if not stop_floor_ready:
            missing_stop_floors.append(symbol)
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
                ready = venue_ready and source_ready and stop_floor_ready
                cell.update(
                    {
                        "venue_budget_bps": venue_budget,
                        "proxy_budget_bps": proxy_budget,
                        "effective_spread_budget_bps": effective_budget,
                        "stop_floor_bps": stop_floor,
                        "cost_mode": cost_mode,
                        "venue_cost_ready": venue_ready,
                        "venue_provenance_ready": venue_ready,
                        "source_ready": source_ready,
                        "stop_floor_ready": stop_floor_ready,
                        "economic_claim_ready": ready,
                        "economics_claim_ready": ready,
                        "source_error": errors.get(symbol),
                    }
                )
                cells.append(cell)

    accounting = trial_accounting(n_symbols=len(requested), prior_tests=prior_tests)
    missing = sorted(set(missing_venue_budgets))
    provenance_missing = sorted(set(missing_venue_provenance))
    floors_missing = sorted(set(missing_stop_floors))
    failures = sorted(set(source_failures))
    full_universe = requested == FX_SYMBOLS
    economics_ready = (
        full_universe
        and not missing
        and not provenance_missing
        and not floors_missing
        and not failures
    )
    trade_ledger = [
        trade
        for cell in cells
        for trade in cell.pop("trade_ledger")
    ]
    return {
        "schema_version": "fxstack.scalp.impulse_pullback_screen.v1",
        "family": "impulse_pullback_continuation",
        "research_only": True,
        "future_data_access": "forbidden",
        "success_claim_authorized": False,
        "activation_authorized": False,
        "economic_passed": False,
        "economic_claim_ready": economics_ready,
        "economics_claim_ready": economics_ready,
        "economic_claim_scope": "cost_inputs_only_not_strategy_success",
        "proxy_discovery_stress": any(
            cell["cost_mode"] != "venue_measured"
            and cell["effective_spread_budget_bps"] is not None
            for cell in cells
        ),
        "symbols": list(requested),
        "grid": [asdict(config) | {"config_id": config.config_id} for config in GRID],
        "fixed_contract": {
            "baseline": "median of 240 M1 true ranges strictly before pattern; 241st bar supplies first previous close",
            "spread": "Q25 of the same 240 prepattern M1 closes; signal-close and t+1-open gates",
            "minimum_retrace_fraction": MIN_RETRACE_FRACTION,
            "directional_impulse_closes": "at least k-1 of k",
            "signal_close_location": "top quartile BUY / bottom quartile SELL",
            "fill_delay_m1_bars": FILL_DELAY_M1_BARS,
            "fill": "exact t+1 open: BUY=next ask open; SELL=next bid open",
            "cooldown_m1_bars_per_pair_side": COOLDOWN_M1_BARS,
            "maximum_trades_per_cell_entry_day": 1,
            "stop_buffer_vol": STOP_BUFFER_VOL,
            "maximum_stop_floor_bps": MAX_STOP_FLOOR_BPS,
            "maximum_initial_risk_bps": MAX_INITIAL_RISK_BPS,
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
            "missing_venue_budget_symbols": missing,
            "missing_or_invalid_venue_provenance_symbols": provenance_missing,
            "missing_or_zero_stop_floor_symbols": floors_missing,
            "source_failure_symbols": failures,
            "venue_budgets_bps": {
                symbol: _table_value(venue_spread_budgets_bps, symbol)
                for symbol in requested
            },
            "proxy_budgets_bps": {
                symbol: _table_value(proxy_spread_budgets_bps, symbol)
                for symbol in requested
            },
            "effective_budgets_bps": effective_budgets,
        },
        "search_accounting": accounting
        | {
            "family_alpha": 0.05,
            "two_sided_sidak_abs_t_threshold": search_corrected_threshold(
                accounting["cumulative_attempted_cells"]
            ),
        },
        "cells": cells,
        "trade_ledger": trade_ledger,
    }


def _load_number_table(path: Path | None, *, value_key: str) -> dict[str, float]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: expected a symbol-keyed object")
    table: dict[str, float] = {}
    for raw_symbol, raw_value in payload.items():
        symbol = str(raw_symbol).strip().upper()
        value = raw_value.get(value_key) if isinstance(raw_value, dict) else raw_value
        parsed = _valid_number(value, allow_zero=True)
        if symbol and parsed is not None:
            table[symbol] = parsed
    return table


def _load_venue_provenance(path: Path | None) -> dict[str, Mapping[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: expected a symbol-keyed object")
    return {
        str(symbol).strip().upper(): value
        for symbol, value in payload.items()
        if str(symbol).strip() and isinstance(value, Mapping)
    }


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
    parser.add_argument("--trade-ledger-out", required=True)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args(argv)

    ledger_output = Path(args.trade_ledger_out).resolve()
    output = Path(args.json_out).resolve()
    if ledger_output == output:
        parser.error("--trade-ledger-out and --json-out must be distinct paths")

    csv_root = Path(args.csv_root)
    venue_path = Path(args.venue_budgets_json) if args.venue_budgets_json else None
    proxy_path = Path(args.proxy_budgets_json) if args.proxy_budgets_json else None
    floor_path = Path(args.stop_floors_json) if args.stop_floors_json else None
    venue_budgets = _load_number_table(venue_path, value_key="budget_bps")
    venue_provenance = _load_venue_provenance(venue_path)
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
            source_errors[symbol] = (
                f"invalid_or_unreadable:{path.name}:{type(exc).__name__}"
            )

    result = screen_universe(
        bars_by_symbol=bars_by_symbol,
        venue_spread_budgets_bps=venue_budgets,
        venue_cost_provenance=venue_provenance,
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
    ledger_output.parent.mkdir(parents=True, exist_ok=True)
    ledger_payload = {
        "schema_version": "fxstack.scalp.impulse_pullback_trade_ledger.v1",
        "family": result["family"],
        "trades": result.pop("trade_ledger"),
    }
    ledger_output.write_text(
        json.dumps(ledger_payload, indent=1, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    result["trade_ledger_evidence"] = {
        "path_recorded": False,
        "trades": len(ledger_payload["trades"]),
        "file_sha256": _sha256(ledger_output),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=1, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    accounting = result["search_accounting"]
    print(
        f"IPC: {len(result['cells'])} cells; "
        f"{accounting['current_attempted_cells']} current / "
        f"{accounting['cumulative_attempted_cells']} cumulative; "
        f"economics_claim_ready={result['economics_claim_ready']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
