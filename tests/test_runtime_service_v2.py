from __future__ import annotations

import sqlite3

from src.trader.application.runtime_service import RuntimeService
from src.trader.interfaces.config import TraderConfig


def test_runtime_service_command_lifecycle(tmp_path):
    db = tmp_path / "runtime.db"
    cfg = TraderConfig(runtime_db_path=str(db), default_session_id="unit", command_ttl_secs=30.0)
    service = RuntimeService(cfg)

    queued, code = service.submit_command({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert code == 200
    assert queued["status"] == "queued"
    command_id = str(queued["command_id"])

    out, code2 = service.poll_command(as_line=False)
    assert code2 == 200
    assert out["status"] == "ok"
    assert out["command"]["command_id"] == command_id
    assert "proto=v2" in str(out["line"])

    ack_out, ack_code = service.ack_command(
        {
            "command_id": command_id,
            "status": "acked",
            "symbol": "EURUSD",
            "ticket": 123,
        }
    )
    assert ack_code == 200
    assert ack_out["status"] == "acked"

    metrics = service.get_metrics()
    assert int(metrics["counters"]["commands_total"]) >= 1
    assert int(metrics["counters"]["acked"]) >= 1
    assert int((metrics.get("throughput", {}) or {}).get("executed_entries_5m", 0)) >= 1
    queue_to_terminal = ((metrics.get("lifecycle_latency_ms", {}) or {}).get("queue_to_terminal", {}) or {})
    assert float(queue_to_terminal.get("p50", 0.0)) >= 0.0

    state = service.get_state()
    assert int(state.get("signals_sent", 0)) >= 1
    assert int(state.get("trades_executed", 0)) >= 1


def test_runtime_service_decision_snapshots_and_governance_events(tmp_path):
    db = tmp_path / "runtime.db"
    cfg = TraderConfig(runtime_db_path=str(db), default_session_id="unit", command_ttl_secs=30.0)
    service = RuntimeService(cfg)

    service.store_decisions(
        decisions=[{"symbol": "EURUSD", "side": "BUY", "score": 0.77}],
        vol=0.0025,
        diagnostics={
            "rejection_stats": {"spread_gate": 2},
            "last_diag": {
                "p_trend": 0.68,
                "vol": 0.0025,
                "score": 0.7,
                "score_effective": 0.77,
                "raw_signal": 0.5,
                "predictive_sharpe": 0.24,
                "predictive_sharpe_aligned": 0.21,
            },
            "governance": {
                "paused": True,
                "risk_scale": 0.35,
                "reasons": ["soft_drawdown"],
                "drawdown_pct": 0.081,
            },
        },
    )

    state = service.get_state()
    risk = dict(state.get("risk_envelope", {}) or {})
    assert float(risk.get("soft_dd_pct", 0.0)) > 0.0
    assert str(risk.get("regime", "")) in {"trend", "range", "transition"}

    events = service.get_governance_events(limit=20)
    assert events
    assert str(events[-1].get("event_type", "")) in {"pause_on", "state_update"}

    metrics = service.get_metrics()
    assert int((metrics.get("decision_pipeline", {}) or {}).get("snapshots_5m", 0)) >= 1
    assert int((metrics.get("governance", {}) or {}).get("events_24h", 0)) >= 1
    stage_attr = dict((metrics.get("decision_pipeline", {}) or {}).get("stage_attribution", {}) or {})
    assert isinstance(stage_attr.get("pipeline_rows", []), list)


def test_runtime_service_restart_requeues_delivered(tmp_path):
    db = tmp_path / "runtime.db"
    cfg = TraderConfig(runtime_db_path=str(db), default_session_id="unit", command_ttl_secs=60.0)

    service1 = RuntimeService(cfg)
    queued, _ = service1.submit_command({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    cid = str(queued["command_id"])
    polled, code = service1.poll_command(as_line=False)
    assert code == 200
    assert polled["status"] == "ok"
    assert polled["command"]["command_id"] == cid
    service1.store.close()

    # Simulate process restart with same persistent DB.
    service2 = RuntimeService(cfg)
    polled2, code2 = service2.poll_command(as_line=False)
    assert code2 == 200
    assert polled2["status"] == "ok"
    assert polled2["command"]["command_id"] == cid


def test_runtime_service_rejects_queued_to_acked_transition(tmp_path):
    db = tmp_path / "runtime.db"
    cfg = TraderConfig(runtime_db_path=str(db), default_session_id="unit", command_ttl_secs=30.0)
    service = RuntimeService(cfg)

    queued, code = service.submit_command({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert code == 200
    cid = str(queued["command_id"])

    # Must pass through delivered before terminal status.
    bad_ack, bad_code = service.ack_command({"command_id": cid, "status": "acked", "ticket": 777})
    assert bad_code == 409
    assert bad_ack["status"] == "transition_conflict"
    assert bad_ack["current_status"] == "queued"
    assert bad_ack["requested_status"] == "acked"

    # Legal path still works.
    polled, pcode = service.poll_command(as_line=False)
    assert pcode == 200
    assert polled["status"] == "ok"
    good_ack, good_code = service.ack_command({"command_id": cid, "status": "acked", "ticket": 777})
    assert good_code == 200
    assert good_ack["status"] == "acked"


def test_runtime_service_command_events_and_idempotent_final_ack(tmp_path):
    db = tmp_path / "runtime.db"
    cfg = TraderConfig(runtime_db_path=str(db), default_session_id="unit", command_ttl_secs=30.0)
    service = RuntimeService(cfg)

    queued, _ = service.submit_command({"cmd": "SELL", "symbol": "GBPUSD", "lots": 0.2})
    cid = str(queued["command_id"])
    service.poll_command(as_line=False)

    ack1, code1 = service.ack_command({"command_id": cid, "status": "acked", "ticket": 101, "status_reason": "order_send_ok"})
    assert code1 == 200
    assert ack1["status"] == "acked"

    # Duplicate terminal ACK is replay-safe and does not mutate final status.
    ack2, code2 = service.ack_command({"command_id": cid, "status": "acked", "ticket": 101})
    assert code2 == 200
    assert ack2["status"] == "already_finalized"
    assert ack2["final_status"] == "acked"

    events = service.get_command_events(command_id=cid, limit=20)
    statuses = [str(evt.get("status", "")) for evt in events]
    assert statuses == ["queued", "delivered", "acked"]
    assert str(events[-1].get("reason", "")) == "order_send_ok"


def test_runtime_service_migrates_legacy_runtime_db(tmp_path):
    db = tmp_path / "runtime_legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE commands (command_id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("CREATE TABLE command_events (id INTEGER PRIMARY KEY AUTOINCREMENT, command_id TEXT, event_status TEXT)")
    conn.commit()
    conn.close()

    cfg = TraderConfig(runtime_db_path=str(db), default_session_id="unit", command_ttl_secs=30.0)
    service = RuntimeService(cfg)

    health = service.get_health()
    assert str(health.get("status", "")) == "healthy"

    queued, code = service.submit_command({"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert code == 200
    assert queued["status"] == "queued"
