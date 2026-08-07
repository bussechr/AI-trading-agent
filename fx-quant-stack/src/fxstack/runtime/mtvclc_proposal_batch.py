# AGENT: ROLE: Pure runtime adapter from authenticated MT4 rows to MTVCLC-v1 inputs.
# AGENT: ENTRYPOINT: `evaluate_mtvclc_profile_batch` with an explicit strategy profile.
# AGENT: PRIMARY INPUTS: exact ordered scope, direct bid/iVolume M1 rows, transport quotes, frozen typed costs, heartbeat identity.
# AGENT: PRIMARY OUTPUTS: ranked authority-free immediate-market candidates and immutable diagnostics.
# AGENT: STATE / SIDE EFFECTS: bounded exact-content caches only; no settings, I/O, certificate, risk, sizing, queue, or broker access.
"""Build production MTVCLC inputs without conferring trade authority.

The bridge exposes a merged M1 endpoint: completed direct MT4 bid/iVolume rows
and tick-derived mid/event-count fallback rows can coexist.  This adapter is
the strategy-specific fail-closed seam that selects only the direct rows and
never normalizes a fallback into an MTVCLC bar.

The adapter also projects the active singleton MT4 producer identity from the
runtime state and requires every selected bar and quote row to carry that exact
authenticated market-source ID.  Live availability is fail-closed on the
server-owned first receipt of the signal bar.  Quote evaluation uses the bridge
transport receipt clock, matching the sealed collector's
``floor(received_at_epoch)`` observation clock; exact fractional receipt
timestamps remain in diagnostics.

An allowed result is still only an unqualified mathematical candidate.  The
output type fixes ``execution_type`` to ``market`` and cannot represent a
pending order.  This module deliberately has no admission, certificate,
activation, sizing, persistence, queue, or execution dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import math
from typing import Any

from fxstack.api.protocol_identity import BRIDGE_PROTOCOL_VERSION
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS, IG_MT4_VENUE_ID
from fxstack.runtime.market_source_identity import (
    MARKET_SOURCE_FIELDS,
    MARKET_SOURCE_SCHEMA,
    AuthenticatedMarketSource,
    current_authenticated_market_source,
    market_source_row_error,
)
from fxstack.strategy.mtvclc import (
    FROZEN_MTVCLC_POLICY,
    MT4_BID_PRICE_BASIS,
    MT4_IVOLUME_SOURCE,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_V1_SYMBOLS,
    REQUIRED_COMPLETED_M1_BARS,
    MTVCLCAuthenticatedQuote,
    MTVCLCBidM1Bar,
    MTVCLCCostCalibration,
    MTVCLCEvaluationRequest,
    MTVCLCMarketSourceIdentity,
    MTVCLCPolicy,
    MTVCLCTradeCandidate,
    _runtime_prepared_quotes,
    evaluate_mtvclc,
    runtime_prepared_bars,
)


MTVCLC_RUNTIME_PROFILE_ID = MTVCLC_STRATEGY_ID
MTVCLC_PROPOSAL_BATCH_SCHEMA_VERSION = "fxstack.runtime.mtvclc_proposal_batch.v2"
M1_SECONDS = 60

_BAR_CACHE_FIELDS = (
    "provider",
    "canonical_symbol",
    "pair",
    "venue",
    "timeframe",
    "source_timeframe",
    "price_basis",
    "volume_source",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "volume",
    *MARKET_SOURCE_FIELDS,
)
_BAR_OPEN_INDEX = _BAR_CACHE_FIELDS.index("bid_open")
_BAR_HIGH_INDEX = _BAR_CACHE_FIELDS.index("bid_high")
_BAR_LOW_INDEX = _BAR_CACHE_FIELDS.index("bid_low")
_BAR_CLOSE_INDEX = _BAR_CACHE_FIELDS.index("bid_close")
_BAR_VOLUME_INDEX = _BAR_CACHE_FIELDS.index("volume")
_CACHEABLE_BAR_VALUE_TYPES = frozenset((str, int, float, bool, type(None)))
_PreparedBarsResult = tuple[
    tuple[MTVCLCBidM1Bar, ...],
    tuple[str, ...],
    int,
    int,
    int,
    int | None,
]
_PREPARED_BAR_CACHE: dict[
    tuple[Any, ...],
    tuple[float, _PreparedBarsResult],
] = {}
_PREPARED_BAR_CACHE_MINUTE: int | None = None


@dataclass(frozen=True, slots=True)
class MTVCLCSymbolProposalDiagnostic:
    symbol: str
    structural_ready: bool
    structural_reasons: tuple[str, ...]
    raw_bar_count: int
    filtered_current_bar_count: int
    finalized_bar_count: int
    selected_history_count: int
    latest_finalized_minute_epoch: int | None
    raw_quote_count: int
    selected_quote_count: int
    quote_transport_received_at_epochs: tuple[float, ...]
    cost_calibration_id: str
    cost_calibration_source_sha256: str
    cost_calibration_row_sha256: str
    evaluation_allowed: bool | None = None
    evaluation_reasons: tuple[str, ...] = ()
    evaluation_side: str | None = None
    evaluation_signal_epoch: int | None = None
    evaluation_expected_entry_epoch: int | None = None
    evaluation_volume_v90: float | None = None
    evaluation_signal_tick_volume: int | None = None
    evaluation_activity_ratio: float | None = None
    evaluation_bid_body_bps: float | None = None
    evaluation_bid_close_location: float | None = None
    evaluation_live_spread_bps: float | None = None
    evaluation_p90_spread_bps: float | None = None
    evaluation_recorded_cost_bps: float | None = None
    evaluation_p_star: float | None = None
    evaluation_target_bps: float | None = None
    evaluation_stop_bps: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name) for name in _SYMBOL_DIAGNOSTIC_FIELD_ORDER
        }

    def to_decision_dict(self) -> dict[str, Any]:
        """Omit absent optional evaluation evidence from recurring decisions."""

        payload = {
            name: getattr(self, name)
            for name in _SYMBOL_DIAGNOSTIC_REQUIRED_FIELD_ORDER
        }
        for name in _SYMBOL_DIAGNOSTIC_OPTIONAL_FIELD_ORDER:
            value = getattr(self, name)
            if value is None or value == ():
                continue
            payload[name] = value
        return payload


_SYMBOL_DIAGNOSTIC_REQUIRED_FIELD_ORDER = (
    "symbol",
    "structural_ready",
    "structural_reasons",
    "raw_bar_count",
    "filtered_current_bar_count",
    "finalized_bar_count",
    "selected_history_count",
    "latest_finalized_minute_epoch",
    "raw_quote_count",
    "selected_quote_count",
    "quote_transport_received_at_epochs",
    "cost_calibration_id",
    "cost_calibration_source_sha256",
    "cost_calibration_row_sha256",
)
_SYMBOL_DIAGNOSTIC_OPTIONAL_FIELD_ORDER = (
    "evaluation_allowed",
    "evaluation_reasons",
    "evaluation_side",
    "evaluation_signal_epoch",
    "evaluation_expected_entry_epoch",
    "evaluation_volume_v90",
    "evaluation_signal_tick_volume",
    "evaluation_activity_ratio",
    "evaluation_bid_body_bps",
    "evaluation_bid_close_location",
    "evaluation_live_spread_bps",
    "evaluation_p90_spread_bps",
    "evaluation_recorded_cost_bps",
    "evaluation_p_star",
    "evaluation_target_bps",
    "evaluation_stop_bps",
)
_SYMBOL_DIAGNOSTIC_FIELD_ORDER = (
    _SYMBOL_DIAGNOSTIC_REQUIRED_FIELD_ORDER
    + _SYMBOL_DIAGNOSTIC_OPTIONAL_FIELD_ORDER
)
if tuple(item.name for item in fields(MTVCLCSymbolProposalDiagnostic)) != (
    _SYMBOL_DIAGNOSTIC_FIELD_ORDER
):
    raise RuntimeError("MTVCLC symbol diagnostic field order drifted")


@dataclass(frozen=True, slots=True)
class MTVCLCProposalBatchDiagnostics:
    accepted: bool
    reasons: tuple[str, ...]
    strategy_profile: str
    as_of_epoch: float | None
    current_minute_epoch: int | None
    common_closed_minute_epoch: int | None
    expected_symbols: tuple[str, ...]
    observed_bar_symbols: tuple[str, ...]
    observed_quote_symbols: tuple[str, ...]
    observed_cost_symbols: tuple[str, ...]
    market_source_id: str
    producer_instance_id: str
    symbol_diagnostics: tuple[MTVCLCSymbolProposalDiagnostic, ...]
    schema_version: str = MTVCLC_PROPOSAL_BATCH_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = {item.name: getattr(self, item.name) for item in fields(self)}
        payload["symbol_diagnostics"] = tuple(
            diagnostic.to_dict() for diagnostic in self.symbol_diagnostics
        )
        return payload

    def to_cycle_summary(self) -> dict[str, Any]:
        """Return batch-wide state; per-symbol evidence lives in decisions."""

        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "symbol_diagnostics"
        }
        payload.update(
            symbol_diagnostic_count=len(self.symbol_diagnostics),
            structural_ready_count=sum(
                diagnostic.structural_ready for diagnostic in self.symbol_diagnostics
            ),
            evaluation_allowed_count=sum(
                diagnostic.evaluation_allowed is True
                for diagnostic in self.symbol_diagnostics
            ),
        )
        return payload


@dataclass(frozen=True, slots=True)
class MTVCLCProposalBatchResult:
    proposals: tuple[MTVCLCTradeCandidate, ...]
    diagnostics: MTVCLCProposalBatchDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposals": tuple(proposal.to_dict() for proposal in self.proposals),
            "diagnostics": self.diagnostics.to_dict(),
        }


@dataclass(frozen=True, slots=True, eq=False)
class _SourceProjection:
    authenticated: AuthenticatedMarketSource
    strategy: MTVCLCMarketSourceIdentity
    _hash: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_hash", hash((self.authenticated, self.strategy)))

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        return self is other or (
            type(other) is _SourceProjection
            and self.authenticated == other.authenticated
            and self.strategy == other.strategy
        )


@dataclass(frozen=True, slots=True, eq=False)
class _BarProjection:
    values: tuple[Any, ...]


class _CachedBarRow(dict[str, Any]):
    """Private immutable row carrying its reusable exact-content cache key."""

    __slots__ = ("projection", "quality_flags")

    def __init__(
        self,
        row: Mapping[str, Any],
        projection: _BarProjection,
        quality_flags: Any,
    ) -> None:
        dict.__init__(self, row)
        self.projection = projection
        self.quality_flags = quality_flags

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("cached MTVCLC bar rows are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> _CachedBarRow:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _CachedBarRow:
        return self


class _BarProjectionKey(tuple[_BarProjection, ...]):
    """Ordered row identity with a once-computed structural hash."""

    def __new__(cls, projections: Sequence[_BarProjection]) -> _BarProjectionKey:
        instance = super().__new__(cls, projections)
        instance._content_hash = tuple.__hash__(instance)
        return instance

    def __hash__(self) -> int:
        return self._content_hash


class _CachedBarRows(list[dict[str, Any]]):
    """Private immutable row batch carrying its aggregate cache key."""

    __slots__ = ("projection_key",)

    def __init__(self, rows: Sequence[_CachedBarRow]) -> None:
        list.__init__(self, rows)
        self.projection_key = _BarProjectionKey(
            tuple(row.projection for row in rows)
        )

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("cached MTVCLC bar row batches are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable

    def __copy__(self) -> _CachedBarRows:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _CachedBarRows:
        return self


@dataclass(frozen=True, slots=True)
class _PreparedSymbol:
    request: MTVCLCEvaluationRequest | None
    diagnostic_base: tuple[Any, ...]


def _finite_float(value: Any) -> float | None:
    if type(value) is float:
        return value if math.isfinite(value) else None
    if type(value) is int:
        try:
            return float(value)
        except OverflowError:
            return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_int(value: Any) -> int | None:
    return int(value) if type(value) is int else None


def _parse_epoch(value: Any) -> int | None:
    if type(value) is int and 0 < value <= 2**53:
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        seconds = value.timestamp()
    elif isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            seconds = float(text)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return None
            seconds = parsed.astimezone(timezone.utc).timestamp()
    if not math.isfinite(seconds) or seconds <= 0.0:
        return None
    rounded = round(seconds)
    if abs(seconds - rounded) > 1e-6:
        return None
    return int(rounded)


def _quality_flags(row: Mapping[str, Any]) -> tuple[str, ...]:
    raw = row.get("quality_flags")
    if raw is None:
        return ()
    if type(raw) in (list, tuple) and not raw:
        return ()
    if isinstance(raw, str):
        values: Sequence[Any] = (raw,)
    elif isinstance(raw, Sequence):
        values = raw
    else:
        values = (raw,)
    return tuple(
        str(value or "").strip() for value in values if str(value or "").strip()
    )


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _normalized_scope_keys(value: Any) -> tuple[tuple[str, ...], bool]:
    if not isinstance(value, Mapping):
        return (), False
    keys: list[str] = []
    seen: set[str] = set()
    duplicate = False
    for raw_key in value:
        key = str(raw_key or "").strip().upper()
        if not key or key in seen:
            duplicate = True
        keys.append(key)
        seen.add(key)
    return tuple(keys), not duplicate


def _scope_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(raw_key or "").strip().upper(): raw_value
        for raw_key, raw_value in value.items()
    }


@lru_cache(maxsize=32)
def _source_projection(
    authenticated: AuthenticatedMarketSource,
    strategy: MTVCLCMarketSourceIdentity,
) -> _SourceProjection:
    return _SourceProjection(authenticated=authenticated, strategy=strategy)


def _project_source_identity(
    state: Mapping[str, Any],
    *,
    as_of_epoch: float,
) -> tuple[_SourceProjection | None, tuple[str, ...]]:
    authenticated, error = current_authenticated_market_source(
        state,
        now_epoch=as_of_epoch,
        require_active_lease=True,
        expected_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    if authenticated is None:
        return None, (str(error or "market_source_unattested"),)
    reasons: list[str] = []
    if authenticated.broker_venue_id != IG_MT4_VENUE_ID:
        reasons.append("market_source_venue_mismatch")
    scope_schema = str(state.get("broker_account_scope_schema") or "").strip()
    scope_version = _strict_int(state.get("broker_account_scope_version"))
    broker_server = str(state.get("broker_server") or "").strip()
    broker_company = str(state.get("broker_company") or "").strip()
    if not scope_schema:
        reasons.append("market_source_account_scope_schema_missing")
    if scope_version is None or scope_version < 0:
        reasons.append("market_source_account_scope_version_invalid")
    if not broker_server:
        reasons.append("market_source_broker_server_missing")
    if not broker_company:
        reasons.append("market_source_broker_company_missing")
    if reasons:
        return None, tuple(reasons)
    assert scope_version is not None
    return (
        _source_projection(
            authenticated,
            MTVCLCMarketSourceIdentity(
                broker_account_scope=authenticated.broker_account_scope,
                broker_account_scope_schema=scope_schema,
                broker_account_scope_version=scope_version,
                broker_server=broker_server,
                broker_company=broker_company,
                consumer_identity=authenticated.producer_identity,
                producer_instance_id=authenticated.producer_instance_id,
                terminal_lease_scope=authenticated.terminal_lease_scope,
                credential_generation_id=(authenticated.credential_generation_id),
                bridge_protocol_version=authenticated.bridge_protocol_version,
            ),
        ),
        (),
    )


def _bar_from_row_uncached(
    *,
    row: Mapping[str, Any],
    symbol: str,
    minute_epoch: int,
    source: _SourceProjection,
) -> tuple[MTVCLCBidM1Bar | None, tuple[str, ...]]:
    reasons: list[str] = []
    provider = str(row.get("provider") or "").strip().lower()
    if provider != "mt4_bridge":
        reasons.append("bar_provider_not_mt4_bridge")
    canonical_symbol = (
        str(row.get("canonical_symbol") or row.get("pair") or "").strip().upper()
    )

    if canonical_symbol != symbol:
        reasons.append("bar_symbol_mismatch")
    venue = str(row.get("venue") or "").strip().lower()
    if venue != IG_MT4_VENUE_ID:
        reasons.append("bar_venue_mismatch")
    timeframe = (
        str(row.get("timeframe") or row.get("source_timeframe") or "M1").strip().upper()
    )
    if timeframe != "M1":
        reasons.append("bar_timeframe_mismatch")
    source_error = market_source_row_error(row, expected=source.authenticated)
    if source_error:
        reasons.append(f"bar_{source_error}")
    if row.get("price_basis") != MT4_BID_PRICE_BASIS:
        reasons.append("bar_price_basis_not_direct_mt4_bid_ohlc")
    if row.get("volume_source") != MT4_IVOLUME_SOURCE:
        reasons.append("bar_volume_source_not_direct_mt4_ivolume")
    flags = _quality_flags(row)
    if flags:
        reasons.append("bar_quality_flags_present")

    open_px = _finite_float(row.get("bid_open"))
    high_px = _finite_float(row.get("bid_high"))
    low_px = _finite_float(row.get("bid_low"))
    close_px = _finite_float(row.get("bid_close"))
    if (
        open_px is None
        or high_px is None
        or low_px is None
        or close_px is None
        or open_px <= 0.0
        or high_px <= 0.0
        or low_px <= 0.0
        or close_px <= 0.0
    ):
        reasons.append("bar_bid_prices_invalid")
    elif (
        high_px < max(open_px, close_px)
        or low_px > min(open_px, close_px)
        or high_px < low_px
    ):
        reasons.append("bar_bid_geometry_invalid")
    tick_volume = _strict_int(row.get("volume"))
    if tick_volume is None or tick_volume < 0:
        reasons.append("bar_ivolume_not_strict_integer")
    if reasons:
        return None, tuple(reasons)

    assert tick_volume is not None
    assert open_px is not None
    assert high_px is not None
    assert low_px is not None
    assert close_px is not None
    return (
        MTVCLCBidM1Bar(
            symbol=symbol,
            venue_id=IG_MT4_VENUE_ID,
            source_id=source.authenticated.source_id,
            source_version=MARKET_SOURCE_SCHEMA,
            source_identity=source.strategy,
            minute_epoch=minute_epoch,
            bar_seconds=M1_SECONDS,
            bid_open=open_px,
            bid_high=high_px,
            bid_low=low_px,
            bid_close=close_px,
            tick_volume=tick_volume,
            volume_source=MT4_IVOLUME_SOURCE,
            price_basis=MT4_BID_PRICE_BASIS,
            closed=True,
            quality_flags=(),
        ),
        (),
    )


def _cacheable_bar_projection(
    row: dict[str, Any],
) -> tuple[tuple[Any, ...], Any] | None:
    projection = tuple(map(row.get, _BAR_CACHE_FIELDS))
    quality_flags = row.get("quality_flags")
    if type(quality_flags) in (list, tuple):
        if not quality_flags:
            quality_flags = ()
        elif set(map(type, quality_flags)).issubset(_CACHEABLE_BAR_VALUE_TYPES):
            quality_flags = tuple(quality_flags)
        else:
            return None
    if not (
        type(quality_flags) in _CACHEABLE_BAR_VALUE_TYPES
        or type(quality_flags) is tuple
    ):
        return None
    if (
        type(projection[_BAR_VOLUME_INDEX]) is not int
        or type(projection[_BAR_OPEN_INDEX]) is bool
        or type(projection[_BAR_HIGH_INDEX]) is bool
        or type(projection[_BAR_LOW_INDEX]) is bool
        or type(projection[_BAR_CLOSE_INDEX]) is bool
        or not set(map(type, projection)).issubset(_CACHEABLE_BAR_VALUE_TYPES)
    ):
        return None
    return projection, quality_flags


def cache_mtvclc_bar_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Copy an external row and retain its immutable adapter cache key."""

    copied = dict(row)
    cached = _cacheable_bar_projection(copied)
    if cached is None:
        return copied
    projection, quality_flags = cached
    return _CachedBarRow(copied, _BarProjection(projection), quality_flags)


def cache_mtvclc_bar_rows(
    rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Freeze a wholly runtime-owned row batch with one aggregate cache key."""

    if rows and all(type(row) is _CachedBarRow for row in rows):
        return _CachedBarRows(rows)
    return list(rows)


def _prepared_bar_cache_key(
    *,
    symbol: str,
    raw_rows: Any,
    current_minute_epoch: int,
    common_closed_minute_epoch: int,
    source: _SourceProjection,
) -> tuple[Any, ...] | None:
    if type(raw_rows) is _CachedBarRows:
        return (
            symbol,
            current_minute_epoch,
            common_closed_minute_epoch,
            source,
            raw_rows.projection_key,
        )
    if type(raw_rows) is not list or not raw_rows:
        return None
    projections: list[_BarProjection] = []
    for row in raw_rows:
        if type(row) is not _CachedBarRow:
            return None
        projections.append(row.projection)
    return (
        symbol,
        current_minute_epoch,
        common_closed_minute_epoch,
        source,
        tuple(projections),
    )


def _clear_prepared_bar_cache() -> None:
    global _PREPARED_BAR_CACHE_MINUTE
    _PREPARED_BAR_CACHE.clear()
    _PREPARED_BAR_CACHE_MINUTE = None


@lru_cache(maxsize=8192)
def _bar_from_projection(
    projection: tuple[Any, ...] | _BarProjection,
    quality_flags: Any,
    symbol: str,
    minute_epoch: int,
    source: _SourceProjection,
) -> tuple[MTVCLCBidM1Bar | None, tuple[str, ...]]:
    values = projection.values if type(projection) is _BarProjection else projection
    row = dict(zip(_BAR_CACHE_FIELDS, values, strict=True))
    row["quality_flags"] = quality_flags
    return _bar_from_row_uncached(
        row=row,
        symbol=symbol,
        minute_epoch=minute_epoch,
        source=source,
    )


def _bar_from_row(
    *,
    row: Mapping[str, Any],
    symbol: str,
    minute_epoch: int,
    source: _SourceProjection,
) -> tuple[MTVCLCBidM1Bar | None, tuple[str, ...]]:
    if type(row) is _CachedBarRow:
        return _bar_from_projection(
            row.projection,
            row.quality_flags,
            symbol,
            minute_epoch,
            source,
        )
    if type(row) is dict:
        cached = _cacheable_bar_projection(row)
        if cached is not None:
            projection, quality_flags = cached
            return _bar_from_projection(
                projection,
                quality_flags,
                symbol,
                minute_epoch,
                source,
            )
    return _bar_from_row_uncached(
        row=row,
        symbol=symbol,
        minute_epoch=minute_epoch,
        source=source,
    )


def _prepare_bars(
    *,
    symbol: str,
    raw_rows: Any,
    as_of_epoch: float,
    current_minute_epoch: int,
    common_closed_minute_epoch: int,
    source: _SourceProjection,
) -> tuple[
    tuple[MTVCLCBidM1Bar, ...],
    tuple[str, ...],
    int,
    int,
    int,
    int | None,
]:
    reasons: list[str] = []
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        return (), ("bar_rows_invalid",), 0, 0, 0, None
    global _PREPARED_BAR_CACHE_MINUTE
    cache_key = _prepared_bar_cache_key(
        symbol=symbol,
        raw_rows=raw_rows,
        current_minute_epoch=current_minute_epoch,
        common_closed_minute_epoch=common_closed_minute_epoch,
        source=source,
    )
    if cache_key is not None:
        if _PREPARED_BAR_CACHE_MINUTE != current_minute_epoch:
            _PREPARED_BAR_CACHE.clear()
            _PREPARED_BAR_CACHE_MINUTE = current_minute_epoch
        cached = _PREPARED_BAR_CACHE.get(cache_key)
        if cached is not None and as_of_epoch >= cached[0]:
            return cached[1]
    raw_count = len(raw_rows)
    filtered_current = 0
    finalized: dict[int, Mapping[str, Any]] = {}
    for raw_row in raw_rows:
        if type(raw_row) is dict:
            raw_epoch = raw_row.get("time")
            if raw_epoch is None:
                raw_epoch = raw_row.get("ts")
        elif isinstance(raw_row, Mapping):
            raw_epoch = raw_row.get("time")
            if raw_epoch is None:
                raw_epoch = raw_row.get("ts")
        else:
            _append_reason(reasons, "bar_row_invalid")
            continue
        minute_epoch = (
            raw_epoch
            if type(raw_epoch) is int and 0 < raw_epoch <= 2**53
            else _parse_epoch(raw_epoch)
        )
        if minute_epoch is None:
            _append_reason(reasons, "bar_time_invalid")
            continue
        if minute_epoch % M1_SECONDS:
            _append_reason(reasons, "bar_time_not_m1_aligned")
            continue
        if minute_epoch == current_minute_epoch:
            filtered_current += 1
            continue
        if (
            minute_epoch > current_minute_epoch
            or minute_epoch + M1_SECONDS > as_of_epoch
        ):
            _append_reason(reasons, "unfinalized_bar_present")
            continue
        if minute_epoch in finalized:
            _append_reason(reasons, "duplicate_finalized_bar_minute")
            continue
        finalized[minute_epoch] = raw_row

    observed_epochs = tuple(sorted(finalized))
    latest_finalized = observed_epochs[-1] if observed_epochs else None
    selected_epochs = observed_epochs[-REQUIRED_COMPLETED_M1_BARS:]
    if common_closed_minute_epoch not in finalized:
        _append_reason(reasons, "common_closed_minute_missing")
    if len(observed_epochs) < REQUIRED_COMPLETED_M1_BARS:
        _append_reason(reasons, "insufficient_finalized_history")

    signal_row = finalized.get(common_closed_minute_epoch)
    if signal_row is not None:
        signal_bar_receipt = _finite_float(signal_row.get("received_at_epoch"))
        if signal_bar_receipt is None or signal_bar_receipt <= 0.0:
            _append_reason(reasons, "signal_bar_receipt_invalid")
        else:
            if signal_bar_receipt < common_closed_minute_epoch + M1_SECONDS:
                _append_reason(
                    reasons,
                    "signal_bar_received_before_close",
                )
            if signal_bar_receipt > as_of_epoch:
                _append_reason(
                    reasons,
                    "signal_bar_received_after_evaluation_time",
                )

    selected: list[MTVCLCBidM1Bar] = []
    if not reasons:
        for minute_epoch in selected_epochs:
            bar, row_reasons = _bar_from_row(
                row=finalized[minute_epoch],
                symbol=symbol,
                minute_epoch=minute_epoch,
                source=source,
            )
            for reason in row_reasons:
                _append_reason(reasons, reason)
            if bar is not None:
                selected.append(bar)
    if reasons:
        selected = []
    result = (
        runtime_prepared_bars(selected),
        tuple(reasons),
        raw_count,
        filtered_current,
        len(finalized),
        latest_finalized,
    )
    if cache_key is not None and not {
        "signal_bar_received_after_evaluation_time",
        "unfinalized_bar_present",
    }.intersection(reasons):
        _PREPARED_BAR_CACHE[cache_key] = (as_of_epoch, result)
    return result


def _quote_rows(raw_rows: Any) -> tuple[Mapping[str, Any], ...] | None:
    if isinstance(raw_rows, Mapping):
        return (raw_rows,)
    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        return None
    if not all(isinstance(row, Mapping) for row in raw_rows):
        return None
    return tuple(raw_rows)


_PreparedQuote = tuple[float, MTVCLCAuthenticatedQuote]


def _prepare_quote_row(
    *,
    symbol: str,
    row: Mapping[str, Any],
    as_of_epoch: float,
    source: _SourceProjection,
) -> tuple[_PreparedQuote | None, tuple[str, ...]]:
    reasons: list[str] = []
    provider = str(row.get("provider") or "").strip().lower()
    if provider != "mt4_bridge":
        _append_reason(reasons, "quote_provider_not_mt4_bridge")
    instrument = row.get("instrument")
    instrument_row = instrument if isinstance(instrument, Mapping) else {}
    canonical_symbol = (
        str(
            instrument_row.get("canonical_symbol")
            or row.get("canonical_symbol")
            or row.get("pair")
            or row.get("symbol")
            or ""
        )
        .strip()
        .upper()
    )
    if canonical_symbol != symbol:
        _append_reason(reasons, "quote_symbol_mismatch")
    venue = str(
        instrument_row.get("venue") or row.get("venue") or ""
    ).strip().lower()
    if venue != IG_MT4_VENUE_ID:
        _append_reason(reasons, "quote_venue_mismatch")
    if _quality_flags(row):
        _append_reason(reasons, "quote_quality_flags_present")
    source_error = market_source_row_error(row, expected=source.authenticated)
    if source_error:
        _append_reason(reasons, f"quote_{source_error}")
    if row.get("transport_fresh") is not True:
        _append_reason(reasons, "quote_transport_not_fresh")
    if row.get("source_event_baseline_initialized") is not True:
        _append_reason(reasons, "quote_source_event_baseline_missing")

    bid = _finite_float(row.get("bid"))
    ask = _finite_float(row.get("ask"))
    if bid is None or ask is None or bid <= 0.0 or ask < bid:
        _append_reason(reasons, "quote_prices_invalid")
    received = _finite_float(row.get("received_at_epoch"))
    if received is None or received <= 0.0:
        _append_reason(reasons, "quote_transport_time_invalid")
    elif received > as_of_epoch:
        _append_reason(reasons, "quote_transport_time_future")
    source_token = str(row.get("source_event_token") or "").strip()
    if (
        not source_token
        or not source_token.isascii()
        or not source_token.isdecimal()
        or int(source_token) <= 0
    ):
        _append_reason(reasons, "quote_source_event_token_invalid")
    market_sequence = _strict_int(row.get("market_event_sequence"))
    if market_sequence is None or market_sequence < 0:
        _append_reason(reasons, "quote_market_event_sequence_invalid")
    market_received_raw = row.get("market_event_received_at_epoch")
    if market_received_raw is not None:
        market_received = _finite_float(market_received_raw)
        if (
            market_received is None
            or market_received <= 0.0
            or (received is not None and market_received > received)
        ):
            _append_reason(reasons, "quote_market_event_receipt_invalid")
    if market_sequence is not None and (
        (market_sequence == 0) != (market_received_raw is None)
    ):
        _append_reason(reasons, "quote_market_event_receipt_sequence_mismatch")
    if reasons:
        return None, tuple(reasons)

    assert bid is not None
    assert ask is not None
    assert received is not None
    assert market_sequence is not None
    return (
        (
            received,
            MTVCLCAuthenticatedQuote(
                symbol=symbol,
                venue_id=IG_MT4_VENUE_ID,
                source_id=source.authenticated.source_id,
                source_version=MARKET_SOURCE_SCHEMA,
                source_identity=source.strategy,
                observed_epoch=int(math.floor(received)),
                bid=bid,
                ask=ask,
                source_event_token_sha256=hashlib.sha256(
                    source_token.encode("utf-8")
                ).hexdigest(),
                market_event_sequence=market_sequence,
            ),
        ),
        (),
    )


def _seal_prepared_quotes(
    prepared: Sequence[_PreparedQuote],
    *,
    symbol: str,
    source: _SourceProjection,
) -> tuple[MTVCLCAuthenticatedQuote, ...]:
    quotes = tuple(item[1] for item in prepared)
    if all(quote.observed_epoch > 0 for quote in quotes):
        return _runtime_prepared_quotes(
            quotes,
            symbol=symbol,
            source_identity=source.strategy,
        )
    return quotes


def _prepare_quotes(
    *,
    symbol: str,
    raw_rows: Any,
    as_of_epoch: float,
    source: _SourceProjection,
) -> tuple[
    tuple[MTVCLCAuthenticatedQuote, ...],
    tuple[str, ...],
    int,
    tuple[float, ...],
]:
    if type(raw_rows) is dict:
        prepared, reasons = _prepare_quote_row(
            symbol=symbol,
            row=raw_rows,
            as_of_epoch=as_of_epoch,
            source=source,
        )
        if reasons or prepared is None:
            return (), reasons, 1, ()
        return (
            _seal_prepared_quotes((prepared,), symbol=symbol, source=source),
            (),
            1,
            (prepared[0],),
        )

    rows = _quote_rows(raw_rows)
    if rows is None:
        return (), ("quote_rows_invalid",), 0, ()
    reasons: list[str] = []
    prepared: list[_PreparedQuote] = []
    for row in rows:
        item, row_reasons = _prepare_quote_row(
            symbol=symbol,
            row=row,
            as_of_epoch=as_of_epoch,
            source=source,
        )
        for reason in row_reasons:
            _append_reason(reasons, reason)
        if not reasons and item is not None:
            prepared.append(item)

    prepared.sort(key=lambda item: item[0])
    receipts = tuple(item[0] for item in prepared)
    observed_epochs = tuple(item[1].observed_epoch for item in prepared)
    if any(current <= previous for previous, current in zip(receipts, receipts[1:])):
        _append_reason(reasons, "quotes_not_strictly_transport_ordered")
    if len(set(observed_epochs)) != len(observed_epochs):
        _append_reason(reasons, "quote_observation_epoch_duplicate")
    if reasons:
        return (), tuple(reasons), len(rows), receipts
    return (
        _seal_prepared_quotes(prepared, symbol=symbol, source=source),
        (),
        len(rows),
        receipts,
    )


def _prepare_symbol(
    *,
    symbol: str,
    raw_bars: Any,
    raw_quotes: Any,
    cost: Any,
    as_of_epoch: float,
    current_minute_epoch: int,
    common_closed_minute_epoch: int,
    source: _SourceProjection,
) -> _PreparedSymbol:
    (
        bars,
        bar_reasons,
        raw_bar_count,
        filtered_current,
        finalized_count,
        latest_finalized,
    ) = _prepare_bars(
        symbol=symbol,
        raw_rows=raw_bars,
        as_of_epoch=as_of_epoch,
        current_minute_epoch=current_minute_epoch,
        common_closed_minute_epoch=common_closed_minute_epoch,
        source=source,
    )
    quotes, quote_reasons, raw_quote_count, receipt_epochs = _prepare_quotes(
        symbol=symbol,
        raw_rows=raw_quotes,
        as_of_epoch=as_of_epoch,
        source=source,
    )
    reasons = list(bar_reasons)
    for reason in quote_reasons:
        _append_reason(reasons, reason)
    typed_cost = cost if isinstance(cost, MTVCLCCostCalibration) else None
    if typed_cost is None:
        _append_reason(reasons, "cost_calibration_not_typed")
    elif typed_cost.symbol != symbol:
        _append_reason(reasons, "cost_calibration_symbol_mismatch")

    cost_id = str(typed_cost.calibration_id or "") if typed_cost is not None else ""
    cost_source = str(typed_cost.source_sha256 or "") if typed_cost is not None else ""
    try:
        cost_row_sha = typed_cost.row_sha256() if typed_cost is not None else ""
    except (TypeError, ValueError, OverflowError):
        cost_row_sha = ""
    structural_ready = not reasons
    request = (
        MTVCLCEvaluationRequest(
            symbol=symbol,
            bars=bars,
            quotes=quotes,
            cost=typed_cost,
        )
        if structural_ready and typed_cost is not None
        else None
    )
    return _PreparedSymbol(
        request=request,
        diagnostic_base=(
            symbol,
            structural_ready,
            tuple(reasons),
            raw_bar_count,
            filtered_current,
            finalized_count,
            len(bars),
            latest_finalized,
            raw_quote_count,
            len(quotes),
            receipt_epochs,
            cost_id,
            cost_source,
            cost_row_sha,
        ),
    )


def _proposal_rank_key(
    proposal: MTVCLCTradeCandidate,
) -> tuple[float, float, float, str, str]:
    p_star = _finite_float(proposal.p_star)
    volume = _finite_float(proposal.signal_tick_volume)
    v90 = _finite_float(proposal.volume_v90)
    activity_ratio = (
        volume / v90
        if volume is not None and v90 is not None and v90 > 0.0
        else -math.inf
    )
    spread = _finite_float(proposal.live_spread_bps)
    return (
        p_star if p_star is not None else math.inf,
        -activity_ratio,
        spread if spread is not None else math.inf,
        str(proposal.symbol or "").strip().upper(),
        str(proposal.side or "").strip().upper(),
    )


def _proposal_diagnostic_values(
    proposal: MTVCLCTradeCandidate,
) -> tuple[Any, ...]:
    """Retain causal strategy measurements even when the candidate abstains."""

    volume_v90 = _finite_float(proposal.volume_v90)
    signal_volume = _strict_int(proposal.signal_tick_volume)
    activity_ratio = (
        float(signal_volume) / volume_v90
        if signal_volume is not None
        and signal_volume >= 0
        and volume_v90 is not None
        and volume_v90 > 0.0
        else None
    )
    return (
        proposal.side,
        proposal.signal_epoch,
        proposal.expected_entry_epoch,
        volume_v90,
        signal_volume,
        activity_ratio,
        _finite_float(proposal.bid_body_bps),
        _finite_float(proposal.bid_close_location),
        _finite_float(proposal.live_spread_bps),
        _finite_float(proposal.p90_spread_bps),
        _finite_float(proposal.recorded_cost_bps),
        _finite_float(proposal.p_star),
        _finite_float(proposal.target_bps),
        _finite_float(proposal.stop_bps),
    )


def _symbol_diagnostic(
    item: _PreparedSymbol,
    proposal: MTVCLCTradeCandidate | None = None,
) -> MTVCLCSymbolProposalDiagnostic:
    if proposal is None:
        return MTVCLCSymbolProposalDiagnostic(*item.diagnostic_base)
    return MTVCLCSymbolProposalDiagnostic(
        *item.diagnostic_base,
        proposal.allowed,
        proposal.reasons,
        *_proposal_diagnostic_values(proposal),
    )


def _global_refusal(
    *,
    reasons: tuple[str, ...],
    strategy_profile: str,
    as_of_epoch: float | None,
    current_minute_epoch: int | None,
    common_closed_minute_epoch: int | None,
    observed_bar_symbols: tuple[str, ...],
    observed_quote_symbols: tuple[str, ...],
    observed_cost_symbols: tuple[str, ...],
    source: _SourceProjection | None = None,
) -> MTVCLCProposalBatchResult:
    return MTVCLCProposalBatchResult(
        proposals=(),
        diagnostics=MTVCLCProposalBatchDiagnostics(
            accepted=False,
            reasons=reasons,
            strategy_profile=strategy_profile,
            as_of_epoch=as_of_epoch,
            current_minute_epoch=current_minute_epoch,
            common_closed_minute_epoch=common_closed_minute_epoch,
            expected_symbols=MTVCLC_V1_SYMBOLS,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
            market_source_id=(source.authenticated.source_id if source else ""),
            producer_instance_id=(
                source.authenticated.producer_instance_id if source else ""
            ),
            symbol_diagnostics=(),
        ),
    )


def evaluate_mtvclc_profile_batch(
    *,
    strategy_profile: str,
    raw_bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    raw_quotes_by_symbol: Mapping[
        str,
        Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ],
    costs_by_symbol: Mapping[str, MTVCLCCostCalibration],
    market_source_state: Mapping[str, Any],
    as_of_epoch: float,
    policy: MTVCLCPolicy = FROZEN_MTVCLC_POLICY,
) -> MTVCLCProposalBatchResult:
    """Evaluate one explicit, exact-scope, authority-free MTVCLC profile batch."""

    normalized_profile = str(strategy_profile or "").strip()
    observed_bar_symbols, bar_keys_valid = _normalized_scope_keys(raw_bars_by_symbol)
    observed_quote_symbols, quote_keys_valid = _normalized_scope_keys(
        raw_quotes_by_symbol
    )
    observed_cost_symbols, cost_keys_valid = _normalized_scope_keys(costs_by_symbol)
    empty_scope = ()

    if normalized_profile != MTVCLC_RUNTIME_PROFILE_ID:
        return _global_refusal(
            reasons=("strategy_profile_unsupported",),
            strategy_profile=normalized_profile,
            as_of_epoch=_finite_float(as_of_epoch),
            current_minute_epoch=None,
            common_closed_minute_epoch=None,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
        )
    if tuple(IG_MT4_SCALP_SYMBOLS) != MTVCLC_V1_SYMBOLS:
        return _global_refusal(
            reasons=("strategy_scope_contract_drifted",),
            strategy_profile=normalized_profile,
            as_of_epoch=_finite_float(as_of_epoch),
            current_minute_epoch=None,
            common_closed_minute_epoch=None,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
        )
    scope_reasons: list[str] = []
    if not bar_keys_valid or observed_bar_symbols != MTVCLC_V1_SYMBOLS:
        scope_reasons.append("bar_scope_must_match_exact_ordered_22")
    if not quote_keys_valid or observed_quote_symbols != MTVCLC_V1_SYMBOLS:
        scope_reasons.append("quote_scope_must_match_exact_ordered_22")
    if not cost_keys_valid or observed_cost_symbols != MTVCLC_V1_SYMBOLS:
        scope_reasons.append("cost_scope_must_match_exact_ordered_22")
    if scope_reasons:
        return _global_refusal(
            reasons=tuple(scope_reasons),
            strategy_profile=normalized_profile,
            as_of_epoch=_finite_float(as_of_epoch),
            current_minute_epoch=None,
            common_closed_minute_epoch=None,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
        )

    as_of = _finite_float(as_of_epoch)
    if as_of is None or as_of <= M1_SECONDS:
        return _global_refusal(
            reasons=("as_of_epoch_invalid",),
            strategy_profile=normalized_profile,
            as_of_epoch=as_of,
            current_minute_epoch=None,
            common_closed_minute_epoch=None,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
        )
    current_minute = int(as_of // M1_SECONDS) * M1_SECONDS
    common_closed_minute = current_minute - M1_SECONDS
    if policy != FROZEN_MTVCLC_POLICY or policy.config_sha256() != MTVCLC_CONFIG_SHA256:
        return _global_refusal(
            reasons=("strategy_policy_not_frozen",),
            strategy_profile=normalized_profile,
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
        )
    if not isinstance(market_source_state, Mapping):
        return _global_refusal(
            reasons=("market_source_state_invalid",),
            strategy_profile=normalized_profile,
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
        )
    source, source_reasons = _project_source_identity(
        market_source_state,
        as_of_epoch=as_of,
    )
    if source is None:
        return _global_refusal(
            reasons=source_reasons or ("market_source_unattested",),
            strategy_profile=normalized_profile,
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
        )

    normalized_bars = _scope_mapping(raw_bars_by_symbol)
    normalized_quotes = _scope_mapping(raw_quotes_by_symbol)
    normalized_costs = _scope_mapping(costs_by_symbol)
    prepared = tuple(
        _prepare_symbol(
            symbol=symbol,
            raw_bars=normalized_bars[symbol],
            raw_quotes=normalized_quotes[symbol],
            cost=normalized_costs[symbol],
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            source=source,
        )
        for symbol in MTVCLC_V1_SYMBOLS
    )

    allowed: list[MTVCLCTradeCandidate] = []
    diagnostics: list[MTVCLCSymbolProposalDiagnostic] = []
    for item in prepared:
        if item.request is None:
            diagnostics.append(_symbol_diagnostic(item))
            continue
        proposal = evaluate_mtvclc(item.request, policy)
        diagnostics.append(_symbol_diagnostic(item, proposal))
        if proposal.allowed:
            allowed.append(proposal)

    ranked = tuple(sorted(allowed, key=_proposal_rank_key))
    return MTVCLCProposalBatchResult(
        proposals=ranked,
        diagnostics=MTVCLCProposalBatchDiagnostics(
            accepted=True,
            reasons=empty_scope,
            strategy_profile=normalized_profile,
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            expected_symbols=MTVCLC_V1_SYMBOLS,
            observed_bar_symbols=observed_bar_symbols,
            observed_quote_symbols=observed_quote_symbols,
            observed_cost_symbols=observed_cost_symbols,
            market_source_id=source.authenticated.source_id,
            producer_instance_id=source.authenticated.producer_instance_id,
            symbol_diagnostics=tuple(diagnostics),
        ),
    )


__all__ = [
    "M1_SECONDS",
    "MTVCLC_PROPOSAL_BATCH_SCHEMA_VERSION",
    "MTVCLC_RUNTIME_PROFILE_ID",
    "MTVCLCProposalBatchDiagnostics",
    "MTVCLCProposalBatchResult",
    "MTVCLCSymbolProposalDiagnostic",
    "evaluate_mtvclc_profile_batch",
]
