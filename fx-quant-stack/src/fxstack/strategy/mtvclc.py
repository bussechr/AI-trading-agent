# AGENT: ROLE: Production-owned pure MTVCLC-v1 immediate-market proposal evaluator.
# AGENT: ENTRYPOINT: `evaluate_mtvclc`; immutable inputs in, unqualified candidate out.
# AGENT: PRIMARY INPUTS: direct MT4 bid M1 OHLC/iVolume, authenticated bid/ask quotes, and a frozen cost row.
# AGENT: PRIMARY OUTPUTS: an unqualified immediate-market BUY/SELL candidate or deterministic refusal reasons.
# AGENT: STATE / SIDE EFFECTS: none; never sizes, persists, enqueues, activates, or executes.
"""Pure production proposal logic for the frozen MTVCLC-v1 strategy.

This module owns the production copy of the strategy arithmetic.  It does not
import the excluded :mod:`fxstack.scalp` research package and performs no I/O.
The input types retain the direct-MT4 provenance needed by a later runtime
adapter: completed bid OHLC, integer ``iVolume``, authenticated terminal
identity, an authenticated two-sided trade quote, and a hash-identified frozen
cost calibration.

An allowed result is only a mathematical candidate.  It is structurally an
immediate ``market`` BUY or SELL trade candidate; pending order types cannot
be represented.  Portfolio, one-entry-per-symbol/day reservation, evidence,
risk, sizing, release authority, queueing, and broker submission remain later
fail-closed production responsibilities.
"""

from __future__ import annotations

import bisect
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Literal, Mapping, Sequence

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
    IgMt4InstrumentIdentity,
    get_ig_mt4_instrument,
)


MTVCLC_STRATEGY_ID = "ig_mt4_tick_volume_close_location_continuation"
MTVCLC_STRATEGY_VERSION = "mtvclc.v1"
MTVCLC_CONFIG_ID = "mtvclc_v1_vq90_cl80_b1k_h30_t4k_s8k"
MTVCLC_CONFIG_SHA256 = (
    "aac8e4bd98d71243b41993983d63ab1da1f8836b74b9243c7e60232f0eb00701"
)
MTVCLC_POLICY_SCHEMA_VERSION = "fxstack.strategy.mtvclc.policy.v1"
MTVCLC_PROPOSAL_SCHEMA_VERSION = "fxstack.strategy.mtvclc.proposal.v1"
MTVCLC_COST_CALIBRATION_SCHEMA_VERSION = "fxstack.strategy.mtvclc.cost_calibration.v1"
MTVCLC_SOURCE_CONTRACT_ID = (
    "authenticated_ig_mt4_bid_m1_ohlc_ivolume_plus_bid_ask_transport_snapshots.v1"
)
MTVCLC_ACTIVITY_METRIC_ID = "mt4_m1_ivolume_tick_volume.v1"

MT4_BID_PRICE_BASIS = "mt4_bid_ohlc_v1"
MT4_IVOLUME_SOURCE = "mt4_ivolume_tick_count_v1"

BASELINE_M1_BARS = 240
REQUIRED_COMPLETED_M1_BARS = BASELINE_M1_BARS + 1
VOLUME_QUANTILE = 0.90
CLOSE_LOCATION_THRESHOLD = 0.80
FIXED_ADVERSE_EXECUTION_DEBIT_BPS = 1.0
IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION = 0.005
TARGET_COST_MULTIPLE = 4.0
STOP_COST_MULTIPLE = 8.0
TIME_STOP_M1_BARS = 30
MAX_ENTRY_DELAY_SECONDS = 5
MAX_QUOTE_GAP_SECONDS = 5
MAX_ENTRIES_PER_SYMBOL_UTC_DAY = 1

ROLLOVER_ENTRY_BLACKOUT_START_SECOND = 20 * 60 * 60 + 20 * 60
ROLLOVER_ENTRY_BLACKOUT_END_SECOND = 22 * 60 * 60 + 10 * 60

MTVCLC_V1_SYMBOLS: tuple[str, ...] = (
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

MTVCLCSide = Literal["BUY", "SELL"]
MTVCLCExecutionType = Literal["market"]
MTVCLCQualification = Literal["candidate_unqualified"]

_HEX = frozenset("0123456789abcdef")


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MTVCLCPolicy:
    """The single preregistered MTVCLC-v1 parameter configuration."""

    baseline_m1_bars: int = BASELINE_M1_BARS
    volume_quantile: float = VOLUME_QUANTILE
    close_location_threshold: float = CLOSE_LOCATION_THRESHOLD
    target_cost_multiple: float = TARGET_COST_MULTIPLE
    stop_cost_multiple: float = STOP_COST_MULTIPLE
    outcome_horizon_m1_bars: int = TIME_STOP_M1_BARS
    maximum_entry_delay_seconds: int = MAX_ENTRY_DELAY_SECONDS
    maximum_quote_gap_seconds: int = MAX_QUOTE_GAP_SECONDS
    rollover_entry_blackout_start_second: int = ROLLOVER_ENTRY_BLACKOUT_START_SECOND
    rollover_entry_blackout_end_second: int = ROLLOVER_ENTRY_BLACKOUT_END_SECOND

    @property
    def config_id(self) -> str:
        return MTVCLC_CONFIG_ID

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return exactly the configuration payload sealed by preregistration."""

        return asdict(self)

    def config_sha256(self) -> str:
        return _canonical_sha256(self.to_canonical_dict())


FROZEN_MTVCLC_POLICY = MTVCLCPolicy()


@dataclass(frozen=True, slots=True)
class MTVCLCMarketSourceIdentity:
    """Authenticated IG-MT4 producer identity retained across market streams."""

    broker_account_scope: str
    broker_account_scope_schema: str
    broker_account_scope_version: int
    broker_server: str
    broker_company: str
    consumer_identity: str
    producer_instance_id: str
    terminal_lease_scope: str
    credential_generation_id: str
    bridge_protocol_version: str

    def to_canonical_dict(self) -> dict[str, Any]:
        return asdict(self)

    def identity_sha256(self) -> str:
        return _canonical_sha256(self.to_canonical_dict())


@dataclass(frozen=True, slots=True)
class MTVCLCBidM1Bar:
    """One finalized direct-MT4 bid M1 bar with authentic ``iVolume``."""

    symbol: str
    venue_id: str
    source_id: str
    source_version: str
    source_identity: MTVCLCMarketSourceIdentity
    minute_epoch: int
    bar_seconds: int
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    tick_volume: int
    volume_source: str
    price_basis: str
    closed: bool
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MTVCLCAuthenticatedQuote:
    """One authenticated, executable, two-sided MT4 transport observation."""

    symbol: str
    venue_id: str
    source_id: str
    source_version: str
    source_identity: MTVCLCMarketSourceIdentity
    observed_epoch: int
    bid: float
    ask: float
    source_event_token_sha256: str
    market_event_sequence: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 1e4


@dataclass(frozen=True, slots=True)
class MTVCLCCostCalibration:
    """One frozen, hash-identified cost row used for signal geometry."""

    symbol: str
    calibration_id: str
    source_sha256: str
    p90_spread_bps: float
    commission_bps_per_round_trip: float
    financing_bps_per_trade: float
    account_currency: str
    pnl_currency: str
    convert_on_close_charge_fraction: float
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
        """Return the conversion-adjusted p-star used by the frozen screen."""

        cost = self.recorded_cost_bps
        target = TARGET_COST_MULTIPLE * cost
        stop = STOP_COST_MULTIPLE * cost
        rate = self.convert_on_close_charge_fraction
        numerator = stop * (1.0 + rate) + cost
        denominator = target * (1.0 - rate) + stop * (1.0 + rate)
        return numerator / denominator

    def row_sha256(self) -> str:
        """Bind the opaque calibration source to this symbol's exact numbers."""

        return _canonical_sha256(
            {
                "schema_version": MTVCLC_COST_CALIBRATION_SCHEMA_VERSION,
                "symbol": self.symbol,
                "calibration_id": self.calibration_id,
                "source_sha256": self.source_sha256,
                "p90_spread_bps": self.p90_spread_bps,
                "commission_bps_per_round_trip": (self.commission_bps_per_round_trip),
                "financing_bps_per_trade": self.financing_bps_per_trade,
                "account_currency": self.account_currency,
                "pnl_currency": self.pnl_currency,
                "convert_on_close_charge_fraction": (
                    self.convert_on_close_charge_fraction
                ),
                "adverse_execution_debit_bps": (self.adverse_execution_debit_bps),
            }
        )


@dataclass(frozen=True, slots=True)
class MTVCLCEvaluationRequest:
    """Pure input for one symbol's completed-bar MTVCLC-v1 evaluation."""

    symbol: str
    bars: tuple[MTVCLCBidM1Bar, ...]
    quotes: tuple[MTVCLCAuthenticatedQuote, ...]
    cost: MTVCLCCostCalibration


@dataclass(frozen=True, slots=True)
class MTVCLCTradeCandidate:
    """An unqualified immediate-market trade candidate or a refusal.

    ``allowed=True`` is not execution authority.  ``execution_type`` and
    ``pending_orders_forbidden`` are fixed non-init fields, so callers cannot
    construct a pending entry through this contract.
    """

    symbol: str
    instrument_id: str
    venue_id: str
    allowed: bool
    reasons: tuple[str, ...]
    bar_source_id: str = ""
    bar_source_version: str = ""
    quote_source_id: str = ""
    quote_source_version: str = ""
    market_source_identity_sha256: str = ""
    cost_calibration_id: str = ""
    cost_calibration_source_sha256: str = ""
    cost_calibration_row_sha256: str = ""
    side: MTVCLCSide | None = None
    signal_epoch: int | None = None
    expected_entry_epoch: int | None = None
    entry_deadline_epoch: int | None = None
    entry_epoch: int | None = None
    entry_day: str | None = None
    entry_bid: float | None = None
    entry_ask: float | None = None
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    live_spread_bps: float | None = None
    p90_spread_bps: float | None = None
    recorded_cost_bps: float | None = None
    conversion_charge_fraction: float | None = None
    p_star: float | None = None
    target_bps: float | None = None
    stop_bps: float | None = None
    time_stop_bars: int | None = None
    maximum_quote_gap_seconds: int | None = None
    volume_v90: float | None = None
    signal_tick_volume: int | None = None
    bid_body_bps: float | None = None
    bid_close_location: float | None = None
    strategy_id: str = field(init=False, default=MTVCLC_STRATEGY_ID)
    strategy_version: str = field(init=False, default=MTVCLC_STRATEGY_VERSION)
    config_id: str = field(init=False, default=MTVCLC_CONFIG_ID)
    config_sha256: str = field(init=False, default=MTVCLC_CONFIG_SHA256)
    scope_version: str = field(init=False, default=IG_MT4_SCALP_SCOPE_VERSION)
    source_contract_id: str = field(
        init=False,
        default=MTVCLC_SOURCE_CONTRACT_ID,
    )
    activity_metric_id: str = field(
        init=False,
        default=MTVCLC_ACTIVITY_METRIC_ID,
    )
    execution_type: MTVCLCExecutionType = field(init=False, default="market")
    pending_orders_forbidden: bool = field(init=False, default=True)
    immediate_market_trade: bool = field(init=False, default=True)
    maximum_entries_per_symbol_utc_day: int = field(
        init=False,
        default=MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
    )
    qualification: MTVCLCQualification = field(
        init=False,
        default="candidate_unqualified",
    )
    evidence_qualified: bool = field(init=False, default=False)
    release_authorized: bool = field(init=False, default=False)
    activation_authorized: bool = field(init=False, default=False)
    risk_qualified: bool = field(init=False, default=False)
    sizing_authorized: bool = field(init=False, default=False)
    queue_authorized: bool = field(init=False, default=False)
    broker_trade_authorized: bool = field(init=False, default=False)
    execution_qualified: bool = field(init=False, default=False)
    win_probability: None = field(init=False, default=None)
    schema_version: str = field(
        init=False,
        default=MTVCLC_PROPOSAL_SCHEMA_VERSION,
    )

    def __post_init__(self) -> None:
        if self.allowed and self.reasons:
            raise ValueError("allowed MTVCLC candidate cannot carry refusal reasons")
        if not self.allowed and not self.reasons:
            raise ValueError("refused MTVCLC candidate requires a reason")
        if self.allowed and self.side not in {"BUY", "SELL"}:
            raise ValueError("allowed MTVCLC candidate requires BUY or SELL")
        if self.allowed and (
            type(self.expected_entry_epoch) is not int
            or type(self.entry_deadline_epoch) is not int
            or int(self.entry_deadline_epoch)
            != int(self.expected_entry_epoch) + MAX_ENTRY_DELAY_SECONDS
        ):
            raise ValueError(
                "allowed MTVCLC candidate requires its exact T+5 entry deadline"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_int(value: Any) -> int | None:
    if type(value) is not int:
        return None
    return int(value)


def _valid_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and set(text) <= _HEX


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _valid_market_source_identity(identity: MTVCLCMarketSourceIdentity) -> bool:
    text_values = (
        identity.broker_account_scope,
        identity.broker_account_scope_schema,
        identity.broker_server,
        identity.broker_company,
        identity.consumer_identity,
        identity.producer_instance_id,
        identity.terminal_lease_scope,
        identity.credential_generation_id,
        identity.bridge_protocol_version,
    )
    version = _strict_int(identity.broker_account_scope_version)
    return bool(
        all(_nonempty_text(value) for value in text_values)
        and version is not None
        and version >= 0
    )


def _valid_cost_calibration(
    cost: MTVCLCCostCalibration,
    *,
    expected_symbol: str,
) -> bool:
    values = (
        _finite_float(cost.p90_spread_bps),
        _finite_float(cost.commission_bps_per_round_trip),
        _finite_float(cost.financing_bps_per_trade),
        _finite_float(cost.adverse_execution_debit_bps),
        _finite_float(cost.convert_on_close_charge_fraction),
    )
    if any(value is None for value in values):
        return False
    spread, commission, financing, debit, conversion = (
        float(value) for value in values if value is not None
    )
    currencies_valid = all(
        isinstance(currency, str)
        and len(currency) == 3
        and currency.isalpha()
        and currency.isupper()
        for currency in (cost.account_currency, cost.pnl_currency)
    )
    expected_conversion = (
        0.0
        if cost.account_currency == cost.pnl_currency
        else IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
    )
    return bool(
        cost.symbol == expected_symbol
        and _nonempty_text(cost.calibration_id)
        and _valid_sha256(cost.source_sha256)
        and spread > 0.0
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
        and cost.recorded_cost_bps > 0.0
        and 0.0 < cost.break_even_win_probability < 1.0
    )


def _bar_validation_reasons(
    bars: tuple[MTVCLCBidM1Bar, ...],
    *,
    symbol: str,
) -> tuple[str, ...]:
    if len(bars) != REQUIRED_COMPLETED_M1_BARS:
        return ("history_must_contain_exactly_241_completed_m1_bars",)

    reasons: list[str] = []
    source_pairs: list[tuple[str, str]] = []
    source_identities: list[MTVCLCMarketSourceIdentity] = []
    epochs: list[int] = []

    for bar in bars:
        if bar.symbol != symbol:
            _append_reason(reasons, "bar_symbol_mismatch")
        if bar.venue_id != IG_MT4_VENUE_ID:
            _append_reason(reasons, "bar_venue_mismatch")
        if not _nonempty_text(bar.source_id) or not _nonempty_text(bar.source_version):
            _append_reason(reasons, "bar_source_identity_missing")
        source_pairs.append((bar.source_id, bar.source_version))
        source_identities.append(bar.source_identity)
        if not _valid_market_source_identity(bar.source_identity):
            _append_reason(reasons, "bar_authenticated_source_identity_invalid")
        if bar.volume_source != MT4_IVOLUME_SOURCE:
            _append_reason(reasons, "bar_volume_source_not_direct_mt4_ivolume")
        if bar.price_basis != MT4_BID_PRICE_BASIS:
            _append_reason(reasons, "bar_price_basis_not_direct_mt4_bid_ohlc")
        if bar.closed is not True:
            _append_reason(reasons, "bar_not_closed")
        if tuple(bar.quality_flags or ()):
            _append_reason(reasons, "bar_quality_flags_present")

        epoch = _strict_int(bar.minute_epoch)
        if epoch is None or epoch <= 0 or epoch % 60 != 0:
            _append_reason(reasons, "bar_time_invalid")
        else:
            epochs.append(epoch)
        if _strict_int(bar.bar_seconds) != 60:
            _append_reason(reasons, "bar_timeframe_not_m1")

        prices = tuple(
            _finite_float(value)
            for value in (
                bar.bid_open,
                bar.bid_high,
                bar.bid_low,
                bar.bid_close,
            )
        )
        if any(value is None or value <= 0.0 for value in prices):
            _append_reason(reasons, "bar_bid_prices_invalid")
        else:
            open_px, high_px, low_px, close_px = (
                float(value) for value in prices if value is not None
            )
            if (
                high_px < max(open_px, close_px)
                or low_px > min(open_px, close_px)
                or high_px < low_px
            ):
                _append_reason(reasons, "bar_bid_geometry_invalid")

        tick_volume = _strict_int(bar.tick_volume)
        if tick_volume is None or tick_volume < 0:
            _append_reason(reasons, "bar_ivolume_invalid")

    if len(set(source_pairs)) != 1:
        _append_reason(reasons, "mixed_bar_source_identity")
    if source_identities and any(
        identity != source_identities[0] for identity in source_identities[1:]
    ):
        _append_reason(reasons, "mixed_bar_authenticated_source_identity")
    if len(epochs) == len(bars) and any(
        current <= previous for previous, current in zip(epochs, epochs[1:])
    ):
        _append_reason(reasons, "bars_not_strictly_time_ordered")
    return tuple(reasons)


def _quote_validation_reasons(
    quotes: tuple[MTVCLCAuthenticatedQuote, ...],
    *,
    symbol: str,
    bar_source_identity: MTVCLCMarketSourceIdentity,
) -> tuple[str, ...]:
    reasons: list[str] = []
    source_pairs: list[tuple[str, str]] = []
    source_identities: list[MTVCLCMarketSourceIdentity] = []
    epochs: list[int] = []

    for quote in quotes:
        if quote.symbol != symbol:
            _append_reason(reasons, "quote_symbol_mismatch")
        if quote.venue_id != IG_MT4_VENUE_ID:
            _append_reason(reasons, "quote_venue_mismatch")
        if not _nonempty_text(quote.source_id) or not _nonempty_text(
            quote.source_version
        ):
            _append_reason(reasons, "quote_source_identity_missing")
        source_pairs.append((quote.source_id, quote.source_version))
        source_identities.append(quote.source_identity)
        if not _valid_market_source_identity(quote.source_identity):
            _append_reason(reasons, "quote_authenticated_source_identity_invalid")
        if quote.source_identity != bar_source_identity:
            _append_reason(reasons, "bar_quote_authenticated_source_mismatch")

        epoch = _strict_int(quote.observed_epoch)
        if epoch is None or epoch <= 0:
            _append_reason(reasons, "quote_time_invalid")
        else:
            epochs.append(epoch)
        bid = _finite_float(quote.bid)
        ask = _finite_float(quote.ask)
        if bid is None or ask is None or bid <= 0.0 or ask < bid:
            _append_reason(reasons, "quote_prices_invalid")
        if not _valid_sha256(quote.source_event_token_sha256):
            _append_reason(reasons, "quote_event_identity_invalid")
        market_sequence = _strict_int(quote.market_event_sequence)
        if market_sequence is None or market_sequence < 0:
            _append_reason(reasons, "quote_event_identity_invalid")

    if len(set(source_pairs)) > 1:
        _append_reason(reasons, "mixed_quote_source_identity")
    if source_identities and any(
        identity != source_identities[0] for identity in source_identities[1:]
    ):
        _append_reason(reasons, "mixed_quote_authenticated_source_identity")
    if len(epochs) == len(quotes) and any(
        current <= previous for previous, current in zip(epochs, epochs[1:])
    ):
        _append_reason(reasons, "quotes_not_strictly_time_ordered")
    return tuple(reasons)


def _type7_quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def entry_in_rollover_blackout(epoch: int) -> bool:
    """Return membership in the frozen half-open UTC interval [20:20, 22:10)."""

    strict_epoch = _strict_int(epoch)
    if strict_epoch is None or strict_epoch <= 0:
        raise ValueError("rollover clock must be a positive integer UTC epoch")
    second_of_day = strict_epoch % (24 * 60 * 60)
    return (
        ROLLOVER_ENTRY_BLACKOUT_START_SECOND
        <= second_of_day
        < ROLLOVER_ENTRY_BLACKOUT_END_SECOND
    )


def _utc_day(epoch: int) -> str | None:
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _candidate(
    *,
    request: MTVCLCEvaluationRequest,
    instrument: IgMt4InstrumentIdentity | None,
    allowed: bool,
    reasons: tuple[str, ...],
    **values: Any,
) -> MTVCLCTradeCandidate:
    bars = tuple(request.bars or ())
    quotes = tuple(request.quotes or ())
    bar = bars[-1] if bars else None
    quote = quotes[0] if quotes else None
    identity = bar.source_identity if bar is not None else None
    cost = request.cost
    return MTVCLCTradeCandidate(
        symbol=(
            instrument.canonical_symbol
            if instrument is not None
            else str(request.symbol or "").strip().upper()
        ),
        instrument_id=instrument.instrument_id if instrument is not None else "",
        venue_id=instrument.venue if instrument is not None else "",
        allowed=allowed,
        reasons=reasons,
        bar_source_id=bar.source_id if bar is not None else "",
        bar_source_version=bar.source_version if bar is not None else "",
        quote_source_id=quote.source_id if quote is not None else "",
        quote_source_version=quote.source_version if quote is not None else "",
        market_source_identity_sha256=(
            identity.identity_sha256()
            if identity is not None and _valid_market_source_identity(identity)
            else ""
        ),
        cost_calibration_id=str(cost.calibration_id or ""),
        cost_calibration_source_sha256=str(cost.source_sha256 or ""),
        cost_calibration_row_sha256=(
            cost.row_sha256()
            if _valid_cost_calibration(
                cost,
                expected_symbol=(
                    instrument.canonical_symbol
                    if instrument is not None
                    else str(request.symbol or "").strip().upper()
                ),
            )
            else ""
        ),
        **values,
    )


def evaluate_mtvclc(
    request: MTVCLCEvaluationRequest,
    policy: MTVCLCPolicy = FROZEN_MTVCLC_POLICY,
) -> MTVCLCTradeCandidate:
    """Evaluate one frozen MTVCLC-v1 immediate-market trade candidate."""

    symbol = str(request.symbol or "").strip().upper()
    instrument = get_ig_mt4_instrument(symbol)
    if policy != FROZEN_MTVCLC_POLICY or policy.config_sha256() != (
        MTVCLC_CONFIG_SHA256
    ):
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("configuration_not_frozen",),
        )
    if not symbol:
        return _candidate(
            request=request,
            instrument=None,
            allowed=False,
            reasons=("entry_symbol_missing",),
        )
    if instrument is None or symbol not in MTVCLC_V1_SYMBOLS:
        return _candidate(
            request=request,
            instrument=None,
            allowed=False,
            reasons=("symbol_outside_exact_scope",),
        )
    if not _valid_cost_calibration(request.cost, expected_symbol=symbol):
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("cost_calibration_invalid",),
        )

    bars = tuple(request.bars or ())
    bar_reasons = _bar_validation_reasons(bars, symbol=symbol)
    if bar_reasons:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=bar_reasons,
        )

    signal_bar = bars[-1]
    signal_close_epoch = signal_bar.minute_epoch + 60
    entry_day = _utc_day(signal_close_epoch)
    if entry_day is None:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("bar_time_invalid",),
        )
    baseline = bars[:-1]
    volume_v90 = _type7_quantile(
        [float(bar.tick_volume) for bar in baseline],
        VOLUME_QUANTILE,
    )
    if volume_v90 <= 0.0:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("volume_v90_not_positive",),
            volume_v90=volume_v90,
        )
    if not signal_bar.tick_volume > volume_v90:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("tick_volume_not_strictly_above_v90",),
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
        )

    bar_range = signal_bar.bid_high - signal_bar.bid_low
    if bar_range <= 0.0:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("bid_range_not_positive",),
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
        )
    body_delta = signal_bar.bid_close - signal_bar.bid_open
    if body_delta == 0.0:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("bid_body_direction_mismatch",),
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
        )
    side: MTVCLCSide = "BUY" if body_delta > 0.0 else "SELL"
    signed_body = abs(body_delta)
    body_bps = signed_body / signal_bar.bid_open * 1e4
    if body_bps < request.cost.recorded_cost_bps:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("bid_body_below_recorded_cost",),
            side=side,
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
            bid_body_bps=body_bps,
        )
    close_location = (
        (signal_bar.bid_close - signal_bar.bid_low) / bar_range
        if side == "BUY"
        else (signal_bar.bid_high - signal_bar.bid_close) / bar_range
    )
    if close_location < CLOSE_LOCATION_THRESHOLD:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("bid_close_location_below_threshold",),
            side=side,
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
            bid_body_bps=body_bps,
            bid_close_location=close_location,
        )

    quote_reasons = _quote_validation_reasons(
        tuple(request.quotes or ()),
        symbol=symbol,
        bar_source_identity=signal_bar.source_identity,
    )
    if quote_reasons:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=quote_reasons,
            side=side,
            signal_epoch=signal_bar.minute_epoch,
            expected_entry_epoch=signal_close_epoch,
            entry_deadline_epoch=signal_close_epoch + MAX_ENTRY_DELAY_SECONDS,
            entry_day=entry_day,
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
            bid_body_bps=body_bps,
            bid_close_location=close_location,
        )

    quotes = tuple(request.quotes or ())
    quote_index = bisect.bisect_left(
        quotes,
        signal_close_epoch,
        key=lambda item: item.observed_epoch,
    )
    if quote_index >= len(quotes):
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("contemporaneous_entry_quote_missing",),
            side=side,
            signal_epoch=signal_bar.minute_epoch,
            expected_entry_epoch=signal_close_epoch,
            entry_deadline_epoch=signal_close_epoch + MAX_ENTRY_DELAY_SECONDS,
            entry_day=entry_day,
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
            bid_body_bps=body_bps,
            bid_close_location=close_location,
        )
    quote = quotes[quote_index]
    expected_entry_epoch = int(quote.observed_epoch)
    entry_deadline_epoch = expected_entry_epoch + MAX_ENTRY_DELAY_SECONDS
    entry_day = _utc_day(expected_entry_epoch)
    if entry_day is None:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("entry_quote_time_invalid",),
            side=side,
            signal_epoch=signal_bar.minute_epoch,
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
            bid_body_bps=body_bps,
            bid_close_location=close_location,
        )
    if quote.spread_bps > request.cost.p90_spread_bps:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("live_spread_above_frozen_p90",),
            side=side,
            signal_epoch=signal_bar.minute_epoch,
            expected_entry_epoch=expected_entry_epoch,
            entry_deadline_epoch=entry_deadline_epoch,
            entry_epoch=quote.observed_epoch,
            entry_day=entry_day,
            entry_bid=quote.bid,
            entry_ask=quote.ask,
            live_spread_bps=quote.spread_bps,
            p90_spread_bps=request.cost.p90_spread_bps,
            volume_v90=volume_v90,
            signal_tick_volume=signal_bar.tick_volume,
            bid_body_bps=body_bps,
            bid_close_location=close_location,
        )

    target_bps = TARGET_COST_MULTIPLE * request.cost.recorded_cost_bps
    stop_bps = STOP_COST_MULTIPLE * request.cost.recorded_cost_bps
    entry_price = quote.ask if side == "BUY" else quote.bid
    if side == "BUY":
        target_price = entry_price * (1.0 + target_bps / 1e4)
        stop_price = entry_price * (1.0 - stop_bps / 1e4)
    else:
        target_price = entry_price * (1.0 - target_bps / 1e4)
        stop_price = entry_price * (1.0 + stop_bps / 1e4)
    if min(entry_price, target_price, stop_price) <= 0.0:
        return _candidate(
            request=request,
            instrument=instrument,
            allowed=False,
            reasons=("bracket_geometry_invalid",),
            side=side,
        )

    return _candidate(
        request=request,
        instrument=instrument,
        allowed=True,
        reasons=(),
        side=side,
        signal_epoch=signal_bar.minute_epoch,
        expected_entry_epoch=expected_entry_epoch,
        entry_deadline_epoch=entry_deadline_epoch,
        entry_epoch=quote.observed_epoch,
        entry_day=entry_day,
        entry_bid=quote.bid,
        entry_ask=quote.ask,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        live_spread_bps=quote.spread_bps,
        p90_spread_bps=request.cost.p90_spread_bps,
        recorded_cost_bps=request.cost.recorded_cost_bps,
        conversion_charge_fraction=(request.cost.convert_on_close_charge_fraction),
        p_star=request.cost.break_even_win_probability,
        target_bps=target_bps,
        stop_bps=stop_bps,
        time_stop_bars=TIME_STOP_M1_BARS,
        maximum_quote_gap_seconds=MAX_QUOTE_GAP_SECONDS,
        volume_v90=volume_v90,
        signal_tick_volume=signal_bar.tick_volume,
        bid_body_bps=body_bps,
        bid_close_location=close_location,
    )


if IG_MT4_SCALP_SCOPE_VERSION != "fxstack.ig_mt4.scalp_scope.v3":
    raise RuntimeError("MTVCLC-v1 requires IG MT4 scalp scope v3")
if IG_MT4_SCALP_SYMBOLS != MTVCLC_V1_SYMBOLS:
    raise RuntimeError("MTVCLC-v1 exact 22-symbol scope drifted")
if FROZEN_MTVCLC_POLICY.config_sha256() != MTVCLC_CONFIG_SHA256:
    raise RuntimeError("MTVCLC-v1 frozen configuration hash drifted")


__all__ = [
    "BASELINE_M1_BARS",
    "CLOSE_LOCATION_THRESHOLD",
    "FIXED_ADVERSE_EXECUTION_DEBIT_BPS",
    "FROZEN_MTVCLC_POLICY",
    "IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION",
    "MAX_ENTRIES_PER_SYMBOL_UTC_DAY",
    "MAX_ENTRY_DELAY_SECONDS",
    "MAX_QUOTE_GAP_SECONDS",
    "MT4_BID_PRICE_BASIS",
    "MT4_IVOLUME_SOURCE",
    "MTVCLC_ACTIVITY_METRIC_ID",
    "MTVCLC_CONFIG_ID",
    "MTVCLC_CONFIG_SHA256",
    "MTVCLC_COST_CALIBRATION_SCHEMA_VERSION",
    "MTVCLC_POLICY_SCHEMA_VERSION",
    "MTVCLC_PROPOSAL_SCHEMA_VERSION",
    "MTVCLC_SOURCE_CONTRACT_ID",
    "MTVCLC_STRATEGY_ID",
    "MTVCLC_STRATEGY_VERSION",
    "MTVCLC_V1_SYMBOLS",
    "MTVCLCAuthenticatedQuote",
    "MTVCLCBidM1Bar",
    "MTVCLCCostCalibration",
    "MTVCLCEvaluationRequest",
    "MTVCLCMarketSourceIdentity",
    "MTVCLCPolicy",
    "MTVCLCTradeCandidate",
    "REQUIRED_COMPLETED_M1_BARS",
    "STOP_COST_MULTIPLE",
    "TARGET_COST_MULTIPLE",
    "TIME_STOP_M1_BARS",
    "VOLUME_QUANTILE",
    "entry_in_rollover_blackout",
    "evaluate_mtvclc",
]
