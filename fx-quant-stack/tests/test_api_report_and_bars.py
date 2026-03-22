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


def test_report_heartbeat_updates_state(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post("/v2/reports", content="HEARTBEAT eq=10001.5 margin=100 freemargin=9901.5 lev=200")
    assert r.status_code == 200

    s = c.get("/v2/state")
    body = s.json()
    assert float(body.get("equity", 0.0)) == 10001.5
    assert float(body.get("margin", 0.0)) == 100.0
    assert float(body.get("freemargin", 0.0)) == 9901.5


def test_report_json_payload_updates_state(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post(
        "/v2/reports",
        json={"equity": 10123.4, "margin": 50.5, "freemargin": 10072.9, "leverage": 200},
    )
    assert r.status_code == 200
    state = c.get("/v2/state").json()
    assert float(state.get("equity", 0.0)) == 10123.4
    assert float(state.get("margin", 0.0)) == 50.5
    assert float(state.get("freemargin", 0.0)) == 10072.9


def test_report_empty_json_body_does_not_500(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post("/v2/reports", headers={"content-type": "application/json"}, content="")
    assert r.status_code == 200


def test_report_malformed_json_body_does_not_500(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post("/v2/reports", headers={"content-type": "application/json"}, content="{invalid")
    assert r.status_code == 200


def test_market_bars_aggregates_ticks(tmp_path: Path):
    c = _fresh_client(tmp_path)

    ticks = [
        {"symbol": "EURUSD", "bid": 1.1, "ask": 1.1002, "spread": 0.2, "time": "2026-01-01T00:00:01Z"},
        {"symbol": "EURUSD", "bid": 1.1001, "ask": 1.1003, "spread": 0.2, "time": "2026-01-01T00:00:20Z"},
        {"symbol": "EURUSD", "bid": 1.1002, "ask": 1.1004, "spread": 0.2, "time": "2026-01-01T00:00:40Z"},
    ]
    for t in ticks:
        r = c.post("/v2/market/tick", json=t)
        assert r.status_code == 200

    out = c.get("/v2/market/bars", params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10})
    assert out.status_code == 200
    bars = list(out.json().get("bars", []))
    assert len(bars) >= 1
    first = bars[-1]
    assert float(first["high"]) >= float(first["open"])
    assert float(first["low"]) <= float(first["close"])


def test_market_tick_normalizes_spread_units(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post(
        "/v2/market/tick",
        json={
            "symbol": "USDJPY",
            "bid": 150.000,
            "ask": 150.006,
            "spread_points": 6,
            "spread_pips": 0.6,
            "digits": 3,
            "time": "2026-01-01T00:00:00Z",
        },
    )
    assert r.status_code == 200
    ticks = c.get("/v2/market/ticks").json()
    tick = dict(ticks.get("USDJPY", {}) or {})
    assert float(tick.get("spread_bps", 0.0)) > 0.0
    assert str(tick.get("spread_unit_source", "")).startswith("tick.")
