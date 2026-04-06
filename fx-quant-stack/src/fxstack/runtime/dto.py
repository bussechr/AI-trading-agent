# AGENT: ROLE: Validate and normalize runtime execution commands and broker ACK payloads.
# AGENT: ENTRYPOINT: imported by runtime service and protocol layers.
# AGENT: PRIMARY INPUTS: raw command payloads, raw ACK payloads, settings-derived defaults.
# AGENT: PRIMARY OUTPUTS: `ExecutionCommand`, `ExecutionAck`.
# AGENT: DEPENDS ON: `fxstack/settings.py`.
# AGENT: CALLED BY: `fxstack/runtime/service.py`, `fxstack/runtime/protocol.py`.
# AGENT: STATE / SIDE EFFECTS: pure validation only.
# AGENT: HANDSHAKES: broker command contract, ACK contract, command dedupe IDs.
# AGENT: SEE: `docs/agents/bridge-and-api-handshakes.md` -> `fxstack/runtime/service.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fxstack.settings import get_settings


SUPPORTED_COMMANDS = {"BUY", "SELL", "CLOSE", "CLOSE_ALL", "CLOSE_PARTIAL", "MODIFY_SL", "INFO"}


def _normalize_intent(cmd: str, raw: Any = None) -> str:
    if str(raw or "").strip():
        return str(raw).strip().upper()
    cmd_up = str(cmd or "").strip().upper()
    if cmd_up in {"BUY", "SELL"}:
        return "ENTRY"
    if cmd_up in {"CLOSE", "CLOSE_PARTIAL"}:
        return "EXIT"
    if cmd_up == "MODIFY_SL":
        return "ADJUST"
    if cmd_up == "CLOSE_ALL":
        return "CLOSE_ALL"
    if cmd_up == "INFO":
        return "INFO"
    return "UNKNOWN"


def _require_finite_positive(value: float | None, *, field_name: str) -> None:
    if value is None:
        raise ValueError(f"{field_name} is required")
    if not math.isfinite(float(value)) or float(value) <= 0.0:
        raise ValueError(f"{field_name} must be a finite positive number")


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
    intent: str = "UNKNOWN"
    trace_id: str = ""
    action: str = ""
    action_score: float = 0.0
    reversal_token: str = ""
    status: str = "queued"
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0
    delivered_count: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if not str(self.command_id).strip():
            raise ValueError("command_id is required")
        if not str(self.cmd).strip():
            raise ValueError("cmd is required")
        if str(self.cmd).upper() not in SUPPORTED_COMMANDS:
            raise ValueError(f"unsupported cmd: {self.cmd}")
        if not math.isfinite(float(self.lots)) or float(self.lots) < 0.0:
            raise ValueError("lots must be a finite non-negative number")
        if not math.isfinite(float(self.close_lots)) or float(self.close_lots) < 0.0:
            raise ValueError("close_lots must be a finite non-negative number")

        cmd = str(self.cmd).upper()
        symbol = str(self.symbol).strip().upper()
        if cmd in {"BUY", "SELL", "CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"} and not symbol:
            raise ValueError(f"symbol is required for {cmd}")
        if cmd in {"BUY", "SELL"}:
            _require_finite_positive(float(self.lots), field_name="lots")
        if cmd == "CLOSE_PARTIAL":
            close_lots = float(self.close_lots if self.close_lots > 0.0 else self.lots)
            _require_finite_positive(close_lots, field_name="close_lots")
        if cmd == "MODIFY_SL":
            _require_finite_positive(self.sl_price, field_name="sl_price")
        if self.tp_price is not None and (not math.isfinite(float(self.tp_price)) or float(self.tp_price) <= 0.0):
            raise ValueError("tp_price must be a finite positive number")
        if self.sl_price is not None and (not math.isfinite(float(self.sl_price)) or float(self.sl_price) <= 0.0):
            raise ValueError("sl_price must be a finite positive number")

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, default_session_id: str, ttl_secs: float, now_ts: float | None = None) -> "ExecutionCommand":
        now = float(time.time() if now_ts is None else now_ts)
        command_id = str(payload.get("command_id") or payload.get("signal_id") or uuid.uuid4())
        cmd = str(payload.get("cmd", "")).strip().upper()
        session_id = str(payload.get("session_id") or default_session_id).strip() or default_session_id
        trace_id = str(payload.get("trace_id") or command_id)
        intent = _normalize_intent(cmd, payload.get("intent"))
        created_at = float(payload.get("created_at", now) or now)
        ttl = float(payload.get("ttl_secs", ttl_secs) or ttl_secs)
        out = cls(
            command_id=command_id,
            session_id=session_id,
            proto="v2",
            cmd=cmd,
            symbol=str(payload.get("symbol", "")).strip().upper(),
            lots=float(payload.get("lots", 0.0) or 0.0),
            tp_cash=(None if payload.get("tp_cash") is None else float(payload.get("tp_cash"))),
            tp_price=(None if payload.get("tp_price") is None else float(payload.get("tp_price"))),
            sl_price=(None if payload.get("sl_price") is None else float(payload.get("sl_price"))),
            close_lots=float(payload.get("close_lots", payload.get("lots", 0.0)) or 0.0),
            magic=int(payload.get("magic", 246810) or 246810),
            intent=intent,
            trace_id=trace_id,
            action=str(payload.get("action") or ""),
            action_score=float(payload.get("action_score", 0.0) or 0.0),
            reversal_token=str(payload.get("reversal_token") or ""),
            status="queued",
            created_at=created_at,
            updated_at=now,
            expires_at=created_at + max(1.0, ttl),
            delivered_count=0,
            payload=dict(payload),
        )
        if bool(get_settings().strict_command_validation):
            out.validate()
        return out

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
            "close_lots": float(self.close_lots),
            "magic": int(self.magic),
            "intent": self.intent,
            "trace_id": self.trace_id,
            "action": self.action,
            "action_score": float(self.action_score),
            "reversal_token": self.reversal_token,
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
    count_as_trade: bool = False
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
        elif raw in {"duplicate"}:
            status = "duplicate"
        else:
            status = raw or "failed"
        ticket = int(payload.get("ticket", -1) or -1)
        message = str(payload.get("message") or payload.get("status_reason") or "")
        count_as_trade = bool(status == "acked" and ticket > 0 and "duplicate" not in message.lower())
        out = cls(
            command_id=str(payload.get("command_id") or payload.get("signal_id") or ""),
            status=status,
            symbol=str(payload.get("symbol", "")),
            ticket=ticket,
            error_code=int(payload.get("error_code", 0) or 0),
            message=message,
            trace_id=str(payload.get("trace_id", "")),
            updated_at=now,
            count_as_trade=count_as_trade,
            raw=dict(payload),
        )
        if not str(out.command_id).strip():
            raise ValueError("ack command_id is required")
        return out

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
            "count_as_trade": bool(self.count_as_trade),
            "raw": dict(self.raw),
        }
