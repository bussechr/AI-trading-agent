# AGENT: ROLE: Serialize validated execution commands into the MT4 bridge wire format.
# AGENT: ENTRYPOINT: imported by `fxstack/runtime/service.py`.
# AGENT: PRIMARY INPUTS: `ExecutionCommand`.
# AGENT: PRIMARY OUTPUTS: `cmd=...;...` MT4 protocol lines.
# AGENT: DEPENDS ON: `fxstack/runtime/dto.py`.
# AGENT: CALLED BY: `fxstack/runtime/service.py`.
# AGENT: STATE / SIDE EFFECTS: pure serialization only.
# AGENT: HANDSHAKES: runtime queue -> MT4 bridge line protocol.
# AGENT: SEE: `docs/agents/bridge-and-api-handshakes.md` -> `fxstack/runtime/dto.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

from typing import Any

from fxstack.providers.execution.mt4 import command_to_wire_line as _mt4_command_to_wire_line
from fxstack.providers.execution.paper import command_to_wire_line as _paper_command_to_wire_line
from fxstack.runtime.dto import ExecutionCommand

SUPPORTED_EXECUTION_PROVIDERS = {"mt4", "paper"}


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    return out[:max_len]


def command_to_mt4_line(command: ExecutionCommand) -> str:
    return _mt4_command_to_wire_line(command)


def command_to_provider_line(command: ExecutionCommand, *, provider: str = "mt4") -> str:
    provider_name = str(provider or "mt4").strip().lower()
    if provider_name not in SUPPORTED_EXECUTION_PROVIDERS:
        raise ValueError(f"unsupported execution provider: {provider_name}")
    if provider_name == "paper":
        return _paper_command_to_wire_line(command)
    return _mt4_command_to_wire_line(command)
