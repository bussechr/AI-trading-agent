from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import os
from uuid import uuid4

from sqlalchemy import select, update

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore
from fxstack.runtime.service import RuntimeService


def _fresh_store(tmp_path: Path) -> PostgresRuntimeStore:
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = db_url
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    return PostgresRuntimeStore(db_url)


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

    ack = ExecutionAck.from_payload({"command_id": "c1", "status": "acked", "ticket": 11})
    out, code = store.ack_command(ack)
    assert code == 200
    assert out["status"] == "acked"

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
    service = RuntimeService(database_url=store.database_url)

    payload = {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}

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
    service = RuntimeService(database_url=store.database_url)

    payload = {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "explicit-dup", "idempotency_key": "idem-1"}

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
    service = RuntimeService(database_url=store.database_url)

    payload = {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "idempotency_key": "idem-ack-1"}
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
    service.record_tick({"symbol": "EURUSD", "bid": 1.1010, "ask": 1.1012, "spread": 0.0002})

    queued, code = service.submit_command(
        {
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
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
    service.record_tick({"symbol": "EURUSD", "bid": None, "ask": None, "mid": 1.2345})

    queued, code = service.submit_command(
        {
            "command_id": "paper-mid-only",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
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

    ok, _ = store.enqueue_command(delivered)
    assert ok is True
    polled = store.poll_next_command()
    assert polled is not None
    assert polled.command_id == "delivered2"

    ok, _ = store.enqueue_command(acked)
    assert ok is True

    ack_polled = store.poll_next_command()
    assert ack_polled is not None
    assert ack_polled.command_id == "acked1"
    store.ack_command(ExecutionAck.from_payload({"command_id": "acked1", "status": "acked", "ticket": 1}))

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


def test_restart_recovery_quarantines_delivered_without_redelivery(tmp_path: Path) -> None:
    store = _fresh_store(tmp_path)
    service = RuntimeService(database_url=store.database_url)

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
