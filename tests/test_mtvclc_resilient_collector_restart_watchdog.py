from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENSURE = ROOT / "ops" / "windows" / "29_ensure_mtvclc_collector_resilient.ps1"
REGISTER = (
    ROOT / "ops" / "windows" / "29_register_mtvclc_collector_resilient_watchdog.ps1"
)


def _parse_powershell(path: Path) -> None:
    command = (
        "$errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}',"
        "[ref]$null,[ref]$errors); "
        "if($errors.Count -gt 0){$errors | ForEach-Object { Write-Error $_ }; exit 1}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def _guard_report(**overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": "fxstack.mtvclc_collector_guard_status.resilient.v1",
        "status": "stopped_during_window",
        "reason": "collector_writer_absent",
        "writer_group_count": 0,
        "supervisor_lock_state": "available",
        "supervisor_lock_held": False,
        "prospective_end_epoch_exclusive": 4_102_444_800,
        "collection_only": True,
        "evaluation_performed": False,
        "signal_computation_authorized": False,
        "outcome_access_authorized": False,
        "performance_computation_authorized": False,
        "success_claim_authorized": False,
        "issuer_authorized": False,
        "signature_authorized": False,
        "authority_granted": False,
        "runtime_authorized": False,
        "activation_authorized": False,
        "broker_access_authorized": False,
        "order_authorized": False,
    }
    report.update(overrides)
    return report


def _run_with_stub_guard(
    tmp_path: Path,
    *,
    report: dict[str, object] | None,
    health_exit_code: int,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo = tmp_path / "repo"
    windows = repo / "ops" / "windows"
    windows.mkdir(parents=True)
    ensure_copy = windows / ENSURE.name
    shutil.copy2(ENSURE, ensure_copy)
    output = tmp_path / "capture"
    output.mkdir()
    marker = output / "stub-started"
    if report is None:
        health_body = "Write-Output 'not-json'; exit 2"
    else:
        encoded = base64.b64encode(json.dumps(report).encode()).decode()
        health_body = f"""
        if (Test-Path -LiteralPath (Join-Path $OutputDir 'stub-started')) {{
            $payload = [pscustomobject]@{{
                schema_version = 'fxstack.mtvclc_collector_guard_status.resilient.v1'
                status = 'starting'
                writer_group_count = 1
                supervisor_lock_state = 'held'
                supervisor_lock_held = $true
                prospective_end_epoch_exclusive = 4102444800
                collection_only = $true
                evaluation_performed = $false
                signal_computation_authorized = $false
                outcome_access_authorized = $false
                performance_computation_authorized = $false
                success_claim_authorized = $false
                issuer_authorized = $false
                signature_authorized = $false
                authority_granted = $false
                runtime_authorized = $false
                activation_authorized = $false
                broker_access_authorized = $false
                order_authorized = $false
            }}
            Write-Output ($payload | ConvertTo-Json -Compress)
            exit 0
        }}
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
        Write-Output $json
        exit {health_exit_code}
        """
    stub = windows / "27_guard_mtvclc_collector_resilient.ps1"
    stub.write_text(
        f"""
        param(
            [string]$Action,
            [string]$PythonExe,
            [string]$Preregistration,
            [string]$OutputDir,
            [string]$ApiKeyFile,
            [string]$BaseUrl,
            [double]$MaximumManifestAgeSeconds,
            [double]$StartupGraceSeconds
        )
        if ($Action -eq 'Health') {{ {health_body} }}
        if ($Action -eq 'StartOrResume') {{
            Set-Content -LiteralPath (Join-Path $OutputDir 'stub-started') -Value 'started'
            Start-Sleep -Seconds 3
            exit 0
        }}
        exit 9
        """,
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            "pwsh.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ensure_copy),
            "-PythonExe",
            str(tmp_path / "python.exe"),
            "-Preregistration",
            str(tmp_path / "prereg.json"),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(tmp_path / "key.txt"),
            "-StartConfirmationSeconds",
            "10",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo,
        timeout=20,
    )
    return completed, marker


def test_watchdog_parses_and_only_restarts_after_exact_absent_writer_health() -> None:
    _parse_powershell(ENSURE)
    source = ENSURE.read_text(encoding="utf-8")

    assert "27_guard_mtvclc_collector_resilient.ps1" in source
    assert 'New-GuardArguments "Health"' in source
    assert 'New-GuardArguments "StartOrResume"' in source
    assert "$health.ExitCode -eq 3" in source
    assert '@("stopped_before_t0", "stopped_during_window")' in source
    assert '[string]$report.reason -eq "collector_writer_absent"' in source
    assert "[int]$report.writer_group_count -eq 0" in source
    assert '@("absent", "available") -contains $lockState' in source
    assert 'reason = "prospective_window_not_open"' in source

    health_index = source.index('New-GuardArguments "Health"')
    proof_index = source.index("$restartAllowed =")
    start_index = source.index('New-GuardArguments "StartOrResume"')
    assert health_index < proof_index < start_index

    lowered = source.lower()
    assert "get-content" not in lowered
    assert "readalltext" not in lowered
    assert "register-scheduledtask" not in lowered
    assert "start-process" in lowered
    assert "-windowstyle hidden" in lowered
    assert "restart_confirmed" in source
    assert "StartConfirmationSeconds" in source
    assert "System32\\WindowsPowerShell\\v1.0\\powershell.exe" in source
    assert "stop-process" not in lowered
    assert "taskkill" not in lowered
    assert re.search(r"--api-key(?:\s|\")", source) is None
    assert '"-ApiKeyFile", $ApiKeyFile' in source
    assert "signal_computation_authorized" in source
    assert "performance_computation_authorized" in source
    assert "success_claim_authorized" in source
    assert "issuer_authorized" in source
    assert "signature_authorized" in source
    assert "order_authorized" in source


def test_watchdog_decision_executes_only_exact_restartable_health(
    tmp_path: Path,
) -> None:
    allowed, marker = _run_with_stub_guard(
        tmp_path / "allowed",
        report=_guard_report(),
        health_exit_code=3,
    )
    assert allowed.returncode == 0, allowed.stderr
    assert '"status":"restart_confirmed"' in allowed.stdout
    assert marker.is_file()

    cases = [
        (_guard_report(supervisor_lock_state="held", supervisor_lock_held=True), 3),
        (
            _guard_report(
                status="collector_activity_stale",
                reason="",
                writer_group_count=1,
                supervisor_lock_state="held",
                supervisor_lock_held=True,
            ),
            3,
        ),
        (_guard_report(signature_authorized=True), 3),
        (None, 2),
    ]
    for index, (report, exit_code) in enumerate(cases):
        refused, refused_marker = _run_with_stub_guard(
            tmp_path / f"refused-{index}",
            report=report,
            health_exit_code=exit_code,
        )
        assert refused.returncode != 0
        assert not refused_marker.exists()


def test_task_installer_is_explicit_reversible_and_single_instance() -> None:
    _parse_powershell(REGISTER)
    source = REGISTER.read_text(encoding="utf-8")

    assert '[ValidateSet("Install", "Preview", "Remove")]' in source
    assert '[ValidateSet("AtLogOn", "AtStartup")]' in source
    assert '[string]$TriggerMode = "AtLogOn"' in source
    assert "29_ensure_mtvclc_collector_resilient.ps1" in source
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUserName" in source
    assert "New-ScheduledTaskTrigger -AtStartup" in source
    assert "-RepetitionInterval" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "Unregister-ScheduledTask" in source
    assert "scheduled_task_identity_mismatch_refusing_remove" in source
    assert "Test-OwnedScheduledTask" in source
    assert "[Security.Principal.NTAccount]::new($taskUser).Translate" in source
    assert '$TaskPath = "\\"' in source
    assert "[string]$Task.TaskPath -eq $TaskPath" in source
    assert "Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName" in source
    assert "-TaskPath $TaskPath" in source
    assert "scheduled_task_mutation_requires_elevated_administrator" in source
    assert "scheduled_task_already_exists_remove_it_explicitly_before_install" in source
    assert "Register-ScheduledTask" in source
    register_call = source[source.index("Register-ScheduledTask") :]
    assert re.search(r"Register-ScheduledTask[^\r\n]*-Force", register_call) is None

    lowered = source.lower()
    assert "get-content" not in lowered
    assert "readalltext" not in lowered
    assert "start-scheduledtask" not in lowered
    assert "stop-scheduledtask" not in lowered
    assert "start-process" not in lowered
    assert "stop-process" not in lowered
    assert "taskkill" not in lowered
    assert "fxstack_bridge_api_key" not in lowered
    assert "-apikeyfile" in lowered


def test_task_preview_builds_non_admin_logon_definition(tmp_path: Path) -> None:
    preregistration = tmp_path / "preregistration.json"
    api_key = tmp_path / "key.txt"
    output = tmp_path / "capture"
    preregistration.write_text("{}", encoding="utf-8")
    api_key.write_text("not-read-by-preview", encoding="utf-8")
    output.mkdir()

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REGISTER),
            "-Action",
            "Preview",
            "-PythonExe",
            sys.executable,
            "-Preregistration",
            str(preregistration),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(api_key),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    preview = json.loads(completed.stdout)
    assert preview["trigger_mode"] == "AtLogOn"
    assert preview["task_path"] == "\\"
    assert preview["logon_trigger"] is True
    assert preview["startup_trigger"] is False
    assert preview["principal_logon_type"] == "Interactive"
    assert preview["principal_run_level"] == "Limited"
    assert preview["multiple_instances"] == "IgnoreNew"
    assert preview["mutation_performed"] is False
    assert "-ExpectedWatchdogSha256" in preview["arguments"]
    assert "-ExpectedGuardSha256" in preview["arguments"]
    assert "not-read-by-preview" not in completed.stdout
