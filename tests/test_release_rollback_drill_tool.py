from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pytest

from fxstack.training.release_evidence import active_manifest_identity, file_sha256
from tools.run_release_rollback_drill import (
    DIAGNOSTIC_SCHEMA,
    PRODUCER_TOOL,
    RollbackDrillConfig,
    RollbackDrillError,
    build_plan,
    main,
    run_release_rollback_drill,
    validate_rollback_evidence_artifact,
    validate_rollback_evidence_payload,
)


PAIR = "EURUSD"
CODE_SHA256 = "c" * 64
CONFIG_SHA256 = "d" * 64
RUNTIME_GENERATION = "runtime-release-17"
RUNTIME_BOOT_ID = "boot-release-17"
CANDIDATE_DATABASE_IDENTITY = "e" * 64
BASELINE_DATABASE_IDENTITY = "f" * 64
CANDIDATE_SNAPSHOT_SCOPE = "candidate-release-17"
CANDIDATE_INSTANCE_ID = "candidate-17"


def _manifest_bytes(*, bundle_run_id: str, model_set_id: str, digest: str) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "active_model_sets": {
                PAIR: {
                    "model_set_id": model_set_id,
                    "metadata": {"bundle_run_id": bundle_run_id},
                    "artifacts": {
                        "meta": {"content_sha256": digest * 64},
                        "intraday": {"content_sha256": digest.upper() * 64},
                    },
                }
            },
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _identity(path: Path) -> dict[str, str]:
    identity = active_manifest_identity(manifest_path=path, pair=PAIR)
    return {
        "pair": identity.pair,
        "bundle_run_id": identity.bundle_run_id,
        "model_set_id": identity.model_set_id,
        "model_manifest_sha256": identity.model_manifest_sha256,
        "artifact_set_sha256": identity.artifact_set_sha256,
    }


class _TickClock:
    def __init__(self) -> None:
        self.value = 1_800_000_000.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


class _FakeRunner:
    def __init__(self, *, active_path: Path, candidate: bytes, target: bytes) -> None:
        self.active_path = active_path
        self.candidate = candidate
        self.target = target
        self.phase = "before"
        self.reload_generation = "reload-candidate-before"
        self.calls: list[str] = []
        self.fail_target = False
        self.runtime_enabled_phase = ""
        self.missing_ack_phase = ""
        self.db_mismatch_phase = ""
        self.database_identity = CANDIDATE_DATABASE_IDENTITY

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, timeout
        if "activate-target" in command:
            name = "activate-target"
        elif "restore-candidate" in command:
            name = "restore-candidate"
        else:
            name = str(command[0])
        self.calls.append(name)
        if name == "activate-target":
            self.active_path.write_bytes(self.target)
            self.phase = "target"
            self.reload_generation = "reload-target"
            return self._result(
                command,
                9 if self.fail_target else 0,
                {"reload_generation": self.reload_generation},
                stderr=b"activation failed" if self.fail_target else b"",
            )
        if name == "restore-candidate":
            self.active_path.write_bytes(self.candidate)
            self.phase = "restored"
            self.reload_generation = "reload-restored"
            return self._result(
                command,
                0,
                {"reload_generation": self.reload_generation},
            )
        if name == "observe-db":
            payload = {
                **_identity(self.active_path),
                "database_role": "candidate",
                "candidate_instance_id": CANDIDATE_INSTANCE_ID,
                "database_identity_sha256": self.database_identity,
                "snapshot_scope": CANDIDATE_SNAPSHOT_SCOPE,
            }
            if self.db_mismatch_phase == self.phase:
                payload["bundle_run_id"] = "forged-db-bundle"
            return self._result(command, 0, payload)
        if name == "observe-runtime":
            payload: dict[str, Any] = {
                **_identity(self.active_path),
                "runtime_boot_id": RUNTIME_BOOT_ID,
                "runtime_generation": RUNTIME_GENERATION,
                "runtime_ready": True,
                "runtime_commands_disabled": self.runtime_enabled_phase != self.phase,
                "reload_generation": self.reload_generation,
                "database_role": "candidate",
                "candidate_instance_id": CANDIDATE_INSTANCE_ID,
                "database_identity_sha256": self.database_identity,
                "snapshot_scope": CANDIDATE_SNAPSHOT_SCOPE,
            }
            if self.missing_ack_phase != self.phase:
                payload["ack_generation"] = self.reload_generation
            return self._result(command, 0, payload)
        raise AssertionError(f"unexpected command: {command!r}")

    @staticmethod
    def _result(
        command: Sequence[str],
        return_code: int,
        payload: dict[str, Any],
        *,
        stderr: bytes = b"",
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            list(command),
            return_code,
            stdout=json.dumps(payload, sort_keys=True).encode("utf-8"),
            stderr=stderr,
        )


def _case(tmp_path: Path) -> tuple[RollbackDrillConfig, _FakeRunner, bytes, bytes]:
    candidate = _manifest_bytes(
        bundle_run_id="bundle-candidate",
        model_set_id="model-candidate",
        digest="a",
    )
    target = _manifest_bytes(
        bundle_run_id="bundle-rollback-target",
        model_set_id="model-rollback-target",
        digest="b",
    )
    active_path = tmp_path / "active_models.json"
    target_path = tmp_path / "rollback_target.json"
    active_path.write_bytes(candidate)
    target_path.write_bytes(target)
    controller_path = tmp_path / "candidate_release_controller.py"
    controller_path.write_text("# candidate-scoped controller fixture\n", encoding="utf-8")
    scope_args = (
        "--candidate-pid",
        "4317",
        "--candidate-port",
        "58711",
        "--candidate-boot-id",
        RUNTIME_BOOT_ID,
        "--candidate-instance-id",
        CANDIDATE_INSTANCE_ID,
        "--candidate-database-identity-sha256",
        CANDIDATE_DATABASE_IDENTITY,
        "--candidate-snapshot-scope",
        CANDIDATE_SNAPSHOT_SCOPE,
    )
    config = RollbackDrillConfig(
        pair=PAIR,
        release_run_id="release-run-17",
        code_sha256=CODE_SHA256,
        git_commit="abc123def456",
        config_sha256=CONFIG_SHA256,
        runtime_generation=RUNTIME_GENERATION,
        runtime_boot_id=RUNTIME_BOOT_ID,
        candidate_controller_path=controller_path,
        candidate_controller_sha256=file_sha256(controller_path),
        candidate_pid=4317,
        candidate_port=58711,
        candidate_instance_id=CANDIDATE_INSTANCE_ID,
        candidate_database_identity_sha256=CANDIDATE_DATABASE_IDENTITY,
        baseline_database_identity_sha256=BASELINE_DATABASE_IDENTITY,
        candidate_snapshot_scope=CANDIDATE_SNAPSHOT_SCOPE,
        active_manifest_path=active_path,
        rollback_target_manifest_path=target_path,
        output_dir=tmp_path / "rollback-drill",
        target_activation_command=(
            sys.executable,
            str(controller_path),
            "activate-target",
            *scope_args,
        ),
        candidate_restore_command=(
            sys.executable,
            str(controller_path),
            "restore-candidate",
            *scope_args,
        ),
        db_observation_command=("observe-db", *scope_args),
        runtime_observation_command=("observe-runtime", *scope_args),
        command_cwd=tmp_path,
        timeout_secs=5.0,
    )
    runner = _FakeRunner(active_path=active_path, candidate=candidate, target=target)
    return config, runner, candidate, target


def _run_success(
    tmp_path: Path,
) -> tuple[RollbackDrillConfig, _FakeRunner, dict[str, Any]]:
    config, runner, _, _ = _case(tmp_path)
    payload = run_release_rollback_drill(
        config,
        command_runner=runner,
        clock=_TickClock(),
    )
    return config, runner, payload


def test_plan_only_does_not_create_authority_shaped_output(tmp_path: Path) -> None:
    config, runner, _, _ = _case(tmp_path)

    plan = build_plan(config)

    assert plan["mode"] == "plan_only"
    assert plan["execute_required"] is True
    assert "schema_version" not in plan
    assert not config.output_dir.exists()
    assert runner.calls == []


def test_cli_defaults_to_plan_only_without_emitting_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _, _, _ = _case(tmp_path)
    argv = [
        "run_release_rollback_drill.py",
        "--pair",
        config.pair,
        "--release-run-id",
        config.release_run_id,
        "--code-sha256",
        config.code_sha256,
        "--git-commit",
        config.git_commit,
        "--config-sha256",
        config.config_sha256,
        "--runtime-generation",
        config.runtime_generation,
        "--runtime-boot-id",
        config.runtime_boot_id,
        "--candidate-controller",
        str(config.candidate_controller_path),
        "--candidate-controller-sha256",
        config.candidate_controller_sha256,
        "--candidate-pid",
        str(config.candidate_pid),
        "--candidate-port",
        str(config.candidate_port),
        "--candidate-instance-id",
        config.candidate_instance_id,
        "--candidate-database-identity-sha256",
        config.candidate_database_identity_sha256,
        "--baseline-database-identity-sha256",
        config.baseline_database_identity_sha256,
        "--candidate-snapshot-scope",
        config.candidate_snapshot_scope,
        "--active-manifest",
        str(config.active_manifest_path),
        "--rollback-target-manifest",
        str(config.rollback_target_manifest_path),
        "--output-dir",
        str(config.output_dir),
        "--target-activation-command-json",
        json.dumps(config.target_activation_command),
        "--candidate-restore-command-json",
        json.dumps(config.candidate_restore_command),
        "--db-observation-command-json",
        json.dumps(config.db_observation_command),
        "--runtime-observation-command-json",
        json.dumps(config.runtime_observation_command),
        "--command-cwd",
        str(config.command_cwd),
    ]
    monkeypatch.setattr(sys, "argv", argv)

    assert main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "plan_only"
    assert "schema_version" not in output
    assert not config.output_dir.exists()


def test_success_executes_both_mutations_and_reopens_all_observations(
    tmp_path: Path,
) -> None:
    config, runner, payload = _run_success(tmp_path)

    assert runner.calls == [
        "observe-db",
        "observe-runtime",
        "activate-target",
        "observe-db",
        "observe-runtime",
        "restore-candidate",
        "observe-db",
        "observe-runtime",
    ]
    assert payload["status"] == "passed"
    assert payload["producer"]["tool"] == PRODUCER_TOOL
    assert {
        key: payload["release_context"][key]
        for key in (
            "release_run_id",
            "code_sha256",
            "git_commit",
            "config_sha256",
            "runtime_generation",
            "runtime_boot_id",
        )
    } == {
        "release_run_id": "release-run-17",
        "code_sha256": CODE_SHA256,
        "git_commit": "abc123def456",
        "config_sha256": CONFIG_SHA256,
        "runtime_generation": RUNTIME_GENERATION,
        "runtime_boot_id": RUNTIME_BOOT_ID,
    }
    before = payload["observations"]["before"]
    target = payload["observations"]["target"]
    restored = payload["observations"]["restored"]
    assert payload["release_context"]["model_identity_sha256"] == payload[
        "evidence_identity"
    ]["model_manifest_sha256"]
    assert payload["release_context"]["manifest_file_sha256"] == before["manifest"][
        "sha256"
    ]
    assert before["manifest"]["sha256"] == restored["manifest"]["sha256"]
    assert target["manifest"]["sha256"] != before["manifest"]["sha256"]
    for phase in (before, target, restored):
        for kind in ("manifest", "db", "runtime"):
            assert Path(phase[kind]["snapshot_path"]).is_absolute()
    evidence_path = config.output_dir / "rollback_evidence.json"
    assert evidence_path.is_file()
    assert not (config.output_dir / "rollback_diagnostic.json").exists()
    assert validate_rollback_evidence_artifact(evidence_path) == []


def test_preexisting_output_fails_before_any_command(tmp_path: Path) -> None:
    config, runner, _, _ = _case(tmp_path)
    config.output_dir.mkdir()
    (config.output_dir / "unrelated.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(RollbackDrillError, match="output path already exists"):
        run_release_rollback_drill(config, command_runner=runner, clock=_TickClock())

    assert runner.calls == []
    assert (config.output_dir / "unrelated.txt").read_text(encoding="utf-8") == "keep"


def test_broad_or_unscoped_controller_is_rejected_before_any_command(
    tmp_path: Path,
) -> None:
    config, runner, _, _ = _case(tmp_path)
    unsafe = replace(
        config,
        target_activation_command=(
            str(tmp_path / "90_stop_all.bat"),
            "all",
            "--candidate-pid",
            "4317",
            "--candidate-port",
            "58711",
            "--candidate-boot-id",
            RUNTIME_BOOT_ID,
        ),
    )

    with pytest.raises(RollbackDrillError, match="unsafe or unscoped"):
        run_release_rollback_drill(unsafe, command_runner=runner, clock=_TickClock())

    assert runner.calls == []
    assert not unsafe.output_dir.exists()


def test_baseline_database_observation_blocks_mutation(tmp_path: Path) -> None:
    config, runner, _, _ = _case(tmp_path)
    runner.database_identity = BASELINE_DATABASE_IDENTITY

    with pytest.raises(RollbackDrillError, match="outside candidate DB scope"):
        run_release_rollback_drill(config, command_runner=runner, clock=_TickClock())

    assert "activate-target" not in runner.calls
    assert not (config.output_dir / "rollback_evidence.json").exists()
    diagnostic = json.loads(
        (config.output_dir / "rollback_diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["authoritative"] is False


def test_target_command_failure_restores_and_emits_only_non_authoritative_diagnostic(
    tmp_path: Path,
) -> None:
    config, runner, candidate, _ = _case(tmp_path)
    runner.fail_target = True

    with pytest.raises(RollbackDrillError, match="target activation command failed"):
        run_release_rollback_drill(config, command_runner=runner, clock=_TickClock())

    assert runner.calls[-3:] == ["restore-candidate", "observe-db", "observe-runtime"]
    assert config.active_manifest_path.read_bytes() == candidate
    assert not (config.output_dir / "rollback_evidence.json").exists()
    diagnostic = json.loads(
        (config.output_dir / "rollback_diagnostic.json").read_text(encoding="utf-8")
    )
    assert diagnostic["schema_version"] == DIAGNOSTIC_SCHEMA
    assert diagnostic["status"] == "failed"
    assert diagnostic["authoritative"] is False
    assert diagnostic["advisory_only"] is True
    assert diagnostic["steps"]["target_activation"]["return_code"] == 9
    assert diagnostic["steps"]["candidate_restore"]["return_code"] == 0


@pytest.mark.parametrize(
    ("failure_mode", "expected_error"),
    [
        ("runtime_enabled", "rollback_target_runtime_enabled"),
        ("missing_ack", "rollback_target_reload_ack_missing"),
        ("identity_mismatch", "rollback_target_db_identity_mismatch"),
    ],
)
def test_observed_target_failure_is_restored_and_never_emits_authority(
    tmp_path: Path,
    failure_mode: str,
    expected_error: str,
) -> None:
    config, runner, candidate, _ = _case(tmp_path)
    if failure_mode == "runtime_enabled":
        runner.runtime_enabled_phase = "target"
    elif failure_mode == "missing_ack":
        runner.missing_ack_phase = "target"
    else:
        runner.db_mismatch_phase = "target"

    with pytest.raises(RollbackDrillError, match=expected_error):
        run_release_rollback_drill(config, command_runner=runner, clock=_TickClock())

    assert config.active_manifest_path.read_bytes() == candidate
    assert "restore-candidate" in runner.calls
    assert not (config.output_dir / "rollback_evidence.json").exists()
    diagnostic = json.loads(
        (config.output_dir / "rollback_diagnostic.json").read_text(encoding="utf-8")
    )
    assert expected_error in " ".join(diagnostic["errors"])


def test_tampered_snapshot_is_rejected_when_artifact_is_reopened(tmp_path: Path) -> None:
    config, _, payload = _run_success(tmp_path)
    db_path = Path(payload["observations"]["target"]["db"]["snapshot_path"])
    db_path.write_text("{}", encoding="utf-8")

    errors = validate_rollback_evidence_artifact(
        config.output_dir / "rollback_evidence.json"
    )

    assert "rollback_target_db_hash_mismatch" in errors


def test_forged_wrapper_cannot_hide_raw_runtime_enabled_observation(
    tmp_path: Path,
) -> None:
    config, _, payload = _run_success(tmp_path)
    target = payload["observations"]["target"]
    runtime_path = Path(target["runtime"]["snapshot_path"])
    raw_runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    raw_runtime["runtime_commands_disabled"] = False
    runtime_path.write_text(json.dumps(raw_runtime, sort_keys=True), encoding="utf-8")
    target["runtime"]["sha256"] = file_sha256(runtime_path)
    target["runtime_commands_disabled"] = True

    errors = validate_rollback_evidence_payload(payload, output_dir=config.output_dir)

    assert "rollback_target_runtime_raw_copy_mismatch" in errors

    raw_stdout = Path(target["capture_steps"]["runtime"]["stdout"]["path"])
    raw_stdout.write_bytes(runtime_path.read_bytes())
    target["capture_steps"]["runtime"]["stdout"]["sha256"] = file_sha256(raw_stdout)
    errors = validate_rollback_evidence_payload(payload, output_dir=config.output_dir)

    assert "rollback_target_runtime_enabled" in errors
