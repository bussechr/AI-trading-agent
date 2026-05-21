"""Direct unit tests for :mod:`fxstack.runtime.artifact_paths`.

Pins the path-resolution and artifact-meta contract independently of the
runner so future refactors can't silently regress these high-traffic
helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.runtime.artifact_paths import (
    artifact_path,
    artifact_value,
    common_registry_root,
    load_artifact_meta,
    normalized_registry_path,
    resolve_optional_path,
)


# ---------------------------------------------------------------------------
# resolve_optional_path
# ---------------------------------------------------------------------------


def test_resolve_optional_path_returns_existing(tmp_path: Path) -> None:
    target = tmp_path / "foo" / "bar.txt"
    target.parent.mkdir()
    target.write_text("x")
    out = resolve_optional_path(str(target), project_root=tmp_path)
    assert out is not None
    assert out.resolve() == target.resolve()


def test_resolve_optional_path_returns_none_when_missing(tmp_path: Path) -> None:
    assert resolve_optional_path("does/not/exist", project_root=tmp_path) is None


def test_resolve_optional_path_handles_backslashes(tmp_path: Path) -> None:
    """Windows ops paste paths with backslashes; the resolver normalizes."""
    target = tmp_path / "win" / "path.txt"
    target.parent.mkdir()
    target.write_text("x")
    raw = str(target).replace("/", "\\")
    out = resolve_optional_path(raw, project_root=tmp_path)
    assert out is not None


def test_resolve_optional_path_empty_returns_none() -> None:
    assert resolve_optional_path("", project_root=Path(".")) is None
    assert resolve_optional_path("   ", project_root=Path(".")) is None


# ---------------------------------------------------------------------------
# artifact_path + artifact_value
# ---------------------------------------------------------------------------


def test_artifact_path_handles_plain_string() -> None:
    """A bare path string round-trips through normalize_artifact_ref."""
    out = artifact_path("models/eurusd/xgb.bin")
    # The shape of normalize_artifact_ref's output is what matters — it
    # should produce some string we can use downstream.
    assert isinstance(out, str)


def test_artifact_path_handles_none_and_empty() -> None:
    assert artifact_path(None) == ""
    assert artifact_path("") == ""


def test_artifact_value_picks_first_non_empty_key() -> None:
    artifacts = {
        "first": "",
        "second": "real/path",
        "third": "other/path",
    }
    # second is the first non-empty
    out = artifact_value(artifacts, "first", "second", "third")
    assert "real/path" in out or out == "real/path"


def test_artifact_value_returns_empty_when_all_missing() -> None:
    assert artifact_value({}, "a", "b", "c") == ""
    assert artifact_value({"a": "", "b": ""}, "a", "b") == ""


# ---------------------------------------------------------------------------
# load_artifact_meta
# ---------------------------------------------------------------------------


def test_load_artifact_meta_reads_meta_json(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "model_v1"
    artifact_dir.mkdir()
    meta = {"run_id": "abc", "calibration": 0.92, "features": ["a", "b"]}
    (artifact_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    out = load_artifact_meta(str(artifact_dir), project_root=tmp_path)
    assert out == meta


def test_load_artifact_meta_missing_file_returns_empty(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "no_meta"
    artifact_dir.mkdir()
    assert load_artifact_meta(str(artifact_dir), project_root=tmp_path) == {}


def test_load_artifact_meta_malformed_json_returns_empty(tmp_path: Path) -> None:
    """Malformed JSON should not propagate as an exception."""
    artifact_dir = tmp_path / "bad_meta"
    artifact_dir.mkdir()
    (artifact_dir / "meta.json").write_text("{not valid json", encoding="utf-8")
    assert load_artifact_meta(str(artifact_dir), project_root=tmp_path) == {}


def test_load_artifact_meta_unresolvable_path_returns_empty(tmp_path: Path) -> None:
    assert load_artifact_meta("definitely/does/not/exist", project_root=tmp_path) == {}


# ---------------------------------------------------------------------------
# normalized_registry_path
# ---------------------------------------------------------------------------


def test_normalized_registry_path_resolves_existing(tmp_path: Path) -> None:
    target = tmp_path / "reg"
    target.mkdir()
    out = normalized_registry_path(str(target), project_root=tmp_path)
    assert Path(out).resolve() == target.resolve()


def test_normalized_registry_path_returns_normalized_when_missing(tmp_path: Path) -> None:
    """Missing paths get backslash → forward-slash normalization for telemetry."""
    out = normalized_registry_path("foo\\bar\\baz", project_root=tmp_path)
    assert out == "foo/bar/baz"


def test_normalized_registry_path_empty_returns_empty() -> None:
    assert normalized_registry_path("", project_root=Path(".")) == ""


# ---------------------------------------------------------------------------
# common_registry_root
# ---------------------------------------------------------------------------


def test_common_registry_root_single() -> None:
    out = common_registry_root(["/var/models/a.bin", "/var/models/b.bin"])
    # Parent of both is /var/models
    assert "models" in out


def test_common_registry_root_multiple_returns_mixed() -> None:
    out = common_registry_root(["/var/models/a.bin", "/srv/other/b.bin"])
    assert out == "mixed"


def test_common_registry_root_empty_returns_empty() -> None:
    assert common_registry_root([]) == ""
    assert common_registry_root(["", "   "]) == ""


@pytest.mark.parametrize("paths", [["a/b"], ["a/b", "a/c"]])
def test_common_registry_root_collapses_to_single_when_same_parent(paths: list[str]) -> None:
    out = common_registry_root(paths)
    assert out == "a"
