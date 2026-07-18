from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.operator_plane.openclaw.service import (
    OpenClawPermissionError,
    OpenClawSupervisor,
    SessionClassPolicy,
    default_config,
)


def _seed_window(repo_root: Path) -> None:
    window_dir = repo_root / "artifacts" / "orchestration" / "exp-2" / "trend"
    window_dir.mkdir(parents=True, exist_ok=True)
    (window_dir / "aggregate.json").write_text(
        json.dumps({"comparison": {"comparable_cycle_count": 2}, "window_status": {"status": "HOLD"}}, indent=2),
        encoding="utf-8",
    )
    (window_dir / "guardrails.json").write_text(json.dumps({"checks": {}}, indent=2), encoding="utf-8")
    (window_dir / "promotion_pack.md").write_text("# Promotion Pack\n", encoding="utf-8")


def test_sandbox_policy_is_enforced(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _seed_window(repo_root)
    config = default_config(
        enabled=True,
        repo_root_path=repo_root,
        state_root=tmp_path / "state",
        staging_workspace_root=tmp_path / "staging",
        release_root=tmp_path / "release",
    )
    config.sessions["operator-write-staging"] = SessionClassPolicy(
        name="operator-write-staging",
        scope="operator.write",
        sandbox_required=False,
        workspace_mode="staging_write",
        workspace_root=str((tmp_path / "staging").resolve()),
        scratch_root=str((tmp_path / "scratch").resolve()),
        allow_repo_writes=True,
    )
    supervisor = OpenClawSupervisor(config=config)
    with pytest.raises(OpenClawPermissionError):
        supervisor.start_flow(
            "draft_experiment",
            session_name="operator-write-staging",
            experiment_id="exp-2",
            window="trend",
            revision="r1",
        )


def test_side_effecting_flows_dedupe_on_idempotency_key(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _seed_window(repo_root)
    config = default_config(
        enabled=True,
        repo_root_path=repo_root,
        state_root=tmp_path / "state",
        staging_workspace_root=tmp_path / "staging",
        release_root=tmp_path / "release",
    )
    supervisor = OpenClawSupervisor(config=config)
    first = supervisor.start_flow(
        "draft_experiment",
        session_name="operator-write-staging",
        experiment_id="exp-2",
        window="trend",
        revision="r1",
        payload={"note": "same"},
    )
    second = supervisor.start_flow(
        "draft_experiment",
        session_name="operator-write-staging",
        experiment_id="exp-2",
        window="trend",
        revision="r1",
        payload={"note": "same"},
    )
    assert first["status"] == "completed"
    assert second["status"] == "duplicate"
    assert second["run_id"] == first["run_id"]


def test_disabled_supervisor_is_filesystem_inert(tmp_path) -> None:
    state_root = tmp_path / "disabled-state"
    config = default_config(
        enabled=False,
        repo_root_path=tmp_path,
        state_root=state_root,
        staging_workspace_root=tmp_path / "staging",
        release_root=tmp_path / "release",
    )
    supervisor = OpenClawSupervisor(config=config)
    assert supervisor.describe()["enabled"] is False
    assert supervisor.start_flow("replay_window", session_name="operator-read")["status"] == "disabled"
    assert not state_root.exists()


def test_enabled_supervisor_rejects_disabled_sandbox_before_writing(tmp_path) -> None:
    state_root = tmp_path / "unsafe-state"
    config = default_config(
        enabled=True,
        repo_root_path=tmp_path,
        state_root=state_root,
        staging_workspace_root=tmp_path / "staging",
        release_root=tmp_path / "release",
    )
    config.sandbox_required = False
    with pytest.raises(OpenClawPermissionError, match="sandbox_required"):
        OpenClawSupervisor(config=config)
    assert not state_root.exists()
