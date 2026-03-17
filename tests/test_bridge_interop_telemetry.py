from __future__ import annotations

import importlib
import json
import time


def _fresh_bridge(monkeypatch, tmp_path):
    trace_path = tmp_path / "transport_trace.jsonl"
    monkeypatch.setenv("TRADER_RUNTIME_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("MT4_INTEROP_AUDIT_ENABLED", "1")
    monkeypatch.setenv("MT4_INTEROP_AUDIT_TRACE_PATH", str(trace_path))
    monkeypatch.setenv("MT4_INTEROP_AUDIT_SAMPLE_RATE", "1")
    monkeypatch.setenv("MT4_INTEROP_AUDIT_MODE", "live_shadow")

    mod = importlib.import_module("bridge_api.bridge")
    importlib.reload(mod)
    return mod, trace_path


def _reset_bridge_state(bridge_mod) -> None:
    with bridge_mod.report_lock:
        bridge_mod.reports.clear()
    with bridge_mod.interop_lock:
        for q in bridge_mod.interop_latency_samples.values():
            q.clear()
        bridge_mod.interop_error_budget.clear()
    with bridge_mod.md_lock:
        bridge_mod.market_data.clear()
        bridge_mod.market_tick_history.clear()


def test_ack_flow_emits_interop_trace_and_metrics(monkeypatch, tmp_path):
    bridge, trace_path = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    command_id = "interop-unit-1"
    t_py = float(time.time() - 0.2)
    r = client.post(
        "/v2/commands",
        json={
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "command_id": command_id,
            "trace_id": "trace-interop-unit-1",
            "t_py_signal_post_start": t_py,
            "audit_session_id": "session-1",
            "audit_profile": "steady_1rps_30m",
            "interop_mode": "live_shadow",
            "thought": "interop probe",
        },
    )
    assert r.status_code == 200

    p = client.get("/v2/commands/poll?format=line")
    assert p.status_code == 200
    assert command_id in p.get_data(as_text=True)

    ack = client.post(
        "/v2/commands/ack",
        json={
            "command_id": command_id,
            "status": "acked",
            "trace_id": "trace-interop-unit-1",
            "t_ea_received": float(time.time() - 0.05),
            "t_ea_exec_start": float(time.time() - 0.04),
            "t_ea_exec_end": float(time.time() - 0.03),
            "t_ea_ack_post": float(time.time() - 0.01),
            "ea_handle_to_ack_ms": 42.0,
        },
    )
    assert ack.status_code == 200

    m = client.get("/v2/metrics").get_json()
    assert "interop" in m
    interop = dict(m["interop"])
    assert interop["enabled"] is True
    latency = dict(interop["latency"])
    assert int(latency["signal_post_to_ack_ms"]["count"]) >= 1

    assert trace_path.exists()
    rows = []
    with trace_path.open("r", encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    assert any(str(r.get("signal_id")) == command_id for r in rows)
    row = [r for r in rows if str(r.get("signal_id")) == command_id][-1]
    assert "stage_latencies_ms" in row
    assert float((row["stage_latencies_ms"] or {}).get("ea_handle_to_ack_ms", 0.0)) >= 0.0
