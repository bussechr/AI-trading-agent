from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools import extract_model_manifest


def test_extract_model_manifest_returns_2_when_source_file_is_missing(tmp_path) -> None:
    code = extract_model_manifest.run(
        argparse.Namespace(
            source_model=str(tmp_path / "missing.h5"),
            output_dir=str(tmp_path / "out"),
            feature_schema_id="",
            scaler_refs=[],
            preprocessor_refs=[],
            skip_savedmodel=False,
        )
    )
    assert code == 2


def test_extract_model_manifest_writes_manifest_and_saved_model_bundle_with_mocked_extractors(tmp_path, monkeypatch) -> None:
    source_model = tmp_path / "transformer_eurusd.h5"
    source_model.write_bytes(b"fake-h5-payload")

    monkeypatch.setattr(
        extract_model_manifest,
        "_collect_hdf5_training_metadata",
        lambda path: {
            "model_config": {"class_name": "Model"},
            "training_config": {"optimizer_config": {"name": "adam"}},
            "keras_version": "3.0.0",
            "backend": "tensorflow",
            "optimizer_state_present": True,
        },
    )
    monkeypatch.setattr(
        extract_model_manifest,
        "_load_runtime_shapes",
        lambda path: {
            "input_shape": [None, 99],
            "output_shape": [None, 1],
            "framework_version": "2.16.1",
        },
    )

    def _fake_export(source: Path, export_dir: Path) -> None:
        export_dir.mkdir(parents=True, exist_ok=True)
        (export_dir / "saved_model.pb").write_bytes(b"pb")

    monkeypatch.setattr(extract_model_manifest, "_export_saved_model", _fake_export)

    output_dir = tmp_path / "bundle"
    code = extract_model_manifest.run(
        argparse.Namespace(
            source_model=str(source_model),
            output_dir=str(output_dir),
            feature_schema_id="eurusd_transformer_v1",
            scaler_refs=["scaler://eurusd"],
            preprocessor_refs=["pre://eurusd"],
            policy_version="policy_v1",
            model_bundle_version="bundle_v1",
            orchestrator_version="orch_v1",
            skip_savedmodel=False,
        )
    )
    assert code == 0

    manifest = json.loads((output_dir / "model-manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_kind"] == "keras_hdf5_training_artifact"
    assert manifest["input_shape"] == [None, 99]
    assert manifest["output_shape"] == [None, 1]
    assert manifest["feature_schema_id"] == "eurusd_transformer_v1"
    assert manifest["optimizer_state_present"] is True
    assert manifest["version_bundle"]["policy_version"] == "policy_v1"
    assert manifest["version_bundle"]["model_bundle_version"] == "bundle_v1"
    assert manifest["version_bundle"]["orchestrator_version"] == "orch_v1"
    assert (output_dir / "saved_model" / "saved_model.pb").exists()
