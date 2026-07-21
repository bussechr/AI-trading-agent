from __future__ import annotations

import json
import sys
from pathlib import Path

import tools.shadow_dual_run as shadow_dual_run
from fxstack.training.release_evidence import active_manifest_identity, file_sha256
from tools.shadow_dual_run import (
    CommandSummary,
    SystemSummary,
    execute_rollback_command,
    evaluate_gates,
    summarize_commands,
)


def test_summarize_commands_filters_window_and_statuses():
    rows = [
        {"created_at": 100.0, "cmd": "BUY", "status": "acked"},
        {"created_at": 101.0, "cmd": "SELL", "status": "failed"},
        {"created_at": 102.0, "cmd": "CLOSE", "status": "acked"},
        {"created_at": 80.0, "cmd": "BUY", "status": "acked"},
    ]
    out = summarize_commands(rows, start_ts=99.0, end_ts=103.0)

    assert out.entries_sent == 2
    assert out.entries_acked == 1
    assert out.entries_failed == 1
    assert out.control_sent == 1
    assert out.control_acked == 1


def _summary(
    name: str,
    acked: int,
    timeout_rate: float,
    hard: bool = False,
    daily: bool = False,
    *,
    runtime_ready: bool = True,
    feature_ready: bool = True,
    trade_flow_seen: bool = True,
) -> SystemSummary:
    return SystemSummary(
        name=name,
        url=f"http://{name}",
        command_summary=CommandSummary(entries_sent=acked + 1, entries_acked=acked, entries_failed=0, control_sent=0, control_acked=0),
        samples=10,
        avg_decisions=1.2,
        avg_pending=0.2,
        max_timeout_rate=timeout_rate,
        max_drawdown_pct=0.05,
        hard_breach_seen=hard,
        daily_breaker_seen=daily,
        governance_pause_seen=False,
        governance_events_window=1,
        runtime_ready_seen=runtime_ready,
        feature_ready_seen=feature_ready,
        canary_active_seen=True,
        max_signals_sent=acked + 2,
        max_approved_entries=acked + 1,
        max_submitted_entries=acked,
        max_divergence_spike_count=0,
        trade_flow_seen=trade_flow_seen,
        poll_attempts=10,
        successful_sample_ratio=1.0,
        runtime_ready_sample_ratio=1.0 if runtime_ready else 0.0,
        feature_ready_sample_ratio=1.0 if feature_ready else 0.0,
        runtime_boot_id="boot-test",
        continuous_boot=True,
    )


def test_evaluate_gates_pass():
    base = _summary("base", acked=5, timeout_rate=0.01)
    cand = _summary("cand", acked=8, timeout_rate=0.02)
    gates = evaluate_gates(
        baseline=base,
        candidate=cand,
        min_throughput_delta=1,
        max_timeout_rate=0.05,
        require_nonzero=True,
    )
    assert gates.passed is True
    assert gates.throughput_delta_entries_acked == 3
    assert gates.rollback_triggers == []


def test_evaluate_gates_fail_with_risk_breach():
    base = _summary("base", acked=5, timeout_rate=0.01)
    cand = _summary("cand", acked=4, timeout_rate=0.08, hard=True)
    gates = evaluate_gates(
        baseline=base,
        candidate=cand,
        min_throughput_delta=1,
        max_timeout_rate=0.05,
        require_nonzero=True,
    )
    assert gates.passed is False
    assert "throughput_gate_failed" in gates.rollback_triggers
    assert "reliability_gate_failed" in gates.rollback_triggers
    assert "risk_gate_failed" in gates.rollback_triggers


def test_evaluate_gates_requires_runtime_and_feature_evidence_without_forcing_trades():
    base = _summary("base", acked=5, timeout_rate=0.01)
    cand = _summary("cand", acked=6, timeout_rate=0.02, runtime_ready=False, feature_ready=False, trade_flow_seen=False)
    gates = evaluate_gates(
        baseline=base,
        candidate=cand,
        min_throughput_delta=1,
        max_timeout_rate=0.05,
        require_nonzero=True,
    )
    assert gates.passed is False
    assert "operability_gate_failed" in gates.rollback_triggers
    assert "runtime_flow_evidence_gate_failed" not in gates.rollback_triggers


def test_evaluate_shadow_gate_allows_idle_zero_order_window():
    base = _summary("base", acked=0, timeout_rate=0.01, trade_flow_seen=False)
    cand = _summary("cand", acked=0, timeout_rate=0.01, trade_flow_seen=False)
    gates = evaluate_gates(
        baseline=base,
        candidate=cand,
        min_throughput_delta=0,
        max_timeout_rate=0.05,
        require_nonzero=False,
    )
    assert gates.passed is True
    assert gates.checks["throughput"] is True
    assert gates.checks["runtime_flow_evidence"] is True


def test_candidate_runtime_evidence_uses_actual_agent_mode_and_loaded_lifecycle(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "active_models.json"
    manifest_path.write_text(
        json.dumps(
            {
                "active_model_sets": {
                    "EURUSD": {
                        "model_set_id": "model-1",
                        "metadata": {"bundle_run_id": "bundle-1"},
                        "artifacts": {"meta": {"content_sha256": "b" * 64}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    expected = active_manifest_identity(manifest_path=manifest_path, pair="EURUSD")
    candidate = _summary("candidate", acked=0, timeout_rate=0.0, trade_flow_seen=False)
    candidate.command_summary = CommandSummary(
        entries_sent=0,
        entries_acked=0,
        entries_failed=0,
        control_sent=0,
        control_acked=0,
    )
    identity, boundary, errors = shadow_dual_run._candidate_runtime_evidence(
        state={
            "startup_inference": {
                "EURUSD": {
                    "ok": True,
                    "model_set_id": "model-1",
                    "has_exit_model": True,
                    "has_reversal_models": True,
                    "lifecycle_activation_mode": "model_driven",
                    "pair_readiness": {"status": "ready"},
                }
            },
            "activation_consistency": {
                "manifest": {
                    "path": str(manifest_path),
                    "manifest_sha256": file_sha256(manifest_path),
                },
                "active_manifest_matches_db": True,
                "runtime_loaded_matches_db": True,
                "activation_mismatch_pairs": [],
            },
            "orchestration_live": {"mode": "canary", "agent_mode": "shadow"},
            "shadowOnlyMode": True,
        },
        expected=expected,
        candidate=candidate,
        command_window_summary={
            "schema_version": "fxstack_command_window_summary_v1",
            "window_complete": True,
            "total_commands": 0,
            "entry_commands": 0,
            "control_commands": 0,
            "status_counts": {},
            "command_counts": {},
        },
    )

    assert errors == []
    assert identity.model_set_id == "model-1"
    assert identity.model_manifest_sha256 == expected.model_manifest_sha256
    assert boundary["agent_mode"] == "shadow"
    assert boundary["observed_manifest_file_sha256_matches"] is True
    assert boundary["startup_lifecycle"]["lifecycle_ready"] is True


def test_execute_rollback_command_success():
    rb = execute_rollback_command("echo rollback_ok", timeout_secs=5.0)
    assert rb.attempted is True
    assert rb.success is True
    assert rb.return_code == 0
    assert "rollback_ok" in rb.stdout_tail


def test_execute_rollback_command_timeout():
    command = f'"{sys.executable}" -c "import time; time.sleep(2)"'
    rb = execute_rollback_command(command, timeout_secs=1.0)
    assert rb.attempted is True
    assert rb.success is False
    assert rb.timed_out is True
    assert rb.return_code == -1


def test_collect_sample_surfaces_trade_flow_readiness(monkeypatch):
    monkeypatch.setattr(shadow_dual_run, "_fetch_ready", lambda base_url: {"runtime_ready": True})
    monkeypatch.setattr(
        shadow_dual_run,
        "_fetch_state",
        lambda base_url: {
            "governance": {"hard_dd_pct": 0.12, "drawdown_pct": 0.01},
            "tradeFlowSummary": {
                "signalsSent": 7,
                "approvedEntryCount": 5,
                "submittedEntryCount": 4,
                "canaryActive": True,
                "ackSuccessRate": 0.75,
                "divergenceCounts": {"shadowLiveOnly": 2, "adaptiveLiveOnly": 1, "orchestratorFaultCount": 1},
                "canaryHealth": {"featureOnlineReady": True, "featureDataFresh": True},
            },
        },
    )
    monkeypatch.setattr(shadow_dual_run, "_fetch_metrics", lambda base_url: {"timeouts": {"ack_timeout_rate_5m": 0.02}, "pending": {"count": 3}})

    sample = shadow_dual_run._collect_sample("http://example", timeout=1.0)
    assert sample.runtime_ready is True
    assert sample.feature_ready is True
    assert sample.canary_active is True
    assert sample.signals_sent == 7
    assert sample.approved_entries == 5
    assert sample.submitted_entries == 4
    assert sample.trade_flow_seen is True
    assert sample.divergence_spike_count == 4
