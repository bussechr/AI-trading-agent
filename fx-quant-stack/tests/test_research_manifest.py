from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.training.research_manifest import (
    RESEARCH_MANIFEST_VERSION,
    build_research_manifest,
)


def _artifact(tmp_path: Path, name: str) -> Path:
    path = tmp_path / "artifacts" / name
    path.mkdir(parents=True)
    (path / "model.bin").write_bytes(f"artifact:{name}".encode("utf-8"))
    return path


def test_build_research_manifest_is_deterministic_and_non_activating(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    registry_root.mkdir()
    artifacts = {
        name: {"path": str(_artifact(tmp_path, name))}
        for name in ("regime", "meta", "swing_xgb", "intraday_xgb")
    }
    artifacts["meta"].update(
        {
            "model_uri": "models:/production.meta@champion",
            "evidence_refs": {
                "artifact_path": str(tmp_path.parent / "production-artifacts" / "meta")
            },
        }
    )
    (registry_root / "eurusd_bundle.json").write_text(
        json.dumps(
            {
                "pair": "EURUSD",
                "model_set_id": "candidate-001",
                "artifacts": artifacts,
                "metadata": {
                    "feature_schema": {"schema_version": "test"},
                    "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
                },
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "research_models.json"

    first = build_research_manifest(
        bundle_root=tmp_path,
        registry_root=registry_root,
        manifest_path=manifest_path,
        pairs=["EURUSD"],
        metadata={"window": "test"},
    )
    first_bytes = manifest_path.read_bytes()
    second = build_research_manifest(
        bundle_root=tmp_path,
        registry_root=registry_root,
        manifest_path=manifest_path,
        pairs=["EURUSD"],
        metadata={"window": "test"},
    )

    assert first == second
    assert manifest_path.read_bytes() == first_bytes
    assert first["version"] == RESEARCH_MANIFEST_VERSION
    assert first["research_only"] is True
    assert first["runtime_store_updated"] is False
    assert first["bundle_root"] == str(tmp_path.resolve())
    assert len(first["manifest_content_sha256"]) == 64
    entry = first["active_model_sets"]["EURUSD"]
    assert entry["enabled"] is True
    assert set(entry["artifacts"]) == set(artifacts)
    for artifact in entry["artifacts"].values():
        assert Path(artifact["path"]).is_absolute()
        assert Path(artifact["path"]).resolve().is_relative_to(tmp_path.resolve())
        assert set(artifact) <= {"path", "artifact_hash", "research_content_sha256"}
        assert len(artifact["research_content_sha256"]) == 64
    assert "models:/production.meta@champion" not in manifest_path.read_text(encoding="utf-8")
    assert "production-artifacts" not in manifest_path.read_text(encoding="utf-8")


def _write_registry(registry_root: Path, artifacts: dict[str, dict[str, str]]) -> None:
    registry_root.mkdir(parents=True, exist_ok=True)
    (registry_root / "eurusd_bundle.json").write_text(
        json.dumps({"pair": "EURUSD", "artifacts": artifacts}),
        encoding="utf-8",
    )


def test_research_manifest_requires_explicit_bundle_root(tmp_path: Path) -> None:
    registry_root = tmp_path / "registry"
    artifacts = {
        name: {"path": str(_artifact(tmp_path, name))}
        for name in ("regime", "meta", "swing_xgb", "intraday_xgb")
    }
    _write_registry(registry_root, artifacts)

    with pytest.raises(TypeError, match="bundle_root"):
        build_research_manifest(  # type: ignore[call-arg]
            registry_root=registry_root,
            manifest_path=tmp_path / "research_models.json",
            pairs=["EURUSD"],
        )


@pytest.mark.parametrize("escaped_path", ["manifest", "registry", "artifact"])
def test_research_manifest_rejects_paths_outside_bundle(
    tmp_path: Path,
    escaped_path: str,
) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    registry_root = bundle_root / "registry"
    manifest_path = bundle_root / "research_models.json"
    artifacts = {
        name: {"path": str(_artifact(bundle_root, name))}
        for name in ("regime", "meta", "swing_xgb", "intraday_xgb")
    }
    if escaped_path == "manifest":
        manifest_path = outside_root / "research_models.json"
    elif escaped_path == "registry":
        registry_root = outside_root / "registry"
    else:
        artifacts["meta"] = {"path": str(_artifact(outside_root, "external_meta"))}
    _write_registry(registry_root, artifacts)

    with pytest.raises(ValueError, match="outside research bundle"):
        build_research_manifest(
            bundle_root=bundle_root,
            registry_root=registry_root,
            manifest_path=manifest_path,
            pairs=["EURUSD"],
        )


def test_research_manifest_rejects_artifact_symlink_resolving_outside_bundle(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir()
    outside_artifact = _artifact(tmp_path / "outside", "external_meta")
    linked_artifact = bundle_root / "artifacts" / "linked_meta"
    linked_artifact.parent.mkdir(parents=True)
    try:
        linked_artifact.symlink_to(outside_artifact, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    artifacts = {
        name: {
            "path": str(linked_artifact if name == "meta" else _artifact(bundle_root, name))
        }
        for name in ("regime", "meta", "swing_xgb", "intraday_xgb")
    }
    registry_root = bundle_root / "registry"
    _write_registry(registry_root, artifacts)

    with pytest.raises(ValueError, match="outside research bundle"):
        build_research_manifest(
            bundle_root=bundle_root,
            registry_root=registry_root,
            manifest_path=bundle_root / "research_models.json",
            pairs=["EURUSD"],
        )


def test_research_manifest_module_has_no_activation_or_runtime_imports() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fxstack"
        / "training"
        / "research_manifest.py"
    ).read_text(encoding="utf-8")

    assert "training.activation" not in source
    assert "fxstack.runtime" not in source
    assert "postgres" not in source.lower()
    assert "http" not in source.lower()
