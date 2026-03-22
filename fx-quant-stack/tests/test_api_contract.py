from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _fresh_client(tmp_path: Path) -> TestClient:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    from fxstack.runtime.db_tools import migrate_database

    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    if "fxstack.api.app" in sys.modules:
        del sys.modules["fxstack.api.app"]
    from fxstack.api.app import app

    return TestClient(app)


def test_v2_health_state_commands_roundtrip(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    r = client.get("/v2/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"

    r = client.get("/v2/ready")
    assert r.status_code == 200
    ready = r.json()
    assert bool(ready.get("bridge_up")) is True
    assert "database_ok" in ready
    assert "runtime_ready" in ready
    assert "status_tier" in ready

    service.patch_state({"runtime_status": "running", "runtime_last_cycle_ts": 1_710_000_000.0})
    r = client.get("/v2/ready")
    assert r.status_code == 200
    ready = r.json()
    assert ready.get("runtime_status") == "running"
    assert isinstance(ready.get("runtime_ready"), bool)

    r = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "x1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"queued", "duplicate"}

    r = client.get("/v2/commands/poll")
    assert r.status_code == 200
    assert r.json().get("status") in {"ok", "empty"}

    r = client.post("/v2/commands/ack", json={"command_id": "x1", "status": "acked"})
    assert r.status_code in {200, 409}

    r = client.get("/v2/state")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)

    r = client.get("/v2/metrics")
    assert r.status_code == 200
    assert "pending" in r.json()

    r = client.get("/v2/ops/events")
    assert r.status_code == 200
    assert "events" in r.json()

    r = client.get("/v2/ops/workflows/status")
    assert r.status_code == 200
    assert "workflows" in r.json()
