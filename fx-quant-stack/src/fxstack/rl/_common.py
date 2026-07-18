from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.settings import get_settings


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _json_dump(path: Path, payload: dict[str, Any]) -> Path:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _csv_dump(path: Path, rows: list[dict[str, Any]]) -> Path:
    _ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as fh:
        if not rows:
            fh.write("")
            return path
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _maybe_mlflow_log(run_name: str, metrics: dict[str, float], params: dict[str, Any], artifacts: list[Path]) -> dict[str, Any]:
    try:
        import mlflow
    except Exception as exc:  # pragma: no cover - optional dependency path
        return {"ok": False, "reason": f"mlflow_unavailable:{type(exc).__name__}"}

    s = get_settings()
    if bool(getattr(s, "mlflow_enabled", False)):
        try:
            tracking_uri = str(s.mlflow_tracking_uri or "").strip()
            registry_uri = str(s.mlflow_registry_uri or "").strip()
            if tracking_uri:
                mlflow.set_tracking_uri(tracking_uri)
            if registry_uri:
                mlflow.set_registry_uri(registry_uri)
        except Exception:
            pass
    with mlflow.start_run(run_name=run_name):
        for key, value in params.items():
            mlflow.log_param(str(key), value)
        for key, value in metrics.items():
            mlflow.log_metric(str(key), float(value))
        logged: list[str] = []
        for artifact in artifacts:
            if artifact.exists():
                mlflow.log_artifact(str(artifact))
                logged.append(str(artifact))
    return {"ok": True, "logged_artifacts": logged}


@dataclass(slots=True)
class RLArtifactBundle:
    root: Path
    run_name: str
    status: str
    summary_path: Path
    transitions_path: Path
    metrics_path: Path
    metadata_path: Path
    mlflow: dict[str, Any]
    artifact_kind: str = ""
    artifact_manifest_path: Path | None = None
    policy_manifest_path: Path | None = None
    checkpoint_path: Path | None = None
    artifacts: list[Path] = field(default_factory=list)
    checkpoint_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in list(out.items()):
            if isinstance(value, Path):
                out[key] = str(value)
            elif isinstance(value, list):
                out[key] = [str(item) if isinstance(item, Path) else item for item in value]
            if value is None and key == "checkpoint_path":
                out[key] = ""
        return out


def _dataset_fingerprint(frame: pd.DataFrame, *, namespace: dict[str, Any] | None = None) -> str:
    payload = {
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "namespace": dict(namespace or {}),
        "head": frame.head(25).to_dict(orient="records") if not frame.empty else [],
    }
    return _hash_payload(payload)


def build_rl_policy_manifest(
    *,
    artifact_bundle: RLArtifactBundle,
    policy_name: str,
    stage: str,
    dataset_path: str = "",
    dataset_fingerprint: str = "",
    policy_role: str = "primary",
    policy_family: str = "rl_replay_linear",
    policy_manifest_path: Path | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checkpoint_path = Path(artifact_bundle.checkpoint_path) if artifact_bundle.checkpoint_path else None
    checkpoint_content_sha256 = (
        hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
        if checkpoint_path is not None and checkpoint_path.is_file()
        else ""
    )
    checkpoint_ref = {
        "path": str(checkpoint_path or ""),
        "content_sha256": checkpoint_content_sha256,
        "runtime_compatible": True,
    }
    artifact_manifest_path = Path(artifact_bundle.artifact_manifest_path) if artifact_bundle.artifact_manifest_path else None
    policy_manifest_ref = Path(policy_manifest_path) if policy_manifest_path else None
    checkpoint_summary = dict(artifact_bundle.checkpoint_summary or {})
    artifact_paths = {
        "summary_path": str(artifact_bundle.summary_path),
        "transitions_path": str(artifact_bundle.transitions_path),
        "metrics_path": str(artifact_bundle.metrics_path),
        "metadata_path": str(artifact_bundle.metadata_path),
        "artifact_manifest_path": str(artifact_manifest_path or ""),
        "policy_manifest_path": str(policy_manifest_ref or ""),
        "checkpoint_path": str(checkpoint_path or ""),
    }
    primary_policy = bool(str(policy_role or "").strip().lower() == "primary")
    return {
        "manifest_version": "rl_policy_manifest_v2",
        "artifact_kind": "rl_policy",
        "policy_role": str(policy_role or "primary"),
        "primary_policy": primary_policy,
        "policy_name": str(policy_name or artifact_bundle.run_name),
        "policy_family": str(policy_family or "rl_replay_linear"),
        "stage": str(stage or ""),
        "run_name": str(artifact_bundle.run_name),
        "status": str(artifact_bundle.status),
        "dataset_path": str(dataset_path or ""),
        "dataset_fingerprint": str(dataset_fingerprint or ""),
        "checkpoint_path": str(checkpoint_path or ""),
        "checkpoint_content_sha256": checkpoint_content_sha256,
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_exists": bool(checkpoint_path and checkpoint_path.exists()),
        "checkpoint_summary": checkpoint_summary,
        "artifact_paths": artifact_paths,
        "artifact_bundle": artifact_bundle.to_dict(),
        "discovery": {
            "primary_policy": primary_policy,
            "policy_name": str(policy_name or artifact_bundle.run_name),
            "policy_role": str(policy_role or "primary"),
            "policy_family": str(policy_family or "rl_replay_linear"),
            "stage": str(stage or ""),
            "checkpoint_path": str(checkpoint_path or ""),
            "checkpoint_content_sha256": checkpoint_content_sha256,
            "checkpoint_ref": checkpoint_ref,
            "policy_manifest_path": str(policy_manifest_ref or ""),
            "artifact_manifest_path": str(artifact_manifest_path or ""),
        },
        "metadata": dict(extra_metadata or {}),
    }
