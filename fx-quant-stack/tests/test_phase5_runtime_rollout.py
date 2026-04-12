from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxstack.mlops.types import ActivationPackage, CanaryPlan, RollbackPlan
from fxstack.risk import MarketState, PolicyIntent, PortfolioState, RiskKernelConfig, evaluate_risk_decision
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.runner import _resolve_main_runtime_rollout_policy, _risk_cycle_summary
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings
from fxstack.training import release_workflow
from fxstack.training.activation import _merge_metadata_patch, parse_registry_entry


def _fresh_service(tmp_path: Path) -> RuntimeService:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    return RuntimeService(database_url=database_url)


def test_resolve_main_runtime_rollout_policy_prefers_explicit_canary_metadata() -> None:
    rollout = _resolve_main_runtime_rollout_policy(
        pair="EURUSD",
        metadata={
            "phase5_rollout": {
                "mode": "canary",
                "enabled": True,
                "allowlisted_pairs": ["EURUSD"],
                "budget_scale": 0.4,
                "max_total_positions": 2,
            },
            "phase5_gate_bundle": {
                "canary_gate": {"passed": True},
            },
        },
    )

    assert rollout["configured"] is True
    assert rollout["active"] is True
    assert rollout["pair_allowlisted"] is True
    assert rollout["budget_scale"] == 0.4
    assert rollout["max_total_positions"] == 2
    assert rollout["source"] == "phase5_rollout"


def test_parse_registry_entry_strips_legacy_rollout_sections_when_canonical_rollout_is_disabled(tmp_path: Path) -> None:
    def _artifact_dir(name: str) -> str:
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        path.joinpath("meta.json").write_text("{}", encoding="utf-8")
        return str(path)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "pair": "EURUSD",
                "run_id": "bundle-1",
                "tier": "tier2",
                "artifacts": {
                    "regime": _artifact_dir("regime"),
                    "meta": _artifact_dir("meta"),
                    "swing_transformer": _artifact_dir("swing_transformer"),
                    "swing_xgb": _artifact_dir("swing_xgb"),
                    "intraday_tcn": _artifact_dir("intraday_tcn"),
                    "intraday_xgb": _artifact_dir("intraday_xgb"),
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "main_runtime_rollout": {
                    "mode": "canary",
                    "enabled": False,
                    "runtime_enabled": False,
                    "allowlisted_pairs": [],
                    "budget_scale": 0.0,
                },
                "rollout": {
                    "mode": "canary",
                    "enabled": True,
                    "allowlisted_pairs": ["EURUSD"],
                    "budget_scale": 0.4,
                },
                "canary": {
                    "mode": "canary",
                    "enabled": True,
                    "allowlisted_pairs": ["EURUSD"],
                    "budget_scale": 0.4,
                },
                "activation_package": {
                    "metadata": {
                        "main_runtime_rollout": {
                            "mode": "canary",
                            "enabled": False,
                            "runtime_enabled": False,
                        },
                        "rollout": {
                            "mode": "canary",
                            "enabled": True,
                        },
                        "canary": {
                            "mode": "canary",
                            "enabled": True,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    parsed = parse_registry_entry(registry_path)
    metadata = dict(parsed["metadata"] or {})
    activation_package = dict(metadata.get("activation_package") or {})
    activation_metadata = dict(activation_package.get("metadata") or {})

    assert dict(metadata.get("main_runtime_rollout") or {}).get("enabled") is False
    assert "rollout" not in metadata
    assert "canary" not in metadata
    assert "phase5_rollout" not in metadata
    assert "phase5_runtime_rollout" not in metadata
    assert "runtime_rollout" not in metadata
    assert "rollout" not in activation_metadata
    assert "canary" not in activation_metadata

    resolved = _resolve_main_runtime_rollout_policy(pair="EURUSD", metadata=metadata)
    assert resolved["active"] is False
    assert resolved["enabled"] is False
    assert resolved["pair_allowlisted"] is False
    assert resolved["budget_scale"] == 0.0
    assert resolved["source"] == "main_runtime_rollout"


def test_merge_metadata_patch_strips_legacy_rollout_sections_when_canonical_rollout_exists() -> None:
    merged = _merge_metadata_patch(
        {
            "main_runtime_rollout": {
                "mode": "canary",
                "enabled": True,
                "allowlisted_pairs": ["EURUSD"],
            },
            "rollout": {
                "mode": "canary",
                "enabled": True,
            },
            "canary": {
                "mode": "canary",
                "enabled": True,
            },
            "activation_package": {
                "metadata": {
                    "main_runtime_rollout": {
                        "mode": "canary",
                        "enabled": True,
                    },
                    "rollout": {
                        "mode": "canary",
                        "enabled": True,
                    },
                    "canary": {
                        "mode": "canary",
                        "enabled": True,
                    },
                }
            },
        },
        {
            "main_runtime_rollout": {
                "enabled": False,
                "runtime_enabled": False,
            },
            "activation_package": {
                "metadata": {
                    "main_runtime_rollout": {
                        "enabled": False,
                        "runtime_enabled": False,
                    }
                }
            },
        },
    )

    assert "rollout" not in merged
    assert "canary" not in merged
    assert "rollout" not in dict(dict(merged.get("activation_package") or {}).get("metadata") or {})
    assert "canary" not in dict(dict(merged.get("activation_package") or {}).get("metadata") or {})


def _live_canary_package(*, pair: str = "EURUSD", allowlisted_pairs: list[str] | None = None) -> ActivationPackage:
    return ActivationPackage(
        bundle_run_id="bundle-live-1",
        pair=pair,
        target_alias="shadow",
        model_alias="shadow",
        release_status="canary_active",
        rollback_target=RollbackPlan(target_bundle_run_id="bundle-champion", target_alias="champion", target_registry_path="mlflow://EURUSD@champion"),
        canary_plan=CanaryPlan(
            plan_id="canary-1",
            scope="orchestration_live_canary",
            status="active",
            traffic_fraction=0.05,
            metadata={
                "mode": "orchestration_live",
                "allowlisted_pairs": list(allowlisted_pairs or [pair]),
                "live_pair_allowlist": list(allowlisted_pairs or [pair]),
                "live_sleeve_allowlist": ["trend"],
                "live_intent_allowlist": ["entry"],
                "budget_scale": 0.05,
                "current_stage_index": 0,
                "current_stage_pct": 1,
                "runtime_enabled": True,
                "queue_kill_active": False,
            },
        ),
        metadata={
            "main_runtime_rollout": {
                "mode": "canary",
                "enabled": True,
                "allowlisted_pairs": list(allowlisted_pairs or [pair]),
            },
            "rollout": {
                "mode": "canary",
                "enabled": True,
                "allowlisted_pairs": list(allowlisted_pairs or [pair]),
            },
            "canary": {
                "mode": "canary",
                "enabled": True,
                "allowlisted_pairs": list(allowlisted_pairs or [pair]),
            },
        },
    )


def _settings_stub(*, phase5_auto_rollback: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        phase5_auto_rollback=phase5_auto_rollback,
        phase5_canary_budget_scale=0.25,
        phase5_canary_latency_budget_ms=250.0,
        phase5_canary_stale_feature_limit=1,
        phase5_canary_drawdown_limit_pct=5.0,
        phase5_canary_calibration_drift_limit=0.1,
        phase6b_canary_p95_overhead_ms=5.0,
        phase6b_canary_p99_overhead_ms=10.0,
        phase6b_canary_ack_success_floor=0.9,
        phase6b_canary_orphan_command_limit=2,
        phase6b_canary_entry_ratio_floor=0.5,
        phase6b_canary_slot_utilisation_floor=0.5,
        phase6b_canary_alert_window_minutes=1,
        phase6b_canary_drawdown_deterioration_pct=5.0,
    )


def test_release_metadata_patch_disables_live_rollout_when_runtime_is_killed() -> None:
    package = _live_canary_package()
    assert package.canary_plan is not None
    package.canary_plan.metadata = {
        **dict(package.canary_plan.metadata or {}),
        "runtime_enabled": False,
        "queue_kill_active": True,
        "queue_kill_reason": "orphan_commands",
        "queue_killed_at": 123.0,
    }

    patch = release_workflow._release_metadata_patch(package=package, phase5_bundle={})

    assert patch["main_runtime_rollout"]["enabled"] is False
    assert patch["main_runtime_rollout"]["runtime_enabled"] is False
    assert patch["main_runtime_rollout"]["queue_kill_active"] is True
    assert "rollout" not in dict(dict(patch.get("activation_package") or {}).get("metadata") or {})
    assert "canary" not in dict(dict(patch.get("activation_package") or {}).get("metadata") or {})


def test_close_canary_reject_patches_champion_metadata_without_legacy_rollout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = _live_canary_package()
    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, object]] = []

    class DummyRuntimeService:
        def __init__(self, database_url: str):
            self.database_url = database_url

        def record_governance_event(self, *args, **kwargs) -> None:
            return None

    def fake_activate_mlflow_alias(**kwargs):
        captured.append(dict(kwargs))
        return []

    monkeypatch.setattr(release_workflow, "load_release_package", lambda **kwargs: (package, release_dir))
    monkeypatch.setattr(release_workflow, "_read_json", lambda path: {})
    monkeypatch.setattr(release_workflow, "activate_mlflow_alias", fake_activate_mlflow_alias)
    monkeypatch.setattr(release_workflow, "_persist_release_artifacts", lambda **kwargs: {"activation_package": str(release_dir / "activation_package.json")})
    monkeypatch.setattr(release_workflow, "RuntimeService", DummyRuntimeService)
    monkeypatch.setattr(release_workflow, "_patch_orchestration_live_runtime_state", lambda **kwargs: {})

    result = release_workflow.close_canary(
        pair="EURUSD",
        database_url="sqlite+pysqlite:///:memory:",
        manifest_path=tmp_path / "active_models.json",
        outcome="reject",
    )

    assert len(captured) == 1
    metadata_patch = dict(captured[0].get("metadata_patch") or {})
    activation_metadata = dict(dict(metadata_patch.get("activation_package") or {}).get("metadata") or {})

    assert result["release_status"] == "rejected"
    assert captured[0]["alias"] == "champion"
    assert metadata_patch["main_runtime_rollout"]["enabled"] is False
    assert metadata_patch["main_runtime_rollout"]["runtime_enabled"] is False
    assert "rollout" not in activation_metadata
    assert "canary" not in activation_metadata


def test_rollback_release_unwinds_every_allowlisted_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = _live_canary_package(allowlisted_pairs=["EURUSD", "GBPUSD"])
    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    captured: list[dict[str, object]] = []

    class DummyRuntimeService:
        def __init__(self, database_url: str):
            self.database_url = database_url

        def record_governance_event(self, *args, **kwargs) -> None:
            return None

    def fake_activate_mlflow_alias(**kwargs):
        captured.append(dict(kwargs))
        return []

    monkeypatch.setattr(release_workflow, "load_release_package", lambda **kwargs: (package, release_dir))
    monkeypatch.setattr(release_workflow, "_read_json", lambda path: {})
    monkeypatch.setattr(release_workflow, "activate_mlflow_alias", fake_activate_mlflow_alias)
    monkeypatch.setattr(release_workflow, "_persist_release_artifacts", lambda **kwargs: {"activation_package": str(release_dir / "activation_package.json")})
    monkeypatch.setattr(release_workflow, "RuntimeService", DummyRuntimeService)
    monkeypatch.setattr(release_workflow, "_patch_orchestration_live_runtime_state", lambda **kwargs: {})
    monkeypatch.setattr(release_workflow, "resolve_bundle_manifest_by_bundle_run_id", lambda **kwargs: object())
    monkeypatch.setattr(release_workflow, "set_bundle_alias", lambda **kwargs: None)

    result = release_workflow.rollback_release(
        pair="EURUSD",
        database_url="sqlite+pysqlite:///:memory:",
        manifest_path=tmp_path / "active_models.json",
        reason="rollout_breach",
    )

    assert result["release_status"] == "rolled_back"
    assert [call["alias"] for call in captured] == ["champion", "champion"]
    assert [call["pairs"] for call in captured] == [["EURUSD"], ["GBPUSD"]]
    assert dict(captured[0].get("metadata_patch") or {})["main_runtime_rollout"]["enabled"] is False
    assert "activation_package" in dict(captured[0].get("metadata_patch") or {})
    assert "activation_package" not in dict(captured[1].get("metadata_patch") or {})


def test_activate_release_alias_for_pairs_uses_anchor_release_payload_only_for_anchor_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = _live_canary_package(allowlisted_pairs=["EURUSD", "GBPUSD"])
    captured: list[dict[str, object]] = []

    def fake_activate_mlflow_alias(**kwargs):
        captured.append(dict(kwargs))
        return [{"pair": str(kwargs["pairs"][0]).upper()}]

    monkeypatch.setattr(release_workflow, "activate_mlflow_alias", fake_activate_mlflow_alias)

    activated = release_workflow._activate_release_alias_for_pairs(
        database_url="sqlite+pysqlite:///:memory:",
        manifest_path=tmp_path / "active_models.json",
        package=package,
        phase5_bundle={"canary_gate": {"passed": True}},
        pairs=["EURUSD", "GBPUSD"],
        alias="shadow",
    )

    assert [item["pair"] for item in activated] == ["EURUSD", "GBPUSD"]
    assert [call["pairs"] for call in captured] == [["EURUSD"], ["GBPUSD"]]

    anchor_patch = dict(captured[0].get("metadata_patch") or {})
    secondary_patch = dict(captured[1].get("metadata_patch") or {})

    assert "activation_package" in anchor_patch
    assert "canary_plan" in anchor_patch
    assert "canary_prep" in anchor_patch
    assert "phase5_gate_bundle" in anchor_patch

    assert "activation_package" not in secondary_patch
    assert "canary_plan" not in secondary_patch
    assert "canary_prep" not in secondary_patch
    assert "phase5_gate_bundle" not in secondary_patch
    assert secondary_patch["main_runtime_rollout"]["allowlisted_pairs"] == ["EURUSD", "GBPUSD"]
    assert secondary_patch["main_runtime_rollout"]["live_sleeve_allowlist"] == ["trend"]
    assert secondary_patch["main_runtime_rollout"]["live_intent_allowlist"] == ["entry"]


def test_close_canary_graduate_promotes_every_allowlisted_pair(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    package = _live_canary_package(allowlisted_pairs=["EURUSD", "GBPUSD"])
    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    promoted: list[tuple[str, str]] = []

    class DummyRuntimeService:
        def __init__(self, database_url: str):
            self.database_url = database_url

        def record_governance_event(self, *args, **kwargs) -> None:
            return None

    def fake_resolve_bundle_manifest_by_alias(*, pair: str, alias: str):
        return SimpleNamespace(pair=pair, alias=alias)

    def fake_set_bundle_alias(*, bundle, alias: str) -> None:
        promoted.append((str(bundle.pair).upper(), alias))

    monkeypatch.setattr(release_workflow, "load_release_package", lambda **kwargs: (package, release_dir))
    monkeypatch.setattr(release_workflow, "_read_json", lambda path: {})
    monkeypatch.setattr(release_workflow, "activate_mlflow_alias", lambda **kwargs: [])
    monkeypatch.setattr(release_workflow, "_persist_release_artifacts", lambda **kwargs: {"activation_package": str(release_dir / "activation_package.json")})
    monkeypatch.setattr(release_workflow, "RuntimeService", DummyRuntimeService)
    monkeypatch.setattr(release_workflow, "_patch_orchestration_live_runtime_state", lambda **kwargs: {})
    monkeypatch.setattr(release_workflow, "resolve_bundle_manifest_by_alias", fake_resolve_bundle_manifest_by_alias)
    monkeypatch.setattr(release_workflow, "set_bundle_alias", fake_set_bundle_alias)

    result = release_workflow.close_canary(
        pair="EURUSD",
        database_url="sqlite+pysqlite:///:memory:",
        manifest_path=tmp_path / "active_models.json",
        outcome="graduate",
    )

    assert result["release_status"] == "graduated"
    assert promoted == [("EURUSD", "champion"), ("GBPUSD", "champion")]


def test_monitor_canary_preserves_rolled_back_release_state_after_auto_rollback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    live_package = _live_canary_package()
    assert live_package.canary_plan is not None
    live_package.canary_plan.success_criteria = {
        "p95_overhead_ms": 1.0,
        "p99_overhead_ms": 1000.0,
        "ack_success_floor": 0.0,
        "orphan_command_limit": 99,
        "entry_ratio_floor": 0.0,
        "slot_utilisation_floor": 0.0,
        "drawdown_deterioration_pct": 1000.0,
        "alert_window_minutes": 1,
    }
    rolled_back_package = _live_canary_package()
    rolled_back_package.release_status = "rolled_back"
    assert rolled_back_package.canary_plan is not None
    rolled_back_package.canary_plan.status = "rolled_back"
    rolled_back_package.canary_plan.metadata = {
        **dict(rolled_back_package.canary_plan.metadata or {}),
        "runtime_enabled": False,
        "queue_kill_active": False,
        "rollback_reason": "overhead_p95_breach",
    }
    release_dir = tmp_path / "release"
    release_dir.mkdir(parents=True, exist_ok=True)
    load_sequence = [(live_package, release_dir), (rolled_back_package, release_dir)]
    persisted_statuses: list[str] = []

    class DummyRuntimeService:
        def __init__(self, database_url: str):
            self.database_url = database_url

        def get_state(self) -> dict[str, object]:
            return {
                "runtime_status": "running",
                "runtime_diag": {
                    "orchestration_live": {
                        "p95_ms": 5.0,
                        "p99_ms": 0.0,
                        "runtime_enabled": True,
                        "queue_kill_active": False,
                    }
                },
            }

        def get_metrics(self) -> dict[str, object]:
            return {}

        def get_commands(self, limit: int = 500) -> list[dict[str, object]]:
            return []

        def get_command_events(self, limit: int = 2000) -> list[dict[str, object]]:
            return []

        def record_governance_event(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(release_workflow, "get_settings", lambda: _settings_stub(phase5_auto_rollback=True))
    monkeypatch.setattr(release_workflow, "load_release_package", lambda **kwargs: load_sequence.pop(0))
    monkeypatch.setattr(release_workflow, "_read_json", lambda path: {})
    monkeypatch.setattr(release_workflow, "RuntimeService", DummyRuntimeService)
    monkeypatch.setattr(release_workflow, "_runtime_pair_readiness", lambda *args, **kwargs: {"ready": True, "reason": "ok"})
    monkeypatch.setattr(release_workflow, "_runtime_strategy_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(release_workflow, "_runtime_rl_state", lambda *args, **kwargs: {})
    monkeypatch.setattr(release_workflow, "_runtime_kill_orchestration_live", lambda **kwargs: {"runtime_enabled": False, "queue_kill_active": False, "queue_kill_reason": "", "queue_killed_at": 0.0})
    monkeypatch.setattr(release_workflow, "activate_mlflow_alias", lambda **kwargs: [])
    monkeypatch.setattr(release_workflow, "rollback_release", lambda **kwargs: {"release_status": "rolled_back"})
    monkeypatch.setattr(
        release_workflow,
        "_persist_release_artifacts",
        lambda **kwargs: (persisted_statuses.append(str(kwargs["package"].release_status)), {"activation_package": str(release_dir / "activation_package.json")})[1],
    )

    result = release_workflow.monitor_canary(
        pair="EURUSD",
        database_url="sqlite+pysqlite:///:memory:",
        manifest_path=tmp_path / "active_models.json",
    )

    assert result["release_status"] == "rolled_back"
    assert persisted_statuses == ["rolled_back"]
    assert not load_sequence


def test_risk_kernel_reduces_canary_entry_budget_for_allowlisted_pair() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.71,
            confidence=0.71,
            expected_edge_bps=8.0,
            metadata={"requested_lots": 0.40, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-04-07T12:00:00Z",
            spread_bps=1.1,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=15000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
            rollout_mode="canary",
            rollout_pair_allowlisted=True,
            rollout_budget_scale=0.25,
        ),
    )

    rollout = dict(decision.metadata.get("rollout") or {})
    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    assert abs(decision.approved_order.lots - 0.1) < 1e-9
    assert rollout["active"] is True
    assert rollout["reduced_budget"] is True
    assert rollout["breach"] is True
    assert rollout["breach_reason"] == "rollout_budget_reduced"
    assert any(item.rule == "rollout_canary" and item.verdict == "reduce" for item in decision.trace)


def test_risk_kernel_blocks_canary_pair_when_not_allowlisted() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="GBPUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.68,
            confidence=0.68,
            expected_edge_bps=6.0,
            metadata={"requested_lots": 0.12, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="GBPUSD",
            ts="2026-04-07T12:05:00Z",
            spread_bps=1.2,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=12000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
            rollout_mode="canary",
            rollout_pair_allowlisted=False,
            rollout_budget_scale=0.25,
        ),
    )

    rollout = dict(decision.metadata.get("rollout") or {})
    assert decision.verdict == "block"
    assert decision.reason == "rollout_pair_not_allowlisted"
    assert decision.approved_order is None
    assert rollout["active"] is False
    assert rollout["breach"] is True
    assert rollout["breach_reason"] == "rollout_pair_not_allowlisted"


def test_risk_cycle_summary_and_metrics_surface_rollout_state(tmp_path: Path) -> None:
    summary = _risk_cycle_summary(
        decisions=[
            {
                "symbol": "EURUSD",
                "execution_ready": True,
                "metadata": {
                    "pair": "EURUSD",
                    "lifecycle_action": "entry",
                    "risk_verdict": "allow",
                    "risk_reason": "approved",
                    "approved_order": {"cmd": "BUY"},
                    "risk_trace": [{"rule": "rollout_canary"}],
                    "rollout": {
                        "mode": "canary",
                        "active": True,
                        "pair_allowlisted": True,
                        "budget_scale": 0.25,
                        "reduced_budget": True,
                        "breach": True,
                        "breach_reason": "rollout_budget_reduced",
                    },
                },
            }
        ]
    )
    assert summary["rollout_active_count"] == 1
    assert summary["rollout_reduced_budget_count"] == 1
    assert summary["rollout_breach_count"] == 1
    assert summary["rollout"]["dominant_breach_reason"] == "rollout_budget_reduced"

    service = _fresh_service(tmp_path)
    service.patch_state(
        {
            "runtime_diag": {
                "rollout_policy": {
                    "configured_pairs": ["EURUSD"],
                    "active_pairs": ["EURUSD"],
                    "pair_budget_scale": {"EURUSD": 0.25},
                },
                "rollout_summary": dict(summary.get("rollout") or {}),
                "risk_cycle_summary": dict(summary),
            }
        }
    )
    metrics = service.get_metrics()
    assert metrics["rollout"]["active_pairs"] == ["EURUSD"]
    assert metrics["rollout"]["policy"]["pair_budget_scale"]["EURUSD"] == 0.25
    assert metrics["rollout"]["breach_count"] == 1
