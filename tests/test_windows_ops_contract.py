from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "ops" / "windows"


def test_launch_and_consumers_share_selected_endpoint_contract() -> None:
    launch = (ROOT / "launch_all.bat").read_text(encoding="utf-8")
    env = (WINDOWS / "_env.bat").read_text(encoding="utf-8")
    monitor = (WINDOWS / "25_monitor_everything.ps1").read_text(encoding="utf-8")
    stop = (WINDOWS / "90_stop_all.bat").read_text(encoding="utf-8")

    assert "resolve_stack_endpoints.ps1" in launch
    assert "active_stack_env.bat" in launch and "active_stack_env.bat" in env
    assert env.index("installed_env.bat") < env.index("active_stack_env.bat")
    assert "--background 58710" not in launch
    assert "--background 3000" not in launch
    assert "%TRADER_BRIDGE_PORT%" in launch
    assert "%TRADER_DASHBOARD_PORT%" in launch
    status_block = launch.split(":status", 1)[1].split(":endpoints", 1)[0]
    assert status_block.count("/v2/ready") == 1
    assert "-Headers $bridgeHeaders" in monitor
    assert "%TRADER_BRIDGE_PORT% %TRADER_DASHBOARD_PORT%" in stop
    assert 'del /q "%ROOT%\\logs\\active_stack_env.bat"' in stop


def test_windows_worker_cleanup_requires_repo_ownership_marker() -> None:
    for name in ("20_start_bridge.bat", "23_start_monitor.bat"):
        text = (WINDOWS / name).read_text(encoding="utf-8")
        assert "--instance-root" in text
        assert "$owned -and" in text
    runtime = (WINDOWS / "21_start_runtime.bat").read_text(encoding="utf-8")
    assert "--instance-root" in runtime
    assert "find_owned_instance_processes.ps1" in runtime
    stop = (WINDOWS / "90_stop_all.bat").read_text(encoding="utf-8")
    assert "$owned -and $worker" in stop
    assert "FXSTACK_STOP_KILL_ALL_PYTHON" in stop  # global kill remains explicit opt-in only
    assert "if(-not $owned -and $name" not in stop
    for path in WINDOWS.glob("*.bat"):
        assert "|| exit /b %errorlevel%" not in path.read_text(encoding="utf-8"), path.name


def test_candidate_runtime_and_feature_worker_have_isolated_instance_state() -> None:
    runtime = (WINDOWS / "21_start_runtime.bat").read_text(encoding="utf-8")
    candidate = (WINDOWS / "24_start_candidate_stack.bat").read_text(encoding="utf-8")
    worker = (WINDOWS / "24_start_feature_push_worker.bat").read_text(encoding="utf-8")

    assert "find_owned_instance_processes.ps1" in runtime
    assert '-Role runtime -InstanceId "%TARGET_INSTANCE%"' in runtime
    assert "--instance-id %INSTANCE_ID%" in runtime
    assert "runtime_%INSTANCE_ID%_%BRIDGE_PORT%" in runtime
    assert "%FXSTACK_CANDIDATE_INSTANCE_ID%" in candidate
    assert "active_candidate_env.bat" in candidate
    assert "FXSTACK_CANDIDATE_INSTANCE_ID=" in candidate
    assert "%FXSTACK_CANDIDATE_BRIDGE_PORT% %FXSTACK_CANDIDATE_INSTANCE_ID%" in candidate
    assert "find_owned_instance_processes.ps1" in worker
    assert '-Role feature-push -InstanceId "%TARGET_INSTANCE%"' in worker
    assert "feature_push_worker_%INSTANCE_ID%" in worker
    assert "--instance-id=%INSTANCE_ID%" in worker
    assert "INSTANCE_WORKER_ID" in worker


@pytest.mark.skipif(os.name != "nt", reason="Windows process identity selector contract")
def test_instance_process_selector_never_claims_coexisting_stack(tmp_path: Path) -> None:
    root = str(ROOT)
    snapshot = [
        {
            "ProcessId": 101,
            "CommandLine": (
                f'python -m src.trader.cli runtime run --instance-root "{root}" '
                "--instance-id baseline"
            ),
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        },
        {
            "ProcessId": 102,
            "CommandLine": (
                f'python -m src.trader.cli runtime run --instance-root "{root}" '
                "--instance-id candidate"
            ),
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        },
        {
            "ProcessId": 103,
            "CommandLine": f'python -m src.trader.cli runtime run --instance-root "{root}"',
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        },
        {
            "ProcessId": 104,
            "CommandLine": (
                'python -m src.trader.cli runtime run --instance-root "D:\\foreign" '
                "--instance-id candidate"
            ),
            "ExecutablePath": "D:\\foreign\\python.exe",
        },
        {
            "ProcessId": 105,
            "CommandLine": (
                f'python -m src.trader.cli runtime run --instance-root "{root}-copy" '
                "--instance-id candidate"
            ),
            "ExecutablePath": f"{root}-copy\\fx-quant-stack\\.venv\\Scripts\\python.exe",
        },
        {
            "ProcessId": 201,
            "CommandLine": (
                f'cmd /c "{WINDOWS / "24_start_feature_push_worker.bat"}" '
                "--run 5 --instance-id=baseline"
            ),
            "ExecutablePath": "C:\\Windows\\System32\\cmd.exe",
        },
        {
            "ProcessId": 202,
            "CommandLine": (
                f'cmd /c "{WINDOWS / "24_start_feature_push_worker.bat"}" '
                "--run 5 --instance-id=candidate"
            ),
            "ExecutablePath": "C:\\Windows\\System32\\cmd.exe",
        },
        {
            "ProcessId": 203,
            "CommandLine": (
                f'python "{WINDOWS / "feature_push_worker_loop.py"}" '
                "--instance-id candidate"
            ),
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        },
        {
            "ProcessId": 204,
            "CommandLine": (
                f'python "{WINDOWS / "feature_push_worker_loop.py"}" '
                "--instance-id baseline"
            ),
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        },
    ]
    snapshot_path = tmp_path / "processes.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    selector = WINDOWS / "find_owned_instance_processes.ps1"

    def selected(role: str, instance: str, process_id: int = 0) -> set[int]:
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(selector),
            "-Root",
            root,
            "-Role",
            role,
            "-InstanceId",
            instance,
            "-SnapshotPath",
            str(snapshot_path),
        ]
        if process_id:
            command.extend(["-ProcessId", str(process_id)])
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
        return {int(line) for line in completed.stdout.splitlines() if line.strip()}

    assert selected("runtime", "candidate") == {102}
    assert selected("runtime", "baseline") == {101, 103}
    assert selected("runtime", "candidate", process_id=101) == set()
    assert selected("feature-push", "candidate") == {202, 203}
    assert selected("feature-push", "baseline") == {201, 204}


def test_safe_operator_defaults_and_local_auth_contract_are_exported() -> None:
    env = (WINDOWS / "_env.bat").read_text(encoding="utf-8")
    for fragment in (
        'FXSTACK_AGENT_MODE=shadow',
        'FXSTACK_BRIDGE_AUTH_REQUIRED=1',
        'FXSTACK_MCP_ENABLED=0',
        'FXSTACK_OPENCLAW_ENABLED=0',
        'FXSTACK_AGENT_ALLOW_REMOTE_LLM=0',
        'FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS=0',
        'ensure_local_bridge_key.ps1',
    ):
        assert fragment in env
    assert 'FXSTACK_SKIP_INSTALLED_ENV%"=="1"' in env
    installed_call = 'if "%LOAD_INSTALLED_ENV%"=="1" if exist "%ROOT%\\ops\\windows\\installed_env.bat"'
    assert env.index("FXSTACK_SKIP_INSTALLED_ENV") < env.index(installed_call)


@pytest.mark.skipif(os.name != "nt", reason="Windows batch isolated-audit contract")
def test_skip_installed_env_preserves_process_supplied_safe_audit_settings() -> None:
    secret_markers = ("CREDENTIAL", "KEY", "PASSWORD", "SECRET", "TOKEN")
    process_env = {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in secret_markers)
    }
    process_env.update(
        {
            "FXSTACK_SKIP_INSTALLED_ENV": "1",
            "FXSTACK_AGENT_MODE": "shadow",
            "FXSTACK_DATABASE_URL": "sqlite+pysqlite:///isolated_audit.db",
            "FXSTACK_ALLOW_SQLITE": "1",
            "FXSTACK_BRIDGE_API_KEY": "isolated-test-key",
            "FXSTACK_FEAST_ENABLED": "0",
            "FXSTACK_FEATURE_PUSH_ENABLED": "0",
        }
    )
    command = (
        "call ops\\windows\\_env.bat >nul 2>&1 && "
        "echo !FXSTACK_AGENT_MODE!;!FXSTACK_DATABASE_URL!;!FXSTACK_ALLOW_SQLITE!;"
        "!FXSTACK_FEAST_ENABLED!;!FXSTACK_FEATURE_PUSH_ENABLED!"
    )
    completed = subprocess.run(
        ["cmd.exe", "/d", "/v:on", "/c", command],
        check=False,
        capture_output=True,
        text=True,
        env=process_env,
        cwd=ROOT,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "shadow;sqlite+pysqlite:///isolated_audit.db;1;0;0"


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell endpoint contract")
def test_endpoint_resolver_skips_occupied_ports_and_persists_selection(tmp_path: Path) -> None:
    listeners: list[socket.socket] = []
    blocked: list[int] = []
    for _ in range(2):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listeners.append(listener)
        blocked.append(int(listener.getsockname()[1]))
    state_file = tmp_path / "active_stack_env.bat"
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(WINDOWS / "resolve_stack_endpoints.ps1"),
                "-BridgePort",
                str(blocked[0]),
                "-DashboardPort",
                str(blocked[1]),
                "-StateFile",
                str(state_file),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        for listener in listeners:
            listener.close()
    bridge, dashboard = [int(value) for value in completed.stdout.strip().split("|")]
    assert bridge not in blocked
    assert dashboard not in blocked
    assert bridge != dashboard
    persisted = state_file.read_text(encoding="utf-8")
    assert f"TRADER_BRIDGE_PORT={bridge}" in persisted
    assert f"TRADER_DASHBOARD_PORT={dashboard}" in persisted
    assert "if not defined TRADER_BRIDGE_PORT" not in persisted


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell local-key contract")
def test_local_bridge_key_is_random_shape_and_stable(tmp_path: Path) -> None:
    key_file = tmp_path / "bridge_api_key.txt"
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(WINDOWS / "ensure_local_bridge_key.ps1"),
        "-KeyFile",
        str(key_file),
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    second = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    assert first == second
    assert re.fullmatch(r"[a-f0-9]{64}", first)
