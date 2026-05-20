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

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CommandRequest(BaseModel):
    """Body of ``POST /v2/commands``.

    All fields optional at the schema layer; the runtime service may impose its
    own required-field rules and is responsible for the final reject decision.
    """

    model_config = ConfigDict(extra="allow")

    command_id: str | None = Field(default=None, max_length=128)
    id: str | None = Field(default=None, max_length=128)
    symbol: str | None = Field(default=None, max_length=32)
    action: str | None = Field(default=None, max_length=32)
    side: str | None = Field(default=None, max_length=8)
    lots: float | None = Field(default=None, ge=0.0, le=1000.0)
    tp_cash: float | None = Field(default=None)
    tp_price: float | None = Field(default=None)
    sl_price: float | None = Field(default=None)
    idempotency_key: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)


class CommandAckRequest(BaseModel):
    """Body of ``POST /v2/commands/ack``."""

    model_config = ConfigDict(extra="allow")

    command_id: str | None = Field(default=None, max_length=128)
    id: str | None = Field(default=None, max_length=128)
    ticket: int | str | None = Field(default=None)
    status: str | None = Field(default=None, max_length=64)
    error: str | None = Field(default=None, max_length=1024)


class MarketTickRequest(BaseModel):
    """Body of ``POST /v2/market/tick``."""

    model_config = ConfigDict(extra="allow")

    symbol: str | None = Field(default=None, max_length=32)
    bid: float | None = Field(default=None, ge=0.0)
    ask: float | None = Field(default=None, ge=0.0)
    mid: float | None = Field(default=None, ge=0.0)
    spread: float | None = Field(default=None, ge=0.0)
    spread_points: float | None = Field(default=None, ge=0.0)
    spread_pips: float | None = Field(default=None, ge=0.0)
    spread_bps: float | None = Field(default=None, ge=0.0, le=100000.0)
    digits: int | None = Field(default=None, ge=0, le=12)
    time: Any | None = Field(default=None)
    ts: Any | None = Field(default=None)
    timestamp: Any | None = Field(default=None)


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

    model_config = ConfigDict(extra="allow")

    report_type: str | None = Field(default=None, max_length=64)
    symbol: str | None = Field(default=None, max_length=32)
    ticket: int | str | None = Field(default=None)
    side: str | None = Field(default=None, max_length=8)
    lots: float | None = Field(default=None, ge=0.0, le=1000.0)
    profit: float | None = Field(default=None)
    open_time: str | float | int | None = Field(default=None)
    close_time: str | float | int | None = Field(default=None)
    open_price: float | None = Field(default=None, ge=0.0)
    close_price: float | None = Field(default=None, ge=0.0)


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
