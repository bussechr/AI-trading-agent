from __future__ import annotations

from pathlib import Path

from fxstack.mlops.lineage import compute_lineage_snapshot


def dataset_fingerprint(
    *,
    data_paths: list[Path],
    feature_schema: dict,
    run_id: str = "",
    label_config: dict | None = None,
    risk_config: dict | None = None,
    training_config: dict | None = None,
    project_root: Path | None = None,
) -> str:
    snapshot = compute_lineage_snapshot(
        feature_paths=list(data_paths),
        feature_schema=dict(feature_schema or {}),
        label_config=dict(label_config or {}),
        risk_config=dict(risk_config or {}),
        training_config={**dict(training_config or {}), "legacy_run_id": str(run_id or "")},
        project_root=project_root,
    )
    return str(snapshot.dataset_fingerprint)
