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

from functools import lru_cache
from importlib import import_module
from typing import Any, Callable

from fxstack.runtime.dto import ExecutionCommand

_PROVIDER_MODULES = {
    provider: f"fxstack.providers.execution.{provider}"
    for provider in ("mt4", "paper", "oanda", "ibkr", "mt5")
}
SUPPORTED_EXECUTION_PROVIDERS = set(_PROVIDER_MODULES)


@lru_cache(maxsize=None)
def _provider_wire_func(provider: str) -> Callable[[ExecutionCommand], str]:
    module_name = _PROVIDER_MODULES[provider]
    try:
        module = import_module(module_name)
        wire_func = module.command_to_wire_line
    except (AttributeError, ImportError) as exc:
        label = "paper execution provider" if provider == "paper" else provider
        raise ValueError(
            f"{label} is unavailable in this runtime distribution"
        ) from exc
    if not callable(wire_func):
        label = "paper execution provider" if provider == "paper" else provider
        raise ValueError(
            f"{label} is unavailable in this runtime distribution"
        )
    return wire_func


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    return out[:max_len]


def command_to_mt4_line(command: ExecutionCommand) -> str:
    return str(_provider_wire_func("mt4")(command))


def command_to_provider_line(command: ExecutionCommand, *, provider: str = "mt4") -> str:
    provider_name = str(provider or "mt4").strip().lower()
    if provider_name not in _PROVIDER_MODULES:
        raise ValueError(f"unsupported execution provider: {provider_name}")
    return str(_provider_wire_func(provider_name)(command))
