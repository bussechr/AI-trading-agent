from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time

import pytest
from sqlalchemy import event, select, update

from fxstack.runtime import release_authority
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.dto import SUPPORTED_COMMANDS, ExecutionAck, ExecutionCommand
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


def _insert_legacy_command(
    store: PostgresRuntimeStore,
    payload: dict[str, object],
    *,
    status: str = "queued",
    delivered_count: int = 0,
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
                status=str(status),
                created_at=cmd.created_at,
                updated_at=cmd.updated_at,
                expires_at=cmd.expires_at,
                delivered_count=int(delivered_count),
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


def test_staged_safe_poll_preserves_reducers_and_quarantines_other_legacy_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fresh_service(tmp_path, monkeypatch)
    for command in sorted(SUPPORTED_COMMANDS):
        _insert_legacy_command(service.store, _payload_for(command, suffix="legacy"))

    polled, status_code = service.poll_command(as_line=False)

    assert status_code == 200
    assert polled == {"status": "empty"}
    rows = service.get_commands(limit=20)
    assert {str(row["cmd"]) for row in rows} == SUPPORTED_COMMANDS
    rows_by_command = {str(row["cmd"]): row for row in rows}
    assert {
        command
        for command, row in rows_by_command.items()
        if str(row["status"]) == "queued"
    } == {"CLOSE", "CLOSE_ALL", "CLOSE_PARTIAL"}
    assert {
        command
        for command, row in rows_by_command.items()
        if str(row["status"]) == "expired"
    } == {"BUY", "SELL", "MODIFY_SL", "INFO"}
    assert all(
        str(row["reason"]).startswith("poll_egress_revoked:execution_egress_disabled")
        for row in rows_by_command.values()
        if str(row["status"]) == "expired"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {
            "command_id": "new-scalp-intent",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.01,
            "intent": "scalp_live_entry",
        },
        {
            "command_id": "scalp:EURUSD:12345",
            "cmd": "SELL",
            "symbol": "EURUSD",
            "lots": 0.01,
        },
        {
            "command_id": "new-scalp-metadata",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.01,
            "scalp_config_sha256": "a" * 64,
        },
    ],
)
def test_store_rejects_new_identifiable_scalp_entries_before_insert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    service = _fresh_service(tmp_path, monkeypatch)
    command = ExecutionCommand.from_payload(
        payload,
        default_session_id="egress-test",
        ttl_secs=120.0,
    )

    accepted, reason = service.store.enqueue_command(command)

    assert accepted is False
    assert (
        reason == "execution_egress_scalp_live_ingress_disabled_unvalidated_authority"
    )
    assert service.get_command(command.command_id) is None


def test_poll_quarantines_legacy_scalp_entries_and_keeps_bare_late_ack_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _fresh_service(tmp_path, monkeypatch)
    store = service.store
    # Isolate this regression from the ordinary release and entry-authority
    # predicates: the disabled scalp fence must dominate even if those would
    # otherwise permit broker delivery.
    store._execution_egress_authorization_failure = (  # type: ignore[method-assign]
        lambda conn, *, now_ts=None, command=None: ""
    )
    store._poll_entry_authorization_failure = (  # type: ignore[method-assign]
        lambda conn, *, row, now_ts: ""
    )
    queued_id = "scalp:EURUSD:legacy-queued"
    delivered_id = "legacy-delivered-scalp"
    _insert_legacy_command(
        store,
        {
            "command_id": queued_id,
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.01,
            "intent": "scalp_live_entry",
        },
    )
    _insert_legacy_command(
        store,
        {
            "command_id": delivered_id,
            "cmd": "SELL",
            "symbol": "EURUSD",
            "lots": 0.01,
            "scalp_config_sha256": "b" * 64,
        },
        status="delivered",
        delivered_count=1,
    )
    protective = ExecutionCommand.from_payload(
        {
            "command_id": "protect-former-scalp-position",
            "cmd": "CLOSE",
            "symbol": "EURUSD",
            "intent": "scalp_live_entry",
            "scalp_config_sha256": "c" * 64,
        },
        default_session_id="egress-test",
        ttl_secs=120.0,
    )
    assert store.enqueue_command(protective) == (True, "queued")

    polled = store.poll_next_command()

    assert polled is not None
    assert polled.command_id == protective.command_id
    queued = store.get_command(queued_id)
    delivered = store.get_command(delivered_id)
    assert queued is not None
    assert delivered is not None
    assert queued["status"] == "expired"
    assert queued["reason"] == (
        "poll_authority_revoked:"
        "execution_egress_scalp_live_ingress_disabled_unvalidated_authority"
    )
    assert delivered["status"] == "reconcile_required"
    assert delivered["reason"] == (
        "poll_authority_revoked:"
        "execution_egress_scalp_live_ingress_disabled_unvalidated_authority:"
        "broker_outcome_unknown"
    )
    assert delivered["delivered_count"] == 1

    out, status_code = store.ack_command(
        ExecutionAck.from_payload(
            {"command_id": delivered_id, "status": "acked", "ticket": 77}
        )
    )
    assert status_code == 200
    assert out["status"] == "reconcile_required"
    assert out["command_id"] == delivered_id
    assert out["reported_status"] == "acked"
    assert "reconciliation_sticky" in out["reasons"]
    assert "ack_actuals_schema_mismatch" in out["reasons"]
    stored = store.get_command(delivered_id)
    assert stored is not None
    assert stored["status"] == "reconcile_required"
    assert stored["ack_json"]["reported_status"] == "acked"
    assert stored["ack_json"]["ticket"] == 77

    queued_events = store.get_command_events(command_id=queued_id, limit=10)
    assert any(
        event["event_status"] == "expired"
        and event["event_json"]["delivery_attempted"] is False
        for event in queued_events
    )
    delivered_events = store.get_command_events(
        command_id=delivered_id,
        limit=10,
    )
    quarantine = next(
        event
        for event in delivered_events
        if event["event_status"] == "reconcile_required"
        and "delivery_attempted" in event["event_json"]
    )
    assert quarantine["event_json"]["delivery_attempted"] is True
    assert quarantine["event_json"]["reconciliation_required"] is True
    late_ack = next(
        event
        for event in delivered_events
        if event["event_status"] == "reconcile_required"
        and event["event_json"].get("reported_status") == "acked"
    )
    assert late_ack["event_json"]["ticket"] == 77
    assert late_ack["event_json"]["store_reconciliation_reasons"] == [
        "reconciliation_sticky"
    ]
    assert not any(event["event_status"] == "acked" for event in delivered_events)


def test_retired_scalp_quarantine_batches_each_transition_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _fresh_service(tmp_path, monkeypatch).store
    expected_ids: set[str] = set()
    for previous_status in ("queued", "delivered"):
        for admission_mode in ("standalone", "direct_demo"):
            for index in range(3):
                command_id = f"batch-{previous_status}-{admission_mode}-{index}"
                expected_ids.add(command_id)
                payload: dict[str, object] = {
                    "command_id": command_id,
                    "cmd": "BUY" if index % 2 == 0 else "SELL",
                    "symbol": "EURUSD",
                    "lots": 0.01,
                }
                if admission_mode == "direct_demo":
                    payload["expected_strategy_admission_mode"] = "direct_demo"
                else:
                    payload["intent"] = "scalp_live_entry"
                _insert_legacy_command(
                    store,
                    payload,
                    status=previous_status,
                    delivered_count=int(previous_status == "delivered"),
                )

    protective_id = "batch-protective-close"
    _insert_legacy_command(
        store,
        {
            "command_id": protective_id,
            "cmd": "CLOSE",
            "symbol": "EURUSD",
            "intent": "scalp_live_entry",
            "scalp_config_sha256": "c" * 64,
        },
    )

    sql_modes: list[bool] = []

    def _capture_sql(
        _conn,
        _cursor,
        _statement,
        _parameters,
        _context,
        executemany,
    ) -> None:
        sql_modes.append(bool(executemany))

    event.listen(store.engine, "before_cursor_execute", _capture_sql)
    try:
        with store.engine.begin() as conn:
            updated = store._quarantine_disabled_scalp_entries(
                conn,
                now_ts=time.time(),
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_sql)

    assert updated == len(expected_ids)
    # One candidate read, four set-based status/reason transitions, and one
    # executemany event append. The number of statements is row-count invariant.
    assert sql_modes == [False, False, False, False, False, True]
    protective = store.get_command(protective_id)
    assert protective is not None
    assert protective["status"] == "queued"

    for command_id in expected_ids:
        stored = store.get_command(command_id)
        assert stored is not None
        delivered = "-delivered-" in command_id
        direct_demo = "-direct_demo-" in command_id
        assert stored["status"] == ("reconcile_required" if delivered else "expired")
        authorization_failure = (
            "scalp_strategy_admission_mode_signed_validation_required"
            if direct_demo
            else "execution_egress_scalp_live_ingress_disabled_unvalidated_authority"
        )
        assert stored["reason"] == (
            f"poll_authority_revoked:{authorization_failure}"
            + (":broker_outcome_unknown" if delivered else "")
        )
        events = store.get_command_events(command_id=command_id, limit=10)
        quarantine = next(
            event for event in events if event["event_status"] == stored["status"]
        )
        assert quarantine["event_json"] == {
            "authorization_failure": authorization_failure,
            "previous_status": "delivered" if delivered else "queued",
            "delivery_attempted": delivered,
            "reconciliation_required": delivered,
        }


def test_poll_reuses_locked_egress_state_for_all_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _fresh_service(tmp_path, monkeypatch).store
    now = time.time()
    with store.engine.begin() as conn:
        conn.execute(
            update(store.runtime_state)
            .where(store.runtime_state.c.id == 1)
            .values(snapshot_json=_active_state(now))
        )

    command_ids = [f"invalid-release-meta-{index:02d}" for index in range(12)]
    for command_id in command_ids:
        _insert_legacy_command(
            store,
            {
                "command_id": command_id,
                "cmd": "INFO",
            },
        )

    runtime_state_reads = 0
    sql_executions = 0

    def _capture_state_reads(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal runtime_state_reads, sql_executions
        sql_executions += 1
        sql = str(statement)
        if "FROM runtime_state" in sql and "snapshot_json" in sql:
            runtime_state_reads += 1

    event.listen(store.engine, "before_cursor_execute", _capture_state_reads)
    try:
        assert store.poll_next_command() is None
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_state_reads)

    assert runtime_state_reads == 1
    assert sql_executions == 7
    for command_id in command_ids:
        stored = store.get_command(command_id)
        assert stored is not None
        assert stored["status"] == "expired"
        assert stored["reason"] == (
            "poll_egress_revoked:"
            "execution_egress_command_release_generation_id_mismatch"
        )

    disabled_state = _active_state(time.time())
    disabled_state["execution_egress_enabled"] = False
    with store.engine.begin() as conn:
        conn.execute(
            update(store.runtime_state)
            .where(store.runtime_state.c.id == 1)
            .values(snapshot_json=disabled_state)
        )
    with store.engine.begin() as conn:
        assert (
            store._execution_egress_authorization_failure(conn)
            == "execution_egress_disabled"
        )


def test_entry_and_egress_checks_share_one_locked_runtime_state_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _fresh_service(tmp_path, monkeypatch).store
    now = time.time()
    state = _active_state(now)
    release = state["release_authority"]
    request = release["request"]
    ack = release["ack"]
    with store.engine.begin() as conn:
        conn.execute(
            update(store.runtime_state)
            .where(store.runtime_state.c.id == 1)
            .values(snapshot_json=state)
        )

    runtime_state_reads = 0

    def _capture_state_reads(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal runtime_state_reads
        sql = str(statement)
        if "FROM runtime_state" in sql and "snapshot_json" in sql:
            runtime_state_reads += 1

    event.listen(store.engine, "before_cursor_execute", _capture_state_reads)
    try:
        with store.engine.begin() as conn:
            assert store._execution_egress_authorization_failure(conn, now_ts=now) == ""
            assert (
                store._live_entry_authorization_failure(
                    conn,
                    pair="EURUSD",
                    expected_account_mode="demo",
                    expected_account_scope="account-1",
                    expected_authority_revision=1,
                    now_ts=now,
                    expected_release_generation_id=str(request["generation_id"]),
                    expected_release_request_sha256=str(request["request_sha256"]),
                    expected_model_identity_sha256=str(
                        request["model_identity_sha256"]
                    ),
                    expected_manifest_file_sha256=str(request["manifest_file_sha256"]),
                    expected_runtime_boot_id=str(ack["runtime_boot_id"]),
                )
                == "live_mode_disabled"
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_state_reads)

    assert runtime_state_reads == 1


def test_poll_flushes_earlier_rejection_before_later_entry_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _fresh_service(tmp_path, monkeypatch).store
    now = time.time()
    state = _active_state(now)
    with store.engine.begin() as conn:
        conn.execute(
            update(store.runtime_state)
            .where(store.runtime_state.c.id == 1)
            .values(snapshot_json=state)
        )

    rejected_id = "earlier-egress-rejection"
    accepted_id = "later-valid-entry"
    _insert_legacy_command(
        store,
        {
            "command_id": rejected_id,
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.01,
        },
    )
    _insert_legacy_command(
        store,
        {
            "command_id": accepted_id,
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.01,
            "orchestration_meta_json": _release_meta(state, sleeve="trend"),
        },
    )

    observed_rejected_statuses: list[str] = []

    def _authorize_later_entry(conn, *, row, now_ts) -> str:
        del row, now_ts
        observed_rejected_statuses.append(
            str(
                conn.execute(
                    select(store.commands.c.status).where(
                        store.commands.c.command_id == rejected_id
                    )
                ).scalar_one()
            )
        )
        return ""

    store._poll_entry_authorization_failure = _authorize_later_entry  # type: ignore[method-assign]

    polled = store.poll_next_command()

    assert polled is not None
    assert polled.command_id == accepted_id
    assert observed_rejected_statuses == ["expired", "expired"]
    rejected = store.get_command(rejected_id)
    assert rejected is not None
    assert rejected["reason"] == (
        "poll_egress_revoked:execution_egress_command_release_generation_id_mismatch"
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda state, now: state["runtime_startup"].update({"boot_id": "boot-2"}),
            "execution_egress_current_boot_mismatch",
        ),
        (
            lambda state, now: state.update({"runtime_last_cycle_ts": now - 31.0}),
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
            lambda state, now: state.update({"broker_account_scope": "account-2"}),
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
    monkeypatch.setattr(
        release_authority, "authority_request_errors", lambda *args, **kwargs: []
    )
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
    monkeypatch.setattr(
        release_authority, "authority_request_errors", lambda *args, **kwargs: []
    )
    monkeypatch.setattr(
        release_authority, "active_authority_errors", lambda *args, **kwargs: []
    )
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

    assert (
        service.compare_and_set_release_authority(next_authority=pending)[
            "execution_egress_enabled"
        ]
        is False
    )
    assert (
        service.compare_and_set_release_authority(
            next_authority=acknowledged,
            expected_generation_id=str(request["generation_id"]),
            expected_status="pending",
        )["execution_egress_enabled"]
        is False
    )
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
