from __future__ import annotations

import pytest

from fxstack.runtime.dto import ExecutionCommand
from fxstack.runtime.protocol import command_to_mt4_line, command_to_provider_line
from fxstack.runtime.service import RuntimeService


def test_protocol_close_partial_serialization() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-close-partial",
            "cmd": "CLOSE_PARTIAL",
            "symbol": "EURUSD",
            "close_lots": 0.12,
            "intent": "EXIT_MODEL",
            "action": "partial_tp",
            "action_score": 0.73,
        },
        default_session_id="unit",
        ttl_secs=60,
    )
    line = command_to_mt4_line(cmd)
    assert "cmd=CLOSE_PARTIAL" in line
    assert "close_lots=0.12" in line
    assert "action=partial_tp" in line


def test_protocol_modify_sl_serialization() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-modify-sl",
            "cmd": "MODIFY_SL",
            "symbol": "USDJPY",
            "sl_price": 149.88,
            "intent": "ADJUST_MODEL",
            "action": "tighten_stop",
            "action_score": 0.5,
            "reversal_token": "rev-1",
        },
        default_session_id="unit",
        ttl_secs=60,
    )
    line = command_to_mt4_line(cmd)
    assert "cmd=MODIFY_SL" in line
    assert "sl=149.88" in line
    assert "reversal_token=rev-1" in line


def test_execution_command_rejects_invalid_entry_without_lots() -> None:
    with pytest.raises(ValueError, match="lots"):
        ExecutionCommand.from_payload(
            {
                "command_id": "c-invalid-buy",
                "cmd": "BUY",
                "symbol": "EURUSD",
                "lots": 0.0,
            },
            default_session_id="unit",
            ttl_secs=60,
        )


def test_execution_command_rejects_modify_sl_without_price() -> None:
    with pytest.raises(ValueError, match="sl_price"):
        ExecutionCommand.from_payload(
            {
                "command_id": "c-invalid-modify",
                "cmd": "MODIFY_SL",
                "symbol": "EURUSD",
            },
            default_session_id="unit",
            ttl_secs=60,
        )


def test_command_to_provider_line_rejects_non_mt4_provider() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-provider-guard",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=60,
    )

    with pytest.raises(ValueError, match="unsupported execution provider"):
        command_to_provider_line(cmd, provider="binance_spot")


def test_runtime_service_fails_closed_for_non_mt4_execution_provider(tmp_path) -> None:
    class _DummyStore:
        def enqueue_command(self, cmd):
            raise AssertionError("unsupported provider should fail before enqueue")

        def poll_next_command(self):
            raise AssertionError("unsupported provider should fail before poll")

    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "binance_spot"
    service.store = _DummyStore()

    queued, code = service.submit_command({"command_id": "c-provider-submit", "cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert code == 400
    assert queued["status"] == "invalid"
    assert "unsupported execution provider" in queued["error"]

    polled, poll_code = service.poll_command(as_line=False)
    assert poll_code == 400
    assert polled["status"] == "invalid"
    assert "unsupported execution provider" in polled["error"]
