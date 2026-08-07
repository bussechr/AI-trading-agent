from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

import src.trader.cli as trader_cli
from src.trader.cli import build_parser


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "trader" / "cli.py"

EXPECTED_TOP_LEVEL = {"agent", "security", "backtest"}
EXPECTED_AGENT_COMMANDS = {
    "build-dataset",
    "explain",
    "improve",
    "llm-check",
    "metrics",
    "propose",
    "robustness",
    "verify-weights",
}
EXPECTED_SECURITY_COMMANDS = {"secret", "validate-offline"}
EXPECTED_BACKTEST_COMMANDS = {"export-lean"}
RETIRED_OPERATIONAL_COMMANDS = {
    "audit",
    "bridge",
    "data",
    "db",
    "features",
    "labels",
    "live",
    "models",
    "monitor",
    "ops",
    "rl",
    "runtime",
    "scenario",
    "stack",
    "train",
}


def _subcommand_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    actions = [
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(actions) == 1
    return actions[0].choices


def test_source_facade_exposes_only_research_security_and_export_commands() -> None:
    top_level = _subcommand_choices(build_parser())
    assert set(top_level) == EXPECTED_TOP_LEVEL
    assert set(_subcommand_choices(top_level["agent"])) == EXPECTED_AGENT_COMMANDS
    assert set(_subcommand_choices(top_level["security"])) == EXPECTED_SECURITY_COMMANDS
    assert set(_subcommand_choices(top_level["backtest"])) == EXPECTED_BACKTEST_COMMANDS


@pytest.mark.parametrize("command", sorted(RETIRED_OPERATIONAL_COMMANDS))
def test_operational_command_families_fail_closed_at_parse_time(command: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args([command, "--help"])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("argv", "family", "leaf"),
    [
        (["agent", "llm-check"], "agent", "llm-check"),
        (["agent", "propose", "--seed", "1729"], "agent", "propose"),
        (
            ["agent", "improve", "--iterations", "12", "--seed", "1729"],
            "agent",
            "improve",
        ),
        (
            [
                "agent",
                "build-dataset",
                "--features",
                "features.parquet",
                "--out",
                "signals.parquet",
            ],
            "agent",
            "build-dataset",
        ),
        (["agent", "explain", "--run-dir", "run"], "agent", "explain"),
        (["agent", "robustness", "--run-dir", "run"], "agent", "robustness"),
        (["agent", "verify-weights", "--manifest", "weights.json"], "agent", "verify-weights"),
        (["agent", "metrics", "--run-dir", "run"], "agent", "metrics"),
        (["security", "validate-offline"], "security", "validate-offline"),
        (["security", "secret", "--list"], "security", "secret"),
        (
            [
                "backtest",
                "export-lean",
                "--run-dir",
                "run",
                "--out",
                "lean",
                "--pairs",
                "EURUSD,GBPUSD",
            ],
            "backtest",
            "export-lean",
        ),
    ],
)
def test_documented_source_commands_parse(argv: list[str], family: str, leaf: str) -> None:
    namespace = build_parser().parse_args(argv)
    assert namespace.cmd == family
    assert getattr(namespace, f"{family}_cmd") == leaf
    assert callable(namespace._fn)


def test_documented_secret_list_flag_is_supported() -> None:
    namespace = build_parser().parse_args(["security", "secret", "--list"])
    assert namespace.list is True
    assert not namespace.set
    assert not namespace.get
    assert not namespace.delete


def test_secret_actions_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["security", "secret", "--set", "TOKEN", "--delete", "TOKEN"])
    assert exc_info.value.code == 2


def test_secret_value_cannot_be_silently_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trader_cli, "_require_fxstack", lambda: None)
    namespace = build_parser().parse_args(["security", "secret", "--list", "--value", "ignored"])
    assert trader_cli._security_secret(namespace) == 2


def test_parser_construction_does_not_import_fxstack(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unexpected_import(name: str):
        raise AssertionError(f"parser construction imported {name}")

    monkeypatch.setattr(trader_cli.importlib, "import_module", _unexpected_import)
    parser = build_parser()
    assert set(_subcommand_choices(parser)) == EXPECTED_TOP_LEVEL


def test_missing_fxstack_reports_the_authoritative_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(_name: str):
        raise ModuleNotFoundError("No module named 'fxstack'", name="fxstack")

    monkeypatch.setattr(trader_cli.importlib, "import_module", _missing)
    with pytest.raises(SystemExit) as exc_info:
        trader_cli._require_fxstack()
    assert "uv run --project fx-quant-stack" in str(exc_info.value)


def test_facade_has_no_operational_dispatch_or_interpreter_reexec_surface() -> None:
    source = CLI.read_text(encoding="utf-8")
    forbidden = (
        "fxstack.api",
        "fxstack.runtime",
        "fxstack.training",
        "RuntimeService",
        "subprocess",
        "os.execve",
        "sys.path",
        "_tool_passthrough",
        "_module_passthrough",
    )
    for marker in forbidden:
        assert marker not in source


def test_facade_imports_only_stdlib_at_module_scope() -> None:
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    top_level_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level_imports.add(node.module or "")
    assert top_level_imports == {
        "__future__",
        "argparse",
        "collections.abc",
        "importlib",
        "json",
        "os",
        "pathlib",
    }


def test_source_compatibility_package_contains_no_duplicate_runtime_stack() -> None:
    package = ROOT / "src" / "trader"
    python_files = {path.relative_to(package).as_posix() for path in package.rglob("*.py")}
    assert python_files == {"__init__.py", "cli.py"}
