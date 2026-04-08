from __future__ import annotations

import json
from pathlib import Path

from services.operator_plane.mcp_release_registry.server import ReleaseRegistryMCPServer, ReleaseRegistryServerConfig
from services.operator_plane.mcp_runtime_state.server import RuntimeStateMCPServer, RuntimeStateServerConfig
from services.operator_plane.mcp_twin_artefacts.server import TwinArtefactsMCPServer, TwinArtefactsServerConfig
from services.operator_plane.openclaw.service import OpenClawSupervisor, default_config


def test_runtime_mcp_paths_and_tools_stay_read_only() -> None:
    payloads = {
        "/v2/ready": {},
        "/v2/state": {},
        "/v2/decision-snapshots": {"items": []},
        "/v2/orchestration/runs": {"items": []},
        "/v2/orchestration/traces": {"items": []},
    }
    runtime_server = RuntimeStateMCPServer(
        config=RuntimeStateServerConfig(enabled=True, transport="stdio", base_url="http://127.0.0.1:58710", api_key=""),
        fetch_json=lambda path: dict(payloads[path]),
    )
    assert runtime_server.allowed_paths == [
        "/v2/ready",
        "/v2/state",
        "/v2/decision-snapshots",
        "/v2/orchestration/runs",
        "/v2/orchestration/traces",
    ]
    assert all("/poll" not in path for path in runtime_server.allowed_paths)


def test_operator_plane_does_not_touch_active_manifest_or_execution_surfaces(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    manifest_path = repo_root / "fx-quant-stack" / "artifacts" / "active_models.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"active_model_sets": {}}, indent=2), encoding="utf-8")

    window_dir = repo_root / "artifacts" / "orchestration" / "exp-4" / "calm"
    window_dir.mkdir(parents=True, exist_ok=True)
    (window_dir / "aggregate.json").write_text(json.dumps({"comparison": {}, "window_status": {"status": "GO"}}, indent=2), encoding="utf-8")
    (window_dir / "guardrails.json").write_text(json.dumps({"checks": {}}, indent=2), encoding="utf-8")
    (window_dir / "promotion_pack.md").write_text("# Promotion Pack\n", encoding="utf-8")

    config = default_config(
        enabled=True,
        repo_root_path=repo_root,
        state_root=tmp_path / "state",
        staging_workspace_root=tmp_path / "staging",
        release_root=tmp_path / "release",
    )
    supervisor = OpenClawSupervisor(config=config)
    before = manifest_path.read_text(encoding="utf-8")
    result = supervisor.start_flow(
        "prepare_paper_pack",
        session_name="operator-write-staging",
        experiment_id="exp-4",
        window="calm",
        revision="r3",
        payload={},
    )
    after = manifest_path.read_text(encoding="utf-8")
    assert before == after
    assert result["status"] == "completed"
    assert "open_pr" in supervisor.describe()["flows"]
    assert "replay_window" in supervisor.describe()["flows"]

    twin_server = TwinArtefactsMCPServer(
        config=TwinArtefactsServerConfig(enabled=True, transport="stdio", artifacts_root=(repo_root / "artifacts" / "orchestration")),
    ).build_server()
    release_server = ReleaseRegistryMCPServer(
        config=ReleaseRegistryServerConfig(
            enabled=True,
            transport="stdio",
            manifest_path=manifest_path,
            registry_root=(repo_root / "fx-quant-stack" / "artifacts" / "registry"),
            release_root=(tmp_path / "release"),
        ),
    ).build_server()
    assert all(item["annotations"]["readOnlyHint"] for item in twin_server.describe()["tools"])
    assert all(item["annotations"]["readOnlyHint"] for item in release_server.describe()["tools"])
