from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _fresh_client(tmp_path: Path) -> TestClient:
    os.environ["FXSTACK_DATABASE_URL"] = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    if "fxstack.api.app" in sys.modules:
        del sys.modules["fxstack.api.app"]
    from fxstack.api.app import app

    return TestClient(app)


def test_v2_health_state_commands_roundtrip(tmp_path: Path):
    client = _fresh_client(tmp_path)

    r = client.get("/v2/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"

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
