"""Unit tests for the headless mock MT4 EA wire parsing + fill simulation."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tools"))

import mock_mt4_ea  # noqa: E402  (tools/ is not a package; load by path)


def test_parse_wire_line():
    line = "cmd=BUY;symbol=EURUSD;lots=0.10;command_id=abc-123;intent=ENTRY;proto=v2"
    fields = mock_mt4_ea.parse_wire_line(line)
    assert fields["cmd"] == "BUY"
    assert fields["symbol"] == "EURUSD"
    assert fields["lots"] == "0.10"
    assert fields["command_id"] == "abc-123"
    assert fields["intent"] == "ENTRY"


def test_parse_wire_line_empty_and_malformed():
    assert mock_mt4_ea.parse_wire_line("") == {}
    assert mock_mt4_ea.parse_wire_line("no_command") == {}
    assert mock_mt4_ea.parse_wire_line("cmd=BUY;;garbage;sl=1.2") == {"cmd": "BUY", "sl": "1.2"}


def test_simulate_fill_open_and_close_all():
    ea = mock_mt4_ea.MockEA(bridge_url="http://x", pairs=["EURUSD"])
    open_fill = ea._simulate_fill({"cmd": "BUY", "symbol": "EURUSD", "lots": "0.1"})
    assert open_fill["status"] == "executed"  # maps to the bridge's 'acked' terminal status
    assert len(ea.positions) == 1
    assert ea.stats["opens"] == 1
    ea._simulate_fill({"cmd": "CLOSE_ALL"})
    assert len(ea.positions) == 0
    assert ea.stats["closes"] == 1


def test_simulate_fill_close_by_symbol():
    ea = mock_mt4_ea.MockEA(bridge_url="http://x", pairs=["EURUSD", "GBPUSD"])
    ea._simulate_fill({"cmd": "BUY", "symbol": "EURUSD", "lots": "0.1"})
    ea._simulate_fill({"cmd": "SELL", "symbol": "GBPUSD", "lots": "0.1"})
    assert len(ea.positions) == 2
    ea._simulate_fill({"cmd": "CLOSE", "symbol": "EURUSD"})
    assert len(ea.positions) == 1
    assert all(p.symbol == "GBPUSD" for p in ea.positions.values())


def test_price_walk_is_deterministic():
    a = mock_mt4_ea.MockEA(bridge_url="http://x", pairs=["EURUSD"], seed=11)
    b = mock_mt4_ea.MockEA(bridge_url="http://x", pairs=["EURUSD"], seed=11)
    assert [a._step_price("EURUSD") for _ in range(5)] == [b._step_price("EURUSD") for _ in range(5)]
