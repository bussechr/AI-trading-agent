"""The runtime execution dispatch routes the OANDA/IBKR/MT5 connectors."""

from __future__ import annotations

import subprocess
import sys

import pytest

from fxstack.runtime.dto import ExecutionCommand
from fxstack.runtime.protocol import SUPPORTED_EXECUTION_PROVIDERS, command_to_provider_line


def _cmd() -> ExecutionCommand:
    return ExecutionCommand.from_payload(
        {
            "command_id": "c-broker",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "correlation_id": "EURUSD:1:x",
            "thread_id": "EURUSD:1:x",
        },
        default_session_id="unit",
        ttl_secs=60,
    )


def test_protocol_import_defers_every_execution_adapter() -> None:
    provider_modules = [
        f"fxstack.providers.execution.{name}"
        for name in ("mt4", "paper", "oanda", "ibkr", "mt5")
    ]
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import fxstack.runtime.protocol; "
            f"unexpected=[name for name in {provider_modules!r} if name in sys.modules]; "
            "assert not unexpected, unexpected",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("selected", ["mt4", "paper", "oanda", "ibkr", "mt5"])
def test_execution_package_imports_only_the_selected_adapter(selected: str) -> None:
    provider_modules = [
        f"fxstack.providers.execution.{name}"
        for name in ("mt4", "paper", "oanda", "ibkr", "mt5")
    ]
    selected_module = f"fxstack.providers.execution.{selected}"
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys; "
            f"importlib.import_module({selected_module!r}); "
            f"unexpected=[name for name in {provider_modules!r} "
            f"if name != {selected_module!r} and name in sys.modules]; "
            "assert not unexpected, unexpected",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert check.returncode == 0, check.stderr


def test_execution_package_preserves_lazy_compatibility_exports() -> None:
    from fxstack.providers import execution

    assert execution.command_to_wire_line is execution.mt4_command_to_wire_line
    assert callable(execution.paper_command_to_wire_line)
    assert execution.__dict__["command_to_wire_line"] is execution.command_to_wire_line
    assert (
        execution.__dict__["paper_command_to_wire_line"]
        is execution.paper_command_to_wire_line
    )


def test_mt4_dispatch_does_not_hydrate_unselected_execution_adapters() -> None:
    unselected = [
        f"fxstack.providers.execution.{name}"
        for name in ("paper", "oanda", "ibkr", "mt5")
    ]
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "from fxstack.runtime.dto import ExecutionCommand; "
            "from fxstack.runtime.protocol import command_to_mt4_line; "
            "cmd=ExecutionCommand.from_payload({'command_id':'cold-info','cmd':'INFO',"
            "'symbol':'EURUSD','lots':0.0},default_session_id='unit',ttl_secs=60); "
            "command_to_mt4_line(cmd); "
            f"unexpected=[name for name in {unselected!r} if name in sys.modules]; "
            "assert not unexpected, unexpected",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert check.returncode == 0, check.stderr


def test_all_brokers_are_supported():
    assert {"mt4", "paper", "oanda", "ibkr", "mt5"} <= SUPPORTED_EXECUTION_PROVIDERS


@pytest.mark.parametrize("provider", ["oanda", "ibkr", "mt5"])
def test_provider_routes_to_its_wire_line(provider):
    line = command_to_provider_line(_cmd(), provider=provider)
    # The provider marker proves the dispatch routed to that connector's wire fn.
    assert f"provider={provider}" in line
    assert "cmd=BUY" in line and "lots=0.1" in line


def test_unknown_provider_still_rejected():
    with pytest.raises(ValueError):
        command_to_provider_line(_cmd(), provider="binance_spot")
