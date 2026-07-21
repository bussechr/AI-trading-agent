from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import pytest

from fxstack.features.session_contract import (
    FEATURE_SCHEMA_VERSION,
    SESSION_CONTRACT_VERSION,
    current_feature_schema,
    feature_contract_metadata,
)
from fxstack.models.artifact_contract import (
    ARTIFACT_PAYLOAD_DIGEST_KEY,
    stamp_artifact_payload_digest,
)
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings
from fxstack.training.release_evidence import (
    ReleaseEvidenceIdentity,
    active_manifest_identity,
    artifact_set_sha256,
    candidate_manifest_identity,
    file_sha256,
)


def _configure_mlflow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    tracking_db = tmp_path / "mlflow.db"
    tracking_uri = f"sqlite:///{tracking_db}"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FXSTACK_MLFLOW_ENABLED", "1")
    monkeypatch.setenv("FXSTACK_MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("FXSTACK_MLFLOW_REGISTRY_URI", tracking_uri)
    monkeypatch.setenv("FXSTACK_MLFLOW_CACHE_ROOT", str(tmp_path / "mlflow_cache"))
    get_settings.cache_clear()
    return tracking_uri


def _make_artifact(
    root: Path,
    name: str,
    *,
    with_reports: bool = False,
    artifact_name: str | None = None,
) -> str:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": str(artifact_name or name),
        **feature_contract_metadata(),
        "trained_at": 1775433600.0,
        "data_window_end": "2026-04-05T00:00:00+00:00",
        "feature_columns": ["ret_1", "spread_bps"],
        "training_window_summary": {
            "rows": 128,
            "start_ts": "2026-01-01T00:00:00+00:00",
            "end_ts": "2026-04-05T00:00:00+00:00",
        },
    }
    (path / "model.bin").write_bytes(f"payload:{name}".encode("utf-8"))
    (path / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if with_reports:
        reports = path / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "training_report.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "promotion_decision": {
                        "status": "eligible",
                        "candidate_metric": 0.62,
                    },
                    "label_quality": {"rows": 128},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (reports / "promotion_decision.json").write_text(
            json.dumps({"status": "eligible", "candidate_metric": 0.62}, indent=2),
            encoding="utf-8",
        )
    stamp_artifact_payload_digest(path)
    return str(path)


def _artifact_ref(path: str | Path) -> dict[str, str]:
    artifact_path = Path(path)
    meta = json.loads((artifact_path / "meta.json").read_text(encoding="utf-8"))
    return {
        "path": str(artifact_path),
        "artifact_hash": str(meta[ARTIFACT_PAYLOAD_DIGEST_KEY]),
    }


def _compat_payload(tmp_path: Path, *, pair: str, run_id: str) -> dict:
    artifacts_root = tmp_path / f"artifacts_{run_id}"
    return {
        "run_id": run_id,
        "bundle_run_id": run_id,
        "pair": pair,
        "tier": "tier2",
        "trained_at": 1775433600.0,
        "data_window_end": "2026-04-05T00:00:00+00:00",
        "dataset_fingerprint": f"{run_id}-fp",
        "feature_service_version": f"{run_id}-feature",
        "label_version": f"{run_id}-label",
        "risk_config_version": f"{run_id}-risk",
        "feature_schema": current_feature_schema(
            {"belief_contract": "directional_belief_v2"}
        ),
        "training_window_summary": {
            "regime": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "swing_xgb": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "intraday_xgb": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "meta": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "exit_policy": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "reversal_failure": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "reversal_opportunity": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
        },
        "promotion_status": "eligible",
        "artifacts": {
            "regime": _artifact_ref(_make_artifact(artifacts_root, "regime_hmm")),
            "meta": _artifact_ref(
                _make_artifact(
                    artifacts_root,
                    "meta_filter",
                    with_reports=True,
                    artifact_name="meta_filter_xgb",
                )
            ),
            "swing_xgb": _artifact_ref(_make_artifact(artifacts_root, "swing_xgb")),
            "intraday_xgb": _artifact_ref(_make_artifact(artifacts_root, "intraday_xgb")),
            "exit_policy": _artifact_ref(
                _make_artifact(artifacts_root, "exit_policy_xgb", with_reports=True)
            ),
            "reversal_failure": _artifact_ref(
                _make_artifact(artifacts_root, "reversal_failure_xgb", with_reports=True)
            ),
            "reversal_opportunity": _artifact_ref(
                _make_artifact(
                    artifacts_root,
                    "reversal_opportunity_xgb",
                    with_reports=True,
                )
            ),
        },
        "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
        "capabilities": {
            "has_exit_model": True,
            "has_reversal_models": True,
            "lifecycle_complete": True,
            "has_directional_belief": False,
        },
        "lifecycle_complete": True,
        "training_config": {"labeling": {"intraday": {"horizon_bars": 18}}},
        "promotion_components": {
            "meta": "eligible",
            "exit": "eligible",
            "reversal_failure": "eligible",
            "reversal_opportunity": "eligible",
        },
        "training_eval_reports": {
            "meta": str(artifacts_root / "meta_filter" / "reports" / "training_report.json"),
            "exit": str(artifacts_root / "exit_policy_xgb" / "reports" / "training_report.json"),
            "reversal_failure": str(artifacts_root / "reversal_failure_xgb" / "reports" / "training_report.json"),
            "reversal_opportunity": str(artifacts_root / "reversal_opportunity_xgb" / "reports" / "training_report.json"),
        },
        "timeframes": {"regime": "H4", "swing": "D", "intraday": "M5"},
    }


def _shadow_samples(
    *, start: float, end: float, poll: float, boot_id: str
) -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    timestamp = float(start)
    while timestamp <= float(end):
        samples.append(
            {
                "ts": timestamp,
                "decisions": 1,
                "pending": 0,
                "timeout_rate": 0.0,
                "drawdown_pct": 0.01,
                "hard_dd_pct": 0.12,
                "daily_breaker_active": False,
                "governance_paused": False,
                "runtime_ready": True,
                "feature_ready": True,
                "canary_active": False,
                "signals_sent": 0,
                "approved_entries": 0,
                "submitted_entries": 0,
                "ack_success_rate": 1.0,
                "divergence_spike_count": 0,
                "trade_flow_seen": True,
                "runtime_boot_id": boot_id,
            }
        )
        timestamp += float(poll)
    return samples


def _command_values(command: list[str], flag: str) -> list[str]:
    return [
        command[index + 1]
        for index, token in enumerate(command[:-1])
        if token == flag
    ]


def _external_engine_process(*, engine: str):
    from fxstack.backtest.harness.contracts import (
        EXTERNAL_ECONOMIC_REPORT_VERSION,
        EXTERNAL_STRESS_REPORT_VERSION,
    )

    def _run(command: list[str], **_kwargs: object) -> object:
        linkage = {
            "harness_run_id": _command_values(
                command, "--fxstack-harness-run-id"
            )[0],
            "engine": engine,
            "engine_version": _command_values(
                command, "--fxstack-engine-version"
            )[0],
            "pair": _command_values(command, "--fxstack-pair")[0],
            "dataset_hash": _command_values(
                command, "--fxstack-dataset-hash"
            )[0],
            "input_bundle_sha256": _command_values(
                command, "--fxstack-input-bundle-sha256"
            )[0],
            "bundle_run_id": _command_values(
                command, "--fxstack-bundle-run-id"
            )[0],
            "model_set_id": _command_values(
                command, "--fxstack-model-set-id"
            )[0],
            "model_manifest_sha256": _command_values(
                command, "--fxstack-model-manifest-sha256"
            )[0],
            "artifact_set_sha256": _command_values(
                command, "--fxstack-artifact-set-sha256"
            )[0],
        }
        economic_report = Path(
            _command_values(command, "--fxstack-economic-report")[0]
        )
        economic_report.write_text(
            json.dumps(
                {
                    "schema_version": EXTERNAL_ECONOMIC_REPORT_VERSION,
                    **linkage,
                    "status": "completed",
                    "realized_pnl_usd": 150.0,
                    "unrealized_pnl_usd": 0.0,
                    "max_drawdown_pct": 2.0,
                    "turnover_lots": 1.25,
                    "trade_count": 10,
                    "partial_fill_count": 0,
                    "latency_ms_p95": 5.0,
                    "rejection_rate": 0.0,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        for raw in _command_values(command, "--fxstack-stress-report"):
            scenario, path_text = raw.split("=", 1)
            Path(path_text).write_text(
                json.dumps(
                    {
                        "schema_version": EXTERNAL_STRESS_REPORT_VERSION,
                        **linkage,
                        "scenario": scenario,
                        "status": "completed",
                        "realized_pnl_usd": 100.0,
                        "unrealized_pnl_usd": 0.0,
                        "max_drawdown_pct": 4.0,
                        "turnover_lots": 1.5,
                        "trade_count": 10,
                        "partial_fill_count": 1,
                        "latency_ms_p95": 15.0,
                        "rejection_rate": 0.01,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        return type(
            "CompletedHarness",
            (),
            {"returncode": 0, "stdout": "completed", "stderr": ""},
        )()

    return _run


def _shadow_evidence(
    *,
    identity: ReleaseEvidenceIdentity,
    model_set_id: str,
    started_at: float,
    ended_at: float,
    boot_id: str,
) -> dict[str, object]:
    poll_interval_secs = 60.0
    samples = _shadow_samples(
        start=started_at,
        end=ended_at,
        poll=poll_interval_secs,
        boot_id=boot_id,
    )
    return {
        "schema_version": "fxstack_shadow_runtime_evidence_v2",
        "producer": {"tool": "tools.shadow_dual_run", "version": "v2"},
        "started_at": started_at,
        "ended_at": ended_at,
        "evidence_identity": {
            **identity.to_dict(),
            "evidence_kind": "runtime_shadow",
            "source_kind": "production_runtime_shadow",
            "advisory_only": False,
        },
        "gates": {
            "passed": True,
            "checks": {"throughput": True, "risk": True, "operability": True},
            "throughput_delta_entries_acked": 0,
        },
        "runtime_boundary": {
            "agent_mode": "shadow",
            "broker_emission_disabled": True,
            "entry_commands_emitted": 0,
            "control_commands_emitted": 0,
            "total_commands_emitted": 0,
            "command_window_summary": {
                "schema_version": "fxstack_command_window_summary_v1",
                "window_complete": True,
                "start_ts": started_at,
                "end_ts": ended_at,
                "queried_at": ended_at,
                "total_commands": 0,
                "entry_commands": 0,
                "control_commands": 0,
                "status_counts": {},
                "command_counts": {},
            },
            "active_manifest_matches_db": True,
            "runtime_loaded_matches_db": True,
            "activation_identity_consistent": True,
            "startup_lifecycle": {
                "startup_inference_ok": True,
                "model_set_id": model_set_id,
                "pair_readiness_status": "ready",
                "has_exit_model": True,
                "has_reversal_models": True,
                "lifecycle_activation_mode": "model_driven",
                "lifecycle_ready": True,
            },
        },
        "candidate": {
            "samples": len(samples),
            "runtime_ready_seen": True,
            "feature_ready_seen": True,
        },
        "observation_coverage": {
            "poll_interval_secs": poll_interval_secs,
            "poll_attempts": len(samples),
            "successful_samples": len(samples),
            "successful_sample_ratio": 1.0,
            "runtime_ready_sample_ratio": 1.0,
            "feature_ready_sample_ratio": 1.0,
            "first_sample_at": started_at,
            "last_sample_at": ended_at,
            "observed_span_secs": ended_at - started_at,
            "max_sample_gap_secs": poll_interval_secs,
            "runtime_boot_id": boot_id,
            "continuous_boot": True,
        },
        "candidate_samples": samples,
        "baseline_samples": samples,
    }


def _attach_bound_phase5_fixture(
    tmp_path: Path,
    payload: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Attach a real v2 gate bundle and exact active-runtime evidence to a test bundle."""

    pair = str(payload["pair"]).upper()
    bundle_run_id = str(payload["bundle_run_id"])
    root = tmp_path / f"phase5_{bundle_run_id}"
    root.mkdir(parents=True, exist_ok=True)
    candidate_manifest = root / "candidate_manifest.json"
    candidate_components = dict(payload.get("artifacts") or {})
    candidate_manifest.write_text(
        json.dumps({"bundle_run_id": bundle_run_id, "components": candidate_components}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    candidate_identity = candidate_manifest_identity(
        manifest_path=candidate_manifest,
        pair=pair,
        model_set_id=bundle_run_id,
    )
    support_paths: dict[str, Path] = {}
    support_payloads: dict[str, dict[str, object]] = {
        "feature_schema": feature_contract_metadata(),
        "lineage": {
            "pair": pair,
            "dataset_fingerprint": str(payload.get("dataset_fingerprint") or "fixture-dataset"),
            "feature_set_hash": "1" * 64,
            "label_config_hash": "2" * 64,
            "risk_config_hash": "3" * 64,
            "training_config_hash": "4" * 64,
            "feature_service_version": str(payload.get("feature_service_version") or "fixture-feature"),
            "label_version": str(payload.get("label_version") or "fixture-label"),
            "risk_config_version": str(payload.get("risk_config_version") or "fixture-risk"),
        },
        "execution_metrics": {
            "status": "completed",
            "pair": pair,
            "engine": "fixture_execution_harness",
            "dataset_hash": "5" * 64,
            "feature_service_version": str(payload.get("feature_service_version") or "fixture-feature"),
            "kernel_version": "risk-kernel-v1",
            "realized_pnl_usd": 125.0,
            "trade_count": 10,
            "max_drawdown_pct": 2.0,
            "turnover_lots": 1.0,
            "latency_ms_p95": 12.0,
            "rejection_rate": 0.0,
        },
        "risk_trace_schema": {
            "schema_version": "phase3_risk_trace_schema_v1",
            "kernel_version": "risk-kernel-v1",
            "rule_order": [
                "market_data",
                "portfolio_limits",
                "position_sizing",
                "execution_admission",
            ],
        },
        "stress_harness_summary": {
            "status": "completed",
            "scenario_count": 2,
            "scenarios": [
                {"scenario": "spread_shock"},
                {"scenario": "latency_shock"},
            ],
            "dataset_hash": "5" * 64,
            "feature_service_version": str(payload.get("feature_service_version") or "fixture-feature"),
            "kernel_version": "risk-kernel-v1",
        },
    }
    for key, support_payload in support_payloads.items():
        path = root / f"{key}.json"
        path.write_text(
            json.dumps(support_payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        support_paths[key] = path
    training_paths = {
        f"training_eval:{name}": Path(path)
        for name, path in dict(payload.get("training_eval_reports") or {}).items()
    }
    refs = {
        **{key: str(path) for key, path in support_paths.items()},
        **{key: str(path) for key, path in training_paths.items()},
        "model_manifest": str(candidate_manifest),
        "backtest_summary": "",
        "shadow_runtime_evidence": "",
    }
    support_artifacts = {
        key: {"path": str(path), "sha256": file_sha256(path)}
        for key, path in {**support_paths, **training_paths}.items()
    }
    support_binding = root / "support_evidence_binding.json"
    support_binding.write_text(
        json.dumps(
            {
                "schema_version": "phase5_support_evidence_binding_v1",
                "evidence_identity": candidate_identity.to_dict(),
                "artifacts": support_artifacts,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    refs["support_evidence_binding"] = str(support_binding)
    phase5_bundle = {
        "bundle_version": "phase5_gate_bundle_v2",
        "pair": pair,
        "evidence_identity": candidate_identity.to_dict(),
        "binding_required_evidence": sorted(
            [*support_paths, *training_paths, "support_evidence_binding"]
        ),
        "evidence_refs": refs,
        "evidence_hashes": {
            "model_manifest": file_sha256(candidate_manifest),
            **{key: file_sha256(path) for key, path in support_paths.items()},
            **{key: file_sha256(path) for key, path in training_paths.items()},
            "support_evidence_binding": file_sha256(support_binding),
        },
        "research_gate": {"gate": "research_gate", "passed": False, "details": {"promotion_status": "eligible"}},
        "economic_gate": {"gate": "economic_gate", "passed": False, "details": {}},
        "operational_gate": {"gate": "operational_gate", "passed": False, "details": {}},
        "shadow_gate": {"gate": "shadow_gate", "passed": False, "details": {"prerequisites_ready": False}},
        "canary_gate": {"gate": "canary_gate", "passed": False, "details": {}},
        "canary_closeout": {"gate": "canary_closeout", "passed": False, "status": "skip", "details": {}},
    }
    phase5_bundle_path = root / "phase5_gate_bundle.json"
    phase5_bundle_path.write_text(json.dumps(phase5_bundle, indent=2, sort_keys=True), encoding="utf-8")
    payload["phase5_gates"] = {
        "phase5_gate_bundle": str(phase5_bundle_path),
        **{name: str(root / f"{name}.json") for name in (
            "research_gate",
            "economic_gate",
            "operational_gate",
            "shadow_gate",
            "canary_gate",
            "canary_closeout",
        )},
    }

    active_manifest = root / "active_models.json"
    active_manifest.write_text(
        json.dumps(
            {
                "active_model_sets": {
                    pair: {
                        "model_set_id": bundle_run_id,
                        "metadata": {
                            "bundle_run_id": bundle_run_id,
                            "promotion_status": "eligible",
                            "lifecycle_complete": True,
                            "capabilities": {"lifecycle_complete": True},
                        },
                        "artifacts": candidate_components,
                    }
                }
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    identity = active_manifest_identity(manifest_path=active_manifest, pair=pair)
    economic_evidence = root / "economic_evidence.json"
    from fxstack.backtest.harness import run_lean_harness
    from tools.assemble_phase5_economic_evidence import (
        assemble_economic_evidence,
    )

    harness_output = root / "lean_output"
    engine_report = harness_output / "lean_economic.json"
    with monkeypatch.context() as harness_patch:
        harness_patch.setenv("FXSTACK_LEAN_VERSION", "lean-test-1")
        harness_patch.setattr(
            "fxstack.backtest.harness.lean.subprocess.run",
            _external_engine_process(engine="lean"),
        )
        harness = run_lean_harness(
            bundle_dir=root,
            output_dir=harness_output,
            pair=pair,
            dataset_hash="fixture-dataset-sha",
            model_manifest_path=active_manifest,
            economic_report_path=engine_report,
            execute=True,
        )
    harness_manifest = harness_output / "lean_manifest.json"
    harness_manifest.write_text(
        json.dumps(harness.to_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    assemble_economic_evidence(
        pair=pair,
        model_manifest_path=active_manifest,
        harness_manifest_path=harness_manifest,
        economic_report_path=engine_report,
        output_path=economic_evidence,
    )
    evidence_dir = root / "finalization"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "blockers.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-04-06T00:00:00+00:00",
                "blockers": [],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (evidence_dir / "gate_summary.json").write_text(
        json.dumps({"schema_version": 1}, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    fast_shadow = root / "fast_shadow.json"
    fast_shadow.write_text(
        json.dumps(
            _shadow_evidence(
                identity=identity,
                model_set_id=bundle_run_id,
                started_at=1_000.0,
                ended_at=1_900.0,
                boot_id=f"{bundle_run_id}-fast",
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    long_shadow = root / "long_shadow.json"
    long_shadow.write_text(
        json.dumps(
            _shadow_evidence(
                identity=identity,
                model_set_id=bundle_run_id,
                started_at=10_000.0,
                ended_at=96_400.0,
                boot_id=f"{bundle_run_id}-long",
            ),
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    rollback_evidence = root / "rollback_evidence.json"
    rollback_evidence.write_text(
        json.dumps(
            {
                "schema_version": "fxstack_rollback_drill_evidence_v1",
                "status": "passed",
                "tested_at": 100_000.0,
                "evidence_identity": {
                    **identity.to_dict(),
                    "evidence_kind": "rollback_validation",
                    "source_kind": "production_rollback_drill",
                    "advisory_only": False,
                },
                "drill": {
                    "executed": True,
                    "return_code": 0,
                    "command": ["models", "rollback-drill"],
                    "runtime_disabled_during_drill": True,
                    "target_activated": True,
                    "candidate_restored": True,
                    "candidate_bundle_run_id": bundle_run_id,
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    from tools import finalize_build

    finalization_rc = finalize_build.run(
        argparse.Namespace(
            evidence_dir=str(evidence_dir),
            evidence_root=str(root),
            fast_gate_artifact=str(fast_shadow),
            shadow_artifact=str(long_shadow),
            rollback_evidence=str(rollback_evidence),
            pair=pair,
            bundle_run_id=bundle_run_id,
            model_manifest=str(active_manifest),
        )
    )
    assert finalization_rc == 0
    release_pointer = json.loads(
        (evidence_dir / "release_validation_bundle.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "model_manifest_path": str(active_manifest),
        "economic_evidence_path": str(economic_evidence),
        "release_validation_bundle_path": str(release_pointer["authority_path"]),
    }


def _install_in_memory_active_canary(
    *,
    monkeypatch: pytest.MonkeyPatch,
    release_workflow,
    pair: str,
    bundle_run_id: str,
):
    """Expose active canary state only to downstream workflow unit seams.

    Callers first exercise and assert the real ``canary_start`` fail-closed
    result. This helper deliberately does not create a signing request,
    external witness, release authority, runtime acknowledgement, or enabled
    execution egress.
    """

    package, release_dir = release_workflow.load_release_package(
        pair=pair,
        bundle_run_id=bundle_run_id,
    )
    assert package.canary_plan is not None
    package.release_status = "canary_active"
    package.canary_plan.status = "active"
    package.canary_plan.metadata = {
        **dict(package.canary_plan.metadata or {}),
        "runtime_enabled": True,
        "queue_kill_active": False,
        "queue_kill_reason": "",
        "queue_killed_at": 0.0,
    }

    def _load_test_package(*, pair: str, bundle_run_id: str = ""):
        assert str(pair).upper() == str(package.pair).upper()
        assert not bundle_run_id or bundle_run_id == str(package.bundle_run_id)
        return package, release_dir

    monkeypatch.setattr(
        release_workflow,
        "load_release_package",
        _load_test_package,
    )
    return package


def test_lineage_snapshot_is_deterministic_and_changes_with_inputs(tmp_path: Path):
    from fxstack.mlops.lineage import compute_lineage_snapshot

    data_root = tmp_path / "features"
    data_root.mkdir(parents=True, exist_ok=True)
    first = data_root / "part-000.parquet"
    first.write_text("a", encoding="utf-8")

    one = compute_lineage_snapshot(
        feature_paths=[data_root],
        feature_schema={"version": 1, "columns": ["a"]},
        label_config={"horizon": 12},
        risk_config={"promotion_policy": "balanced"},
        training_config={"epochs": 1},
        pair="EURUSD",
        timeframes={"intraday": "M5"},
        project_root=Path(__file__).resolve().parents[1],
    )
    two = compute_lineage_snapshot(
        feature_paths=[data_root],
        feature_schema={"version": 1, "columns": ["a"]},
        label_config={"horizon": 12},
        risk_config={"promotion_policy": "balanced"},
        training_config={"epochs": 1},
        pair="EURUSD",
        timeframes={"intraday": "M5"},
        project_root=Path(__file__).resolve().parents[1],
    )
    assert one.dataset_fingerprint == two.dataset_fingerprint
    assert one.feature_schema["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert one.feature_schema["session_contract_version"] == SESSION_CONTRACT_VERSION

    first.write_text("b", encoding="utf-8")
    changed = compute_lineage_snapshot(
        feature_paths=[data_root],
        feature_schema={"version": 1, "columns": ["a"]},
        label_config={"horizon": 12},
        risk_config={"promotion_policy": "balanced"},
        training_config={"epochs": 1},
        pair="EURUSD",
        timeframes={"intraday": "M5"},
        project_root=Path(__file__).resolve().parents[1],
    )
    assert changed.dataset_fingerprint != one.dataset_fingerprint


def test_model_uri_resolves_registered_alias_to_legacy_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tracking_uri = _configure_mlflow_env(tmp_path, monkeypatch)
    assert tracking_uri.startswith("sqlite:///")

    from fxstack.mlops.model_uri import resolve_model_artifact_path
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias

    bundle = import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-a"), intended_alias="champion")
    resolved_bundle = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="champion")
    assert resolved_bundle.bundle_run_id == bundle.bundle_run_id

    artifact_path = resolve_model_artifact_path(resolved_bundle.components["meta"].model_uri)
    assert (artifact_path / "meta.json").exists()
    meta = json.loads((artifact_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "meta_filter_xgb"


def test_resolve_model_artifact_path_prefers_local_path_when_mlflow_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fxstack.mlops.model_uri import resolve_model_artifact_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FXSTACK_MLFLOW_ENABLED", "0")
    get_settings.cache_clear()
    try:
        artifact_dir = Path(_make_artifact(tmp_path, "artifact_local"))
        artifact_ref = _artifact_ref(artifact_dir)
        artifact_ref["model_uri"] = "models:/fx.meta_filter.EURUSD.M5/1"
        artifact_ref["model_name"] = "fx.meta_filter.EURUSD.M5"
        artifact_ref["model_version"] = "1"

        resolved = resolve_model_artifact_path(
            artifact_ref,
            project_root=tmp_path,
        )

        assert resolved == artifact_dir.resolve()
    finally:
        get_settings.cache_clear()


def test_resolve_model_artifact_path_prefers_local_path_when_mlflow_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.model_uri import resolve_model_artifact_path

    try:
        artifact_dir = Path(_make_artifact(tmp_path, "artifact_local"))
        artifact_ref = _artifact_ref(artifact_dir)
        artifact_ref["model_uri"] = "models:/fx.meta_filter.EURUSD.M5/1"
        artifact_ref["model_name"] = "fx.meta_filter.EURUSD.M5"
        artifact_ref["model_version"] = "1"

        resolved = resolve_model_artifact_path(
            artifact_ref,
            project_root=tmp_path,
        )

        assert resolved == artifact_dir.resolve()
    finally:
        get_settings.cache_clear()


def test_shadow_alias_resolves_patchtst_components(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias

    payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-shadow-patchtst")
    patch_root = tmp_path / "artifacts_patchtst"
    payload["artifacts"]["swing_patchtst"] = _artifact_ref(
        _make_artifact(patch_root, "swing_patchtst", with_reports=True)
    )
    payload["artifacts"]["intraday_patchtst"] = _artifact_ref(
        _make_artifact(patch_root, "intraday_patchtst", with_reports=True)
    )
    import_compat_bundle_to_mlflow(payload, intended_alias="shadow")

    resolved = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="shadow")

    assert "swing_patchtst" in resolved.components
    assert "intraday_patchtst" in resolved.components
    swing_ref = resolved.components["swing_patchtst"]
    assert swing_ref.model_uri == f"models:/{swing_ref.model_name}/{swing_ref.model_version}"


def test_shadow_alias_preserves_patchtst_phase4_metadata_and_report_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias

    payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-shadow-patchtst-phase4")
    patch_root = tmp_path / "artifacts_patchtst_phase4"
    swing_path = Path(_make_artifact(patch_root, "swing_patchtst", with_reports=True))
    intraday_path = Path(_make_artifact(patch_root, "intraday_patchtst", with_reports=True))
    for artifact_path, prefix in [(swing_path, "swing"), (intraday_path, "intraday")]:
        reports = artifact_path / "reports"
        sequence_dataset_manifest = artifact_path / f"{prefix}_sequence_dataset_manifest.json"
        portfolio_report = artifact_path / f"{prefix}_portfolio_report.json"
        head_to_head = artifact_path / f"{prefix}_challenger_head_to_head.json"
        disagreement = artifact_path / f"{prefix}_portfolio_disagreement.json"
        for path in [sequence_dataset_manifest, portfolio_report, head_to_head, disagreement]:
            path.write_text("{}", encoding="utf-8")
        meta = json.loads((artifact_path / "meta.json").read_text(encoding="utf-8"))
        meta.update(
            {
                "sequence_dataset_manifest": str(sequence_dataset_manifest),
                "portfolio_report": str(portfolio_report),
                "challenger_head_to_head": str(head_to_head),
                "portfolio_disagreement": str(disagreement),
                "model_manifest": str(tmp_path / f"{prefix}_bundle_manifest.json"),
            }
        )
        (artifact_path / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        stamp_artifact_payload_digest(artifact_path)
        assert reports.exists()
    payload["phase4_shadow_only"] = True
    payload["phase4_sequence_dataset_manifests"] = {"swing_patchtst": "seq-swing.json", "intraday_patchtst": "seq-intraday.json"}
    payload["phase4_portfolio_reports"] = {"swing_patchtst": "portfolio-swing.json", "intraday_patchtst": "portfolio-intraday.json"}
    payload["phase4_challenger_reports"] = {"swing_patchtst": "head-swing.json", "intraday_patchtst": "head-intraday.json"}
    payload["artifacts"]["swing_patchtst"] = _artifact_ref(swing_path)
    payload["artifacts"]["intraday_patchtst"] = _artifact_ref(intraday_path)

    import_compat_bundle_to_mlflow(payload, intended_alias="shadow")
    resolved = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="shadow")

    assert bool(resolved.metadata["phase4_shadow_only"]) is True
    assert resolved.metadata["phase4_sequence_dataset_manifests"]["swing_patchtst"] == "seq-swing.json"
    assert resolved.metadata["phase4_portfolio_reports"]["intraday_patchtst"] == "portfolio-intraday.json"
    swing_refs = resolved.components["swing_patchtst"].evidence_refs
    assert swing_refs["training_report"].endswith("training_report.json")
    assert swing_refs["model_manifest"].endswith("bundle_manifest.json")
    assert swing_refs["sequence_dataset_manifest"].endswith("swing_sequence_dataset_manifest.json")
    assert swing_refs["portfolio_report"].endswith("swing_portfolio_report.json")


def test_activate_mlflow_alias_populates_runtime_store_and_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.training.activation import activate_mlflow_alias

    bundle = import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-live"), intended_alias="champion")
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    manifest_path = tmp_path / "active_models.json"
    activated = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=manifest_path,
        pairs=["EURUSD"],
        alias="champion",
    )
    assert str(activated[0]["model_set_id"]) == bundle.bundle_run_id

    svc = RuntimeService(database_url=db_url)
    active = svc.get_active_model_set("EURUSD")
    assert active is not None
    artifacts = dict(active.get("artifacts_json") or {})
    meta_ref = dict(artifacts["meta"] or {})
    assert str(meta_ref.get("model_uri") or "") == (
        f"models:/{meta_ref['model_name']}/{meta_ref['model_version']}"
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = payload["active_model_sets"]["EURUSD"]
    assert str(entry["metadata"]["bundle_run_id"]) == bundle.bundle_run_id
    persisted_meta_ref = dict(entry["artifacts"]["meta"])
    assert str(persisted_meta_ref["model_uri"]) == (
        f"models:/{persisted_meta_ref['model_name']}/"
        f"{persisted_meta_ref['model_version']}"
    )

    candidate_path = tmp_path / "candidate_bundle.json"
    candidate_path.write_text(json.dumps(bundle.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    candidate_identity = candidate_manifest_identity(
        manifest_path=candidate_path,
        pair="EURUSD",
        model_set_id=bundle.bundle_run_id,
    )
    active_identity = active_manifest_identity(manifest_path=manifest_path, pair="EURUSD")
    assert candidate_identity.artifact_set_sha256 == active_identity.artifact_set_sha256
    assert candidate_identity.model_manifest_sha256 == active_identity.model_manifest_sha256
    assert artifact_set_sha256(bundle.to_dict()["components"]) == artifact_set_sha256(entry["artifacts"])

    mutated_artifacts = dict(entry["artifacts"])
    mutated_artifacts["meta"] = {**dict(mutated_artifacts["meta"]), "artifact_hash": "f" * 64}
    assert artifact_set_sha256(mutated_artifacts) != active_identity.artifact_set_sha256


def test_activate_mlflow_alias_rejects_runtime_incompatible_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.training.activation import activate_mlflow_alias

    payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-incompatible")
    payload["runtime_compatible"] = False
    import_compat_bundle_to_mlflow(payload, intended_alias="champion")
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    with pytest.raises(ValueError, match="runtime_incompatible"):
        activate_mlflow_alias(
            database_url=db_url,
            manifest_path=tmp_path / "active_models.json",
            pairs=["EURUSD"],
            alias="champion",
        )


def test_backfill_and_alias_reassignment_supports_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, set_bundle_alias
    from fxstack.training.activation import activate_mlflow_alias, backfill_mlflow_state

    champion_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-champion")
    shadow_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-shadow")
    champion_registry = tmp_path / "registry" / "eurusd_champion.json"
    champion_registry.parent.mkdir(parents=True, exist_ok=True)
    champion_registry.write_text(json.dumps(champion_payload, indent=2), encoding="utf-8")
    shadow_registry = tmp_path / "artifacts_shadow" / "registry_full_20260405_1200_manual" / "eurusd_shadow.json"
    shadow_registry.parent.mkdir(parents=True, exist_ok=True)
    shadow_registry.write_text(json.dumps(shadow_payload, indent=2), encoding="utf-8")

    active_manifest = tmp_path / "active_models.json"
    active_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_model_sets": {
                    "EURUSD": {
                        "model_set_id": "bundle-champion",
                        "registry_path": str(champion_registry),
                        "artifacts": champion_payload["artifacts"],
                        "policies": champion_payload["policies"],
                        "metadata": champion_payload,
                        "enabled": True,
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    backfill = backfill_mlflow_state(
        active_manifest_path=active_manifest,
        registry_root=champion_registry.parent,
        shadow_root=tmp_path / "artifacts_shadow",
    )
    assert bool(backfill.get("ok")) is True
    assert "EURUSD" in backfill["active_pairs"]
    assert "EURUSD" in backfill["shadow_pairs"]

    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    champion_active = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=tmp_path / "activated_models.json",
        pairs=["EURUSD"],
        alias="champion",
    )
    shadow_active = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=tmp_path / "activated_models_shadow.json",
        pairs=["EURUSD"],
        alias="shadow",
    )
    assert champion_active[0]["model_set_id"] != shadow_active[0]["model_set_id"]

    old_bundle = import_compat_bundle_to_mlflow(champion_payload, intended_alias="")
    moved = set_bundle_alias(bundle=old_bundle, alias="champion")
    assert bool(moved.get("ok")) is True

    rolled_back = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=tmp_path / "activated_models_rollback.json",
        pairs=["EURUSD"],
        alias="champion",
    )
    assert rolled_back[0]["model_set_id"] == champion_active[0]["model_set_id"]


def test_phase5_release_workflow_stages_canaries_graduates_and_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias
    from fxstack.training import release_workflow
    from fxstack.training.release_workflow import (
        canary_start,
        close_canary,
        promote_release,
        release_status,
        rollback_release,
        shadow_accept,
        stage_release,
    )

    champion_bundle = import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-p5-champion"), intended_alias="champion")
    shadow_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-p5-shadow")
    shadow_payload["metadata"] = {
        **dict(shadow_payload.get("metadata") or {}),
        "experiment_id": "exp-phase5-001",
        "promotion_id": "promo-phase5-001",
    }
    release_evidence = _attach_bound_phase5_fixture(
        tmp_path, shadow_payload, monkeypatch
    )
    shadow_bundle = import_compat_bundle_to_mlflow(shadow_payload, intended_alias="shadow")

    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    manifest_path = tmp_path / "active_models.json"

    staged = stage_release(pair="EURUSD", alias="shadow", author="ops", allowlisted_pairs=["EURUSD"])
    assert bool(staged.get("ok")) is True
    activation_package_path = Path(staged["activation_package"])
    activation_package = json.loads(activation_package_path.read_text(encoding="utf-8"))
    assert activation_package["experiment_id"] == "exp-phase5-001"
    assert activation_package["promotion_id"] == "promo-phase5-001"
    assert Path(activation_package["experiment_lineage_ref"]).exists()
    assert Path(activation_package["paper_pack_ref"]).exists()
    assert Path(activation_package["canary_pack_ref"]).exists()
    assert Path(activation_package["rollback_plan_ref"]).exists()
    assert activation_package["evidence_refs"]["experiment_lineage"] == activation_package["experiment_lineage_ref"]

    promoted = promote_release(pair="EURUSD", author="ops")
    assert promoted["release_status"] == "staged"
    assert "ops" in promoted["signed_off_by"]

    shadow_ok = shadow_accept(
        pair="EURUSD",
        database_url=db_url,
        activation_manifest_path=manifest_path,
        **release_evidence,
    )
    assert bool(shadow_ok.get("ok")) is True
    assert shadow_ok["release_status"] == "shadow_accepted"
    assert shadow_ok["shadow_acceptance_summary"]["ready"] is True
    assert shadow_ok["phase5_gate_summary"]["all_required_passed"] is True

    started = canary_start(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
    )
    assert bool(started.get("ok")) is False
    assert started["error"] == "canary_start_blocked"
    assert "external_release_witness_missing" in started["blockers"]
    assert "canonical_release_signing_request_missing" in started["blockers"]
    assert started["release_status"] == "shadow_accepted"
    assert resolve_bundle_manifest_by_alias(
        pair="EURUSD", alias="champion"
    ).bundle_run_id == champion_bundle.bundle_run_id

    # The lifecycle operations below have their own unit boundary. Feed them
    # explicit in-memory state without weakening or bypassing canary admission.
    test_package = _install_in_memory_active_canary(
        monkeypatch=monkeypatch,
        release_workflow=release_workflow,
        pair="EURUSD",
        bundle_run_id=str(started["bundle_run_id"]),
    )
    canary_prep = release_workflow.canary_prep_metadata(test_package)
    assert canary_prep["status"] == "active"
    assert canary_prep["allowlisted_pairs"] == ["EURUSD"]
    assert canary_prep["experiment_id"] == "exp-phase5-001"
    assert canary_prep["promotion_id"] == "promo-phase5-001"
    assert Path(canary_prep["experiment_lineage_ref"]).exists()
    assert Path(canary_prep["paper_pack_ref"]).exists()
    assert Path(canary_prep["canary_pack_ref"]).exists()
    assert Path(canary_prep["rollback_plan_ref"]).exists()

    graduated = close_canary(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        outcome="graduate",
    )
    assert bool(graduated.get("ok")) is True
    assert graduated["release_status"] == "graduated"
    resolved_champion = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="champion")
    assert resolved_champion.bundle_run_id == shadow_bundle.bundle_run_id

    rolled_back = rollback_release(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        reason="test_rollback",
    )
    assert bool(rolled_back.get("ok")) is True
    assert rolled_back["release_status"] == "rolled_back"
    restored = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="champion")
    assert restored.bundle_run_id == champion_bundle.bundle_run_id

    status = release_status(pair="EURUSD", database_url=db_url)
    assert bool(status.get("ok")) is True
    assert status["release_status"] == "rolled_back"
    assert status["shadow_acceptance_summary"]["release_status"] == "rolled_back"
    assert status["canary_prep"]["status"] == "rolled_back"


def test_canary_start_blocks_when_release_is_not_shadow_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release_root = tmp_path / "releases"
    package_dir = release_root / "eurusd" / "bundle-canary-blocked"
    package_dir.mkdir(parents=True, exist_ok=True)
    package_dir.joinpath("activation_package.json").write_text(
        json.dumps(
            {
                "schema_version": "phase5_activation_package_v1",
                "bundle_run_id": "bundle-canary-blocked",
                "pair": "EURUSD",
                "target_alias": "shadow",
                "model_alias": "shadow",
                "release_status": "staged",
                "promotion_status": "eligible",
                "runtime_compatible": True,
                "canary_plan": {
                    "plan_id": "eurusd-bundle-canary-blocked-canary",
                    "scope": "pair_allowlist",
                    "status": "planned",
                    "traffic_fraction": 1.0,
                    "duration_minutes": 60,
                    "metrics_window_minutes": 60,
                    "success_criteria": {
                        "latency_budget_ms": 5000.0,
                        "stale_feature_limit": 1,
                        "drawdown_limit_pct": 5.0,
                        "calibration_drift_limit": 0.05,
                    },
                    "abort_conditions": [
                        "latency_breach",
                        "stale_features",
                        "rollout_breach",
                        "drawdown_breach",
                        "calibration_drift",
                    ],
                    "metadata": {"allowlisted_pairs": ["EURUSD"], "budget_scale": 0.25},
                },
                "promotion_gates": [],
                "evidence_refs": {},
                "metadata": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("FXSTACK_PHASE5_RELEASE_ROOT", str(release_root))
    get_settings.cache_clear()
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    from fxstack.training import release_workflow

    called = {"activate": False}

    def _unexpected_activate(*args, **kwargs):
        called["activate"] = True
        raise AssertionError("activate_mlflow_alias should not be called when canary start is blocked")

    monkeypatch.setattr(release_workflow, "activate_mlflow_alias", _unexpected_activate)

    try:
        blocked = release_workflow.canary_start(
            pair="EURUSD",
            database_url=db_url,
            manifest_path=tmp_path / "active_models.json",
            bundle_run_id="bundle-canary-blocked",
        )
        status = release_workflow.release_status(
            pair="EURUSD",
            database_url=db_url,
            bundle_run_id="bundle-canary-blocked",
        )
    finally:
        get_settings.cache_clear()

    assert called["activate"] is False
    assert bool(blocked["ok"]) is False
    assert blocked["error"] == "canary_start_blocked"
    assert "release_status:staged" in blocked["blockers"]
    assert "canary_plan_status:planned" in blocked["blockers"]
    assert any(str(item).startswith("missing_phase5_gates:") for item in blocked["blockers"])
    assert blocked["shadow_acceptance_summary"]["ready"] is False
    assert blocked["canary_prep"]["status"] == "planned"
    assert status["canary_ready"] is False
    assert "release_status:staged" in status["canary_blockers"]


def test_runtime_strategy_state_omits_retired_challenger_conflict() -> None:
    from fxstack.training.release_workflow import _runtime_strategy_state

    out = _runtime_strategy_state(
        {
            "runtime_diag": {
                "strategy_engine_mode": "rl_primary",
                "supervised_fallback": {"enabled": True},
                "challenger_conflict": {"mode": "hard_gate"},
            }
        }
    )

    assert out["strategy_engine_mode"] == "rl_primary"
    assert out["supervised_fallback"]["enabled"] is True
    assert "challenger_conflict" not in out


def test_canary_monitor_surfaces_pair_readiness_blockers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    monkeypatch.setenv("FXSTACK_PHASE5_AUTO_ROLLBACK", "0")
    get_settings.cache_clear()
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.training import release_workflow
    from fxstack.training.release_workflow import canary_start, monitor_canary, release_status, shadow_accept, stage_release

    import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-pair-readiness-champion"), intended_alias="champion")
    shadow_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-pair-readiness-shadow")
    release_evidence = _attach_bound_phase5_fixture(
        tmp_path, shadow_payload, monkeypatch
    )
    import_compat_bundle_to_mlflow(shadow_payload, intended_alias="shadow")

    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    manifest_path = tmp_path / "active_models.json"

    stage_release(pair="EURUSD", alias="shadow", author="ops", allowlisted_pairs=["EURUSD"])
    shadow_accept(
        pair="EURUSD",
        database_url=db_url,
        activation_manifest_path=manifest_path,
        **release_evidence,
    )
    started = canary_start(pair="EURUSD", database_url=db_url, manifest_path=manifest_path)
    assert bool(started.get("ok")) is False
    assert started["error"] == "canary_start_blocked"
    assert "external_release_witness_missing" in started["blockers"]
    assert "canonical_release_signing_request_missing" in started["blockers"]
    _install_in_memory_active_canary(
        monkeypatch=monkeypatch,
        release_workflow=release_workflow,
        pair="EURUSD",
        bundle_run_id=str(started["bundle_run_id"]),
    )

    svc = RuntimeService(database_url=db_url)
    svc.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": 1775433600.0,
            "symbol_readiness": {"EURUSD": {"supported": True, "broker_symbol": "EURUSD"}},
            "runtime_diag": {
                "loop_latency_ms": 25.0,
                "strategy_engine_mode": "rl_primary",
                "supervised_fallback": {"enabled": True, "fallback_count": 1, "fallback_reasons": ["signal_fallback"], "primary_reason": "signal_fallback"},
                "challenger_conflict": {
                    "mode": "hard_gate",
                    "active": True,
                    "max_gap": 0.44,
                    "active_pairs": ["EURUSD"],
                    "verdict_counts": {"hard_conflict": 1},
                    "dominant_verdict": "hard_conflict",
                },
                "feature_serving": {
                    "source": "feast_online",
                    "stale": True,
                    "reason": "stale",
                },
                "feature_serving_by_pair": {
                    "EURUSD:M5": {
                        "source": "feast_online",
                        "stale": True,
                        "reason": "stale",
                    }
                },
                "startup_inference": {
                    "EURUSD": {
                        "ok": False,
                        "reason": "model_load_timeout",
                    }
                },
                "pair_readiness": {
                    "EURUSD": {
                        "pair": "EURUSD",
                        "ready": False,
                        "status": "blocked",
                        "reason": "startup_inference:model_load_timeout",
                        "blockers": ["startup_inference:model_load_timeout", "feature_serving:stale"],
                        "startup_inference_ok": False,
                        "feature_serving_source": "feast_online",
                        "feature_serving_stale": True,
                        "symbol_supported": True,
                    }
                },
                "entry_execution_policy": {
                    "execution_mode": "rl_primary",
                    "strategy_engine_mode": "rl_primary",
                    "rl_checkpoint_loaded": True,
                    "rl_checkpoint_path": "mlruns/eurusd/rl.chkpt",
                    "rl_proposal_source": "rl_checkpoint",
                    "rl_routed_entry_count": 4,
                    "rl_blocked_entry_count": 1,
                    "rl_fallback_entry_count": 2,
                    "rl_scaled_entry_count": 1,
                    "rl_lifecycle_reviewed_count": 6,
                    "rl_lifecycle_applied_count": 3,
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
                            "action": {"target_position": 0.5, "close_position": False, "tighten_stop": False},
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
                "risk_cycle_summary": {"rollout": {"breach_count": 0}},
            },
        }
    )

    monitor = monitor_canary(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        bundle_run_id=str(started["bundle_run_id"]),
    )
    assert monitor["status"] == "breach"
    assert monitor["pair_readiness"]["status"] == "blocked"
    assert any(str(item).startswith("pair_readiness:") for item in monitor["breaches"])
    assert monitor["strategy_state"]["strategy_engine_mode"] == "rl_primary"
    assert monitor["strategy_state"]["supervised_fallback"]["enabled"] is True
    assert "challenger_conflict" not in monitor["strategy_state"]
    assert monitor["runtime_rl_state"]["checkpoint_loaded"] is True
    assert monitor["runtime_rl_state"]["proposal_source"] == "rl_checkpoint"
    assert monitor["runtime_rl_state"]["routed_entry_count"] == 4
    assert monitor["runtime_rl_state"]["fallback_entry_count"] == 2
    assert monitor["runtime_rl_state"]["pair_universe"] == ["EURUSD"]
    assert monitor["runtime_rl_state"]["lifecycle_summary"]["applied_count"] == 3
    assert monitor["runtime_rl_state"]["artifact_readiness"]["ready"] is True

    status = release_status(pair="EURUSD", database_url=db_url, bundle_run_id=str(started["bundle_run_id"]))
    assert status["canary_ready"] is False
    assert status["runtime_pair_readiness"]["status"] == "blocked"
    assert any(str(item).startswith("runtime_pair_readiness:") for item in status["canary_blockers"])
    assert status["strategy_state"]["strategy_engine_mode"] == "rl_primary"
    assert status["runtime_rl_state"]["checkpoint_loaded"] is True
    assert status["runtime_rl_state"]["proposal_source"] == "rl_checkpoint"
    assert status["runtime_rl_state"]["routed_entry_count"] == 4
    assert status["runtime_rl_state"]["fallback_entry_count"] == 2
    assert status["runtime_rl_state"]["rebalance_summary"]["exit_count"] == 1
    assert status["runtime_rl_state"]["flip_intent"]["non_flat_target_count"] == 1


def test_phase6b_command_metrics_use_event_ts_and_bound_future_skew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.training import release_workflow

    class _CommandMetricService:
        events = [
            {
                "command_id": "entry-1",
                "event_status": "acked",
                # command_events expose `ts`; a misleading legacy-shaped
                # created_at must neither be required nor trusted.
                "ts": 995.0,
                "created_at": 1.0,
            }
        ]

        def get_commands(self, *, limit: int):
            assert limit == 500
            return [
                {
                    "command_id": "entry-1",
                    "symbol": "EURUSD",
                    "created_at": 990.0,
                    "status": "acked",
                    "orchestration_meta_json": {"agent_mode": "live"},
                }
            ]

        def get_command_events(self, *, limit: int):
            assert limit == 2000
            return list(self.events)

    monkeypatch.setattr(release_workflow, "_now_ts", lambda: 1000.0)
    svc = _CommandMetricService()
    metrics = release_workflow._orchestration_live_command_metrics(
        svc=svc,
        pair="EURUSD",
        alert_window_minutes=1,
    )
    assert metrics == {
        "command_count": 1,
        "ack_success_rate": 1.0,
        "ack_timeout_rate": 0.0,
        "orphan_command_count": 0,
    }

    svc.events = [
        {
            "command_id": "entry-1",
            "event_status": "acked",
            "ts": 900.0,
            "created_at": 999.0,
        }
    ]
    expired_metrics = release_workflow._orchestration_live_command_metrics(
        svc=svc,
        pair="EURUSD",
        alert_window_minutes=1,
    )
    assert expired_metrics["ack_success_rate"] == 0.0
    assert expired_metrics["ack_timeout_rate"] == 0.0
    assert expired_metrics["orphan_command_count"] == 0
    assert release_workflow._timestamp_age_in_window(
        1004.0,
        now_ts=1000.0,
        window_secs=60.0,
    ) == 0.0
    assert release_workflow._timestamp_age_in_window(
        1006.0,
        now_ts=1000.0,
        window_secs=60.0,
    ) is None


def test_phase6b_live_canary_requires_pack_to_advance_and_queue_kills_on_breach(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    monkeypatch.setenv("FXSTACK_PHASE5_AUTO_ROLLBACK", "0")
    monkeypatch.setenv("FXSTACK_AGENT_MODE", "live")
    monkeypatch.setenv("FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST", "EURUSD")
    monkeypatch.setenv("FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST", "trend")
    monkeypatch.setenv("FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST", "enter")
    monkeypatch.setenv("FXSTACK_PHASE6B_CANARY_DRAWDOWN_DETERIORATION_PCT", "1.5")
    monkeypatch.setenv("FXSTACK_PHASE6B_CANARY_ALERT_WINDOW_MINUTES", "1")
    get_settings.cache_clear()

    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.training import release_workflow
    from fxstack.training.release_workflow import (
        advance_canary_stage,
        canary_start,
        monitor_canary,
        promote_release,
        release_status,
        shadow_accept,
        stage_release,
    )

    import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-p6b-champion"), intended_alias="champion")
    shadow_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-p6b-shadow")
    release_evidence = _attach_bound_phase5_fixture(
        tmp_path, shadow_payload, monkeypatch
    )
    import_compat_bundle_to_mlflow(shadow_payload, intended_alias="shadow")

    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    manifest_path = tmp_path / "active_models.json"

    staged = stage_release(pair="EURUSD", alias="shadow", author="ops")
    assert staged["canary_prep"]["mode"] == "orchestration_live"
    assert staged["canary_prep"]["live_pair_allowlist"] == ["EURUSD"]
    assert staged["canary_prep"]["live_sleeve_allowlist"] == ["trend"]
    assert staged["canary_prep"]["current_stage_pct"] == 1

    promote_release(pair="EURUSD", author="ops")
    shadow_accept(
        pair="EURUSD",
        database_url=db_url,
        activation_manifest_path=manifest_path,
        **release_evidence,
    )
    started = canary_start(pair="EURUSD", database_url=db_url, manifest_path=manifest_path)
    assert bool(started.get("ok")) is False
    assert started["error"] == "canary_start_blocked"
    assert "external_release_witness_missing" in started["blockers"]
    assert "canonical_release_signing_request_missing" in started["blockers"]
    test_package = _install_in_memory_active_canary(
        monkeypatch=monkeypatch,
        release_workflow=release_workflow,
        pair="EURUSD",
        bundle_run_id=str(started["bundle_run_id"]),
    )
    assert test_package.canary_plan is not None
    assert test_package.canary_plan.metadata["runtime_enabled"] is True
    assert test_package.canary_plan.metadata["current_stage_pct"] == 1

    blocked = advance_canary_stage(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        promotion_pack_path=str(tmp_path / "missing-pack.md"),
        author="ops",
    )
    assert blocked["ok"] is False
    assert blocked["error"] == "promotion_pack_missing"

    promotion_pack = tmp_path / "promotion-pack.md"
    promotion_pack.write_text("# Signed Promotion Pack\n", encoding="utf-8")
    evidence_blocked = advance_canary_stage(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        promotion_pack_path=str(promotion_pack),
        author="ops",
    )
    assert evidence_blocked["ok"] is False
    assert evidence_blocked["error"] == "canary_monitor_evidence_required"
    svc = RuntimeService(database_url=db_url)
    evidence_observed_at = time.time()
    svc.patch_state(
        {
            "runtime_status": "running",
            "runtime_diag": {
                "pair_readiness": {
                    "EURUSD": {
                        "pair": "EURUSD",
                        "ready": True,
                        "status": "ready",
                        "reason": "ok",
                        "blockers": [],
                    }
                },
                "orchestration_live": {
                    "enabled": True,
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "current_stage_index": 0,
                    "current_stage_pct": 1,
                    "budget_scale": 0.01,
                    "p95_ms": 90.0,
                    "p99_ms": 120.0,
                    # Aggregate evidence is deliberately below the floor;
                    # EURUSD must be judged only by its own release ledger.
                    "entry_ratio_vs_baseline": 0.5,
                    "entry_ratio_evaluable": True,
                    "entry_ratio_approved_count": 2,
                    "entry_ratio_submitted_count": 2,
                    "entry_ratio_accepted_count": 1,
                    "entry_ratio_observed_at": evidence_observed_at,
                    "entry_ratio_stage_index": 0,
                    "entry_ratio_stage_pct": 1,
                    "entry_evidence_by_pair": {
                        "EURUSD": {
                            "pair": "EURUSD",
                            "bundle_run_id": str(started["bundle_run_id"]),
                            "stage_index": 0,
                            "stage_pct": 1,
                            "approved_action_keys": ["EURUSD:stage0:entry1"],
                            "submitted_action_keys": ["EURUSD:stage0:entry1"],
                            "accepted_action_keys": ["EURUSD:stage0:entry1"],
                            "approved_count": 1,
                            "submitted_count": 1,
                            "accepted_count": 1,
                            "entry_ratio_vs_baseline": 1.0,
                            "entry_ratio_evaluable": True,
                            "observed_at": evidence_observed_at,
                        },
                        "GBPUSD": {
                            "pair": "GBPUSD",
                            "bundle_run_id": str(started["bundle_run_id"]),
                            "stage_index": 0,
                            "stage_pct": 1,
                            "approved_action_keys": ["GBPUSD:stage0:entry1"],
                            "submitted_action_keys": ["GBPUSD:stage0:entry1"],
                            "accepted_action_keys": [],
                            "approved_count": 1,
                            "submitted_count": 1,
                            "accepted_count": 0,
                            "entry_ratio_vs_baseline": 0.0,
                            "entry_ratio_evaluable": True,
                            "observed_at": evidence_observed_at,
                        }
                    },
                    "slot_utilisation_vs_baseline": 1.0,
                    "drawdown_deterioration_pct": 0.1,
                },
            },
        }
    )
    with monkeypatch.context() as time_context:
        time_context.setattr(
            release_workflow,
            "_now_ts",
            lambda: evidence_observed_at + 61.0,
        )
        expired_observation = monitor_canary(
            pair="EURUSD",
            database_url=db_url,
            manifest_path=manifest_path,
            bundle_run_id=str(started["bundle_run_id"]),
        )
    assert expired_observation["status"] == "insufficient_evidence"
    assert expired_observation["evidence_metrics"] == {
        "entry_ratio_evidence_age_secs": pytest.approx(61.0),
        "entry_ratio_evidence_window_secs": 60.0,
        "entry_ratio_evidence_within_window": False,
        "entry_ratio_evidence_future_skew_valid": True,
        "entry_ratio_evidence_fresh": False,
        "entry_ratio_evidence_stage_matches": True,
    }
    observed = monitor_canary(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        bundle_run_id=str(started["bundle_run_id"]),
    )
    assert observed["status"] == "ok"
    assert observed["evidence_metrics"]["entry_ratio_evidence_within_window"] is True
    assert observed["evidence_metrics"]["entry_ratio_evidence_fresh"] is True
    advanced = advance_canary_stage(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        promotion_pack_path=str(promotion_pack),
        author="ops",
    )
    assert advanced["ok"] is True
    assert advanced["current_stage_pct"] == 5
    assert advanced["canary_prep"]["current_stage_pct"] == 5
    reused_observation = monitor_canary(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        bundle_run_id=str(started["bundle_run_id"]),
    )
    assert reused_observation["status"] == "insufficient_evidence"
    stale_evidence = advance_canary_stage(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        promotion_pack_path=str(promotion_pack),
        author="ops",
    )
    assert stale_evidence["ok"] is False
    assert stale_evidence["error"] == "canary_monitor_evidence_required"

    svc = RuntimeService(database_url=db_url)
    svc.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": 1775433600.0,
            "runtime_diag": {
                "pair_readiness": {
                    "EURUSD": {
                        "pair": "EURUSD",
                        "ready": True,
                        "status": "ready",
                        "reason": "ok",
                        "blockers": [],
                    }
                },
                "orchestration_live": {
                    "enabled": True,
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "current_stage_index": 1,
                    "current_stage_pct": 5,
                    "budget_scale": 0.05,
                    "p95_ms": 90.0,
                    "p99_ms": 120.0,
                    "entry_ratio_vs_baseline": 1.0,
                    "entry_ratio_evaluable": True,
                    "entry_ratio_observed_at": time.time(),
                    "entry_ratio_stage_index": 1,
                    "entry_ratio_stage_pct": 5,
                    "slot_utilisation_vs_baseline": 1.0,
                    "drawdown_deterioration_pct": 0.1,
                    "graph_fault_count": 0,
                    "repeated_graph_fault_count": 0,
                    "trace_persistence_failure_count": 0,
                    "baseline_fallback_count": 0,
                },
                "risk_cycle_summary": {"rollout": {"breach_count": 0}},
            },
        }
    )
    denied, denied_code = svc.submit_command(
        {
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "sl_price": 0.9,
            "tp_price": 1.0,
            "command_id": "p6b-live-delivered",
            "intent": "ENTRY_MODEL",
            "correlation_id": "EURUSD:p6b:delivered",
            "thread_id": "EURUSD:p6b:delivered",
            "idempotency_key": "p6b-idem-delivered",
            "schema_version": "orchestration.phase4.v1",
            "orchestration_meta_json": {"agent_mode": "live", "run_id": "p6b-run-1", "trace_id": "p6b-trace-2"},
        }
    )
    assert denied_code == 403
    assert denied["error"] == "release_authority_invalid"

    # Queue-kill monitoring consumes historical command/event rows. Insert
    # those rows directly into this isolated database so the fixture cannot
    # be mistaken for broker admission or manufacture signed authority.
    command_ts = time.time()
    historical_rows = [
        {
            "command_id": "p6b-live-delivered",
            "session_id": "phase6b-monitor-fixture",
            "proto": "v2",
            "cmd": "BUY",
            "symbol": "EURUSD",
            "lots": 0.1,
            "tp_cash": None,
            "tp_price": 1.0,
            "sl_price": 0.9,
            "magic": 246810,
            "intent": "ENTRY_MODEL",
            "trace_id": "p6b-trace-2",
            "correlation_id": "EURUSD:p6b:delivered",
            "thread_id": "EURUSD:p6b:delivered",
            "idempotency_key": "p6b-idem-delivered",
            "schema_version": "orchestration.phase4.v1",
            "orchestration_meta_json": {
                "agent_mode": "live",
                "run_id": "p6b-run-1",
                "trace_id": "p6b-trace-2",
            },
            "status": "delivered",
            "created_at": command_ts,
            "updated_at": command_ts,
            "expires_at": command_ts + 600.0,
            "delivered_count": 1,
            "reason": "historical_monitor_fixture",
            "payload_json": {},
            "ack_json": {},
        },
        {
            "command_id": "p6b-live-1",
            "session_id": "phase6b-monitor-fixture",
            "proto": "v2",
            "cmd": "CLOSE",
            "symbol": "USDCHF",
            "lots": 0.1,
            "tp_cash": None,
            "tp_price": None,
            "sl_price": None,
            "magic": 246810,
            "intent": "LIFECYCLE_EXIT",
            "trace_id": "p6b-trace-1",
            "correlation_id": "USDCHF:p6b:1",
            "thread_id": "USDCHF:p6b:1",
            "idempotency_key": "p6b-idem-1",
            "schema_version": "orchestration.phase4.v1",
            "orchestration_meta_json": {
                "agent_mode": "live",
                "run_id": "p6b-run-1",
                "trace_id": "p6b-trace-1",
            },
            "status": "queued",
            "created_at": command_ts,
            "updated_at": command_ts,
            "expires_at": command_ts + 600.0,
            "delivered_count": 0,
            "reason": "historical_monitor_fixture",
            "payload_json": {},
            "ack_json": {},
        },
    ]
    with svc.store.engine.begin() as conn:
        conn.execute(svc.store.commands.insert(), historical_rows)
        conn.execute(
            svc.store.command_events.insert(),
            [
                {
                    "command_id": row["command_id"],
                    "event_status": row["status"],
                    "reason": "historical_monitor_fixture",
                    "ts": command_ts,
                    "event_json": {},
                }
                for row in historical_rows
            ],
        )

    monitor = monitor_canary(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        bundle_run_id=str(started["bundle_run_id"]),
    )
    assert monitor["status"] == "breach"
    assert "ack_timeout_spike" in monitor["breaches"]
    assert "orphan_commands" in monitor["breaches"]
    assert monitor["control_action"] == "queue_kill"
    assert monitor["orchestration_live"]["queue_kill_active"] is True
    assert monitor["orchestration_live"]["runtime_enabled"] is False
    queued_row = svc.get_command("p6b-live-1")
    delivered_row = svc.get_command("p6b-live-delivered")
    assert queued_row is not None
    assert delivered_row is not None
    assert str(queued_row["status"]) == "expired"
    assert str(delivered_row["status"]) == "delivered"

    live_state = dict(dict(svc.get_state().get("runtime_diag") or {}).get("orchestration_live") or {})
    assert live_state["queue_kill_active"] is True
    assert live_state["runtime_enabled"] is False

    status = release_status(pair="EURUSD", database_url=db_url, bundle_run_id=str(started["bundle_run_id"]))
    assert status["canary_prep"]["current_stage_pct"] == 5
    assert status["canary_prep"]["queue_kill_active"] is True

    get_settings.cache_clear()
