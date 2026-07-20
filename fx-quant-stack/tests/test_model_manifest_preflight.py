from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from fxstack.features.session_contract import feature_contract_metadata
from fxstack.models.artifact_contract import stamp_artifact_payload_digest
from fxstack.runtime.model_manifest_preflight import (
    ModelManifestPreflightError,
    preflight_active_model_manifest,
)


PAIR = "EURUSD"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _artifact(root: Path, name: str) -> dict[str, object]:
    path = root / name
    path.mkdir(parents=True)
    (path / "model.bin").write_bytes(f"payload:{name}".encode("utf-8"))
    _write_json(
        path / "meta.json",
        {
            "name": name,
            "pair": PAIR,
            **feature_contract_metadata(),
        },
    )
    stamped = stamp_artifact_payload_digest(path)
    return {
        "path": str(path),
        "artifact_hash": str(stamped["artifact_payload_sha256"]),
        "runtime_compatible": True,
    }


def _valid_manifest(tmp_path: Path) -> tuple[Path, dict[str, dict[str, object]]]:
    artifacts = {
        "regime": _artifact(tmp_path / "artifacts", "regime_hmm"),
        "meta": _artifact(tmp_path / "artifacts", "meta_filter"),
        "swing_xgb": _artifact(tmp_path / "artifacts", "swing_xgb"),
        "intraday_xgb": _artifact(tmp_path / "artifacts", "intraday_xgb"),
    }
    manifest = tmp_path / "active_models.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "active_model_sets": {
                PAIR: {
                    "enabled": True,
                    "model_set_id": "candidate-v2",
                    "registry_path": "mlflow://EURUSD@candidate",
                    "artifacts": artifacts,
                    "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
                    "metadata": {
                        "pair": PAIR,
                        "promotion_status": "eligible",
                        "feature_schema": feature_contract_metadata(),
                    },
                }
            },
        },
    )
    return manifest, artifacts


def _tree_state(root: Path) -> list[tuple[str, int, int]]:
    return sorted(
        (
            str(path.relative_to(root)).replace("\\", "/"),
            int(path.stat().st_size),
            int(path.stat().st_mtime_ns),
        )
        for path in root.rglob("*")
        if path.is_file()
    )


def test_preflight_accepts_current_contract_without_writing(tmp_path: Path) -> None:
    manifest, _ = _valid_manifest(tmp_path)
    before = _tree_state(tmp_path)

    result = preflight_active_model_manifest(
        manifest_path=manifest,
        project_root=tmp_path,
        required_pairs=[PAIR],
    )

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["validated_pairs"] == [PAIR]
    assert result["validated_artifacts"] == 4
    assert result["manifest_content_sha256"] == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
    assert _tree_state(tmp_path) == before


def test_preflight_rejects_legacy_contract_before_artifact_resolution(tmp_path: Path) -> None:
    manifest = tmp_path / "active_models.json"
    _write_json(
        manifest,
        {
            "schema_version": 1,
            "active_model_sets": {
                PAIR: {
                    "enabled": True,
                    "model_set_id": "legacy-v1",
                    "registry_path": "missing-registry.json",
                    "artifacts": {
                        "regime": "missing/regime",
                        "meta": "missing/meta",
                        "swing_xgb": "missing/swing",
                        "intraday_xgb": "missing/intraday",
                    },
                    "metadata": {
                        "pair": PAIR,
                        "feature_schema": {"intraday_contract": "hierarchical_v1"},
                    },
                }
            },
        },
    )
    before = _tree_state(tmp_path)

    with pytest.raises(ModelManifestPreflightError) as exc_info:
        preflight_active_model_manifest(
            manifest_path=manifest,
            project_root=tmp_path,
            required_pairs=[PAIR],
        )

    message = str(exc_info.value)
    assert "feature_contract_mismatch:manifest:EURUSD" in message
    assert "expected:hierarchical_v2|actual:hierarchical_v1" in message
    assert "feature_schema_version=expected:fx_features_v2|actual:<missing>" in message
    assert _tree_state(tmp_path) == before


def test_preflight_rejects_missing_configured_pair(tmp_path: Path) -> None:
    manifest, _ = _valid_manifest(tmp_path)

    with pytest.raises(
        ModelManifestPreflightError,
        match="active_manifest_missing_pairs:USDJPY",
    ):
        preflight_active_model_manifest(
            manifest_path=manifest,
            project_root=tmp_path,
            required_pairs=[PAIR, "USDJPY"],
        )


def test_preflight_rejects_payload_tampering(tmp_path: Path) -> None:
    manifest, artifacts = _valid_manifest(tmp_path)
    intraday = Path(str(artifacts["intraday_xgb"]["path"]))
    (intraday / "model.bin").write_bytes(b"tampered")

    with pytest.raises(ModelManifestPreflightError) as exc_info:
        preflight_active_model_manifest(
            manifest_path=manifest,
            project_root=tmp_path,
            required_pairs=[PAIR],
        )

    assert "artifact_invalid:EURUSD:intraday_xgb" in str(exc_info.value)
    assert "artifact_payload_digest_mismatch" in str(exc_info.value)


def test_preflight_binds_local_registry_identity(tmp_path: Path) -> None:
    manifest, _ = _valid_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    row = payload["active_model_sets"][PAIR]
    registry = tmp_path / "registry" / "eurusd.json"
    _write_json(
        registry,
        {
            "pair": PAIR,
            "run_id": row["model_set_id"],
            "feature_schema": feature_contract_metadata(),
        },
    )
    row["registry_path"] = str(registry)
    _write_json(manifest, payload)

    preflight_active_model_manifest(
        manifest_path=manifest,
        project_root=tmp_path,
        required_pairs=[PAIR],
    )

    registry_payload = json.loads(registry.read_text(encoding="utf-8"))
    registry_payload["run_id"] = "different-model-set"
    _write_json(registry, registry_payload)
    with pytest.raises(ModelManifestPreflightError) as exc_info:
        preflight_active_model_manifest(
            manifest_path=manifest,
            project_root=tmp_path,
            required_pairs=[PAIR],
        )
    assert "registry_model_set_mismatch:EURUSD" in str(exc_info.value)


def test_preflight_rejects_research_manifest(tmp_path: Path) -> None:
    manifest, _ = _valid_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["research_only"] = True
    _write_json(manifest, payload)

    with pytest.raises(
        ModelManifestPreflightError,
        match="research_manifest_rejected_for_runtime",
    ):
        preflight_active_model_manifest(
            manifest_path=manifest,
            project_root=tmp_path,
            required_pairs=[PAIR],
        )


def test_preflight_rejects_noneligible_model_set(tmp_path: Path) -> None:
    manifest, _ = _valid_manifest(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["active_model_sets"][PAIR]["metadata"]["promotion_status"] = "research_only"
    _write_json(manifest, payload)

    with pytest.raises(
        ModelManifestPreflightError,
        match="promotion_status_not_eligible:EURUSD:actual:research_only",
    ):
        preflight_active_model_manifest(
            manifest_path=manifest,
            project_root=tmp_path,
            required_pairs=[PAIR],
        )


def test_windows_runtime_runs_model_preflight_before_process_reset() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime = (root / "ops" / "windows" / "21_start_runtime.bat").read_text(
        encoding="utf-8"
    )
    launch = (root / "launch_all.bat").read_text(encoding="utf-8")

    assert runtime.index("call :preflight_active_models") < runtime.index(
        "call :reset_runtime_processes"
    )
    preflight_block = runtime.split(":preflight_active_models", 1)[1].split(
        ":resolve_launch_posture", 1
    )[0]
    assert "-I -B -m fxstack.runtime.model_manifest_preflight" in preflight_block
    assert "tools\\preflight_active_models.py" not in preflight_block
    assert '"%TRADER_PYTHON_EXE%" -I -B -m fxstack.runtime.model_manifest_preflight' in preflight_block
    assert "models activate" not in preflight_block
    live_block = launch.split(":live", 1)[1].split(":full", 1)[0]
    assert '21_start_runtime.bat" --validate-models' in live_block
    assert live_block.index("--validate-models") < live_block.index(":auto_db_fallback")
