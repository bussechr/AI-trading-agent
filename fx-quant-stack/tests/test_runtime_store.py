from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.service import FinalEntryApproval, RuntimeService


RELEASE_GENERATION_ID = "legacy-release-generation"
RELEASE_REQUEST_SHA256 = "1" * 64
RELEASE_MODEL_IDENTITY_SHA256 = "2" * 64
RELEASE_MANIFEST_FILE_SHA256 = "3" * 64
RELEASE_RUNTIME_BOOT_ID = "legacy-runtime-boot"


@pytest.fixture(autouse=True)
def _isolate_legacy_store_contracts_from_external_release_verification(monkeypatch):
    """Leave cryptographic release verification to its focused test module."""

    from fxstack.runtime import release_authority

    monkeypatch.setattr(
        release_authority,
        "active_authority_errors",
        lambda *args, **kwargs: (),
    )


def _disable_release_egress_fence_for_legacy_queue_test(
    store: PostgresRuntimeStore,
) -> None:
    """Keep legacy queue tests below scoped away from release authority."""

    store._execution_egress_authorization_failure = (  # type: ignore[method-assign]
        lambda conn, *, now_ts=None, command=None: ""
    )


def _seed_legacy_release_identity(store: PostgresRuntimeStore) -> None:
    """Seed identity fields consumed by the older live-admission contract."""

    state = store.get_state()
    state["release_authority"] = {
        "status": "legacy_test_fixture",
        "request": {
            "generation_id": RELEASE_GENERATION_ID,
            "request_sha256": RELEASE_REQUEST_SHA256,
            "model_identity_sha256": RELEASE_MODEL_IDENTITY_SHA256,
            "manifest_file_sha256": RELEASE_MANIFEST_FILE_SHA256,
        },
        "ack": {"runtime_boot_id": RELEASE_RUNTIME_BOOT_ID},
    }
    with store.engine.begin() as conn:
        conn.execute(
            update(store.runtime_state)
            .where(store.runtime_state.c.id == 1)
            .values(snapshot_json=state)
        )


def _fresh_store(
    tmp_path: Path,
    *,
    enforce_entry_poll_authority: bool = False,
    enforce_execution_egress: bool = False,
) -> PostgresRuntimeStore:
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = db_url
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    store = PostgresRuntimeStore(db_url)
    _seed_legacy_release_identity(store)
    # Release/egress authority has a dedicated adversarial test module. This
    # file owns lower-level queue, reconciliation, and legacy live-admission
    # behavior, so bypass only the newly added transaction-local fence on this
    # isolated instance.
    if not enforce_execution_egress:
        _disable_release_egress_fence_for_legacy_queue_test(store)
    if not enforce_entry_poll_authority:
        # Generic queue-lifecycle tests below intentionally exercise legacy
        # rows without constructing the full live authority plane. Production
        # has no bypass switch: this test fixture replaces the instance method
        # only for those isolated store-mechanics tests.
        store._poll_entry_authorization_failure = (  # type: ignore[method-assign]
            lambda conn, *, row, now_ts: ""
        )
    return store


def _service_for_direct_entry_queue_contract(
    store: PostgresRuntimeStore,
) -> RuntimeService:
    service = RuntimeService(database_url=store.database_url)
    _disable_release_egress_fence_for_legacy_queue_test(service.store)
    service._require_entry_approval = False
    service.store._poll_entry_authorization_failure = (  # type: ignore[method-assign]
        lambda conn, *, row, now_ts: ""
    )
    return service


def test_execution_queue_uses_transaction_scoped_postgres_advisory_lock() -> None:
    calls: list[tuple[str, dict[str, int]]] = []

    class _Connection:
        dialect = SimpleNamespace(name="postgresql")

        def execute(self, statement, params):
            calls.append((str(statement), dict(params)))

    store = PostgresRuntimeStore.__new__(PostgresRuntimeStore)
    store._acquire_execution_queue_lock(_Connection())

    assert calls == [
        (
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": PostgresRuntimeStore._EXECUTION_QUEUE_ADVISORY_LOCK_KEY},
        )
    ]


def test_bridge_consumer_lease_is_singleton_and_generation_bound(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    first = store.claim_bridge_consumer_lease(
        consumer_identity="ea-primary",
        terminal_lease_scope="terminal-all-charts",
        credential_generation_id="generation-1",
        channel="poll",
        lease_secs=15.0,
    )
    assert first["ok"] is True
    lease = dict(first["lease"])
    assert lease["poll_authenticated_at"] >= lease["acquired_at"]

    busy = store.claim_bridge_consumer_lease(
        consumer_identity="ea-second",
        terminal_lease_scope="terminal-all-charts",
        credential_generation_id="generation-1",
        channel="poll",
        lease_secs=15.0,
    )
    assert busy == {
        "ok": False,
        "reason": "bridge_consumer_lease_busy",
        "expires_at": lease["expires_at"],
    }

    ack = store.claim_bridge_consumer_lease(
        consumer_identity="ea-primary",
        terminal_lease_scope="terminal-all-charts",
        credential_generation_id="generation-1",
        channel="ack",
        lease_secs=15.0,
    )
    assert ack["ok"] is True
    assert float(ack["lease"]["ack_authenticated_at"]) > 0.0


LIVE_AUTHORITY_REVISION = 1


def _live_authority(**overrides: object) -> dict[str, object]:
    live: dict[str, object] = {
        "authority_revision": LIVE_AUTHORITY_REVISION,
        "enabled": True,
        "mode": "live",
        "runtime_enabled": True,
        "queue_kill_active": False,
        "active_pair_scope": ["EURUSD"],
        "active_sleeve_scope": ["trend"],
        "active_intent_scope": ["enter"],
    }
    live.update(overrides)
    return live


def _required_live_admission() -> dict[str, object]:
    return {
        "pair": "EURUSD",
        "broker_account_mode": "demo",
        "broker_account_scope": "scope-1",
        "authority_revision": LIVE_AUTHORITY_REVISION,
        "release_generation_id": RELEASE_GENERATION_ID,
        "release_request_sha256": RELEASE_REQUEST_SHA256,
        "model_identity_sha256": RELEASE_MODEL_IDENTITY_SHA256,
        "manifest_file_sha256": RELEASE_MANIFEST_FILE_SHA256,
        "runtime_boot_id": RELEASE_RUNTIME_BOOT_ID,
    }


def _live_admission_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "system_status": "connected",
        "last_heartbeat": datetime.now(UTC).timestamp(),
        "broker_account_mode": "demo",
        "broker_account_scope": "scope-1",
        "runtime_diag": {
            "orchestration_live": _live_authority(),
            "live_command_admission": {
                "allowed": True,
                "pairs": {"EURUSD": {"allowed": True}},
            },
        },
    }
    state.update(overrides)
    return state


def test_runtime_cycle_patch_preserves_concurrent_live_authority_and_stage_reset(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    cycle_start_live = {
        "enabled": True,
        "mode": "live",
        "runtime_enabled": True,
        "queue_kill_active": False,
        "queue_kill_reason": "",
        "queue_killed_at": 0.0,
        "active_pair_scope": ["EURUSD"],
        "active_sleeve_scope": ["trend"],
        "active_intent_scope": ["enter"],
        "current_stage_index": 0,
        "current_stage_pct": 1,
        "budget_scale": 0.01,
        "release_status": "canary_active",
        "bundle_run_id": "bundle-a",
        "entry_ratio_vs_baseline": 1.0,
        "entry_ratio_evaluable": True,
        "entry_ratio_status": "observed",
        "entry_ratio_approved_count": 1,
        "entry_ratio_submitted_count": 1,
        "entry_ratio_accepted_count": 1,
        "entry_ratio_observed_at": 100.0,
        "entry_ratio_stage_index": 0,
        "entry_ratio_stage_pct": 1,
        "entry_evidence_by_pair": {"EURUSD": {"approved_count": 1}},
        "graph_fault_count": 0,
    }
    store.update_state_patch(
        {"runtime_diag": {"orchestration_live": dict(cycle_start_live)}}
    )

    concurrently_updated_live = {
        **cycle_start_live,
        "queue_kill_active": True,
        "queue_kill_reason": "operator_kill",
        "queue_killed_at": 200.0,
        "current_stage_index": 1,
        "current_stage_pct": 5,
        "budget_scale": 0.05,
        "release_status": "canary_paused",
        "bundle_run_id": "bundle-b",
        "entry_ratio_vs_baseline": 0.0,
        "entry_ratio_evaluable": False,
        "entry_ratio_status": "insufficient_evidence",
        "entry_ratio_approved_count": 0,
        "entry_ratio_submitted_count": 0,
        "entry_ratio_accepted_count": 0,
        "entry_ratio_observed_at": 0.0,
        "entry_ratio_stage_index": 1,
        "entry_ratio_stage_pct": 5,
        "entry_evidence_by_pair": {},
    }
    store.update_state_patch(
        {"runtime_diag": {"orchestration_live": concurrently_updated_live}}
    )

    stale_cycle_live = {
        **cycle_start_live,
        "graph_fault_count": 7,
    }
    store.update_state_patch(
        {
            "__expected_orchestration_live_authority__": dict(cycle_start_live),
            "runtime_diag": {
                "orchestration_live": stale_cycle_live,
                "cycle_telemetry": {"loop": 2},
            },
        }
    )

    state = store.get_state()
    live = state["runtime_diag"]["orchestration_live"]
    assert live["queue_kill_active"] is True
    assert live["queue_kill_reason"] == "operator_kill"
    assert live["current_stage_index"] == 1
    assert live["current_stage_pct"] == 5
    assert live["budget_scale"] == 0.05
    assert live["release_status"] == "canary_paused"
    assert live["bundle_run_id"] == "bundle-b"
    assert live["entry_ratio_evaluable"] is False
    assert live["entry_ratio_approved_count"] == 0
    assert live["entry_evidence_by_pair"] == {}
    assert live["graph_fault_count"] == 7
    assert state["runtime_diag"]["cycle_telemetry"] == {"loop": 2}
    assert "__expected_orchestration_live_authority__" not in state


def test_runtime_cycle_patch_applies_when_live_authority_is_unchanged(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    expected_live = {
        "enabled": False,
        "mode": "shadow",
        "runtime_enabled": True,
        "queue_kill_active": False,
        "active_pair_scope": [],
        "active_sleeve_scope": [],
        "active_intent_scope": [],
        "current_stage_index": 0,
        "current_stage_pct": 1,
        "budget_scale": 0.01,
        "release_status": "shadow",
        "bundle_run_id": "",
    }
    store.update_state_patch(
        {"runtime_diag": {"orchestration_live": dict(expected_live)}}
    )
    expected_live = dict(
        store.get_state()["runtime_diag"]["orchestration_live"]
    )

    store.update_state_patch(
        {
            "__expected_orchestration_live_authority__": dict(expected_live),
            "runtime_diag": {
                "orchestration_live": {
                    **expected_live,
                    "enabled": True,
                    "mode": "live",
                    "active_pair_scope": ["EURUSD"],
                    "active_sleeve_scope": ["trend"],
                    "active_intent_scope": ["enter"],
                    "graph_fault_count": 2,
                }
            },
        }
    )

    state = store.get_state()
    live = state["runtime_diag"]["orchestration_live"]
    assert live["enabled"] is True
    assert live["mode"] == "live"
    assert live["active_pair_scope"] == ["EURUSD"]
    assert live["active_sleeve_scope"] == ["trend"]
    assert live["active_intent_scope"] == ["enter"]
    assert live["graph_fault_count"] == 2
    assert "__expected_orchestration_live_authority__" not in state


def test_atomic_live_authority_kill_dominates_stale_ramp_and_requires_explicit_start(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(_live_admission_state())
    before_kill = dict(
        store.get_state()["runtime_diag"]["orchestration_live"]
    )

    killed = store.patch_orchestration_live_state(
        updates={
            "runtime_enabled": False,
            "queue_kill_active": True,
            "queue_kill_reason": "operator_kill",
        },
        expected_live_authority=None,
        safety_dominant=True,
    )

    assert killed["authority_revision"] == before_kill["authority_revision"] + 1
    assert killed["runtime_enabled"] is False
    assert killed["queue_kill_active"] is True
    with pytest.raises(RuntimeError, match="orchestration_live_authority_conflict"):
        store.patch_orchestration_live_state(
            updates={
                "runtime_enabled": True,
                "queue_kill_active": False,
                "current_stage_index": 1,
                "current_stage_pct": 5,
            },
            expected_live_authority=before_kill,
        )
    with pytest.raises(
        RuntimeError,
        match="orchestration_live_reenable_requires_start",
    ):
        store.patch_orchestration_live_state(
            updates={"runtime_enabled": True, "queue_kill_active": False},
            expected_live_authority=killed,
        )

    restarted = store.patch_orchestration_live_state(
        updates={
            "enabled": True,
            "mode": "live",
            "runtime_enabled": True,
            "queue_kill_active": False,
            "queue_kill_reason": "",
        },
        expected_live_authority=killed,
        allow_reenable=True,
    )
    assert restarted["authority_revision"] == killed["authority_revision"] + 1
    assert restarted["runtime_enabled"] is True
    assert restarted["queue_kill_active"] is False


def test_queued_entry_cannot_revive_after_a_new_live_authority_generation(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path, enforce_entry_poll_authority=True)
    store.update_state_patch(_live_admission_state())
    _record_fresh_eurusd_tick(store)
    assert store.enqueue_command(
        _account_bound_entry("poll-old-authority-generation"),
        required_live_admission=_required_live_admission(),
    ) == (True, "queued")

    killed = store.patch_orchestration_live_state(
        updates={
            "runtime_enabled": False,
            "queue_kill_active": True,
            "queue_kill_reason": "operator_kill",
        },
        expected_live_authority=None,
        safety_dominant=True,
    )
    restarted = store.patch_orchestration_live_state(
        updates={
            "enabled": True,
            "mode": "live",
            "runtime_enabled": True,
            "queue_kill_active": False,
            "queue_kill_reason": "",
        },
        expected_live_authority=killed,
        allow_reenable=True,
    )
    assert restarted["authority_revision"] > LIVE_AUTHORITY_REVISION

    assert store.poll_next_command() is None
    row = store.get_command("poll-old-authority-generation")
    assert row is not None
    assert row["status"] == "expired"
    assert row["reason"] == (
        "poll_authority_revoked:live_authority_revision_changed"
    )


def _record_fresh_eurusd_tick(store: PostgresRuntimeStore) -> None:
    store.record_tick(
        {
            "symbol": "EURUSD",
            "bid": 1.1000,
            "ask": 1.1002,
            "spread": 0.0002,
        }
    )


def test_production_runtime_authority_does_not_require_external_release(
    tmp_path: Path,
) -> None:
    store = _fresh_store(
        tmp_path,
        enforce_entry_poll_authority=True,
        enforce_execution_egress=True,
    )
    now = datetime.now(UTC).timestamp()
    store.update_state_patch(
        {
            **_live_admission_state(),
            "runtime_status": "running",
            "runtime_last_cycle_ts": now,
            "runtime_startup": {"boot_id": RELEASE_RUNTIME_BOOT_ID},
            "runtime_attestation": {"runtime_boot_id": RELEASE_RUNTIME_BOOT_ID},
        }
    )
    _record_fresh_eurusd_tick(store)
    enabled = store.enable_production_execution_egress(
        runtime_boot_id=RELEASE_RUNTIME_BOOT_ID,
    )
    assert enabled["execution_egress_enabled"] is True
    assert enabled["source"] == "production_runtime"

    command = ExecutionCommand.from_payload(
        {
            "command_id": "production-owned-entry",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
            "expected_account_mode": "demo",
            "expected_account_scope": "scope-1",
            "expected_authority_revision": LIVE_AUTHORITY_REVISION,
            "orchestration_meta_json": {"adaptive_sleeve": "trend"},
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    queued, status = store.enqueue_command(
        command,
        require_resolved_execution=True,
        required_live_admission={
            "pair": "EURUSD",
            "broker_account_mode": "demo",
            "broker_account_scope": "scope-1",
            "authority_revision": LIVE_AUTHORITY_REVISION,
        },
    )
    assert (queued, status) == (True, "queued")
    delivered = store.poll_next_command()
    assert delivered is not None
    assert delivered.command_id == "production-owned-entry"


def _account_bound_entry(command_id: str) -> ExecutionCommand:
    return ExecutionCommand.from_payload(
        {
            "command_id": command_id,
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
            "expected_account_mode": "demo",
            "expected_account_scope": "scope-1",
            "expected_authority_revision": LIVE_AUTHORITY_REVISION,
            "expected_release_generation_id": RELEASE_GENERATION_ID,
            "expected_release_request_sha256": RELEASE_REQUEST_SHA256,
            "expected_model_identity_sha256": RELEASE_MODEL_IDENTITY_SHA256,
            "expected_manifest_file_sha256": RELEASE_MANIFEST_FILE_SHA256,
            "expected_runtime_boot_id": RELEASE_RUNTIME_BOOT_ID,
        },
        default_session_id="unit",
        ttl_secs=120,
    )


def test_enqueue_revalidates_live_authority_inside_the_queue_transaction(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(_live_admission_state())
    _record_fresh_eurusd_tick(store)

    ok, status = store.enqueue_command(
        _account_bound_entry("atomic-live-ready"),
        required_live_admission=_required_live_admission(),
    )

    assert (ok, status) == (True, "queued")


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (
            _live_admission_state(
                runtime_diag={
                    "orchestration_live": _live_authority(
                        runtime_enabled=False,
                    ),
                    "live_command_admission": {
                        "allowed": True,
                        "pairs": {"EURUSD": {"allowed": True}},
                    },
                }
            ),
            "live_runtime_killed",
        ),
        (
            _live_admission_state(
                runtime_diag={
                    "orchestration_live": _live_authority(
                        queue_kill_active=True,
                    ),
                    "live_command_admission": {
                        "allowed": True,
                        "pairs": {"EURUSD": {"allowed": True}},
                    },
                }
            ),
            "live_queue_killed",
        ),
        (_live_admission_state(broker_account_mode="real"), "broker_account_mode_changed"),
        (_live_admission_state(broker_account_scope="scope-2"), "broker_account_scope_changed"),
    ],
)
def test_enqueue_fails_closed_when_authority_changes_after_approval(
    tmp_path: Path,
    state: dict[str, object],
    expected_status: str,
) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(state)
    _record_fresh_eurusd_tick(store)

    ok, status = store.enqueue_command(
        _account_bound_entry(f"atomic-live-blocked-{expected_status}"),
        required_live_admission=_required_live_admission(),
    )

    assert (ok, status) == (False, expected_status)
    assert store.get_command(f"atomic-live-blocked-{expected_status}") is None


@pytest.mark.parametrize(
    ("state", "expected_status"),
    [
        (
            _live_admission_state(
                last_heartbeat=datetime.now(UTC).timestamp() - 120.0,
            ),
            "broker_heartbeat_stale",
        ),
        (
            _live_admission_state(system_status="disconnected"),
            "broker_heartbeat_disconnected",
        ),
    ],
)
def test_enqueue_fails_closed_without_fresh_broker_transport(
    tmp_path: Path,
    state: dict[str, object],
    expected_status: str,
) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(state)
    _record_fresh_eurusd_tick(store)

    ok, status = store.enqueue_command(
        _account_bound_entry(f"atomic-transport-{expected_status}"),
        required_live_admission=_required_live_admission(),
    )

    assert (ok, status) == (False, expected_status)


def test_enqueue_fails_closed_without_current_pair_tick(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(_live_admission_state())

    ok, status = store.enqueue_command(
        _account_bound_entry("atomic-no-tick"),
        required_live_admission=_required_live_admission(),
    )

    assert (ok, status) == (False, "market_tick_missing")


def test_enqueue_uses_broker_tick_event_time_for_freshness(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(_live_admission_state())
    store.record_tick(
        {
            "symbol": "EURUSD",
            "bid": 1.1000,
            "ask": 1.1002,
            "time": datetime.fromtimestamp(
                datetime.now(UTC).timestamp() - 120.0,
                tz=UTC,
            ).isoformat(),
        }
    )

    ok, status = store.enqueue_command(
        _account_bound_entry("atomic-stale-broker-tick"),
        required_live_admission=_required_live_admission(),
    )

    assert (ok, status) == (False, "market_tick_stale")


def test_approved_entry_service_reports_stale_transport_as_unavailable(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(
        database_url=store.database_url,
        execution_provider="mt4",
    )
    _disable_release_egress_fence_for_legacy_queue_test(service.store)
    service.patch_state(_live_admission_state())
    payload = {
        "command_id": "approved-entry-no-tick",
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "sl_price": 1.09,
        "tp_price": 1.12,
        "correlation_id": "EURUSD:approved:no-tick",
        "trace_id": "trace-approved-no-tick",
        "orchestration_meta_json": {
            "trace_id": "trace-approved-no-tick",
            "authority_revision": LIVE_AUTHORITY_REVISION,
            "release_generation_id": RELEASE_GENERATION_ID,
            "release_request_sha256": RELEASE_REQUEST_SHA256,
            "release_model_identity_sha256": RELEASE_MODEL_IDENTITY_SHA256,
            "release_manifest_file_sha256": RELEASE_MANIFEST_FILE_SHA256,
            "release_runtime_boot_id": RELEASE_RUNTIME_BOOT_ID,
            "adaptive_sleeve": "trend",
        },
    }
    approval = FinalEntryApproval(
        pair="EURUSD",
        side="BUY",
        risk_approved_payload=dict(payload),
        canonical_ready=True,
        governed_allowed=True,
        rollout_active=True,
        rollout_mode="canary",
        rollout_pair_allowlisted=True,
        correlation_id="EURUSD:approved:no-tick",
        trace_id="trace-approved-no-tick",
        broker_account_mode="demo",
        broker_account_scope="scope-1",
        authority_revision=LIVE_AUTHORITY_REVISION,
        release_generation_id=RELEASE_GENERATION_ID,
        release_request_sha256=RELEASE_REQUEST_SHA256,
        model_identity_sha256=RELEASE_MODEL_IDENTITY_SHA256,
        manifest_file_sha256=RELEASE_MANIFEST_FILE_SHA256,
        runtime_boot_id=RELEASE_RUNTIME_BOOT_ID,
        sleeve="trend",
    )

    response, status_code = service.submit_approved_command(
        payload,
        approval=approval,
    )

    assert status_code == 503, response
    assert response["status"] == "unavailable"
    assert response["error"] == "market_tick_missing"
    assert service.get_command("approved-entry-no-tick") is None


def test_poll_reauthorizes_entry_immediately_before_delivery(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path, enforce_entry_poll_authority=True)
    store.update_state_patch(_live_admission_state())
    _record_fresh_eurusd_tick(store)
    assert store.enqueue_command(
        _account_bound_entry("poll-live-ready"),
        required_live_admission=_required_live_admission(),
    ) == (True, "queued")

    delivered = store.poll_next_command()

    assert delivered is not None
    assert delivered.command_id == "poll-live-ready"
    assert delivered.status == "delivered"


@pytest.mark.parametrize(
    ("revoked_state", "expected_reason"),
    [
        (
            _live_admission_state(broker_account_scope="scope-2"),
            "broker_account_scope_changed",
        ),
        (
            _live_admission_state(
                runtime_diag={
                    "orchestration_live": _live_authority(
                        queue_kill_active=True,
                    ),
                    "live_command_admission": {
                        "allowed": True,
                        "pairs": {"EURUSD": {"allowed": True}},
                    },
                }
            ),
            "live_queue_killed",
        ),
        (
            _live_admission_state(
                last_heartbeat=datetime.now(UTC).timestamp() - 120.0,
            ),
            "broker_heartbeat_stale",
        ),
    ],
)
def test_poll_expires_entry_when_live_authority_is_revoked(
    tmp_path: Path,
    revoked_state: dict[str, object],
    expected_reason: str,
) -> None:
    store = _fresh_store(tmp_path, enforce_entry_poll_authority=True)
    store.update_state_patch(_live_admission_state())
    _record_fresh_eurusd_tick(store)
    assert store.enqueue_command(
        _account_bound_entry(f"poll-revoked-{expected_reason}"),
        required_live_admission=_required_live_admission(),
    ) == (True, "queued")
    store.update_state_patch(revoked_state)

    assert store.poll_next_command() is None
    row = store.get_command(f"poll-revoked-{expected_reason}")
    assert row is not None
    assert row["status"] == "expired"
    assert row["reason"] == f"poll_authority_revoked:{expected_reason}"
    events = store.get_command_events(
        command_id=f"poll-revoked-{expected_reason}",
        limit=10,
    )
    assert any(
        event["event_status"] == "expired"
        and event["reason"] == f"poll_authority_revoked:{expected_reason}"
        for event in events
    )


def test_poll_expires_entry_when_current_pair_tick_goes_stale(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path, enforce_entry_poll_authority=True)
    store.update_state_patch(_live_admission_state())
    _record_fresh_eurusd_tick(store)
    assert store.enqueue_command(
        _account_bound_entry("poll-stale-tick"),
        required_live_admission=_required_live_admission(),
    ) == (True, "queued")
    with store.engine.begin() as conn:
        conn.execute(
            update(store.market_ticks).values(
                ts=datetime.now(UTC).timestamp() - 120.0,
            )
        )

    assert store.poll_next_command() is None
    row = store.get_command("poll-stale-tick")
    assert row is not None
    assert row["status"] == "expired"
    assert row["reason"] == "poll_authority_revoked:market_tick_stale"


def test_poll_expires_legacy_unattested_entry_but_delivers_protection(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path, enforce_entry_poll_authority=True)
    store.update_state_patch(_live_admission_state())
    _record_fresh_eurusd_tick(store)
    legacy = ExecutionCommand.from_payload(
        {
            "command_id": "poll-legacy-entry",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    protection = ExecutionCommand.from_payload(
        {
            "command_id": "poll-protective-close",
            "cmd": "CLOSE",
            "symbol": "EURUSD",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(legacy) == (True, "queued")
    assert store.enqueue_command(protection) == (True, "queued")

    delivered = store.poll_next_command()

    assert delivered is not None
    assert delivered.command_id == "poll-protective-close"
    legacy_row = store.get_command("poll-legacy-entry")
    assert legacy_row is not None
    assert legacy_row["status"] == "expired"
    assert legacy_row["reason"] == (
        "poll_authority_revoked:broker_account_mode_unattested"
    )


def test_poll_kill_switch_revokes_entry_without_blocking_protection(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path, enforce_entry_poll_authority=True)
    store.update_state_patch(_live_admission_state())
    _record_fresh_eurusd_tick(store)
    assert store.enqueue_command(
        _account_bound_entry("poll-killed-entry"),
        required_live_admission=_required_live_admission(),
    ) == (True, "queued")
    protection = ExecutionCommand.from_payload(
        {
            "command_id": "poll-killed-protective-close",
            "cmd": "CLOSE",
            "symbol": "EURUSD",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(protection) == (True, "queued")
    store.update_state_patch(
        _live_admission_state(
            runtime_diag={
                "orchestration_live": _live_authority(
                    queue_kill_active=True,
                ),
                "live_command_admission": {
                    "allowed": True,
                    "pairs": {"EURUSD": {"allowed": True}},
                },
            }
        )
    )

    delivered = store.poll_next_command()

    assert delivered is not None
    assert delivered.command_id == "poll-killed-protective-close"
    entry_row = store.get_command("poll-killed-entry")
    assert entry_row is not None
    assert entry_row["status"] == "expired"
    assert entry_row["reason"] == "poll_authority_revoked:live_queue_killed"


def test_command_lifecycle_roundtrip(tmp_path: Path):
    store = _fresh_store(tmp_path)

    cmd = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "c1"},
        default_session_id="unit",
        ttl_secs=120,
    )
    ok, state = store.enqueue_command(cmd)
    assert ok is True
    assert state == "queued"

    polled = store.poll_next_command()
    assert polled is not None
    assert polled.command_id == "c1"
    assert polled.status == "delivered"

    ack_payload = {"command_id": "c1", "status": "acked", "ticket": 11}
    ack = ExecutionAck.from_payload(ack_payload)
    out, code = store.ack_command(ack)
    assert code == 200
    assert out["status"] == "acked"

    events_after_first = store.get_command_events(command_id="c1", limit=20)
    trades_after_first = int(store.get_state().get("trades_executed") or 0)
    replay_out, replay_code = store.ack_command(ExecutionAck.from_payload(ack_payload))
    assert replay_code == 200
    assert replay_out == {"status": "acked", "command_id": "c1", "idempotent": True}
    events_after_replay = store.get_command_events(command_id="c1", limit=20)
    assert sum(event["event_status"] == "acked" for event in events_after_replay) == 1
    assert events_after_replay == events_after_first
    assert trades_after_first == 1
    assert int(store.get_state().get("trades_executed") or 0) == trades_after_first

    row = store.get_command("c1")
    assert row is not None
    assert str(row["status"]) == "acked"


def test_future_dated_legacy_command_is_neither_active_nor_pollable(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    now = datetime.now(UTC).timestamp()
    cmd = ExecutionCommand.from_payload(
        {
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "command_id": "future-legacy",
            "idempotency_key": "future-legacy-idem",
        },
        default_session_id="unit",
        ttl_secs=120,
        now_ts=now,
    )
    assert store.enqueue_command(cmd)[0] is True
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == cmd.command_id)
            .values(created_at=now + 3_600.0, expires_at=now + 7_200.0)
        )

    assert store.get_active_command_by_idempotency_key("future-legacy-idem") is None
    assert store.poll_next_command() is None


def test_runtime_service_dedupes_direct_retry_without_command_id(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)

    payload = {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "sl_price": 1.09, "tp_price": 1.12}

    out1, code1 = service.submit_command(dict(payload))
    out2, code2 = service.submit_command(dict(payload))

    assert code1 == 200
    assert code2 == 200
    assert out1["status"] == "queued"
    assert out2["status"] == "duplicate"
    assert out1["command_id"] == out2["command_id"]

    row = store.get_command(out1["command_id"])
    assert row is not None
    assert str(row["status"]) == "queued"


def test_runtime_service_dedupes_duplicate_explicit_command_id(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)

    payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "sl_price": 1.09,
        "tp_price": 1.12,
        "command_id": "explicit-dup",
        "idempotency_key": "idem-1",
    }

    out1, code1 = service.submit_command(dict(payload))
    out2, code2 = service.submit_command(dict(payload))

    assert code1 == 200
    assert code2 == 200
    assert out1["status"] == "queued"
    assert out2["status"] == "duplicate"
    assert out1["command_id"] == "explicit-dup"
    assert out2["command_id"] == "explicit-dup"

    row = store.get_command("explicit-dup")
    assert row is not None
    assert str(row["status"]) == "queued"
    state = store.get_state()
    assert int(state.get("signals_sent", 0)) == 1
    assert state["last_signal"]["command_id"] == "explicit-dup"


def test_runtime_service_ack_uses_idempotency_key_without_command_id(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)

    payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "sl_price": 1.09,
        "tp_price": 1.12,
        "idempotency_key": "idem-ack-1",
    }
    queued, code = service.submit_command(dict(payload))
    assert code == 200
    assert queued["status"] == "queued"

    polled = store.poll_next_command()
    assert polled is not None
    assert polled.command_id == queued["command_id"]

    out, ack_code = service.ack_command({"status": "acked", "ticket": 11, "idempotency_key": "idem-ack-1"})
    assert ack_code == 200
    assert out["status"] == "acked"
    assert out["command_id"] == queued["command_id"]
    assert out["idempotency_key"] == "idem-ack-1"

    row = store.get_command(queued["command_id"])
    assert row is not None
    assert str(row["status"]) == "acked"


def test_runtime_service_paper_execution_auto_acks_and_polls_empty(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url, execution_provider="paper")
    _disable_release_egress_fence_for_legacy_queue_test(service.store)
    service.record_tick({"symbol": "EURUSD", "bid": 1.1010, "ask": 1.1012, "spread": 0.0002})

    queued, code = service.submit_command(
        {
            "cmd": "BUY",
            "symbol": "EURUSD",
                "lots": 0.1,
                "sl_price": 1.09,
                "tp_price": 1.12,
                "command_id": "paper-1",
            "correlation_id": "EURUSD:paper:1",
            "thread_id": "EURUSD:paper:1",
            "idempotency_key": "idem-paper-1",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_meta_json": {
                "agent_mode": "paper",
                "run_id": "run-paper-1",
                "trace_id": "trace-paper-1",
            },
        }
    )

    assert code == 200
    assert queued["status"] == "queued"
    assert queued["execution_provider"] == "paper"
    assert queued["paper_execution"]["delivery"]["status"] == "delivered"
    assert queued["paper_execution"]["ack"]["status"] == "acked"

    row = store.get_command("paper-1")
    assert row is not None
    assert str(row["status"]) == "acked"
    ack_json = dict(row["ack_json"] or {})
    assert ack_json["status"] == "acked"
    assert ack_json["orchestration_meta_json"]["paper_fill_source"] in {"ask", "mid"}

    events = store.get_command_events(command_id="paper-1", limit=10)
    statuses = {str(item["event_status"]) for item in events}
    assert {"queued", "delivered", "acked"} <= statuses

    polled, poll_code = service.poll_command(as_line=False)
    assert poll_code == 200
    assert polled["status"] == "empty"
    assert polled["execution_provider"] == "paper"


def test_runtime_service_paper_execution_uses_persisted_mid_only_tick(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url, execution_provider="paper")
    _disable_release_egress_fence_for_legacy_queue_test(service.store)
    service.record_tick({"symbol": "EURUSD", "bid": None, "ask": None, "mid": 1.2345})

    queued, code = service.submit_command(
        {
            "command_id": "paper-mid-only",
            "cmd": "BUY",
            "symbol": "EURUSD",
                "lots": 0.1,
                "sl_price": 1.23,
                "tp_price": 1.24,
            }
    )

    assert code == 200
    assert queued["paper_execution"]["fill_price"] == 1.2345
    assert queued["paper_execution"]["fill_source"] == "mid"
    latest = service.get_latest_tick("EURUSD")
    assert latest is not None
    assert latest["bid"] is None
    assert latest["ask"] is None
    assert latest["mid"] == 1.2345


def test_runtime_service_paper_execution_reports_paper_provider_health(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url, execution_provider="paper")
    _disable_release_egress_fence_for_legacy_queue_test(service.store)

    service.patch_state(
        {
            "runtime_diag": {
                "provider_roles": {
                    "history_provider": "dukascopy",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "paper",
                },
                "provider_health": {
                    "execution_provider": {
                        "provider": "paper",
                        "role": "execution",
                        "status": "ok",
                        "shadow_only": True,
                        "provenance": "runtime_service",
                        "details": {"execution_provider": "paper", "paused": False, "entries_only": False},
                    }
                },
            }
        }
    )

    state = store.get_state()
    assert state["runtime_diag"]["provider_roles"]["execution_provider"] == "paper"
    assert state["runtime_diag"]["provider_health"]["execution_provider"]["provider"] == "paper"
    assert state["runtime_diag"]["provider_health"]["execution_provider"]["status"] == "ok"


def test_duplicate_ack_does_not_increment_trade_counter(tmp_path: Path):
    store = _fresh_store(tmp_path)

    cmd = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "dup1"},
        default_session_id="unit",
        ttl_secs=120,
    )
    ok, _ = store.enqueue_command(cmd)
    assert ok is True
    polled = store.poll_next_command()
    assert polled is not None

    ack = ExecutionAck.from_payload({"command_id": "dup1", "status": "duplicate", "ticket": -1, "message": "duplicate_suppressed"})
    out, code = store.ack_command(ack)
    assert code == 200
    assert out["status"] == "duplicate"

    state = store.get_state()
    assert int(state.get("trades_executed", 0)) == 0


def test_constructor_does_not_mutate_delivered_command(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    store = _fresh_store(tmp_path)

    cmd = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "delivered1"},
        default_session_id="unit",
        ttl_secs=120,
    )
    ok, _ = store.enqueue_command(cmd)
    assert ok is True
    polled = store.poll_next_command()
    assert polled is not None
    assert polled.status == "delivered"

    restarted = PostgresRuntimeStore(db_url)
    row = restarted.get_command("delivered1")
    assert row is not None
    assert str(row["status"]) == "delivered"


def test_purge_pending_commands_expires_only_pending_rows(tmp_path: Path):
    store = _fresh_store(tmp_path)

    delivered = ExecutionCommand.from_payload(
        {"cmd": "SELL", "symbol": "GBPUSD", "lots": 0.1, "command_id": "delivered2"},
        default_session_id="unit",
        ttl_secs=120,
    )
    queued = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "queued1"},
        default_session_id="unit",
        ttl_secs=120,
    )
    acked = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "USDJPY", "lots": 0.1, "command_id": "acked1"},
        default_session_id="unit",
        ttl_secs=120,
    )

    # Resolve the terminal fixture before creating the deliberately unresolved
    # delivery; once a delivery is unresolved the poll fence must not release
    # another queued BUY/SELL.
    ok, _ = store.enqueue_command(acked)
    assert ok is True
    ack_polled = store.poll_next_command()
    assert ack_polled is not None
    assert ack_polled.command_id == "acked1"
    store.ack_command(ExecutionAck.from_payload({"command_id": "acked1", "status": "acked", "ticket": 1}))

    ok, _ = store.enqueue_command(delivered)
    assert ok is True
    polled = store.poll_next_command()
    assert polled is not None
    assert polled.command_id == "delivered2"

    ok, _ = store.enqueue_command(queued)
    assert ok is True

    updated = store.purge_pending_commands(reason="runtime_restart_purged")
    assert updated == 2

    queued_row = store.get_command("queued1")
    delivered_row = store.get_command("delivered2")
    acked_row = store.get_command("acked1")
    assert queued_row is not None
    assert delivered_row is not None
    assert acked_row is not None
    assert str(queued_row["status"]) == "expired"
    assert str(queued_row["reason"]) == "runtime_restart_purged"
    assert str(delivered_row["status"]) == "expired"
    assert str(delivered_row["reason"]) == "runtime_restart_purged"
    assert str(acked_row["status"]) == "acked"


def test_command_window_summary_counts_every_row_without_history_cap(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    outside = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "outside-window"},
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(outside)[0] is True
    start_ts = datetime.now(UTC).timestamp()

    commands = [
        ExecutionCommand.from_payload(
            {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "window-buy"},
            default_session_id="unit",
            ttl_secs=120,
        ),
        ExecutionCommand.from_payload(
            {"cmd": "SELL", "symbol": "GBPUSD", "lots": 0.1, "command_id": "window-sell"},
            default_session_id="unit",
            ttl_secs=120,
        ),
        ExecutionCommand.from_payload(
            {"cmd": "CLOSE", "symbol": "EURUSD", "command_id": "window-close"},
            default_session_id="unit",
            ttl_secs=120,
        ),
    ]
    for command in commands:
        assert store.enqueue_command(command)[0] is True
    end_ts = datetime.now(UTC).timestamp()

    summary = store.get_command_window_summary(start_ts=start_ts, end_ts=end_ts)
    assert summary["schema_version"] == "fxstack_command_window_summary_v1"
    assert summary["window_complete"] is True
    assert summary["total_commands"] == 3
    assert summary["entry_commands"] == 2
    assert summary["control_commands"] == 1
    assert summary["status_counts"] == {"queued": 3}
    assert summary["command_counts"] == {"BUY": 1, "CLOSE": 1, "SELL": 1}
    assert summary["first_created_at"] >= start_ts
    assert summary["last_created_at"] <= end_ts

    with pytest.raises(ValueError, match="invalid_command_window"):
        store.get_command_window_summary(start_ts=end_ts, end_ts=start_ts)


def test_restart_recovery_quarantines_delivered_without_redelivery(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url)
    _disable_release_egress_fence_for_legacy_queue_test(service.store)

    delivered = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "delivered-recover"},
        default_session_id="unit",
        ttl_secs=120,
    )
    queued = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "USDCHF", "lots": 0.1, "command_id": "queued-recover"},
        default_session_id="unit",
        ttl_secs=120,
    )

    ok, _ = store.enqueue_command(delivered)
    assert ok is True
    polled = store.poll_next_command()
    assert polled is not None
    assert polled.command_id == "delivered-recover"

    ok, _ = store.enqueue_command(queued)
    assert ok is True

    old_ts = datetime.now(UTC).timestamp() - 300.0
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == "delivered-recover")
            .values(updated_at=old_ts)
        )
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == "queued-recover")
            .values(updated_at=old_ts)
        )

    purged = service.purge_pending_commands(reason="runtime_restart_purged", include_delivered=False)
    quarantined = service.quarantine_stale_delivered(age_secs=60.0)

    assert purged == 1
    assert quarantined == 1

    delivered_row = store.get_command("delivered-recover")
    queued_row = store.get_command("queued-recover")
    assert delivered_row is not None
    assert queued_row is not None
    assert str(delivered_row["status"]) == "reconcile_required"
    assert str(delivered_row["reason"]) == "stale_delivery_outcome_unknown"
    assert int(delivered_row["delivered_count"]) == 1
    assert str(queued_row["status"]) == "expired"
    assert str(queued_row["reason"]) == "runtime_restart_purged"
    assert store.poll_next_command() is None

    events = store.get_command_events(command_id="delivered-recover", limit=10)
    quarantine_event = next(item for item in events if item["event_status"] == "reconcile_required")
    assert quarantine_event["reason"] == "stale_delivery_outcome_unknown"
    event_payload = dict(quarantine_event["event_json"])
    assert float(event_payload["quarantined_at"]) > old_ts
    assert event_payload["previous_status"] == "delivered"
    assert event_payload["delivered_count"] == 1
    assert event_payload["reconciliation_required"] is True


def test_quarantined_delivered_command_accepts_late_ack_without_redelivery(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url)
    _disable_release_egress_fence_for_legacy_queue_test(service.store)

    cmd = ExecutionCommand.from_payload(
        {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "late-ack-1"},
        default_session_id="unit",
        ttl_secs=120,
    )
    ok, _ = store.enqueue_command(cmd)
    assert ok is True
    polled = store.poll_next_command()
    assert polled is not None
    assert polled.command_id == "late-ack-1"

    old_ts = datetime.now(UTC).timestamp() - 300.0
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == "late-ack-1")
            .values(updated_at=old_ts)
        )

    quarantined = service.quarantine_stale_delivered(age_secs=60.0)
    assert quarantined == 1
    assert store.poll_next_command() is None

    out, code = store.ack_command(ExecutionAck.from_payload({"command_id": "late-ack-1", "status": "acked", "ticket": 22}))
    assert code == 200
    assert out["status"] == "acked"

    row = store.get_command("late-ack-1")
    assert row is not None
    assert str(row["status"]) == "acked"
    assert int(row["delivered_count"]) == 1
    events = store.get_command_events(command_id="late-ack-1", limit=10)
    statuses = [str(item["event_status"]) for item in events]
    assert statuses.count("delivered") == 1
    assert statuses.count("queued") == 1
    assert "reconcile_required" in statuses
    assert "acked" in statuses


def test_runtime_service_blocks_entries_while_delivery_is_unresolved_but_allows_protection(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)

    queued, code = service.submit_command(
        {
            "command_id": "unresolved-entry-1",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
        }
    )
    assert code == 200
    assert queued["status"] == "queued"
    delivered = service.store.poll_next_command()
    assert delivered is not None
    assert delivered.command_id == "unresolved-entry-1"

    blocked, blocked_code = service.submit_command(
        {
            "command_id": "blocked-entry-2",
            "cmd": "SELL",
            "symbol": "GBPUSD",
            "lots": 0.1,
            "sl_price": 1.31,
            "tp_price": 1.28,
        }
    )
    assert blocked_code == 409
    assert blocked["status"] == "reconciliation_required"
    assert blocked["error"] == "new_exposure_blocked_by_unresolved_execution_outcome"
    assert blocked["execution_uncertainty"]["blocked"] is True
    assert blocked["execution_uncertainty"]["statuses"] == {"delivered": 1}
    assert service.get_command("blocked-entry-2") is None

    protective_payloads = [
        {"command_id": "protect-close", "cmd": "CLOSE", "symbol": "EURUSD"},
        {
            "command_id": "protect-partial",
            "cmd": "CLOSE_PARTIAL",
            "symbol": "EURUSD",
            "close_lots": 0.05,
        },
        {
            "command_id": "protect-stop",
            "cmd": "MODIFY_SL",
            "symbol": "EURUSD",
            "sl_price": 1.095,
        },
        {"command_id": "protect-all", "cmd": "CLOSE_ALL"},
    ]
    for payload in protective_payloads:
        admitted, admitted_code = service.submit_command(payload)
        assert admitted_code == 200
        assert admitted["status"] == "queued"


def test_reconcile_required_fence_clears_only_after_terminal_ack(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    queued, code = service.submit_command(
        {
            "command_id": "reconcile-entry-1",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
        }
    )
    assert code == 200
    assert queued["status"] == "queued"
    assert service.store.poll_next_command() is not None

    old_ts = datetime.now(UTC).timestamp() - 300.0
    with service.store.engine.begin() as conn:
        conn.execute(
            update(service.store.commands)
            .where(service.store.commands.c.command_id == "reconcile-entry-1")
            .values(updated_at=old_ts)
        )
    assert service.quarantine_stale_delivered(age_secs=60.0) == 1
    assert service.get_execution_uncertainty()["statuses"] == {"reconcile_required": 1}

    blocked, blocked_code = service.submit_command(
        {
            "command_id": "reconcile-entry-2",
            "cmd": "SELL",
            "symbol": "GBPUSD",
            "lots": 0.1,
            "sl_price": 1.31,
            "tp_price": 1.28,
        }
    )
    assert blocked_code == 409
    assert blocked["status"] == "reconciliation_required"

    acked, ack_code = service.ack_command(
        {"command_id": "reconcile-entry-1", "status": "acked", "ticket": 41}
    )
    assert ack_code == 200
    assert acked["status"] == "acked"
    assert service.get_execution_uncertainty()["blocked"] is False

    admitted, admitted_code = service.submit_command(
        {
            "command_id": "reconcile-entry-2",
            "cmd": "SELL",
            "symbol": "GBPUSD",
            "lots": 0.1,
            "sl_price": 1.31,
            "tp_price": 1.28,
        }
    )
    assert admitted_code == 200
    assert admitted["status"] == "queued"


def test_poll_holds_prequeued_entry_behind_unresolved_delivery_but_releases_protection(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    first = ExecutionCommand.from_payload(
        {"command_id": "prequeued-first", "cmd": "BUY", "symbol": "EURUSD", "lots": 0.1},
        default_session_id="unit",
        ttl_secs=120,
    )
    second = ExecutionCommand.from_payload(
        {"command_id": "prequeued-second", "cmd": "SELL", "symbol": "GBPUSD", "lots": 0.1},
        default_session_id="unit",
        ttl_secs=120,
    )
    protection = ExecutionCommand.from_payload(
        {"command_id": "prequeued-close", "cmd": "CLOSE", "symbol": "EURUSD"},
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(first)[0] is True
    assert store.enqueue_command(second)[0] is True
    assert store.enqueue_command(protection)[0] is True

    delivered_first = store.poll_next_command()
    assert delivered_first is not None
    assert delivered_first.command_id == "prequeued-first"
    delivered_protection = store.poll_next_command()
    assert delivered_protection is not None
    assert delivered_protection.command_id == "prequeued-close"
    assert store.poll_next_command() is None

    assert store.ack_command(
        ExecutionAck.from_payload({"command_id": "prequeued-first", "status": "acked", "ticket": 51})
    )[1] == 200
    assert store.ack_command(
        ExecutionAck.from_payload({"command_id": "prequeued-close", "status": "acked", "ticket": 51})
    )[1] == 200
    delivered_second = store.poll_next_command()
    assert delivered_second is not None
    assert delivered_second.command_id == "prequeued-second"


def test_expired_after_delivery_remains_fenced_and_accepts_late_resolution(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    first, first_code = service.submit_command(
        {
            "command_id": "expired-after-delivery",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
        }
    )
    assert first_code == 200
    assert first["status"] == "queued"
    assert service.store.poll_next_command() is not None
    with service.store.engine.begin() as conn:
        conn.execute(
            update(service.store.commands)
            .where(service.store.commands.c.command_id == "expired-after-delivery")
            .values(status="expired", reason="ttl_expired")
        )

    uncertainty = service.get_execution_uncertainty()
    assert uncertainty["blocked"] is True
    assert uncertainty["statuses"] == {"expired": 1}
    blocked, blocked_code = service.submit_command(
        {
            "command_id": "expired-fenced-entry",
            "cmd": "SELL",
            "symbol": "GBPUSD",
            "lots": 0.1,
            "sl_price": 1.31,
            "tp_price": 1.28,
        }
    )
    assert blocked_code == 409
    assert blocked["status"] == "reconciliation_required"

    acked, ack_code = service.ack_command(
        {"command_id": "expired-after-delivery", "status": "acked", "ticket": 61}
    )
    assert ack_code == 200
    assert acked["status"] == "acked"
    assert service.get_execution_uncertainty()["blocked"] is False


def test_entry_admission_fails_closed_when_reconciliation_query_errors(tmp_path: Path, monkeypatch) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)

    def _query_failure(*, limit: int = 20) -> dict[str, object]:
        raise RuntimeError("synthetic query failure")

    monkeypatch.setattr(service.store, "get_execution_uncertainty", _query_failure)
    blocked, code = service.submit_command(
        {
            "command_id": "query-failure-entry",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
        }
    )
    assert code == 503
    assert blocked["status"] == "reconciliation_check_failed"
    assert blocked["error"] == "unable_to_prove_prior_execution_outcomes_resolved"
    assert service.get_command("query-failure-entry") is None


def test_record_runtime_boot_failure_persists_governance_event(tmp_path: Path):
    store = _fresh_store(tmp_path)

    store.record_runtime_boot_failure(
        boot={
            "boot_id": "boot-err-1",
            "booted_at": "2026-03-24T07:00:00+00:00",
            "runtime_pid": 321,
            "phase": "initial_refresh",
            "phase_pair": "CHFJPY",
            "phase_index": 7,
            "phase_total": 18,
            "last_progress_ts": 1774306800.0,
            "failure_reason": "",
            "failed_at": "",
            "pending_command_policy": "purge_and_mark_stale",
        },
        failure_reason="RuntimeError:boom",
        failed_at="2026-03-24T07:00:05+00:00",
        patch={"runtime_status": "failed"},
        prune_state=True,
    )

    events = store.get_governance_events(limit=10)
    assert len(events) >= 1
    event = events[0]
    assert str(event["event_type"]) == "runtime_startup_failed"
    assert str(event["reason"]) == "RuntimeError:boom"
    payload = dict(event["payload_json"] or {})
    assert str(payload["boot_id"]) == "boot-err-1"
    assert str(payload["phase"]) == "initial_refresh"
    assert str(payload["phase_pair"]) == "CHFJPY"
    assert str(payload["failure_reason"]) == "RuntimeError:boom"


def test_command_roundtrip_preserves_phase1_orchestration_fields(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    cmd = ExecutionCommand.from_payload(
        {
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "command_id": "orch-c1",
            "correlation_id": "EURUSD:1:shadow",
            "thread_id": "EURUSD:1:shadow",
            "idempotency_key": "idem-1",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_meta_json": {"run_id": "run-1", "trace_id": "trace-1"},
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    ok, _ = store.enqueue_command(cmd)
    assert ok is True

    polled = store.poll_next_command()
    assert polled is not None
    assert polled.correlation_id == "EURUSD:1:shadow"
    assert polled.thread_id == "EURUSD:1:shadow"
    assert polled.idempotency_key == "idem-1"
    assert polled.schema_version == ORCHESTRATION_SCHEMA_VERSION
    assert polled.orchestration_meta_json["run_id"] == "run-1"


def test_store_orchestration_bundle_and_query_endpoints(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    run_id = str(uuid4())
    trace_id = "trace-1"
    context = {
        "run_id": run_id,
        "cycle_id": "123",
        "thread_id": "EURUSD:123:shadow",
        "correlation_id": "EURUSD:123:shadow",
        "ts_utc": datetime(2026, 4, 8, 12, 0, tzinfo=UTC).isoformat(),
        "pair": "EURUSD",
        "runtime_mode": "shadow",
        "version_bundle": {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "policy_version": "fxstack_policy_v1",
            "model_bundle_version": "bundle-v1",
            "orchestrator_version": ORCHESTRATION_SCHEMA_VERSION,
        },
    }
    packet = {
        "packet_id": str(uuid4()),
        "run_id": run_id,
        "pair": "EURUSD",
        "ts_utc": context["ts_utc"],
        "baseline_action": {"action": "no_trade"},
        "shadow_action": {"action": "no_trade", "side": "FLAT"},
        "divergence_reason": "agree",
        "proposal_votes": {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}},
        "fault_classification": None,
        "proposals": [],
        "governed_decision": {
            "decision_id": str(uuid4()),
            "run_id": run_id,
            "allowed": False,
            "selected_action": "no_trade",
            "command_preview": None,
            "blocking_reasons": ["shadow_only"],
            "approval_state": "auto",
            "governor_version": ORCHESTRATION_SCHEMA_VERSION,
            "invariants_ok": True,
        },
        "latency_ms": 4,
        "fallback_used": False,
        "trace_id": trace_id,
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
    }
    trace = {
        "trace_id": trace_id,
        "run_id": run_id,
        "node_spans": [{"node": "noop", "latency_ms": 4}],
        "tool_calls": [],
        "model_calls": [],
        "persistence_refs": [f"run://{run_id}"],
        "prompt_hashes": [],
        "input_hash": "sha256:in",
        "output_hash": "sha256:out",
        "error_class": None,
        "created_at": context["ts_utc"],
        "checkpoint": {"thread_id": context["thread_id"], "checkpoint": {}},
    }
    store.store_orchestration_bundle(
        context=context,
        packet=packet,
        trace=trace,
        runtime_mode="shadow",
        fallback_used=False,
    )

    runs = store.get_orchestration_runs(limit=10, pair="EURUSD", runtime_mode="shadow", cycle_id="123")
    traces = store.get_orchestration_traces(limit=10, run_id=run_id, pair="EURUSD")
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id
    assert runs[0]["correlation_id"] == "EURUSD:123:shadow"
    assert dict(runs[0]["packet_json"] or {})["shadow_action"]["action"] == "no_trade"
    assert len(traces) == 1
    assert traces[0]["trace_id"] == trace_id
    trace_json = dict(traces[0]["trace_json"] or {})
    checkpoint = dict(trace_json.get("checkpoint") or {})
    assert checkpoint["thread_id"] == "EURUSD:123:shadow"

    with store.engine.begin() as conn:
        governed = conn.execute(
            select(store.governed_decisions).where(store.governed_decisions.c.run_id == run_id)
        ).mappings().first()
    assert governed is not None
    assert str(governed["runtime_mode"]) == "shadow"


def test_store_orchestration_bundle_normalizes_packet_fallback_used(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    run_id = str(uuid4())
    context = {
        "run_id": run_id,
        "cycle_id": "124",
        "thread_id": "EURUSD:124:shadow",
        "correlation_id": "EURUSD:124:shadow",
        "ts_utc": datetime(2026, 4, 8, 12, 1, tzinfo=UTC).isoformat(),
        "pair": "EURUSD",
        "runtime_mode": "shadow",
        "version_bundle": {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "policy_version": "fxstack_policy_v1",
            "model_bundle_version": "bundle-v1",
            "orchestrator_version": ORCHESTRATION_SCHEMA_VERSION,
        },
    }
    packet = {
        "packet_id": str(uuid4()),
        "run_id": run_id,
        "pair": "EURUSD",
        "ts_utc": context["ts_utc"],
        "baseline_action": {"action": "enter"},
        "shadow_action": {"action": "no_trade", "side": "FLAT"},
        "divergence_reason": "shadow_fault",
        "proposal_votes": {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}},
        "fault_classification": "latency_budget_exceeded",
        "proposals": [],
        "governed_decision": {
            "decision_id": str(uuid4()),
            "run_id": run_id,
            "allowed": False,
            "selected_action": "no_trade",
            "command_preview": None,
            "blocking_reasons": ["latency_budget_exceeded"],
            "approval_state": "auto",
            "governor_version": ORCHESTRATION_SCHEMA_VERSION,
            "invariants_ok": True,
        },
        "latency_ms": 9,
        "fallback_used": False,
        "trace_id": "trace-fallback",
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
    }
    trace = {
        "trace_id": "trace-fallback",
        "run_id": run_id,
        "node_spans": [],
        "tool_calls": [],
        "model_calls": [],
        "persistence_refs": [f"run://{run_id}"],
        "prompt_hashes": [],
        "input_hash": "sha256:in",
        "output_hash": "sha256:out",
        "error_class": "latency_budget_exceeded",
        "created_at": context["ts_utc"],
        "checkpoint": {"thread_id": context["thread_id"], "checkpoint": {}},
    }
    store.store_orchestration_bundle(
        context=context,
        packet=packet,
        trace=trace,
        runtime_mode="shadow",
        fallback_used=True,
    )

    runs = store.get_orchestration_runs(limit=1, pair="EURUSD", runtime_mode="shadow", cycle_id="124")
    assert len(runs) == 1
    latest_packet = dict(runs[0]["packet_json"] or {})
    assert runs[0]["fallback_used"] in {1, True}
    assert latest_packet["fallback_used"] is True


def test_experiment_proposal_promotion_and_lineage_roundtrip(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    experiment_id = str(uuid4())
    proposal = store.upsert_experiment_proposal(
        {
            "experiment_id": experiment_id,
            "source_run_id": str(uuid4()),
            "hypothesis": "phase7 promotion ledger roundtrip",
            "change_set": [{"path": "fxstack/runtime/postgres_store.py", "change": "add lineage"}],
            "evaluation_plan": {"replay": "golden-pack"},
            "risk_notes": ["keep prompt text out of contracts"],
            "evidence_refs": ["snapshot://1"],
            "prompt_hash": "sha256:proposal",
            "tool_trace_hash": "sha256:trace",
            "model_id": "fxstack.phase7.proposal",
            "decision_seed": 7,
            "input_artefact_refs": ["artifact://proposal"],
            "config_diff": {"prompt": "redacted"},
            "replay_window": "2026-04-08T12:00:00Z/2026-04-09T12:00:00Z",
            "artifact_root": "/tmp/artifacts",
            "latest_stage": "draft",
            "latest_promotion_id": "",
            "approval_status": "draft",
        }
    )
    assert proposal["experiment_id"] == experiment_id
    assert proposal["prompt_hash"] == "sha256:proposal"
    assert proposal["latest_stage"] == "draft"

    approval = store.record_approval_event(
        subject_type="experiment",
        subject_id=experiment_id,
        approver="ops",
        decision="approved",
        reason="phase7 promotion approved",
    )
    promotion = store.upsert_experiment_promotion(
        {
            "promotion_id": str(uuid4()),
            "experiment_id": experiment_id,
            "prompt_hash": proposal["prompt_hash"],
            "tool_trace_hash": proposal["tool_trace_hash"],
            "model_id": proposal["model_id"],
            "config_diff": proposal["config_diff"],
            "replay_window": proposal["replay_window"],
            "replay_results": {"status": "eligible"},
            "approval_records": [{"event_id": approval["event_id"], "decision": approval["decision"]}],
            "paper_results": {"status": "pass"},
            "canary_results": {"status": "pass"},
            "release_manifest_ref": "release://manifest-1",
            "rollback_metadata": {"enabled": False},
            "artefact_hashes": {"proposal": "sha256:proposal"},
            "status": "promoted",
        }
    )
    assert promotion["experiment_id"] == experiment_id
    assert promotion["status"] == "promoted"
    assert promotion["approval_records"][0]["event_id"] == approval["event_id"]

    lineage = store.upsert_experiment_lineage(
        {
            "experiment_id": experiment_id,
            "proposal_ref": "proposal://1",
            "review_ref": "review://1",
            "replay_refs": ["replay://1"],
            "paper_pack_ref": "paper://1",
            "canary_pack_ref": "canary://1",
            "promotion_decision_ref": "promotion://1",
            "rollback_plan_ref": "rollback://1",
            "release_manifest_ref": "release://manifest-1",
            "reflection_memory_ref": "memory://1",
            "latest_stage": "promoted",
            "latest_promotion_id": promotion["promotion_id"],
            "approval_status": "promoted",
            "evidence_refs": ["snapshot://1"],
            "promotion_ids": [promotion["promotion_id"]],
            "approval_event_ids": [approval["event_id"]],
        }
    )
    assert lineage["experiment_id"] == experiment_id
    assert lineage["latest_promotion_id"] == promotion["promotion_id"]
    assert lineage["approval_event_ids"] == [approval["event_id"]]

    fetched_proposals = store.get_experiment_proposals(limit=10, approval_status="draft", source_run_id=proposal["source_run_id"])
    assert len(fetched_proposals) == 1
    assert fetched_proposals[0]["prompt_hash"] == "sha256:proposal"

    fetched_promotion = store.get_experiment_promotion(promotion["promotion_id"])
    assert fetched_promotion is not None
    assert fetched_promotion["status"] == "promoted"

    fetched_lineage = store.get_experiment_lineage(experiment_id)
    assert fetched_lineage is not None
    assert fetched_lineage["latest_stage"] == "promoted"
    assert fetched_lineage["approval_event_ids"] == [approval["event_id"]]

    approval_rows = store.get_approval_events(limit=10, subject_type="experiment", subject_id=experiment_id)
    assert len(approval_rows) == 1
    assert approval_rows[0]["event_id"] == approval["event_id"]
