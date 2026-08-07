"""Execution-provider exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports

_EXPORTS = {
    "command_to_wire_line": ("fxstack.providers.execution.mt4", "command_to_wire_line"),
    "mt4_command_to_wire_line": (
        "fxstack.providers.execution.mt4",
        "command_to_wire_line",
    ),
    "paper_command_to_wire_line": (
        "fxstack.providers.execution.paper",
        "command_to_wire_line",
    ),
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
