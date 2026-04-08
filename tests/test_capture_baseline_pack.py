from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools import capture_baseline_pack


def test_capture_baseline_pack_writes_expected_files(tmp_path, monkeypatch) -> None:
    payloads = {
        "/v2/ready": {
            "status": "ok",
            "reason": "ok",
            "bridge_up": True,
            "mt4_connected": True,
            "runtime_status": "running",
            "runtime_phase": "main_loop",
            "runtime_ready": True,
            "runtime_startup_status": "ok",
            "startup_inference_failures": 0,
            "model_activation_manifest": "fx-quant-stack/artifacts/active_models.json",
        },
        "/v2/state": {
            "pending_command_count": 2,
            "submitted_entry_count": 1,
            "submitted_live_entry_count": 1,
            "runtime_status": "running",
            "lifecycle_capabilities": {
                "EURUSD": {"registry_path": "fx-quant-stack/artifacts/registry/eurusd.json"}
            },
        },
        "/v2/decision-snapshots": {
            "items": [
                {
                    "vol": 0.12,
                    "decisions_json": [
                        {
                            "symbol": "EURUSD",
                            "side": "BUY",
                            "score": 4.2,
                            "confidence": 77.0,
                            "execution_ready": False,
                            "reasons": ["shadow_meta_reject"],
                            "metadata": {"adaptive_playbook": "trend_pullback", "allocator_rank": 1},
                        }
                    ],
                    "diagnostics_json": {"runtime": "fxstack"},
                }
            ]
        },
        "/v2/orchestration/runs": {
            "items": [
                {
                    "run_id": "run-1",
                    "pair": "EURUSD",
                    "runtime_mode": "live",
                    "version_bundle_json": {
                        "schema_version": "orchestration_v1",
                        "policy_version": "policy_v1",
                        "model_bundle_version": "bundle_v1",
                        "orchestrator_version": "orch_v1",
                    },
                }
            ]
        },
        "/v2/orchestration/traces": {
            "items": [
                {
                    "trace_id": "trace-1",
                    "run_id": "run-1",
                    "pair": "EURUSD",
                    "trace_json": {"schema_version": "orchestration_trace_v1"},
                }
            ]
        },
    }

    def _fake_fetch(base_url: str, path: str, timeout: float = 3.0) -> dict:
        return dict(payloads[path])

    monkeypatch.setattr(capture_baseline_pack, "_fetch_json", _fake_fetch)
    code = capture_baseline_pack.run(
        argparse.Namespace(
            base_url="http://127.0.0.1:58710",
            output_root=str(tmp_path),
            tag="unit",
            timeout=1.0,
            project_root=str(Path(__file__).resolve().parents[1]),
        )
    )
    assert code == 0

    capture_dirs = sorted(tmp_path.glob("*_unit"))
    assert capture_dirs
    out_dir = capture_dirs[-1]
    for name in ["ready.json", "state.json", "decision-snapshots.json", "normalized-summary.json", "baseline-pack.json"]:
        assert (out_dir / name).exists()

    summary = json.loads((out_dir / "normalized-summary.json").read_text(encoding="utf-8"))
    assert summary["blocking_reasons"] == {"shadow_meta_reject": 1}
    assert summary["command_queue_summary"]["pending_command_count"] == 2
    assert "fx-quant-stack/artifacts/active_models.json" in summary["active_model_manifest_refs"]
    assert summary["orchestration_run_count"] == 1
    assert summary["orchestration_trace_count"] == 1
    assert summary["latest_orchestration_run_id"] == "run-1"
    assert summary["latest_orchestration_trace_id"] == "trace-1"
    assert summary["latest_orchestration_version_bundle"]["schema_version"] == "orchestration_v1"
    assert summary["latest_orchestration_version_bundle"]["orchestrator_version"] == "orch_v1"


def test_normalize_capture_is_stable_for_same_payloads() -> None:
    payloads = {
        "ready": {"status": "ok", "reason": "ok", "bridge_up": True, "mt4_connected": True},
        "state": {"pending_command_count": 0},
        "decision_snapshots": {"items": [{"vol": 0.1, "decisions_json": [], "diagnostics_json": {}}]},
    }

    first = capture_baseline_pack.normalize_capture(payloads)
    second = capture_baseline_pack.normalize_capture(payloads)
    assert first == second
