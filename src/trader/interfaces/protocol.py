from __future__ import annotations

from typing import Any

from fxstack.runtime.protocol import command_to_mt4_line as _command_to_mt4_line
from fxstack.runtime.protocol import safe_text

from .dto import ExecutionCommand


def command_to_mt4_line(command: ExecutionCommand) -> str:
    return _command_to_mt4_line(command)
