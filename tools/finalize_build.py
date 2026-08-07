from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.training.release_evidence import (  # noqa: E402
    FAST_SHADOW_MIN_DURATION_SECS,
    PHASE5_SHADOW_MIN_DURATION_SECS,
    RELEASE_VALIDATION_BUNDLE_SCHEMA,
    ReleaseEvidenceIdentity,
    active_manifest_identity,
    file_sha256,
    validate_blockers_artifact,
    validate_release_validation_bundle,
    validate_rollback_evidence,
    validate_shadow_runtime_evidence,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _resolve_latest_evidence_dir(evidence_root: Path) -> Path | None:
    if not evidence_root.exists():
        return None
    candidates = [path for path in evidence_root.glob("*_full_process") if path.is_dir()]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _required_path(args: argparse.Namespace, name: str) -> Path:
    value = str(getattr(args, name, "") or "").strip()
    if not value:
        raise SystemExit(f"--{name.replace('_', '-')} is required")
    path = Path(value).resolve()
    if not path.is_file():
        raise SystemExit(f"--{name.replace('_', '-')} does not name a file: {path}")
    return path


def _immutable_snapshot(*, source: Path, evidence_dir: Path, label: str) -> Path:
    """Copy authority input bytes to a content-addressed, write-once evidence file."""

    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    root = evidence_dir.resolve()
    target = root / f"{label}_{digest}.json"
    if not target.resolve(strict=False).is_relative_to(root):
        raise SystemExit(f"immutable evidence snapshot escaped evidence directory: {target}")

    def _reject_link_or_reparse() -> None:
        if target.is_symlink():
            raise SystemExit(f"immutable evidence snapshot target is a symlink: {target}")
        try:
            attributes = int(getattr(target.lstat(), "st_file_attributes", 0) or 0)
        except FileNotFoundError:
            attributes = 0
        if attributes & 0x400:  # Windows FILE_ATTRIBUTE_REPARSE_POINT
            raise SystemExit(f"immutable evidence snapshot target is a reparse point: {target}")

    _reject_link_or_reparse()
    if target.exists():
        if not target.is_file() or target.read_bytes() != payload:
            raise SystemExit(f"immutable evidence snapshot collision: {target}")
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError:
            _reject_link_or_reparse()
            if not target.is_file() or target.read_bytes() != payload:
                raise SystemExit(f"immutable evidence snapshot collision: {target}") from None
        else:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
    _reject_link_or_reparse()
    if not target.resolve(strict=True).is_relative_to(root):
        raise SystemExit(f"immutable evidence snapshot escaped evidence directory: {target}")
    if file_sha256(target) != digest:
        raise SystemExit(f"immutable evidence snapshot verification failed: {target}")
    return target.resolve()


def _write_authority_bundle(*, payload: dict[str, Any], evidence_dir: Path) -> Path:
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    root = evidence_dir.resolve()
    target = root / f"release_validation_bundle_{digest}.json"
    if not target.resolve(strict=False).is_relative_to(root):
        raise SystemExit(f"immutable release bundle escaped evidence directory: {target}")
    try:
        attributes = int(getattr(target.lstat(), "st_file_attributes", 0) or 0)
    except FileNotFoundError:
        attributes = 0
    if target.is_symlink() or attributes & 0x400:
        raise SystemExit(f"immutable release bundle target is a link/reparse point: {target}")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
            raise SystemExit(f"immutable release bundle collision: {target}")
        return target.resolve()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
            raise SystemExit(f"immutable release bundle collision: {target}") from None
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    try:
        attributes = int(getattr(target.lstat(), "st_file_attributes", 0) or 0)
    except FileNotFoundError:
        attributes = 0
    if target.is_symlink() or attributes & 0x400 or target.resolve(strict=True).parent != root:
        raise SystemExit(f"immutable release bundle escaped evidence directory: {target}")
    if file_sha256(target) != digest:
        raise SystemExit(f"immutable release bundle verification failed: {target}")
    return target.resolve()


def _shadow_result(
    *,
    path: Path,
    expected: ReleaseEvidenceIdentity,
    minimum_duration_secs: float,
) -> dict[str, Any]:
    payload = _load_json(path)
    validation = validate_shadow_runtime_evidence(
        path=path,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
        minimum_duration_secs=minimum_duration_secs,
    )
    return {
        "path": str(path),
        "sha256": validation.artifact_sha256,
        "window": {
            "started_at": payload.get("started_at"),
            "ended_at": payload.get("ended_at"),
        },
        "validation": validation.to_dict(),
    }


def _window(ref: dict[str, Any]) -> tuple[float, float]:
    raw = dict(ref.get("window") or {})
    try:
        return float(raw.get("started_at")), float(raw.get("ended_at"))
    except (TypeError, ValueError, OverflowError):
        return float("nan"), float("nan")


def run(args: argparse.Namespace) -> int:
    evidence_dir = (
        Path(str(args.evidence_dir)).resolve()
        if str(getattr(args, "evidence_dir", "") or "").strip()
        else _resolve_latest_evidence_dir(Path(str(args.evidence_root)).resolve())
    )
    if evidence_dir is None or not evidence_dir.is_dir():
        raise SystemExit("No audit evidence directory found. Run tools/full_process_audit.py first.")

    model_manifest_source = _required_path(args, "model_manifest")
    # The operational activation manifest may later acquire rollout metadata.
    # Finalized evidence must never point at those mutable bytes: bind an exact,
    # content-addressed snapshot and validate the live active identity separately
    # at each release transition.
    model_manifest = _immutable_snapshot(
        source=model_manifest_source,
        evidence_dir=evidence_dir,
        label="finalized_model_manifest",
    )
    fast_source = _required_path(args, "fast_gate_artifact")
    long_source = _required_path(args, "shadow_artifact")
    rollback_source = _required_path(args, "rollback_evidence")
    blockers_source = (evidence_dir / "blockers.json").resolve()
    if not blockers_source.is_file():
        raise SystemExit(f"required blockers artifact is missing: {blockers_source}")
    fast_path = _immutable_snapshot(source=fast_source, evidence_dir=evidence_dir, label="fast_shadow")
    long_path = _immutable_snapshot(source=long_source, evidence_dir=evidence_dir, label="long_shadow")
    rollback_path = _immutable_snapshot(
        source=rollback_source,
        evidence_dir=evidence_dir,
        label="rollback_evidence",
    )
    blockers_path = _immutable_snapshot(
        source=blockers_source,
        evidence_dir=evidence_dir,
        label="blockers",
    )

    expected = active_manifest_identity(
        manifest_path=model_manifest,
        pair=str(args.pair).upper(),
    )
    requested_bundle_run_id = str(getattr(args, "bundle_run_id", "") or "").strip()
    if requested_bundle_run_id and requested_bundle_run_id != expected.bundle_run_id:
        raise SystemExit("--bundle-run-id does not match the selected pair in --model-manifest")
    if not expected.bundle_run_id or not expected.model_set_id or not expected.artifact_set_sha256:
        raise SystemExit("selected pair has an incomplete active model identity")

    # Reopen every input on every invocation. Existing gate_summary/go_no_go
    # artifacts are outputs only and never authority for a subsequent run.
    fast = _shadow_result(
        path=fast_path,
        expected=expected,
        minimum_duration_secs=FAST_SHADOW_MIN_DURATION_SECS,
    )
    long = _shadow_result(
        path=long_path,
        expected=expected,
        minimum_duration_secs=PHASE5_SHADOW_MIN_DURATION_SECS,
    )
    rollback = validate_rollback_evidence(
        path=rollback_path,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
    )
    blocker_errors, open_critical_high, blocker_sha = validate_blockers_artifact(blockers_path)

    distinct_errors: list[str] = []
    if fast_source == long_source:
        distinct_errors.append("shadow_artifact_paths_duplicate")
    if str(fast.get("sha256") or "") == str(long.get("sha256") or ""):
        distinct_errors.append("shadow_artifact_bytes_duplicate")
    fast_window = _window(fast)
    long_window = _window(long)
    if not (
        fast_window[0] < fast_window[1]
        and long_window[0] < long_window[1]
        and (fast_window[1] <= long_window[0] or long_window[1] <= fast_window[0])
    ):
        distinct_errors.append("shadow_run_windows_overlap_or_invalid")

    checks = {
        "fast_shadow_valid": bool(dict(fast.get("validation") or {}).get("valid", False)),
        "long_shadow_valid": bool(dict(long.get("validation") or {}).get("valid", False)),
        "shadow_runs_distinct_non_overlapping": not distinct_errors,
        "rollback_evidence_valid": rollback.valid,
        "blockers_artifact_valid": not blocker_errors,
        "no_open_critical_high": not open_critical_high,
    }
    reasons = [name for name, passed in checks.items() if not passed]
    reasons.extend(distinct_errors)
    reasons.extend(f"rollback:{item}" for item in rollback.errors)
    reasons.extend(f"blockers:{item}" for item in blocker_errors)
    go = all(checks.values())

    final_identity = ReleaseEvidenceIdentity(
        pair=expected.pair,
        bundle_run_id=expected.bundle_run_id,
        model_set_id=expected.model_set_id,
        model_manifest_sha256=expected.model_manifest_sha256,
        artifact_set_sha256=expected.artifact_set_sha256,
        evidence_kind="runtime_shadow",
        source_kind="production_release_finalization",
        advisory_only=False,
    )
    artifacts = {
        "model_manifest": {"path": str(model_manifest), "sha256": file_sha256(model_manifest)},
        "fast_shadow": {"path": str(fast_path), "sha256": str(fast.get("sha256") or "")},
        "long_shadow": {"path": str(long_path), "sha256": str(long.get("sha256") or "")},
        "rollback_evidence": {"path": str(rollback_path), "sha256": rollback.artifact_sha256},
        "blockers": {"path": str(blockers_path), "sha256": blocker_sha},
    }
    release_bundle = {
        "schema_version": RELEASE_VALIDATION_BUNDLE_SCHEMA,
        "generated_at": _now_iso(),
        "generated_at_epoch": time.time(),
        "status": "passed" if go else "failed",
        "valid": bool(go),
        "evidence_identity": final_identity.to_dict(),
        "artifacts": artifacts,
        "fast_shadow": fast,
        "long_shadow": long,
        "rollback_validation": rollback.to_dict(),
        "blockers_validation": {
            "valid": not blocker_errors,
            "errors": blocker_errors,
            "open_critical_high": open_critical_high,
        },
        "checks": checks,
        "errors": list(dict.fromkeys(reasons)),
    }
    release_bundle_path = _write_authority_bundle(payload=release_bundle, evidence_dir=evidence_dir)

    # Prove that the emitted envelope itself can reopen and revalidate all
    # source bytes. A construction bug therefore degrades GO to HOLD.
    envelope_validation = validate_release_validation_bundle(
        path=release_bundle_path,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
    )
    if go and not envelope_validation.valid:
        go = False
        release_bundle["status"] = "failed"
        release_bundle["valid"] = False
        release_bundle["errors"] = [
            *list(release_bundle.get("errors") or []),
            *(f"envelope:{item}" for item in envelope_validation.errors),
        ]
        release_bundle_path = _write_authority_bundle(payload=release_bundle, evidence_dir=evidence_dir)

    # This fixed-name document is an advisory locator only. Validators reject
    # it as authority; consumers must use the exact content-addressed path/hash.
    latest_pointer = {
        "schema_version": "phase5_release_validation_pointer_v1",
        "advisory_only": True,
        "generated_at": _now_iso(),
        "authority_path": str(release_bundle_path),
        "authority_sha256": file_sha256(release_bundle_path),
    }
    (evidence_dir / "release_validation_bundle.json").write_text(
        json.dumps(latest_pointer, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    gate_summary = {
        "schema_version": 2,
        "generated_at": _now_iso(),
        "fast_gate": fast,
        "shadow_24h": long,
        "rollback": rollback.to_dict(),
        "blockers": {
            "path": str(blockers_path),
            "sha256": blocker_sha,
            "errors": blocker_errors,
            "open_critical_high": open_critical_high,
        },
        "release_evidence_distinct": {
            "valid": not distinct_errors,
            "errors": distinct_errors,
            "fast_window": list(fast_window),
            "shadow_24h_window": list(long_window),
        },
        "release_validation_bundle": {
            "path": str(release_bundle_path),
            "sha256": file_sha256(release_bundle_path),
        },
    }
    (evidence_dir / "gate_summary.json").write_text(
        json.dumps(gate_summary, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    decision = {
        "schema_version": 2,
        "generated_at": _now_iso(),
        "decision": "GO" if go else "HOLD",
        "go": bool(go),
        "checks": checks,
        "reasons": list(release_bundle.get("errors") or []),
        "release_validation_bundle": str(release_bundle_path),
        "release_validation_bundle_sha256": file_sha256(release_bundle_path),
    }
    (evidence_dir / "go_no_go.json").write_text(
        json.dumps(decision, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    summary = [
        "# Finalization Summary",
        "",
        f"Evidence directory: `{evidence_dir}`",
        f"Decision: **{decision['decision']}**",
        f"Release validation bundle: `{release_bundle_path}`",
        "",
        "## Checks",
        "",
        *(f"- `{name}`: `{value}`" for name, value in checks.items()),
        "",
        "## Reasons",
        "",
        *(f"- `{reason}`" for reason in list(decision.get("reasons") or []) or ["(none)"]),
        "",
    ]
    (evidence_dir / "finalization_summary.md").write_text("\n".join(summary), encoding="utf-8")
    print(
        json.dumps(
            {
                "evidence_dir": str(evidence_dir),
                "decision": decision["decision"],
                "release_validation_bundle": str(release_bundle_path),
            },
            indent=2,
        )
    )
    return 0 if go else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Finalize audit evidence from fresh fast, 24-hour, rollback, and blocker artifacts"
    )
    parser.add_argument("--evidence-dir", default="")
    parser.add_argument("--evidence-root", default="docs/audit")
    parser.add_argument("--fast-gate-artifact", required=True)
    parser.add_argument("--shadow-artifact", required=True)
    parser.add_argument("--rollback-evidence", required=True)
    parser.add_argument("--pair", required=True)
    parser.add_argument("--bundle-run-id", default="")
    parser.add_argument("--model-manifest", required=True)
    return parser


def main() -> None:
    raise SystemExit(int(run(build_parser().parse_args()) or 0))


if __name__ == "__main__":
    main()
