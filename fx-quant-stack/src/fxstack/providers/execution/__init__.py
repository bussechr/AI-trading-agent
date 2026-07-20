from importlib import import_module
from typing import Any

from fxstack.providers.execution.mt4 import command_to_wire_line as mt4_command_to_wire_line

command_to_wire_line = mt4_command_to_wire_line

__all__ = ["command_to_wire_line", "mt4_command_to_wire_line", "paper_command_to_wire_line"]


def __getattr__(name: str) -> Any:
    if name == "paper_command_to_wire_line":
        module = import_module("fxstack.providers.execution.paper")
        return module.command_to_wire_line
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
