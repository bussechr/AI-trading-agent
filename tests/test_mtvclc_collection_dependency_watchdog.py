from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENSURE = ROOT / "ops" / "windows" / "28_ensure_mtvclc_collection_dependencies.ps1"
REGISTER = (
    ROOT
    / "ops"
    / "windows"
    / "28_register_mtvclc_collection_dependencies_watchdog.ps1"
)
BRIDGE = ROOT / "ops" / "windows" / "20_start_bridge.bat"
MT4 = ROOT / "ops" / "windows" / "19_start_mt4.ps1"

EXPECTED_SYMBOLS = [
    "EURUSD",
    "USDJPY",
    "AUDUSD",
    "GBPUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "NZDUSD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "GBPCAD",
    "GBPCHF",
    "GBPJPY",
    "BTCUSD",
    "ETHUSD",
    "AUDCAD",
    "NZDJPY",
]


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


def test_dependency_ensure_is_exact_collection_only_and_fail_closed() -> None:
    _parse_powershell(ENSURE)
    source = ENSURE.read_text(encoding="utf-8")
    lowered = source.lower()

    symbols_block = source[
        source.index("$ExpectedSymbols = @(") : source.index(")\n$RepositoryRoot")
    ]
    assert re.findall(r'"([A-Z]{6})"', symbols_block) == EXPECTED_SYMBOLS

    assert '$ExpectedBaseUrl = "http://127.0.0.1:58710"' in source
    assert "$ExpectedBridgePort = 58710" in source
    assert '"/v2/handshake"' in source
    assert '"/v2/ready"' in source
    assert '"/v2/state"' in source
    assert '"X-API-Key" = $ApiKey' in source
    assert '[string]$state.Body.broker_venue_id -ne "ig_mt4"' in source
    assert '[string]$state.Body.broker_account_mode -ne "demo"' in source
    assert '[string]$state.Body.broker_server -ne "IG-DEMO"' in source
    assert "bridge_pid_marker_identity_mismatch" in source
    assert "bridge_listener_repository_ownership_mismatch" in source
    assert "foreign_or_unconfigured_mt4_terminal_refused" in source
    assert '"--background-if-absent"' in source
    assert "Test-InteractiveDesktopAvailable" in source
    assert "-File $Mt4LauncherPath" in source
    assert "AuditOnly" in source

    assert "21_start_runtime" not in lowered
    assert "fxstack.runtime.runner" not in lowered
    assert "launch_all.bat" not in lowered
    assert "/v2/commands" not in lowered
    assert "stop-process" not in lowered
    assert "taskkill" not in lowered
    assert "start-scheduledtask" not in lowered
    assert "order_authorized" not in lowered
    assert '$Payload["runtime_start_attempted"] = $false' in source
    assert '$Payload["api_key_emitted"] = $false' in source
    assert 'status = "bridge_unready_refused_no_process_stop"' in source


def test_absent_only_bridge_mode_cannot_enter_reset_path() -> None:
    source = BRIDGE.read_text(encoding="utf-8")
    lowered = source.lower()
    safe_branch = lowered[lowered.index(":bg_if_absent") : lowered.index("\n:bg\n")]
    safe_guard = lowered[
        lowered.index(":require_bridge_absent") : lowered.index("\n:run\n")
    ]

    assert "--background-if-absent" in source
    assert "call :require_bridge_absent %port%" in safe_branch
    assert "goto bg_start" in safe_branch
    assert "reset_bridge_processes" not in safe_branch
    assert "taskkill" not in safe_guard
    assert "stop-process" not in safe_guard
    assert "get-nettcpconnection" in safe_guard
    assert "get-ciminstance win32_process" in safe_guard
    assert "-windowstyle hidden" in lowered
    assert "PINNED_API_KEY_FILE" in source
    assert "PINNED_PYTHON" in source

    mt4_source = MT4.read_text(encoding="utf-8").lower()
    assert "start-process -filepath $terminalpath" in mt4_source
    assert "-windowstyle hidden" not in mt4_source


def test_task_registrar_is_reversible_hash_pinned_and_identity_safe() -> None:
    _parse_powershell(REGISTER)
    source = REGISTER.read_text(encoding="utf-8")
    lowered = source.lower()

    assert '[ValidateSet("Install", "Preview", "Remove")]' in source
    assert '[ValidateSet("AtLogOn", "AtStartup")]' in source
    assert '[string]$TriggerMode = "AtLogOn"' in source
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUserName" in source
    assert "New-ScheduledTaskTrigger -AtStartup" in source
    assert "-RepetitionInterval" in source
    assert "-StartWhenAvailable" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-LogonType Interactive" in source
    assert "scheduled_task_mutation_requires_elevated_administrator" in source
    assert "scheduled_task_identity_mismatch_refusing_remove" in source
    assert "scheduled_task_identity_mismatch_refusing_overwrite" in source
    assert "Test-OwnedScheduledTask" in source
    assert "Unregister-ScheduledTask" in source
    assert "Register-ScheduledTask" in source
    assert "-Force" in source
    assert "-ExpectedEnsureSha256" in source
    assert "-ExpectedBridgeLauncherSha256" in source
    assert "-ExpectedMt4LauncherSha256" in source
    assert "-ExpectedEnvSha256" in source

    ownership_check = source.index(
        "$null -ne $existing -and -not (Test-OwnedScheduledTask $existing)"
    )
    overwrite = source.rindex("Register-ScheduledTask")
    assert ownership_check < overwrite

    assert "readalltext" not in lowered
    assert "get-content" not in lowered
    assert "start-scheduledtask" not in lowered
    assert "stop-scheduledtask" not in lowered
    assert "start-process" not in lowered
    assert "stop-process" not in lowered
    assert "taskkill" not in lowered
    assert "fxstack_bridge_api_key" not in lowered


def test_task_preview_contains_paths_and_hashes_but_not_key_value(
    tmp_path: Path,
) -> None:
    python_exe = tmp_path / "repo-python.exe"
    terminal = tmp_path / "IG MetaTrader 4 Terminal" / "terminal.exe"
    api_key = tmp_path / "bridge-key.txt"
    python_exe.write_bytes(b"python-placeholder")
    terminal.parent.mkdir()
    terminal.write_bytes(b"terminal-placeholder")
    secret = "preview-must-not-read-or-emit-this-secret-value"
    api_key.write_text(secret, encoding="utf-8")

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
            str(python_exe),
            "-TerminalExe",
            str(terminal),
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
    assert preview["start_when_available"] is True
    assert preview["api_key_value_stored"] is False
    assert preview["mutation_performed"] is False
    assert "-ExpectedEnsureSha256" in preview["arguments"]
    assert "-ExpectedBridgeLauncherSha256" in preview["arguments"]
    assert "-ExpectedMt4LauncherSha256" in preview["arguments"]
    assert "-ExpectedEnvSha256" in preview["arguments"]
    assert str(api_key) in preview["arguments"]
    assert secret not in completed.stdout
