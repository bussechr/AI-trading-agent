from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.training.release_evidence import (  # noqa: E402
    ROLLBACK_EVIDENCE_SCHEMA,
    ReleaseEvidenceIdentity,
    active_manifest_identity,
    file_sha256,
    is_sha256,
)


PRODUCER_TOOL = "tools.run_release_rollback_drill"
PRODUCER_VERSION = "v1"
DIAGNOSTIC_SCHEMA = "fxstack_rollback_drill_diagnostic_v1"
PHASES = ("before", "target", "restored")
MUTATION_STEPS = ("target_activation", "candidate_restore")


class RollbackDrillError(RuntimeError):
    pass


CommandRunner = Callable[..., subprocess.CompletedProcess[Any]]
Clock = Callable[[], float]


@dataclass(frozen=True)
class RollbackDrillConfig:
    pair: str
    release_run_id: str
    code_sha256: str
    config_sha256: str
    runtime_generation: str
    runtime_boot_id: str
    candidate_controller_path: Path
    candidate_controller_sha256: str
    candidate_pid: int
    candidate_port: int
    candidate_instance_id: str
    candidate_database_identity_sha256: str
    baseline_database_identity_sha256: str
    candidate_snapshot_scope: str
    active_manifest_path: Path
    rollback_target_manifest_path: Path
    output_dir: Path
    target_activation_command: tuple[str, ...]
    candidate_restore_command: tuple[str, ...]
    db_observation_command: tuple[str, ...]
    runtime_observation_command: tuple[str, ...]
    git_commit: str = ""
    command_cwd: Path | None = None
    timeout_secs: float = 300.0


def _safe_float(value: Any, default: float = float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _json_object(raw: bytes | str, *, label: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        payload = json.loads(text)
    except Exception as exc:
        raise RollbackDrillError(f"{label} did not emit one valid JSON object") from exc
    if not isinstance(payload, dict):
        raise RollbackDrillError(f"{label} did not emit one valid JSON object")
    return dict(payload)


def _command_tuple(value: Sequence[str], *, label: str) -> tuple[str, ...]:
    command = tuple(str(item) for item in value)
    if not command or any(not item.strip() for item in command):
        raise RollbackDrillError(f"{label} must be a non-empty argv array")
    return command


def _identity_fields(identity: Any) -> dict[str, str]:
    return {
        "pair": str(getattr(identity, "pair", "") or "").strip().upper(),
        "bundle_run_id": str(getattr(identity, "bundle_run_id", "") or "").strip(),
        "model_set_id": str(getattr(identity, "model_set_id", "") or "").strip(),
        "model_manifest_sha256": str(getattr(identity, "model_manifest_sha256", "") or "").strip().lower(),
        "artifact_set_sha256": str(getattr(identity, "artifact_set_sha256", "") or "").strip().lower(),
    }


def _identity_from_mapping(value: Mapping[str, Any]) -> dict[str, str]:
    return {
        "pair": str(value.get("pair") or "").strip().upper(),
        "bundle_run_id": str(value.get("bundle_run_id") or "").strip(),
        "model_set_id": str(value.get("model_set_id") or "").strip(),
        "model_manifest_sha256": str(value.get("model_manifest_sha256") or "").strip().lower(),
        "artifact_set_sha256": str(value.get("artifact_set_sha256") or "").strip().lower(),
    }


def _identity_errors(identity: Mapping[str, Any], *, label: str) -> list[str]:
    normalized = _identity_from_mapping(identity)
    errors: list[str] = []
    for field in ("pair", "bundle_run_id", "model_set_id"):
        if not normalized[field]:
            errors.append(f"{label}_{field}_missing")
    for field in ("model_manifest_sha256", "artifact_set_sha256"):
        if not is_sha256(normalized[field]):
            errors.append(f"{label}_{field}_invalid")
    return errors


def _configured_release_context(config: RollbackDrillConfig) -> dict[str, str]:
    return {
        "release_run_id": str(config.release_run_id or "").strip(),
        "code_sha256": str(config.code_sha256 or "").strip().lower(),
        "git_commit": str(config.git_commit or "").strip(),
        "config_sha256": str(config.config_sha256 or "").strip().lower(),
        "runtime_generation": str(config.runtime_generation or "").strip(),
        "runtime_boot_id": str(config.runtime_boot_id or "").strip(),
    }


def _bound_release_context(
    config: RollbackDrillConfig,
    *,
    candidate_identity: Mapping[str, Any],
    manifest_file_sha256: str,
) -> dict[str, str]:
    return {
        **_configured_release_context(config),
        "model_identity_sha256": str(
            candidate_identity.get("model_manifest_sha256") or ""
        )
        .strip()
        .lower(),
        "manifest_file_sha256": str(manifest_file_sha256 or "").strip().lower(),
    }


def _release_context_errors(context: Mapping[str, Any]) -> list[str]:
    normalized = {
        "release_run_id": str(context.get("release_run_id") or "").strip(),
        "code_sha256": str(context.get("code_sha256") or "").strip().lower(),
        "git_commit": str(context.get("git_commit") or "").strip(),
        "config_sha256": str(context.get("config_sha256") or "").strip().lower(),
        "runtime_generation": str(context.get("runtime_generation") or "").strip(),
        "runtime_boot_id": str(context.get("runtime_boot_id") or "").strip(),
        "model_identity_sha256": str(
            context.get("model_identity_sha256") or ""
        )
        .strip()
        .lower(),
        "manifest_file_sha256": str(
            context.get("manifest_file_sha256") or ""
        )
        .strip()
        .lower(),
    }
    errors: list[str] = []
    for field in ("release_run_id", "runtime_generation", "runtime_boot_id"):
        if not normalized[field]:
            errors.append(f"rollback_release_context_{field}_missing")
    for field in (
        "code_sha256",
        "config_sha256",
        "model_identity_sha256",
        "manifest_file_sha256",
    ):
        if not is_sha256(normalized[field]):
            errors.append(f"rollback_release_context_{field}_invalid")
    return errors


_BROAD_COMMAND_MARKERS = (
    "90_stop_all",
    "stop-all",
    "stop_all",
    "stopall",
    "kill-all",
    "kill_all",
    "taskkill",
    "stop-process",
    "baseline",
    "shared",
)
_SCOPE_FLAGS = (
    "--candidate-pid",
    "--candidate-port",
    "--candidate-boot-id",
    "--candidate-instance-id",
    "--candidate-database-identity-sha256",
    "--candidate-snapshot-scope",
)


def _candidate_scope(config: RollbackDrillConfig) -> dict[str, Any]:
    return {
        "controller_path": str(config.candidate_controller_path.resolve()),
        "controller_sha256": str(config.candidate_controller_sha256 or "").strip().lower(),
        "candidate_pid": _safe_int(config.candidate_pid),
        "candidate_port": _safe_int(config.candidate_port),
        "candidate_boot_id": str(config.runtime_boot_id or "").strip(),
        "candidate_instance_id": str(config.candidate_instance_id or "").strip(),
        "candidate_database_identity_sha256": str(
            config.candidate_database_identity_sha256 or ""
        )
        .strip()
        .lower(),
        "baseline_database_identity_sha256": str(
            config.baseline_database_identity_sha256 or ""
        )
        .strip()
        .lower(),
        "candidate_snapshot_scope": str(config.candidate_snapshot_scope or "").strip(),
    }


def _same_resolved_path(raw: str, expected: Path) -> bool:
    try:
        return Path(raw).resolve() == expected.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _invokes_controller(command: Sequence[str], controller_path: Path) -> bool:
    argv = [str(item) for item in command]
    if not argv:
        return False
    if _same_resolved_path(argv[0], controller_path):
        return True
    launcher = Path(argv[0]).name.lower()
    if (launcher.startswith("python") or launcher in {"py", "py.exe"}) and len(argv) > 1:
        return _same_resolved_path(argv[1], controller_path)
    if launcher.startswith("powershell") or launcher.startswith("pwsh"):
        lowered = [item.lower() for item in argv]
        if "-file" in lowered:
            index = lowered.index("-file")
            return index + 1 < len(argv) and _same_resolved_path(
                argv[index + 1],
                controller_path,
            )
    return False


def _scoped_command_errors(
    command: Sequence[str],
    *,
    scope: Mapping[str, Any],
    label: str,
    require_controller: bool,
    required_operation: str = "",
) -> list[str]:
    argv = [str(item) for item in command]
    errors: list[str] = []
    lowered = [item.strip().lower() for item in argv]
    command_names = [Path(item).name.lower() for item in argv]
    if any(
        marker in command_name
        for marker in _BROAD_COMMAND_MARKERS
        for command_name in command_names
    ) or "all" in lowered:
        errors.append(f"{label}_broad_command_rejected")
    if any(any(char in item for char in ";&|><\r\n") for item in argv):
        errors.append(f"{label}_shell_metacharacter_rejected")
    expected_values = {
        "--candidate-pid": str(_safe_int(scope.get("candidate_pid"))),
        "--candidate-port": str(_safe_int(scope.get("candidate_port"))),
        "--candidate-boot-id": str(scope.get("candidate_boot_id") or "").strip(),
        "--candidate-instance-id": str(scope.get("candidate_instance_id") or "").strip(),
        "--candidate-database-identity-sha256": str(
            scope.get("candidate_database_identity_sha256") or ""
        )
        .strip()
        .lower(),
        "--candidate-snapshot-scope": str(
            scope.get("candidate_snapshot_scope") or ""
        ).strip(),
    }
    for flag in _SCOPE_FLAGS:
        positions = [index for index, token in enumerate(lowered) if token == flag]
        if len(positions) != 1:
            errors.append(f"{label}_{flag[2:].replace('-', '_')}_missing")
            continue
        index = positions[0]
        if index + 1 >= len(argv) or argv[index + 1] != expected_values[flag]:
            errors.append(f"{label}_{flag[2:].replace('-', '_')}_mismatch")
    controller_path = Path(str(scope.get("controller_path") or ""))
    if require_controller and not _invokes_controller(argv, controller_path):
        errors.append(f"{label}_controller_mismatch")
    if required_operation and required_operation not in lowered:
        errors.append(f"{label}_operation_missing")
    return errors


def _candidate_scope_errors(scope: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    controller_path = Path(str(scope.get("controller_path") or ""))
    controller_sha = str(scope.get("controller_sha256") or "").strip().lower()
    if (
        not controller_path.is_absolute()
        or controller_path.suffix.lower() not in {".exe", ".py", ".ps1"}
        or not controller_path.is_file()
    ):
        errors.append("rollback_candidate_controller_invalid")
    elif not is_sha256(controller_sha) or file_sha256(controller_path) != controller_sha:
        errors.append("rollback_candidate_controller_hash_mismatch")
    try:
        candidate_pid = int(scope.get("candidate_pid") or 0)
    except (TypeError, ValueError, OverflowError):
        candidate_pid = 0
    try:
        candidate_port = int(scope.get("candidate_port") or 0)
    except (TypeError, ValueError, OverflowError):
        candidate_port = 0
    if candidate_pid <= 0:
        errors.append("rollback_candidate_pid_invalid")
    if not 1 <= candidate_port <= 65535:
        errors.append("rollback_candidate_port_invalid")
    if not str(scope.get("candidate_boot_id") or "").strip():
        errors.append("rollback_candidate_boot_id_missing")
    candidate_instance_id = str(scope.get("candidate_instance_id") or "").strip()
    if not candidate_instance_id or candidate_instance_id.lower() in {
        "baseline",
        "shared",
        "production",
    }:
        errors.append("rollback_candidate_instance_id_invalid")
    candidate_database = str(
        scope.get("candidate_database_identity_sha256") or ""
    ).strip().lower()
    baseline_database = str(
        scope.get("baseline_database_identity_sha256") or ""
    ).strip().lower()
    if not is_sha256(candidate_database):
        errors.append("rollback_candidate_database_identity_invalid")
    if not is_sha256(baseline_database):
        errors.append("rollback_baseline_database_identity_invalid")
    if candidate_database == baseline_database:
        errors.append("rollback_candidate_database_not_isolated")
    snapshot_scope = str(scope.get("candidate_snapshot_scope") or "").strip()
    if not snapshot_scope or snapshot_scope.lower() in {"baseline", "shared", "production"}:
        errors.append("rollback_candidate_snapshot_scope_invalid")
    return errors


def _observation_scope_errors(
    payload: Mapping[str, Any],
    *,
    scope: Mapping[str, Any],
    label: str,
) -> list[str]:
    errors: list[str] = []
    expected = {
        "candidate_instance_id": str(scope.get("candidate_instance_id") or "").strip(),
        "database_identity_sha256": str(
            scope.get("candidate_database_identity_sha256") or ""
        )
        .strip()
        .lower(),
        "snapshot_scope": str(scope.get("candidate_snapshot_scope") or "").strip(),
    }
    observed = {
        "candidate_instance_id": str(payload.get("candidate_instance_id") or "").strip(),
        "database_identity_sha256": str(
            payload.get("database_identity_sha256") or ""
        )
        .strip()
        .lower(),
        "snapshot_scope": str(payload.get("snapshot_scope") or "").strip(),
    }
    if str(payload.get("database_role") or "").strip().lower() != "candidate":
        errors.append(f"{label}_database_role_invalid")
    for field, expected_value in expected.items():
        if observed[field] != expected_value:
            errors.append(f"{label}_{field}_mismatch")
    return errors


def _artifact_ref(path: Path, *, snapshot: bool = False) -> dict[str, str]:
    resolved = path.resolve()
    return {
        "snapshot_path" if snapshot else "path": str(resolved),
        "sha256": file_sha256(resolved),
    }


def _write_exclusive(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()


def _default_command_runner(
    command: Sequence[str],
    *,
    cwd: Path | None,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        cwd=None if cwd is None else str(cwd),
        timeout=float(timeout),
        shell=False,
        check=False,
        capture_output=True,
    )


def _result_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _execute_step(
    *,
    name: str,
    command: Sequence[str],
    output_dir: Path,
    command_runner: CommandRunner,
    command_cwd: Path | None,
    timeout_secs: float,
    clock: Clock,
) -> dict[str, Any]:
    argv = _command_tuple(command, label=name)
    started_at = float(clock())
    try:
        result = command_runner(argv, cwd=command_cwd, timeout=float(timeout_secs))
        return_code = int(result.returncode)
        stdout = _result_bytes(result.stdout)
        stderr = _result_bytes(result.stderr)
    except Exception as exc:
        return_code = -1
        stdout = b""
        stderr = f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace")
    ended_at = float(clock())
    stdout_path = output_dir / "command_outputs" / f"{name}.stdout"
    stderr_path = output_dir / "command_outputs" / f"{name}.stderr"
    _write_exclusive(stdout_path, stdout)
    _write_exclusive(stderr_path, stderr)
    return {
        "command": list(argv),
        "started_at": started_at,
        "ended_at": ended_at,
        "return_code": return_code,
        "stdout": _artifact_ref(stdout_path),
        "stderr": _artifact_ref(stderr_path),
    }


def _step_stdout_payload(step: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    stdout_ref = dict(step.get("stdout") or {})
    stdout_path = Path(str(stdout_ref.get("path") or ""))
    if not stdout_path.is_file():
        raise RollbackDrillError(f"{label} stdout is missing")
    return _json_object(stdout_path.read_bytes(), label=f"{label} stdout")


def _snapshot_manifest(*, source: Path, destination: Path) -> dict[str, str]:
    if not source.is_file():
        raise RollbackDrillError(f"active manifest is missing: {source}")
    _write_exclusive(destination, source.read_bytes())
    return _artifact_ref(destination, snapshot=True)


def _capture_observation(
    *,
    phase: str,
    config: RollbackDrillConfig,
    output_dir: Path,
    command_runner: CommandRunner,
    clock: Clock,
) -> dict[str, Any]:
    phase_dir = output_dir / "observations" / phase
    manifest_ref = _snapshot_manifest(
        source=config.active_manifest_path,
        destination=phase_dir / "manifest.json",
    )
    manifest_identity = _identity_fields(
        active_manifest_identity(
            manifest_path=Path(manifest_ref["snapshot_path"]),
            pair=config.pair,
        )
    )
    db_step = _execute_step(
        name=f"observe_{phase}_db",
        command=config.db_observation_command,
        output_dir=output_dir,
        command_runner=command_runner,
        command_cwd=config.command_cwd,
        timeout_secs=config.timeout_secs,
        clock=clock,
    )
    runtime_step = _execute_step(
        name=f"observe_{phase}_runtime",
        command=config.runtime_observation_command,
        output_dir=output_dir,
        command_runner=command_runner,
        command_cwd=config.command_cwd,
        timeout_secs=config.timeout_secs,
        clock=clock,
    )
    if int(db_step["return_code"]) != 0:
        raise RollbackDrillError(f"{phase} DB observation command failed")
    if int(runtime_step["return_code"]) != 0:
        raise RollbackDrillError(f"{phase} runtime observation command failed")
    db_stdout = Path(str(dict(db_step["stdout"])["path"]))
    runtime_stdout = Path(str(dict(runtime_step["stdout"])["path"]))
    _json_object(db_stdout.read_bytes(), label=f"{phase} DB observation")
    runtime_payload = _json_object(runtime_stdout.read_bytes(), label=f"{phase} runtime observation")
    db_snapshot = phase_dir / "db.json"
    runtime_snapshot = phase_dir / "runtime.json"
    _write_exclusive(db_snapshot, db_stdout.read_bytes())
    _write_exclusive(runtime_snapshot, runtime_stdout.read_bytes())
    observed_at = float(clock())
    return {
        "observed_at": observed_at,
        "model_identity_sha256": manifest_identity["model_manifest_sha256"],
        "manifest_file_sha256": manifest_ref["sha256"],
        "runtime_boot_id": str(runtime_payload.get("runtime_boot_id") or "").strip(),
        "runtime_ready": runtime_payload.get("runtime_ready") is True,
        "runtime_commands_disabled": runtime_payload.get("runtime_commands_disabled") is True,
        "manifest": manifest_ref,
        "db": _artifact_ref(db_snapshot, snapshot=True),
        "runtime": _artifact_ref(runtime_snapshot, snapshot=True),
        "capture_steps": {
            "db": db_step,
            "runtime": runtime_step,
        },
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _artifact_errors(
    ref: Mapping[str, Any],
    *,
    output_dir: Path,
    label: str,
    snapshot: bool,
) -> list[str]:
    path_key = "snapshot_path" if snapshot else "path"
    raw_path = str(ref.get(path_key) or "").strip()
    expected_sha = str(ref.get("sha256") or "").strip().lower()
    errors: list[str] = []
    path = Path(raw_path) if raw_path else Path()
    if not raw_path or not path.is_absolute() or not _is_within(path, output_dir):
        errors.append(f"{label}_path_invalid")
        return errors
    if not path.is_file():
        errors.append(f"{label}_missing")
        return errors
    if not is_sha256(expected_sha) or file_sha256(path) != expected_sha:
        errors.append(f"{label}_hash_mismatch")
    return errors


def _step_errors(
    step: Mapping[str, Any],
    *,
    output_dir: Path,
    producer_started_at: float,
    producer_finished_at: float,
    label: str,
) -> list[str]:
    errors: list[str] = []
    command = list(step.get("command") or [])
    started_at = _safe_float(step.get("started_at"))
    ended_at = _safe_float(step.get("ended_at"))
    if not command or any(not str(item).strip() for item in command):
        errors.append(f"{label}_command_missing")
    if not (
        math.isfinite(started_at)
        and math.isfinite(ended_at)
        and producer_started_at <= started_at <= ended_at <= producer_finished_at
    ):
        errors.append(f"{label}_window_invalid")
    if int(_safe_float(step.get("return_code"), -1.0)) != 0:
        errors.append(f"{label}_return_code_failed")
    errors.extend(
        _artifact_errors(
            dict(step.get("stdout") or {}),
            output_dir=output_dir,
            label=f"{label}_stdout",
            snapshot=False,
        )
    )
    errors.extend(
        _artifact_errors(
            dict(step.get("stderr") or {}),
            output_dir=output_dir,
            label=f"{label}_stderr",
            snapshot=False,
        )
    )
    return errors


def _snapshot_payload(ref: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    path = Path(str(ref.get("snapshot_path") or ""))
    if not path.is_file():
        raise RollbackDrillError(f"{label} snapshot is missing")
    return _json_object(path.read_bytes(), label=label)


def _observation_identity(
    observation: Mapping[str, Any],
    *,
    pair: str,
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    manifest_ref = dict(observation.get("manifest") or {})
    db_ref = dict(observation.get("db") or {})
    runtime_ref = dict(observation.get("runtime") or {})
    manifest_identity = _identity_fields(
        active_manifest_identity(
            manifest_path=Path(str(manifest_ref.get("snapshot_path") or "")),
            pair=pair,
        )
    )
    return (
        manifest_identity,
        _snapshot_payload(db_ref, label="DB observation"),
        _snapshot_payload(runtime_ref, label="runtime observation"),
    )


def validate_rollback_evidence_payload(
    payload: Mapping[str, Any],
    *,
    output_dir: str | Path,
) -> list[str]:
    """Reopen and recompute producer evidence without trusting claimed booleans."""

    root = Path(output_dir).resolve()
    errors: list[str] = []
    producer = dict(payload.get("producer") or {})
    producer_started_at = _safe_float(producer.get("started_at"))
    producer_finished_at = _safe_float(producer.get("finished_at"))
    if str(payload.get("schema_version") or "") != ROLLBACK_EVIDENCE_SCHEMA:
        errors.append("rollback_evidence_schema_invalid")
    if str(payload.get("status") or "").strip().lower() != "passed":
        errors.append("rollback_status_invalid")
    if str(producer.get("tool") or "") != PRODUCER_TOOL or str(producer.get("version") or "") != PRODUCER_VERSION:
        errors.append("rollback_producer_invalid")
    if not str(producer.get("invocation_id") or "").strip():
        errors.append("rollback_invocation_id_missing")
    if not (
        math.isfinite(producer_started_at)
        and math.isfinite(producer_finished_at)
        and producer_started_at < producer_finished_at
    ):
        errors.append("rollback_producer_window_invalid")

    release_context = dict(payload.get("release_context") or {})
    if set(release_context) != {
        "release_run_id",
        "code_sha256",
        "git_commit",
        "config_sha256",
        "runtime_generation",
        "runtime_boot_id",
        "model_identity_sha256",
        "manifest_file_sha256",
    }:
        errors.append("rollback_release_context_fields_invalid")
    errors.extend(_release_context_errors(release_context))

    candidate_scope = dict(payload.get("candidate_scope") or {})
    if set(candidate_scope) != {
        "controller_path",
        "controller_sha256",
        "candidate_pid",
        "candidate_port",
        "candidate_boot_id",
        "candidate_instance_id",
        "candidate_database_identity_sha256",
        "baseline_database_identity_sha256",
        "candidate_snapshot_scope",
    }:
        errors.append("rollback_candidate_scope_fields_invalid")
    errors.extend(_candidate_scope_errors(candidate_scope))
    if str(candidate_scope.get("candidate_boot_id") or "").strip() != str(
        release_context.get("runtime_boot_id") or ""
    ).strip():
        errors.append("rollback_candidate_scope_boot_mismatch")

    candidate_identity = _identity_from_mapping(dict(payload.get("evidence_identity") or {}))
    target_identity = _identity_from_mapping(dict(payload.get("rollback_target_identity") or {}))
    errors.extend(_identity_errors(candidate_identity, label="candidate"))
    errors.extend(_identity_errors(target_identity, label="target"))
    if str(dict(payload.get("evidence_identity") or {}).get("evidence_kind") or "") != "rollback_validation":
        errors.append("rollback_identity_kind_invalid")
    if str(dict(payload.get("evidence_identity") or {}).get("source_kind") or "") != "production_rollback_drill":
        errors.append("rollback_identity_source_invalid")
    if dict(payload.get("evidence_identity") or {}).get("advisory_only") is not False:
        errors.append("rollback_identity_advisory")
    if target_identity["bundle_run_id"] == candidate_identity["bundle_run_id"]:
        errors.append("rollback_target_not_distinct")
    if str(payload.get("rollback_target_bundle_run_id") or "") != target_identity["bundle_run_id"]:
        errors.append("rollback_target_bundle_mismatch")

    steps = dict(payload.get("steps") or {})
    if set(steps) != set(MUTATION_STEPS):
        errors.append("rollback_mutation_steps_invalid")
    for name in MUTATION_STEPS:
        step = dict(steps.get(name) or {})
        errors.extend(
            _step_errors(
                step,
                output_dir=root,
                producer_started_at=producer_started_at,
                producer_finished_at=producer_finished_at,
                label=name,
            )
        )
        errors.extend(
            _scoped_command_errors(
                list(step.get("command") or []),
                scope=candidate_scope,
                label=name,
                require_controller=True,
                required_operation=(
                    "activate-target" if name == "target_activation" else "restore-candidate"
                ),
            )
        )

    observations = dict(payload.get("observations") or {})
    if set(observations) != set(PHASES):
        errors.append("rollback_observation_phases_invalid")
    phase_identities: dict[str, dict[str, str]] = {}
    phase_runtime_payloads: dict[str, dict[str, Any]] = {}
    for phase in PHASES:
        observation = dict(observations.get(phase) or {})
        observed_at = _safe_float(observation.get("observed_at"))
        if not (
            math.isfinite(observed_at)
            and producer_started_at <= observed_at <= producer_finished_at
        ):
            errors.append(f"rollback_{phase}_observed_at_invalid")
        for kind in ("manifest", "db", "runtime"):
            errors.extend(
                _artifact_errors(
                    dict(observation.get(kind) or {}),
                    output_dir=root,
                    label=f"rollback_{phase}_{kind}",
                    snapshot=True,
                )
            )
        capture_steps = dict(observation.get("capture_steps") or {})
        if set(capture_steps) != {"db", "runtime"}:
            errors.append(f"rollback_{phase}_capture_steps_invalid")
        for kind in ("db", "runtime"):
            capture_step = dict(capture_steps.get(kind) or {})
            errors.extend(
                _step_errors(
                    capture_step,
                    output_dir=root,
                    producer_started_at=producer_started_at,
                    producer_finished_at=producer_finished_at,
                    label=f"observe_{phase}_{kind}",
                )
            )
            errors.extend(
                _scoped_command_errors(
                    list(capture_step.get("command") or []),
                    scope=candidate_scope,
                    label=f"observe_{phase}_{kind}",
                    require_controller=False,
                )
            )
            snapshot_ref = dict(observation.get(kind) or {})
            raw_stdout_ref = dict(dict(capture_steps.get(kind) or {}).get("stdout") or {})
            if snapshot_ref.get("sha256") != raw_stdout_ref.get("sha256"):
                errors.append(f"rollback_{phase}_{kind}_raw_copy_mismatch")
        if any(item.startswith(f"rollback_{phase}_") or item.startswith(f"observe_{phase}_") for item in errors):
            continue
        try:
            manifest_identity, db_payload, runtime_payload = _observation_identity(
                observation,
                pair=candidate_identity["pair"],
            )
        except Exception:
            errors.append(f"rollback_{phase}_snapshot_invalid")
            continue
        phase_identities[phase] = manifest_identity
        phase_runtime_payloads[phase] = runtime_payload
        if str(observation.get("model_identity_sha256") or "").strip().lower() != str(
            manifest_identity["model_manifest_sha256"]
        ).lower():
            errors.append(f"rollback_{phase}_model_identity_mismatch")
        if str(observation.get("manifest_file_sha256") or "").strip().lower() != str(
            dict(observation.get("manifest") or {}).get("sha256") or ""
        ).strip().lower():
            errors.append(f"rollback_{phase}_manifest_file_hash_mismatch")
        if _identity_from_mapping(db_payload) != manifest_identity:
            errors.append(f"rollback_{phase}_db_identity_mismatch")
        if _identity_from_mapping(runtime_payload) != manifest_identity:
            errors.append(f"rollback_{phase}_runtime_identity_mismatch")
        errors.extend(
            _observation_scope_errors(
                db_payload,
                scope=candidate_scope,
                label=f"rollback_{phase}_db_scope",
            )
        )
        errors.extend(
            _observation_scope_errors(
                runtime_payload,
                scope=candidate_scope,
                label=f"rollback_{phase}_runtime_scope",
            )
        )
        if observation.get("runtime_commands_disabled") is not True or runtime_payload.get("runtime_commands_disabled") is not True:
            errors.append(f"rollback_{phase}_runtime_enabled")
        if observation.get("runtime_ready") is not True or runtime_payload.get("runtime_ready") is not True:
            errors.append(f"rollback_{phase}_runtime_not_ready")
        runtime_boot_id = str(runtime_payload.get("runtime_boot_id") or "").strip()
        if not runtime_boot_id or str(observation.get("runtime_boot_id") or "").strip() != runtime_boot_id:
            errors.append(f"rollback_{phase}_runtime_boot_id_invalid")
        if runtime_boot_id != str(release_context.get("runtime_boot_id") or "").strip():
            errors.append(f"rollback_{phase}_release_boot_mismatch")
        runtime_generation = str(runtime_payload.get("runtime_generation") or "").strip()
        if runtime_generation != str(release_context.get("runtime_generation") or "").strip():
            errors.append(f"rollback_{phase}_release_generation_mismatch")
        reload_generation = str(runtime_payload.get("reload_generation") or "").strip()
        ack_generation = str(runtime_payload.get("ack_generation") or "").strip()
        if not reload_generation or not ack_generation or reload_generation != ack_generation:
            errors.append(f"rollback_{phase}_reload_ack_missing")

    if phase_identities.get("before") != candidate_identity:
        errors.append("rollback_before_candidate_identity_mismatch")
    if phase_identities.get("target") != target_identity:
        errors.append("rollback_target_identity_mismatch")
    if phase_identities.get("restored") != candidate_identity:
        errors.append("rollback_restored_candidate_identity_mismatch")
    if str(release_context.get("model_identity_sha256") or "").strip().lower() != str(
        candidate_identity.get("model_manifest_sha256") or ""
    ).strip().lower():
        errors.append("rollback_release_model_identity_mismatch")

    before_ref = dict(dict(observations.get("before") or {}).get("manifest") or {})
    target_ref = dict(dict(observations.get("target") or {}).get("manifest") or {})
    restored_ref = dict(dict(observations.get("restored") or {}).get("manifest") or {})
    if str(release_context.get("manifest_file_sha256") or "").strip().lower() != str(
        before_ref.get("sha256") or ""
    ).strip().lower():
        errors.append("rollback_release_manifest_file_hash_mismatch")
    if before_ref.get("sha256") != restored_ref.get("sha256"):
        errors.append("rollback_restored_manifest_bytes_mismatch")
    source_artifacts = dict(payload.get("source_artifacts") or {})
    target_reference = dict(source_artifacts.get("rollback_target_manifest") or {})
    target_reference_path = Path(str(target_reference.get("path") or ""))
    target_reference_sha = str(target_reference.get("sha256") or "").strip().lower()
    if not target_reference_path.is_file() or not is_sha256(target_reference_sha) or file_sha256(target_reference_path) != target_reference_sha:
        errors.append("rollback_target_reference_invalid")
    elif target_ref.get("sha256") != target_reference_sha:
        errors.append("rollback_target_manifest_bytes_mismatch")
    active_manifest_path = Path(str(source_artifacts.get("active_manifest_path") or ""))
    if not active_manifest_path.is_file() or file_sha256(active_manifest_path) != str(restored_ref.get("sha256") or ""):
        errors.append("rollback_active_manifest_not_restored")

    for step_name, phase in (("target_activation", "target"), ("candidate_restore", "restored")):
        step = dict(steps.get(step_name) or {})
        try:
            command_payload = _step_stdout_payload(step, label=step_name)
        except RollbackDrillError:
            errors.append(f"rollback_{step_name}_generation_missing")
            continue
        command_generation = str(command_payload.get("reload_generation") or "").strip()
        runtime_payload = phase_runtime_payloads.get(phase, {})
        if not command_generation or command_generation != str(runtime_payload.get("reload_generation") or "").strip():
            errors.append(f"rollback_{step_name}_generation_mismatch")
        if command_generation != str(runtime_payload.get("ack_generation") or "").strip():
            errors.append(f"rollback_{step_name}_ack_mismatch")

    before_at = _safe_float(dict(observations.get("before") or {}).get("observed_at"))
    target_at = _safe_float(dict(observations.get("target") or {}).get("observed_at"))
    restored_at = _safe_float(dict(observations.get("restored") or {}).get("observed_at"))
    target_step = dict(steps.get("target_activation") or {})
    restore_step = dict(steps.get("candidate_restore") or {})
    if not (
        before_at <= _safe_float(target_step.get("started_at"))
        <= _safe_float(target_step.get("ended_at"))
        <= target_at
        <= _safe_float(restore_step.get("started_at"))
        <= _safe_float(restore_step.get("ended_at"))
        <= restored_at
    ):
        errors.append("rollback_sequence_invalid")
    return list(dict.fromkeys(errors))


def validate_rollback_evidence_artifact(path: str | Path) -> list[str]:
    evidence_path = Path(path).resolve()
    if not evidence_path.is_file():
        return ["rollback_evidence_missing"]
    try:
        payload = _json_object(evidence_path.read_bytes(), label="rollback evidence")
    except RollbackDrillError:
        return ["rollback_evidence_invalid_json"]
    return validate_rollback_evidence_payload(payload, output_dir=evidence_path.parent)


def _validate_config(config: RollbackDrillConfig) -> tuple[dict[str, str], dict[str, str]]:
    pair = str(config.pair or "").strip().upper()
    if not pair:
        raise RollbackDrillError("pair is required")
    active_manifest = config.active_manifest_path.resolve()
    target_manifest = config.rollback_target_manifest_path.resolve()
    output_dir = config.output_dir.resolve()
    if not active_manifest.is_file():
        raise RollbackDrillError(f"active manifest is missing: {active_manifest}")
    if not target_manifest.is_file():
        raise RollbackDrillError(f"rollback target manifest is missing: {target_manifest}")
    if active_manifest == target_manifest:
        raise RollbackDrillError("active manifest and rollback target manifest must be distinct paths")
    if output_dir.exists():
        raise RollbackDrillError(f"output path already exists: {output_dir}")
    if config.command_cwd is not None and not config.command_cwd.resolve().is_dir():
        raise RollbackDrillError(f"command cwd is missing: {config.command_cwd}")
    if not math.isfinite(float(config.timeout_secs)) or float(config.timeout_secs) <= 0.0:
        raise RollbackDrillError("timeout must be finite and positive")
    for label, command in (
        ("target activation command", config.target_activation_command),
        ("candidate restore command", config.candidate_restore_command),
        ("DB observation command", config.db_observation_command),
        ("runtime observation command", config.runtime_observation_command),
    ):
        _command_tuple(command, label=label)
    candidate_scope = _candidate_scope(config)
    scope_errors = _candidate_scope_errors(candidate_scope)
    for label, command, require_controller, operation in (
        (
            "target_activation",
            config.target_activation_command,
            True,
            "activate-target",
        ),
        (
            "candidate_restore",
            config.candidate_restore_command,
            True,
            "restore-candidate",
        ),
        ("db_observation", config.db_observation_command, False, ""),
        ("runtime_observation", config.runtime_observation_command, False, ""),
    ):
        scope_errors.extend(
            _scoped_command_errors(
                command,
                scope=candidate_scope,
                label=label,
                require_controller=require_controller,
                required_operation=operation,
            )
        )
    if scope_errors:
        raise RollbackDrillError(
            "unsafe or unscoped candidate command:" + ",".join(scope_errors)
        )
    candidate_identity = _identity_fields(active_manifest_identity(manifest_path=active_manifest, pair=pair))
    target_identity = _identity_fields(active_manifest_identity(manifest_path=target_manifest, pair=pair))
    candidate_errors = _identity_errors(candidate_identity, label="candidate")
    target_errors = _identity_errors(target_identity, label="target")
    if candidate_errors or target_errors:
        raise RollbackDrillError("invalid manifest identity:" + ",".join(candidate_errors + target_errors))
    if candidate_identity["bundle_run_id"] == target_identity["bundle_run_id"]:
        raise RollbackDrillError("rollback target must differ from the active candidate")
    release_context_errors = _release_context_errors(
        _bound_release_context(
            config,
            candidate_identity=candidate_identity,
            manifest_file_sha256=file_sha256(active_manifest),
        )
    )
    if release_context_errors:
        raise RollbackDrillError(
            "invalid release context:" + ",".join(release_context_errors)
        )
    return candidate_identity, target_identity


def build_plan(config: RollbackDrillConfig) -> dict[str, Any]:
    candidate_identity, target_identity = _validate_config(config)
    return {
        "mode": "plan_only",
        "execute_required": True,
        "pair": candidate_identity["pair"],
        "release_context": _bound_release_context(
            config,
            candidate_identity=candidate_identity,
            manifest_file_sha256=file_sha256(config.active_manifest_path.resolve()),
        ),
        "candidate_scope": _candidate_scope(config),
        "candidate_identity": candidate_identity,
        "rollback_target_identity": target_identity,
        "active_manifest_path": str(config.active_manifest_path.resolve()),
        "rollback_target_manifest_path": str(config.rollback_target_manifest_path.resolve()),
        "output_dir": str(config.output_dir.resolve()),
        "commands": {
            "target_activation": list(config.target_activation_command),
            "candidate_restore": list(config.candidate_restore_command),
            "db_observation": list(config.db_observation_command),
            "runtime_observation": list(config.runtime_observation_command),
        },
    }


def _write_failure_diagnostic(
    *,
    config: RollbackDrillConfig,
    output_dir: Path,
    invocation_id: str,
    producer_started_at: float,
    producer_finished_at: float,
    candidate_identity: Mapping[str, Any],
    target_identity: Mapping[str, Any],
    candidate_reference_sha: str,
    target_reference_sha: str,
    steps: Mapping[str, Any],
    observations: Mapping[str, Any],
    errors: Sequence[str],
) -> Path:
    """Write recovery diagnostics that cannot be mistaken for release authority."""

    active_path = config.active_manifest_path.resolve()
    active_sha = file_sha256(active_path) if active_path.is_file() else ""
    payload = {
        "schema_version": DIAGNOSTIC_SCHEMA,
        "status": "failed",
        "authoritative": False,
        "advisory_only": True,
        "producer": {
            "tool": PRODUCER_TOOL,
            "version": PRODUCER_VERSION,
            "invocation_id": invocation_id,
            "started_at": producer_started_at,
            "finished_at": producer_finished_at,
        },
        "expected_release_context": _bound_release_context(
            config,
            candidate_identity=candidate_identity,
            manifest_file_sha256=candidate_reference_sha,
        ),
        "candidate_scope": _candidate_scope(config),
        "candidate_identity": dict(candidate_identity),
        "rollback_target_identity": dict(target_identity),
        "errors": [str(item) for item in errors if str(item).strip()],
        "steps": dict(steps),
        "observations": dict(observations),
        "recovery_state": {
            "active_manifest_path": str(active_path),
            "active_manifest_sha256": active_sha,
        },
        "source_artifacts": {
            "rollback_target_manifest": {
                "path": str(config.rollback_target_manifest_path.resolve()),
                "sha256": target_reference_sha,
            }
        },
    }
    path = output_dir / "rollback_diagnostic.json"
    _write_exclusive(path, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))
    return path


def run_release_rollback_drill(
    config: RollbackDrillConfig,
    *,
    command_runner: CommandRunner = _default_command_runner,
    clock: Clock = time.time,
) -> dict[str, Any]:
    candidate_identity, target_identity = _validate_config(config)
    output_dir = config.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    producer_started_at = float(clock())
    invocation_id = str(uuid4())
    candidate_before_bytes = config.active_manifest_path.resolve().read_bytes()
    candidate_reference_sha = file_sha256(config.active_manifest_path.resolve())
    target_reference_sha = file_sha256(config.rollback_target_manifest_path.resolve())
    steps: dict[str, Any] = {}
    observations: dict[str, Any] = {}

    def fail(*messages: str, cause: Exception | None = None) -> NoReturn:
        clean_messages = [str(item) for item in messages if str(item).strip()]
        finished_at = float(clock())
        try:
            _write_failure_diagnostic(
                config=config,
                output_dir=output_dir,
                invocation_id=invocation_id,
                producer_started_at=producer_started_at,
                producer_finished_at=finished_at,
                candidate_identity=candidate_identity,
                target_identity=target_identity,
                candidate_reference_sha=candidate_reference_sha,
                target_reference_sha=target_reference_sha,
                steps=steps,
                observations=observations,
                errors=clean_messages,
            )
        except Exception as diagnostic_exc:
            clean_messages.append(f"diagnostic_write_failed:{diagnostic_exc}")
        error = RollbackDrillError("; ".join(clean_messages))
        if cause is None:
            raise error
        raise error from cause

    try:
        before = _capture_observation(
            phase="before",
            config=config,
            output_dir=output_dir,
            command_runner=command_runner,
            clock=clock,
        )
        observations["before"] = before
        manifest_identity, db_payload, runtime_payload = _observation_identity(
            before,
            pair=candidate_identity["pair"],
        )
    except Exception as exc:
        fail(f"before observation is invalid: {exc}", cause=exc)
    if (
        manifest_identity != candidate_identity
        or _identity_from_mapping(db_payload) != candidate_identity
        or _identity_from_mapping(runtime_payload) != candidate_identity
    ):
        fail("before observation identity mismatch")
    before_scope_errors = _observation_scope_errors(
        db_payload,
        scope=_candidate_scope(config),
        label="before_db_scope",
    )
    before_scope_errors.extend(
        _observation_scope_errors(
            runtime_payload,
            scope=_candidate_scope(config),
            label="before_runtime_scope",
        )
    )
    if before_scope_errors:
        fail("before observation is outside candidate DB scope:" + ",".join(before_scope_errors))
    if (
        runtime_payload.get("runtime_commands_disabled") is not True
        or before.get("runtime_commands_disabled") is not True
    ):
        fail("runtime commands must be disabled before rollback mutation")
    if runtime_payload.get("runtime_ready") is not True:
        fail("before runtime observation is not ready")
    observed_runtime_boot_id = str(runtime_payload.get("runtime_boot_id") or "").strip()
    observed_runtime_generation = str(runtime_payload.get("runtime_generation") or "").strip()
    expected_context = _bound_release_context(
        config,
        candidate_identity=candidate_identity,
        manifest_file_sha256=candidate_reference_sha,
    )
    if observed_runtime_boot_id != expected_context["runtime_boot_id"]:
        fail("before runtime boot identity does not match release context")
    if observed_runtime_generation != expected_context["runtime_generation"]:
        fail("before runtime generation does not match release context")
    before_generation = str(runtime_payload.get("reload_generation") or "").strip()
    before_ack = str(runtime_payload.get("ack_generation") or "").strip()
    if not before_generation or before_generation != before_ack:
        fail("before runtime reload acknowledgement is missing")

    target: dict[str, Any] | None = None
    target_error: Exception | None = None
    target_step: dict[str, Any] | None = None
    restore_step: dict[str, Any] | None = None
    try:
        target_step = _execute_step(
            name="target_activation",
            command=config.target_activation_command,
            output_dir=output_dir,
            command_runner=command_runner,
            command_cwd=config.command_cwd,
            timeout_secs=config.timeout_secs,
            clock=clock,
        )
        steps["target_activation"] = target_step
        if int(target_step["return_code"]) != 0:
            raise RollbackDrillError("target activation command failed")
        target = _capture_observation(
            phase="target",
            config=config,
            output_dir=output_dir,
            command_runner=command_runner,
            clock=clock,
        )
        observations["target"] = target
    except Exception as exc:
        target_error = exc
    finally:
        try:
            restore_step = _execute_step(
                name="candidate_restore",
                command=config.candidate_restore_command,
                output_dir=output_dir,
                command_runner=command_runner,
                command_cwd=config.command_cwd,
                timeout_secs=config.timeout_secs,
                clock=clock,
            )
            steps["candidate_restore"] = restore_step
        except Exception as exc:
            restore_step = None
            steps["candidate_restore_error"] = {
                "error": f"{type(exc).__name__}:{exc}"
            }

    restore_errors: list[str] = []
    if restore_step is None:
        restore_errors.append("candidate restore command could not be recorded")
    elif int(restore_step["return_code"]) != 0:
        restore_errors.append("candidate restore command failed")
    try:
        restored = _capture_observation(
            phase="restored",
            config=config,
            output_dir=output_dir,
            command_runner=command_runner,
            clock=clock,
        )
        observations["restored"] = restored
    except Exception as exc:
        restored = None
        restore_errors.append(f"restored observation failed: {exc}")
    if config.active_manifest_path.resolve().read_bytes() != candidate_before_bytes:
        restore_errors.append("candidate manifest bytes were not restored exactly")

    failure_messages: list[str] = []
    if target_error is not None:
        failure_messages.append(f"target activation evidence failed after restore: {target_error}")
    failure_messages.extend(restore_errors)
    if failure_messages:
        fail(*failure_messages, cause=target_error)
    assert target_step is not None and restore_step is not None
    assert target is not None and restored is not None

    producer_finished_at = float(clock())
    identity = ReleaseEvidenceIdentity(
        pair=candidate_identity["pair"],
        bundle_run_id=candidate_identity["bundle_run_id"],
        model_set_id=candidate_identity["model_set_id"],
        model_manifest_sha256=candidate_identity["model_manifest_sha256"],
        artifact_set_sha256=candidate_identity["artifact_set_sha256"],
        evidence_kind="rollback_validation",
        source_kind="production_rollback_drill",
        advisory_only=False,
    )
    payload: dict[str, Any] = {
        "schema_version": ROLLBACK_EVIDENCE_SCHEMA,
        "status": "passed",
        "tested_at": producer_finished_at,
        "release_context": {
            **expected_context,
            "runtime_generation": observed_runtime_generation,
            "runtime_boot_id": observed_runtime_boot_id,
        },
        "candidate_scope": _candidate_scope(config),
        "evidence_identity": identity.to_dict(),
        "rollback_target_bundle_run_id": target_identity["bundle_run_id"],
        "rollback_target_identity": target_identity,
        "producer": {
            "tool": PRODUCER_TOOL,
            "version": PRODUCER_VERSION,
            "invocation_id": invocation_id,
            "started_at": producer_started_at,
            "finished_at": producer_finished_at,
        },
        "steps": {
            "target_activation": target_step,
            "candidate_restore": restore_step,
        },
        "observations": {
            "before": before,
            "target": target,
            "restored": restored,
        },
        "source_artifacts": {
            "active_manifest_path": str(config.active_manifest_path.resolve()),
            "rollback_target_manifest": {
                "path": str(config.rollback_target_manifest_path.resolve()),
                "sha256": target_reference_sha,
            },
        },
    }
    errors = validate_rollback_evidence_payload(payload, output_dir=output_dir)
    if errors:
        fail("rollback evidence failed producer validation:" + ",".join(errors))
    payload["drill"] = {
        "executed": True,
        "return_code": 0,
        "command": list(config.target_activation_command),
        "runtime_disabled_during_drill": True,
        "target_activated": True,
        "candidate_restored": True,
        "candidate_bundle_run_id": candidate_identity["bundle_run_id"],
    }
    evidence_path = output_dir / "rollback_evidence.json"
    _write_exclusive(
        evidence_path,
        json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
    )
    return payload


def _parse_command_json(raw: str, *, label: str) -> tuple[str, ...]:
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be a JSON argv array") from exc
    if not isinstance(value, list):
        raise argparse.ArgumentTypeError(f"{label} must be a JSON argv array")
    try:
        return _command_tuple([str(item) for item in value], label=label)
    except RollbackDrillError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute and bind a reversible release activation drill. Defaults to plan-only; --execute is required to mutate.",
    )
    parser.add_argument("--pair", required=True)
    parser.add_argument("--release-run-id", required=True)
    parser.add_argument("--code-sha256", required=True)
    parser.add_argument("--git-commit", default="")
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--runtime-generation", required=True)
    parser.add_argument("--runtime-boot-id", required=True)
    parser.add_argument("--candidate-controller", required=True)
    parser.add_argument("--candidate-controller-sha256", required=True)
    parser.add_argument("--candidate-pid", type=int, required=True)
    parser.add_argument("--candidate-port", type=int, required=True)
    parser.add_argument("--candidate-instance-id", required=True)
    parser.add_argument("--candidate-database-identity-sha256", required=True)
    parser.add_argument("--baseline-database-identity-sha256", required=True)
    parser.add_argument("--candidate-snapshot-scope", required=True)
    parser.add_argument("--active-manifest", required=True)
    parser.add_argument("--rollback-target-manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target-activation-command-json", required=True)
    parser.add_argument("--candidate-restore-command-json", required=True)
    parser.add_argument("--db-observation-command-json", required=True)
    parser.add_argument("--runtime-observation-command-json", required=True)
    parser.add_argument("--command-cwd", default="")
    parser.add_argument("--timeout-secs", type=float, default=300.0)
    parser.add_argument("--execute", action="store_true")
    return parser


def _config_from_args(args: argparse.Namespace) -> RollbackDrillConfig:
    return RollbackDrillConfig(
        pair=str(args.pair).upper(),
        release_run_id=str(args.release_run_id),
        code_sha256=str(args.code_sha256),
        git_commit=str(args.git_commit),
        config_sha256=str(args.config_sha256),
        runtime_generation=str(args.runtime_generation),
        runtime_boot_id=str(args.runtime_boot_id),
        candidate_controller_path=Path(args.candidate_controller),
        candidate_controller_sha256=str(args.candidate_controller_sha256),
        candidate_pid=int(args.candidate_pid),
        candidate_port=int(args.candidate_port),
        candidate_instance_id=str(args.candidate_instance_id),
        candidate_database_identity_sha256=str(
            args.candidate_database_identity_sha256
        ),
        baseline_database_identity_sha256=str(
            args.baseline_database_identity_sha256
        ),
        candidate_snapshot_scope=str(args.candidate_snapshot_scope),
        active_manifest_path=Path(args.active_manifest),
        rollback_target_manifest_path=Path(args.rollback_target_manifest),
        output_dir=Path(args.output_dir),
        target_activation_command=_parse_command_json(
            args.target_activation_command_json,
            label="target activation command",
        ),
        candidate_restore_command=_parse_command_json(
            args.candidate_restore_command_json,
            label="candidate restore command",
        ),
        db_observation_command=_parse_command_json(
            args.db_observation_command_json,
            label="DB observation command",
        ),
        runtime_observation_command=_parse_command_json(
            args.runtime_observation_command_json,
            label="runtime observation command",
        ),
        command_cwd=Path(args.command_cwd) if str(args.command_cwd).strip() else None,
        timeout_secs=float(args.timeout_secs),
    )


def main() -> int:
    args = build_parser().parse_args()
    config = _config_from_args(args)
    try:
        if not bool(args.execute):
            print(json.dumps(build_plan(config), indent=2, sort_keys=True))
            return 0
        payload = run_release_rollback_drill(config)
    except RollbackDrillError as exc:
        print(f"rollback drill failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": payload["status"], "output": str((config.output_dir / "rollback_evidence.json").resolve())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
