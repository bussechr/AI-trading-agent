# AGENT: ROLE: Research-only dense median-stretch-reversal M1 screen.
# AGENT: ENTRYPOINT: `screen_universe`; CLI `python -m fxstack.scalp.screen_median_stretch_reversal`.
# AGENT: PRIMARY INPUTS: immutable bid/ask M1 CSV snapshots and one frozen proxy-cost contract.
# AGENT: PRIMARY OUTPUTS: every fixed cell plus complete reservation and scored-trade ledgers.
# AGENT ISOLATION: advisory discovery evidence only; never authorizes success or activation.
"""Causal M1 screen for symmetric Median-Stretch Reversal (MSR).

MSR is a dense two-close mean-reversion hypothesis.  Its fair value, true-range
scale, and spread baseline all end before the stretched close at t-1.  The
closed t bar must reverse toward fair value, and execution is exactly at the
observed t+1 opening quote.  No signal-close fallback price exists.

Every admitted cell/day is reserved before outcome-horizon availability is
checked.  A missing future minute therefore creates an explicit unresolved
ledger row and cannot be replaced by a later signal.  A scored trade requires
all twelve consecutive M1 outcome bars before any exit is inspected.  Gaps
are adverse, stops win ambiguous bars, and no trade management is simulated.

Observed bid/ask quotes are used throughout.  Pair-specific proxy spread
budgets require frozen provenance, and a fixed adverse one-basis-point
round-trip pad is charged.  The fixed prior-search ledger is 2,802 cells; the
first frozen MSR attempt added 288 fail-closed source-contract cells without
parsing a numeric market row.  The corrected fixed prior-search ledger is
therefore 3,090 cells; the eight MSR configurations over 18 pairs and two
directions advance it to 3,378.  Outputs are permanently research-only and
cannot authorize a claim, activation, registry write, or order.
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

CSV_HEADER: tuple[str, ...] = (
    "timestamp",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "volume",
)
BASELINE_M1_BARS = 240
STOP_BUFFER_VOL = 0.10
FROZEN_STOP_FLOOR_BPS = 4.5
MAX_RISK_BPS = 25.0
REWARD_RISK = 1.0
P_STAR_MAX = 0.55
MIN_TARGET_COST_RATIO = 4.0
FROZEN_EXTRA_ROUND_TRIP_COST_BPS = 1.0
OUTCOME_HORIZON_M1_BARS = 12
FILL_DELAY_M1_BARS = 1
IMMUTABLE_PRIOR_TESTS = 3_090
MIN_TRADES_PER_CELL = 100
MIN_INDEPENDENT_ENTRY_DAYS = 60
MIN_OBSERVED_FULL_TARGET_RATE = 0.90
MIN_SIMULTANEOUS_WILSON_LOWER_BOUND = 0.90
SIMULTANEOUS_PAIR_DIRECTION_CELLS = len(FX_SYMBOLS) * 2
REQUIRED_PROXY_PROVENANCE_FIELDS: tuple[str, ...] = (
    "source_id",
    "as_of_utc",
    "method",
)


@dataclass(frozen=True, slots=True)
class MSRConfig:
    fair_lookback: int
    stretch_vol: float
    reversal_vol: float

    @property
    def config_id(self) -> str:
        stretch = int(round(self.stretch_vol * 100.0))
        reversal = int(round(self.reversal_vol * 100.0))
        return f"l{self.fair_lookback:02d}_s{stretch:03d}_r{reversal:02d}"


GRID: tuple[MSRConfig, ...] = tuple(
    MSRConfig(fair_lookback=lookback, stretch_vol=stretch, reversal_vol=reversal)
    for lookback in (12, 24)
    for stretch in (0.75, 1.25)
    for reversal in (0.10, 0.25)
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
    event_start_index: int
    volatility_bps: float
    spread_q25_bps: float
    fair_bid: float
    fair_ask: float


@dataclass(frozen=True, slots=True)
class MSRSignal:
    config_id: str
    symbol: str
    side: str
    signal_index: int
    signal_epoch: int
    entry_index: int
    entry_epoch: int
    entry_day: str
    entry_price: float
    stop_price: float
    target_price: float
    raw_risk_bps: float
    risk_bps: float
    quote_target_bps: float
    recorded_cost_bps: float
    p_star: float
    volatility_bps: float
    fair_value: float
    stretch_vol_units: float
    reversal_vol_units: float
    close_location: float
    spread_q25_bps: float
    proxy_budget_bps: float
    signal_spread_bps: float
    entry_spread_bps: float
    extra_round_trip_cost_bps: float


@dataclass(frozen=True, slots=True)
class MSRTrade:
    event_id: str
    symbol: str
    side: str
    config_id: str
    signal_epoch: int
    entry_epoch: int
    entry_day: str
    exit_epoch: int
    entry_price: float
    exit_price: float
    risk_bps: float
    pnl_bps: float
    pnl_r: float
    bars_held: int
    exit_reason: str
    full_target_win: bool
    positive_outcome: bool


@dataclass(slots=True)
class PreparedSeries:
    bars: list[QuoteBar]
    volatility_before_event: list[float | None]
    spread_q25_before_event: list[float | None]
    fair_bid_before_event: dict[int, list[float | None]]
    fair_ask_before_event: dict[int, list[float | None]]


def _spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0.0 or ask < bid:
        return 0.0
    return (ask - bid) / mid * 1e4


def _valid_positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0.0:
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
    """Load exact-schema monotonic bid/ask M1; gaps remain visible."""

    start_epoch = _parse_epoch(start)
    end_epoch = _parse_epoch(end)
    rows: list[QuoteBar] = []
    last_epoch: int | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if tuple(header or ()) != CSV_HEADER:
            raise ValueError(f"{path.name}: unexpected M1 header")
        for row_number, row in enumerate(reader, start=2):
            if len(row) != len(CSV_HEADER):
                raise ValueError(f"{path.name}:{row_number}: malformed M1 row")
            try:
                epoch = _parse_epoch(row[0])
                assert epoch is not None
                values = [float(value) for value in row[1:9]]
                volume = float(row[9])
                if not math.isfinite(volume) or volume < 0.0:
                    raise ValueError("invalid volume")
            except (AssertionError, TypeError, ValueError) as exc:
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


def _build_pre_event_context(
    bars: Sequence[QuoteBar],
) -> tuple[
    list[float | None],
    list[float | None],
    dict[int, list[float | None]],
    dict[int, list[float | None]],
]:
    """Build values keyed by t-1, excluding both stretched and reversal bars."""

    volatility: list[float | None] = [None] * len(bars)
    q25: list[float | None] = [None] * len(bars)
    lookbacks = sorted({config.fair_lookback for config in GRID})
    fair_bid = {lookback: [None] * len(bars) for lookback in lookbacks}
    fair_ask = {lookback: [None] * len(bars) for lookback in lookbacks}
    for run_start, run_end in _consecutive_runs(bars):
        first_event_start = run_start + BASELINE_M1_BARS + 1
        if first_event_start >= run_end:
            continue
        window_start = first_event_start - BASELINE_M1_BARS
        spread_ordered = sorted(
            bars[index].spread_close_bps
            for index in range(window_start, first_event_start)
        )
        tr_ordered = sorted(
            _true_range_bps(bars[index - 1], bars[index])
            for index in range(window_start, first_event_start)
        )
        for event_start in range(first_event_start, run_end):
            median_tr = statistics.median(tr_ordered)
            if math.isfinite(median_tr) and median_tr > 0.0:
                volatility[event_start] = median_tr
                q25[event_start] = _quantile_sorted(spread_ordered, 0.25)
                for lookback in lookbacks:
                    fair_bid[lookback][event_start] = statistics.median(
                        bars[index].bid_c
                        for index in range(event_start - lookback, event_start)
                    )
                    fair_ask[lookback][event_start] = statistics.median(
                        bars[index].ask_c
                        for index in range(event_start - lookback, event_start)
                    )
            next_event_start = event_start + 1
            if next_event_start >= run_end:
                continue
            expired_index = event_start - BASELINE_M1_BARS
            admitted_index = event_start
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
    return volatility, q25, fair_bid, fair_ask


def prepare_series(bars: Iterable[QuoteBar]) -> PreparedSeries:
    rows = list(bars)
    for index, bar in enumerate(rows):
        if not validate_quote_bar(bar):
            raise ValueError(f"invalid quote bar at index {index}")
        if index and bar.epoch <= rows[index - 1].epoch:
            raise ValueError("M1 timestamps must be strictly increasing")
    volatility, q25, fair_bid, fair_ask = _build_pre_event_context(rows)
    return PreparedSeries(rows, volatility, q25, fair_bid, fair_ask)


def baseline_context_at(
    prepared: PreparedSeries, *, signal_index: int, config: MSRConfig
) -> BaselineContext | None:
    event_start = signal_index - 1
    if event_start < 0 or signal_index >= len(prepared.bars):
        return None
    volatility = prepared.volatility_before_event[event_start]
    q25 = prepared.spread_q25_before_event[event_start]
    fair_bid = prepared.fair_bid_before_event[config.fair_lookback][event_start]
    fair_ask = prepared.fair_ask_before_event[config.fair_lookback][event_start]
    if any(value is None or value <= 0.0 for value in (volatility, q25, fair_bid, fair_ask)):
        return None
    stretch, reversal = prepared.bars[event_start : signal_index + 1]
    if reversal.epoch != stretch.epoch + 60:
        return None
    return BaselineContext(
        event_start_index=event_start,
        volatility_bps=float(volatility),
        spread_q25_bps=float(q25),
        fair_bid=float(fair_bid),
        fair_ask=float(fair_ask),
    )


def _entry_price(next_bar: QuoteBar, *, side: str) -> float:
    if side == "BUY":
        return next_bar.ask_o
    if side == "SELL":
        return next_bar.bid_o
    raise ValueError("side must be BUY or SELL")


def _floored_risk_bps(raw_risk_bps: float) -> float:
    return max(float(raw_risk_bps), FROZEN_STOP_FLOOR_BPS)


def evaluate_signal(
    *,
    prepared: PreparedSeries,
    signal_index: int,
    symbol: str,
    side: str,
    config: MSRConfig,
    proxy_spread_budget_bps: float | None,
) -> tuple[MSRSignal | None, str]:
    """Evaluate one event with only the pre-event context, closed t, and t+1 open."""

    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    budget = _valid_positive_number(proxy_spread_budget_bps)
    if budget is None:
        return None, "proxy_cost_unavailable"
    if signal_index < 0 or signal_index + FILL_DELAY_M1_BARS >= len(prepared.bars):
        return None, "exact_next_open_unavailable"
    signal_bar = prepared.bars[signal_index]
    next_bar = prepared.bars[signal_index + FILL_DELAY_M1_BARS]
    if next_bar.epoch != signal_bar.epoch + 60:
        return None, "exact_next_open_gap"
    context = baseline_context_at(prepared, signal_index=signal_index, config=config)
    if context is None:
        return None, "strict_pre_event_context_unavailable"

    signal_spread = signal_bar.spread_close_bps
    entry_spread = next_bar.spread_open_bps
    spread_cap = min(context.spread_q25_bps, budget)
    if signal_spread > spread_cap + 1e-12:
        return None, "signal_spread_above_q25_or_proxy"
    if entry_spread > spread_cap + 1e-12:
        return None, "entry_spread_above_q25_or_proxy"

    stretch_bar = prepared.bars[context.event_start_index]
    if side == "BUY":
        fair_value = context.fair_bid
        vol_price = context.volatility_bps / 1e4 * fair_value
        stretch_units = (fair_value - stretch_bar.bid_c) / vol_price
        reversal_units = (signal_bar.bid_c - stretch_bar.bid_c) / vol_price
        signal_range = signal_bar.bid_h - signal_bar.bid_l
        close_location = (
            (signal_bar.bid_c - signal_bar.bid_l) / signal_range
            if signal_range > 0.0
            else 0.0
        )
        reversal_bar_ok = signal_bar.bid_c > signal_bar.bid_o and close_location >= 0.50
    else:
        fair_value = context.fair_ask
        vol_price = context.volatility_bps / 1e4 * fair_value
        stretch_units = (stretch_bar.ask_c - fair_value) / vol_price
        reversal_units = (stretch_bar.ask_c - signal_bar.ask_c) / vol_price
        signal_range = signal_bar.ask_h - signal_bar.ask_l
        close_location = (
            (signal_bar.ask_h - signal_bar.ask_c) / signal_range
            if signal_range > 0.0
            else 0.0
        )
        reversal_bar_ok = signal_bar.ask_c < signal_bar.ask_o and close_location >= 0.50
    if not math.isfinite(stretch_units) or stretch_units < config.stretch_vol:
        return None, "stretch_too_small"
    if not math.isfinite(reversal_units) or reversal_units < config.reversal_vol:
        return None, "reversal_too_small"
    if not reversal_bar_ok:
        return None, "reversal_bar_missing"

    entry = _entry_price(next_bar, side=side)
    buffer_price = STOP_BUFFER_VOL * context.volatility_bps / 1e4 * fair_value
    if side == "BUY":
        raw_stop = min(stretch_bar.bid_l, signal_bar.bid_l) - buffer_price
        raw_risk_bps = (entry - raw_stop) / entry * 1e4
    else:
        raw_stop = max(stretch_bar.ask_h, signal_bar.ask_h) + buffer_price
        raw_risk_bps = (raw_stop - entry) / entry * 1e4
    if not math.isfinite(raw_risk_bps) or raw_risk_bps <= 0.0:
        return None, "degenerate_stop"
    risk_bps = _floored_risk_bps(raw_risk_bps)
    if risk_bps > MAX_RISK_BPS + 1e-12:
        return None, "risk_above_25bps"
    if side == "BUY":
        stop_price = entry * (1.0 - risk_bps / 1e4)
        target_price = entry * (1.0 + risk_bps * REWARD_RISK / 1e4)
        quote_target_bps = (target_price - entry) / entry * 1e4
        target_within_fair_value = target_price <= context.fair_bid + 1e-12
    else:
        stop_price = entry * (1.0 + risk_bps / 1e4)
        target_price = entry * (1.0 - risk_bps * REWARD_RISK / 1e4)
        quote_target_bps = (entry - target_price) / entry * 1e4
        target_within_fair_value = target_price >= context.fair_ask - 1e-12
    if not target_within_fair_value:
        return None, "target_beyond_frozen_fair_value"

    net_target_bps = quote_target_bps - FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    net_stop_loss_bps = risk_bps + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    if net_target_bps <= 0.0:
        return None, "bracket_cost_dead"
    p_star = net_stop_loss_bps / (net_stop_loss_bps + net_target_bps)
    if not math.isfinite(p_star) or p_star > P_STAR_MAX + 1e-12:
        return None, "bracket_cost_dead"
    recorded_cost_bps = entry_spread + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    if quote_target_bps + 1e-12 < MIN_TARGET_COST_RATIO * recorded_cost_bps:
        return None, "target_too_small_vs_cost"

    return (
        MSRSignal(
            config_id=config.config_id,
            symbol=str(symbol).upper(),
            side=side,
            signal_index=signal_index,
            signal_epoch=signal_bar.epoch,
            entry_index=signal_index + FILL_DELAY_M1_BARS,
            entry_epoch=next_bar.epoch,
            entry_day=_utc_day(next_bar.epoch),
            entry_price=entry,
            stop_price=stop_price,
            target_price=target_price,
            raw_risk_bps=raw_risk_bps,
            risk_bps=risk_bps,
            quote_target_bps=quote_target_bps,
            recorded_cost_bps=recorded_cost_bps,
            p_star=p_star,
            volatility_bps=context.volatility_bps,
            fair_value=fair_value,
            stretch_vol_units=stretch_units,
            reversal_vol_units=reversal_units,
            close_location=close_location,
            spread_q25_bps=context.spread_q25_bps,
            proxy_budget_bps=budget,
            signal_spread_bps=signal_spread,
            entry_spread_bps=entry_spread,
            extra_round_trip_cost_bps=FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
        ),
        "",
    )


def outcome_horizon_is_complete(bars: Sequence[QuoteBar], *, entry_index: int) -> bool:
    horizon_end = entry_index + OUTCOME_HORIZON_M1_BARS
    if entry_index < 0 or horizon_end > len(bars):
        return False
    entry_epoch = bars[entry_index].epoch
    return all(
        bars[index].epoch == entry_epoch + offset * 60
        for offset, index in enumerate(range(entry_index, horizon_end))
    )


def _event_id(signal: MSRSignal) -> str:
    raw = (
        f"{signal.config_id}|{signal.symbol}|{signal.side}|"
        f"{signal.signal_epoch}|{signal.entry_epoch}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _trade_result(
    signal: MSRSignal,
    *,
    exit_bar: QuoteBar,
    exit_price: float,
    bars_held: int,
    reason: str,
) -> MSRTrade:
    if signal.side == "BUY":
        gross_bps = (exit_price - signal.entry_price) / signal.entry_price * 1e4
    else:
        gross_bps = (signal.entry_price - exit_price) / signal.entry_price * 1e4
    pnl_bps = gross_bps - FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    full_target = reason in {"tp", "tp_gap_open"} and pnl_bps > 0.0
    return MSRTrade(
        event_id=_event_id(signal),
        symbol=signal.symbol,
        side=signal.side,
        config_id=signal.config_id,
        signal_epoch=signal.signal_epoch,
        entry_epoch=signal.entry_epoch,
        entry_day=signal.entry_day,
        exit_epoch=exit_bar.epoch,
        entry_price=signal.entry_price,
        exit_price=exit_price,
        risk_bps=signal.risk_bps,
        pnl_bps=pnl_bps,
        pnl_r=pnl_bps / signal.risk_bps,
        bars_held=bars_held,
        exit_reason=reason,
        full_target_win=full_target,
        positive_outcome=pnl_bps > 0.0,
    )


def simulate_trade(bars: Sequence[QuoteBar], *, signal: MSRSignal) -> MSRTrade | None:
    """Score only a complete horizon, then apply adverse gap/SL-first ordering."""

    if not outcome_horizon_is_complete(bars, entry_index=signal.entry_index):
        return None
    for offset in range(OUTCOME_HORIZON_M1_BARS):
        bar = bars[signal.entry_index + offset]
        buy = signal.side == "BUY"
        exit_open = bar.bid_o if buy else bar.ask_o
        stop_gap = exit_open <= signal.stop_price if buy else exit_open >= signal.stop_price
        if stop_gap:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=exit_open,
                bars_held=offset + 1,
                reason="sl_gap_open",
            )
        target_gap = (
            exit_open >= signal.target_price if buy else exit_open <= signal.target_price
        )
        if target_gap:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=signal.target_price,
                bars_held=offset + 1,
                reason="tp_gap_open",
            )
        exit_low = bar.bid_l if buy else bar.ask_l
        exit_high = bar.bid_h if buy else bar.ask_h
        stop_hit = exit_low <= signal.stop_price if buy else exit_high >= signal.stop_price
        target_hit = exit_high >= signal.target_price if buy else exit_low <= signal.target_price
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
        if offset == OUTCOME_HORIZON_M1_BARS - 1:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=bar.bid_c if buy else bar.ask_c,
                bars_held=OUTCOME_HORIZON_M1_BARS,
                reason="time_stop",
            )
    raise RuntimeError("complete outcome horizon did not resolve")


def _utc_day(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def _reservation_row(
    signal: MSRSignal, *, status: str, trade: MSRTrade | None
) -> dict[str, Any]:
    gate_pnl_r = (
        trade.pnl_r
        if trade is not None
        else -(signal.risk_bps + FROZEN_EXTRA_ROUND_TRIP_COST_BPS) / signal.risk_bps
    )
    row: dict[str, Any] = {
        "event_id": _event_id(signal),
        **asdict(signal),
        "reservation_status": status,
        "outcome_reason": trade.exit_reason if trade is not None else "incomplete_outcome_horizon",
        "exit_epoch": trade.exit_epoch if trade is not None else None,
        "exit_price": trade.exit_price if trade is not None else None,
        "bars_held": trade.bars_held if trade is not None else None,
        "pnl_bps": trade.pnl_bps if trade is not None else None,
        "pnl_r": trade.pnl_r if trade is not None else None,
        "gate_pnl_r": gate_pnl_r,
        "gate_treatment": (
            "observed_trade"
            if trade is not None
            else "unresolved_as_adverse_stop_for_discovery_gate"
        ),
        "full_target_win": trade.full_target_win if trade is not None else False,
        "positive_outcome": trade.positive_outcome if trade is not None else False,
    }
    return row


def screen_cell(
    *,
    prepared: PreparedSeries,
    symbol: str,
    side: str,
    config: MSRConfig,
    proxy_spread_budget_bps: float | None,
) -> dict[str, Any]:
    trades: list[MSRTrade] = []
    event_ledger: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    reserved_entry_days: set[str] = set()
    raw_events = 0
    for index in range(len(prepared.bars)):
        signal, reason = evaluate_signal(
            prepared=prepared,
            signal_index=index,
            symbol=symbol,
            side=side,
            config=config,
            proxy_spread_budget_bps=proxy_spread_budget_bps,
        )
        if signal is None:
            reasons[reason] += 1
            continue
        raw_events += 1
        if signal.entry_day in reserved_entry_days:
            reasons["entry_day_already_reserved"] += 1
            continue

        # Reservation deliberately precedes horizon inspection.  A gap or
        # end-of-file can never cause a later same-day event to substitute.
        reserved_entry_days.add(signal.entry_day)
        if not outcome_horizon_is_complete(
            prepared.bars, entry_index=signal.entry_index
        ):
            reasons["incomplete_outcome_horizon"] += 1
            event_ledger.append(_reservation_row(signal, status="unresolved", trade=None))
            continue
        trade = simulate_trade(prepared.bars, signal=signal)
        if trade is None:
            raise RuntimeError("complete horizon unexpectedly failed to score")
        trades.append(trade)
        event_ledger.append(_reservation_row(signal, status="scored", trade=trade))

    full_target_wins = sum(trade.full_target_win for trade in trades)
    positive_outcomes = sum(trade.positive_outcome for trade in trades)
    positive_time_stops = sum(
        trade.exit_reason == "time_stop" and trade.positive_outcome for trade in trades
    )
    pnl_rs = [trade.pnl_r for trade in trades]
    reservations = len(event_ledger)
    unresolved = sum(row["reservation_status"] == "unresolved" for row in event_ledger)
    gate_pnl_rs = [float(row["gate_pnl_r"]) for row in event_ledger]
    return {
        "config_id": config.config_id,
        "fair_lookback": config.fair_lookback,
        "stretch_vol": config.stretch_vol,
        "reversal_vol": config.reversal_vol,
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "raw_events": raw_events,
        "entry_day_reservations": reservations,
        "unresolved_reservations": unresolved,
        "scored_trades": len(trades),
        "full_target_wins": full_target_wins,
        "full_target_trade_win_rate": (
            full_target_wins / len(trades) if trades else 0.0
        ),
        "full_target_reservation_rate": (
            full_target_wins / reservations if reservations else 0.0
        ),
        "positive_outcomes": positive_outcomes,
        "positive_outcome_rate": positive_outcomes / len(trades) if trades else 0.0,
        "positive_time_stops": positive_time_stops,
        "total_r": sum(pnl_rs),
        "mean_r": statistics.fmean(pnl_rs) if pnl_rs else 0.0,
        "gate_total_r": sum(gate_pnl_rs),
        "gate_mean_r": statistics.fmean(gate_pnl_rs) if gate_pnl_rs else 0.0,
        "exit_mix": dict(Counter(trade.exit_reason for trade in trades)),
        "reasons": dict(sorted(reasons.items())),
        "event_ledger": event_ledger,
        "trade_ledger": [asdict(trade) for trade in trades],
        "one_trade_per_cell_entry_day": True,
        "outcome_horizon_bars": OUTCOME_HORIZON_M1_BARS,
        "reward_risk": REWARD_RISK,
        "stop_floor_bps": FROZEN_STOP_FLOOR_BPS,
        "max_risk_bps": MAX_RISK_BPS,
        "p_star_max": P_STAR_MAX,
        "min_target_cost_ratio": MIN_TARGET_COST_RATIO,
        "extra_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
    }


def trial_accounting(*, n_symbols: int = len(FX_SYMBOLS)) -> dict[str, int]:
    symbols = max(0, int(n_symbols))
    current = len(GRID) * symbols * 2
    return {
        "grid_configurations": len(GRID),
        "directions": 2,
        "symbols": symbols,
        "current_attempted_cells": current,
        "prior_attempted_cells": IMMUTABLE_PRIOR_TESTS,
        "cumulative_attempted_cells": IMMUTABLE_PRIOR_TESTS + current,
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


def _one_sided_wilson_lower_bound(
    wins: int,
    observations: int,
    *,
    family_alpha: float = 0.05,
    simultaneous_cells: int = SIMULTANEOUS_PAIR_DIRECTION_CELLS,
) -> float:
    n = max(0, int(observations))
    x = min(n, max(0, int(wins)))
    if n == 0:
        return 0.0
    tests = max(1, int(simultaneous_cells))
    z = statistics.NormalDist().inv_cdf(1.0 - float(family_alpha) / tests)
    proportion = x / n
    z_squared = z * z
    denominator = 1.0 + z_squared / n
    center = proportion + z_squared / (2.0 * n)
    radius = z * math.sqrt(
        proportion * (1.0 - proportion) / n + z_squared / (4.0 * n * n)
    )
    return max(0.0, (center - radius) / denominator)


def _finite_one_sample_t(values: Sequence[float]) -> float:
    rows = [float(value) for value in values]
    if len(rows) < 2 or any(not math.isfinite(value) for value in rows):
        return 0.0
    mean = statistics.fmean(rows)
    deviation = statistics.stdev(rows)
    if deviation <= 1e-15:
        if mean > 0.0:
            return 1e12
        if mean < 0.0:
            return -1e12
        return 0.0
    statistic = mean / (deviation / math.sqrt(len(rows)))
    return max(-1e12, min(1e12, statistic))


def _passing_global_configurations(
    cells: Sequence[Mapping[str, Any]], *, symbols: Sequence[str]
) -> list[str]:
    expected = {
        (str(symbol).strip().upper(), side)
        for symbol in symbols
        for side in ("BUY", "SELL")
    }
    passing: list[str] = []
    for config in GRID:
        rows = [cell for cell in cells if cell.get("config_id") == config.config_id]
        keys = {
            (str(cell.get("symbol") or "").upper(), str(cell.get("side") or "").upper())
            for cell in rows
        }
        if (
            len(rows) == len(expected)
            and keys == expected
            and all(cell.get("passes_discovery_cell_gate") is True for cell in rows)
        ):
            passing.append(config.config_id)
    return passing


def _apply_discovery_gate(
    *,
    cells: list[dict[str, Any]],
    event_ledger: Sequence[Mapping[str, Any]],
    trade_ledger: Sequence[Mapping[str, Any]],
    symbols: Sequence[str],
    corrected_t_threshold: float,
) -> dict[str, Any]:
    events_by_cell: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for event in event_ledger:
        key = (
            str(event.get("config_id") or ""),
            str(event.get("symbol") or "").upper(),
            str(event.get("side") or "").upper(),
        )
        events_by_cell.setdefault(key, []).append(event)
    scored_event_ids = Counter(
        str(event.get("event_id") or "")
        for event in event_ledger
        if event.get("reservation_status") == "scored"
    )
    trade_event_ids = Counter(
        str(trade.get("event_id") or "") for trade in trade_ledger
    )
    global_trade_identity_matches = scored_event_ids == trade_event_ids

    for cell in cells:
        key = (cell["config_id"], cell["symbol"], cell["side"])
        events = events_by_cell.get(key, [])
        days = [str(event.get("entry_day") or "") for event in events]
        gate_returns = [float(event.get("gate_pnl_r")) for event in events]
        wins = sum(event.get("full_target_win") is True for event in events)
        ledger_consistent = bool(
            len(events) == int(cell["entry_day_reservations"])
            and wins == int(cell["full_target_wins"])
            and len(set(days)) == len(days)
            and all(day for day in days)
            and all(math.isfinite(value) for value in gate_returns)
            and global_trade_identity_matches
        )
        independent_days = len(set(days))
        reservation_rate = wins / len(events) if events else 0.0
        wilson_lower = _one_sided_wilson_lower_bound(wins, len(events))
        day_t = _finite_one_sample_t(gate_returns)
        gate_mean = statistics.fmean(gate_returns) if gate_returns else 0.0
        passes = bool(
            ledger_consistent
            and cell.get("source_ready") is True
            and cell.get("proxy_budget_ready") is True
            and int(cell["scored_trades"]) >= MIN_TRADES_PER_CELL
            and independent_days >= MIN_INDEPENDENT_ENTRY_DAYS
            and reservation_rate >= MIN_OBSERVED_FULL_TARGET_RATE
            and wilson_lower >= MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
            and gate_mean > 0.0
            and day_t >= corrected_t_threshold
        )
        cell.update(
            {
                "independent_entry_days": independent_days,
                "gate_full_target_reservation_rate": reservation_rate,
                "simultaneous_wilson_lower_bound": wilson_lower,
                "day_clustered_gate_t": day_t,
                "gate_ledger_consistent": ledger_consistent,
                "passes_discovery_cell_gate": passes,
            }
        )

    requested = tuple(str(symbol).strip().upper() for symbol in symbols)
    full_universe = requested == FX_SYMBOLS
    passing_configs = (
        _passing_global_configurations(cells, symbols=requested)
        if full_universe
        else []
    )
    return {
        "passed": bool(passing_configs),
        "passing_config_ids": passing_configs,
        "unchanged_single_configuration_required": True,
        "full_canonical_universe_required": True,
        "full_canonical_universe_present": full_universe,
        "pair_direction_cells_per_configuration": SIMULTANEOUS_PAIR_DIRECTION_CELLS,
        "minimum_scored_trades_per_cell": MIN_TRADES_PER_CELL,
        "minimum_independent_entry_days_per_cell": MIN_INDEPENDENT_ENTRY_DAYS,
        "minimum_observed_full_target_reservation_rate": (
            MIN_OBSERVED_FULL_TARGET_RATE
        ),
        "minimum_simultaneous_wilson_lower_bound": (
            MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
        ),
        "wilson_method": "one-sided Bonferroni over 36 fixed pair-direction cells",
        "minimum_gate_mean_r": 0.0,
        "minimum_day_clustered_t": corrected_t_threshold,
        "unresolved_treatment": "non-win and adverse net stop-R",
        "scored_reservation_trade_ids_match": global_trade_identity_matches,
        "success_claim_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def _has_scorable_run(bars: Sequence[QuoteBar]) -> bool:
    minimum = BASELINE_M1_BARS + 1 + 2 + OUTCOME_HORIZON_M1_BARS
    return any(run_end - run_start >= minimum for run_start, run_end in _consecutive_runs(bars))


def _valid_proxy_provenance(provenance: Mapping[str, Any] | None) -> bool:
    if provenance is None or provenance.get("frozen") is not True:
        return False
    for field in REQUIRED_PROXY_PROVENANCE_FIELDS:
        value = provenance.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    try:
        parsed_as_of = dt.datetime.fromisoformat(
            str(provenance["as_of_utc"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False
    return (
        parsed_as_of.tzinfo is not None
        and parsed_as_of.utcoffset() == dt.timedelta(0)
    )


def screen_universe(
    *,
    bars_by_symbol: Mapping[str, Sequence[QuoteBar]],
    proxy_spread_budgets_bps: Mapping[str, float],
    proxy_provenance: Mapping[str, Any] | None,
    symbols: Sequence[str] = FX_SYMBOLS,
    source_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    requested = tuple(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    if len(set(requested)) != len(requested):
        raise ValueError("symbols must be unique")
    normalized_bars = {
        str(symbol).strip().upper(): rows for symbol, rows in bars_by_symbol.items()
    }
    errors = {str(key).upper(): str(value) for key, value in (source_errors or {}).items()}
    provenance_ready = _valid_proxy_provenance(proxy_provenance)
    cells: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    source_failure_symbols: list[str] = []
    missing_proxy_budget_symbols: list[str] = []

    for symbol in requested:
        try:
            prepared = prepare_series(normalized_bars.get(symbol, ()))
        except ValueError as exc:
            prepared = prepare_series([])
            errors[symbol] = str(exc)
        if symbol in errors:
            # An explicitly failed source is never screened even if a caller
            # accidentally supplied rows alongside the failure metadata.
            prepared = prepare_series([])
        else:
            if not prepared.bars:
                errors[symbol] = "empty_after_date_filter"
            elif not _has_scorable_run(prepared.bars):
                errors[symbol] = "no_complete_255_bar_m1_run"
        source_ready = symbol not in errors
        if not source_ready:
            source_failure_symbols.append(symbol)
            # Unready sources emit neither scored nor unresolved evidence.
            prepared = prepare_series([])

        budget = _valid_positive_number(proxy_spread_budgets_bps.get(symbol))
        budget_ready = provenance_ready and budget is not None
        if not budget_ready:
            missing_proxy_budget_symbols.append(symbol)
        effective_budget = budget if budget_ready else None
        cost_mode = (
            "pair_specific_frozen_proxy_discovery_stress"
            if budget_ready
            else "cost_unavailable"
        )
        for config in GRID:
            for side in ("BUY", "SELL"):
                cell = screen_cell(
                    prepared=prepared,
                    symbol=symbol,
                    side=side,
                    config=config,
                    proxy_spread_budget_bps=effective_budget,
                )
                cell.update(
                    {
                        "proxy_budget_bps": budget,
                        "cost_mode": cost_mode,
                        "proxy_contract_ready": provenance_ready,
                        "proxy_budget_ready": budget_ready,
                        "source_ready": source_ready,
                        "source_error": errors.get(symbol),
                        "economic_claim_ready": False,
                        "economics_claim_ready": False,
                    }
                )
                cell_events = cell.pop("event_ledger")
                cell_trades = cell.pop("trade_ledger")
                all_events.extend(cell_events)
                all_trades.extend(cell_trades)
                cell["event_ids"] = [row["event_id"] for row in cell_events]
                cell["trade_event_ids"] = [row["event_id"] for row in cell_trades]
                cells.append(cell)

    accounting = trial_accounting(n_symbols=len(requested))
    cumulative = accounting["cumulative_attempted_cells"]
    corrected_t_threshold = search_corrected_threshold(cumulative)
    discovery_gate = _apply_discovery_gate(
        cells=cells,
        event_ledger=all_events,
        trade_ledger=all_trades,
        symbols=requested,
        corrected_t_threshold=corrected_t_threshold,
    )
    return {
        "schema_version": "fxstack.scalp.median_stretch_reversal_screen.v2",
        "family": "median_stretch_reversal",
        "research_only": True,
        "future_data_access_for_signal": "forbidden_except_exact_t_plus_1_open_fill",
        "success_claim_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "order_authorized": False,
        "economic_passed": False,
        "economic_claim_ready": False,
        "economics_claim_ready": False,
        "economic_claim_scope": "none_proxy_discovery_only",
        "symbols": list(requested),
        "grid": [asdict(config) | {"config_id": config.config_id} for config in GRID],
        "fixed_contract": {
            "baseline": "median 240 M1 true ranges ending t-2; 241st bar supplies first previous close",
            "fair_value": "quote-side median close ending t-2",
            "spread": "Q25 of same exact 240 pre-event close spreads",
            "fill": "exact t+1 ask open BUY / bid open SELL",
            "outcome_horizon_m1_bars": OUTCOME_HORIZON_M1_BARS,
            "reserve_before_horizon_check": True,
            "one_trade_per_cell_entry_day": True,
            "stop_buffer_vol": STOP_BUFFER_VOL,
            "stop_floor_bps": FROZEN_STOP_FLOOR_BPS,
            "max_risk_bps": MAX_RISK_BPS,
            "reward_risk": REWARD_RISK,
            "target_must_not_cross_frozen_fair_value": True,
            "p_star_max": P_STAR_MAX,
            "p_star": "net quote stop loss divided by net quote stop loss plus net quote target payoff",
            "min_target_cost_ratio": MIN_TARGET_COST_RATIO,
            "recorded_cost": "actual t+1 opening spread plus frozen one-basis-point pad",
            "extra_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
            "ambiguous_bar": "stop_loss_first",
            "exit_quote": "bid_for_BUY_ask_for_SELL",
            "full_target_win": "positive_net_tp_or_tp_gap_open_only",
            "unresolved_gate_treatment": "non-win and adverse net stop-R; never dropped or replaced",
        },
        "cost_readiness": {
            "proxy_contract_ready": provenance_ready,
            "proxy_provenance": dict(proxy_provenance or {}),
            "missing_proxy_budget_symbols": sorted(set(missing_proxy_budget_symbols)),
            "source_failure_symbols": sorted(set(source_failure_symbols)),
            "proxy_budgets_bps": {
                symbol: _valid_positive_number(proxy_spread_budgets_bps.get(symbol))
                for symbol in requested
            },
            "observed_source_bid_ask_used": True,
            "extra_adverse_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
        },
        "search_accounting": accounting
        | {
            "family_alpha": 0.05,
            "two_sided_sidak_abs_t_threshold": corrected_t_threshold,
        },
        "discovery_gate": discovery_gate,
        "event_ledger": all_events,
        "trade_ledger": all_trades,
        "cells": cells,
    }


def _load_proxy_contract(path: Path) -> tuple[dict[str, float], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: expected an object")
    provenance = payload.get("provenance")
    budgets_payload = payload.get("budgets_bps")
    if not isinstance(provenance, dict) or not _valid_proxy_provenance(provenance):
        raise ValueError(f"{path.name}: missing frozen proxy provenance")
    if not isinstance(budgets_payload, dict):
        raise ValueError(f"{path.name}: missing budgets_bps object")
    budgets: dict[str, float] = {}
    for raw_symbol, raw_value in budgets_payload.items():
        symbol = str(raw_symbol).strip().upper()
        value = _valid_positive_number(raw_value)
        if symbol and value is not None:
            budgets[symbol] = value
    return budgets, dict(provenance)


def _sha256(path: Path) -> str:
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
    parser.add_argument("--proxy-budgets-json", required=True)
    parser.add_argument("--event-ledger-out", required=True)
    parser.add_argument("--trade-ledger-out", required=True)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args(argv)

    event_output = Path(args.event_ledger_out).resolve()
    trade_output = Path(args.trade_ledger_out).resolve()
    output = Path(args.json_out).resolve()
    outputs = {
        "--event-ledger-out": event_output,
        "--trade-ledger-out": trade_output,
        "--json-out": output,
    }
    if len(set(outputs.values())) != len(outputs):
        parser.error("ledger and cell outputs must use three distinct paths")
    for option, path in outputs.items():
        if path.exists():
            parser.error(f"{option} already exists; refusing to overwrite")

    csv_root = Path(args.csv_root)
    proxy_path = Path(args.proxy_budgets_json)
    proxy_budgets, proxy_provenance = _load_proxy_contract(proxy_path)
    bars_by_symbol: dict[str, list[QuoteBar]] = {}
    source_errors: dict[str, str] = {}
    source_sha256: dict[str, str] = {}
    for symbol in FX_SYMBOLS:
        path = csv_root / f"{symbol}_M1.csv"
        if not path.exists():
            bars_by_symbol[symbol] = []
            source_errors[symbol] = f"missing:{path.name}"
            continue
        try:
            source_sha256[symbol] = _sha256(path)
            bars_by_symbol[symbol] = load_m1_csv(path, start=args.start, end=args.end)
            if not bars_by_symbol[symbol]:
                source_errors[symbol] = f"empty_after_date_filter:{path.name}"
        except (OSError, ValueError) as exc:
            bars_by_symbol[symbol] = []
            source_errors[symbol] = (
                f"invalid_or_unreadable:{path.name}:{type(exc).__name__}"
            )

    result = screen_universe(
        bars_by_symbol=bars_by_symbol,
        proxy_spread_budgets_bps=proxy_budgets,
        proxy_provenance=proxy_provenance,
        source_errors=source_errors,
    )
    result["input_metadata"] = {
        "csv_root_recorded": False,
        "start": args.start,
        "end_exclusive": args.end,
        "proxy_contract_sha256": _sha256(proxy_path),
        "m1_csv_sha256_by_symbol": source_sha256,
    }
    event_payload = {
        "schema_version": "fxstack.scalp.median_stretch_reversal_event_ledger.v2",
        "family": result["family"],
        "events": result.pop("event_ledger"),
    }
    trade_payload = {
        "schema_version": "fxstack.scalp.median_stretch_reversal_trade_ledger.v2",
        "family": result["family"],
        "trades": result.pop("trade_ledger"),
    }
    for path, payload in (
        (event_output, event_payload),
        (trade_output, trade_payload),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=1, sort_keys=True, allow_nan=False))
    result["event_ledger_evidence"] = {
        "path_recorded": False,
        "reservations": len(event_payload["events"]),
        "file_sha256": _sha256(event_output),
    }
    result["trade_ledger_evidence"] = {
        "path_recorded": False,
        "trades": len(trade_payload["trades"]),
        "file_sha256": _sha256(trade_output),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, indent=1, sort_keys=True, allow_nan=False))
    accounting = result["search_accounting"]
    print(
        f"MSR: {len(result['cells'])} cells; "
        f"{accounting['current_attempted_cells']} current / "
        f"{accounting['cumulative_attempted_cells']} cumulative; "
        f"reservations={len(event_payload['events'])}; "
        f"trades={len(trade_payload['trades'])}; research_only=True"
    )
    return (
        0
        if not result["cost_readiness"]["source_failure_symbols"]
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
