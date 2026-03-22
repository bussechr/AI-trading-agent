from __future__ import annotations

from fxstack.runtime.dto import ExecutionAck as FxExecutionAck
from fxstack.runtime.dto import ExecutionCommand as FxExecutionCommand
from fxstack.runtime.protocol import command_to_mt4_line as fx_command_to_mt4_line
from src.trader.interfaces.dto import ExecutionAck as LegacyExecutionAck
from src.trader.interfaces.dto import ExecutionCommand as LegacyExecutionCommand
from src.trader.interfaces.protocol import command_to_mt4_line as legacy_command_to_mt4_line


def test_legacy_execution_command_matches_fxstack_mapping() -> None:
    payload = {
        "command_id": "cmd-1",
        "cmd": "CLOSE_PARTIAL",
        "symbol": "EURUSD",
        "lots": 0.10,
        "close_lots": 0.05,
        "action": "partial_tp",
        "action_score": 0.73,
        "reversal_token": "rev-1",
        "intent": "EXIT_MODEL",
        "ttl_secs": 30.0,
    }
    legacy = LegacyExecutionCommand.from_payload(payload, default_session_id="unit", proto="v2", now_ts=123.0)
    active = FxExecutionCommand.from_payload(payload, default_session_id="unit", ttl_secs=30.0, now_ts=123.0)
    active.proto = "v2"

    assert legacy.to_dict() == active.to_dict()
    assert legacy_command_to_mt4_line(legacy) == fx_command_to_mt4_line(active)


def test_legacy_execution_ack_matches_fxstack_mapping() -> None:
    payload = {
        "command_id": "cmd-1",
        "status": "acked",
        "symbol": "EURUSD",
        "ticket": 101,
        "message": "ok",
        "trace_id": "cmd-1",
    }
    legacy = LegacyExecutionAck.from_payload(payload, now_ts=456.0)
    active = FxExecutionAck.from_payload(payload, now_ts=456.0)

    assert legacy.to_dict() == active.to_dict()
