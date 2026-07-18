"""Tests for ``trader stack deploy`` and its supporting helpers.

The deploy command composes four steps:
1. ``ops validate-config`` — pure, no I/O
2. ``db migrate`` — subprocess to alembic
3. Spawn the bridge process
4. Poll bridge liveness (or full readiness when explicitly requested)

Spawning a real bridge in a unit test is fragile (it would touch sqlite,
spawn a uvicorn worker, and depend on free ports). Instead these tests:
* Cover ``_port_from_url`` and ``_port_in_use`` as pure helpers.
* Drive ``_stack_deploy`` with monkey-patched validate/migrate/Popen so the
  composition wiring is exercised end-to-end without real side effects.
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trader import cli as trader_cli  # noqa: E402


# ---------------------------------------------------------------------------
# _port_from_url
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:58710", 58710),
        ("https://example.com:443", 443),
        ("http://localhost:8000/v2/health", 8000),
        ("", 0),
        ("not a url", 0),
        ("http://nohost", 0),
    ],
)
def test_port_from_url(url: str, expected: int) -> None:
    assert trader_cli._port_from_url(url) == expected


# ---------------------------------------------------------------------------
# _port_in_use
# ---------------------------------------------------------------------------


def test_port_in_use_returns_false_when_nothing_listening() -> None:
    """An ephemeral port that we know is unbound must return False."""
    # Bind a socket to get an unused port, then close it before probing.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert trader_cli._port_in_use("127.0.0.1", port) is False


def test_port_in_use_returns_true_when_listening() -> None:
    """A socket actively listening must produce a True result."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert trader_cli._port_in_use("127.0.0.1", port) is True


# ---------------------------------------------------------------------------
# _stack_deploy — composition wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def deploy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the minimum env so validate-config passes.

    Also stubs `_fxstack_guard` — its real impl re-execs into an fxstack-ready
    interpreter, which is irrelevant to the deploy-flow logic these tests
    exercise and breaks under the test runner.
    """
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./deploy_test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
    monkeypatch.setattr(trader_cli, "_fxstack_guard", lambda: None)

    from fxstack.settings import get_settings
    get_settings.cache_clear()


def _make_deploy_args(**overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 0,
        "timeout": 1.0,
        "log_dir": "",
        "database_url": "",
        "allow_reuse_port": False,
        "require_full_ready": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf"), "invalid"])
def test_deploy_rejects_invalid_timeout_before_preflight(
    deploy_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    timeout: object,
) -> None:
    def _unexpected_preflight(args: argparse.Namespace) -> int:
        raise AssertionError("preflight must not run")

    monkeypatch.setattr(trader_cli, "_ops_validate_config", _unexpected_preflight)

    rc = trader_cli._stack_deploy(_make_deploy_args(timeout=timeout))

    assert rc == 2
    assert "finite positive number" in capsys.readouterr().out


@pytest.mark.parametrize("port", [-1, 65536, "invalid"])
def test_deploy_rejects_invalid_port_before_socket_or_process_work(
    deploy_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    port: object,
) -> None:
    def _unexpected_port_probe(host: str, checked_port: int) -> bool:
        raise AssertionError("socket probe must not run")

    monkeypatch.setattr(trader_cli, "_ops_validate_config", lambda args: 0)
    monkeypatch.setattr(trader_cli, "_db_migrate", lambda args: 0)
    monkeypatch.setattr(trader_cli, "_port_in_use", _unexpected_port_probe)

    rc = trader_cli._stack_deploy(_make_deploy_args(port=port))

    assert rc == 2
    assert "between 1 and 65535" in capsys.readouterr().out


def test_deploy_returns_two_when_config_invalid(
    deploy_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A misconfig must abort before any subprocess work happens."""
    # Force a config error
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_API_KEY", "")
    from fxstack.settings import get_settings
    get_settings.cache_clear()

    # Sentinel: migrate must not be called if validate fails
    migrate_called: list[bool] = []

    def _spy_migrate(args: argparse.Namespace) -> int:
        migrate_called.append(True)
        return 0

    monkeypatch.setattr(trader_cli, "_db_migrate", _spy_migrate)

    rc = trader_cli._stack_deploy(_make_deploy_args())
    out = capsys.readouterr().out
    assert rc == 2
    assert "config validation FAILED" in out
    assert migrate_called == []


def test_deploy_returns_two_when_migration_fails(
    deploy_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A migration failure must abort before the bridge is spawned."""
    monkeypatch.setattr(trader_cli, "_db_migrate", lambda args: 1)

    popen_called: list[bool] = []

    class _FakePopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            popen_called.append(True)

    monkeypatch.setattr(trader_cli.subprocess, "Popen", _FakePopen)

    rc = trader_cli._stack_deploy(_make_deploy_args())
    out = capsys.readouterr().out
    assert rc == 2
    assert "db migration FAILED" in out
    assert popen_called == []


def test_deploy_succeeds_when_bridge_reports_ready(
    deploy_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Full-ready mode: validate OK → migrate OK → bridge spawn → readyz 200."""
    monkeypatch.setattr(trader_cli, "_db_migrate", lambda args: 0)
    # Pick an ephemeral free port so the "port in use" preflight passes.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    fake_proc = SimpleNamespace(pid=99999, returncode=None, poll=lambda: None)

    class _FakePopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.pid = fake_proc.pid

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr(trader_cli.subprocess, "Popen", _FakePopen)

    probe_count = {"livez": 0, "readyz": 0}

    def fake_probe(url: str, *, timeout: float, api_key: str) -> dict[str, object]:
        if url.endswith("/v2/livez"):
            probe_count["livez"] += 1
            return {"ok": True, "status_code": 200, "body": {"status": "ok"}, "error": None}
        if url.endswith("/v2/readyz"):
            probe_count["readyz"] += 1
            return {
                "ok": True,
                "status_code": 200,
                "body": {"ready": True, "checks": {"database_ok": True}},
                "error": None,
            }
        return {"ok": False, "status_code": None, "body": None, "error": "unknown"}

    monkeypatch.setattr(trader_cli, "_ops_probe_endpoint", fake_probe)

    rc = trader_cli._stack_deploy(
        _make_deploy_args(
            port=free_port,
            log_dir=str(tmp_path),
            timeout=5.0,
            require_full_ready=True,
        )
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "[1/4] validating config" in out
    assert "[2/4] running db migrations" in out
    assert "[3/4] starting bridge" in out
    assert "[4/4] polling /v2/readyz" in out
    assert "bridge READY" in out
    # PID file written
    pid_file = tmp_path / f"bridge_{free_port}.pid"
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8").strip() == str(fake_proc.pid)
    assert probe_count == {"livez": 1, "readyz": 1}


def test_deploy_reuses_existing_bridge_and_checks_livez_by_default(
    deploy_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(trader_cli, "_db_migrate", lambda args: 0)
    monkeypatch.setattr(trader_cli, "_port_in_use", lambda host, port: True)

    def _unexpected_popen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("reuse mode must not spawn another bridge")

    monkeypatch.setattr(trader_cli.subprocess, "Popen", _unexpected_popen)
    calls: list[str] = []

    def _probe(url: str, *, timeout: float, api_key: str) -> dict[str, object]:
        calls.append(url)
        return {"ok": True, "status_code": 200, "body": {"status": "ok"}, "error": None}

    monkeypatch.setattr(trader_cli, "_ops_probe_endpoint", _probe)

    rc = trader_cli._stack_deploy(
        _make_deploy_args(
            port=58710,
            log_dir=str(tmp_path),
            timeout=5.0,
            allow_reuse_port=True,
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "attaching to existing bridge" in out
    assert "polling /v2/livez" in out
    assert "bridge LIVE  existing process" in out
    assert calls == ["http://127.0.0.1:58710/v2/livez"]
    assert not (tmp_path / "bridge_58710.pid").exists()


def test_deploy_passes_database_override_to_bridge_process(
    deploy_env: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(trader_cli, "_db_migrate", lambda args: 0)
    monkeypatch.setattr(trader_cli, "_port_in_use", lambda host, port: False)
    spawned: dict[str, Any] = {}

    class _FakePopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            spawned.update(kwargs)
            self.pid = 77777
            self.returncode = None

        def poll(self) -> int | None:
            return None

    monkeypatch.setattr(trader_cli.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        trader_cli,
        "_ops_probe_endpoint",
        lambda url, *, timeout, api_key: {
            "ok": True,
            "status_code": 200,
            "body": {"status": "ok"},
            "error": None,
        },
    )
    override = "sqlite+pysqlite:///./override-runtime.db"

    rc = trader_cli._stack_deploy(
        _make_deploy_args(
            port=58711,
            log_dir=str(tmp_path),
            timeout=5.0,
            database_url=override,
        )
    )

    assert rc == 0
    assert spawned["env"]["FXSTACK_DATABASE_URL"] == override


def test_deploy_returns_two_when_bridge_exits_early(
    deploy_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """If the bridge process dies before readiness, deploy reports 2 with log path."""
    monkeypatch.setattr(trader_cli, "_db_migrate", lambda args: 0)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]

    class _FakePopen:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.pid = 88888
            self.returncode = 42

        def poll(self) -> int | None:
            return 42  # Process already exited

    monkeypatch.setattr(trader_cli.subprocess, "Popen", _FakePopen)
    # Probe should never report ready
    monkeypatch.setattr(
        trader_cli,
        "_ops_probe_endpoint",
        lambda url, *, timeout, api_key: {
            "ok": False,
            "status_code": None,
            "body": None,
            "error": "refused",
        },
    )

    rc = trader_cli._stack_deploy(
        _make_deploy_args(port=free_port, log_dir=str(tmp_path), timeout=5.0)
    )
    out = capsys.readouterr().out
    assert rc == 2
    assert "exited early" in out
    assert "42" in out  # the exit code


def test_deploy_returns_two_when_port_in_use_without_override(
    deploy_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A bound port must abort the deploy unless --allow-reuse-port is set."""
    monkeypatch.setattr(trader_cli, "_db_migrate", lambda args: 0)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        rc = trader_cli._stack_deploy(_make_deploy_args(port=port, timeout=1.0))
    out = capsys.readouterr().out
    assert rc == 2
    assert "already bound" in out
