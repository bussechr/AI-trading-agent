from __future__ import annotations

import json
from pathlib import Path

from fxstack.settings import get_settings
from services.operator_plane.mcp_release_registry.server import ReleaseRegistryMCPServer, ReleaseRegistryServerConfig
from services.operator_plane.mcp_runtime_state.server import RuntimeStateMCPServer, RuntimeStateServerConfig
from services.operator_plane.mcp_twin_artefacts.server import TwinArtefactsMCPServer, TwinArtefactsServerConfig, default_config as twin_default_config


def test_runtime_state_server_capabilities_are_read_only() -> None:
    payloads = {
        "/v2/ready": {"runtime_status": "running", "runtime_ready": True, "bridge_up": True},
        "/v2/state": {
            "pending_command_count": 0,
            "submitted_entry_count": 3,
            "signals_sent": 5,
            "runtime_diag": {
                "entry_execution_policy": {
                    "approved_entry_count": 2,
                    "submitted_entry_count": 3,
                },
                "shadow_policy": {"divergence_counts": {"liveOnly": 1}},
                "adaptive_shadow_policy": {"divergence_counts": {"liveOnly": 2}},
            },
            "capital_governance": {"canary_active": True},
            "orchestration_live": {"enabled": True, "current_stage_pct": 30, "runtime_enabled": True},
            "feature_online_ready": True,
            "feature_data_fresh": True,
            "feature_push_backlog": 0,
            "feature_blocker_reason": "",
            "shadow_orchestrator": {"fault_count": 1},
        },
        "/v2/decision-snapshots": {"items": []},
        "/v2/orchestration/runs": {"items": [{"run_id": "run-1"}]},
        "/v2/orchestration/traces": {"items": [{"trace_id": "trace-1"}]},
    }
    server = RuntimeStateMCPServer(
        config=RuntimeStateServerConfig(
            enabled=True,
            transport="stdio",
            base_url="http://127.0.0.1:58710",
            api_key="",
        ),
        fetch_json=lambda path: dict(payloads[path]),
    ).build_server()
    description = server.describe()
    assert description["transport"] == "stdio"
    assert {item["name"] for item in description["tools"]} == {
        "list_recent_snapshots",
        "get_orchestration_run",
        "get_orchestration_trace",
    }
    assert all(item["annotations"]["readOnlyHint"] for item in description["tools"])

    health = json.loads(RuntimeStateMCPServer(
        config=RuntimeStateServerConfig(
            enabled=True,
            transport="stdio",
            base_url="http://127.0.0.1:58710",
            api_key="",
        ),
        fetch_json=lambda path: dict(payloads[path]),
    )._resource_health("runtime://health/summary")["text"])
    assert health["approved_entry_count"] == 2
    assert health["acked_entry_count"] == 5
    assert health["canary_active"] is True
    assert health["feature_online_ready"] is True
    assert health["feature_data_fresh"] is True
    assert health["divergence_spike_count"] == 4


def test_twin_and_release_servers_publish_expected_capabilities(tmp_path) -> None:
    artefacts_root = tmp_path / "artifacts" / "orchestration" / "exp-3" / "shock"
    artefacts_root.mkdir(parents=True)
    (artefacts_root / "aggregate.json").write_text(json.dumps({"window_status": {"status": "GO"}}, indent=2), encoding="utf-8")
    (artefacts_root / "guardrails.json").write_text(json.dumps({"checks": {}}, indent=2), encoding="utf-8")
    (artefacts_root / "promotion_pack.md").write_text("# Pack\n", encoding="utf-8")

    twin_server = TwinArtefactsMCPServer(
        config=TwinArtefactsServerConfig(enabled=True, transport="stdio", artifacts_root=(tmp_path / "artifacts" / "orchestration")),
    ).build_server()
    twin_description = twin_server.describe()
    assert {item["name"] for item in twin_description["resources"]} == {
        "twin.orchestration.index",
        "twin.orchestration.summary",
        "twin.orchestration.bundles.index",
    }
    assert {item["name"] for item in twin_description["prompts"]} == {
        "replay-analysis",
        "divergence-review",
        "bundle-review",
    }
    assert {item["name"] for item in twin_description["tools"]} == {
        "list_experiments",
        "read_artifact_file",
        "summarize_window",
        "list_experiment_bundles",
        "read_experiment_bundle",
    }

    manifest_path = tmp_path / "fx-quant-stack" / "artifacts" / "active_models.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"active_model_sets": {"EURUSD": {"registry_path": str(tmp_path / 'fx-quant-stack' / 'artifacts' / 'registry' / 'eurusd.json')}}}, indent=2),
        encoding="utf-8",
    )
    registry_root = tmp_path / "fx-quant-stack" / "artifacts" / "registry"
    registry_root.mkdir(parents=True, exist_ok=True)
    (registry_root / "eurusd.json").write_text(json.dumps({"pair": "EURUSD"}, indent=2), encoding="utf-8")
    release_root = tmp_path / "fx-quant-stack" / "artifacts" / "releases"
    (release_root / "eurusd" / "bundle-1").mkdir(parents=True, exist_ok=True)
    promotion_ledger_root = tmp_path / "fx-quant-stack" / "artifacts" / "eurusd" / "reports"
    promotion_ledger_root.mkdir(parents=True, exist_ok=True)
    (promotion_ledger_root / "promotion_decision.json").write_text(
        json.dumps({"status": "eligible", "policy": "balanced", "delta": 0.25, "gates": {"cv": True}}, indent=2),
        encoding="utf-8",
    )

    release_server = ReleaseRegistryMCPServer(
        config=ReleaseRegistryServerConfig(
            enabled=True,
            transport="stdio",
            manifest_path=manifest_path,
            registry_root=registry_root,
            release_root=release_root,
        ),
    ).build_server()
    release_description = release_server.describe()
    assert {item["name"] for item in release_description["resources"]} == {
        "release.active_manifest",
        "release.registry_index",
        "release.candidates_index",
        "release.approval_pack_index",
        "release.promotion_ledger_index",
    }
    assert {item["name"] for item in release_description["prompts"]} == {
        "release-inspection",
        "approval-pack-draft",
        "promotion-ledger-review",
    }
    assert {item["name"] for item in release_description["tools"]} == {
        "resolve_active_model_set",
        "list_release_candidates",
        "inspect_manifest_consistency",
        "list_promotion_ledger_entries",
        "read_promotion_ledger_entry",
    }
    assert all(item["annotations"]["readOnlyHint"] for item in release_description["tools"])


def test_mcp_default_config_honors_settings_flags(monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_MCP_ENABLED", "true")
    monkeypatch.setenv("FXSTACK_MCP_TRANSPORT", "stdio")
    get_settings.cache_clear()
    try:
        config = twin_default_config()
        assert config.enabled is True
        assert config.transport == "stdio"
    finally:
        get_settings.cache_clear()
