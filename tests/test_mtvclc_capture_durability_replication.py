from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterator

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "ops" / "windows" / "29_preserve_mtvclc_capture.ps1"
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


def _make_source(
    tmp_path: Path,
    *,
    recent_chunk: bool = False,
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
    prereg.write_text('{"sealed":"opaque-test-bytes"}\n', encoding="utf-8")

    capture = tmp_path / f"{capture_prefix}{PREREG_ID[:16]}"
    hour = capture / "chunks" / "20260803T15"
    hour.mkdir(parents=True)
    guard = capture / guard_name
    manifest = capture / "manifest.sha256.jsonl"
    journal = capture / "active-hour.journal.sha256.jsonl"
    chunk = hour / "ig-mt4-m1-activity-s0001-q0000000001.json"
    guard.write_text('{"identity":"opaque-test-bytes"}\n', encoding="utf-8")
    manifest.write_text('{"manifest":"opaque-test-bytes"}\n', encoding="utf-8")
    journal.write_text('{"journal":"opaque-test-bytes"}\n', encoding="utf-8")
    chunk.write_text('{"chunk":"opaque-test-bytes"}\n', encoding="utf-8")
    if not recent_chunk:
        old = time.time() - 600
        os.utime(chunk, (old, old))
    return {
        "prereg": prereg,
        "capture": capture,
        "guard": guard,
        "manifest": manifest,
        "journal": journal,
        "chunk": chunk,
        "prereg_hash": _sha256(prereg),
        "guard_hash": _sha256(guard),
    }


@contextlib.contextmanager
def _backup_root_on_repo_drive() -> Iterator[Path]:
    # Pytest's tmp_path is on C: on the Windows host; use an isolated sibling of
    # the D: repository so the script exercises its distinct-volume boundary.
    path = Path(tempfile.mkdtemp(prefix="mtvclc-durability-test-", dir=ROOT.parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _run(
    source: dict[str, Path | str],
    backup_root: Path,
    prereg_hash: str | None = None,
    guard_hash: str | None = None,
    preservation_script_hash: str | None = None,
    warning_free_bytes: int = 1,
    hard_minimum_free_bytes: int = 1,
    maximum_journal_age_seconds: int = 600,
    maximum_manifest_age_seconds: int = 600,
    minimum_closed_age_seconds: int = 60,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    capture = Path(source["capture"])
    arguments = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(SCRIPT),
        "-Preregistration",
        str(source["prereg"]),
        "-CaptureRoot",
        str(capture),
        "-BackupRoot",
        str(backup_root),
        "-ExpectedPreregistrationSha256",
        prereg_hash or str(source["prereg_hash"]),
        "-ExpectedGuardIdentitySha256",
        guard_hash or str(source["guard_hash"]),
    ]
    if preservation_script_hash is not None:
        arguments.extend(
            ["-ExpectedPreservationScriptSha256", preservation_script_hash]
        )
    arguments.extend(
        [
            "-ExpectedCaptureDriveLetter",
            capture.drive.rstrip(":"),
            "-WarningFreeBytes",
            str(warning_free_bytes),
            "-HardMinimumFreeBytes",
            str(hard_minimum_free_bytes),
            "-MaximumJournalAgeSeconds",
            str(maximum_journal_age_seconds),
            "-MaximumManifestAgeSeconds",
            str(maximum_manifest_age_seconds),
            "-MinimumClosedAgeSeconds",
            str(minimum_closed_age_seconds),
            "-StabilityProbeSeconds",
            "0",
        ]
    )
    completed = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, (completed.stdout, completed.stderr)
    return completed, json.loads(lines[0])


def test_script_is_metadata_only_append_only_and_authority_free() -> None:
    _parse_powershell(SCRIPT)
    source = SCRIPT.read_text(encoding="utf-8")
    lowered = source.lower()

    assert "[string]$BackupRoot" in source
    assert '[string]$ExpectedCaptureDriveLetter = "D"' in source
    assert "[long]$WarningFreeBytes = 500GB" in source
    assert "[long]$HardMinimumFreeBytes = 311GB" in source
    assert "backup_volume_must_differ_from_capture_volume" in source
    assert "active-hour.journal.sha256.jsonl" in source
    assert "active_journal_copied" in source
    assert "active_journal_copy_authorized" in source
    assert "manifest.sha256.{0}.jsonl" in source
    assert "closed_chunk_changed_during_stability_probe" in source
    assert "source_file_changed_during_copy" in source
    assert "[string]$ExpectedPreservationScriptSha256" in source
    assert "preservation_script_sha256_mismatch" in source
    assert "preservation_script_sha256 = $preservationScriptSha256" in source

    assert "get-content" not in lowered
    assert "convertfrom-json" not in lowered
    assert "readalltext" not in lowered
    assert "import-csv" not in lowered
    assert "select-string" not in lowered
    assert "remove-item" not in lowered
    assert "clear-content" not in lowered
    assert "robocopy" not in lowered
    assert "copy-appendonlystablefile $journal" not in lowered
    assert '"e:\\"' not in lowered

    for field in (
        "evaluation_performed",
        "signal_computation_authorized",
        "outcome_access_authorized",
        "performance_computation_authorized",
        "success_claim_authorized",
        "issuer_authorized",
        "signature_authorized",
        "authority_granted",
        "runtime_authorized",
        "activation_authorized",
        "broker_access_authorized",
        "order_authorized",
    ):
        assert f'$Payload["{field}"] = $false' in source


def test_replication_is_exact_verified_idempotent_and_keeps_history(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    with _backup_root_on_repo_drive() as backup_root:
        first, first_report = _run(source, backup_root)
        assert first.returncode == 0, first.stderr
        assert first_report["status"] == "replication_current"
        assert first_report["files_copied"] == 4
        assert first_report["files_reused"] == 0
        assert first_report["active_journal_copied"] is False
        assert first_report["authority"] is False
        assert first_report["authority_granted"] is False
        assert first_report["destination_delete_performed"] is False

        tuple_root = Path(str(first_report["backup_tuple_root"]))
        copied_prereg = tuple_root / "preregistration" / Path(source["prereg"]).name
        copied_guard = tuple_root / "metadata" / Path(source["guard"]).name
        copied_chunk = (
            tuple_root
            / "chunks"
            / "20260803T15"
            / Path(source["chunk"]).name
        )
        manifest_snapshots = list((tuple_root / "metadata").glob("manifest.sha256.*.jsonl"))
        assert copied_prereg.read_bytes() == Path(source["prereg"]).read_bytes()
        assert copied_guard.read_bytes() == Path(source["guard"]).read_bytes()
        assert copied_chunk.read_bytes() == Path(source["chunk"]).read_bytes()
        assert len(manifest_snapshots) == 1
        assert not list(tuple_root.rglob("active-hour.journal.sha256.jsonl"))

        sentinel = tuple_root / "operator-retained-sentinel"
        sentinel.write_text("must-survive", encoding="utf-8")
        second, second_report = _run(source, backup_root)
        assert second.returncode == 0, second.stderr
        assert second_report["files_copied"] == 0
        assert second_report["files_reused"] == 4
        assert sentinel.read_text(encoding="utf-8") == "must-survive"

        Path(source["manifest"]).write_text(
            '{"manifest":"second-opaque-snapshot"}\n', encoding="utf-8"
        )
        third, third_report = _run(source, backup_root)
        assert third.returncode == 0, third.stderr
        assert third_report["files_copied"] == 1
        assert third_report["files_reused"] == 3
        assert len(list((tuple_root / "metadata").glob("manifest.sha256.*.jsonl"))) == 2
        assert sentinel.read_text(encoding="utf-8") == "must-survive"


def test_replication_accepts_watermark_v2_sealed_parent(tmp_path: Path) -> None:
    source = _make_source(
        tmp_path,
        prereg_parent="mtvclc_prereg_sealed_watermark_v2",
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root)
        assert completed.returncode == 0, completed.stderr
        assert report["status"] == "replication_current"
        assert report["files_copied"] == 4


@pytest.mark.parametrize(
    "prereg_parent",
    [
        "mtvclc_prereg_sealed_runtime_bound_v3",
        "mtvclc_prereg_sealed_runtime_bound_v4",
    ],
)
def test_replication_accepts_exact_gap_v3_v4_tuple(
    tmp_path: Path,
    prereg_parent: str,
) -> None:
    source = _make_source(
        tmp_path,
        prereg_parent=prereg_parent,
        tuple_family="gap_v3",
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root)
        assert completed.returncode == 0, completed.stderr
        assert report["status"] == "replication_current"
        assert report["files_copied"] == 4


def test_replication_accepts_only_gap_v5_guard_for_runtime_bound_v5(
    tmp_path: Path,
) -> None:
    source = _make_source(
        tmp_path,
        prereg_parent="mtvclc_prereg_sealed_runtime_bound_v5",
        tuple_family="gap_v5",
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root)
        assert completed.returncode == 0, completed.stderr
        assert report["status"] == "replication_current"
        assert report["files_copied"] == 4
        assert report["preservation_filename_schema_version"] == (
            "fxstack.scalp.mtvclc_preservation_filenames.v5"
        )
        assert report["guard_identity_filename"] == (
            "collector-guard.identity.gap-v5.v1.json"
        )


@pytest.mark.parametrize(
    ("prereg_parent", "tuple_family"),
    [
        ("mtvclc_prereg_sealed_runtime_bound_v5", "gap_v3"),
        ("mtvclc_prereg_sealed_runtime_bound_v4", "gap_v5"),
    ],
)
def test_replication_refuses_mixed_gap_v5_guard_leaf(
    tmp_path: Path,
    prereg_parent: str,
    tuple_family: str,
) -> None:
    source = _make_source(
        tmp_path,
        prereg_parent=prereg_parent,
        tuple_family=tuple_family,
    )
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root)
        assert completed.returncode == 2
        assert report["reason"] == "guard_identity_missing_or_invalid"
        assert list(backup_root.iterdir()) == []


def test_replication_refuses_mixed_gap_and_legacy_tuple_names(tmp_path: Path) -> None:
    source = _make_source(
        tmp_path,
        prereg_parent="mtvclc_prereg_sealed_runtime_bound_v4",
        tuple_family="gap_v3",
    )
    mixed_capture = tmp_path / f"mtvclc_prospective_capture_{PREREG_ID[:16]}"
    Path(source["capture"]).rename(mixed_capture)
    source["capture"] = mixed_capture
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root)
        assert completed.returncode == 2
        assert report["reason"] == "preregistration_capture_tuple_mismatch"
        assert list(backup_root.iterdir()) == []


def test_recent_chunk_defers_manifest_and_never_copies_active_journal(tmp_path: Path) -> None:
    source = _make_source(tmp_path, recent_chunk=True)
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(
            source,
            backup_root,
            minimum_closed_age_seconds=300,
        )
        assert completed.returncode == 0, completed.stderr
        assert report["status"] == "replication_waiting_for_closed_chunks"
        assert report["recent_chunk_skipped_count"] == 1
        assert report["manifest_snapshot_deferred"] is True
        assert report["files_copied"] == 2
        tuple_root = Path(str(report["backup_tuple_root"]))
        assert not list((tuple_root / "chunks").rglob("*.json"))
        assert not list((tuple_root / "metadata").glob("manifest.sha256.*.jsonl"))
        assert not list(tuple_root.rglob("active-hour.journal.sha256.jsonl"))


def test_refuses_stale_hard_capacity_wrong_identity_and_same_volume(tmp_path: Path) -> None:
    source = _make_source(tmp_path)

    old = time.time() - 3600
    os.utime(Path(source["journal"]), (old, old))
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root)
        assert completed.returncode == 2
        assert report["reason"] == "active_journal_metadata_stale"
        assert list(backup_root.iterdir()) == []

    os.utime(Path(source["journal"]), None)
    os.utime(Path(source["manifest"]), (old, old))
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root)
        assert completed.returncode == 2
        assert report["reason"] == "manifest_metadata_stale"
        assert list(backup_root.iterdir()) == []

    os.utime(Path(source["manifest"]), None)
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(
            source,
            backup_root,
            warning_free_bytes=2**63 - 1,
            hard_minimum_free_bytes=2**63 - 1,
        )
        assert completed.returncode == 2
        assert report["reason"] == "capture_volume_free_space_below_hard_minimum"
        assert list(backup_root.iterdir()) == []

    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(source, backup_root, prereg_hash="f" * 64)
        assert completed.returncode == 2
        assert report["reason"] == "preregistration_sha256_mismatch"
        assert list(backup_root.iterdir()) == []

    same_volume_backup = tmp_path / "same-volume-backup"
    same_volume_backup.mkdir()
    completed, report = _run(source, same_volume_backup)
    assert completed.returncode == 2
    assert report["reason"] == "backup_volume_must_differ_from_capture_volume"
    assert list(same_volume_backup.iterdir()) == []


def test_refuses_wrong_preservation_script_hash_before_backup_mutation(
    tmp_path: Path,
) -> None:
    source = _make_source(tmp_path)
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(
            source,
            backup_root,
            preservation_script_hash="f" * 64,
        )
        assert completed.returncode == 2
        assert report["reason"] == "preservation_script_sha256_mismatch"
        assert list(backup_root.iterdir()) == []


def test_capacity_warning_is_reported_without_weakening_replication(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    with _backup_root_on_repo_drive() as backup_root:
        completed, report = _run(
            source,
            backup_root,
            warning_free_bytes=2**63 - 1,
            hard_minimum_free_bytes=1,
        )
        assert completed.returncode == 0, completed.stderr
        assert report["status"] == "replicated_with_capacity_warning"
        assert report["capacity_status"] == "warning"
        assert report["files_copied"] == 4
        assert report["authority"] is False


def test_existing_destination_mismatch_is_never_overwritten(tmp_path: Path) -> None:
    source = _make_source(tmp_path)
    with _backup_root_on_repo_drive() as backup_root:
        first, report = _run(source, backup_root)
        assert first.returncode == 0, first.stderr
        tuple_root = Path(str(report["backup_tuple_root"]))
        copied_chunk = (
            tuple_root
            / "chunks"
            / "20260803T15"
            / Path(source["chunk"]).name
        )
        copied_chunk.write_text("destination-bytes-must-remain", encoding="utf-8")

        refused, refused_report = _run(source, backup_root)
        assert refused.returncode == 2
        assert refused_report["reason"] == "backup_existing_file_identity_mismatch"
        assert copied_chunk.read_text(encoding="utf-8") == "destination-bytes-must-remain"
