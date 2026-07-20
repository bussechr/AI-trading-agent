from __future__ import annotations

from types import ModuleType

import pytest

from tools import orchestration_experiments


@pytest.mark.parametrize(
    ("command", "helper_name", "module_name"),
    [
        ("draft", "draft_experiment", "fxstack.orchestration.experiments"),
        ("review", "review_experiment", "fxstack.orchestration.experiments"),
        ("research-replay", "run_experiment", "fxstack.orchestration.replay"),
        ("paper-pack", "paper_pack_experiment", "fxstack.orchestration.experiments"),
        ("canary-pack", "canary_pack_experiment", "fxstack.orchestration.experiments"),
        ("promote", "promote_experiment", "fxstack.orchestration.experiments"),
        ("trace", "trace_experiment", "fxstack.orchestration.experiments"),
    ],
)
def test_orchestration_experiments_dispatches_each_subcommand(
    command: str,
    helper_name: str,
    module_name: str,
    monkeypatch,
    capsys,
) -> None:
    module = ModuleType(module_name)
    captured: list[dict[str, object]] = []

    def _helper(**kwargs):
        captured.append(dict(kwargs))
        return {"ok": True, "helper": helper_name, "received": sorted(kwargs)}

    setattr(module, helper_name, _helper)

    def _fake_import(name: str):
        if name == module_name:
            return module
        raise ImportError(name)

    monkeypatch.setattr(orchestration_experiments.importlib, "import_module", _fake_import)

    argv = [command, "--config", "cfg.json"]
    if command != "research-replay":
        argv.extend(
            [
                "--pair",
                "eurusd",
                "--bundle-run-id",
                "bundle-1",
                "--author",
                "anscombe",
                "--note",
                "phase7",
            ]
        )
    rc = orchestration_experiments.main(argv)

    out = capsys.readouterr().out
    assert rc == 0
    assert helper_name in out
    assert captured
    assert captured[0]["config_path"] == "cfg.json"
    if command == "research-replay":
        assert '"advisory_only": true' in out
        assert '"authorizes_activation": false' in out
        assert captured[0]["window_name"] == "all"
    else:
        assert captured[0]["pair"] == "eurusd"
        assert captured[0]["bundle_run_id"] == "bundle-1"
