from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "windows" / "22_manage_scalp_runtime_task.ps1"
LAUNCHER = ROOT / "ops" / "windows" / "21_start_scalp_runtime.bat"
TASK_LAUNCHER = ROOT / "ops" / "windows" / "21_run_scalp_runtime_task.ps1"


def _powershell(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(SCRIPT),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )


def test_scalp_runtime_task_script_parses_and_defaults_to_read_only_status() -> None:
    parser = (
        "$errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',"
        "[ref]$null,[ref]$errors); "
        "if($errors.Count -gt 0){$errors | ForEach-Object { Write-Error $_ }; exit 1}"
    )
    parsed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", parser],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert parsed.returncode == 0, parsed.stderr

    source = SCRIPT.read_text(encoding="utf-8")
    assert '[string]$Action = "Status"' in source

    status = _powershell()
    assert status.returncode == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["schema_version"] == "fxstack.scalp_runtime_scheduled_task_status.v1"
    assert payload["task_name"] == "TradingAgentScalpRuntime"
    assert payload["task_path"] == "\\"
    assert payload["expected_action"]["execute"].lower().endswith(
        "\\windowspowershell\\v1.0\\powershell.exe"
    )
    assert payload["expected_action"]["arguments"] == (
        "-NoProfile -NonInteractive -WindowStyle Hidden "
        f'-ExecutionPolicy Bypass -File "{TASK_LAUNCHER}"'
    )
    assert Path(payload["expected_action"]["working_directory"]) == ROOT


def test_scalp_runtime_task_contract_is_exact_current_user_and_persistent() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert (
        '[ValidateSet("Status", "Register", "Start", "Stop", "Disable", '
        '"Unregister")]' in source
    )
    assert '$TaskName = "TradingAgentScalpRuntime"' in source
    assert '$TaskPath = "\\"' in source
    assert '"ops\\windows\\21_start_scalp_runtime.bat"' in source
    assert '"ops\\windows\\21_run_scalp_runtime_task.ps1"' in source
    assert "-WindowStyle Hidden" in source
    assert "$LegacyArguments = '/d /c \"\"' + $LauncherPath + '\" --run\"'" in source
    assert "$legacyActionMatches" in source
    assert "-WorkingDirectory $RepositoryRoot" in source
    assert "New-ScheduledTaskTrigger -AtLogOn -User $CurrentUserName" in source
    assert "-LogonType Interactive" in source
    assert "-RunLevel Limited" in source
    assert "-AllowStartIfOnBatteries" in source
    assert "-DontStopIfGoingOnBatteries" in source
    assert "-StartWhenAvailable" in source
    assert "-RestartCount 10" in source
    assert "-RestartInterval (New-TimeSpan -Minutes 1)" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in source
    assert "[bool]$settings.AllowDemandStart" in source
    assert "[bool]$settings.Enabled" in source
    assert "enabled = $true" in source
    assert "[string]$settings.RestartInterval -eq \"PT1M\"" in source
    assert "[string]$settings.ExecutionTimeLimit -eq \"PT0S\"" in source
    assert "-not [bool]$settings.DisallowStartIfOnBatteries" in source
    assert "-not [bool]$settings.StopIfGoingOnBatteries" in source


def test_scalp_runtime_task_mutations_require_owned_identity_and_are_reversible() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "FXSTACK_OWNER=signed_validation_scalp_runtime_task_v4" in source
    assert "direct_demo" not in source
    assert "Get-ExistingOwnedTask" in source
    assert "scheduled_task_identity_mismatch_refusing_mutation" in source
    assert "scheduled_task_identity_mismatch_refusing_overwrite" in source
    assert "registered_task_contract_verification_failed" in source
    assert "scalp_runtime_scheduled_task_running_stop_before_unregister" in source
    assert "scalp_runtime_scheduled_task_stop_timeout" in source

    for command in (
        "Register-ScheduledTask",
        "Set-ScheduledTask",
        "Stop-ScheduledTask",
        "Disable-ScheduledTask",
        "Unregister-ScheduledTask",
    ):
        assert command in source

    assert "Enable-ScheduledTask" in source
    assert "Start-ScheduledTask" in source
    assert "scalp_runtime_scheduled_task_disarmed_signed_release_required" not in source
    assert source.index("Enable-ScheduledTask `") < source.index(
        "$registered = Get-ScheduledTask `"
    )

    ownership_check = source.index(
        "scheduled_task_identity_mismatch_refusing_overwrite"
    )
    register_mutation = source.index("Register-ScheduledTask `")
    assert ownership_check < register_mutation

    unregister_block = source[source.index('if ($Action -eq "Unregister")') :]
    assert unregister_block.index("Get-ExistingOwnedTask") < unregister_block.index(
        "Unregister-ScheduledTask"
    )
    assert unregister_block.index(
        "scalp_runtime_scheduled_task_running_stop_before_unregister"
    ) < unregister_block.index("Unregister-ScheduledTask")

    stop_block = source[
        source.index('if ($Action -eq "Stop")') : source.index(
            'if ($Action -eq "Disable")'
        )
    ]
    assert stop_block.index("Stop-ScheduledTask") < stop_block.index(
        "Wait-TaskNotRunning"
    )


def test_scalp_runtime_task_has_no_unrelated_operational_surface() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    lowered = source.lower()

    forbidden = (
        "19_start_mt4",
        "20_start_bridge",
        "terminal.exe",
        "bridge_api_key",
        "bridge_command_token",
        "get-content",
        "set-content",
        "remove-item",
        "postgres",
        "registry_root",
        "run_causal",
        "mtvclc",
        "13_train",
        "14_activate",
        "taskkill",
        "stop-process",
    )
    for token in forbidden:
        assert token not in lowered

    assert re.search(r"(?i)start-process", source) is None
    assert re.search(r"(?i)invoke-restmethod", source) is None


def test_scalp_runtime_task_launcher_is_hidden_narrow_and_parseable() -> None:
    source = TASK_LAUNCHER.read_text(encoding="utf-8")
    parser = (
        "$errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{TASK_LAUNCHER}',"
        "[ref]$null,[ref]$errors); "
        "if($errors.Count -gt 0){$errors | ForEach-Object { Write-Error $_ }; exit 1}"
    )
    parsed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", parser],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )

    assert parsed.returncode == 0, parsed.stderr
    assert '"21_start_scalp_runtime.bat"' in source
    assert "--run" in source
    assert "exit [int]$LASTEXITCODE" in source
    for forbidden in ("19_start_mt4", "bridge_api_key", "Start-Process", "Stop-Process"):
        assert forbidden not in source
