from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass
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

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        for key, value in list(out.items()):
            if isinstance(value, Path):
                out[key] = str(value)
        return out


def _dataset_fingerprint(frame: pd.DataFrame, *, namespace: dict[str, Any] | None = None) -> str:
    payload = {
        "rows": int(len(frame)),
        "columns": list(frame.columns),
        "namespace": dict(namespace or {}),
        "head": frame.head(25).to_dict(orient="records") if not frame.empty else [],
    }
    return _hash_payload(payload)
