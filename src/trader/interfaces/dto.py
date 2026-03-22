from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from fxstack.runtime.dto import ExecutionAck as _CoreExecutionAck
from fxstack.runtime.dto import ExecutionCommand as _CoreExecutionCommand

try:
    from enum import StrEnum
except ImportError:
    class StrEnum(str, Enum):
        """Compatibility shim for Python builds without enum.StrEnum."""
        pass


class CommandStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACKED = "acked"
    FAILED = "failed"
    EXPIRED = "expired"
    DUPLICATE = "duplicate"


class CommandIntent(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ADJUST = "ADJUST"
    CLOSE_ALL = "CLOSE_ALL"
    INFO = "INFO"
    UNKNOWN = "UNKNOWN"

@dataclass(slots=True)
class ExecutionCommand:
    command_id: str
    session_id: str
    proto: str
    cmd: str
    symbol: str = ""
    lots: float = 0.0
    tp_cash: float | None = None
    tp_price: float | None = None
    sl_price: float | None = None
    close_lots: float = 0.0
    magic: int = 246810
    intent: str = CommandIntent.UNKNOWN.value
    trace_id: str = ""
    action: str = ""
    action_score: float = 0.0
    reversal_token: str = ""
    status: str = CommandStatus.QUEUED.value
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    delivered_count: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _from_core(cls, core: _CoreExecutionCommand) -> "ExecutionCommand":
        data = core.to_dict()
        return cls(
            command_id=str(data["command_id"]),
            session_id=str(data["session_id"]),
            proto=str(data["proto"]),
            cmd=str(data["cmd"]),
            symbol=str(data["symbol"]),
            lots=float(data["lots"]),
            tp_cash=data.get("tp_cash"),
            tp_price=data.get("tp_price"),
            sl_price=data.get("sl_price"),
            close_lots=float(data.get("close_lots", 0.0) or 0.0),
            magic=int(data["magic"]),
            intent=str(data["intent"]),
            trace_id=str(data["trace_id"]),
            action=str(data.get("action", "")),
            action_score=float(data.get("action_score", 0.0) or 0.0),
            reversal_token=str(data.get("reversal_token", "")),
            status=str(data["status"]),
            created_at=float(data["created_at"]),
            updated_at=float(data["updated_at"]),
            expires_at=float(data["expires_at"]),
            delivered_count=int(data["delivered_count"]),
            payload=dict(data.get("payload") or {}),
        )

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_session_id: str,
        proto: str = "v2",
        now_ts: float | None = None,
    ) -> "ExecutionCommand":
        core = _CoreExecutionCommand.from_payload(
            dict(payload or {}),
            default_session_id=default_session_id,
            ttl_secs=float(payload.get("ttl_secs", 120.0) or 120.0),
            now_ts=now_ts,
        )
        core.proto = str(proto or "v2").strip().lower()
        return cls._from_core(core)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": str(self.command_id),
            "session_id": str(self.session_id),
            "proto": str(self.proto),
            "cmd": str(self.cmd),
            "symbol": str(self.symbol),
            "lots": float(self.lots),
            "tp_cash": self.tp_cash,
            "tp_price": self.tp_price,
            "sl_price": self.sl_price,
            "close_lots": float(self.close_lots),
            "magic": int(self.magic),
            "intent": str(self.intent),
            "trace_id": str(self.trace_id),
            "action": str(self.action),
            "action_score": float(self.action_score),
            "reversal_token": str(self.reversal_token),
            "status": str(self.status),
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "expires_at": float(self.expires_at),
            "delivered_count": int(self.delivered_count),
            "payload": dict(self.payload or {}),
        }


@dataclass(slots=True)
class ExecutionAck:
    command_id: str
    status: str
    symbol: str = ""
    ticket: int = -1
    error_code: int = 0
    message: str = ""
    trace_id: str = ""
    updated_at: float = 0.0
    count_as_trade: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def _from_core(cls, core: _CoreExecutionAck) -> "ExecutionAck":
        data = core.to_dict()
        return cls(
            command_id=str(data["command_id"]),
            status=str(data["status"]),
            symbol=str(data["symbol"]),
            ticket=int(data["ticket"]),
            error_code=int(data["error_code"]),
            message=str(data["message"]),
            trace_id=str(data["trace_id"]),
            updated_at=float(data["updated_at"]),
            count_as_trade=bool(data.get("count_as_trade", False)),
            raw=dict(data.get("raw") or {}),
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, now_ts: float | None = None) -> "ExecutionAck":
        core = _CoreExecutionAck.from_payload(dict(payload or {}), now_ts=now_ts)
        return cls._from_core(core)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": str(self.command_id),
            "status": str(self.status),
            "symbol": str(self.symbol),
            "ticket": int(self.ticket),
            "error_code": int(self.error_code),
            "message": str(self.message),
            "trace_id": str(self.trace_id),
            "updated_at": float(self.updated_at),
            "count_as_trade": bool(self.count_as_trade),
            "raw": dict(self.raw or {}),
        }


@dataclass(slots=True)
class DecisionContext:
    symbol: str
    now_ts: float
    prices: list[float] = field(default_factory=list)
    features: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class DecisionOutcome:
    symbol: str
    side: str
    score: float
    confidence: float
    execution_ready: bool
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": str(self.symbol),
            "side": str(self.side),
            "score": float(self.score),
            "confidence": float(self.confidence),
            "execution_ready": bool(self.execution_ready),
            "reasons": list(self.reasons or []),
            "metadata": dict(self.metadata or {}),
        }


@dataclass(slots=True)
class RiskEnvelopeState:
    soft_dd_pct: float
    hard_dd_pct: float
    daily_breaker_pct: float
    regime: str
    volatility: float
    updated_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "soft_dd_pct": float(self.soft_dd_pct),
            "hard_dd_pct": float(self.hard_dd_pct),
            "daily_breaker_pct": float(self.daily_breaker_pct),
            "regime": str(self.regime),
            "volatility": float(self.volatility),
            "updated_at": float(self.updated_at),
        }


@dataclass(slots=True)
class RuntimeSnapshot:
    time: float
    system_status: str
    equity: float
    margin: float
    freemargin: float
    leverage: float
    positions: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "time": float(self.time),
            "system_status": str(self.system_status),
            "equity": float(self.equity),
            "margin": float(self.margin),
            "freemargin": float(self.freemargin),
            "leverage": float(self.leverage),
            "positions": list(self.positions or []),
            "decisions": list(self.decisions or []),
            "diagnostics": dict(self.diagnostics or {}),
        }
