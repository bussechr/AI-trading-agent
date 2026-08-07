from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from fxstack.models.artifact_contract import validate_artifact_contract
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
        explicit_path = (
            str(payload.get("path") or payload.get("artifact_path") or "")
            if "path" in payload or "artifact_path" in payload
            else str(evidence_refs.get("artifact_path") or "")
        )
        return {
            "path": explicit_path,
            "model_uri": str(payload.get("model_uri") or payload.get("uri") or ""),
            "model_name": str(payload.get("model_name") or ""),
            "model_version": "" if payload.get("model_version") in (None, "") else str(payload.get("model_version")),
            "alias": str(payload.get("alias") or ""),
            "bundle_run_id": str(payload.get("bundle_run_id") or ""),
            "dataset_fingerprint": str(payload.get("dataset_fingerprint") or ""),
            "artifact_hash": str(payload.get("artifact_hash") or ""),
            "content_sha256": str(
                payload.get("content_sha256")
                or evidence_refs.get("content_sha256")
                or ""
            ),
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
            "content_sha256": "",
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
        "content_sha256": "",
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


def _canonical_registered_model_uri(ref: dict[str, Any]) -> str:
    model_uri = str(ref.get("model_uri") or "").strip()
    model_name = str(ref.get("model_name") or "").strip()
    model_version = str(ref.get("model_version") or "").strip()
    is_registered_uri = model_uri.startswith("models:/")
    if is_registered_uri and "@" in model_uri:
        raise ValueError(
            f"artifact_registry_uri_moving_alias:{model_uri}; exact version is required"
        )
    if not (is_registered_uri or model_version):
        return model_uri
    if not model_name or re.fullmatch(r"[0-9]+", model_version) is None:
        raise ValueError(
            f"artifact_registry_version_invalid:{model_uri or model_name}:"
            " model_name and numeric model_version are required"
        )
    expected_uri = f"models:/{model_name}/{model_version}"
    if model_uri and model_uri != expected_uri:
        raise ValueError(
            f"artifact_registry_uri_mismatch:expected:{expected_uri}|actual:{model_uri}"
        )
    return expected_uri


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
    model_uri = (
        _canonical_registered_model_uri(ref)
        if isinstance(value, dict)
        else str(ref.get("model_uri") or "").strip()
    )
    expected_hash: str | None = None
    if isinstance(value, dict):
        expected_hash = str(ref.get("artifact_hash") or "").strip().lower()
        if (local_path or model_uri) and not expected_hash:
            raise ValueError(
                f"artifact_registry_hash_missing:{local_path or model_uri}; registration is required"
            )
    local_error: Exception | None = None
    if local_path:
        try:
            candidate = _resolve_local_path(local_path, project_root=root)
            if expected_hash is not None:
                validate_artifact_contract(
                    candidate,
                    label=f"registered_local:{local_path}",
                    expected_digest=expected_hash,
                )
            return candidate
        except Exception as exc:
            local_error = exc
            if not model_uri:
                raise
    if model_uri and bool(s.mlflow_enabled):
        mlflow = _get_mlflow()
        mlflow.set_tracking_uri(str(s.mlflow_tracking_uri))
        registry_uri = str(s.mlflow_registry_uri or s.mlflow_tracking_uri).strip()
        if registry_uri:
            mlflow.set_registry_uri(registry_uri)
        cache_key = expected_hash or hashlib.sha256(model_uri.encode("utf-8")).hexdigest()
        dst = Path(cache_root or s.mlflow_cache_root).expanduser() / cache_key
        dst.mkdir(parents=True, exist_ok=True)
        downloaded = Path(mlflow.artifacts.download_artifacts(artifact_uri=model_uri, dst_path=str(dst)))
        candidate = _legacy_dir_from_download(downloaded)
        validate_artifact_contract(
            candidate,
            label=f"registered_uri:{model_uri}",
            expected_digest=expected_hash,
        )
        return candidate
    if local_error is not None:
        raise RuntimeError(
            f"registered_local_artifact_rejected:{local_path}; registered URI was unavailable"
        ) from local_error
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
