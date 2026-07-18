"""Fail-closed model artifact metadata and payload integrity contracts."""

from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from filelock import FileLock

from fxstack.features.session_contract import feature_contract_mismatches


ARTIFACT_PAYLOAD_CONTRACT_VERSION = "relative_path_bytes_canonical_meta_sha256_v2"
ARTIFACT_PAYLOAD_CONTRACT_KEY = "artifact_payload_contract"
ARTIFACT_PAYLOAD_DIGEST_KEY = "artifact_payload_sha256"
_DIGEST_META_KEYS = frozenset(
    {ARTIFACT_PAYLOAD_CONTRACT_KEY, ARTIFACT_PAYLOAD_DIGEST_KEY}
)
_LOCKS: dict[str, FileLock] = {}
_LOCKS_GUARD = threading.Lock()


def artifact_lock_path(path: str | Path) -> Path:
    root = Path(path).expanduser().absolute()
    return root.parent / f".{root.name}.artifact.lock"


def _artifact_file_lock(path: str | Path) -> FileLock:
    lock_path = artifact_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(lock_path))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = FileLock(str(lock_path), timeout=60.0)
            _LOCKS[key] = lock
        return lock


@contextmanager
def artifact_lock(path: str | Path):
    """Hold the cooperative cross-process lock for one artifact directory."""

    with _artifact_file_lock(path):
        yield


def artifact_io_locked(func):
    """Keep a model save/load method under its artifact lock."""

    @wraps(func)
    def _wrapped(*args, **kwargs):
        raw_path = kwargs.get(
            "path",
            kwargs.get("raw_path", kwargs.get("artifact_path")),
        )
        if raw_path is None:
            if not args:
                raise TypeError("artifact path is required")
            raw_path = args[-1]
        with artifact_lock(raw_path):
            return func(*args, **kwargs)

    return _wrapped


def _artifact_files(path: Path) -> list[Path]:
    if path.is_symlink():
        raise ValueError(f"artifact_symlink_rejected:{path}")
    if not path.is_dir():
        raise ValueError(f"artifact_directory_missing:{path}")
    files: list[Path] = []
    for child in path.rglob("*"):
        if child.is_symlink():
            raise ValueError(
                f"artifact_symlink_rejected:{child.relative_to(path).as_posix()}"
            )
        if not child.is_file():
            continue
        files.append(child)
    return sorted(files, key=lambda item: item.relative_to(path).as_posix())


def _canonical_meta_bytes(path: Path, *, root: Path) -> bytes:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            f"artifact_meta_invalid:{path.relative_to(root).as_posix()}"
        ) from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError(
            f"artifact_meta_invalid:{path.relative_to(root).as_posix()}"
        )
    canonical = {
        str(key): value
        for key, value in payload.items()
        if str(key) not in _DIGEST_META_KEYS
    }
    try:
        return json.dumps(
            canonical,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"artifact_meta_not_canonicalizable:{path.relative_to(root).as_posix()}"
        ) from exc


def _hash_file_bytes(digest: Any, child: Path) -> None:
    before = child.stat()
    digest.update(int(before.st_size).to_bytes(8, "big"))
    bytes_read = 0
    with child.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            bytes_read += len(chunk)
            digest.update(chunk)
    after = child.stat()
    if (
        child.is_symlink()
        or bytes_read != int(before.st_size)
        or int(after.st_size) != int(before.st_size)
        or int(after.st_mtime_ns) != int(before.st_mtime_ns)
    ):
        raise ValueError(f"artifact_payload_changed_during_hash:{child}")


def _artifact_payload_digest_unlocked(path: str | Path) -> str:
    """Hash portable paths, raw payload bytes, and canonical semantic metadata."""

    root = Path(path)
    files = _artifact_files(root)
    if not any(child.name != "meta.json" for child in files):
        raise ValueError(f"artifact_payload_missing:{root}")
    digest = hashlib.sha256()
    digest.update(f"fxstack:{ARTIFACT_PAYLOAD_CONTRACT_VERSION}\0".encode("utf-8"))
    for child in files:
        relative_bytes = child.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        if child.name == "meta.json":
            content = _canonical_meta_bytes(child, root=root)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        else:
            _hash_file_bytes(digest, child)
    return digest.hexdigest()


def artifact_payload_digest(path: str | Path) -> str:
    with artifact_lock(path):
        return _artifact_payload_digest_unlocked(path)


def _load_meta(path: Path, *, label: str) -> dict[str, Any]:
    meta_path = path / "meta.json"
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"artifact_sidecar_invalid:{label}:{meta_path}") from exc
    if not isinstance(payload, dict) or not payload:
        raise ValueError(f"artifact_sidecar_invalid:{label}:{meta_path}")
    return dict(payload)


def _validate_artifact_contract_unlocked(
    path: str | Path,
    *,
    label: str,
    expected_digest: str | None = None,
    expected_name: str | None = None,
) -> dict[str, Any]:
    """Validate metadata versions and payload bytes before model deserialization."""

    root = Path(path)
    meta = _load_meta(root, label=label)
    mismatches = feature_contract_mismatches(meta)
    if mismatches:
        detail = ",".join(
            f"{key}=expected:{expected}|actual:{actual or '<missing>'}"
            for key, (expected, actual) in sorted(mismatches.items())
        )
        raise ValueError(f"feature_contract_mismatch:{label}:{detail}; retraining is required")

    payload_contract = str(meta.get(ARTIFACT_PAYLOAD_CONTRACT_KEY) or "").strip()
    if payload_contract != ARTIFACT_PAYLOAD_CONTRACT_VERSION:
        raise ValueError(
            f"artifact_payload_contract_mismatch:{label}:"
            f"expected:{ARTIFACT_PAYLOAD_CONTRACT_VERSION}|actual:{payload_contract or '<missing>'}; "
            "retraining is required"
        )
    sidecar_digest = str(meta.get(ARTIFACT_PAYLOAD_DIGEST_KEY) or "").strip().lower()
    if not sidecar_digest:
        raise ValueError(f"artifact_payload_digest_missing:{label}:{root}; retraining is required")
    if expected_digest is not None:
        registered_digest = str(expected_digest).strip().lower()
        if not registered_digest:
            raise ValueError(
                f"artifact_registry_hash_missing:{label}:{root}; registration is required"
            )
        if not hmac.compare_digest(registered_digest, sidecar_digest):
            raise ValueError(
                f"artifact_registry_hash_mismatch:{label}:"
                f"expected:{registered_digest}|sidecar:{sidecar_digest}"
            )
    actual_digest = _artifact_payload_digest_unlocked(root)
    if not hmac.compare_digest(sidecar_digest, actual_digest):
        raise ValueError(
            f"artifact_payload_digest_mismatch:{label}:"
            f"expected:{sidecar_digest}|actual:{actual_digest}; retraining is required"
        )
    if expected_name is not None:
        actual_name = str(meta.get("name") or "").strip()
        required_name = str(expected_name or "").strip()
        if not required_name or actual_name != required_name:
            raise ValueError(
                f"artifact_model_name_mismatch:{label}:"
                f"expected:{required_name or '<missing>'}|actual:{actual_name or '<missing>'}"
            )
    return meta


def validate_artifact_contract(
    path: str | Path,
    *,
    label: str,
    expected_digest: str | None = None,
    expected_name: str | None = None,
) -> dict[str, Any]:
    with artifact_lock(path):
        return _validate_artifact_contract_unlocked(
            path,
            label=label,
            expected_digest=expected_digest,
            expected_name=expected_name,
        )


def _write_meta_atomic(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def stamp_artifact_payload_digest(path: str | Path) -> dict[str, Any]:
    """Stamp a digest after a real model save has written all payload files."""

    root = Path(path)
    with artifact_lock(root):
        meta = _load_meta(root, label=str(root))
        meta[ARTIFACT_PAYLOAD_CONTRACT_KEY] = ARTIFACT_PAYLOAD_CONTRACT_VERSION
        meta.pop(ARTIFACT_PAYLOAD_DIGEST_KEY, None)
        _write_meta_atomic(root / "meta.json", meta)
        meta[ARTIFACT_PAYLOAD_DIGEST_KEY] = _artifact_payload_digest_unlocked(root)
        _write_meta_atomic(root / "meta.json", meta)
        return _validate_artifact_contract_unlocked(root, label=str(root))


__all__ = [
    "ARTIFACT_PAYLOAD_CONTRACT_KEY",
    "ARTIFACT_PAYLOAD_CONTRACT_VERSION",
    "ARTIFACT_PAYLOAD_DIGEST_KEY",
    "artifact_lock",
    "artifact_lock_path",
    "artifact_io_locked",
    "artifact_payload_digest",
    "stamp_artifact_payload_digest",
    "validate_artifact_contract",
]
