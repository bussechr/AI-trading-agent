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


class CommandAckRequest(BaseModel):
    """Body of ``POST /v2/commands/ack``."""

    model_config = ConfigDict(extra="allow")

    command_id: str | None = Field(default=None, max_length=128)
    id: str | None = Field(default=None, max_length=128)
    signal_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=128)
    ticket: int | str | None = Field(default=None)
    status: str = Field(min_length=1, max_length=64)
    message: str | None = Field(default=None, max_length=1024)
    status_reason: str | None = Field(default=None, max_length=1024)
    error: str | None = Field(default=None, max_length=1024)
    error_code: int | None = Field(default=None)
    symbol: str | None = Field(default=None, max_length=32)
    trace_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=256)
    thread_id: str | None = Field(default=None, max_length=256)
    schema_version: str | None = Field(default=None, max_length=64)


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


class StateDecisionsRequest(BaseModel):
    """Body of ``POST /v2/state/decisions``."""

    model_config = ConfigDict(extra="allow")

    decisions: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    vol: float = Field(default=0.0)


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
