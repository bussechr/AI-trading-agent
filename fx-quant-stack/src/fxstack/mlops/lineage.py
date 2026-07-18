from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Iterable

from fxstack.features.session_contract import current_feature_schema
from fxstack.mlops.types import LineageSnapshot
from fxstack.utils.hashing import hash_mapping


def _git_output(*args: str, project_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if int(proc.returncode) != 0:
        return ""
    return str(proc.stdout or "").strip()


def _normalize_inputs(paths: Iterable[Path | str] | None) -> list[Path]:
    out: list[Path] = []
    for raw in list(paths or []):
        txt = str(raw or "").strip()
        if not txt:
            continue
        out.append(Path(txt))
    return out


def _digest_file(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _path_entries(paths: Iterable[Path | str] | None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw in _normalize_inputs(paths):
        path = Path(raw)
        if not path.exists():
            entries.append({"path": str(path), "exists": False, "kind": "missing", "hash": ""})
            continue
        if path.is_file():
            entries.append({"path": str(path), "exists": True, "kind": "file", "hash": _digest_file(path)})
            continue
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            rel = child.relative_to(path)
            entries.append(
                {
                    "path": str(path / rel),
                    "relative_path": str(rel).replace("\\", "/"),
                    "exists": True,
                    "kind": "file",
                    "hash": _digest_file(child),
                }
            )
        if not any(item.get("path", "").startswith(str(path)) for item in entries):
            entries.append({"path": str(path), "exists": True, "kind": "dir", "hash": ""})
    return entries


def _entries_hash(entries: list[dict[str, Any]]) -> str:
    return hash_mapping({"entries": entries})


def artifact_tree_hash(path: Path | str) -> str:
    entries = _path_entries([Path(str(path))])
    return _entries_hash(entries)


def compute_lineage_snapshot(
    *,
    raw_paths: Iterable[Path | str] | None = None,
    feature_paths: Iterable[Path | str] | None = None,
    label_paths: Iterable[Path | str] | None = None,
    feature_schema: dict[str, Any] | None = None,
    label_config: dict[str, Any] | None = None,
    risk_config: dict[str, Any] | None = None,
    training_config: dict[str, Any] | None = None,
    pair: str = "",
    timeframes: dict[str, str] | None = None,
    project_root: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> LineageSnapshot:
    root = Path(project_root or Path(__file__).resolve().parents[2])
    raw_entries = _path_entries(raw_paths)
    feature_entries = _path_entries(feature_paths)
    label_entries = _path_entries(label_paths)
    schema = current_feature_schema(feature_schema)

    raw_hash = _entries_hash(raw_entries)
    feature_hash = _entries_hash(feature_entries + [{"feature_schema": schema}])
    label_hash = hash_mapping(
        {
            "label_entries": label_entries,
            "label_config": dict(label_config or {}),
        }
    )
    risk_hash = hash_mapping(dict(risk_config or {}))
    training_hash = hash_mapping(
        {
            "training_config": dict(training_config or {}),
            "feature_schema": schema,
            "extra": dict(extra or {}),
            "timeframes": dict(timeframes or {}),
        }
    )

    git_sha = _git_output("rev-parse", "HEAD", project_root=root)
    git_branch = _git_output("rev-parse", "--abbrev-ref", "HEAD", project_root=root)
    git_dirty = bool(_git_output("status", "--porcelain", project_root=root))

    dataset_fingerprint = hash_mapping(
        {
            "pair": str(pair).upper(),
            "timeframes": dict(timeframes or {}),
            "raw_bars_hash": raw_hash,
            "feature_set_hash": feature_hash,
            "label_config_hash": label_hash,
            "risk_config_hash": risk_hash,
            "training_config_hash": training_hash,
            "git_sha": git_sha,
            "git_dirty": git_dirty,
        }
    )

    return LineageSnapshot(
        dataset_fingerprint=dataset_fingerprint,
        raw_bars_hash=raw_hash,
        feature_set_hash=feature_hash,
        label_config_hash=label_hash,
        risk_config_hash=risk_hash,
        training_config_hash=training_hash,
        feature_service_version=feature_hash[:16],
        label_version=label_hash[:16],
        risk_config_version=risk_hash[:16],
        git_sha=git_sha,
        git_dirty=git_dirty,
        pair=str(pair).upper(),
        raw_inputs=[str(item.get("path") or "") for item in raw_entries],
        feature_inputs=[str(item.get("path") or "") for item in feature_entries],
        label_inputs=[str(item.get("path") or "") for item in label_entries],
        timeframes={str(k): str(v) for k, v in dict(timeframes or {}).items()},
        feature_schema=schema,
        label_config=dict(label_config or {}),
        risk_config=dict(risk_config or {}),
        training_config=dict(training_config or {}),
        git_branch=git_branch,
    )
