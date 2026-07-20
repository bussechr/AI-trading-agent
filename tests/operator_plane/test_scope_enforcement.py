from __future__ import annotations

from pathlib import Path

import pytest

from services.operator_plane.openclaw.service import OpenClawPermissionError, OpenClawSupervisor, default_config

def test_flow_scope_enforcement_and_staging_writes(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    staging_root = tmp_path / "staging"
    state_root = tmp_path / "state"
    release_root = staging_root / "fx-quant-stack" / "artifacts" / "releases"
    config = default_config(
        enabled=True,
        repo_root_path=repo_root,
        state_root=state_root,
        staging_workspace_root=staging_root,
        release_root=release_root,
    )
    supervisor = OpenClawSupervisor(config=config)

    description = supervisor.describe()
    assert set(description["sessions"]) == {"operator-write-staging"}
    assert "repo_root" not in description
    for removed_flow in ("replay_window", "analyse_divergence", "improvement_factory"):
        with pytest.raises(KeyError, match="unknown flow"):
            supervisor.start_flow(removed_flow, session_name="operator-write-staging")

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
        payload={
            "hypothesis": "A staged candidate is worth operator review.",
            "summary": {"owner": "operator"},
            "requested_checks": ["runtime shadow smoke"],
        },
    )
    assert draft["status"] == "completed"
    assert draft["result"]["input_mode"] == "operator_authored"
    assert draft["workspace_root"] == str(staging_root.resolve())
    assert all(str(Path(path)).startswith(str(staging_root.resolve())) for path in draft["artifact_refs"])
    assert not any(str(Path(path)).startswith(str(repo_root.resolve())) for path in draft["artifact_refs"])

    approval = supervisor.start_flow(
        "collect_approval_pack",
        session_name="operator-write-staging",
        experiment_id="exp-1",
        window="calm",
        revision="r2",
        payload={"decision": "paper", "reviewer": "operator"},
    )
    assert approval["status"] == "completed"
    assert approval["result"]["decision"] == "paper"

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

    with pytest.raises(OpenClawPermissionError, match="outside the operator-plane trust boundary"):
        supervisor.start_flow(
            "draft_experiment",
            session_name="operator-write-staging",
            experiment_id="exp-2",
            payload={"artifact_dir": str(repo_root / "artifacts")},
        )


def test_disabled_operator_plane_returns_disabled_status(tmp_path) -> None:
    config = default_config(
        enabled=False,
        repo_root_path=tmp_path / "repo",
        state_root=tmp_path / "state",
        staging_workspace_root=tmp_path / "staging",
        release_root=tmp_path / "release",
    )
    supervisor = OpenClawSupervisor(config=config)
    result = supervisor.start_flow("draft_experiment", session_name="operator-write-staging", execute=False)
    assert result["status"] == "disabled"
    assert result["reason"] == "openclaw_disabled"
