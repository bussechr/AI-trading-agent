"""Historical-provider exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports

_EXPORTS = {
    "fetch_binance_spot_ohlcv_frame": (
        "fxstack.providers.history.binance_spot",
        "fetch_ohlcv_frame",
    ),
    "load_dukascopy_history_frame": (
        "fxstack.providers.history.dukascopy",
        "load_history_frame",
    ),
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
