from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from fxstack.backtest.harness.contracts import (
    REQUIRED_EXTERNAL_STRESS_SCENARIOS,
    HarnessRunManifest,
    directory_file_hashes,
    directory_sha256,
)
from fxstack.training.release_evidence import active_manifest_identity


def _nautilus_engine_version() -> str:
    return str(os.environ.get("FXSTACK_NAUTILUS_VERSION", "")).strip()


def build_nautilus_command(
    *,
    bundle_dir: Path,
    output_dir: Path,
    contract_args: list[str] | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    configured = str(os.environ.get("FXSTACK_NAUTILUS_CMD", "")).strip()
    if configured:
        base = configured.split()
    else:
        base = ["python", "-m", "nautilus_trader"]
    return [
        *base,
        "backtest",
        "--bundle",
        str(bundle_dir),
        "--out",
        str(output_dir),
        *(list(contract_args or [])),
        *(list(extra_args or [])),
    ]


def run_nautilus_harness(
    *,
    bundle_dir: Path,
    output_dir: Path,
    pair: str,
    dataset_hash: str = "",
    feature_service_name: str = "",
    feature_service_version: str = "",
    kernel_version: str = "",
    model_manifest_path: Path | None = None,
    economic_report_path: Path | None = None,
    stress_report_paths: dict[str, Path] | None = None,
    extra_args: list[str] | None = None,
    execute: bool = False,
) -> HarnessRunManifest:
    bundle_dir = Path(bundle_dir).resolve()
    output_dir = Path(output_dir).resolve()
    if execute and (model_manifest_path is None or economic_report_path is None):
        raise ValueError("executed Nautilus harness requires model_manifest_path and economic_report_path")
    if execute and not Path(model_manifest_path).is_file():
        raise ValueError("executed Nautilus harness model manifest is missing")
    contract_args: list[str] = []
    harness_run_id = ""
    input_bundle_sha256 = ""
    input_bundle_files: dict[str, str] = {}
    process_started_at_ns = 0
    process_finished_at_ns = 0
    normalized_stress_paths: dict[str, Path] = {}
    economic_report = Path(economic_report_path).resolve() if economic_report_path is not None else None
    if execute:
        if output_dir.exists():
            raise ValueError("executed Nautilus harness requires a fresh output_dir")
        if economic_report is None or not economic_report.is_relative_to(output_dir):
            raise ValueError("executed Nautilus harness economic report must be inside output_dir")
        engine_version = _nautilus_engine_version()
        if not str(dataset_hash).strip():
            raise ValueError("executed Nautilus harness dataset_hash is required")
        if not engine_version:
            raise ValueError("executed Nautilus harness engine_version is required")
        normalized_stress_paths = {
            str(name): Path(path).resolve()
            for name, path in dict(
                stress_report_paths
                or {
                    name: output_dir / f"nautilus.stress.{name}.json"
                    for name in REQUIRED_EXTERNAL_STRESS_SCENARIOS
                }
            ).items()
        }
        if set(normalized_stress_paths) != set(REQUIRED_EXTERNAL_STRESS_SCENARIOS):
            raise ValueError("executed Nautilus harness requires every external stress scenario")
        for name, path in normalized_stress_paths.items():
            if not path.is_relative_to(output_dir):
                raise ValueError(f"executed Nautilus harness stress report must be inside output_dir:{name}")
            if path.exists():
                raise ValueError(f"executed Nautilus harness stress report path already exists:{name}")
        if economic_report.exists():
            raise ValueError("executed Nautilus harness economic report path already exists")
        identity = active_manifest_identity(manifest_path=Path(model_manifest_path), pair=pair)
        if not identity.model_set_id or not identity.bundle_run_id or not identity.artifact_set_sha256:
            raise ValueError("executed Nautilus harness model identity is incomplete")
        input_bundle_files = directory_file_hashes(bundle_dir, excluded_root=output_dir)
        input_bundle_sha256 = directory_sha256(bundle_dir, excluded_root=output_dir)
        harness_run_id = uuid.uuid4().hex
        contract_args = [
            "--fxstack-economic-report",
            str(economic_report),
            "--fxstack-harness-run-id",
            harness_run_id,
            "--fxstack-pair",
            str(pair).upper(),
            "--fxstack-dataset-hash",
            str(dataset_hash),
            "--fxstack-engine-version",
            engine_version,
            "--fxstack-input-bundle-sha256",
            input_bundle_sha256,
            "--fxstack-model-manifest",
            str(Path(model_manifest_path).resolve()),
            "--fxstack-bundle-run-id",
            identity.bundle_run_id,
            "--fxstack-model-set-id",
            identity.model_set_id,
            "--fxstack-model-manifest-sha256",
            identity.model_manifest_sha256,
            "--fxstack-artifact-set-sha256",
            identity.artifact_set_sha256,
        ]
        for name in REQUIRED_EXTERNAL_STRESS_SCENARIOS:
            contract_args.extend(
                ["--fxstack-stress-report", f"{name}={normalized_stress_paths[name]}"]
            )
    command = build_nautilus_command(
        bundle_dir=bundle_dir,
        output_dir=output_dir,
        contract_args=contract_args,
        extra_args=extra_args,
    )
    status = "planned"
    process_returncode: int | None = None
    artifacts = {"output_dir": str(output_dir)}
    if execute:
        output_dir.mkdir(parents=True, exist_ok=False)
        process_started_at_ns = time.time_ns()
        proc = subprocess.run(command, cwd=str(bundle_dir), capture_output=True, text=True, check=False)
        process_finished_at_ns = time.time_ns()
        process_returncode = int(proc.returncode)
        status = "completed" if process_returncode == 0 else "failed"
        artifacts["stdout"] = str(output_dir / "nautilus.stdout.txt")
        artifacts["stderr"] = str(output_dir / "nautilus.stderr.txt")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "nautilus.stdout.txt").write_text(str(proc.stdout or ""), encoding="utf-8")
        (output_dir / "nautilus.stderr.txt").write_text(str(proc.stderr or ""), encoding="utf-8")
    manifest = HarnessRunManifest(
        engine="nautilus",
        status=status,
        pair=str(pair).upper(),
        dataset_hash=str(dataset_hash),
        feature_service_name=str(feature_service_name),
        feature_service_version=str(feature_service_version),
        kernel_version=str(kernel_version),
        engine_version=_nautilus_engine_version(),
        command=list(command),
        working_directory=str(bundle_dir),
        artifacts=artifacts,
        environment={"FXSTACK_NAUTILUS_CMD": str(os.environ.get("FXSTACK_NAUTILUS_CMD", ""))},
        metadata={
            "execute": bool(execute),
            "engine_package": "nautilus_trader",
            "fresh_output_dir": bool(execute),
            "process_returncode": process_returncode,
        },
    )
    if status == "completed":
        manifest.bind_executed_economic_evidence(
            model_manifest_path=Path(model_manifest_path),
            economic_report_path=Path(economic_report),
            output_dir=output_dir,
            harness_run_id=harness_run_id,
            input_bundle_sha256=input_bundle_sha256,
            input_bundle_files=input_bundle_files,
            process_started_at_ns=process_started_at_ns,
            process_finished_at_ns=process_finished_at_ns,
            stress_report_paths=normalized_stress_paths,
        )
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Run or plan a Nautilus Phase 3 harness execution")
    ap.add_argument("--bundle-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--pair", required=True)
    ap.add_argument("--dataset-hash", default="")
    ap.add_argument("--feature-service-name", default="")
    ap.add_argument("--feature-service-version", default="")
    ap.add_argument("--kernel-version", default="")
    ap.add_argument("--model-manifest", default="")
    ap.add_argument("--economic-report", default="")
    ap.add_argument(
        "--stress-report",
        action="append",
        default=[],
        metavar="SCENARIO=PATH",
    )
    ap.add_argument("--manifest-output", default="")
    ap.add_argument("--execute", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("extra_args", nargs="*")
    args = ap.parse_args()
    stress_report_paths: dict[str, Path] = {}
    for raw in list(args.stress_report or []):
        name, separator, path = str(raw).partition("=")
        if not separator or not name.strip() or not path.strip():
            ap.error("--stress-report must be SCENARIO=PATH")
        stress_report_paths[name.strip()] = Path(path.strip())
    manifest = run_nautilus_harness(
        bundle_dir=Path(args.bundle_dir),
        output_dir=Path(args.output_dir),
        pair=str(args.pair),
        dataset_hash=str(args.dataset_hash),
        feature_service_name=str(args.feature_service_name),
        feature_service_version=str(args.feature_service_version),
        kernel_version=str(args.kernel_version),
        model_manifest_path=Path(args.model_manifest) if str(args.model_manifest).strip() else None,
        economic_report_path=Path(args.economic_report) if str(args.economic_report).strip() else None,
        stress_report_paths=stress_report_paths or None,
        extra_args=list(args.extra_args or []),
        execute=bool(args.execute),
    )
    if str(args.manifest_output).strip():
        manifest_output = Path(args.manifest_output)
        manifest_output.parent.mkdir(parents=True, exist_ok=True)
        manifest_output.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
