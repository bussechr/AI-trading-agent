from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from fxstack.providers.catalog import InstrumentCatalog, infer_instrument_ref
from fxstack.providers import registry
from fxstack.data import live_quotes
from fxstack.providers.history.binance_spot import normalize_exchange_timeframe, normalize_ohlcv_rows
from fxstack.providers.history.dukascopy import load_history_frame as load_dukascopy_history_frame
from fxstack.providers.market import binance_spot as binance_market
from fxstack.providers.market import mt4_bridge
from fxstack.providers.registry import (
    provider_capabilities,
    resolve_execution_provider,
    resolve_history_provider,
    resolve_market_data_provider,
)


class _FakeResponse:
    def __init__(self, payload: Any, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = int(status_code)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> Any:
        return self._payload


def _write_dukascopy_csv(path: Path) -> None:
    raw = pd.DataFrame(
        {
            "Gmt time": ["2024-01-01 00:05:00", "2024-01-01 00:00:00", "2024-01-01 00:05:00"],
            "Open": [1.1002, 1.1000, 1.1003],
            "High": [1.1005, 1.1002, 1.1006],
            "Low": [1.1000, 1.0998, 1.1001],
            "Close": [1.1001, 1.1001, 1.1004],
            "Volume": [10, 11, 12],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    raw.to_csv(path, index=False)


def test_catalog_normalizes_fx_and_crypto_symbols() -> None:
    fx = infer_instrument_ref("eur/usd", provider="dukascopy", venue="otc", asset_class="fx")
    assert fx.canonical_symbol == "EURUSD"
    assert fx.provider_symbol == "EUR/USD"
    assert fx.pair == "EURUSD"
    assert (fx.base_ccy, fx.quote_ccy) == ("EUR", "USD")

    crypto = infer_instrument_ref("btc/usdt", provider="binance_spot", venue="spot", asset_class="crypto")
    assert crypto.canonical_symbol == "BTCUSDT"
    assert crypto.provider_symbol == "BTC/USDT"
    assert crypto.pair == ""
    assert (crypto.base_ccy, crypto.quote_ccy) == ("BTC", "USDT")

    catalog = InstrumentCatalog()
    first = catalog.get("BTC/USDT", provider="binance_spot", venue="spot", asset_class="crypto")
    second = catalog.get("BTC-USDT", provider="binance_spot", venue="spot", asset_class="crypto")
    assert first.canonical_symbol == "BTCUSDT"
    assert second.canonical_symbol == "BTCUSDT"
    assert len(catalog.instruments) == 1


def test_dukascopy_history_frame_normalizes_symbol_provenance_and_order(tmp_path: Path) -> None:
    csv_path = tmp_path / "EURUSD_M5.csv"
    _write_dukascopy_csv(csv_path)

    out = load_dukascopy_history_frame(csv_path=csv_path, pair="eur/usd", timeframe="m5")

    assert len(out) == 2
    assert list(pd.to_datetime(out["ts"], utc=True)) == sorted(list(pd.to_datetime(out["ts"], utc=True)))
    assert set(out["pair"]) == {"EURUSD"}
    assert set(out["provider"]) == {"dukascopy"}
    assert set(out["canonical_symbol"]) == {"EURUSD"}
    assert set(out["provider_symbol"]) == {"EUR/USD"}
    assert set(out["provenance"]) == {"dukascopy_csv"}
    assert all(isinstance(flags, list) and not flags for flags in out["quality_flags"])


def test_binance_history_normalizes_duplicates_proxy_spread_and_symbols() -> None:
    rows = [
        [1704067500000, 50010.0, 50020.0, 49990.0, 50015.0, 2.0],
        [1704067200000, 50000.0, 50010.0, 49980.0, 50005.0, 1.0],
        [1704067500000, 50030.0, 50040.0, 50000.0, 50035.0, 3.0],
        [None, 0.0, 0.0, 0.0, 0.0, 0.0],
    ]

    out = normalize_ohlcv_rows(rows, symbol="BTC/USDT", timeframe="5m")

    assert len(out) == 2
    assert list(pd.to_datetime(out["ts"], utc=True)) == sorted(list(pd.to_datetime(out["ts"], utc=True)))
    assert float(out.iloc[-1]["mid_open"]) == pytest.approx(50030.0)
    assert set(out["pair"]) == {"BTCUSDT"}
    assert set(out["canonical_symbol"]) == {"BTCUSDT"}
    assert set(out["provider_symbol"]) == {"BTC/USDT"}
    assert set(out["provenance"]) == {"ccxt_binance_spot"}
    assert set(out["spread"]) == {0.0}
    assert all(flags == ["proxy_spread"] for flags in out["quality_flags"])


def test_binance_timeframe_normalization_maps_repo_timeframes() -> None:
    assert normalize_exchange_timeframe("M5") == "5m"
    assert normalize_exchange_timeframe("H1") == "1h"
    assert normalize_exchange_timeframe("D") == "1d"
    assert normalize_exchange_timeframe("15m") == "15m"


def test_mt4_bridge_quotes_normalize_spread_provenance_and_quality_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "eur/usd": {
            "symbol": "eur/usd",
            "bid": 1.1000,
            "ask": 1.1002,
            "spread_bps": 1.5,
            "time": "2026-01-01T00:00:00Z",
        },
        "gbp/usd": {
            "symbol": "gbp/usd",
            "bid": 0.0,
            "ask": 1.2502,
            "mid": 1.2500,
            "time": "2026-01-01T00:00:10Z",
        },
    }

    def _fake_get(url: str, headers: dict[str, str] | None = None, timeout: int = 0) -> _FakeResponse:
        assert url.endswith("/v2/market/ticks")
        return _FakeResponse(payload)

    monkeypatch.setattr(mt4_bridge.requests, "get", _fake_get)

    out = mt4_bridge.fetch_quotes("http://127.0.0.1:58710")

    assert set(out.keys()) == {"EURUSD", "GBPUSD"}
    eurusd = out["EURUSD"]
    assert eurusd["instrument"]["canonical_symbol"] == "EURUSD"
    assert eurusd["instrument"]["provider_symbol"] == "EUR/USD"
    assert eurusd["spread_bps"] == pytest.approx(1.5)
    assert eurusd["metadata"]["spread_unit_source"] == "tick.spread_bps"
    assert eurusd["quality_flags"] == []
    assert eurusd["provenance"] == "mt4_bridge"

    gbpusd = out["GBPUSD"]
    assert gbpusd["instrument"]["canonical_symbol"] == "GBPUSD"
    assert gbpusd["quality_flags"] == ["missing_bid", "missing_spread"]
    assert gbpusd["metadata"]["spread_unit_source"] == "missing"


def test_mt4_bridge_bars_sort_dedupe_and_stamp_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "bars": [
            {"ts": "2026-01-01T00:05:00Z", "mid_close": 1.1002, "quality_flags": ["gap_fill"]},
            {"ts": "2026-01-01T00:00:00Z", "mid_close": 1.1000},
            {"ts": "2026-01-01T00:05:00Z", "mid_close": 1.1004, "quality_flags": ["gap_fill", "replacement"]},
        ]
    }

    def _fake_get(
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 0,
    ) -> _FakeResponse:
        assert url.endswith("/v2/market/bars")
        assert params is not None
        return _FakeResponse(payload)

    monkeypatch.setattr(mt4_bridge.requests, "get", _fake_get)

    out = mt4_bridge.fetch_bars("http://127.0.0.1:58710", symbol="eur/usd", timeframe="m5", limit=10)

    assert len(out) == 2
    assert list(pd.to_datetime([item["ts"] for item in out], utc=True)) == sorted(pd.to_datetime([item["ts"] for item in out], utc=True))
    assert out[-1]["mid_close"] == pytest.approx(1.1004)
    assert out[-1]["quality_flags"] == ["gap_fill", "replacement"]
    assert all(item["pair"] == "EURUSD" for item in out)
    assert all(item["provider"] == "mt4_bridge" for item in out)
    assert all(item["canonical_symbol"] == "EURUSD" for item in out)
    assert all(item["provenance"] == "mt4_bridge" for item in out)


def test_binance_spot_quotes_normalize_symbols_and_proxy_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeExchange:
        def __init__(self, config: dict[str, Any]) -> None:
            self.config = dict(config)

        def fetch_tickers(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
            assert symbols == ["BTC/USDT", "ETH-USDT"]
            return {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "bid": 68000.0,
                    "ask": 68010.0,
                    "last": 68005.0,
                    "datetime": "2026-01-01T00:00:00Z",
                    "quoteVolume": 1000000.0,
                    "baseVolume": 14.0,
                },
                "ETH-USDT": {
                    "symbol": "ETH-USDT",
                    "bid": 0.0,
                    "ask": 0.0,
                    "last": 3500.0,
                    "datetime": "2026-01-01T00:01:00Z",
                    "quoteVolume": 200000.0,
                    "baseVolume": 57.0,
                },
            }

    class _FakeCcxt:
        binance = _FakeExchange

    monkeypatch.setattr(binance_market, "_require_ccxt", lambda: _FakeCcxt)

    out = binance_market.fetch_latest_quotes(symbols=["BTC/USDT", "ETH-USDT"])

    assert set(out.keys()) == {"BTCUSDT", "ETHUSDT"}
    btc = out["BTCUSDT"]
    assert btc["instrument"]["canonical_symbol"] == "BTCUSDT"
    assert btc["instrument"]["provider_symbol"] == "BTC/USDT"
    assert btc["quality_flags"] == []
    assert btc["spread_bps"] == pytest.approx(((68010.0 - 68000.0) / 68005.0) * 10000.0)
    assert btc["provenance"] == "ccxt_binance_spot"

    eth = out["ETHUSDT"]
    assert eth["instrument"]["canonical_symbol"] == "ETHUSDT"
    assert eth["instrument"]["provider_symbol"] == "ETH-USDT"
    assert eth["quality_flags"] == ["missing_bid", "missing_ask", "proxy_spread"]
    assert eth["bid"] == pytest.approx(3500.0)
    assert eth["ask"] == pytest.approx(3500.0)
    assert eth["metadata"]["quote_volume"] == pytest.approx(200000.0)
    assert eth["metadata"]["base_volume"] == pytest.approx(57.0)


def test_provider_capabilities_expose_shadow_and_proxy_spread_support() -> None:
    dukascopy = provider_capabilities("dukascopy")
    bridge = provider_capabilities("mt4_bridge")
    binance = provider_capabilities("binance_spot")
    oanda = provider_capabilities("oanda")

    assert dukascopy.supports_history is True
    assert dukascopy.supports_bid_ask is True
    assert dukascopy.supports_proxy_spread is False

    assert bridge.supports_market_data is True
    assert bridge.asset_classes == ["fx"]

    assert binance.supports_history is True
    assert binance.supports_market_data is True
    assert binance.supports_proxy_spread is True
    assert binance.shadow_only is True

    assert oanda.supports_execution is True
    assert oanda.shadow_only is True
    assert oanda.metadata == {"dry_run": True, "runtime_dispatch": False}


def test_provider_resolvers_return_explicit_or_default_roles() -> None:
    class _Settings:
        normalized_data_provider = "dukascopy"
        history_provider = ""
        market_data_provider = ""
        execution_provider = ""

    settings = _Settings()

    assert resolve_history_provider(settings) == "dukascopy"
    assert resolve_market_data_provider(settings) == "mt4_bridge"
    assert resolve_execution_provider(settings) == "mt4"
    assert resolve_market_data_provider(settings, provider="binance_spot") == "binance_spot"


def test_registry_resolves_market_provider_from_settings_and_override() -> None:
    class _Settings:
        market_data_provider = "binance_spot"
        normalized_data_provider = "mt4_bridge"

    assert registry.resolve_market_data_provider(_Settings()) == "binance_spot"
    assert registry.resolve_market_data_provider(_Settings(), provider="mt4_bridge") == "mt4_bridge"
    assert registry.market_provider_shadow_only("binance_spot") is True
    assert registry.market_provider_shadow_only("mt4_bridge") is False


def test_live_quotes_dispatches_market_provider_without_changing_bridge_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    bridge_called = {"quotes": 0, "bars": 0, "ready": 0}
    binance_called = {"quotes": 0, "bars": 0}

    def _bridge_quotes(_bridge_url: str, *, api_key: str = "") -> dict[str, dict[str, Any]]:
        bridge_called["quotes"] += 1
        assert api_key == "bridge-key"
        return {"EURUSD": {"provider": "mt4_bridge"}}

    def _bridge_bars(_bridge_url: str, *, symbol: str, timeframe: str, limit: int = 400, api_key: str = "") -> list[dict[str, Any]]:
        bridge_called["bars"] += 1
        assert symbol == "EURUSD"
        assert timeframe == "M5"
        assert limit == 10
        assert api_key == "bridge-key"
        return [{"provider": "mt4_bridge", "ts": "2026-01-01T00:00:00Z"}]

    def _bridge_ready(_bridge_url: str, *, api_key: str = "") -> dict[str, Any]:
        bridge_called["ready"] += 1
        assert api_key == "bridge-key"
        return {"provider": "mt4_bridge", "status": "ok"}

    def _binance_quotes(*, symbols: list[str], exchange_id: str = "binance") -> dict[str, dict[str, Any]]:
        binance_called["quotes"] += 1
        assert symbols == ["BTCUSDT"]
        assert exchange_id == "binance"
        return {"BTCUSDT": {"provider": "binance_spot"}}

    def _binance_bars(*, symbol: str, timeframe: str, limit: int = 500, exchange_id: str = "binance"):
        binance_called["bars"] += 1
        assert symbol == "BTCUSDT"
        assert timeframe == "M5"
        assert limit == 20
        assert exchange_id == "binance"
        return pd.DataFrame(
            [{"ts": "2026-01-01T00:00:00Z", "provider": "binance_spot", "canonical_symbol": "BTCUSDT"}]
        )

    monkeypatch.setattr(live_quotes, "_fetch_bridge_quotes_via_provider", _bridge_quotes)
    monkeypatch.setattr(live_quotes, "_fetch_bridge_bars_via_provider", _bridge_bars)
    monkeypatch.setattr(live_quotes, "_fetch_bridge_ready_via_provider", _bridge_ready)
    monkeypatch.setattr(live_quotes, "_fetch_binance_quotes_via_provider", _binance_quotes)
    monkeypatch.setattr(live_quotes, "_fetch_binance_ohlcv_frame_via_provider", _binance_bars)
    monkeypatch.setattr(live_quotes, "_bridge_api_key", lambda settings=None: "bridge-key")

    assert live_quotes.fetch_bridge_ticks("http://bridge") == {"EURUSD": {"provider": "mt4_bridge"}}
    assert live_quotes.fetch_bridge_bars("http://bridge", symbol="EURUSD", timeframe="M5", limit=10) == [
        {"provider": "mt4_bridge", "ts": "2026-01-01T00:00:00Z"}
    ]
    assert live_quotes.fetch_bridge_ready("http://bridge") == {"provider": "mt4_bridge", "status": "ok"}
    assert live_quotes.fetch_market_ticks("http://bridge", provider="binance_spot", symbols=["BTCUSDT"]) == {
        "BTCUSDT": {"provider": "binance_spot"}
    }
    assert live_quotes.fetch_market_bars(
        "http://bridge",
        provider="binance_spot",
        symbol="BTCUSDT",
        timeframe="M5",
        limit=20,
    ) == [{"ts": "2026-01-01T00:00:00Z", "provider": "binance_spot", "canonical_symbol": "BTCUSDT"}]
    assert live_quotes.fetch_market_ready("http://bridge", provider="binance_spot")["provider"] == "binance_spot"

    assert bridge_called == {"quotes": 1, "bars": 1, "ready": 1}
    assert binance_called == {"quotes": 1, "bars": 1}
