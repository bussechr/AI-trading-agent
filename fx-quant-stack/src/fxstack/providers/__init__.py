"""Canonical provider exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "CanonicalBar": "fxstack.providers.contracts",
    "CanonicalQuote": "fxstack.providers.contracts",
    "ExecutionRequest": "fxstack.providers.contracts",
    "ExecutionUpdate": "fxstack.providers.contracts",
    "InstrumentCatalog": "fxstack.providers.catalog",
    "InstrumentRef": "fxstack.providers.contracts",
    "ProviderCapabilities": "fxstack.providers.contracts",
    "ProviderSnapshot": "fxstack.providers.contracts",
    "build_default_catalog": "fxstack.providers.catalog",
    "execution_provider_name": "fxstack.providers.registry",
    "history_provider_name": "fxstack.providers.registry",
    "infer_instrument_ref": "fxstack.providers.catalog",
    "market_data_provider_name": "fxstack.providers.registry",
    "provider_roles_from_settings": "fxstack.providers.registry",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
