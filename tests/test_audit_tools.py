from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools import finalize_build
from tools import full_process_audit
from tools import dukascopy_coverage_gate
from tools import live_stack_check

REPO_ROOT = Path(__file__).resolve().parents[1]


def _shadow_samples(*, start: float, end: float, poll: float, boot_id: str) -> list[dict[str, object]]:
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


def test_full_process_audit_bootstrap_writes_expected_artifacts(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "fx-quant-stack" / "scripts").mkdir(parents=True)

    monkeypatch.setattr(full_process_audit, "_repo_root", lambda: repo)
    monkeypatch.setattr(
        full_process_audit,
        "_collect_metadata",
        lambda _root: {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "git": {"sha": "deadbeef", "ok": True},
            "versions": {"python": "3.11.0", "node": "v22", "pnpm": "10", "uv": "0.10"},
            "env": {},
        },
    )

    def _fake_run_command(**kwargs):
        log_file = Path(kwargs["logs_dir"]) / f"{kwargs['name']}.log"
        log_file.write_text("ok\n", encoding="utf-8")
        return full_process_audit.CommandResult(
            name=str(kwargs["name"]),
            command="echo ok",
            cwd=str(kwargs["cwd"]),
            return_code=0,
            passed=True,
            duration_secs=0.01,
            log_file=str(log_file),
        )

    monkeypatch.setattr(full_process_audit, "_run_command", _fake_run_command)

    args = argparse.Namespace(
        evidence_root=str(repo / "docs" / "audit"),
        baseline_url="",
        candidate_url="",
        profile="balanced",
        skip_static_checks=True,
        skip_frontend=True,
        strict=False,
    )
    rc = full_process_audit.run(args)
    assert rc == 0

    evidence_dirs = sorted((repo / "docs" / "audit").glob("*_full_process"))
    assert evidence_dirs
    evidence = evidence_dirs[-1]
    for rel in (
        "metadata.json",
        "master_report.md",
        "blockers.json",
        "gate_summary.json",
        "go_no_go.json",
        "cutover_checklist.md",
        "rollback_runbook.md",
    ):
        assert (evidence / rel).exists(), rel

    report = (evidence / "master_report.md").read_text(encoding="utf-8")
    assert "Read-only repository/toolchain metadata" in report
    assert "Baseline freeze command" not in report
    assert "python tools/shadow_dual_run.py" in report
    assert "--pair <PAIR>" in report
    assert "--model-manifest <ISOLATED_MODEL_MANIFEST>" in report
    assert "--out-dir <ISOLATED_EVIDENCE_DIR>" in report
    assert "external isolated validation host or VM" in report
    assert "src.trader.cli" not in report
    assert "--require-nonzero-entries" not in report


def test_full_process_audit_metadata_records_env_presence_without_values(tmp_path: Path, monkeypatch) -> None:
    secret = "must-not-enter-audit-evidence"
    monkeypatch.setenv("FXSTACK_BRIDGE_API_KEY", secret)
    monkeypatch.setenv("FXSTACK_DATABASE_URL", f"postgresql://user:{secret}@localhost/fx")
    monkeypatch.setenv("UNRELATED_SECRET", secret)
    monkeypatch.setattr(full_process_audit, "_run_output", lambda *_args, **_kwargs: (0, "ok"))

    metadata = full_process_audit._collect_metadata(tmp_path)

    assert "launcher_defaults" not in metadata
    assert metadata["env"]["FXSTACK_BRIDGE_API_KEY"] == {"present": True, "nonempty": True}
    assert metadata["env"]["FXSTACK_DATABASE_URL"] == {"present": True, "nonempty": True}
    assert "UNRELATED_SECRET" not in metadata["env"]
    assert secret not in json.dumps(metadata)


def test_full_process_audit_static_gate_names_only_current_root_tests() -> None:
    source = Path(full_process_audit.__file__).read_text(encoding="utf-8")
    for current in (
        "tests/test_trader_cli.py",
        "tests/test_public_docs_contract.py",
        "tests/test_agent_nav_audit.py",
        "tests/test_audit_tools.py",
    ):
        assert current in source
    for retired in (
        "tests/test_runtime_service_v2.py",
        "tests/test_decision_pipeline.py",
        "tests/test_trader_cli_fxstack_commands.py",
    ):
        assert retired not in source


def test_full_process_audit_has_no_legacy_runtime_snapshot_dependency() -> None:
    source = Path(full_process_audit.__file__).read_text(encoding="utf-8")
    parser = full_process_audit.build_parser()
    args = parser.parse_args([])

    assert "freeze_baseline.py" not in source
    assert not hasattr(args, "runtime_db")
    assert not hasattr(args, "audit_dir")
    assert args.baseline_url == ""
    assert args.candidate_url == ""


def test_ad_hoc_machine_pinned_and_authority_bypassing_tools_are_absent() -> None:
    for retired in (
        "tools/backfill_fx_data.py",
        "tools/backtest_agentic_reactions.py",
        "tools/baseline_freeze.py",
        "tests/test_baseline_freeze_tool.py",
        "tools/build_realdata_selfcorrect_dataset.py",
        "tools/autonomous_improve_loop.py",
        "tools/compare_research_runs.py",
        "tools/capture_baseline_pack.py",
        "tools/dual_run_compare.py",
        "tests/test_dual_run_compare_tool.py",
        "tools/enable_all_pairs_canary.py",
        "tools/fetch_pvsclc_dual_side_volume_snapshot.py",
        "tests/test_fetch_pvsclc_dual_side_volume_snapshot.py",
        "tools/fix_live_intent_scope.py",
        "tools/evaluate_mt4_tick_volume_capture.py",
        "tests/test_evaluate_mt4_tick_volume_capture.py",
        "tools/extract_model_manifest.py",
        "tests/test_extract_model_manifest.py",
        "tools/improve_from_real_trades.py",
        "tools/inspect_agent_activity.py",
        "tools/live_execution_smoke.py",
        "tools/mt4_interop_efficiency_audit.py",
        "tools/orchestration_experiments.py",
        "tests/test_orchestration_experiments_tool.py",
        "tools/orchestration_canary_control.py",
        "tools/probe_alloc_truth.py",
        "tools/probe_allocator_block.py",
        "tools/probe_model_set_rollout.py",
        "tools/probe_runtime_live_decision.py",
        "tools/production_scalp_evidence.py",
        "tools/replay_orchestration.py",
        "tools/self_healing_watchdog.py",
        "tools/test_seasonality_alpha.py",
        "tools/watch_eurusd_live.py",
        "ops/windows/19_monitor_shadow_training.bat",
        "ops/windows/19_monitor_shadow_training.ps1",
        "ops/windows/27_live_execution_smoke.bat",
        "src/audit/__init__.py",
        "src/audit/interop_efficiency.py",
        "src/audit/strategy_conflict_metrics.py",
        "src/config/fx_el_minis.yaml",
        "fx-quant-stack/src/fxstack/scalp/screen_failed_auction_reclaim.py",
        "fx-quant-stack/src/fxstack/scalp/screen_impulse_pullback.py",
        "fx-quant-stack/src/fxstack/scalp/screen_liquidity_sweep.py",
        "fx-quant-stack/src/fxstack/scalp/screen_median_stretch_reversal.py",
        "fx-quant-stack/src/fxstack/scalp/screen_provider_volume_shock_close_location_continuation.py",
        "fx-quant-stack/src/fxstack/scalp/screen_quote_side_convergence_continuation.py",
        "fx-quant-stack/src/fxstack/scalp/screen_range_displacement_trigger.py",
        "fx-quant-stack/src/fxstack/scalp/screen_rolling_sign_transition_state.py",
        "fx-quant-stack/src/fxstack/scalp/screen_volatility_compression_expansion.py",
        "fx-quant-stack/src/fxstack/runtime/scalp_cycle_capacity.py",
        "fx-quant-stack/src/fxstack/runtime/scalp_entry_quote.py",
        "fx-quant-stack/src/fxstack/runtime/scalp_entry_qualification.py",
        "fx-quant-stack/src/fxstack/runtime/scalp_proposal_batch.py",
        "fx-quant-stack/src/fxstack/scalp/edge_math.py",
        "fx-quant-stack/scripts/freeze_baseline.py",
        "fx-quant-stack/scripts/evaluate_fast_gate.py",
        "fx-quant-stack/scripts/train_exit.py",
        "fx-quant-stack/scripts/train_meta.py",
        "fx-quant-stack/scripts/train_reversal.py",
        "MQL4/Experts/SymbolScanner.mq4",
        "MQL4/Indicators/BridgeVisualizer.mq4",
        "public/placeholder-logo.svg",
        "public/placeholder-user.jpg",
        "public/placeholder.jpg",
        "public/placeholder.svg",
        "fx-quant-stack/configs/base.yaml",
        "fx-quant-stack/configs/live.yaml",
        "fx-quant-stack/configs/horizons/intraday.yaml",
        "fx-quant-stack/configs/horizons/swing.yaml",
        "fx-quant-stack/configs/pairs/majors.yaml",
        "tests/golden/orchestration/README.md",
        "ops/windows/26_guard_mtvclc_collector.ps1",
        "ops/windows/27_guard_mtvclc_collector_resilient.ps1",
        "ops/windows/27_guard_mtvclc_collector_resilient_v2.ps1",
        "ops/windows/28_register_mtvclc_collection_dependencies_watchdog.ps1",
        "ops/windows/29_ensure_mtvclc_collector_resilient.ps1",
        "ops/windows/29_ensure_mtvclc_collector_resilient_v2.ps1",
        "ops/windows/29_register_mtvclc_collector_resilient_watchdog.ps1",
        "ops/windows/29_register_mtvclc_collector_resilient_watchdog_v2.ps1",
        "tools/check_mt4_tick_volume_collector_continuity.py",
        "tools/check_mt4_tick_volume_collector_continuity_resilient_v2.py",
        "tests/test_mtvclc_collector_continuity_guard.py",
        "tests/test_mtvclc_resilient_collector_restart_watchdog.py",
        "tests/test_mtvclc_gap_v3_collector_supervision.py",
        "docs/agents/mtvclc-preregistration.md",
        "docs/agents/mtvclc-preregistration-v4.md",
    ):
        assert not (REPO_ROOT / retired).exists(), retired


def test_finalize_build_sets_go_when_gates_pass_and_no_high_critical(tmp_path: Path):
    evidence = tmp_path / "docs" / "audit" / "20260317_full_process"
    evidence.mkdir(parents=True)

    blockers = {
        "schema_version": 1,
        "generated_at": "2026-03-17T00:00:00+00:00",
        "blockers": [
            {"id": "B-1", "severity": "medium", "status": "open"},
        ],
    }
    (evidence / "blockers.json").write_text(json.dumps(blockers), encoding="utf-8")
    (evidence / "gate_summary.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    model_manifest = tmp_path / "active_models.json"
    model_manifest.write_text(
        json.dumps(
            {
                "active_model_sets": {
                    "EURUSD": {
                        "model_set_id": "bundle-audit",
                        "metadata": {"bundle_run_id": "bundle-audit"},
                        "artifacts": {"meta": {"content_sha256": "a" * 64}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    identity = finalize_build.active_manifest_identity(manifest_path=model_manifest, pair="EURUSD")
    long_samples = _shadow_samples(
        start=10_000.0,
        end=96_400.0,
        poll=60.0,
        boot_id="boot-audit-long",
    )
    shadow = {
        "schema_version": "fxstack_shadow_runtime_evidence_v2",
        "producer": {"tool": "tools.shadow_dual_run", "version": "v2"},
        "started_at": 10_000.0,
        "ended_at": 96_400.0,
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
                "start_ts": 10_000.0,
                "end_ts": 96_400.0,
                "queried_at": 96_400.0,
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
                "model_set_id": "bundle-audit",
                "pair_readiness_status": "ready",
                "has_exit_model": True,
                "has_reversal_models": True,
                "lifecycle_activation_mode": "model_driven",
                "lifecycle_ready": True,
            },
        },
        "candidate": {
            "samples": len(long_samples),
            "runtime_ready_seen": True,
            "feature_ready_seen": True,
        },
        "observation_coverage": {
            "poll_interval_secs": 60.0,
            "poll_attempts": len(long_samples),
            "successful_samples": len(long_samples),
            "successful_sample_ratio": 1.0,
            "runtime_ready_sample_ratio": 1.0,
            "feature_ready_sample_ratio": 1.0,
            "first_sample_at": 10_000.0,
            "last_sample_at": 96_400.0,
            "observed_span_secs": 86_400.0,
            "max_sample_gap_secs": 60.0,
            "runtime_boot_id": "boot-audit-long",
            "continuous_boot": True,
        },
        "candidate_samples": long_samples,
        "baseline_samples": long_samples,
    }
    fast_gate = dict(shadow)
    fast_gate["started_at"] = 1_000.0
    fast_gate["ended_at"] = 1_900.0
    fast_samples = _shadow_samples(
        start=1_000.0,
        end=1_900.0,
        poll=60.0,
        boot_id="boot-audit-fast",
    )
    fast_gate["candidate"] = {
        "samples": len(fast_samples),
        "runtime_ready_seen": True,
        "feature_ready_seen": True,
    }
    fast_gate["runtime_boundary"] = {
        **shadow["runtime_boundary"],
        "command_window_summary": {
            "schema_version": "fxstack_command_window_summary_v1",
            "window_complete": True,
            "start_ts": 1_000.0,
            "end_ts": 1_900.0,
            "queried_at": 1_900.0,
            "total_commands": 0,
            "entry_commands": 0,
            "control_commands": 0,
            "status_counts": {},
            "command_counts": {},
        },
    }
    fast_gate["observation_coverage"] = {
        "poll_interval_secs": 60.0,
        "poll_attempts": len(fast_samples),
        "successful_samples": len(fast_samples),
        "successful_sample_ratio": 1.0,
        "runtime_ready_sample_ratio": 1.0,
        "feature_ready_sample_ratio": 1.0,
        "first_sample_at": 1_000.0,
        "last_sample_at": 1_900.0,
        "observed_span_secs": 900.0,
        "max_sample_gap_secs": 60.0,
        "runtime_boot_id": "boot-audit-fast",
        "continuous_boot": True,
    }
    fast_gate["candidate_samples"] = fast_samples
    fast_gate["baseline_samples"] = fast_samples
    fast_path = tmp_path / "fast.json"
    shadow_path = tmp_path / "shadow.json"
    fast_path.write_text(json.dumps(fast_gate), encoding="utf-8")
    shadow_path.write_text(json.dumps(shadow), encoding="utf-8")
    rollback_path = tmp_path / "rollback.json"
    rollback_path.write_text(
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
                    "candidate_bundle_run_id": "bundle-audit",
                },
            }
        ),
        encoding="utf-8",
    )

    args = argparse.Namespace(
        evidence_dir=str(evidence),
        evidence_root=str(tmp_path / "docs" / "audit"),
        fast_gate_artifact=str(fast_path),
        shadow_artifact=str(shadow_path),
        rollback_evidence=str(rollback_path),
        pair="EURUSD",
        bundle_run_id="bundle-audit",
        model_manifest=str(model_manifest),
    )
    rc = finalize_build.run(args)
    assert rc == 0
    go_no_go = json.loads((evidence / "go_no_go.json").read_text(encoding="utf-8"))
    assert go_no_go["decision"] == "GO"
    latest_pointer = json.loads(
        (evidence / "release_validation_bundle.json").read_text(encoding="utf-8")
    )
    assert latest_pointer["advisory_only"] is True
    release_bundle_path = Path(latest_pointer["authority_path"])
    release_bundle = json.loads(release_bundle_path.read_text(encoding="utf-8"))
    assert release_bundle["valid"] is True
    assert release_bundle["artifacts"]["rollback_evidence"]["sha256"]
    finalized_manifest = Path(release_bundle["artifacts"]["model_manifest"]["path"])
    assert finalized_manifest.parent == evidence
    assert finalized_manifest != model_manifest
    finalized_bytes = finalized_manifest.read_bytes()

    # Activation-stage metadata may mutate the operational manifest after
    # finalization. The authority envelope must remain valid against its pinned
    # bytes instead of invalidating itself.
    mutable_manifest = json.loads(model_manifest.read_text(encoding="utf-8"))
    mutable_manifest["active_model_sets"]["EURUSD"]["metadata"]["main_runtime_rollout"] = {
        "enabled": True,
        "stage": "canary",
    }
    model_manifest.write_text(json.dumps(mutable_manifest), encoding="utf-8")
    pinned_validation = finalize_build.validate_release_validation_bundle(
        path=release_bundle_path,
        expected_pair=identity.pair,
        expected_bundle_run_id=identity.bundle_run_id,
        expected_model_set_id=identity.model_set_id,
        expected_model_manifest_sha256=identity.model_manifest_sha256,
        expected_artifact_set_sha256=identity.artifact_set_sha256,
    )
    assert pinned_validation.valid, pinned_validation.errors
    assert finalized_manifest.read_bytes() == finalized_bytes

    args.shadow_artifact = str(fast_path)
    duplicate_rc = finalize_build.run(args)
    assert duplicate_rc == 2
    duplicate_summary = json.loads((evidence / "gate_summary.json").read_text(encoding="utf-8"))
    assert "shadow_artifact_paths_duplicate" in duplicate_summary["release_evidence_distinct"]["errors"]
    assert "shadow_run_windows_overlap_or_invalid" in duplicate_summary["release_evidence_distinct"]["errors"]
    duplicate_decision = json.loads((evidence / "go_no_go.json").read_text(encoding="utf-8"))
    assert duplicate_decision["decision"] == "HOLD"

    # Cached GO outputs cannot hide a newly opened blocker; every source is
    # reopened and rehashed on each finalization attempt.
    args.shadow_artifact = str(shadow_path)
    blockers["blockers"].append(
        {"id": "B-critical", "severity": "critical", "status": "open"}
    )
    (evidence / "blockers.json").write_text(json.dumps(blockers), encoding="utf-8")
    reopened_rc = finalize_build.run(args)
    assert reopened_rc == 2
    reopened_decision = json.loads((evidence / "go_no_go.json").read_text(encoding="utf-8"))
    assert reopened_decision["checks"]["no_open_critical_high"] is False
    assert reopened_decision["decision"] == "HOLD"


def _write_ohlc_csv(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["time,open,high,low,close\n"]
    for i in range(rows):
        lines.append(f"2026-01-01 00:{i%60:02d}:00,1.0,1.1,0.9,1.0\n")
    path.write_text("".join(lines), encoding="utf-8")


def test_dukascopy_coverage_gate_passes_when_all_files_meet_thresholds(tmp_path: Path):
    root = tmp_path / "dukascopy"
    _write_ohlc_csv(root / "EURUSD_M1.csv", rows=10)
    _write_ohlc_csv(root / "EURUSD_M5.csv", rows=8)
    _write_ohlc_csv(root / "USDJPY_M1.csv", rows=11)
    _write_ohlc_csv(root / "USDJPY_M5.csv", rows=9)

    out = tmp_path / "gate.json"
    args = argparse.Namespace(
        source_root=str(root),
        pairs="EURUSD,USDJPY",
        timeframes="M1,M5",
        file_pattern="{pair}_{granularity}.csv",
        min_rows_m1=10,
        min_rows_m5=8,
        min_rows_m15=1,
        min_rows_h4=1,
        min_rows_d=1,
        out=str(out),
    )
    rc = dukascopy_coverage_gate.run(args)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["summary"]["passed"]) is True
    assert int(payload["summary"]["missing_count"]) == 0
    assert int(payload["summary"]["insufficient_count"]) == 0


def test_dukascopy_coverage_gate_fails_on_missing_and_insufficient_files(tmp_path: Path):
    root = tmp_path / "dukascopy"
    _write_ohlc_csv(root / "EURUSD_M1.csv", rows=5)
    # EURUSD_M5 missing intentionally.
    _write_ohlc_csv(root / "USDJPY_M1.csv", rows=10)
    _write_ohlc_csv(root / "USDJPY_M5.csv", rows=2)

    args = argparse.Namespace(
        source_root=str(root),
        pairs="EURUSD,USDJPY",
        timeframes="M1,M5",
        file_pattern="{pair}_{granularity}.csv",
        min_rows_m1=10,
        min_rows_m5=8,
        min_rows_m15=1,
        min_rows_h4=1,
        min_rows_d=1,
        out="",
    )
    rc = dukascopy_coverage_gate.run(args)
    assert rc == 2


def test_live_stack_check_passes_with_heartbeat_ticks_and_acked_command(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])
    event_rows = iter(
        [
            {"events": [{"status": "queued"}, {"status": "delivered"}]},
            {"events": [{"status": "queued"}, {"status": "delivered"}, {"status": "acked"}]},
        ]
    )

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": True,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        if path == "/v2/market/ticks":
            return {"EURUSD": {"bid": 1.1, "ask": 1.1002}}
        if path.startswith("/v2/commands/events"):
            try:
                return next(event_rows)
            except StopIteration:
                return {"events": [{"status": "acked"}]}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(
        live_stack_check,
        "_fetch_dashboard_state",
        lambda *a, **k: {
            "checked": True,
            "status_code": 200,
            "ok": True,
            "payload": {"systemStatus": "connected"},
            "text": "{\"systemStatus\":\"connected\"}",
            "url": "http://127.0.0.1:3000/api/trading/state",
        },
    )
    monkeypatch.setattr(live_stack_check, "_post_json", lambda *a, **k: {"status": "queued"})
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="http://127.0.0.1:3000",
        timeout_secs=5.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        min_observation_secs=0.0,
        runtime_stall_secs=60.0,
        keepalive_heartbeat_secs=0.0,
        require_ticks=True,
        require_acked_command=True,
        require_feature_serving=True,
        require_paper_boundary=False,
        paper_safe_command_check=False,
        command="INFO",
        symbol="EURUSD",
        lots=0.0,
        command_timeout_secs=1.0,
        out=str(out),
    )
    rc = live_stack_check.run(args)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["passed"]) is True
    assert bool(payload["checks"]["dashboard_state_ok"]) is True
    assert bool(payload["checks"]["runtime_startup_ok"]) is True
    assert bool(payload["checks"]["command_acked"]) is True
    assert bool(payload["checks"]["ticks_present"]) is True
    assert bool(payload["checks"]["provider_health_ok"]) is True
    assert bool(payload["checks"]["feature_ready_ok"]) is True


def test_live_stack_check_can_require_paper_boundary_and_read_event_status(monkeypatch, tmp_path: Path):
    state_rows = iter(
        [
            {
                "last_heartbeat": "hb-1",
                "paper_execution": {
                    "enabled": True,
                    "execution_provider": "paper",
                    "agent_mode": "paper",
                },
            },
            {
                "last_heartbeat": "hb-2",
                "paper_execution": {
                    "enabled": True,
                    "execution_provider": "paper",
                    "agent_mode": "paper",
                },
            },
        ]
    )
    event_rows = iter(
        [
            {"events": [{"event_status": "queued"}, {"event_status": "delivered"}]},
            {"events": [{"event_status": "queued"}, {"event_status": "delivered"}, {"event_status": "acked"}]},
        ]
    )

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": True,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "shadow_only", "provider": "paper"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "paper",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {
                    "last_heartbeat": "hb-2",
                    "paper_execution": {
                        "enabled": True,
                        "execution_provider": "paper",
                        "agent_mode": "paper",
                    },
                }
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        if path.startswith("/v2/commands/events"):
            try:
                return next(event_rows)
            except StopIteration:
                return {"events": [{"event_status": "acked"}]}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check, "_post_json", lambda *a, **k: {"status": "queued"})
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_paper.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="",
        timeout_secs=5.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        min_observation_secs=0.0,
        runtime_stall_secs=60.0,
        keepalive_heartbeat_secs=0.0,
        require_ticks=False,
        require_acked_command=True,
        require_feature_serving=False,
        require_paper_boundary=True,
        paper_safe_command_check=False,
        command="INFO",
        symbol="EURUSD",
        lots=0.0,
        command_timeout_secs=1.0,
        out=str(out),
    )
    rc = live_stack_check.run(args)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["checks"]["paper_boundary_ok"]) is True
    assert bool(payload["checks"]["command_acked"]) is True
    assert payload["details"]["paper_boundary"]["paper_execution_provider"] == "paper"


def test_live_stack_check_refreshes_ready_after_keepalive(monkeypatch, tmp_path: Path):
    health_rows = iter(
        [
            {"status": "ok", "system_status": "starting"},
            {"status": "ok", "system_status": "connected"},
        ]
    )
    ready_rows = iter(
        [
            {
                "status": "ok",
                "reason": "mt4_heartbeat_stale",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": False,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": True,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "degraded"},
                    "execution_provider": {"status": "ok", "provider": "paper"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "paper",
                },
            },
            {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": True,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok", "provider": "paper"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "paper",
                },
            },
        ]
    )
    state_rows = iter(
        [
            {
                "last_heartbeat": "hb-1",
                "paper_execution": {
                    "enabled": True,
                    "execution_provider": "paper",
                    "agent_mode": "paper",
                },
            },
            {
                "last_heartbeat": "hb-2",
                "paper_execution": {
                    "enabled": True,
                    "execution_provider": "paper",
                    "agent_mode": "paper",
                },
            },
        ]
    )

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            try:
                return next(health_rows)
            except StopIteration:
                return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            try:
                return next(ready_rows)
            except StopIteration:
                return {
                    "status": "ok",
                    "reason": "ok",
                    "runtime_status": "running",
                    "runtime_phase": "main_loop",
                    "runtime_last_progress_age_secs": 1.0,
                    "runtime_failure_reason": "",
                    "mt4_fresh": True,
                    "ticks_fresh": True,
                    "feature_online_ready": True,
                    "feature_data_fresh": True,
                    "feature_push_backlog": 0,
                    "feature_push_backlog_ok": True,
                    "feature_blocker_reason": "",
                    "provider_health": {
                        "history_provider": {"status": "ok"},
                        "market_data_provider": {"status": "ok"},
                        "execution_provider": {"status": "ok", "provider": "paper"},
                    },
                    "provider_roles": {
                        "history_provider": "parquet",
                        "market_data_provider": "mt4_bridge",
                        "execution_provider": "paper",
                    },
                }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {
                    "last_heartbeat": "hb-2",
                    "paper_execution": {
                        "enabled": True,
                        "execution_provider": "paper",
                        "agent_mode": "paper",
                    },
                }
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check, "_post_keepalive_report", lambda *a, **k: {"status": "ok"})
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_ready_refresh.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="",
        timeout_secs=2.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        min_observation_secs=0.0,
        runtime_stall_secs=60.0,
        keepalive_heartbeat_secs=0.01,
        require_ticks=False,
        require_acked_command=False,
        require_feature_serving=False,
        require_paper_boundary=True,
        paper_safe_command_check=False,
        command="INFO",
        symbol="EURUSD",
        lots=0.0,
        command_timeout_secs=1.0,
        out=str(out),
    )
    rc = live_stack_check.run(args)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["checks"]["health_ok"]) is True
    assert bool(payload["checks"]["ready_ok"]) is True
    assert bool(payload["checks"]["mt4_fresh"]) is True
    assert bool(payload["checks"]["provider_health_ok"]) is True
    assert "ready_reason:mt4_heartbeat_stale" not in payload["findings"]


def test_live_stack_check_non_trading_probe_allows_info_only(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])
    posted: list[tuple[str, dict[str, object]]] = []
    event_rows = iter([{"events": [{"status": "queued"}, {"status": "acked"}]}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": True,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        if path.startswith("/v2/commands/events"):
            try:
                return next(event_rows)
            except StopIteration:
                return {"events": [{"status": "acked"}]}
        return {}

    def _fake_post(base_url: str, path: str, payload: dict[str, object], timeout: float = 2.0):
        del base_url, timeout
        posted.append((path, dict(payload)))
        return {"status": "queued"}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check, "_post_json", _fake_post)
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_paper_safe.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        runtime_stall_secs=30.0,
        require_ticks=False,
        require_acked_command=True,
        require_feature_serving=False,
        paper_safe_command_check=True,
        command="INFO",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out=str(out),
    )

    rc = live_stack_check.run(args)
    assert rc == 0
    assert posted
    assert posted[0][1]["cmd"] == "INFO"
    assert posted[0][1]["intent"] == "NON_TRADING_HEALTH_CHECK"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["checks"]["command_acked"]) is True
    assert payload["details"]["command_statuses"] == ["queued", "acked"]


def test_live_stack_check_blocks_execution_command_probe(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": True,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        return {}

    def _unexpected_post(*_args, **_kwargs):
        raise AssertionError("paper-safe command check should not post unsafe commands")

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check, "_post_json", _unexpected_post)
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_paper_safe_blocked.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        runtime_stall_secs=30.0,
        require_ticks=False,
        require_acked_command=True,
        require_feature_serving=False,
        paper_safe_command_check=True,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out=str(out),
    )

    rc = live_stack_check.run(args)
    assert rc == 2
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "non_trading_command_probe_blocked:CLOSE_ALL" in payload["findings"]
    assert "non_trading_command_probe_requires_INFO" in payload["errors"]
    assert payload["details"]["command_id"] == ""


def test_live_stack_check_can_require_feature_serving_readiness(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": True,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check, "_fetch_dashboard_state", lambda *a, **k: {"checked": False, "status_code": None, "ok": False, "payload": {}, "text": "", "url": ""})
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_feature.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        runtime_stall_secs=30.0,
        require_ticks=False,
        require_acked_command=False,
        require_feature_serving=True,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out=str(out),
    )
    rc = live_stack_check.run(args)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["checks"]["feature_ready_ok"]) is True
    assert payload["details"]["feature_serving"]["online_ready"] is True
    assert payload["details"]["feature_serving"]["data_fresh"] is True


def test_live_stack_check_reports_feature_serving_dependency_source_and_bar_status(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "feature_online_ready": True,
                "feature_data_fresh": False,
                "feature_push_backlog": 0,
                "feature_push_backlog_ok": True,
                "feature_blocker_reason": "feature_serving:stale",
                "feature_blocker_source": "feature_serving",
                "feature_bar_status": "stale",
                "feature_serving_source": "parquet_fallback",
                "feature_serving_reason": "feast_unavailable",
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        if path == "/v2/market/ticks":
            return {"EURUSD": {"bid": 1.1, "ask": 1.1002}}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_feature_dependency.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        runtime_stall_secs=30.0,
        require_ticks=False,
        require_acked_command=False,
        require_feature_serving=True,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out=str(out),
    )

    rc = live_stack_check.run(args)
    assert rc == 2
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["details"]["feature_serving"]["blocker_source"] == "feature_serving"
    assert payload["details"]["feature_serving"]["bar_status"] == "stale"
    assert payload["details"]["feature_serving"]["serving_reason"] == "feast_unavailable"
    assert "feature_blocker_source:feature_serving" in payload["findings"]
    assert "feature_bar_status:stale" in payload["findings"]
    assert "feature_serving_not_ready" in payload["findings"]


def test_live_stack_check_fails_when_ticks_missing(monkeypatch):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT"}]}
        if path == "/v2/market/ticks":
            return {}
        return {"events": []}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        require_ticks=True,
        require_acked_command=False,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out="",
    )
    rc = live_stack_check.run(args)
    assert rc == 2


def test_live_stack_check_reports_runtime_dashboard_and_provider_failures(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "runtime_startup_stalled",
                "runtime_status": "starting",
                "runtime_phase": "model_load",
                "runtime_phase_pair": "EURUSD",
                "runtime_last_progress_age_secs": 125.0,
                "runtime_failure_reason": "TimeoutError:model_load_timeout",
                "mt4_fresh": False,
                "ticks_fresh": False,
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "degraded"},
                    "execution_provider": {"status": "degraded"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT"}]}
        if path == "/v2/market/ticks":
            return {}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(
        live_stack_check,
        "_fetch_dashboard_state",
        lambda *a, **k: {
            "checked": True,
            "status_code": 503,
            "ok": False,
            "payload": {"systemStatus": "error"},
            "text": "{\"systemStatus\":\"error\"}",
            "url": "http://127.0.0.1:3000/api/trading/state",
        },
    )
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_failure.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="http://127.0.0.1:3000",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        runtime_stall_secs=30.0,
        require_ticks=True,
        require_acked_command=False,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out=str(out),
    )
    rc = live_stack_check.run(args)
    assert rc == 2
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["checks"]["runtime_startup_ok"]) is False
    assert bool(payload["checks"]["dashboard_state_ok"]) is False
    assert bool(payload["checks"]["provider_health_ok"]) is False
    assert bool(payload["checks"]["mt4_fresh"]) is False
    assert bool(payload["checks"]["ticks_fresh"]) is False
    assert "runtime_startup_failure_reason:TimeoutError:model_load_timeout" in payload["findings"]
    assert any(item.startswith("runtime_model_load_stalled:") for item in payload["findings"])
    assert "dashboard_state_http_503" in payload["findings"]
