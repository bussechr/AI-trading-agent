from __future__ import annotations

import pytest

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
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


def test_protocol_omits_orchestration_wire_fields_when_not_present() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-off-mode",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=60,
    )
    line = command_to_mt4_line(cmd)
    assert "correlation_id=" not in line
    assert "thread_id=" not in line
    assert "idempotency_key=" not in line
    assert "schema_version=" not in line
    assert "orchestration_meta_json=" not in line


def test_protocol_includes_orchestration_wire_fields_when_present() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-shadow-mode",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "correlation_id": "EURUSD:123:shadow",
            "thread_id": "EURUSD:123:shadow",
            "idempotency_key": "idem-123",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_meta_json": {"run_id": "run-1", "trace_id": "trace-1"},
        },
        default_session_id="unit",
        ttl_secs=60,
    )
    line = command_to_mt4_line(cmd)
    assert "correlation_id=EURUSD:123:shadow" in line
    assert "thread_id=EURUSD:123:shadow" in line
    assert "idempotency_key=idem-123" in line
    assert f"schema_version={ORCHESTRATION_SCHEMA_VERSION}" in line
    assert "orchestration_meta_json=" in line


def test_protocol_supports_paper_execution_provider() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-paper-mode",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "correlation_id": "EURUSD:123:paper",
            "thread_id": "EURUSD:123:paper",
        },
        default_session_id="unit",
        ttl_secs=60,
    )
    line = command_to_provider_line(cmd, provider="paper")
    assert "provider=paper" in line
    assert "paper_simulated=1" in line
    assert "correlation_id=EURUSD:123:paper" in line


def test_protocol_uses_command_proto_when_present() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-proto-override",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=60,
    )
    cmd.proto = "v1"
    line = command_to_mt4_line(cmd)
    assert "proto=v1" in line


def test_execution_command_generates_stable_command_id_when_missing() -> None:
    payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
    }
    cmd1 = ExecutionCommand.from_payload(payload, default_session_id="unit", ttl_secs=60)
    cmd2 = ExecutionCommand.from_payload(payload, default_session_id="unit", ttl_secs=60)

    assert cmd1.command_id == cmd2.command_id
    assert cmd1.trace_id == cmd1.command_id
    assert cmd2.trace_id == cmd2.command_id


def test_execution_command_accepts_documented_id_alias() -> None:
    cmd = ExecutionCommand.from_payload(
        {"id": "legacy-command-1", "cmd": "BUY", "symbol": "EURUSD", "lots": 0.1},
        default_session_id="unit",
        ttl_secs=60,
    )

    assert cmd.command_id == "legacy-command-1"
    assert cmd.trace_id == "legacy-command-1"


def test_execution_ack_accepts_idempotency_key_without_command_id() -> None:
    ack = ExecutionAck.from_payload({"status": "acked", "ticket": 11, "idempotency_key": "idem-1"})
    assert ack.command_id == ""
    assert ack.idempotency_key == "idem-1"


def test_execution_ack_accepts_documented_id_and_error_aliases() -> None:
    ack = ExecutionAck.from_payload(
        {"id": "legacy-command-1", "status": "error", "error": "broker rejected order"}
    )

    assert ack.command_id == "legacy-command-1"
    assert ack.status == "failed"
    assert ack.message == "broker rejected order"


def test_execution_ack_preserves_filled_wire_compatibility() -> None:
    ack = ExecutionAck.from_payload(
        {"command_id": "legacy-fill-1", "status": "filled", "ticket": 42}
    )

    assert ack.status == "acked"
    assert ack.count_as_trade is True


@pytest.mark.parametrize("status", [None, "", "ackd"])
def test_execution_ack_rejects_missing_or_unknown_status(status: str | None) -> None:
    with pytest.raises(ValueError, match="status"):
        ExecutionAck.from_payload({"command_id": "c-invalid-ack", "status": status})


def test_execution_command_rejects_non_finite_queue_math() -> None:
    with pytest.raises(ValueError, match="ttl_secs"):
        ExecutionCommand.from_payload(
            {"command_id": "c-invalid-ttl", "cmd": "BUY", "symbol": "EURUSD", "lots": 0.1},
            default_session_id="unit",
            ttl_secs=float("nan"),
        )


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


def test_runtime_service_does_not_queue_dry_run_external_provider() -> None:
    class _DummyStore:
        def enqueue_command(self, cmd):
            raise AssertionError("dry-run provider must fail before enqueue")

    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "oanda"
    service.store = _DummyStore()

    out, code = service.submit_command(
        {"command_id": "c-oanda", "cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
    )

    assert code == 400
    assert out["status"] == "invalid"
    assert "no active runtime adapter" in out["error"]


def test_runtime_service_preserves_id_alias_without_content_dedupe_key() -> None:
    captured: list[ExecutionCommand] = []

    class _DummyStore:
        def enqueue_command(self, cmd):
            captured.append(cmd)
            return True, "queued"

    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "mt4"
    service.store = _DummyStore()

    out, code = service.submit_command(
        {"id": "legacy-command-2", "cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
    )

    assert code == 200
    assert out["command_id"] == "legacy-command-2"
    assert captured[0].idempotency_key == ""


def test_runtime_service_returns_400_for_non_scalar_command_field() -> None:
    class _DummyStore:
        def enqueue_command(self, cmd):
            raise AssertionError("invalid command must fail before enqueue")

    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "mt4"
    service.store = _DummyStore()

    out, code = service.submit_command(
        {"command_id": "c-bad-lots", "cmd": "BUY", "symbol": "EURUSD", "lots": []}
    )

    assert code == 400
    assert out["status"] == "invalid"
