from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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


class CommandIntent(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    CLOSE_ALL = "CLOSE_ALL"
    INFO = "INFO"
    UNKNOWN = "UNKNOWN"


def _norm_intent(cmd: str, raw: str | None = None) -> str:
    if raw:
        return str(raw).strip().upper()
    cmd_up = str(cmd or "").strip().upper()
    if cmd_up in {"BUY", "SELL"}:
        return CommandIntent.ENTRY.value
    if cmd_up == "CLOSE":
        return CommandIntent.EXIT.value
    if cmd_up == "CLOSE_ALL":
        return CommandIntent.CLOSE_ALL.value
    if cmd_up == "INFO":
        return CommandIntent.INFO.value
    return CommandIntent.UNKNOWN.value


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
    magic: int = 246810
    intent: str = CommandIntent.UNKNOWN.value
    trace_id: str = ""
    status: str = CommandStatus.QUEUED.value
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    delivered_count: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        default_session_id: str,
        proto: str = "v2",
        now_ts: float | None = None,
    ) -> "ExecutionCommand":
        now = float(time.time() if now_ts is None else now_ts)
        cmd = str(payload.get("cmd", "")).strip().upper()
        command_id = str(
            payload.get("command_id")
            or payload.get("signal_id")
            or uuid.uuid4()
        ).strip()
        session_id = str(payload.get("session_id") or default_session_id).strip() or default_session_id
        trace_id = str(payload.get("trace_id") or command_id).strip() or command_id
        created_raw = payload.get("created_at", now)
        try:
            created_at = float(created_raw)
        except Exception:
            created_at = now

        ttl_secs = float(payload.get("ttl_secs", 120.0) or 120.0)
        expires_at = float(created_at + max(ttl_secs, 1.0))

        return cls(
            command_id=command_id,
            session_id=session_id,
            proto=str(proto or "v2").strip().lower(),
            cmd=cmd,
            symbol=str(payload.get("symbol", "")).strip(),
            lots=float(payload.get("lots", 0.0) or 0.0),
            tp_cash=(None if payload.get("tp_cash") is None else float(payload.get("tp_cash"))),
            tp_price=(None if payload.get("tp_price") is None else float(payload.get("tp_price"))),
            sl_price=(None if payload.get("sl_price") is None else float(payload.get("sl_price"))),
            magic=int(payload.get("magic", 246810) or 246810),
            intent=_norm_intent(cmd, payload.get("intent")),
            trace_id=trace_id,
            status=CommandStatus.QUEUED.value,
            created_at=created_at,
            updated_at=now,
            expires_at=expires_at,
            delivered_count=0,
            payload=dict(payload or {}),
        )

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
            "magic": int(self.magic),
            "intent": str(self.intent),
            "trace_id": str(self.trace_id),
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
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, now_ts: float | None = None) -> "ExecutionAck":
        now = float(time.time() if now_ts is None else now_ts)
        raw_status = str(payload.get("status", "")).strip().lower()
        if raw_status in {"ok", "success", "done", "executed", "acked"}:
            status = CommandStatus.ACKED.value
        elif raw_status in {"failed", "error", "rejected"}:
            status = CommandStatus.FAILED.value
        elif raw_status in {"delivered", "queued", "retry"}:
            status = CommandStatus.DELIVERED.value
        else:
            status = raw_status or CommandStatus.FAILED.value

        return cls(
            command_id=str(payload.get("command_id") or payload.get("signal_id") or "").strip(),
            status=str(status),
            symbol=str(payload.get("symbol", "")).strip(),
            ticket=int(payload.get("ticket", -1) or -1),
            error_code=int(payload.get("error_code", 0) or 0),
            message=str(payload.get("message") or payload.get("status_reason") or ""),
            trace_id=str(payload.get("trace_id", "")).strip(),
            updated_at=now,
            raw=dict(payload or {}),
        )

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
