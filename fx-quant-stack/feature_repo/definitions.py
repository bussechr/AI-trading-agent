from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import json
from typing import Any

from entities import pair

try:
    from feast import FeatureService, FeatureView, Field, FileSource, PushSource  # type: ignore
    from feast.types import Float64  # type: ignore
except Exception:  # pragma: no cover
    FeatureService = None  # type: ignore
    FeatureView = None  # type: ignore
    Field = None  # type: ignore
    FileSource = None  # type: ignore
    PushSource = None  # type: ignore
    Float64 = None  # type: ignore


FEATURE_REPO_ROOT = Path(__file__).resolve().parent
STACK_ROOT = FEATURE_REPO_ROOT.parent
ACTIVE_MODELS_PATH = STACK_ROOT / "artifacts" / "active_models.json"
OFFLINE_PLACEHOLDER_PATH = FEATURE_REPO_ROOT / "data" / "offline_placeholder.parquet"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _artifact_path(value: Any) -> str:
    if isinstance(value, dict):
        payload = dict(value or {})
        evidence = dict(payload.get("evidence_refs") or {})
        return str(payload.get("path") or payload.get("artifact_path") or evidence.get("artifact_path") or "").strip()
    return str(value or "").strip()


def _canonical_components(item: dict[str, Any]) -> list[tuple[str, Path]]:
    artifacts = dict(item.get("artifacts") or {})
    candidates = [
        ("regime_hmm", _artifact_path(artifacts.get("regime"))),
        ("swing_xgb", _artifact_path(artifacts.get("swing_xgb") or artifacts.get("swing"))),
        ("intraday_xgb", _artifact_path(artifacts.get("intraday_xgb") or artifacts.get("intraday"))),
    ]
    out: list[tuple[str, Path]] = []
    for component, raw in candidates:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = (STACK_ROOT.parent / path).resolve()
        if (path / "meta.json").exists():
            out.append((component, path))
    return out


def _service_name(pair_value: str, component: str, timeframe: str) -> str:
    return f"fx_{str(pair_value).lower()}_{str(component).lower()}_{str(timeframe).lower()}"


def _view_name(service_name: str) -> str:
    return f"{service_name}__fv"


def _push_name(service_name: str) -> str:
    return f"{service_name}__push"


def _file_name(service_name: str) -> str:
    return f"{service_name}__file"


def _load_service_specs() -> list[dict[str, Any]]:
    manifest = _load_json(ACTIVE_MODELS_PATH)
    active = dict(manifest.get("active_model_sets") or {})
    specs: list[dict[str, Any]] = []
    for pair_value, row in sorted(active.items()):
        for component, artifact_dir in _canonical_components(dict(row or {})):
            meta = _load_json(artifact_dir / "meta.json")
            features = [str(col).strip() for col in list(meta.get("feature_columns") or []) if str(col).strip()]
            timeframe = str(meta.get("timeframe") or "").upper()
            if not features or not timeframe:
                continue
            service_name = _service_name(str(pair_value).upper(), component, timeframe)
            specs.append(
                {
                    "pair": str(pair_value).upper(),
                    "component": component,
                    "timeframe": timeframe,
                    "service_name": service_name,
                    "view_name": _view_name(service_name),
                    "push_name": _push_name(service_name),
                    "file_name": _file_name(service_name),
                    "features": features,
                }
            )
    return specs


FEATURE_VIEWS: list[Any] = []
FEATURE_SERVICES: list[Any] = []
PUSH_SOURCES: list[Any] = []


if all(item is not None for item in (FeatureService, FeatureView, Field, FileSource, PushSource, Float64, pair)):
    for spec in _load_service_specs():
        batch_source = FileSource(
            name=str(spec["file_name"]),
            path=str(OFFLINE_PLACEHOLDER_PATH).replace("\\", "/"),
            timestamp_field="event_timestamp",
        )
        push_source = PushSource(
            name=str(spec["push_name"]),
            batch_source=batch_source,
        )
        feature_view = FeatureView(
            name=str(spec["view_name"]),
            source=push_source,
            schema=[Field(name=str(col), dtype=Float64) for col in list(spec["features"])],
            entities=[pair],
            ttl=timedelta(days=3650),
            online=True,
            description=f"Live push-backed features for {spec['pair']} {spec['component']} {spec['timeframe']}.",
        )
        feature_service = FeatureService(
            name=str(spec["service_name"]),
            features=[feature_view],
            description=f"Live service for {spec['pair']} {spec['component']} {spec['timeframe']}.",
        )
        globals()[str(spec["push_name"])] = push_source
        globals()[str(spec["view_name"])] = feature_view
        globals()[str(spec["service_name"])] = feature_service
        PUSH_SOURCES.append(push_source)
        FEATURE_VIEWS.append(feature_view)
        FEATURE_SERVICES.append(feature_service)

