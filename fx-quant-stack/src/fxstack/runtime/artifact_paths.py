"""Path + artifact-reference helpers extracted from ``fxstack.runtime.runner``.

Continues the pattern of carving self-contained chunks out of the 9k-line
runner module. These seven helpers are the high-traffic utilities every
model-loading and activation code path uses to:

* Resolve a raw path string (possibly relative to project root) to an
  absolute :class:`pathlib.Path`.
* Pull a usable file/dir path out of an artifact reference (MLflow URI,
  filesystem path, dict envelope).
* Read the ``meta.json`` sidecar that activation packages drop alongside
  every model artifact.
* Normalize registry paths and collapse a set of paths to a single common
  root for telemetry.

All seven are pure functions over their inputs (no clock reads, no state).
The runner.py re-imports each under its original underscored name so the
~45 internal call sites continue to work unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fxstack.mlops.model_uri import normalize_artifact_ref, resolve_model_artifact_path
from fxstack.models.artifact_contract import artifact_lock, validate_artifact_contract


def resolve_path(raw: str, project_root: Path) -> Path:
    """Thin wrapper over :func:`resolve_model_artifact_path` for the common case.

    Kept as a one-liner because runner.py and downstream call sites passed
    raw path strings without consulting the artifact-ref normalizer.
    """
    return resolve_model_artifact_path(raw, project_root=project_root)


def resolve_optional_path(raw: str, project_root: Path) -> Path | None:
    """Try ``raw``, its forward-slash form, and project-root-anchored variants.

    Returns the first existing :class:`Path` (resolved), or ``None`` if no
    variant exists on disk. Tolerant of Windows backslashes — the path
    layer normalizes them before checking.
    """
    txt = str(raw or "").strip()
    if not txt:
        return None
    variants = [txt]
    normalized = txt.replace("\\", "/")
    if normalized != txt:
        variants.append(normalized)
    for value in variants:
        p = Path(value).expanduser()
        for cand in (p, project_root / p, project_root.parent / p):
            if cand.exists():
                return cand.resolve()
    return None


def artifact_path(raw: Any) -> str:
    """Pull the ``path`` (or ``model_uri``) string out of an artifact reference."""
    ref = normalize_artifact_ref(raw)
    return str(ref.get("path") or ref.get("model_uri") or "")


def artifact_value(artifacts: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty artifact path under any of ``keys``."""
    for key in keys:
        value = artifact_path(artifacts.get(key))
        if value.strip():
            return value
    return ""


def load_artifact_meta(raw_path: Any, project_root: Path) -> dict[str, Any]:
    """Read validated ``meta.json`` under the artifact's cooperative lock.

    Activation packages drop a ``meta.json`` next to every model artifact
    that captures the training run id, calibration scores, and feature
    column list. Only a truly absent optional reference returns ``{}``;
    configured refs fail closed if resolution or integrity validation fails.
    """
    ref = normalize_artifact_ref(raw_path)
    if not str(ref.get("path") or ref.get("model_uri") or "").strip():
        return {}
    expected_digest = (
        str(ref.get("artifact_hash") or "").strip().lower()
        if isinstance(raw_path, dict)
        else None
    )
    path = resolve_model_artifact_path(raw_path, project_root=project_root)
    label = f"artifact_meta:{path}"
    with artifact_lock(path):
        meta = validate_artifact_contract(
            path,
            label=label,
            expected_digest=expected_digest,
        )
        validate_artifact_contract(
            path,
            label=label,
            expected_digest=expected_digest,
        )
        return meta


def normalized_registry_path(raw: str, *, project_root: Path) -> str:
    """Best-effort canonicalization of a registry path for telemetry."""
    txt = str(raw or "").strip()
    if not txt:
        return ""
    resolved = resolve_optional_path(txt, project_root)
    if resolved is not None:
        return str(resolved)
    return txt.replace("\\", "/")


def common_registry_root(paths: list[str]) -> str:
    """Return the single common parent of ``paths``, or ``"mixed"`` if multiple.

    Used to summarize active model sets on the dashboard — if every pair
    pulls from the same registry root, show that; if they diverge, show
    ``"mixed"`` rather than picking one arbitrarily.
    """
    roots = {str(Path(p).parent) for p in paths if str(p).strip()}
    if not roots:
        return ""
    if len(roots) == 1:
        return next(iter(roots))
    return "mixed"


__all__ = [
    "artifact_path",
    "artifact_value",
    "common_registry_root",
    "load_artifact_meta",
    "normalized_registry_path",
    "resolve_optional_path",
    "resolve_path",
]
