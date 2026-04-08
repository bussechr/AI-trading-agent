from __future__ import annotations

import json
import zlib
from datetime import datetime, timezone
from typing import Any

from fxstack.runtime.dto import ExecutionCommand


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    return out[:max_len]


def command_to_wire_line(command: ExecutionCommand) -> str:
    payload = dict(command.payload or {})
    cmd = str(command.cmd).upper().strip()
    parts: list[str] = [f"provider=paper", f"cmd={cmd}"]
    if command.symbol:
        parts.append(f"symbol={command.symbol}")
    lots_value = float(command.lots)
    if cmd == "CLOSE_PARTIAL":
        lots_value = float(command.close_lots if command.close_lots > 0.0 else command.lots)
        parts.append(f"close_lots={float(lots_value)}")
    parts.append(f"lots={float(lots_value)}")
    if command.tp_cash is not None:
        parts.append(f"tp_cash={float(command.tp_cash)}")
    if command.tp_price is not None:
        parts.append(f"tp_price={float(command.tp_price)}")
    if command.sl_price is not None:
        parts.append(f"sl={float(command.sl_price)}")
    if command.action:
        parts.append(f"action={safe_text(command.action, max_len=64)}")
    if float(command.action_score) != 0.0:
        parts.append(f"action_score={float(command.action_score):.6f}")
    if command.reversal_token:
        parts.append(f"reversal_token={safe_text(command.reversal_token, max_len=96)}")
    parts.extend(
        [
            f"proto={safe_text(command.proto or 'v2', max_len=16)}",
            f"paper_simulated=1",
            f"command_id={command.command_id}",
            f"session_id={command.session_id}",
            f"intent={command.intent}",
            f"trace_id={command.trace_id or command.command_id}",
            f"t_bridge_queued={float(command.created_at):.6f}",
        ]
    )
    if command.correlation_id:
        parts.append(f"correlation_id={safe_text(command.correlation_id, max_len=160)}")
    if command.thread_id:
        parts.append(f"thread_id={safe_text(command.thread_id, max_len=192)}")
    if command.idempotency_key:
        parts.append(f"idempotency_key={safe_text(command.idempotency_key, max_len=160)}")
    if command.schema_version:
        parts.append(f"schema_version={safe_text(command.schema_version, max_len=96)}")
    if command.orchestration_meta_json:
        payload_json = json.dumps(dict(command.orchestration_meta_json or {}), separators=(",", ":"), sort_keys=True)
        parts.append(f"orchestration_meta_json={safe_text(payload_json)}")
    thought = payload.get("thought")
    if thought:
        parts.append(f"thought={safe_text(thought)}")
    return ";".join(parts)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _ticket_for_command(command_id: str) -> int:
    return max(1, int(zlib.crc32(str(command_id or "").encode("utf-8")) & 0x7FFFFFFF))


def _fill_price_for_command(command: ExecutionCommand, tick: dict[str, Any] | None) -> tuple[float | None, str]:
    row = dict(tick or {})
    bid = _safe_float(row.get("bid"))
    ask = _safe_float(row.get("ask"))
    mid = _safe_float(row.get("mid"))
    if mid <= 0.0 and bid > 0.0 and ask > 0.0:
        mid = (bid + ask) / 2.0
    cmd = str(command.cmd or "").upper().strip()
    if cmd == "BUY":
        if ask > 0.0:
            return ask, "ask"
        if mid > 0.0:
            return mid, "mid"
    elif cmd == "SELL":
        if bid > 0.0:
            return bid, "bid"
        if mid > 0.0:
            return mid, "mid"
    elif cmd in {"CLOSE", "CLOSE_PARTIAL", "CLOSE_ALL", "MODIFY_SL"}:
        if mid > 0.0:
            return mid, "mid"
        if bid > 0.0:
            return bid, "bid"
        if ask > 0.0:
            return ask, "ask"
    return None, "unavailable"


def build_simulated_ack_payloads(
    command: ExecutionCommand,
    *,
    tick: dict[str, Any] | None = None,
    now_ts: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ts = float(now_ts or datetime.now(timezone.utc).timestamp())
    fill_price, fill_source = _fill_price_for_command(command, tick)
    filled_lots = float(command.close_lots if str(command.cmd).upper() == "CLOSE_PARTIAL" and command.close_lots > 0.0 else command.lots)
    paper_meta = {
        **dict(command.orchestration_meta_json or {}),
        "execution_provider": "paper",
        "paper_simulated": True,
        "paper_fill_price": fill_price,
        "paper_fill_source": str(fill_source),
        "paper_filled_lots": float(filled_lots),
        "paper_filled_at": ts,
    }
    delivered = {
        "command_id": command.command_id,
        "status": "delivered",
        "symbol": command.symbol,
        "message": "paper_delivery_simulated",
        "trace_id": command.trace_id,
        "correlation_id": command.correlation_id,
        "thread_id": command.thread_id,
        "idempotency_key": command.idempotency_key,
        "schema_version": command.schema_version,
        "orchestration_meta_json": dict(paper_meta),
        "updated_at": ts,
    }
    acked = {
        "command_id": command.command_id,
        "status": "acked",
        "symbol": command.symbol,
        "ticket": _ticket_for_command(command.command_id),
        "message": "paper_fill_simulated",
        "trace_id": command.trace_id,
        "correlation_id": command.correlation_id,
        "thread_id": command.thread_id,
        "idempotency_key": command.idempotency_key,
        "schema_version": command.schema_version,
        "orchestration_meta_json": dict(paper_meta),
        "updated_at": ts,
    }
    return delivered, acked
