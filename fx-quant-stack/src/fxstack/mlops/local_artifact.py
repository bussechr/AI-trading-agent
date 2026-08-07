"""Local-only artifact references for the installed production runtime.

This module deliberately has no registry client, downloader, cache writer, or
MLflow import. External training and activation code uses ``model_uri``;
production model loading imports this module directly.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from fxstack._lazy import lazy_get_settings as get_settings

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
            "model_version": (
                ""
                if payload.get("model_version") in (None, "")
                else str(payload.get("model_version"))
            ),
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
    return local_path or model_uri


def _resolve_local_path(path_value: str, *, project_root: Path) -> Path:
    txt = str(path_value or "").strip()
    if not txt:
        raise FileNotFoundError("empty model artifact path")
    raw = Path(txt.replace("\\", "/")).expanduser()
    for candidate in (raw, project_root / raw, project_root.parent / raw):
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(f"model artifact not found: {path_value}")


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
    """Resolve and validate an already-present artifact; never fetch or write."""

    del cache_root
    root = Path(project_root) if project_root is not None else Path(get_settings().project_root)
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
    if local_path:
        try:
            candidate = _resolve_local_path(local_path, project_root=root)
            if expected_hash is not None:
                from fxstack.models.artifact_contract import validate_artifact_contract

                validate_artifact_contract(
                    candidate,
                    label=f"registered_local:{local_path}",
                    expected_digest=expected_hash,
                )
            return candidate
        except Exception as exc:
            if not model_uri:
                raise
            raise RuntimeError(
                f"registered_local_artifact_rejected:{local_path}; "
                "remote registry access is unavailable in the production runtime"
            ) from exc
    if model_uri:
        raise RuntimeError(
            f"remote_model_artifact_unavailable_in_production_runtime:{model_uri}"
        )
    return _resolve_local_path(local_path, project_root=root)


__all__ = [
    "artifact_ref_value",
    "is_model_uri",
    "normalize_artifact_ref",
    "resolve_model_artifact_path",
]
