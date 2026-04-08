from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fxstack.settings import get_settings


def _get_mlflow() -> Any:
    try:
        import mlflow  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised in envs without mlflow
        raise RuntimeError(
            "MLflow artifact resolution requires the `mlflow` package. Install the fx-quant-stack research extra or add mlflow."
        ) from exc
    return mlflow


def is_model_uri(value: str) -> bool:
    txt = str(value or "").strip()
    return txt.startswith("models:/") or txt.startswith("runs:/")


def normalize_artifact_ref(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        payload = dict(value or {})
        evidence_refs = dict(payload.get("evidence_refs") or {})
        return {
            "path": str(payload.get("path") or payload.get("artifact_path") or evidence_refs.get("artifact_path") or ""),
            "model_uri": str(payload.get("model_uri") or payload.get("uri") or ""),
            "model_name": str(payload.get("model_name") or ""),
            "model_version": "" if payload.get("model_version") in (None, "") else str(payload.get("model_version")),
            "alias": str(payload.get("alias") or ""),
            "bundle_run_id": str(payload.get("bundle_run_id") or ""),
            "dataset_fingerprint": str(payload.get("dataset_fingerprint") or ""),
            "artifact_hash": str(payload.get("artifact_hash") or ""),
            "runtime_compatible": bool(payload.get("runtime_compatible", True)),
            "feature_service_name": str(payload.get("feature_service_name") or ""),
            "feature_service_version": str(payload.get("feature_service_version") or ""),
            "feature_contract_hash": str(payload.get("feature_contract_hash") or ""),
            "feature_view_names": list(payload.get("feature_view_names") or []),
            "evidence_refs": evidence_refs,
        }
    txt = str(value or "").strip()
    if is_model_uri(txt):
        return {
            "path": "",
            "model_uri": txt,
            "runtime_compatible": True,
            "feature_service_name": "",
            "feature_service_version": "",
            "feature_contract_hash": "",
            "feature_view_names": [],
            "evidence_refs": {},
        }
    return {
        "path": txt,
        "model_uri": "",
        "runtime_compatible": True,
        "feature_service_name": "",
        "feature_service_version": "",
        "feature_contract_hash": "",
        "feature_view_names": [],
        "evidence_refs": {},
    }


def artifact_ref_value(value: Any) -> str:
    ref = normalize_artifact_ref(value)
    local_path = str(ref.get("path") or "").strip()
    model_uri = str(ref.get("model_uri") or "").strip()
    if local_path:
        return local_path
    return str(model_uri or "")


def _resolve_local_path(path_value: str, *, project_root: Path) -> Path:
    txt = str(path_value or "").strip()
    if not txt:
        raise FileNotFoundError("empty model artifact path")
    raw = Path(txt.replace("\\", "/")).expanduser()
    for candidate in (raw, project_root / raw, project_root.parent / raw):
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"model artifact not found: {path_value}")


def _legacy_dir_from_download(path: Path) -> Path:
    candidates = [
        path / "legacy_artifact",
        path / "data" / "legacy_artifact",
        path / "artifacts" / "legacy_artifact",
        path,
    ]
    for candidate in candidates:
        if (candidate / "meta.json").exists():
            return candidate.resolve()
    mlmodel = path / "MLmodel"
    if mlmodel.exists():
        try:
            payload = mlmodel.read_text(encoding="utf-8")
        except Exception:
            payload = ""
        if "legacy_artifact" in payload:
            candidate = path / "legacy_artifact"
            if candidate.exists():
                return candidate.resolve()
    raise FileNotFoundError(f"downloaded MLflow artifact does not contain a legacy artifact directory: {path}")


def resolve_model_artifact_path(
    value: Any,
    *,
    project_root: Path | None = None,
    cache_root: Path | None = None,
) -> Path:
    s = get_settings()
    root = Path(project_root or s.project_root)
    ref = normalize_artifact_ref(value)
    local_path = str(ref.get("path") or "").strip()
    model_uri = str(ref.get("model_uri") or "").strip()
    if local_path:
        try:
            return _resolve_local_path(local_path, project_root=root)
        except FileNotFoundError:
            if not model_uri:
                raise
    if model_uri and bool(s.mlflow_enabled):
        mlflow = _get_mlflow()
        mlflow.set_tracking_uri(str(s.mlflow_tracking_uri))
        registry_uri = str(s.mlflow_registry_uri or s.mlflow_tracking_uri).strip()
        if registry_uri:
            mlflow.set_registry_uri(registry_uri)
        dst = Path(cache_root or s.mlflow_cache_root).expanduser()
        dst.mkdir(parents=True, exist_ok=True)
        downloaded = Path(mlflow.artifacts.download_artifacts(artifact_uri=model_uri, dst_path=str(dst)))
        return _legacy_dir_from_download(downloaded)
    return _resolve_local_path(local_path, project_root=root)


def load_bundle_manifest_from_run(run_id: str, *, cache_root: Path | None = None) -> dict[str, Any]:
    s = get_settings()
    mlflow = _get_mlflow()
    mlflow.set_tracking_uri(str(s.mlflow_tracking_uri))
    registry_uri = str(s.mlflow_registry_uri or s.mlflow_tracking_uri).strip()
    if registry_uri:
        mlflow.set_registry_uri(registry_uri)
    dst = Path(cache_root or s.mlflow_cache_root).expanduser()
    dst.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(
        mlflow.artifacts.download_artifacts(artifact_uri=f"runs:/{str(run_id)}/bundle_manifest.json", dst_path=str(dst))
    )
    return dict(json.loads(manifest_path.read_text(encoding="utf-8")) or {})
