# AGENT: ROLE: Research-only Rolling Sign-Transition State M1 screen.
# AGENT: ENTRYPOINT: `screen_universe`; CLI `python -m fxstack.scalp.screen_rolling_sign_transition_state`.
# AGENT: PRIMARY INPUTS: immutable bid/ask M1 CSV snapshots and one frozen proxy-cost contract.
# AGENT: PRIMARY OUTPUTS: all fixed cells plus complete reservation and scored-trade ledgers.
# AGENT ISOLATION: advisory discovery evidence only; never authorizes success or activation.
"""Causal M1 screen for the symmetric Rolling Sign-Transition State (RSTS).

For a closed signal bar ``t``, a consecutive 240-bar midpoint true-range and
close-spread baseline ends at ``t-1``; one additional prior close supplies the
first true range.  The last 120 midpoint-close return signs ending at ``t-1``
form 119 adjacent transitions.  Transitions touching a zero sign are excluded,
at least 80 active transitions are required, and
``a=(same-opposite)/(same+opposite)`` measures persistence versus reversal.
The signal score is ``F=a*x_t``, where ``x_t`` is the sign of the closed ``t``
return.  The fixed grid is ``theta in {0.05, 0.10}``; BUY requires
``F >= theta`` and SELL requires ``F <= -theta``.

Execution is exactly at the observed ``t+1`` opening quote.  Missing exact
fills and incomplete 20-bar horizons are adverse non-win reservations, and the
first reservation for a cell/UTC entry day cannot be replaced.  Observed
bid/ask quotes, a frozen pair proxy, and an adverse one-bp round-trip pad are
included.  Stops use the three signal-side quote bars ``t-2:t`` plus a frozen
0.10-volatility buffer, with 4.5-bp floor, 25-bp cap, and a fixed 1R target.

This permanent research artifact has no success, holdout, activation,
registry-write, runtime, bridge, broker, or order authority.
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
RETURN_SIGN_WINDOW = 120
RETURN_SIGN_TRANSITIONS = RETURN_SIGN_WINDOW - 1
MIN_ACTIVE_TRANSITIONS = 80
MIN_SIGNAL_RETURN_VOL = 0.10
MAX_SIGNAL_TRUE_RANGE_VOL = 3.0
SWING_STOP_M1_BARS = 3
STOP_BUFFER_VOL = 0.10
FROZEN_STOP_FLOOR_BPS = 4.5
MAX_RISK_BPS = 25.0
REWARD_RISK = 1.0
P_STAR_MAX = 0.55
MIN_TARGET_COST_RATIO = 4.0
FROZEN_EXTRA_ROUND_TRIP_COST_BPS = 1.0
OUTCOME_HORIZON_M1_BARS = 20
FILL_DELAY_M1_BARS = 1

IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 4_242
IMMUTABLE_CURRENT_ATTEMPTED_CELLS = 72
IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 4_314
TWO_SIDED_BONFERRONI_FAMILY_ALPHA = 0.05
BONFERRONI_STUDENT_T_MIN_DF = 99
TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD = 4.619583862948160
PROXY_CONTRACT_SCHEMA_VERSION = "fxstack.scalp.pair_proxy_spread_contract.v1"

MIN_TRADES_PER_CELL = 100
MIN_UNIQUE_RESERVED_UTC_ENTRY_DAYS = 100
MIN_OBSERVED_FULL_TARGET_RATE = 0.90
MIN_SIMULTANEOUS_WILSON_LOWER_BOUND = 0.90
SIMULTANEOUS_PAIR_DIRECTION_CELLS = len(FX_SYMBOLS) * 2
SIMULTANEOUS_WILSON_FAMILY_CELLS = IMMUTABLE_CURRENT_ATTEMPTED_CELLS
MIN_ALL_WIN_RESERVATIONS_FOR_WILSON = 92

TEMPORAL_THIRDS = 3
MIN_RESERVATIONS_PER_TEMPORAL_THIRD = 30
TEMPORAL_THIRD_WIN_RATE_NUMERATOR = 9
TEMPORAL_THIRD_WIN_RATE_DENOMINATOR = 10
CALENDAR_MONTH_BOUNDARY_EPOCHS: tuple[int, ...] = (
    1_672_531_200,
    1_675_209_600,
    1_677_628_800,
    1_680_307_200,
    1_682_899_200,
    1_685_577_600,
    1_688_169_600,
)
CALENDAR_MONTHS = len(CALENDAR_MONTH_BOUNDARY_EPOCHS) - 1
MIN_RESERVATIONS_PER_CALENDAR_MONTH = 12
CALENDAR_MONTH_WIN_RATE_NUMERATOR = 9
CALENDAR_MONTH_WIN_RATE_DENOMINATOR = 10

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
class RSTSConfig:
    forecast_threshold: float

    @property
    def config_id(self) -> str:
        return f"theta{int(round(self.forecast_threshold * 100.0)):02d}"


GRID: tuple[RSTSConfig, ...] = tuple(RSTSConfig(value) for value in (0.05, 0.10))
RSTS_CONFIG_BY_ID: dict[str, RSTSConfig] = {config.config_id: config for config in GRID}


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
    active_transitions: int
    same_sign_transitions: int
    opposite_sign_transitions: int
    transition_state: float


@dataclass(frozen=True, slots=True)
class RSTSClosedSignal:
    config_id: str
    symbol: str
    side: str
    signal_index: int
    signal_epoch: int
    expected_entry_index: int
    expected_entry_epoch: int
    entry_day: str
    volatility_bps: float
    spread_q25_bps: float
    proxy_budget_bps: float
    active_transitions: int
    same_sign_transitions: int
    opposite_sign_transitions: int
    transition_state: float
    signal_return_bps: float
    signal_sign: int
    forecast_score: float
    current_true_range_bps: float
    current_true_range_vol_units: float
    structural_stop: float
    signal_spread_bps: float


@dataclass(frozen=True, slots=True)
class RSTSSignal:
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
    spread_q25_bps: float
    proxy_budget_bps: float
    active_transitions: int
    same_sign_transitions: int
    opposite_sign_transitions: int
    transition_state: float
    signal_return_bps: float
    signal_sign: int
    forecast_score: float
    current_true_range_bps: float
    current_true_range_vol_units: float
    structural_stop: float
    signal_spread_bps: float
    entry_spread_bps: float
    extra_round_trip_cost_bps: float


@dataclass(frozen=True, slots=True)
class RSTSTrade:
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


RSTS_SIGNAL_FIELD_NAMES = frozenset(RSTSSignal.__dataclass_fields__)
RSTS_TRADE_FIELD_NAMES = frozenset(RSTSTrade.__dataclass_fields__)
RSTS_RESERVATION_OUTCOME_FIELD_NAMES = frozenset(
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
RSTS_RESERVATION_FIELD_NAMES = (
    RSTS_SIGNAL_FIELD_NAMES | RSTS_RESERVATION_OUTCOME_FIELD_NAMES
)
RSTS_MISSING_FILL_RESERVATION_FIELD_NAMES = RSTS_RESERVATION_FIELD_NAMES | {
    "gate_risk_basis_bps"
}
RSTS_CELL_PRE_GATE_FIELD_NAMES = frozenset(
    {
        "config_id",
        "forecast_threshold",
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
        "minimum_active_transitions",
        "minimum_signal_return_vol",
        "maximum_signal_true_range_vol",
        "swing_stop_bars",
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
RSTS_CELL_GATE_FIELD_NAMES = frozenset(
    {
        "unique_reserved_utc_entry_days",
        "gate_full_target_reservation_rate",
        "simultaneous_wilson_lower_bound",
        "reserved_utc_day_one_sample_t",
        "temporal_thirds_reservation_counts",
        "temporal_thirds_full_target_wins",
        "temporal_thirds_full_target_rates",
        "temporal_thirds_gate_total_rs",
        "temporal_thirds_gate_mean_rs",
        "gate_temporal_thirds_stable",
        "calendar_month_reservation_counts",
        "calendar_month_full_target_wins",
        "calendar_month_full_target_rates",
        "calendar_month_gate_total_rs",
        "calendar_month_gate_mean_rs",
        "gate_calendar_months_stable",
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
    active_transitions_before_signal: list[int | None]
    same_transitions_before_signal: list[int | None]
    opposite_transitions_before_signal: list[int | None]
    transition_state_before_signal: list[float | None]


def _spread_bps(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2.0
    if mid <= 0.0 or ask < bid:
        return 0.0
    return (ask - bid) / mid * 1e4


def _valid_positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


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
    if (
        parsed.microsecond != 0
        or not math.isfinite(timestamp)
        or not timestamp.is_integer()
    ):
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
    last_epoch: int | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        if tuple(next(reader, None) or ()) != CSV_HEADER:
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
            if last_epoch is not None and epoch <= last_epoch:
                raise ValueError(f"{path.name}:{row_number}: non-monotonic timestamp")
            last_epoch = epoch
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


def _nearest_rank_quantile_sorted(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    if not 0.0 < q <= 1.0:
        raise ValueError("nearest-rank q must be in (0, 1]")
    rank = max(1, math.ceil(q * len(values)))
    return float(values[rank - 1])


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


def _mid_close_return_bps(previous: QuoteBar, current: QuoteBar) -> float:
    if previous.mid_c <= 0.0 or current.mid_c <= 0.0:
        return 0.0
    return math.log(current.mid_c / previous.mid_c) * 1e4


def _return_sign(return_bps: float) -> int:
    if return_bps > 0.0:
        return 1
    if return_bps < 0.0:
        return -1
    return 0


def _transition_kind(left: int, right: int) -> tuple[int, int]:
    if left == 0 or right == 0:
        return 0, 0
    return (1, 0) if left == right else (0, 1)


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
    list[int | None],
    list[int | None],
    list[int | None],
    list[float | None],
]:
    """Build the 240-bar and 120-sign context ending at t-1 only."""

    size = len(bars)
    volatility: list[float | None] = [None] * size
    spread_q25: list[float | None] = [None] * size
    active_rows: list[int | None] = [None] * size
    same_rows: list[int | None] = [None] * size
    opposite_rows: list[int | None] = [None] * size
    state_rows: list[float | None] = [None] * size
    for run_start, run_end in _consecutive_runs(bars):
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
        signs = deque(
            _return_sign(_mid_close_return_bps(bars[index - 1], bars[index]))
            for index in range(first_signal - RETURN_SIGN_WINDOW, first_signal)
        )
        same = 0
        opposite = 0
        sign_list = list(signs)
        for left, right in zip(sign_list, sign_list[1:]):
            same_delta, opposite_delta = _transition_kind(left, right)
            same += same_delta
            opposite += opposite_delta
        for signal_index in range(first_signal, run_end):
            median_tr = statistics.median(tr_ordered)
            q25 = _nearest_rank_quantile_sorted(spread_ordered, 0.25)
            active = same + opposite
            if (
                math.isfinite(median_tr)
                and median_tr > 0.0
                and math.isfinite(q25)
                and q25 >= 0.0
                and active >= MIN_ACTIVE_TRANSITIONS
            ):
                volatility[signal_index] = median_tr
                spread_q25[signal_index] = q25
                active_rows[signal_index] = active
                same_rows[signal_index] = same
                opposite_rows[signal_index] = opposite
                state_rows[signal_index] = (same - opposite) / active

            if signal_index + 1 >= run_end:
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

            old_first = signs.popleft()
            new_first = signs[0]
            same_delta, opposite_delta = _transition_kind(old_first, new_first)
            same -= same_delta
            opposite -= opposite_delta
            new_sign = _return_sign(
                _mid_close_return_bps(bars[signal_index - 1], bars[signal_index])
            )
            same_delta, opposite_delta = _transition_kind(signs[-1], new_sign)
            same += same_delta
            opposite += opposite_delta
            signs.append(new_sign)
    return (
        volatility,
        spread_q25,
        active_rows,
        same_rows,
        opposite_rows,
        state_rows,
    )


def prepare_series(bars: Iterable[QuoteBar]) -> PreparedSeries:
    rows = list(bars)
    for index, bar in enumerate(rows):
        if not validate_quote_bar(bar):
            raise ValueError(f"invalid quote bar at index {index}")
        if index and bar.epoch <= rows[index - 1].epoch:
            raise ValueError("M1 timestamps must be strictly increasing")
    context = _build_pre_signal_context(rows)
    return PreparedSeries(rows, *context)


def baseline_context_at(
    prepared: PreparedSeries, *, signal_index: int
) -> BaselineContext | None:
    if signal_index < 0 or signal_index >= len(prepared.bars):
        return None
    values = (
        prepared.volatility_before_signal[signal_index],
        prepared.spread_q25_before_signal[signal_index],
        prepared.active_transitions_before_signal[signal_index],
        prepared.same_transitions_before_signal[signal_index],
        prepared.opposite_transitions_before_signal[signal_index],
        prepared.transition_state_before_signal[signal_index],
    )
    volatility, q25, active, same, opposite, state = values
    if (
        volatility is None
        or q25 is None
        or active is None
        or same is None
        or opposite is None
        or state is None
        or not math.isfinite(volatility)
        or volatility <= 0.0
        or not math.isfinite(q25)
        or q25 < 0.0
        or active < MIN_ACTIVE_TRANSITIONS
        or active != same + opposite
        or not math.isfinite(state)
    ):
        return None
    return BaselineContext(
        volatility_bps=float(volatility),
        spread_q25_bps=float(q25),
        active_transitions=int(active),
        same_sign_transitions=int(same),
        opposite_sign_transitions=int(opposite),
        transition_state=float(state),
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
    config: RSTSConfig,
    proxy_spread_budget_bps: float | None,
) -> tuple[RSTSClosedSignal | None, str]:
    """Evaluate the closed-t state without reading or requiring t+1."""

    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    budget = _valid_positive_number(proxy_spread_budget_bps)
    if budget is None:
        return None, "proxy_cost_unavailable"
    if signal_index < 0 or signal_index >= len(prepared.bars):
        return None, "signal_bar_unavailable"
    context = baseline_context_at(prepared, signal_index=signal_index)
    if context is None:
        return None, "strict_pre_signal_context_unavailable"
    signal_bar = prepared.bars[signal_index]
    previous_bar = prepared.bars[signal_index - 1]
    signal_spread = signal_bar.spread_close_bps
    spread_cap = min(context.spread_q25_bps, budget)
    if signal_spread > spread_cap + 1e-12:
        return None, "signal_spread_above_q25_or_proxy"

    signal_return = _mid_close_return_bps(previous_bar, signal_bar)
    signal_sign = _return_sign(signal_return)
    if signal_sign == 0:
        return None, "zero_signal_return"
    if abs(signal_return) + 1e-12 < MIN_SIGNAL_RETURN_VOL * context.volatility_bps:
        return None, "signal_return_too_small"
    current_tr = _true_range_bps(previous_bar, signal_bar)
    current_tr_units = current_tr / context.volatility_bps
    if (
        not math.isfinite(current_tr_units)
        or current_tr_units > MAX_SIGNAL_TRUE_RANGE_VOL + 1e-12
    ):
        return None, "signal_true_range_too_large"
    forecast_score = context.transition_state * signal_sign
    if side == "BUY" and forecast_score + 1e-12 < config.forecast_threshold:
        return None, "forecast_score_below_buy_threshold"
    if side == "SELL" and forecast_score - 1e-12 > -config.forecast_threshold:
        return None, "forecast_score_above_sell_threshold"

    swing = prepared.bars[signal_index - SWING_STOP_M1_BARS + 1 : signal_index + 1]
    structural_stop = (
        min(bar.bid_l for bar in swing)
        if side == "BUY"
        else max(bar.ask_h for bar in swing)
    )
    expected_entry_epoch = signal_bar.epoch + 60
    return (
        RSTSClosedSignal(
            config_id=config.config_id,
            symbol=str(symbol).upper(),
            side=side,
            signal_index=signal_index,
            signal_epoch=signal_bar.epoch,
            expected_entry_index=signal_index + FILL_DELAY_M1_BARS,
            expected_entry_epoch=expected_entry_epoch,
            entry_day=_utc_day(expected_entry_epoch),
            volatility_bps=context.volatility_bps,
            spread_q25_bps=context.spread_q25_bps,
            proxy_budget_bps=budget,
            active_transitions=context.active_transitions,
            same_sign_transitions=context.same_sign_transitions,
            opposite_sign_transitions=context.opposite_sign_transitions,
            transition_state=context.transition_state,
            signal_return_bps=signal_return,
            signal_sign=signal_sign,
            forecast_score=forecast_score,
            current_true_range_bps=current_tr,
            current_true_range_vol_units=current_tr_units,
            structural_stop=structural_stop,
            signal_spread_bps=signal_spread,
        ),
        "",
    )


def _complete_signal_at_exact_next_open(
    *, prepared: PreparedSeries, closed: RSTSClosedSignal
) -> tuple[RSTSSignal | None, str]:
    if closed.expected_entry_index >= len(prepared.bars):
        return None, "exact_next_open_unavailable"
    next_bar = prepared.bars[closed.expected_entry_index]
    if next_bar.epoch != closed.expected_entry_epoch:
        return None, "exact_next_open_gap"
    entry_spread = next_bar.spread_open_bps
    if entry_spread > min(closed.spread_q25_bps, closed.proxy_budget_bps) + 1e-12:
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
    spread_stress = max(closed.signal_spread_bps, entry_spread, closed.proxy_budget_bps)
    incremental_stress = max(0.0, spread_stress - entry_spread)
    execution_debit = incremental_stress + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    recorded_cost = entry_spread + execution_debit
    p_star = _bracket_p_star(risk_bps, execution_cost_debit_bps=execution_debit)
    if not math.isfinite(p_star) or p_star > P_STAR_MAX + 1e-12:
        return None, "bracket_cost_dead"
    if quote_target_bps + 1e-12 < MIN_TARGET_COST_RATIO * recorded_cost:
        return None, "target_too_small_vs_cost"
    return (
        RSTSSignal(
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
            recorded_cost_bps=recorded_cost,
            incremental_spread_stress_bps=incremental_stress,
            execution_cost_debit_bps=execution_debit,
            p_star=p_star,
            volatility_bps=closed.volatility_bps,
            spread_q25_bps=closed.spread_q25_bps,
            proxy_budget_bps=closed.proxy_budget_bps,
            active_transitions=closed.active_transitions,
            same_sign_transitions=closed.same_sign_transitions,
            opposite_sign_transitions=closed.opposite_sign_transitions,
            transition_state=closed.transition_state,
            signal_return_bps=closed.signal_return_bps,
            signal_sign=closed.signal_sign,
            forecast_score=closed.forecast_score,
            current_true_range_bps=closed.current_true_range_bps,
            current_true_range_vol_units=closed.current_true_range_vol_units,
            structural_stop=closed.structural_stop,
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
    config: RSTSConfig,
    proxy_spread_budget_bps: float | None,
) -> tuple[RSTSSignal | None, str]:
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
    *, config_id: str, symbol: str, side: str, signal_epoch: int, entry_epoch: int
) -> str:
    raw = f"rolling_sign_transition_state|{config_id}|{symbol}|{side}|{signal_epoch}|{entry_epoch}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _event_id(signal: RSTSSignal) -> str:
    return _event_id_fields(
        config_id=signal.config_id,
        symbol=signal.symbol,
        side=signal.side,
        signal_epoch=signal.signal_epoch,
        entry_epoch=signal.entry_epoch,
    )


def _trade_result(
    signal: RSTSSignal,
    *,
    exit_bar: QuoteBar,
    exit_price: float,
    bars_held: int,
    reason: str,
) -> RSTSTrade:
    gross_bps = (
        (exit_price - signal.entry_price) / signal.entry_price * 1e4
        if signal.side == "BUY"
        else (signal.entry_price - exit_price) / signal.entry_price * 1e4
    )
    pnl_bps = gross_bps - signal.execution_cost_debit_bps
    full_target = reason in {"tp", "tp_gap_open"} and pnl_bps > 0.0
    return RSTSTrade(
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


def simulate_trade(bars: Sequence[QuoteBar], *, signal: RSTSSignal) -> RSTSTrade | None:
    if not outcome_horizon_is_complete(bars, entry_index=signal.entry_index):
        return None
    for offset in range(OUTCOME_HORIZON_M1_BARS):
        bar = bars[signal.entry_index + offset]
        buy = signal.side == "BUY"
        exit_open = bar.bid_o if buy else bar.ask_o
        stop_gap = (
            exit_open <= signal.stop_price if buy else exit_open >= signal.stop_price
        )
        if stop_gap:
            return _trade_result(
                signal,
                exit_bar=bar,
                exit_price=exit_open,
                bars_held=offset + 1,
                reason="sl_gap_open",
            )
        target_gap = (
            exit_open >= signal.target_price
            if buy
            else exit_open <= signal.target_price
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
        stop_hit = (
            exit_low <= signal.stop_price if buy else exit_high >= signal.stop_price
        )
        target_hit = (
            exit_high >= signal.target_price if buy else exit_low <= signal.target_price
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
    signal: RSTSSignal, *, status: str, trade: RSTSTrade | None
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
        "outcome_reason": trade.exit_reason
        if trade is not None
        else "incomplete_outcome_horizon",
        "exit_epoch": trade.exit_epoch if trade is not None else None,
        "exit_price": trade.exit_price if trade is not None else None,
        "bars_held": trade.bars_held if trade is not None else None,
        "pnl_bps": trade.pnl_bps if trade is not None else None,
        "pnl_r": trade.pnl_r if trade is not None else None,
        "gate_pnl_r": gate_pnl_r,
        "gate_treatment": "observed_trade"
        if trade is not None
        else "unresolved_as_adverse_stop_for_discovery_gate",
        "full_target_win": trade.full_target_win if trade is not None else False,
        "positive_outcome": trade.positive_outcome if trade is not None else False,
    }


def _missing_fill_reservation_row(
    closed: RSTSClosedSignal, *, reason: str
) -> dict[str, Any]:
    conservative_debit = closed.proxy_budget_bps + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
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
        "execution_cost_debit_bps": conservative_debit,
        "p_star": None,
        "volatility_bps": closed.volatility_bps,
        "spread_q25_bps": closed.spread_q25_bps,
        "proxy_budget_bps": closed.proxy_budget_bps,
        "active_transitions": closed.active_transitions,
        "same_sign_transitions": closed.same_sign_transitions,
        "opposite_sign_transitions": closed.opposite_sign_transitions,
        "transition_state": closed.transition_state,
        "signal_return_bps": closed.signal_return_bps,
        "signal_sign": closed.signal_sign,
        "forecast_score": closed.forecast_score,
        "current_true_range_bps": closed.current_true_range_bps,
        "current_true_range_vol_units": closed.current_true_range_vol_units,
        "structural_stop": closed.structural_stop,
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
        "gate_pnl_r": -(gate_risk_basis + conservative_debit) / gate_risk_basis,
        "gate_treatment": "unresolved_missing_fill_as_adverse_stop_for_discovery_gate",
        "gate_risk_basis_bps": gate_risk_basis,
        "full_target_win": False,
        "positive_outcome": False,
    }


def screen_cell(
    *,
    prepared: PreparedSeries,
    symbol: str,
    side: str,
    config: RSTSConfig,
    proxy_spread_budget_bps: float | None,
) -> dict[str, Any]:
    trades: list[RSTSTrade] = []
    reservations: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    reserved_days: set[str] = set()
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
        if closed.entry_day in reserved_days:
            reasons["entry_day_already_reserved"] += 1
            continue
        signal, reason = _complete_signal_at_exact_next_open(
            prepared=prepared, closed=closed
        )
        if signal is None:
            reasons[reason] += 1
            if reason not in {"exact_next_open_unavailable", "exact_next_open_gap"}:
                continue
            eligible_events += 1
            reserved_days.add(closed.entry_day)
            reservations.append(_missing_fill_reservation_row(closed, reason=reason))
            continue
        eligible_events += 1
        reserved_days.add(signal.entry_day)
        if not outcome_horizon_is_complete(
            prepared.bars, entry_index=signal.entry_index
        ):
            reasons["incomplete_outcome_horizon"] += 1
            reservations.append(
                _reservation_row(signal, status="unresolved", trade=None)
            )
            continue
        trade = simulate_trade(prepared.bars, signal=signal)
        if trade is None:
            raise RuntimeError("complete horizon unexpectedly failed to score")
        trades.append(trade)
        reservations.append(_reservation_row(signal, status="scored", trade=trade))
    wins = sum(trade.full_target_win for trade in trades)
    positives = sum(trade.positive_outcome for trade in trades)
    pnl_rs = [trade.pnl_r for trade in trades]
    gate_rs = [float(row["gate_pnl_r"]) for row in reservations]
    unresolved = sum(row["reservation_status"] == "unresolved" for row in reservations)
    return {
        "config_id": config.config_id,
        "forecast_threshold": config.forecast_threshold,
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "closed_signal_events": closed_signal_events,
        "eligible_events": eligible_events,
        "entry_day_reservations": len(reservations),
        "unresolved_reservations": unresolved,
        "scored_trades": len(trades),
        "full_target_wins": wins,
        "full_target_trade_win_rate": wins / len(trades) if trades else 0.0,
        "full_target_reservation_rate": wins / len(reservations)
        if reservations
        else 0.0,
        "positive_outcomes": positives,
        "positive_outcome_rate": positives / len(trades) if trades else 0.0,
        "total_r": math.fsum(pnl_rs),
        "mean_r": statistics.fmean(pnl_rs) if pnl_rs else 0.0,
        "gate_total_r": math.fsum(gate_rs),
        "gate_mean_r": statistics.fmean(gate_rs) if gate_rs else 0.0,
        "exit_mix": dict(Counter(trade.exit_reason for trade in trades)),
        "reasons": dict(sorted(reasons.items())),
        "reservation_ledger": reservations,
        "trade_ledger": [asdict(trade) for trade in trades],
        "one_trade_per_cell_entry_day": True,
        "outcome_horizon_bars": OUTCOME_HORIZON_M1_BARS,
        "reward_risk": REWARD_RISK,
        "stop_floor_bps": FROZEN_STOP_FLOOR_BPS,
        "max_risk_bps": MAX_RISK_BPS,
        "p_star_max": P_STAR_MAX,
        "min_target_cost_ratio": MIN_TARGET_COST_RATIO,
        "extra_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
        "minimum_active_transitions": MIN_ACTIVE_TRANSITIONS,
        "minimum_signal_return_vol": MIN_SIGNAL_RETURN_VOL,
        "maximum_signal_true_range_vol": MAX_SIGNAL_TRUE_RANGE_VOL,
        "swing_stop_bars": SWING_STOP_M1_BARS,
    }


def trial_accounting() -> dict[str, int]:
    current = len(GRID) * len(FX_SYMBOLS) * 2
    if current != IMMUTABLE_CURRENT_ATTEMPTED_CELLS:
        raise RuntimeError("RSTS fixed-grid accounting changed")
    if (
        IMMUTABLE_PRIOR_ATTEMPTED_CELLS + current
        != IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    ):
        raise RuntimeError("RSTS cumulative trial ledger is inconsistent")
    return {
        "grid_configurations": len(GRID),
        "directions": 2,
        "symbols": len(FX_SYMBOLS),
        "current_attempted_cells": current,
        "prior_attempted_cells": IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
        "cumulative_attempted_cells": IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS,
        "expected_full_universe_cells": current,
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
    z = statistics.NormalDist().inv_cdf(
        1.0 - float(family_alpha) / max(1, int(simultaneous_cells))
    )
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
        return 1e12 if mean > 0.0 else -1e12 if mean < 0.0 else 0.0
    return max(-1e12, min(1e12, mean / (deviation / math.sqrt(len(rows)))))


def _passing_global_configurations(cells: Sequence[Mapping[str, Any]]) -> list[str]:
    expected = {(symbol, side) for symbol in FX_SYMBOLS for side in ("BUY", "SELL")}
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


def _ledger_cell_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("config_id") or ""),
        str(row.get("symbol") or "").upper(),
        str(row.get("side") or "").upper(),
    )


def _exact_nonnegative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _exact_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _numbers_match(left: Any, right: Any) -> bool:
    left_value = _finite_float(left)
    right_value = _finite_float(right)
    return bool(
        left_value is not None
        and right_value is not None
        and math.isclose(left_value, right_value, rel_tol=1e-12, abs_tol=1e-12)
    )


def _temporal_third_index(
    entry_epoch: Any, *, evaluation_start_epoch: int, evaluation_end_epoch: int
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
    if evaluation_start_epoch < 0 or evaluation_end_epoch <= evaluation_start_epoch:
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
    valid = True
    for row in reservations:
        segment = _temporal_third_index(
            row.get("entry_epoch"),
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        )
        gate_return = _finite_float(row.get("gate_pnl_r"))
        win = row.get("full_target_win")
        if segment is None or gate_return is None or not isinstance(win, bool):
            valid = False
            continue
        counts[segment] += 1
        wins[segment] += int(win)
        returns[segment].append(gate_return)
    try:
        totals = [math.fsum(values) for values in returns]
    except (OverflowError, ValueError):
        totals = [0.0] * TEMPORAL_THIRDS
        valid = False
    rates = [wins[i] / counts[i] if counts[i] else 0.0 for i in range(TEMPORAL_THIRDS)]
    means = [
        totals[i] / counts[i] if counts[i] else 0.0 for i in range(TEMPORAL_THIRDS)
    ]
    stable = bool(
        valid
        and all(
            counts[i] >= MIN_RESERVATIONS_PER_TEMPORAL_THIRD
            and TEMPORAL_THIRD_WIN_RATE_DENOMINATOR * wins[i]
            >= TEMPORAL_THIRD_WIN_RATE_NUMERATOR * counts[i]
            and totals[i] > 0.0
            for i in range(TEMPORAL_THIRDS)
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


def _calendar_month_index(
    entry_epoch: Any, *, evaluation_start_epoch: int, evaluation_end_epoch: int
) -> int | None:
    epoch = _exact_int(entry_epoch)
    if (
        evaluation_start_epoch != CALENDAR_MONTH_BOUNDARY_EPOCHS[0]
        or evaluation_end_epoch != CALENDAR_MONTH_BOUNDARY_EPOCHS[-1]
        or epoch is None
        or not evaluation_start_epoch <= epoch <= evaluation_end_epoch
    ):
        return None
    if epoch == evaluation_end_epoch:
        return CALENDAR_MONTHS - 1
    index = bisect.bisect_right(CALENDAR_MONTH_BOUNDARY_EPOCHS, epoch) - 1
    return index if 0 <= index < CALENDAR_MONTHS else None


def _calendar_month_diagnostics(
    reservations: Sequence[Mapping[str, Any]],
    *,
    evaluation_start_epoch: int,
    evaluation_end_epoch: int,
) -> dict[str, Any]:
    counts = [0] * CALENDAR_MONTHS
    wins = [0] * CALENDAR_MONTHS
    returns: list[list[float]] = [[] for _ in range(CALENDAR_MONTHS)]
    valid = bool(
        evaluation_start_epoch == CALENDAR_MONTH_BOUNDARY_EPOCHS[0]
        and evaluation_end_epoch == CALENDAR_MONTH_BOUNDARY_EPOCHS[-1]
    )
    for row in reservations:
        segment = _calendar_month_index(
            row.get("entry_epoch"),
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        )
        gate_return = _finite_float(row.get("gate_pnl_r"))
        win = row.get("full_target_win")
        if segment is None or gate_return is None or not isinstance(win, bool):
            valid = False
            continue
        counts[segment] += 1
        wins[segment] += int(win)
        returns[segment].append(gate_return)
    try:
        totals = [math.fsum(values) for values in returns]
    except (OverflowError, ValueError):
        totals = [0.0] * CALENDAR_MONTHS
        valid = False
    rates = [wins[i] / counts[i] if counts[i] else 0.0 for i in range(CALENDAR_MONTHS)]
    means = [
        totals[i] / counts[i] if counts[i] else 0.0 for i in range(CALENDAR_MONTHS)
    ]
    stable = bool(
        valid
        and all(
            counts[i] >= MIN_RESERVATIONS_PER_CALENDAR_MONTH
            and CALENDAR_MONTH_WIN_RATE_DENOMINATOR * wins[i]
            >= CALENDAR_MONTH_WIN_RATE_NUMERATOR * counts[i]
            and totals[i] > 0.0
            for i in range(CALENDAR_MONTHS)
        )
    )
    return {
        "calendar_month_reservation_counts": counts,
        "calendar_month_full_target_wins": wins,
        "calendar_month_full_target_rates": rates,
        "calendar_month_gate_total_rs": totals,
        "calendar_month_gate_mean_rs": means,
        "gate_calendar_months_stable": stable,
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
        or signal_epoch < 0
        or signal_epoch % 60 != 0
        or entry_epoch != signal_epoch + 60
        or not isinstance(event_id, str)
        or not event_id
    ):
        return False
    try:
        day_matches = row.get("entry_day") == _utc_day(entry_epoch)
    except (OSError, OverflowError, ValueError):
        return False
    return bool(
        day_matches
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
    config = RSTS_CONFIG_BY_ID.get(key[0])
    volatility = _finite_float(row.get("volatility_bps"))
    spread_q25 = _finite_float(row.get("spread_q25_bps"))
    proxy = _finite_float(row.get("proxy_budget_bps"))
    active = _exact_nonnegative_int(row.get("active_transitions"))
    same = _exact_nonnegative_int(row.get("same_sign_transitions"))
    opposite = _exact_nonnegative_int(row.get("opposite_sign_transitions"))
    state = _finite_float(row.get("transition_state"))
    signal_return = _finite_float(row.get("signal_return_bps"))
    signal_sign = _exact_int(row.get("signal_sign"))
    score = _finite_float(row.get("forecast_score"))
    current_tr = _finite_float(row.get("current_true_range_bps"))
    current_units = _finite_float(row.get("current_true_range_vol_units"))
    structural_stop = _finite_float(row.get("structural_stop"))
    signal_spread = _finite_float(row.get("signal_spread_bps"))
    extra_cost = _finite_float(row.get("extra_round_trip_cost_bps"))
    if (
        config is None
        or volatility is None
        or volatility <= 0.0
        or spread_q25 is None
        or spread_q25 < 0.0
        or proxy is None
        or proxy <= 0.0
        or active is None
        or same is None
        or opposite is None
        or not MIN_ACTIVE_TRANSITIONS <= active <= RETURN_SIGN_TRANSITIONS
        or active != same + opposite
        or state is None
        or not -1.0 - 1e-12 <= state <= 1.0 + 1e-12
        or signal_return is None
        or signal_sign not in {-1, 1}
        or score is None
        or current_tr is None
        or current_tr < 0.0
        or current_units is None
        or structural_stop is None
        or structural_stop <= 0.0
        or signal_spread is None
        or signal_spread < 0.0
        or extra_cost is None
    ):
        return False
    spread_cap = min(spread_q25, proxy)
    expected_sign = _return_sign(signal_return)
    direction_passes = (
        score + 1e-12 >= config.forecast_threshold
        if key[2] == "BUY"
        else score - 1e-12 <= -config.forecast_threshold
    )
    return bool(
        _numbers_match(state, (same - opposite) / active)
        and expected_sign == signal_sign
        and _numbers_match(score, state * signal_sign)
        and direction_passes
        and abs(signal_return) + 1e-12 >= MIN_SIGNAL_RETURN_VOL * volatility
        and _numbers_match(current_units, current_tr / volatility)
        and current_units <= MAX_SIGNAL_TRUE_RANGE_VOL + 1e-12
        and signal_spread <= spread_cap + 1e-12
        and _numbers_match(extra_cost, FROZEN_EXTRA_ROUND_TRIP_COST_BPS)
    )


def _completed_signal_semantics_are_valid(
    row: Mapping[str, Any], *, key: tuple[str, str, str]
) -> bool:
    if not _signal_feature_semantics_are_valid(row, key=key):
        return False
    entry = _finite_float(row.get("entry_price"))
    stop = _finite_float(row.get("stop_price"))
    target = _finite_float(row.get("target_price"))
    raw_risk = _finite_float(row.get("raw_risk_bps"))
    risk = _finite_float(row.get("risk_bps"))
    quote_target = _finite_float(row.get("quote_target_bps"))
    recorded = _finite_float(row.get("recorded_cost_bps"))
    incremental = _finite_float(row.get("incremental_spread_stress_bps"))
    debit = _finite_float(row.get("execution_cost_debit_bps"))
    p_star = _finite_float(row.get("p_star"))
    signal_spread = _finite_float(row.get("signal_spread_bps"))
    entry_spread = _finite_float(row.get("entry_spread_bps"))
    proxy = _finite_float(row.get("proxy_budget_bps"))
    q25 = _finite_float(row.get("spread_q25_bps"))
    volatility = _finite_float(row.get("volatility_bps"))
    structural_stop = _finite_float(row.get("structural_stop"))
    if any(
        value is None
        for value in (
            entry,
            stop,
            target,
            raw_risk,
            risk,
            quote_target,
            recorded,
            incremental,
            debit,
            p_star,
            signal_spread,
            entry_spread,
            proxy,
            q25,
            volatility,
            structural_stop,
        )
    ):
        return False
    assert all(
        value is not None
        for value in (
            entry,
            stop,
            target,
            raw_risk,
            risk,
            quote_target,
            recorded,
            incremental,
            debit,
            p_star,
            signal_spread,
            entry_spread,
            proxy,
            q25,
            volatility,
            structural_stop,
        )
    )
    if (
        entry <= 0.0
        or stop <= 0.0
        or target <= 0.0
        or raw_risk <= 0.0
        or risk <= 0.0
        or risk > MAX_RISK_BPS + 1e-12
        or quote_target <= 0.0
        or recorded <= 0.0
        or incremental < 0.0
        or debit <= 0.0
        or p_star > P_STAR_MAX + 1e-12
        or entry_spread < 0.0
        or entry_spread > min(q25, proxy) + 1e-12
    ):
        return False
    expected_stress = max(signal_spread, entry_spread, proxy)
    expected_incremental = max(0.0, expected_stress - entry_spread)
    expected_debit = expected_incremental + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    expected_recorded = entry_spread + expected_debit
    expected_p_star = _bracket_p_star(risk, execution_cost_debit_bps=expected_debit)
    if key[2] == "BUY":
        raw_stop = structural_stop - STOP_BUFFER_VOL * volatility / 1e4 * entry
        expected_raw_risk = (entry - raw_stop) / entry * 1e4
        stop_risk = (entry - stop) / entry * 1e4
        target_distance = (target - entry) / entry * 1e4
        ordered = stop < entry < target
    else:
        raw_stop = structural_stop + STOP_BUFFER_VOL * volatility / 1e4 * entry
        expected_raw_risk = (raw_stop - entry) / entry * 1e4
        stop_risk = (stop - entry) / entry * 1e4
        target_distance = (entry - target) / entry * 1e4
        ordered = target < entry < stop
    return bool(
        ordered
        and math.isfinite(expected_p_star)
        and expected_raw_risk > 0.0
        and _numbers_match(raw_risk, expected_raw_risk)
        and _numbers_match(risk, _floored_risk_bps(raw_risk))
        and _numbers_match(stop_risk, risk)
        and _numbers_match(target_distance, risk * REWARD_RISK)
        and _numbers_match(quote_target, target_distance)
        and _numbers_match(incremental, expected_incremental)
        and _numbers_match(debit, expected_debit)
        and _numbers_match(recorded, expected_recorded)
        and _numbers_match(recorded, expected_stress + FROZEN_EXTRA_ROUND_TRIP_COST_BPS)
        and _numbers_match(p_star, expected_p_star)
        and quote_target + 1e-12 >= MIN_TARGET_COST_RATIO * recorded
    )


def _trade_semantics_are_valid(
    trade: Mapping[str, Any], *, key: tuple[str, str, str]
) -> bool:
    if set(trade) != RSTS_TRADE_FIELD_NAMES or not _event_identity_is_valid(
        trade, key=key, require_indices=False
    ):
        return False
    entry_epoch = _exact_int(trade.get("entry_epoch"))
    exit_epoch = _exact_int(trade.get("exit_epoch"))
    bars_held = _exact_int(trade.get("bars_held"))
    risk = _finite_float(trade.get("risk_bps"))
    pnl_bps = _finite_float(trade.get("pnl_bps"))
    pnl_r = _finite_float(trade.get("pnl_r"))
    reason = trade.get("exit_reason")
    if (
        entry_epoch is None
        or exit_epoch is None
        or exit_epoch < entry_epoch
        or (exit_epoch - entry_epoch) % 60 != 0
        or bars_held is None
        or bars_held != (exit_epoch - entry_epoch) // 60 + 1
        or not 1 <= bars_held <= OUTCOME_HORIZON_M1_BARS
        or risk is None
        or risk <= 0.0
        or pnl_bps is None
        or pnl_r is None
        or not _numbers_match(pnl_r, pnl_bps / risk)
        or reason
        not in {
            "sl_gap_open",
            "tp_gap_open",
            "sl_double_touch",
            "sl",
            "tp",
            "time_stop",
        }
    ):
        return False
    positive = pnl_bps > 0.0
    full_target = reason in {"tp", "tp_gap_open"} and positive
    return bool(
        (reason != "time_stop" or bars_held == OUTCOME_HORIZON_M1_BARS)
        and trade.get("positive_outcome") is positive
        and trade.get("full_target_win") is full_target
    )


def _scored_reservation_semantics_are_valid(
    reservation: Mapping[str, Any],
    *,
    trade: Mapping[str, Any] | None,
    key: tuple[str, str, str],
) -> bool:
    if (
        trade is None
        or set(reservation) != RSTS_RESERVATION_FIELD_NAMES
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
        if not _numbers_match(reservation.get(field), trade.get(field)):
            return False
    entry = _finite_float(reservation.get("entry_price"))
    exit_price = _finite_float(reservation.get("exit_price"))
    stop = _finite_float(reservation.get("stop_price"))
    target = _finite_float(reservation.get("target_price"))
    risk = _finite_float(reservation.get("risk_bps"))
    debit = _finite_float(reservation.get("execution_cost_debit_bps"))
    if any(value is None for value in (entry, exit_price, stop, target, risk, debit)):
        return False
    assert entry is not None and exit_price is not None and stop is not None
    assert target is not None and risk is not None and debit is not None
    if key[2] == "BUY":
        gross = (exit_price - entry) / entry * 1e4
        stop_risk = (entry - stop) / entry * 1e4
        target_distance = (target - entry) / entry * 1e4
        ordered = stop < entry < target
    else:
        gross = (entry - exit_price) / entry * 1e4
        stop_risk = (stop - entry) / entry * 1e4
        target_distance = (entry - target) / entry * 1e4
        ordered = target < entry < stop
    reason = reservation.get("outcome_reason")
    bracket_exit = bool(
        (reason not in {"tp", "tp_gap_open"} or _numbers_match(exit_price, target))
        and (
            reason not in {"sl", "sl_double_touch"} or _numbers_match(exit_price, stop)
        )
        and (
            reason != "sl_gap_open"
            or (key[2] == "BUY" and exit_price <= stop + 1e-12)
            or (key[2] == "SELL" and exit_price >= stop - 1e-12)
        )
        and (
            reason != "time_stop"
            or (key[2] == "BUY" and stop < exit_price < target)
            or (key[2] == "SELL" and target < exit_price < stop)
        )
    )
    return bool(
        reservation.get("exit_epoch") == trade.get("exit_epoch")
        and reservation.get("bars_held") == trade.get("bars_held")
        and ordered
        and bracket_exit
        and _numbers_match(stop_risk, risk)
        and _numbers_match(target_distance, risk * REWARD_RISK)
        and _numbers_match(reservation.get("quote_target_bps"), target_distance)
        and _numbers_match(reservation.get("pnl_bps"), gross - debit)
        and _numbers_match(reservation.get("gate_pnl_r"), trade.get("pnl_r"))
    )


def _unresolved_reservation_semantics_are_valid(
    reservation: Mapping[str, Any], *, key: tuple[str, str, str]
) -> bool:
    reason = reservation.get("outcome_reason")
    expected_schema = (
        RSTS_MISSING_FILL_RESERVATION_FIELD_NAMES
        if reason in {"exact_next_open_unavailable", "exact_next_open_gap"}
        else RSTS_RESERVATION_FIELD_NAMES
    )
    if (
        set(reservation) != expected_schema
        or reservation.get("reservation_status") != "unresolved"
        or not _signal_feature_semantics_are_valid(reservation, key=key)
        or reservation.get("full_target_win") is not False
        or reservation.get("positive_outcome") is not False
        or any(
            reservation.get(field) is not None
            for field in ("exit_epoch", "exit_price", "bars_held", "pnl_bps", "pnl_r")
        )
    ):
        return False
    gate_return = _finite_float(reservation.get("gate_pnl_r"))
    debit = _finite_float(reservation.get("execution_cost_debit_bps"))
    if gate_return is None or gate_return >= 0.0 or debit is None or debit <= 0.0:
        return False
    if reason == "incomplete_outcome_horizon":
        if not _completed_signal_semantics_are_valid(reservation, key=key):
            return False
        risk_basis = _finite_float(reservation.get("risk_bps"))
        treatment = "unresolved_as_adverse_stop_for_discovery_gate"
    elif reason in {"exact_next_open_unavailable", "exact_next_open_gap"}:
        risk_basis = _finite_float(reservation.get("gate_risk_basis_bps"))
        treatment = "unresolved_missing_fill_as_adverse_stop_for_discovery_gate"
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
        proxy = _finite_float(reservation.get("proxy_budget_bps"))
        if (
            proxy is None
            or not _numbers_match(risk_basis, FROZEN_STOP_FLOOR_BPS)
            or not _numbers_match(debit, proxy + FROZEN_EXTRA_ROUND_TRIP_COST_BPS)
        ):
            return False
    else:
        return False
    if (
        risk_basis is None
        or risk_basis <= 0.0
        or reservation.get("gate_treatment") != treatment
    ):
        return False
    return _numbers_match(gate_return, -(risk_basis + debit) / risk_basis)


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
            if not _numbers_match(actual_value, expected_value):
                return False
        elif actual_value != expected_value:
            return False
    return True


def _cell_matches_complete_source_replay(
    *,
    cell: Mapping[str, Any],
    reservations: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    key: tuple[str, str, str],
    prepared: PreparedSeries | None,
    proxy_budget_bps: float | None,
) -> bool:
    config = RSTS_CONFIG_BY_ID.get(key[0])
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
                reservations, expected_reservations, strict=True
            )
        )
        and all(
            _mapping_matches_exact_generated_row(actual, expected)
            for actual, expected in zip(trades, expected_trades, strict=True)
        )
    )


def _cell_contract_is_valid(
    cell: Mapping[str, Any], *, key: tuple[str, str, str], config: RSTSConfig | None
) -> bool:
    fields = frozenset(cell)
    if fields not in {
        RSTS_CELL_PRE_GATE_FIELD_NAMES,
        RSTS_CELL_PRE_GATE_FIELD_NAMES | RSTS_CELL_GATE_FIELD_NAMES,
    }:
        return False
    closed = _exact_nonnegative_int(cell.get("closed_signal_events"))
    eligible = _exact_nonnegative_int(cell.get("eligible_events"))
    reasons = cell.get("reasons")
    source_ready = cell.get("source_ready")
    source_error = cell.get("source_error")
    proxy_ready = cell.get("proxy_budget_ready")
    if (
        config is None
        or cell.get("config_id") != key[0]
        or cell.get("symbol") != key[1]
        or cell.get("side") != key[2]
        or closed is None
        or eligible is None
        or closed < eligible
        or not isinstance(reasons, Mapping)
        or any(not isinstance(reason, str) or not reason for reason in reasons)
        or any(_exact_nonnegative_int(count) is None for count in reasons.values())
        or not isinstance(source_ready, bool)
        or not isinstance(proxy_ready, bool)
        or not isinstance(cell.get("proxy_contract_ready"), bool)
        or (source_ready and source_error is not None)
        or (
            not source_ready and (not isinstance(source_error, str) or not source_error)
        )
        or cell.get("one_trade_per_cell_entry_day") is not True
        or _exact_int(cell.get("outcome_horizon_bars")) != OUTCOME_HORIZON_M1_BARS
        or not _numbers_match(cell.get("reward_risk"), REWARD_RISK)
        or not _numbers_match(cell.get("stop_floor_bps"), FROZEN_STOP_FLOOR_BPS)
        or not _numbers_match(cell.get("max_risk_bps"), MAX_RISK_BPS)
        or not _numbers_match(cell.get("p_star_max"), P_STAR_MAX)
        or not _numbers_match(cell.get("min_target_cost_ratio"), MIN_TARGET_COST_RATIO)
        or not _numbers_match(
            cell.get("extra_round_trip_cost_bps"), FROZEN_EXTRA_ROUND_TRIP_COST_BPS
        )
        or _exact_int(cell.get("minimum_active_transitions")) != MIN_ACTIVE_TRANSITIONS
        or not _numbers_match(
            cell.get("minimum_signal_return_vol"), MIN_SIGNAL_RETURN_VOL
        )
        or not _numbers_match(
            cell.get("maximum_signal_true_range_vol"), MAX_SIGNAL_TRUE_RANGE_VOL
        )
        or _exact_int(cell.get("swing_stop_bars")) != SWING_STOP_M1_BARS
        or cell.get("economic_claim_ready") is not False
        or cell.get("economics_claim_ready") is not False
    ):
        return False
    expected_cost_mode = (
        "pair_specific_frozen_proxy_spread_stress"
        if proxy_ready
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
    valid_window = bool(
        _exact_int(evaluation_start_epoch) is not None
        and _exact_int(evaluation_end_epoch) is not None
        and evaluation_start_epoch >= 0
        and evaluation_end_epoch > evaluation_start_epoch
    )
    frozen_calendar_window = bool(
        evaluation_start_epoch == CALENDAR_MONTH_BOUNDARY_EPOCHS[0]
        and evaluation_end_epoch == CALENDAR_MONTH_BOUNDARY_EPOCHS[-1]
    )
    try:
        expected_budgets = _normalize_proxy_budgets(expected_proxy_spread_budgets_bps)
    except (AttributeError, TypeError, ValueError):
        expected_budgets = {}
    proxy_mapping_complete = set(expected_budgets) == set(FX_SYMBOLS)
    prepared_mapping_complete = bool(
        isinstance(prepared_series_by_symbol, Mapping)
        and set(prepared_series_by_symbol) == set(FX_SYMBOLS)
        and all(
            isinstance(prepared_series_by_symbol.get(symbol), PreparedSeries)
            for symbol in FX_SYMBOLS
        )
    )
    reservations_by_cell: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    trades_by_cell: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in reservation_ledger:
        reservations_by_cell.setdefault(_ledger_cell_key(row), []).append(row)
    for row in trade_ledger:
        trades_by_cell.setdefault(_ledger_cell_key(row), []).append(row)

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
    known_keys = set(cell_keys)
    full_family_present = bool(
        len(cell_keys) == len(planned_keys)
        and len(known_keys) == len(cell_keys)
        and known_keys == planned_keys
    )
    ledger_keys_known = bool(
        set(reservations_by_cell).issubset(planned_keys)
        and set(reservations_by_cell).issubset(known_keys)
        and set(trades_by_cell).issubset(planned_keys)
        and set(trades_by_cell).issubset(known_keys)
    )
    global_scored_ids = [
        str(row.get("event_id") or "")
        for row in reservation_ledger
        if row.get("reservation_status") == "scored"
    ]
    global_trade_ids = [str(row.get("event_id") or "") for row in trade_ledger]
    global_trade_identity = bool(
        ledger_keys_known
        and all(global_scored_ids)
        and all(global_trade_ids)
        and len(global_scored_ids) == len(set(global_scored_ids))
        and len(global_trade_ids) == len(set(global_trade_ids))
        and Counter(global_scored_ids) == Counter(global_trade_ids)
    )

    for cell in cells:
        key = (
            str(cell.get("config_id") or ""),
            str(cell.get("symbol") or "").upper(),
            str(cell.get("side") or "").upper(),
        )
        config = RSTS_CONFIG_BY_ID.get(key[0])
        cell_contract = _cell_contract_is_valid(cell, key=key, config=config)
        expected_budget = expected_budgets.get(key[1])
        cell_budget = _finite_float(cell.get("proxy_budget_bps"))
        reservations = reservations_by_cell.get(key, [])
        trades = trades_by_cell.get(key, [])
        temporal_scope = bool(
            valid_window
            and all(
                _row_is_within_evaluation_window(
                    row,
                    start_epoch=evaluation_start_epoch,
                    end_epoch=evaluation_end_epoch,
                )
                for row in (*reservations, *trades)
            )
        )
        scored = [
            row for row in reservations if row.get("reservation_status") == "scored"
        ]
        unresolved = [
            row for row in reservations if row.get("reservation_status") == "unresolved"
        ]
        statuses_exact = len(scored) + len(unresolved) == len(reservations)
        trade_by_id = {str(row.get("event_id") or ""): row for row in trades}
        prepared = (
            prepared_series_by_symbol.get(key[1]) if prepared_mapping_complete else None
        )
        source_replay = _cell_matches_complete_source_replay(
            cell=cell,
            reservations=reservations,
            trades=trades,
            key=key,
            prepared=prepared,
            proxy_budget_bps=expected_budget,
        )
        reservation_ids = [str(row.get("event_id") or "") for row in reservations]
        trade_ids = [str(row.get("event_id") or "") for row in trades]
        scored_ids = [str(row.get("event_id") or "") for row in scored]
        per_cell_trade_identity = bool(
            all(reservation_ids)
            and all(trade_ids)
            and len(reservation_ids) == len(set(reservation_ids))
            and len(trade_ids) == len(set(trade_ids))
            and Counter(scored_ids) == Counter(trade_ids)
        )
        semantic_rows = bool(
            all(
                _scored_reservation_semantics_are_valid(
                    row,
                    trade=trade_by_id.get(str(row.get("event_id") or "")),
                    key=key,
                )
                for row in scored
            )
            and all(
                _unresolved_reservation_semantics_are_valid(row, key=key)
                for row in unresolved
            )
        )
        days = [str(row.get("entry_day") or "") for row in reservations]
        parsed_gate = [_finite_float(row.get("gate_pnl_r")) for row in reservations]
        gate_returns = [value for value in parsed_gate if value is not None]
        parsed_pnl = [_finite_float(row.get("pnl_r")) for row in trades]
        pnl_rs = [value for value in parsed_pnl if value is not None]
        wins = sum(row.get("full_target_win") is True for row in scored)
        positives = sum(row.get("positive_outcome") is True for row in scored)
        n_reservations = len(reservations)
        n_scored = len(scored)
        n_unresolved = len(unresolved)
        trade_rate = wins / n_scored if n_scored else 0.0
        reservation_rate = wins / n_reservations if n_reservations else 0.0
        positive_rate = positives / n_scored if n_scored else 0.0
        aggregates_match = bool(
            _exact_nonnegative_int(cell.get("entry_day_reservations")) == n_reservations
            and _exact_nonnegative_int(cell.get("eligible_events")) == n_reservations
            and _exact_nonnegative_int(cell.get("scored_trades")) == n_scored
            and _exact_nonnegative_int(cell.get("unresolved_reservations"))
            == n_unresolved
            and _exact_nonnegative_int(cell.get("full_target_wins")) == wins
            and _exact_nonnegative_int(cell.get("positive_outcomes")) == positives
            and cell.get("reservation_event_ids") == reservation_ids
            and cell.get("trade_event_ids") == trade_ids
            and len(pnl_rs) == len(trades)
            and cell.get("exit_mix")
            == dict(Counter(str(row.get("exit_reason")) for row in trades))
            and _numbers_match(cell.get("full_target_trade_win_rate"), trade_rate)
            and _numbers_match(
                cell.get("full_target_reservation_rate"), reservation_rate
            )
            and _numbers_match(cell.get("positive_outcome_rate"), positive_rate)
            and _numbers_match(cell.get("total_r"), math.fsum(pnl_rs))
            and _numbers_match(
                cell.get("mean_r"), statistics.fmean(pnl_rs) if pnl_rs else 0.0
            )
            and _numbers_match(cell.get("gate_total_r"), math.fsum(gate_returns))
            and _numbers_match(
                cell.get("gate_mean_r"),
                statistics.fmean(gate_returns) if gate_returns else 0.0,
            )
        )
        proxy_binding = bool(
            proxy_mapping_complete
            and expected_budget is not None
            and _numbers_match(cell_budget, expected_budget)
            and all(
                _numbers_match(row.get("proxy_budget_bps"), expected_budget)
                for row in reservations
            )
        )
        ledger_consistent = bool(
            full_family_present
            and cell_key_counts[key] == 1
            and cell_contract
            and config is not None
            and _numbers_match(
                cell.get("forecast_threshold"), config.forecast_threshold
            )
            and temporal_scope
            and source_replay
            and ledger_keys_known
            and statuses_exact
            and per_cell_trade_identity
            and semantic_rows
            and aggregates_match
            and proxy_binding
            and len(days) == len(set(days))
            and all(days)
            and len(gate_returns) == len(reservations)
            and len(pnl_rs) == len(trades)
            and global_trade_identity
        )
        unique_entry_days = len(set(days))
        wilson_lower = _one_sided_wilson_lower_bound(wins, n_reservations)
        reserved_day_t = _finite_one_sample_t(gate_returns)
        thirds = _temporal_thirds_diagnostics(
            reservations,
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        )
        months = _calendar_month_diagnostics(
            reservations,
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        )
        gate_mean = statistics.fmean(gate_returns) if gate_returns else 0.0
        passes = bool(
            ledger_consistent
            and frozen_calendar_window
            and cell.get("source_ready") is True
            and cell.get("proxy_budget_ready") is True
            and cell.get("proxy_contract_ready") is True
            and cell.get("cost_mode") == "pair_specific_frozen_proxy_spread_stress"
            and cell.get("source_error") is None
            and cell_budget is not None
            and cell_budget > 0.0
            and n_scored >= MIN_TRADES_PER_CELL
            and unique_entry_days >= MIN_UNIQUE_RESERVED_UTC_ENTRY_DAYS
            and reservation_rate >= MIN_OBSERVED_FULL_TARGET_RATE
            and wilson_lower >= MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
            and gate_mean > 0.0
            and reserved_day_t >= TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
            and thirds["gate_temporal_thirds_stable"] is True
            and months["gate_calendar_months_stable"] is True
        )
        cell.update(
            {
                "unique_reserved_utc_entry_days": unique_entry_days,
                "gate_full_target_reservation_rate": reservation_rate,
                "simultaneous_wilson_lower_bound": wilson_lower,
                "reserved_utc_day_one_sample_t": reserved_day_t,
                **thirds,
                **months,
                "gate_ledger_consistent": ledger_consistent,
                "gate_actual_scored_reservations": n_scored,
                "gate_actual_unresolved_reservations": n_unresolved,
                "gate_scored_trade_ids_match": per_cell_trade_identity,
                "gate_row_semantics_consistent": semantic_rows,
                "gate_temporal_scope_consistent": temporal_scope,
                "gate_proxy_contract_binding_consistent": proxy_binding,
                "gate_source_replay_consistent": source_replay,
                "gate_cell_contract_consistent": cell_contract,
                "passes_discovery_cell_gate": passes,
            }
        )

    passing_configs = _passing_global_configurations(cells)
    return {
        "passed": bool(passing_configs),
        "passing_config_ids": passing_configs,
        "unchanged_single_configuration_required": True,
        "full_canonical_universe_required": True,
        "full_canonical_universe_present": full_family_present,
        "evaluation_window_valid": valid_window,
        "evaluation_start_epoch": evaluation_start_epoch,
        "evaluation_end_epoch": evaluation_end_epoch,
        "expected_proxy_mapping_complete": proxy_mapping_complete,
        "prepared_source_mapping_complete": prepared_mapping_complete,
        "pair_direction_cells_per_configuration": SIMULTANEOUS_PAIR_DIRECTION_CELLS,
        "simultaneous_wilson_family_cells": SIMULTANEOUS_WILSON_FAMILY_CELLS,
        "minimum_scored_trades_per_cell": MIN_TRADES_PER_CELL,
        "minimum_unique_reserved_utc_entry_days_per_cell": (
            MIN_UNIQUE_RESERVED_UTC_ENTRY_DAYS
        ),
        "unique_reserved_utc_entry_days_definition": (
            "count of distinct UTC entry-day labels; date uniqueness does not "
            "establish statistical independence"
        ),
        "minimum_observed_full_target_reservation_rate": MIN_OBSERVED_FULL_TARGET_RATE,
        "minimum_simultaneous_wilson_lower_bound": MIN_SIMULTANEOUS_WILSON_LOWER_BOUND,
        "minimum_all_win_reservations_for_wilson": MIN_ALL_WIN_RESERVATIONS_FOR_WILSON,
        "wilson_method": (
            "descriptive one-sided Wilson boundary at nominal Bonferroni-adjusted "
            "alpha over all 72 fixed current cells"
        ),
        "minimum_gate_mean_r": 0.0,
        "reserved_utc_day_observation_unit": "one_gate_pnl_r_per_unique_reserved_utc_entry_day",
        "reserved_utc_day_one_sample_t_multiplicity_method": (
            "descriptive_two_sided_bonferroni_student_t_veto"
        ),
        "cross_cell_bonferroni_requires_cross_cell_independence": False,
        "within_cell_serial_dependence_adjustment_applied": False,
        "wilson_within_cell_serial_dependence_robust": False,
        "student_t_within_cell_serial_dependence_robust": False,
        "inferential_calibration_authorized": False,
        "reserved_utc_day_bonferroni_family_cells": IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS,
        "reserved_utc_day_bonferroni_family_alpha": TWO_SIDED_BONFERRONI_FAMILY_ALPHA,
        "reserved_utc_day_one_sample_t_degrees_of_freedom_floor": BONFERRONI_STUDENT_T_MIN_DF,
        "minimum_reserved_utc_day_one_sample_t": TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD,
        "temporal_thirds_role": "deterministic_discovery_only_robustness_veto_no_inferential_claim",
        "temporal_thirds_partition_formula": (
            "k=min(2,floor(3*(entry_epoch-evaluation_start_epoch)/"
            "(evaluation_end_epoch-evaluation_start_epoch)))"
        ),
        "temporal_thirds_half_open_boundary_epochs": _temporal_thirds_boundary_epochs(
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        ),
        "temporal_thirds_include_all_exact_reservations": True,
        "temporal_thirds_include_adverse_unresolved": True,
        "temporal_thirds_pooling_or_selection_allowed": False,
        "temporal_thirds_minimum_reservations_per_segment": MIN_RESERVATIONS_PER_TEMPORAL_THIRD,
        "temporal_thirds_minimum_full_target_rate_numerator": TEMPORAL_THIRD_WIN_RATE_NUMERATOR,
        "temporal_thirds_minimum_full_target_rate_denominator": TEMPORAL_THIRD_WIN_RATE_DENOMINATOR,
        "temporal_thirds_minimum_gate_total_r_exclusive": 0.0,
        "temporal_thirds_inferential_claim_authorized": False,
        "calendar_month_window_matches_frozen": frozen_calendar_window,
        "calendar_month_boundary_epochs": list(CALENDAR_MONTH_BOUNDARY_EPOCHS),
        "calendar_months_include_all_exact_reservations": True,
        "calendar_months_include_adverse_unresolved": True,
        "calendar_months_pooling_or_selection_allowed": False,
        "calendar_months_minimum_reservations_per_segment": MIN_RESERVATIONS_PER_CALENDAR_MONTH,
        "calendar_months_minimum_full_target_rate_numerator": CALENDAR_MONTH_WIN_RATE_NUMERATOR,
        "calendar_months_minimum_full_target_rate_denominator": CALENDAR_MONTH_WIN_RATE_DENOMINATOR,
        "calendar_months_minimum_gate_total_r_exclusive": 0.0,
        "calendar_months_inferential_claim_authorized": False,
        "unresolved_treatment": "non-win and adverse net stop-R",
        "scored_reservation_trade_ids_match": global_trade_identity,
        "success_claim_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def _has_scorable_run(bars: Sequence[QuoteBar]) -> bool:
    minimum = BASELINE_M1_BARS + 1 + 1 + OUTCOME_HORIZON_M1_BARS
    return any(
        run_end - run_start >= minimum for run_start, run_end in _consecutive_runs(bars)
    )


def _valid_proxy_provenance(
    provenance: Mapping[str, Any] | None,
    *,
    evaluation_start_epoch: int,
    preregistration_lock_epoch: int,
) -> bool:
    if provenance is None or provenance.get("frozen") is not True:
        return False
    if set(provenance) != set(REQUIRED_PROXY_PROVENANCE_FIELDS) | {"frozen"}:
        return False
    for field in REQUIRED_PROXY_PROVENANCE_FIELDS:
        value = provenance.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    if provenance.get("units") != "bps":
        return False
    snapshot_sha = str(provenance.get("source_snapshot_sha256") or "")
    if len(snapshot_sha) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in snapshot_sha
    ):
        return False
    try:
        as_of = _parse_epoch(str(provenance["as_of_utc"]))
        cutoff = _parse_epoch(str(provenance["source_cutoff_utc"]))
    except (TypeError, ValueError):
        return False
    return bool(
        as_of is not None
        and cutoff is not None
        and cutoff <= as_of
        and cutoff <= evaluation_start_epoch
        and as_of <= preregistration_lock_epoch
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
    preregistration_lock_utc: str,
    source_errors: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Screen the exact 72-cell family without granting trading authority.

    Direct callers receive the same half-open evaluation-window enforcement as
    the CSV loader.  A supplied pre-start or post-end bar invalidates the whole
    symbol instead of being silently available to signal or outcome code.
    """

    evaluation_start_epoch = _parse_epoch(evaluation_start_utc)
    evaluation_end_epoch = _parse_epoch(evaluation_end_utc)
    if (
        evaluation_start_epoch is None
        or evaluation_end_epoch is None
        or evaluation_end_epoch <= evaluation_start_epoch
    ):
        raise ValueError("a valid exclusive evaluation UTC interval is required")
    preregistration_lock_epoch = _parse_epoch(preregistration_lock_utc)
    if preregistration_lock_epoch is None:
        raise ValueError("preregistration_lock_utc is required")

    normalized_bars = _normalize_bars_mapping(bars_by_symbol)
    normalized_budgets = _normalize_proxy_budgets(proxy_spread_budgets_bps)
    errors = {
        str(key).strip().upper(): str(value)
        for key, value in (source_errors or {}).items()
    }
    unknown_error_symbols = set(errors).difference(FX_SYMBOLS)
    if unknown_error_symbols:
        raise ValueError(
            "noncanonical source-error symbol: " + sorted(unknown_error_symbols)[0]
        )
    provenance_ready = bool(
        proxy_contract_schema_version == PROXY_CONTRACT_SCHEMA_VERSION
        and _valid_proxy_provenance(
            proxy_provenance,
            evaluation_start_epoch=evaluation_start_epoch,
            preregistration_lock_epoch=preregistration_lock_epoch,
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
            # An explicit source failure always dominates accidentally supplied rows.
            prepared = prepare_series([])
        elif any(
            bar.epoch < evaluation_start_epoch or bar.epoch >= evaluation_end_epoch
            for bar in prepared.bars
        ):
            errors[symbol] = "bar_outside_evaluation_window"
        elif not prepared.bars:
            errors[symbol] = "empty_after_date_filter"
        elif not _has_scorable_run(prepared.bars):
            errors[symbol] = "no_complete_262_bar_m1_run"

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
        "schema_version": "fxstack.scalp.rolling_sign_transition_state_screen.v1",
        "family": "rolling_sign_transition_state",
        "acronym": "RSTS",
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
                "a=(same-opposite)/active over 119 adjacent prior-return-sign "
                "transitions after excluding zero-touching transitions without "
                "re-pairing; x is the nonzero signal return sign; F=a*x"
            ),
            "direction": (
                "BUY requires F>=theta and SELL requires F<=-theta for the fixed "
                "theta grid {0.05,0.10}"
            ),
            "baseline": (
                "median of exactly 240 M1 midpoint true ranges plus nearest-rank "
                "Q25 (sorted index 59) of exactly 240 close spreads, both ending "
                "t-1; one extra leading close supplies the first TR"
            ),
            "transition_window": (
                "exactly 120 midpoint-close log-return signs ending t-1, yielding "
                "119 adjacent transitions; at least 80 active transitions"
            ),
            "signal": (
                "nonzero x; absolute signal return at least 0.10 baseline-volatility "
                "units; current midpoint true range no more than 3.0 units"
            ),
            "spread": (
                "signal close and exact t+1 open spreads no wider than min(frozen "
                "240-bar nearest-rank Q25, frozen pair proxy)"
            ),
            "proxy_time_contract": (
                "source_cutoff_utc <= evaluation_start_utc; honest artifact "
                "as_of_utc <= explicit preregistration_lock_utc"
            ),
            "fill": "exact t+1 ask open BUY / bid open SELL",
            "outcome_horizon_m1_bars": OUTCOME_HORIZON_M1_BARS,
            "reserve_before_horizon_check": True,
            "unresolved_gate_treatment": (
                "non-win and adverse net stop-R; never dropped or replaced"
            ),
            "missing_exact_fill_treatment": (
                "qualifying closed signal reserves expected UTC entry day as an "
                "unresolved adverse stop; no same-day substitution"
            ),
            "known_entry_rejection_treatment": (
                "all known completion rejects, including entry-spread, degenerate "
                "stop or bracket, risk-cap, p-star, and target-cost rejects, do "
                "not reserve"
            ),
            "one_trade_per_cell_entry_day": True,
            "stop": (
                "opposite executable quote-side swing extreme over t-2:t inclusive "
                "plus frozen 0.10 baseline-volatility buffer"
            ),
            "stop_floor_bps": FROZEN_STOP_FLOOR_BPS,
            "max_risk_bps": MAX_RISK_BPS,
            "reward_risk": REWARD_RISK,
            "p_star_max": P_STAR_MAX,
            "p_star": (
                "net quote stop loss divided by net quote stop loss plus net quote "
                "target payoff after proxy-excess and one-bp debits"
            ),
            "min_target_cost_ratio": MIN_TARGET_COST_RATIO,
            "recorded_cost": (
                "max(signal spread, exact entry spread, frozen pair proxy) plus "
                "one-bp adverse round-trip pad"
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
            "evaluation_interval": "half_open_start_inclusive_end_exclusive",
        },
        "cost_readiness": {
            "proxy_contract_ready": provenance_ready,
            "proxy_contract_schema_version": proxy_contract_schema_version,
            "proxy_provenance": dict(proxy_provenance or {}),
            "evaluation_start_utc": evaluation_start_utc,
            "evaluation_end_utc": evaluation_end_utc,
            "preregistration_lock_utc": preregistration_lock_utc,
            "source_cutoff_no_later_than_evaluation_start": provenance_ready,
            "artifact_as_of_no_later_than_preregistration_lock": provenance_ready,
            "missing_proxy_budget_symbols": sorted(set(missing_proxy_budget_symbols)),
            "source_failure_symbols": sorted(set(source_failure_symbols)),
            "proxy_budgets_bps": {
                symbol: _valid_positive_number(normalized_budgets.get(symbol))
                for symbol in FX_SYMBOLS
            },
            "observed_source_bid_ask_used": True,
            "proxy_used_as_cost_stress": True,
            "proxy_excess_debited_in_pstar_and_pnl": True,
            "extra_adverse_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
        },
        "search_accounting": accounting
        | {
            "family_alpha": TWO_SIDED_BONFERRONI_FAMILY_ALPHA,
            "two_sided_bonferroni_student_t_min_df99_abs_threshold": (
                TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
            ),
            "cumulative_threshold_role": (
                "conservative_discovery_veto_only_not_an_inferential_success_claim"
            ),
            "anytime_valid_for_unbounded_optional_stopping": False,
            "future_success_claim_requires": (
                "prospective_finite_hypothesis_cap_or_dependence_robust_alpha_spending_"
                "plus_untouched_validation"
            ),
        },
        "discovery_gate": discovery_gate,
        "reservation_ledger": all_reservations,
        "trade_ledger": all_trades,
        "cells": cells,
    }


def _load_proxy_contract(
    path: Path,
    *,
    evaluation_start_utc: str,
    preregistration_lock_utc: str,
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
    preregistration_lock_epoch = _parse_epoch(preregistration_lock_utc)
    if preregistration_lock_epoch is None:
        raise ValueError("preregistration_lock_utc is required")
    if not isinstance(provenance, dict) or not _valid_proxy_provenance(
        provenance,
        evaluation_start_epoch=evaluation_start_epoch,
        preregistration_lock_epoch=preregistration_lock_epoch,
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
    parser.add_argument("--preregistration-lock-utc", required=True)
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
        preregistration_lock_utc=args.preregistration_lock_utc,
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
        proxy_contract_schema_version=proxy_schema_version,
        evaluation_start_utc=args.start,
        evaluation_end_utc=args.end,
        preregistration_lock_utc=args.preregistration_lock_utc,
        source_errors=source_errors,
    )
    result["input_metadata"] = {
        "csv_root_recorded": False,
        "start": args.start,
        "end_exclusive": args.end,
        "preregistration_lock_utc": args.preregistration_lock_utc,
        "proxy_contract_sha256": _sha256(proxy_path),
        "m1_csv_sha256_by_symbol": source_sha256,
    }
    reservation_payload = {
        "schema_version": "fxstack.scalp.rolling_sign_transition_state_reservation_ledger.v1",
        "family": result["family"],
        "reservations": result.pop("reservation_ledger"),
    }
    trade_payload = {
        "schema_version": "fxstack.scalp.rolling_sign_transition_state_trade_ledger.v1",
        "family": result["family"],
        "trades": result.pop("trade_ledger"),
    }
    for path, payload in (
        (reservation_output, reservation_payload),
        (trade_output, trade_payload),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=1, sort_keys=True, allow_nan=False))
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
        f"RSTS: {len(result['cells'])} cells; "
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
