from __future__ import annotations

import ast
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "ops" / "windows"


def _isolated_launch_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    launch_keys = {
        "FXSTACK_START_PROFILE",
        "FXSTACK_AGENT_MODE",
        "FXSTACK_LIVE_ARMED",
        "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST",
        "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST",
        "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST",
        "FXSTACK_EQUITY_LOTS_PER_USD",
        "FXSTACK_DEFAULT_ORDER_LOTS",
        "FXSTACK_MIN_ORDER_LOTS",
        "FXSTACK_ORDER_LOT_STEP",
        "FXSTACK_MAX_ORDER_LOTS",
        "FXSTACK_RISK_MAX_DRAWDOWN_PCT",
        "FXSTACK_RISK_MAX_GROSS_EXPOSURE",
        "FXSTACK_RISK_MAX_NET_EXPOSURE",
        "FXSTACK_PROJECT_ROOT",
    }
    process_env = {name: value for name, value in os.environ.items() if name not in launch_keys}
    process_env.update(
        {
            "FXSTACK_SKIP_INSTALLED_ENV": "1",
            "FXSTACK_BRIDGE_API_KEY": "isolated-test-key",
        }
    )
    process_env.update(dict(overrides or {}))
    return process_env


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
    live_block = launch.split(":live", 1)[1].split(":full", 1)[0]
    assert live_block.index('set "STEP=sync_python"') < live_block.index(
        'set "STEP=resolve_endpoints"'
    )
    assert live_block.index('set "STEP=start_bridge"') < live_block.index(
        'set "STEP=start_mt4"'
    ) < live_block.index('set "STEP=start_runtime"')
    mt4 = (WINDOWS / "19_start_mt4.ps1").read_text(encoding="utf-8")
    assert "FXSTACK_MT4_TERMINAL_EXE" in mt4
    assert "IG MetaTrader 4 Terminal" in mt4
    assert "Get-RunningTerminal" in mt4
    assert "Stop-Process" not in mt4
    installer = (ROOT / "tools" / "build_windows_installer.py").read_text(encoding="utf-8")
    assert '"19_start_mt4.ps1"' in installer
    status_block = launch.split(":status", 1)[1].split(":endpoints", 1)[0]
    assert status_block.count("/v2/ready") == 1
    assert "-Headers $bridgeHeaders" in monitor
    assert "%TRADER_BRIDGE_PORT% %TRADER_DASHBOARD_PORT%" in stop
    assert 'del /q "%ROOT%\\logs\\active_stack_env.bat"' in stop


def test_windows_worker_cleanup_requires_repo_ownership_marker() -> None:
    for name in ("20_start_bridge.bat", "23_start_monitor.bat"):
        text = (WINDOWS / name).read_text(encoding="utf-8")
        assert "$owned -and" in text
    runtime = (WINDOWS / "21_start_runtime.bat").read_text(encoding="utf-8")
    assert "--instance-root" in runtime
    assert "-I -u -m fxstack.runtime.runner" in runtime
    assert "-u -m src.trader.cli runtime run" not in runtime
    assert "find_owned_instance_processes.ps1" in runtime
    stop = (WINDOWS / "90_stop_all.bat").read_text(encoding="utf-8")
    assert "$owned -and $worker" in stop
    assert "FXSTACK_STOP_KILL_ALL_PYTHON" in stop  # global kill remains explicit opt-in only
    assert "if(-not $owned -and $name" not in stop
    for path in WINDOWS.glob("*.bat"):
        assert "|| exit /b %errorlevel%" not in path.read_text(encoding="utf-8"), path.name


def test_runtime_background_wait_is_bound_to_fresh_spawned_generation() -> None:
    runtime = (WINDOWS / "21_start_runtime.bat").read_text(encoding="utf-8")
    background = runtime.split("\n:bg\n", 1)[1].split("\n:wait_runtime\n", 1)[0]
    wait = runtime.split("\n:wait_runtime\n", 1)[1].split("\n:emit_runtime_failure_context\n", 1)[0]

    assert "PREVIOUS_RUNTIME_BOOT_ID" in background
    assert background.index("PREVIOUS_RUNTIME_BOOT_ID") < background.index("Start-Process")
    assert "EXPECTED_RUNTIME_PID" in background
    assert 'call :wait_runtime %BRIDGE_PORT% "!PREVIOUS_RUNTIME_BOOT_ID!" "!EXPECTED_RUNTIME_PID!"' in background
    assert "runtime_boot_id" in wait
    assert "runtime_startup_summary.runtime_pid" in wait
    assert 'set "RUNTIME_GENERATION_MATCH=0"' in wait
    assert 'if "!RUNTIME_GENERATION_MATCH!"=="1" if /I "!RUNTIME_STATUS!"=="failed"' in wait
    assert 'if "!RUNTIME_GENERATION_MATCH!"=="1" if /I "!RUNTIME_STATUS!"=="stalled"' in wait
    ready_guard = 'if "!RUNTIME_GENERATION_MATCH!"=="1" if "!RUNNING!"=="1"'
    assert ready_guard in wait
    assert "Get-Process -Id !EXPECTED_RUNTIME_PID!" in wait
    assert wait.index("Get-Process -Id !EXPECTED_RUNTIME_PID!") < wait.index(ready_guard)
    assert "exited before readiness" in wait


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime generation-bound readiness contract")
def test_runtime_wait_ignores_stale_generation_and_wrong_pid(tmp_path: Path) -> None:
    runtime = (WINDOWS / "21_start_runtime.bat").read_text(encoding="utf-8")
    wait = runtime.split("\n:wait_runtime\n", 1)[1].split("\n:emit_runtime_failure_context\n", 1)[0]
    dummy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    payloads = [
        {
            "runtime_ready": True,
            "runtime_status": "running",
            "runtime_phase": "main_loop",
            "runtime_phase_pair": "",
            "runtime_last_progress_age_secs": 0.1,
            "runtime_failure_reason": "",
            "runtime_boot_id": "old-boot",
            "runtime_startup_summary": {"runtime_pid": dummy.pid},
        },
        {
            "runtime_ready": True,
            "runtime_status": "running",
            "runtime_phase": "main_loop",
            "runtime_phase_pair": "",
            "runtime_last_progress_age_secs": 0.1,
            "runtime_failure_reason": "",
            "runtime_boot_id": "new-boot",
            "runtime_startup_summary": {"runtime_pid": dummy.pid + 1},
        },
        {
            "runtime_ready": False,
            "runtime_status": "failed",
            "runtime_phase": "model_load",
            "runtime_phase_pair": "EURUSD",
            "runtime_last_progress_age_secs": 0.1,
            "runtime_failure_reason": "rollout_not_configured",
            "runtime_boot_id": "new-boot",
            "runtime_startup_summary": {"runtime_pid": dummy.pid},
        },
    ]

    class ReadyHandler(BaseHTTPRequestHandler):
        requests_seen = 0

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            index = min(type(self).requests_seen, len(payloads) - 1)
            type(self).requests_seen += 1
            body = json.dumps(payloads[index]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ReadyHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = int(server.server_address[1])
    harness = tmp_path / "wait_runtime_harness.bat"
    harness.write_text(
        "\n".join(
            [
                "@echo off",
                "setlocal enabledelayedexpansion",
                f'set "BRIDGE_URL=http://127.0.0.1:{port}"',
                'set "FXSTACK_BRIDGE_API_KEY="',
                'set "FXSTACK_RUNTIME_STARTUP_TIMEOUT_SECS=5"',
                f'call :wait_runtime {port} "old-boot" "{dummy.pid}"',
                'set "WAIT_RESULT=!errorlevel!"',
                "exit /b !WAIT_RESULT!",
                ":wait_runtime",
                wait,
                ":cleanup_failed_start",
                "exit /b 0",
                ":emit_runtime_failure_context",
                "exit /b 0",
            ]
        ),
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            ["cmd.exe", "/d", "/v:on", "/c", "call", str(harness)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={**os.environ, "FXSTACK_BRIDGE_API_KEY": ""},
        )
    finally:
        dummy.terminate()
        dummy.wait(timeout=10)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=10)

    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 2, output
    assert ReadyHandler.requests_seen >= 3
    assert "runtime startup failed" in output
    assert "phase=model_load pair=EURUSD reason=rollout_not_configured" in output


def test_installed_python_entrypoints_and_cleanup_selectors_are_aligned() -> None:
    bridge = (WINDOWS / "20_start_bridge.bat").read_text(encoding="utf-8")
    monitor = (WINDOWS / "23_start_monitor.bat").read_text(encoding="utf-8")
    worker = (WINDOWS / "24_start_feature_push_worker.bat").read_text(encoding="utf-8")
    selector = (WINDOWS / "find_owned_instance_processes.ps1").read_text(encoding="utf-8")
    stop = (WINDOWS / "90_stop_all.bat").read_text(encoding="utf-8")

    assert "$arguments='-I -u -m uvicorn fxstack.api.app:app" in bridge
    assert '"%TRADER_PYTHON_EXE%" -I -u -m uvicorn fxstack.api.app:app' in bridge
    assert "$arguments='-I -u -m fxstack.runtime.monitor" in monitor
    assert '"%TRADER_PYTHON_EXE%" -I -u -m fxstack.runtime.monitor' in monitor
    assert "@('-I','-u','-m','fxstack.runtime.feature_push_worker'" in worker
    assert "feature_push_worker_loop.py" not in worker

    assert r"fxstack\.runtime\.runner" in selector
    assert r"fxstack\.runtime\.feature_push_worker" in selector
    for module_pattern in (
        "uvicorn fxstack.api.app:app",
        "fxstack.runtime.runner",
        "fxstack.runtime.feature_push_worker",
        "fxstack.runtime.monitor",
    ):
        assert module_pattern in stop


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
    assert "'--project-root','%ROOT%'" in worker
    assert "'--instance-id','%INSTANCE_ID%'" in worker
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
            "ProcessId": 106,
            "CommandLine": (
                f'python -I -u -m fxstack.runtime.runner --instance-root "{root}" '
                "--instance-id candidate"
            ),
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        },
        {
            "ProcessId": 107,
            "CommandLine": (
                f'python -I -u -m fxstack.runtime.runner --instance-root "{root}" '
                "--instance-id baseline"
            ),
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
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
        {
            "ProcessId": 205,
            "CommandLine": (
                f'python -I -u -m fxstack.runtime.feature_push_worker --project-root "{root}" '
                "--instance-id candidate"
            ),
            "ExecutablePath": str(ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe"),
        },
        {
            "ProcessId": 206,
            "CommandLine": (
                f'python -I -u -m fxstack.runtime.feature_push_worker --project-root "{root}" '
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

    assert selected("runtime", "candidate") == {102, 106}
    assert selected("runtime", "baseline") == {101, 103, 107}
    assert selected("runtime", "candidate", process_id=101) == set()
    assert selected("feature-push", "candidate") == {202, 203, 205}
    assert selected("feature-push", "baseline") == {201, 204, 206}


def test_safe_operator_defaults_and_local_auth_contract_are_exported() -> None:
    env = (WINDOWS / "_env.bat").read_text(encoding="utf-8")
    for fragment in (
        'FXSTACK_AGENT_MODE=shadow',
        'FXSTACK_BRIDGE_AUTH_REQUIRED=1',
        'FXSTACK_MCP_ENABLED=0',
        'FXSTACK_OPENCLAW_ENABLED=0',
        'FXSTACK_AGENT_ALLOW_REMOTE_LLM=0',
        'FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS=0',
        'FXSTACK_ADAPTIVE_SHADOW_ENABLED=0',
        'FXSTACK_USE_STRUCTURE_TIMING_SHADOW=1',
        'FXSTACK_USE_UNCERTAINTY_GATE=1',
        'FXSTACK_BELIEF_SHADOW_ENABLED=1',
        'FXSTACK_BELIEF_RUNTIME_REQUIRED=1',
        'FXSTACK_BELIEF_INFLUENCE_MODE=hard_gate',
        'FXSTACK_CAMPAIGN_MANAGER_ENABLED=1',
        'FXSTACK_CAMPAIGN_SHADOW_ONLY=0',
        'FXSTACK_CAPITAL_GOVERNANCE_ENABLED=1',
        'FXSTACK_EQUITY_LOTS_PER_USD=0.00001',
        'FXSTACK_MAX_ORDER_LOTS=0.10',
        'FXSTACK_RISK_MAX_DRAWDOWN_PCT=5.0',
        'FXSTACK_RISK_MAX_GROSS_EXPOSURE=0.30',
        'FXSTACK_RISK_MAX_NET_EXPOSURE=0.20',
        'FXSTACK_PROJECT_ROOT=%ROOT%',
        'ensure_local_bridge_key.ps1',
    ):
        assert fragment in env
    assert 'FXSTACK_SKIP_INSTALLED_ENV%"=="1"' in env
    installed_call = 'if "%LOAD_INSTALLED_ENV%"=="1" if exist "%ROOT%\\ops\\windows\\installed_env.bat"'
    assert env.index("FXSTACK_SKIP_INSTALLED_ENV") < env.index(installed_call)
    assert 'FXSTACK_LIVE_ARMED set "FXSTACK_LIVE_ARMED=0"' in env
    assert 'FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST=%FXSTACK_PAIRS%' not in env
    assert 'FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST=trend_pullback' not in env
    assert 'FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST=enter' not in env
    assert 'set "FXSTACK_PROJECT_ROOT=%FXSTACK_PROJECT_ROOT%"' in env


def test_windows_installer_requires_verified_noneditable_active_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import build_windows_installer

    monkeypatch.setattr(build_windows_installer, "REPO", tmp_path)
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    stack_root = tmp_path / "fx-quant-stack"
    stack_root.mkdir()

    with pytest.raises(RuntimeError, match="isolated runtime marker is missing"):
        build_windows_installer.active_runtime_venv()

    runtime = stack_root / ".venv_runtime"
    runtime.mkdir()
    (stack_root / ".venv_win.active").write_text(".venv_runtime\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="active runtime is not isolation-verified"):
        build_windows_installer.active_runtime_venv()

    (runtime / ".fxstack_runtime_isolated").write_text("verified\n", encoding="utf-8")
    site_packages = runtime / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    editable_hook = site_packages / "__editable__.fx_quant_stack-0.1.0.pth"
    editable_hook.write_text("source-tree-hook\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="editable source hook"):
        build_windows_installer.active_runtime_venv()

    editable_hook.unlink()
    source_hook = site_packages / "legacy_source.pth"
    source_hook.write_text(str(stack_root / "src") + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="editable source hook"):
        build_windows_installer.active_runtime_venv()

    source_hook.unlink()
    dist_info = site_packages / "fx_quant_stack-0.1.0.dist-info"
    dist_info.mkdir()
    direct_url = dist_info / "direct_url.json"
    direct_url.write_text(
        json.dumps(
            {
                "url": stack_root.as_uri(),
                "dir_info": {"editable": True},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="editable source hook"):
        build_windows_installer.active_runtime_venv()

    direct_url.write_text(
        json.dumps({"url": "file:///runtime/fx_quant_stack.whl", "archive_info": {}}),
        encoding="utf-8",
    )
    assert build_windows_installer.active_runtime_venv() == runtime.resolve()

    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [],
            2,
            "runtime distribution must not contain importable module mlflow",
            "",
        ),
    )
    with pytest.raises(RuntimeError, match="current physical-isolation probe.*mlflow"):
        build_windows_installer.active_runtime_venv()


def test_windows_installer_collects_only_exact_active_manifest_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools import build_windows_installer

    monkeypatch.setattr(build_windows_installer, "REPO", tmp_path)
    manifest_path = tmp_path / "fx-quant-stack" / "artifacts" / "active_models.json"
    artifact = tmp_path / "fx-quant-stack" / "artifacts_shadow" / "run" / "eurusd" / "meta"
    registry = tmp_path / "fx-quant-stack" / "artifacts_shadow" / "registry" / "eurusd.json"
    unrelated = tmp_path / "fx-quant-stack" / "artifacts_shadow" / "run" / "reports"
    manifest_path.parent.mkdir(parents=True)
    artifact.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    registry.write_text("{}", encoding="utf-8")
    unrelated.mkdir(parents=True)
    (unrelated / "training_report.json").write_text("{}", encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "active_model_sets": {
                    "EURUSD": {
                        "artifacts": {
                            "meta": {
                                "path": "fx-quant-stack/artifacts_shadow/run/eurusd/meta",
                                "model_uri": "models:/fx.meta_filter.EURUSD.M5/7",
                            }
                        },
                        "registry_path": (
                            "fx-quant-stack/artifacts_shadow/registry/eurusd.json"
                        ),
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    _, pairs, artifact_paths, registry_paths = (
        build_windows_installer.read_active_manifest()
    )

    assert pairs == ["EURUSD"]
    assert artifact_paths == [
        Path("fx-quant-stack/artifacts_shadow/run/eurusd/meta")
    ]
    assert registry_paths == [
        Path("fx-quant-stack/artifacts_shadow/registry/eurusd.json")
    ]
    assert Path("fx-quant-stack/artifacts_shadow/run/reports") not in artifact_paths


def test_windows_installer_payload_excludes_raw_repository_source_trees() -> None:
    from tools import build_windows_installer

    source = (ROOT / "tools" / "build_windows_installer.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    build_payload = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_payload"
    )
    string_literals = {
        node.value for node in ast.walk(build_payload) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    call_names = {
        node.func.id
        for node in ast.walk(build_payload)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert {
        "src",
        "tools",
        "fx-quant-stack/src",
        "fx-quant-stack/scripts",
        "ops/windows",
    }.isdisjoint(string_literals)
    assert "installer/windows" in string_literals
    assert "active_runtime_venv" in call_names
    assert {
        "13_train_all.bat",
        "14_activate_models.bat",
        "15_backtest_smoke.bat",
        "24_start_candidate_stack.bat",
        "26_weekly_full_retrain_and_activate.bat",
        "40_full_scale_e2e_validation.bat",
    }.isdisjoint(build_windows_installer.RUNTIME_OPS_FILES)
    assert {
        "25_monitor_everything.bat",
        "25_monitor_everything.ps1",
    }.isdisjoint(build_windows_installer.RUNTIME_OPS_FILES)
    assert {
        "_env.bat",
        "00_preflight.bat",
        "20_start_bridge.bat",
        "21_start_runtime.bat",
        "24_start_feature_push_worker.bat",
        "90_stop_all.bat",
    } <= set(build_windows_installer.RUNTIME_OPS_FILES)
    assert '"mlflow"' in source
    assert "runtime_physical_isolation_errors as check" in source
    assert "active_artifact_paths" in source


def test_external_training_selector_does_not_narrow_belief_context_universe() -> None:
    source = (ROOT / "ops" / "windows" / "13_train_all.bat").read_text(encoding="utf-8")

    assert 'if not defined FXSTACK_TRAIN_PAIRS set "FXSTACK_TRAIN_PAIRS=%FXSTACK_PAIRS%"' in source
    assert 'set "FXSTACK_TRAIN_PAIRS_SP=%FXSTACK_TRAIN_PAIRS:,= %"' in source
    assert "for %%P in (%FXSTACK_TRAIN_PAIRS_SP%) do (" in source
    assert "for %%P in (%FXSTACK_PAIRS_SP%) do (" not in source


def test_packaged_launcher_rejects_training_and_backtest_full_mode() -> None:
    source = (ROOT / "launch_all.bat").read_text(encoding="utf-8")
    full_block = source.split(":full", 1)[1].split(":stop", 1)[0]

    assert 'if /I "%FXSTACK_PACKAGE_MODE%"=="1"' in full_block
    assert "full training/backtest validation is not present" in full_block
    assert full_block.index("FXSTACK_PACKAGE_MODE") < full_block.index(
        "40_full_scale_e2e_validation.bat"
    )


def test_runtime_posture_validation_precedes_every_launch_mutation() -> None:
    runtime = (WINDOWS / "21_start_runtime.bat").read_text(encoding="utf-8")
    launch = (ROOT / "launch_all.bat").read_text(encoding="utf-8")

    assert runtime.index("call :resolve_launch_posture") < runtime.index("call :reset_runtime_processes")
    assert runtime.count("call :validate_runtime_risk_limits") == 1
    assert "validate_runtime_risk_limits.ps1" in runtime
    assert runtime.index('if /I "%MODE%"=="--validate"') < runtime.index(":bg")
    live_block = launch.split(":live", 1)[1].split(":full", 1)[0]
    validate_index = live_block.index("21_start_runtime.bat\" --validate")
    assert validate_index < live_block.index(":auto_db_fallback")
    assert validate_index < live_block.index("90_stop_all.bat")
    assert validate_index < live_block.index("20_start_bridge.bat")


@pytest.mark.skipif(os.name != "nt", reason="Windows batch isolated-audit contract")
def test_skip_installed_env_preserves_process_supplied_safe_audit_settings() -> None:
    secret_markers = ("CREDENTIAL", "KEY", "PASSWORD", "SECRET", "TOKEN")
    process_env = {
        name: value
        for name, value in os.environ.items()
        if not any(marker in name.upper() for marker in secret_markers)
    }
    process_env.pop("FXSTACK_PROJECT_ROOT", None)
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
        "!FXSTACK_FEAST_ENABLED!;!FXSTACK_FEATURE_PUSH_ENABLED!;!FXSTACK_PROJECT_ROOT!"
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
    assert completed.stdout.strip() == f"shadow;sqlite+pysqlite:///isolated_audit.db;1;0;0;{ROOT}"


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime launch-posture contract")
@pytest.mark.parametrize(
    ("overrides", "expected_returncode", "expected_output"),
    [
        ({}, 0, "profile=staged_safe mode=shadow"),
        (
            {"FXSTACK_START_PROFILE": "paper"},
            2,
            "paper is unavailable in the production runtime distribution",
        ),
        (
            {"FXSTACK_START_PROFILE": "paper", "FXSTACK_AGENT_MODE": "shadow"},
            2,
            "paper is unavailable in the production runtime distribution",
        ),
        (
            {
                "FXSTACK_START_PROFILE": "live",
                "FXSTACK_LIVE_ARMED": "1",
                "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": "EURUSD",
                "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
                "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
            },
            2,
            "requires explicit FXSTACK_AGENT_MODE=live",
        ),
        (
            {
                "FXSTACK_START_PROFILE": "live",
                "FXSTACK_AGENT_MODE": "live",
                "FXSTACK_LIVE_ARMED": "0",
                "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": "EURUSD",
                "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
                "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
            },
            2,
            "requires explicit FXSTACK_LIVE_ARMED=1",
        ),
        (
            {
                "FXSTACK_START_PROFILE": "live",
                "FXSTACK_AGENT_MODE": "live",
                "FXSTACK_LIVE_ARMED": "1",
                "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": "EURUSD",
                "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
                "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
            },
            0,
            "profile=live mode=live",
        ),
        (
            {
                "FXSTACK_START_PROFILE": "staged_safe",
                "FXSTACK_AGENT_MODE": "live",
                "FXSTACK_LIVE_ARMED": "1",
                "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": "EURUSD",
                "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
                "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
            },
            2,
            "requires FXSTACK_AGENT_MODE=shadow",
        ),
        (
            {
                "FXSTACK_START_PROFILE": "live",
                "FXSTACK_AGENT_MODE": "live",
                "FXSTACK_LIVE_ARMED": "1",
                "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": "   ",
                "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
                "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
            },
            2,
            "explicit non-empty FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST",
        ),
    ],
)
def test_runtime_launch_posture_resolver(
    overrides: dict[str, str],
    expected_returncode: int,
    expected_output: str,
) -> None:
    completed = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "call ops\\windows\\21_start_runtime.bat --validate",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_isolated_launch_env(overrides),
        cwd=ROOT,
        timeout=20,
    )

    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == expected_returncode, output
    assert expected_output in output


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime hard-risk contract")
@pytest.mark.parametrize(
    "invalid_key",
    [
        "FXSTACK_EQUITY_LOTS_PER_USD",
        "FXSTACK_MAX_ORDER_LOTS",
        "FXSTACK_RISK_MAX_DRAWDOWN_PCT",
        "FXSTACK_RISK_MAX_GROSS_EXPOSURE",
        "FXSTACK_RISK_MAX_NET_EXPOSURE",
    ],
)
def test_live_posture_rejects_disabled_hard_risk_limits(invalid_key: str) -> None:
    overrides = {
        "FXSTACK_START_PROFILE": "live",
        "FXSTACK_AGENT_MODE": "live",
        "FXSTACK_LIVE_ARMED": "1",
        "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": "EURUSD",
        "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
        "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
        invalid_key: "0",
    }
    completed = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "call ops\\windows\\21_start_runtime.bat --validate",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_isolated_launch_env(overrides),
        cwd=ROOT,
        timeout=20,
    )

    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 2, output
    assert invalid_key in output


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime launch-posture contract")
def test_paper_posture_is_rejected_before_risk_validation() -> None:
    completed = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "call ops\\windows\\21_start_runtime.bat --validate",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_isolated_launch_env(
            {
                "FXSTACK_START_PROFILE": "paper",
                "FXSTACK_RISK_MAX_GROSS_EXPOSURE": "NaN",
            }
        ),
        cwd=ROOT,
        timeout=20,
    )

    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 2, output
    assert "paper is unavailable in the production runtime distribution" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime live-scope contract")
@pytest.mark.parametrize(
    ("missing_key", "expected_output"),
    [
        ("FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST", "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST"),
        ("FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST", "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST"),
        ("FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST", "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST"),
    ],
)
def test_live_posture_requires_each_explicit_scope(missing_key: str, expected_output: str) -> None:
    overrides = {
        "FXSTACK_START_PROFILE": "live",
        "FXSTACK_AGENT_MODE": "live",
        "FXSTACK_LIVE_ARMED": "1",
        "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": "EURUSD",
        "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
        "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
    }
    overrides.pop(missing_key)
    completed = subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "call ops\\windows\\21_start_runtime.bat --validate",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_isolated_launch_env(overrides),
        cwd=ROOT,
        timeout=20,
    )

    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 2, output
    assert expected_output in output


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


def test_bridge_ea_blocks_non_tightening_stop_modifications_at_last_mile() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    helper = source.split("bool IsStrictlyTighterStop", 1)[1].split("bool ModifySymbolStop", 1)[0]
    modify = source.split("bool ModifySymbolStop", 1)[1].split("string ToUpperSafe", 1)[0]

    assert "if(current_sl <= 0.0) return true;" in helper
    assert "if(order_type == OP_BUY) return proposed_sl > current_sl;" in helper
    assert "if(order_type == OP_SELL) return proposed_sl < current_sl;" in helper
    assert modify.index("IsStrictlyTighterStop(ty, currentSl, slNorm)") < modify.index("OrderModify(")
    assert "lastErr = 409" in modify
    assert "ERR modify_sl non_tightening" in modify
    assert source.count('"sl\\\":" + DoubleToString(OrderStopLoss(), odg)') == 1
    assert source.count('",sl=" + DoubleToString(OrderStopLoss(), odg)') == 1


def test_bridge_ea_never_rounds_approved_entry_or_partial_close_lots() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    utils = (ROOT / "MQL4" / "Include" / "BridgeUtils.mqh").read_text(encoding="utf-8")

    exact_validator = utils.split("bool ValidateExactBrokerLots", 1)[1].split(
        "// For IG mini contracts", 1
    )[0]
    compact_validator = re.sub(r"\s+", "", exact_validator)
    assert "!MathIsValidNumber(requestedLots)||requestedLots<=0.0" in compact_validator
    assert "requestedLots<minlot-tolerance" in compact_validator
    assert "requestedLots>maxlot+tolerance" in compact_validator
    assert "MathAbs(units-nearestUnits)>1e-7" in compact_validator
    assert "normalized>requestedLots" in compact_validator
    assert "MathAbs(normalized-requestedLots)>tolerance" in compact_validator

    legacy_rounder = utils.split("double RoundLot", 1)[1].split("int LotDigitsForStep", 1)[0]
    assert "ValidateExactBrokerLots(sym,lots,exactLots,reason)" in legacy_rounder
    assert "MathFloor" not in legacy_rounder

    execute = source.split("void Execute", 1)[1].split("void manageCycle", 1)[0]
    assert execute.index("ValidateExactBrokerLots(brokerSym,lots,lots2,lotReason)") < execute.index(
        "OrderSend("
    )
    assert "RoundLot(" not in execute
    assert "MinLot(" not in execute
    assert "IGMiniLot(" not in execute

    preflight = source.split("bool ValidatePartialClosePlan", 1)[1].split(
        "bool CloseSymbolPartial", 1
    )[0]
    compact_preflight = re.sub(r"\s+", "", preflight)
    assert "ValidateExactBrokerLots(osym,requestedChunk,exactChunk,lotReason)" in preflight
    assert "exactChunk>requestedChunk" in compact_preflight
    assert "exactChunk>remaining" in compact_preflight
    assert "ticket_remainder_not_executable" in preflight

    partial = source.split("bool CloseSymbolPartial", 1)[1].split(
        "bool IsStrictlyTighterStop", 1
    )[0]
    compact_partial = re.sub(r"\s+", "", partial)
    assert partial.index("ValidatePartialClosePlan(") < partial.index("OrderClose(")
    assert "RoundLot(" not in partial
    assert "exactChunk>remaining" in compact_partial
    assert "closedTotal+exactChunk>closeLots" in compact_partial
    assert partial.index("closedTotal+exactChunk>closeLots") < partial.index("OrderClose(")
    assert "closedTotal>closeLots" in compact_partial
    assert "OrderClose(OrderTicket(), exactChunk" in partial
    assert "!MathIsValidNumber(close_lots) || close_lots <= 0.0" in source


def test_bridge_ea_requires_directional_sl_and_tp_before_every_entry_send() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    helper = source.split("bool ValidateDirectionalEntryProtection", 1)[1].split(
        "void Execute", 1
    )[0]
    compact_helper = re.sub(r"\s+", "", helper)

    assert "!MathIsValidNumber(slPrice)||slPrice<=0.0" in compact_helper
    assert "!MathIsValidNumber(tpPrice)||tpPrice<=0.0" in compact_helper
    assert "slExact>=bid" in compact_helper
    assert "tpExact<=ask" in compact_helper
    assert "slExact<=ask" in compact_helper
    assert "tpExact>=bid" in compact_helper
    assert "MODE_STOPLEVEL" in helper

    execute = source.split("void Execute", 1)[1].split("void manageCycle", 1)[0]
    retry_loop = execute.split("for(int attempt=0; attempt<3; attempt++)", 1)[1].split(
        "if(ticket<0)", 1
    )[0]
    assert retry_loop.index("ValidateDirectionalEntryProtection(") < retry_loop.index("OrderSend(")
    assert "brokerSym,type,bid,ask,sl,tp_price_in" in re.sub(r"\s+", "", retry_loop)
    assert "TpFromCash(" not in execute
    assert "entry_protection_invalid:" in execute
    assert "!MathIsValidNumber(lots) || lots <= 0.0" in source


def test_bridge_ea_binds_every_entry_to_the_attested_account_at_last_mile() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    heartbeat = source.split("void heartbeat", 1)[1].split("void reportBridgeStatus", 1)[0]
    handler = source.split("void HandleCmd", 1)[1]
    entry_gate = handler.split('if(cmd=="BUY" || cmd=="SELL")', 1)[1].split(
        'if(StringLen(sym) <= 0)', 1
    )[0]

    assert "AccountInfoInteger(ACCOUNT_TRADE_MODE)" in source
    assert "AccountNumber()" in source
    assert "AccountServer()" in source
    assert '" account_mode=" + accountMode' in heartbeat
    assert '" account_scope=" + accountScope' in heartbeat
    assert '" account_magic=" + IntegerToString(Magic)' in heartbeat
    assert 'if(k=="expected_account_mode")' in handler
    assert 'if(k=="expected_account_scope")' in handler
    assert "expected_account_mode != current_account_mode" in entry_gate
    assert "expected_account_scope != current_account_scope" in entry_gate
    assert 'post_ack(' in entry_gate
    assert '403, "broker_account_mismatch"' in entry_gate
    assert entry_gate.index("expected_account_mode != current_account_mode") < entry_gate.index("post_ack(")


def test_bridge_ea_empty_info_dashboard_payload_cannot_stop_ack_timer() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    dashboard = source.split("void UpdateDashboard", 1)[1].split(
        "bool ValidateDirectionalEntryProtection", 1
    )[0]
    info_handler = source.split('if(cmd=="INFO")', 1)[1].split(
        'if(cmd=="BUY" || cmd=="SELL")', 1
    )[0]

    assert 'UpdateDashboard(thought);' in info_handler
    assert 'if(n < 1)' in dashboard
    assert 'ArrayResize(lines, 1);' in dashboard
    assert 'lines[0] = "";' in dashboard
    assert dashboard.index('if(n < 1)') < dashboard.index('if(shown > 0)')


def test_bridge_ea_ack_outbox_persists_before_post_and_dequeues_only_on_2xx() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    persist = source.split("bool PersistAckPayloadBeforePost", 1)[1].split(
        "bool RetainUnpersistedAckPayload", 1
    )[0]
    post_ack = source.split("void post_ack", 1)[1].split("void CleanupSeenSignals", 1)[0]
    replay_file = source.split("bool ReplayAckOutboxFile", 1)[1].split(
        "int ReplayAckOutbox", 1
    )[0]

    assert "FILE_WRITE|FILE_TXT|FILE_ANSI|FILE_COMMON" in persist
    assert persist.index("FileWriteString(handle,payload)") < persist.index("FileFlush(handle)")
    assert persist.index("FileFlush(handle)") < persist.index("FileClose(handle)")
    assert persist.index("FileClose(handle)") < persist.index("FileMove(")
    assert post_ack.index("QueueAckPayloadBeforePost(payload,queuedPath)") < post_ack.index(
        "ReplayAckOutboxFile(queuedPath)"
    )
    assert "HttpPOST(" not in post_ack
    assert source.count("HttpPOST(gAckScopeApiBase+AckPath(),payload,gBridgeApiKey)") == 1

    compact_replay = re.sub(r"\s+", "", replay_file)
    assert replay_file.index("HttpPOST(gAckScopeApiBase+AckPath(),payload,gBridgeApiKey)") < replay_file.index(
        "AckHttpStatusIsSuccess(statusCode)"
    )
    assert "if(!AckHttpStatusIsSuccess(statusCode)){" in compact_replay
    assert replay_file.index("if(!AckHttpStatusIsSuccess(statusCode))") < replay_file.index(
        "FileDelete(path,FILE_COMMON)"
    )
    assert source.count("FileDelete(path,FILE_COMMON)") == 1
    assert 'WarnAuthFailure("ack_replay",statusCode)' in replay_file


def test_bridge_ea_ack_outbox_replays_on_startup_and_timer_without_reexecution() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    pin_scope = source.split("bool TryPinAckOutboxScopeIdentity", 1)[1].split(
        "bool AckOutboxScopeIdentityMatches", 1
    )[0]
    validate_scope = source.split("bool AckOutboxScopeIdentityMatches", 1)[1].split(
        "string AckOutboxScopeDirectory", 1
    )[0]
    scope = source.split("string AckOutboxScopeDirectory", 1)[1].split(
        "int AckOutboxCountPattern", 1
    )[0]
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]
    timer = source.split("void OnTimer", 1)[1].split("void HandleCmd", 1)[0]
    deinit = source.split("void OnDeinit", 1)[1].split("void post_report", 1)[0]
    allocate = source.split("bool AllocateAckOutboxPaths", 1)[1].split(
        "bool ReadAckOutboxPayload", 1
    )[0]
    replay = source.split("int ReplayAckOutbox", 1)[1].split("void ServiceAckOutbox", 1)[0]
    service_outbox = source.split("void ServiceAckOutbox", 1)[1].split(
        "bool AckOutboxAllowsCommandPolling", 1
    )[0]
    polling_gate = source.split("bool AckOutboxAllowsCommandPolling", 1)[1].split(
        "void FlushAckOutboxOnDeinit", 1
    )[0]

    assert "FILE_COMMON" in source
    assert "accountNumber=AccountNumber()" in pin_scope
    assert "accountServer=StringTrim(AccountServer())" in pin_scope
    assert "endpoint=StringTrim(ApiBase)" in pin_scope
    assert "TerminalInfoString(TERMINAL_DATA_PATH)" in pin_scope
    assert "accountNumber<=0" in pin_scope
    assert "gAckScopePinned=true" in pin_scope
    assert "gAckScopeDirectory=" in pin_scope
    assert "ApiKey" not in pin_scope
    assert "AccountNumber()!=gAckScopeAccountNumber" in validate_scope
    assert "StringTrim(AccountServer())!=gAckScopeAccountServer" in validate_scope
    assert "StringTrim(ApiBase)!=gAckScopeApiBase" in validate_scope
    assert "scope_terminal_instance_changed" in validate_scope
    assert "AccountNumber()" not in scope
    assert "AccountServer()" not in scope
    assert "ApiBase" not in scope
    assert "return(gAckScopePinned ? gAckScopeDirectory" in scope
    assert "ACK_OUTBOX_MAX_PENDING 512" in source
    assert "ACK_OUTBOX_REPLAY_PER_TIMER 4" in source
    assert "gAckOutboxSequence++" in allocate
    assert 'candidateFinal=candidateBase+".ack"' in allocate
    assert "gAckScopeTerminalToken" in allocate
    assert "FileIsExist(candidateFinal,FILE_COMMON)" in allocate
    assert "FILE_REWRITE" not in allocate
    assert 'AckOutboxListPattern("*.ack",paths)' in replay
    assert "ArrayResize(paths,count+1)" in source

    assert "ServiceAckOutbox(ACK_OUTBOX_REPLAY_ON_STARTUP)" in init
    assert "AckOutboxScopeIdentityMatches(identityReason)" in service_outbox
    assert "TryPinAckOutboxScopeIdentity()" in polling_gate
    assert "AckOutboxScopeIdentityMatches(identityReason)" in polling_gate
    identity_mismatch_block = polling_gate.split(
        "if(!AckOutboxScopeIdentityMatches(identityReason))", 1
    )[1].split("int pending", 1)[0]
    assert "BlockAckOutbox(identityReason)" in identity_mismatch_block
    assert "return(false)" in identity_mismatch_block
    assert timer.index("ServiceAckOutbox(ACK_OUTBOX_REPLAY_PER_TIMER)") < timer.index(
        "AckOutboxAllowsCommandPolling()"
    )
    assert timer.index("AckOutboxAllowsCommandPolling()") < timer.index("HttpGET(pollUrl, gBridgeApiKey)")
    assert timer.index("AckOutboxAllowsCommandPolling()") < timer.index("HandleCmd(resp)")
    assert "HandleCmd(" not in replay
    assert deinit.index("FlushAckOutboxOnDeinit()") < deinit.index("DeinitBridgeHttp()")


def test_bridge_ea_auth_uses_terminal_file_without_journal_secret() -> None:
    source = (ROOT / "MQL4" / "Experts" / "BridgeEA.mq4").read_text(encoding="utf-8")
    deploy = (ROOT / "ops" / "windows" / "24_deploy_bridge_ea.ps1").read_text(encoding="utf-8")
    loader = source.split("string LoadBridgeApiKey", 1)[1].split("void WarnAuthFailure", 1)[0]
    init = source.split("int OnInit", 1)[1].split("void RemoveDashboard", 1)[0]

    assert 'input string ApiKey = "";' in source
    assert 'FileOpen("bridge_api_key.txt", FILE_READ|FILE_TXT|FILE_ANSI)' in loader
    assert "gBridgeApiKey = LoadBridgeApiKey();" in init
    assert "HttpGET(pollUrl, gBridgeApiKey)" in source
    assert "HttpPOST(ApiBase + TickPath(), tick, gBridgeApiKey)" in source
    assert '${env:ProgramFiles(x86)}' in deploy
    assert 'MQL4\\\\Files' in deploy
    assert 'bridge_api_key.txt' in deploy
    assert "MT4 automatically records every EA input in its journal" in deploy
    assert "$replacement = '${1}'" in deploy
    assert "$replacement = '${1}' + $apiKey" not in deploy
