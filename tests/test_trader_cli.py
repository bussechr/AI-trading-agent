from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

import src.trader.cli as trader_cli
from src.trader.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


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


def test_cli_parses_backtest_internal_pnl():
    parser = build_parser()
    ns = parser.parse_args(["backtest", "internal-pnl", "--", "--pairs", "EURUSD,USDJPY"])
    assert ns.cmd == "backtest"
    assert ns.backtest_cmd == "internal-pnl"
    assert "--pairs" in ns.tool_args


def test_cli_parses_backtest_nautilus():
    parser = build_parser()
    ns = parser.parse_args(["backtest", "nautilus", "--", "--bundle-dir", "out/bundle"])
    assert ns.cmd == "backtest"
    assert ns.backtest_cmd == "nautilus"
    assert "--bundle-dir" in ns.module_args


def test_cli_parses_backtest_lean():
    parser = build_parser()
    ns = parser.parse_args(["backtest", "lean", "--", "--bundle-dir", "out/bundle"])
    assert ns.cmd == "backtest"
    assert ns.backtest_cmd == "lean"
    assert "--bundle-dir" in ns.module_args


def test_cli_parses_backtest_stress():
    parser = build_parser()
    ns = parser.parse_args(["backtest", "stress", "--", "--report-json", "{}"])
    assert ns.cmd == "backtest"
    assert ns.backtest_cmd == "stress"
    assert "--report-json" in ns.module_args


def test_cli_parses_train_swing_patchtst():
    parser = build_parser()
    ns = parser.parse_args(["train", "swing-patchtst", "--pair", "EURUSD"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "swing-patchtst"
    assert ns.pair == "EURUSD"


def test_cli_parses_train_intraday_patchtst():
    parser = build_parser()
    ns = parser.parse_args(["train", "intraday-patchtst", "--pair", "EURUSD"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "intraday-patchtst"
    assert ns.pair == "EURUSD"


def test_cli_parses_train_all_with_patchtst():
    parser = build_parser()
    ns = parser.parse_args(["train", "all", "--pair", "EURUSD", "--with-patchtst"])
    assert ns.cmd == "train"
    assert ns.train_cmd == "all"
    assert bool(ns.with_patchtst) is True


def test_cli_parses_models_stage_release():
    parser = build_parser()
    ns = parser.parse_args(
        [
            "models",
            "stage-release",
            "--pair",
            "EURUSD",
            "--author",
            "ops",
            "--allowlisted-pair",
            "EURUSD",
        ]
    )
    assert ns.cmd == "models"
    assert ns.models_cmd == "stage-release"
    assert ns.pair == "EURUSD"
    assert ns.allowlisted_pair == ["EURUSD"]


def test_models_activate_defaults_to_compat_when_source_is_omitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FXSTACK_MLFLOW_ENABLED", "1")
    monkeypatch.setenv("FXSTACK_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}")
    monkeypatch.setattr(trader_cli, "_ensure_fxstack_runtime", lambda: None)

    from fxstack.settings import get_settings

    get_settings.cache_clear()

    calls: dict[str, object] = {}

    def _activate_pairs(**kwargs):
        calls["source"] = "compat"
        calls["kwargs"] = kwargs
        return [{"pair": "EURUSD"}]

    def _activate_mlflow_alias(**kwargs):
        calls["source"] = "mlflow"
        calls["kwargs"] = kwargs
        return []

    monkeypatch.setattr("fxstack.training.activation.activate_pairs", _activate_pairs)
    monkeypatch.setattr("fxstack.training.activation.activate_mlflow_alias", _activate_mlflow_alias)

    try:
        rc = trader_cli._models_activate(
            argparse.Namespace(
                database_url="sqlite+pysqlite:///" + str(tmp_path / "runtime.db"),
                registry_root=str(tmp_path / "registry"),
                manifest=str(tmp_path / "manifest.json"),
                registry_file="",
                pair=["EURUSD"],
                source="",
                alias="champion",
                require_all=False,
            )
        )
    finally:
        get_settings.cache_clear()

    assert rc == 0
    assert calls["source"] == "compat"


def test_cli_parses_models_canary_close():
    parser = build_parser()
    ns = parser.parse_args(["models", "canary-close", "--pair", "EURUSD", "--outcome", "graduate"])
    assert ns.cmd == "models"
    assert ns.models_cmd == "canary-close"
    assert ns.outcome == "graduate"


def test_cli_parses_models_release_status():
    parser = build_parser()
    ns = parser.parse_args(["models", "release-status", "--pair", "EURUSD"])
    assert ns.cmd == "models"
    assert ns.models_cmd == "release-status"
    assert ns.pair == "EURUSD"


def test_cli_parses_rl_export_transitions():
    parser = build_parser()
    ns = parser.parse_args(["rl", "export-transitions", "--input", "bundle.json"])
    assert ns.cmd == "rl"
    assert ns.rl_cmd == "export-transitions"
    assert ns.input == "bundle.json"


def test_cli_parses_rl_train_ppo():
    parser = build_parser()
    ns = parser.parse_args(["rl", "train-ppo", "--dataset", "bundle.parquet"])
    assert ns.cmd == "rl"
    assert ns.rl_cmd == "train-ppo"
    assert ns.dataset == "bundle.parquet"


def test_cli_parses_rl_train_cql():
    parser = build_parser()
    ns = parser.parse_args(["rl", "train-cql", "--dataset", "bundle.parquet"])
    assert ns.cmd == "rl"
    assert ns.rl_cmd == "train-cql"
    assert ns.dataset == "bundle.parquet"


def test_cli_parses_rl_evaluate():
    parser = build_parser()
    ns = parser.parse_args(["rl", "evaluate", "--dataset", "bundle.parquet"])
    assert ns.cmd == "rl"
    assert ns.rl_cmd == "evaluate"
    assert ns.dataset == "bundle.parquet"


def test_cli_parses_stack_sequence_research_check():
    parser = build_parser()
    ns = parser.parse_args(["stack", "sequence-research-check"])
    assert ns.cmd == "stack"
    assert ns.stack_cmd == "sequence-research-check"


def test_cli_parses_features_compact_feast():
    parser = build_parser()
    ns = parser.parse_args(["features", "compact-feast", "--pair", "EURUSD", "GBPUSD"])
    assert ns.cmd == "features"
    assert ns.features_cmd == "compact-feast"
    assert ns.pair == ["EURUSD", "GBPUSD"]


def test_cli_parses_features_push_worker():
    parser = build_parser()
    ns = parser.parse_args(["features", "push-worker", "--limit", "10", "--dry-run"])
    assert ns.cmd == "features"
    assert ns.features_cmd == "push-worker"
    assert int(ns.limit) == 10
    assert bool(ns.dry_run) is True


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


def test_module_passthrough_strips_double_dash(monkeypatch):
    calls: dict[str, object] = {}

    def _fake_run(module_name: str, func_name: str = "main", argv: list[str] | None = None) -> int:
        calls["module_name"] = module_name
        calls["func_name"] = func_name
        calls["argv"] = list(argv or [])
        return 0

    monkeypatch.setattr(trader_cli, "_run_python_main", _fake_run)
    ns = argparse.Namespace(module_args=["--", "--bundle-dir", "out/bundle"])
    rc = trader_cli._module_passthrough("fxstack.backtest.harness.nautilus", ns)

    assert int(rc) == 0
    assert calls["module_name"] == "fxstack.backtest.harness.nautilus"
    assert calls["argv"] == ["--bundle-dir", "out/bundle"]


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


def test_fxstack_python_candidates_preserve_venv_symlink_path(monkeypatch, tmp_path: Path):
    repo_root = tmp_path / "repo"
    bin_dir = repo_root / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    target = tmp_path / "python-real"
    target.write_text("", encoding="utf-8")
    symlink = bin_dir / "python"
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")

    current_python = tmp_path / "current-python"
    current_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(trader_cli, "_repo_root", lambda: repo_root)
    monkeypatch.setattr(trader_cli.sys, "executable", str(current_python))
    monkeypatch.delenv("TRADER_FXSTACK_PYTHON", raising=False)
    monkeypatch.delenv("FXSTACK_PYTHON", raising=False)

    candidates = trader_cli._fxstack_python_candidates()

    assert symlink.absolute() in candidates
    assert target.resolve() not in candidates
