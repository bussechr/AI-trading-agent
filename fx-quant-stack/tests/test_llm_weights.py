"""Unit tests for :mod:`fxstack.llm.weights`.

These tests never touch the network. They pin the offline-first checksum
contract: pre-stage weights, verify their sha256, and refuse egress by default.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fxstack.llm.weights import (
    WeightArtifact,
    WeightError,
    WeightManifest,
    download_artifact,
    load_manifest,
    save_manifest,
    sha256_file,
    verify_artifact,
    verify_manifest,
)

_PAYLOAD = b"the quick brown fox jumps over the lazy dog\n" * 4096


def _stage(tmp_path: Path, name: str = "model.bin", data: bytes = _PAYLOAD) -> tuple[Path, str]:
    """Write ``data`` to ``tmp_path/name`` and return (path, expected_sha256)."""
    file_path = tmp_path / name
    file_path.write_bytes(data)
    return file_path, hashlib.sha256(data).hexdigest()


def _artifact(file_path: Path, sha: str, *, size: int = 0, uri: str = "") -> WeightArtifact:
    return WeightArtifact(
        name="weights-v1",
        uri=uri,
        sha256=sha,
        path=str(file_path),
        size_bytes=size,
    )


# ---------------------------------------------------------------------------
# sha256_file
# ---------------------------------------------------------------------------


def test_sha256_file_matches_hashlib(tmp_path: Path) -> None:
    file_path, expected = _stage(tmp_path)
    assert sha256_file(file_path) == expected
    # And matches a fresh hashlib computation on the bytes.
    assert sha256_file(file_path) == hashlib.sha256(file_path.read_bytes()).hexdigest()


def test_sha256_file_empty_file(tmp_path: Path) -> None:
    file_path, expected = _stage(tmp_path, "empty.bin", b"")
    assert sha256_file(file_path) == expected
    assert sha256_file(file_path) == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# verify_artifact
# ---------------------------------------------------------------------------


def test_verify_artifact_passes_on_correct_hash(tmp_path: Path) -> None:
    file_path, sha = _stage(tmp_path)
    ok, reason = verify_artifact(_artifact(file_path, sha))
    assert ok is True
    assert reason == ""


def test_verify_artifact_passes_with_correct_size(tmp_path: Path) -> None:
    file_path, sha = _stage(tmp_path)
    ok, reason = verify_artifact(_artifact(file_path, sha, size=len(_PAYLOAD)))
    assert ok is True
    assert reason == ""


def test_verify_artifact_fails_on_wrong_hash(tmp_path: Path) -> None:
    file_path, _ = _stage(tmp_path)
    wrong = "0" * 64
    ok, reason = verify_artifact(_artifact(file_path, wrong))
    assert ok is False
    assert "sha256 mismatch" in reason


def test_verify_artifact_fails_on_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.bin"
    ok, reason = verify_artifact(_artifact(missing, "a" * 64))
    assert ok is False
    assert "missing file" in reason


def test_verify_artifact_fails_on_size_mismatch(tmp_path: Path) -> None:
    file_path, sha = _stage(tmp_path)
    ok, reason = verify_artifact(_artifact(file_path, sha, size=len(_PAYLOAD) + 1))
    assert ok is False
    assert "size mismatch" in reason


def test_verify_artifact_fails_on_directory(tmp_path: Path) -> None:
    a_dir = tmp_path / "a_dir"
    a_dir.mkdir()
    ok, reason = verify_artifact(_artifact(a_dir, "b" * 64))
    assert ok is False
    assert "not a regular file" in reason


def test_verify_artifact_case_insensitive_expected_hash(tmp_path: Path) -> None:
    file_path, sha = _stage(tmp_path)
    ok, _ = verify_artifact(_artifact(file_path, sha.upper()))
    assert ok is True


# ---------------------------------------------------------------------------
# verify_manifest
# ---------------------------------------------------------------------------


def test_verify_manifest_all_ok(tmp_path: Path) -> None:
    p1, s1 = _stage(tmp_path, "a.bin", b"alpha")
    p2, s2 = _stage(tmp_path, "b.bin", b"beta")
    manifest = WeightManifest(
        artifacts=[
            WeightArtifact(name="a", sha256=s1, path=str(p1)),
            WeightArtifact(name="b", sha256=s2, path=str(p2)),
        ]
    )
    report = verify_manifest(manifest)
    assert report["ok"] is True
    assert report["total"] == 2
    assert report["verified"] == 2
    assert report["failed"] == 0
    assert {row["name"] for row in report["artifacts"]} == {"a", "b"}


def test_verify_manifest_reports_failures(tmp_path: Path) -> None:
    p1, s1 = _stage(tmp_path, "a.bin", b"alpha")
    manifest = WeightManifest(
        artifacts=[
            WeightArtifact(name="a", sha256=s1, path=str(p1)),
            WeightArtifact(name="b", sha256="f" * 64, path=str(tmp_path / "missing.bin")),
        ]
    )
    report = verify_manifest(manifest)
    assert report["ok"] is False
    assert report["verified"] == 1
    assert report["failed"] == 1
    failing = next(r for r in report["artifacts"] if r["name"] == "b")
    assert failing["ok"] is False
    assert failing["reason"]


def test_verify_manifest_empty_is_ok() -> None:
    report = verify_manifest(WeightManifest())
    assert report == {"ok": True, "total": 0, "verified": 0, "failed": 0, "artifacts": []}


# ---------------------------------------------------------------------------
# manifest round-trip + schema
# ---------------------------------------------------------------------------


def test_manifest_round_trips(tmp_path: Path) -> None:
    p1, s1 = _stage(tmp_path, "a.bin", b"alpha")
    manifest = WeightManifest(
        version=1,
        artifacts=[
            WeightArtifact(
                name="a",
                uri="s3://bucket/a.bin",
                sha256=s1,
                path=str(p1),
                size_bytes=len(b"alpha"),
            )
        ],
    )
    out = tmp_path / "nested" / "manifest.json"
    save_manifest(manifest, out)
    assert out.exists()

    loaded = load_manifest(out)
    assert loaded == manifest
    assert loaded.by_name("a") is not None
    assert loaded.by_name("missing") is None


def test_manifest_forbids_extra_fields(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "artifacts": [],
        "unexpected": True,
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_manifest(path)


def test_artifact_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        WeightArtifact(name="a", sha256="0" * 64, path="x", bogus=1)  # type: ignore[call-arg]


def test_artifact_default_size_is_zero() -> None:
    art = WeightArtifact(name="a", sha256="0" * 64, path="x")
    assert art.size_bytes == 0
    assert art.uri == ""


# ---------------------------------------------------------------------------
# download_artifact (offline-first)
# ---------------------------------------------------------------------------


def test_download_artifact_missing_file_refuses_without_network(tmp_path: Path) -> None:
    missing = tmp_path / "nope.bin"
    art = _artifact(missing, "a" * 64)
    with pytest.raises(WeightError) as exc:
        download_artifact(art, allow_network=False)
    msg = str(exc.value)
    assert "not staged" in msg
    assert "network access is disabled" in msg


def test_download_artifact_default_is_offline(tmp_path: Path) -> None:
    """The default (no kwarg) must behave like allow_network=False."""
    art = _artifact(tmp_path / "nope.bin", "a" * 64)
    with pytest.raises(WeightError):
        download_artifact(art)


def test_download_artifact_existing_correct_file_returns_verified(tmp_path: Path) -> None:
    file_path, sha = _stage(tmp_path)
    art = _artifact(file_path, sha, size=len(_PAYLOAD))
    ok, status = download_artifact(art, allow_network=False)
    assert ok is True
    assert status == "verified"


def test_download_artifact_existing_correct_file_verified_even_with_network(tmp_path: Path) -> None:
    """An existing correct file is verified without any fetch, regardless of flag."""
    file_path, sha = _stage(tmp_path)
    ok, status = download_artifact(_artifact(file_path, sha), allow_network=True)
    assert ok is True
    assert status == "verified"


def test_download_artifact_existing_bad_file_raises(tmp_path: Path) -> None:
    file_path, _ = _stage(tmp_path)
    art = _artifact(file_path, "c" * 64)
    with pytest.raises(WeightError) as exc:
        download_artifact(art, allow_network=False)
    assert "failed verification" in str(exc.value)


def test_download_artifact_missing_with_network_optin_not_implemented(tmp_path: Path) -> None:
    """allow_network=True is an explicit opt-in but staging stays manual."""
    art = _artifact(tmp_path / "nope.bin", "a" * 64, uri="https://example.invalid/w.bin")
    with pytest.raises(WeightError) as exc:
        download_artifact(art, allow_network=True)
    assert "not implemented" in str(exc.value)
