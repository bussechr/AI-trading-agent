"""Build immutable model manifests for offline research only.

The builder reads local training-registry JSON files and writes one manifest.
It has no activation, runtime-store, network, database, or execution behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


RESEARCH_MANIFEST_VERSION = "fxstack_research_manifest_v1"

_COMPONENT_NAMES = (
    "directional_belief",
    "reversal_opportunity",
    "reversal_failure",
    "swing_transformer",
    "intraday_tcn",
    "intraday_xgb",
    "swing_xgb",
    "exit_policy",
    "regime",
    "meta",
)


def _json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid research registry JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"research registry entry must be an object: {path}")
    return payload


def _artifact_path(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, dict):
        return ""
    evidence = dict(raw.get("evidence_refs") or {})
    return str(
        raw.get("path")
        or raw.get("artifact_path")
        or raw.get("artifact_dir")
        or raw.get("model_path")
        or evidence.get("artifact_path")
        or ""
    ).strip()


def _resolved_bundle_root(bundle_root: Path) -> Path:
    raw = str(bundle_root or "").strip()
    if not raw:
        raise ValueError("an explicit research bundle_root is required")
    try:
        resolved = Path(raw.replace("\\", os.sep)).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"missing research bundle root: {raw}") from exc
    if not resolved.is_dir():
        raise NotADirectoryError(f"research bundle root is not a directory: {resolved}")
    return resolved


def _bundle_path(
    raw_path: str | Path,
    *,
    bundle_root: Path,
    label: str,
    must_exist: bool,
) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise ValueError(f"{label} is required")
    path = Path(raw.replace("\\", os.sep)).expanduser()
    candidate = path if path.is_absolute() else bundle_root / path
    try:
        resolved = candidate.resolve(strict=must_exist)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"missing {label}: {candidate}") from exc
    if not resolved.is_relative_to(bundle_root):
        raise ValueError(
            f"{label} resolves outside research bundle: path={resolved} bundle={bundle_root}"
        )
    return resolved


def _tree_sha256(path: Path, *, bundle_root: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    if not path.is_dir():
        raise FileNotFoundError(f"research artifact is not local: {path}")
    entries = sorted(path.rglob("*"))
    for entry in entries:
        _bundle_path(
            entry,
            bundle_root=bundle_root,
            label="research artifact tree entry",
            must_exist=True,
        )
    files = [item for item in entries if item.is_file()]
    if not files:
        raise RuntimeError(f"research artifact directory is empty: {path}")
    for file_path in files:
        digest.update(file_path.relative_to(path).as_posix().encode("utf-8"))
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _resolve_local_artifact(
    raw_path: str,
    *,
    registry_root: Path,
    bundle_root: Path,
) -> Path:
    path = Path(str(raw_path).replace("\\", os.sep)).expanduser()
    candidates = (path,) if path.is_absolute() else (registry_root.parent / path, bundle_root / path)
    seen: set[Path] = set()
    for candidate in candidates:
        unresolved = candidate.absolute()
        if unresolved in seen:
            continue
        seen.add(unresolved)
        resolved = _bundle_path(
            candidate,
            bundle_root=bundle_root,
            label="research artifact path",
            must_exist=False,
        )
        if resolved.exists():
            return _bundle_path(
                candidate,
                bundle_root=bundle_root,
                label="research artifact path",
                must_exist=True,
            )
    raise FileNotFoundError(f"research artifact path does not exist: {raw_path}")


def _immutable_artifact_ref(
    raw: Any,
    *,
    registry_root: Path,
    bundle_root: Path,
) -> dict[str, Any]:
    if isinstance(raw, dict):
        ref = dict(raw)
    elif isinstance(raw, str):
        ref = {"path": raw}
    else:
        raise RuntimeError("research artifact reference must be a local path or object")
    path_value = _artifact_path(ref)
    if not path_value:
        raise RuntimeError("research artifact reference is missing a local path")
    resolved = _resolve_local_artifact(
        path_value,
        registry_root=registry_root,
        bundle_root=bundle_root,
    )
    digest = str(ref.get("artifact_hash") or "").strip().lower()
    digest_is_valid = len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )
    immutable_ref: dict[str, Any] = {"path": str(resolved)}
    if digest_is_valid:
        immutable_ref["artifact_hash"] = digest
    immutable_ref["research_content_sha256"] = _tree_sha256(
        resolved,
        bundle_root=bundle_root,
    )
    return immutable_ref


def _pair_row(payload: dict[str, Any], pair: str) -> dict[str, Any] | None:
    for key in ("active_model_sets", "model_sets", "candidates"):
        rows = payload.get(key)
        if isinstance(rows, dict):
            for raw_pair, row in rows.items():
                if str(raw_pair).upper() == pair and isinstance(row, dict):
                    return dict(row)
    for key in ("model_set", "candidate", "record"):
        row = payload.get(key)
        if isinstance(row, dict) and isinstance(row.get("artifacts"), dict):
            return dict(row)
    if isinstance(payload.get("artifacts"), dict):
        payload_pair = str(payload.get("pair") or payload.get("symbol") or pair).upper()
        if payload_pair == pair:
            return dict(payload)
    return None


def _component_name(payload: dict[str, Any], path: Path) -> str:
    identity = "_".join(
        str(payload.get(key) or "")
        for key in ("component", "model_name", "model_type", "role", "name")
    ).lower()
    identity = f"{identity}_{path.stem.lower()}"
    return next((name for name in _COMPONENT_NAMES if name in identity), "")


def _component_ref(payload: dict[str, Any]) -> Any:
    for key in ("artifact", "artifact_ref", "artifact_path", "artifact_dir", "model_path", "path"):
        value = payload.get(key)
        if _artifact_path(value):
            return value
    metadata = dict(payload.get("metadata") or {})
    for key in ("artifact", "artifact_ref", "artifact_path", "artifact_dir", "model_path", "path"):
        value = metadata.get(key)
        if _artifact_path(value):
            return value
    return None


def _build_pair_entry(
    *,
    pair: str,
    paths: list[Path],
    registry_root: Path,
    bundle_root: Path,
) -> dict[str, Any]:
    complete_rows: list[tuple[Path, dict[str, Any]]] = []
    component_rows: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        payload = _json_object(path)
        row = _pair_row(payload, pair)
        if row is not None:
            complete_rows.append((path, row))
        else:
            component_rows.append((path, payload))

    if complete_rows:
        selected_path, selected = sorted(complete_rows, key=lambda item: item[0].name)[-1]
        artifacts: dict[str, Any] = {}
        metadata: dict[str, Any] = {}
        registry_paths: list[str] = []
        for source_path, source_row in sorted(complete_rows, key=lambda item: item[0].name):
            for name, raw in sorted(dict(source_row.get("artifacts") or {}).items()):
                if _artifact_path(raw):
                    artifacts[str(name)] = _immutable_artifact_ref(
                        raw,
                        registry_root=registry_root,
                        bundle_root=bundle_root,
                    )
            metadata.update(dict(source_row.get("metadata") or {}))
            registry_paths.append(str(source_path.resolve()))
        feature_schema = dict(selected.get("feature_schema") or metadata.get("feature_schema") or {})
        if feature_schema:
            metadata["feature_schema"] = feature_schema
        policies = dict(selected.get("policies") or metadata.get("policies") or {})
        if policies:
            metadata["policies"] = policies
        model_set_id = str(selected.get("model_set_id") or selected_path.stem)
    else:
        artifacts: dict[str, Any] = {}
        metadata = {}
        registry_paths = []
        for path, payload in component_rows:
            component = _component_name(payload, path)
            raw_ref = _component_ref(payload)
            if not component or raw_ref is None:
                continue
            if component in artifacts:
                raise RuntimeError(f"duplicate research component for {pair}: {component}")
            artifacts[component] = _immutable_artifact_ref(
                raw_ref,
                registry_root=registry_root,
                bundle_root=bundle_root,
            )
            registry_paths.append(str(path.resolve()))
            metadata.update(dict(payload.get("model_set_metadata") or {}))
        model_set_id = f"research-{pair.lower()}-{hashlib.sha256(json.dumps(artifacts, sort_keys=True).encode('utf-8')).hexdigest()[:16]}"

    required = {"regime", "meta"}
    if not required.issubset(artifacts):
        missing = ",".join(sorted(required.difference(artifacts)))
        raise RuntimeError(f"research registry missing core artifacts for {pair}: {missing}")
    if not ({"swing_xgb", "swing_transformer"} & set(artifacts)):
        raise RuntimeError(f"research registry missing swing artifact for {pair}")
    if not ({"intraday_xgb", "intraday_tcn"} & set(artifacts)):
        raise RuntimeError(f"research registry missing intraday artifact for {pair}")

    return {
        "pair": pair,
        "enabled": True,
        "model_set_id": model_set_id,
        "registry_path": registry_paths[-1] if registry_paths else "",
        "registry_paths": sorted(registry_paths),
        "artifacts": dict(sorted(artifacts.items())),
        "metadata": metadata,
    }


def build_research_manifest(
    *,
    bundle_root: Path,
    registry_root: Path,
    manifest_path: Path,
    pairs: list[str],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a deterministic, non-activating manifest from local registry evidence."""
    bundle_root = _resolved_bundle_root(bundle_root)
    registry_root = _bundle_path(
        registry_root,
        bundle_root=bundle_root,
        label="research registry root",
        must_exist=True,
    )
    manifest_path = _bundle_path(
        manifest_path,
        bundle_root=bundle_root,
        label="research manifest path",
        must_exist=False,
    )
    normalized_pairs = list(
        dict.fromkeys(str(pair).upper().strip() for pair in pairs if str(pair).strip())
    )
    if not normalized_pairs:
        raise ValueError("at least one research pair is required")
    if not registry_root.is_dir():
        raise FileNotFoundError(f"missing research registry root: {registry_root}")

    active_model_sets: dict[str, Any] = {}
    for pair in normalized_pairs:
        paths = sorted(registry_root.glob(f"{pair.lower()}_*.json"))
        if not paths:
            paths = sorted(registry_root.glob(f"{pair.lower()}*.json"))
        if not paths:
            raise FileNotFoundError(f"research registry has no entries for {pair}: {registry_root}")
        paths = [
            _bundle_path(
                path,
                bundle_root=bundle_root,
                label="research registry entry",
                must_exist=True,
            )
            for path in paths
        ]
        active_model_sets[pair] = _build_pair_entry(
            pair=pair,
            paths=paths,
            registry_root=registry_root,
            bundle_root=bundle_root,
        )

    payload = {
        "version": RESEARCH_MANIFEST_VERSION,
        "research_only": True,
        "runtime_store_updated": False,
        "pairs": normalized_pairs,
        "bundle_root": str(bundle_root),
        "registry_root": str(registry_root),
        "active_model_sets": active_model_sets,
        "metadata": dict(metadata or {}),
    }
    payload["manifest_content_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = _bundle_path(
        manifest_path,
        bundle_root=bundle_root,
        label="research manifest path",
        must_exist=False,
    )
    temporary_path = _bundle_path(
        manifest_path.with_suffix(f"{manifest_path.suffix}.tmp"),
        bundle_root=bundle_root,
        label="research manifest temporary path",
        must_exist=False,
    )
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary_path.replace(manifest_path)
    return payload


__all__ = ["RESEARCH_MANIFEST_VERSION", "build_research_manifest"]
