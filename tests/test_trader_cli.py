from __future__ import annotations

import argparse

import pytest

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


def test_cli_parses_audit_full_process():
    parser = build_parser()
    ns = parser.parse_args(["audit", "full-process", "--", "--evidence-root", "docs/audit"])
    assert ns.cmd == "audit"
    assert ns.audit_cmd == "full-process"
    assert "--evidence-root" in ns.tool_args


def test_cli_parses_audit_finalize_build():
    parser = build_parser()
    ns = parser.parse_args(["audit", "finalize-build", "--", "--evidence-root", "docs/audit"])
    assert ns.cmd == "audit"
    assert ns.audit_cmd == "finalize-build"
    assert "--evidence-root" in ns.tool_args


def test_cli_parses_audit_dukascopy_gate():
    parser = build_parser()
    ns = parser.parse_args(["audit", "dukascopy-gate", "--", "--source-root", "fx-quant-stack/data/dukascopy"])
    assert ns.cmd == "audit"
    assert ns.audit_cmd == "dukascopy-gate"
    assert "--source-root" in ns.tool_args


def test_cli_parses_audit_live_stack_check():
    parser = build_parser()
    ns = parser.parse_args(["audit", "live-stack-check", "--", "--base-url", "http://127.0.0.1:58710"])
    assert ns.cmd == "audit"
    assert ns.audit_cmd == "live-stack-check"
    assert "--base-url" in ns.tool_args


def test_cli_parses_data_fetch_dukascopy_matrix():
    parser = build_parser()
    ns = parser.parse_args(["data", "fetch-dukascopy-matrix", "--", "--start", "2024-01-01T00:00:00Z"])
    assert ns.cmd == "data"
    assert ns.data_cmd == "fetch-dukascopy-matrix"
    assert "--start" in ns.tool_args


def test_cli_parses_backtest_full():
    parser = build_parser()
    ns = parser.parse_args(["backtest", "full", "--", "--pairs", "EURUSD,USDJPY"])
    assert ns.cmd == "backtest"
    assert ns.backtest_cmd == "full"
    assert "--pairs" in ns.tool_args


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


def test_removed_legacy_subcommands_raise_parser_error():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["backtest", "walk-forward"])
    with pytest.raises(SystemExit):
        parser.parse_args(["audit", "strategy-conflict"])
    with pytest.raises(SystemExit):
        parser.parse_args(["optimize", "profile"])
    with pytest.raises(SystemExit):
        parser.parse_args(["scenario", "matrix"])


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


def test_runtime_legacy_impl_is_hard_rejected(monkeypatch):
    monkeypatch.setenv("TRADER_RUNTIME_IMPL", "legacy")
    ns = argparse.Namespace(config="", equity=10000.0, sleep=10)
    try:
        trader_cli._runtime_run(ns)
    except SystemExit as exc:
        assert "Legacy runtime implementation is no longer supported" in str(exc)
    else:
        raise AssertionError("expected SystemExit when legacy runtime is requested")


def test_runtime_fxstack_does_not_silently_fallback(monkeypatch):
    monkeypatch.setenv("TRADER_RUNTIME_IMPL", "fxstack")
    monkeypatch.setattr(trader_cli, "_ensure_fxstack_path", lambda: True)

    calls: list[str] = []

    def _fake_run(module_name: str, func_name: str = "main", argv: list[str] | None = None) -> int:
        calls.append(module_name)
        raise RuntimeError("boom")

    monkeypatch.setattr(trader_cli, "_run_python_main", _fake_run)
    ns = argparse.Namespace(config="", equity=10000.0, sleep=10)
    try:
        trader_cli._runtime_run(ns)
    except SystemExit as exc:
        assert "fxstack runtime startup failed" in str(exc)
    else:
        raise AssertionError("expected SystemExit when fxstack startup fails")
    assert calls == ["fxstack.runtime.runner"]


def test_bridge_legacy_impl_is_hard_rejected(monkeypatch):
    monkeypatch.setenv("TRADER_BRIDGE_IMPL", "legacy")
    ns = argparse.Namespace(host="127.0.0.1", port=58710)
    try:
        trader_cli._bridge_serve(ns)
    except SystemExit as exc:
        assert "Legacy bridge implementation is no longer supported" in str(exc)
    else:
        raise AssertionError("expected SystemExit when legacy bridge is requested")
