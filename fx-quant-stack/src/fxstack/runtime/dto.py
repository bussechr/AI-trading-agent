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
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_COMMANDS = {"BUY", "SELL", "CLOSE", "CLOSE_ALL", "CLOSE_PARTIAL", "MODIFY_SL", "INFO"}
ENTRY_COMMANDS = {"BUY", "SELL"}
TICKET_MANAGEMENT_COMMANDS = {"CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"}
OWNER_TOKEN_MAX_LENGTH = 31
TICKET_OWNER_CONTRACT = "ticket_owner_v1"
LEGACY_ELBRIDGE_CONTRACT = "legacy_elbridge_v1"
PRODUCTION_SCALPER_LANE = "production_scalper"
_OWNER_TOKEN_RE = re.compile(
    rf"^[A-Za-z0-9._:-]{{1,{OWNER_TOKEN_MAX_LENGTH}}}$"
)


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


def _strict_ack_integer(value: Any, *, field_name: str, default: int = -1) -> int:
    if value is None or value == "":
        return int(default)
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if not math.isfinite(numeric) or not numeric.is_integer():
        raise ValueError(f"{field_name} must be an integer")
    return int(numeric)


def _optional_ack_float(value: Any, *, field_name: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(numeric):
        raise ValueError(f"{field_name} must be a finite number")
    return numeric


def _ack_reasons(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple)) else (value,)
    return tuple(
        dict.fromkeys(str(item or "").strip() for item in values if str(item or "").strip())
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _derived_entry_owner_token(*, command_id: str, session_id: str, magic: int) -> str:
    """Return a stable MT4-comment-safe owner identity for one entry command."""

    material = _canonical_json(
        {
            "command_id": str(command_id).strip(),
            "session_id": str(session_id).strip(),
            "magic": int(magic),
        }
    )
    # MT4 order comments are bounded to 31 characters. A per-command token
    # avoids the old fixed-comment ownership ambiguity while remaining stable
    # across queue persistence and delivery retries.
    # Leave room for the broker/server to append a suffix to OrderComment.
    # Ownership therefore uses prefix matching plus ticket/Magic/symbol joins.
    return f"fxs-{uuid.uuid5(uuid.NAMESPACE_URL, material).hex[:18]}"


def _require_owner_token(value: Any) -> str:
    owner_token = str(value or "").strip()
    if not _OWNER_TOKEN_RE.fullmatch(owner_token):
        raise ValueError(
            "owner_token must be 1-31 characters using only letters, digits, '.', '_', ':', or '-'"
        )
    return owner_token


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
    target_ticket: int,
    owner_token: str,
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
            "target_ticket": int(target_ticket),
            "owner_token": str(owner_token or "").strip(),
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
    target_ticket: int = -1
    owner_token: str = ""
    ownership_contract: str = ""
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
        if cmd in ENTRY_COMMANDS:
            _require_finite_positive(float(self.lots), field_name="lots")
            if int(self.magic) <= 0:
                raise ValueError("magic must be a positive integer for exposure-increasing commands")
            # Direct connector DTOs are provider-neutral and may predate the
            # MT4 additive fields. MT4 serialization always derives and checks
            # them; payload-based runtime commands already carry them here.
            if str(self.owner_token or "").strip() or str(
                self.ownership_contract or ""
            ).strip():
                _require_owner_token(self.owner_token)
                if str(self.ownership_contract or "") != TICKET_OWNER_CONTRACT:
                    raise ValueError(
                        f"ownership_contract must be {TICKET_OWNER_CONTRACT} for exposure-increasing commands"
                    )
            if bool(dict(self.payload or {}).get("entry_protection_required", False)):
                _require_finite_positive(self.sl_price, field_name="sl_price")
                _require_finite_positive(self.tp_price, field_name="tp_price")
            payload = dict(self.payload or {})
            claims_production_scalper = bool(
                str(payload.get("strategy_lane") or "").strip().lower()
                == PRODUCTION_SCALPER_LANE
                or str(payload.get("intent") or self.intent or "").strip().lower()
                == "production_scalper_entry"
            )
            if claims_production_scalper:
                if str(payload.get("execution_type") or "").strip().lower() != "market":
                    raise ValueError(
                        "production scalp entries must use execution_type=market"
                    )
                if payload.get("pending_orders_forbidden") is not True:
                    raise ValueError(
                        "production scalp entries must forbid pending orders"
                    )
                raw_deadline = payload.get("entry_deadline_epoch")
                if isinstance(raw_deadline, bool):
                    raise ValueError("entry_deadline_epoch must be an integer")
                try:
                    deadline = float(raw_deadline)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(
                        "entry_deadline_epoch must be an integer"
                    ) from exc
                if (
                    not math.isfinite(deadline)
                    or deadline <= 0.0
                    or not deadline.is_integer()
                    or deadline > 2_147_483_647
                ):
                    raise ValueError("entry_deadline_epoch must be an integer")
                if timestamps[0] > 0.0 and float(self.expires_at) > deadline:
                    raise ValueError(
                        "expires_at cannot exceed entry_deadline_epoch"
                    )
        if cmd in TICKET_MANAGEMENT_COMMANDS:
            payload = dict(self.payload or {})
            if int(self.magic) <= 0:
                raise ValueError(f"magic must be a positive integer for {cmd}")
            production_scalper = (
                str(payload.get("strategy_lane") or "").strip().lower()
                == PRODUCTION_SCALPER_LANE
            )
            strict_requested = bool(
                int(self.target_ticket) >= 0
                or str(self.owner_token or "").strip()
                or "target_ticket" in payload
                or "owner_token" in payload
                or production_scalper
            )
            if strict_requested:
                if int(self.target_ticket) <= 0:
                    raise ValueError(f"target_ticket must be a positive broker ticket for {cmd}")
                _require_owner_token(self.owner_token)
                if str(self.ownership_contract or "") != TICKET_OWNER_CONTRACT:
                    raise ValueError(
                        f"ownership_contract must be {TICKET_OWNER_CONTRACT} for {cmd}"
                    )
            elif str(self.ownership_contract or "") and str(
                self.ownership_contract or ""
            ) != LEGACY_ELBRIDGE_CONTRACT:
                raise ValueError(
                    f"legacy {cmd} must use the isolated {LEGACY_ELBRIDGE_CONTRACT} contract"
                )
        if cmd == "CLOSE_ALL" and int(self.magic) <= 0:
            raise ValueError("magic must be a positive integer for CLOSE_ALL")
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
        magic = int(payload.get("magic")) if payload.get("magic") is not None else 246810
        target_ticket = (
            int(payload.get("target_ticket"))
            if payload.get("target_ticket") is not None
            else -1
        )
        owner_token = str(payload.get("owner_token") or "").strip()
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
                target_ticket=target_ticket,
                owner_token=owner_token,
            )
        )
        if cmd in ENTRY_COMMANDS and not owner_token:
            owner_token = _derived_entry_owner_token(
                command_id=command_id,
                session_id=session_id,
                magic=magic,
            )
        production_scalper = (
            str(payload.get("strategy_lane") or "").strip().lower()
            == PRODUCTION_SCALPER_LANE
            or str(payload.get("intent") or "").strip().lower()
            == "production_scalper_entry"
        )
        strict_ownership = bool(
            cmd in ENTRY_COMMANDS
            or (
                cmd in TICKET_MANAGEMENT_COMMANDS
                and (
                    "target_ticket" in payload
                    or "owner_token" in payload
                    or target_ticket >= 0
                    or owner_token
                    or production_scalper
                )
            )
        )
        ownership_contract = (
            TICKET_OWNER_CONTRACT
            if strict_ownership
            else (
                LEGACY_ELBRIDGE_CONTRACT
                if cmd in TICKET_MANAGEMENT_COMMANDS or cmd == "CLOSE_ALL"
                else ""
            )
        )
        normalized_payload = dict(payload)
        normalized_payload["magic"] = int(magic)
        if owner_token:
            normalized_payload["owner_token"] = owner_token
        if target_ticket > 0:
            normalized_payload["target_ticket"] = int(target_ticket)
        if ownership_contract:
            normalized_payload["ownership_contract"] = ownership_contract
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
        if production_scalper and cmd in ENTRY_COMMANDS:
            if str(payload.get("execution_type") or "").strip().lower() != "market":
                raise ValueError(
                    "production scalp entries must use execution_type=market"
                )
            if payload.get("pending_orders_forbidden") is not True:
                raise ValueError("production scalp entries must forbid pending orders")
            raw_deadline = payload.get("entry_deadline_epoch")
            if isinstance(raw_deadline, bool):
                raise ValueError("entry_deadline_epoch must be an integer")
            try:
                deadline_number = float(raw_deadline)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("entry_deadline_epoch must be an integer") from exc
            if (
                not math.isfinite(deadline_number)
                or deadline_number <= 0.0
                or not deadline_number.is_integer()
                or deadline_number > 2_147_483_647
            ):
                raise ValueError("entry_deadline_epoch must be an integer")
            deadline = int(deadline_number)
            remaining = float(deadline) - now
            if remaining <= 0.0:
                raise ValueError("entry_deadline_epoch has expired")
            # The server receipt clock owns command lifetime. Neither a
            # caller TTL nor a caller timestamp can extend an instant entry
            # beyond its strategy deadline.
            ttl = min(ttl, remaining)
            normalized_payload["execution_type"] = "market"
            normalized_payload["pending_orders_forbidden"] = True
            normalized_payload["entry_deadline_epoch"] = deadline
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
            target_ticket=target_ticket,
            owner_token=owner_token,
            ownership_contract=ownership_contract,
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
            payload=normalized_payload,
        )
        # Queue ingress is an authority boundary. Validation is unconditional;
        # an operator flag must never turn malformed broker commands into a
        # live execution path.
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
            "target_ticket": int(self.target_ticket),
            "owner_token": self.owner_token,
            "ownership_contract": self.ownership_contract,
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


def command_ownership_fields(command: Any) -> tuple[str, int, str]:
    """Resolve and validate the MT4 ownership fields for wire serialization.

    This also supports the legacy compatibility DTO, whose durable ``payload``
    contains additive fields even though its dataclass predates them.
    """

    payload = dict(getattr(command, "payload", {}) or {})
    cmd = str(getattr(command, "cmd", "") or "").strip().upper()
    magic = int(getattr(command, "magic", 0) or 0)
    raw_target_ticket = getattr(command, "target_ticket", -1)
    target_ticket = int(raw_target_ticket) if raw_target_ticket is not None else -1
    if target_ticket < 0 and payload.get("target_ticket") is not None:
        target_ticket = int(payload.get("target_ticket"))
    owner_token = str(getattr(command, "owner_token", "") or "").strip()
    if not owner_token:
        owner_token = str(payload.get("owner_token") or "").strip()
    production_scalper = (
        str(payload.get("strategy_lane") or "").strip().lower()
        == PRODUCTION_SCALPER_LANE
    )

    if cmd in ENTRY_COMMANDS:
        if magic <= 0:
            raise ValueError(
                "magic must be a positive integer for exposure-increasing commands"
            )
        if not owner_token:
            owner_token = _derived_entry_owner_token(
                command_id=str(getattr(command, "command_id", "") or ""),
                session_id=str(getattr(command, "session_id", "") or ""),
                magic=magic,
            )
        return TICKET_OWNER_CONTRACT, -1, _require_owner_token(owner_token)

    if cmd in TICKET_MANAGEMENT_COMMANDS:
        strict_requested = bool(
            target_ticket >= 0
            or owner_token
            or "target_ticket" in payload
            or "owner_token" in payload
            or production_scalper
        )
        if strict_requested:
            if target_ticket <= 0:
                raise ValueError(f"target_ticket must be a positive broker ticket for {cmd}")
            if magic <= 0:
                raise ValueError(f"magic must be a positive integer for {cmd}")
            return (
                TICKET_OWNER_CONTRACT,
                target_ticket,
                _require_owner_token(owner_token),
            )
        if magic <= 0:
            raise ValueError(f"magic must be a positive integer for legacy {cmd}")
        return LEGACY_ELBRIDGE_CONTRACT, -1, ""

    if cmd == "CLOSE_ALL":
        if magic <= 0:
            raise ValueError("magic must be a positive integer for legacy CLOSE_ALL")
        return LEGACY_ELBRIDGE_CONTRACT, -1, ""
    return "", -1, ""


@dataclass(slots=True)
class ExecutionAck:
    command_id: str
    status: str
    symbol: str = ""
    ticket: int = -1
    magic: int = -1
    owner_token: str = ""
    broker_symbol: str = ""
    cmd: str = ""
    side: str = ""
    execution_type: str = ""
    mutation_state: str = ""
    target_ticket: int = -1
    actual_lots: float | None = None
    actual_open_price: float | None = None
    actual_sl_price: float | None = None
    actual_tp_price: float | None = None
    order_comment: str = ""
    attestation_reasons: tuple[str, ...] = ()
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
        raw_status = str(payload.get("status", "")).strip().lower()
        if not raw_status:
            raise ValueError("ack status is required")
        if raw_status in {"ok", "success", "done", "executed", "filled", "acked"}:
            status = "acked"
        elif raw_status in {"failed", "error", "rejected"}:
            status = "failed"
        elif raw_status in {"delivered", "queued", "retry"}:
            status = "delivered"
        elif raw_status in {"duplicate"}:
            status = "duplicate"
        elif raw_status in {
            "reconcile_required",
            "reconciliation_required",
            "execution_uncertain",
        }:
            status = "reconcile_required"
        else:
            raise ValueError(f"unsupported ack status: {raw_status}")
        ticket = _strict_ack_integer(
            payload.get("ticket", payload.get("actual_ticket")),
            field_name="ticket",
        )
        magic = _strict_ack_integer(
            payload.get("magic"),
            field_name="magic",
        )
        target_ticket = _strict_ack_integer(
            payload.get("target_ticket", payload.get("actual_target_ticket")),
            field_name="target_ticket",
        )
        mutation_state = str(payload.get("mutation_state") or "").strip().lower()
        if mutation_state and mutation_state not in {
            "not_attempted",
            "attempted",
            "confirmed",
            "unknown",
        }:
            raise ValueError("unsupported mutation_state")
        message = str(payload.get("message") or payload.get("status_reason") or payload.get("error") or "")
        count_as_trade = bool(status == "acked" and ticket > 0)
        out = cls(
            command_id=str(payload.get("command_id") or payload.get("id") or payload.get("signal_id") or ""),
            status=status,
            symbol=str(payload.get("symbol", "")),
            ticket=ticket,
            magic=magic,
            owner_token=str(payload.get("owner_token") or "").strip(),
            broker_symbol=str(
                payload.get("broker_symbol")
                or payload.get("actual_broker_symbol")
                or ""
            ).strip(),
            cmd=str(payload.get("cmd") or payload.get("actual_cmd") or "")
            .strip()
            .upper(),
            side=str(payload.get("side") or payload.get("actual_side") or "")
            .strip()
            .upper(),
            execution_type=str(
                payload.get("execution_type")
                or payload.get("actual_execution_type")
                or ""
            )
            .strip()
            .lower(),
            mutation_state=mutation_state,
            target_ticket=target_ticket,
            actual_lots=_optional_ack_float(
                payload.get("actual_lots", payload.get("lots")),
                field_name="actual_lots",
            ),
            actual_open_price=_optional_ack_float(
                payload.get("actual_open_price", payload.get("open_price")),
                field_name="actual_open_price",
            ),
            actual_sl_price=_optional_ack_float(
                payload.get("actual_sl_price", payload.get("sl_price")),
                field_name="actual_sl_price",
            ),
            actual_tp_price=_optional_ack_float(
                payload.get("actual_tp_price", payload.get("tp_price")),
                field_name="actual_tp_price",
            ),
            order_comment=str(
                payload.get("order_comment")
                or payload.get("actual_order_comment")
                or ""
            ),
            attestation_reasons=_ack_reasons(
                payload.get("attestation_reasons")
                or payload.get("attestation_reason")
            ),
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
            "magic": int(self.magic),
            "owner_token": self.owner_token,
            "broker_symbol": self.broker_symbol,
            "cmd": self.cmd,
            "side": self.side,
            "execution_type": self.execution_type,
            "mutation_state": self.mutation_state,
            "target_ticket": int(self.target_ticket),
            "actual_lots": self.actual_lots,
            "actual_open_price": self.actual_open_price,
            "actual_sl_price": self.actual_sl_price,
            "actual_tp_price": self.actual_tp_price,
            "order_comment": self.order_comment,
            "attestation_reasons": list(self.attestation_reasons),
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
