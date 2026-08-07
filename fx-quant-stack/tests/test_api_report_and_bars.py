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
from sqlalchemy import func, select

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION


_PRODUCER_IDENTITY = "ig-mt4-production-ea"
_PRODUCER_INSTANCE_ID = "mt4-terminal-instance-a"
_TERMINAL_LEASE_SCOPE = "ig-mt4-terminal-scope"
_CREDENTIAL_GENERATION_ID = "ig-mt4-generation-1"


def _fresh_client(tmp_path: Path) -> TestClient:
    os.environ["FXSTACK_DATABASE_URL"] = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_RUNTIME_ALLOW_CREATE_ALL"] = "1"
    # Do NOT evict fxstack.runtime.db_tools here. It reads the environment at
    # call time and caches nothing, so re-importing buys nothing -- but it does
    # rebind MigrationResourcesError to a fresh class object while modules that
    # already imported from it (postgres_store) keep raising the original. That
    # split makes `pytest.raises(db_tools.MigrationResourcesError)` in later
    # tests silently unmatchable.
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


def _authenticated_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setenv("FXSTACK_START_PROFILE", "staged_safe")
    monkeypatch.setenv("FXSTACK_BRIDGE_CONSUMER_IDENTITY", _PRODUCER_IDENTITY)
    monkeypatch.setenv(
        "FXSTACK_BRIDGE_TERMINAL_LEASE_SCOPE",
        _TERMINAL_LEASE_SCOPE,
    )
    monkeypatch.setenv(
        "FXSTACK_BRIDGE_CREDENTIAL_GENERATION_ID",
        _CREDENTIAL_GENERATION_ID,
    )
    return _fresh_client(tmp_path)


def _market_source_payload(
    *,
    account_scope: str = "ig-demo-account",
    broker_server: str = "IG-DEMO",
    broker_company: str = "IG Europe GmbH",
    consumer_identity: str = _PRODUCER_IDENTITY,
    producer_instance_id: str = _PRODUCER_INSTANCE_ID,
    terminal_lease_scope: str = _TERMINAL_LEASE_SCOPE,
    credential_generation_id: str = _CREDENTIAL_GENERATION_ID,
    protocol_version: str = BRIDGE_PROTOCOL_VERSION,
) -> dict[str, object]:
    return {
        "broker_account_scope": account_scope,
        "broker_account_scope_schema": "fxstack_mt4_account_scope_djb2_xor32_v1",
        "broker_account_scope_version": 1,
        "broker_server": broker_server,
        "broker_company": broker_company,
        "consumer_identity": consumer_identity,
        "producer_instance_id": producer_instance_id,
        "terminal_lease_scope": terminal_lease_scope,
        "credential_generation_id": credential_generation_id,
        "bridge_protocol_version": protocol_version,
    }


def _post_authenticated_heartbeat(client: TestClient) -> None:
    response = client.post(
        "/v2/reports",
        json={
            "report_type": "heartbeat",
            "broker_account_mode": "demo",
            "broker_account_magic": 246810,
            **_market_source_payload(),
        },
    )
    assert response.status_code == 200, response.text


def _direct_mt4_bar(
    time_value: str,
    *,
    volume: int = 321,
) -> dict[str, object]:
    return {
        "time": time_value,
        "open": 1.1001,
        "high": 1.1011,
        "low": 1.0991,
        "close": 1.1006,
        "bid_open": 1.1000,
        "bid_high": 1.1010,
        "bid_low": 1.0990,
        "bid_close": 1.1005,
        "spread": 0.0002,
        "volume": volume,
        "volume_source": "mt4_ivolume_tick_count_v1",
        "price_basis": "mt4_bid_ohlc_v1",
    }


def test_authenticated_heartbeat_accepts_ig_group_limited_demo_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)

    response = client.post(
        "/v2/reports",
        json={
            "report_type": "heartbeat",
            "broker_account_mode": "demo",
            "broker_account_magic": 246810,
            **_market_source_payload(broker_company="IG Group Limited"),
        },
    )

    assert response.status_code == 200, response.text
    state = client.get("/v2/state").json()
    assert state["broker_company"] == "IG Group Limited"
    assert state["bridge_market_source"]["broker_venue_id"] == "ig_mt4"

    tick = client.post(
        "/v2/market/tick",
        json={
            "symbol": "EURUSD",
            "bid": 1.1000,
            "ask": 1.1002,
            "source_event_token": "ig-group-demo-1",
            **_market_source_payload(broker_company="IG Group Limited"),
        },
    )
    bridge_status = client.post(
        "/v2/reports",
        json={
            "report_type": "bridge_status",
            **_market_source_payload(broker_company="IG Group Limited"),
        },
    )

    assert tick.status_code == 200, tick.text
    assert bridge_status.status_code == 200, bridge_status.text
    stored_tick = client.get("/v2/market/ticks").json()["EURUSD"]
    assert stored_tick["broker_venue_id"] == "ig_mt4"
    assert stored_tick["producer_instance_id"] == _PRODUCER_INSTANCE_ID


def test_authenticated_tick_batch_renews_once_and_shares_receipt_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]

    lease_channels: list[str] = []
    original_claim = app_module.service.claim_bridge_consumer_lease

    def tracked_claim(**kwargs: object) -> dict[str, object]:
        lease_channels.append(str(kwargs.get("channel") or ""))
        return original_claim(**kwargs)

    monkeypatch.setattr(
        app_module.service,
        "claim_bridge_consumer_lease",
        tracked_claim,
    )
    persisted_batch_sizes: list[int] = []
    original_record_ticks = app_module.service.record_ticks

    def tracked_record_ticks(payloads: list[dict[str, object]]) -> None:
        persisted_batch_sizes.append(len(payloads))
        original_record_ticks(payloads)

    monkeypatch.setattr(app_module.service, "record_ticks", tracked_record_ticks)
    clock = [time.time()]

    def advancing_clock() -> float:
        clock[0] += 0.001
        return clock[0]

    monkeypatch.setattr(app_module, "_utc_now_ts", advancing_clock)
    source = _market_source_payload()
    baseline = client.post(
        "/v2/market/ticks",
        json={
            **source,
            "ticks": [
                {
                    "symbol": "EURUSD",
                    "broker_symbol": "EURUSD",
                    "bid": 1.1000,
                    "ask": 1.1002,
                    "source_event_token": "eur-1",
                },
                {
                    "symbol": "GBPUSD",
                    "broker_symbol": "GBPUSD",
                    "bid": 1.2000,
                    "ask": 1.2003,
                    "source_event_token": "gbp-1",
                },
            ],
        },
    )

    assert baseline.status_code == 200, baseline.text
    assert baseline.json() == {"status": "ok", "accepted": 2, "persisted": 0}
    assert lease_channels == ["tick"]
    assert persisted_batch_sizes == []
    first_eur = dict(app_module._market_ticks_mem["EURUSD"])
    first_gbp = dict(app_module._market_ticks_mem["GBPUSD"])
    first_eur_get = dict(client.get("/v2/market/ticks").json()["EURUSD"])
    assert first_eur["received_at_epoch"] == first_gbp["received_at_epoch"]
    assert first_eur["provider"] == "mt4_bridge"
    assert first_eur["canonical_symbol"] == "EURUSD"
    assert first_eur["pair"] == "EURUSD"
    assert first_eur["venue"] == "ig_mt4"
    assert first_eur["instrument"] == {
        "canonical_symbol": "EURUSD",
        "pair": "EURUSD",
        "venue": "ig_mt4",
    }
    assert first_eur_get["provider"] == "mt4_bridge"
    assert first_eur_get["instrument"] == first_eur["instrument"]
    assert first_eur["market_event_trigger"] == "baseline"
    assert first_gbp["market_event_trigger"] == "baseline"

    advanced = client.post(
        "/v2/market/ticks",
        json={
            **source,
            "ticks": [
                {
                    "symbol": "EURUSD",
                    "broker_symbol": "EURUSD",
                    "bid": 1.1000,
                    "ask": 1.1002,
                    "source_event_token": "eur-2",
                },
                {
                    "symbol": "GBPUSD",
                    "broker_symbol": "GBPUSD",
                    "bid": 1.2000,
                    "ask": 1.2003,
                    "source_event_token": "gbp-2",
                },
            ],
        },
    )

    assert advanced.status_code == 200, advanced.text
    assert advanced.json() == {"status": "ok", "accepted": 2, "persisted": 2}
    assert lease_channels == ["tick", "tick"]
    assert persisted_batch_sizes == [2]
    second_eur = dict(app_module._market_ticks_mem["EURUSD"])
    second_gbp = dict(app_module._market_ticks_mem["GBPUSD"])
    assert second_eur["received_at_epoch"] == second_gbp["received_at_epoch"]
    assert second_eur["received_at_epoch"] > first_eur["received_at_epoch"]
    assert second_eur["market_event_trigger"] == "source_event_token_changed"
    assert second_gbp["market_event_trigger"] == "source_event_token_changed"
    assert second_eur["market_source_id"] == second_gbp["market_source_id"]

    single = client.post(
        "/v2/market/tick",
        json={
            **source,
            "symbol": "EURUSD",
            "bid": 1.1001,
            "ask": 1.1003,
            "source_event_token": "eur-3",
        },
    )
    assert single.status_code == 200, single.text
    assert single.json() == {"status": "ok"}


def test_tick_batch_does_not_publish_memory_when_bulk_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]
    source = _market_source_payload()
    symbols = ("EURUSD", "GBPUSD")
    baseline = client.post(
        "/v2/market/ticks",
        json={
            **source,
            "ticks": [
                {
                    "symbol": symbol,
                    "bid": 1.1 + index / 10.0,
                    "ask": 1.1002 + index / 10.0,
                    "source_event_token": f"{symbol}-baseline",
                }
                for index, symbol in enumerate(symbols)
            ],
        },
    )
    assert baseline.status_code == 200, baseline.text
    before_ticks = {
        symbol: dict(app_module._market_ticks_mem[symbol]) for symbol in symbols
    }
    before_history = {
        symbol: list(app_module._market_tick_history[symbol]) for symbol in symbols
    }

    def fail_bulk_persistence(_payloads: list[dict[str, object]]) -> None:
        raise RuntimeError("synthetic_tick_batch_write_failure")

    monkeypatch.setattr(app_module.service, "record_ticks", fail_bulk_persistence)
    with pytest.raises(RuntimeError, match="synthetic_tick_batch_write_failure"):
        client.post(
            "/v2/market/ticks",
            json={
                **source,
                "ticks": [
                    {
                        "symbol": symbol,
                        "bid": 1.2 + index / 10.0,
                        "ask": 1.2002 + index / 10.0,
                        "source_event_token": f"{symbol}-advanced",
                    }
                    for index, symbol in enumerate(symbols)
                ],
            },
        )

    assert {
        symbol: dict(app_module._market_ticks_mem[symbol]) for symbol in symbols
    } == before_ticks
    assert {
        symbol: list(app_module._market_tick_history[symbol]) for symbol in symbols
    } == before_history


def test_empty_api_generation_requests_exactly_one_ea_seed_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]
    monkeypatch.setattr(
        app_module,
        "_BAR_SEED_RECOVERY_PRODUCER_IDENTITY",
        _PRODUCER_IDENTITY,
    )
    source = _market_source_payload()
    payload = {
        **source,
        "ticks": [
            {
                "symbol": "EURUSD",
                "broker_symbol": "EURUSD",
                "bid": 1.1000,
                "ask": 1.1002,
                "source_event_token": "seed-request-1",
            }
        ],
    }

    requested = client.post("/v2/market/ticks", json=payload)
    accepted = client.post("/v2/market/ticks", json=payload)

    assert requested.status_code == 503
    assert "direct_m1_seed_recovery_requested" in requested.text
    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"status": "ok", "accepted": 1, "persisted": 0}


def test_tick_batch_is_bounded_and_rejects_duplicate_normalized_symbols(
    tmp_path: Path,
) -> None:
    client = _fresh_client(tmp_path)
    quote = {"bid": 1.1000, "ask": 1.1002}

    empty = client.post("/v2/market/ticks", json={"ticks": []})
    oversized = client.post(
        "/v2/market/ticks",
        json={
            "ticks": [{"symbol": f"PAIR{index:02d}", **quote} for index in range(65)]
        },
    )
    duplicate = client.post(
        "/v2/market/ticks",
        json={
            "ticks": [
                {"symbol": "eurusd", **quote},
                {"symbol": " EURUSD ", **quote},
            ]
        },
    )

    assert empty.status_code == 422
    assert oversized.status_code == 422
    assert duplicate.status_code == 422
    assert "unique after normalization" in duplicate.text


def test_report_heartbeat_updates_state(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post(
        "/v2/reports",
        content=(
            "HEARTBEAT eq=10001.5 margin=100 freemargin=9901.5 lev=200 "
            "account_mode=demo account_scope=-12345 account_magic=246810"
        ),
    )
    assert r.status_code == 200

    s = c.get("/v2/state")
    body = s.json()
    assert float(body.get("equity", 0.0)) == 10001.5
    assert float(body.get("margin", 0.0)) == 100.0
    assert float(body.get("freemargin", 0.0)) == 9901.5
    assert body["broker_account_mode"] == "demo"
    assert body["broker_account_scope"] == "-12345"
    assert body["broker_account_magic"] == 246810
    assert body.get("database_ok") is True


def test_legacy_heartbeat_clears_stale_broker_account_attestation(
    tmp_path: Path,
) -> None:
    c = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {"broker_account_mode": "real", "broker_account_scope": "stale-account"}
    )

    response = c.post(
        "/v2/reports", content="HEARTBEAT eq=10000 margin=0 freemargin=10000"
    )

    assert response.status_code == 200
    state = c.get("/v2/state").json()
    assert state["broker_account_mode"] == "unknown"
    assert state["broker_account_scope"] == ""


def test_plain_report_heartbeat_near_miss_cannot_refresh_liveness_or_identity(
    tmp_path: Path,
) -> None:
    c = _fresh_client(tmp_path)
    from fxstack.api.app import service

    stale_heartbeat = "2026-01-01T00:00:00+00:00"
    service.patch_state(
        {
            "system_status": "disconnected",
            "last_heartbeat": stale_heartbeat,
            "broker_account_mode": "real",
            "broker_account_scope": "trusted-account",
            "broker_account_magic": 246810,
        }
    )

    response = c.post(
        "/v2/reports",
        content=(
            "HEARTBEAT_BOGUS account_mode=demo "
            "account_scope=spoofed-account account_magic=999"
        ),
    )

    assert response.status_code == 200
    state = service.get_state()
    assert state["system_status"] == "disconnected"
    assert state["last_heartbeat"] == stale_heartbeat
    assert state["broker_account_mode"] == "real"
    assert state["broker_account_scope"] == "trusted-account"
    assert state["broker_account_magic"] == 246810


def test_v2_state_reports_current_database_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_report_json_heartbeat_payload_updates_state(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post(
        "/v2/reports",
        json={
            "report_type": "heartbeat",
            "equity": 10123.4,
            "margin": 50.5,
            "freemargin": 10072.9,
            "leverage": 200,
        },
    )
    assert r.status_code == 200
    state = c.get("/v2/state").json()
    assert float(state.get("equity", 0.0)) == 10123.4
    assert float(state.get("margin", 0.0)) == 50.5
    assert float(state.get("freemargin", 0.0)) == 10072.9


def test_positions_snapshot_does_not_refresh_heartbeat_or_mutate_identity(
    tmp_path: Path,
) -> None:
    c = _fresh_client(tmp_path)
    from fxstack.api.app import service

    stale_heartbeat = "2026-01-01T00:00:00+00:00"
    service.patch_state(
        {
            "system_status": "disconnected",
            "last_heartbeat": stale_heartbeat,
            "broker_account_mode": "real",
            "broker_account_scope": "trusted-account",
            "broker_account_magic": 246810,
        }
    )

    response = c.post(
        "/v2/reports",
        json={
            "report_type": "positions_snapshot",
            "positions": [{"symbol": "EURUSD", "lots": 0.1}],
            "broker_account_mode": "demo",
            "broker_account_scope": "spoofed-account",
            "broker_account_magic": 999,
        },
    )

    assert response.status_code == 200
    state = service.get_state()
    assert state["positions"] == [{"symbol": "EURUSD", "lots": 0.1}]
    assert state["system_status"] == "disconnected"
    assert state["last_heartbeat"] == stale_heartbeat
    assert state["broker_account_mode"] == "real"
    assert state["broker_account_scope"] == "trusted-account"
    assert state["broker_account_magic"] == 246810


def test_json_heartbeat_refreshes_liveness_and_resets_omitted_identity(
    tmp_path: Path,
) -> None:
    c = _fresh_client(tmp_path)
    from fxstack.api.app import service

    stale_heartbeat = "2026-01-01T00:00:00+00:00"
    service.patch_state(
        {
            "system_status": "disconnected",
            "last_heartbeat": stale_heartbeat,
            "broker_account_mode": "real",
            "broker_account_scope": "stale-account",
            "broker_account_magic": 246810,
        }
    )

    response = c.post(
        "/v2/reports",
        json={"report_type": "heartbeat", "equity": 10050.0},
    )

    assert response.status_code == 200
    state = service.get_state()
    assert state["system_status"] == "connected"
    assert state["last_heartbeat"] != stale_heartbeat
    assert state["broker_account_mode"] == "unknown"
    assert state["broker_account_scope"] == ""
    assert state["broker_account_magic"] == 0


def test_json_heartbeat_parses_current_broker_identity(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)

    response = c.post(
        "/v2/reports",
        json={
            "report_type": "heartbeat",
            "broker_account_mode": "demo",
            "broker_account_scope": "demo-account",
            "broker_account_magic": 246810,
        },
    )

    assert response.status_code == 200
    state = sys.modules["fxstack.api.app"].service.get_state()
    assert state["broker_account_mode"] == "demo"
    assert state["broker_account_scope"] == "demo-account"
    assert state["broker_account_magic"] == 246810


def test_report_empty_json_body_does_not_500(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post("/v2/reports", headers={"content-type": "application/json"}, content="")
    assert r.status_code == 200


def test_report_malformed_json_body_does_not_500(tmp_path: Path):
    c = _fresh_client(tmp_path)
    r = c.post(
        "/v2/reports", headers={"content-type": "application/json"}, content="{invalid"
    )
    assert r.status_code == 200


def test_report_json_rejects_nonfinite_values_without_persistence(
    tmp_path: Path,
) -> None:
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
        {
            "symbol": "EURUSD",
            "bid": 1.1,
            "ask": 1.1002,
            "spread": 0.2,
            "time": "2026-01-01T00:00:01Z",
            "source_event_token": "1001",
        },
        {
            "symbol": "EURUSD",
            "bid": 1.1001,
            "ask": 1.1003,
            "spread": 0.2,
            "time": "2026-01-01T00:00:20Z",
            "source_event_token": "1002",
        },
        {
            "symbol": "EURUSD",
            "bid": 1.1002,
            "ask": 1.1004,
            "spread": 0.2,
            "time": "2026-01-01T00:00:40Z",
            "source_event_token": "1003",
        },
    ]
    for t in ticks:
        r = c.post("/v2/market/tick", json=t)
        assert r.status_code == 200

    out = c.get(
        "/v2/market/bars", params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10}
    )
    assert out.status_code == 200
    bars = list(out.json().get("bars", []))
    assert len(bars) >= 1
    first = bars[-1]
    assert float(first["high"]) >= float(first["open"])
    assert float(first["low"]) <= float(first["close"])
    # The first token establishes broker-event identity; only the two
    # confirmed advances enter the fallback bar cache.
    assert first["volume"] == 2
    assert first["volume_source"] == "bridge_market_event_count_v1"
    assert first["price_basis"] == "bridge_tick_mid_ohlc_v1"


def test_market_bar_history_batch_survives_without_tick_history(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    payload = {
        "symbol": "EURUSD",
        "timeframe": "M5",
        "bars": [
            {
                "time": "2026-01-01T00:00:00Z",
                "open": 1.1000,
                "high": 1.1010,
                "low": 1.0990,
                "close": 1.1005,
                "spread": 0.0002,
                "volume": 100,
            },
            {
                "time": "2026-01-01T00:05:00Z",
                "open": 1.1005,
                "high": 1.1020,
                "low": 1.1000,
                "close": 1.1015,
                "spread": 0.0002,
                "volume": 120,
            },
        ],
    }

    posted = c.post("/v2/market/bars", json=payload)
    assert posted.status_code == 200
    assert posted.json()["accepted"] == 2

    response = c.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M5", "limit": 10},
    )
    assert response.status_code == 200
    bars = response.json()["bars"]
    assert len(bars) == 2
    assert float(bars[-1]["close"]) == pytest.approx(1.1015)
    assert float(bars[-1]["bid_close"]) == pytest.approx(1.1014)
    assert float(bars[-1]["ask_close"]) == pytest.approx(1.1016)
    assert isinstance(bars[-1]["volume"], int)
    assert bars[-1]["volume_source"] == "legacy_unspecified_volume_v1"
    assert bars[-1]["price_basis"] == "legacy_synthetic_mid_ohlc_v1"


def test_market_bar_history_preserves_exact_mt4_bid_ohlc_and_ivolume(
    tmp_path: Path,
) -> None:
    c = _fresh_client(tmp_path)
    bar = {
        "time": "2026-01-01T00:00:00Z",
        "open": 1.1001,
        "high": 1.1011,
        "low": 1.0991,
        "close": 1.1006,
        "bid_open": 1.1000,
        "bid_high": 1.1010,
        "bid_low": 1.0990,
        "bid_close": 1.1005,
        "spread": 0.0002,
        "volume": 321,
        "volume_source": "mt4_ivolume_tick_count_v1",
        "price_basis": "mt4_bid_ohlc_v1",
    }

    posted = c.post(
        "/v2/market/bars",
        json={"symbol": "EURUSD", "timeframe": "M1", "bars": [bar]},
    )
    assert posted.status_code == 200, posted.text

    response = c.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    )
    assert response.status_code == 200
    bars = response.json()["bars"]
    assert len(bars) == 1
    actual = bars[0]
    for field in ("bid_open", "bid_high", "bid_low", "bid_close"):
        assert float(actual[field]) == pytest.approx(float(bar[field]))
    assert actual["volume"] == 321
    assert isinstance(actual["volume"], int)
    assert actual["volume_source"] == "mt4_ivolume_tick_count_v1"
    assert actual["price_basis"] == "mt4_bid_ohlc_v1"
    assert actual["source_timeframe"] == "M1"


def test_direct_mt4_coverage_proof_is_small_source_bound_and_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    app_module = sys.modules["fxstack.api.app"]
    monkeypatch.setattr(app_module, "_DIRECT_M1_FULL_SNAPSHOT_MIN_ROWS", 3)
    _post_authenticated_heartbeat(client)
    batch = {
        "symbol": "EURUSD",
        "timeframe": "M1",
        "bars": [
            _direct_mt4_bar("2026-01-01T00:00:00Z"),
            _direct_mt4_bar("2026-01-01T00:01:00Z", volume=400),
        ],
        **_market_source_payload(),
    }
    posted = client.post("/v2/market/bars", json=batch)
    assert posted.status_code == 200, posted.text

    ready = client.get(
        "/v2/market/bars/coverage",
        params={"symbol": "eurusd", "timeframe": "m1", "minimum": 2},
    )
    assert ready.status_code == 200, ready.text
    assert len(ready.content) < 512
    assert ready.json() == {
        "schema": "fxstack.direct_mt4_bar_coverage.v1",
        "symbol": "EURUSD",
        "timeframe": "M1",
        "minimum": 2,
        "direct_row_count": 2,
        "contiguous_direct_row_count": 2,
        "full_snapshot_attested": False,
        "latest_direct_time": "2026-01-01T00:01:00+00:00",
        "ready": True,
    }

    insufficient = client.get(
        "/v2/market/bars/coverage",
        params={"symbol": "EURUSD", "timeframe": "M1", "minimum": 3},
    )
    assert insufficient.status_code == 200
    assert insufficient.json()["ready"] is False
    assert insufficient.json()["direct_row_count"] == 2
    assert insufficient.json()["contiguous_direct_row_count"] == 2

    gap_batch = {
        "symbol": "EURUSD",
        "timeframe": "M1",
        "bars": [_direct_mt4_bar("2026-01-01T00:03:00Z", volume=401)],
        **_market_source_payload(),
    }
    gap_posted = client.post("/v2/market/bars", json=gap_batch)
    assert gap_posted.status_code == 200, gap_posted.text
    gapped = client.get(
        "/v2/market/bars/coverage",
        params={"symbol": "EURUSD", "timeframe": "M1", "minimum": 3},
    )
    assert gapped.status_code == 200
    assert gapped.json()["direct_row_count"] == 3
    assert gapped.json()["contiguous_direct_row_count"] == 1
    assert gapped.json()["ready"] is False

    attested_batch = {
        "symbol": "USDJPY",
        "timeframe": "M1",
        "bars": [
            _direct_mt4_bar("2026-01-01T00:00:00Z"),
            _direct_mt4_bar("2026-01-01T00:01:00Z"),
            _direct_mt4_bar("2026-01-01T00:03:00Z"),
        ],
        **_market_source_payload(),
    }
    attested_post = client.post("/v2/market/bars", json=attested_batch)
    assert attested_post.status_code == 200, attested_post.text
    attested = client.get(
        "/v2/market/bars/coverage",
        params={"symbol": "USDJPY", "timeframe": "M1", "minimum": 3},
    )
    assert attested.status_code == 200
    assert attested.json()["contiguous_direct_row_count"] == 1
    assert attested.json()["full_snapshot_attested"] is True
    assert attested.json()["ready"] is True

    next_bar = {
        "symbol": "USDJPY",
        "timeframe": "M1",
        "bars": [_direct_mt4_bar("2026-01-01T00:04:00Z")],
        **_market_source_payload(),
    }
    assert client.post("/v2/market/bars", json=next_bar).status_code == 200
    advanced = client.get(
        "/v2/market/bars/coverage",
        params={"symbol": "USDJPY", "timeframe": "M1", "minimum": 3},
    )
    assert advanced.json()["full_snapshot_attested"] is True
    assert advanced.json()["ready"] is True

    new_gap = {
        "symbol": "USDJPY",
        "timeframe": "M1",
        "bars": [_direct_mt4_bar("2026-01-01T00:06:00Z")],
        **_market_source_payload(),
    }
    assert client.post("/v2/market/bars", json=new_gap).status_code == 200
    invalidated = client.get(
        "/v2/market/bars/coverage",
        params={"symbol": "USDJPY", "timeframe": "M1", "minimum": 3},
    )
    assert invalidated.json()["full_snapshot_attested"] is False
    assert invalidated.json()["ready"] is False


def test_authenticated_direct_mt4_completed_bar_is_idempotent_and_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]
    receipt_clock = [time.time()]
    monkeypatch.setattr(app_module, "_utc_now_ts", lambda: receipt_clock[0])
    bar = _direct_mt4_bar("2026-01-01T00:00:00Z")
    batch = {
        "symbol": "EURUSD",
        "timeframe": "M1",
        "bars": [bar],
        **_market_source_payload(),
    }

    first = client.post("/v2/market/bars", json=batch)
    first_receipt = receipt_clock[0]
    receipt_clock[0] += 0.3
    identical_repost = client.post("/v2/market/bars", json=batch)

    assert first.status_code == 200, first.text
    assert identical_repost.status_code == 200, identical_repost.text
    assert identical_repost.json()["accepted"] == 1
    assert identical_repost.json()["retained"] == 1

    direct_mutations: tuple[tuple[str, object], ...] = (
        ("bid_open", 1.10001),
        ("bid_high", 1.10101),
        ("bid_low", 1.09899),
        ("bid_close", 1.10051),
        ("volume", 322),
    )
    for field, value in direct_mutations:
        conflict = client.post(
            "/v2/market/bars",
            json={**batch, "bars": [{**bar, field: value}]},
        )
        assert conflict.status_code == 409, conflict.text
        error = conflict.json()["error"]
        assert error["message"] == "completed_direct_mt4_bar_immutable_conflict"
        assert error["detail"]["reason"] == (
            "authenticated_completed_bar_mutation_rejected"
        )
        assert error["detail"]["conflicting_fields"] == [field]

    legacy_replacement = {
        field: value
        for field, value in bar.items()
        if field
        not in {
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "volume_source",
            "price_basis",
        }
    }
    provenance_conflict = client.post(
        "/v2/market/bars",
        json={**batch, "bars": [legacy_replacement]},
    )
    assert provenance_conflict.status_code == 409, provenance_conflict.text
    assert provenance_conflict.json()["error"]["detail"]["conflicting_fields"] == [
        "volume_source",
        "price_basis",
    ]

    stored = client.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    ).json()["bars"]
    assert len(stored) == 1
    assert stored[0]["received_at_epoch"] == pytest.approx(first_receipt)
    assert stored[0]["provider"] == "mt4_bridge"
    assert stored[0]["canonical_symbol"] == "EURUSD"
    assert stored[0]["pair"] == "EURUSD"
    assert stored[0]["venue"] == "ig_mt4"
    for field in (
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "volume",
        "volume_source",
        "price_basis",
    ):
        assert stored[0][field] == bar[field]

    new_bar = client.post(
        "/v2/market/bars",
        json={
            **batch,
            "bars": [_direct_mt4_bar("2026-01-01T00:01:00Z", volume=400)],
        },
    )
    assert new_bar.status_code == 200, new_bar.text
    assert new_bar.json()["accepted"] == 1
    assert new_bar.json()["retained"] == 2

    client_supplied_receipt = client.post(
        "/v2/market/bars",
        json={
            **batch,
            "bars": [{**bar, "received_at_epoch": first_receipt}],
        },
    )
    assert client_supplied_receipt.status_code == 422


def test_authenticated_market_bar_must_be_completed_before_server_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]
    current_minute = int(time.time() // 60) * 60
    monkeypatch.setattr(
        app_module,
        "_utc_now_ts",
        lambda: float(current_minute + 2),
    )

    response = client.post(
        "/v2/market/bars",
        json={
            "symbol": "EURUSD",
            "timeframe": "M1",
            "bars": [_direct_mt4_bar(current_minute)],
            **_market_source_payload(),
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"] == (
        "bar batch contains a bar that is not completed"
    )


def test_authenticated_direct_mt4_mutation_rejects_entire_batch_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    original = _direct_mt4_bar("2026-01-01T00:00:00Z")
    source = _market_source_payload()
    accepted = client.post(
        "/v2/market/bars",
        json={
            "symbol": "EURUSD",
            "timeframe": "M1",
            "bars": [original],
            **source,
        },
    )
    assert accepted.status_code == 200, accepted.text

    response = client.post(
        "/v2/market/bars",
        json={
            "symbol": "EURUSD",
            "timeframe": "M1",
            "bars": [
                _direct_mt4_bar("2026-01-01T00:01:00Z", volume=400),
                {**original, "volume": 322},
            ],
            **source,
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["detail"]["conflicting_fields"] == ["volume"]
    stored = client.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    ).json()["bars"]
    assert len(stored) == 1
    assert stored[0]["time"] == "2026-01-01T00:00:00+00:00"
    assert stored[0]["volume"] == 321


def test_authenticated_legacy_bar_repost_does_not_invent_first_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    batch = {
        "symbol": "EURUSD",
        "timeframe": "M1",
        "bars": [_direct_mt4_bar("2026-01-01T00:00:00Z")],
        **_market_source_payload(),
    }
    assert client.post("/v2/market/bars", json=batch).status_code == 200

    app_module = sys.modules["fxstack.api.app"]
    stored_row = app_module._market_bar_history[("EURUSD", "M1")][0]
    stored_row.pop("received_at_epoch")

    repost = client.post("/v2/market/bars", json=batch)
    rows = client.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    ).json()["bars"]

    assert repost.status_code == 200, repost.text
    assert len(rows) == 1
    assert rows[0]["received_at_epoch"] is None


@pytest.mark.parametrize(
    ("update", "removed_field"),
    [
        ({"volume": 321.5}, None),
        ({}, "bid_close"),
        ({"volume_source": "bridge_market_event_count_v1"}, None),
        ({"bid_high": 1.0999}, None),
    ],
)
def test_market_bar_history_rejects_ambiguous_direct_mt4_contract(
    tmp_path: Path,
    update: dict[str, object],
    removed_field: str | None,
) -> None:
    c = _fresh_client(tmp_path)
    bar: dict[str, object] = {
        "time": "2026-01-01T00:00:00Z",
        "open": 1.1001,
        "high": 1.1011,
        "low": 1.0991,
        "close": 1.1006,
        "bid_open": 1.1000,
        "bid_high": 1.1010,
        "bid_low": 1.0990,
        "bid_close": 1.1005,
        "spread": 0.0002,
        "volume": 321,
        "volume_source": "mt4_ivolume_tick_count_v1",
        "price_basis": "mt4_bid_ohlc_v1",
    }
    bar.update(update)
    if removed_field is not None:
        bar.pop(removed_field)

    response = c.post(
        "/v2/market/bars",
        json={"symbol": "EURUSD", "timeframe": "M1", "bars": [bar]},
    )

    assert response.status_code == 422


def test_authenticated_tick_rejects_interleaved_producer_account_and_venue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    quote = {
        "symbol": "EURUSD",
        "bid": 1.1000,
        "ask": 1.1002,
        **_market_source_payload(),
    }
    assert (
        client.post(
            "/v2/market/tick",
            json={**quote, "source_event_token": "source-a-1"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v2/market/tick",
            json={**quote, "source_event_token": "source-a-2"},
        ).status_code
        == 200
    )

    cloned_terminal = client.post(
        "/v2/market/tick",
        json={
            **quote,
            **_market_source_payload(
                producer_instance_id="mt4-terminal-instance-b",
            ),
            "bid": 9.0,
            "ask": 9.1,
            "source_event_token": "source-b-1",
        },
    )
    foreign_account_and_venue = client.post(
        "/v2/market/tick",
        json={
            **quote,
            **_market_source_payload(
                account_scope="foreign-account",
                broker_server="OTHER-SERVER",
                broker_company="Other Broker",
            ),
            "bid": 8.0,
            "ask": 8.1,
            "source_event_token": "source-c-1",
        },
    )
    stale_protocol = client.post(
        "/v2/market/tick",
        json={
            **quote,
            **_market_source_payload(protocol_version="v0.0.0"),
            "source_event_token": "source-d-1",
        },
    )

    assert cloned_terminal.status_code == 409
    assert foreign_account_and_venue.status_code == 409
    assert stale_protocol.status_code == 409
    tick = client.get("/v2/market/ticks").json()["EURUSD"]
    assert float(tick["bid"]) == pytest.approx(1.1000)
    assert tick["market_source_authenticated"] is True
    assert tick["broker_account_scope"] == "ig-demo-account"
    assert tick["broker_venue_id"] == "ig_mt4"
    assert tick["producer_identity"] == _PRODUCER_IDENTITY
    assert tick["producer_instance_id"] == _PRODUCER_INSTANCE_ID
    assert tick["bridge_protocol_version"] == BRIDGE_PROTOCOL_VERSION

    app_module = sys.modules["fxstack.api.app"]
    with app_module.service.store.engine.begin() as conn:
        rows = (
            conn.execute(select(app_module.service.store.market_ticks)).mappings().all()
        )
    assert len(rows) == 1
    assert rows[0]["market_source_authenticated"] == 1
    assert rows[0]["producer_identity"] == _PRODUCER_IDENTITY
    assert rows[0]["producer_instance_id"] == _PRODUCER_INSTANCE_ID


def test_authenticated_bar_history_rejects_interleaved_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    bar = {
        "time": "2026-08-03T00:00:00Z",
        "open": 1.1000,
        "high": 1.1010,
        "low": 1.0990,
        "close": 1.1005,
        "spread": 0.0002,
        "volume": 100,
    }
    accepted = client.post(
        "/v2/market/bars",
        json={
            "symbol": "EURUSD",
            "timeframe": "M1",
            "bars": [bar],
            **_market_source_payload(),
        },
    )
    foreign = client.post(
        "/v2/market/bars",
        json={
            "symbol": "EURUSD",
            "timeframe": "M1",
            "bars": [{**bar, "close": 8.0, "high": 8.0}],
            **_market_source_payload(
                account_scope="foreign-account",
                broker_server="OTHER-SERVER",
                broker_company="Other Broker",
            ),
        },
    )
    cloned_terminal = client.post(
        "/v2/market/bars",
        json={
            "symbol": "EURUSD",
            "timeframe": "M1",
            "bars": [bar],
            **_market_source_payload(
                producer_instance_id="mt4-terminal-instance-b",
            ),
        },
    )

    assert accepted.status_code == 200, accepted.text
    assert foreign.status_code == 409
    assert cloned_terminal.status_code == 409
    bars = client.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    ).json()["bars"]
    assert len(bars) == 1
    assert float(bars[0]["close"]) == pytest.approx(1.1005)
    assert bars[0]["market_source_authenticated"] is True
    assert bars[0]["producer_identity"] == _PRODUCER_IDENTITY
    assert bars[0]["producer_instance_id"] == _PRODUCER_INSTANCE_ID
    assert bars[0]["broker_account_scope"] == "ig-demo-account"


def test_source_bound_broker_truth_reports_stamp_source_and_clear_on_rollover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    source = _market_source_payload()

    specs = client.post(
        "/v2/reports",
        json={
            "report_type": "symbol_specs",
            **source,
            "account_leverage": 200,
            "specs": {
                "EURUSD": {
                    "broker_symbol": "EURUSD",
                    "trade_allowed": False,
                    "lot_size": 100000,
                    "point": 0.00001,
                }
            },
        },
    )
    status = client.post(
        "/v2/reports",
        json={
            "report_type": "bridge_status",
            **source,
            "configured_pairs": ["EURUSD"],
            "symbol_readiness": {
                "EURUSD": {
                    "broker_symbol": "EURUSD",
                    "supported": True,
                    "selected": True,
                }
            },
        },
    )
    positions = client.post(
        "/v2/reports",
        json={
            "report_type": "positions_snapshot",
            "schema_version": "fxstack_mt4_positions_snapshot_v2",
            **source,
            "positions": [{"symbol": "EURUSD", "ticket": 17, "lots": 0.1}],
        },
    )
    closed = client.post(
        "/v2/reports",
        json={
            "report_type": "closed_trade",
            **source,
            "ticket": 16,
            "symbol": "EURUSD",
            "lots": 0.1,
            "open_time": 1_800_000_000,
            "close_time": 1_800_000_060,
        },
    )

    assert [
        response.status_code for response in (specs, status, positions, closed)
    ] == [
        200,
        200,
        200,
        200,
    ]
    state = client.get("/v2/state").json()
    source_id = state["bridge_market_source"]["market_source_id"]
    assert (
        state["bridge_market_source"]["producer_instance_id"] == _PRODUCER_INSTANCE_ID
    )
    assert state["symbol_specs"]["EURUSD"]["trade_allowed"] is False
    assert state["symbol_specs_market_source_id"] == source_id
    assert state["positions_snapshot_market_source_id"] == source_id
    assert state["bridge_status_market_source_id"] == source_id
    specs_surface = client.get("/v2/market/specs").json()
    assert specs_surface["market_source_id"] == source_id
    assert specs_surface["market_source"] == state["bridge_market_source"]
    reconcile = client.get("/v2/positions/reconcile").json()
    assert reconcile["ea_market_source_id"] == source_id
    assert reconcile["market_source_matches"] is True
    trade = client.get("/v2/closed-trades").json()["trades"][0]
    assert trade["market_source_id"] == source_id
    assert trade["producer_instance_id"] == _PRODUCER_INSTANCE_ID

    app_module = sys.modules["fxstack.api.app"]
    app_module.service.patch_state({"bridge_consumer_lease": {"expires_at": 0.0}})
    replacement = _market_source_payload(producer_instance_id="mt4-terminal-instance-b")
    rollover = client.post(
        "/v2/reports",
        json={
            "report_type": "heartbeat",
            "broker_account_mode": "demo",
            "broker_account_magic": 246810,
            **replacement,
        },
    )
    assert rollover.status_code == 200, rollover.text
    rolled = client.get("/v2/state").json()
    assert (
        rolled["bridge_market_source"]["producer_instance_id"]
        == "mt4-terminal-instance-b"
    )
    assert rolled["symbol_specs"] == {}
    assert rolled["positions"] == []
    assert rolled["symbol_readiness"] == {}
    assert rolled["symbol_specs_market_source_id"] == ""
    assert rolled["positions_snapshot_market_source_id"] == ""

    stale_source_report = client.post(
        "/v2/reports",
        json={
            "report_type": "symbol_specs",
            **source,
            "specs": {"EURUSD": {"trade_allowed": True}},
        },
    )
    legacy_structured_report = client.post(
        "/v2/reports",
        json={
            "report_type": "positions_snapshot",
            "positions": [],
        },
    )
    assert stale_source_report.status_code == 409
    assert legacy_structured_report.status_code == 403


def test_command_channel_same_instance_resumes_and_clone_is_excluded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]
    app_module.service.store._execution_egress_authorization_failure = (  # type: ignore[method-assign]
        lambda conn, *, now_ts=None, command=None: ""
    )
    queued = client.post(
        "/v2/commands",
        json={"cmd": "INFO", "command_id": "producer-instance-replay"},
    )
    assert queued.status_code == 200, queued.text
    auth = {
        "consumer_identity": _PRODUCER_IDENTITY,
        "producer_instance_id": _PRODUCER_INSTANCE_ID,
        "terminal_lease_scope": _TERMINAL_LEASE_SCOPE,
        "credential_generation_id": _CREDENTIAL_GENERATION_ID,
        "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
    }

    polled = client.get("/v2/commands/poll", params=auth)
    assert polled.status_code == 200, polled.text
    assert polled.json()["command"]["command_id"] == "producer-instance-replay"
    ack_payload = {
        **auth,
        "command_id": "producer-instance-replay",
        "status": "acked",
    }
    first_ack = client.post("/v2/commands/ack", json=ack_payload)
    replay_ack = client.post("/v2/commands/ack", json=ack_payload)
    assert first_ack.status_code == 200, first_ack.text
    assert replay_ack.status_code == 200, replay_ack.text
    assert replay_ack.json()["idempotent"] is True

    clone = client.get(
        "/v2/commands/poll",
        params={**auth, "producer_instance_id": "mt4-terminal-instance-b"},
    )
    missing_instance_ack = client.post(
        "/v2/commands/ack",
        json={
            key: value
            for key, value in ack_payload.items()
            if key != "producer_instance_id"
        },
    )
    assert clone.status_code == 409
    assert missing_instance_ack.status_code == 403


def test_market_bar_history_resamples_completed_m5_context(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    payload = {
        "symbol": "EURUSD",
        "timeframe": "M5",
        "bars": [
            {
                "time": "2026-01-04T23:45:00Z",
                "open": 1.1000,
                "high": 1.1010,
                "low": 1.0990,
                "close": 1.1005,
                "spread": 0.0002,
                "volume": 100,
            },
            {
                "time": "2026-01-04T23:50:00Z",
                "open": 1.1005,
                "high": 1.1020,
                "low": 1.1000,
                "close": 1.1015,
                "spread": 0.0003,
                "volume": 120,
            },
            {
                "time": "2026-01-04T23:55:00Z",
                "open": 1.1015,
                "high": 1.1030,
                "low": 1.1010,
                "close": 1.1025,
                "spread": 0.0004,
                "volume": 140,
            },
        ],
    }
    assert c.post("/v2/market/bars", json=payload).status_code == 200

    h1 = c.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "H1", "limit": 10},
    ).json()["bars"]
    daily = c.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "D", "limit": 10},
    ).json()["bars"]

    for bars in (h1, daily):
        assert len(bars) == 1
        assert bars[0]["source_timeframe"] == "M5"
        assert float(bars[0]["open"]) == pytest.approx(1.1000)
        assert float(bars[0]["high"]) == pytest.approx(1.1030)
        assert float(bars[0]["low"]) == pytest.approx(1.0990)
        assert float(bars[0]["close"]) == pytest.approx(1.1025)
        assert float(bars[0]["spread"]) == pytest.approx(0.0003)
        assert int(bars[0]["volume"]) == 360


def test_mid_only_tick_remains_positive_in_ticks_and_bars(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    mid = 1.2345

    baseline = c.post(
        "/v2/market/tick",
        json={
            "symbol": "EURUSD",
            "mid": mid,
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_event_token": "2001",
        },
    )
    response = c.post(
        "/v2/market/tick",
        json={
            "symbol": "EURUSD",
            "mid": mid,
            "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source_event_token": "2002",
        },
    )

    assert baseline.status_code == 200
    assert response.status_code == 200
    tick = dict(c.get("/v2/market/ticks").json().get("EURUSD", {}) or {})
    assert float(tick["mid"]) == pytest.approx(mid)
    bars = c.get(
        "/v2/market/bars", params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10}
    ).json()["bars"]
    assert len(bars) == 1
    assert float(bars[0]["close"]) == pytest.approx(mid)
    assert bars[0]["bid_close"] is None
    assert bars[0]["ask_close"] is None
    assert bars[0]["spread"] is None


def test_mid_only_tick_does_not_dilute_observed_bar_spread(tmp_path: Path) -> None:
    c = _fresh_client(tmp_path)
    observed_bid = 1.2344
    observed_ask = 1.2346

    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "EURUSD",
                "bid": observed_bid,
                "ask": observed_ask,
                "time": "2026-01-01T00:00:10Z",
                "source_event_token": "3001",
            },
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "EURUSD",
                "bid": observed_bid,
                "ask": observed_ask,
                "time": "2026-01-01T00:00:11Z",
                "source_event_token": "3002",
            },
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "EURUSD",
                "mid": 1.2347,
                "time": "2026-01-01T00:00:20Z",
                "source_event_token": "3003",
            },
        ).status_code
        == 200
    )

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


def _persisted_market_tick_count() -> int:
    from fxstack.api.app import service

    with service.store.engine.begin() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(service.store.market_ticks)
            ).scalar_one()
        )


def test_duplicate_source_event_keeps_transport_fresh_but_market_event_stales(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _fresh_client(tmp_path)
    app_module = sys.modules["fxstack.api.app"]
    clock = [1_800_000_000.0]
    monkeypatch.setattr(app_module, "_utc_now_ts", lambda: clock[0])
    quote = {
        "symbol": "EURUSD",
        "bid": 1.1000,
        "ask": 1.1002,
    }

    assert (
        c.post(
            "/v2/market/tick",
            json={**quote, "source_event_token": "4001"},
        ).status_code
        == 200
    )
    baseline = dict(c.get("/v2/market/ticks").json()["EURUSD"])
    assert baseline["market_event_fresh"] is False
    assert baseline["market_event_reason"] == "broker_market_event_baseline_unconfirmed"
    assert _persisted_market_tick_count() == 0

    clock[0] += 1.0
    assert (
        c.post(
            "/v2/market/tick",
            json={**quote, "source_event_token": "4002"},
        ).status_code
        == 200
    )
    advanced = dict(c.get("/v2/market/ticks").json()["EURUSD"])
    assert advanced["market_event_fresh"] is True
    assert advanced["market_event_reason"] == "ok"
    assert _persisted_market_tick_count() == 1
    first_bars = c.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    ).json()["bars"]
    assert len(first_bars) == 1

    clock[0] += 31.0
    assert (
        c.post(
            "/v2/market/tick",
            json={**quote, "source_event_token": "4002"},
        ).status_code
        == 200
    )
    duplicate = dict(c.get("/v2/market/ticks").json()["EURUSD"])
    state = c.get("/v2/state").json()
    duplicate_bars = c.get(
        "/v2/market/bars",
        params={"symbol": "EURUSD", "timeframe": "M1", "limit": 10},
    ).json()["bars"]

    assert state["ticks_fresh"] is True
    assert state["market_event_fresh"] is False
    assert (
        state["market_event_by_symbol"]["EURUSD"]["reason"]
        == "broker_market_event_stale"
    )
    assert duplicate["transport_fresh"] is True
    assert duplicate["market_event_fresh"] is False
    assert duplicate["market_event_reason"] == "broker_market_event_stale"
    assert float(duplicate["market_event_age_secs"]) == pytest.approx(31.0)
    assert float(duplicate["received_at_epoch"]) == pytest.approx(clock[0])
    assert float(duplicate["market_event_received_at_epoch"]) == pytest.approx(
        float(advanced["market_event_received_at_epoch"])
    )
    assert _persisted_market_tick_count() == 1
    assert duplicate_bars == first_bars


def test_source_token_or_quote_change_advances_market_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _fresh_client(tmp_path)
    app_module = sys.modules["fxstack.api.app"]
    clock = [1_800_100_000.0]
    monkeypatch.setattr(app_module, "_utc_now_ts", lambda: clock[0])

    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "EURUSD",
                "bid": 1.2000,
                "ask": 1.2002,
                "source_event_token": "5001",
            },
        ).status_code
        == 200
    )
    clock[0] += 1.0
    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "EURUSD",
                "bid": 1.2000,
                "ask": 1.2002,
                "source_event_token": "5002",
            },
        ).status_code
        == 200
    )
    token_advanced = dict(c.get("/v2/market/ticks").json()["EURUSD"])
    assert token_advanced["market_event_fresh"] is True
    assert token_advanced["market_event_trigger"] == "source_event_token_changed"

    clock[0] += 1.0
    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "EURUSD",
                "bid": 1.2001,
                "ask": 1.2003,
                "source_event_token": "5002",
            },
        ).status_code
        == 200
    )
    quote_advanced = dict(c.get("/v2/market/ticks").json()["EURUSD"])
    assert quote_advanced["market_event_fresh"] is True
    assert quote_advanced["market_event_trigger"] == "quote_changed"
    assert _persisted_market_tick_count() == 2


def test_missing_or_first_source_identity_fails_closed_per_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _fresh_client(tmp_path)
    app_module = sys.modules["fxstack.api.app"]
    clock = [1_800_200_000.0]
    monkeypatch.setattr(app_module, "_utc_now_ts", lambda: clock[0])

    assert (
        c.post(
            "/v2/market/tick",
            json={"symbol": "EURUSD", "bid": 1.3000, "ask": 1.3002},
        ).status_code
        == 200
    )
    missing = dict(c.get("/v2/market/ticks").json()["EURUSD"])
    assert missing["market_event_identity_present"] is False
    assert missing["market_event_fresh"] is False
    assert missing["market_event_reason"] == "broker_market_event_identity_missing"

    clock[0] += 1.0
    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "EURUSD",
                "bid": 1.3000,
                "ask": 1.3002,
                "source_event_token": "6001",
            },
        ).status_code
        == 200
    )
    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "GBPUSD",
                "bid": 1.4000,
                "ask": 1.4002,
                "source_event_token": "7001",
            },
        ).status_code
        == 200
    )
    clock[0] += 1.0
    assert (
        c.post(
            "/v2/market/tick",
            json={
                "symbol": "GBPUSD",
                "bid": 1.4000,
                "ask": 1.4002,
                "source_event_token": "7002",
            },
        ).status_code
        == 200
    )
    ticks = c.get("/v2/market/ticks").json()

    assert ticks["EURUSD"]["market_event_fresh"] is False
    assert (
        ticks["EURUSD"]["market_event_reason"]
        == "broker_market_event_baseline_unconfirmed"
    )
    assert ticks["GBPUSD"]["market_event_fresh"] is True


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
def test_market_tick_rejects_unusable_quotes(
    tmp_path: Path, payload: dict[str, float | str]
) -> None:
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


def test_heartbeat_account_mode_attestation_parsing() -> None:
    """The exploration_demo entry fence depends on this parser contract: every
    heartbeat is authoritative, so a heartbeat WITHOUT account_mode resets the
    attestation to 'unknown' (fail-closed), and the mock-EA heartbeat format
    with account_mode=demo attests demo."""

    from fxstack.api.app import _state_patch_from_heartbeat_text

    # The mock-EA / BridgeEA format attests demo.
    attested = _state_patch_from_heartbeat_text(
        "HEARTBEAT eq=10000.00 account_mode=demo account_scope=mock-ea-demo account_magic=0"
    )
    assert attested["broker_account_mode"] == "demo"
    assert attested["broker_account_scope"] == "mock-ea-demo"
    assert attested["equity"] == 10000.0

    # A bare legacy heartbeat authoritatively CLEARS any prior attestation.
    bare = _state_patch_from_heartbeat_text("HEARTBEAT eq=10000.00")
    assert bare["broker_account_mode"] == "unknown"
    assert bare["broker_account_scope"] == ""

    # An unrecognized mode value must not attest anything.
    junk = _state_patch_from_heartbeat_text("HEARTBEAT eq=1 account_mode=demoo")
    assert junk["broker_account_mode"] == "unknown"
