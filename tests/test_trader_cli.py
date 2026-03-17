from __future__ import annotations

import argparse

import src.trader.cli as trader_cli
from src.trader.cli import build_parser


def test_cli_parses_runtime_run():
    parser = build_parser()
    ns = parser.parse_args(["runtime", "run", "--equity", "10000"])
    assert ns.cmd == "runtime"
    assert ns.runtime_cmd == "run"
    assert float(ns.equity) == 10000.0


def test_cli_parses_bridge_serve():
    parser = build_parser()
    ns = parser.parse_args(["bridge", "serve", "--port", "58710"])
    assert ns.cmd == "bridge"
    assert ns.bridge_cmd == "serve"
    assert int(ns.port) == 58710


def test_cli_parses_audit_baseline_freeze():
    parser = build_parser()
    ns = parser.parse_args(["audit", "baseline-freeze", "--", "--db-path", "data/state/runtime.db"])
    assert ns.cmd == "audit"
    assert ns.audit_cmd == "baseline-freeze"
    assert ns.tool_args[-2:] == ["--db-path", "data/state/runtime.db"]


def test_cli_parses_scenario_dual_run_compare():
    parser = build_parser()
    ns = parser.parse_args(
        [
            "scenario",
            "dual-run-compare",
            "--",
            "--baseline",
            "base.jsonl",
            "--candidate",
            "cand.jsonl",
        ]
    )
    assert ns.cmd == "scenario"
    assert ns.scenario_cmd == "dual-run-compare"
    assert "--baseline" in ns.tool_args
    assert "--candidate" in ns.tool_args


def test_cli_parses_scenario_shadow_run():
    parser = build_parser()
    ns = parser.parse_args(
        [
            "scenario",
            "shadow-run",
            "--",
            "--baseline-url",
            "http://127.0.0.1:58710",
            "--candidate-url",
            "http://127.0.0.1:58711",
        ]
    )
    assert ns.cmd == "scenario"
    assert ns.scenario_cmd == "shadow-run"
    assert "--baseline-url" in ns.tool_args
    assert "--candidate-url" in ns.tool_args


def test_tool_passthrough_strips_double_dash(monkeypatch):
    calls: dict[str, object] = {}

    def _fake_run(module_name: str, func_name: str = "main", argv: list[str] | None = None) -> int:
        calls["module_name"] = module_name
        calls["func_name"] = func_name
        calls["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(trader_cli, "_run_python_main", _fake_run)
    ns = argparse.Namespace(tool_args=["--", "--baseline-url", "http://127.0.0.1:58710"])
    rc = trader_cli._tool_passthrough("tools.shadow_dual_run", ns)

    assert int(rc) == 0
    assert calls["module_name"] == "tools.shadow_dual_run"
    assert calls["argv"] == ["--baseline-url", "http://127.0.0.1:58710"]
