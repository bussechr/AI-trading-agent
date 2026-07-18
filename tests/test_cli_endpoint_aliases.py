from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from trader import cli as trader_cli  # noqa: E402


def _clear_bridge_urls(monkeypatch) -> None:
    for name in ("MT4_BRIDGE_URL", "TRADER_BRIDGE_URL", "BRIDGE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_monitor_default_follows_trader_bridge_host_port(monkeypatch) -> None:
    _clear_bridge_urls(monkeypatch)
    monkeypatch.setenv("TRADER_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("TRADER_BRIDGE_PORT", "59992")
    args = trader_cli.build_parser().parse_args(["monitor", "confidence"])
    assert args.bridge_url == "http://127.0.0.1:59992"


def test_bridge_parser_help_build_is_safe_with_invalid_env_port(monkeypatch) -> None:
    monkeypatch.setenv("TRADER_BRIDGE_PORT", "not-a-port")
    args = trader_cli.build_parser().parse_args(["bridge", "serve"])
    assert args.port == 58710


def test_instance_root_marker_is_accepted_by_owned_worker_commands() -> None:
    parser = trader_cli.build_parser()
    root = r"D:\Development\Trading Agent"
    assert parser.parse_args(["bridge", "serve", "--instance-root", root]).instance_root == root
    runtime_args = parser.parse_args(
        [
            "runtime",
            "run",
            "--equity",
            "10000",
            "--instance-root",
            root,
            "--instance-id",
            "candidate",
        ]
    )
    assert runtime_args.instance_root == root
    assert runtime_args.instance_id == "candidate"
    with pytest.raises(SystemExit):
        parser.parse_args(["runtime", "run", "--equity", "10000", "--instance-id", "bad id"])
    assert parser.parse_args(
        ["monitor", "confidence", "--instance-root", root]
    ).instance_root == root


def test_stack_preflight_surfaces_cross_field_auth_errors(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'preflight.db'}")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_API_KEY", "")
    monkeypatch.setenv("FXSTACK_REQUIRE_CUDA", "false")
    monkeypatch.setenv("FXSTACK_PACKAGE_MODE", "1")
    monkeypatch.setenv("NODE_EXE", sys.executable)
    monkeypatch.setenv("FXSTACK_DUKASCOPY_SOURCE_ROOT", str(tmp_path))
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    try:
        rc = trader_cli._stack_preflight(argparse.Namespace(allow_sqlite=True))
    finally:
        get_settings.cache_clear()
    output = capsys.readouterr().out
    assert rc == 2
    assert "settings_validate_for_startup" in output
    assert "bridge_auth_required" in output
