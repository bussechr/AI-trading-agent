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
