# AGENT: ROLE: Research-only dense range-displacement-trigger M1 screen.
# AGENT: ENTRYPOINT: `screen_universe`; CLI `python -m fxstack.scalp.screen_range_displacement_trigger`.
# AGENT: PRIMARY INPUTS: immutable bid/ask M1 CSV snapshots and one frozen proxy-cost contract.
# AGENT: PRIMARY OUTPUTS: every fixed cell plus complete reservation and scored-trade ledgers.
# AGENT ISOLATION: advisory discovery evidence only; never authorizes success or activation.
"""Causal M1 screen for the symmetric Range-Displacement Trigger (RDT).

RDT is a single-closed-bar continuation hypothesis.  Before signal bar ``t``
opens, a consecutive 240-bar baseline freezes median true range and the lower
quartile of observed spread; the last ``L`` baseline bars freeze a quote-side
range boundary.  A BUY requires the bid bar at ``t`` to open at or below the
prior bid high, close strictly above it, have a bullish body of at least ``d``
frozen median true ranges, and close in the top ``q`` fraction of its range.
A SELL is the exact ask-side mirror.  The fixed 2 x 2 x 2 grid is
``L in {12, 24}``, ``d in {0.50, 0.75}``, and ``q in {0.70, 0.85}``.

Execution is exactly at the observed ``t+1`` opening quote.  Each cell reserves
its first eligible UTC entry day before outcome availability is examined, so
a missing future minute becomes an unresolved reservation and can never be
replaced using hindsight.  Scoring requires all twelve consecutive outcome
bars before any exit is read.  Stop gaps receive the adverse observed open,
target gaps are capped at the target, and a stop wins any ambiguous bar.

Observed source bid/ask quotes are retained.  A frozen pair proxy is both a
spread cap and a conservative cost stress.  Any excess of the greatest signal,
entry, or proxy spread over the observed entry spread is debited from p* and
every scored outcome, together with a frozen adverse one-bp round-trip pad.
Risk is fixed at 1R:1R, floored at 4.5 bps, capped at 25 bps, requires target
distance of at least four recorded costs, and requires ``p* <= 0.55``.  The
immutable prior ledger is 3,666 cells; these eight configs
over 18 pairs and two directions add 288, for 3,954 cumulative cells and a
dependence-robust two-sided Bonferroni Student-t absolute threshold of
4.597560439065422 at the fixed minimum 99 degrees of freedom.

This module is a permanent research artifact.  It cannot claim success,
activate a model, write a registry, connect to a runtime, or submit an order.
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
IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 3_666
IMMUTABLE_CURRENT_ATTEMPTED_CELLS = 288
IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 3_954
TWO_SIDED_BONFERRONI_FAMILY_ALPHA = 0.05
BONFERRONI_STUDENT_T_MIN_DF = 99
TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD = 4.597560439065422
PROXY_CONTRACT_SCHEMA_VERSION = "fxstack.scalp.pair_proxy_spread_contract.v1"
MIN_TRADES_PER_CELL = 100
MIN_INDEPENDENT_ENTRY_DAYS = 100
MIN_OBSERVED_FULL_TARGET_RATE = 0.90
MIN_SIMULTANEOUS_WILSON_LOWER_BOUND = 0.90
TEMPORAL_THIRDS = 3
MIN_RESERVATIONS_PER_TEMPORAL_THIRD = 30
TEMPORAL_THIRD_WIN_RATE_NUMERATOR = 9
TEMPORAL_THIRD_WIN_RATE_DENOMINATOR = 10
SIMULTANEOUS_PAIR_DIRECTION_CELLS = len(FX_SYMBOLS) * 2
SIMULTANEOUS_WILSON_FAMILY_CELLS = IMMUTABLE_CURRENT_ATTEMPTED_CELLS
MIN_ALL_WIN_RESERVATIONS_FOR_WILSON = 116
REQUIRED_PROXY_PROVENANCE_FIELDS: tuple[str, ...] = (
    "venue_id",
    "source_id",
    "as_of_utc",
    "source_cutoff_utc",
    "method",
    "units",
    "source_snapshot_sha256",
)


@dataclass(frozen=True, slots=True)
class RDTConfig:
    channel_lookback: int
    displacement_vol: float
    close_location_floor: float

    @property
    def config_id(self) -> str:
        displacement = int(round(self.displacement_vol * 100.0))
        close_location = int(round(self.close_location_floor * 100.0))
        return f"l{self.channel_lookback:02d}_d{displacement:02d}_q{close_location:02d}"


GRID: tuple[RDTConfig, ...] = tuple(
    RDTConfig(
        channel_lookback=lookback,
        displacement_vol=displacement,
        close_location_floor=close_location,
    )
    for lookback in (12, 24)
    for displacement in (0.50, 0.75)
    for close_location in (0.70, 0.85)
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
    volume: float

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
    volatility_bps: float
    spread_q25_bps: float
    prior_bid_high: float
    prior_ask_low: float


@dataclass(frozen=True, slots=True)
class RDTClosedSignal:
    config_id: str
    symbol: str
    side: str
    signal_index: int
    signal_epoch: int
    expected_entry_index: int
    expected_entry_epoch: int
    entry_day: str
    volatility_bps: float
    channel_level: float
    displacement_bps: float
    displacement_vol_units: float
    close_location: float
    structural_stop: float
    spread_q25_bps: float
    proxy_budget_bps: float
    signal_spread_bps: float


@dataclass(frozen=True, slots=True)
class RDTSignal:
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
    incremental_spread_stress_bps: float
    execution_cost_debit_bps: float
    p_star: float
    volatility_bps: float
    channel_level: float
    structural_stop: float
    displacement_bps: float
    displacement_vol_units: float
    close_location: float
    spread_q25_bps: float
    proxy_budget_bps: float
    signal_spread_bps: float
    entry_spread_bps: float
    extra_round_trip_cost_bps: float


@dataclass(frozen=True, slots=True)
class RDTTrade:
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


RDT_CONFIG_BY_ID: dict[str, RDTConfig] = {config.config_id: config for config in GRID}
RDT_SIGNAL_FIELD_NAMES = frozenset(RDTSignal.__dataclass_fields__)
RDT_TRADE_FIELD_NAMES = frozenset(RDTTrade.__dataclass_fields__)
RDT_RESERVATION_OUTCOME_FIELD_NAMES = frozenset(
    {
        "event_id",
        "reservation_status",
        "outcome_reason",
        "exit_epoch",
        "exit_price",
        "bars_held",
        "pnl_bps",
        "pnl_r",
        "gate_pnl_r",
        "gate_treatment",
        "full_target_win",
        "positive_outcome",
    }
)
RDT_RESERVATION_FIELD_NAMES = (
    RDT_SIGNAL_FIELD_NAMES | RDT_RESERVATION_OUTCOME_FIELD_NAMES
)
RDT_MISSING_FILL_RESERVATION_FIELD_NAMES = (
    RDT_RESERVATION_FIELD_NAMES | {"gate_risk_basis_bps"}
)
RDT_CELL_PRE_GATE_FIELD_NAMES = frozenset(
    {
        "config_id",
        "channel_lookback",
        "displacement_vol",
        "close_location_floor",
        "symbol",
        "side",
        "closed_signal_events",
        "eligible_events",
        "entry_day_reservations",
        "unresolved_reservations",
        "scored_trades",
        "full_target_wins",
        "full_target_trade_win_rate",
        "full_target_reservation_rate",
        "positive_outcomes",
        "positive_outcome_rate",
        "total_r",
        "mean_r",
        "gate_total_r",
        "gate_mean_r",
        "exit_mix",
        "reasons",
        "one_trade_per_cell_entry_day",
        "outcome_horizon_bars",
        "reward_risk",
        "stop_floor_bps",
        "max_risk_bps",
        "p_star_max",
        "min_target_cost_ratio",
        "extra_round_trip_cost_bps",
        "proxy_budget_bps",
        "cost_mode",
        "proxy_contract_ready",
        "proxy_budget_ready",
        "source_ready",
        "source_error",
        "economic_claim_ready",
        "economics_claim_ready",
        "reservation_event_ids",
        "trade_event_ids",
    }
)
RDT_CELL_GATE_FIELD_NAMES = frozenset(
    {
        "independent_entry_days",
        "gate_full_target_reservation_rate",
        "simultaneous_wilson_lower_bound",
        "reserved_utc_day_one_sample_t",
        "temporal_thirds_reservation_counts",
        "temporal_thirds_full_target_wins",
        "temporal_thirds_full_target_rates",
        "temporal_thirds_gate_total_rs",
        "temporal_thirds_gate_mean_rs",
        "gate_temporal_thirds_stable",
        "gate_ledger_consistent",
        "gate_actual_scored_reservations",
        "gate_actual_unresolved_reservations",
        "gate_scored_trade_ids_match",
        "gate_row_semantics_consistent",
        "gate_temporal_scope_consistent",
        "gate_proxy_contract_binding_consistent",
        "gate_source_replay_consistent",
        "gate_cell_contract_consistent",
        "passes_discovery_cell_gate",
    }
)


@dataclass(slots=True)
class PreparedSeries:
    bars: list[QuoteBar]
    volatility_before_signal: list[float | None]
    spread_q25_before_signal: list[float | None]
    prior_bid_high: dict[int, list[float | None]]
    prior_ask_low: dict[int, list[float | None]]


def _spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0.0 or ask < bid:
        return 0.0
    return (ask - bid) / mid * 1e4


def _valid_positive_number(value: Any) -> float | None:
    # JSON booleans are integers in Python and float("1.0") accepts strings.
    # The frozen proxy schema permits only actual JSON number tokens.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_nonfinite_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON token: {token}")


def validate_quote_bar(bar: QuoteBar) -> bool:
    if not isinstance(bar.epoch, int) or isinstance(bar.epoch, bool):
        return False
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
    if bar.epoch % 60 != 0 or not all(
        math.isfinite(value) and value > 0.0 for value in values
    ):
        return False
    if not math.isfinite(bar.volume) or bar.volume < 0.0:
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
    if text is None:
        return None
    raw = str(text)
    if not raw or raw != raw.strip():
        raise ValueError("timestamp must be an explicit UTC instant")
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("timestamp must carry an explicit UTC offset")
    timestamp = parsed.timestamp()
    if parsed.microsecond != 0 or not math.isfinite(timestamp) or not timestamp.is_integer():
        raise ValueError("timestamp must resolve to an exact whole second")
    return int(timestamp)


def load_m1_csv(
    path: Path, *, start: str | None = None, end: str | None = None
) -> list[QuoteBar]:
    """Load exact-schema, strict-UTC, monotonic bid/ask M1; retain gaps."""

    start_epoch = _parse_epoch(start)
    end_epoch = _parse_epoch(end)
    if start_epoch is not None and end_epoch is not None and start_epoch >= end_epoch:
        raise ValueError("start must be earlier than end")
    rows: list[QuoteBar] = []
    last_source_epoch: int | None = None
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
                values = [float(value) for value in row[1:]]
            except (AssertionError, TypeError, ValueError) as exc:
                raise ValueError(f"{path.name}:{row_number}: malformed M1 row") from exc
            if last_source_epoch is not None and epoch <= last_source_epoch:
                raise ValueError(f"{path.name}:{row_number}: non-monotonic timestamp")
            last_source_epoch = epoch
            bar = QuoteBar(epoch, *values)
            if not validate_quote_bar(bar):
                raise ValueError(f"{path.name}:{row_number}: invalid bid/ask OHLC")
            if start_epoch is not None and epoch < start_epoch:
                continue
            if end_epoch is not None and epoch >= end_epoch:
                continue
            rows.append(bar)
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
    run_start = 0
    for index in range(1, len(bars)):
        if bars[index].epoch != bars[index - 1].epoch + 60:
            runs.append((run_start, index))
            run_start = index
    runs.append((run_start, len(bars)))
    return runs


def _build_pre_signal_context(
    bars: Sequence[QuoteBar],
) -> tuple[
    list[float | None],
    list[float | None],
    dict[int, list[float | None]],
    dict[int, list[float | None]],
]:
    """Build context for t solely from consecutive bars ending at t-1."""

    volatility: list[float | None] = [None] * len(bars)
    spread_q25: list[float | None] = [None] * len(bars)
    lookbacks = sorted({config.channel_lookback for config in GRID})
    prior_bid_high = {lookback: [None] * len(bars) for lookback in lookbacks}
    prior_ask_low = {lookback: [None] * len(bars) for lookback in lookbacks}
    for run_start, run_end in _consecutive_runs(bars):
        # The extra leading bar supplies the previous close for the first of
        # exactly 240 true ranges.  Signal t itself is never admitted.
        first_signal = run_start + BASELINE_M1_BARS + 1
        if first_signal >= run_end:
            continue
        baseline_start = first_signal - BASELINE_M1_BARS
        spread_ordered = sorted(
            bars[index].spread_close_bps
            for index in range(baseline_start, first_signal)
        )
        tr_ordered = sorted(
            _true_range_bps(bars[index - 1], bars[index])
            for index in range(baseline_start, first_signal)
        )
        for signal_index in range(first_signal, run_end):
            median_tr = statistics.median(tr_ordered)
            q25 = _quantile_sorted(spread_ordered, 0.25)
            if math.isfinite(median_tr) and median_tr > 0.0 and q25 > 0.0:
                volatility[signal_index] = median_tr
                spread_q25[signal_index] = q25
                for lookback in lookbacks:
                    channel = bars[signal_index - lookback : signal_index]
                    prior_bid_high[lookback][signal_index] = max(
                        bar.bid_h for bar in channel
                    )
                    prior_ask_low[lookback][signal_index] = min(
                        bar.ask_l for bar in channel
                    )

            next_signal = signal_index + 1
            if next_signal >= run_end:
                continue
            expired_index = signal_index - BASELINE_M1_BARS
            admitted_index = signal_index
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
    return volatility, spread_q25, prior_bid_high, prior_ask_low


def prepare_series(bars: Iterable[QuoteBar]) -> PreparedSeries:
    rows = list(bars)
    for index, bar in enumerate(rows):
        if not validate_quote_bar(bar):
            raise ValueError(f"invalid quote bar at index {index}")
        if index and bar.epoch <= rows[index - 1].epoch:
            raise ValueError("M1 timestamps must be strictly increasing")
    volatility, spread_q25, prior_bid_high, prior_ask_low = (
        _build_pre_signal_context(rows)
    )
    return PreparedSeries(
        rows,
        volatility,
        spread_q25,
        prior_bid_high,
        prior_ask_low,
    )


def baseline_context_at(
    prepared: PreparedSeries, *, signal_index: int, config: RDTConfig
) -> BaselineContext | None:
    if signal_index < 0 or signal_index >= len(prepared.bars):
        return None
    volatility = prepared.volatility_before_signal[signal_index]
    q25 = prepared.spread_q25_before_signal[signal_index]
    bid_high = prepared.prior_bid_high[config.channel_lookback][signal_index]
    ask_low = prepared.prior_ask_low[config.channel_lookback][signal_index]
    if any(
        value is None or not math.isfinite(value) or value <= 0.0
        for value in (volatility, q25, bid_high, ask_low)
    ):
        return None
    return BaselineContext(
        volatility_bps=float(volatility),
        spread_q25_bps=float(q25),
        prior_bid_high=float(bid_high),
        prior_ask_low=float(ask_low),
    )


def _entry_price(next_bar: QuoteBar, *, side: str) -> float:
    if side == "BUY":
        return next_bar.ask_o
    if side == "SELL":
        return next_bar.bid_o
    raise ValueError("side must be BUY or SELL")


def _floored_risk_bps(raw_risk_bps: float) -> float:
    return max(float(raw_risk_bps), FROZEN_STOP_FLOOR_BPS)


def _bracket_p_star(risk_bps: float, *, execution_cost_debit_bps: float) -> float:
    net_target_bps = risk_bps * REWARD_RISK - execution_cost_debit_bps
    net_stop_loss_bps = risk_bps + execution_cost_debit_bps
    if net_target_bps <= 0.0:
        return math.inf
    return net_stop_loss_bps / (net_stop_loss_bps + net_target_bps)


def evaluate_closed_signal(
    *,
    prepared: PreparedSeries,
    signal_index: int,
    symbol: str,
    side: str,
    config: RDTConfig,
    proxy_spread_budget_bps: float | None,
) -> tuple[RDTClosedSignal | None, str]:
    """Evaluate the closed-t trigger without reading or requiring t+1."""

    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    budget = _valid_positive_number(proxy_spread_budget_bps)
    if budget is None:
        return None, "proxy_cost_unavailable"
    if signal_index < 0 or signal_index >= len(prepared.bars):
        return None, "signal_bar_unavailable"
    signal_bar = prepared.bars[signal_index]
    context = baseline_context_at(prepared, signal_index=signal_index, config=config)
    if context is None:
        return None, "strict_pre_signal_context_unavailable"

    signal_spread = signal_bar.spread_close_bps
    spread_cap = min(context.spread_q25_bps, budget)
    if signal_spread > spread_cap + 1e-12:
        return None, "signal_spread_above_q25_or_proxy"

    if side == "BUY":
        channel_level = context.prior_bid_high
        crossed_from_inside = (
            signal_bar.bid_o <= channel_level
            and signal_bar.bid_c > channel_level
        )
        displacement_bps = (
            (signal_bar.bid_c - signal_bar.bid_o) / signal_bar.bid_o * 1e4
        )
        quote_range = signal_bar.bid_h - signal_bar.bid_l
        close_location = (
            (signal_bar.bid_c - signal_bar.bid_l) / quote_range
            if quote_range > 0.0
            else 0.0
        )
        structural_stop = signal_bar.bid_l
    else:
        channel_level = context.prior_ask_low
        crossed_from_inside = (
            signal_bar.ask_o >= channel_level
            and signal_bar.ask_c < channel_level
        )
        displacement_bps = (
            (signal_bar.ask_o - signal_bar.ask_c) / signal_bar.ask_o * 1e4
        )
        quote_range = signal_bar.ask_h - signal_bar.ask_l
        close_location = (
            (signal_bar.ask_h - signal_bar.ask_c) / quote_range
            if quote_range > 0.0
            else 0.0
        )
        structural_stop = signal_bar.ask_h
    if not crossed_from_inside:
        return None, "prior_range_not_crossed_from_inside"
    displacement_units = displacement_bps / context.volatility_bps
    if (
        not math.isfinite(displacement_units)
        or displacement_units + 1e-12 < config.displacement_vol
    ):
        return None, "displacement_too_small"
    if close_location + 1e-12 < config.close_location_floor:
        return None, "close_location_too_weak"

    expected_entry_epoch = signal_bar.epoch + 60
    return (
        RDTClosedSignal(
            config_id=config.config_id,
            symbol=str(symbol).upper(),
            side=side,
            signal_index=signal_index,
            signal_epoch=signal_bar.epoch,
            expected_entry_index=signal_index + FILL_DELAY_M1_BARS,
            expected_entry_epoch=expected_entry_epoch,
            entry_day=_utc_day(expected_entry_epoch),
            volatility_bps=context.volatility_bps,
            channel_level=channel_level,
            displacement_bps=displacement_bps,
            displacement_vol_units=displacement_units,
            close_location=close_location,
            structural_stop=structural_stop,
            spread_q25_bps=context.spread_q25_bps,
            proxy_budget_bps=budget,
            signal_spread_bps=signal_spread,
        ),
        "",
    )


def _complete_signal_at_exact_next_open(
    *,
    prepared: PreparedSeries,
    closed: RDTClosedSignal,
) -> tuple[RDTSignal | None, str]:
    """Apply fill-dependent gates only after the closed trigger is frozen."""

    if closed.expected_entry_index >= len(prepared.bars):
        return None, "exact_next_open_unavailable"
    next_bar = prepared.bars[closed.expected_entry_index]
    if next_bar.epoch != closed.expected_entry_epoch:
        return None, "exact_next_open_gap"
    entry_spread = next_bar.spread_open_bps
    spread_cap = min(closed.spread_q25_bps, closed.proxy_budget_bps)
    if entry_spread > spread_cap + 1e-12:
        return None, "entry_spread_above_q25_or_proxy"

    entry = _entry_price(next_bar, side=closed.side)
    buffer_price = STOP_BUFFER_VOL * closed.volatility_bps / 1e4 * entry
    if closed.side == "BUY":
        raw_stop = closed.structural_stop - buffer_price
        raw_risk_bps = (entry - raw_stop) / entry * 1e4
    else:
        raw_stop = closed.structural_stop + buffer_price
        raw_risk_bps = (raw_stop - entry) / entry * 1e4
    if not math.isfinite(raw_risk_bps) or raw_risk_bps <= 0.0:
        return None, "degenerate_stop"
    risk_bps = _floored_risk_bps(raw_risk_bps)
    if risk_bps > MAX_RISK_BPS + 1e-12:
        return None, "risk_above_25bps"
    if closed.side == "BUY":
        stop_price = entry * (1.0 - risk_bps / 1e4)
        target_price = entry * (1.0 + risk_bps * REWARD_RISK / 1e4)
        quote_target_bps = (target_price - entry) / entry * 1e4
    else:
        stop_price = entry * (1.0 + risk_bps / 1e4)
        target_price = entry * (1.0 - risk_bps * REWARD_RISK / 1e4)
        quote_target_bps = (entry - target_price) / entry * 1e4
    if stop_price <= 0.0 or target_price <= 0.0:
        return None, "degenerate_bracket"

    spread_stress_bps = max(
        closed.signal_spread_bps,
        entry_spread,
        closed.proxy_budget_bps,
    )
    incremental_spread_stress_bps = max(0.0, spread_stress_bps - entry_spread)
    execution_cost_debit_bps = (
        incremental_spread_stress_bps + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    )
    recorded_cost_bps = entry_spread + execution_cost_debit_bps
    p_star = _bracket_p_star(
        risk_bps,
        execution_cost_debit_bps=execution_cost_debit_bps,
    )
    if not math.isfinite(p_star) or p_star > P_STAR_MAX + 1e-12:
        return None, "bracket_cost_dead"
    if quote_target_bps + 1e-12 < MIN_TARGET_COST_RATIO * recorded_cost_bps:
        return None, "target_too_small_vs_cost"

    return (
        RDTSignal(
            config_id=closed.config_id,
            symbol=closed.symbol,
            side=closed.side,
            signal_index=closed.signal_index,
            signal_epoch=closed.signal_epoch,
            entry_index=closed.expected_entry_index,
            entry_epoch=next_bar.epoch,
            entry_day=closed.entry_day,
            entry_price=entry,
            stop_price=stop_price,
            target_price=target_price,
            raw_risk_bps=raw_risk_bps,
            risk_bps=risk_bps,
            quote_target_bps=quote_target_bps,
            recorded_cost_bps=recorded_cost_bps,
            incremental_spread_stress_bps=incremental_spread_stress_bps,
            execution_cost_debit_bps=execution_cost_debit_bps,
            p_star=p_star,
            volatility_bps=closed.volatility_bps,
            channel_level=closed.channel_level,
            structural_stop=closed.structural_stop,
            displacement_bps=closed.displacement_bps,
            displacement_vol_units=closed.displacement_vol_units,
            close_location=closed.close_location,
            spread_q25_bps=closed.spread_q25_bps,
            proxy_budget_bps=closed.proxy_budget_bps,
            signal_spread_bps=closed.signal_spread_bps,
            entry_spread_bps=entry_spread,
            extra_round_trip_cost_bps=FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
        ),
        "",
    )


def evaluate_signal(
    *,
    prepared: PreparedSeries,
    signal_index: int,
    symbol: str,
    side: str,
    config: RDTConfig,
    proxy_spread_budget_bps: float | None,
) -> tuple[RDTSignal | None, str]:
    """Freeze closed t first, then require the exact observed t+1 quote open."""

    closed, reason = evaluate_closed_signal(
        prepared=prepared,
        signal_index=signal_index,
        symbol=symbol,
        side=side,
        config=config,
        proxy_spread_budget_bps=proxy_spread_budget_bps,
    )
    if closed is None:
        return None, reason
    return _complete_signal_at_exact_next_open(prepared=prepared, closed=closed)


def outcome_horizon_is_complete(bars: Sequence[QuoteBar], *, entry_index: int) -> bool:
    horizon_end = entry_index + OUTCOME_HORIZON_M1_BARS
    if entry_index < 0 or horizon_end > len(bars):
        return False
    entry_epoch = bars[entry_index].epoch
    return all(
        bars[index].epoch == entry_epoch + offset * 60
        for offset, index in enumerate(range(entry_index, horizon_end))
    )


def _event_id_fields(
    *,
    config_id: str,
    symbol: str,
    side: str,
    signal_epoch: int,
    entry_epoch: int,
) -> str:
    raw = (
        f"range_displacement_trigger|{config_id}|{symbol}|{side}|"
        f"{signal_epoch}|{entry_epoch}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _event_id(signal: RDTSignal) -> str:
    return _event_id_fields(
        config_id=signal.config_id,
        symbol=signal.symbol,
        side=signal.side,
        signal_epoch=signal.signal_epoch,
        entry_epoch=signal.entry_epoch,
    )


def _trade_result(
    signal: RDTSignal,
    *,
    exit_bar: QuoteBar,
    exit_price: float,
    bars_held: int,
    reason: str,
) -> RDTTrade:
    if signal.side == "BUY":
        gross_bps = (exit_price - signal.entry_price) / signal.entry_price * 1e4
    else:
        gross_bps = (signal.entry_price - exit_price) / signal.entry_price * 1e4
    pnl_bps = gross_bps - signal.execution_cost_debit_bps
    full_target = reason in {"tp", "tp_gap_open"} and pnl_bps > 0.0
    return RDTTrade(
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


def simulate_trade(bars: Sequence[QuoteBar], *, signal: RDTSignal) -> RDTTrade | None:
    """Prevalidate the whole horizon, then score adverse gap/SL-first exits."""

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
    signal: RDTSignal, *, status: str, trade: RDTTrade | None
) -> dict[str, Any]:
    gate_pnl_r = (
        trade.pnl_r
        if trade is not None
        else -(signal.risk_bps + signal.execution_cost_debit_bps) / signal.risk_bps
    )
    return {
        "event_id": _event_id(signal),
        **asdict(signal),
        "reservation_status": status,
        "outcome_reason": (
            trade.exit_reason if trade is not None else "incomplete_outcome_horizon"
        ),
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


def _missing_fill_reservation_row(
    closed: RDTClosedSignal, *, reason: str
) -> dict[str, Any]:
    conservative_cost_debit = (
        closed.proxy_budget_bps + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    )
    gate_risk_basis = FROZEN_STOP_FLOOR_BPS
    return {
        "event_id": _event_id_fields(
            config_id=closed.config_id,
            symbol=closed.symbol,
            side=closed.side,
            signal_epoch=closed.signal_epoch,
            entry_epoch=closed.expected_entry_epoch,
        ),
        "config_id": closed.config_id,
        "symbol": closed.symbol,
        "side": closed.side,
        "signal_index": closed.signal_index,
        "signal_epoch": closed.signal_epoch,
        "entry_index": closed.expected_entry_index,
        "entry_epoch": closed.expected_entry_epoch,
        "entry_day": closed.entry_day,
        "entry_price": None,
        "stop_price": None,
        "target_price": None,
        "raw_risk_bps": None,
        "risk_bps": None,
        "quote_target_bps": None,
        "recorded_cost_bps": None,
        "incremental_spread_stress_bps": None,
        "execution_cost_debit_bps": conservative_cost_debit,
        "p_star": None,
        "volatility_bps": closed.volatility_bps,
        "channel_level": closed.channel_level,
        "structural_stop": closed.structural_stop,
        "displacement_bps": closed.displacement_bps,
        "displacement_vol_units": closed.displacement_vol_units,
        "close_location": closed.close_location,
        "spread_q25_bps": closed.spread_q25_bps,
        "proxy_budget_bps": closed.proxy_budget_bps,
        "signal_spread_bps": closed.signal_spread_bps,
        "entry_spread_bps": None,
        "extra_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
        "reservation_status": "unresolved",
        "outcome_reason": reason,
        "exit_epoch": None,
        "exit_price": None,
        "bars_held": None,
        "pnl_bps": None,
        "pnl_r": None,
        "gate_pnl_r": -(
            gate_risk_basis + conservative_cost_debit
        ) / gate_risk_basis,
        "gate_treatment": (
            "unresolved_missing_fill_as_adverse_stop_for_discovery_gate"
        ),
        "gate_risk_basis_bps": gate_risk_basis,
        "full_target_win": False,
        "positive_outcome": False,
    }


def screen_cell(
    *,
    prepared: PreparedSeries,
    symbol: str,
    side: str,
    config: RDTConfig,
    proxy_spread_budget_bps: float | None,
) -> dict[str, Any]:
    trades: list[RDTTrade] = []
    reservation_ledger: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    reserved_entry_days: set[str] = set()
    closed_signal_events = 0
    eligible_events = 0
    for index in range(len(prepared.bars)):
        closed, reason = evaluate_closed_signal(
            prepared=prepared,
            signal_index=index,
            symbol=symbol,
            side=side,
            config=config,
            proxy_spread_budget_bps=proxy_spread_budget_bps,
        )
        if closed is None:
            reasons[reason] += 1
            continue
        closed_signal_events += 1
        if closed.entry_day in reserved_entry_days:
            reasons["entry_day_already_reserved"] += 1
            continue

        signal, reason = _complete_signal_at_exact_next_open(
            prepared=prepared,
            closed=closed,
        )
        if signal is None:
            reasons[reason] += 1
            if reason not in {"exact_next_open_unavailable", "exact_next_open_gap"}:
                continue
            # A qualifying closed trigger whose execution quote is unknowable
            # consumes the day.  Later events cannot substitute based on the
            # missing fill, and its gate return is conservatively adverse.
            eligible_events += 1
            reserved_entry_days.add(closed.entry_day)
            reservation_ledger.append(
                _missing_fill_reservation_row(closed, reason=reason)
            )
            continue

        eligible_events += 1
        reserved_entry_days.add(signal.entry_day)
        if not outcome_horizon_is_complete(
            prepared.bars, entry_index=signal.entry_index
        ):
            reasons["incomplete_outcome_horizon"] += 1
            reservation_ledger.append(
                _reservation_row(signal, status="unresolved", trade=None)
            )
            continue
        trade = simulate_trade(prepared.bars, signal=signal)
        if trade is None:
            raise RuntimeError("complete horizon unexpectedly failed to score")
        trades.append(trade)
        reservation_ledger.append(
            _reservation_row(signal, status="scored", trade=trade)
        )

    full_target_wins = sum(trade.full_target_win for trade in trades)
    positive_outcomes = sum(trade.positive_outcome for trade in trades)
    pnl_rs = [trade.pnl_r for trade in trades]
    reservations = len(reservation_ledger)
    unresolved = sum(
        row["reservation_status"] == "unresolved" for row in reservation_ledger
    )
    gate_pnl_rs = [float(row["gate_pnl_r"]) for row in reservation_ledger]
    return {
        "config_id": config.config_id,
        "channel_lookback": config.channel_lookback,
        "displacement_vol": config.displacement_vol,
        "close_location_floor": config.close_location_floor,
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "closed_signal_events": closed_signal_events,
        "eligible_events": eligible_events,
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
        "total_r": sum(pnl_rs),
        "mean_r": statistics.fmean(pnl_rs) if pnl_rs else 0.0,
        "gate_total_r": sum(gate_pnl_rs),
        "gate_mean_r": statistics.fmean(gate_pnl_rs) if gate_pnl_rs else 0.0,
        "exit_mix": dict(Counter(trade.exit_reason for trade in trades)),
        "reasons": dict(sorted(reasons.items())),
        "reservation_ledger": reservation_ledger,
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


def trial_accounting() -> dict[str, int]:
    current = len(GRID) * len(FX_SYMBOLS) * 2
    if current != IMMUTABLE_CURRENT_ATTEMPTED_CELLS:
        raise RuntimeError("RDT fixed-grid accounting changed")
    return {
        "grid_configurations": len(GRID),
        "directions": 2,
        "symbols": len(FX_SYMBOLS),
        "current_attempted_cells": IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
        "prior_attempted_cells": IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
        "cumulative_attempted_cells": IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS,
        "expected_full_universe_cells": IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
    }


def _one_sided_wilson_lower_bound(
    wins: int,
    observations: int,
    *,
    family_alpha: float = 0.05,
    simultaneous_cells: int = SIMULTANEOUS_WILSON_FAMILY_CELLS,
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


def _passing_global_configurations(cells: Sequence[Mapping[str, Any]]) -> list[str]:
    expected = {
        (symbol, side)
        for symbol in FX_SYMBOLS
        for side in ("BUY", "SELL")
    }
    passing: list[str] = []
    for config in GRID:
        rows = [cell for cell in cells if cell.get("config_id") == config.config_id]
        keys = {
            (
                str(cell.get("symbol") or "").upper(),
                str(cell.get("side") or "").upper(),
            )
            for cell in rows
        }
        if (
            len(rows) == len(expected)
            and keys == expected
            and all(cell.get("passes_discovery_cell_gate") is True for cell in rows)
        ):
            passing.append(config.config_id)
    return passing


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _exact_nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _same_finite_number(left: Any, right: Any) -> bool:
    left_value = _finite_float(left)
    right_value = _finite_float(right)
    return bool(
        left_value is not None
        and right_value is not None
        and math.isclose(left_value, right_value, rel_tol=1e-12, abs_tol=1e-12)
    )


def _ledger_cell_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("config_id") or ""),
        str(row.get("symbol") or "").upper(),
        str(row.get("side") or "").upper(),
    )


def _exact_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _temporal_third_index(
    entry_epoch: Any,
    *,
    evaluation_start_epoch: int,
    evaluation_end_epoch: int,
) -> int | None:
    epoch = _exact_int(entry_epoch)
    if (
        epoch is None
        or evaluation_start_epoch < 0
        or evaluation_end_epoch <= evaluation_start_epoch
        or not evaluation_start_epoch <= epoch <= evaluation_end_epoch
    ):
        return None
    duration = evaluation_end_epoch - evaluation_start_epoch
    return min(
        TEMPORAL_THIRDS - 1,
        TEMPORAL_THIRDS * (epoch - evaluation_start_epoch) // duration,
    )


def _temporal_thirds_boundary_epochs(
    *, evaluation_start_epoch: int, evaluation_end_epoch: int
) -> list[int]:
    if (
        evaluation_start_epoch < 0
        or evaluation_end_epoch <= evaluation_start_epoch
    ):
        return []
    duration = evaluation_end_epoch - evaluation_start_epoch
    return [
        evaluation_start_epoch,
        evaluation_start_epoch + (duration + TEMPORAL_THIRDS - 1) // TEMPORAL_THIRDS,
        evaluation_start_epoch
        + (2 * duration + TEMPORAL_THIRDS - 1) // TEMPORAL_THIRDS,
        evaluation_end_epoch,
    ]


def _temporal_thirds_diagnostics(
    reservations: Sequence[Mapping[str, Any]],
    *,
    evaluation_start_epoch: int,
    evaluation_end_epoch: int,
) -> dict[str, Any]:
    counts = [0] * TEMPORAL_THIRDS
    wins = [0] * TEMPORAL_THIRDS
    returns: list[list[float]] = [[] for _ in range(TEMPORAL_THIRDS)]
    inputs_valid = True
    for reservation in reservations:
        segment = _temporal_third_index(
            reservation.get("entry_epoch"),
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        )
        gate_return = _finite_float(reservation.get("gate_pnl_r"))
        full_target_win = reservation.get("full_target_win")
        if (
            segment is None
            or gate_return is None
            or not isinstance(full_target_win, bool)
        ):
            inputs_valid = False
            continue
        counts[segment] += 1
        wins[segment] += int(full_target_win)
        returns[segment].append(gate_return)

    try:
        totals = [math.fsum(segment_returns) for segment_returns in returns]
    except (OverflowError, ValueError):
        totals = [0.0] * TEMPORAL_THIRDS
        inputs_valid = False
    rates = [
        segment_wins / segment_count if segment_count else 0.0
        for segment_wins, segment_count in zip(wins, counts, strict=True)
    ]
    means = [
        total / segment_count if segment_count else 0.0
        for total, segment_count in zip(totals, counts, strict=True)
    ]
    stable = bool(
        inputs_valid
        and all(
            segment_count >= MIN_RESERVATIONS_PER_TEMPORAL_THIRD
            and TEMPORAL_THIRD_WIN_RATE_DENOMINATOR * segment_wins
            >= TEMPORAL_THIRD_WIN_RATE_NUMERATOR * segment_count
            and total > 0.0
            for segment_count, segment_wins, total in zip(
                counts,
                wins,
                totals,
                strict=True,
            )
        )
    )
    return {
        "temporal_thirds_reservation_counts": counts,
        "temporal_thirds_full_target_wins": wins,
        "temporal_thirds_full_target_rates": rates,
        "temporal_thirds_gate_total_rs": totals,
        "temporal_thirds_gate_mean_rs": means,
        "gate_temporal_thirds_stable": stable,
    }


def _event_identity_is_valid(
    row: Mapping[str, Any], *, key: tuple[str, str, str], require_indices: bool = True
) -> bool:
    signal_index = (
        _exact_nonnegative_int(row.get("signal_index")) if require_indices else 0
    )
    entry_index = (
        _exact_nonnegative_int(row.get("entry_index")) if require_indices else 1
    )
    signal_epoch = _exact_int(row.get("signal_epoch"))
    entry_epoch = _exact_int(row.get("entry_epoch"))
    event_id = row.get("event_id")
    if (
        row.get("config_id") != key[0]
        or row.get("symbol") != key[1]
        or row.get("side") != key[2]
        or _ledger_cell_key(row) != key
        or signal_index is None
        or entry_index is None
        or entry_index != signal_index + FILL_DELAY_M1_BARS
        or signal_epoch is None
        or entry_epoch is None
        or signal_epoch < 0
        or signal_epoch % 60 != 0
        or entry_epoch != signal_epoch + 60
        or not isinstance(event_id, str)
        or not event_id
    ):
        return False
    try:
        entry_day_matches = row.get("entry_day") == _utc_day(entry_epoch)
    except (OSError, OverflowError, ValueError):
        return False
    return bool(
        entry_day_matches
        and event_id
        == _event_id_fields(
            config_id=key[0],
            symbol=key[1],
            side=key[2],
            signal_epoch=signal_epoch,
            entry_epoch=entry_epoch,
        )
    )


def _signal_feature_semantics_are_valid(
    row: Mapping[str, Any], *, key: tuple[str, str, str]
) -> bool:
    if not _event_identity_is_valid(row, key=key):
        return False
    config = RDT_CONFIG_BY_ID.get(key[0])
    volatility = _finite_float(row.get("volatility_bps"))
    channel_level = _finite_float(row.get("channel_level"))
    structural_stop = _finite_float(row.get("structural_stop"))
    displacement = _finite_float(row.get("displacement_bps"))
    displacement_units = _finite_float(row.get("displacement_vol_units"))
    close_location = _finite_float(row.get("close_location"))
    spread_q25 = _finite_float(row.get("spread_q25_bps"))
    proxy_budget = _finite_float(row.get("proxy_budget_bps"))
    signal_spread = _finite_float(row.get("signal_spread_bps"))
    extra_cost = _finite_float(row.get("extra_round_trip_cost_bps"))
    if (
        config is None
        or volatility is None
        or volatility <= 0.0
        or channel_level is None
        or channel_level <= 0.0
        or structural_stop is None
        or structural_stop <= 0.0
        or displacement is None
        or displacement <= 0.0
        or displacement_units is None
        or close_location is None
        or spread_q25 is None
        or spread_q25 <= 0.0
        or proxy_budget is None
        or proxy_budget <= 0.0
        or signal_spread is None
        or signal_spread < 0.0
        or extra_cost is None
    ):
        return False
    spread_cap = min(spread_q25, proxy_budget)
    return bool(
        _same_finite_number(displacement_units, displacement / volatility)
        and displacement_units + 1e-12 >= config.displacement_vol
        and config.close_location_floor - 1e-12 <= close_location <= 1.0 + 1e-12
        and signal_spread <= spread_cap + 1e-12
        and (
            (key[2] == "BUY" and structural_stop <= channel_level + 1e-12)
            or (key[2] == "SELL" and structural_stop >= channel_level - 1e-12)
        )
        and _same_finite_number(extra_cost, FROZEN_EXTRA_ROUND_TRIP_COST_BPS)
    )


def _completed_signal_semantics_are_valid(
    row: Mapping[str, Any], *, key: tuple[str, str, str]
) -> bool:
    if not _signal_feature_semantics_are_valid(row, key=key):
        return False
    entry_price = _finite_float(row.get("entry_price"))
    stop_price = _finite_float(row.get("stop_price"))
    target_price = _finite_float(row.get("target_price"))
    raw_risk = _finite_float(row.get("raw_risk_bps"))
    risk = _finite_float(row.get("risk_bps"))
    quote_target = _finite_float(row.get("quote_target_bps"))
    recorded_cost = _finite_float(row.get("recorded_cost_bps"))
    incremental_stress = _finite_float(row.get("incremental_spread_stress_bps"))
    debit = _finite_float(row.get("execution_cost_debit_bps"))
    p_star = _finite_float(row.get("p_star"))
    spread_q25 = _finite_float(row.get("spread_q25_bps"))
    proxy_budget = _finite_float(row.get("proxy_budget_bps"))
    signal_spread = _finite_float(row.get("signal_spread_bps"))
    entry_spread = _finite_float(row.get("entry_spread_bps"))
    volatility = _finite_float(row.get("volatility_bps"))
    structural_stop = _finite_float(row.get("structural_stop"))
    if (
        entry_price is None
        or entry_price <= 0.0
        or stop_price is None
        or stop_price <= 0.0
        or target_price is None
        or target_price <= 0.0
        or raw_risk is None
        or raw_risk <= 0.0
        or risk is None
        or risk <= 0.0
        or risk > MAX_RISK_BPS + 1e-12
        or quote_target is None
        or quote_target <= 0.0
        or recorded_cost is None
        or recorded_cost <= 0.0
        or incremental_stress is None
        or incremental_stress < 0.0
        or debit is None
        or debit <= 0.0
        or p_star is None
        or p_star > P_STAR_MAX + 1e-12
        or spread_q25 is None
        or proxy_budget is None
        or signal_spread is None
        or entry_spread is None
        or entry_spread < 0.0
        or entry_spread > min(spread_q25, proxy_budget) + 1e-12
        or volatility is None
        or structural_stop is None
    ):
        return False
    expected_risk = _floored_risk_bps(raw_risk)
    expected_stress = max(signal_spread, entry_spread, proxy_budget)
    expected_incremental = max(0.0, expected_stress - entry_spread)
    expected_debit = expected_incremental + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    expected_recorded = entry_spread + expected_debit
    expected_p_star = _bracket_p_star(
        risk,
        execution_cost_debit_bps=expected_debit,
    )
    if key[2] == "BUY":
        raw_stop = structural_stop - STOP_BUFFER_VOL * volatility / 1e4 * entry_price
        expected_raw_risk = (entry_price - raw_stop) / entry_price * 1e4
        stop_risk = (entry_price - stop_price) / entry_price * 1e4
        target_distance = (target_price - entry_price) / entry_price * 1e4
        bracket_is_ordered = stop_price < entry_price < target_price
    else:
        raw_stop = structural_stop + STOP_BUFFER_VOL * volatility / 1e4 * entry_price
        expected_raw_risk = (raw_stop - entry_price) / entry_price * 1e4
        stop_risk = (stop_price - entry_price) / entry_price * 1e4
        target_distance = (entry_price - target_price) / entry_price * 1e4
        bracket_is_ordered = target_price < entry_price < stop_price
    return bool(
        bracket_is_ordered
        and math.isfinite(expected_p_star)
        and math.isfinite(expected_raw_risk)
        and expected_raw_risk > 0.0
        and _same_finite_number(raw_risk, expected_raw_risk)
        and _same_finite_number(risk, expected_risk)
        and _same_finite_number(stop_risk, risk)
        and _same_finite_number(target_distance, risk * REWARD_RISK)
        and _same_finite_number(quote_target, target_distance)
        and _same_finite_number(incremental_stress, expected_incremental)
        and _same_finite_number(debit, expected_debit)
        and _same_finite_number(recorded_cost, expected_recorded)
        and _same_finite_number(p_star, expected_p_star)
        and quote_target + 1e-12 >= MIN_TARGET_COST_RATIO * recorded_cost
    )


def _trade_semantics_are_valid(
    trade: Mapping[str, Any], *, key: tuple[str, str, str]
) -> bool:
    if set(trade) != RDT_TRADE_FIELD_NAMES or not _event_identity_is_valid(
        trade,
        key=key,
        require_indices=False,
    ):
        return False
    entry_epoch = _exact_int(trade.get("entry_epoch"))
    exit_epoch = _exact_int(trade.get("exit_epoch"))
    bars_held = _exact_int(trade.get("bars_held"))
    risk_bps = _finite_float(trade.get("risk_bps"))
    pnl_bps = _finite_float(trade.get("pnl_bps"))
    pnl_r = _finite_float(trade.get("pnl_r"))
    exit_reason = trade.get("exit_reason")
    if (
        entry_epoch is None
        or exit_epoch is None
        or exit_epoch < entry_epoch
        or (exit_epoch - entry_epoch) % 60 != 0
        or bars_held is None
        or bars_held != (exit_epoch - entry_epoch) // 60 + 1
        or not 1 <= bars_held <= OUTCOME_HORIZON_M1_BARS
        or risk_bps is None
        or risk_bps <= 0.0
        or pnl_bps is None
        or pnl_r is None
        or not _same_finite_number(pnl_r, pnl_bps / risk_bps)
        or exit_reason
        not in {"sl_gap_open", "tp_gap_open", "sl_double_touch", "sl", "tp", "time_stop"}
    ):
        return False
    positive_outcome = pnl_bps > 0.0
    full_target_win = bool(
        exit_reason in {"tp", "tp_gap_open"} and positive_outcome
    )
    return bool(
        (exit_reason != "time_stop" or bars_held == OUTCOME_HORIZON_M1_BARS)
        and trade.get("positive_outcome") is positive_outcome
        and trade.get("full_target_win") is full_target_win
    )


def _scored_reservation_matches_trade(
    reservation: Mapping[str, Any],
    trade: Mapping[str, Any] | None,
    *,
    key: tuple[str, str, str],
) -> bool:
    if (
        trade is None
        or set(reservation) != RDT_RESERVATION_FIELD_NAMES
        or reservation.get("reservation_status") != "scored"
        or reservation.get("gate_treatment") != "observed_trade"
        or not _completed_signal_semantics_are_valid(reservation, key=key)
        or not _trade_semantics_are_valid(trade, key=key)
        or reservation.get("event_id") != trade.get("event_id")
        or reservation.get("outcome_reason") != trade.get("exit_reason")
        or reservation.get("full_target_win") is not trade.get("full_target_win")
        or reservation.get("positive_outcome") is not trade.get("positive_outcome")
    ):
        return False
    for field in ("entry_price", "exit_price", "risk_bps", "pnl_bps", "pnl_r"):
        if not _same_finite_number(reservation.get(field), trade.get(field)):
            return False
    entry_price = _finite_float(reservation.get("entry_price"))
    exit_price = _finite_float(reservation.get("exit_price"))
    stop_price = _finite_float(reservation.get("stop_price"))
    target_price = _finite_float(reservation.get("target_price"))
    risk_bps = _finite_float(reservation.get("risk_bps"))
    debit = _finite_float(reservation.get("execution_cost_debit_bps"))
    if (
        entry_price is None
        or entry_price <= 0.0
        or exit_price is None
        or exit_price <= 0.0
        or stop_price is None
        or stop_price <= 0.0
        or target_price is None
        or target_price <= 0.0
        or risk_bps is None
        or risk_bps <= 0.0
        or debit is None
        or debit <= 0.0
    ):
        return False
    if key[2] == "BUY":
        gross_bps = (exit_price - entry_price) / entry_price * 1e4
        stop_risk_bps = (entry_price - stop_price) / entry_price * 1e4
        target_bps = (target_price - entry_price) / entry_price * 1e4
        bracket_is_ordered = stop_price < entry_price < target_price
    else:
        gross_bps = (entry_price - exit_price) / entry_price * 1e4
        stop_risk_bps = (stop_price - entry_price) / entry_price * 1e4
        target_bps = (entry_price - target_price) / entry_price * 1e4
        bracket_is_ordered = target_price < entry_price < stop_price
    reason = reservation.get("outcome_reason")
    bracket_exit_matches = bool(
        (reason not in {"tp", "tp_gap_open"} or _same_finite_number(exit_price, target_price))
        and (
            reason not in {"sl", "sl_double_touch"}
            or _same_finite_number(exit_price, stop_price)
        )
        and (
            reason != "sl_gap_open"
            or (key[2] == "BUY" and exit_price <= stop_price + 1e-12)
            or (key[2] == "SELL" and exit_price >= stop_price - 1e-12)
        )
        and (
            reason != "time_stop"
            or (key[2] == "BUY" and stop_price < exit_price < target_price)
            or (key[2] == "SELL" and target_price < exit_price < stop_price)
        )
    )
    return bool(
        reservation.get("exit_epoch") == trade.get("exit_epoch")
        and reservation.get("bars_held") == trade.get("bars_held")
        and bracket_is_ordered
        and bracket_exit_matches
        and _same_finite_number(stop_risk_bps, risk_bps)
        and _same_finite_number(target_bps, risk_bps * REWARD_RISK)
        and _same_finite_number(reservation.get("quote_target_bps"), target_bps)
        and _same_finite_number(
            reservation.get("pnl_bps"), gross_bps - debit
        )
        and _same_finite_number(
            reservation.get("gate_pnl_r"), trade.get("pnl_r")
        )
    )


def _unresolved_reservation_is_adverse(
    reservation: Mapping[str, Any], *, key: tuple[str, str, str]
) -> bool:
    reason = reservation.get("outcome_reason")
    expected_schema = (
        RDT_MISSING_FILL_RESERVATION_FIELD_NAMES
        if reason in {"exact_next_open_unavailable", "exact_next_open_gap"}
        else RDT_RESERVATION_FIELD_NAMES
    )
    if (
        set(reservation) != expected_schema
        or reservation.get("reservation_status") != "unresolved"
        or not _signal_feature_semantics_are_valid(reservation, key=key)
        or reservation.get("full_target_win") is not False
        or reservation.get("positive_outcome") is not False
        or any(
            reservation.get(field) is not None
            for field in (
                "exit_epoch",
                "exit_price",
                "bars_held",
                "pnl_bps",
                "pnl_r",
            )
        )
    ):
        return False
    treatment = reservation.get("gate_treatment")
    gate_return = _finite_float(reservation.get("gate_pnl_r"))
    debit = _finite_float(reservation.get("execution_cost_debit_bps"))
    if gate_return is None or gate_return >= 0.0 or debit is None or debit <= 0.0:
        return False
    if reason == "incomplete_outcome_horizon":
        if not _completed_signal_semantics_are_valid(reservation, key=key):
            return False
        risk_basis = _finite_float(reservation.get("risk_bps"))
        expected_treatment = "unresolved_as_adverse_stop_for_discovery_gate"
    elif reason in {"exact_next_open_unavailable", "exact_next_open_gap"}:
        risk_basis = _finite_float(reservation.get("gate_risk_basis_bps"))
        expected_treatment = (
            "unresolved_missing_fill_as_adverse_stop_for_discovery_gate"
        )
        if any(
            reservation.get(field) is not None
            for field in (
                "entry_price",
                "stop_price",
                "target_price",
                "raw_risk_bps",
                "risk_bps",
                "quote_target_bps",
                "recorded_cost_bps",
                "incremental_spread_stress_bps",
                "p_star",
                "entry_spread_bps",
            )
        ):
            return False
        proxy_budget = _finite_float(reservation.get("proxy_budget_bps"))
        if (
            proxy_budget is None
            or not _same_finite_number(risk_basis, FROZEN_STOP_FLOOR_BPS)
            or not _same_finite_number(
                debit,
                proxy_budget + FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
            )
        ):
            return False
    else:
        return False
    if risk_basis is None or risk_basis <= 0.0 or treatment != expected_treatment:
        return False
    expected_gate_return = -(risk_basis + debit) / risk_basis
    return _same_finite_number(gate_return, expected_gate_return)


def _row_is_within_evaluation_window(
    row: Mapping[str, Any], *, start_epoch: int, end_epoch: int
) -> bool:
    signal_epoch = _exact_int(row.get("signal_epoch"))
    entry_epoch = _exact_int(row.get("entry_epoch"))
    exit_value = row.get("exit_epoch")
    if (
        signal_epoch is None
        or entry_epoch is None
        or not start_epoch <= signal_epoch < entry_epoch
        or signal_epoch >= end_epoch
    ):
        return False
    missing_fill = bool(
        row.get("reservation_status") == "unresolved"
        and row.get("outcome_reason")
        in {"exact_next_open_unavailable", "exact_next_open_gap"}
    )
    if missing_fill:
        return bool(entry_epoch <= end_epoch and exit_value is None)
    if entry_epoch >= end_epoch:
        return False
    if exit_value is None:
        return True
    exit_epoch = _exact_int(exit_value)
    return bool(exit_epoch is not None and entry_epoch <= exit_epoch < end_epoch)


def _mapping_matches_exact_generated_row(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    if set(actual) != set(expected):
        return False
    for field, expected_value in expected.items():
        actual_value = actual.get(field)
        if isinstance(expected_value, bool):
            if actual_value is not expected_value:
                return False
        elif isinstance(expected_value, int):
            if (
                not isinstance(actual_value, int)
                or isinstance(actual_value, bool)
                or actual_value != expected_value
            ):
                return False
        elif isinstance(expected_value, float):
            if not _same_finite_number(actual_value, expected_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _reservation_matches_source_replay(
    reservation: Mapping[str, Any],
    trade: Mapping[str, Any] | None,
    *,
    key: tuple[str, str, str],
    prepared: PreparedSeries | None,
    proxy_budget_bps: float | None,
) -> bool:
    config = RDT_CONFIG_BY_ID.get(key[0])
    signal_index = _exact_nonnegative_int(reservation.get("signal_index"))
    if config is None or signal_index is None or prepared is None:
        return False
    closed, _ = evaluate_closed_signal(
        prepared=prepared,
        signal_index=signal_index,
        symbol=key[1],
        side=key[2],
        config=config,
        proxy_spread_budget_bps=proxy_budget_bps,
    )
    if closed is None:
        return False
    signal, completion_reason = _complete_signal_at_exact_next_open(
        prepared=prepared,
        closed=closed,
    )
    if signal is None:
        if completion_reason not in {
            "exact_next_open_unavailable",
            "exact_next_open_gap",
        }:
            return False
        expected_reservation = _missing_fill_reservation_row(
            closed,
            reason=completion_reason,
        )
        return bool(
            trade is None
            and _mapping_matches_exact_generated_row(
                reservation,
                expected_reservation,
            )
        )
    expected_trade = simulate_trade(prepared.bars, signal=signal)
    expected_reservation = _reservation_row(
        signal,
        status="scored" if expected_trade is not None else "unresolved",
        trade=expected_trade,
    )
    if not _mapping_matches_exact_generated_row(reservation, expected_reservation):
        return False
    if expected_trade is None:
        return trade is None
    return bool(
        trade is not None
        and _mapping_matches_exact_generated_row(trade, asdict(expected_trade))
    )


def _cell_matches_complete_source_replay(
    *,
    cell: Mapping[str, Any],
    reservations: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    key: tuple[str, str, str],
    prepared: PreparedSeries | None,
    proxy_budget_bps: float | None,
) -> bool:
    config = RDT_CONFIG_BY_ID.get(key[0])
    if config is None or prepared is None:
        return False
    try:
        expected_cell = screen_cell(
            prepared=prepared,
            symbol=key[1],
            side=key[2],
            config=config,
            proxy_spread_budget_bps=proxy_budget_bps,
        )
    except (RuntimeError, TypeError, ValueError):
        return False
    expected_reservations = expected_cell.pop("reservation_ledger")
    expected_trades = expected_cell.pop("trade_ledger")
    actual_source_fields = {field: cell.get(field) for field in expected_cell}
    return bool(
        _mapping_matches_exact_generated_row(actual_source_fields, expected_cell)
        and len(reservations) == len(expected_reservations)
        and len(trades) == len(expected_trades)
        and all(
            _mapping_matches_exact_generated_row(actual, expected)
            for actual, expected in zip(
                reservations,
                expected_reservations,
                strict=True,
            )
        )
        and all(
            _mapping_matches_exact_generated_row(actual, expected)
            for actual, expected in zip(trades, expected_trades, strict=True)
        )
    )


def _cell_contract_is_valid(
    cell: Mapping[str, Any],
    *,
    key: tuple[str, str, str],
    config: RDTConfig | None,
) -> bool:
    fields = frozenset(cell)
    if fields not in {
        RDT_CELL_PRE_GATE_FIELD_NAMES,
        RDT_CELL_PRE_GATE_FIELD_NAMES | RDT_CELL_GATE_FIELD_NAMES,
    }:
        return False
    closed_events = _exact_nonnegative_int(cell.get("closed_signal_events"))
    eligible_events = _exact_nonnegative_int(cell.get("eligible_events"))
    reasons = cell.get("reasons")
    source_ready = cell.get("source_ready")
    proxy_contract_ready = cell.get("proxy_contract_ready")
    proxy_budget_ready = cell.get("proxy_budget_ready")
    source_error = cell.get("source_error")
    if (
        config is None
        or cell.get("config_id") != key[0]
        or cell.get("symbol") != key[1]
        or cell.get("side") != key[2]
        or closed_events is None
        or eligible_events is None
        or closed_events < eligible_events
        or not isinstance(reasons, Mapping)
        or any(not isinstance(reason, str) or not reason for reason in reasons)
        or any(_exact_nonnegative_int(count) is None for count in reasons.values())
        or not isinstance(source_ready, bool)
        or not isinstance(proxy_contract_ready, bool)
        or not isinstance(proxy_budget_ready, bool)
        or (source_ready and source_error is not None)
        or (
            not source_ready
            and (not isinstance(source_error, str) or not source_error)
        )
        or cell.get("one_trade_per_cell_entry_day") is not True
        or _exact_int(cell.get("outcome_horizon_bars"))
        != OUTCOME_HORIZON_M1_BARS
        or not _same_finite_number(cell.get("reward_risk"), REWARD_RISK)
        or not _same_finite_number(
            cell.get("stop_floor_bps"),
            FROZEN_STOP_FLOOR_BPS,
        )
        or not _same_finite_number(cell.get("max_risk_bps"), MAX_RISK_BPS)
        or not _same_finite_number(cell.get("p_star_max"), P_STAR_MAX)
        or not _same_finite_number(
            cell.get("min_target_cost_ratio"),
            MIN_TARGET_COST_RATIO,
        )
        or not _same_finite_number(
            cell.get("extra_round_trip_cost_bps"),
            FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
        )
        or cell.get("economic_claim_ready") is not False
        or cell.get("economics_claim_ready") is not False
    ):
        return False
    expected_cost_mode = (
        "pair_specific_frozen_proxy_spread_stress"
        if proxy_budget_ready
        else "cost_unavailable"
    )
    return cell.get("cost_mode") == expected_cost_mode


def _apply_discovery_gate(
    *,
    cells: list[dict[str, Any]],
    reservation_ledger: Sequence[Mapping[str, Any]],
    trade_ledger: Sequence[Mapping[str, Any]],
    evaluation_start_epoch: int,
    evaluation_end_epoch: int,
    expected_proxy_spread_budgets_bps: Mapping[str, float],
    prepared_series_by_symbol: Mapping[str, PreparedSeries],
) -> dict[str, Any]:
    valid_evaluation_window = bool(
        _exact_int(evaluation_start_epoch) is not None
        and _exact_int(evaluation_end_epoch) is not None
        and evaluation_start_epoch >= 0
        and evaluation_end_epoch > evaluation_start_epoch
    )
    try:
        expected_proxy_budgets = _normalize_proxy_budgets(
            expected_proxy_spread_budgets_bps
        )
    except (AttributeError, TypeError, ValueError):
        expected_proxy_budgets = {}
    proxy_mapping_complete = set(expected_proxy_budgets) == set(FX_SYMBOLS)
    prepared_mapping_complete = bool(
        isinstance(prepared_series_by_symbol, Mapping)
        and set(prepared_series_by_symbol) == set(FX_SYMBOLS)
        and all(
            isinstance(prepared_series_by_symbol.get(symbol), PreparedSeries)
            for symbol in FX_SYMBOLS
        )
    )
    reservations_by_cell: dict[
        tuple[str, str, str], list[Mapping[str, Any]]
    ] = {}
    for reservation in reservation_ledger:
        key = (
            str(reservation.get("config_id") or ""),
            str(reservation.get("symbol") or "").upper(),
            str(reservation.get("side") or "").upper(),
        )
        reservations_by_cell.setdefault(key, []).append(reservation)
    trades_by_cell: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for trade in trade_ledger:
        key = (
            str(trade.get("config_id") or ""),
            str(trade.get("symbol") or "").upper(),
            str(trade.get("side") or "").upper(),
        )
        trades_by_cell.setdefault(key, []).append(trade)

    planned_keys = {
        (config.config_id, symbol, side)
        for config in GRID
        for symbol in FX_SYMBOLS
        for side in ("BUY", "SELL")
    }
    cell_keys = [
        (
            str(cell.get("config_id") or ""),
            str(cell.get("symbol") or "").upper(),
            str(cell.get("side") or "").upper(),
        )
        for cell in cells
    ]
    cell_key_counts = Counter(cell_keys)
    known_cell_keys = set(cell_keys)
    full_family_cells_present = bool(
        len(cell_keys) == len(planned_keys)
        and len(known_cell_keys) == len(cell_keys)
        and known_cell_keys == planned_keys
    )
    ledger_keys_valid = bool(
        set(reservations_by_cell).issubset(planned_keys)
        and set(reservations_by_cell).issubset(known_cell_keys)
        and set(trades_by_cell).issubset(planned_keys)
        and set(trades_by_cell).issubset(known_cell_keys)
    )
    scored_event_id_rows = [
        str(reservation.get("event_id") or "")
        for reservation in reservation_ledger
        if reservation.get("reservation_status") == "scored"
    ]
    trade_event_id_rows = [
        str(trade.get("event_id") or "") for trade in trade_ledger
    ]
    global_trade_identity_matches = bool(
        ledger_keys_valid
        and all(scored_event_id_rows)
        and all(trade_event_id_rows)
        and len(scored_event_id_rows) == len(set(scored_event_id_rows))
        and len(trade_event_id_rows) == len(set(trade_event_id_rows))
        and Counter(scored_event_id_rows) == Counter(trade_event_id_rows)
    )

    for cell in cells:
        key = (
            str(cell.get("config_id") or ""),
            str(cell.get("symbol") or "").upper(),
            str(cell.get("side") or "").upper(),
        )
        fixed_config = RDT_CONFIG_BY_ID.get(key[0])
        cell_contract_consistent = _cell_contract_is_valid(
            cell,
            key=key,
            config=fixed_config,
        )
        cell_proxy_budget = _finite_float(cell.get("proxy_budget_bps"))
        expected_proxy_budget = expected_proxy_budgets.get(key[1])
        reservations = reservations_by_cell.get(key, [])
        trades = trades_by_cell.get(key, [])
        temporal_scope_consistent = bool(
            valid_evaluation_window
            and all(
                _row_is_within_evaluation_window(
                    row,
                    start_epoch=evaluation_start_epoch,
                    end_epoch=evaluation_end_epoch,
                )
                for row in (*reservations, *trades)
            )
        )
        days = [str(row.get("entry_day") or "") for row in reservations]
        parsed_gate_returns = [
            _finite_float(row.get("gate_pnl_r")) for row in reservations
        ]
        gate_returns = [
            value for value in parsed_gate_returns if value is not None
        ]
        scored = [
            row for row in reservations if row.get("reservation_status") == "scored"
        ]
        unresolved = [
            row
            for row in reservations
            if row.get("reservation_status") == "unresolved"
        ]
        wins = sum(row.get("full_target_win") is True for row in scored)
        positive_outcomes = sum(
            row.get("positive_outcome") is True for row in scored
        )
        all_reservation_ids = [
            str(row.get("event_id") or "") for row in reservations
        ]
        scored_ids = [str(row.get("event_id") or "") for row in scored]
        trade_ids = [str(row.get("event_id") or "") for row in trades]
        reservation_ids = Counter(scored_ids)
        cell_trade_ids = Counter(trade_ids)
        trade_by_id = {
            str(trade.get("event_id") or ""): trade for trade in trades
        }
        prepared_for_symbol = (
            prepared_series_by_symbol.get(key[1])
            if prepared_mapping_complete
            else None
        )
        source_replay_consistent = _cell_matches_complete_source_replay(
            cell=cell,
            reservations=reservations,
            trades=trades,
            key=key,
            prepared=prepared_for_symbol,
            proxy_budget_bps=expected_proxy_budget,
        )
        scored_rows_match_trades = bool(
            reservation_ids == cell_trade_ids
            and "" not in reservation_ids
            and all(count == 1 for count in reservation_ids.values())
            and all(count == 1 for count in cell_trade_ids.values())
            and len(trade_by_id) == len(trades)
            and all(
                _scored_reservation_matches_trade(
                    row,
                    trade_by_id.get(str(row.get("event_id") or "")),
                    key=key,
                )
                for row in scored
            )
        )
        all_reservation_ids_are_unique = bool(
            all(all_reservation_ids)
            and len(all_reservation_ids) == len(set(all_reservation_ids))
        )
        trade_returns = [_finite_float(row.get("pnl_r")) for row in trades]
        finite_trade_returns = [
            value for value in trade_returns if value is not None
        ]
        expected_trade_rate = wins / len(trades) if trades else 0.0
        expected_reservation_rate = wins / len(reservations) if reservations else 0.0
        expected_positive_rate = (
            positive_outcomes / len(scored) if scored else 0.0
        )
        expected_total_r = sum(finite_trade_returns)
        expected_mean_r = (
            statistics.fmean(finite_trade_returns) if finite_trade_returns else 0.0
        )
        expected_gate_total_r = sum(gate_returns)
        expected_gate_mean_r = (
            statistics.fmean(gate_returns) if gate_returns else 0.0
        )
        expected_exit_mix = dict(
            Counter(str(trade.get("exit_reason") or "") for trade in trades)
        )
        reservation_event_ids = cell.get("reservation_event_ids")
        cell_trade_event_ids = cell.get("trade_event_ids")
        cell_exit_mix = cell.get("exit_mix")
        ledger_consistent = bool(
            full_family_cells_present
            and cell_key_counts[key] == 1
            and cell_contract_consistent
            and cell.get("config_id") == key[0]
            and cell.get("symbol") == key[1]
            and cell.get("side") == key[2]
            and fixed_config is not None
            and temporal_scope_consistent
            and source_replay_consistent
            and proxy_mapping_complete
            and expected_proxy_budget is not None
            and _same_finite_number(cell_proxy_budget, expected_proxy_budget)
            and _exact_int(cell.get("channel_lookback"))
            == fixed_config.channel_lookback
            and _same_finite_number(
                cell.get("displacement_vol"),
                fixed_config.displacement_vol,
            )
            and _same_finite_number(
                cell.get("close_location_floor"),
                fixed_config.close_location_floor,
            )
            and ledger_keys_valid
            and len(reservations)
            == _exact_nonnegative_int(cell.get("entry_day_reservations"))
            and len(scored) + len(unresolved) == len(reservations)
            and len(scored) == _exact_nonnegative_int(cell.get("scored_trades"))
            and len(unresolved)
            == _exact_nonnegative_int(cell.get("unresolved_reservations"))
            and wins == _exact_nonnegative_int(cell.get("full_target_wins"))
            and positive_outcomes
            == _exact_nonnegative_int(cell.get("positive_outcomes"))
            and len(set(days)) == len(days)
            and all(days)
            and len(gate_returns) == len(reservations)
            and len(finite_trade_returns) == len(trades)
            and all_reservation_ids_are_unique
            and all(
                _same_finite_number(
                    row.get("proxy_budget_bps"),
                    expected_proxy_budget,
                )
                for row in reservations
            )
            and all(
                _unresolved_reservation_is_adverse(row, key=key)
                for row in unresolved
            )
            and scored_rows_match_trades
            and global_trade_identity_matches
            and isinstance(reservation_event_ids, list)
            and reservation_event_ids == all_reservation_ids
            and isinstance(cell_trade_event_ids, list)
            and cell_trade_event_ids == trade_ids
            and _exact_nonnegative_int(cell.get("eligible_events"))
            == len(reservations)
            and _same_finite_number(
                cell.get("full_target_trade_win_rate"), expected_trade_rate
            )
            and _same_finite_number(
                cell.get("full_target_reservation_rate"),
                expected_reservation_rate,
            )
            and _same_finite_number(
                cell.get("positive_outcome_rate"), expected_positive_rate
            )
            and _same_finite_number(cell.get("total_r"), expected_total_r)
            and _same_finite_number(cell.get("mean_r"), expected_mean_r)
            and _same_finite_number(
                cell.get("gate_total_r"), expected_gate_total_r
            )
            and _same_finite_number(
                cell.get("gate_mean_r"), expected_gate_mean_r
            )
            and isinstance(cell_exit_mix, Mapping)
            and dict(cell_exit_mix) == expected_exit_mix
        )
        independent_days = len(set(days))
        reservation_rate = wins / len(reservations) if reservations else 0.0
        wilson_lower = _one_sided_wilson_lower_bound(wins, len(reservations))
        reserved_utc_day_t = _finite_one_sample_t(gate_returns)
        temporal_thirds = _temporal_thirds_diagnostics(
            reservations,
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        )
        gate_mean = statistics.fmean(gate_returns) if gate_returns else 0.0
        passes = bool(
            ledger_consistent
            and cell.get("source_ready") is True
            and cell.get("proxy_budget_ready") is True
            and cell.get("proxy_contract_ready") is True
            and cell.get("cost_mode")
            == "pair_specific_frozen_proxy_spread_stress"
            and cell.get("source_error") is None
            and cell_proxy_budget is not None
            and cell_proxy_budget > 0.0
            and len(scored) >= MIN_TRADES_PER_CELL
            and independent_days >= MIN_INDEPENDENT_ENTRY_DAYS
            and reservation_rate >= MIN_OBSERVED_FULL_TARGET_RATE
            and wilson_lower >= MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
            and gate_mean > 0.0
            and reserved_utc_day_t
            >= TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
            and temporal_thirds["gate_temporal_thirds_stable"] is True
        )
        cell.update(
            {
                "independent_entry_days": independent_days,
                "gate_full_target_reservation_rate": reservation_rate,
                "simultaneous_wilson_lower_bound": wilson_lower,
                "reserved_utc_day_one_sample_t": reserved_utc_day_t,
                **temporal_thirds,
                "gate_ledger_consistent": ledger_consistent,
                "gate_actual_scored_reservations": len(scored),
                "gate_actual_unresolved_reservations": len(unresolved),
                "gate_scored_trade_ids_match": scored_rows_match_trades,
                "gate_row_semantics_consistent": bool(
                    all(
                        _unresolved_reservation_is_adverse(row, key=key)
                        for row in unresolved
                    )
                    and all(
                        _scored_reservation_matches_trade(
                            row,
                            trade_by_id.get(str(row.get("event_id") or "")),
                            key=key,
                        )
                        for row in scored
                    )
                ),
                "gate_temporal_scope_consistent": temporal_scope_consistent,
                "gate_proxy_contract_binding_consistent": bool(
                    proxy_mapping_complete
                    and expected_proxy_budget is not None
                    and _same_finite_number(cell_proxy_budget, expected_proxy_budget)
                    and all(
                        _same_finite_number(
                            row.get("proxy_budget_bps"),
                            expected_proxy_budget,
                        )
                        for row in reservations
                    )
                ),
                "gate_source_replay_consistent": source_replay_consistent,
                "gate_cell_contract_consistent": cell_contract_consistent,
                "passes_discovery_cell_gate": passes,
            }
        )

    passing_configs = _passing_global_configurations(cells)
    return {
        "passed": bool(passing_configs),
        "passing_config_ids": passing_configs,
        "unchanged_single_configuration_required": True,
        "full_canonical_universe_required": True,
        "full_canonical_universe_present": full_family_cells_present,
        "evaluation_window_valid": valid_evaluation_window,
        "evaluation_start_epoch": evaluation_start_epoch,
        "evaluation_end_epoch": evaluation_end_epoch,
        "expected_proxy_mapping_complete": proxy_mapping_complete,
        "prepared_source_mapping_complete": prepared_mapping_complete,
        "pair_direction_cells_per_configuration": (
            SIMULTANEOUS_PAIR_DIRECTION_CELLS
        ),
        "simultaneous_wilson_family_cells": SIMULTANEOUS_WILSON_FAMILY_CELLS,
        "minimum_scored_trades_per_cell": MIN_TRADES_PER_CELL,
        "minimum_independent_entry_days_per_cell": MIN_INDEPENDENT_ENTRY_DAYS,
        "minimum_observed_full_target_reservation_rate": (
            MIN_OBSERVED_FULL_TARGET_RATE
        ),
        "minimum_simultaneous_wilson_lower_bound": (
            MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
        ),
        "minimum_all_win_reservations_for_wilson": (
            MIN_ALL_WIN_RESERVATIONS_FOR_WILSON
        ),
        "wilson_method": (
            "one-sided Bonferroni over all 288 cells in the fixed current family"
        ),
        "wilson_scope": (
            "current fixed family only; prior failed families are not reopened, "
            "while the reserved-UTC-day one-sample t gate uses a dependence-robust "
            "two-sided Bonferroni Student-t threshold over all 3954 cumulative "
            "attempts at the fixed minimum df=99"
        ),
        "minimum_gate_mean_r": 0.0,
        "reserved_utc_day_observation_unit": (
            "one_gate_pnl_r_per_unique_reserved_utc_entry_day"
        ),
        "reserved_utc_day_one_sample_t_multiplicity_method": (
            "dependence_robust_two_sided_bonferroni_student_t"
        ),
        "reserved_utc_day_bonferroni_family_cells": (
            IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
        ),
        "reserved_utc_day_bonferroni_family_alpha": (
            TWO_SIDED_BONFERRONI_FAMILY_ALPHA
        ),
        "reserved_utc_day_one_sample_t_degrees_of_freedom_floor": (
            BONFERRONI_STUDENT_T_MIN_DF
        ),
        "minimum_reserved_utc_day_one_sample_t": (
            TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        ),
        "temporal_thirds_role": (
            "deterministic_discovery_only_robustness_veto_no_inferential_claim"
        ),
        "temporal_thirds_partition_formula": (
            "k=min(2,floor(3*(entry_epoch-evaluation_start_epoch)/"
            "(evaluation_end_epoch-evaluation_start_epoch)))"
        ),
        "temporal_thirds_half_open_boundary_epochs": (
            _temporal_thirds_boundary_epochs(
                evaluation_start_epoch=evaluation_start_epoch,
                evaluation_end_epoch=evaluation_end_epoch,
            )
        ),
        "temporal_thirds_include_all_exact_reservations": True,
        "temporal_thirds_include_adverse_unresolved": True,
        "temporal_thirds_pooling_or_selection_allowed": False,
        "temporal_thirds_minimum_reservations_per_segment": (
            MIN_RESERVATIONS_PER_TEMPORAL_THIRD
        ),
        "temporal_thirds_minimum_full_target_rate_numerator": (
            TEMPORAL_THIRD_WIN_RATE_NUMERATOR
        ),
        "temporal_thirds_minimum_full_target_rate_denominator": (
            TEMPORAL_THIRD_WIN_RATE_DENOMINATOR
        ),
        "temporal_thirds_minimum_gate_total_r_exclusive": 0.0,
        "temporal_thirds_inferential_claim_authorized": False,
        "unresolved_treatment": "non-win and adverse net stop-R",
        "scored_reservation_trade_ids_match": global_trade_identity_matches,
        "success_claim_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def _has_scorable_run(bars: Sequence[QuoteBar]) -> bool:
    minimum = BASELINE_M1_BARS + 1 + 1 + OUTCOME_HORIZON_M1_BARS
    return any(
        run_end - run_start >= minimum
        for run_start, run_end in _consecutive_runs(bars)
    )


def _valid_proxy_provenance(
    provenance: Mapping[str, Any] | None,
    *,
    evaluation_start_epoch: int,
) -> bool:
    if provenance is None or provenance.get("frozen") is not True:
        return False
    expected_keys = set(REQUIRED_PROXY_PROVENANCE_FIELDS) | {"frozen"}
    if set(provenance) != expected_keys:
        return False
    for field in REQUIRED_PROXY_PROVENANCE_FIELDS:
        value = provenance.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    if provenance.get("units") != "bps":
        return False
    snapshot_sha = str(provenance.get("source_snapshot_sha256") or "")
    if len(snapshot_sha) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in snapshot_sha
    ):
        return False
    try:
        as_of_epoch = _parse_epoch(str(provenance["as_of_utc"]))
        source_cutoff_epoch = _parse_epoch(str(provenance["source_cutoff_utc"]))
    except (TypeError, ValueError):
        return False
    return bool(
        as_of_epoch is not None
        and source_cutoff_epoch is not None
        and source_cutoff_epoch <= as_of_epoch
        and source_cutoff_epoch <= evaluation_start_epoch
    )


def _normalize_bars_mapping(
    bars_by_symbol: Mapping[str, Sequence[QuoteBar]],
) -> dict[str, Sequence[QuoteBar]]:
    normalized: dict[str, Sequence[QuoteBar]] = {}
    for raw_symbol, rows in bars_by_symbol.items():
        symbol = str(raw_symbol).strip().upper()
        if symbol not in FX_SYMBOLS:
            raise ValueError(f"noncanonical source symbol: {symbol}")
        if symbol in normalized:
            raise ValueError(f"duplicate normalized symbol: {symbol}")
        normalized[symbol] = rows
    return normalized


def _normalize_proxy_budgets(
    proxy_spread_budgets_bps: Mapping[str, float],
) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for raw_symbol, raw_value in proxy_spread_budgets_bps.items():
        symbol = str(raw_symbol).strip().upper()
        if symbol not in FX_SYMBOLS:
            raise ValueError(f"noncanonical proxy-budget symbol: {symbol}")
        if symbol in normalized:
            raise ValueError(f"duplicate normalized proxy-budget symbol: {symbol}")
        value = _valid_positive_number(raw_value)
        if value is None:
            raise ValueError(f"invalid proxy budget for {symbol}")
        normalized[symbol] = value
    return normalized


def screen_universe(
    *,
    bars_by_symbol: Mapping[str, Sequence[QuoteBar]],
    proxy_spread_budgets_bps: Mapping[str, float],
    proxy_provenance: Mapping[str, Any] | None,
    proxy_contract_schema_version: str,
    evaluation_start_utc: str,
    evaluation_end_utc: str,
    source_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    evaluation_start_epoch = _parse_epoch(evaluation_start_utc)
    evaluation_end_epoch = _parse_epoch(evaluation_end_utc)
    if (
        evaluation_start_epoch is None
        or evaluation_end_epoch is None
        or evaluation_end_epoch <= evaluation_start_epoch
    ):
        raise ValueError("a valid exclusive evaluation UTC interval is required")
    normalized_bars = _normalize_bars_mapping(bars_by_symbol)
    normalized_budgets = _normalize_proxy_budgets(proxy_spread_budgets_bps)
    errors = {
        str(key).strip().upper(): str(value)
        for key, value in (source_errors or {}).items()
    }
    provenance_ready = bool(
        proxy_contract_schema_version == PROXY_CONTRACT_SCHEMA_VERSION
        and _valid_proxy_provenance(
            proxy_provenance,
            evaluation_start_epoch=evaluation_start_epoch,
        )
    )
    cells: list[dict[str, Any]] = []
    all_reservations: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    prepared_series_by_symbol: dict[str, PreparedSeries] = {}
    source_failure_symbols: list[str] = []
    missing_proxy_budget_symbols: list[str] = []

    for symbol in FX_SYMBOLS:
        try:
            prepared = prepare_series(normalized_bars.get(symbol, ()))
        except ValueError as exc:
            prepared = prepare_series([])
            errors[symbol] = str(exc)
        if symbol in errors:
            # Explicit source failure always dominates accidentally supplied rows.
            prepared = prepare_series([])
        elif prepared.bars and prepared.bars[0].epoch < evaluation_start_epoch:
            errors[symbol] = "bar_before_evaluation_start"
        elif not prepared.bars:
            errors[symbol] = "empty_after_date_filter"
        elif not _has_scorable_run(prepared.bars):
            errors[symbol] = "no_complete_254_bar_m1_run"
        source_ready = symbol not in errors
        if not source_ready:
            source_failure_symbols.append(symbol)
            prepared = prepare_series([])
        prepared_series_by_symbol[symbol] = prepared

        budget = normalized_budgets.get(symbol)
        budget_ready = provenance_ready and budget is not None
        if not budget_ready:
            missing_proxy_budget_symbols.append(symbol)
        effective_budget = budget if budget_ready else None
        cost_mode = (
            "pair_specific_frozen_proxy_spread_stress"
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
                reservations = cell.pop("reservation_ledger")
                trades = cell.pop("trade_ledger")
                all_reservations.extend(reservations)
                all_trades.extend(trades)
                cell["reservation_event_ids"] = [
                    row["event_id"] for row in reservations
                ]
                cell["trade_event_ids"] = [row["event_id"] for row in trades]
                cells.append(cell)

    accounting = trial_accounting()
    discovery_gate = _apply_discovery_gate(
        cells=cells,
        reservation_ledger=all_reservations,
        trade_ledger=all_trades,
        evaluation_start_epoch=evaluation_start_epoch,
        evaluation_end_epoch=evaluation_end_epoch,
        expected_proxy_spread_budgets_bps=normalized_budgets,
        prepared_series_by_symbol=prepared_series_by_symbol,
    )
    return {
        "schema_version": "fxstack.scalp.range_displacement_trigger_screen.v1",
        "family": "range_displacement_trigger",
        "acronym": "RDT",
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
        "symbols": list(FX_SYMBOLS),
        "grid": [asdict(config) | {"config_id": config.config_id} for config in GRID],
        "fixed_contract": {
            "formula": (
                "BUY bid / SELL ask crosses the frozen L-bar quote-side range "
                "from inside with directional body >= d*median prior TR and "
                "directional close location >= q"
            ),
            "baseline": (
                "median of exactly 240 M1 true ranges ending t-1; one extra "
                "leading bar supplies the first previous close"
            ),
            "range": "quote-side L-bar high/low ending t-1",
            "spread": "Q25 of the exact same 240 pre-signal close spreads",
            "fill": "exact t+1 ask open BUY / bid open SELL",
            "outcome_horizon_m1_bars": OUTCOME_HORIZON_M1_BARS,
            "reserve_before_horizon_check": True,
            "unresolved_gate_treatment": (
                "non-win and adverse net stop-R; never dropped or replaced"
            ),
            "missing_exact_fill_treatment": (
                "qualifying closed signal reserves expected UTC entry day as "
                "an unresolved adverse stop; no same-day substitution"
            ),
            "one_trade_per_cell_entry_day": True,
            "stop": "opposite signal-bar quote extreme plus frozen 0.10 median-TR buffer",
            "stop_floor_bps": FROZEN_STOP_FLOOR_BPS,
            "max_risk_bps": MAX_RISK_BPS,
            "reward_risk": REWARD_RISK,
            "p_star_max": P_STAR_MAX,
            "p_star": (
                "net quote stop loss divided by net quote stop loss plus net "
                "quote target payoff after proxy-excess and one-bp debits"
            ),
            "min_target_cost_ratio": MIN_TARGET_COST_RATIO,
            "recorded_cost": (
                "max(signal spread, exact entry spread, frozen pair proxy) "
                "plus one-bp adverse round-trip pad"
            ),
            "scored_cost_debit": (
                "max(0, max(signal spread, entry spread, proxy) - entry spread) "
                "plus one bp; applied identically to p-star, PnL, and unresolved "
                "gate returns"
            ),
            "extra_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
            "ambiguous_bar": "stop_loss_first",
            "adverse_gap": "stop at observed quote open; target capped at target",
            "exit_quote": "bid_for_BUY_ask_for_SELL",
            "full_target_win": "positive_net_tp_or_tp_gap_open_only",
        },
        "cost_readiness": {
            "proxy_contract_ready": provenance_ready,
            "proxy_contract_schema_version": proxy_contract_schema_version,
            "proxy_provenance": dict(proxy_provenance or {}),
            "evaluation_start_utc": evaluation_start_utc,
            "source_cutoff_no_later_than_evaluation_start": provenance_ready,
            "missing_proxy_budget_symbols": sorted(
                set(missing_proxy_budget_symbols)
            ),
            "source_failure_symbols": sorted(set(source_failure_symbols)),
            "proxy_budgets_bps": {
                symbol: _valid_positive_number(
                    normalized_budgets.get(symbol)
                )
                for symbol in FX_SYMBOLS
            },
            "observed_source_bid_ask_used": True,
            "proxy_used_as_cost_stress": True,
            "proxy_excess_debited_in_pstar_and_pnl": True,
            "extra_adverse_round_trip_cost_bps": (
                FROZEN_EXTRA_ROUND_TRIP_COST_BPS
            ),
        },
        "search_accounting": accounting
        | {
            "family_alpha": TWO_SIDED_BONFERRONI_FAMILY_ALPHA,
            "two_sided_bonferroni_student_t_min_df99_abs_threshold": (
                TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
            ),
        },
        "discovery_gate": discovery_gate,
        "reservation_ledger": all_reservations,
        "trade_ledger": all_trades,
        "cells": cells,
    }


def _load_proxy_contract(
    path: Path, *, evaluation_start_utc: str
) -> tuple[dict[str, float], dict[str, Any], str]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_nonfinite_json_constant,
    )
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name}: expected an object")
    if set(payload) != {"schema_version", "provenance", "budgets_bps"}:
        raise ValueError(f"{path.name}: unexpected proxy contract schema")
    schema_version = payload.get("schema_version")
    if schema_version != PROXY_CONTRACT_SCHEMA_VERSION:
        raise ValueError(f"{path.name}: unexpected proxy contract version")
    provenance = payload.get("provenance")
    budgets_payload = payload.get("budgets_bps")
    evaluation_start_epoch = _parse_epoch(evaluation_start_utc)
    if evaluation_start_epoch is None:
        raise ValueError("evaluation_start_utc is required")
    if not isinstance(provenance, dict) or not _valid_proxy_provenance(
        provenance,
        evaluation_start_epoch=evaluation_start_epoch,
    ):
        raise ValueError(f"{path.name}: missing frozen proxy provenance")
    if not isinstance(budgets_payload, dict):
        raise ValueError(f"{path.name}: missing budgets_bps object")
    budgets = _normalize_proxy_budgets(budgets_payload)
    return budgets, dict(provenance), str(schema_version)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv-root", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--proxy-budgets-json", required=True)
    parser.add_argument("--reservation-ledger-out", required=True)
    parser.add_argument("--trade-ledger-out", required=True)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args(argv)

    reservation_output = Path(args.reservation_ledger_out).resolve()
    trade_output = Path(args.trade_ledger_out).resolve()
    output = Path(args.json_out).resolve()
    outputs = {
        "--reservation-ledger-out": reservation_output,
        "--trade-ledger-out": trade_output,
        "--json-out": output,
    }
    if len(set(outputs.values())) != len(outputs):
        parser.error("reservation, trade, and cell outputs require distinct paths")
    for option, path in outputs.items():
        if path.exists():
            parser.error(f"{option} already exists; refusing to overwrite")

    csv_root = Path(args.csv_root)
    proxy_path = Path(args.proxy_budgets_json)
    proxy_budgets, proxy_provenance, proxy_schema_version = _load_proxy_contract(
        proxy_path,
        evaluation_start_utc=args.start,
    )
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
            bars_by_symbol[symbol] = load_m1_csv(
                path, start=args.start, end=args.end
            )
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
        proxy_contract_schema_version=proxy_schema_version,
        evaluation_start_utc=args.start,
        evaluation_end_utc=args.end,
        source_errors=source_errors,
    )
    result["input_metadata"] = {
        "csv_root_recorded": False,
        "start": args.start,
        "end_exclusive": args.end,
        "proxy_contract_sha256": _sha256(proxy_path),
        "m1_csv_sha256_by_symbol": source_sha256,
    }
    reservation_payload = {
        "schema_version": (
            "fxstack.scalp.range_displacement_trigger_reservation_ledger.v1"
        ),
        "family": result["family"],
        "reservations": result.pop("reservation_ledger"),
    }
    trade_payload = {
        "schema_version": (
            "fxstack.scalp.range_displacement_trigger_trade_ledger.v1"
        ),
        "family": result["family"],
        "trades": result.pop("trade_ledger"),
    }
    for path, payload in (
        (reservation_output, reservation_payload),
        (trade_output, trade_payload),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, indent=1, sort_keys=True, allow_nan=False)
            )
    result["reservation_ledger_evidence"] = {
        "path_recorded": False,
        "reservations": len(reservation_payload["reservations"]),
        "file_sha256": _sha256(reservation_output),
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
        f"RDT: {len(result['cells'])} cells; "
        f"{accounting['current_attempted_cells']} current / "
        f"{accounting['cumulative_attempted_cells']} cumulative; "
        f"reservations={len(reservation_payload['reservations'])}; "
        f"trades={len(trade_payload['trades'])}; research_only=True"
    )
    readiness = result["cost_readiness"]
    return (
        0
        if not readiness["source_failure_symbols"]
        and not readiness["missing_proxy_budget_symbols"]
        and readiness["proxy_contract_ready"] is True
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
