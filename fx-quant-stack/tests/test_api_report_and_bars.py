from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _fresh_client(tmp_path: Path) -> TestClient:
    os.environ["FXSTACK_DATABASE_URL"] = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_RUNTIME_ALLOW_CREATE_ALL"] = "1"
    if "fxstack.runtime.db_tools" in sys.modules:
        del sys.modules["fxstack.runtime.db_tools"]
    from fxstack.runtime.db_tools import migrate_database

    result = migrate_database(database_url=os.environ["FXSTACK_DATABASE_URL"])
    assert bool(result.get("ok")), result
    if "fxstack.settings" in sys.modules:
        from fxstack.settings import get_settings

        get_settings.cache_clear()
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
    assert body.get("database_ok") is True


def test_v2_state_reports_current_database_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _fresh_client(tmp_path)
    app_module = sys.modules["fxstack.api.app"]
    monkeypatch.setattr(
        app_module.service,
        "get_health",
        lambda: {"tables_ok": False, "database": "unreachable"},
    )

    body = c.get("/v2/state").json()

    assert body["database_ok"] is False
    assert body["database_status"] == "unreachable"
    assert body["status_tier"] == "bridge_up_db_unhealthy"


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


def test_report_json_rejects_nonfinite_values_without_persistence(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    finite_fields = (
        "profit",
        "swap",
        "commission",
        "net_profit",
        "equity",
        "margin",
        "freemargin",
        "leverage",
    )

    for field in finite_fields:
        for constant in ("NaN", "Infinity", "-Infinity"):
            response = c.post(
                "/v2/reports",
                headers={"content-type": "application/json"},
                content=f'{{"{field}":{constant}}}',
            )

            assert response.status_code == 422, (field, constant, response.text)
            assert response.json()["error"]["code"] == "http_422"

    assert c.get("/v2/reports").json()["reports"] == []


@pytest.mark.parametrize(
    "payload",
    [
        '{"report_type":"diagnostic","metadata":{"metric":NaN}}',
        '{"report_type":"diagnostic","extra_metric":Infinity}',
        '{"report_type":"diagnostic","samples":[{"metric":1e999}]}',
    ],
)
def test_report_json_rejects_nested_extra_and_overflow_nonfinite_values(
    tmp_path: Path,
    payload: str,
) -> None:
    c = _fresh_client(tmp_path)

    response = c.post(
        "/v2/reports",
        headers={"content-type": "application/json"},
        content=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "http_422"
    assert c.get("/v2/reports").json()["reports"] == []


def test_closed_trades_normalizes_nonfinite_legacy_rows_to_strict_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _fresh_client(tmp_path)
    app_module = sys.modules["fxstack.api.app"]
    monkeypatch.setattr(
        app_module.service,
        "get_closed_trade_reports",
        lambda limit=200: [
            {
                "ts": float("inf"),
                "report_text": "",
                "report_json": {
                    "report_type": "closed_trade",
                    "ticket": float("inf"),
                    "symbol": "EURUSD",
                    "type": float("nan"),
                    "lots": float("nan"),
                    "open_price": float("inf"),
                    "close_price": float("-inf"),
                    "open_time": float("nan"),
                    "close_time": float("inf"),
                    "profit": float("nan"),
                    "swap": float("inf"),
                    "commission": float("-inf"),
                    "net_profit": float("nan"),
                },
            }
        ],
    )

    response = c.get("/v2/closed-trades")

    assert response.status_code == 200

    def reject_constant(value: str) -> None:
        raise AssertionError(f"non-standard JSON constant leaked: {value}")

    payload = json.loads(response.text, parse_constant=reject_constant)
    trade = payload["trades"][0]
    for field in (
        "lots",
        "open_price",
        "close_price",
        "profit",
        "swap",
        "commission",
        "net_profit",
        "report_ts",
    ):
        assert math.isfinite(float(trade[field])), field
    assert trade["ticket"] == -1
    assert trade["type"] == -1
    assert trade["close_time_epoch"] is None
    assert trade["duration_secs"] is None


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


def test_mid_only_tick_remains_positive_in_ticks_and_bars(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    mid = 1.2345

    response = c.post(
        "/v2/market/tick",
        json={
            "symbol": "EURUSD",
            "mid": mid,
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )

    assert response.status_code == 200
    tick = dict(c.get("/v2/market/ticks").json().get("EURUSD", {}) or {})
    assert float(tick["mid"]) == pytest.approx(mid)
    bars = c.get("/v2/market/bars", params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10}).json()["bars"]
    assert len(bars) == 1
    assert float(bars[0]["close"]) == pytest.approx(mid)
    assert bars[0]["bid_close"] is None
    assert bars[0]["ask_close"] is None
    assert bars[0]["spread"] is None


def test_mid_only_tick_does_not_dilute_observed_bar_spread(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    observed_bid = 1.2344
    observed_ask = 1.2346

    assert c.post(
        "/v2/market/tick",
        json={
            "symbol": "EURUSD",
            "bid": observed_bid,
            "ask": observed_ask,
            "time": "2026-01-01T00:00:10Z",
        },
    ).status_code == 200
    assert c.post(
        "/v2/market/tick",
        json={"symbol": "EURUSD", "mid": 1.2347, "time": "2026-01-01T00:00:20Z"},
    ).status_code == 200

    bars = c.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    ).json()["bars"]
    assert len(bars) == 1
    assert float(bars[0]["close"]) == pytest.approx(1.2347)
    assert float(bars[0]["bid_close"]) == pytest.approx(observed_bid)
    assert float(bars[0]["ask_close"]) == pytest.approx(observed_ask)
    assert float(bars[0]["spread"]) == pytest.approx(observed_ask - observed_bid)


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
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    assert r.status_code == 200
    ticks = c.get("/v2/market/ticks").json()
    tick = dict(ticks.get("USDJPY", {}) or {})
    assert float(tick.get("spread_bps", 0.0)) > 0.0
    assert str(tick.get("spread_unit_source", "")).startswith("tick.")


def test_market_tick_without_timestamp_uses_receipt_time(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    before = time.time()

    r = c.post("/v2/market/tick", json={"symbol": "EURUSD", "bid": 1.1, "ask": 1.1002})
    after = time.time()

    assert r.status_code == 200
    tick = dict(c.get("/v2/market/ticks").json().get("EURUSD", {}) or {})
    assert before <= float(tick["ts_epoch"]) <= after


@pytest.mark.parametrize("remote_time", ["not-a-timestamp", 9_999_999_999.0])
def test_market_tick_invalid_or_future_timestamp_cannot_poison_freshness(
    tmp_path: Path,
    remote_time: str | float,
) -> None:
    c = _fresh_client(tmp_path)
    before = time.time()

    r = c.post(
        "/v2/market/tick",
        json={"symbol": "EURUSD", "bid": 1.1, "ask": 1.1002, "time": remote_time},
    )
    after = time.time()

    assert r.status_code == 200
    tick = dict(c.get("/v2/market/ticks").json().get("EURUSD", {}) or {})
    assert before <= float(tick["ts_epoch"]) <= after


@pytest.mark.parametrize(
    "payload",
    [
        {"bid": 1.1, "ask": 1.1002},
        {"symbol": "EURUSD", "bid": 0.0, "ask": 0.0},
        {"symbol": "EURUSD", "bid": 1.1002, "ask": 1.1},
    ],
)
def test_market_tick_rejects_unusable_quotes(tmp_path: Path, payload: dict[str, float | str]) -> None:
    c = _fresh_client(tmp_path)

    r = c.post("/v2/market/tick", json=payload)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"


def test_market_bars_invalid_timeframe_returns_http_error(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)

    r = c.get("/v2/market/bars", params={"symbol": "EURUSD", "timeframe": "W7"})

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "http_400"
    assert "Unsupported timeframe" in r.json()["error"]["message"]
