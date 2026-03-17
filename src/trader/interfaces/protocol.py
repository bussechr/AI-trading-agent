from __future__ import annotations

from typing import Any

from .dto import ExecutionCommand


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    if len(out) > max_len:
        out = out[:max_len]
    return out


def command_to_mt4_line(command: ExecutionCommand) -> str:
    payload = dict(command.payload or {})
    cmd = str(command.cmd).upper().strip()
    if cmd == "CLOSE_ALL":
        parts = ["cmd=CLOSE_ALL"]
    else:
        parts = [f"cmd={cmd}"]
        if command.symbol:
            parts.append(f"symbol={command.symbol}")

    if command.lots is not None:
        parts.append(f"lots={float(command.lots)}")
    if command.tp_cash is not None:
        parts.append(f"tp_cash={float(command.tp_cash)}")
    if command.tp_price is not None:
        parts.append(f"tp_price={float(command.tp_price)}")
    if command.sl_price is not None:
        parts.append(f"sl={float(command.sl_price)}")

    parts.append(f"magic={int(command.magic)}")
    parts.append("proto=v2")
    parts.append(f"command_id={command.command_id}")
    parts.append(f"session_id={command.session_id}")
    parts.append(f"intent={command.intent}")
    parts.append(f"trace_id={command.trace_id or command.command_id}")
    parts.append(f"t_bridge_queued={float(command.created_at):.6f}")

    if payload.get("thought"):
        parts.append(f"thought={safe_text(payload.get('thought'))}")

    return ";".join(parts)
