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

import json
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


def _coerce_json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except Exception:
            return {}
        if isinstance(decoded, dict):
            return dict(decoded)
    return {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _fallback_command_id(
    *,
    session_id: str,
    cmd: str,
    symbol: str,
    lots: float,
    close_lots: float,
    tp_cash: float | None,
    tp_price: float | None,
    sl_price: float | None,
    magic: int,
    intent: str,
    action: str,
    reversal_token: str,
    idempotency_key: str,
) -> str:
    if str(idempotency_key or "").strip():
        material = {
            "session_id": str(session_id).strip(),
            "idempotency_key": str(idempotency_key).strip(),
        }
    else:
        material = {
            "session_id": str(session_id).strip(),
            "cmd": str(cmd or "").strip().upper(),
            "symbol": str(symbol or "").strip().upper(),
            "lots": float(lots),
            "close_lots": float(close_lots),
            "tp_cash": tp_cash,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "magic": int(magic),
            "intent": str(intent or "").strip().upper(),
            "action": str(action or "").strip(),
            "reversal_token": str(reversal_token or "").strip(),
        }
    return str(uuid.uuid5(uuid.NAMESPACE_URL, _canonical_json(material)))


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
    correlation_id: str = ""
    thread_id: str = ""
    idempotency_key: str = ""
    schema_version: str = ""
    orchestration_meta_json: dict[str, Any] = field(default_factory=dict)
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
        if self.tp_cash is not None and not math.isfinite(float(self.tp_cash)):
            raise ValueError("tp_cash must be finite")
        if not math.isfinite(float(self.action_score)):
            raise ValueError("action_score must be finite")
        timestamps = (float(self.created_at), float(self.updated_at), float(self.expires_at))
        if any(not math.isfinite(value) for value in timestamps):
            raise ValueError("command timestamps must be finite")
        # Detached connector intents may use the dataclass's all-zero sentinel.
        # Persisted/runtime commands must carry a complete, ordered timestamp set.
        if any(value != 0.0 for value in timestamps):
            if any(value <= 0.0 for value in timestamps):
                raise ValueError("command timestamps must all be positive when present")
            if float(self.expires_at) <= float(self.created_at):
                raise ValueError("expires_at must be later than created_at")

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
        cmd = str(payload.get("cmd", "")).strip().upper()
        session_id = str(payload.get("session_id") or default_session_id).strip() or default_session_id
        symbol = str(payload.get("symbol", "")).strip().upper()
        lots = float(payload.get("lots", 0.0) or 0.0)
        close_lots = float(payload.get("close_lots", payload.get("lots", 0.0)) or 0.0)
        tp_cash = None if payload.get("tp_cash") is None else float(payload.get("tp_cash"))
        tp_price = None if payload.get("tp_price") is None else float(payload.get("tp_price"))
        sl_price = None if payload.get("sl_price") is None else float(payload.get("sl_price"))
        magic = int(payload.get("magic", 246810) or 246810)
        intent = _normalize_intent(cmd, payload.get("intent"))
        action = str(payload.get("action") or "")
        reversal_token = str(payload.get("reversal_token") or "")
        command_id = str(
            payload.get("command_id")
            or payload.get("id")
            or payload.get("signal_id")
            or _fallback_command_id(
                session_id=session_id,
                cmd=cmd,
                symbol=symbol,
                lots=lots,
                close_lots=close_lots,
                tp_cash=tp_cash,
                tp_price=tp_price,
                sl_price=sl_price,
                magic=magic,
                intent=intent,
                action=action,
                reversal_token=reversal_token,
                idempotency_key=str(payload.get("idempotency_key") or ""),
            )
        )
        trace_id = str(payload.get("trace_id") or command_id)
        server_ttl = float(ttl_secs)
        if not math.isfinite(server_ttl) or server_ttl <= 0.0:
            raise ValueError("server command ttl_secs must be a finite positive number")
        requested_created_at = float(payload.get("created_at", now) or now)
        if not math.isfinite(requested_created_at) or requested_created_at <= 0.0:
            raise ValueError("created_at must be a finite positive timestamp")
        if requested_created_at > now + 5.0:
            raise ValueError("created_at cannot be in the future")
        requested_ttl = float(payload.get("ttl_secs", server_ttl) or server_ttl)
        if not math.isfinite(requested_ttl) or requested_ttl <= 0.0:
            raise ValueError("ttl_secs must be a finite positive number")
        if requested_ttl > server_ttl:
            raise ValueError(f"ttl_secs cannot exceed the server limit of {server_ttl:g}")
        # Queue age and expiry are server-authoritative. A client timestamp is
        # accepted only as bounded metadata and cannot extend command lifetime.
        created_at = now
        ttl = requested_ttl
        out = cls(
            command_id=command_id,
            session_id=session_id,
            proto="v2",
            cmd=cmd,
            symbol=symbol,
            lots=lots,
            tp_cash=tp_cash,
            tp_price=tp_price,
            sl_price=sl_price,
            close_lots=close_lots,
            magic=magic,
            intent=intent,
            trace_id=trace_id,
            correlation_id=str(payload.get("correlation_id") or ""),
            thread_id=str(payload.get("thread_id") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            schema_version=str(payload.get("schema_version") or ""),
            orchestration_meta_json=_coerce_json_mapping(payload.get("orchestration_meta_json")),
            action=action,
            action_score=float(payload.get("action_score", 0.0) or 0.0),
            reversal_token=reversal_token,
            status="queued",
            created_at=created_at,
            updated_at=now,
            expires_at=now + ttl,
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
            "correlation_id": self.correlation_id,
            "thread_id": self.thread_id,
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
            "orchestration_meta_json": dict(self.orchestration_meta_json or {}),
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
    correlation_id: str = ""
    thread_id: str = ""
    idempotency_key: str = ""
    schema_version: str = ""
    orchestration_meta_json: dict[str, Any] = field(default_factory=dict)
    updated_at: float = 0.0
    count_as_trade: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], now_ts: float | None = None) -> "ExecutionAck":
        now = float(time.time() if now_ts is None else now_ts)
        raw = str(payload.get("status", "")).strip().lower()
        if not raw:
            raise ValueError("ack status is required")
        if raw in {"ok", "success", "done", "executed", "filled", "acked"}:
            status = "acked"
        elif raw in {"failed", "error", "rejected"}:
            status = "failed"
        elif raw in {"delivered", "queued", "retry"}:
            status = "delivered"
        elif raw in {"duplicate"}:
            status = "duplicate"
        else:
            raise ValueError(f"unsupported ack status: {raw}")
        ticket = int(payload.get("ticket", -1) or -1)
        message = str(payload.get("message") or payload.get("status_reason") or payload.get("error") or "")
        count_as_trade = bool(status == "acked" and ticket > 0 and "duplicate" not in message.lower())
        out = cls(
            command_id=str(payload.get("command_id") or payload.get("id") or payload.get("signal_id") or ""),
            status=status,
            symbol=str(payload.get("symbol", "")),
            ticket=ticket,
            error_code=int(payload.get("error_code", 0) or 0),
            message=message,
            trace_id=str(payload.get("trace_id", "")),
            correlation_id=str(payload.get("correlation_id") or ""),
            thread_id=str(payload.get("thread_id") or ""),
            idempotency_key=str(payload.get("idempotency_key") or ""),
            schema_version=str(payload.get("schema_version") or ""),
            orchestration_meta_json=_coerce_json_mapping(payload.get("orchestration_meta_json")),
            updated_at=now,
            count_as_trade=count_as_trade,
            raw=dict(payload),
        )
        if not str(out.command_id).strip() and not str(out.idempotency_key).strip():
            raise ValueError("ack command_id or idempotency_key is required")
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
            "correlation_id": self.correlation_id,
            "thread_id": self.thread_id,
            "idempotency_key": self.idempotency_key,
            "schema_version": self.schema_version,
            "orchestration_meta_json": dict(self.orchestration_meta_json or {}),
            "updated_at": float(self.updated_at),
            "count_as_trade": bool(self.count_as_trade),
            "raw": dict(self.raw),
        }
