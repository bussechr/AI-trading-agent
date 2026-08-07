from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]


def _repo_relative_destination(repo_root: Path, raw: str) -> tuple[Path, Path]:
    rel = Path(str(raw).replace("\\", "/"))
    if rel.is_absolute():
        raise ValueError(f"destination must be repository-relative: {raw}")
    resolved = (repo_root / rel).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ValueError(f"destination escapes repository: {raw}") from exc
    return rel, resolved


def _rebase_artifact_value(value: Any, *, source_root: Path, destination_rel: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: _rebase_artifact_value(item, source_root=source_root, destination_rel=destination_rel)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _rebase_artifact_value(item, source_root=source_root, destination_rel=destination_rel)
            for item in value
        ]
    if not isinstance(value, str) or not value.strip() or "://" in value:
        return value
    candidate = Path(value)
    if not candidate.is_absolute():
        return value
    try:
        tail = candidate.resolve().relative_to(source_root.resolve())
    except ValueError:
        return value
    return (destination_rel / tail).as_posix()


def stage_candidate_registry(
    *,
    repo_root: Path,
    candidate_artifact_root: Path,
    registry_file: Path,
    destination_artifact_root: str,
    destination_registry_root: str,
) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    candidate_artifact_root = candidate_artifact_root.resolve()
    registry_file = registry_file.resolve()
    if not candidate_artifact_root.is_dir():
        raise FileNotFoundError(candidate_artifact_root)
    if not registry_file.is_file():
        raise FileNotFoundError(registry_file)

    artifact_rel, artifact_dest = _repo_relative_destination(repo_root, destination_artifact_root)
    registry_rel, registry_dest = _repo_relative_destination(repo_root, destination_registry_root)
    allowed_root = (repo_root / "fx-quant-stack" / "artifacts_shadow").resolve()
    for destination in (artifact_dest, registry_dest):
        try:
            destination.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError(f"staged destination must stay under {allowed_root}: {destination}") from exc
        if destination.exists():
            raise FileExistsError(destination)

    shutil.copytree(candidate_artifact_root, artifact_dest)
    registry_dest.mkdir(parents=True, exist_ok=False)
    payload = json.loads(registry_file.read_text(encoding="utf-8"))
    staged = _rebase_artifact_value(
        payload,
        source_root=candidate_artifact_root,
        destination_rel=artifact_rel,
    )
    staged_registry = registry_dest / registry_file.name
    staged_registry.write_text(json.dumps(staged, indent=2, sort_keys=True), encoding="utf-8")

    artifacts = dict(staged.get("artifacts") or {})
    validated = 0
    for key, raw in artifacts.items():
        ref = dict(raw or {}) if isinstance(raw, dict) else {"path": raw}
        path_value = str(ref.get("path") or ref.get("artifact_path") or "").strip()
        if not path_value or "://" in path_value:
            continue
        path = (repo_root / Path(path_value.replace("\\", "/"))).resolve()
        try:
            path.relative_to(artifact_dest)
        except ValueError as exc:
            raise ValueError(f"staged artifact path escaped destination ({key}): {path}") from exc
        if not path.exists():
            raise FileNotFoundError(path)
        validated += 1

    return {
        "ok": True,
        "artifact_root": artifact_rel.as_posix(),
        "registry_root": registry_rel.as_posix(),
        "registry_file": staged_registry.relative_to(repo_root).as_posix(),
        "validated_artifacts": validated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage an isolated candidate registry for activation and packaging.")
    parser.add_argument("--candidate-artifact-root", required=True)
    parser.add_argument("--registry-file", required=True)
    parser.add_argument("--destination-artifact-root", required=True)
    parser.add_argument("--destination-registry-root", required=True)
    parser.add_argument("--repo-root", default=str(REPO))
    args = parser.parse_args()
    result = stage_candidate_registry(
        repo_root=Path(args.repo_root),
        candidate_artifact_root=Path(args.candidate_artifact_root),
        registry_file=Path(args.registry_file),
        destination_artifact_root=str(args.destination_artifact_root),
        destination_registry_root=str(args.destination_registry_root),
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
