from __future__ import annotations

from types import ModuleType

import pytest

from tools import orchestration_experiments


@pytest.mark.parametrize(
    ("command", "helper_name"),
    [
        ("draft", "draft_experiment"),
        ("review", "review_experiment"),
        ("replay", "replay_experiment"),
        ("paper-pack", "paper_pack_experiment"),
        ("canary-pack", "canary_pack_experiment"),
        ("promote", "promote_experiment"),
        ("trace", "trace_experiment"),
    ],
)
def test_orchestration_experiments_dispatches_each_subcommand(command: str, helper_name: str, monkeypatch, capsys) -> None:
    module = ModuleType("fxstack.orchestration.experiments")
    captured: list[dict[str, object]] = []

    def _helper(**kwargs):
        captured.append(dict(kwargs))
        return {"ok": True, "helper": helper_name, "received": sorted(kwargs)}

    setattr(module, helper_name, _helper)

    def _fake_import(name: str):
        if name == "fxstack.orchestration.experiments":
            return module
        raise ImportError(name)

    monkeypatch.setattr(orchestration_experiments.importlib, "import_module", _fake_import)

    rc = orchestration_experiments.main(
        [
            command,
            "--config",
            "cfg.json",
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

    out = capsys.readouterr().out
    assert rc == 0
    assert helper_name in out
    assert captured
    assert captured[0]["pair"] == "eurusd"
    assert captured[0]["bundle_run_id"] == "bundle-1"
    assert captured[0]["config_path"] == "cfg.json"

