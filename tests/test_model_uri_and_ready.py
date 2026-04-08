from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.api import app as bridge_app
from fxstack.mlops.model_uri import artifact_ref_value, normalize_artifact_ref, resolve_model_artifact_path


def test_resolve_model_artifact_path_accepts_windows_separators(tmp_path: Path):
    model_dir = tmp_path / "artifacts" / "pair_model"
    model_dir.mkdir(parents=True)

    resolved = resolve_model_artifact_path(r"artifacts\pair_model", project_root=tmp_path)

    assert resolved == model_dir.resolve()


def test_artifact_ref_value_preserves_model_uri_inputs():
    ref = "models:/EURUSD@champion"

    assert artifact_ref_value(ref) == "models:/EURUSD@champion"
    assert artifact_ref_value({"model_uri": "models:/EURUSD@champion"}) == "models:/EURUSD@champion"
    assert normalize_artifact_ref({"uri": "models:/EURUSD@champion"})["model_uri"] == "models:/EURUSD@champion"


def test_ready_payload_surfaces_runtime_startup_failure(tmp_path: Path, monkeypatch):
    class _FakeService:
        def get_state(self) -> dict[str, object]:
            return {
                "system_status": "connected",
                "runtime_status": "failed",
                "runtime_cycle_age_secs": 1.0,
                "runtime_startup": {
                    "boot_id": "boot-1",
                    "booted_at": "2026-04-07T00:00:00+00:00",
                    "runtime_pid": 1234,
                    "phase": "model_load",
                    "phase_pair": "EURUSD",
                    "phase_index": 1,
                    "phase_total": 2,
                    "last_progress_ts": 123.0,
                    "failure_reason": "TimeoutError:model_load_timeout",
                    "failed_at": "2026-04-07T00:00:01+00:00",
                    "pending_command_policy": "purge_and_mark_stale",
                },
                "runtime_failure_reason": "TimeoutError:model_load_timeout",
                "runtime_last_progress_age_secs": 125.0,
                "runtime_phase": "model_load",
                "runtime_phase_pair": "EURUSD",
                "runtime_status": "failed",
                "runtime_boot_id": "boot-1",
            }

        def get_health(self) -> dict[str, object]:
            return {"tables_ok": True}

        def get_metrics(self) -> dict[str, object]:
            return {}

    monkeypatch.setattr(bridge_app, "service", _FakeService())

    ready = bridge_app._ready_payload()

    assert ready["reason"] == "runtime_startup_failed"
    assert ready["runtime_ready"] is False
    assert ready["runtime_status"] == "failed"
    assert ready["runtime_phase"] == "model_load"
    assert ready["runtime_failure_reason"] == "TimeoutError:model_load_timeout"
    assert ready["status_tier"] == "bridge_up_runtime_failed"
