from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fxstack.runtime import monitor


def _start_script(tmp_path: Path) -> Path:
    path = tmp_path / "19_start_mt4.ps1"
    path.write_text("# test fixture\n", encoding="utf-8")
    return path


def test_mt4_supervisor_restarts_only_after_sustained_stale_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _run(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="started", stderr="")

    monkeypatch.setattr(monitor.subprocess, "run", _run)
    supervisor = monitor._Mt4Supervisor(
        start_script=str(_start_script(tmp_path)),
        stale_polls=3,
        cooldown_seconds=60.0,
    )
    stale = {"bridge_up": True, "runtime_ready": True, "mt4_fresh": False}

    assert supervisor.observe(stale, now=100.0) == ""
    assert supervisor.observe(stale, now=102.0) == ""
    assert supervisor.observe(stale, now=104.0) == "mt4_restart_requested"
    assert len(calls) == 1
    assert Path(calls[0][-3]).name == "19_start_mt4.ps1"
    assert calls[0][-2:] == ["-WaitSeconds", "30"]
    assert supervisor.observe(stale, now=106.0) == ""
    assert len(calls) == 1


def test_mt4_supervisor_resets_on_recovery_and_ignores_bridge_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda args, **_kwargs: (
            calls.append(list(args))
            or SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    supervisor = monitor._Mt4Supervisor(
        start_script=str(_start_script(tmp_path)),
        stale_polls=2,
        cooldown_seconds=10.0,
    )
    stale = {"bridge_up": True, "runtime_ready": True, "mt4_fresh": False}

    assert supervisor.observe(stale, now=100.0) == ""
    assert supervisor.observe(
        {"bridge_up": False, "runtime_ready": True, "mt4_fresh": False},
        now=102.0,
    ) == ""
    assert supervisor.observe(stale, now=104.0) == ""
    assert supervisor.observe(
        {"bridge_up": True, "runtime_ready": True, "mt4_fresh": True},
        now=106.0,
    ) == ""
    assert supervisor.observe(stale, now=108.0) == ""
    assert supervisor.observe(stale, now=110.0) == "mt4_restart_requested"
    assert len(calls) == 1


def test_mt4_supervisor_rejects_an_unexpected_script(tmp_path: Path) -> None:
    unexpected = tmp_path / "restart_anything.ps1"
    unexpected.write_text("# test fixture\n", encoding="utf-8")

    with pytest.raises(ValueError, match="mt4_start_script_must_be_19_start_mt4_ps1"):
        monitor._Mt4Supervisor(start_script=str(unexpected))
