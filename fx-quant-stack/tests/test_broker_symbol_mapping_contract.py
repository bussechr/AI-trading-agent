from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


def _fresh_client(tmp_path: Path) -> TestClient:
    os.environ["FXSTACK_DATABASE_URL"] = (
        f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    )
    os.environ["FXSTACK_RUNTIME_ALLOW_CREATE_ALL"] = "1"

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


def test_reported_broker_symbol_and_mapping_diagnostics_reach_api_consumers(
    tmp_path: Path,
) -> None:
    client = _fresh_client(tmp_path)
    assert client.post(
        "/v2/reports",
        json={"report_type": "heartbeat", "equity": 10_000.0},
    ).status_code == 200
    assert client.post(
        "/v2/reports",
        json={
            "report_type": "bridge_status",
            "configured_pairs": ["EURUSD", "GBPUSD"],
            "symbol_readiness": {
                "EURUSD": {
                    "broker_symbol": "EURUSD.DFB",
                    "supported": True,
                    "selected": True,
                    "mapping_ambiguous": False,
                    "mapping_reason": "ok",
                    "mapping_kind": "selected_partial",
                    "mapping_candidate_count": 1,
                },
                "GBPUSD": {
                    "broker_symbol": "",
                    "supported": False,
                    "selected": False,
                    "mapping_ambiguous": True,
                    "mapping_reason": "ambiguous_selected_partial_match",
                    "mapping_kind": "selected_partial",
                    "mapping_candidate_count": 2,
                },
            },
        },
    ).status_code == 200
    assert client.post(
        "/v2/reports",
        json={
            "report_type": "symbol_specs",
            "specs": {
                "EURUSD": {
                    "broker_symbol": "EURUSD.DFB",
                    "lot_size": 100_000.0,
                    "point": 0.00001,
                    "digits": 5,
                }
            },
        },
    ).status_code == 200

    state = client.get("/v2/state").json()
    readiness = state["symbol_readiness"]
    assert readiness["EURUSD"] == {
        "broker_symbol": "EURUSD.DFB",
        "supported": True,
        "selected": True,
        "mapping_ambiguous": False,
        "mapping_reason": "ok",
        "mapping_kind": "selected_partial",
        "mapping_candidate_count": 1,
    }
    assert readiness["GBPUSD"]["mapping_ambiguous"] is True
    assert (
        readiness["GBPUSD"]["mapping_reason"]
        == "ambiguous_selected_partial_match"
    )
    assert readiness["GBPUSD"]["mapping_candidate_count"] == 2
    assert "GBPUSD" in state["unsupported_pairs"]

    specs = client.get("/v2/market/specs").json()["specs"]
    assert specs["EURUSD"]["broker_symbol"] == "EURUSD.DFB"
    assert specs["EURUSD"]["lot_size"] == 100_000.0


def test_tick_consumer_preserves_the_raw_broker_symbol(tmp_path: Path) -> None:
    client = _fresh_client(tmp_path)
    response = client.post(
        "/v2/market/tick",
        json={
            "symbol": "EURUSD",
            "broker_symbol": "EURUSD.DFB",
            "bid": 1.10000,
            "ask": 1.10020,
            "source_event_token": "9001",
        },
    )

    assert response.status_code == 200
    tick = client.get("/v2/market/ticks").json()["EURUSD"]
    assert tick["symbol"] == "EURUSD"
    assert tick["broker_symbol"] == "EURUSD.DFB"

