from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.stage_candidate_registry import stage_candidate_registry


def test_stage_candidate_registry_copies_and_rebases_only_artifact_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = tmp_path / "candidate" / "artifacts"
    model = source / "eurusd" / "intraday_xgb"
    model.mkdir(parents=True)
    (model / "meta.json").write_text("{}", encoding="utf-8")
    registry = tmp_path / "candidate" / "registry" / "eurusd_run.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "artifacts": {
                    "intraday_xgb": {
                        "path": str(model),
                        "evidence_refs": {"meta": str(model / "meta.json")},
                    }
                },
                "feature_repo": str(tmp_path / "candidate" / "feature_repo"),
            }
        ),
        encoding="utf-8",
    )

    result = stage_candidate_registry(
        repo_root=repo,
        candidate_artifact_root=source,
        registry_file=registry,
        destination_artifact_root="fx-quant-stack/artifacts_shadow/full_run",
        destination_registry_root="fx-quant-stack/artifacts_shadow/registry_run",
    )

    staged = json.loads((repo / result["registry_file"]).read_text(encoding="utf-8"))
    assert staged["artifacts"]["intraday_xgb"]["path"] == (
        "fx-quant-stack/artifacts_shadow/full_run/eurusd/intraday_xgb"
    )
    assert staged["artifacts"]["intraday_xgb"]["evidence_refs"]["meta"].endswith(
        "full_run/eurusd/intraday_xgb/meta.json"
    )
    assert staged["feature_repo"] == str(tmp_path / "candidate" / "feature_repo")
    assert result["validated_artifacts"] == 1


def test_stage_candidate_registry_rejects_active_or_external_destination(tmp_path: Path) -> None:
    source = tmp_path / "candidate" / "artifacts"
    source.mkdir(parents=True)
    registry = tmp_path / "registry.json"
    registry.write_text('{"artifacts": {}}', encoding="utf-8")
    repo = tmp_path / "repo"

    with pytest.raises(ValueError, match="artifacts_shadow"):
        stage_candidate_registry(
            repo_root=repo,
            candidate_artifact_root=source,
            registry_file=registry,
            destination_artifact_root="fx-quant-stack/artifacts/active",
            destination_registry_root="fx-quant-stack/artifacts_shadow/registry_run",
        )
