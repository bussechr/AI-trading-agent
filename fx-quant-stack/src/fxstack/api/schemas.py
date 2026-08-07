"""Pydantic schemas for v2 bridge endpoints.

Schemas are intentionally permissive (``extra='allow'``) so that the underlying
:class:`fxstack.runtime.service.RuntimeService` continues to receive any
forward-compatible fields it expects. The schemas exist to:

1. Bound the size and type of well-known fields (command id, symbol, lots, etc.)
   so a malformed or oversized payload returns 422 instead of crashing the
   service or sending a bad order.
2. Provide a clear documented surface for future tightening (extra fields will
   keep working today; future migrations can flip ``extra='forbid'`` once the
   ecosystem is clean).

Each handler converts the validated model back to a dict via
``model_dump(exclude_none=True)`` before delegating to ``service.*`` so the
service contract is unchanged.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


MT4_IVOLUME_SOURCE = "mt4_ivolume_tick_count_v1"
MT4_BID_PRICE_BASIS = "mt4_bid_ohlc_v1"
BRIDGE_MARKET_EVENT_VOLUME_SOURCE = "bridge_market_event_count_v1"
BRIDGE_TICK_MID_PRICE_BASIS = "bridge_tick_mid_ohlc_v1"
LEGACY_UNSPECIFIED_VOLUME_SOURCE = "legacy_unspecified_volume_v1"
LEGACY_SYNTHETIC_MID_PRICE_BASIS = "legacy_synthetic_mid_ohlc_v1"
MIXED_BAR_VOLUME_SOURCE = "mixed_bar_volume_sources_v1"
MIXED_BAR_PRICE_BASIS = "mixed_bar_price_bases_v1"


class CommandRequest(BaseModel):
    """Body of ``POST /v2/commands``.

    The command verb is required here; command-specific requirements (symbol,
    lots, stop price, and so on) remain the runtime service's responsibility.
    """

    model_config = ConfigDict(extra="allow")

    cmd: str = Field(min_length=1, max_length=32)
    command_id: str | None = Field(default=None, max_length=128)
    id: str | None = Field(default=None, max_length=128)
    signal_id: str | None = Field(default=None, max_length=128)
    symbol: str | None = Field(default=None, max_length=32)
    action: str | None = Field(default=None, max_length=32)
    side: str | None = Field(default=None, max_length=8)
    lots: float | None = Field(default=None, ge=0.0, le=1000.0, allow_inf_nan=False)
    close_lots: float | None = Field(default=None, ge=0.0, le=1000.0, allow_inf_nan=False)
    tp_cash: float | None = Field(default=None, allow_inf_nan=False)
    tp_price: float | None = Field(default=None, allow_inf_nan=False)
    sl_price: float | None = Field(default=None, allow_inf_nan=False)
    magic: int | None = Field(default=None, ge=0)
    target_ticket: int | None = Field(default=None, ge=1)
    owner_token: str | None = Field(
        default=None,
        min_length=1,
        max_length=31,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    intent: str | None = Field(default=None, max_length=64)
    action_score: float | None = Field(default=None, allow_inf_nan=False)
    reversal_token: str | None = Field(default=None, max_length=256)
    idempotency_key: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    trace_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=256)
    thread_id: str | None = Field(default=None, max_length=256)
    schema_version: str | None = Field(default=None, max_length=64)
    ttl_secs: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    created_at: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    execution_type: str | None = Field(default=None, max_length=16)
    pending_orders_forbidden: bool | None = None
    entry_deadline_epoch: int | None = Field(
        default=None,
        ge=1,
        le=2_147_483_647,
    )


class CommandAckRequest(BaseModel):
    """Body of ``POST /v2/commands/ack``."""

    model_config = ConfigDict(extra="allow")

    command_id: str | None = Field(default=None, max_length=128)
    id: str | None = Field(default=None, max_length=128)
    signal_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    ticket: int | str | None = Field(default=None)
    magic: int | None = Field(default=None)
    owner_token: str | None = Field(default=None, max_length=31)
    status: str = Field(min_length=1, max_length=64)
    mutation_state: str | None = Field(default=None, max_length=32)
    actuals_schema: str | None = Field(default=None, max_length=64)
    message: str | None = Field(default=None, max_length=1024)
    status_reason: str | None = Field(default=None, max_length=1024)
    error: str | None = Field(default=None, max_length=1024)
    error_code: int | None = Field(default=None)
    symbol: str | None = Field(default=None, max_length=32)
    broker_symbol: str | None = Field(default=None, max_length=32)
    cmd: str | None = Field(default=None, max_length=32)
    side: str | None = Field(default=None, max_length=8)
    execution_type: str | None = Field(default=None, max_length=32)
    target_ticket: int | str | None = Field(default=None)
    order_comment: str | None = Field(default=None, max_length=31)
    actual_command_id: str | None = Field(default=None, max_length=128)
    actual_symbol: str | None = Field(default=None, max_length=32)
    actual_broker_symbol: str | None = Field(default=None, max_length=32)
    actual_cmd: str | None = Field(default=None, max_length=32)
    actual_side: str | None = Field(default=None, max_length=8)
    actual_execution_type: str | None = Field(default=None, max_length=32)
    actual_ticket: int | str | None = Field(default=None)
    actual_target_ticket: int | str | None = Field(default=None)
    actual_magic: int | None = Field(default=None)
    actual_owner_token: str | None = Field(default=None, max_length=31)
    actual_order_comment: str | None = Field(default=None, max_length=31)
    actual_lots: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    actual_open_price: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
    )
    actual_sl_price: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    actual_tp_price: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    actual_remaining_lots: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
    )
    actual_close_time: float | None = Field(
        default=None,
        ge=0.0,
        allow_inf_nan=False,
    )
    broker_mutation_attempted: bool | None = Field(default=None)
    broker_mutation_confirmed: bool | None = Field(default=None)
    broker_outcome_known: bool | None = Field(default=None)
    execution_uncertain: bool | None = Field(default=None)
    trace_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=256)
    thread_id: str | None = Field(default=None, max_length=256)
    schema_version: str | None = Field(default=None, max_length=64)
    consumer_identity: str | None = Field(default=None, max_length=128)
    producer_instance_id: str | None = Field(default=None, max_length=128)
    terminal_lease_scope: str | None = Field(default=None, max_length=128)
    credential_generation_id: str | None = Field(default=None, max_length=128)
    bridge_protocol_version: str | None = Field(default=None, max_length=32)


class MarketTickRequest(BaseModel):
    """Body of ``POST /v2/market/tick``."""

    model_config = ConfigDict(extra="allow")

    symbol: str = Field(min_length=1, max_length=32)
    bid: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    ask: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    mid: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    spread: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    spread_points: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    spread_pips: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    spread_bps: float | None = Field(default=None, ge=0.0, le=100000.0, allow_inf_nan=False)
    digits: int | None = Field(default=None, ge=0, le=12)
    source_event_token: str | int | None = Field(default=None)
    broker_account_scope: str | None = Field(default=None, max_length=128)
    broker_account_scope_schema: str | None = Field(default=None, max_length=96)
    broker_account_scope_version: int | None = Field(default=None, ge=0)
    broker_server: str | None = Field(default=None, max_length=128)
    broker_company: str | None = Field(default=None, max_length=160)
    consumer_identity: str | None = Field(default=None, max_length=128)
    producer_instance_id: str | None = Field(default=None, max_length=128)
    terminal_lease_scope: str | None = Field(default=None, max_length=128)
    credential_generation_id: str | None = Field(default=None, max_length=128)
    bridge_protocol_version: str | None = Field(default=None, max_length=32)
    time: Any | None = Field(default=None)
    ts: Any | None = Field(default=None)
    timestamp: Any | None = Field(default=None)

    @model_validator(mode="after")
    def validate_quote(self) -> "MarketTickRequest":
        if not self.symbol.strip():
            raise ValueError("tick symbol must not be blank")
        has_two_sided_quote = self.bid is not None and self.ask is not None
        if not has_two_sided_quote and self.mid is None:
            raise ValueError("tick requires positive bid and ask, or a positive mid")
        if has_two_sided_quote and float(self.ask) < float(self.bid):
            raise ValueError("tick ask must be greater than or equal to bid")
        return self


class MarketTickBatchItemRequest(BaseModel):
    """One quote in an authenticated ``POST /v2/market/ticks`` envelope."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    broker_symbol: str | None = Field(default=None, max_length=32)
    bid: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    ask: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    mid: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    spread: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    spread_points: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    spread_pips: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    spread_bps: float | None = Field(
        default=None,
        ge=0.0,
        le=100000.0,
        allow_inf_nan=False,
    )
    digits: int | None = Field(default=None, ge=0, le=12)
    source_event_token: str | int | None = Field(default=None)
    time: Any | None = Field(default=None)
    ts: Any | None = Field(default=None)
    timestamp: Any | None = Field(default=None)

    @model_validator(mode="after")
    def validate_quote(self) -> "MarketTickBatchItemRequest":
        if not self.symbol.strip():
            raise ValueError("tick symbol must not be blank")
        has_two_sided_quote = self.bid is not None and self.ask is not None
        if not has_two_sided_quote and self.mid is None:
            raise ValueError("tick requires positive bid and ask, or a positive mid")
        if has_two_sided_quote and float(self.ask) < float(self.bid):
            raise ValueError("tick ask must be greater than or equal to bid")
        return self


class MarketTickBatchRequest(BaseModel):
    """Bounded quotes sharing one authenticated MT4 producer identity."""

    model_config = ConfigDict(extra="forbid")

    ticks: list[MarketTickBatchItemRequest] = Field(min_length=1, max_length=64)
    broker_account_scope: str | None = Field(default=None, max_length=128)
    broker_account_scope_schema: str | None = Field(default=None, max_length=96)
    broker_account_scope_version: int | None = Field(default=None, ge=0)
    broker_server: str | None = Field(default=None, max_length=128)
    broker_company: str | None = Field(default=None, max_length=160)
    consumer_identity: str | None = Field(default=None, max_length=128)
    producer_instance_id: str | None = Field(default=None, max_length=128)
    terminal_lease_scope: str | None = Field(default=None, max_length=128)
    credential_generation_id: str | None = Field(default=None, max_length=128)
    bridge_protocol_version: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_unique_symbols(self) -> "MarketTickBatchRequest":
        normalized = [item.symbol.strip().upper() for item in self.ticks]
        if len(normalized) != len(set(normalized)):
            raise ValueError("tick batch symbols must be unique after normalization")
        return self


class MarketBarRequest(BaseModel):
    """One completed broker bar in a bounded startup-history batch."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    time: Any
    open: float = Field(gt=0.0, allow_inf_nan=False)
    high: float = Field(gt=0.0, allow_inf_nan=False)
    low: float = Field(gt=0.0, allow_inf_nan=False)
    close: float = Field(gt=0.0, allow_inf_nan=False)
    spread: float = Field(default=0.0, ge=0.0, allow_inf_nan=False)
    volume: int | None = Field(
        default=None,
        strict=True,
        ge=0,
        le=2_147_483_647,
    )
    bid_open: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    bid_high: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    bid_low: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    bid_close: float | None = Field(default=None, gt=0.0, allow_inf_nan=False)
    volume_source: str | None = Field(default=None, min_length=1, max_length=64)
    price_basis: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_ohlc(self) -> "MarketBarRequest":
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high must be at least open, low, and close")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low must be at most open, high, and close")

        bid_values = (self.bid_open, self.bid_high, self.bid_low, self.bid_close)
        has_any_bid = any(value is not None for value in bid_values)
        has_all_bid = all(value is not None for value in bid_values)
        has_any_provenance = (
            self.volume_source is not None or self.price_basis is not None
        )
        if has_any_bid and not has_all_bid:
            raise ValueError("bid OHLC fields must be supplied together")
        if has_all_bid:
            bid_open = float(self.bid_open)
            bid_high = float(self.bid_high)
            bid_low = float(self.bid_low)
            bid_close = float(self.bid_close)
            if bid_high < max(bid_open, bid_close, bid_low):
                raise ValueError(
                    "bar bid_high must be at least bid_open, bid_low, and bid_close"
                )
            if bid_low > min(bid_open, bid_close, bid_high):
                raise ValueError(
                    "bar bid_low must be at most bid_open, bid_high, and bid_close"
                )

        # Explicit MT4 provenance is an atomic contract: exact bid OHLC,
        # integer iVolume, and both fixed provenance identifiers. Legacy rows
        # may omit the whole extension, but partial or invented provenance must
        # never masquerade as direct terminal data.
        if has_any_bid or has_any_provenance:
            if not has_all_bid:
                raise ValueError("direct MT4 bars require complete bid OHLC")
            if self.volume is None:
                raise ValueError("direct MT4 bars require integer volume")
            if self.volume_source != MT4_IVOLUME_SOURCE:
                raise ValueError(
                    f"direct MT4 volume_source must be {MT4_IVOLUME_SOURCE}"
                )
            if self.price_basis != MT4_BID_PRICE_BASIS:
                raise ValueError(
                    f"direct MT4 price_basis must be {MT4_BID_PRICE_BASIS}"
                )
        return self


class MarketBarBatchRequest(BaseModel):
    """Completed broker bars used to backfill bridge history after restart."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=8)
    bars: list[MarketBarRequest] = Field(min_length=1, max_length=500)
    broker_account_scope: str | None = Field(default=None, max_length=128)
    broker_account_scope_schema: str | None = Field(default=None, max_length=96)
    broker_account_scope_version: int | None = Field(default=None, ge=0)
    broker_server: str | None = Field(default=None, max_length=128)
    broker_company: str | None = Field(default=None, max_length=160)
    consumer_identity: str | None = Field(default=None, max_length=128)
    producer_instance_id: str | None = Field(default=None, max_length=128)
    terminal_lease_scope: str | None = Field(default=None, max_length=128)
    credential_generation_id: str | None = Field(default=None, max_length=128)
    bridge_protocol_version: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_scope(self) -> "MarketBarBatchRequest":
        if not self.symbol.strip():
            raise ValueError("bar batch symbol must not be blank")
        if self.timeframe.strip().upper() not in {"M1", "M5", "M15", "H1", "H4", "D"}:
            raise ValueError("unsupported bar batch timeframe")
        return self


class MarketBarBatchItemRequest(BaseModel):
    """One symbol/timeframe group in an atomic multi-bar request."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    timeframe: str = Field(min_length=1, max_length=8)
    bars: list[MarketBarRequest] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_scope(self) -> "MarketBarBatchItemRequest":
        if not self.symbol.strip():
            raise ValueError("bar batch symbol must not be blank")
        if self.timeframe.strip().upper() not in {"M1", "M5", "M15", "H1", "H4", "D"}:
            raise ValueError("unsupported bar batch timeframe")
        return self


class MarketBarMultiBatchRequest(BaseModel):
    """Bounded atomic bar frame sharing one authenticated MT4 source."""

    model_config = ConfigDict(extra="forbid")

    batches: list[MarketBarBatchItemRequest] = Field(min_length=1, max_length=64)
    broker_account_scope: str | None = Field(default=None, max_length=128)
    broker_account_scope_schema: str | None = Field(default=None, max_length=96)
    broker_account_scope_version: int | None = Field(default=None, ge=0)
    broker_server: str | None = Field(default=None, max_length=128)
    broker_company: str | None = Field(default=None, max_length=160)
    consumer_identity: str | None = Field(default=None, max_length=128)
    producer_instance_id: str | None = Field(default=None, max_length=128)
    terminal_lease_scope: str | None = Field(default=None, max_length=128)
    credential_generation_id: str | None = Field(default=None, max_length=128)
    bridge_protocol_version: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_total_rows(self) -> "MarketBarMultiBatchRequest":
        if sum(len(batch.bars) for batch in self.batches) > 500:
            raise ValueError("bar multi-batch must contain at most 500 total rows")
        normalized = [
            (batch.symbol.strip().upper(), batch.timeframe.strip().upper())
            for batch in self.batches
        ]
        if len(normalized) != len(set(normalized)):
            raise ValueError(
                "bar multi-batch symbol/timeframe groups must be unique after normalization"
            )
        return self


class StateDecisionsRequest(BaseModel):
    """Body of ``POST /v2/state/decisions``."""

    model_config = ConfigDict(extra="allow")

    decisions: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    vol: float = Field(default=0.0)


class SymbolSpecReport(BaseModel):
    """One broker ``MarketInfo`` row in a ``symbol_specs`` report."""

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    broker_symbol: str | None = Field(default=None, max_length=32)
    trade_allowed: bool | None = Field(default=None)


class ReportRequest(BaseModel):
    """Body of ``POST /v2/reports`` when ``Content-Type: application/json``.

    The reports endpoint is dual-mode: MT4 may submit plain-text reports
    (the legacy fast path) or structured JSON. When JSON is submitted, the
    payload is validated against this schema; on validation failure the
    endpoint returns 422 via the standard error envelope. The text path
    bypasses this schema entirely.
    """

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    report_type: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=32)
    ticket: int | str | None = Field(default=None)
    side: str | None = Field(default=None, max_length=8)
    lots: float | None = Field(default=None, ge=0.0, le=1000.0, allow_inf_nan=False)
    profit: float | None = Field(default=None, allow_inf_nan=False)
    swap: float | None = Field(default=None, allow_inf_nan=False)
    commission: float | None = Field(default=None, allow_inf_nan=False)
    net_profit: float | None = Field(default=None, allow_inf_nan=False)
    equity: float | None = Field(default=None, allow_inf_nan=False)
    margin: float | None = Field(default=None, allow_inf_nan=False)
    freemargin: float | None = Field(default=None, allow_inf_nan=False)
    leverage: float | None = Field(default=None, allow_inf_nan=False)
    open_time: str | float | int | None = Field(default=None)
    close_time: str | float | int | None = Field(default=None)
    open_price: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    close_price: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    consumer_identity: str | None = Field(default=None, max_length=128)
    producer_instance_id: str | None = Field(default=None, max_length=128)
    terminal_lease_scope: str | None = Field(default=None, max_length=128)
    credential_generation_id: str | None = Field(default=None, max_length=128)
    bridge_protocol_version: str | None = Field(default=None, max_length=32)
    specs: dict[str, SymbolSpecReport] | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def reject_non_finite_numbers(cls, value: Any) -> Any:
        """Reject NaN/Infinity anywhere in permissive structured reports."""

        def _walk(node: Any, path: str) -> None:
            if isinstance(node, float) and not math.isfinite(node):
                raise ValueError(f"report payload contains a non-finite number at {path}")
            if isinstance(node, dict):
                for key, item in node.items():
                    _walk(item, f"{path}.{key}")
            elif isinstance(node, (list, tuple)):
                for index, item in enumerate(node):
                    _walk(item, f"{path}[{index}]")

        _walk(value, "$")
        return value


class PositionView(BaseModel):
    """Single open position as known to the bridge."""

    model_config = ConfigDict(extra="allow")

    symbol: str = Field(min_length=1, max_length=32)
    side: str | None = Field(default=None, max_length=8)
    lots: float | None = Field(default=None, ge=0.0)
    ticket: int | str | None = Field(default=None)
    source: str = Field(default="db", max_length=16)


class PositionReconcileResponse(BaseModel):
    """Body of ``GET /v2/positions/reconcile``.

    Returns the bridge / DB view of open positions alongside the most-recent
    snapshot reported by the EA (if any), plus a precomputed diff so the
    runtime startup hook does not need to recompute it.

    The diff is computed against ``symbol`` (case-insensitive). Lot-level
    diffs are reported as a separate ``lot_mismatches`` list. This endpoint
    is *informational only* — it does not mutate state. Runtime is expected
    to log/alert on a non-empty diff rather than auto-correct.
    """

    model_config = ConfigDict(extra="allow")

    db_positions: list[PositionView] = Field(default_factory=list)
    ea_positions: list[PositionView] = Field(default_factory=list)
    only_in_db: list[str] = Field(default_factory=list)
    only_in_ea: list[str] = Field(default_factory=list)
    lot_mismatches: list[dict[str, Any]] = Field(default_factory=list)
    ea_snapshot_age_secs: float | None = Field(default=None)
    ea_snapshot_available: bool = Field(default=False)
    ea_market_source: dict[str, Any] = Field(default_factory=dict)
    ea_market_source_id: str = Field(default="", max_length=64)
    current_market_source: dict[str, Any] = Field(default_factory=dict)
    market_source_matches: bool = Field(default=False)
