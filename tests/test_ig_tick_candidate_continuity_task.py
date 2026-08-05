from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "ops" / "windows" / "30_checkpoint_ig_tick_microstructure_candidate.ps1"
REGISTRAR = ROOT / "ops" / "windows" / "30_manage_ig_tick_microstructure_continuity_task.ps1"


def _parse(path: Path) -> None:
    command = "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile('" + str(path) + "',[ref]$null,[ref]$e); if($e.Count){$e|%{Write-Error $_};exit 1}"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_wrapper_is_hash_pinned_collection_only() -> None:
    _parse(WRAPPER)
    source = WRAPPER.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "check_ig_tick_history_readiness.py" in source
    assert "check_ig_tick_microstructure_candidate_continuity.py" in source
    assert "ExpectedContinuityToolSha256" in source
    assert "ExpectedCaptureToolSha256" in source
    assert "--api-key-file $KeyFile" in source
    assert "--checkpoint-root $CheckpointDirectory" in source
    assert "/v2/commands" not in lowered
    assert "start-process" not in lowered
    assert "runtime.runner" not in lowered
    assert "order_authorized" not in lowered


def test_registrar_is_reversible_and_ignore_new() -> None:
    _parse(REGISTRAR)
    source = REGISTRAR.read_text(encoding="utf-8")
    lowered = source.lower()
    assert '[ValidateSet("Install", "Preview", "Remove")]' in source
    assert "TradingAgentIgTickCandidateContinuity" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-RepetitionInterval" in source
    assert "prospective_window.end_utc_exclusive" in source
    assert "prospective_window_too_close_or_ended" in source
    assert "scheduled_task_identity_mismatch_refusing_overwrite" in source
    assert "scheduled_task_identity_mismatch_refusing_remove" in source
    assert "Unregister-ScheduledTask" in source
    assert "Register-ScheduledTask" in source
    assert "start-scheduledtask" not in lowered
    assert "start-process" not in lowered
    assert "stop-process" not in lowered
