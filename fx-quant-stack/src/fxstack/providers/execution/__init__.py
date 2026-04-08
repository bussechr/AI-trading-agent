from fxstack.providers.execution.mt4 import command_to_wire_line as mt4_command_to_wire_line
from fxstack.providers.execution.paper import command_to_wire_line as paper_command_to_wire_line

command_to_wire_line = mt4_command_to_wire_line

__all__ = ["command_to_wire_line", "mt4_command_to_wire_line", "paper_command_to_wire_line"]
