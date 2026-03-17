from __future__ import annotations

import importlib


def _fresh_bridge(monkeypatch, tmp_path, *, enable_legacy_v1_compat: bool = False):
    monkeypatch.setenv("TRADER_RUNTIME_DB_PATH", str(tmp_path / "runtime.db"))
    monkeypatch.setenv("MT4_BRIDGE_ENABLE_V1_COMPAT", "1" if enable_legacy_v1_compat else "0")
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
    with bridge_mod.md_lock:
        bridge_mod.market_data.clear()
        bridge_mod.market_tick_history.clear()


def test_v2_command_endpoints_lifecycle(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    post = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert post.status_code == 200
    body = post.get_json()
    assert body["status"] == "queued"
    cid = str(body["command_id"])

    poll = client.get("/v2/commands/poll?format=line")
    assert poll.status_code == 200
    txt = poll.get_data(as_text=True)
    assert f"command_id={cid}" in txt
    assert "proto=v2" in txt

    ack = client.post("/v2/commands/ack", json={"command_id": cid, "status": "acked", "ticket": 42})
    assert ack.status_code == 200
    assert ack.get_json()["status"] == "acked"

    state = client.get("/v2/state")
    assert state.status_code == 200
    st = state.get_json()
    assert int(st.get("signals_sent", 0)) >= 1

    metrics = client.get("/v2/metrics")
    assert metrics.status_code == 200
    mm = metrics.get_json()
    assert int(mm["counters"]["commands_total"]) >= 1
    assert int(mm["counters"]["acked"]) >= 1
    assert int((mm.get("throughput", {}) or {}).get("executed_entries_5m", 0)) >= 1

    hist = client.get("/v2/commands/history?limit=20")
    assert hist.status_code == 200
    hh = hist.get_json()
    assert hh["status"] == "ok"
    assert isinstance(hh.get("commands"), list)
    assert len(hh["commands"]) >= 1

    ev = client.get(f"/v2/commands/events?command_id={cid}&limit=20")
    assert ev.status_code == 200
    ee = ev.get_json()
    assert ee["status"] == "ok"
    statuses = [str(row.get("status", "")) for row in ee.get("events", [])]
    assert statuses == ["queued", "delivered", "acked"]


def test_v2_command_ack_conflict_requires_delivery_first(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    post = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert post.status_code == 200
    cid = str(post.get_json()["command_id"])

    # Illegal queued -> acked transition should be rejected.
    ack = client.post("/v2/commands/ack", json={"command_id": cid, "status": "acked", "ticket": 9})
    assert ack.status_code == 409
    body = ack.get_json()
    assert body["status"] == "transition_conflict"
    assert body["current_status"] == "queued"
    assert body["requested_status"] == "acked"


def test_v2_commands_feed_metrics_and_poll(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    r = client.post("/v2/commands", json={"cmd": "INFO", "command_id": "v2-mirror-1", "thought": "mirror"})
    assert r.status_code == 200

    m = client.get("/v2/metrics")
    assert m.status_code == 200
    body = m.get_json()
    assert int(body["counters"]["commands_total"]) >= 1

    p = client.get("/v2/commands/poll")
    assert p.status_code == 200
    out = p.get_json()
    assert out["status"] in {"ok", "empty"}


def test_v2_governance_events_endpoint(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    r = client.post(
        "/v2/state/decisions",
        json={
            "decisions": [{"symbol": "EURUSD", "side": "BUY", "score": 0.61}],
            "vol": 0.0021,
            "diagnostics": {
                "last_diag": {"p_trend": 0.64, "vol": 0.0021, "score": 0.59, "score_effective": 0.61},
                "governance": {"paused": True, "risk_scale": 0.5, "reasons": ["soft_drawdown"]},
                "rejection_stats": {"spread_gate": 1},
            },
        },
    )
    assert r.status_code == 200

    ev = client.get("/v2/governance/events?limit=20")
    assert ev.status_code == 200
    body = ev.get_json()
    assert body["status"] == "ok"
    assert isinstance(body.get("events"), list)
    assert len(body.get("events", [])) >= 1

    mm = client.get("/v2/metrics").get_json()
    assert int((mm.get("decision_pipeline", {}) or {}).get("snapshots_5m", 0)) >= 1
    assert int((mm.get("governance", {}) or {}).get("events_24h", 0)) >= 1
    assert isinstance(((mm.get("decision_pipeline", {}) or {}).get("stage_attribution", {}) or {}).get("pipeline_rows", []), list)


def test_v2_state_includes_rolling_edge_diagnostics_fields(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    r = client.post(
        "/v2/state/decisions",
        json={
            "decisions": [{"symbol": "EURUSD", "side": "BUY", "score": 0.25}],
            "vol": 0.0015,
            "diagnostics": {
                "side_share_buy_rolling": 0.12,
                "side_share_sell_rolling": 0.88,
                "abstain_rate_rolling": 0.31,
                "edge_vs_random_hit_delta": 0.04,
                "edge_vs_random_expectancy_delta": 0.0017,
            },
        },
    )
    assert r.status_code == 200

    state = client.get("/v2/state")
    assert state.status_code == 200
    body = state.get_json()
    diag = dict(body.get("agent_diagnostics", {}) or {})
    assert float(diag.get("side_share_buy_rolling", 0.0)) == 0.12
    assert float(diag.get("side_share_sell_rolling", 0.0)) == 0.88
    assert float(diag.get("abstain_rate_rolling", 0.0)) == 0.31
    assert float(diag.get("edge_vs_random_hit_delta", 0.0)) == 0.04
    assert float(diag.get("edge_vs_random_expectancy_delta", 0.0)) == 0.0017


def test_v2_tick_and_reports_update_state(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    t = client.post(
        "/v2/market/tick",
        json={"symbol": "EURUSD", "bid": 1.10001, "ask": 1.10021, "spread": 1.2},
    )
    assert t.status_code == 200
    assert t.get_json()["status"] == "ok"

    ticks = client.get("/v2/market/ticks")
    assert ticks.status_code == 200
    body_ticks = ticks.get_json()
    assert "EURUSD" in body_ticks
    assert float(body_ticks["EURUSD"]["bid"]) > 0.0

    hb = client.post(
        "/v2/reports",
        json={
            "type": "HEARTBEAT",
            "equity": 12345.67,
            "margin": 345.0,
            "freemargin": 12000.0,
            "leverage": 200,
        },
    )
    assert hb.status_code == 200
    assert hb.get_json()["status"] == "ok"

    pos = client.post(
        "/v2/reports",
        json={
            "type": "POSITIONS",
            "positions": [{"symbol": "EURUSD", "type": 0, "lots": 0.1, "profit": 1.23}],
        },
    )
    assert pos.status_code == 200
    assert pos.get_json()["status"] == "ok"

    state = client.get("/v2/state")
    assert state.status_code == 200
    st = state.get_json()
    assert float(st.get("equity", 0.0)) == 12345.67
    assert isinstance(st.get("positions"), list)
    assert len(st.get("positions", [])) == 1
    assert st["positions"][0]["symbol"] == "EURUSD"


def test_v2_visuals_tap_non_consuming_and_limit(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    r1 = client.post("/v2/visuals", json={"symbol": "EURUSD", "type": "arrow", "side": "BUY", "time": 1})
    r2 = client.post("/v2/visuals", json={"symbol": "EURUSD", "type": "label", "text": "a", "time": 2})
    r3 = client.post("/v2/visuals", json={"symbol": "EURUSD", "type": "label", "text": "b", "time": 3})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200

    tap = client.get("/v2/visuals/tap?symbol=EURUSD&limit=2")
    assert tap.status_code == 200
    tap_payload = tap.get_json()
    assert isinstance(tap_payload, list)
    assert len(tap_payload) == 2
    assert tap_payload[0]["time"] == 2
    assert tap_payload[1]["time"] == 3

    # v2/visuals should still consume the full queue after non-consuming tap.
    consume = client.get("/v2/visuals?symbol=EURUSD")
    assert consume.status_code == 200
    consume_payload = consume.get_json()
    assert isinstance(consume_payload, list)
    assert len(consume_payload) == 3

    empty_after_consume = client.get("/v2/visuals?symbol=EURUSD")
    assert empty_after_consume.status_code == 200
    assert empty_after_consume.get_json() == []


def test_v2_visuals_tap_handles_missing_symbol_and_case_insensitive_lookup(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    missing = client.get("/v2/visuals/tap")
    assert missing.status_code == 400

    posted = client.post("/v2/visuals", json={"symbol": "EURUSD", "type": "arrow", "side": "SELL"})
    assert posted.status_code == 200

    tap = client.get("/v2/visuals/tap?symbol=eurusd&limit=999")
    assert tap.status_code == 200
    payload = tap.get_json()
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["symbol"] == "EURUSD"


def test_v1_transport_endpoints_are_removed(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    poll = client.get("/poll")
    assert poll.status_code == 404

    home = client.get("/")
    assert home.status_code == 404

    thought = client.post("/thought", json={"thought": "legacy"})
    assert thought.status_code == 404

    signal = client.post("/signal", json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert signal.status_code == 404

    ack = client.post("/ack", json={"signal_id": "x", "status": "acked"})
    assert ack.status_code == 404

    report = client.post("/report", data="HEARTBEAT")
    assert report.status_code == 404

    tick = client.post("/tick", json={"symbol": "EURUSD", "bid": 1.1, "ask": 1.1002})
    assert tick.status_code == 404

    reports_get = client.get("/reports")
    assert reports_get.status_code == 404

    health = client.get("/health")
    assert health.status_code == 404

    metrics = client.get("/metrics")
    assert metrics.status_code == 404

    state = client.get("/state")
    assert state.status_code == 404

    monitor = client.get("/monitor")
    assert monitor.status_code == 404

    visuals_post = client.post("/visuals", json={"symbol": "EURUSD", "type": "arrow"})
    assert visuals_post.status_code == 404

    visuals_get = client.get("/visuals?symbol=EURUSD")
    assert visuals_get.status_code == 404

    ticks_get = client.get("/ticks")
    assert ticks_get.status_code == 404

    bars_get = client.get("/bars?symbol=EURUSD")
    assert bars_get.status_code == 404

    execution = client.get("/execution")
    assert execution.status_code == 404

    diagnostics = client.get("/diagnostics")
    assert diagnostics.status_code == 404

    indicator = client.get("/indicator")
    assert indicator.status_code == 404

    thought_v2 = client.post("/v2/thought", json={"thought": "hello-v2"})
    assert thought_v2.status_code == 200
    assert thought_v2.get_json()["status"] == "ok"

    state_decisions = client.post("/state/decisions", json={"decisions": [], "vol": 0.0, "diagnostics": {}})
    assert state_decisions.status_code == 404

    decisions_post = client.post("/decisions", json={"decisions": [], "vol": 0.0, "diagnostics": {}})
    assert decisions_post.status_code == 404

    decisions_get = client.get("/decisions")
    assert decisions_get.status_code == 404

    v2 = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1})
    assert v2.status_code == 200
    assert v2.get_json()["status"] == "queued"


def test_v1_transport_endpoints_compat_mode(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path, enable_legacy_v1_compat=True)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    poll = client.get("/poll")
    assert poll.status_code == 200
    assert poll.headers.get("X-Bridge-Legacy-Compat") == "1"
    assert "text/plain" in str(poll.headers.get("Content-Type", "")).lower()

    report = client.post("/report", data="HEARTBEAT eq=12345 margin=120 freemargin=12225")
    assert report.status_code == 200
    assert report.get_data(as_text=True).strip() == "OK"
    assert report.headers.get("X-Bridge-Legacy-Compat") == "1"

    tick = client.post("/tick", json={"symbol": "EURUSD", "bid": 1.1, "ask": 1.1002, "spread": 1.2})
    assert tick.status_code == 200
    assert tick.get_data(as_text=True).strip() == "OK"
    assert tick.headers.get("X-Bridge-Legacy-Compat") == "1"

    ticks = client.get("/v2/market/ticks")
    assert ticks.status_code == 200
    assert "EURUSD" in ticks.get_json()

    visuals_post = client.post("/v2/visuals", json={"symbol": "EURUSD", "type": "arrow", "side": "BUY"})
    assert visuals_post.status_code == 200

    indicator = client.get("/indicator?symbol=EURUSD")
    assert indicator.status_code == 200
    payload = indicator.get_json()
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["symbol"] == "EURUSD"
    assert indicator.headers.get("X-Bridge-Legacy-Compat") == "1"


def test_v1_indicator_converts_hud_payload_for_legacy_parser(monkeypatch, tmp_path):
    bridge = _fresh_bridge(monkeypatch, tmp_path, enable_legacy_v1_compat=True)
    _reset_bridge_state(bridge)
    client = bridge.app.test_client()

    tick = client.post(
        "/tick",
        json={"symbol": "EURUSD", "bid": 1.1010, "ask": 1.1012, "spread": 1.2},
    )
    assert tick.status_code == 200

    visual = client.post(
        "/v2/visuals",
        json={
            "symbol": "EURUSD",
            "type": "hud",
            "action": "Scanning",
            "score": 0.42,
            "trend": 0.61,
            "sharpe": 1.10,
        },
    )
    assert visual.status_code == 200

    indicator = client.get("/indicator?symbol=EURUSD")
    assert indicator.status_code == 200
    payload = indicator.get_json()
    assert isinstance(payload, list)
    assert len(payload) == 1
    row = dict(payload[0] or {})
    assert row.get("type") == "label"
    assert row.get("symbol") == "EURUSD"
    assert isinstance(row.get("text"), str) and "Scanning" in row.get("text", "")
