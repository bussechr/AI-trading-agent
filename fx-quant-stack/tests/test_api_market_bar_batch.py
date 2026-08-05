from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS


_PRODUCER_IDENTITY = "ig-mt4-production-ea"
_PRODUCER_INSTANCE_ID = "mt4-terminal-instance-a"
_TERMINAL_LEASE_SCOPE = "ig-mt4-terminal-scope"
_CREDENTIAL_GENERATION_ID = "ig-mt4-generation-1"


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
    sys.modules.pop("fxstack.api.app", None)
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
    producer_instance_id: str = _PRODUCER_INSTANCE_ID,
) -> dict[str, object]:
    return {
        "broker_account_scope": "ig-demo-account",
        "broker_account_scope_schema": "fxstack_mt4_account_scope_djb2_xor32_v1",
        "broker_account_scope_version": 1,
        "broker_server": "IG-DEMO",
        "broker_company": "IG Europe GmbH",
        "consumer_identity": _PRODUCER_IDENTITY,
        "producer_instance_id": producer_instance_id,
        "terminal_lease_scope": _TERMINAL_LEASE_SCOPE,
        "credential_generation_id": _CREDENTIAL_GENERATION_ID,
        "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
    }


def test_in_memory_market_history_is_bounded_for_mt4_responsiveness(
    tmp_path: Path,
) -> None:
    client = _fresh_client(tmp_path)
    try:
        app_module = sys.modules["fxstack.api.app"]
        tick_history = app_module._market_tick_history["EURUSD"]
        bar_history = app_module._market_bar_history[("EURUSD", "M1")]

        assert app_module.MARKET_TICK_HISTORY_MAXLEN == 2_048
        assert app_module.MARKET_BAR_HISTORY_MAXLEN == 2_048
        assert tick_history.maxlen == app_module.MARKET_TICK_HISTORY_MAXLEN
        assert bar_history.maxlen == app_module.MARKET_BAR_HISTORY_MAXLEN

        for sequence in range(app_module.MARKET_TICK_HISTORY_MAXLEN + 1):
            tick_history.append({"sequence": sequence})
        assert len(tick_history) == app_module.MARKET_TICK_HISTORY_MAXLEN
        assert tick_history[0]["sequence"] == 1
    finally:
        client.close()


def test_exact_scalp_bar_batch_get_preserves_ordered_scope(tmp_path: Path) -> None:
    client = _fresh_client(tmp_path)
    try:
        response = client.get(
            "/v2/market/bars/batch",
            params={"timeframe": "M1", "limit": 242},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["schema"] == "fxstack.exact_scalp_bar_batch.v1"
        assert tuple(payload["symbols"]) == IG_MT4_SCALP_SYMBOLS
        assert tuple(payload["bars_by_symbol"]) == IG_MT4_SCALP_SYMBOLS
        assert all(not payload["bars_by_symbol"][symbol] for symbol in IG_MT4_SCALP_SYMBOLS)
    finally:
        client.close()


def test_exact_scalp_bar_batch_get_reads_direct_cache_without_tick_aggregation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    try:
        _post_authenticated_heartbeat(client)
        app_module = sys.modules["fxstack.api.app"]
        current_minute = int(time.time() // 60) * 60
        response = client.post(
            "/v2/market/bars",
            json={
                "symbol": "EURUSD",
                "timeframe": "M1",
                "bars": [_direct_bar(current_minute - 60)],
                **_market_source_payload(),
            },
        )
        assert response.status_code == 200, response.text
        app_module._market_tick_history["EURUSD"].append(
            {
                "ts_epoch": float(current_minute),
                "bid": 1.1004,
                "ask": 1.1006,
                "volume_source": "bridge_market_event_count_v1",
                "price_basis": "bridge_tick_mid_ohlc_v1",
            }
        )
        monkeypatch.setattr(
            app_module,
            "_aggregate_bars",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("tick_aggregation_reached")
            ),
        )

        batch = client.get(
            "/v2/market/bars/batch",
            params={"timeframe": "M1", "limit": 242},
        )

        assert batch.status_code == 200, batch.text
        rows = batch.json()["bars_by_symbol"]["EURUSD"]
        assert len(rows) == 1
        assert rows[0]["volume_source"] == "mt4_ivolume_tick_count_v1"
        assert rows[0]["price_basis"] == "mt4_bid_ohlc_v1"
    finally:
        client.close()


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


def _direct_bar(timestamp: float, *, volume: int = 321) -> dict[str, object]:
    return {
        "time": timestamp,
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


def _frame(
    batches: list[dict[str, object]],
    *,
    producer_instance_id: str = _PRODUCER_INSTANCE_ID,
) -> dict[str, object]:
    return {
        "batches": batches,
        **_market_source_payload(producer_instance_id=producer_instance_id),
    }


def test_bar_batch_shares_one_receipt_and_exact_repost_preserves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]
    current_minute = int(time.time() // 60) * 60
    receipt = float(current_minute + 10)
    monkeypatch.setattr(app_module, "_utc_now_ts", lambda: receipt)
    payload = _frame(
        [
            {
                "symbol": "eurusd",
                "timeframe": "m1",
                "bars": [_direct_bar(current_minute - 60)],
            },
            {
                "symbol": "GBPUSD",
                "timeframe": "M1",
                "bars": [_direct_bar(current_minute - 60, volume=400)],
            },
        ]
    )

    first = client.post("/v2/market/bars/batch", json=payload)

    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 2
    assert first.json()["batch_count"] == 2
    eur = dict(app_module._market_bar_history[("EURUSD", "M1")][0])
    gbp = dict(app_module._market_bar_history[("GBPUSD", "M1")][0])
    assert eur["received_at_epoch"] == pytest.approx(receipt)
    assert gbp["received_at_epoch"] == pytest.approx(receipt)
    assert eur["market_source_id"] == gbp["market_source_id"]

    later_receipt = receipt + 1.25
    monkeypatch.setattr(app_module, "_utc_now_ts", lambda: later_receipt)
    repost = client.post("/v2/market/bars/batch", json=payload)

    assert repost.status_code == 200, repost.text
    assert repost.json()["accepted"] == 2
    assert app_module._market_bar_history[("EURUSD", "M1")][0][
        "received_at_epoch"
    ] == pytest.approx(receipt)
    assert app_module._market_bar_history[("GBPUSD", "M1")][0][
        "received_at_epoch"
    ] == pytest.approx(receipt)


def test_bar_batch_conflict_rejects_all_without_partial_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    current_minute = int(time.time() // 60) * 60
    original = _direct_bar(current_minute - 120)
    seeded = client.post(
        "/v2/market/bars",
        json={
            "symbol": "EURUSD",
            "timeframe": "M1",
            "bars": [original],
            **_market_source_payload(),
        },
    )
    assert seeded.status_code == 200, seeded.text

    response = client.post(
        "/v2/market/bars/batch",
        json=_frame(
            [
                {
                    "symbol": "GBPUSD",
                    "timeframe": "M1",
                    "bars": [_direct_bar(current_minute - 60, volume=500)],
                },
                {
                    "symbol": "EURUSD",
                    "timeframe": "M1",
                    "bars": [{**original, "volume": 322}],
                },
            ]
        ),
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["detail"]["conflicting_fields"] == [
        "volume"
    ]
    app_module = sys.modules["fxstack.api.app"]
    assert ("GBPUSD", "M1") not in app_module._market_bar_history
    eur_rows = list(app_module._market_bar_history[("EURUSD", "M1")])
    assert len(eur_rows) == 1
    assert eur_rows[0]["volume"] == 321


def test_bar_batch_unclosed_row_rejects_every_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    app_module = sys.modules["fxstack.api.app"]
    current_minute = int(time.time() // 60) * 60
    receipt = float(current_minute + 2)
    monkeypatch.setattr(app_module, "_utc_now_ts", lambda: receipt)

    response = client.post(
        "/v2/market/bars/batch",
        json=_frame(
            [
                {
                    "symbol": "EURUSD",
                    "timeframe": "M1",
                    "bars": [_direct_bar(current_minute - 60)],
                },
                {
                    "symbol": "GBPUSD",
                    "timeframe": "M1",
                    "bars": [_direct_bar(current_minute)],
                },
            ]
        ),
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["message"] == (
        "bar batch contains a bar that is not completed"
    )
    assert ("EURUSD", "M1") not in app_module._market_bar_history
    assert ("GBPUSD", "M1") not in app_module._market_bar_history


def test_bar_batch_requires_one_matching_top_level_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _authenticated_client(tmp_path, monkeypatch)
    _post_authenticated_heartbeat(client)
    current_minute = int(time.time() // 60) * 60
    group: dict[str, object] = {
        "symbol": "EURUSD",
        "timeframe": "M1",
        "bars": [_direct_bar(current_minute - 60)],
    }

    mismatch = client.post(
        "/v2/market/bars/batch",
        json=_frame([group], producer_instance_id="other-terminal-instance"),
    )
    nested_source = client.post(
        "/v2/market/bars/batch",
        json=_frame([{**group, "producer_instance_id": _PRODUCER_INSTANCE_ID}]),
    )

    assert mismatch.status_code == 409, mismatch.text
    assert nested_source.status_code == 422, nested_source.text
    app_module = sys.modules["fxstack.api.app"]
    assert ("EURUSD", "M1") not in app_module._market_bar_history


def test_bar_batch_enforces_batch_and_total_row_bounds(tmp_path: Path) -> None:
    client = _fresh_client(tmp_path)
    timestamp = float(int(time.time() // 60) * 60 - 60)
    bar = _direct_bar(timestamp)

    too_many_batches = client.post(
        "/v2/market/bars/batch",
        json={
            "batches": [
                {
                    "symbol": f"PAIR{index:02d}",
                    "timeframe": "M1",
                    "bars": [bar],
                }
                for index in range(65)
            ]
        },
    )
    too_many_rows = client.post(
        "/v2/market/bars/batch",
        json={
            "batches": [
                {"symbol": "EURUSD", "timeframe": "M1", "bars": [bar] * 250},
                {"symbol": "GBPUSD", "timeframe": "M1", "bars": [bar] * 251},
            ]
        },
    )
    duplicate_group = client.post(
        "/v2/market/bars/batch",
        json={
            "batches": [
                {"symbol": "eurusd", "timeframe": "m1", "bars": [bar]},
                {"symbol": " EURUSD ", "timeframe": "M1", "bars": [bar]},
            ]
        },
    )

    assert too_many_batches.status_code == 422, too_many_batches.text
    assert too_many_rows.status_code == 422, too_many_rows.text
    assert duplicate_group.status_code == 422, duplicate_group.text
    assert "at most 500 total rows" in too_many_rows.text
    assert "must be unique after normalization" in duplicate_group.text
