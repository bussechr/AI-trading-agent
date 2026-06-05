"""Offline-first model weight manager (download-once + checksum verify).

Design principle: operators *pre-stage* model weights and then **block egress**.
This module never reaches the network on import or on the default code path. A
:class:`WeightManifest` records the expected sha256 of each artifact so the
runtime can prove that a locally staged file is the exact weight it expects
before loading it.

Security posture:
- :func:`download_artifact` refuses to touch the network unless the caller
  explicitly passes ``allow_network=True``. The default raises a clear error
  telling the operator how to pre-stage the file.
- When a staged file already exists, ``download_artifact`` only re-verifies its
  checksum -- it never re-downloads and never mutates the file.
- No secrets are logged; we deal only in paths, sizes and digests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "WeightArtifact",
    "WeightManifest",
    "WeightError",
    "sha256_file",
    "verify_artifact",
    "verify_manifest",
    "load_manifest",
    "save_manifest",
    "download_artifact",
]

# 1 MiB read window keeps memory flat for multi-GB weight files.
_CHUNK_SIZE = 1 << 20


class WeightError(RuntimeError):
    """Raised for weight-management failures (missing file, network refusal)."""


class WeightArtifact(BaseModel):
    """A single model-weight file the runtime expects to find on disk.

    ``uri`` records where the operator originally staged the file from (e.g. an
    ``s3://`` or ``https://`` location) purely for documentation; it is never
    fetched unless the operator opts into networking explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Logical artifact name (unique within a manifest).")
    uri: str = Field("", description="Provenance URI the file was staged from; never fetched by default.")
    sha256: str = Field(..., description="Expected lowercase hex sha256 digest of the file.")
    path: str = Field(..., min_length=1, description="Local path where the staged weight lives.")
    size_bytes: int = Field(0, ge=0, description="Expected file size in bytes; 0 means unknown/unchecked.")

    def resolved_path(self) -> Path:
        """Return ``path`` as a :class:`~pathlib.Path` (no filesystem access)."""

        return Path(self.path)

    def normalized_sha256(self) -> str:
        """Lowercase, whitespace-stripped expected digest."""

        return str(self.sha256 or "").strip().lower()


class WeightManifest(BaseModel):
    """An ordered collection of :class:`WeightArtifact` entries."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(1, ge=1, description="Manifest schema version.")
    artifacts: list[WeightArtifact] = Field(default_factory=list)

    def by_name(self, name: str) -> WeightArtifact | None:
        """Return the artifact named ``name`` or ``None``."""

        for art in self.artifacts:
            if art.name == name:
                return art
        return None


def sha256_file(path: str | Path) -> str:
    """Return the lowercase hex sha256 digest of the file at ``path``.

    Reads the file in fixed-size chunks so arbitrarily large weight files stay
    within a bounded memory budget.
    """

    digest = hashlib.sha256()
    file_path = Path(path)
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(artifact: WeightArtifact) -> tuple[bool, str]:
    """Verify a staged artifact against its manifest entry.

    Returns ``(ok, reason)``. ``ok`` is ``True`` only when the file exists and
    its sha256 (and, when declared, its size) matches the manifest. ``reason``
    is empty on success and a human-readable explanation otherwise.
    """

    expected = artifact.normalized_sha256()
    if not expected:
        return False, "manifest entry has no expected sha256"

    file_path = artifact.resolved_path()
    if not file_path.exists():
        return False, f"missing file: {file_path}"
    if not file_path.is_file():
        return False, f"not a regular file: {file_path}"

    if artifact.size_bytes:
        actual_size = file_path.stat().st_size
        if actual_size != artifact.size_bytes:
            return False, f"size mismatch: expected {artifact.size_bytes} bytes, got {actual_size}"

    actual = sha256_file(file_path)
    if actual != expected:
        return False, f"sha256 mismatch: expected {expected}, got {actual}"
    return True, ""


def verify_manifest(manifest: WeightManifest) -> dict[str, Any]:
    """Verify every artifact in ``manifest`` and return a structured report.

    The report has the shape::

        {
            "ok": bool,               # True iff every artifact verified
            "total": int,
            "verified": int,
            "failed": int,
            "artifacts": [
                {"name": str, "ok": bool, "reason": str, "path": str},
                ...
            ],
        }
    """

    rows: list[dict[str, Any]] = []
    verified = 0
    for art in manifest.artifacts:
        ok, reason = verify_artifact(art)
        verified += int(ok)
        rows.append({"name": art.name, "ok": ok, "reason": reason, "path": art.path})
    total = len(manifest.artifacts)
    return {
        "ok": verified == total,
        "total": total,
        "verified": verified,
        "failed": total - verified,
        "artifacts": rows,
    }


def load_manifest(path: str | Path) -> WeightManifest:
    """Load and validate a :class:`WeightManifest` from a JSON file."""

    raw = Path(path).read_text(encoding="utf-8")
    return WeightManifest.model_validate_json(raw)


def save_manifest(manifest: WeightManifest, path: str | Path) -> None:
    """Write ``manifest`` to ``path`` as pretty-printed JSON."""

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True)
    out_path.write_text(text + "\n", encoding="utf-8")


def download_artifact(artifact: WeightArtifact, *, allow_network: bool = False) -> tuple[bool, str]:
    """Ensure ``artifact`` is present and verified on disk.

    Offline-first contract:

    - If the file already exists, this only re-verifies its checksum and never
      touches the network. Returns ``(True, "verified")`` on a match and raises
      :class:`WeightError` on a checksum/size mismatch (a mismatching staged
      file is a hard error, not something to silently re-fetch).
    - If the file is missing and ``allow_network`` is ``False`` (the default),
      this raises :class:`WeightError` telling the operator to pre-stage the
      weight. It never opens a socket on the default path.
    - If the file is missing and ``allow_network`` is ``True``, network fetching
      is still intentionally not implemented here: operators are expected to
      stage weights out-of-band. We raise a clear, distinct error so an explicit
      opt-in can never be mistaken for a silent no-op.
    """

    file_path = artifact.resolved_path()

    if file_path.exists():
        ok, reason = verify_artifact(artifact)
        if ok:
            return True, "verified"
        raise WeightError(
            f"staged weight {artifact.name!r} at {file_path} failed verification: {reason}; "
            "re-stage the correct file and confirm its sha256"
        )

    if not allow_network:
        raise WeightError(
            f"weight {artifact.name!r} is not staged at {file_path} and network access is disabled. "
            "Pre-stage the file out-of-band (e.g. copy it from your secured artifact store), "
            f"verify its sha256 == {artifact.normalized_sha256() or '<unknown>'}, then retry. "
            "Pass allow_network=True only in an environment that is permitted egress."
        )

    # Explicit opt-in, but we deliberately do not embed a network client here:
    # staging is an operator responsibility so egress stays auditable.
    raise WeightError(
        f"network staging for {artifact.name!r} from {artifact.uri or '<no uri>'} is not implemented; "
        "stage the file manually and re-run with the file present so its checksum can be verified"
    )
