from __future__ import annotations

from typing import Any

from fxstack.providers.contracts import ProviderCapabilities


def _normalized_provider_name(raw: Any) -> str:
    return str(raw or "").strip().lower()


def history_provider_name(settings: Any) -> str:
    txt = str(getattr(settings, "history_provider", "") or "").strip().lower()
    if txt:
        return txt
    txt = str(getattr(settings, "normalized_data_provider", "") or "").strip().lower()
    return txt or "dukascopy"


def market_data_provider_name(settings: Any) -> str:
    txt = _normalized_provider_name(getattr(settings, "market_data_provider", "") or "")
    return txt or "mt4_bridge"


def execution_provider_name(settings: Any) -> str:
    txt = _normalized_provider_name(getattr(settings, "execution_provider", "") or "")
    return txt or "mt4"


def resolve_history_provider(settings: Any | None = None, *, provider: str = "") -> str:
    txt = _normalized_provider_name(provider)
    if txt:
        return txt
    if settings is None:
        return "dukascopy"
    txt = history_provider_name(settings)
    return txt or "dukascopy"


def resolve_market_data_provider(settings: Any | None = None, *, provider: str = "") -> str:
    txt = _normalized_provider_name(provider)
    if txt:
        return txt
    if settings is None:
        return "mt4_bridge"
    txt = market_data_provider_name(settings)
    if txt:
        return txt
    txt = _normalized_provider_name(getattr(settings, "normalized_data_provider", "") or "")
    return txt or "mt4_bridge"


def resolve_execution_provider(settings: Any | None = None, *, provider: str = "") -> str:
    txt = _normalized_provider_name(provider)
    if txt:
        return txt
    if settings is None:
        return "mt4"
    txt = execution_provider_name(settings)
    return txt or "mt4"


def market_provider_capabilities(provider: str) -> ProviderCapabilities:
    return provider_capabilities(provider)


def market_provider_shadow_only(provider: str) -> bool:
    return bool(market_provider_capabilities(provider).shadow_only)


def provider_roles_from_settings(settings: Any) -> dict[str, str]:
    return {
        "history_provider": history_provider_name(settings),
        "market_data_provider": market_data_provider_name(settings),
        "execution_provider": execution_provider_name(settings),
    }


def provider_capabilities(provider: str) -> ProviderCapabilities:
    key = str(provider or "").strip().lower()
    if key == "dukascopy":
        return ProviderCapabilities(
            provider=key,
            asset_classes=["fx"],
            supports_history=True,
            supports_bid_ask=True,
            supports_proxy_spread=False,
        )
    if key == "mt4_bridge":
        return ProviderCapabilities(
            provider=key,
            asset_classes=["fx"],
            supports_market_data=True,
            supports_bid_ask=True,
            supports_proxy_spread=False,
        )
    if key == "mt4":
        return ProviderCapabilities(
            provider=key,
            asset_classes=["fx"],
            supports_execution=True,
            supports_bid_ask=False,
            supports_proxy_spread=False,
        )
    if key == "binance_spot":
        return ProviderCapabilities(
            provider=key,
            asset_classes=["crypto"],
            supports_history=True,
            supports_market_data=True,
            supports_bid_ask=True,
            supports_proxy_spread=True,
            shadow_only=True,
        )
    return ProviderCapabilities(provider=key, asset_classes=["unknown"])
