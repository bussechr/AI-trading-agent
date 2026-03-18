from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


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
    intent: str = "UNKNOWN"
    trace_id: str = ""
    status: str = "queued"
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    delivered_count: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, default_session_id: str, ttl_secs: float, now_ts: float | None = None) -> "ExecutionCommand":
        now = float(time.time() if now_ts is None else now_ts)
        command_id = str(payload.get("command_id") or payload.get("signal_id") or uuid.uuid4())
        cmd = str(payload.get("cmd", "")).strip().upper()
        session_id = str(payload.get("session_id") or default_session_id).strip() or default_session_id
        trace_id = str(payload.get("trace_id") or command_id)
        intent = str(payload.get("intent") or ("ENTRY" if cmd in {"BUY", "SELL"} else "UNKNOWN"))
        created_at = float(payload.get("created_at", now) or now)
        ttl = float(payload.get("ttl_secs", ttl_secs) or ttl_secs)
        return cls(
            command_id=command_id,
            session_id=session_id,
            proto="v2",
            cmd=cmd,
            symbol=str(payload.get("symbol", "")),
            lots=float(payload.get("lots", 0.0) or 0.0),
            tp_cash=(None if payload.get("tp_cash") is None else float(payload.get("tp_cash"))),
            tp_price=(None if payload.get("tp_price") is None else float(payload.get("tp_price"))),
            sl_price=(None if payload.get("sl_price") is None else float(payload.get("sl_price"))),
            magic=int(payload.get("magic", 246810) or 246810),
            intent=intent,
            trace_id=trace_id,
            status="queued",
            created_at=created_at,
            updated_at=now,
            expires_at=created_at + max(1.0, ttl),
            delivered_count=0,
            payload=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "session_id": self.session_id,
            "proto": self.proto,
            "cmd": self.cmd,
            "symbol": self.symbol,
            "lots": float(self.lots),
            "tp_cash": self.tp_cash,
            "tp_price": self.tp_price,
            "sl_price": self.sl_price,
            "magic": int(self.magic),
            "intent": self.intent,
            "trace_id": self.trace_id,
            "status": self.status,
            "created_at": float(self.created_at),
            "updated_at": float(self.updated_at),
            "expires_at": float(self.expires_at),
            "delivered_count": int(self.delivered_count),
            "payload": dict(self.payload),
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
    def from_payload(cls, payload: dict[str, Any], now_ts: float | None = None) -> "ExecutionAck":
        now = float(time.time() if now_ts is None else now_ts)
        raw = str(payload.get("status", "")).strip().lower()
        if raw in {"ok", "success", "done", "executed", "acked"}:
            status = "acked"
        elif raw in {"failed", "error", "rejected"}:
            status = "failed"
        elif raw in {"delivered", "queued", "retry"}:
            status = "delivered"
        else:
            status = raw or "failed"
        return cls(
            command_id=str(payload.get("command_id") or payload.get("signal_id") or ""),
            status=status,
            symbol=str(payload.get("symbol", "")),
            ticket=int(payload.get("ticket", -1) or -1),
            error_code=int(payload.get("error_code", 0) or 0),
            message=str(payload.get("message") or payload.get("status_reason") or ""),
            trace_id=str(payload.get("trace_id", "")),
            updated_at=now,
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "status": self.status,
            "symbol": self.symbol,
            "ticket": int(self.ticket),
            "error_code": int(self.error_code),
            "message": self.message,
            "trace_id": self.trace_id,
            "updated_at": float(self.updated_at),
            "raw": dict(self.raw),
        }
