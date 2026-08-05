# AGENT: ROLE: Research-only Quote-Side Convergence Continuation bid/ask M1 discovery screen.
# AGENT: ENTRYPOINT: `screen_universe`; CLI `python -m fxstack.scalp.screen_quote_side_convergence_continuation`.
# AGENT: PRIMARY INPUTS: immutable exact-schema bid/ask M1 CSVs and one frozen proxy-cost contract.
# AGENT: PRIMARY OUTPUTS: all fixed cells plus complete reservation and scored-trade ledgers.
# AGENT ISOLATION: advisory discovery evidence only; never authorizes success, activation, or orders.
"""Causal M1 screen for the symmetric Quote-Side Convergence Continuation (QSCC).

For a closed signal bar ``t``, the volatility and spread baselines contain
exactly 240 consecutive observations from ``t-241`` through ``t-2``; midpoint
true range also uses the completed predecessor ``t-242``.  The volatility
baseline is the ordinary median midpoint true range and the spread baseline is
the nearest-rank Q25 (sorted index 59) of close spreads.

At each completed close ``j``, ``r_j = 1e4*log(M_j/M_{j-1})`` and
``q_j = 1e4*(log(A_j/B_j)-log(A_{j-1}/B_{j-1}))``, where ``A``, ``B``, and
``M`` are ask close, bid close, and their midpoint.  Positive ``q`` says only
that proportional spread is wider at the later completed close; this screen
makes no within-minute path, sequencing, or order-flow claim.  Both directions
require widening at ``t-1``, convergence at ``t``, and two same-direction
midpoint returns.  SELL is the exact sign mirror of BUY only for those returns.

Execution is delayed to the exact observed ``t+1`` ask/bid open.  Costs,
brackets, and outcomes use executable-side quotes.  A qualifying signal with a
missing/gapped exact fill or an incomplete 30-bar horizon consumes the first
cell/UTC-entry-day reservation and is treated as an adverse non-win.  A known
entry-spread rejection does not reserve the day.

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
QUOTE_WIDENING_RECORDED_COST_FRACTION = 0.10
QUOTE_CONVERGENCE_PRIOR_CHANGE_FRACTION = 0.50
PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD = 0.35
CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD = 0.10
MAX_SIGNAL_TRUE_RANGE_VOL = 2.5
FROZEN_EXTRA_ROUND_TRIP_COST_BPS = 1.0
TARGET_RECORDED_COST_MULTIPLE = 4.0
STOP_RECORDED_COST_MULTIPLE = 8.0
MAX_PROXY_SPREAD_BPS = 3.0
MAX_GROSS_STOP_BPS = 32.0
MAX_P_STAR = 3.0 / 4.0
OUTCOME_HORIZON_M1_BARS = 30
FILL_DELAY_M1_BARS = 1

IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 4_386
IMMUTABLE_CURRENT_ATTEMPTED_CELLS = 36
IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 4_422
TWO_SIDED_BONFERRONI_FAMILY_ALPHA = 0.05
BONFERRONI_STUDENT_T_MIN_DF = 99
TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD = 4.6258224137931165
PROXY_CONTRACT_SCHEMA_VERSION = "fxstack.scalp.pair_proxy_spread_contract.v1"

MIN_TRADES_PER_CELL = 100
MIN_UNIQUE_RESERVED_UTC_ENTRY_DAYS = 100
MIN_OBSERVED_FULL_TARGET_RATE = 0.90
MIN_SIMULTANEOUS_WILSON_LOWER_BOUND = 0.90
SIMULTANEOUS_PAIR_DIRECTION_CELLS = len(FX_SYMBOLS) * 2
SIMULTANEOUS_WILSON_FAMILY_CELLS = IMMUTABLE_CURRENT_ATTEMPTED_CELLS
MIN_PROFIT_FACTOR = 1.5

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
class QSCCConfig:
    """The single frozen QSCC configuration."""

    quote_widening_recorded_cost_fraction: float = QUOTE_WIDENING_RECORDED_COST_FRACTION
    quote_convergence_prior_change_fraction: float = (
        QUOTE_CONVERGENCE_PRIOR_CHANGE_FRACTION
    )
    prior_directional_return_vol_threshold: float = (
        PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD
    )
    current_directional_return_vol_threshold: float = (
        CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD
    )
    maximum_signal_true_range_vol: float = MAX_SIGNAL_TRUE_RANGE_VOL
    target_recorded_cost_multiple: float = TARGET_RECORDED_COST_MULTIPLE
    stop_recorded_cost_multiple: float = STOP_RECORDED_COST_MULTIPLE

    @property
    def config_id(self) -> str:
        return "qscc_v1"


GRID: tuple[QSCCConfig, ...] = (QSCCConfig(),)
QSCC_CONFIG_BY_ID: dict[str, QSCCConfig] = {config.config_id: config for config in GRID}


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
    volatility_bps: float
    spread_q25_bps: float


@dataclass(frozen=True, slots=True)
class QSCCClosedSignal:
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
    spread_cap_bps: float
    prior_return_bps: float
    prior_return_vol_units: float
    current_return_bps: float
    current_return_vol_units: float
    signal_true_range_bps: float
    signal_true_range_vol_units: float
    prior_quote_change_bps: float
    signal_quote_change_bps: float
    quote_convergence_ratio: float
    prior_true_range_bps: float
    prior_true_range_vol_units: float
    baseline_end_spread_bps: float
    prior_spread_bps: float
    signal_spread_bps: float


@dataclass(frozen=True, slots=True)
class QSCCSignal:
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
    volatility_bps: float
    spread_q25_bps: float
    proxy_budget_bps: float
    spread_cap_bps: float
    prior_return_bps: float
    prior_return_vol_units: float
    current_return_bps: float
    current_return_vol_units: float
    signal_true_range_bps: float
    signal_true_range_vol_units: float
    prior_quote_change_bps: float
    signal_quote_change_bps: float
    quote_convergence_ratio: float
    prior_true_range_bps: float
    prior_true_range_vol_units: float
    baseline_end_spread_bps: float
    prior_spread_bps: float
    signal_spread_bps: float
    entry_spread_bps: float
    spread_stress_bps: float
    recorded_cost_bps: float
    execution_cost_debit_bps: float
    gross_target_bps: float
    gross_stop_bps: float
    p_star: float


@dataclass(frozen=True, slots=True)
class QSCCTrade:
    event_id: str
    symbol: str
    side: str
    config_id: str
    signal_epoch: int
    entry_epoch: int
    entry_day: str
    exit_epoch: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    gross_stop_bps: float
    gross_target_bps: float
    execution_cost_debit_bps: float
    pnl_bps: float
    pnl_r: float
    bars_held: int
    exit_reason: str
    full_target_win: bool
    positive_outcome: bool


QSCC_SIGNAL_FIELD_NAMES = frozenset(QSCCSignal.__dataclass_fields__)
QSCC_TRADE_FIELD_NAMES = frozenset(QSCCTrade.__dataclass_fields__)
QSCC_RESERVATION_OUTCOME_FIELD_NAMES = frozenset(
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
QSCC_RESERVATION_FIELD_NAMES = (
    QSCC_SIGNAL_FIELD_NAMES | QSCC_RESERVATION_OUTCOME_FIELD_NAMES
)
QSCC_MISSING_FILL_RESERVATION_FIELD_NAMES = QSCC_RESERVATION_FIELD_NAMES | {
    "gate_risk_basis_bps"
}


@dataclass(slots=True)
class PreparedSeries:
    bars: list[QuoteBar]
    volatility_before_signal: list[float | None]
    spread_q25_before_signal: list[float | None]


def _spread_bps(bid: float, ask: float) -> float:
    midpoint = (bid + ask) / 2.0
    if midpoint <= 0.0 or ask < bid:
        return 0.0
    return (ask - bid) / midpoint * 1e4


def _valid_positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed > 0.0 else None


def _valid_proxy_budget(value: Any) -> float | None:
    parsed = _valid_positive_number(value)
    if parsed is None or parsed > MAX_PROXY_SPREAD_BPS:
        return None
    return parsed


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _reject_nonfinite_json_constant(token: str) -> None:
    raise ValueError(f"non-finite JSON constant: {token}")


def loads_strict_json(raw: str) -> Any:
    """Parse JSON while rejecting duplicate keys and non-finite constants."""

    return json.loads(
        raw,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_nonfinite_json_constant,
    )


def dumps_strict_json(payload: Any, *, indent: int | None = 1) -> str:
    """Serialize deterministic JSON and fail on every non-finite number."""

    return json.dumps(payload, indent=indent, sort_keys=True, allow_nan=False)


def _all_json_numbers_finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_all_json_numbers_finite(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _all_json_numbers_finite(item)
            for key, item in value.items()
        )
    return False


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
    """Load exact-schema, strict-UTC, monotonic bid/ask M1 while retaining gaps."""

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


def _true_range_bps(previous: QuoteBar, current: QuoteBar) -> float:
    if current.mid_c <= 0.0:
        return 0.0
    true_range = max(
        current.mid_h - current.mid_l,
        abs(current.mid_h - previous.mid_c),
        abs(current.mid_l - previous.mid_c),
    )
    return true_range / current.mid_c * 1e4


def _mid_close_return_bps(previous: QuoteBar, current: QuoteBar) -> float:
    if previous.mid_c <= 0.0 or current.mid_c <= 0.0:
        return 0.0
    return math.log(current.mid_c / previous.mid_c) * 1e4


def _quote_log_spread_change_bps(previous: QuoteBar, current: QuoteBar) -> float:
    """Completed-close proportional-spread change; not an intrabar path claim."""

    if (
        previous.bid_c <= 0.0
        or previous.ask_c <= 0.0
        or current.bid_c <= 0.0
        or current.ask_c <= 0.0
    ):
        return 0.0
    return 1e4 * (
        math.log(current.ask_c / current.bid_c)
        - math.log(previous.ask_c / previous.bid_c)
    )


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
) -> tuple[list[float | None], list[float | None]]:
    """Build exact 240-observation baselines ending at t-2."""

    size = len(bars)
    volatility: list[float | None] = [None] * size
    spread_q25: list[float | None] = [None] * size
    for run_start, run_end in _consecutive_runs(bars):
        first_signal = run_start + BASELINE_M1_BARS + 2
        if first_signal >= run_end:
            continue
        baseline_start = first_signal - BASELINE_M1_BARS - 1
        baseline_end = first_signal - 1
        spread_ordered = sorted(
            bars[index].spread_close_bps
            for index in range(baseline_start, baseline_end)
        )
        tr_ordered = sorted(
            _true_range_bps(bars[index - 1], bars[index])
            for index in range(baseline_start, baseline_end)
        )
        if (
            len(spread_ordered) != BASELINE_M1_BARS
            or len(tr_ordered) != BASELINE_M1_BARS
        ):
            raise RuntimeError("QSCC rolling baseline length changed")
        for signal_index in range(first_signal, run_end):
            median_tr = statistics.median(tr_ordered)
            q25 = float(spread_ordered[59])
            if (
                math.isfinite(median_tr)
                and median_tr > 0.0
                and math.isfinite(q25)
                and q25 >= 0.0
            ):
                volatility[signal_index] = median_tr
                spread_q25[signal_index] = q25
            if signal_index + 1 >= run_end:
                continue
            expired_index = signal_index - BASELINE_M1_BARS - 1
            admitted_index = signal_index - 1
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
    return volatility, spread_q25


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
    volatility = prepared.volatility_before_signal[signal_index]
    q25 = prepared.spread_q25_before_signal[signal_index]
    if (
        volatility is None
        or q25 is None
        or not math.isfinite(volatility)
        or volatility <= 0.0
        or not math.isfinite(q25)
        or q25 < 0.0
    ):
        return None
    return BaselineContext(float(volatility), float(q25))


def _utc_day(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%d")


def _normalized_p_star(
    *, recorded_cost_bps: float, execution_cost_debit_bps: float
) -> float:
    """Evaluate ``(R+D)/(R+T)`` through frozen cost-normalized multiples."""

    if (
        not math.isfinite(recorded_cost_bps)
        or recorded_cost_bps <= 0.0
        or not math.isfinite(execution_cost_debit_bps)
        or execution_cost_debit_bps < 0.0
    ):
        return math.inf
    debit_cost_ratio = execution_cost_debit_bps / recorded_cost_bps
    p_star = (8.0 + debit_cost_ratio) / 12.0
    if execution_cost_debit_bps == recorded_cost_bps and p_star != MAX_P_STAR:
        raise RuntimeError("QSCC normalized p-star boundary lost exactness")
    if execution_cost_debit_bps <= recorded_cost_bps and p_star > MAX_P_STAR:
        raise RuntimeError("QSCC normalized p-star exceeded its derived maximum")
    return p_star


def _p_star_forms_agree(
    *,
    normalized_p_star: float,
    gross_stop_bps: float,
    execution_cost_debit_bps: float,
    gross_target_bps: float,
) -> bool:
    """Check normalized p-star against ``(R+D)/(R+T)`` algebraically only."""

    denominator = gross_stop_bps + gross_target_bps
    if (
        not math.isfinite(normalized_p_star)
        or not math.isfinite(gross_stop_bps)
        or not math.isfinite(execution_cost_debit_bps)
        or not math.isfinite(gross_target_bps)
        or denominator <= 0.0
    ):
        return False
    standard_p_star = (gross_stop_bps + execution_cost_debit_bps) / denominator
    return _numbers_match(normalized_p_star, standard_p_star)


def evaluate_closed_signal(
    *,
    prepared: PreparedSeries,
    signal_index: int,
    symbol: str,
    side: str,
    config: QSCCConfig,
    proxy_spread_budget_bps: float | None,
) -> tuple[QSCCClosedSignal | None, str]:
    """Evaluate the closed-t QSCC trigger without reading or requiring t+1."""

    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    budget = _valid_proxy_budget(proxy_spread_budget_bps)
    if budget is None:
        return None, "proxy_cost_unavailable"
    if signal_index < 0 or signal_index >= len(prepared.bars):
        return None, "signal_bar_unavailable"
    context = baseline_context_at(prepared, signal_index=signal_index)
    if context is None:
        return None, "strict_pre_signal_context_unavailable"
    baseline_end_bar = prepared.bars[signal_index - 2]
    previous_bar = prepared.bars[signal_index - 1]
    signal_bar = prepared.bars[signal_index]
    spread_cap = min(context.spread_q25_bps, budget)
    baseline_end_spread = baseline_end_bar.spread_close_bps
    prior_spread = previous_bar.spread_close_bps
    signal_spread = signal_bar.spread_close_bps
    if baseline_end_spread > spread_cap:
        return None, "baseline_end_spread_above_q25_or_proxy"
    if prior_spread > budget:
        return None, "prior_spread_above_proxy"
    if signal_spread > spread_cap:
        return None, "signal_spread_above_q25_or_proxy"
    prior_tr = _true_range_bps(baseline_end_bar, previous_bar)
    signal_tr = _true_range_bps(previous_bar, signal_bar)
    prior_tr_units = prior_tr / context.volatility_bps
    signal_tr_units = signal_tr / context.volatility_bps
    if (
        not math.isfinite(prior_tr_units)
        or prior_tr_units > config.maximum_signal_true_range_vol
    ):
        return None, "prior_true_range_too_large"
    if (
        not math.isfinite(signal_tr_units)
        or signal_tr_units > config.maximum_signal_true_range_vol
    ):
        return None, "signal_true_range_too_large"
    prior_return = _mid_close_return_bps(baseline_end_bar, previous_bar)
    current_return = _mid_close_return_bps(previous_bar, signal_bar)
    prior_units = prior_return / context.volatility_bps
    current_units = current_return / context.volatility_bps
    prior_quote_change = _quote_log_spread_change_bps(baseline_end_bar, previous_bar)
    signal_quote_change = _quote_log_spread_change_bps(previous_bar, signal_bar)
    recorded_cost_basis = budget + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    if (
        not math.isfinite(prior_quote_change)
        or prior_quote_change
        < config.quote_widening_recorded_cost_fraction * recorded_cost_basis
    ):
        return None, "prior_quote_widening_too_small"
    if (
        not math.isfinite(signal_quote_change)
        or signal_quote_change
        > -config.quote_convergence_prior_change_fraction * prior_quote_change
    ):
        return None, "signal_quote_convergence_too_small"
    direction = 1.0 if side == "BUY" else -1.0
    if direction * prior_return < (
        config.prior_directional_return_vol_threshold * context.volatility_bps
    ):
        return None, "prior_directional_return_too_small"
    if direction * current_return < (
        config.current_directional_return_vol_threshold * context.volatility_bps
    ):
        return None, "current_directional_return_too_small"
    quote_convergence_ratio = signal_quote_change / prior_quote_change
    expected_entry_epoch = signal_bar.epoch + 60
    return (
        QSCCClosedSignal(
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
            spread_cap_bps=spread_cap,
            prior_return_bps=prior_return,
            prior_return_vol_units=prior_units,
            current_return_bps=current_return,
            current_return_vol_units=current_units,
            signal_true_range_bps=signal_tr,
            signal_true_range_vol_units=signal_tr_units,
            prior_quote_change_bps=prior_quote_change,
            signal_quote_change_bps=signal_quote_change,
            quote_convergence_ratio=quote_convergence_ratio,
            prior_true_range_bps=prior_tr,
            prior_true_range_vol_units=prior_tr_units,
            baseline_end_spread_bps=baseline_end_spread,
            prior_spread_bps=prior_spread,
            signal_spread_bps=signal_spread,
        ),
        "",
    )


def _complete_signal_at_exact_next_open(
    *, prepared: PreparedSeries, closed: QSCCClosedSignal
) -> tuple[QSCCSignal | None, str]:
    if closed.expected_entry_index >= len(prepared.bars):
        return None, "exact_next_open_unavailable"
    next_bar = prepared.bars[closed.expected_entry_index]
    if not validate_quote_bar(next_bar):
        return None, "exact_next_open_invalid"
    if next_bar.epoch != closed.expected_entry_epoch:
        return None, "exact_next_open_gap"
    entry_spread = next_bar.spread_open_bps
    if entry_spread > closed.spread_cap_bps:
        return None, "entry_spread_above_q25_or_proxy"
    entry_price = next_bar.ask_o if closed.side == "BUY" else next_bar.bid_o
    spread_stress = max(
        closed.baseline_end_spread_bps,
        closed.prior_spread_bps,
        closed.signal_spread_bps,
        entry_spread,
        closed.proxy_budget_bps,
    )
    if spread_stress != closed.proxy_budget_bps:
        raise RuntimeError("QSCC admitted spread stress must equal frozen proxy")
    recorded_cost = spread_stress + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    execution_debit = (
        max(0.0, spread_stress - entry_spread) + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    )
    gross_target = TARGET_RECORDED_COST_MULTIPLE * recorded_cost
    gross_stop = STOP_RECORDED_COST_MULTIPLE * recorded_cost
    if gross_stop > MAX_GROSS_STOP_BPS:
        return None, "risk_above_32bps"
    p_star = _normalized_p_star(
        recorded_cost_bps=recorded_cost,
        execution_cost_debit_bps=execution_debit,
    )
    if not _p_star_forms_agree(
        normalized_p_star=p_star,
        gross_stop_bps=gross_stop,
        execution_cost_debit_bps=execution_debit,
        gross_target_bps=gross_target,
    ):
        raise RuntimeError("QSCC normalized and standard p-star forms disagree")
    if not math.isfinite(p_star) or p_star > MAX_P_STAR:
        return None, "bracket_cost_dead"
    if closed.side == "BUY":
        stop_price = entry_price * (1.0 - gross_stop / 1e4)
        target_price = entry_price * (1.0 + gross_target / 1e4)
    else:
        stop_price = entry_price * (1.0 + gross_stop / 1e4)
        target_price = entry_price * (1.0 - gross_target / 1e4)
    if stop_price <= 0.0 or target_price <= 0.0:
        return None, "degenerate_bracket"
    return (
        QSCCSignal(
            config_id=closed.config_id,
            symbol=closed.symbol,
            side=closed.side,
            signal_index=closed.signal_index,
            signal_epoch=closed.signal_epoch,
            entry_index=closed.expected_entry_index,
            entry_epoch=next_bar.epoch,
            entry_day=closed.entry_day,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            volatility_bps=closed.volatility_bps,
            spread_q25_bps=closed.spread_q25_bps,
            proxy_budget_bps=closed.proxy_budget_bps,
            spread_cap_bps=closed.spread_cap_bps,
            prior_return_bps=closed.prior_return_bps,
            prior_return_vol_units=closed.prior_return_vol_units,
            current_return_bps=closed.current_return_bps,
            current_return_vol_units=closed.current_return_vol_units,
            signal_true_range_bps=closed.signal_true_range_bps,
            signal_true_range_vol_units=closed.signal_true_range_vol_units,
            prior_quote_change_bps=closed.prior_quote_change_bps,
            signal_quote_change_bps=closed.signal_quote_change_bps,
            quote_convergence_ratio=closed.quote_convergence_ratio,
            prior_true_range_bps=closed.prior_true_range_bps,
            prior_true_range_vol_units=closed.prior_true_range_vol_units,
            baseline_end_spread_bps=closed.baseline_end_spread_bps,
            prior_spread_bps=closed.prior_spread_bps,
            signal_spread_bps=closed.signal_spread_bps,
            entry_spread_bps=entry_spread,
            spread_stress_bps=spread_stress,
            recorded_cost_bps=recorded_cost,
            execution_cost_debit_bps=execution_debit,
            gross_target_bps=gross_target,
            gross_stop_bps=gross_stop,
            p_star=p_star,
        ),
        "",
    )


def evaluate_signal(
    *,
    prepared: PreparedSeries,
    signal_index: int,
    symbol: str,
    side: str,
    config: QSCCConfig,
    proxy_spread_budget_bps: float | None,
) -> tuple[QSCCSignal | None, str]:
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
    raw = f"quote_side_convergence_continuation|{config_id}|{symbol}|{side}|{signal_epoch}|{entry_epoch}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _event_id(signal: QSCCSignal) -> str:
    return _event_id_fields(
        config_id=signal.config_id,
        symbol=signal.symbol,
        side=signal.side,
        signal_epoch=signal.signal_epoch,
        entry_epoch=signal.entry_epoch,
    )


def _trade_result(
    signal: QSCCSignal,
    *,
    exit_bar: QuoteBar,
    exit_price: float,
    bars_held: int,
    reason: str,
) -> QSCCTrade:
    gross_bps = (
        (exit_price - signal.entry_price) / signal.entry_price * 1e4
        if signal.side == "BUY"
        else (signal.entry_price - exit_price) / signal.entry_price * 1e4
    )
    pnl_bps = gross_bps - signal.execution_cost_debit_bps
    full_target = reason in {"tp", "tp_gap_open"} and pnl_bps > 0.0
    return QSCCTrade(
        event_id=_event_id(signal),
        symbol=signal.symbol,
        side=signal.side,
        config_id=signal.config_id,
        signal_epoch=signal.signal_epoch,
        entry_epoch=signal.entry_epoch,
        entry_day=signal.entry_day,
        exit_epoch=exit_bar.epoch,
        entry_price=signal.entry_price,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        exit_price=exit_price,
        gross_stop_bps=signal.gross_stop_bps,
        gross_target_bps=signal.gross_target_bps,
        execution_cost_debit_bps=signal.execution_cost_debit_bps,
        pnl_bps=pnl_bps,
        pnl_r=pnl_bps / signal.gross_stop_bps,
        bars_held=bars_held,
        exit_reason=reason,
        full_target_win=full_target,
        positive_outcome=pnl_bps > 0.0,
    )


def simulate_trade(bars: Sequence[QuoteBar], *, signal: QSCCSignal) -> QSCCTrade | None:
    """Replay 30 executable-side M1 bars, including the exact entry bar."""

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
    raise RuntimeError("complete QSCC outcome horizon did not resolve")


def _reservation_row(
    signal: QSCCSignal, *, status: str, trade: QSCCTrade | None
) -> dict[str, Any]:
    gate_pnl_r = (
        trade.pnl_r
        if trade is not None
        else -(signal.gross_stop_bps + signal.execution_cost_debit_bps)
        / signal.gross_stop_bps
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
            else "unresolved_incomplete_horizon_as_adverse_stop_for_discovery_gate"
        ),
        "full_target_win": trade.full_target_win if trade is not None else False,
        "positive_outcome": trade.positive_outcome if trade is not None else False,
    }


def _missing_fill_reservation_row(
    closed: QSCCClosedSignal, *, reason: str
) -> dict[str, Any]:
    spread_stress = closed.proxy_budget_bps
    recorded_cost = spread_stress + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    execution_debit = recorded_cost
    gross_target = TARGET_RECORDED_COST_MULTIPLE * recorded_cost
    gross_stop = STOP_RECORDED_COST_MULTIPLE * recorded_cost
    p_star = _normalized_p_star(
        recorded_cost_bps=recorded_cost,
        execution_cost_debit_bps=execution_debit,
    )
    if not _p_star_forms_agree(
        normalized_p_star=p_star,
        gross_stop_bps=gross_stop,
        execution_cost_debit_bps=execution_debit,
        gross_target_bps=gross_target,
    ):
        raise RuntimeError("QSCC normalized and standard p-star forms disagree")
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
        "volatility_bps": closed.volatility_bps,
        "spread_q25_bps": closed.spread_q25_bps,
        "proxy_budget_bps": closed.proxy_budget_bps,
        "spread_cap_bps": closed.spread_cap_bps,
        "prior_return_bps": closed.prior_return_bps,
        "prior_return_vol_units": closed.prior_return_vol_units,
        "current_return_bps": closed.current_return_bps,
        "current_return_vol_units": closed.current_return_vol_units,
        "signal_true_range_bps": closed.signal_true_range_bps,
        "signal_true_range_vol_units": closed.signal_true_range_vol_units,
        "prior_quote_change_bps": closed.prior_quote_change_bps,
        "signal_quote_change_bps": closed.signal_quote_change_bps,
        "quote_convergence_ratio": closed.quote_convergence_ratio,
        "prior_true_range_bps": closed.prior_true_range_bps,
        "prior_true_range_vol_units": closed.prior_true_range_vol_units,
        "baseline_end_spread_bps": closed.baseline_end_spread_bps,
        "prior_spread_bps": closed.prior_spread_bps,
        "signal_spread_bps": closed.signal_spread_bps,
        "entry_spread_bps": None,
        "spread_stress_bps": spread_stress,
        "recorded_cost_bps": recorded_cost,
        "execution_cost_debit_bps": execution_debit,
        "gross_target_bps": gross_target,
        "gross_stop_bps": gross_stop,
        "p_star": p_star,
        "reservation_status": "unresolved",
        "outcome_reason": reason,
        "exit_epoch": None,
        "exit_price": None,
        "bars_held": None,
        "pnl_bps": None,
        "pnl_r": None,
        "gate_pnl_r": -(gross_stop + execution_debit) / gross_stop,
        "gate_treatment": "unresolved_missing_fill_as_adverse_stop_for_discovery_gate",
        "gate_risk_basis_bps": gross_stop,
        "full_target_win": False,
        "positive_outcome": False,
    }


def _profit_factor(values: Sequence[float]) -> tuple[float | None, bool, float, float]:
    gross_profit = math.fsum(max(float(value), 0.0) for value in values)
    gross_loss = math.fsum(max(-float(value), 0.0) for value in values)
    if gross_loss > 0.0:
        return gross_profit / gross_loss, False, gross_profit, gross_loss
    if gross_profit > 0.0:
        return None, True, gross_profit, 0.0
    return 0.0, False, 0.0, 0.0


def _profit_factor_diagnostics(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    values: list[float] = []
    valid = True
    for row in rows:
        value = _finite_float(row.get("gate_pnl_r"))
        if value is None:
            valid = False
            continue
        values.append(value)
    try:
        profit_factor, no_loss, gross_profit, gross_loss = _profit_factor(values)
    except (OverflowError, ValueError):
        profit_factor, no_loss, gross_profit, gross_loss = 0.0, False, 0.0, 0.0
        valid = False
    gate = bool(
        valid
        and (
            (no_loss and profit_factor is None)
            or (
                not no_loss
                and profit_factor is not None
                and math.isfinite(profit_factor)
                and profit_factor >= MIN_PROFIT_FACTOR
            )
        )
    )
    return {
        "gross_profit_r": gross_profit,
        "gross_loss_r": gross_loss,
        "profit_factor": profit_factor,
        "profit_factor_no_loss": no_loss,
        "gate_profit_factor": gate,
    }


def _maximum_drawdown_r(values: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for raw_value in values:
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError("drawdown input must be finite")
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _adverse_gap_diagnostics(trades: Sequence[QSCCTrade]) -> dict[str, Any]:
    gap_returns = [
        trade.pnl_r for trade in trades if trade.exit_reason == "sl_gap_open"
    ]
    return {
        "adverse_gap_stop_count": len(gap_returns),
        "adverse_gap_total_r": math.fsum(gap_returns),
        "adverse_gap_min_r": min(gap_returns) if gap_returns else None,
    }


def screen_cell(
    *,
    prepared: PreparedSeries,
    symbol: str,
    side: str,
    config: QSCCConfig,
    proxy_spread_budget_bps: float | None,
) -> dict[str, Any]:
    """Generate the complete ordered source replay for one fixed QSCC cell."""

    trades: list[QSCCTrade] = []
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
            if reason in {
                "exact_next_open_unavailable",
                "exact_next_open_gap",
            }:
                eligible_events += 1
                reserved_days.add(closed.entry_day)
                reservations.append(
                    _missing_fill_reservation_row(closed, reason=reason)
                )
                continue
            if reason == "entry_spread_above_q25_or_proxy":
                continue
            raise RuntimeError(f"QSCC completion readiness failure: {reason}")
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
            raise RuntimeError("complete QSCC horizon unexpectedly failed to score")
        trades.append(trade)
        reservations.append(_reservation_row(signal, status="scored", trade=trade))
    wins = sum(trade.full_target_win for trade in trades)
    positives = sum(trade.positive_outcome for trade in trades)
    pnl_rs = [trade.pnl_r for trade in trades]
    gate_rs = [float(row["gate_pnl_r"]) for row in reservations]
    unresolved = sum(row["reservation_status"] == "unresolved" for row in reservations)
    profit_factor = _profit_factor_diagnostics(reservations)
    gap_diagnostics = _adverse_gap_diagnostics(trades)
    return {
        "config_id": config.config_id,
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
        "maximum_drawdown_r": _maximum_drawdown_r(gate_rs),
        **profit_factor,
        **gap_diagnostics,
        "exit_mix": dict(Counter(trade.exit_reason for trade in trades)),
        "reasons": dict(sorted(reasons.items())),
        "reservation_ledger": reservations,
        "trade_ledger": [asdict(trade) for trade in trades],
        "one_trade_per_cell_entry_day": True,
        "outcome_horizon_bars": OUTCOME_HORIZON_M1_BARS,
        "quote_widening_recorded_cost_fraction": (
            config.quote_widening_recorded_cost_fraction
        ),
        "quote_convergence_prior_change_fraction": (
            config.quote_convergence_prior_change_fraction
        ),
        "prior_directional_return_vol_threshold": config.prior_directional_return_vol_threshold,
        "current_directional_return_vol_threshold": config.current_directional_return_vol_threshold,
        "maximum_signal_true_range_vol": config.maximum_signal_true_range_vol,
        "target_recorded_cost_multiple": config.target_recorded_cost_multiple,
        "stop_recorded_cost_multiple": config.stop_recorded_cost_multiple,
        "maximum_proxy_spread_bps": MAX_PROXY_SPREAD_BPS,
        "maximum_gross_stop_bps": MAX_GROSS_STOP_BPS,
        "maximum_p_star": MAX_P_STAR,
        "extra_round_trip_cost_bps": FROZEN_EXTRA_ROUND_TRIP_COST_BPS,
    }


def trial_accounting() -> dict[str, int]:
    current = len(GRID) * len(FX_SYMBOLS) * 2
    if current != IMMUTABLE_CURRENT_ATTEMPTED_CELLS:
        raise RuntimeError("QSCC fixed-family accounting changed")
    if (
        IMMUTABLE_PRIOR_ATTEMPTED_CELLS + current
        != IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    ):
        raise RuntimeError("QSCC cumulative trial ledger is inconsistent")
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
    family_alpha: float = TWO_SIDED_BONFERRONI_FAMILY_ALPHA,
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
    if deviation == 0.0:
        return 1e12 if mean > 0.0 else -1e12 if mean < 0.0 else 0.0
    return max(-1e12, min(1e12, mean / (deviation / math.sqrt(len(rows)))))


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
    if epoch == evaluation_end_epoch:
        return TEMPORAL_THIRDS - 1
    duration = evaluation_end_epoch - evaluation_start_epoch
    return min(
        TEMPORAL_THIRDS - 1,
        TEMPORAL_THIRDS * (epoch - evaluation_start_epoch) // duration,
    )


def _temporal_thirds_boundary_epochs(
    *, evaluation_start_epoch: int, evaluation_end_epoch: int
) -> tuple[int, ...]:
    if evaluation_start_epoch < 0 or evaluation_end_epoch <= evaluation_start_epoch:
        return ()
    duration = evaluation_end_epoch - evaluation_start_epoch
    return (
        evaluation_start_epoch,
        evaluation_start_epoch + (duration + TEMPORAL_THIRDS - 1) // TEMPORAL_THIRDS,
        evaluation_start_epoch
        + (2 * duration + TEMPORAL_THIRDS - 1) // TEMPORAL_THIRDS,
        evaluation_end_epoch,
    )


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
    totals = [math.fsum(values) for values in returns]
    rates = [
        wins[index] / counts[index] if counts[index] else 0.0 for index in range(3)
    ]
    means = [
        totals[index] / counts[index] if counts[index] else 0.0 for index in range(3)
    ]
    stable = bool(
        valid
        and all(
            counts[index] >= MIN_RESERVATIONS_PER_TEMPORAL_THIRD
            and TEMPORAL_THIRD_WIN_RATE_DENOMINATOR * wins[index]
            >= TEMPORAL_THIRD_WIN_RATE_NUMERATOR * counts[index]
            and totals[index] > 0.0
            for index in range(TEMPORAL_THIRDS)
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
    totals = [math.fsum(values) for values in returns]
    rates = [
        wins[index] / counts[index] if counts[index] else 0.0
        for index in range(CALENDAR_MONTHS)
    ]
    means = [
        totals[index] / counts[index] if counts[index] else 0.0
        for index in range(CALENDAR_MONTHS)
    ]
    stable = bool(
        valid
        and all(
            counts[index] >= MIN_RESERVATIONS_PER_CALENDAR_MONTH
            and CALENDAR_MONTH_WIN_RATE_DENOMINATOR * wins[index]
            >= CALENDAR_MONTH_WIN_RATE_NUMERATOR * counts[index]
            and totals[index] > 0.0
            for index in range(CALENDAR_MONTHS)
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


QSCC_CELL_SOURCE_FIELD_NAMES = frozenset(
    {
        "config_id",
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
        "gross_profit_r",
        "gross_loss_r",
        "profit_factor",
        "profit_factor_no_loss",
        "gate_profit_factor",
        "maximum_drawdown_r",
        "adverse_gap_stop_count",
        "adverse_gap_total_r",
        "adverse_gap_min_r",
        "exit_mix",
        "reasons",
        "one_trade_per_cell_entry_day",
        "outcome_horizon_bars",
        "quote_widening_recorded_cost_fraction",
        "quote_convergence_prior_change_fraction",
        "prior_directional_return_vol_threshold",
        "current_directional_return_vol_threshold",
        "maximum_signal_true_range_vol",
        "target_recorded_cost_multiple",
        "stop_recorded_cost_multiple",
        "maximum_proxy_spread_bps",
        "maximum_gross_stop_bps",
        "maximum_p_star",
        "extra_round_trip_cost_bps",
    }
)
QSCC_CELL_READINESS_FIELD_NAMES = frozenset(
    {
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
QSCC_CELL_GATE_FIELD_NAMES = frozenset(
    {
        "unique_reserved_utc_entry_days",
        "gate_full_target_reservation_rate",
        "simultaneous_wilson_lower_bound",
        "reserved_utc_day_one_sample_t",
        "temporal_thirds_boundary_epochs",
        "temporal_thirds_reservation_counts",
        "temporal_thirds_full_target_wins",
        "temporal_thirds_full_target_rates",
        "temporal_thirds_gate_total_rs",
        "temporal_thirds_gate_mean_rs",
        "gate_temporal_thirds_stable",
        "calendar_month_boundary_epochs",
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
QSCC_CELL_FIELD_NAMES = (
    QSCC_CELL_SOURCE_FIELD_NAMES
    | QSCC_CELL_READINESS_FIELD_NAMES
    | QSCC_CELL_GATE_FIELD_NAMES
)


def _passing_global_configurations(cells: Sequence[Mapping[str, Any]]) -> list[str]:
    expected = {(symbol, side) for symbol in FX_SYMBOLS for side in ("BUY", "SELL")}
    passing: list[str] = []
    for config in GRID:
        rows = [cell for cell in cells if cell.get("config_id") == config.config_id]
        keys = [
            (
                str(cell.get("symbol") or "").upper(),
                str(cell.get("side") or "").upper(),
            )
            for cell in rows
        ]
        if (
            len(rows) == len(expected)
            and len(set(keys)) == len(keys)
            and set(keys) == expected
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


def _event_identity_is_valid(row: Mapping[str, Any]) -> bool:
    config_id = row.get("config_id")
    symbol = row.get("symbol")
    side = row.get("side")
    signal_index = _exact_nonnegative_int(row.get("signal_index"))
    entry_index = _exact_nonnegative_int(row.get("entry_index"))
    signal_epoch = _exact_int(row.get("signal_epoch"))
    entry_epoch = _exact_int(row.get("entry_epoch"))
    event_id = row.get("event_id")
    if (
        config_id != "qscc_v1"
        or not isinstance(symbol, str)
        or symbol not in FX_SYMBOLS
        or not isinstance(side, str)
        or side not in {"BUY", "SELL"}
        or signal_index is None
        or entry_index is None
        or entry_index != signal_index + 1
        or signal_epoch is None
        or entry_epoch is None
        or entry_epoch != signal_epoch + 60
        or row.get("entry_day") != _utc_day(entry_epoch)
        or not isinstance(event_id, str)
    ):
        return False
    return event_id == _event_id_fields(
        config_id=str(config_id),
        symbol=symbol,
        side=side,
        signal_epoch=signal_epoch,
        entry_epoch=entry_epoch,
    )


def _signal_feature_semantics_are_valid(row: Mapping[str, Any]) -> bool:
    numbers = {
        name: _finite_float(row.get(name))
        for name in (
            "volatility_bps",
            "spread_q25_bps",
            "proxy_budget_bps",
            "spread_cap_bps",
            "prior_return_bps",
            "prior_return_vol_units",
            "current_return_bps",
            "current_return_vol_units",
            "signal_true_range_bps",
            "signal_true_range_vol_units",
            "prior_quote_change_bps",
            "signal_quote_change_bps",
            "quote_convergence_ratio",
            "prior_true_range_bps",
            "prior_true_range_vol_units",
            "baseline_end_spread_bps",
            "prior_spread_bps",
            "signal_spread_bps",
        )
    }
    if any(value is None for value in numbers.values()):
        return False
    value = {
        name: float(number) for name, number in numbers.items() if number is not None
    }
    expected_spread_cap = min(value["spread_q25_bps"], value["proxy_budget_bps"])
    if (
        value["volatility_bps"] <= 0.0
        or value["spread_q25_bps"] < 0.0
        or not 0.0 < value["proxy_budget_bps"] <= MAX_PROXY_SPREAD_BPS
        or value["prior_spread_bps"] < 0.0
        or value["signal_spread_bps"] < 0.0
        or value["baseline_end_spread_bps"] < 0.0
        or value["prior_true_range_bps"] < 0.0
        or value["signal_true_range_bps"] < 0.0
        or value["prior_quote_change_bps"] <= 0.0
        or value["spread_cap_bps"] != expected_spread_cap
        or value["baseline_end_spread_bps"] > expected_spread_cap
        or value["prior_spread_bps"] > value["proxy_budget_bps"]
        or value["signal_spread_bps"] > expected_spread_cap
        or not _numbers_match(
            value["prior_return_vol_units"],
            value["prior_return_bps"] / value["volatility_bps"],
        )
        or not _numbers_match(
            value["current_return_vol_units"],
            value["current_return_bps"] / value["volatility_bps"],
        )
        or not _numbers_match(
            value["prior_true_range_vol_units"],
            value["prior_true_range_bps"] / value["volatility_bps"],
        )
        or not _numbers_match(
            value["signal_true_range_vol_units"],
            value["signal_true_range_bps"] / value["volatility_bps"],
        )
        or value["prior_true_range_bps"]
        > MAX_SIGNAL_TRUE_RANGE_VOL * value["volatility_bps"]
        or value["signal_true_range_bps"]
        > MAX_SIGNAL_TRUE_RANGE_VOL * value["volatility_bps"]
        or not _numbers_match(
            value["quote_convergence_ratio"],
            value["signal_quote_change_bps"] / value["prior_quote_change_bps"],
        )
        or value["prior_quote_change_bps"]
        < QUOTE_WIDENING_RECORDED_COST_FRACTION
        * (value["proxy_budget_bps"] + FROZEN_EXTRA_ROUND_TRIP_COST_BPS)
        or value["signal_quote_change_bps"]
        > -QUOTE_CONVERGENCE_PRIOR_CHANGE_FRACTION * value["prior_quote_change_bps"]
    ):
        return False
    direction = 1.0 if row.get("side") == "BUY" else -1.0
    return bool(
        row.get("side") in {"BUY", "SELL"}
        and direction * value["prior_return_bps"]
        >= PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD * value["volatility_bps"]
        and direction * value["current_return_bps"]
        >= CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD * value["volatility_bps"]
    )


def _completed_signal_semantics_are_valid(
    row: Mapping[str, Any], *, missing_fill: bool
) -> bool:
    if not _event_identity_is_valid(row) or not _signal_feature_semantics_are_valid(
        row
    ):
        return False
    numeric_names = (
        "spread_stress_bps",
        "recorded_cost_bps",
        "execution_cost_debit_bps",
        "gross_target_bps",
        "gross_stop_bps",
        "p_star",
    )
    values = {name: _finite_float(row.get(name)) for name in numeric_names}
    if any(value is None for value in values.values()):
        return False
    value = {
        name: float(number) for name, number in values.items() if number is not None
    }
    proxy = _finite_float(row.get("proxy_budget_bps"))
    cap = _finite_float(row.get("spread_cap_bps"))
    baseline_end_spread = _finite_float(row.get("baseline_end_spread_bps"))
    prior_spread = _finite_float(row.get("prior_spread_bps"))
    signal_spread = _finite_float(row.get("signal_spread_bps"))
    if (
        proxy is None
        or cap is None
        or baseline_end_spread is None
        or prior_spread is None
        or signal_spread is None
    ):
        return False
    if missing_fill:
        if any(
            row.get(name) is not None
            for name in (
                "entry_price",
                "stop_price",
                "target_price",
                "entry_spread_bps",
            )
        ):
            return False
        entry_spread_for_debit = 0.0
        expected_stress = proxy
    else:
        entry_price = _finite_float(row.get("entry_price"))
        stop_price = _finite_float(row.get("stop_price"))
        target_price = _finite_float(row.get("target_price"))
        entry_spread = _finite_float(row.get("entry_spread_bps"))
        if (
            entry_price is None
            or stop_price is None
            or target_price is None
            or entry_spread is None
            or entry_price <= 0.0
            or stop_price <= 0.0
            or target_price <= 0.0
            or entry_spread < 0.0
            or entry_spread > cap
        ):
            return False
        entry_spread_for_debit = entry_spread
        expected_stress = max(
            baseline_end_spread,
            prior_spread,
            signal_spread,
            entry_spread,
            proxy,
        )
        if row.get("side") == "BUY":
            if not (
                stop_price == entry_price * (1.0 - value["gross_stop_bps"] / 1e4)
                and target_price
                == entry_price * (1.0 + value["gross_target_bps"] / 1e4)
            ):
                return False
        elif not (
            stop_price == entry_price * (1.0 + value["gross_stop_bps"] / 1e4)
            and target_price == entry_price * (1.0 - value["gross_target_bps"] / 1e4)
        ):
            return False
    expected_cost = expected_stress + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    expected_debit = (
        max(0.0, expected_stress - entry_spread_for_debit)
        + FROZEN_EXTRA_ROUND_TRIP_COST_BPS
    )
    expected_target = TARGET_RECORDED_COST_MULTIPLE * expected_cost
    expected_stop = STOP_RECORDED_COST_MULTIPLE * expected_cost
    expected_p_star = (8.0 + expected_debit / expected_cost) / 12.0
    return bool(
        value["spread_stress_bps"] == expected_stress
        and value["recorded_cost_bps"] == expected_cost
        and value["execution_cost_debit_bps"] == expected_debit
        and value["gross_target_bps"] == expected_target
        and value["gross_stop_bps"] == expected_stop
        and expected_stop <= MAX_GROSS_STOP_BPS
        and value["gross_stop_bps"] <= MAX_GROSS_STOP_BPS
        and _p_star_forms_agree(
            normalized_p_star=expected_p_star,
            gross_stop_bps=expected_stop,
            execution_cost_debit_bps=expected_debit,
            gross_target_bps=expected_target,
        )
        and value["p_star"] == expected_p_star
        and expected_p_star <= MAX_P_STAR
        and value["p_star"] <= MAX_P_STAR
    )


def validate_trade_row(row: Mapping[str, Any]) -> bool:
    if set(row) != QSCC_TRADE_FIELD_NAMES or not _all_json_numbers_finite(dict(row)):
        return False
    event_id = row.get("event_id")
    symbol = row.get("symbol")
    side = row.get("side")
    signal_epoch = _exact_int(row.get("signal_epoch"))
    entry_epoch = _exact_int(row.get("entry_epoch"))
    exit_epoch = _exact_int(row.get("exit_epoch"))
    bars_held = _exact_nonnegative_int(row.get("bars_held"))
    entry_price = _finite_float(row.get("entry_price"))
    stop_price = _finite_float(row.get("stop_price"))
    target_price = _finite_float(row.get("target_price"))
    exit_price = _finite_float(row.get("exit_price"))
    stop = _finite_float(row.get("gross_stop_bps"))
    target = _finite_float(row.get("gross_target_bps"))
    debit = _finite_float(row.get("execution_cost_debit_bps"))
    pnl_bps = _finite_float(row.get("pnl_bps"))
    pnl_r = _finite_float(row.get("pnl_r"))
    reason = row.get("exit_reason")
    if (
        row.get("config_id") != "qscc_v1"
        or not isinstance(symbol, str)
        or symbol not in FX_SYMBOLS
        or not isinstance(side, str)
        or side not in {"BUY", "SELL"}
        or signal_epoch is None
        or entry_epoch != signal_epoch + 60
        or exit_epoch is None
        or exit_epoch < entry_epoch
        or bars_held is None
        or not 1 <= bars_held <= OUTCOME_HORIZON_M1_BARS
        or exit_epoch != entry_epoch + (bars_held - 1) * 60
        or row.get("entry_day") != _utc_day(entry_epoch)
        or entry_price is None
        or stop_price is None
        or target_price is None
        or exit_price is None
        or stop is None
        or target is None
        or debit is None
        or pnl_bps is None
        or pnl_r is None
        or min(entry_price, stop_price, target_price, exit_price, stop, target) <= 0.0
        or debit < 0.0
        or reason
        not in {
            "sl_gap_open",
            "tp_gap_open",
            "sl_double_touch",
            "sl",
            "tp",
            "time_stop",
        }
        or event_id
        != _event_id_fields(
            config_id="qscc_v1",
            symbol=symbol,
            side=side,
            signal_epoch=signal_epoch,
            entry_epoch=entry_epoch,
        )
    ):
        return False
    gross = (
        (exit_price - entry_price) / entry_price * 1e4
        if side == "BUY"
        else (entry_price - exit_price) / entry_price * 1e4
    )
    expected_pnl = gross - debit
    full_target = reason in {"tp", "tp_gap_open"} and expected_pnl > 0.0
    if side == "BUY":
        bracket_valid = bool(
            stop_price == entry_price * (1.0 - stop / 1e4)
            and target_price == entry_price * (1.0 + target / 1e4)
        )
        adverse_gap_valid = exit_price <= stop_price
    else:
        bracket_valid = bool(
            stop_price == entry_price * (1.0 + stop / 1e4)
            and target_price == entry_price * (1.0 - target / 1e4)
        )
        adverse_gap_valid = exit_price >= stop_price
    if reason == "time_stop" and bars_held != OUTCOME_HORIZON_M1_BARS:
        return False
    if reason in {"tp", "tp_gap_open"} and exit_price != target_price:
        return False
    if reason in {"sl", "sl_double_touch"} and exit_price != stop_price:
        return False
    if reason == "sl_gap_open" and not adverse_gap_valid:
        return False
    return bool(
        bracket_valid
        and pnl_bps == expected_pnl
        and pnl_r == expected_pnl / stop
        and row.get("full_target_win") is full_target
        and row.get("positive_outcome") is (expected_pnl > 0.0)
    )


def validate_reservation_row(row: Mapping[str, Any]) -> bool:
    missing = "gate_risk_basis_bps" in row
    expected_fields = (
        QSCC_MISSING_FILL_RESERVATION_FIELD_NAMES
        if missing
        else QSCC_RESERVATION_FIELD_NAMES
    )
    if set(row) != expected_fields or not _all_json_numbers_finite(dict(row)):
        return False
    if not _completed_signal_semantics_are_valid(row, missing_fill=missing):
        return False
    status = row.get("reservation_status")
    reason = row.get("outcome_reason")
    gate_return = _finite_float(row.get("gate_pnl_r"))
    stop = _finite_float(row.get("gross_stop_bps"))
    debit = _finite_float(row.get("execution_cost_debit_bps"))
    if gate_return is None or stop is None or debit is None:
        return False
    if missing:
        return bool(
            status == "unresolved"
            and reason
            in {
                "exact_next_open_unavailable",
                "exact_next_open_gap",
            }
            and row.get("gate_treatment")
            == "unresolved_missing_fill_as_adverse_stop_for_discovery_gate"
            and row.get("gate_risk_basis_bps") == stop
            and gate_return == -(stop + debit) / stop
            and all(
                row.get(name) is None
                for name in (
                    "exit_epoch",
                    "exit_price",
                    "bars_held",
                    "pnl_bps",
                    "pnl_r",
                )
            )
            and row.get("full_target_win") is False
            and row.get("positive_outcome") is False
        )
    if status == "unresolved":
        return bool(
            reason == "incomplete_outcome_horizon"
            and row.get("gate_treatment")
            == "unresolved_incomplete_horizon_as_adverse_stop_for_discovery_gate"
            and gate_return == -(stop + debit) / stop
            and all(
                row.get(name) is None
                for name in (
                    "exit_epoch",
                    "exit_price",
                    "bars_held",
                    "pnl_bps",
                    "pnl_r",
                )
            )
            and row.get("full_target_win") is False
            and row.get("positive_outcome") is False
        )
    if status != "scored" or row.get("gate_treatment") != "observed_trade":
        return False
    trade_row = {
        name: row.get("outcome_reason") if name == "exit_reason" else row.get(name)
        for name in QSCC_TRADE_FIELD_NAMES
    }
    return bool(validate_trade_row(trade_row) and gate_return == row.get("pnl_r"))


def validate_reservation_ledger_payload(payload: Any) -> bool:
    rows: Any = payload
    if isinstance(payload, Mapping):
        if set(payload) != {"schema_version", "family", "reservations"}:
            return False
        if (
            payload.get("schema_version")
            != "fxstack.scalp.quote_side_convergence_continuation_reservation_ledger.v1"
            or payload.get("family") != "quote_side_convergence_continuation"
        ):
            return False
        rows = payload.get("reservations")
    if not isinstance(rows, list):
        return False
    return bool(
        _all_json_numbers_finite(rows)
        and all(
            isinstance(row, Mapping) and validate_reservation_row(row) for row in rows
        )
        and len({row["event_id"] for row in rows}) == len(rows)
    )


def validate_trade_ledger_payload(payload: Any) -> bool:
    rows: Any = payload
    if isinstance(payload, Mapping):
        if set(payload) != {"schema_version", "family", "trades"}:
            return False
        if (
            payload.get("schema_version")
            != "fxstack.scalp.quote_side_convergence_continuation_trade_ledger.v1"
            or payload.get("family") != "quote_side_convergence_continuation"
        ):
            return False
        rows = payload.get("trades")
    if not isinstance(rows, list):
        return False
    return bool(
        _all_json_numbers_finite(rows)
        and all(isinstance(row, Mapping) and validate_trade_row(row) for row in rows)
        and len({row["event_id"] for row in rows}) == len(rows)
    )


def validate_cells_payload(cells: Any) -> bool:
    if not isinstance(cells, list) or len(cells) != IMMUTABLE_CURRENT_ATTEMPTED_CELLS:
        return False
    if not _all_json_numbers_finite(cells):
        return False
    keys: list[tuple[str, str, str]] = []
    for cell in cells:
        if not isinstance(cell, Mapping) or set(cell) != QSCC_CELL_FIELD_NAMES:
            return False
        key = _ledger_cell_key(cell)
        if (
            key[0] != "qscc_v1"
            or key[1] not in FX_SYMBOLS
            or key[2] not in {"BUY", "SELL"}
            or not isinstance(cell.get("passes_discovery_cell_gate"), bool)
        ):
            return False
        keys.append(key)
    expected = {
        ("qscc_v1", symbol, side) for symbol in FX_SYMBOLS for side in ("BUY", "SELL")
    }
    return len(set(keys)) == len(keys) and set(keys) == expected


def validate_ledger_consistency(
    *,
    cells: Sequence[Mapping[str, Any]],
    reservation_ledger: Sequence[Mapping[str, Any]],
    trade_ledger: Sequence[Mapping[str, Any]],
) -> bool:
    if not validate_reservation_ledger_payload(list(reservation_ledger)):
        return False
    if not validate_trade_ledger_payload(list(trade_ledger)):
        return False
    cell_keys = {_ledger_cell_key(cell) for cell in cells}
    if len(cell_keys) != len(cells):
        return False
    reservations_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {
        key: [] for key in cell_keys
    }
    trades_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {
        key: [] for key in cell_keys
    }
    for row in reservation_ledger:
        key = _ledger_cell_key(row)
        if key not in reservations_by_key:
            return False
        reservations_by_key[key].append(row)
    for row in trade_ledger:
        key = _ledger_cell_key(row)
        if key not in trades_by_key:
            return False
        trades_by_key[key].append(row)
    for cell in cells:
        key = _ledger_cell_key(cell)
        reservations = reservations_by_key[key]
        trades = trades_by_key[key]
        if cell.get("reservation_event_ids") != [
            row["event_id"] for row in reservations
        ]:
            return False
        if cell.get("trade_event_ids") != [row["event_id"] for row in trades]:
            return False
        scored = [
            row["event_id"]
            for row in reservations
            if row.get("reservation_status") == "scored"
        ]
        if Counter(scored) != Counter(row["event_id"] for row in trades):
            return False
        trade_by_id = {row["event_id"]: row for row in trades}
        for reservation in reservations:
            if reservation.get("reservation_status") != "scored":
                continue
            projected = {
                name: (
                    reservation.get("outcome_reason")
                    if name == "exit_reason"
                    else reservation.get(name)
                )
                for name in QSCC_TRADE_FIELD_NAMES
            }
            if projected != trade_by_id.get(reservation["event_id"]):
                return False
    return True


def _mapping_matches_exact_generated_row(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    return bool(
        set(actual) == set(expected)
        and _all_json_numbers_finite(dict(actual))
        and dumps_strict_json(dict(actual), indent=None)
        == dumps_strict_json(dict(expected), indent=None)
    )


def _row_is_within_evaluation_window(
    row: Mapping[str, Any], *, evaluation_start_epoch: int, evaluation_end_epoch: int
) -> bool:
    signal_epoch = _exact_int(row.get("signal_epoch"))
    entry_epoch = _exact_int(row.get("entry_epoch"))
    exit_epoch = row.get("exit_epoch")
    if (
        signal_epoch is None
        or entry_epoch is None
        or not evaluation_start_epoch <= signal_epoch < evaluation_end_epoch
        or not evaluation_start_epoch <= entry_epoch <= evaluation_end_epoch
    ):
        return False
    if exit_epoch is None:
        return True
    parsed_exit = _exact_int(exit_epoch)
    return bool(
        parsed_exit is not None and entry_epoch <= parsed_exit < evaluation_end_epoch
    )


def _cell_contract_is_valid(
    cell: Mapping[str, Any],
    *,
    reservations: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
) -> bool:
    if set(cell) != QSCC_CELL_SOURCE_FIELD_NAMES | QSCC_CELL_READINESS_FIELD_NAMES:
        return False
    if not _all_json_numbers_finite(dict(cell)):
        return False
    reservation_gate_values = [float(row["gate_pnl_r"]) for row in reservations]
    trade_values = [float(row["pnl_r"]) for row in trades]
    wins = sum(row.get("full_target_win") is True for row in trades)
    positives = sum(row.get("positive_outcome") is True for row in trades)
    unresolved = sum(
        row.get("reservation_status") == "unresolved" for row in reservations
    )
    profit_factor = _profit_factor_diagnostics(reservations)
    gap_rows = [row for row in trades if row.get("exit_reason") == "sl_gap_open"]
    gap_values = [float(row["pnl_r"]) for row in gap_rows]
    expected_scalars: dict[str, Any] = {
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
        "total_r": math.fsum(trade_values),
        "mean_r": statistics.fmean(trade_values) if trade_values else 0.0,
        "gate_total_r": math.fsum(reservation_gate_values),
        "gate_mean_r": (
            statistics.fmean(reservation_gate_values)
            if reservation_gate_values
            else 0.0
        ),
        "maximum_drawdown_r": _maximum_drawdown_r(reservation_gate_values),
        "adverse_gap_stop_count": len(gap_rows),
        "adverse_gap_total_r": math.fsum(gap_values),
        "adverse_gap_min_r": min(gap_values) if gap_values else None,
        **profit_factor,
    }
    for name, expected in expected_scalars.items():
        actual = cell.get(name)
        if expected is None:
            if actual is not None:
                return False
        elif isinstance(expected, bool) or isinstance(expected, int):
            if actual != expected or type(actual) is not type(expected):
                return False
        elif not _numbers_match(actual, expected):
            return False
    if cell.get("reservation_event_ids") != [row["event_id"] for row in reservations]:
        return False
    if cell.get("trade_event_ids") != [row["event_id"] for row in trades]:
        return False
    if cell.get("exit_mix") != dict(Counter(row["exit_reason"] for row in trades)):
        return False
    return bool(
        cell.get("config_id") == "qscc_v1"
        and str(cell.get("symbol") or "").upper() in FX_SYMBOLS
        and str(cell.get("side") or "").upper() in {"BUY", "SELL"}
        and _exact_nonnegative_int(cell.get("closed_signal_events")) is not None
        and _exact_nonnegative_int(cell.get("eligible_events")) is not None
        and int(cell["eligible_events"]) >= len(reservations)
        and cell.get("one_trade_per_cell_entry_day") is True
        and cell.get("outcome_horizon_bars") == OUTCOME_HORIZON_M1_BARS
        and cell.get("quote_widening_recorded_cost_fraction")
        == QUOTE_WIDENING_RECORDED_COST_FRACTION
        and cell.get("quote_convergence_prior_change_fraction")
        == QUOTE_CONVERGENCE_PRIOR_CHANGE_FRACTION
        and cell.get("prior_directional_return_vol_threshold")
        == PRIOR_DIRECTIONAL_RETURN_VOL_THRESHOLD
        and cell.get("current_directional_return_vol_threshold")
        == CURRENT_DIRECTIONAL_RETURN_VOL_THRESHOLD
        and cell.get("maximum_signal_true_range_vol") == MAX_SIGNAL_TRUE_RANGE_VOL
        and cell.get("target_recorded_cost_multiple") == TARGET_RECORDED_COST_MULTIPLE
        and cell.get("stop_recorded_cost_multiple") == STOP_RECORDED_COST_MULTIPLE
        and cell.get("maximum_proxy_spread_bps") == MAX_PROXY_SPREAD_BPS
        and cell.get("maximum_gross_stop_bps") == MAX_GROSS_STOP_BPS
        and cell.get("maximum_p_star") == MAX_P_STAR
        and cell.get("extra_round_trip_cost_bps") == FROZEN_EXTRA_ROUND_TRIP_COST_BPS
        and cell.get("economic_claim_ready") is False
        and cell.get("economics_claim_ready") is False
    )


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
    reservations_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    trades_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in reservation_ledger:
        reservations_by_key.setdefault(_ledger_cell_key(row), []).append(row)
    for row in trade_ledger:
        trades_by_key.setdefault(_ledger_cell_key(row), []).append(row)
    global_ledger_consistency = validate_ledger_consistency(
        cells=cells,
        reservation_ledger=reservation_ledger,
        trade_ledger=trade_ledger,
    )
    for cell in cells:
        key = _ledger_cell_key(cell)
        reservations = reservations_by_key.get(key, [])
        trades = trades_by_key.get(key, [])
        config = QSCC_CONFIG_BY_ID.get(key[0])
        prepared = prepared_series_by_symbol.get(key[1])
        expected_budget = expected_proxy_spread_budgets_bps.get(key[1])

        row_semantics = bool(
            all(validate_reservation_row(row) for row in reservations)
            and all(validate_trade_row(row) for row in trades)
        )
        days = [str(row.get("entry_day") or "") for row in reservations]
        unique_days = len(set(days))
        day_consistency = bool(
            all(days) and unique_days == len(days) or not reservations
        )
        temporal_scope = all(
            _row_is_within_evaluation_window(
                row,
                evaluation_start_epoch=evaluation_start_epoch,
                evaluation_end_epoch=evaluation_end_epoch,
            )
            for row in reservations
        ) and all(
            _row_is_within_evaluation_window(
                row,
                evaluation_start_epoch=evaluation_start_epoch,
                evaluation_end_epoch=evaluation_end_epoch,
            )
            for row in trades
        )
        proxy_binding = bool(
            expected_budget is not None
            and cell.get("proxy_budget_bps") == expected_budget
            and all(
                row.get("proxy_budget_bps") == expected_budget
                for row in (*reservations, *trades)
                if "proxy_budget_bps" in row
            )
        )

        scored_ids = [
            str(row.get("event_id"))
            for row in reservations
            if row.get("reservation_status") == "scored"
        ]
        trade_ids = [str(row.get("event_id")) for row in trades]
        scored_trade_match = Counter(scored_ids) == Counter(trade_ids)
        actual_unresolved = sum(
            row.get("reservation_status") == "unresolved" for row in reservations
        )

        pre_gate_cell = dict(cell)
        cell_contract = _cell_contract_is_valid(
            pre_gate_cell, reservations=reservations, trades=trades
        )
        source_replay = False
        if config is not None and prepared is not None:
            replay = screen_cell(
                prepared=prepared,
                symbol=key[1],
                side=key[2],
                config=config,
                proxy_spread_budget_bps=(
                    expected_budget if cell.get("proxy_budget_ready") is True else None
                ),
            )
            replay_reservations = replay.pop("reservation_ledger")
            replay_trades = replay.pop("trade_ledger")
            replay.update(
                {
                    "proxy_budget_bps": cell.get("proxy_budget_bps"),
                    "cost_mode": cell.get("cost_mode"),
                    "proxy_contract_ready": cell.get("proxy_contract_ready"),
                    "proxy_budget_ready": cell.get("proxy_budget_ready"),
                    "source_ready": cell.get("source_ready"),
                    "source_error": cell.get("source_error"),
                    "economic_claim_ready": False,
                    "economics_claim_ready": False,
                    "reservation_event_ids": [
                        row["event_id"] for row in replay_reservations
                    ],
                    "trade_event_ids": [row["event_id"] for row in replay_trades],
                }
            )
            source_replay = bool(
                _mapping_matches_exact_generated_row(pre_gate_cell, replay)
                and list(reservations) == replay_reservations
                and list(trades) == replay_trades
            )

        wins = sum(row.get("full_target_win") is True for row in reservations)
        gate_values = [
            float(value)
            for row in reservations
            if (value := _finite_float(row.get("gate_pnl_r"))) is not None
        ]
        reservation_rate = wins / len(reservations) if reservations else 0.0
        wilson = _one_sided_wilson_lower_bound(wins, len(reservations))
        t_statistic = _finite_one_sample_t(gate_values)
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
        profit_factor = _profit_factor_diagnostics(reservations)
        aggregate_consistency = bool(
            len(gate_values) == len(reservations)
            and _numbers_match(cell.get("gate_total_r"), math.fsum(gate_values))
            and _numbers_match(
                cell.get("gate_mean_r"),
                statistics.fmean(gate_values) if gate_values else 0.0,
            )
            and all(cell.get(name) == value for name, value in profit_factor.items())
        )
        gate_ledger = bool(
            global_ledger_consistency
            and day_consistency
            and aggregate_consistency
            and int(cell.get("scored_trades") or 0) == len(scored_ids)
            and int(cell.get("unresolved_reservations") or 0) == actual_unresolved
        )
        updates: dict[str, Any] = {
            "unique_reserved_utc_entry_days": unique_days,
            "gate_full_target_reservation_rate": reservation_rate,
            "simultaneous_wilson_lower_bound": wilson,
            "reserved_utc_day_one_sample_t": t_statistic,
            "temporal_thirds_boundary_epochs": list(
                _temporal_thirds_boundary_epochs(
                    evaluation_start_epoch=evaluation_start_epoch,
                    evaluation_end_epoch=evaluation_end_epoch,
                )
            ),
            **thirds,
            "calendar_month_boundary_epochs": list(CALENDAR_MONTH_BOUNDARY_EPOCHS),
            **months,
            "gate_ledger_consistent": gate_ledger,
            "gate_actual_scored_reservations": len(scored_ids),
            "gate_actual_unresolved_reservations": actual_unresolved,
            "gate_scored_trade_ids_match": scored_trade_match,
            "gate_row_semantics_consistent": row_semantics,
            "gate_temporal_scope_consistent": temporal_scope,
            "gate_proxy_contract_binding_consistent": proxy_binding,
            "gate_source_replay_consistent": source_replay,
            "gate_cell_contract_consistent": cell_contract,
        }
        updates["passes_discovery_cell_gate"] = bool(
            cell.get("source_ready") is True
            and cell.get("proxy_contract_ready") is True
            and cell.get("proxy_budget_ready") is True
            and gate_ledger
            and len(scored_ids) >= MIN_TRADES_PER_CELL
            and unique_days >= MIN_UNIQUE_RESERVED_UTC_ENTRY_DAYS
            and reservation_rate >= MIN_OBSERVED_FULL_TARGET_RATE
            and wilson >= MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
            and math.fsum(gate_values) > 0.0
            and (statistics.fmean(gate_values) if gate_values else 0.0) > 0.0
            and t_statistic >= TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
            and thirds["gate_temporal_thirds_stable"] is True
            and months["gate_calendar_months_stable"] is True
            and profit_factor["gate_profit_factor"] is True
            and scored_trade_match
            and row_semantics
            and temporal_scope
            and proxy_binding
            and source_replay
            and cell_contract
        )
        cell.update(updates)

    passing = _passing_global_configurations(cells)
    gap_returns = [
        float(row["pnl_r"])
        for row in trade_ledger
        if row.get("exit_reason") == "sl_gap_open"
    ]
    return {
        "minimum_scored_trades_per_cell": MIN_TRADES_PER_CELL,
        "minimum_unique_reserved_utc_entry_days_per_cell": (
            MIN_UNIQUE_RESERVED_UTC_ENTRY_DAYS
        ),
        "minimum_observed_full_target_reservation_rate": (
            MIN_OBSERVED_FULL_TARGET_RATE
        ),
        "minimum_simultaneous_wilson_lower_bound": (
            MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
        ),
        "simultaneous_wilson_family_cells": SIMULTANEOUS_WILSON_FAMILY_CELLS,
        "minimum_reserved_utc_day_one_sample_t": (
            TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        ),
        "minimum_profit_factor": MIN_PROFIT_FACTOR,
        "minimum_reservations_per_temporal_third": (
            MIN_RESERVATIONS_PER_TEMPORAL_THIRD
        ),
        "minimum_reservations_per_calendar_month": (
            MIN_RESERVATIONS_PER_CALENDAR_MONTH
        ),
        "passing_global_configurations": passing,
        "all_pair_directions_same_configuration": passing == ["qscc_v1"],
        "discovery_survivor": passing[0] if len(passing) == 1 else None,
        "complete_source_replay_required": True,
        "all_reservations_included_in_gates": True,
        "within_cell_serial_dependence_robust": False,
        "inferential_calibration_authorized": False,
        "maximum_cell_drawdown_r": max(
            (float(cell["maximum_drawdown_r"]) for cell in cells), default=0.0
        ),
        "adverse_gap_stop_count": len(gap_returns),
        "adverse_gap_total_r": math.fsum(gap_returns),
        "adverse_gap_min_r": min(gap_returns) if gap_returns else None,
        "success_claim_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def _has_scorable_run(bars: Sequence[QuoteBar]) -> bool:
    minimum = BASELINE_M1_BARS + 2 + FILL_DELAY_M1_BARS + OUTCOME_HORIZON_M1_BARS
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
        character not in "0123456789abcdefABCDEF" for character in snapshot_sha
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
        value = _valid_proxy_budget(raw_value)
        if value is None:
            raise ValueError(
                f"invalid proxy budget for {symbol}; expected 0 < bps <= 3"
            )
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
    """Screen the exact 36-cell QSCC family with permanent research-only authority."""

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
    errors: dict[str, str] = {}
    for raw_symbol, raw_error in (source_errors or {}).items():
        symbol = str(raw_symbol).strip().upper()
        if symbol not in FX_SYMBOLS:
            raise ValueError(f"noncanonical source-error symbol: {symbol}")
        if symbol in errors:
            raise ValueError(f"duplicate normalized source-error symbol: {symbol}")
        errors[symbol] = str(raw_error)
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
            prepared = prepare_series([])
        elif any(
            bar.epoch < evaluation_start_epoch or bar.epoch >= evaluation_end_epoch
            for bar in prepared.bars
        ):
            errors[symbol] = "bar_outside_evaluation_window"
        elif not prepared.bars:
            errors[symbol] = "empty_after_date_filter"
        elif not _has_scorable_run(prepared.bars):
            errors[symbol] = "no_complete_273_bar_m1_run"

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
        "schema_version": "fxstack.scalp.quote_side_convergence_continuation_screen.v1",
        "family": "quote_side_convergence_continuation",
        "acronym": "QSCC",
        "research_only": True,
        "discovery_only": True,
        "future_data_access_for_signal": "forbidden_except_exact_t_plus_1_open_fill",
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "promotion_authorized": False,
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
                "completed-close proportional-spread convergence followed by "
                "same-direction midpoint-return continuation"
            ),
            "baseline": (
                "median of exactly 240 midpoint true ranges and nearest-rank Q25 "
                "sorted index 59 of exactly 240 close spreads, t-241 through "
                "t-2 inclusive; t-242 supplies the first true-range predecessor"
            ),
            "completed_close_formulas": (
                "M_j=(ask_close_j+bid_close_j)/2; "
                "r_j=1e4*log(M_j/M_(j-1)); "
                "q_j=1e4*(log(ask_close_j/bid_close_j)-"
                "log(ask_close_(j-1)/bid_close_(j-1)))"
            ),
            "quote_change_interpretation": (
                "q>0 means only that proportional spread is wider at the later "
                "completed close; no within-minute path, sequencing, or "
                "order-flow claim"
            ),
            "buy": ("q(t-1)>=0.10K, q(t)<=-0.50q(t-1), r(t-1)>=0.35V, r(t)>=0.10V"),
            "sell": ("q(t-1)>=0.10K, q(t)<=-0.50q(t-1), r(t-1)<=-0.35V, r(t)<=-0.10V"),
            "signal_range": "midpoint TR(t-1)<=2.5V and TR(t)<=2.5V",
            "spread": (
                "spread(t-2)<=cap, spread(t-1)<=P, spread(t)<=cap, and exact "
                "t+1 open spread<=cap, where cap=min(Q25,P)"
            ),
            "fill": "exact t+1 ask open BUY / bid open SELL",
            "recorded_cost": (
                "S=max(spread(t-2),spread(t-1),spread(t),entry spread,P)=P; K=C=S+1bp"
            ),
            "execution_debit": "D=max(0,S-entry spread)+1bp",
            "gross_target": "T=4C",
            "gross_stop": "R=8C and R<=32bp",
            "p_star": "(R+D)/(R+T)=(8+D/C)/12<=3/4",
            "outcome_horizon_m1_bars": OUTCOME_HORIZON_M1_BARS,
            "reserve_before_horizon_check": True,
            "missing_exact_fill_treatment": (
                "only unavailable or gapped exact t+1 reserves expected day; "
                "S=P,C=D=P+1,T=4C,R=8C; gate pnl R=-9/8"
            ),
            "present_invalid_exact_fill_treatment": "readiness-fatal",
            "known_entry_spread_rejection_treatment": "reject without reservation",
            "one_trade_per_cell_entry_day": True,
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
                symbol: normalized_budgets.get(symbol) for symbol in FX_SYMBOLS
            },
            "maximum_proxy_budget_bps": MAX_PROXY_SPREAD_BPS,
            "observed_source_bid_ask_used": True,
            "proxy_used_as_cost_stress": True,
            "extra_adverse_round_trip_cost_bps": (FROZEN_EXTRA_ROUND_TRIP_COST_BPS),
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
        },
        "discovery_gate": discovery_gate,
        "reservation_ledger": all_reservations,
        "trade_ledger": all_trades,
        "cells": cells,
    }


QSCC_RESULT_FIELD_NAMES = frozenset(
    {
        "schema_version",
        "family",
        "acronym",
        "research_only",
        "discovery_only",
        "future_data_access_for_signal",
        "success_claim_authorized",
        "holdout_access_authorized",
        "promotion_authorized",
        "activation_authorized",
        "registry_write_authorized",
        "order_authorized",
        "economic_passed",
        "economic_claim_ready",
        "economics_claim_ready",
        "economic_claim_scope",
        "symbols",
        "grid",
        "fixed_contract",
        "cost_readiness",
        "search_accounting",
        "discovery_gate",
        "reservation_ledger",
        "trade_ledger",
        "cells",
    }
)


def _cell_gate_fields_are_valid(
    cell: Mapping[str, Any],
    *,
    reservations: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    evaluation_start_epoch: int,
    evaluation_end_epoch: int,
) -> bool:
    pre_gate = {
        name: cell.get(name)
        for name in QSCC_CELL_SOURCE_FIELD_NAMES | QSCC_CELL_READINESS_FIELD_NAMES
    }
    if not _cell_contract_is_valid(pre_gate, reservations=reservations, trades=trades):
        return False
    days = [str(row.get("entry_day") or "") for row in reservations]
    if len(set(days)) != len(days) or any(not day for day in days):
        return False
    wins = sum(row.get("full_target_win") is True for row in reservations)
    rate = wins / len(reservations) if reservations else 0.0
    values = [float(row["gate_pnl_r"]) for row in reservations]
    scored_ids = [
        row["event_id"]
        for row in reservations
        if row.get("reservation_status") == "scored"
    ]
    trade_ids = [row["event_id"] for row in trades]
    unresolved = sum(
        row.get("reservation_status") == "unresolved" for row in reservations
    )
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
    exact_values: dict[str, Any] = {
        "unique_reserved_utc_entry_days": len(set(days)),
        "gate_full_target_reservation_rate": rate,
        "simultaneous_wilson_lower_bound": _one_sided_wilson_lower_bound(
            wins, len(reservations)
        ),
        "reserved_utc_day_one_sample_t": _finite_one_sample_t(values),
        "temporal_thirds_boundary_epochs": list(
            _temporal_thirds_boundary_epochs(
                evaluation_start_epoch=evaluation_start_epoch,
                evaluation_end_epoch=evaluation_end_epoch,
            )
        ),
        **thirds,
        "calendar_month_boundary_epochs": list(CALENDAR_MONTH_BOUNDARY_EPOCHS),
        **months,
        "gate_actual_scored_reservations": len(scored_ids),
        "gate_actual_unresolved_reservations": unresolved,
        "gate_scored_trade_ids_match": Counter(scored_ids) == Counter(trade_ids),
        "gate_row_semantics_consistent": True,
        "gate_temporal_scope_consistent": all(
            _row_is_within_evaluation_window(
                row,
                evaluation_start_epoch=evaluation_start_epoch,
                evaluation_end_epoch=evaluation_end_epoch,
            )
            for row in (*reservations, *trades)
        ),
        "gate_source_replay_consistent": True,
        "gate_cell_contract_consistent": True,
        "gate_ledger_consistent": True,
    }
    for name, expected in exact_values.items():
        actual = cell.get(name)
        if isinstance(expected, float):
            if not _numbers_match(actual, expected):
                return False
        elif actual != expected or type(actual) is not type(expected):
            return False
    if cell.get("gate_proxy_contract_binding_consistent") is not True:
        return False
    profit_factor = _profit_factor_diagnostics(reservations)
    expected_pass = bool(
        cell.get("source_ready") is True
        and cell.get("proxy_contract_ready") is True
        and cell.get("proxy_budget_ready") is True
        and len(scored_ids) >= MIN_TRADES_PER_CELL
        and len(set(days)) >= MIN_UNIQUE_RESERVED_UTC_ENTRY_DAYS
        and rate >= MIN_OBSERVED_FULL_TARGET_RATE
        and float(exact_values["simultaneous_wilson_lower_bound"])
        >= MIN_SIMULTANEOUS_WILSON_LOWER_BOUND
        and math.fsum(values) > 0.0
        and (statistics.fmean(values) if values else 0.0) > 0.0
        and float(exact_values["reserved_utc_day_one_sample_t"])
        >= TWO_SIDED_BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        and thirds["gate_temporal_thirds_stable"] is True
        and months["gate_calendar_months_stable"] is True
        and profit_factor["gate_profit_factor"] is True
    )
    return cell.get("passes_discovery_cell_gate") is expected_pass


def validate_result_bundle(result: Mapping[str, Any]) -> bool:
    """Validate complete schemas, ledgers, aggregates, gates, and no-authority flags."""

    if set(result) != QSCC_RESULT_FIELD_NAMES or not _all_json_numbers_finite(
        dict(result)
    ):
        return False
    if (
        result.get("schema_version")
        != "fxstack.scalp.quote_side_convergence_continuation_screen.v1"
        or result.get("family") != "quote_side_convergence_continuation"
        or result.get("acronym") != "QSCC"
        or result.get("research_only") is not True
        or result.get("discovery_only") is not True
        or result.get("success_claim_authorized") is not False
        or result.get("holdout_access_authorized") is not False
        or result.get("promotion_authorized") is not False
        or result.get("activation_authorized") is not False
        or result.get("registry_write_authorized") is not False
        or result.get("order_authorized") is not False
        or result.get("economic_passed") is not False
        or result.get("economic_claim_ready") is not False
        or result.get("economics_claim_ready") is not False
        or result.get("symbols") != list(FX_SYMBOLS)
        or result.get("grid")
        != [asdict(config) | {"config_id": config.config_id} for config in GRID]
        or result.get("search_accounting", {}).get("current_attempted_cells")
        != IMMUTABLE_CURRENT_ATTEMPTED_CELLS
        or result.get("search_accounting", {}).get("prior_attempted_cells")
        != IMMUTABLE_PRIOR_ATTEMPTED_CELLS
        or result.get("search_accounting", {}).get("cumulative_attempted_cells")
        != IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    ):
        return False
    cells = result.get("cells")
    reservations = result.get("reservation_ledger")
    trades = result.get("trade_ledger")
    if (
        not validate_cells_payload(cells)
        or not validate_reservation_ledger_payload(reservations)
        or not validate_trade_ledger_payload(trades)
        or not validate_ledger_consistency(
            cells=cells,
            reservation_ledger=reservations,
            trade_ledger=trades,
        )
    ):
        return False
    readiness = result.get("cost_readiness")
    if not isinstance(readiness, Mapping):
        return False
    try:
        evaluation_start_epoch = _parse_epoch(str(readiness["evaluation_start_utc"]))
        evaluation_end_epoch = _parse_epoch(str(readiness["evaluation_end_utc"]))
    except (KeyError, TypeError, ValueError):
        return False
    if evaluation_start_epoch is None or evaluation_end_epoch is None:
        return False
    reservations_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    trades_by_key: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for row in reservations:
        reservations_by_key.setdefault(_ledger_cell_key(row), []).append(row)
    for row in trades:
        trades_by_key.setdefault(_ledger_cell_key(row), []).append(row)
    for cell in cells:
        key = _ledger_cell_key(cell)
        if not _cell_gate_fields_are_valid(
            cell,
            reservations=reservations_by_key.get(key, []),
            trades=trades_by_key.get(key, []),
            evaluation_start_epoch=evaluation_start_epoch,
            evaluation_end_epoch=evaluation_end_epoch,
        ):
            return False
    discovery = result.get("discovery_gate")
    if not isinstance(discovery, Mapping):
        return False
    passing = _passing_global_configurations(cells)
    gap_returns = [
        float(row["pnl_r"]) for row in trades if row.get("exit_reason") == "sl_gap_open"
    ]
    expected_discovery_values: dict[str, Any] = {
        "passing_global_configurations": passing,
        "all_pair_directions_same_configuration": passing == ["qscc_v1"],
        "discovery_survivor": passing[0] if len(passing) == 1 else None,
        "maximum_cell_drawdown_r": max(
            (float(cell["maximum_drawdown_r"]) for cell in cells), default=0.0
        ),
        "adverse_gap_stop_count": len(gap_returns),
        "adverse_gap_total_r": math.fsum(gap_returns),
        "adverse_gap_min_r": min(gap_returns) if gap_returns else None,
        "success_claim_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
    }
    for name, expected in expected_discovery_values.items():
        actual = discovery.get(name)
        if expected is None:
            if actual is not None:
                return False
        elif isinstance(expected, float):
            if not _numbers_match(actual, expected):
                return False
        elif actual != expected or type(actual) is not type(expected):
            return False
    return True


def validate_source_replay(
    *,
    result: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[QuoteBar]],
    proxy_spread_budgets_bps: Mapping[str, float],
    proxy_provenance: Mapping[str, Any] | None,
    proxy_contract_schema_version: str,
    evaluation_start_utc: str,
    evaluation_end_utc: str,
    preregistration_lock_utc: str,
    source_errors: Mapping[str, str] | None = None,
) -> bool:
    """Regenerate all 36 cells and ordered ledgers and require byte-equivalence."""

    if not validate_result_bundle(result):
        return False
    try:
        replay = screen_universe(
            bars_by_symbol=bars_by_symbol,
            proxy_spread_budgets_bps=proxy_spread_budgets_bps,
            proxy_provenance=proxy_provenance,
            proxy_contract_schema_version=proxy_contract_schema_version,
            evaluation_start_utc=evaluation_start_utc,
            evaluation_end_utc=evaluation_end_utc,
            preregistration_lock_utc=preregistration_lock_utc,
            source_errors=source_errors,
        )
        return dumps_strict_json(dict(result), indent=None) == dumps_strict_json(
            replay, indent=None
        )
    except (KeyError, TypeError, ValueError):
        return False


def replay_and_validate_result(**kwargs: Any) -> bool:
    """Callable alias for isolated finalizers and synthetic tests."""

    return validate_source_replay(**kwargs)


def _load_proxy_contract(
    path: Path,
    *,
    evaluation_start_utc: str,
    preregistration_lock_utc: str,
) -> tuple[dict[str, float], dict[str, Any], str]:
    payload = loads_strict_json(path.read_text(encoding="utf-8"))
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
    preregistration_lock_epoch = _parse_epoch(preregistration_lock_utc)
    if evaluation_start_epoch is None or preregistration_lock_epoch is None:
        raise ValueError("evaluation start and preregistration lock are required")
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
    if not validate_source_replay(
        result=result,
        bars_by_symbol=bars_by_symbol,
        proxy_spread_budgets_bps=proxy_budgets,
        proxy_provenance=proxy_provenance,
        proxy_contract_schema_version=proxy_schema_version,
        evaluation_start_utc=args.start,
        evaluation_end_utc=args.end,
        preregistration_lock_utc=args.preregistration_lock_utc,
        source_errors=source_errors,
    ):
        raise RuntimeError("QSCC complete source replay validation failed")
    result["input_metadata"] = {
        "csv_root_recorded": False,
        "start": args.start,
        "end_exclusive": args.end,
        "preregistration_lock_utc": args.preregistration_lock_utc,
        "proxy_contract_sha256": _sha256(proxy_path),
        "m1_csv_sha256_by_symbol": source_sha256,
    }
    reservation_payload = {
        "schema_version": "fxstack.scalp.quote_side_convergence_continuation_reservation_ledger.v1",
        "family": result["family"],
        "reservations": result.pop("reservation_ledger"),
    }
    trade_payload = {
        "schema_version": "fxstack.scalp.quote_side_convergence_continuation_trade_ledger.v1",
        "family": result["family"],
        "trades": result.pop("trade_ledger"),
    }
    if not validate_reservation_ledger_payload(reservation_payload):
        raise RuntimeError("QSCC reservation-ledger schema validation failed")
    if not validate_trade_ledger_payload(trade_payload):
        raise RuntimeError("QSCC trade-ledger schema validation failed")
    for path, payload in (
        (reservation_output, reservation_payload),
        (trade_output, trade_payload),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(dumps_strict_json(payload))
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
    result["complete_bundle_validation_passed_before_split"] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps_strict_json(result))
    accounting = result["search_accounting"]
    print(
        f"QSCC: {len(result['cells'])} cells; "
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
