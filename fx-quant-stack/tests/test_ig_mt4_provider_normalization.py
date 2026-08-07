from __future__ import annotations

from typing import Any

import pytest

from fxstack.providers.catalog import infer_asset_class
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_INSTRUMENTS
from fxstack.providers.market import mt4_bridge
from fxstack.providers.registry import provider_capabilities


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


@pytest.mark.parametrize("identity", IG_MT4_SCALP_INSTRUMENTS)
def test_ig_mt4_quote_normalization_uses_explicit_catalog_identity(
    identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        identity.provider_symbol: {
            "symbol": identity.provider_symbol,
            "bid": 1.0,
            "ask": 1.0002,
            "spread_bps": 2.0,
            "time": "2026-08-03T10:00:00Z",
        }
    }

    def _fake_get(
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 0,
    ) -> _FakeResponse:
        assert url.endswith("/v2/market/ticks")
        return _FakeResponse(payload)

    monkeypatch.setattr(mt4_bridge.requests, "get", _fake_get)

    out = mt4_bridge.fetch_quotes("http://127.0.0.1:58710")

    assert tuple(out) == (identity.canonical_symbol,)
    instrument = out[identity.canonical_symbol]["instrument"]
    assert instrument["instrument_id"] == identity.instrument_id
    assert instrument["canonical_symbol"] == identity.canonical_symbol
    assert instrument["provider_symbol"] == identity.provider_symbol
    assert instrument["pair"] == (
        identity.canonical_symbol if identity.asset_class == "fx" else ""
    )
    assert instrument["asset_class"] == identity.asset_class
    assert instrument["venue"] == identity.venue
    assert instrument["base_ccy"] == identity.base_ccy
    assert instrument["quote_ccy"] == identity.quote_ccy


@pytest.mark.parametrize("identity", IG_MT4_SCALP_INSTRUMENTS)
def test_ig_mt4_bar_normalization_uses_explicit_catalog_identity(
    identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {
        "bars": [
            {
                "ts": "2026-08-03T10:00:00Z",
                "mid_open": 1.0,
                "mid_high": 1.0003,
                "mid_low": 0.9999,
                "mid_close": 1.0001,
                "bid_open": 0.9999,
                "bid_high": 1.0002,
                "bid_low": 0.9998,
                "bid_close": 1.0000,
                "volume": 137,
                "volume_source": "mt4_ivolume_tick_count_v1",
                "price_basis": "mt4_bid_ohlc_v1",
            }
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
        assert params["symbol"] == identity.provider_symbol
        return _FakeResponse(payload)

    monkeypatch.setattr(mt4_bridge.requests, "get", _fake_get)

    out = mt4_bridge.fetch_bars(
        "http://127.0.0.1:58710",
        symbol=identity.provider_symbol,
        timeframe="M5",
        limit=10,
    )

    assert len(out) == 1
    bar = out[0]
    assert bar["instrument_id"] == identity.instrument_id
    assert bar["canonical_symbol"] == identity.canonical_symbol
    assert bar["provider_symbol"] == identity.provider_symbol
    assert bar["pair"] == identity.canonical_symbol
    assert bar["asset_class"] == identity.asset_class
    assert bar["venue"] == identity.venue
    assert bar["base_ccy"] == identity.base_ccy
    assert bar["quote_ccy"] == identity.quote_ccy
    assert bar["bid_open"] == pytest.approx(0.9999)
    assert bar["bid_high"] == pytest.approx(1.0002)
    assert bar["bid_low"] == pytest.approx(0.9998)
    assert bar["bid_close"] == pytest.approx(1.0000)
    assert bar["volume"] == 137
    assert isinstance(bar["volume"], int)
    assert bar["volume_source"] == "mt4_ivolume_tick_count_v1"
    assert bar["price_basis"] == "mt4_bid_ohlc_v1"


@pytest.mark.parametrize("identity", IG_MT4_SCALP_INSTRUMENTS)
def test_generic_asset_class_inference_respects_ig_mt4_catalog(identity) -> None:
    separated = f"{identity.base_ccy}/{identity.quote_ccy}"
    assert infer_asset_class(identity.canonical_symbol) == identity.asset_class
    assert infer_asset_class(separated) == identity.asset_class


@pytest.mark.parametrize("provider", ("mt4_bridge", "mt4"))
def test_mt4_provider_capabilities_cover_fx_and_crypto_cfds(provider: str) -> None:
    capabilities = provider_capabilities(provider)

    assert capabilities.asset_classes == ["fx", "crypto"]
