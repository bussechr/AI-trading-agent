from __future__ import annotations

import importlib
import os
import tempfile
import time


def _fresh_bridge():
    db_dir = tempfile.mkdtemp(prefix="bridge_lifecycle_")
    os.environ["TRADER_RUNTIME_DB_PATH"] = os.path.join(db_dir, "runtime.db")
    mod = importlib.import_module("bridge_api.bridge")
    importlib.reload(mod)
    return mod


def _reset_bridge_state(bridge_mod) -> None:
    with bridge_mod.report_lock:
        bridge_mod.reports.clear()
    with bridge_mod.interop_lock:
        for q in bridge_mod.interop_latency_samples.values():
            q.clear()
        bridge_mod.interop_error_budget.clear()
    with bridge_mod.state_lock:
        bridge_mod.trading_state["positions"] = []
        bridge_mod.trading_state["signals_sent"] = 0
        bridge_mod.trading_state["trades_executed"] = 0
        bridge_mod.trading_state["last_signal"] = None
        bridge_mod.trading_state["last_ack"] = None
        bridge_mod.trading_state["agent_decisions"] = []
        bridge_mod.trading_state["agent_diagnostics"] = {}
        bridge_mod.trading_state["monitor"] = {}
    with bridge_mod.md_lock:
        bridge_mod.market_data.clear()
        bridge_mod.market_tick_history.clear()


def test_signal_lifecycle_ack_flow():
    bridge = _fresh_bridge()
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    command_id = "unit-command-1"
    r = client.post(
        "/v2/commands",
        json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": command_id},
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "queued"
    assert body["command_id"] == command_id

    # Poll should deliver but not drop pending until ACK is posted.
    p = client.get("/v2/commands/poll?format=line")
    assert p.status_code == 200
    assert "command_id=unit-command-1" in p.get_data(as_text=True)
    m = client.get("/v2/metrics").get_json()
    assert int(m["pending"]["count"]) == 1

    ack = client.post("/v2/commands/ack", json={"command_id": command_id, "status": "acked", "ticket": 12345})
    assert ack.status_code == 200
    assert ack.get_json()["status"] == "acked"
    m2 = client.get("/v2/metrics").get_json()
    assert int(m2["pending"]["count"]) == 0
    assert int(m2["counters"]["acked"]) >= 1


def test_duplicate_signal_id_is_suppressed():
    bridge = _fresh_bridge()
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    command_id = "dupe-1"
    r1 = client.post("/v2/commands", json={"cmd": "SELL", "symbol": "EURUSD", "command_id": command_id})
    assert r1.status_code == 200
    r2 = client.post("/v2/commands", json={"cmd": "SELL", "symbol": "EURUSD", "command_id": command_id})
    assert r2.status_code == 200
    assert r2.get_json()["status"] == "duplicate"


def test_report_positions_text_updates_state():
    bridge = _fresh_bridge()
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    msg = (
        "POSITIONS "
        "symbol=EURUSD,type=0,open_price=1.10001,open_time=1700000000,lots=0.10,profit=1.23 "
        "symbol=GBPUSD,type=1,open_price=1.25001,open_time=1700000500,lots=0.20,profit=-0.34"
    )
    r = client.post("/v2/reports", data=msg)
    assert r.status_code == 200

    state = client.get("/v2/state").get_json()
    positions = list(state.get("positions", []))
    assert len(positions) == 2
    assert positions[0]["symbol"] == "EURUSD"
    assert positions[1]["symbol"] == "GBPUSD"


def test_tick_history_exposes_h1_bars():
    bridge = _fresh_bridge()
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    # Post a few ticks spanning two H1 bars.
    now = time.time()
    for i, px in enumerate([1.1000, 1.1005, 1.1010, 1.1015]):
        ts = now - (7200 - (i * 2400))
        iso = bridge.datetime.fromtimestamp(ts).isoformat()
        r = client.post(
            "/v2/market/tick",
            data=f'{{"symbol":"EURUSD","bid":{px:.5f},"ask":{(px+0.0002):.5f},"spread":1.2,"time":"{iso}"}}',
        )
        assert r.status_code == 200

    bars = client.get("/v2/market/bars?symbol=EURUSD&timeframe=H1&limit=10")
    assert bars.status_code == 200
    payload = bars.get_json()
    assert payload["status"] == "ok"
    assert isinstance(payload.get("bars"), list)
    assert len(payload.get("bars", [])) >= 1
    one = payload["bars"][-1]
    assert "open" in one and "high" in one and "low" in one and "close" in one


def test_v2_metrics_include_runtime_and_interop_blocks():
    bridge = _fresh_bridge()
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    # Seed command and duplicate to exercise counters/idempotency.
    r1 = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "command_id": "qos-1"})
    assert r1.status_code == 200
    r2 = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "command_id": "qos-1"})
    assert r2.status_code == 200

    m = client.get("/v2/metrics")
    assert m.status_code == 200
    body = m.get_json()
    assert "counters" in body
    assert "timeouts" in body
    assert "pending" in body
    assert "interop" in body
    assert int(body["counters"]["commands_total"]) >= 1
    assert float(body["timeouts"]["ack_timeout_rate_5m"]) >= 0.0
    assert float(body["pending"]["oldest_pending_secs"]) >= 0.0
    assert isinstance(((body.get("interop", {}) or {}).get("latency", {}) or {}), dict)


def test_monitor_endpoint_exposes_entry_and_close_payload():
    bridge = _fresh_bridge()
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    monitor_payload = {
        "updated_ts": time.time(),
        "cycle_id": 42,
        "entry": {
            "symbol": "EURUSD",
            "side": "SELL",
            "open_proximity_pct": 67.5,
            "execution_ready": False,
            "blocked_by": "low_score",
        },
        "close": {
            "close_proximity_pct": 18.0,
            "dominant_close_reason": "risk_trailing_stop",
            "positions_open": 1,
            "positions": [
                {
                    "symbol": "EURUSD",
                    "side": "BUY",
                    "close_proximity_pct": 18.0,
                    "dominant_close_reason": "risk_trailing_stop",
                    "last_action": "hold",
                }
            ],
        },
        "warmup_mode": False,
        "starvation_mode": True,
        "relax_level": 0.09,
    }
    posted = client.post(
        "/v2/state/decisions",
        json={
            "decisions": [],
            "vol": 0.0011,
            "diagnostics": {"monitor": monitor_payload},
        },
    )
    assert posted.status_code == 200

    r = client.get("/v2/monitor")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert body["monitor"]["cycle_id"] == 42
    assert body["monitor"]["entry"]["symbol"] == "EURUSD"
    assert float(body["monitor"]["entry"]["open_proximity_pct"]) == 67.5
    assert body["monitor"]["close"]["dominant_close_reason"] == "risk_trailing_stop"
    assert int(body["monitor"]["close"]["positions_open"]) == 1
