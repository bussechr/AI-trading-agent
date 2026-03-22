from __future__ import annotations

from pathlib import Path

from fxstack.runtime.dto import ExecutionAck, ExecutionCommand
from fxstack.runtime.postgres_store import PostgresRuntimeStore


def test_command_lifecycle_roundtrip(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    store = PostgresRuntimeStore(db_url)

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
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    store = PostgresRuntimeStore(db_url)

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
