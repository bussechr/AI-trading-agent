from __future__ import annotations

from datetime import UTC, datetime
import json
import math
from pathlib import Path
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.market_source_identity import build_authenticated_market_source
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.service import FinalEntryApproval, RuntimeService


RELEASE_GENERATION_ID = "legacy-release-generation"
RELEASE_REQUEST_SHA256 = "1" * 64
RELEASE_MODEL_IDENTITY_SHA256 = "2" * 64
RELEASE_MANIFEST_FILE_SHA256 = "3" * 64
RELEASE_RUNTIME_BOOT_ID = "legacy-runtime-boot"


def _exact_model_stack_market_entry_fields(
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
) -> dict[str, object]:
    quote = 1.3 if symbol == "GBPUSD" else 1.1
    worst = quote + 0.0002 if side == "BUY" else quote - 0.0002
    return {
        "execution_type": "market",
        "pending_orders_forbidden": True,
        "entry_quote_price": quote,
        "entry_price": worst,
        "worst_fill_price": worst,
        "max_slippage_points": 20,
        "expected_broker_contract_state_schema": "fxstack_ig_mt4_contract_state_v1",
        "expected_broker_contract_venue_id": "ig_mt4",
        "expected_broker_contract_symbol": symbol,
        "expected_broker_contract_broker_symbol": f"{symbol}.IG",
        "expected_broker_contract_account_currency": "EUR",
        "expected_broker_contract_binding_sha256": "a" * 64,
        "expected_broker_contract_lot_size": 100_000.0,
        "expected_broker_contract_min_lot": 0.01,
        "expected_broker_contract_lot_step": 0.01,
        "expected_broker_contract_max_lot": 100.0,
        "expected_broker_contract_point": 0.00001,
        "expected_broker_contract_tick_size": 0.00001,
        "expected_broker_contract_margin_required": 100.0,
        "broker_contract_margin_utilization_cap": 0.25,
        "expected_broker_contract_stop_level_points": 0.0,
        "expected_broker_contract_freeze_level_points": 0.0,
        "expected_broker_contract_digits": 5,
        "expected_broker_contract_trade_allowed": True,
    }


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
    out = migrate_database(
        database_url=db_url, root=Path(__file__).resolve().parents[1]
    )
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


def _exact_market_entry_ack(
    store: PostgresRuntimeStore,
    command_id: str,
    *,
    ticket: int,
    production_scalper: bool = False,
) -> dict[str, object]:
    """Complete a legacy queue fixture with exact broker ACK expectations."""

    row = store.get_command(command_id)
    assert row is not None
    payload = dict(row.get("payload_json") or {})
    side = str(row.get("cmd") or "").strip().upper()
    symbol = str(row.get("symbol") or "").strip().upper()
    assert side in {"BUY", "SELL"}
    assert symbol
    jpy = symbol.endswith("JPY")
    open_price = 110.0 if jpy else 1.1
    sl_price = row.get("sl_price")
    tp_price = row.get("tp_price")
    if sl_price is None:
        distance = 1.0 if jpy else 0.01
        sl_price = open_price - distance if side == "BUY" else open_price + distance
    if tp_price is None:
        distance = 2.0 if jpy else 0.02
        tp_price = open_price + distance if side == "BUY" else open_price - distance
    broker_symbol = f"{symbol}.IG"
    payload.update(
        {
            "execution_type": "market",
            "worst_fill_price": open_price,
            "sl_price": float(sl_price),
            "tp_price": float(tp_price),
            "expected_broker_contract_broker_symbol": broker_symbol,
            "expected_broker_contract_tick_size": 0.001 if jpy else 0.00001,
            "expected_broker_contract_lot_step": 0.01,
        }
    )
    if production_scalper:
        payload.update(
            {
                "strategy_lane": "production_scalper",
                "intent": "production_scalper_entry",
            }
        )
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == command_id)
            .values(
                sl_price=float(sl_price),
                tp_price=float(tp_price),
                payload_json=payload,
            )
        )
    owner_token = str(payload.get("owner_token") or "")
    return {
        "command_id": command_id,
        "status": "acked",
        "mutation_state": "confirmed",
        "ticket": int(ticket),
        "magic": int(row.get("magic") or 0),
        "owner_token": owner_token,
        "symbol": symbol,
        "actuals_schema": "fxstack.mt4_order_actuals.v1",
        "actual_command_id": command_id,
        "actual_cmd": side,
        "actual_side": side,
        "actual_symbol": symbol,
        "actual_broker_symbol": broker_symbol,
        "actual_execution_type": "market",
        "actual_ticket": int(ticket),
        "actual_magic": int(row.get("magic") or 0),
        "actual_owner_token": owner_token,
        "actual_order_comment": f"{owner_token}.ig",
        "actual_lots": float(row.get("lots") or 0.0),
        "actual_open_price": open_price,
        "actual_sl_price": float(sl_price),
        "actual_tp_price": float(tp_price),
        "actual_remaining_lots": float(row.get("lots") or 0.0),
        "actual_close_time": 0.0,
    }


def test_runtime_service_open_positions_prefers_canonical_broker_snapshot(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url)
    canonical = [{"symbol": "EURUSD", "lots": 0.03, "ticket": 17}]
    service.patch_state(
        {
            "positions": canonical,
            "open_positions": [{"symbol": "GBPUSD", "lots": 0.50}],
        }
    )

    assert service.get_open_positions() == canonical


def test_store_decisions_keeps_large_telemetry_out_of_runtime_state(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(
        {
            "runtime_diag": {"production_scalp": {"cycle_id": "cycle-1"}},
            "agent_decisions": [],
            "agent_diagnostics": {},
            "vol": 0.0,
        }
    )
    state_before = store.get_state()
    decisions = [{"symbol": "EURUSD", "metadata": {"payload": "x" * 4_096}}]
    diagnostics = {
        "runtime": "fxstack_production_scalp",
        "production_scalp": {"cycle_id": "cycle-1", "payload": "y" * 8_192},
    }

    store.store_decisions(
        decisions=decisions,
        vol=0.25,
        diagnostics=diagnostics,
    )

    assert store.get_state() == state_before
    latest = store.get_decision_snapshots(limit=1)[0]
    assert latest["decisions_json"] == decisions
    assert latest["diagnostics_json"] == diagnostics
    assert latest["vol"] == pytest.approx(0.25)
    readiness_statements: list[str] = []

    def _capture_readiness_query(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        readiness_statements.append(str(statement))

    event.listen(store.engine, "before_cursor_execute", _capture_readiness_query)
    try:
        ready_state, ready_metrics, ready_diagnostics = (
            store.get_state_metrics_and_latest_decision_diagnostics()
        )
    finally:
        event.remove(
            store.engine,
            "before_cursor_execute",
            _capture_readiness_query,
        )
    assert ready_state == state_before
    assert ready_metrics["decision_pipeline"]["snapshots_5m"] == 1
    assert ready_diagnostics["diagnostics_json"] == diagnostics
    assert ready_diagnostics["ts"] == pytest.approx(latest["ts"])
    assert "decisions_json" not in ready_diagnostics
    assert len(readiness_statements) == 3
    assert "decision_snapshots.diagnostics_json" in readiness_statements[-1]
    assert "decision_snapshots.decisions_json" not in readiness_statements[-1]
    with store.engine.begin() as conn:
        raw_decisions, raw_diagnostics = conn.exec_driver_sql(
            "SELECT decisions_json, diagnostics_json "
            "FROM decision_snapshots ORDER BY id DESC LIMIT 1"
        ).one()
    assert "__fxstack_json_encoding__" in str(raw_decisions)
    assert "__fxstack_json_encoding__" in str(raw_diagnostics)
    raw_uncompressed_size = len(json.dumps(decisions)) + len(json.dumps(diagnostics))
    assert len(str(raw_decisions)) + len(str(raw_diagnostics)) < raw_uncompressed_size


def test_decision_snapshot_reader_accepts_legacy_uncompressed_json(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    decisions = [{"symbol": "EURUSD", "side": "FLAT"}]
    diagnostics = {"runtime": "legacy"}
    with store.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO decision_snapshots "
            "(ts, vol, decisions_json, diagnostics_json) VALUES (?, ?, ?, ?)",
            (1.0, 0.0, json.dumps(decisions), json.dumps(diagnostics)),
        )

    latest = store.get_decision_snapshots(limit=1)[0]
    assert latest["decisions_json"] == decisions
    assert latest["diagnostics_json"] == diagnostics


def test_cycle_state_and_decisions_commit_atomically_in_one_transaction(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    statements: list[str] = []

    def _capture_query(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(str(statement))

    event.listen(store.engine, "before_cursor_execute", _capture_query)
    try:
        store.commit_state_and_decisions(
            {"runtime_status": "running", "cycle_marker": "committed"},
            runtime_diag_patch={"live_command_admission": {"allowed": True}},
            runtime_diag_remove=("production_scalp",),
            decisions=[{"symbol": "EURUSD", "side": "FLAT"}],
            vol=0.0,
            diagnostics={"runtime": "fxstack_production_scalp"},
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_query)

    assert len(statements) == 3
    state = store.get_state()
    latest = store.get_decision_snapshots(limit=1)[0]
    assert state["cycle_marker"] == "committed"
    assert latest["decisions_json"] == [{"symbol": "EURUSD", "side": "FLAT"}]
    assert latest["diagnostics_json"] == {"runtime": "fxstack_production_scalp"}
    assert latest["ts"] == pytest.approx(state["last_update"])

    state_before_failure = store.get_state()
    snapshots_before_failure = store.get_decision_snapshots(limit=10)

    def _fail_snapshot_insert(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if "INSERT INTO decision_snapshots" in str(statement):
            raise RuntimeError("forced_decision_snapshot_failure")

    event.listen(store.engine, "before_cursor_execute", _fail_snapshot_insert)
    try:
        with pytest.raises(RuntimeError, match="forced_decision_snapshot_failure"):
            store.commit_state_and_decisions(
                {"cycle_marker": "must_rollback"},
                decisions=[],
                vol=0.0,
                diagnostics={},
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", _fail_snapshot_insert)

    assert store.get_state() == state_before_failure
    assert store.get_decision_snapshots(limit=10) == snapshots_before_failure


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


def test_bridge_consumer_lease_is_singleton_and_generation_bound(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    first = store.claim_bridge_consumer_lease(
        consumer_identity="ea-primary",
        producer_instance_id="mt4-terminal-instance-a",
        terminal_lease_scope="terminal-all-charts",
        credential_generation_id="generation-1",
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
        channel="poll",
        lease_secs=15.0,
    )
    assert first["ok"] is True
    lease = dict(first["lease"])
    assert lease["poll_authenticated_at"] >= lease["acquired_at"]

    assert lease["schema_version"] == "fxstack_bridge_consumer_lease_v2"
    assert lease["producer_instance_id"] == "mt4-terminal-instance-a"
    assert lease["bridge_protocol_version"] == BRIDGE_PROTOCOL_VERSION

    busy = store.claim_bridge_consumer_lease(
        consumer_identity="ea-primary",
        producer_instance_id="mt4-terminal-instance-b",
        terminal_lease_scope="terminal-all-charts",
        credential_generation_id="generation-1",
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
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
        producer_instance_id="mt4-terminal-instance-a",
        terminal_lease_scope="terminal-all-charts",
        credential_generation_id="generation-1",
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
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
    expected_live = dict(store.get_state()["runtime_diag"]["orchestration_live"])

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


def test_runtime_diag_patch_merges_under_the_state_write_lock(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(
        {
            "runtime_diag": {
                "orchestration_live": {"authority_revision": 7},
                "provider_health": {"market": "ok"},
                "production_scalp": {"cycle_id": "old"},
            }
        }
    )
    initial_live = dict(store.get_state()["runtime_diag"]["orchestration_live"])
    statements: list[str] = []

    def _capture_query(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(str(statement))

    event.listen(store.engine, "before_cursor_execute", _capture_query)
    try:
        store.update_state_patch(
            {"runtime_status": "running"},
            runtime_diag_patch={
                "production_scalp": {"cycle_id": "new"},
                "live_command_admission": {"allowed": True},
            },
            runtime_diag_remove=("provider_health",),
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_query)

    assert len(statements) == 2
    state = store.get_state()
    runtime_diag = state["runtime_diag"]
    assert runtime_diag["orchestration_live"] == {
        **initial_live,
        "authority_revision": int(initial_live.get("authority_revision") or 0) + 1,
    }
    assert "provider_health" not in runtime_diag
    assert runtime_diag["production_scalp"] == {"cycle_id": "new"}
    assert runtime_diag["live_command_admission"] == {"allowed": True}
    assert state["runtime_status"] == "running"

    with pytest.raises(
        ValueError,
        match="runtime_diag cannot be supplied with a nested diagnostic mutation",
    ):
        store.update_state_patch(
            {"runtime_diag": {}},
            runtime_diag_patch={},
        )


def test_atomic_live_authority_kill_dominates_stale_ramp_and_requires_explicit_start(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    store.update_state_patch(_live_admission_state())
    before_kill = dict(store.get_state()["runtime_diag"]["orchestration_live"])

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
    assert row["reason"] == ("poll_authority_revoked:live_authority_revision_changed")


def _record_fresh_eurusd_tick(store: PostgresRuntimeStore) -> None:
    store.record_tick(
        {
            "symbol": "EURUSD",
            "bid": 1.1000,
            "ask": 1.1002,
            "spread": 0.0002,
        }
    )


def test_record_ticks_uses_one_executemany_and_preserves_rows(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    payloads = [
        {
            "symbol": f"PAIR{index:02d}",
            "bid": 1.1 + index / 10_000.0,
            "ask": 1.1002 + index / 10_000.0,
            "spread": 0.0002,
            "time": datetime.now(UTC).timestamp(),
            "raw": {"sequence": index},
        }
        for index in range(12)
    ]
    insert_modes: list[bool] = []

    def _capture_tick_insert(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        executemany,
    ) -> None:
        if "INSERT INTO market_ticks" in str(statement):
            insert_modes.append(bool(executemany))

    event.listen(store.engine, "before_cursor_execute", _capture_tick_insert)
    try:
        store.record_ticks(payloads)
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_tick_insert)

    assert insert_modes == [True]
    with store.engine.begin() as conn:
        rows = (
            conn.execute(
                select(store.market_ticks).order_by(store.market_ticks.c.symbol)
            )
            .mappings()
            .all()
        )
    assert [row["symbol"] for row in rows] == [
        payload["symbol"] for payload in payloads
    ]
    assert [dict(row["raw_json"])["raw"]["sequence"] for row in rows] == list(range(12))


def test_get_state_and_metrics_uses_three_exact_reads_and_preserves_counts(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    now = datetime.now(UTC).timestamp()
    command_statuses = ["queued", "queued", "delivered", "acked", "acked"]
    push_statuses = ["queued", "retry", "claimed", "delivered"]
    with store.engine.begin() as conn:
        conn.execute(
            store.commands.insert(),
            [
                {
                    "command_id": f"metrics-command-{index}",
                    "session_id": "metrics",
                    "proto": "v2",
                    "cmd": "INFO",
                    "status": status,
                    "created_at": now,
                    "updated_at": now,
                    "expires_at": now + 60.0,
                    "delivered_count": 0,
                    "ack_terminal_safe": False,
                }
                for index, status in enumerate(command_statuses)
            ],
        )
        conn.execute(
            store.feature_push_outbox.insert(),
            [
                {
                    "outbox_key": f"metrics-outbox-{index}",
                    "pair": "EURUSD",
                    "feature_service": "metrics",
                    "entity_key": "EURUSD",
                    "event_timestamp": now,
                    "payload_json": {},
                    "status": status,
                    "attempt_count": 0,
                    "created_at": now,
                    "updated_at": now,
                }
                for index, status in enumerate(push_statuses)
            ],
        )
        conn.execute(
            store.decision_snapshots.insert(),
            [{"ts": now}, {"ts": now - 1.0}, {"ts": now - 301.0}],
        )
        conn.execute(
            store.command_events.insert(),
            [
                {
                    "command_id": "metrics-command-0",
                    "event_status": "queued",
                    "ts": now + index,
                }
                for index in range(3)
            ],
        )
        conn.execute(
            store.active_model_sets.insert(),
            [
                {
                    "pair": pair,
                    "model_set_id": f"model-{index}",
                    "registry_path": "registry",
                    "artifacts_json": {},
                    "enabled": enabled,
                    "updated_at": now,
                }
                for index, (pair, enabled) in enumerate(
                    (("EURUSD", 1), ("GBPUSD", 0))
                )
            ],
        )
        conn.execute(
            store.feature_push_audit.insert(),
            [
                {
                    "outbox_key": f"metrics-audit-{index}",
                    "pair": "EURUSD",
                    "feature_service": "metrics",
                    "entity_key": "EURUSD",
                    "event_timestamp": now,
                    "status": "delivered",
                    "payload_json": {},
                    "created_at": now,
                }
                for index in range(2)
            ],
        )
        conn.execute(
            store.feature_parity_audit.insert(),
            [
                {
                    "pair": "EURUSD",
                    "feature_service": "metrics",
                    "entity_key": "EURUSD",
                    "event_timestamp": now,
                    "source": "runtime",
                    "parity_ok": parity_ok,
                    "payload_json": {},
                    "created_at": now,
                }
                for parity_ok in (0, 1, 0)
            ],
        )

    statements: list[str] = []

    def _capture_query(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(str(statement))

    event.listen(store.engine, "before_cursor_execute", _capture_query)
    try:
        state, metrics = store.get_state_and_metrics()
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_query)

    assert len(statements) == 3
    assert "decision_snapshots.ts BETWEEN" in statements[-1]
    assert state["release_authority"]["status"] == "legacy_test_fixture"
    assert metrics["commands"] == {"acked": 2, "delivered": 1, "queued": 2}
    assert metrics["pending"] == {"count": 3}
    assert metrics["decision_pipeline"]["snapshots_5m"] == 2
    assert len(store.get_decision_snapshots(limit=10)) == 3
    assert metrics["command_events"] == {"count": 3}
    assert metrics["models"] == {"active_sets": 1}
    assert metrics["feature_push"] == {
        "outbox": {"claimed": 1, "delivered": 1, "queued": 1, "retry": 1},
        "backlog": 3,
        "audit_rows": 2,
    }
    assert metrics["feature_parity"] == {"total": 3, "breaches": 2}
    assert store.get_metrics() == metrics

    governance_statements: list[str] = []

    def _capture_governance_query(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        governance_statements.append(str(statement))

    event.listen(store.engine, "before_cursor_execute", _capture_governance_query)
    try:
        governance_state, governance_metrics = (
            store.get_state_and_governance_metrics()
        )
    finally:
        event.remove(
            store.engine,
            "before_cursor_execute",
            _capture_governance_query,
        )

    assert len(governance_statements) == 1
    assert governance_state == state
    assert governance_metrics == {"feature_parity": {"breaches": 2}}
    assert "commands" not in governance_statements[0].lower()
    assert "feature_push_outbox" not in governance_statements[0].lower()


def test_scalp_reconciliation_command_read_excludes_inert_terminal_rows(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    now = datetime.now(UTC).timestamp()
    rows = [
        ("active-info", "INFO", "queued"),
        ("unknown-info", "INFO", "unexpected"),
        ("acked-info", "INFO", "acked"),
        ("failed-info", "INFO", "failed"),
        ("acked-entry", "BUY", "acked"),
        ("failed-entry", "SELL", "failed"),
        ("acked-close", "CLOSE", "acked"),
    ]
    with store.engine.begin() as conn:
        conn.execute(
            store.commands.insert(),
            [
                {
                    "command_id": command_id,
                    "session_id": "reconciliation-scope",
                    "proto": "v2",
                    "cmd": cmd,
                    "status": status,
                    "created_at": now + index,
                    "updated_at": now + index,
                    "expires_at": now + 60.0,
                    "delivered_count": 0,
                    "ack_terminal_safe": False,
                }
                for index, (command_id, cmd, status) in enumerate(rows)
            ],
        )

    without_history = store.get_scalp_reconciliation_commands(include_historical=False)
    assert set(without_history[0]) == {
        "command_id",
        "cmd",
        "symbol",
        "magic",
        "intent",
        "status",
        "payload_json",
        "ack_json",
    }
    assert {row["command_id"] for row in without_history} == {
        "active-info",
        "unknown-info",
    }

    with_history = store.get_scalp_reconciliation_commands(include_historical=True)
    assert {row["command_id"] for row in with_history} == {
        "active-info",
        "unknown-info",
        "acked-entry",
        "failed-entry",
        "acked-close",
    }


def test_latest_ticks_use_one_indexed_seek_union_with_source_and_id_tie_break(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    source = build_authenticated_market_source(
        broker_account_scope="account-a",
        broker_venue_id="ig_mt4",
        producer_identity="ea",
        producer_instance_id="instance-a",
        terminal_lease_scope="scope",
        credential_generation_id="generation",
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    foreign_source = build_authenticated_market_source(
        broker_account_scope="account-a",
        broker_venue_id="ig_mt4",
        producer_identity="ea",
        producer_instance_id="instance-b",
        terminal_lease_scope="scope",
        credential_generation_id="generation",
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert source is not None
    assert foreign_source is not None
    base_ts = datetime.now(UTC).timestamp() - 1_000.0
    payloads = [
        {
            "symbol": symbol,
            "bid": 1.0 + symbol_index + tick_index / 100_000.0,
            "ask": 1.0002 + symbol_index + tick_index / 100_000.0,
            "time": base_ts + tick_index,
            **source.to_fields(),
        }
        for tick_index in range(100)
        for symbol_index, symbol in enumerate(("EURUSD", "GBPUSD"))
    ]
    tie_ts = base_ts + 100.0
    payloads.extend(
        [
            {
                "symbol": "EURUSD",
                "bid": 9.0,
                "ask": 9.1,
                "time": tie_ts,
                **source.to_fields(),
            },
            {
                "symbol": "EURUSD",
                "bid": 10.0,
                "ask": 10.1,
                "time": tie_ts,
                **source.to_fields(),
            },
            {
                "symbol": "GBPUSD",
                "bid": 99.0,
                "ask": 99.1,
                "time": tie_ts + 1.0,
                **foreign_source.to_fields(),
            },
        ]
    )
    store.record_ticks(payloads)

    statements: list[str] = []

    def _capture_query(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(str(statement))

    event.listen(store.engine, "before_cursor_execute", _capture_query)
    try:
        with store.engine.begin() as conn:
            latest = store._latest_market_ticks_for_symbols(
                conn,
                symbols={"EURUSD", "GBPUSD", "MISSING"},
                market_source=source,
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_query)

    assert len(statements) == 1
    assert "ROW_NUMBER" not in statements[0].upper()
    assert set(latest) == {"EURUSD", "GBPUSD"}
    assert latest["EURUSD"]["bid"] == 10.0
    assert latest["EURUSD"]["ts"] == tie_ts
    assert latest["GBPUSD"]["bid"] < 99.0


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


def test_enqueue_revalidates_live_authority_inside_the_queue_transaction(
    tmp_path: Path,
) -> None:
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
        (
            _live_admission_state(broker_account_mode="real"),
            "broker_account_mode_changed",
        ),
        (
            _live_admission_state(broker_account_scope="scope-2"),
            "broker_account_scope_changed",
        ),
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
        **_exact_model_stack_market_entry_fields(),
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

    ack_payload = _exact_market_entry_ack(store, "c1", ticket=11)
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
    durable_ack = dict(row["ack_json"] or {})
    attestation = dict(durable_ack["execution_ack_attestation"])
    assert attestation["attested"] is True
    assert attestation["effective_status"] == "acked"
    assert attestation["policy_scope"] == "production_mt4_exact"
    assert attestation["actuals"]["ticket"] == 11


def test_positive_ticket_failure_is_quarantined_for_reconciliation(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "failed-with-ticket",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    ack_payload = _exact_market_entry_ack(
        store,
        command.command_id,
        ticket=73,
        production_scalper=True,
    )
    ack_payload.update(
        {
            "status": "failed",
            "mutation_state": "not_attempted",
        }
    )

    out, code = store.ack_command(ExecutionAck.from_payload(ack_payload))

    assert code == 200
    assert out["status"] == "reconcile_required"
    assert "ack_failed_with_positive_ticket" in out["reasons"]
    assert store.get_execution_uncertainty()["statuses"] == {"reconcile_required": 1}
    assert int(store.get_state().get("trades_executed") or 0) == 0


def test_identity_mismatch_ack_is_quarantined_and_does_not_count_trade(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "wrong-broker-symbol",
            "cmd": "SELL",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    ack_payload = _exact_market_entry_ack(
        store,
        command.command_id,
        ticket=74,
        production_scalper=True,
    )
    ack_payload["actual_broker_symbol"] = "EURUSD.WRONG"

    out, code = store.ack_command(ExecutionAck.from_payload(ack_payload))

    assert code == 200
    assert out["status"] == "reconcile_required"
    assert "ack_broker_symbol_mismatch" in out["reasons"]
    assert int(store.get_state().get("trades_executed") or 0) == 0


def test_contradictory_terminal_ack_escalates_and_reconcile_is_sticky(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "terminal-ack-contradiction",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    exact_ack = _exact_market_entry_ack(
        store,
        command.command_id,
        ticket=75,
        production_scalper=True,
    )
    first, first_code = store.ack_command(ExecutionAck.from_payload(exact_ack))
    assert first_code == 200
    assert first["status"] == "acked"
    assert int(store.get_state().get("trades_executed") or 0) == 1

    contradiction, contradiction_code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": "failed",
                "mutation_state": "not_attempted",
                "ticket": -1,
            }
        )
    )
    assert contradiction_code == 200
    assert contradiction["status"] == "reconcile_required"
    assert "terminal_ack_contradiction" in contradiction["reasons"]

    sticky, sticky_code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": "duplicate",
                "mutation_state": "not_attempted",
                "ticket": -1,
            }
        )
    )
    assert sticky_code == 200
    assert sticky["status"] == "reconcile_required"
    assert "reconciliation_sticky" in sticky["reasons"]
    assert int(store.get_state().get("trades_executed") or 0) == 1

    resolved, resolved_code = store.ack_command(ExecutionAck.from_payload(exact_ack))
    assert resolved_code == 200
    assert resolved["status"] == "acked"
    assert int(store.get_state().get("trades_executed") or 0) == 1


def test_legacy_unsafe_terminal_refusal_still_fences_new_exposure(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "legacy-unsafe-failure",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == command.command_id)
            .values(
                status="failed",
                ack_json={"status": "failed", "ticket": 76},
            )
        )

    uncertainty = store.get_execution_uncertainty()

    assert uncertainty["blocked"] is True
    assert uncertainty["statuses"] == {"failed": 1}


def test_non_scalp_entry_rejects_bare_positive_ticket_ack(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "model-stack-legacy-ack",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None

    out, code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": "acked",
                "ticket": 80,
            }
        )
    )

    assert code == 200
    assert out["status"] == "reconcile_required"
    assert "ack_actuals_schema_mismatch" in out["reasons"]
    assert "ack_explicit_actual_cmd_missing" in out["reasons"]
    row = store.get_command(command.command_id)
    assert row is not None
    attestation = dict(dict(row["ack_json"])["execution_ack_attestation"])
    assert attestation["policy_scope"] == "production_mt4_exact"
    assert attestation["attested"] is False
    assert int(store.get_state().get("trades_executed") or 0) == 0


def test_legacy_close_all_success_without_per_ticket_proof_reconciles(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "legacy-close-all-unattested",
            "cmd": "CLOSE_ALL",
            "magic": 246_810,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None

    out, code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": "acked",
                "mutation_state": "confirmed",
                "ticket": -1,
            }
        )
    )

    assert code == 200
    assert out["status"] == "reconcile_required"
    assert "ack_positive_ticket_missing" in out["reasons"]
    assert "ack_actuals_schema_mismatch" in out["reasons"]


def test_mt4_stamped_command_rejects_claimed_paper_ack_spoof(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "mt4-paper-spoof",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "strategy_lane": "production_scalper",
            "intent": "production_scalper_entry",
            "execution_type": "market",
            "pending_orders_forbidden": True,
            "entry_deadline_epoch": math.ceil(datetime.now(UTC).timestamp()) + 5,
            "_execution_provider": "mt4",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None

    out, code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": "acked",
                "ticket": 901,
                "orchestration_meta_json": {
                    "execution_provider": "paper",
                    "paper_simulated": True,
                    "paper_fill_price": 1.1,
                },
            }
        )
    )

    assert code == 200
    assert out["status"] == "reconcile_required"
    row = store.get_command(command.command_id)
    assert row is not None
    attestation = dict(dict(row["ack_json"])["execution_ack_attestation"])
    assert attestation["policy_scope"] == "production_mt4_exact"
    assert "paper_simulation_non_broker" not in attestation["reasons"]
    assert int(store.get_state().get("trades_executed") or 0) == 0


@pytest.mark.parametrize("reported_status", ["failed", "duplicate"])
def test_legacy_terminal_refusal_without_mutation_state_reconciles(
    tmp_path: Path,
    reported_status: str,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": f"legacy-{reported_status}-missing-mutation",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None

    out, code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": reported_status,
                "ticket": -1,
            }
        )
    )

    assert code == 200
    assert out["status"] == "reconcile_required"
    assert "ack_not_conclusive_pre_mutation_refusal" in out["reasons"]
    assert store.get_execution_uncertainty()["statuses"] == {"reconcile_required": 1}


@pytest.mark.parametrize(
    "ack_fields",
    [
        {"status": "failed", "ticket": -1, "mutation_state": "attempted"},
        {"status": "acked", "ticket": 902, "mutation_state": "confirmed"},
    ],
    ids=["attempted", "positive-ticket"],
)
def test_expired_never_delivered_command_reconciles_contradictory_ack(
    tmp_path: Path,
    ack_fields: dict[str, object],
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "expired-never-delivered-contradiction",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == command.command_id)
            .values(status="expired", reason="ttl_expired", delivered_count=0)
        )

    out, code = store.ack_command(
        ExecutionAck.from_payload({"command_id": command.command_id, **ack_fields})
    )

    assert code == 200
    assert out["status"] == "reconcile_required"
    assert "expired_never_delivered_ack_contradiction" in out["reasons"]
    assert store.get_execution_uncertainty()["statuses"] == {"reconcile_required": 1}


def test_reconcile_required_row_rejects_bare_legacy_positive_ticket_ack(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "legacy-reconcile-bare-ticket",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == command.command_id)
            .values(status="reconcile_required", reason="broker_outcome_unknown")
        )

    out, code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": "acked",
                "ticket": 903,
            }
        )
    )

    assert code == 200
    assert out["status"] == "reconcile_required"
    assert "reconciliation_sticky" in out["reasons"]
    assert int(store.get_state().get("trades_executed") or 0) == 0


def test_info_ack_with_positive_ticket_does_not_increment_trade_count(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "info-positive-ticket",
            "cmd": "INFO",
            "symbol": "EURUSD",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    trades_before = int(store.get_state().get("trades_executed") or 0)

    out, code = store.ack_command(
        ExecutionAck.from_payload(
            {
                "command_id": command.command_id,
                "status": "acked",
                "ticket": 904,
            }
        )
    )

    assert code == 200
    assert out["status"] == "acked"
    assert int(store.get_state().get("trades_executed") or 0) == trades_before
    row = store.get_command(command.command_id)
    assert row is not None
    assert dict(row["ack_json"])["count_as_trade"] is False


def test_ack_transaction_rolls_back_command_event_and_runtime_state_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "ack-atomic-rollback",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    ack_payload = _exact_market_entry_ack(
        store,
        command.command_id,
        ticket=905,
    )
    row_before = store.get_command(command.command_id)
    events_before = store.get_command_events(command_id=command.command_id, limit=20)
    state_before = store.get_state()
    assert row_before is not None
    original_append = store._append_command_event

    def _append_then_fail(**kwargs) -> None:
        original_append(**kwargs)
        if kwargs.get("event_status") == "acked":
            raise RuntimeError("injected failure after ACK event insert")

    monkeypatch.setattr(store, "_append_command_event", _append_then_fail)

    with pytest.raises(RuntimeError, match="injected failure"):
        store.ack_command(
            ExecutionAck.from_payload(ack_payload)
        )

    row_after = store.get_command(command.command_id)
    assert row_after is not None
    assert row_after["status"] == row_before["status"] == "delivered"
    assert dict(row_after["ack_json"] or {}) == dict(row_before["ack_json"] or {})
    assert (
        store.get_command_events(command_id=command.command_id, limit=20)
        == events_before
    )
    assert store.get_state() == state_before


def test_malformed_production_scalp_acked_row_still_fences_new_entries(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "production-scalper:old-malformed-acked",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "strategy_lane": "production_scalper",
            "intent": "production_scalper_entry",
            "execution_type": "market",
            "pending_orders_forbidden": True,
            "entry_deadline_epoch": math.ceil(datetime.now(UTC).timestamp()) + 5,
            "_execution_provider": "mt4",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == command.command_id)
            .values(
                status="acked",
                ack_json={"status": "acked", "ticket": 906},
                reason="legacy_unattested_ack",
            )
        )

    uncertainty = store.get_execution_uncertainty()

    assert uncertainty["blocked"] is True
    assert uncertainty["statuses"] == {"acked": 1}
    service = _service_for_direct_entry_queue_contract(store)
    blocked, blocked_code = service.submit_command(
        {
            "command_id": "entry-blocked-by-old-malformed-ack",
            "cmd": "BUY",
            "symbol": "GBPUSD",
            "lots": 0.1,
            "sl_price": 1.31,
            "tp_price": 1.28,
            **_exact_model_stack_market_entry_fields(symbol="GBPUSD"),
        }
    )
    assert blocked_code == 409
    assert blocked["status"] == "reconciliation_required"
    assert blocked["error"] == "new_exposure_blocked_by_unresolved_execution_outcome"
    assert service.get_command("entry-blocked-by-old-malformed-ack") is None


def test_failed_row_with_raw_actual_ticket_still_fences_new_entries(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    command = ExecutionCommand.from_payload(
        {
            "command_id": "failed-raw-actual-ticket",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(command)[0] is True
    assert store.poll_next_command() is not None
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id == command.command_id)
            .values(
                status="failed",
                ack_json={
                    "status": "failed",
                    "mutation_state": "not_attempted",
                    "raw": {"actual_ticket": 907},
                },
                reason="legacy_failure_with_hidden_ticket",
            )
        )

    uncertainty = store.get_execution_uncertainty()

    assert uncertainty["blocked"] is True
    assert uncertainty["statuses"] == {"failed": 1}


def test_future_dated_legacy_command_is_neither_active_nor_pollable(
    tmp_path: Path,
) -> None:
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


def test_runtime_service_dedupes_direct_retry_without_command_id(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)

    payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "sl_price": 1.09,
        "tp_price": 1.12,
        **_exact_model_stack_market_entry_fields(),
    }

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
        **_exact_model_stack_market_entry_fields(),
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


def _exact_close_retry_payload(
    command_id: str,
    *,
    target_ticket: int,
) -> dict[str, object]:
    return {
        "command_id": command_id,
        "cmd": "CLOSE",
        "symbol": "EURUSD",
        "lots": 0.0,
        "close_lots": 0.0,
        "target_ticket": target_ticket,
        "magic": 246810,
        "owner_token": f"fxs-owned-ticket-{target_ticket}",
        "ownership_contract": "ticket_owner_v1",
        "intent": "EXIT",
        "action": "time_stop",
        "management_strategy": "scalp-dislocation-v1",
        "trace_id": f"trace-{command_id}",
        "correlation_id": f"correlation-{command_id}",
        "thread_id": f"scalp:EURUSD:{target_ticket}",
        "expected_strategy_generation_id": "strategy-generation-1",
        "expected_strategy_config_sha256": "a" * 64,
    }


def test_exact_never_delivered_expired_close_is_atomically_requeued(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    payload = _exact_close_retry_payload(
        "expired-close-retry",
        target_ticket=301,
    )
    first, first_code = service.submit_command(dict(payload))
    assert first_code == 200
    assert first["status"] == "queued"

    with service.store.engine.begin() as conn:
        conn.execute(
            update(service.store.commands)
            .where(service.store.commands.c.command_id == "expired-close-retry")
            .values(created_at=1.0, updated_at=1.0, expires_at=2.0)
        )
    assert service.store.cleanup_expired_commands() == 1
    expired = service.get_command("expired-close-retry")
    assert expired is not None
    assert str(expired["status"]) == "expired"
    assert int(expired["delivered_count"]) == 0

    retried, retry_code = service.submit_command(dict(payload))

    assert retry_code == 200
    assert retried["status"] == "queued"
    assert retried["command_id"] == "expired-close-retry"
    requeued = service.get_command("expired-close-retry")
    assert requeued is not None
    assert str(requeued["status"]) == "queued"
    assert str(requeued["reason"]) == "expired_never_delivered_requeued"
    assert int(requeued["delivered_count"]) == 0
    assert float(requeued["created_at"]) > 2.0
    assert float(requeued["updated_at"]) > float(expired["updated_at"])
    assert float(requeued["expires_at"]) > float(requeued["updated_at"])
    assert dict(requeued["payload_json"] or {}) == dict(first["command"]["payload"])

    events = service.store.get_command_events(
        command_id="expired-close-retry",
        limit=10,
    )
    assert events[0]["event_status"] == "requeued"
    assert events[0]["reason"] == "expired_never_delivered_requeued"
    event_payload = dict(events[0]["event_json"] or {})
    assert event_payload["previous_status"] == "expired"
    assert event_payload["previous_reason"] == "ttl_expired"
    assert event_payload["delivered_count"] == 0

    duplicate, duplicate_code = service.submit_command(dict(payload))
    assert duplicate_code == 200
    assert duplicate == {
        "status": "duplicate",
        "command_id": "expired-close-retry",
        "state": "queued",
    }
    assert (
        sum(
            event["event_status"] == "requeued"
            for event in service.store.get_command_events(
                command_id="expired-close-retry",
                limit=10,
            )
        )
        == 1
    )


def test_expired_command_resurrection_rejects_entries_payload_drift_and_prior_delivery(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    cases: list[tuple[dict[str, object], str, int, dict[str, object]]] = []

    entry = {
        "command_id": "expired-entry-not-requeued",
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "sl_price": 1.09,
        "tp_price": 1.12,
        **_exact_model_stack_market_entry_fields(),
    }
    cases.append((entry, "expired", 0, dict(entry)))

    changed_close = _exact_close_retry_payload(
        "expired-close-payload-changed",
        target_ticket=302,
    )
    changed_retry = {
        **changed_close,
        "target_ticket": 303,
        "owner_token": "fxs-owned-ticket-303",
    }
    cases.append((changed_close, "expired", 0, changed_retry))

    for status, delivered_count in (
        ("expired", 1),
        ("delivered", 1),
        ("reconcile_required", 1),
        ("acked", 1),
    ):
        command_id = f"close-not-requeued-{status}"
        close = _exact_close_retry_payload(
            command_id,
            target_ticket=310 + len(cases),
        )
        cases.append((close, status, delivered_count, dict(close)))

    for original, status, delivered_count, retry in cases:
        queued, queued_code = service.submit_command(dict(original))
        assert queued_code == 200
        assert queued["status"] == "queued"
        command_id = str(original["command_id"])
        with service.store.engine.begin() as conn:
            conn.execute(
                update(service.store.commands)
                .where(service.store.commands.c.command_id == command_id)
                .values(
                    status=status,
                    delivered_count=delivered_count,
                    reason="synthetic_terminal_state",
                )
            )

        duplicate, duplicate_code = service.submit_command(dict(retry))

        assert duplicate_code == 200
        assert duplicate["status"] == "duplicate"
        assert duplicate["state"] == status
        row = service.get_command(command_id)
        assert row is not None
        assert str(row["status"]) == status
        assert int(row["delivered_count"]) == delivered_count
        assert all(
            event["event_status"] != "requeued"
            for event in service.store.get_command_events(
                command_id=command_id,
                limit=10,
            )
        )


def test_runtime_service_legacy_ack_uses_idempotency_key_without_command_id(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)

    payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.1,
        "sl_price": 1.09,
        "tp_price": 1.12,
        "idempotency_key": "idem-ack-1",
        **_exact_model_stack_market_entry_fields(),
    }
    queued, code = service.submit_command(dict(payload))
    assert code == 200
    assert queued["status"] == "queued"

    polled = store.poll_next_command()
    assert polled is not None
    assert polled.command_id == queued["command_id"]

    ack_payload = _exact_market_entry_ack(
        service.store,
        str(queued["command_id"]),
        ticket=11,
    )
    ack_payload.pop("command_id")
    ack_payload["idempotency_key"] = "idem-ack-1"
    out, ack_code = service.ack_command(ack_payload)
    assert ack_code == 200
    assert out["status"] == "acked"
    assert out["command_id"] == queued["command_id"]
    assert out["idempotency_key"] == "idem-ack-1"

    row = store.get_command(queued["command_id"])
    assert row is not None
    assert str(row["status"]) == "acked"


def test_runtime_service_paper_execution_auto_acks_and_polls_empty(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(
        database_url=store.database_url, execution_provider="paper"
    )
    _disable_release_egress_fence_for_legacy_queue_test(service.store)
    service.record_tick(
        {"symbol": "EURUSD", "bid": 1.1010, "ask": 1.1012, "spread": 0.0002}
    )

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


def test_runtime_service_paper_execution_uses_persisted_mid_only_tick(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(
        database_url=store.database_url, execution_provider="paper"
    )
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


def test_runtime_service_paper_execution_reports_paper_provider_health(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(
        database_url=store.database_url, execution_provider="paper"
    )
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
                        "details": {
                            "execution_provider": "paper",
                            "paused": False,
                            "entries_only": False,
                        },
                    }
                },
            }
        }
    )

    state = store.get_state()
    assert state["runtime_diag"]["provider_roles"]["execution_provider"] == "paper"
    assert (
        state["runtime_diag"]["provider_health"]["execution_provider"]["provider"]
        == "paper"
    )
    assert (
        state["runtime_diag"]["provider_health"]["execution_provider"]["status"] == "ok"
    )


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

    ack = ExecutionAck.from_payload(
        {
            "command_id": "dup1",
            "status": "duplicate",
            "mutation_state": "not_attempted",
            "ticket": -1,
            "message": "duplicate_suppressed",
        }
    )
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
    store.ack_command(
        ExecutionAck.from_payload(_exact_market_entry_ack(store, "acked1", ticket=1))
    )

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


def test_command_cleanup_batches_updates_and_audit_events(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)

    def _enqueue_info(command_id: str) -> None:
        command = ExecutionCommand.from_payload(
            {"cmd": "INFO", "command_id": command_id},
            default_session_id="unit",
            ttl_secs=120,
        )
        assert store.enqueue_command(command)[0] is True

    expired_ids = [f"expired-batch-{index:02d}" for index in range(12)]
    for command_id in expired_ids:
        _enqueue_info(command_id)
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id.in_(expired_ids))
            .values(expires_at=0.0)
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
        assert store.cleanup_expired_commands() == len(expired_ids)
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_sql)
    assert sql_modes == [False, False, True]

    purge_ids = [f"purged-batch-{index:02d}" for index in range(12)]
    for command_id in purge_ids:
        _enqueue_info(command_id)
    preserved = ExecutionCommand.from_payload(
        {"cmd": "CLOSE", "symbol": "EURUSD", "command_id": "preserved-close"},
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(preserved)[0] is True

    sql_modes.clear()
    event.listen(store.engine, "before_cursor_execute", _capture_sql)
    try:
        assert store.purge_pending_commands(
            reason="batched_restart_purge",
            preserve_queued_exposure_reducing=True,
        ) == len(purge_ids)
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_sql)
    assert sql_modes == [False, False, True]
    preserved_row = store.get_command("preserved-close")
    assert preserved_row is not None
    assert preserved_row["status"] == "queued"

    quarantine_ids = [f"quarantine-batch-{index:02d}" for index in range(12)]
    for command_id in quarantine_ids:
        _enqueue_info(command_id)
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id.in_(quarantine_ids))
            .values(
                status="delivered",
                updated_at=0.0,
                expires_at=datetime.now(UTC).timestamp() + 3_600.0,
                delivered_count=2,
            )
        )

    sql_modes.clear()
    event.listen(store.engine, "before_cursor_execute", _capture_sql)
    try:
        assert store.quarantine_stale_delivered(age_secs=1.0) == len(quarantine_ids)
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_sql)
    assert sql_modes == [False, True]

    with store.engine.begin() as conn:
        expired_events = (
            conn.execute(
                select(store.command_events).where(
                    store.command_events.c.command_id.in_(expired_ids)
                )
            )
            .mappings()
            .all()
        )
        purge_events = (
            conn.execute(
                select(store.command_events).where(
                    store.command_events.c.command_id.in_(purge_ids)
                )
            )
            .mappings()
            .all()
        )
        quarantine_events = (
            conn.execute(
                select(store.command_events).where(
                    store.command_events.c.command_id.in_(quarantine_ids)
                )
            )
            .mappings()
            .all()
        )
    assert len(expired_events) == len(expired_ids) * 2
    assert sum(row["event_status"] == "expired" for row in expired_events) == len(
        expired_ids
    )
    assert len(purge_events) == len(purge_ids) * 2
    assert sum(row["reason"] == "batched_restart_purge" for row in purge_events) == len(
        purge_ids
    )
    assert len(quarantine_events) == len(quarantine_ids) * 2
    quarantined = [
        row for row in quarantine_events if row["event_status"] == "reconcile_required"
    ]
    assert len(quarantined) == len(quarantine_ids)
    assert all(row["event_json"]["delivered_count"] == 2 for row in quarantined)

    disable_queued_ids = [f"disable-queued-{index:02d}" for index in range(12)]
    disable_delivered_ids = [f"disable-delivered-{index:02d}" for index in range(12)]
    for command_id in [*disable_queued_ids, *disable_delivered_ids]:
        _enqueue_info(command_id)
    with store.engine.begin() as conn:
        conn.execute(
            update(store.commands)
            .where(store.commands.c.command_id.in_(disable_delivered_ids))
            .values(status="delivered", delivered_count=1)
        )

    sql_modes.clear()
    event.listen(store.engine, "before_cursor_execute", _capture_sql)
    try:
        disabled = store.disable_execution_egress(
            reason="batched_emergency_disable",
            preserve_queued_exposure_reducing=True,
        )
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture_sql)
    assert disabled["quarantined_command_count"] == 24
    assert sql_modes == [False, False, False, False, True, False]

    queued_rows = [store.get_command(command_id) for command_id in disable_queued_ids]
    delivered_rows = [
        store.get_command(command_id) for command_id in disable_delivered_ids
    ]
    assert all(row is not None for row in [*queued_rows, *delivered_rows])
    assert all(row["status"] == "expired" for row in queued_rows if row is not None)
    assert all(
        row["reason"] == "batched_emergency_disable"
        for row in queued_rows
        if row is not None
    )
    assert all(
        row["status"] == "reconcile_required"
        for row in delivered_rows
        if row is not None
    )
    assert all(
        row["reason"] == "batched_emergency_disable:broker_outcome_unknown"
        for row in delivered_rows
        if row is not None
    )
    preserved_after_disable = store.get_command("preserved-close")
    assert preserved_after_disable is not None
    assert preserved_after_disable["status"] == "queued"


def _seed_boot_queue_recovery_commands(
    store: PostgresRuntimeStore,
    *,
    suffix: str,
) -> tuple[str, str, str]:
    delivered_close_id = f"delivered-close-{suffix}"
    queued_close_id = f"queued-close-{suffix}"
    queued_buy_id = f"queued-buy-{suffix}"
    delivered_close = ExecutionCommand.from_payload(
        {
            "cmd": "CLOSE",
            "symbol": "EURUSD",
            "command_id": delivered_close_id,
            "target_ticket": 101,
            "owner_token": "fxs-owned-ticket-101",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    queued_close = ExecutionCommand.from_payload(
        {
            "cmd": "CLOSE",
            "symbol": "GBPUSD",
            "command_id": queued_close_id,
            "target_ticket": 102,
            "owner_token": "fxs-owned-ticket-102",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    queued_buy = ExecutionCommand.from_payload(
        {
            "cmd": "BUY",
            "symbol": "USDJPY",
            "lots": 0.1,
            "command_id": queued_buy_id,
        },
        default_session_id="unit",
        ttl_secs=120,
    )

    assert store.enqueue_command(delivered_close)[0] is True
    delivered = store.poll_next_command()
    assert delivered is not None
    assert delivered.command_id == delivered_close_id
    assert store.enqueue_command(queued_close)[0] is True
    assert store.enqueue_command(queued_buy)[0] is True
    return delivered_close_id, queued_close_id, queued_buy_id


def _assert_boot_queue_recovery_states(
    store: PostgresRuntimeStore,
    *,
    delivered_close_id: str,
    queued_close_id: str,
    queued_buy_id: str,
    reason: str,
) -> None:
    delivered_close = store.get_command(delivered_close_id)
    queued_close = store.get_command(queued_close_id)
    queued_buy = store.get_command(queued_buy_id)

    assert delivered_close is not None
    assert queued_close is not None
    assert queued_buy is not None
    assert str(delivered_close["status"]) == "reconcile_required"
    assert str(delivered_close["reason"]) == (f"{reason}:broker_outcome_unknown")
    assert str(queued_close["status"]) == "queued"
    assert str(queued_buy["status"]) == "expired"
    assert str(queued_buy["reason"]) == reason


def test_runtime_boot_state_preserves_only_queued_exposure_reducing_commands(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    delivered_close_id, queued_close_id, queued_buy_id = (
        _seed_boot_queue_recovery_commands(store, suffix="state")
    )

    store.record_runtime_boot_state(
        boot={"boot_id": "boot-preserve-state"},
        preserve_queued_exposure_reducing=True,
    )

    _assert_boot_queue_recovery_states(
        store,
        delivered_close_id=delivered_close_id,
        queued_close_id=queued_close_id,
        queued_buy_id=queued_buy_id,
        reason="runtime_boot_requires_new_release_ack",
    )
    purged = store.purge_pending_commands(
        reason="runtime_restart_purged",
        include_delivered=False,
        preserve_queued_exposure_reducing=True,
    )
    assert purged == 0
    queued_close = store.get_command(queued_close_id)
    assert queued_close is not None
    assert str(queued_close["status"]) == "queued"


def test_boot_preserved_close_waits_through_disabled_poll_then_delivers(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path, enforce_execution_egress=True)
    initial_boot_id = "boot-before-preserved-close"
    restarted_boot_id = "boot-with-preserved-close"
    live_state = _live_admission_state(
        runtime_status="running",
        runtime_last_cycle_ts=datetime.now(UTC).timestamp(),
        runtime_startup={"boot_id": initial_boot_id},
        runtime_attestation={"runtime_boot_id": initial_boot_id},
        runtime_diag={
            "orchestration_live": _live_authority(
                active_intent_scope=["enter", "exit", "reduce"]
            ),
            "live_command_admission": {
                "allowed": True,
                "pairs": {"EURUSD": {"allowed": True}},
            },
        },
    )
    store.update_state_patch(live_state)
    assert (
        store.enable_production_execution_egress(runtime_boot_id=initial_boot_id)[
            "execution_egress_enabled"
        ]
        is True
    )

    close = ExecutionCommand.from_payload(
        {
            "cmd": "CLOSE",
            "symbol": "EURUSD",
            "command_id": "queued-close-across-disabled-poll",
            "target_ticket": 201,
            "owner_token": "fxs-owned-ticket-201",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(close)[0] is True
    store.record_runtime_boot_state(
        boot={"boot_id": restarted_boot_id},
        patch={"runtime_status": "starting"},
        preserve_queued_exposure_reducing=True,
    )

    # The EA may poll before boot activation finishes. Egress remains closed,
    # so nothing is delivered, but the exact protective CLOSE stays queued.
    assert store.poll_next_command() is None
    queued = store.get_command(close.command_id)
    assert queued is not None
    assert str(queued["status"]) == "queued"

    disabled_state = store.get_state()
    disabled_live = dict(
        dict(disabled_state.get("runtime_diag") or {}).get("orchestration_live") or {}
    )
    restarted_live = store.patch_orchestration_live_state(
        updates={
            "enabled": True,
            "mode": "live",
            "runtime_enabled": True,
            "queue_kill_active": False,
            "queue_kill_reason": "",
        },
        expected_live_authority=disabled_live,
        allow_reenable=True,
    )
    assert restarted_live["runtime_enabled"] is True
    store.update_state_patch(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": datetime.now(UTC).timestamp(),
            "runtime_attestation": {
                "runtime_boot_id": restarted_boot_id,
            },
        }
    )
    assert (
        store.enable_production_execution_egress(runtime_boot_id=restarted_boot_id)[
            "execution_egress_enabled"
        ]
        is True
    )

    delivered = store.poll_next_command()
    assert delivered is not None
    assert delivered.command_id == close.command_id
    assert delivered.status == "delivered"


def test_runtime_service_boot_failure_preserves_only_queued_exposure_reducing_commands(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    delivered_close_id, queued_close_id, queued_buy_id = (
        _seed_boot_queue_recovery_commands(store, suffix="failure")
    )
    service = RuntimeService(database_url=store.database_url)

    service.record_runtime_boot_failure(
        boot={"boot_id": "boot-preserve-failure"},
        failure_reason="activation_failed",
        preserve_queued_exposure_reducing=True,
    )

    _assert_boot_queue_recovery_states(
        service.store,
        delivered_close_id=delivered_close_id,
        queued_close_id=queued_close_id,
        queued_buy_id=queued_buy_id,
        reason="runtime_boot_failed",
    )
    purged = service.purge_pending_commands(
        reason="runtime_restart_purged",
        include_delivered=False,
        preserve_queued_exposure_reducing=True,
    )
    assert purged == 0
    queued_close = service.get_command(queued_close_id)
    assert queued_close is not None
    assert str(queued_close["status"]) == "queued"


def test_runtime_boot_queue_preservation_defaults_off(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    queued_close = ExecutionCommand.from_payload(
        {
            "cmd": "CLOSE",
            "symbol": "EURUSD",
            "command_id": "queued-close-default-off",
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    assert store.enqueue_command(queued_close)[0] is True

    store.record_runtime_boot_state(boot={"boot_id": "boot-default-off"})

    row = store.get_command(queued_close.command_id)
    assert row is not None
    assert str(row["status"]) == "expired"
    assert str(row["reason"]) == "runtime_boot_requires_new_release_ack"


def test_command_window_summary_counts_every_row_without_history_cap(
    tmp_path: Path,
) -> None:
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
            {
                "cmd": "SELL",
                "symbol": "GBPUSD",
                "lots": 0.1,
                "command_id": "window-sell",
            },
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


def test_restart_recovery_quarantines_delivered_without_redelivery(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url)
    _disable_release_egress_fence_for_legacy_queue_test(service.store)

    delivered = ExecutionCommand.from_payload(
        {
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "command_id": "delivered-recover",
        },
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

    purged = service.purge_pending_commands(
        reason="runtime_restart_purged", include_delivered=False
    )
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
    quarantine_event = next(
        item for item in events if item["event_status"] == "reconcile_required"
    )
    assert quarantine_event["reason"] == "stale_delivery_outcome_unknown"
    event_payload = dict(quarantine_event["event_json"])
    assert float(event_payload["quarantined_at"]) > old_ts
    assert event_payload["previous_status"] == "delivered"
    assert event_payload["delivered_count"] == 1
    assert event_payload["reconciliation_required"] is True


def test_quarantined_delivered_command_accepts_late_ack_without_redelivery(
    tmp_path: Path,
) -> None:
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

    out, code = store.ack_command(
        ExecutionAck.from_payload(
            _exact_market_entry_ack(
                store,
                "late-ack-1",
                ticket=22,
                production_scalper=True,
            )
        )
    )
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


def test_runtime_service_blocks_entries_while_delivery_is_unresolved_but_allows_protection(
    tmp_path: Path,
) -> None:
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
            **_exact_model_stack_market_entry_fields(),
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
            **_exact_model_stack_market_entry_fields(symbol="GBPUSD", side="SELL"),
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


def test_reconcile_required_fence_clears_only_after_terminal_ack(
    tmp_path: Path,
) -> None:
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
            **_exact_model_stack_market_entry_fields(),
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
            **_exact_model_stack_market_entry_fields(symbol="GBPUSD", side="SELL"),
        }
    )
    assert blocked_code == 409
    assert blocked["status"] == "reconciliation_required"

    acked, ack_code = service.ack_command(
        _exact_market_entry_ack(
            service.store,
            "reconcile-entry-1",
            ticket=41,
            production_scalper=True,
        )
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
            **_exact_model_stack_market_entry_fields(symbol="GBPUSD", side="SELL"),
        }
    )
    assert admitted_code == 200
    assert admitted["status"] == "queued"


def test_newer_authoritative_book_contains_uncertainty_to_affected_symbol(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    queued, code = service.submit_command(
        {
            "command_id": "scoped-uncertain-entry",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.09,
            "tp_price": 1.12,
            **_exact_model_stack_market_entry_fields(),
        }
    )
    assert code == 200
    assert queued["status"] == "queued"
    delivered = service.store.poll_next_command()
    assert delivered is not None
    assert delivered.command_id == "scoped-uncertain-entry"

    now = datetime.now(UTC).timestamp()
    with service.store.engine.begin() as conn:
        conn.execute(
            update(service.store.commands)
            .where(service.store.commands.c.command_id == "scoped-uncertain-entry")
            .values(updated_at=now - 2.0)
        )
    service.patch_state(
        {
            "broker_account_scope": "scope-1",
            "positions": [],
            "positions_snapshot_authoritative": True,
            "positions_snapshot_source": "positions_snapshot",
            "positions_snapshot_schema": "fxstack_mt4_positions_snapshot_v2",
            "positions_snapshot_contract_current": True,
            "positions_snapshot_token": "post-uncertainty-snapshot",
            "positions_snapshot_received_at": now - 1.0,
            "positions_snapshot_source_ts": now - 1.0,
            "positions_snapshot_account_scope": "scope-1",
        }
    )

    account_diagnostic = service.get_execution_uncertainty()
    assert account_diagnostic["present"] is True
    assert account_diagnostic["blocked"] is False
    assert account_diagnostic["scope_contained"] is True
    assert account_diagnostic["blocked_symbols"] == ["EURUSD"]
    assert service.get_execution_uncertainty(symbol="EURUSD")["blocked"] is True
    assert service.get_execution_uncertainty(symbol="GBPUSD")["blocked"] is False

    same_symbol, same_symbol_code = service.submit_command(
        {
            "command_id": "same-symbol-still-blocked",
            "cmd": "SELL",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 1.11,
            "tp_price": 1.08,
            **_exact_model_stack_market_entry_fields(side="SELL"),
        }
    )
    assert same_symbol_code == 409
    assert same_symbol["status"] == "reconciliation_required"

    unrelated, unrelated_code = service.submit_command(
        {
            "command_id": "unrelated-symbol-admitted",
            "cmd": "SELL",
            "symbol": "GBPUSD",
            "lots": 0.1,
            "sl_price": 1.31,
            "tp_price": 1.28,
            **_exact_model_stack_market_entry_fields(symbol="GBPUSD", side="SELL"),
        }
    )
    assert unrelated_code == 200
    assert unrelated["status"] == "queued"
    delivered_unrelated = service.store.poll_next_command()
    assert delivered_unrelated is not None
    assert delivered_unrelated.command_id == "unrelated-symbol-admitted"


def test_ambiguous_close_all_uncertainty_remains_account_wide(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    queued, code = service.submit_command(
        {
            "command_id": "uncertain-close-all",
            "cmd": "CLOSE_ALL",
        }
    )
    assert code == 200
    assert queued["status"] == "queued"
    delivered = service.store.poll_next_command()
    assert delivered is not None
    assert delivered.command_id == "uncertain-close-all"

    now = datetime.now(UTC).timestamp()
    with service.store.engine.begin() as conn:
        conn.execute(
            update(service.store.commands)
            .where(service.store.commands.c.command_id == "uncertain-close-all")
            .values(updated_at=now - 2.0)
        )
    service.patch_state(
        {
            "broker_account_scope": "scope-1",
            "positions": [],
            "positions_snapshot_authoritative": True,
            "positions_snapshot_source": "positions_snapshot",
            "positions_snapshot_schema": "fxstack_mt4_positions_snapshot_v2",
            "positions_snapshot_contract_current": True,
            "positions_snapshot_token": "post-close-all-snapshot",
            "positions_snapshot_received_at": now - 1.0,
            "positions_snapshot_source_ts": now - 1.0,
            "positions_snapshot_account_scope": "scope-1",
        }
    )

    diagnostic = service.get_execution_uncertainty(symbol="GBPUSD")
    assert diagnostic["present"] is True
    assert diagnostic["blocked"] is True
    assert diagnostic["scope_contained"] is False
    assert diagnostic["scope_reason"] == "uncertain_command_scope_not_exact"


def test_newer_authoritative_book_reconciles_old_terminal_and_expired_rows(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    now = datetime.now(UTC).timestamp()
    rows = (
        ("legacy-acked", "BUY", "EURUSD", "acked", 1, now - 300.0, now - 250.0),
        (
            "legacy-close-all",
            "CLOSE_ALL",
            None,
            "failed",
            1,
            now - 280.0,
            now - 240.0,
        ),
        (
            "old-expired",
            "SELL",
            "USDJPY",
            "expired",
            1,
            now - 260.0,
            now - 220.0,
        ),
        (
            "current-delivery",
            "BUY",
            "GBPUSD",
            "delivered",
            1,
            now - 2.0,
            now + 120.0,
        ),
    )
    with store.engine.begin() as conn:
        conn.execute(
            store.commands.insert(),
            [
                {
                    "command_id": command_id,
                    "session_id": "authoritative-book-reconciliation",
                    "proto": "v2",
                    "cmd": cmd,
                    "symbol": symbol,
                    "status": status,
                    "created_at": updated_at - 1.0,
                    "updated_at": updated_at,
                    "expires_at": expires_at,
                    "delivered_count": delivered_count,
                    "ack_terminal_safe": False,
                }
                for (
                    command_id,
                    cmd,
                    symbol,
                    status,
                    delivered_count,
                    updated_at,
                    expires_at,
                ) in rows
            ],
        )
    service.patch_state(
        {
            "broker_account_scope": "scope-1",
            "positions": [],
            "positions_snapshot_authoritative": True,
            "positions_snapshot_source": "positions_snapshot",
            "positions_snapshot_schema": "fxstack_mt4_positions_snapshot_v2",
            "positions_snapshot_contract_current": True,
            "positions_snapshot_token": "current-authoritative-book",
            "positions_snapshot_received_at": now - 1.0,
            "positions_snapshot_source_ts": now - 1.0,
            "positions_snapshot_account_scope": "scope-1",
        }
    )

    diagnostic = service.get_execution_uncertainty(symbol="EURUSD")
    assert diagnostic["statuses"] == {"delivered": 1}
    assert diagnostic["blocked"] is False
    assert diagnostic["scope_contained"] is True
    assert diagnostic["blocked_symbols"] == ["GBPUSD"]
    assert service.get_execution_uncertainty(symbol="GBPUSD")["blocked"] is True


def test_poll_holds_prequeued_entry_behind_unresolved_delivery_but_releases_protection(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    first = ExecutionCommand.from_payload(
        {
            "command_id": "prequeued-first",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
        },
        default_session_id="unit",
        ttl_secs=120,
    )
    second = ExecutionCommand.from_payload(
        {
            "command_id": "prequeued-second",
            "cmd": "SELL",
            "symbol": "GBPUSD",
            "lots": 0.1,
        },
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

    assert (
        store.ack_command(
            ExecutionAck.from_payload(
                _exact_market_entry_ack(store, "prequeued-first", ticket=51)
            )
        )[1]
        == 200
    )
    assert (
        store.ack_command(
            ExecutionAck.from_payload(
                {
                    "command_id": "prequeued-close",
                    "status": "failed",
                    "mutation_state": "not_attempted",
                    "ticket": -1,
                }
            )
        )[1]
        == 200
    )
    delivered_second = store.poll_next_command()
    assert delivered_second is not None
    assert delivered_second.command_id == "prequeued-second"


def test_expired_after_delivery_remains_fenced_and_accepts_late_resolution(
    tmp_path: Path,
) -> None:
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
            **_exact_model_stack_market_entry_fields(),
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
                **_exact_model_stack_market_entry_fields(
                    symbol="GBPUSD",
                    side="SELL",
                ),
            }
    )
    assert blocked_code == 409
    assert blocked["status"] == "reconciliation_required"

    acked, ack_code = service.ack_command(
        _exact_market_entry_ack(
            service.store,
            "expired-after-delivery",
            ticket=61,
            production_scalper=True,
        )
    )
    assert ack_code == 200
    assert acked["status"] == "acked"
    assert service.get_execution_uncertainty()["blocked"] is False


def test_expired_info_probe_does_not_create_execution_uncertainty(
    tmp_path: Path,
) -> None:
    store = _fresh_store(tmp_path)
    service = _service_for_direct_entry_queue_contract(store)
    queued, code = service.submit_command(
        {
            "command_id": "expired-info-probe",
            "cmd": "INFO",
            "symbol": "EURUSD",
            "lots": 0.0,
        }
    )
    assert code == 200
    assert queued["status"] == "queued"
    delivered = service.store.poll_next_command()
    assert delivered is not None
    assert delivered.command_id == "expired-info-probe"
    with service.store.engine.begin() as conn:
        conn.execute(
            update(service.store.commands)
            .where(service.store.commands.c.command_id == "expired-info-probe")
            .values(status="expired", reason="ttl_expired")
        )

    assert service.get_execution_uncertainty() == {
        "present": False,
        "blocked": False,
        "reason": "",
        "count": 0,
        "statuses": {},
        "scope_contained": False,
        "scope_reason": "",
        "blocked_symbols": [],
        "requested_symbol": "",
        "commands": [],
    }


def test_entry_admission_fails_closed_when_reconciliation_query_errors(
    tmp_path: Path, monkeypatch
) -> None:
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
            **_exact_model_stack_market_entry_fields(),
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


def test_command_roundtrip_preserves_phase1_orchestration_fields(
    tmp_path: Path,
) -> None:
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

    runs = store.get_orchestration_runs(
        limit=10, pair="EURUSD", runtime_mode="shadow", cycle_id="123"
    )
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
        governed = (
            conn.execute(
                select(store.governed_decisions).where(
                    store.governed_decisions.c.run_id == run_id
                )
            )
            .mappings()
            .first()
        )
    assert governed is not None
    assert str(governed["runtime_mode"]) == "shadow"


def test_store_orchestration_bundle_batches_agent_proposals(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    run_id = str(uuid4())
    proposals = [
        {
            "proposal_id": f"proposal-{index:02d}",
            "agent_id": f"agent-{index:02d}",
            "phase": "entry",
            "intent": "enter",
            "side": "BUY",
            "confidence": 0.8 + index / 100.0,
            "expected_edge_bps": 4.0 + index,
            "uncertainty": 0.1,
            "risk_cost": 0.2,
            "ttl_ms": 250,
            "evidence_refs": [f"evidence-{index:02d}"],
            "constraints": {"max_lots": 0.1},
            "advisory_only": True,
        }
        for index in range(12)
    ]
    proposal_insert_modes: list[bool] = []

    def _capture_proposal_insert(
        _conn,
        _cursor,
        statement,
        _parameters,
        _context,
        executemany,
    ) -> None:
        if "INSERT INTO agent_proposals" in str(statement):
            proposal_insert_modes.append(bool(executemany))

    event.listen(store.engine, "before_cursor_execute", _capture_proposal_insert)
    try:
        store.store_orchestration_bundle(
            context={
                "cycle_id": "bulk-1",
                "thread_id": "EURUSD:bulk-1:shadow",
                "correlation_id": "EURUSD:bulk-1:shadow",
                "ts_utc": datetime(2026, 4, 8, 12, 2, tzinfo=UTC).isoformat(),
                "pair": "EURUSD",
                "version_bundle": {"schema_version": ORCHESTRATION_SCHEMA_VERSION},
            },
            packet={
                "run_id": run_id,
                "pair": "EURUSD",
                "proposals": proposals,
            },
            trace={"trace_id": "trace-bulk-1", "run_id": run_id},
            runtime_mode="shadow",
            fallback_used=False,
        )
    finally:
        event.remove(
            store.engine,
            "before_cursor_execute",
            _capture_proposal_insert,
        )

    assert proposal_insert_modes == [True]
    with store.engine.begin() as conn:
        stored = (
            conn.execute(
                select(store.agent_proposals)
                .where(store.agent_proposals.c.run_id == run_id)
                .order_by(store.agent_proposals.c.proposal_id)
            )
            .mappings()
            .all()
        )
    assert len(stored) == len(proposals)
    assert [row["proposal_id"] for row in stored] == [
        proposal["proposal_id"] for proposal in proposals
    ]
    assert stored[0]["evidence_json"] == ["evidence-00"]
    assert stored[0]["constraints_json"] == {"max_lots": 0.1}
    assert all(row["created_at"] == stored[0]["created_at"] for row in stored)


def test_store_orchestration_bundle_normalizes_packet_fallback_used(
    tmp_path: Path,
) -> None:
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

    runs = store.get_orchestration_runs(
        limit=1, pair="EURUSD", runtime_mode="shadow", cycle_id="124"
    )
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
            "change_set": [
                {"path": "fxstack/runtime/postgres_store.py", "change": "add lineage"}
            ],
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
            "approval_records": [
                {"event_id": approval["event_id"], "decision": approval["decision"]}
            ],
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

    fetched_proposals = store.get_experiment_proposals(
        limit=10, approval_status="draft", source_run_id=proposal["source_run_id"]
    )
    assert len(fetched_proposals) == 1
    assert fetched_proposals[0]["prompt_hash"] == "sha256:proposal"

    fetched_promotion = store.get_experiment_promotion(promotion["promotion_id"])
    assert fetched_promotion is not None
    assert fetched_promotion["status"] == "promoted"

    fetched_lineage = store.get_experiment_lineage(experiment_id)
    assert fetched_lineage is not None
    assert fetched_lineage["latest_stage"] == "promoted"
    assert fetched_lineage["approval_event_ids"] == [approval["event_id"]]

    approval_rows = store.get_approval_events(
        limit=10, subject_type="experiment", subject_id=experiment_id
    )
    assert len(approval_rows) == 1
    assert approval_rows[0]["event_id"] == approval["event_id"]
