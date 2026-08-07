"""Market-provider exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "fetch_bars": "fxstack.providers.market.mt4_bridge",
    "fetch_binance_spot_quotes": "fxstack.providers.market.binance_spot",
    "fetch_quotes": "fxstack.providers.market.mt4_bridge",
    "fetch_ready": "fxstack.providers.market.mt4_bridge",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
