from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION


def _fresh_client(tmp_path: Path) -> TestClient:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    if "fxstack.api.app" in sys.modules:
        del sys.modules["fxstack.api.app"]
    from fxstack.api.app import app

    return TestClient(app)


def _make_artifact(root: Path, name: str) -> str:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({"name": name}, indent=2), encoding="utf-8")
    return str(path)


def _write_registry(
    *,
    path: Path,
    pair: str,
    run_id: str,
    artifacts_root: Path,
    promotion_status: str,
    trained_at: float,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "pair": pair,
        "trained_at": trained_at,
        "promotion_status": promotion_status,
        "artifacts": {
            "regime": {"path": _make_artifact(artifacts_root, f"{run_id}_regime_hmm")},
            "meta": {"path": _make_artifact(artifacts_root, f"{run_id}_meta_filter")},
            "swing_xgb": {"path": _make_artifact(artifacts_root, f"{run_id}_swing_xgb")},
            "intraday_xgb": {"path": _make_artifact(artifacts_root, f"{run_id}_intraday_xgb")},
            "exit_policy": {"path": _make_artifact(artifacts_root, f"{run_id}_exit_policy")},
            "reversal_failure": {"path": _make_artifact(artifacts_root, f"{run_id}_reversal_failure")},
            "reversal_opportunity": {"path": _make_artifact(artifacts_root, f"{run_id}_reversal_opportunity")},
        },
        "policies": {
            "swing": "xgb_only",
            "intraday": "xgb_only",
        },
        "feature_schema": {
            "intraday_contract": "hierarchical_v1",
            "swing_policy": "xgb_only",
            "intraday_policy": "xgb_only",
            "tier": "tier2",
        },
        "training_eval_reports": {
            "meta": str(path.parent / f"{run_id}_meta_filter" / "reports" / "training_report.json"),
            "reversal_failure": str(path.parent / f"{run_id}_reversal_failure" / "reports" / "training_report.json"),
            "reversal_opportunity": str(path.parent / f"{run_id}_reversal_opportunity" / "reports" / "training_report.json"),
            "exit": str(path.parent / f"{run_id}_exit_policy" / "reports" / "training_report.json"),
        },
        "capabilities": {
            "has_exit_model": True,
            "has_reversal_models": True,
            "lifecycle_complete": True,
        },
        "phase3_execution_required": True,
        "phase3_evidence": {},
    }
    phase3_dir = path.parent / f"{run_id}_phase3"
    phase3_dir.mkdir(parents=True, exist_ok=True)
    dataset_hash = f"{run_id}-dataset"
    feature_service_name = f"fx_{pair.lower()}_execution_grade_m5"
    feature_service_version = "svc-v1"
    kernel_version = "phase3_risk_kernel_v1"
    manifest_payloads = {
        "internal_harness_manifest.json": {
            "engine": "internal",
            "status": "planned",
            "pair": pair,
            "manifest_version": "phase3_harness_manifest_v1",
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "engine_version": "3.12.0",
        },
        "nautilus_harness_manifest.json": {
            "engine": "nautilus",
            "status": "planned",
            "pair": pair,
            "manifest_version": "phase3_harness_manifest_v1",
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "engine_version": "1.0.0",
        },
        "lean_harness_manifest.json": {
            "engine": "lean",
            "status": "planned",
            "pair": pair,
            "manifest_version": "phase3_harness_manifest_v1",
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "engine_version": "2.0.0",
        },
    }
    payloads = {
        "execution_metrics.json": {
            "status": "planned",
            "engine": "internal",
            "pair": pair,
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
        },
        "market_replay_bundle.json": {
            "pair": pair,
            "timeframe": "M5",
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
        },
        "intent_replay_bundle.json": {
            "pair": pair,
            "intents_path": str(phase3_dir / "intent_replay_bundle.json"),
            "policy_version": "phase3_policy_v1",
            "kernel_version": kernel_version,
        },
        "golden_dataset_report.json": {
            "status": "ok",
            "market": {
                "pair": pair,
                "dataset_hash": dataset_hash,
                "feature_service_name": feature_service_name,
                "feature_service_version": feature_service_version,
            },
            "intents": {
                "pair": pair,
                "kernel_version": kernel_version,
            },
        },
        "stress_harness_summary.json": {
            "status": "planned",
            "base_engine": "internal",
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "scenario_count": 1,
            "scenarios": [{"name": "BaseCase"}],
        },
        "harness_comparison.json": {
            "status": "ok",
            "manifests": list(manifest_payloads.values()),
        },
        "risk_trace_schema.json": {
            "schema_version": "phase3_risk_trace_schema_v1",
            "kernel_version": kernel_version,
            "rule_order": ["data_freshness", "marketability"],
        },
        **manifest_payloads,
    }
    for name, report in payloads.items():
        report_path = phase3_dir / name
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        payload["phase3_evidence"][name.replace(".json", "")] = str(report_path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _seed_orchestration_evidence(
    *,
    service,
    experiment_id: str,
    promotion_id: str,
    source_run_id: str,
    now: float,
) -> dict[str, Any]:
    approval_records = [
        {
            "event_id": f"{experiment_id}-approval-exp",
            "subject_type": "experiment",
            "subject_id": experiment_id,
            "approver": "alice",
            "decision": "approved",
            "reason": "lineage_and_backtest_ok",
            "created_at": now - 20.0,
        },
        {
            "event_id": f"{promotion_id}-approval-promo",
            "subject_type": "promotion",
            "subject_id": promotion_id,
            "approver": "bob",
            "decision": "approved",
            "reason": "canary_evidence_ok",
            "created_at": now - 15.0,
        },
    ]
    experiment_row = {
        "experiment_id": experiment_id,
        "source_run_id": source_run_id,
        "hypothesis": "risk budgeted ranking with concentration guardrails",
        "change_set_json": {"scope": "portfolio", "change": "add_risk_budgeted_ranker"},
        "evaluation_plan_json": {"paper": True, "canary": True, "walk_forward": True},
        "risk_notes_json": {"max_drawdown": "2pct", "position_cap": "1.0"},
        "evidence_refs_json": {
            "experiment_lineage_ref": f"/tmp/{experiment_id}/lineage.json",
            "paper_results": f"/tmp/{promotion_id}/paper_results.json",
            "canary_results": f"/tmp/{promotion_id}/canary_results.json",
        },
        "prompt_hash": f"{experiment_id}-prompt",
        "tool_trace_hash": f"{experiment_id}-trace",
        "model_id": f"{experiment_id}-model",
        "decision_seed": 7,
        "input_artefact_refs_json": {
            "dataset": f"/tmp/{experiment_id}/dataset.parquet",
            "experiment_lineage_ref": f"/tmp/{experiment_id}/lineage.json",
        },
        "config_diff_json": {"slippage_bps": 1.25, "max_pair_share": 0.35},
        "replay_window": "2026-04-01/2026-04-07",
        "artifact_root": f"/tmp/{experiment_id}/artifacts",
        "latest_stage": "promoted",
        "latest_promotion_id": promotion_id,
        "approval_status": "approved",
        "created_at": now - 30.0,
    }
    promotion_row = {
        "promotion_id": promotion_id,
        "experiment_id": experiment_id,
        "prompt_hash": f"{experiment_id}-prompt",
        "tool_trace_hash": f"{experiment_id}-trace",
        "model_id": f"{experiment_id}-model",
        "config_diff_json": {"slippage_bps": 1.25, "max_pair_share": 0.35},
        "replay_window": "2026-04-01/2026-04-07",
        "replay_results_json": {"paper": {"sharpe": 1.45}, "canary": {"sharpe": 1.18}},
        "approval_records_json": approval_records,
        "paper_results_json": {"pnl": 12.5, "trades": 8},
        "canary_results_json": {"pnl": 10.8, "trades": 6},
        "release_manifest_ref": f"/tmp/{promotion_id}/release_manifest.json",
        "rollback_metadata_json": {"target_bundle_run_id": "baseline-bundle"},
        "artefact_hashes_json": {"model": f"{promotion_id}-model-hash"},
        "status": "eligible",
        "created_at": now - 10.0,
        "updated_at": now - 5.0,
    }
    with service.store.engine.begin() as conn:
        conn.execute(service.store.experiment_proposals.insert().values(**experiment_row))
        conn.execute(service.store.experiment_promotions.insert().values(**promotion_row))
        for record in approval_records:
            conn.execute(service.store.approval_events.insert().values(**record))
    return {
        "experiment": experiment_row,
        "promotion": promotion_row,
        "approvals": approval_records,
    }


def test_v2_health_state_commands_roundtrip(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    r = client.get("/v2/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"

    r = client.get("/v2/ready")
    assert r.status_code == 200
    ready = r.json()
    assert bool(ready.get("bridge_up")) is True
    assert "database_ok" in ready
    assert "runtime_ready" in ready
    assert "status_tier" in ready

    service.patch_state({"runtime_status": "running", "runtime_last_cycle_ts": time.time()})
    r = client.get("/v2/ready")
    assert r.status_code == 200
    ready = r.json()
    assert ready.get("runtime_status") == "running"
    assert isinstance(ready.get("runtime_ready"), bool)

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "runtime_startup": {
                "boot_id": "boot-123",
                "phase": "model_load",
                "phase_pair": "EURUSD",
                "phase_index": 2,
                "phase_total": 4,
                "last_progress_ts": time.time(),
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
            "runtime_diag": {
                "model_load_errors": 1,
                "model_load_timeouts": 2,
                "startup_inference_failures": 1,
                "startup_disabled_pairs": ["EURUSD"],
            },
        }
    )
    r = client.get("/v2/ready")
    assert r.status_code == 200
    ready = r.json()
    assert ready.get("runtime_startup_status") == "recovered_with_warnings"
    assert ready.get("runtime_startup_warning_count") == 4
    assert ready.get("model_load_errors") == 1
    assert ready.get("startup_inference_failures") == 1

    r = client.get("/v2/state")
    assert r.status_code == 200
    state = r.json()
    assert state.get("runtime_startup_summary", {}).get("status") == "recovered_with_warnings"
    assert state.get("runtimeStartupSummary", {}).get("startup_disabled_pairs") == ["EURUSD"]

    r = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "x1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"queued", "duplicate"}

    r = client.get("/v2/commands/poll")
    assert r.status_code == 200
    assert r.json().get("status") in {"ok", "empty"}

    r = client.post("/v2/commands/ack", json={"command_id": "x1", "status": "acked"})
    assert r.status_code in {200, 409}


def test_v2_commands_dedupes_retry_without_command_id(tmp_path: Path) -> None:
    client = _fresh_client(tmp_path)

    payload = {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}
    first = client.post("/v2/commands", json=payload)
    second = client.post("/v2/commands", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["status"] == "queued"
    assert second_body["status"] == "duplicate"
    assert first_body["command_id"] == second_body["command_id"]


def test_v2_state_surfaces_entry_execution_policy_counts_and_rejection_summaries(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "runtime_diag": {
                "entry_execution_policy": {
                    "execution_mode": "adaptive",
                    "adaptive_execution_enabled": True,
                    "pending_entry_count": 4,
                    "approved_entry_count": 2,
                    "blocked_entry_count": 1,
                    "submitted_entry_count": 3,
                    "duplicate_entry_count": 1,
                    "rejection_reason_counts": {"spread_too_wide": 2, "low_model_intelligence": 1},
                    "rejection_reason_summary": {"spread_too_wide": 2, "low_model_intelligence": 1},
                    "dominant_rejection_reason": "spread_too_wide",
                }
            },
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()

    for payload in (state, ready):
        policy = payload["entryExecutionPolicy"]
        assert policy["execution_mode"] == "adaptive"
        assert policy["adaptive_execution_enabled"] is True
        assert policy["pending_entry_count"] == 4
        assert policy["approved_entry_count"] == 2
        assert policy["blocked_entry_count"] == 1
        assert policy["submitted_entry_count"] == 3
        assert policy["duplicate_entry_count"] == 1
        assert policy["dominant_rejection_reason"] == "spread_too_wide"
        assert policy["rejection_reason_counts"]["spread_too_wide"] == 2
        assert policy["rejection_reason_summary"]["low_model_intelligence"] == 1


def test_v2_state_surfaces_trade_flow_inputs_and_canary_signals(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    now = time.time()
    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": now,
            "signals_sent": 7,
            "trades_executed": 5,
            "runtime_diag": {
                "entry_execution_policy": {
                    "execution_mode": "adaptive",
                    "approved_entry_count": 2,
                    "submitted_entry_count": 3,
                    "blocked_entry_count": 1,
                    "pending_entry_count": 0,
                },
                "shadow_policy": {"divergence_counts": {"liveOnly": 2, "shadowOnly": 1}},
                "adaptive_shadow_policy": {"divergence_counts": {"liveOnly": 1, "adaptiveOnly": 0}},
            },
            "capital_governance": {"canary_active": True},
            "orchestration_live": {
                "enabled": True,
                "current_stage_pct": 25,
                "runtime_enabled": True,
                "queue_kill_active": False,
                "ack_success_rate": 0.8,
                "ack_timeout_rate": 0.1,
                "last_command": {"status": "acked"},
            },
            "feature_online_ready": True,
            "feature_data_fresh": True,
            "feature_push_backlog": 0,
            "feature_blocker_reason": "",
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()

    assert state["signals_sent"] == 7
    assert state["trades_executed"] == 5
    assert state["runtime_diag"]["entry_execution_policy"]["approved_entry_count"] == 2
    assert state["runtime_diag"]["entry_execution_policy"]["submitted_entry_count"] == 3
    assert state["runtime_diag"]["shadow_policy"]["divergence_counts"]["liveOnly"] == 2
    assert state["runtime_diag"]["adaptive_shadow_policy"]["divergence_counts"]["liveOnly"] == 1
    assert ready["runtime_ready"] is True


def test_v2_state_preserves_active_decisions_when_runtime_cycle_is_fresh(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    now = time.time()
    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": now,
            "agent_decisions": [
                {"symbol": "EURUSD", "side": "BUY"},
                {"symbol": "GBPUSD", "side": "SELL"},
            ],
            "runtime_diag": {
                "risk_cycle_summary": {
                    "decision_count": 2,
                    "active_count": 2,
                    "rollout_active_count": 0,
                }
            },
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()

    assert state["runtime_signal_fresh"] is True
    assert state["agent_decisions_stale"] is False
    assert len(state["decisions"]) == 2
    assert len(state["latest_decisions"]) == 2
    assert state["runtime_diag"]["risk_cycle_summary"]["decision_count"] == 2
    assert ready["runtime_ready"] is True
    assert ready["runtime_status"] == "running"


def test_v2_state_retains_startup_failure_history_after_recovery(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.record_governance_event(
        event_type="runtime_startup_failed",
        reason="model_load_timeout",
        payload={
            "boot_id": "boot-old",
            "phase": "model_load",
            "phase_pair": "EURUSD",
            "failure_reason": "model_load_timeout",
            "failed_at": time.time() - 300.0,
        },
        ts=time.time() - 300.0,
    )
    service.patch_state(
        {
            "runtime_status": "starting",
            "runtime_last_cycle_ts": time.time(),
            "symbol_readiness": {"EURUSD": {"supported": True, "broker_symbol": "EURUSD"}},
            "runtime_startup": {
                "boot_id": "boot-old",
                "phase": "model_load",
                "phase_pair": "EURUSD",
                "phase_index": 2,
                "phase_total": 4,
                "last_progress_ts": time.time(),
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
            "runtime_diag": {
                "startup_inference": {"EURUSD": {"ok": True, "reason": "ok"}},
                "feature_serving_by_pair": {"EURUSD:M5": {"source": "feast_online", "stale": False, "reason": "ok"}},
                "model_load": {"pairs": {"EURUSD": {"failure_reason": ""}}},
                "strategy_engine_mode": "rl_primary",
                "supervised_fallback": {"enabled": True, "fallback_count": 2, "fallback_reasons": ["signal_fallback"], "primary_reason": "signal_fallback"},
                "challenger_conflict": {
                    "mode": "hard_gate",
                    "active": True,
                    "max_gap": 0.41,
                    "active_pairs": ["EURUSD"],
                    "verdict_counts": {"hard_conflict": 1},
                    "dominant_verdict": "hard_conflict",
                },
                "risk_cycle_summary": {
                    "decision_count": 1,
                    "active_count": 1,
                    "rollout_active_count": 0,
                },
                "entry_execution_policy": {
                    "execution_mode": "rl_primary",
                    "strategy_engine_mode": "rl_primary",
                    "rl_checkpoint_loaded": True,
                    "rl_checkpoint_path": "mlruns/eurusd/rl.chkpt",
                    "rl_proposal_source": "rl_checkpoint",
                    "rl_routed_entry_count": 3,
                    "rl_blocked_entry_count": 1,
                    "rl_fallback_entry_count": 1,
                    "rl_scaled_entry_count": 2,
                    "rl_lifecycle_reviewed_count": 5,
                    "rl_lifecycle_applied_count": 2,
                    "rl_lifecycle_exit_count": 1,
                    "rl_lifecycle_resize_count": 1,
                    "rl_lifecycle_tighten_stop_count": 1,
                    "rl_lifecycle_preserved_exit_count": 1,
                    "rl_lifecycle_fallback_count": 1,
                    "rl_lifecycle_pairs": ["EURUSD"],
                },
                "rl_portfolio_proposal": {
                    "ts": "2026-04-08T00:00:00Z",
                    "pair_universe": ["EURUSD"],
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "fallback_reason": "",
                    "checkpoint_path": "mlruns/eurusd/rl.chkpt",
                    "checkpoint_loaded": True,
                    "checkpoint_summary": {"feature_count": 8, "schema_version": "rl_linear_checkpoint_v1"},
                    "proposals_by_pair": {
                        "EURUSD": {
                            "source": "rl_checkpoint",
                            "supervised_fallback_used": False,
                            "action": {"target_position": 0.75, "close_position": False, "tighten_stop": False},
                        }
                    },
                    "diagnostics": {
                        "decision_count": 1,
                        "candidate_count": 1,
                        "checkpoint_summary": {"feature_count": 8, "schema_version": "rl_linear_checkpoint_v1"},
                        "artifact_discovery": {
                            "checkpoint_loaded": True,
                            "checkpoint_path": "mlruns/eurusd/rl.chkpt",
                            "fallback_reason": "",
                        },
                    },
                },
            },
            "agent_decisions": [{"symbol": "EURUSD", "side": "BUY", "metadata": {"pair": "EURUSD"}}],
        }
    )

    r = client.get("/v2/state")
    assert r.status_code == 200
    state = r.json()
    assert state.get("runtimeStartup", {}).get("recovered") is False
    assert state.get("lastRuntimeStartupFailure", {}).get("bootId") == "boot-old"
    assert state.get("runtimeStartupFailureHistory", [])[0]["bootId"] == "boot-old"
    assert state.get("pairReadiness", {}).get("EURUSD", {}).get("ready") is True
    assert state.get("startupInferenceByPair", {}).get("EURUSD", {}).get("ok") is True
    assert state.get("strategyEngineMode") == "rl_primary"
    assert state.get("supervisedFallback", {}).get("enabled") is True
    assert state.get("challengerConflict", {}).get("verdict_counts", {}).get("hard_conflict") == 1
    assert state.get("runtime_diag", {}).get("risk_cycle_summary", {}).get("decision_count") == 1
    assert state.get("rlCheckpointLoaded") is True
    assert state.get("rlCheckpointPath") == "mlruns/eurusd/rl.chkpt"
    assert state.get("rlProposalSource") == "rl_checkpoint"
    assert state.get("rlSupervisedFallbackUsed") is True
    assert state.get("rlFallbackReason") == ""
    assert state.get("rlRoutedEntryCount") == 3
    assert state.get("rlFallbackEntryCount") == 1
    assert state.get("rlExecutionPolicy", {}).get("proposal_count") == 1
    assert state.get("rlPortfolioProposal", {}).get("checkpoint_loaded") is True
    assert state.get("rlPortfolioProposal", {}).get("supervised_fallback_used") is True
    assert state.get("rlLifecycleSummary", {}).get("applied_count") == 2
    assert state.get("rlLifecycleSummary", {}).get("reviewed_count") == 5
    assert state.get("rlRebalanceSummary", {}).get("exit_count") == 1
    assert state.get("rlFlipIntent", {}).get("non_flat_target_count") == 1
    assert state.get("rlArtifactReadiness", {}).get("ready") is True

    ready = client.get("/v2/ready").json()
    assert ready.get("lastRuntimeStartupFailure", {}).get("bootId") == "boot-old"
    assert ready.get("runtimeStartupFailureHistory", [])[0]["bootId"] == "boot-old"
    assert ready.get("startupInferenceByPair", {}).get("EURUSD", {}).get("ok") is True
    assert ready.get("featureServingByPair", {}).get("EURUSD:M5", {}).get("source") == "feast_online"
    assert ready.get("strategyEngineMode") == "rl_primary"
    assert ready.get("supervisedFallback", {}).get("fallback_count") == 2
    assert ready.get("challengerConflict", {}).get("mode") == "hard_gate"
    assert ready.get("rlCheckpointLoaded") is True
    assert ready.get("rlCheckpointPath") == "mlruns/eurusd/rl.chkpt"
    assert ready.get("rlProposalSource") == "rl_checkpoint"
    assert ready.get("rlSupervisedFallbackUsed") is True
    assert ready.get("rlFallbackReason") == ""
    assert ready.get("rlRoutedEntryCount") == 3
    assert ready.get("rlExecutionPolicy", {}).get("proposal_count") == 1
    assert ready.get("rlLifecycleSummary", {}).get("reviewed_count") == 5
    assert ready.get("rlArtifactReadiness", {}).get("ready") is True

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "runtime_startup": {
                "boot_id": "boot-new",
                "phase": "main_loop",
                "phase_pair": "",
                "phase_index": 0,
                "phase_total": 0,
                "last_progress_ts": time.time(),
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    r = client.get("/v2/state")
    assert r.status_code == 200
    state = r.json()
    assert state.get("runtimeStartup", {}).get("recovered") is True
    assert state.get("lastRuntimeStartupFailure", {}).get("bootId") == "boot-old"
    assert state.get("runtimeStartupFailureHistory", [])[0]["bootId"] == "boot-old"
    assert state.get("strategyEngineMode") == "rl_primary"
    assert state.get("rlCheckpointLoaded") is True
    assert state.get("rlSupervisedFallbackUsed") is True
    assert state.get("rlFallbackReason") == ""
    assert state.get("rlPortfolioProposal", {}).get("supervised_fallback_used") is True

    r = client.get("/v2/state")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)

    r = client.get("/v2/metrics")
    assert r.status_code == 200
    assert "pending" in r.json()

    r = client.get("/v2/ops/events")
    assert r.status_code == 200
    assert "events" in r.json()

    r = client.get("/v2/ops/workflows/status")
    assert r.status_code == 200
    assert "workflows" in r.json()


def test_v2_ops_events_surface_latest_shadow_training_update(tmp_path: Path, monkeypatch):
    client = _fresh_client(tmp_path)
    from fxstack.api import app as app_module

    original_resolve_repo_path = app_module._resolve_repo_path

    def _resolve_repo_path(raw: str):
        if str(raw).replace("\\", "/") == "fx-quant-stack/artifacts_shadow":
            return (tmp_path / "fx-quant-stack" / "artifacts_shadow").resolve()
        return original_resolve_repo_path(raw)

    monkeypatch.setattr(app_module, "_resolve_repo_path", _resolve_repo_path)

    shadow_report = (
        tmp_path
        / "fx-quant-stack"
        / "artifacts_shadow"
        / "full_20260405_0100_manual"
        / "eurusd"
        / "reports"
        / "training_report.json"
    )
    shadow_report.parent.mkdir(parents=True, exist_ok=True)
    shadow_report.write_text("{}", encoding="utf-8")
    ts = time.time() - 5.0
    os.utime(shadow_report, (ts, ts))

    r = client.get("/v2/ops/events")
    assert r.status_code == 200
    body = r.json()
    shadow_event = next((item for item in body.get("events", []) if item.get("event_type") == "training_shadow_update"), None)
    assert shadow_event is not None
    assert shadow_event["status"] == "running"
    assert shadow_event["payload"]["pair"] == "EURUSD"
    assert shadow_event["payload"]["run_name"] == "full_20260405_0100_manual"


def test_v2_ops_workflows_status_prefers_newest_shadow_registry(tmp_path: Path, monkeypatch):
    client = _fresh_client(tmp_path)
    from fxstack.api import app as app_module
    from fxstack.api.app import service

    original_resolve_repo_path = app_module._resolve_repo_path

    def _resolve_repo_path(raw: str):
        if str(raw).replace("\\", "/") == "fx-quant-stack/artifacts_shadow":
            return (tmp_path / "fx-quant-stack" / "artifacts_shadow").resolve()
        return original_resolve_repo_path(raw)

    monkeypatch.setattr(app_module, "_resolve_repo_path", _resolve_repo_path)
    app_module._workflow_status_cache = None

    pair = "AUDJPY"
    old_registry = _write_registry(
        path=tmp_path / "legacy_registry" / "audjpy_legacy.json",
        pair=pair,
        run_id="legacy-run",
        artifacts_root=tmp_path / "legacy_artifacts",
        promotion_status="research_only",
        trained_at=1775330000.0,
    )
    old_ts = time.time() - 3600.0
    os.utime(old_registry, (old_ts, old_ts))

    active_artifacts = {
        "regime": str(tmp_path / "legacy_artifacts" / "legacy-run_regime_hmm"),
        "meta": str(tmp_path / "legacy_artifacts" / "legacy-run_meta_filter"),
        "swing_xgb": str(tmp_path / "legacy_artifacts" / "legacy-run_swing_xgb"),
        "intraday_xgb": str(tmp_path / "legacy_artifacts" / "legacy-run_intraday_xgb"),
        "exit_policy": str(tmp_path / "legacy_artifacts" / "legacy-run_exit_policy"),
        "reversal_failure": str(tmp_path / "legacy_artifacts" / "legacy-run_reversal_failure"),
        "reversal_opportunity": str(tmp_path / "legacy_artifacts" / "legacy-run_reversal_opportunity"),
    }
    service.upsert_active_model_set(
        pair=pair,
        model_set_id="legacy-run",
        registry_path=str(old_registry),
        artifacts=active_artifacts,
        metadata={"promotion_status": "research_only", "capabilities": {"has_exit_model": True, "has_reversal_models": True, "lifecycle_complete": True}},
        enabled=True,
    )

    shadow_registry = _write_registry(
        path=tmp_path
        / "fx-quant-stack"
        / "artifacts_shadow"
        / "registry_full_20260405_1200_manual"
        / "audjpy_shadow.json",
        pair=pair,
        run_id="shadow-run",
        artifacts_root=tmp_path / "fx-quant-stack" / "artifacts_shadow" / "full_20260405_1200_manual" / "audjpy",
        promotion_status="eligible",
        trained_at=1775440000.0,
    )
    new_ts = time.time()
    os.utime(shadow_registry, (new_ts, new_ts))

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "audjpy-training-eval")
    assert workflow["status"] == "eligible"
    assert workflow["details_json"]["registry_meta"]["run_id"] == "shadow-run"
    assert str(workflow["details_json"]["lifecycle_capabilities"]["registry_path"]).endswith("audjpy_shadow.json")
    assert workflow["details_json"]["registry_meta"]["promotion_status"] == "eligible"


def test_v2_ops_workflows_status_includes_shadow_only_pairs(tmp_path: Path, monkeypatch):
    client = _fresh_client(tmp_path)
    from fxstack.api import app as app_module

    original_resolve_repo_path = app_module._resolve_repo_path

    def _resolve_repo_path(raw: str):
        if str(raw).replace("\\", "/") == "fx-quant-stack/artifacts_shadow":
            return (tmp_path / "fx-quant-stack" / "artifacts_shadow").resolve()
        return original_resolve_repo_path(raw)

    monkeypatch.setattr(app_module, "_resolve_repo_path", _resolve_repo_path)
    app_module._workflow_status_cache = None

    _write_registry(
        path=tmp_path
        / "fx-quant-stack"
        / "artifacts_shadow"
        / "registry_full_20260405_1200_manual"
        / "gbpusd_shadow.json",
        pair="GBPUSD",
        run_id="shadow-only-run",
        artifacts_root=tmp_path / "fx-quant-stack" / "artifacts_shadow" / "full_20260405_1200_manual" / "gbpusd",
        promotion_status="eligible",
        trained_at=1775443600.0,
    )

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "gbpusd-training-eval")
    assert workflow["status"] == "eligible"
    assert workflow["details_json"]["registry_meta"]["run_id"] == "shadow-only-run"
    assert workflow["details_json"]["lifecycle_capabilities"]["has_exit_model"] is True
    assert workflow["details_json"]["registry_source"] == "shadow"


def test_v2_ops_workflows_status_surfaces_phase3_evidence(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service
    from fxstack.api import app as app_module

    app_module._workflow_status_cache = None
    registry = _write_registry(
        path=tmp_path / "registry" / "eurusd_phase3.json",
        pair="EURUSD",
        run_id="phase3-run",
        artifacts_root=tmp_path / "artifacts",
        promotion_status="eligible",
        trained_at=1775443600.0,
    )
    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    service.upsert_active_model_set(
        pair="EURUSD",
        model_set_id="phase3-run",
        registry_path=str(registry),
        artifacts={"regime": str(tmp_path / "artifacts" / "phase3-run_regime_hmm")},
        metadata={
            "promotion_status": "eligible",
            "capabilities": {"has_exit_model": True, "has_reversal_models": True, "lifecycle_complete": True},
            "phase3_execution_required": True,
            "phase3_evidence": dict(registry_payload.get("phase3_evidence") or {}),
        },
        enabled=True,
    )

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "eurusd-training-eval")
    details = dict(workflow["details_json"] or {})
    assert bool(details["phase3_execution_required"]) is True
    evidence = dict(details["phase3_evidence"] or {})
    assert "execution_metrics" in evidence
    assert "risk_trace_schema" in evidence


def test_v2_telemetry_aliases_remain_backwards_compatible(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "runtime_diag": {
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok"},
                },
                "provider_roles": {
                    "history_provider": "dukascopy",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
                "portfolio_intelligence": {"gross_exposure": 1.25},
                "capital_governance": {
                    "capital_band": "micro_live",
                    "mode": "entries_only",
                    "entries_only": True,
                    "shadow_only": False,
                },
            },
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()
    metrics = client.get("/v2/metrics").json()
    health = client.get("/v2/health").json()

    for payload in (state, ready, metrics, health):
        assert payload["provider_health"]["history_provider"]["status"] == "ok"
        assert payload["providerHealth"]["market_data_provider"]["status"] == "ok"
        assert payload["provider_roles"]["execution_provider"] == "mt4"
        assert payload["providerRoles"]["history_provider"] == "dukascopy"
        assert payload["portfolio_intelligence"]["gross_exposure"] == 1.25
        assert payload["portfolioTelemetry"]["gross_exposure"] == 1.25
        assert payload["capital_governance"]["capital_band"] == "micro_live"
        assert payload["capitalGovernance"]["mode"] == "entries_only"
        assert payload["capitalBand"] == "micro_live"
        assert payload["governanceMode"] == "entries_only"
        assert payload["entriesOnlyMode"] is True
        assert payload["shadowOnlyMode"] is False

def test_v2_ops_workflows_status_surfaces_phase4_shadow_metadata(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service
    from fxstack.api import app as app_module

    app_module._workflow_status_cache = None
    registry = _write_registry(
        path=tmp_path / "registry" / "eurusd_phase4.json",
        pair="EURUSD",
        run_id="phase4-run",
        artifacts_root=tmp_path / "artifacts",
        promotion_status="eligible",
        trained_at=1775443600.0,
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["artifacts"]["swing_patchtst"] = {"path": _make_artifact(tmp_path / "artifacts", "phase4-run_swing_patchtst")}
    payload["artifacts"]["intraday_patchtst"] = {"path": _make_artifact(tmp_path / "artifacts", "phase4-run_intraday_patchtst")}
    payload["phase4_shadow_only"] = True
    payload["phase4_sequence_dataset_manifests"] = {"swing_patchtst": "seq-swing.json", "intraday_patchtst": "seq-intraday.json"}
    payload["phase4_portfolio_reports"] = {"swing_patchtst": "portfolio-swing.json", "intraday_patchtst": "portfolio-intraday.json"}
    payload["phase4_challenger_reports"] = {"swing_patchtst": "head-swing.json", "intraday_patchtst": "head-intraday.json"}
    registry.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    service.upsert_active_model_set(
        pair="EURUSD",
        model_set_id="phase4-run",
        registry_path=str(registry),
        artifacts={"regime": str(tmp_path / "artifacts" / "phase4-run_regime_hmm")},
        metadata={
            "promotion_status": "eligible",
            "capabilities": {"has_exit_model": True, "has_reversal_models": True, "lifecycle_complete": True},
            "phase4_shadow_only": True,
            "phase4_sequence_dataset_manifests": dict(payload.get("phase4_sequence_dataset_manifests") or {}),
            "phase4_portfolio_reports": dict(payload.get("phase4_portfolio_reports") or {}),
            "phase4_challenger_reports": dict(payload.get("phase4_challenger_reports") or {}),
        },
        enabled=True,
    )

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "eurusd-training-eval")
    details = dict(workflow["details_json"] or {})
    assert bool(details["phase4_shadow_only"]) is True
    assert details["phase4_sequence_dataset_manifests"]["swing_patchtst"] == "seq-swing.json"
    assert details["phase4_portfolio_reports"]["intraday_patchtst"] == "portfolio-intraday.json"
    assert "swing_patchtst" in details["challenger_components"]


def test_v2_ready_surfaces_runtime_startup_progress_and_failure_states(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service
    from fxstack.settings import get_settings

    now = time.time()
    stale_secs = float(get_settings().runtime_startup_progress_stale_secs)
    service.patch_state(
        {
            "runtime_status": "starting",
            "runtime_last_cycle_ts": 0.0,
            "runtime_startup": {
                "boot_id": "boot-1",
                "booted_at": "2026-03-24T07:00:00+00:00",
                "runtime_pid": 123,
                "phase": "model_load",
                "phase_pair": "",
                "phase_index": 0,
                "phase_total": 18,
                "last_progress_ts": now,
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "starting"
    assert ready["runtime_phase"] == "model_load"
    assert ready["runtime_boot_id"] == "boot-1"
    assert ready["reason"] == "runtime_starting"

    service.patch_state(
        {
            "runtime_status": "starting",
            "runtime_last_cycle_ts": 0.0,
            "agent_decisions": [{"symbol": "EURUSD"}],
            "agent_diagnostics": {"foo": "bar"},
            "runtime_startup": {
                "boot_id": "boot-2",
                "booted_at": "2026-03-24T07:05:00+00:00",
                "runtime_pid": 456,
                "phase": "initial_refresh",
                "phase_pair": "GBPJPY",
                "phase_index": 10,
                "phase_total": 18,
                "last_progress_ts": now - (stale_secs + 15.0),
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    state = client.get("/v2/state").json()
    assert state["runtime_status"] == "stalled"
    assert state["runtime_phase"] == "initial_refresh"
    assert state["runtime_phase_pair"] == "GBPJPY"
    assert state["agent_decisions_stale"] is True
    assert len(state["agent_decisions"]) == 1
    assert len(state["decisions"]) == 1
    assert len(state["latest_decisions"]) == 1
    assert state["agent_diagnostics"] == {"foo": "bar"}

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "stalled"
    assert ready["status_tier"] == "bridge_up_runtime_stalled"
    assert ready["reason"] == "runtime_startup_stalled"
    assert ready["runtime_phase"] == "initial_refresh"
    assert ready["runtime_phase_pair"] == "GBPJPY"
    assert float(ready["runtime_last_progress_age_secs"]) >= stale_secs
    assert ready["feature_online_ready"] is False
    assert ready["feature_blocker_reason"] in {"feature_serving:worker_absent", "feature_serving:missing_source"}

    service.patch_state(
        {
            "runtime_status": "failed",
            "runtime_last_cycle_ts": 0.0,
            "runtime_startup": {
                "boot_id": "boot-3",
                "booted_at": "2026-03-24T07:10:00+00:00",
                "runtime_pid": 789,
                "phase": "model_load",
                "phase_pair": "EURUSD",
                "phase_index": 1,
                "phase_total": 18,
                "last_progress_ts": now,
                "failure_component": "exit_policy",
                "failure_pair": "EURUSD",
                "failure_reason": "RuntimeError:boom",
                "failed_at": "2026-03-24T07:10:05+00:00",
                "pending_command_policy": "purge_and_mark_stale",
            },
            "runtime_diag": {
                "model_load": {
                    "model_load_timeouts": 1,
                    "model_load_errors": 2,
                    "failure_component": "exit_policy",
                    "failure_pair": "EURUSD",
                    "failure_reason": "load_error:TimeoutError",
                    "failed_pairs": ["EURUSD"],
                    "degraded_pairs": ["GBPJPY"],
                    "pairs": {
                        "EURUSD": {
                            "status": "failed",
                            "failure_component": "exit_policy",
                            "failure_reason": "load_error:TimeoutError",
                        }
                    },
                }
            },
        }
    )

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "failed"
    assert ready["status_tier"] == "bridge_up_runtime_failed"
    assert ready["reason"] == "runtime_startup_failed"
    assert ready["runtime_failure_component"] == "exit_policy"
    assert ready["runtime_failure_pair"] == "EURUSD"
    assert ready["runtime_failure_reason"] == "RuntimeError:boom"
    assert ready["runtime_startup_summary"]["failure_component"] == "exit_policy"
    assert ready["runtime_startup_summary"]["failure_pair"] == "EURUSD"
    assert ready["runtime_model_load"]["failure_component"] == "exit_policy"
    assert ready["runtime_model_load_failures"] == 1
    assert ready["runtime_model_load_failed_pairs"] == ["EURUSD"]

    service.record_runtime_boot_failure(
        boot={
            "boot_id": "boot-3",
            "booted_at": "2026-03-24T07:10:00+00:00",
            "runtime_pid": 789,
            "phase": "model_load",
            "phase_pair": "EURUSD",
            "phase_index": 1,
            "phase_total": 18,
            "last_progress_ts": now,
            "failure_reason": "",
            "failed_at": "",
            "pending_command_policy": "purge_and_mark_stale",
        },
        failure_reason="RuntimeError:boom",
        failed_at="2026-03-24T07:10:05+00:00",
    )
    governance = client.get("/v2/governance/events").json()
    assert len(governance["events"]) >= 1
    assert governance["events"][0]["event_type"] == "runtime_startup_failed"


def test_v2_ready_reports_recovered_startup_even_with_stale_failed_at(tmp_path: Path):
    _fresh_client(tmp_path)
    from fxstack.api.app import _runtime_startup_summary

    ready = _runtime_startup_summary(
        {
            "runtime_status": "running",
            "runtime_startup": {
                "boot_id": "boot-recovered",
                "booted_at": "2026-03-24T07:20:00+00:00",
                "runtime_pid": 321,
                "phase": "main_loop",
                "phase_pair": "",
                "phase_index": 18,
                "phase_total": 18,
                "last_progress_ts": time.time(),
                "failure_reason": "",
                "failed_at": "2026-03-24T06:00:00+00:00",
                "pending_command_policy": "purge_and_mark_stale",
            },
            "runtime_diag": {},
        }
    )

    assert ready["status"] == "ready"
    assert ready["recovered"] is True
    assert ready["failed_at"] == "2026-03-24T06:00:00+00:00"
    assert ready["failure_reason"] == ""


def test_v2_ready_surfaces_canary_readiness_from_runtime_diag(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "runtime_diag": {
                "rollout_policy": {
                    "active_count": 1,
                    "active_pairs": ["EURUSD"],
                },
                "risk_cycle_summary": {
                    "rollout_active_count": 1,
                    "rollout_breach_count": 2,
                    "rollout": {
                        "active_pairs": ["EURUSD", "GBPUSD"],
                        "breach_count": 2,
                    },
                },
            },
        }
    )

    ready = client.get("/v2/ready").json()
    assert ready["canary_active"] is True
    assert ready["canary_pairs"] == ["EURUSD", "GBPUSD"]
    assert ready["canary_breach_count"] == 2
    assert ready["status_tier"] in {"bridge_up_runtime_ready_mt4_stale", "bridge_up_runtime_ready_mt4_live", "bridge_up_runtime_starting"}


def test_v2_decision_snapshots_exposes_persisted_history(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.store_decisions(
        decisions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "score": 4.2,
                "confidence": 77.0,
                "execution_ready": False,
                "reasons": ["shadow_meta_reject"],
                "metadata": {
                    "structure_timing_score": 0.81,
                    "structure_rescue_active": False,
                    "shadow_rejection_reason": "shadow_meta_reject",
                    "orchestration_shadow": {
                        "enabled": True,
                        "baseline_action": {"action": "no_trade", "side": "FLAT"},
                        "shadow_action": {"action": "enter", "side": "BUY"},
                        "divergence_reason": "shadow_only_enter",
                        "blocking_reasons": [],
                        "proposal_votes": {"total": 2, "by_intent": {"enter": 2}, "by_side": {"BUY": 2}, "by_agent": {"signal": "enter", "risk": "enter"}},
                        "run_id": "run-1",
                        "trace_id": "trace-1",
                        "fault_classification": "",
                        "latency_ms": 19,
                        "committee": {
                            "winning_agent": "committee.trend_pullback",
                            "winning_proposal_id": "proposal-1",
                            "winning_score": 3.2,
                            "arbiter_stage": "entry_ranking",
                            "rationale": "trend pullback aligned on playbook, location, and trigger",
                            "blocking_reasons": [],
                            "top_ranked_proposals": [
                                {"proposal_id": "proposal-1", "agent_id": "committee.trend_pullback", "intent": "enter", "score": 3.2}
                            ],
                        },
                    },
                    "adaptive_environment_state": "CorrectiveTrend",
                    "adaptive_playbook": "trend_pullback",
                    "adaptive_sleeve": "trend_pullback",
                    "adaptive_entry_quality": 0.67,
                    "adaptive_shadow_would_trade": True,
                    "allocator_score": 0.71,
                    "allocator_rank": 1,
                    "allocator_selected": True,
                },
            }
        ],
        vol=0.12,
        diagnostics={
            "runtime": "fxstack",
            "orchestration": {
                "enabled": False,
                "agent_mode": "off",
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "correlation_id": "",
                "thread_id": "",
                "fallback_used": False,
                "run_id": "",
                "trace_id": "",
            },
            "shadow_policy": {"candidate_count": 1},
            "adaptive_shadow_policy": {"candidate_count": 1, "would_trade_count": 1},
            "allocator_policy": {"candidate_count": 1, "selected_count": 1},
            "orchestration_shadow": {
                "enabled": False,
                "pair_count": 0,
                "packet_count": 0,
                "trace_count": 0,
                "fault_count": 0,
                "phase2": {
                    "adaptive_shadow_policy": {"candidate_count": 1},
                    "entry_execution_policy": {"submitted_live_entry_count": 1},
                },
            },
        },
    )

    body = client.get("/v2/decision-snapshots?limit=5").json()
    assert "items" in body
    assert len(body["items"]) >= 1
    latest = body["items"][0]
    assert latest["vol"] == 0.12
    assert latest["decisions_json"][0]["symbol"] == "EURUSD"
    assert latest["decisions_json"][0]["metadata"]["structure_timing_score"] == 0.81
    assert latest["decisions_json"][0]["metadata"]["orchestration_shadow"]["shadow_action"]["action"] == "enter"
    assert latest["decisions_json"][0]["metadata"]["orchestration_shadow"]["committee"]["winning_agent"] == "committee.trend_pullback"
    assert latest["decisions_json"][0]["metadata"]["adaptive_playbook"] == "trend_pullback"
    assert latest["diagnostics_json"]["orchestration"]["agent_mode"] == "off"
    assert latest["diagnostics_json"]["orchestration"]["schema_version"] == ORCHESTRATION_SCHEMA_VERSION
    assert latest["diagnostics_json"]["orchestration_shadow"]["pair_count"] == 0
    assert latest["diagnostics_json"]["orchestration_shadow"]["phase2"]["adaptive_shadow_policy"]["candidate_count"] == 1
    assert latest["diagnostics_json"]["adaptive_shadow_policy"]["candidate_count"] == 1
    assert latest["diagnostics_json"]["allocator_policy"]["candidate_count"] == 1


def test_v2_decision_snapshots_preserves_additive_fields(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.store_decisions(
        decisions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "score": 5.4,
                "confidence": 88.0,
                "execution_ready": True,
                "reasons": [],
                "future_decision_field": "kept",
                "metadata": {
                    "pair": "EURUSD",
                    "feature_push_backlog": 2,
                    "feature_bar_status": "fresh",
                    "future_metadata_field": "kept",
                },
            }
        ],
        vol=0.34,
        diagnostics={
            "runtime": "fxstack",
            "orchestration": {
                "enabled": False,
                "agent_mode": "off",
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            },
            "feature_observability": {
                "feature_push_backlog": 2,
                "feature_bar_status": "fresh",
            },
            "future_diagnostics_field": {"kept": True},
        },
    )

    body = client.get("/v2/decision-snapshots?limit=5").json()
    latest = body["items"][0]
    assert latest["vol"] == 0.34
    assert latest["decisions_json"][0]["future_decision_field"] == "kept"
    assert latest["decisions_json"][0]["metadata"]["future_metadata_field"] == "kept"
    assert latest["diagnostics_json"]["feature_observability"]["feature_push_backlog"] == 2
    assert latest["diagnostics_json"]["feature_observability"]["feature_bar_status"] == "fresh"
    assert latest["diagnostics_json"]["future_diagnostics_field"]["kept"] is True


def test_v2_orchestration_endpoints_expose_runs_and_traces(tmp_path: Path) -> None:
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    run_id = "00000000-0000-0000-0000-000000000010"
    trace_id = "trace-10"
    context = {
        "run_id": run_id,
        "cycle_id": "123",
        "thread_id": "EURUSD:123:shadow",
        "correlation_id": "EURUSD:123:shadow",
        "ts_utc": "2026-04-08T12:00:00+00:00",
        "pair": "EURUSD",
        "runtime_mode": "shadow",
        "version_bundle": {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "policy_version": "fxstack_policy_v1",
            "model_bundle_version": "bundle-v1",
            "orchestrator_version": ORCHESTRATION_SCHEMA_VERSION,
        },
    }
    packet = {
        "packet_id": "00000000-0000-0000-0000-000000000011",
        "run_id": run_id,
        "pair": "EURUSD",
        "ts_utc": "2026-04-08T12:00:00+00:00",
        "baseline_action": {"action": "no_trade"},
        "shadow_action": {"action": "no_trade", "side": "FLAT"},
        "divergence_reason": "agree",
        "proposal_votes": {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}},
        "fault_classification": None,
        "proposals": [],
        "governed_decision": {
            "decision_id": "00000000-0000-0000-0000-000000000012",
            "run_id": run_id,
            "allowed": False,
            "selected_action": "no_trade",
            "command_preview": None,
            "blocking_reasons": ["shadow_only"],
            "approval_state": "auto",
            "governor_version": ORCHESTRATION_SCHEMA_VERSION,
            "invariants_ok": True,
        },
        "latency_ms": 5,
        "fallback_used": False,
        "trace_id": trace_id,
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
    }
    trace = {
        "trace_id": trace_id,
        "run_id": run_id,
        "node_spans": [{"node": "noop", "latency_ms": 5}],
        "tool_calls": [],
        "model_calls": [],
        "persistence_refs": [f"run://{run_id}"],
        "prompt_hashes": [],
        "input_hash": "sha256:in",
        "output_hash": "sha256:out",
        "error_class": None,
        "created_at": "2026-04-08T12:00:00+00:00",
        "checkpoint": {"thread_id": "EURUSD:123:shadow", "checkpoint": {}},
    }
    service.store_orchestration_bundle(
        context=context,
        packet=packet,
        trace=trace,
        runtime_mode="shadow",
        fallback_used=False,
    )

    runs_body = client.get("/v2/orchestration/runs?pair=EURUSD&runtime_mode=shadow&cycle_id=123").json()
    traces_body = client.get(f"/v2/orchestration/traces?run_id={run_id}&pair=EURUSD").json()
    assert runs_body["items"][0]["run_id"] == run_id
    assert runs_body["items"][0]["runtime_mode"] == "shadow"
    assert traces_body["items"][0]["trace_id"] == trace_id


def test_v2_orchestration_experiments_and_promotions_surface_evidence_and_state_summary(tmp_path: Path) -> None:
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    now = time.time()
    _seed_orchestration_evidence(
        service=service,
        experiment_id="exp-phase7-001",
        promotion_id="promo-phase7-001",
        source_run_id="run-phase7-001",
        now=now,
    )

    experiments = client.get("/v2/orchestration/experiments?limit=25").json()
    assert experiments["summary"]["experiment_count"] == 1
    assert experiments["summary"]["promotion_count"] == 1
    assert experiments["summary"]["approval_event_count"] == 2
    assert experiments["summary"]["latest_experiment_id"] == "exp-phase7-001"
    assert experiments["summary"]["latest_promotion_id"] == "promo-phase7-001"
    assert experiments["summary"]["latest_approval_decision"] == "approved"
    assert experiments["summary"]["latest_lineage"]["experiment_lineage_ref"] == "/tmp/exp-phase7-001/lineage.json"
    assert experiments["summary"]["approval_status_counts"]["approved"] == 1
    assert experiments["summary"]["promotion_status_counts"]["eligible"] == 1
    assert experiments["summary"]["approval_decision_counts"]["approved"] == 2
    experiment_item = experiments["items"][0]
    assert experiment_item["experiment_id"] == "exp-phase7-001"
    assert experiment_item["lineage"]["approval_count"] == 1
    assert experiment_item["lineage"]["promotion_count"] == 1
    assert experiment_item["lineage"]["latest_promotion_status"] == "eligible"

    experiment_detail = client.get("/v2/orchestration/experiments/exp-phase7-001").json()
    assert experiment_detail["summary"]["experiment_count"] == 1
    assert experiment_detail["summary"]["promotion_count"] == 1
    assert experiment_detail["summary"]["approval_event_count"] == 1
    assert experiment_detail["experiment"]["experiment_id"] == "exp-phase7-001"
    assert experiment_detail["experiment"]["approvals"][0]["event_id"] == "exp-phase7-001-approval-exp"
    assert experiment_detail["experiment"]["promotions"][0]["promotion_id"] == "promo-phase7-001"
    assert experiment_detail["experiment"]["latest_promotion"]["status"] == "eligible"
    assert experiment_detail["experiment"]["lineage"]["experiment_lineage_ref"] == "/tmp/exp-phase7-001/lineage.json"
    assert experiment_detail["experiment"]["lineage"]["approval_count"] == 1
    assert experiment_detail["experiment"]["lineage"]["promotion_count"] == 1
    assert experiment_detail["experiment"]["summary"]["latest_promotion_status"] == "eligible"

    promotions = client.get("/v2/orchestration/promotions?limit=25").json()
    assert promotions["summary"]["promotion_count"] == 1
    assert promotions["summary"]["approval_event_count"] == 2
    assert promotions["summary"]["latest_promotion_id"] == "promo-phase7-001"
    assert promotions["summary"]["latest_promotion_status"] == "eligible"
    assert promotions["summary"]["promotion_status_counts"]["eligible"] == 1
    assert promotions["summary"]["approval_decision_counts"]["approved"] == 2
    promotion_item = promotions["items"][0]
    assert promotion_item["promotion_id"] == "promo-phase7-001"
    assert promotion_item["approval_records"][0]["event_id"] == "exp-phase7-001-approval-exp"
    assert promotion_item["paper_results"]["pnl"] == 12.5
    assert promotion_item["canary_results"]["trades"] == 6
    assert promotion_item["lineage"]["approval_record_count"] == 2
    assert promotion_item["lineage"]["paper_result_keys"] == ["pnl", "trades"]
    assert promotion_item["lineage"]["canary_result_keys"] == ["pnl", "trades"]

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()
    for payload in (state, ready):
        evidence = payload["orchestrationEvidence"]
        assert evidence["experiment_count"] == 1
        assert evidence["promotion_count"] == 1
        assert evidence["approval_event_count"] == 2
        assert evidence["latest_experiment_id"] == "exp-phase7-001"
        assert evidence["latest_promotion_id"] == "promo-phase7-001"
        assert evidence["latest_approval_event_id"] == "promo-phase7-001-approval-promo"
        assert evidence["latest_promotion_status"] == "eligible"
        assert evidence["latest_lineage"]["experiment_lineage_ref"] == "/tmp/exp-phase7-001/lineage.json"
        assert payload["orchestration_evidence"]["latest_approval_decision"] == "approved"


def test_v2_ops_workflows_status_surfaces_lineage_and_promotion_summary(tmp_path: Path) -> None:
    client = _fresh_client(tmp_path)
    from fxstack.api import app as app_module
    from fxstack.api.app import service

    app_module._workflow_status_cache = None
    registry = _write_registry(
        path=tmp_path / "registry" / "eurusd_phase7.json",
        pair="EURUSD",
        run_id="phase7-run",
        artifacts_root=tmp_path / "artifacts",
        promotion_status="eligible",
        trained_at=1775443600.0,
    )
    service.upsert_active_model_set(
        pair="EURUSD",
        model_set_id="phase7-run",
        registry_path=str(registry),
        artifacts={"regime": str(tmp_path / "artifacts" / "phase7-run_regime_hmm")},
        metadata={
            "promotion_status": "eligible",
            "promotion_summary": {
                "status": "eligible",
                "experiment_id": "exp-phase7-001",
                "promotion_id": "promo-phase7-001",
                "approval_status": "approved",
            },
            "lineage": {
                "experiment_id": "exp-phase7-001",
                "experiment_lineage_ref": "/tmp/exp-phase7-001/lineage.json",
                "dataset_fingerprint": "fp-phase7-001",
                "feature_service_version": "svc-phase7-001",
                "label_version": "lbl-phase7-001",
                "risk_config_version": "risk-phase7-001",
                "git_sha": "abc123",
                "git_dirty": False,
            },
            "capabilities": {"has_exit_model": True, "has_reversal_models": True, "lifecycle_complete": True},
        },
        enabled=True,
    )

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "eurusd-training-eval")
    details = dict(workflow["details_json"] or {})
    assert details["promotion_summary"]["status"] == "eligible"
    assert details["promotion_summary"]["report_count"] >= 1
    assert details["promotion_summary"]["experiment_id"] == "exp-phase7-001"
    assert details["lineage"]["experiment_id"] == "exp-phase7-001"
    assert details["lineage"]["experiment_lineage_ref"] == "/tmp/exp-phase7-001/lineage.json"
    assert details["lineage"]["dataset_fingerprint"] == "fp-phase7-001"
    assert details["lineage"]["feature_service_version"] == "svc-phase7-001"
    assert details["lineage"]["label_version"] == "lbl-phase7-001"
    assert details["lineage"]["risk_config_version"] == "risk-phase7-001"
    assert details["lineage"]["git_sha"] == "abc123"
    assert details["lineage"]["promotion_status"] == "eligible"
    assert details["lineage"]["approval_status"] == "approved"
    assert details["lineage"]["report_count"] >= 1
    assert details["lineage"]["release_manifest_ref"] == ""


def test_v2_state_surfaces_paper_execution_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_AGENT_MODE", "paper")
    monkeypatch.setenv("FXSTACK_AGENT_PAPER_PAIR_ALLOWLIST", "EURUSD")
    monkeypatch.setenv("FXSTACK_AGENT_PAPER_INTENT_ALLOWLIST", "enter")
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.record_tick({"symbol": "EURUSD", "bid": 1.1010, "ask": 1.1012, "spread": 0.0002})
    run_id = "paper-run-1"
    trace_id = "paper-trace-1"
    context = {
        "run_id": run_id,
        "cycle_id": "paper-123",
        "thread_id": "EURUSD:paper-123:paper",
        "correlation_id": "EURUSD:paper-123:paper",
        "ts_utc": "2026-04-08T12:05:00+00:00",
        "pair": "EURUSD",
        "runtime_mode": "paper",
        "version_bundle": {
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "policy_version": "fxstack_policy_v1",
            "model_bundle_version": "bundle-v1",
            "orchestrator_version": ORCHESTRATION_SCHEMA_VERSION,
        },
    }
    packet = {
        "packet_id": "paper-packet-1",
        "run_id": run_id,
        "pair": "EURUSD",
        "ts_utc": context["ts_utc"],
        "baseline_action": {"action": "enter"},
        "shadow_action": {"action": "enter", "side": "BUY"},
        "divergence_reason": "agree",
        "proposal_votes": {"total": 1, "by_intent": {"enter": 1}, "by_side": {"BUY": 1}, "by_agent": {"signal": 1}},
        "fault_classification": None,
        "proposals": [],
        "governed_decision": {
            "decision_id": "paper-decision-1",
            "run_id": run_id,
            "allowed": True,
            "selected_action": "enter",
            "command_preview": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "action": "entry"},
            "blocking_reasons": [],
            "approval_state": "auto",
            "governor_version": ORCHESTRATION_SCHEMA_VERSION,
            "invariants_ok": True,
            "winning_proposal_id": "proposal-1",
        },
        "latency_ms": 12,
        "fallback_used": False,
        "trace_id": trace_id,
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
    }
    trace = {
        "trace_id": trace_id,
        "run_id": run_id,
        "node_spans": [],
        "tool_calls": [],
        "model_calls": [],
        "persistence_refs": [f"run://{run_id}"],
        "prompt_hashes": [],
        "input_hash": "sha256:in",
        "output_hash": "sha256:out",
        "error_class": None,
        "created_at": context["ts_utc"],
        "checkpoint": {"thread_id": context["thread_id"], "checkpoint": {}},
    }
    service.store_orchestration_bundle(
        context=context,
        packet=packet,
        trace=trace,
        runtime_mode="paper",
        fallback_used=False,
    )

    queued = client.post(
        "/v2/commands",
        json={
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "command_id": "paper-api-1",
            "correlation_id": context["correlation_id"],
            "thread_id": context["thread_id"],
            "idempotency_key": "paper-idem-1",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_meta_json": {
                "agent_mode": "paper",
                "run_id": run_id,
                "trace_id": trace_id,
            },
        },
    )
    assert queued.status_code == 200
    assert queued.json()["execution_provider"] == "paper"

    state = client.get("/v2/state").json()
    paper = state["paper_execution"]
    assert paper["enabled"] is True
    assert paper["execution_provider"] == "paper"
    assert paper["agent_mode"] == "paper"
    assert paper["governed_decision"]["run_id"] == run_id
    assert paper["governed_decision"]["approval_state"] == "auto"
    assert paper["last_command"]["command_id"] == "paper-api-1"
    assert paper["last_command"]["status"] == "acked"
    assert paper["last_event"]["status"] == "acked"
    assert float(paper["last_event"]["fill_price"]) == 1.1012
    assert "acked" in paper["event_flow"]["statuses"]
    assert state["paperExecution"]["last_command"]["command_id"] == "paper-api-1"


def test_v2_state_surfaces_paper_execution_summary_without_explicit_agent_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_EXECUTION_PROVIDER", "paper")
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.record_tick({"symbol": "EURUSD", "bid": 1.2020, "ask": 1.2023, "spread": 0.0003})
    queued = client.post(
        "/v2/commands",
        json={
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "command_id": "paper-api-2",
            "correlation_id": "EURUSD:paper-2:paper",
            "thread_id": "EURUSD:paper-2:paper",
            "idempotency_key": "paper-idem-2",
        },
    )
    assert queued.status_code == 200
    assert queued.json()["execution_provider"] == "paper"

    state = client.get("/v2/state").json()
    paper = state["paper_execution"]
    assert paper["enabled"] is True
    assert paper["execution_provider"] == "paper"
    assert paper["agent_mode"] == "off"
    assert paper["recent_command_count"] == 1
    assert paper["status_counts"]["acked"] == 1
    assert paper["last_command"]["command_id"] == "paper-api-2"
    assert paper["last_command"]["status"] == "acked"
    assert paper["last_event"]["status"] == "acked"
    assert paper["event_flow"]["statuses"][:3] == ["queued", "delivered", "acked"]

    ready = client.get("/v2/ready").json()
    assert ready["paperExecution"]["enabled"] is True
    assert ready["paperExecution"]["status_counts"]["acked"] == 1


def test_v2_state_and_ready_surface_orchestration_live_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_AGENT_MODE", "live")
    monkeypatch.setenv("FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST", "EURUSD")
    monkeypatch.setenv("FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST", "trend")
    monkeypatch.setenv("FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST", "enter")
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.upsert_active_model_set(
        pair="EURUSD",
        model_set_id="bundle-live-1",
        registry_path="mlflow://EURUSD@shadow",
        artifacts={"meta": {"model_uri": "models:/fx.meta_filter.EURUSD.M5@shadow"}},
        metadata={
            "release_status": "canary_active",
            "canary_plan": {
                "status": "active",
                "metadata": {
                    "mode": "orchestration_live",
                    "allowlisted_pairs": ["EURUSD"],
                    "live_pair_allowlist": ["EURUSD"],
                    "live_sleeve_allowlist": ["trend"],
                    "live_intent_allowlist": ["enter"],
                    "ramp_steps_pct": [1, 5, 10],
                    "current_stage_index": 0,
                    "current_stage_pct": 1,
                    "promotion_pack_path": str(tmp_path / "promotion_pack.md"),
                    "signoff_records": [{"stage_pct": 1, "author": "ops"}],
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                },
            },
            "canary_prep": {
                "mode": "orchestration_live",
                "allowlisted_pairs": ["EURUSD"],
                "live_pair_allowlist": ["EURUSD"],
                "live_sleeve_allowlist": ["trend"],
                "live_intent_allowlist": ["enter"],
                "ramp_steps_pct": [1, 5, 10],
                "current_stage_index": 0,
                "current_stage_pct": 1,
                "budget_scale": 0.01,
                "promotion_pack_path": str(tmp_path / "promotion_pack.md"),
                "signoff_records": [{"stage_pct": 1, "author": "ops"}],
                "runtime_enabled": True,
                "queue_kill_active": False,
            },
            "activation_package": {"bundle_run_id": "bundle-live-1", "release_status": "canary_active"},
        },
        enabled=True,
    )
    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "system_status": "connected",
            "last_heartbeat": time.time(),
            "heartbeat_age_secs": 1.0,
            "heartbeat_stale_after_secs": 30.0,
            "ticks_fresh": True,
            "tick_status": "fresh",
            "tick_reason": "fresh",
            "canary_pairs": ["EURUSD"],
            "runtime_diag": {
                "orchestration_live": {
                    "enabled": True,
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "active_pair_scope": ["EURUSD"],
                    "active_sleeve_scope": ["trend"],
                    "active_intent_scope": ["enter"],
                    "ramp_steps_pct": [1, 5, 10],
                    "current_stage_index": 0,
                    "current_stage_pct": 1,
                    "budget_scale": 0.01,
                    "p95_ms": 120.0,
                    "p99_ms": 180.0,
                    "ack_success_rate": 1.0,
                    "ack_timeout_rate": 0.0,
                    "orphan_command_count": 0,
                    "entry_ratio_vs_baseline": 1.0,
                    "slot_utilisation_vs_baseline": 1.0,
                    "drawdown_deterioration_pct": 0.1,
                    "repeated_graph_fault_count": 0,
                    "trace_persistence_failure_count": 0,
                    "baseline_fallback_count": 0,
                }
            },
        }
    )

    run_id = "live-run-1"
    trace_id = "live-trace-1"
    service.store_orchestration_bundle(
        context={
            "run_id": run_id,
            "cycle_id": "live-123",
            "thread_id": "EURUSD:live-123:live",
            "correlation_id": "EURUSD:live-123:live",
            "ts_utc": "2026-04-08T12:10:00+00:00",
            "pair": "EURUSD",
            "runtime_mode": "live",
            "version_bundle": {
                "schema_version": ORCHESTRATION_SCHEMA_VERSION,
                "policy_version": "fxstack_policy_v1",
                "model_bundle_version": "bundle-live-1",
                "orchestrator_version": ORCHESTRATION_SCHEMA_VERSION,
            },
        },
        packet={
            "packet_id": "live-packet-1",
            "run_id": run_id,
            "pair": "EURUSD",
            "ts_utc": "2026-04-08T12:10:00+00:00",
            "baseline_action": {"action": "enter"},
            "shadow_action": {"action": "enter", "side": "BUY"},
            "divergence_reason": "agree",
            "proposal_votes": {"total": 1},
            "fault_classification": None,
            "proposals": [],
            "governed_decision": {
                "decision_id": "live-decision-1",
                "run_id": run_id,
                "allowed": True,
                "selected_action": "enter",
                "command_preview": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "action": "entry"},
                "blocking_reasons": [],
                "approval_state": "auto",
                "governor_version": ORCHESTRATION_SCHEMA_VERSION,
                "invariants_ok": True,
                "winning_proposal_id": "proposal-1",
            },
            "latency_ms": 12,
            "fallback_used": False,
            "trace_id": trace_id,
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
        },
        trace={
            "trace_id": trace_id,
            "run_id": run_id,
            "node_spans": [],
            "tool_calls": [],
            "model_calls": [],
            "persistence_refs": [f"run://{run_id}"],
            "prompt_hashes": [],
            "input_hash": "sha256:in",
            "output_hash": "sha256:out",
            "error_class": None,
            "created_at": "2026-04-08T12:10:00+00:00",
            "checkpoint": {"thread_id": "EURUSD:live-123:live", "checkpoint": {}},
        },
        runtime_mode="live",
        fallback_used=False,
    )

    queued = client.post(
        "/v2/commands",
        json={
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "command_id": "live-api-1",
            "intent": "ENTRY_MODEL",
            "correlation_id": "EURUSD:live-123:live",
            "thread_id": "EURUSD:live-123:live",
            "idempotency_key": "live-idem-1",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_meta_json": {
                "agent_mode": "live",
                "run_id": run_id,
                "trace_id": trace_id,
                "command_source": "governed_live",
            },
        },
    )
    assert queued.status_code == 200
    delivered = client.post(
        "/v2/commands/ack",
        json={
            "command_id": "live-api-1",
            "status": "delivered",
            "symbol": "EURUSD",
            "correlation_id": "EURUSD:live-123:live",
            "thread_id": "EURUSD:live-123:live",
            "idempotency_key": "live-idem-1",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_meta_json": {"agent_mode": "live"},
        },
    )
    assert delivered.status_code == 200
    acked = client.post(
        "/v2/commands/ack",
        json={
            "command_id": "live-api-1",
            "status": "acked",
            "symbol": "EURUSD",
            "ticket": 123456,
            "correlation_id": "EURUSD:live-123:live",
            "thread_id": "EURUSD:live-123:live",
            "idempotency_key": "live-idem-1",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "orchestration_meta_json": {"agent_mode": "live"},
        },
    )
    assert acked.status_code == 200

    state = client.get("/v2/state").json()
    live = state["orchestration_live"]
    live_health = state["orchestration_live_health"]
    assert live["enabled"] is True
    assert live["current_stage_pct"] == 1
    assert live["runtime_enabled"] is True
    assert live["queue_kill_active"] is False
    assert live["governed_decision"]["run_id"] == run_id
    assert live["last_command"]["command_id"] == "live-api-1"
    assert live["last_event"]["status"] == "acked"
    assert live["ack_success_rate"] == 1.0
    assert live_health["status"] == "healthy"
    assert live_health["reason"] == "ok"
    assert live_health["warning_count"] == 0
    assert live_health["blocking_count"] == 0
    assert live_health["ack_timeout_rate"] == 0.0
    assert state["orchestrationLiveHealth"]["status"] == "healthy"
    assert state["orchestrationLive"]["last_command"]["command_id"] == "live-api-1"

    ready = client.get("/v2/ready").json()
    assert ready["orchestration_live"]["enabled"] is True
    assert ready["orchestrationLive"]["current_stage_pct"] == 1
    assert ready["orchestration_live_health"]["status"] == "healthy"
    assert ready["orchestrationLiveHealth"]["reason"] == "ok"


def test_v2_state_and_ready_surface_orchestration_live_health_degradation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_AGENT_MODE", "live")
    monkeypatch.setenv("FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST", "EURUSD")
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "system_status": "connected",
            "last_heartbeat": time.time(),
            "heartbeat_age_secs": 1.0,
            "heartbeat_stale_after_secs": 30.0,
            "ticks_fresh": True,
            "tick_status": "fresh",
            "tick_reason": "ok",
            "canary_pairs": ["EURUSD"],
            "runtime_diag": {
                "orchestration_live": {
                    "enabled": True,
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "pending_command_count": 3,
                    "orphan_command_count": 2,
                    "ack_success_rate": 0.8,
                    "ack_timeout_rate": 0.2,
                    "repeated_graph_fault_count": 1,
                    "trace_persistence_failure_count": 1,
                    "baseline_fallback_count": 1,
                }
            },
        }
    )

    state = client.get("/v2/state").json()
    live_health = state["orchestration_live_health"]
    assert live_health["status"] == "degraded"
    assert live_health["reason"] in {
        "orphan_commands",
        "ack_timeout_spike",
        "graph_faults",
        "trace_persistence_failures",
        "baseline_fallbacks",
    }
    assert live_health["warning_count"] >= 4
    assert live_health["blocking_count"] == 0
    assert set(live_health["reasons"]) >= {
        "orphan_commands",
        "ack_timeout_spike",
        "graph_faults",
        "trace_persistence_failures",
        "baseline_fallbacks",
    }

    ready = client.get("/v2/ready").json()
    assert ready["orchestration_live_health"]["status"] == "degraded"
    assert set(ready["orchestrationLiveHealth"]["reasons"]) >= {
        "orphan_commands",
        "ack_timeout_spike",
        "graph_faults",
        "trace_persistence_failures",
    }


def test_v2_state_preserves_directional_belief_fields(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "runtime_diag": {
                "directional_belief_policy": {
                    "enabled": True,
                    "runtime_required": False,
                    "short_horizon_bars": 3,
                    "trade_horizon_bars": 12,
                    "structural_horizon_bars": 48,
                },
                "directional_belief_cycle_summary": {
                    "candidate_count_with_belief": 2,
                    "avg_belief_gap": 0.14,
                    "avg_fragility_score": 0.22,
                    "avg_primary_rank_score": 0.31,
                    "avg_primary_ev_above_hurdle_prob": 0.62,
                    "avg_primary_expected_net_ev_bps": 4.8,
                    "avg_primary_fail_fast_prob": 0.21,
                    "no_edge_share": 0.1,
                    "primary_scenario_counts": {"trend_pullback": 2},
                    "opposition_scenario_counts": {"failed_breakout_reversal": 1},
                    "opposition_side_counts": {"short": 2},
                    "artifact_versions": {"EURUSD": "belief_v1"},
                },
                "directional_belief_metrics": {
                    "decision_count": 2,
                    "belief_loaded_share": 1.0,
                    "avg_belief_gap": 0.14,
                    "avg_fragility_score": 0.22,
                    "avg_primary_rank_score": 0.31,
                    "avg_primary_ev_above_hurdle_prob": 0.62,
                    "avg_primary_expected_net_ev_bps": 4.8,
                    "avg_primary_fail_fast_prob": 0.21,
                    "no_edge_share": 0.1,
                    "primary_scenario_counts": {"trend_pullback": 2},
                    "opposition_scenario_counts": {"failed_breakout_reversal": 1},
                    "opposition_side_counts": {"short": 2},
                },
            },
            "agent_decisions": [
                {
                    "symbol": "EURUSD",
                    "side": "BUY",
                    "score": 5.1,
                    "execution_ready": False,
                    "reasons": ["shadow_meta_reject"],
                    "metadata": {
                        "pair": "EURUSD",
                        "ts": "2026-03-26T12:00:00Z",
                        "belief_primary_side": "long",
                        "belief_primary_scenario": "trend_pullback",
                        "belief_primary_thesis": "trend_pullback:long",
                        "belief_primary_score": 0.44,
                        "belief_primary_rank_score": 0.61,
                        "belief_primary_ev_above_hurdle_prob": 0.73,
                        "belief_primary_expected_net_ev_bps": 8.4,
                        "belief_primary_confirm_prob": 0.66,
                        "belief_primary_fail_fast_prob": 0.14,
                        "belief_no_edge": False,
                        "belief_opposing_side": "short",
                        "belief_opposing_scenario": "failed_breakout_reversal",
                        "belief_opposing_thesis": "failed_breakout_reversal:short",
                        "belief_opposing_score": 0.18,
                        "belief_gap": 0.26,
                        "belief_fragility_score": 0.21,
                        "belief_horizon_alignment_score": 0.88,
                        "belief_short_up_prob": 0.58,
                        "belief_trade_up_prob": 0.69,
                        "belief_structural_up_prob": 0.74,
                        "belief_regime_fit_score": 1.0,
                        "belief_expected_confirmation_window_bars": 3,
                        "belief_expected_path_shape": "pullback_then_resume",
                        "belief_invalidation_reason": "trigger_score_lt_0.35_or_trade_prob_lt_0.50",
                        "belief_model_version": "belief_v1",
                        "belief_source_mode": "artifact",
                    },
                }
            ],
        }
    )

    state = client.get("/v2/state").json()
    assert state["runtime_diag"]["directional_belief_policy"]["enabled"] is True
    assert state["runtime_diag"]["directional_belief_cycle_summary"]["candidate_count_with_belief"] == 2
    assert state["runtime_diag"]["directional_belief_cycle_summary"]["avg_primary_rank_score"] == 0.31
    assert state["runtime_diag"]["directional_belief_metrics"]["no_edge_share"] == 0.1
    assert len(state["decisions"]) == 1
    assert len(state["latest_decisions"]) == 1
    assert state["agent_decisions"][0]["metadata"]["belief_primary_scenario"] == "trend_pullback"
    assert state["agent_decisions"][0]["metadata"]["belief_primary_ev_above_hurdle_prob"] == 0.73
    assert state["agent_decisions"][0]["metadata"]["belief_no_edge"] is False
    assert state["agent_decisions"][0]["metadata"]["belief_source_mode"] == "artifact"


def test_v2_ops_workflows_status_surfaces_mlflow_audit_fields(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    pair = "EURUSD"
    service.patch_state(
        {
            "runtime_diag": {
                "feature_serving": {
                    "source": "feast_online",
                    "source_chain": ["feast_online", "parquet_fallback", "raw_contract_fallback"],
                    "feature_service": "fx_eurusd_m5",
                    "cache_hit": True,
                    "freshness_secs": 4.5,
                    "stale": False,
                    "reason": "ok",
                    "details": {"cache_key": "EURUSD:M5"},
                },
                "feature_serving_by_pair": {
                    "EURUSD:M5": {
                        "source": "feast_online",
                        "reason": "ok",
                    }
                },
            }
        }
    )
    service.upsert_active_model_set(
        pair=pair,
        model_set_id="bundle-mlflow-1",
        registry_path="mlflow://EURUSD@champion",
        artifacts={
            "meta": {
                "path": _make_artifact(tmp_path / "artifacts", "meta_filter"),
                "model_uri": "models:/fx.meta_filter.EURUSD.M5@champion",
                "model_name": "fx.meta_filter.EURUSD.M5",
                "model_version": "7",
                "feature_service_name": "fx_eurusd_meta_filter_m5",
                "feature_service_version": "svc-meta-1",
                "feature_contract_hash": "hash-meta-1",
                "feature_view_names": ["anchor_m5", "context_m15"],
            },
            "regime": {
                "path": _make_artifact(tmp_path / "artifacts", "regime_hmm"),
                "model_uri": "models:/fx.regime_hmm.EURUSD.H4@champion",
                "model_name": "fx.regime_hmm.EURUSD.H4",
                "model_version": "4",
                "feature_service_name": "fx_eurusd_regime_hmm_h4",
                "feature_service_version": "svc-regime-1",
                "feature_contract_hash": "hash-regime-1",
                "feature_view_names": ["anchor_h4"],
            },
            "swing_xgb": {
                "path": _make_artifact(tmp_path / "artifacts", "swing_xgb"),
                "model_uri": "models:/fx.swing_xgb.EURUSD.D@champion",
                "model_name": "fx.swing_xgb.EURUSD.D",
                "model_version": "5",
                "feature_service_name": "fx_eurusd_swing_xgb_d",
                "feature_service_version": "svc-swing-1",
                "feature_contract_hash": "hash-swing-1",
                "feature_view_names": ["anchor_d"],
            },
            "intraday_xgb": {
                "path": _make_artifact(tmp_path / "artifacts", "intraday_xgb"),
                "model_uri": "models:/fx.intraday_xgb.EURUSD.M5@champion",
                "model_name": "fx.intraday_xgb.EURUSD.M5",
                "model_version": "6",
                "feature_service_name": "fx_eurusd_intraday_xgb_m5",
                "feature_service_version": "svc-intraday-1",
                "feature_contract_hash": "hash-intraday-1",
                "feature_view_names": ["anchor_m5", "context_m15", "context_h1", "context_h4", "context_d"],
            },
            "exit_policy": {"path": _make_artifact(tmp_path / "artifacts", "exit_policy")},
            "reversal_failure": {"path": _make_artifact(tmp_path / "artifacts", "reversal_failure")},
            "reversal_opportunity": {"path": _make_artifact(tmp_path / "artifacts", "reversal_opportunity")},
        },
        metadata={
            "bundle_run_id": "bundle-mlflow-1",
            "promotion_status": "eligible",
            "capabilities": {"has_exit_model": True, "has_reversal_models": True, "lifecycle_complete": True},
            "mlflow": {
                "tracking_uri": "sqlite:///tmp/mlflow.db",
                "registry_uri": "sqlite:///tmp/mlflow.db",
                "activated_alias": "champion",
                "component_versions": {
                    "meta": {"model_name": "fx.meta_filter.EURUSD.M5", "model_version": "7"},
                },
            },
        },
        enabled=True,
    )

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "eurusd-training-eval")
    assert workflow["details_json"]["bundle_run_id"] == "bundle-mlflow-1"
    assert workflow["details_json"]["activation_alias"] == "champion"
    assert workflow["details_json"]["mlflow"]["tracking_uri"] == "sqlite:///tmp/mlflow.db"
    assert workflow["details_json"]["component_model_uris"]["meta"].endswith("@champion")
    assert workflow["details_json"]["component_feature_services"]["meta"]["feature_service_name"] == "fx_eurusd_meta_filter_m5"
    assert "fx_eurusd_intraday_xgb_m5" in workflow["details_json"]["active_feature_services"]
    assert workflow["details_json"]["feature_serving"]["source"] == "feast_online"
    assert workflow["details_json"]["feature_serving_source"] == "feast_online"


def test_v2_state_ready_metrics_surface_feature_serving_telemetry(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_diag": {
                "feature_serving": {
                    "source": "parquet_fallback",
                    "source_chain": ["feast_online", "parquet_fallback", "raw_contract_fallback"],
                    "feature_service": "fx_eurusd_m5",
                    "cache_hit": False,
                    "freshness_secs": 12.0,
                    "stale": True,
                    "reason": "feast_unavailable",
                    "details": {"fallback_from": "feast_online"},
                }
            }
        }
    )

    state = client.get("/v2/state").json()
    assert state["feature_serving"]["source"] == "parquet_fallback"
    assert state["feature_serving_source"] == "parquet_fallback"
    assert state["feature_serving_feature_service"] == "fx_eurusd_m5"
    assert state["featureObservability"]["feature_serving"]["source"] == "parquet_fallback"
    assert state["feature_blocker_source"] == "feature_serving"
    assert state["feature_blocker_reason"] == "feature_serving:stale"
    assert state["feature_bar_status"] == "stale"

    ready = client.get("/v2/ready").json()
    assert ready["feature_serving"]["source"] == "parquet_fallback"
    assert ready["feature_serving_cache_hit"] is False
    assert ready["featureObservability"]["feature_serving"]["source"] == "parquet_fallback"
    assert ready["feature_blocker_source"] == "feature_serving"
    assert ready["feature_bar_status"] == "stale"

    metrics = client.get("/v2/metrics").json()
    assert metrics["feature_serving"]["stale"] is True
    assert metrics["feature_serving_reason"] == "feast_unavailable"


def test_v2_ready_and_state_honor_top_level_feature_serving_patch(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "feature_serving": {
                "source": "paper_smoke",
                "source_chain": ["paper_smoke"],
                "feature_service": "fx_eurusd_m5",
                "cache_hit": True,
                "freshness_secs": 1.0,
                "stale": False,
                "reason": "ok",
                "details": {"source": "top_level_patch"},
            }
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()

    assert state["feature_serving"]["source"] == "paper_smoke"
    assert state["feature_serving_source"] == "paper_smoke"
    assert state["featureObservability"]["feature_online_ready"] is True
    assert state["featureObservability"]["feature_data_fresh"] is True
    assert state["feature_bar_status"] == "fresh"
    assert ready["feature_serving"]["source"] == "paper_smoke"
    assert ready["feature_serving_source"] == "paper_smoke"
    assert ready["featureObservability"]["feature_online_ready"] is True
    assert ready["featureObservability"]["feature_data_fresh"] is True
    assert ready["feature_bar_status"] == "fresh"


def test_v2_state_ready_metrics_surface_phase7_provider_and_governance_telemetry(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_diag": {
                "provider_roles": {
                    "history_provider": "dukascopy",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
                "provider_health": {
                    "history_provider": {"provider": "dukascopy", "role": "history", "status": "ok"},
                    "market_data_provider": {"provider": "mt4_bridge", "role": "market_data", "status": "degraded"},
                    "execution_provider": {"provider": "mt4", "role": "execution", "status": "ok"},
                },
                "portfolio_intelligence": {
                    "gross_exposure": 2.5,
                    "net_exposure": 1.5,
                    "open_position_count": 2,
                    "concentration": {"top_symbol": "EURUSD", "top_symbol_share": 0.6},
                },
                "capital_governance": {
                    "capital_band": "micro_live",
                    "mode": "entries_only",
                    "paused": False,
                    "entries_only": True,
                    "budget_scale": 0.1,
                    "rollback_actions": [{"action": "execution_rollback", "armed": True, "reason": "entries_only"}],
                },
            }
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()
    metrics = client.get("/v2/metrics").json()
    health = client.get("/v2/health").json()

    assert state["provider_health"]["roles"]["history_provider"] == "dukascopy"
    assert state["provider_health"]["source_chain"] == ["dukascopy", "mt4_bridge", "mt4"]
    assert state["provider_health"]["market_data_provider_name"] == "mt4_bridge"
    assert state["portfolio_intelligence"]["gross_exposure"] == 2.5
    assert state["portfolio_intelligence"]["budget_targets"] == {}
    assert state["capital_governance"]["mode"] == "entries_only"
    assert state["capital_governance"]["release_mode"] == "entries_only"
    assert state["capital_governance"]["risk_scale"] == 0.1
    assert state["capital_governance"]["rollback_armed"] is True
    assert state["entries_only_mode"] is True
    assert ready["provider_health"]["roles"]["market_data_provider"] == "mt4_bridge"
    assert ready["capital_band"] == "micro_live"
    assert metrics["provider_roles"]["execution_provider"] == "mt4"
    assert metrics["capital_governance"]["entries_only"] is True
    assert health["provider_health"]["market_data_provider"]["status"] == "degraded"


def test_v2_state_ready_metrics_surface_paper_execution_provider(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_diag": {
                "provider_roles": {
                    "history_provider": "dukascopy",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "paper",
                },
                "provider_health": {
                    "history_provider": {"provider": "dukascopy", "role": "history", "status": "ok"},
                    "market_data_provider": {"provider": "mt4_bridge", "role": "market_data", "status": "ok"},
                    "execution_provider": {
                        "provider": "paper",
                        "role": "execution",
                        "status": "ok",
                        "shadow_only": True,
                        "provenance": "runtime_service",
                        "details": {"execution_provider": "paper", "paused": False, "entries_only": False},
                    },
                },
            }
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()
    metrics = client.get("/v2/metrics").json()

    assert state["provider_health"]["roles"]["execution_provider"] == "paper"
    assert state["provider_health"]["execution_provider"]["provider"] == "paper"
    assert state["provider_health"]["execution_provider"]["status"] == "ok"
    assert state["provider_health"]["execution_provider"]["shadow_only"] is True
    assert ready["provider_health"]["execution_provider"]["provider"] == "paper"
    assert ready["provider_health"]["execution_provider"]["status"] == "ok"
    assert ready["provider_health"]["execution_provider"]["shadow_only"] is True
    assert metrics["provider_roles"]["execution_provider"] == "paper"


def test_v2_ready_provider_health_falls_back_to_settings_when_runtime_diag_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FXSTACK_AGENT_MODE", "paper")
    monkeypatch.setenv("FXSTACK_EXECUTION_PROVIDER", "paper")
    monkeypatch.setenv("FXSTACK_DATA_PROVIDER", "dukascopy")
    monkeypatch.setenv("FXSTACK_MARKET_DATA_PROVIDER", "mt4_bridge")
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    now = time.time()
    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": now,
            "system_status": "connected",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "ticks_fresh": True,
            "tick_status": "fresh",
            "tick_reason": "ok",
            "tick_max_age_secs": 0.0,
            "tick_symbols_count": 1,
            "feature_serving_source": "parquet_fallback",
            "feature_serving_reason": "ok",
            "feature_serving_stale": False,
            "feature_bar_status": "fresh",
            "runtime_diag": {},
        }
    )

    ready = client.get("/v2/ready").json()
    state = client.get("/v2/state").json()

    assert ready["provider_roles"]["execution_provider"] == "paper"
    assert ready["provider_health"]["execution_provider"]["provider"] == "paper"
    assert ready["provider_health"]["execution_provider"]["status"] == "ok"
    assert ready["provider_health"]["market_data_provider"]["provider"] == "mt4_bridge"
    assert ready["provider_health"]["market_data_provider"]["status"] == "ok"
    assert state["provider_health"]["roles"]["history_provider"] == "dukascopy"
    assert state["provider_health"]["execution_provider"]["shadow_only"] is True


def test_v2_ready_surfaces_missing_feature_serving_snapshot_diagnostics(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "feature_serving": {},
            "feature_serving_source": "",
            "feature_serving_reason": "",
            "feature_serving_stale": False,
            "runtime_diag": {
                "feature_serving_by_pair": {
                    "EURUSD:M5": {"source": "", "stale": True, "reason": "feast_unavailable"},
                    "GBPUSD:D": {"source": "", "stale": True, "reason": "feast_unavailable"},
                },
            },
        }
    )
    service.enqueue_feature_push(
        {
            "outbox_key": "obs-queued",
            "pair": "EURUSD",
            "feature_service": "fx_eurusd_m5",
            "entity_key": "EURUSD",
            "status": "queued",
        }
    )
    service.enqueue_feature_push(
        {
            "outbox_key": "obs-retry",
            "pair": "EURUSD",
            "feature_service": "fx_eurusd_m5",
            "entity_key": "EURUSD",
            "status": "retry",
        }
    )
    service.enqueue_feature_push(
        {
            "outbox_key": "obs-claimed",
            "pair": "GBPUSD",
            "feature_service": "fx_gbpusd_m5",
            "entity_key": "GBPUSD",
            "status": "claimed",
            "claimed_at": time.time(),
        }
    )

    state = client.get("/v2/state").json()
    ready = client.get("/v2/ready").json()

    for payload in (state, ready):
        observability = payload["featureObservability"]
        summary = observability["feature_serving_summary"]
        push_summary = observability["feature_push_summary"]
        assert observability["feature_online_ready"] is False
        assert observability["feature_data_fresh"] is False
        assert payload["feature_blocker_source"] == "feature_serving"
        assert payload["feature_blocker_reason"].startswith("feature_serving:")
        assert summary["by_pair_count"] == 2
        assert summary["by_pair_pairs"] == ["EURUSD", "GBPUSD"]
        assert summary["by_pair_stale_count"] == 2
        assert summary["by_pair_stale_pairs"] == ["EURUSD", "GBPUSD"]
        assert summary["selected_pairs_count"] == 0
        assert push_summary["backlog"] == 3
        assert push_summary["outbox_count"] == 3


def test_v2_metrics_and_health_surface_risk_cycle_summary(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_diag": {
                "risk_cycle_summary": {
                    "decision_count": 4,
                    "approved_order_count": 2,
                    "blocked_entry_count": 1,
                    "verdict_counts": {"allow": 2, "block": 1, "hold": 1},
                    "dominant_block_reason": "spread_too_wide",
                }
            }
        }
    )

    metrics = client.get("/v2/metrics").json()
    health = client.get("/v2/health").json()
    assert metrics["risk_cycle_summary"]["decision_count"] == 4
    assert metrics["risk_cycle_summary"]["dominant_block_reason"] == "spread_too_wide"
    assert health["risk_cycle_summary"]["approved_order_count"] == 2


def test_v2_ops_events_surfaces_feature_incidents(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.record_governance_event(
        event_type="feature_push_failed",
        reason="feast_unavailable",
        payload={"pair": "EURUSD", "feature_service": "fx_eurusd_intraday_xgb_m5"},
    )
    service.record_governance_event(
        event_type="feature_parity_breach",
        reason="drift_score_gt_tolerance",
        payload={"pair": "EURUSD", "feature_service": "fx_eurusd_intraday_xgb_m5", "drift_score": 0.42},
    )

    body = client.get("/v2/ops/events").json()
    event_types = [str(item.get("event_type") or "") for item in body["events"]]
    assert "feature_push_failed" in event_types
    assert "feature_parity_breach" in event_types
    failed = next(item for item in body["events"] if item.get("event_type") == "feature_push_failed")
    assert failed["status"] == "error"
    assert failed["payload"]["pair"] == "EURUSD"


def test_workflow_status_surfaces_phase5_release_metadata(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.upsert_active_model_set(
        pair="EURUSD",
        model_set_id="bundle-phase5",
        registry_path="mlflow://EURUSD@shadow",
        artifacts={
            "meta": {
                "model_uri": "models:/fx.meta_filter.EURUSD.M5@shadow",
                "model_version": "7",
                "feature_service_name": "fx_eurusd_execution_grade_m5",
                "feature_service_version": "svc-v1",
                "feature_contract_hash": "contract-1",
                "feature_view_names": ["anchor_m5"],
            }
        },
        metadata={
            "bundle_run_id": "bundle-phase5",
            "capabilities": {"has_exit_model": True, "has_reversal_models": True, "lifecycle_complete": True},
            "release_status": "canary_active",
            "rollback_target": {"target_bundle_run_id": "bundle-prev", "target_alias": "champion"},
            "operator_signoff": {"approvers": ["ops"]},
            "canary_plan": {"status": "active", "metadata": {"allowlisted_pairs": ["EURUSD"], "budget_scale": 0.25}},
            "promotion_gates": [{"gate_id": "research_gate", "status": "pass", "passed": True}],
            "shadow_acceptance_summary": {
                "status": "ready",
                "ready": True,
                "release_status": "canary_active",
                "gate_summary": {"all_required_passed": True},
            },
            "phase5_gate_summary": {
                "status": "passed",
                "gate_count": 1,
                "passed_gate_count": 1,
                "all_required_passed": True,
            },
            "canary_prep": {
                "status": "active",
                "allowlisted_pairs": ["EURUSD"],
                "budget_scale": 0.25,
                "duration_minutes": 60,
                "metrics_window_minutes": 60,
            },
            "activation_package": {"bundle_run_id": "bundle-phase5", "model_alias": "shadow", "release_status": "canary_active"},
            "phase5_gates": {"phase5_gate_bundle": str(tmp_path / "phase5_gate_bundle.json")},
            "phase5_gate_bundle": {"canary_gate": {"passed": True}},
            "mlflow": {"activated_alias": "shadow"},
        },
        enabled=True,
    )

    r = client.get("/v2/ops/workflows/status")
    assert r.status_code == 200
    workflows = list(r.json().get("workflows") or [])
    eurusd = next(item for item in workflows if str(item.get("workflow_id") or "").startswith("eurusd-"))
    details = dict(eurusd.get("details_json") or {})
    assert details["release_status"] == "canary_active"
    assert dict(details["rollback_target"])["target_bundle_run_id"] == "bundle-prev"
    assert dict(details["canary_plan"])["status"] == "active"
    assert dict(details["shadow_acceptance_summary"])["ready"] is True
    assert dict(details["phase5_gate_summary"])["passed_gate_count"] == 1
    assert dict(details["canary_prep"])["allowlisted_pairs"] == ["EURUSD"]
    assert dict(details["activation_package"])["model_alias"] == "shadow"
