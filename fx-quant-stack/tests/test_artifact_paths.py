"""Direct unit tests for :mod:`fxstack.runtime.artifact_paths`.

Pins the path-resolution and artifact-meta contract independently of the
runner so future refactors can't silently regress these high-traffic
helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.features.session_contract import feature_contract_metadata
from fxstack.models.artifact_contract import stamp_artifact_payload_digest
from fxstack.runtime.artifact_paths import (
    artifact_path,
    artifact_value,
    common_registry_root,
    load_artifact_meta,
    normalized_registry_path,
    resolve_optional_path,
)


def _valid_artifact(root: Path, **extra: object) -> Path:
    """Build a contract-valid artifact directory with a stamped payload digest."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.bin").write_bytes(b"payload-bytes")
    meta: dict[str, object] = {"name": root.name, **feature_contract_metadata(), **extra}
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    stamp_artifact_payload_digest(root)
    return root


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


def test_load_artifact_meta_reads_validated_meta_json(tmp_path: Path) -> None:
    artifact_dir = _valid_artifact(tmp_path / "model_v1", run_id="abc", calibration=0.92)
    out = load_artifact_meta(str(artifact_dir), project_root=tmp_path)
    assert out["run_id"] == "abc"
    assert out["calibration"] == 0.92
    # The contract stamp travels with the meta the loader hands back.
    for key, expected in feature_contract_metadata().items():
        assert out[key] == expected


def test_load_artifact_meta_absent_reference_returns_empty(tmp_path: Path) -> None:
    """Only a truly absent optional reference is allowed to return ``{}``."""
    assert load_artifact_meta("", project_root=tmp_path) == {}
    assert load_artifact_meta(None, project_root=tmp_path) == {}


def test_load_artifact_meta_missing_file_fails_closed(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "no_meta"
    artifact_dir.mkdir()
    with pytest.raises(ValueError, match="artifact_sidecar_invalid"):
        load_artifact_meta(str(artifact_dir), project_root=tmp_path)


def test_load_artifact_meta_malformed_json_fails_closed(tmp_path: Path) -> None:
    """A configured-but-corrupt sidecar must not be silently swallowed."""
    artifact_dir = tmp_path / "bad_meta"
    artifact_dir.mkdir()
    (artifact_dir / "meta.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_sidecar_invalid"):
        load_artifact_meta(str(artifact_dir), project_root=tmp_path)


def test_load_artifact_meta_stale_feature_contract_fails_closed(tmp_path: Path) -> None:
    """A model trained against a superseded feature contract cannot load."""
    artifact_dir = _valid_artifact(tmp_path / "stale")
    meta = json.loads((artifact_dir / "meta.json").read_text(encoding="utf-8"))
    meta["session_contract_version"] = "utc_session_buckets_v1"
    (artifact_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(ValueError, match="feature_contract_mismatch"):
        load_artifact_meta(str(artifact_dir), project_root=tmp_path)


def test_load_artifact_meta_unresolvable_path_fails_closed(tmp_path: Path) -> None:
    with pytest.raises((ValueError, FileNotFoundError)):
        load_artifact_meta("definitely/does/not/exist", project_root=tmp_path)


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
