from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from fxstack.mlops.types import LineageSnapshot
from fxstack.settings import get_settings


def _get_mlflow() -> Any:
    try:
        import mlflow  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised in envs without mlflow
        raise RuntimeError(
            "MLflow support requires the `mlflow` package. Install the fx-quant-stack research extra or add mlflow."
        ) from exc
    return mlflow


def mlflow_available() -> bool:
    try:
        _get_mlflow()
    except Exception:
        return False
    return True


def configure_mlflow() -> Any:
    s = get_settings()
    mlflow = _get_mlflow()
    mlflow.set_tracking_uri(str(s.mlflow_tracking_uri))
    registry_uri = str(s.mlflow_registry_uri or s.mlflow_tracking_uri).strip()
    if registry_uri:
        mlflow.set_registry_uri(registry_uri)
    return mlflow


def build_standard_run_tags(
    *,
    git_sha: str,
    experiment_family: str,
    pair: str,
    timeframe: str,
    training_window: str = "",
    validation_window: str = "",
    feature_service_version: str = "",
    label_version: str = "",
    risk_config_version: str = "",
    model_family: str = "",
    hyperparameter_profile: str = "",
    hardware_profile: str = "",
    activation_candidate: str = "",
    bundle_run_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, str]:
    tags = {
        "fxstack.git_sha": str(git_sha or ""),
        "fxstack.experiment_family": str(experiment_family or ""),
        "fxstack.pair": str(pair or "").upper(),
        "fxstack.timeframe": str(timeframe or "").upper(),
        "fxstack.training_window": str(training_window or ""),
        "fxstack.validation_window": str(validation_window or ""),
        "fxstack.feature_service_version": str(feature_service_version or ""),
        "fxstack.label_version": str(label_version or ""),
        "fxstack.risk_config_version": str(risk_config_version or ""),
        "fxstack.model_family": str(model_family or ""),
        "fxstack.hyperparameter_profile": str(hyperparameter_profile or "default"),
        "fxstack.hardware_profile": str(hardware_profile or "unspecified"),
        "fxstack.activation_candidate": str(activation_candidate or ""),
        "fxstack.bundle_run_id": str(bundle_run_id or ""),
    }
    for key, value in dict(extra or {}).items():
        tags[str(key)] = "" if value is None else str(value)
    return tags


class MlflowRunContext(AbstractContextManager["MlflowRunContext"]):
    def __init__(
        self,
        *,
        experiment_name: str,
        run_name: str,
        tags: dict[str, Any] | None = None,
        lineage: LineageSnapshot | None = None,
        enabled: bool | None = None,
    ) -> None:
        s = get_settings()
        self.enabled = bool(s.mlflow_enabled) if enabled is None else bool(enabled)
        self.experiment_name = str(experiment_name or "")
        self.run_name = str(run_name or "")
        self.tags = {str(k): str(v) for k, v in dict(tags or {}).items()}
        self.lineage = lineage
        self.run_id = ""
        self._active = False
        self._mlflow = None

    def __enter__(self) -> "MlflowRunContext":
        if not self.enabled:
            return self
        self._mlflow = configure_mlflow()
        self._mlflow.set_experiment(self.experiment_name)
        active = self._mlflow.start_run(run_name=self.run_name)
        self.run_id = str(active.info.run_id)
        self._active = True
        if self.tags:
            self._mlflow.set_tags(self.tags)
        if self.lineage is not None:
            self.log_dict(self.lineage.to_dict(), "lineage.json")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool | None:
        if self.enabled and self._active and self._mlflow is not None:
            status = "FAILED" if exc is not None else "FINISHED"
            self._mlflow.end_run(status=status)
            self._active = False
        return None

    def log_params(self, params: dict[str, Any]) -> None:
        if not self.enabled or not self._active or self._mlflow is None:
            return
        safe = {
            str(key): str(value)
            for key, value in dict(params or {}).items()
            if value is not None and str(value) != ""
        }
        if safe:
            self._mlflow.log_params(safe)

    def log_metrics(self, metrics: dict[str, Any]) -> None:
        if not self.enabled or not self._active or self._mlflow is None:
            return
        safe: dict[str, float] = {}
        for key, value in dict(metrics or {}).items():
            try:
                safe[str(key)] = float(value)
            except Exception:
                continue
        if safe:
            self._mlflow.log_metrics(safe)

    def log_artifact(self, path: Path | str, artifact_path: str | None = None) -> None:
        if not self.enabled or not self._active or self._mlflow is None:
            return
        self._mlflow.log_artifact(str(path), artifact_path=artifact_path)

    def log_artifacts(self, path: Path | str, artifact_path: str | None = None) -> None:
        if not self.enabled or not self._active or self._mlflow is None:
            return
        self._mlflow.log_artifacts(str(path), artifact_path=artifact_path)

    def log_text(self, text: str, artifact_file: str) -> None:
        if not self.enabled or not self._active or self._mlflow is None:
            return
        self._mlflow.log_text(str(text), artifact_file=artifact_file)

    def log_dict(self, payload: dict[str, Any], artifact_file: str) -> None:
        if not self.enabled or not self._active or self._mlflow is None:
            return
        self._mlflow.log_text(json.dumps(payload, indent=2, sort_keys=True), artifact_file=artifact_file)


def log_evidence_bundle(
    *,
    run: MlflowRunContext,
    evidence_files: dict[str, Path | str] | None = None,
    evidence_dicts: dict[str, dict[str, Any]] | None = None,
) -> None:
    for artifact_file, raw_path in dict(evidence_files or {}).items():
        path = Path(str(raw_path))
        if path.exists():
            run.log_artifact(path, artifact_path=str(Path(artifact_file).parent))
    for artifact_file, payload in dict(evidence_dicts or {}).items():
        run.log_dict(dict(payload or {}), artifact_file=str(artifact_file))
