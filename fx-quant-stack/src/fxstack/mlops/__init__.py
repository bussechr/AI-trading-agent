"""MLOps artifact exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "artifact_ref_value": "fxstack.mlops.local_artifact",
    "normalize_artifact_ref": "fxstack.mlops.local_artifact",
    "resolve_model_artifact_path": "fxstack.mlops.local_artifact",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
