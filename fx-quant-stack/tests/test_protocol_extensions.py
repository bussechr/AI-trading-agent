from __future__ import annotations

import pytest

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.runtime import service as runtime_service_module
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.protocol import command_to_mt4_line, command_to_provider_line
from fxstack.runtime.service import FinalEntryApproval, RuntimeService


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


def test_runtime_service_loads_paper_adapter_only_when_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []
    real_import_module = runtime_service_module.import_module

    def _recording_import(module_name: str):
        imported.append(module_name)
        return real_import_module(module_name)

    monkeypatch.setattr(runtime_service_module, "import_module", _recording_import)
    monkeypatch.setattr(
        runtime_service_module,
        "PostgresRuntimeStore",
        lambda *_args, **_kwargs: object(),
    )

    RuntimeService(database_url="unused://mt4", execution_provider="mt4")
    assert imported == []

    RuntimeService(database_url="unused://paper", execution_provider="paper")
    assert imported == ["fxstack.providers.execution.paper"]


def test_runtime_service_fails_before_store_if_paper_adapter_is_pruned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def _missing_paper(module_name: str):
        events.append(f"import:{module_name}")
        raise ModuleNotFoundError(module_name)

    def _unexpected_store(*_args, **_kwargs):
        events.append("store")
        raise AssertionError("paper capability rejection must precede database setup")

    monkeypatch.setattr(runtime_service_module, "import_module", _missing_paper)
    monkeypatch.setattr(runtime_service_module, "PostgresRuntimeStore", _unexpected_store)

    with pytest.raises(RuntimeError, match="paper execution provider is unavailable"):
        RuntimeService(database_url="unused://paper", execution_provider="paper")

    assert events == ["import:fxstack.providers.execution.paper"]


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


def test_execution_command_rejects_marked_runtime_entry_without_protection() -> None:
    with pytest.raises(ValueError, match="sl_price"):
        ExecutionCommand.from_payload(
            {
                "command_id": "c-unprotected-runtime-buy",
                "cmd": "BUY",
                "symbol": "EURUSD",
                "lots": 0.1,
                "entry_protection_required": True,
            },
            default_session_id="unit",
            ttl_secs=60,
        )


def test_marked_runtime_entry_serializes_both_protection_prices() -> None:
    cmd = ExecutionCommand.from_payload(
        {
            "command_id": "c-protected-runtime-buy",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.099,
            "tp_price": 1.104,
            "entry_protection_required": True,
            "expected_account_mode": "demo",
            "expected_account_scope": "demo-scope-17",
        },
        default_session_id="unit",
        ttl_secs=60,
    )

    line = command_to_mt4_line(cmd)
    assert "sl=1.099" in line
    assert "tp_price=1.104" in line
    assert "expected_account_mode=demo" in line
    assert "expected_account_scope=demo-scope-17" in line


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


def test_live_mt4_service_rejects_direct_entry_without_canonical_approval() -> None:
    captured: list[ExecutionCommand] = []

    class _DummyStore:
        state: dict[str, object] = {}

        def update_state_patch(self, patch):
            self.state.update(dict(patch or {}))

        def get_state(self):
            return dict(self.state)

        def get_execution_uncertainty(self):
            return {
                "blocked": False,
                "reason": "",
                "count": 0,
                "statuses": {},
                "commands": [],
            }

        def enqueue_command(
            self,
            cmd,
            *,
            require_resolved_execution=False,
            required_live_admission=None,
        ):
            assert require_resolved_execution is True
            assert required_live_admission == {
                "pair": "EURUSD",
                "broker_account_mode": "demo",
                "broker_account_scope": "demo-account-scope",
                "authority_revision": 1,
            }
            captured.append(cmd)
            return True, "queued"

    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "mt4"
    service.store = _DummyStore()
    risk_payload = {
        "command_id": "approved-entry",
        "cmd": "BUY",
        "side": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "sl_price": 1.09,
        "tp_price": 1.12,
        "intent": "ENTRY",
        "action": "entry",
    }
    final_payload = {
        **risk_payload,
        "correlation_id": "EURUSD:live:approved",
        "thread_id": "EURUSD:live:approved",
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "trace_id": "trace-approved",
        "orchestration_meta_json": {
            "trace_id": "trace-approved",
            "authority_revision": 1,
        },
    }
    service.patch_state(
        {
            "broker_account_mode": "demo",
            "broker_account_scope": "demo-account-scope",
            "runtime_diag": {
                "orchestration_live": {
                    "authority_revision": 1,
                    "enabled": True,
                    "mode": "live",
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "active_pair_scope": ["EURUSD"],
                    "active_intent_scope": ["enter"],
                },
                "live_command_admission": {
                    "allowed": True,
                    "pairs": {"EURUSD": {"allowed": True}},
                },
            },
        }
    )

    blocked, blocked_code = service.submit_command(final_payload)

    assert blocked_code == 403
    assert blocked["error"] == "final_entry_approval_required"
    assert captured == []

    approval = FinalEntryApproval(
        pair="EURUSD",
        side="BUY",
        risk_approved_payload=risk_payload,
        canonical_ready=True,
        governed_allowed=True,
        rollout_active=True,
        rollout_mode="canary",
        rollout_pair_allowlisted=True,
        correlation_id="EURUSD:live:approved",
        trace_id="trace-approved",
        broker_account_mode="demo",
        broker_account_scope="demo-account-scope",
        authority_revision=1,
    )
    queued, queued_code = service.submit_approved_command(
        final_payload,
        approval=approval,
    )

    assert queued_code == 200
    assert queued["status"] == "queued"
    assert len(captured) == 1


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
        def get_execution_uncertainty(self):
            return {"blocked": False, "reason": "", "count": 0, "statuses": {}, "commands": []}

        def enqueue_command(self, cmd, *, require_resolved_execution=False):
            assert require_resolved_execution is True
            captured.append(cmd)
            return True, "queued"

    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "mt4"
    service._require_entry_approval = False
    service.store = _DummyStore()

    out, code = service.submit_command(
        {
            "id": "legacy-command-2",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
        }
    )

    assert code == 200
    assert out["command_id"] == "legacy-command-2"
    assert captured[0].idempotency_key == ""


def test_runtime_service_rejects_unprotected_entry_even_when_legacy_strict_flag_is_off(monkeypatch) -> None:
    class _DummyStore:
        def enqueue_command(self, cmd):  # pragma: no cover - rejection happens first
            raise AssertionError("unprotected entry reached the queue")

    monkeypatch.setenv("FXSTACK_STRICT_COMMAND_VALIDATION", "0")
    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "mt4"
    service._require_entry_approval = False
    service.store = _DummyStore()

    out, code = service.submit_command(
        {"command_id": "unprotected-entry", "cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
    )

    assert code == 400
    assert out["status"] == "invalid"
    assert "sl_price is required" in out["error"]


def test_runtime_service_returns_400_for_non_scalar_command_field() -> None:
    class _DummyStore:
        def enqueue_command(self, cmd):
            raise AssertionError("invalid command must fail before enqueue")

    service = RuntimeService.__new__(RuntimeService)
    service.default_session_id = "unit"
    service.command_ttl_secs = 30.0
    service.execution_provider = "mt4"
    service._require_entry_approval = False
    service.store = _DummyStore()

    out, code = service.submit_command(
        {"command_id": "c-bad-lots", "cmd": "BUY", "symbol": "EURUSD", "lots": []}
    )

    assert code == 400
    assert out["status"] == "invalid"
