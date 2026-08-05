from __future__ import annotations

import pytest
from fxstack.api.schemas import CommandAckRequest, CommandRequest
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.runtime import service as runtime_service_module
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.protocol import command_to_mt4_line, command_to_provider_line
from fxstack.runtime.service import FinalEntryApproval, RuntimeService
from pydantic import ValidationError


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


def test_entry_wire_derives_stable_bounded_owner_and_exact_positive_magic() -> None:
    payload = {
        "command_id": "owned-entry-1",
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "magic": 246810,
    }
    first = ExecutionCommand.from_payload(
        payload,
        default_session_id="unit",
        ttl_secs=60,
    )
    second = ExecutionCommand.from_payload(
        payload,
        default_session_id="unit",
        ttl_secs=60,
    )

    assert first.owner_token == second.owner_token
    assert first.owner_token.startswith("fxs-")
    assert len(first.owner_token) == 22
    assert len(first.owner_token) < 31
    line = command_to_mt4_line(first)
    assert "magic=246810" in line
    assert f"owner_token={first.owner_token}" in line
    assert "ownership_contract=ticket_owner_v1" in line
    assert '"owner_token"' not in line


def test_entry_rejects_non_positive_magic() -> None:
    with pytest.raises(ValueError, match="magic must be a positive integer"):
        ExecutionCommand.from_payload(
            {
                "command_id": "owned-entry-zero-magic",
                "cmd": "SELL",
                "symbol": "EURUSD",
                "lots": 0.1,
                "magic": 0,
            },
            default_session_id="unit",
            ttl_secs=60,
        )


@pytest.mark.parametrize("cmd", ["CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"])
def test_strict_management_wire_targets_one_ticket_owner_and_magic(cmd: str) -> None:
    payload = {
        "command_id": f"owned-{cmd.lower()}",
        "cmd": cmd,
        "symbol": "EURUSD",
        "magic": 246810,
        "target_ticket": 12345,
        "owner_token": "fxs-owned-ticket-123",
        "strategy_lane": "production_scalper",
    }
    if cmd == "CLOSE_PARTIAL":
        payload["close_lots"] = 0.05
    if cmd == "MODIFY_SL":
        payload["sl_price"] = 1.101

    command = ExecutionCommand.from_payload(
        payload,
        default_session_id="unit",
        ttl_secs=60,
    )
    line = command_to_mt4_line(command)

    assert "target_ticket=12345" in line
    assert "magic=246810" in line
    assert "owner_token=fxs-owned-ticket-123" in line
    assert "ownership_contract=ticket_owner_v1" in line


@pytest.mark.parametrize("cmd", ["CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"])
def test_production_scalper_management_fails_without_restart_join_identity(cmd: str) -> None:
    payload = {
        "command_id": f"unjoined-{cmd.lower()}",
        "cmd": cmd,
        "symbol": "EURUSD",
        "magic": 246810,
        "strategy_lane": "production_scalper",
    }
    if cmd == "CLOSE_PARTIAL":
        payload["close_lots"] = 0.05
    if cmd == "MODIFY_SL":
        payload["sl_price"] = 1.101

    with pytest.raises(ValueError, match="target_ticket must be a positive broker ticket"):
        ExecutionCommand.from_payload(
            payload,
            default_session_id="unit",
            ttl_secs=60,
        )


def test_legacy_management_is_explicitly_isolated_from_new_owner_comments() -> None:
    command = ExecutionCommand.from_payload(
        {
            "command_id": "legacy-close",
            "cmd": "CLOSE",
            "symbol": "EURUSD",
            "magic": 246810,
        },
        default_session_id="unit",
        ttl_secs=60,
    )

    line = command_to_mt4_line(command)
    assert "ownership_contract=legacy_elbridge_v1" in line
    assert "target_ticket=" not in line
    assert "owner_token=" not in line


@pytest.mark.parametrize(
    "identity_patch",
    [
        {"target_ticket": 0},
        {"owner_token": ""},
        {"target_ticket": 12345, "owner_token": ""},
    ],
)
def test_malformed_strict_identity_never_downgrades_to_legacy_symbol_management(
    identity_patch: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="target_ticket|owner_token"):
        ExecutionCommand.from_payload(
            {
                "command_id": "malformed-owned-close",
                "cmd": "CLOSE",
                "symbol": "EURUSD",
                "magic": 246810,
                **identity_patch,
            },
            default_session_id="unit",
            ttl_secs=60,
        )


def test_persisted_management_payload_restores_owner_identity_at_poll_serialization() -> None:
    # The durable command table keeps additive ownership fields in payload_json;
    # older table layouts reconstruct the typed DTO with default field values.
    rehydrated = ExecutionCommand(
        command_id="rehydrated-owned-close",
        session_id="unit",
        proto="v2",
        cmd="CLOSE",
        symbol="EURUSD",
        magic=246810,
        payload={
            "target_ticket": 12345,
            "owner_token": "fxs-owned-ticket-123",
            "ownership_contract": "ticket_owner_v1",
            "strategy_lane": "production_scalper",
        },
    )

    line = command_to_mt4_line(rehydrated)
    assert "target_ticket=12345" in line
    assert "owner_token=fxs-owned-ticket-123" in line
    assert "ownership_contract=ticket_owner_v1" in line


def test_execution_ack_preserves_broker_ticket_magic_and_owner_token() -> None:
    ack = ExecutionAck.from_payload(
        {
            "command_id": "owned-entry-ack",
            "status": "acked",
            "symbol": "EURUSD",
            "ticket": 12345,
            "magic": 246810,
            "owner_token": "fxs-owned-ticket-123",
        }
    )

    assert ack.ticket == 12345
    assert ack.magic == 246810
    assert ack.owner_token == "fxs-owned-ticket-123"
    assert ack.to_dict()["owner_token"] == "fxs-owned-ticket-123"


def test_command_and_ack_api_schemas_bound_owner_identity_fields() -> None:
    command = CommandRequest.model_validate(
        {
            "cmd": "CLOSE",
            "target_ticket": 12345,
            "magic": 246810,
            "owner_token": "fxs-owned-ticket-123",
        }
    )
    ack = CommandAckRequest.model_validate(
        {
            "command_id": "owned-command",
            "status": "acked",
            "ticket": 12345,
            "magic": 246810,
            "owner_token": "fxs-owned-ticket-123",
        }
    )

    assert command.target_ticket == 12345
    assert command.owner_token == "fxs-owned-ticket-123"
    assert ack.magic == 246810
    assert ack.owner_token == "fxs-owned-ticket-123"
    with pytest.raises(ValidationError):
        CommandRequest.model_validate(
            {"cmd": "CLOSE", "target_ticket": 0, "owner_token": "bad token"}
        )


def test_ack_api_schema_types_market_execution_attestation_and_preserves_provenance() -> None:
    ack = CommandAckRequest.model_validate(
        {
            "command_id": "market-entry-ack-1",
            "status": "reconcile_required",
            "mutation_state": "attempted",
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD.IG",
            "cmd": "BUY",
            "side": "BUY",
            "execution_type": "market",
            "ticket": 731,
            "target_ticket": -1,
            "magic": 246_810,
            "owner_token": "fxs-owner-prefix",
            "order_comment": "fxs-owner-prefix-srv",
            "actual_command_id": "market-entry-ack-1",
            "actual_symbol": "EURUSD",
            "actual_broker_symbol": "EURUSD.IG",
            "actual_cmd": "BUY",
            "actual_side": "BUY",
            "actual_execution_type": "market",
            "actual_ticket": 731,
            "actual_target_ticket": -1,
            "actual_magic": 246_810,
            "actual_owner_token": "fxs-owner-prefix",
            "actual_order_comment": "fxs-owner-prefix-srv",
            "actual_lots": 0.1,
            "actual_open_price": 1.1002,
            "actual_sl_price": 1.0992,
            "actual_tp_price": 1.1022,
            "broker_mutation_attempted": True,
            "broker_mutation_confirmed": False,
            "broker_outcome_known": False,
            "execution_uncertain": True,
            "t_ea_exec_end": 1_800_000_000.25,
        }
    )

    payload = ack.model_dump(exclude_none=True)
    assert ack.status == "reconcile_required"
    assert ack.execution_type == "market"
    assert ack.actual_execution_type == "market"
    assert ack.actual_lots == pytest.approx(0.1)
    assert ack.actual_order_comment == "fxs-owner-prefix-srv"
    assert payload["t_ea_exec_end"] == pytest.approx(1_800_000_000.25)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("actual_lots", float("nan")),
        ("actual_open_price", float("inf")),
        ("actual_sl_price", -1.0),
        ("actual_tp_price", -1.0),
        ("actual_order_comment", "x" * 32),
    ],
)
def test_ack_api_schema_rejects_invalid_market_attestation_values(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        CommandAckRequest.model_validate(
            {
                "command_id": "invalid-market-ack",
                "status": "reconcile_required",
                field_name: value,
            }
        )


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


def test_execution_ack_accepts_reconcile_required_and_actual_attestation() -> None:
    ack = ExecutionAck.from_payload(
        {
            "command_id": "scalp-entry-1",
            "status": "reconcile_required",
            "mutation_state": "attempted",
            "ticket": 731,
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD.IG",
            "cmd": "BUY",
            "side": "BUY",
            "actual_execution_type": "market",
            "magic": 246_810,
            "owner_token": "fxs-owner-prefix",
            "actual_lots": 0.1,
            "actual_open_price": 1.1002,
            "actual_sl_price": 1.0992,
            "actual_tp_price": 1.1022,
            "order_comment": "fxs-owner-prefix-srv",
            "attestation_reasons": ["actual_tp_mismatch"],
        }
    )

    assert ack.status == "reconcile_required"
    assert ack.count_as_trade is False
    assert ack.mutation_state == "attempted"
    assert ack.broker_symbol == "EURUSD.IG"
    assert ack.execution_type == "market"
    assert ack.actual_lots == pytest.approx(0.1)
    assert ack.order_comment.startswith(ack.owner_token)
    assert ack.attestation_reasons == ("actual_tp_mismatch",)


def test_execution_ack_uses_actual_market_aliases_without_losing_raw_evidence() -> None:
    ack = ExecutionAck.from_payload(
        {
            "actual_command_id": "actual-only-entry-ack",
            "idempotency_key": "actual-only-entry-ack-key",
            "status": "acked",
            "mutation_state": "confirmed",
            "actual_ticket": 991,
            "actual_target_ticket": -1,
            "actual_execution_type": "MARKET",
            "actual_lots": 0.12,
            "broker_mutation_confirmed": True,
            "broker_outcome_known": True,
        }
    )

    payload = ack.to_dict()
    assert ack.ticket == 991
    assert ack.target_ticket == -1
    assert ack.execution_type == "market"
    assert ack.count_as_trade is True
    assert payload["raw"]["actual_command_id"] == "actual-only-entry-ack"
    assert payload["raw"]["broker_mutation_confirmed"] is True


@pytest.mark.parametrize("ticket", (True, 1.5, "1.5", float("nan")))
def test_execution_ack_rejects_non_integral_ticket(ticket: object) -> None:
    with pytest.raises(ValueError, match="ticket"):
        ExecutionAck.from_payload(
            {
                "command_id": "invalid-ticket-ack",
                "status": "failed",
                "ticket": ticket,
            }
        )


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

        def get_execution_uncertainty(self, *, symbol=""):
            assert symbol == "EURUSD"
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
            # The store re-checks live admission atomically with the enqueue.
            # Identity fields are carried for imported external witnesses and
            # are empty under production-owned authority.
            assert required_live_admission == {
                "pair": "EURUSD",
                "broker_account_mode": "demo",
                "broker_account_scope": "demo-account-scope",
                "authority_revision": 1,
                "release_generation_id": "",
                "release_request_sha256": "",
                "model_identity_sha256": "",
                "manifest_file_sha256": "",
                "runtime_boot_id": "",
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
            "adaptive_sleeve": "trend_pullback",
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
        sleeve="trend_pullback",
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
        def get_execution_uncertainty(self, *, symbol=""):
            assert symbol == "EURUSD"
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
