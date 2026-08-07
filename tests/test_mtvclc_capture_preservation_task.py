from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterator

import pytest


ROOT = Path(__file__).resolve().parents[1]
PRESERVE = ROOT / "ops" / "windows" / "29_preserve_mtvclc_capture.ps1"
REGISTER = (
    ROOT / "ops" / "windows" / "29_register_mtvclc_capture_preservation_task.ps1"
)
PREREG_ID = "07b78ce6d697a61db308560c325f547e90984143f2b71c1613bb8ee12b9f879c"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_powershell(path: Path) -> None:
    command = (
        "$errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}',"
        "[ref]$null,[ref]$errors); "
        "if($errors.Count -gt 0){$errors | ForEach-Object { Write-Error $_ }; exit 1}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def _make_tuple(
    tmp_path: Path,
    *,
    prereg_parent: str = "mtvclc_prereg_sealed_resilient_v1",
    tuple_family: str = "legacy",
) -> dict[str, Path | str]:
    prereg_dir = tmp_path / prereg_parent
    prereg_dir.mkdir()
    if tuple_family == "legacy":
        prereg_prefix = "mtvclc_v1_preregistration_"
        capture_prefix = "mtvclc_prospective_capture_"
        guard_name = "collector-guard.identity.resilient.v1.json"
    elif tuple_family == "gap_v3":
        prereg_prefix = "mtvclc_gap_v3_preregistration_"
        capture_prefix = "mtvclc_prospective_capture_gap_v3_"
        guard_name = "collector-guard.identity.gap-v3.v1.json"
    elif tuple_family == "gap_v5":
        prereg_prefix = "mtvclc_gap_v3_preregistration_"
        capture_prefix = "mtvclc_prospective_capture_gap_v3_"
        guard_name = "collector-guard.identity.gap-v5.v1.json"
    else:
        raise AssertionError(f"unsupported test tuple family: {tuple_family}")
    prereg = prereg_dir / f"{prereg_prefix}{PREREG_ID}.json"
    prereg.write_text('{"sealed":"opaque-task-test"}\n', encoding="utf-8")
    capture = tmp_path / f"{capture_prefix}{PREREG_ID[:16]}"
    capture.mkdir()
    guard = capture / guard_name
    guard.write_text('{"identity":"opaque-task-test"}\n', encoding="utf-8")
    return {
        "prereg": prereg,
        "capture": capture,
        "guard": guard,
        "prereg_hash": _sha256(prereg),
        "guard_hash": _sha256(guard),
    }


@contextlib.contextmanager
def _backup_root_on_repo_drive() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="mtvclc-task-test-", dir=ROOT.parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _task_arguments(
    source: dict[str, Path | str],
    backup_root: Path,
    *,
    action: str = "Preview",
    trigger_mode: str = "AtLogOn",
    prereg_hash: str | None = None,
) -> list[str]:
    capture = Path(source["capture"])
    return [
        "-Action",
        action,
        "-TriggerMode",
        trigger_mode,
        "-Preregistration",
        str(source["prereg"]),
        "-CaptureRoot",
        str(capture),
        "-BackupRoot",
        str(backup_root),
        "-ExpectedPreregistrationSha256",
        prereg_hash or str(source["prereg_hash"]),
        "-ExpectedGuardIdentitySha256",
        str(source["guard_hash"]),
        "-ExpectedCaptureDriveLetter",
        capture.drive.rstrip(":"),
    ]


def _preview(
    source: dict[str, Path | str],
    backup_root: Path,
    *,
    trigger_mode: str = "AtLogOn",
    prereg_hash: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REGISTER),
            *_task_arguments(
                source,
                backup_root,
                trigger_mode=trigger_mode,
                prereg_hash=prereg_hash,
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    report = json.loads(lines[0]) if len(lines) == 1 else None
    return completed, report


def test_registrar_is_exact_reversible_and_has_no_evidence_or_key_surface() -> None:
    _parse_powershell(REGISTER)
    source = REGISTER.read_text(encoding="utf-8")
    lowered = source.lower()

    assert '[ValidateSet("Install", "Preview", "Remove")]' in source
    assert '[ValidateSet("AtLogOn", "AtStartup")]' in source
    assert '[string]$TriggerMode = "AtLogOn"' in source
    assert '[int]$PreservationIntervalMinutes = 60' in source
    assert "29_preserve_mtvclc_capture.ps1" in source
    assert '"-ExpectedPreservationScriptSha256"' in source
    assert '"-ExpectedPreregistrationSha256"' in source
    assert '"-ExpectedGuardIdentitySha256"' in source
    assert '"-Preregistration"' in source
    assert '"-CaptureRoot"' in source
    assert '"-BackupRoot"' in source
    assert "New-ScheduledTaskTrigger -AtLogOn -User $currentUserName" in source
    assert "New-ScheduledTaskTrigger -AtStartup" in source
    assert "-RepetitionInterval" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-StartWhenAvailable" in source
    assert "Test-OwnedScheduledTask" in source
    assert "scheduled_task_identity_mismatch_refusing_overwrite" in source
    assert "scheduled_task_identity_mismatch_refusing_remove" in source
    assert "Unregister-ScheduledTask" in source
    assert "Register-ScheduledTask" in source

    assert "get-content" not in lowered
    assert "readalltext" not in lowered
    assert "convertfrom-json" not in lowered
    assert "import-csv" not in lowered
    assert "start-scheduledtask" not in lowered
    assert "start-process" not in lowered
    assert "api-key" not in lowered
    assert "get-credential" not in lowered
    assert "$apikey" not in lowered
    register_call = source[source.index("Register-ScheduledTask `") :]
    assert "-Force" not in register_call


def test_preview_pins_exact_tuple_and_defaults_to_hourly_logon(tmp_path: Path) -> None:
    source = _make_tuple(tmp_path)
    with _backup_root_on_repo_drive() as backup_root:
        completed, preview = _preview(source, backup_root)
        assert completed.returncode == 0, completed.stderr
        assert preview is not None
        assert preview["trigger_mode"] == "AtLogOn"
        assert preview["logon_trigger"] is True
        assert preview["startup_trigger"] is False
        assert preview["repetition_interval_minutes"] == 60
        assert preview["multiple_instances"] == "IgnoreNew"
        assert preview["start_when_available"] is True
        assert preview["principal_logon_type"] == "Interactive"
        assert preview["principal_run_level"] == "Limited"
        assert preview["preservation_script_sha256"] == _sha256(PRESERVE)
        assert preview["preregistration_sha256"] == source["prereg_hash"]
        assert preview["guard_identity_sha256"] == source["guard_hash"]
        assert preview["preregistration_path"] == str(Path(source["prereg"]).resolve())
        assert preview["capture_root"] == str(Path(source["capture"]).resolve())
        assert preview["backup_root"] == str(backup_root.resolve())
        assert "-WindowStyle Hidden" in preview["arguments"]
        assert "-ExpectedPreservationScriptSha256" in preview["arguments"]
        assert str(Path(source["prereg"]).resolve()) in preview["arguments"]
        assert str(Path(source["capture"]).resolve()) in preview["arguments"]
        assert str(backup_root.resolve()) in preview["arguments"]
        assert preview["key_value_read"] is False
        assert preview["key_value_stored"] is False
        assert preview["evidence_rows_interpreted"] is False
        assert preview["evaluation_performed"] is False
        assert preview["authority"] is False
        assert preview["mutation_performed"] is False

        startup, startup_preview = _preview(
            source,
            backup_root,
            trigger_mode="AtStartup",
        )
        assert startup.returncode == 0, startup.stderr
        assert startup_preview is not None
        assert startup_preview["startup_trigger"] is True
        assert startup_preview["logon_trigger"] is False
        assert startup_preview["principal_logon_type"] == "S4U"
        assert startup_preview["principal_run_level"] == "Highest"
        assert startup_preview["mutation_performed"] is False


def test_preview_accepts_watermark_v2_sealed_parent(tmp_path: Path) -> None:
    source = _make_tuple(
        tmp_path,
        prereg_parent="mtvclc_prereg_sealed_watermark_v2",
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, preview = _preview(source, backup_root)
        assert completed.returncode == 0, completed.stderr
        assert preview is not None
        assert preview["preregistration_path"] == str(Path(source["prereg"]).resolve())
        assert preview["capture_root"] == str(Path(source["capture"]).resolve())
        assert preview["mutation_performed"] is False


@pytest.mark.parametrize(
    "prereg_parent",
    [
        "mtvclc_prereg_sealed_runtime_bound_v3",
        "mtvclc_prereg_sealed_runtime_bound_v4",
    ],
)
def test_preview_accepts_exact_gap_v3_v4_tuple(
    tmp_path: Path,
    prereg_parent: str,
) -> None:
    source = _make_tuple(
        tmp_path,
        prereg_parent=prereg_parent,
        tuple_family="gap_v3",
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, preview = _preview(source, backup_root)
        assert completed.returncode == 0, completed.stderr
        assert preview is not None
        assert preview["preregistration_path"] == str(Path(source["prereg"]).resolve())
        assert preview["capture_root"] == str(Path(source["capture"]).resolve())
        assert preview["preservation_filename_schema_version"] == (
            "fxstack.scalp.mtvclc_preservation_filenames.v3"
        )
        assert preview["guard_identity_filename"] == (
            "collector-guard.identity.gap-v3.v1.json"
        )
        assert preview["mutation_performed"] is False


def test_preview_accepts_only_gap_v5_guard_for_runtime_bound_v5(
    tmp_path: Path,
) -> None:
    source = _make_tuple(
        tmp_path,
        prereg_parent="mtvclc_prereg_sealed_runtime_bound_v5",
        tuple_family="gap_v5",
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, preview = _preview(source, backup_root)
        assert completed.returncode == 0, completed.stderr
        assert preview is not None
        assert preview["preservation_filename_schema_version"] == (
            "fxstack.scalp.mtvclc_preservation_filenames.v5"
        )
        assert preview["guard_identity_filename"] == (
            "collector-guard.identity.gap-v5.v1.json"
        )
        assert preview["mutation_performed"] is False


@pytest.mark.parametrize(
    ("prereg_parent", "tuple_family"),
    [
        ("mtvclc_prereg_sealed_runtime_bound_v5", "gap_v3"),
        ("mtvclc_prereg_sealed_runtime_bound_v4", "gap_v5"),
    ],
)
def test_preview_refuses_mixed_gap_v5_guard_leaf(
    tmp_path: Path,
    prereg_parent: str,
    tuple_family: str,
) -> None:
    source = _make_tuple(
        tmp_path,
        prereg_parent=prereg_parent,
        tuple_family=tuple_family,
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, preview = _preview(source, backup_root)
        assert completed.returncode != 0
        assert preview is None
        assert "guard_identity_missing_or_invalid" in completed.stderr
        assert list(backup_root.iterdir()) == []


def test_preview_refuses_mixed_gap_and_legacy_tuple_names(tmp_path: Path) -> None:
    source = _make_tuple(
        tmp_path,
        prereg_parent="mtvclc_prereg_sealed_runtime_bound_v4",
        tuple_family="gap_v3",
    )
    mixed_capture = tmp_path / f"mtvclc_prospective_capture_{PREREG_ID[:16]}"
    Path(source["capture"]).rename(mixed_capture)
    source["capture"] = mixed_capture
    with _backup_root_on_repo_drive() as backup_root:
        completed, preview = _preview(source, backup_root)
        assert completed.returncode != 0
        assert preview is None
        assert "preregistration_capture_tuple_mismatch" in completed.stderr
        assert list(backup_root.iterdir()) == []


def test_preview_refuses_wrong_preregistration_identity(tmp_path: Path) -> None:
    source = _make_tuple(tmp_path)
    with _backup_root_on_repo_drive() as backup_root:
        completed, preview = _preview(source, backup_root, prereg_hash="f" * 64)
        assert completed.returncode != 0
        assert preview is None
        assert "preregistration_sha256_mismatch" in completed.stderr
        assert list(backup_root.iterdir()) == []


def test_remove_and_install_mutations_follow_full_owned_identity_proof() -> None:
    source = REGISTER.read_text(encoding="utf-8")

    assert "$argumentsMatch = [string]::Equals(" in source
    assert "$descriptionMatches = [string]$Task.Description -eq $taskDescription" in source
    assert "$userMatches" in source
    assert "$ignoreNewMatches" in source
    assert "$startWhenAvailableMatches" in source
    assert 'reason = "trigger_count_mismatch"' in source
    assert "$triggerTopologyMatches" in source
    assert "$logOnTriggerUserMatches" in source
    assert "$repetitionIntervalMatches" in source
    assert "$repetitionDurationMatches" in source

    remove_start = source.index('if ($Action -eq "Remove")')
    remove_end = source.index("$taskAction = New-ScheduledTaskAction", remove_start)
    remove_block = source[remove_start:remove_end]
    assert remove_block.index("Test-OwnedScheduledTask") < remove_block.index(
        "Unregister-ScheduledTask"
    )
    assert "scheduled_task_identity_mismatch_refusing_remove" in remove_block

    install_start = source.rindex(
        '$existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName'
    )
    install_block = source[install_start:]
    assert install_block.index("Test-OwnedScheduledTask") < install_block.index(
        "Register-ScheduledTask"
    )
    assert "task already current" in install_block
    assert "scheduled_task_identity_mismatch_refusing_overwrite" in install_block
