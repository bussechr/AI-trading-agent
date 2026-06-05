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

from fxstack.providers.execution.ibkr import command_to_wire_line as _ibkr_command_to_wire_line
from fxstack.providers.execution.mt4 import command_to_wire_line as _mt4_command_to_wire_line
from fxstack.providers.execution.mt5 import command_to_wire_line as _mt5_command_to_wire_line
from fxstack.providers.execution.oanda import command_to_wire_line as _oanda_command_to_wire_line
from fxstack.providers.execution.paper import command_to_wire_line as _paper_command_to_wire_line
from fxstack.runtime.dto import ExecutionCommand

SUPPORTED_EXECUTION_PROVIDERS = {"mt4", "paper", "oanda", "ibkr", "mt5"}

_PROVIDER_WIRE_FUNCS = {
    "mt4": _mt4_command_to_wire_line,
    "paper": _paper_command_to_wire_line,
    "oanda": _oanda_command_to_wire_line,
    "ibkr": _ibkr_command_to_wire_line,
    "mt5": _mt5_command_to_wire_line,
}


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    return out[:max_len]


def command_to_mt4_line(command: ExecutionCommand) -> str:
    return _mt4_command_to_wire_line(command)


def command_to_provider_line(command: ExecutionCommand, *, provider: str = "mt4") -> str:
    provider_name = str(provider or "mt4").strip().lower()
    wire_func = _PROVIDER_WIRE_FUNCS.get(provider_name)
    if wire_func is None:
        raise ValueError(f"unsupported execution provider: {provider_name}")
    return wire_func(command)
