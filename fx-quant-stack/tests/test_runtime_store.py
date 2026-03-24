from __future__ import annotations

from pathlib import Path
import os

from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore


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


def test_constructor_does_not_requeue_stale_delivered(tmp_path: Path):
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
