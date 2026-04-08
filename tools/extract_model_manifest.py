from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION


MODEL_MANIFEST_SCHEMA_VERSION = "phase0.model_manifest.v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_hash(value: Any) -> str:
    if value in (None, "", b""):
        return ""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _decode_attr(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _import_or_raise(module_name: str, package_hint: str) -> Any:
    spec = importlib.util.find_spec(module_name)
    if spec is None:
        raise RuntimeError(f"{module_name} is required for {package_hint} extraction")
    return importlib.import_module(module_name)


def _collect_hdf5_training_metadata(source_model: Path) -> dict[str, Any]:
    h5py = _import_or_raise("h5py", "HDF5")
    with h5py.File(str(source_model), "r") as handle:
        model_config = _decode_attr(handle.attrs.get("model_config", ""))
        training_config = _decode_attr(handle.attrs.get("training_config", ""))
        keras_version = _decode_attr(handle.attrs.get("keras_version", ""))
        backend = _decode_attr(handle.attrs.get("backend", ""))
        return {
            "model_config": json.loads(model_config) if model_config else {},
            "training_config": json.loads(training_config) if training_config else {},
            "keras_version": str(keras_version or ""),
            "backend": str(backend or ""),
            "optimizer_state_present": bool(training_config) or "optimizer_weights" in handle,
        }


def _shape_to_list(shape: Any) -> list[int | None]:
    out: list[int | None] = []
    for item in list(shape or []):
        try:
            out.append(None if item is None else int(item))
        except Exception:
            out.append(None)
    return out


def _load_runtime_shapes(source_model: Path) -> dict[str, Any]:
    tensorflow = _import_or_raise("tensorflow", "SavedModel export")
    model = tensorflow.keras.models.load_model(str(source_model), compile=False)
    input_shapes = [_shape_to_list(getattr(tensor, "shape", [])) for tensor in list(getattr(model, "inputs", []) or [])]
    output_shapes = [_shape_to_list(getattr(tensor, "shape", [])) for tensor in list(getattr(model, "outputs", []) or [])]
    framework_version = str(getattr(tensorflow, "__version__", "") or "")
    return {
        "input_shape": input_shapes[0] if len(input_shapes) == 1 else input_shapes,
        "output_shape": output_shapes[0] if len(output_shapes) == 1 else output_shapes,
        "framework_version": framework_version,
    }


def build_manifest(
    *,
    source_model: Path,
    training_metadata: dict[str, Any],
    runtime_metadata: dict[str, Any],
    feature_schema_id: str,
    scaler_refs: list[str],
    preprocessor_refs: list[str],
    policy_version: str = "",
    model_bundle_version: str = "",
    orchestrator_version: str = "",
) -> dict[str, Any]:
    model_config = training_metadata.get("model_config") or {}
    training_config = training_metadata.get("training_config") or {}
    version_bundle = {
        "schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "policy_version": str(policy_version or feature_schema_id or ""),
        "model_bundle_version": str(model_bundle_version or _sha256_file(source_model)),
        "orchestrator_version": str(orchestrator_version or MODEL_MANIFEST_SCHEMA_VERSION),
    }
    return {
        "schema_version": MODEL_MANIFEST_SCHEMA_VERSION,
        "artifact_kind": "keras_hdf5_training_artifact",
        "inference_runtime_format": "tensorflow_savedmodel",
        "source_artifact": {
            "path": str(source_model),
            "sha256": _sha256_file(source_model),
        },
        "architecture_hash": _canonical_json_hash(model_config),
        "training_config_hash": _canonical_json_hash(training_config),
        "optimizer_state_present": bool(training_metadata.get("optimizer_state_present", False)),
        "framework": {
            "keras_version": str(training_metadata.get("keras_version") or ""),
            "backend": str(training_metadata.get("backend") or ""),
            "runtime_version": str(runtime_metadata.get("framework_version") or ""),
        },
        "input_shape": runtime_metadata.get("input_shape"),
        "output_shape": runtime_metadata.get("output_shape"),
        "feature_schema_id": str(feature_schema_id or ""),
        "scaler_refs": list(scaler_refs),
        "preprocessor_refs": list(preprocessor_refs),
        "version_bundle": version_bundle,
    }


def _export_saved_model(source_model: Path, export_dir: Path) -> None:
    tensorflow = _import_or_raise("tensorflow", "SavedModel export")
    model = tensorflow.keras.models.load_model(str(source_model), compile=False)
    export_dir.mkdir(parents=True, exist_ok=True)
    tensorflow.saved_model.save(model, str(export_dir))


def write_manifest_bundle(
    *,
    manifest: dict[str, Any],
    output_dir: Path,
    source_model: Path,
    export_savedmodel: bool,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_model_dir = output_dir / "saved_model"
    if export_savedmodel:
        _export_saved_model(source_model, saved_model_dir)
    (output_dir / "model-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return output_dir


def run(args: argparse.Namespace) -> int:
    source_model = Path(args.source_model)
    if not source_model.exists():
        print(f"source model not found: {source_model}", file=os.sys.stderr)
        return 2
    requested_output_dir = str(getattr(args, "output_dir", "") or "").strip()
    output_dir = Path(requested_output_dir) if requested_output_dir else source_model.with_suffix("")
    try:
        training_metadata = _collect_hdf5_training_metadata(source_model)
        runtime_metadata = _load_runtime_shapes(source_model)
        manifest = build_manifest(
            source_model=source_model,
            training_metadata=training_metadata,
            runtime_metadata=runtime_metadata,
            feature_schema_id=str(getattr(args, "feature_schema_id", "") or ""),
            scaler_refs=list(getattr(args, "scaler_refs", []) or []),
            preprocessor_refs=list(getattr(args, "preprocessor_refs", []) or []),
            policy_version=str(getattr(args, "policy_version", "") or ""),
            model_bundle_version=str(getattr(args, "model_bundle_version", "") or ""),
            orchestrator_version=str(getattr(args, "orchestrator_version", "") or ""),
        )
        write_manifest_bundle(
            manifest=manifest,
            output_dir=output_dir,
            source_model=source_model,
            export_savedmodel=not bool(getattr(args, "skip_savedmodel", False)),
        )
    except Exception as exc:
        print(f"manifest extraction failed: {type(exc).__name__}: {exc}", file=os.sys.stderr)
        return 1
    print(str(output_dir))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract a Phase 0 model manifest and stripped SavedModel bundle.")
    parser.add_argument("source_model")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--feature-schema-id", default="")
    parser.add_argument("--scaler-ref", action="append", dest="scaler_refs", default=[])
    parser.add_argument("--preprocessor-ref", action="append", dest="preprocessor_refs", default=[])
    parser.add_argument("--policy-version", default="")
    parser.add_argument("--model-bundle-version", default="")
    parser.add_argument("--orchestrator-version", default="")
    parser.add_argument("--skip-savedmodel", action="store_true")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_build_parser().parse_args()))
