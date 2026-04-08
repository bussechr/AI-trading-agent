from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.operator_plane.openclaw.service import OpenClawPermissionError, OpenClawSupervisor, default_config


def _write_window_artifacts(root: Path, *, experiment_id: str, window: str) -> Path:
    window_dir = root / "artifacts" / "orchestration" / experiment_id / window
    window_dir.mkdir(parents=True, exist_ok=True)
    (window_dir / "aggregate.json").write_text(
        json.dumps(
            {
                "comparison": {"comparable_cycle_count": 4},
                "window_status": {"status": "GO"},
                "lanes": {"baseline": {"entries": 2}},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (window_dir / "guardrails.json").write_text(
        json.dumps({"checks": {"entry_ratio_floor": {"passed": True}}}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (window_dir / "promotion_pack.md").write_text("# Promotion Pack\n", encoding="utf-8")
    return window_dir


def test_flow_scope_enforcement_and_staging_writes(tmp_path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_root = tmp_path / "staging"
    state_root = tmp_path / "state"
    release_root = staging_root / "fx-quant-stack" / "artifacts" / "releases"
    _write_window_artifacts(repo_root, experiment_id="exp-1", window="calm")

    config = default_config(
        enabled=True,
        repo_root_path=repo_root,
        state_root=state_root,
        staging_workspace_root=staging_root,
        release_root=release_root,
    )
    supervisor = OpenClawSupervisor(config=config)

    replay = supervisor.start_flow(
        "replay_window",
        session_name="operator-read",
        experiment_id="exp-1",
        window="calm",
        payload={"seed": 7},
        execute=False,
    )
    assert replay["status"] == "planned"
    assert replay["session_name"] == "operator-read"

    with pytest.raises(OpenClawPermissionError):
        supervisor.start_flow(
            "draft_experiment",
            session_name="operator-read",
            experiment_id="exp-1",
            window="calm",
            revision="r2",
            payload={},
        )

    draft = supervisor.start_flow(
        "draft_experiment",
        session_name="operator-write-staging",
        experiment_id="exp-1",
        window="calm",
        revision="r2",
        payload={},
    )
    assert draft["status"] == "completed"
    assert draft["workspace_root"] == str(staging_root.resolve())
    assert all(str(Path(path)).startswith(str(staging_root.resolve())) for path in draft["artifact_refs"])
    assert not any(str(Path(path)).startswith(str(repo_root.resolve())) for path in draft["artifact_refs"])

    paper = supervisor.start_flow(
        "prepare_paper_pack",
        session_name="operator-write-staging",
        experiment_id="exp-1",
        window="calm",
        revision="r2",
        payload={},
    )
    assert paper["status"] == "completed"
    assert all(str(Path(path)).startswith(str(release_root.resolve())) for path in paper["artifact_refs"])

    improvement = supervisor.start_flow(
        "improvement_factory",
        session_name="operator-write-staging",
        experiment_id="exp-1",
        window="calm",
        revision="r2",
        payload={},
        execute=False,
    )
    assert improvement["status"] == "completed"
    assert improvement["taskflow_mode"] == "managed"
    assert improvement["result"]["step_chain"] == [
        "analyse_divergence",
        "draft_experiment",
        "collect_approval_pack",
        "prepare_paper_pack",
    ]
    assert any(str(Path(path)).startswith(str(staging_root.resolve())) for path in improvement["artifact_refs"])
    assert any(str(Path(path)).startswith(str(release_root.resolve())) for path in improvement["artifact_refs"])


def test_disabled_operator_plane_returns_disabled_status(tmp_path) -> None:
    config = default_config(
        enabled=False,
        repo_root_path=tmp_path / "repo",
        state_root=tmp_path / "state",
        staging_workspace_root=tmp_path / "staging",
        release_root=tmp_path / "release",
    )
    supervisor = OpenClawSupervisor(config=config)
    result = supervisor.start_flow("replay_window", session_name="operator-read", execute=False)
    assert result["status"] == "disabled"
    assert result["reason"] == "openclaw_disabled"
