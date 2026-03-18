from __future__ import annotations

from typing import Any

from fxstack.runtime.dto import ExecutionCommand


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    return out[:max_len]


def command_to_mt4_line(command: ExecutionCommand) -> str:
    payload = dict(command.payload or {})
    cmd = str(command.cmd).upper().strip()
    parts: list[str] = ["cmd=CLOSE_ALL"] if cmd == "CLOSE_ALL" else [f"cmd={cmd}"]
    if cmd != "CLOSE_ALL" and command.symbol:
        parts.append(f"symbol={command.symbol}")

    parts.append(f"lots={float(command.lots)}")
    if command.tp_cash is not None:
        parts.append(f"tp_cash={float(command.tp_cash)}")
    if command.tp_price is not None:
        parts.append(f"tp_price={float(command.tp_price)}")
    if command.sl_price is not None:
        parts.append(f"sl={float(command.sl_price)}")

    parts.extend(
        [
            f"magic={int(command.magic)}",
            "proto=v2",
            f"command_id={command.command_id}",
            f"session_id={command.session_id}",
            f"intent={command.intent}",
            f"trace_id={command.trace_id or command.command_id}",
            f"t_bridge_queued={float(command.created_at):.6f}",
        ]
    )

    thought = payload.get("thought")
    if thought:
        parts.append(f"thought={safe_text(thought)}")

    return ";".join(parts)
