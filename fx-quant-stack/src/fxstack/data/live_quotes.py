from __future__ import annotations

from typing import Any

from fxstack.live.policy import normalize_spread_bps
from fxstack.providers.history.binance_spot import fetch_ohlcv_frame as _fetch_binance_ohlcv_frame_via_provider
from fxstack.providers.market.binance_spot import fetch_latest_quotes as _fetch_binance_quotes_via_provider
from fxstack.providers.market.mt4_bridge import (
    fetch_bars as _fetch_bridge_bars_via_provider,
    fetch_quotes as _fetch_bridge_quotes_via_provider,
    fetch_ready as _fetch_bridge_ready_via_provider,
)
from fxstack.providers.registry import market_provider_capabilities, resolve_market_data_provider


def _settings_or_default(settings: Any | None = None) -> Any:
    if settings is not None:
        return settings
    from fxstack.settings import get_settings

    return get_settings()


def _provider_name(provider: str = "", *, settings: Any | None = None) -> str:
    return resolve_market_data_provider(_settings_or_default(settings), provider=provider)


def _bridge_api_key(settings: Any | None = None) -> str:
    cfg = _settings_or_default(settings)
    return str(getattr(cfg, "bridge_api_key", "") or "")


def _crypto_exchange_id(settings: Any | None = None) -> str:
    cfg = _settings_or_default(settings)
    return str(getattr(cfg, "crypto_exchange_id", "binance") or "binance")


def _market_symbols(settings: Any | None = None, symbols: list[str] | None = None) -> list[str]:
    if symbols:
        return [str(item).strip().upper() for item in list(symbols) if str(item).strip()]
    cfg = _settings_or_default(settings)
    allowlist = list(getattr(cfg, "provider_symbol_allowlist", []) or [])
    if allowlist:
        return [str(item).strip().upper() for item in allowlist if str(item).strip()]
    pairs = list(getattr(cfg, "pairs", []) or [])
    return [str(item).strip().upper() for item in pairs if str(item).strip()]


def fetch_bridge_ticks(bridge_url: str) -> dict[str, dict[str, Any]]:
    return _fetch_bridge_quotes_via_provider(bridge_url, api_key=_bridge_api_key())


def fetch_bridge_bars(bridge_url: str, *, symbol: str, timeframe: str, limit: int = 400) -> list[dict[str, Any]]:
    return _fetch_bridge_bars_via_provider(
        bridge_url,
        symbol=str(symbol).upper(),
        timeframe=str(timeframe).upper(),
        limit=max(1, min(int(limit), 2000)),
        api_key=_bridge_api_key(),
    )


def fetch_bridge_ready(bridge_url: str) -> dict[str, Any]:
    return _fetch_bridge_ready_via_provider(bridge_url, api_key=_bridge_api_key())


def fetch_market_ticks(
    bridge_url: str,
    *,
    provider: str = "",
    symbols: list[str] | None = None,
    settings: Any | None = None,
) -> dict[str, dict[str, Any]]:
    provider_name = _provider_name(provider, settings=settings)
    if provider_name == "binance_spot":
        return _fetch_binance_quotes_via_provider(symbols=_market_symbols(settings=settings, symbols=symbols), exchange_id=_crypto_exchange_id(settings))
    return _fetch_bridge_quotes_via_provider(bridge_url, api_key=_bridge_api_key(settings))


def fetch_market_bars(
    bridge_url: str,
    *,
    symbol: str,
    timeframe: str,
    limit: int = 400,
    provider: str = "",
    settings: Any | None = None,
) -> list[dict[str, Any]]:
    provider_name = _provider_name(provider, settings=settings)
    if provider_name == "binance_spot":
        frame = _fetch_binance_ohlcv_frame_via_provider(
            symbol=str(symbol).upper(),
            timeframe=str(timeframe).upper(),
            limit=max(1, min(int(limit), 2000)),
            exchange_id=_crypto_exchange_id(settings),
        )
        if frame.empty:
            return []
        return [dict(row) for row in frame.to_dict(orient="records")]
    return _fetch_bridge_bars_via_provider(
        bridge_url,
        symbol=str(symbol).upper(),
        timeframe=str(timeframe).upper(),
        limit=max(1, min(int(limit), 2000)),
        api_key=_bridge_api_key(settings),
    )


def fetch_market_ready(
    bridge_url: str,
    *,
    provider: str = "",
    settings: Any | None = None,
) -> dict[str, Any]:
    provider_name = _provider_name(provider, settings=settings)
    capabilities = market_provider_capabilities(provider_name)
    if provider_name == "binance_spot":
        return {
            "provider": provider_name,
            "status": "ok",
            "supported": True,
            "source": "ccxt",
            "shadow_only": bool(capabilities.shadow_only),
            "fresh": True,
            "details": {"asset_classes": list(capabilities.asset_classes)},
        }
    if provider_name in {"mt4_bridge", "mt4"}:
        payload = _fetch_bridge_ready_via_provider(bridge_url, api_key=_bridge_api_key(settings))
        if payload:
            payload = dict(payload)
            payload.setdefault("provider", provider_name)
            payload.setdefault("shadow_only", bool(capabilities.shadow_only))
            payload.setdefault("supported", True)
        return payload
    return {
        "provider": provider_name,
        "status": "unknown",
        "supported": False,
        "shadow_only": bool(capabilities.shadow_only),
        "details": {},
    }


def overlay_execution_quote(features_row: dict[str, Any], tick: dict[str, Any]) -> dict[str, Any]:
    out = dict(features_row)
    bid = float(tick.get("bid", 0.0) or 0.0)
    ask = float(tick.get("ask", 0.0) or 0.0)
    spread_bps, spread_source = normalize_spread_bps(
        tick=dict(tick or {}),
        row=out,
        pair=str(out.get("pair") or out.get("symbol") or ""),
    )
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(out.get("mid_close", 0.0) or 0.0)
    out["live_bid"] = bid
    out["live_ask"] = ask
    out["live_mid"] = mid
    out["spread_bps"] = float(spread_bps)
    out["spread_unit_source"] = str(spread_source)
    return out
