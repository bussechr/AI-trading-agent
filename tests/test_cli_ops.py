"""Tests for the ``trader ops`` operator CLI commands.

Pins the three things operators rely on this CLI to do:

* ``trader ops status`` — probe a running bridge; exit code reflects state.
* ``trader ops validate-config`` — non-zero exit on any Settings misconfig.
* ``trader ops tail-logs`` — convert JSON log lines into human-readable
  output without losing fields.

The status command is tested by monkey-patching the urllib probe so the
test doesn't depend on a running bridge.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trader import cli as trader_cli  # noqa: E402


# ---------------------------------------------------------------------------
# validate-config
# ---------------------------------------------------------------------------


def test_validate_config_returns_zero_on_clean_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Clean defaults must produce exit code 0 + "config OK"."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./ops_test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")

    from fxstack.settings import get_settings
    get_settings.cache_clear()

    args = argparse.Namespace(json=False)
    rc = trader_cli._ops_validate_config(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "config OK" in out


def test_validate_config_returns_one_on_misconfig(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The classic auth-on-empty-key misconfig must produce exit 1 + named error."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./ops_test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_API_KEY", "")

    from fxstack.settings import get_settings
    get_settings.cache_clear()

    args = argparse.Namespace(json=False)
    rc = trader_cli._ops_validate_config(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "bridge_auth_required" in out
    assert "bridge_api_key" in out


def test_validate_config_json_mode_emits_structured_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--json` must emit machine-parseable output for ops automation."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "")  # force an error
    from fxstack.settings import get_settings
    get_settings.cache_clear()

    args = argparse.Namespace(json=True)
    trader_cli._ops_validate_config(args)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert isinstance(payload["errors"], list)
    assert len(payload["errors"]) >= 1


# ---------------------------------------------------------------------------
# status (with mocked HTTP probe)
# ---------------------------------------------------------------------------


def test_status_exit_zero_when_livez_and_readyz_ok(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./ops_test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
    from fxstack.settings import get_settings
    get_settings.cache_clear()

    def fake_probe(url: str, *, timeout: float, api_key: str) -> dict[str, object]:
        if url.endswith("/v2/livez"):
            return {"ok": True, "status_code": 200, "body": {"status": "ok"}, "error": None}
        if url.endswith("/v2/readyz"):
            return {
                "ok": True,
                "status_code": 200,
                "body": {
                    "ready": True,
                    "checks": {"database_ok": True, "runtime_running": True, "mt4_fresh": True, "not_draining": True},
                },
                "error": None,
            }
        return {"ok": False, "status_code": None, "body": None, "error": "unknown"}

    monkeypatch.setattr(trader_cli, "_ops_probe_endpoint", fake_probe)

    args = argparse.Namespace(url="", api_key="", timeout=1.0, json=False)
    rc = trader_cli._ops_status(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "livez:    OK" in out
    assert "READY (200)" in out


def test_status_exit_one_when_livez_ok_but_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A booted-but-not-ready bridge is the staging-deploy case — exit 1."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./ops_test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
    from fxstack.settings import get_settings
    get_settings.cache_clear()

    def fake_probe(url: str, *, timeout: float, api_key: str) -> dict[str, object]:
        if url.endswith("/v2/livez"):
            return {"ok": True, "status_code": 200, "body": {"status": "ok"}, "error": None}
        return {
            "ok": False,
            "status_code": 503,
            "body": {
                "ready": False,
                "checks": {"database_ok": True, "runtime_running": False, "mt4_fresh": False, "not_draining": True},
            },
            "error": None,
        }

    monkeypatch.setattr(trader_cli, "_ops_probe_endpoint", fake_probe)
    args = argparse.Namespace(url="", api_key="", timeout=1.0, json=False)
    rc = trader_cli._ops_status(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT READY (503)" in out
    assert "runtime_running: FAIL" in out


def test_status_exit_two_when_livez_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead process must report exit code 2, distinct from "not ready"."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./ops_test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
    from fxstack.settings import get_settings
    get_settings.cache_clear()

    def fake_probe(url: str, *, timeout: float, api_key: str) -> dict[str, object]:
        return {"ok": False, "status_code": None, "body": None, "error": "unreachable: refused"}

    monkeypatch.setattr(trader_cli, "_ops_probe_endpoint", fake_probe)
    args = argparse.Namespace(url="http://localhost:1", api_key="", timeout=1.0, json=False)
    rc = trader_cli._ops_status(args)
    out = capsys.readouterr().out
    assert rc == 2
    assert "FAIL" in out


def test_status_json_mode_dumps_full_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./ops_test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
    from fxstack.settings import get_settings
    get_settings.cache_clear()

    monkeypatch.setattr(
        trader_cli,
        "_ops_probe_endpoint",
        lambda url, *, timeout, api_key: {
            "ok": True,
            "status_code": 200,
            "body": {"ready": True, "checks": {}},
            "error": None,
        },
    )
    args = argparse.Namespace(url="", api_key="", timeout=1.0, json=True)
    trader_cli._ops_status(args)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert "livez" in payload
    assert "readyz" in payload
    assert payload["livez"]["ok"] is True


# ---------------------------------------------------------------------------
# tail-logs
# ---------------------------------------------------------------------------


def test_tail_logs_pretty_prints_json_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each JSON line collapses into one human-readable line."""
    log_path = tmp_path / "bridge.log"
    log_path.write_text(
        json.dumps({"ts": "2026-05-21T12:00:00Z", "level": "INFO", "logger": "fxstack.api.app", "rid": "abc", "msg": "ready"})
        + "\n"
        + json.dumps({"ts": "2026-05-21T12:00:01Z", "level": "WARNING", "logger": "fxstack.api.auth", "rid": "abc", "msg": "auth disabled"})
        + "\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(file=str(log_path))
    rc = trader_cli._ops_tail_logs(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "2026-05-21T12:00:00Z" in out
    assert "[fxstack.api.app]" in out
    assert "[rid=abc]" in out
    assert "ready" in out
    assert "WARNING" in out


def test_tail_logs_preserves_non_json_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-JSON lines (e.g. Python traceback frames) pass through unchanged."""
    log_path = tmp_path / "mixed.log"
    log_path.write_text(
        json.dumps({"ts": "2026-05-21T12:00:00Z", "level": "ERROR", "logger": "test", "rid": "-", "msg": "boom"})
        + "\n"
        + "Traceback (most recent call last):\n"
        + "  File \"foo.py\", line 1, in <module>\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(file=str(log_path))
    trader_cli._ops_tail_logs(args)
    out = capsys.readouterr().out
    assert "Traceback" in out
    assert "File \"foo.py\"" in out


def test_tail_logs_appends_extra_fields_as_json_suffix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Extra fields beyond the standard set must survive as a suffix."""
    log_path = tmp_path / "extras.log"
    log_path.write_text(
        json.dumps({
            "ts": "2026-05-21T12:00:00Z",
            "level": "INFO",
            "logger": "fxstack.runtime",
            "rid": "xyz",
            "msg": "signal",
            "pair": "EURUSD",
            "score": 0.62,
        })
        + "\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(file=str(log_path))
    trader_cli._ops_tail_logs(args)
    out = capsys.readouterr().out
    assert "EURUSD" in out
    assert "0.62" in out


def test_tail_logs_reads_stdin_when_dash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Passing ``-`` reads from stdin — the common pipe-from-kubectl-logs use."""
    payload = json.dumps({"ts": "X", "level": "INFO", "logger": "L", "rid": "R", "msg": "M"}) + "\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    args = argparse.Namespace(file="-")
    rc = trader_cli._ops_tail_logs(args)
    out = capsys.readouterr().out
    assert rc == 0
    assert "[L]" in out
    assert "[rid=R]" in out
