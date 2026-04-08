from __future__ import annotations

import json
from typing import Any

from fxstack.runtime.dto import ExecutionCommand


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    return out[:max_len]


def command_to_wire_line(command: ExecutionCommand) -> str:
    payload = dict(command.payload or {})
    cmd = str(command.cmd).upper().strip()
    parts: list[str] = ["cmd=CLOSE_ALL"] if cmd == "CLOSE_ALL" else [f"cmd={cmd}"]
    if cmd != "CLOSE_ALL" and command.symbol:
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
            f"magic={int(command.magic)}",
            f"proto={safe_text(command.proto or 'v2', max_len=16)}",
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
