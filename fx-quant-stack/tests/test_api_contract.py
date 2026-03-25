from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient


def _fresh_client(tmp_path: Path) -> TestClient:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
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

    service.patch_state({"runtime_status": "running", "runtime_last_cycle_ts": time.time()})
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


def test_v2_ready_surfaces_runtime_startup_progress_and_failure_states(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service
    from fxstack.settings import get_settings

    now = time.time()
    stale_secs = float(get_settings().runtime_startup_progress_stale_secs)
    service.patch_state(
        {
            "runtime_status": "starting",
            "runtime_last_cycle_ts": 0.0,
            "runtime_startup": {
                "boot_id": "boot-1",
                "booted_at": "2026-03-24T07:00:00+00:00",
                "runtime_pid": 123,
                "phase": "model_load",
                "phase_pair": "",
                "phase_index": 0,
                "phase_total": 18,
                "last_progress_ts": now,
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "starting"
    assert ready["runtime_phase"] == "model_load"
    assert ready["runtime_boot_id"] == "boot-1"
    assert ready["reason"] == "runtime_starting"

    service.patch_state(
        {
            "runtime_status": "starting",
            "runtime_last_cycle_ts": 0.0,
            "agent_decisions": [{"symbol": "EURUSD"}],
            "agent_diagnostics": {"foo": "bar"},
            "runtime_startup": {
                "boot_id": "boot-2",
                "booted_at": "2026-03-24T07:05:00+00:00",
                "runtime_pid": 456,
                "phase": "initial_refresh",
                "phase_pair": "GBPJPY",
                "phase_index": 10,
                "phase_total": 18,
                "last_progress_ts": now - (stale_secs + 15.0),
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    state = client.get("/v2/state").json()
    assert state["runtime_status"] == "stalled"
    assert state["runtime_phase"] == "initial_refresh"
    assert state["runtime_phase_pair"] == "GBPJPY"
    assert state["agent_decisions"] == []
    assert state["agent_diagnostics"] == {}

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "stalled"
    assert ready["status_tier"] == "bridge_up_runtime_stalled"
    assert ready["reason"] == "runtime_startup_stalled"
    assert ready["runtime_phase"] == "initial_refresh"
    assert ready["runtime_phase_pair"] == "GBPJPY"
    assert float(ready["runtime_last_progress_age_secs"]) >= stale_secs

    service.patch_state(
        {
            "runtime_status": "failed",
            "runtime_last_cycle_ts": 0.0,
            "runtime_startup": {
                "boot_id": "boot-3",
                "booted_at": "2026-03-24T07:10:00+00:00",
                "runtime_pid": 789,
                "phase": "model_load",
                "phase_pair": "EURUSD",
                "phase_index": 1,
                "phase_total": 18,
                "last_progress_ts": now,
                "failure_reason": "RuntimeError:boom",
                "failed_at": "2026-03-24T07:10:05+00:00",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "failed"
    assert ready["status_tier"] == "bridge_up_runtime_failed"
    assert ready["reason"] == "runtime_startup_failed"
    assert ready["runtime_failure_reason"] == "RuntimeError:boom"

    service.record_runtime_boot_failure(
        boot={
            "boot_id": "boot-3",
            "booted_at": "2026-03-24T07:10:00+00:00",
            "runtime_pid": 789,
            "phase": "model_load",
            "phase_pair": "EURUSD",
            "phase_index": 1,
            "phase_total": 18,
            "last_progress_ts": now,
            "failure_reason": "",
            "failed_at": "",
            "pending_command_policy": "purge_and_mark_stale",
        },
        failure_reason="RuntimeError:boom",
        failed_at="2026-03-24T07:10:05+00:00",
    )
    governance = client.get("/v2/governance/events").json()
    assert len(governance["events"]) >= 1
    assert governance["events"][0]["event_type"] == "runtime_startup_failed"


def test_v2_decision_snapshots_exposes_persisted_history(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.store_decisions(
        decisions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "score": 4.2,
                "confidence": 77.0,
                "execution_ready": False,
                "reasons": ["shadow_meta_reject"],
                "metadata": {
                    "structure_timing_score": 0.81,
                    "structure_rescue_active": False,
                    "shadow_rejection_reason": "shadow_meta_reject",
                    "adaptive_environment_state": "CorrectiveTrend",
                    "adaptive_playbook": "trend_pullback",
                    "adaptive_entry_quality": 0.67,
                    "adaptive_shadow_would_trade": True,
                },
            }
        ],
        vol=0.12,
        diagnostics={
            "runtime": "fxstack",
            "shadow_policy": {"candidate_count": 1},
            "adaptive_shadow_policy": {"candidate_count": 1, "would_trade_count": 1},
        },
    )

    body = client.get("/v2/decision-snapshots?limit=5").json()
    assert "items" in body
    assert len(body["items"]) >= 1
    latest = body["items"][0]
    assert latest["vol"] == 0.12
    assert latest["decisions_json"][0]["symbol"] == "EURUSD"
    assert latest["decisions_json"][0]["metadata"]["structure_timing_score"] == 0.81
    assert latest["decisions_json"][0]["metadata"]["adaptive_playbook"] == "trend_pullback"
    assert latest["diagnostics_json"]["adaptive_shadow_policy"]["candidate_count"] == 1
