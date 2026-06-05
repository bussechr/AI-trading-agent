"""The runtime execution dispatch routes the OANDA/IBKR/MT5 connectors."""

from __future__ import annotations

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
