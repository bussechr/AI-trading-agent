from __future__ import annotations

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "FeatureParityResult": "fxstack.feast.types",
    "FeaturePushAttempt": "fxstack.feast.types",
    "FeaturePushIntent": "fxstack.feast.types",
    "FeatureServiceRef": "fxstack.feast.types",
    "FeatureServingSnapshot": "fxstack.feast.types",
    "HistoricalDatasetProvenance": "fxstack.feast.types",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
