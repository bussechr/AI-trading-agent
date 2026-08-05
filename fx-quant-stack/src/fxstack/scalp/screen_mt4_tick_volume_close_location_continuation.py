# AGENT: ROLE: Research-only IG MT4 tick-volume close-location continuation screen.
# AGENT: ENTRYPOINT: `screen_universe`; pure functions only, with no I/O or live authority.
# AGENT: PRIMARY INPUTS: sealed MT4 bid M1 OHLC/iVolume and contemporaneous bid/ask ticks.
# AGENT: PRIMARY OUTPUTS: all fixed cells plus complete reservation and outcome ledgers.
# AGENT ISOLATION: advisory research evidence only; never authorizes activation or orders.
"""Pure causal evaluator for the frozen MTVCLC-v1 research hypothesis.

The activity field in this module is MetaTrader 4 ``iVolume`` (broker tick
volume) for a completed M1 bar.  It is intentionally *not* interchangeable
with Dukascopy bid-volume, ask-volume, their sum, exchange volume, or a tick
count reconstructed by the API.  Signal geometry uses authentic broker bid
OHLC.  Entry and outcome evaluation require separately captured,
contemporaneous MT4 bid/ask transport snapshots; historical ask bars
synthesized from one current spread are inadmissible.  An unchanged
authenticated broker event may be published repeatedly through quiet seconds.
Quote-gap checks therefore use the continuous transport-observation clock,
not unique price changes or a unique broker-event clock.

For signal bar ``t``, the volume baseline is exactly the 240 consecutive
completed M1 bars ``t-240`` through ``t-1``.  One symmetric fixed
configuration is evaluated across the ordered scope-v3 universe.  A complete
signal enters at the first quote at or after the bar close, no more than five
seconds later.  The evaluator models an immediate market BUY at ask or SELL at
bid, never a pending order.  Stops are tested before targets and any quote gap
or incomplete horizon is adverse.

This module deliberately contains no CLI, filesystem, network, database,
credential, bridge, runtime, issuer, registry, or broker-order integration.
It can falsify the hypothesis from immutable inputs; it cannot authorize a
success claim or production use.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Any, Mapping, Sequence


MTVCLC_SYMBOLS: tuple[str, ...] = (
    "EURUSD",
    "USDJPY",
    "AUDUSD",
    "GBPUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "NZDUSD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "GBPCAD",
    "GBPCHF",
    "GBPJPY",
    "BTCUSD",
    "ETHUSD",
    "AUDCAD",
    "NZDJPY",
)

STRATEGY_ID = "ig_mt4_tick_volume_close_location_continuation"
STRATEGY_VERSION = "mtvclc.v1"
CONFIG_ID = "mtvclc_v1_vq90_cl80_b1k_h30_t4k_s8k"
SOURCE_CONTRACT_ID = (
    "authenticated_ig_mt4_bid_m1_ohlc_ivolume_plus_bid_ask_transport_snapshots.v1"
)
ACTIVITY_METRIC_ID = "mt4_m1_ivolume_tick_volume.v1"

BASELINE_M1_BARS = 240
VOLUME_QUANTILE = 0.90
CLOSE_LOCATION_THRESHOLD = 0.80
FIXED_ADVERSE_EXECUTION_DEBIT_BPS = 1.0
IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION = 0.005
TARGET_COST_MULTIPLE = 4.0
STOP_COST_MULTIPLE = 8.0
OUTCOME_HORIZON_M1_BARS = 30
MAX_ENTRY_DELAY_SECONDS = 5
MAX_QUOTE_GAP_SECONDS = 5
MAX_ENTRIES_PER_SYMBOL_UTC_DAY = 1

# IG's 22:00 Europe/London funding boundary is 21:00 UTC during BST and 22:00
# UTC during GMT.  Cover both possibilities without depending on a host
# timezone database.  This fixed half-open entry blackout begins one complete
# 30-minute horizon plus ten minutes of close slack before the earliest
# boundary and ends ten minutes after the latest boundary.
IG_FUNDING_BOUNDARY_LONDON_MINUTE = 22 * 60
IG_FUNDING_BOUNDARY_EARLIEST_UTC_MINUTE = 21 * 60
IG_FUNDING_BOUNDARY_LATEST_UTC_MINUTE = 22 * 60
ROLLOVER_CLOSE_SLACK_SECONDS = 10 * 60
ROLLOVER_ENTRY_BLACKOUT_START_SECOND = (
    IG_FUNDING_BOUNDARY_EARLIEST_UTC_MINUTE * 60
    - OUTCOME_HORIZON_M1_BARS * 60
    - ROLLOVER_CLOSE_SLACK_SECONDS
)
ROLLOVER_ENTRY_BLACKOUT_END_SECOND = (
    IG_FUNDING_BOUNDARY_LATEST_UTC_MINUTE * 60
    + ROLLOVER_CLOSE_SLACK_SECONDS
)

IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 4_654
IMMUTABLE_CURRENT_ATTEMPTED_CELLS = 44
IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 4_698
BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD = 4.641077835714861

MIN_TRADES_PER_CELL = 30
MIN_INDEPENDENT_DAYS_PER_CELL = 10
WIN_PROBABILITY_FAMILY_CONFIDENCE = 0.95
BASE_COST_BREAK_EVEN_WIN_PROBABILITY = 0.75

_SIDES = ("BUY", "SELL")
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class MTVCLCConfig:
    """The one frozen MTVCLC-v1 configuration."""

    baseline_m1_bars: int = BASELINE_M1_BARS
    volume_quantile: float = VOLUME_QUANTILE
    close_location_threshold: float = CLOSE_LOCATION_THRESHOLD
    target_cost_multiple: float = TARGET_COST_MULTIPLE
    stop_cost_multiple: float = STOP_COST_MULTIPLE
    outcome_horizon_m1_bars: int = OUTCOME_HORIZON_M1_BARS
    maximum_entry_delay_seconds: int = MAX_ENTRY_DELAY_SECONDS
    maximum_quote_gap_seconds: int = MAX_QUOTE_GAP_SECONDS
    rollover_entry_blackout_start_second: int = (
        ROLLOVER_ENTRY_BLACKOUT_START_SECOND
    )
    rollover_entry_blackout_end_second: int = ROLLOVER_ENTRY_BLACKOUT_END_SECOND

    @property
    def config_id(self) -> str:
        return CONFIG_ID


GRID: tuple[MTVCLCConfig, ...] = (MTVCLCConfig(),)


@dataclass(frozen=True, slots=True)
class MT4BidBar:
    """One completed broker M1 bar from MT4 iOHLC plus iVolume."""

    epoch: int
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    tick_volume: int


@dataclass(frozen=True, slots=True)
class MT4Quote:
    """One authenticated executable MT4 transport snapshot.

    ``epoch`` is the transport-observation clock used for entry freshness and
    continuity.  ``source_event_token_sha256`` is the hash of MT4's opaque
    broker-server event identity and may repeat while the quote is unchanged;
    that is continuous authenticated transport, not a missing quote interval.
    ``market_event_sequence`` is retained as process-local audit metadata; the
    strategy never uses it as a cross-restart clock.
    """

    epoch: int
    bid: float
    ask: float
    source_event_token_sha256: str | None = None
    market_event_sequence: int | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4


@dataclass(frozen=True, slots=True)
class MT4CostCalibration:
    """One frozen, hash-bound IG-MT4 cost row for a symbol."""

    symbol: str
    p90_spread_bps: float
    commission_bps_per_round_trip: float
    financing_bps_per_trade: float
    account_currency: str
    pnl_currency: str
    convert_on_close_charge_fraction: float
    source_sha256: str
    adverse_execution_debit_bps: float = FIXED_ADVERSE_EXECUTION_DEBIT_BPS

    @property
    def recorded_cost_bps(self) -> float:
        return (
            self.p90_spread_bps
            + self.commission_bps_per_round_trip
            + self.financing_bps_per_trade
            + self.adverse_execution_debit_bps
        )

    @property
    def conversion_applies(self) -> bool:
        return self.pnl_currency != self.account_currency

    @property
    def break_even_win_probability(self) -> float:
        """Break even after fixed costs and IG's P/L conversion charge."""

        cost = self.recorded_cost_bps
        target = TARGET_COST_MULTIPLE * cost
        stop = STOP_COST_MULTIPLE * cost
        rate = self.convert_on_close_charge_fraction
        numerator = stop * (1.0 + rate) + cost
        denominator = target * (1.0 - rate) + stop * (1.0 + rate)
        return numerator / denominator


@dataclass(frozen=True, slots=True)
class MTVCLCClosedSignal:
    config_id: str
    symbol: str
    side: str
    signal_index: int
    signal_epoch: int
    expected_entry_epoch: int
    entry_day: str
    volume_v90: float
    signal_tick_volume: int
    bid_body_bps: float
    bid_close_location: float
    p90_spread_bps: float
    recorded_cost_bps: float
    convert_on_close_charge_fraction: float
    target_bps: float
    stop_bps: float
    p_star: float


@dataclass(frozen=True, slots=True)
class MTVCLCSignal:
    config_id: str
    symbol: str
    side: str
    signal_index: int
    signal_epoch: int
    entry_epoch: int
    entry_day: str
    entry_bid: float
    entry_ask: float
    entry_price: float
    live_spread_bps: float
    target_price: float
    stop_price: float
    target_bps: float
    stop_bps: float
    recorded_cost_bps: float
    convert_on_close_charge_fraction: float
    p_star: float
    volume_v90: float
    signal_tick_volume: int
    bid_body_bps: float
    bid_close_location: float


@dataclass(frozen=True, slots=True)
class MTVCLCOutcome:
    config_id: str
    symbol: str
    side: str
    signal_epoch: int
    entry_day: str
    entry_epoch: int | None
    exit_epoch: int | None
    entry_price: float | None
    exit_price: float | None
    exit_reason: str
    full_target_hit_first: bool
    gross_quote_bps: float
    recorded_cost_bps: float
    currency_conversion_debit_bps: float
    net_bps: float


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and set(text) <= _HEX


def validate_bid_bar(bar: MT4BidBar) -> bool:
    if isinstance(bar.epoch, bool) or not isinstance(bar.epoch, int) or bar.epoch <= 0:
        return False
    if bar.epoch % 60 != 0:
        return False
    open_px = _finite(bar.bid_open)
    high_px = _finite(bar.bid_high)
    low_px = _finite(bar.bid_low)
    close_px = _finite(bar.bid_close)
    if (
        open_px is None
        or high_px is None
        or low_px is None
        or close_px is None
        or min(open_px, high_px, low_px, close_px) <= 0.0
    ):
        return False
    if high_px < max(open_px, close_px) or low_px > min(open_px, close_px):
        return False
    if high_px < low_px:
        return False
    return (
        not isinstance(bar.tick_volume, bool)
        and isinstance(bar.tick_volume, int)
        and bar.tick_volume >= 0
    )


def validate_quote(quote: MT4Quote) -> bool:
    if isinstance(quote.epoch, bool) or not isinstance(quote.epoch, int):
        return False
    bid = _finite(quote.bid)
    ask = _finite(quote.ask)
    token_hash = quote.source_event_token_sha256
    market_sequence = quote.market_event_sequence
    event_identity_valid = bool(
        token_hash is None
        and market_sequence is None
        or (
            isinstance(token_hash, str)
            and _valid_sha256(token_hash)
            and not isinstance(market_sequence, bool)
            and isinstance(market_sequence, int)
            and market_sequence >= 0
        )
    )
    return bool(
        quote.epoch > 0
        and bid is not None
        and ask is not None
        and bid > 0.0
        and ask >= bid
        and event_identity_valid
    )


def validate_cost_calibration(
    calibration: MT4CostCalibration, *, expected_symbol: str | None = None
) -> bool:
    if expected_symbol is not None and calibration.symbol != expected_symbol:
        return False
    spread = _finite(calibration.p90_spread_bps)
    commission = _finite(calibration.commission_bps_per_round_trip)
    financing = _finite(calibration.financing_bps_per_trade)
    debit = _finite(calibration.adverse_execution_debit_bps)
    conversion = _finite(calibration.convert_on_close_charge_fraction)
    if (
        spread is None
        or commission is None
        or financing is None
        or debit is None
        or conversion is None
    ):
        return False
    account_currency = calibration.account_currency
    pnl_currency = calibration.pnl_currency
    currencies_valid = all(
        isinstance(currency, str)
        and len(currency) == 3
        and currency.isalpha()
        and currency.isupper()
        for currency in (account_currency, pnl_currency)
    )
    expected_conversion = (
        0.0
        if account_currency == pnl_currency
        else IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
    )
    return bool(
        spread > 0.0
        and commission >= 0.0
        and financing >= 0.0
        and debit == FIXED_ADVERSE_EXECUTION_DEBIT_BPS
        and currencies_valid
        and math.isclose(
            conversion,
            expected_conversion,
            rel_tol=0.0,
            abs_tol=1e-15,
        )
        and calibration.recorded_cost_bps > 0.0
        and 0.0 < calibration.break_even_win_probability < 1.0
        and _valid_sha256(calibration.source_sha256)
    )


def prepare_bars(bars: Sequence[MT4BidBar]) -> tuple[MT4BidBar, ...]:
    prepared = tuple(bars)
    if any(not validate_bid_bar(bar) for bar in prepared):
        raise ValueError("invalid MT4 bid bar")
    if any(right.epoch <= left.epoch for left, right in zip(prepared, prepared[1:])):
        raise ValueError("MT4 bid bars must be strictly time ordered")
    return prepared


def prepare_quotes(quotes: Sequence[MT4Quote]) -> tuple[MT4Quote, ...]:
    prepared = tuple(quotes)
    if any(not validate_quote(quote) for quote in prepared):
        raise ValueError("invalid contemporaneous MT4 quote")
    if any(right.epoch <= left.epoch for left, right in zip(prepared, prepared[1:])):
        raise ValueError("MT4 quotes must be strictly time ordered")
    return prepared


def _type7_quantile(values: Sequence[float], probability: float) -> float:
    if not values or not 0.0 <= probability <= 1.0:
        raise ValueError("invalid type-7 quantile input")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise ValueError("non-finite quantile input")
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def baseline_volume_v90(
    prepared: Sequence[MT4BidBar], *, signal_index: int
) -> float | None:
    start = signal_index - BASELINE_M1_BARS
    if start < 0 or signal_index >= len(prepared):
        return None
    window = prepared[start : signal_index + 1]
    if len(window) != BASELINE_M1_BARS + 1:
        return None
    if any(right.epoch != left.epoch + 60 for left, right in zip(window, window[1:])):
        return None
    return _type7_quantile(
        [float(bar.tick_volume) for bar in window[:-1]], VOLUME_QUANTILE
    )


def _utc_day(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()


def entry_in_rollover_blackout(epoch: int) -> bool:
    """Return membership in the fixed half-open UTC interval [20:20, 22:10)."""

    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
        raise ValueError("rollover clock must be a positive integer UTC epoch")
    utc_value = datetime.fromtimestamp(epoch, tz=timezone.utc)
    second_of_day = (
        utc_value.hour * 60 * 60 + utc_value.minute * 60 + utc_value.second
    )
    return (
        ROLLOVER_ENTRY_BLACKOUT_START_SECOND
        <= second_of_day
        < ROLLOVER_ENTRY_BLACKOUT_END_SECOND
    )


def evaluate_closed_signal(
    *,
    prepared: Sequence[MT4BidBar],
    signal_index: int,
    symbol: str,
    side: str,
    cost: MT4CostCalibration,
    config: MTVCLCConfig = GRID[0],
) -> tuple[MTVCLCClosedSignal | None, str]:
    if symbol not in MTVCLC_SYMBOLS:
        return None, "symbol_outside_exact_scope"
    if side not in _SIDES:
        return None, "side_invalid"
    if config != GRID[0]:
        return None, "configuration_not_frozen"
    if not validate_cost_calibration(cost, expected_symbol=symbol):
        return None, "cost_calibration_invalid"
    if signal_index < 0 or signal_index >= len(prepared):
        return None, "signal_index_invalid"
    expected_entry_epoch = prepared[signal_index].epoch + 60
    if entry_in_rollover_blackout(expected_entry_epoch):
        return None, "entry_in_fixed_utc_rollover_blackout"
    volume_v90 = baseline_volume_v90(prepared, signal_index=signal_index)
    if volume_v90 is None:
        return None, "baseline_incomplete_or_gapped"
    if volume_v90 <= 0.0:
        return None, "volume_v90_not_positive"
    bar = prepared[signal_index]
    if not bar.tick_volume > volume_v90:
        return None, "tick_volume_not_strictly_above_v90"
    bar_range = bar.bid_high - bar.bid_low
    if bar_range <= 0.0:
        return None, "bid_range_not_positive"
    direction = 1.0 if side == "BUY" else -1.0
    signed_body = direction * (bar.bid_close - bar.bid_open)
    if signed_body <= 0.0:
        return None, "bid_body_direction_mismatch"
    body_bps = signed_body / bar.bid_open * 1e4
    if body_bps < cost.recorded_cost_bps:
        return None, "bid_body_below_recorded_cost"
    close_location = (
        (bar.bid_close - bar.bid_low) / bar_range
        if side == "BUY"
        else (bar.bid_high - bar.bid_close) / bar_range
    )
    if close_location < config.close_location_threshold:
        return None, "bid_close_location_below_threshold"
    target_bps = config.target_cost_multiple * cost.recorded_cost_bps
    stop_bps = config.stop_cost_multiple * cost.recorded_cost_bps
    p_star = cost.break_even_win_probability
    return (
        MTVCLCClosedSignal(
            config_id=config.config_id,
            symbol=symbol,
            side=side,
            signal_index=signal_index,
            signal_epoch=bar.epoch,
            expected_entry_epoch=expected_entry_epoch,
            entry_day=_utc_day(expected_entry_epoch),
            volume_v90=volume_v90,
            signal_tick_volume=bar.tick_volume,
            bid_body_bps=body_bps,
            bid_close_location=close_location,
            p90_spread_bps=cost.p90_spread_bps,
            recorded_cost_bps=cost.recorded_cost_bps,
            convert_on_close_charge_fraction=(
                cost.convert_on_close_charge_fraction
            ),
            target_bps=target_bps,
            stop_bps=stop_bps,
            p_star=p_star,
        ),
        "",
    )


def _first_entry_quote(
    closed: MTVCLCClosedSignal, quotes: Sequence[MT4Quote]
) -> MT4Quote | None:
    index = bisect.bisect_left(
        quotes,
        closed.expected_entry_epoch,
        key=lambda quote: quote.epoch,
    )
    if index >= len(quotes):
        return None
    quote = quotes[index]
    if quote.epoch > closed.expected_entry_epoch + MAX_ENTRY_DELAY_SECONDS:
        return None
    return quote


def attach_entry(
    *,
    closed: MTVCLCClosedSignal,
    quotes: Sequence[MT4Quote],
) -> tuple[MTVCLCSignal | None, str]:
    quote = _first_entry_quote(closed, quotes)
    if quote is None:
        return None, "contemporaneous_entry_quote_missing"
    if quote.spread_bps > closed.p90_spread_bps:
        return None, "live_spread_above_frozen_p90"
    entry_price = quote.ask if closed.side == "BUY" else quote.bid
    if closed.side == "BUY":
        target_price = entry_price * (1.0 + closed.target_bps / 1e4)
        stop_price = entry_price * (1.0 - closed.stop_bps / 1e4)
    else:
        target_price = entry_price * (1.0 - closed.target_bps / 1e4)
        stop_price = entry_price * (1.0 + closed.stop_bps / 1e4)
    return (
        MTVCLCSignal(
            config_id=closed.config_id,
            symbol=closed.symbol,
            side=closed.side,
            signal_index=closed.signal_index,
            signal_epoch=closed.signal_epoch,
            entry_epoch=quote.epoch,
            entry_day=closed.entry_day,
            entry_bid=quote.bid,
            entry_ask=quote.ask,
            entry_price=entry_price,
            live_spread_bps=quote.spread_bps,
            target_price=target_price,
            stop_price=stop_price,
            target_bps=closed.target_bps,
            stop_bps=closed.stop_bps,
            recorded_cost_bps=closed.recorded_cost_bps,
            convert_on_close_charge_fraction=(
                closed.convert_on_close_charge_fraction
            ),
            p_star=closed.p_star,
            volume_v90=closed.volume_v90,
            signal_tick_volume=closed.signal_tick_volume,
            bid_body_bps=closed.bid_body_bps,
            bid_close_location=closed.bid_close_location,
        ),
        "",
    )


def evaluate_signal(
    *,
    prepared_bars: Sequence[MT4BidBar],
    prepared_quotes: Sequence[MT4Quote],
    signal_index: int,
    symbol: str,
    side: str,
    cost: MT4CostCalibration,
    config: MTVCLCConfig = GRID[0],
) -> tuple[MTVCLCSignal | None, str]:
    closed, reason = evaluate_closed_signal(
        prepared=prepared_bars,
        signal_index=signal_index,
        symbol=symbol,
        side=side,
        cost=cost,
        config=config,
    )
    if closed is None:
        return None, reason
    return attach_entry(closed=closed, quotes=prepared_quotes)


def _gross_quote_bps(signal: MTVCLCSignal, exit_price: float) -> float:
    if signal.side == "BUY":
        return (exit_price - signal.entry_price) / signal.entry_price * 1e4
    return (signal.entry_price - exit_price) / signal.entry_price * 1e4


def _outcome(
    signal: MTVCLCSignal,
    *,
    quote: MT4Quote | None,
    exit_reason: str,
    full_target_hit_first: bool,
    exit_price: float | None = None,
) -> MTVCLCOutcome:
    resolved_exit: float | None
    if full_target_hit_first:
        gross = signal.target_bps
        resolved_exit = signal.target_price
    elif exit_reason == "STOP_LOSS" and exit_price is not None:
        gross = _gross_quote_bps(signal, exit_price)
        resolved_exit = exit_price
    elif exit_reason == "TIME_STOP" and exit_price is not None:
        gross = _gross_quote_bps(signal, exit_price)
        resolved_exit = exit_price
    else:
        gross = -signal.stop_bps
        resolved_exit = exit_price
    conversion_debit = (
        abs(gross) * signal.convert_on_close_charge_fraction
    )
    return MTVCLCOutcome(
        config_id=signal.config_id,
        symbol=signal.symbol,
        side=signal.side,
        signal_epoch=signal.signal_epoch,
        entry_day=signal.entry_day,
        entry_epoch=signal.entry_epoch,
        exit_epoch=None if quote is None else quote.epoch,
        entry_price=signal.entry_price,
        exit_price=resolved_exit,
        exit_reason=exit_reason,
        full_target_hit_first=full_target_hit_first,
        gross_quote_bps=gross,
        recorded_cost_bps=signal.recorded_cost_bps,
        currency_conversion_debit_bps=conversion_debit,
        net_bps=gross - signal.recorded_cost_bps - conversion_debit,
    )


def adverse_missing_entry_outcome(closed: MTVCLCClosedSignal) -> MTVCLCOutcome:
    gross = -closed.stop_bps
    conversion_debit = (
        abs(gross) * closed.convert_on_close_charge_fraction
    )
    return MTVCLCOutcome(
        config_id=closed.config_id,
        symbol=closed.symbol,
        side=closed.side,
        signal_epoch=closed.signal_epoch,
        entry_day=closed.entry_day,
        entry_epoch=None,
        exit_epoch=None,
        entry_price=None,
        exit_price=None,
        exit_reason="ENTRY_QUOTE_MISSING",
        full_target_hit_first=False,
        gross_quote_bps=gross,
        recorded_cost_bps=closed.recorded_cost_bps,
        currency_conversion_debit_bps=conversion_debit,
        net_bps=gross - closed.recorded_cost_bps - conversion_debit,
    )


def score_signal(
    signal: MTVCLCSignal, *, quotes: Sequence[MT4Quote]
) -> MTVCLCOutcome:
    start = bisect.bisect_right(
        quotes,
        signal.entry_epoch,
        key=lambda quote: quote.epoch,
    )
    horizon_epoch = signal.entry_epoch + OUTCOME_HORIZON_M1_BARS * 60
    previous_epoch = signal.entry_epoch
    for quote in quotes[start:]:
        if quote.epoch - previous_epoch > MAX_QUOTE_GAP_SECONDS:
            return _outcome(
                signal,
                quote=quote,
                exit_reason="QUOTE_GAP_ADVERSE",
                full_target_hit_first=False,
            )
        previous_epoch = quote.epoch
        executable = quote.bid if signal.side == "BUY" else quote.ask
        stop_hit = (
            executable <= signal.stop_price
            if signal.side == "BUY"
            else executable >= signal.stop_price
        )
        target_hit = (
            executable >= signal.target_price
            if signal.side == "BUY"
            else executable <= signal.target_price
        )
        if stop_hit:
            return _outcome(
                signal,
                quote=quote,
                exit_price=executable,
                exit_reason="STOP_LOSS",
                full_target_hit_first=False,
            )
        if target_hit:
            return _outcome(
                signal,
                quote=quote,
                exit_reason="TAKE_PROFIT",
                full_target_hit_first=True,
            )
        if quote.epoch >= horizon_epoch:
            return _outcome(
                signal,
                quote=quote,
                exit_price=executable,
                exit_reason="TIME_STOP",
                full_target_hit_first=False,
            )
    return _outcome(
        signal,
        quote=None,
        exit_reason="INCOMPLETE_HORIZON_ADVERSE",
        full_target_hit_first=False,
    )


def _wilson_one_sided_lower(wins: int, trials: int) -> float:
    if trials <= 0 or wins < 0 or wins > trials:
        return 0.0
    alpha = (1.0 - WIN_PROBABILITY_FAMILY_CONFIDENCE) / len(
        MTVCLC_SYMBOLS
    ) / len(_SIDES)
    z = NormalDist().inv_cdf(1.0 - alpha)
    point = wins / trials
    z_sq = z * z
    denominator = 1.0 + z_sq / trials
    center = point + z_sq / (2.0 * trials)
    radius = z * math.sqrt(
        point * (1.0 - point) / trials + z_sq / (4.0 * trials * trials)
    )
    return max(0.0, (center - radius) / denominator)


def _cell_payload(
    *,
    symbol: str,
    side: str,
    source_ready: bool,
    outcomes: Sequence[MTVCLCOutcome],
    break_even_probability: float,
) -> dict[str, Any]:
    selected = [
        outcome
        for outcome in outcomes
        if outcome.symbol == symbol and outcome.side == side
    ]
    wins = sum(outcome.full_target_hit_first for outcome in selected)
    days = len({outcome.entry_day for outcome in selected})
    lower = _wilson_one_sided_lower(wins, len(selected))
    mean_net = (
        sum(outcome.net_bps for outcome in selected) / len(selected)
        if selected
        else 0.0
    )
    passes = bool(
        source_ready
        and len(selected) >= MIN_TRADES_PER_CELL
        and days >= MIN_INDEPENDENT_DAYS_PER_CELL
        and lower > break_even_probability
        and mean_net > 0.0
    )
    return {
        "config_id": CONFIG_ID,
        "symbol": symbol,
        "side": side,
        "source_ready": source_ready,
        "reservations": len(selected),
        "wins": wins,
        "independent_days": days,
        "full_target_rate": wins / len(selected) if selected else 0.0,
        "win_probability_wilson_lower": lower,
        "base_break_even_probability": break_even_probability,
        "mean_net_bps": mean_net,
        "passes_fixed_cell_screen": passes,
    }


def attempt_manifest() -> dict[str, Any]:
    """Return the immutable, research-only attempt declaration."""

    return {
        "schema_version": "fxstack.scalp.mtvclc_attempt_manifest.v1",
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "config_ids": [CONFIG_ID],
        "parameter_configurations": 1,
        "configuration": {
            "baseline_m1_bars": BASELINE_M1_BARS,
            "volume_quantile": VOLUME_QUANTILE,
            "volume_quantile_method": "type7",
            "close_location_threshold": CLOSE_LOCATION_THRESHOLD,
            "body_floor": "recorded_cost_bps",
            "recorded_cost_formula": (
                "ig_mt4_p90_spread_bps+commission_bps_per_round_trip+"
                "financing_bps_per_trade+1.0bps_adverse_execution_debit"
            ),
            "convert_on_close_treatment": (
                "debit 0.5% of absolute realized gross quote-currency P/L "
                "when pnl_currency differs from the IG account currency"
            ),
            "convert_on_close_charge_fraction": (
                IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
            ),
            "target_cost_multiple": TARGET_COST_MULTIPLE,
            "stop_cost_multiple": STOP_COST_MULTIPLE,
            "outcome_horizon_m1_bars": OUTCOME_HORIZON_M1_BARS,
            "maximum_entry_delay_seconds": MAX_ENTRY_DELAY_SECONDS,
            "maximum_quote_gap_seconds": MAX_QUOTE_GAP_SECONDS,
            "quote_gap_clock": "authenticated_transport_snapshot_epoch",
            "repeated_unchanged_broker_event_allowed": True,
            "maximum_entries_per_symbol_utc_day": (
                MAX_ENTRIES_PER_SYMBOL_UTC_DAY
            ),
            "rollover_guard": {
                "funding_boundary_london_minute": (
                    IG_FUNDING_BOUNDARY_LONDON_MINUTE
                ),
                "earliest_funding_boundary_utc_minute": (
                    IG_FUNDING_BOUNDARY_EARLIEST_UTC_MINUTE
                ),
                "latest_funding_boundary_utc_minute": (
                    IG_FUNDING_BOUNDARY_LATEST_UTC_MINUTE
                ),
                "maximum_holding_seconds": OUTCOME_HORIZON_M1_BARS * 60,
                "close_slack_seconds": ROLLOVER_CLOSE_SLACK_SECONDS,
                "entry_blackout_start_second": (
                    ROLLOVER_ENTRY_BLACKOUT_START_SECOND
                ),
                "entry_blackout_end_second": ROLLOVER_ENTRY_BLACKOUT_END_SECOND,
                "entry_blackout_utc": "[20:20:00,22:10:00)",
                "half_open": True,
                "signals_inside_blackout_reserve": False,
            },
            "execution_type": "market",
            "pending_orders_forbidden": True,
            "base_cost_break_even_win_probability": (
                BASE_COST_BREAK_EVEN_WIN_PROBABILITY
            ),
            "two_x_cost_break_even_win_probability": 10.0 / 12.0,
        },
        "symbol_scope": list(MTVCLC_SYMBOLS),
        "sides": list(_SIDES),
        "prior_attempted_cells_lower_bound": IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
        "current_attempted_cells": IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
        "cumulative_attempted_cells_lower_bound": (
            IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
        ),
        "descriptive_df99_bonferroni_abs_t_threshold": (
            BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        ),
        "anytime_valid": False,
        "loop_until_pass_forbidden": True,
        "source_contract_id": SOURCE_CONTRACT_ID,
        "activity_metric_id": ACTIVITY_METRIC_ID,
        "provider_volume_interchangeable": False,
        "mt4_history_export_required": True,
        "contemporaneous_tick_outcomes_required": True,
        "historical_synthesized_ask_bars_forbidden": True,
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def screen_universe(
    *,
    bars_by_symbol: Mapping[str, Sequence[MT4BidBar]],
    quotes_by_symbol: Mapping[str, Sequence[MT4Quote]],
    costs_by_symbol: Mapping[str, MT4CostCalibration],
    source_sha256_by_symbol: Mapping[str, str],
    source_contract_id: str,
) -> dict[str, Any]:
    """Evaluate the one fixed configuration without touching an external system."""

    expected = set(MTVCLC_SYMBOLS)
    mapping_scope_ready = all(
        set(mapping) == expected
        for mapping in (
            bars_by_symbol,
            quotes_by_symbol,
            costs_by_symbol,
            source_sha256_by_symbol,
        )
    )
    identity_ready = bool(
        source_contract_id == SOURCE_CONTRACT_ID
        and set(source_sha256_by_symbol) == expected
        and all(_valid_sha256(value) for value in source_sha256_by_symbol.values())
    )
    outcomes: list[MTVCLCOutcome] = []
    reservations: list[dict[str, Any]] = []
    source_errors: list[str] = []
    symbol_ready: dict[str, bool] = {}

    for symbol in MTVCLC_SYMBOLS:
        ready = mapping_scope_ready and identity_ready
        cost = costs_by_symbol.get(symbol)
        if cost is None or not validate_cost_calibration(cost, expected_symbol=symbol):
            ready = False
            source_errors.append(f"cost_calibration_invalid:{symbol}")
        try:
            bars = prepare_bars(bars_by_symbol.get(symbol, ()))
            quotes = prepare_quotes(quotes_by_symbol.get(symbol, ()))
        except ValueError as exc:
            bars = ()
            quotes = ()
            ready = False
            source_errors.append(f"source_invalid:{symbol}:{exc}")
        if len(bars) < BASELINE_M1_BARS + 1 or not quotes:
            ready = False
            source_errors.append(f"source_coverage_insufficient:{symbol}")
        symbol_ready[symbol] = ready
        if not ready or cost is None:
            continue

        reserved_days: set[str] = set()
        for signal_index in range(BASELINE_M1_BARS, len(bars)):
            bar = bars[signal_index]
            if bar.bid_close == bar.bid_open:
                continue
            side = "BUY" if bar.bid_close > bar.bid_open else "SELL"
            closed, _reason = evaluate_closed_signal(
                prepared=bars,
                signal_index=signal_index,
                symbol=symbol,
                side=side,
                cost=cost,
            )
            if closed is None or closed.entry_day in reserved_days:
                continue
            signal, entry_reason = attach_entry(closed=closed, quotes=quotes)
            if entry_reason == "live_spread_above_frozen_p90":
                continue
            reserved_days.add(closed.entry_day)
            reservation = asdict(closed)
            reservation["entry_status"] = entry_reason or "admitted"
            reservations.append(reservation)
            if signal is None:
                outcomes.append(adverse_missing_entry_outcome(closed))
            else:
                outcomes.append(score_signal(signal, quotes=quotes))

    cells = [
        _cell_payload(
            symbol=symbol,
            side=side,
            source_ready=symbol_ready.get(symbol, False),
            outcomes=outcomes,
            break_even_probability=(
                costs_by_symbol[symbol].break_even_win_probability
                if symbol in costs_by_symbol
                and validate_cost_calibration(
                    costs_by_symbol[symbol], expected_symbol=symbol
                )
                else BASE_COST_BREAK_EVEN_WIN_PROBABILITY
            ),
        )
        for symbol in MTVCLC_SYMBOLS
        for side in _SIDES
    ]
    result = {
        "schema_version": "fxstack.scalp.mtvclc_screen_result.v1",
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "config_ids": [CONFIG_ID],
        "symbol_scope": list(MTVCLC_SYMBOLS),
        "source_contract_id": source_contract_id,
        "activity_metric_id": ACTIVITY_METRIC_ID,
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
            "current_attempted_cells": IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
            "cumulative_attempted_cells_lower_bound": (
                IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
            ),
        },
        "source_scope_ready": bool(
            mapping_scope_ready and identity_ready and all(symbol_ready.values())
        ),
        "source_sha256_by_symbol": {
            symbol: source_sha256_by_symbol[symbol]
            for symbol in MTVCLC_SYMBOLS
            if symbol in source_sha256_by_symbol
        },
        "source_errors": sorted(set(source_errors)),
        "costs": {
            symbol: asdict(costs_by_symbol[symbol])
            for symbol in MTVCLC_SYMBOLS
            if symbol in costs_by_symbol
        },
        "cells": cells,
        "reservation_ledger": reservations,
        "outcome_ledger": [asdict(outcome) for outcome in outcomes],
        "all_cells_pass_fixed_screen": all(
            cell["passes_fixed_cell_screen"] for cell in cells
        ),
        "attempt_manifest": attempt_manifest(),
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "order_authorized": False,
    }
    return result


def validate_result_bundle(result: Mapping[str, Any]) -> bool:
    """Reject incomplete scopes and any forged authority bit."""

    if result.get("schema_version") != "fxstack.scalp.mtvclc_screen_result.v1":
        return False
    if result.get("strategy_id") != STRATEGY_ID:
        return False
    if result.get("strategy_version") != STRATEGY_VERSION:
        return False
    if result.get("config_ids") != [CONFIG_ID]:
        return False
    if result.get("symbol_scope") != list(MTVCLC_SYMBOLS):
        return False
    if result.get("source_contract_id") != SOURCE_CONTRACT_ID:
        return False
    if result.get("activity_metric_id") != ACTIVITY_METRIC_ID:
        return False
    source_hashes = result.get("source_sha256_by_symbol")
    if (
        not isinstance(source_hashes, Mapping)
        or set(source_hashes) != set(MTVCLC_SYMBOLS)
        or any(not _valid_sha256(value) for value in source_hashes.values())
    ):
        return False
    if result.get("attempt_manifest") != attempt_manifest():
        return False
    accounting = result.get("attempt_accounting")
    if accounting != {
        "prior_attempted_cells_lower_bound": IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
        "current_attempted_cells": IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
        "cumulative_attempted_cells_lower_bound": (
            IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
        ),
    }:
        return False
    cells = result.get("cells")
    if not isinstance(cells, list) or len(cells) != IMMUTABLE_CURRENT_ATTEMPTED_CELLS:
        return False
    expected_cells = {
        (CONFIG_ID, symbol, side) for symbol in MTVCLC_SYMBOLS for side in _SIDES
    }
    observed_cells = {
        (cell.get("config_id"), cell.get("symbol"), cell.get("side"))
        for cell in cells
        if isinstance(cell, Mapping)
    }
    if observed_cells != expected_cells:
        return False
    if not isinstance(result.get("reservation_ledger"), list):
        return False
    if not isinstance(result.get("outcome_ledger"), list):
        return False
    if result.get("research_only") is not True:
        return False
    for field in (
        "success_claim_authorized",
        "holdout_access_authorized",
        "promotion_authorized",
        "activation_authorized",
        "registry_write_authorized",
        "runtime_authorized",
        "order_authorized",
    ):
        if result.get(field) is not False:
            return False
    return True


__all__ = [
    "ACTIVITY_METRIC_ID",
    "BASELINE_M1_BARS",
    "CONFIG_ID",
    "GRID",
    "IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS",
    "IMMUTABLE_CURRENT_ATTEMPTED_CELLS",
    "IMMUTABLE_PRIOR_ATTEMPTED_CELLS",
    "IG_FUNDING_BOUNDARY_EARLIEST_UTC_MINUTE",
    "IG_FUNDING_BOUNDARY_LATEST_UTC_MINUTE",
    "IG_FUNDING_BOUNDARY_LONDON_MINUTE",
    "MT4BidBar",
    "MT4CostCalibration",
    "MT4Quote",
    "MTVCLCClosedSignal",
    "MTVCLCConfig",
    "MTVCLCOutcome",
    "MTVCLCSignal",
    "MTVCLC_SYMBOLS",
    "ROLLOVER_CLOSE_SLACK_SECONDS",
    "ROLLOVER_ENTRY_BLACKOUT_END_SECOND",
    "ROLLOVER_ENTRY_BLACKOUT_START_SECOND",
    "SOURCE_CONTRACT_ID",
    "adverse_missing_entry_outcome",
    "attach_entry",
    "attempt_manifest",
    "baseline_volume_v90",
    "entry_in_rollover_blackout",
    "evaluate_closed_signal",
    "evaluate_signal",
    "prepare_bars",
    "prepare_quotes",
    "score_signal",
    "screen_universe",
    "validate_bid_bar",
    "validate_cost_calibration",
    "validate_quote",
    "validate_result_bundle",
]
