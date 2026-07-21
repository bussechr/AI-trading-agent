from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time

import pytest
from sqlalchemy import select

from fxstack.runtime import release_authority
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.dto import SUPPORTED_COMMANDS, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


def _fresh_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RuntimeService:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    monkeypatch.setenv("FXSTACK_DATABASE_URL", database_url)
    get_settings.cache_clear()
    migrated = migrate_database(
        database_url=database_url,
        root=Path(__file__).resolve().parents[1],
    )
    assert migrated.get("ok") is True, migrated
    get_settings.cache_clear()
    return RuntimeService(
        database_url=database_url,
        execution_provider="mt4",
    )


def _payload_for(command: str, *, suffix: str = "shadow") -> dict[str, object]:
    cmd = str(command).upper()
    payload: dict[str, object] = {
        "command_id": f"egress-{suffix}-{cmd.lower()}",
        "cmd": cmd,
    }
    if cmd in {"BUY", "SELL", "CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"}:
        payload["symbol"] = "EURUSD"
    if cmd in {"BUY", "SELL"}:
        payload.update(
            {
                "lots": 0.01,
                "sl_price": 1.09,
                "tp_price": 1.12,
            }
        )
    if cmd == "CLOSE_PARTIAL":
        payload["close_lots"] = 0.01
    if cmd == "MODIFY_SL":
        payload["sl_price"] = 1.095
    return payload


def _insert_legacy_queued(
    store: PostgresRuntimeStore,
    payload: dict[str, object],
) -> None:
    cmd = ExecutionCommand.from_payload(
        payload,
        default_session_id="egress-test",
        ttl_secs=120.0,
    )
    with store.engine.begin() as conn:
        conn.execute(
            store.commands.insert().values(
                command_id=cmd.command_id,
                session_id=cmd.session_id,
                proto=cmd.proto,
                cmd=cmd.cmd,
                symbol=cmd.symbol,
                lots=cmd.lots,
                tp_cash=cmd.tp_cash,
                tp_price=cmd.tp_price,
                sl_price=cmd.sl_price,
                magic=cmd.magic,
                intent=cmd.intent,
                trace_id=cmd.trace_id,
                correlation_id=cmd.correlation_id,
                thread_id=cmd.thread_id,
                idempotency_key=cmd.idempotency_key,
                schema_version=cmd.schema_version,
                orchestration_meta_json=dict(cmd.orchestration_meta_json),
                status="queued",
                created_at=cmd.created_at,
                updated_at=cmd.updated_at,
                expires_at=cmd.expires_at,
                delivered_count=0,
                reason="legacy_unfenced_row",
                payload_json=dict(cmd.payload),
                ack_json={},
            )
        )


def _structural_release_request(now: float) -> dict[str, object]:
    digest = "a" * 64
    return {
        "schema_version": "fxstack_live_release_authority_request_v1",
        "generation_id": "generation-1",
        "request_sha256": "b" * 64,
        "pair": "EURUSD",
        "scope_key": "EURUSD",
        "bundle_run_id": "bundle-1",
        "model_set_id": "model-1",
        "source_sha256": digest,
        "package_merkle_sha256": digest,
        "config_sha256": digest,
        "manifest_file_sha256": digest,
        "model_identity_sha256": digest,
        "artifact_set_sha256": digest,
        "phase5_bundle_sha256": digest,
        "release_validation_bundle_sha256": digest,
        "authorized_execution": {
            "agent_mode": "live",
            "execution_provider": "mt4",
            "account_mode": "demo",
            "account_scope": "account-1",
            "pair_scope": ["EURUSD"],
            "sleeve_scope": ["trend"],
            "intent_scope": ["enter", "exit", "adjust", "info"],
            "protective_intent_scope": ["exit", "adjust"],
            "emergency_flatten_all": False,
        },
        "external_witness": {
            "schema_version": "fxstack_external_release_witness_v1",
            "nonce": "n" * 32,
            "expires_at": now + 3600.0,
            "signature": "externally-signed",
        },
    }


def _structural_ack(request: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "fxstack_live_release_authority_ack_v1",
        "generation_id": request["generation_id"],
        "request_sha256": request["request_sha256"],
        "runtime_boot_id": "boot-1",
        **{
            field: request[field]
            for field in (
                "source_sha256",
                "package_merkle_sha256",
                "config_sha256",
                "manifest_file_sha256",
                "model_identity_sha256",
                "artifact_set_sha256",
                "model_set_id",
            )
        },
    }


def _active_state(now: float) -> dict[str, object]:
    request = _structural_release_request(now)
    ack = _structural_ack(request)
    return {
        "execution_egress_enabled": True,
        "execution_egress_authority": {
            "schema_version": "fxstack_execution_egress_authority_v1",
            "enabled": True,
            "generation_id": request["generation_id"],
            "request_sha256": request["request_sha256"],
            "runtime_boot_id": ack["runtime_boot_id"],
        },
        "release_authority": {
            "schema_version": "fxstack_live_release_authority_state_v1",
            "status": "active",
            "request": request,
            "ack": ack,
        },
        "runtime_status": "running",
        "runtime_last_cycle_ts": now,
        "runtime_startup": {"boot_id": "boot-1"},
        "runtime_attestation": {
            "runtime_boot_id": "boot-1",
            "source_clean": True,
        },
        "broker_account_mode": "demo",
        "broker_account_scope": "account-1",
        "runtime_diag": {
            "orchestration_live": {
                "active_pair_scope": ["EURUSD"],
                "active_sleeve_scope": ["trend"],
                "active_intent_scope": ["enter", "exit", "adjust", "info"],
            }
        },
    }


def _release_meta(
    state: dict[str, object],
    *,
    sleeve: str = "",
) -> dict[str, object]:
    release = state["release_authority"]
    request = release["request"]
    ack = release["ack"]
    return {
        "release_generation_id": request["generation_id"],
        "release_request_sha256": request["request_sha256"],
        "release_model_identity_sha256": request["model_identity_sha256"],
        "release_manifest_file_sha256": request["manifest_file_sha256"],
        "release_runtime_boot_id": ack["runtime_boot_id"],
        "adaptive_sleeve": sleeve,
    }


def test_staged_safe_rejects_every_supported_command_and_poll_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fresh_service(tmp_path, monkeypatch)
    service.patch_state({"runtime_profile": "staged_safe", "runtime_status": "running"})

    observed: dict[str, tuple[int, str]] = {}
    for command in sorted(SUPPORTED_COMMANDS):
        response, status_code = service.submit_command(_payload_for(command))
        observed[command] = (status_code, str(response.get("error") or ""))

    assert set(observed) == SUPPORTED_COMMANDS
    assert all(status_code == 403 for status_code, _ in observed.values())
    assert service.get_commands(limit=20) == []
    polled, poll_code = service.poll_command(as_line=False)
    assert poll_code == 200
    assert polled == {"status": "empty"}


def test_staged_safe_poll_quarantines_legacy_rows_for_every_supported_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fresh_service(tmp_path, monkeypatch)
    for command in sorted(SUPPORTED_COMMANDS):
        _insert_legacy_queued(service.store, _payload_for(command, suffix="legacy"))

    polled, status_code = service.poll_command(as_line=False)

    assert status_code == 200
    assert polled == {"status": "empty"}
    rows = service.get_commands(limit=20)
    assert {str(row["cmd"]) for row in rows} == SUPPORTED_COMMANDS
    assert {str(row["status"]) for row in rows} == {"expired"}
    assert all(
        str(row["reason"]).startswith(
            "poll_egress_revoked:execution_egress_disabled"
        )
        for row in rows
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda state, now: state["runtime_startup"].update(
                {"boot_id": "boot-2"}
            ),
            "execution_egress_current_boot_mismatch",
        ),
        (
            lambda state, now: state.update(
                {"runtime_last_cycle_ts": now - 31.0}
            ),
            "execution_egress_runner_lease_stale",
        ),
        (
            lambda state, now: state.update({"runtime_status": "failed"}),
            "execution_egress_runner_not_running",
        ),
        (
            lambda state, now: state["release_authority"]["request"][
                "external_witness"
            ].update({"expires_at": now - 1.0}),
            "execution_egress_witness_expired",
        ),
        (
            lambda state, now: state.update(
                {"broker_account_scope": "account-2"}
            ),
            "execution_egress_account_scope_changed",
        ),
    ],
)
def test_egress_lease_fails_closed_on_runtime_or_account_drift(
    mutation,
    expected: str,
) -> None:
    now = time.time()
    state = _active_state(now)
    mutation(state, now)

    assert (
        PostgresRuntimeStore._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now,
        )
        == expected
    )


def test_egress_command_is_bound_to_signed_pair_intent_and_sleeve() -> None:
    now = time.time()
    state = _active_state(now)
    valid = ExecutionCommand.from_payload(
        {
            "command_id": "signed-entry",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.01,
            "sl_price": 1.09,
            "tp_price": 1.12,
            "sleeve": "trend",
            "orchestration_meta_json": _release_meta(
                state,
                sleeve="trend",
            ),
        },
        default_session_id="egress-test",
        ttl_secs=120.0,
    )
    assert (
        PostgresRuntimeStore._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now,
            command=valid,
        )
        == ""
    )

    wrong_pair = deepcopy(valid)
    wrong_pair.symbol = "GBPUSD"
    assert (
        PostgresRuntimeStore._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now,
            command=wrong_pair,
        )
        == "execution_egress_command_pair_blocked"
    )
    missing_sleeve = deepcopy(valid)
    missing_sleeve.payload.pop("sleeve")
    missing_sleeve.orchestration_meta_json["adaptive_sleeve"] = ""
    assert (
        PostgresRuntimeStore._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now,
            command=missing_sleeve,
        )
        == "execution_egress_command_sleeve_blocked"
    )
    close_all = ExecutionCommand.from_payload(
        {
            "command_id": "unscoped-close-all",
            "cmd": "CLOSE_ALL",
            "orchestration_meta_json": _release_meta(state),
        },
        default_session_id="egress-test",
        ttl_secs=120.0,
    )
    assert (
        PostgresRuntimeStore._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now,
            command=close_all,
        )
        == "execution_egress_emergency_flatten_unauthorized"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"command_id": "protective-close", "cmd": "CLOSE", "symbol": "EURUSD"},
        {
            "command_id": "protective-partial",
            "cmd": "CLOSE_PARTIAL",
            "symbol": "EURUSD",
            "close_lots": 0.01,
        },
        {
            "command_id": "protective-adjust",
            "cmd": "MODIFY_SL",
            "symbol": "EURUSD",
            "sl_price": 1.095,
        },
    ],
)
def test_active_release_allows_pair_bound_protection_without_sleeve(
    payload: dict[str, object],
) -> None:
    now = time.time()
    state = _active_state(now)
    command = ExecutionCommand.from_payload(
        {
            **payload,
            "orchestration_meta_json": _release_meta(state),
        },
        default_session_id="egress-test",
        ttl_secs=120.0,
    )

    assert (
        PostgresRuntimeStore._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now,
            command=command,
        )
        == ""
    )


def test_emergency_flatten_requires_separately_signed_capability() -> None:
    now = time.time()
    state = _active_state(now)
    state["release_authority"]["request"]["authorized_execution"][
        "emergency_flatten_all"
    ] = True
    command = ExecutionCommand.from_payload(
        {
            "command_id": "emergency-flatten",
            "cmd": "CLOSE_ALL",
            "orchestration_meta_json": _release_meta(state),
        },
        default_session_id="egress-test",
        ttl_secs=120.0,
    )

    assert (
        PostgresRuntimeStore._execution_egress_authorization_failure_from_state(
            state,
            now_ts=now,
            command=command,
        )
        == ""
    )


def test_release_cas_rejects_safety_activation_and_consumes_every_pending_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fresh_service(tmp_path, monkeypatch)
    monkeypatch.setattr(release_authority, "authority_request_errors", lambda *args, **kwargs: [])
    now = time.time()
    request = _structural_release_request(now)
    pending = {
        "schema_version": "fxstack_live_release_authority_state_v1",
        "status": "pending",
        "request": request,
        "ack": {},
    }

    first = service.compare_and_set_release_authority(next_authority=pending)
    replay = service.compare_and_set_release_authority(next_authority=pending)
    fake_active = service.compare_and_set_release_authority(
        next_authority={
            **pending,
            "status": "active",
            "ack": _structural_ack(request),
        },
        safety_dominant=True,
    )

    assert first["updated"] is True
    assert replay == {
        "updated": False,
        "reason": "release_witness_replayed",
        "authority": pending,
    }
    assert fake_active["updated"] is False
    assert fake_active["reason"] == "release_safety_transition_must_revoke"
    assert service.get_state()["execution_egress_enabled"] is False


def test_release_cas_exact_ack_is_only_egress_enable_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fresh_service(tmp_path, monkeypatch)
    monkeypatch.setattr(release_authority, "authority_request_errors", lambda *args, **kwargs: [])
    monkeypatch.setattr(release_authority, "active_authority_errors", lambda *args, **kwargs: [])
    now = time.time()
    request = _structural_release_request(now)
    ack = _structural_ack(request)
    service.patch_state(
        {
            **_active_state(now),
            "release_authority": {},
            "execution_egress_enabled": False,
            "execution_egress_authority": {},
        }
    )
    pending = {
        "schema_version": "fxstack_live_release_authority_state_v1",
        "status": "pending",
        "request": request,
        "ack": {},
    }
    acknowledged = {**pending, "status": "acknowledged", "ack": ack}
    active = {**acknowledged, "status": "active"}

    assert service.compare_and_set_release_authority(
        next_authority=pending
    )["execution_egress_enabled"] is False
    assert service.compare_and_set_release_authority(
        next_authority=acknowledged,
        expected_generation_id=str(request["generation_id"]),
        expected_status="pending",
    )["execution_egress_enabled"] is False
    activated = service.compare_and_set_release_authority(
        next_authority=active,
        expected_generation_id=str(request["generation_id"]),
        expected_status="acknowledged",
    )

    assert activated["updated"] is True
    assert activated["execution_egress_enabled"] is True
    state = service.get_state()
    assert state["execution_egress_enabled"] is True
    assert state["execution_egress_authority"] == {
        "schema_version": "fxstack_execution_egress_authority_v1",
        "enabled": True,
        "reason": "externally_witnessed_runner_ack",
        "generation_id": request["generation_id"],
        "request_sha256": request["request_sha256"],
        "runtime_boot_id": "boot-1",
        "updated_at": state["execution_egress_authority"]["updated_at"],
    }
    with service.store.engine.begin() as conn:
        persisted = conn.execute(
            select(service.store.runtime_state.c.snapshot_json).where(
                service.store.runtime_state.c.id == 1
            )
        ).scalar_one()
    assert persisted["execution_egress_enabled"] is True
