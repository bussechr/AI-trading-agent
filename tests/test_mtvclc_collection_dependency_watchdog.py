from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENSURE = ROOT / "ops" / "windows" / "28_ensure_mtvclc_collection_dependencies.ps1"
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
