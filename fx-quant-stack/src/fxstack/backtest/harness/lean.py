from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from fxstack.backtest.harness.contracts import HarnessRunManifest


def _lean_engine_version() -> str:
    return str(os.environ.get("FXSTACK_LEAN_VERSION", "")).strip()


def build_lean_command(*, bundle_dir: Path, output_dir: Path, extra_args: list[str] | None = None) -> list[str]:
    configured = str(os.environ.get("FXSTACK_LEAN_CMD", "")).strip()
    if configured:
        base = configured.split()
    else:
        base = ["lean", "backtest"]
    return [*base, "--bundle", str(bundle_dir), "--output", str(output_dir), *(list(extra_args or []))]


def run_lean_harness(
    *,
    bundle_dir: Path,
    output_dir: Path,
    pair: str,
    dataset_hash: str = "",
    feature_service_name: str = "",
    feature_service_version: str = "",
    kernel_version: str = "",
    extra_args: list[str] | None = None,
    execute: bool = False,
) -> HarnessRunManifest:
    command = build_lean_command(bundle_dir=bundle_dir, output_dir=output_dir, extra_args=extra_args)
    status = "planned"
    artifacts = {"output_dir": str(output_dir)}
    if execute:
        proc = subprocess.run(command, cwd=str(bundle_dir), capture_output=True, text=True, check=False)
        status = "completed" if int(proc.returncode) == 0 else "failed"
        artifacts["stdout"] = str(output_dir / "lean.stdout.txt")
        artifacts["stderr"] = str(output_dir / "lean.stderr.txt")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "lean.stdout.txt").write_text(str(proc.stdout or ""), encoding="utf-8")
        (output_dir / "lean.stderr.txt").write_text(str(proc.stderr or ""), encoding="utf-8")
    return HarnessRunManifest(
        engine="lean",
        status=status,
        pair=str(pair).upper(),
        dataset_hash=str(dataset_hash),
        feature_service_name=str(feature_service_name),
        feature_service_version=str(feature_service_version),
        kernel_version=str(kernel_version),
        engine_version=_lean_engine_version(),
        command=list(command),
        working_directory=str(bundle_dir),
        artifacts=artifacts,
        environment={"FXSTACK_LEAN_CMD": str(os.environ.get("FXSTACK_LEAN_CMD", ""))},
        metadata={"execute": bool(execute), "engine_package": "lean"},
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Run or plan a LEAN Phase 3 harness execution")
    ap.add_argument("--bundle-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--pair", required=True)
    ap.add_argument("--dataset-hash", default="")
    ap.add_argument("--feature-service-name", default="")
    ap.add_argument("--feature-service-version", default="")
    ap.add_argument("--kernel-version", default="")
    ap.add_argument("--execute", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("extra_args", nargs="*")
    args = ap.parse_args()
    manifest = run_lean_harness(
        bundle_dir=Path(args.bundle_dir),
        output_dir=Path(args.output_dir),
        pair=str(args.pair),
        dataset_hash=str(args.dataset_hash),
        feature_service_name=str(args.feature_service_name),
        feature_service_version=str(args.feature_service_version),
        kernel_version=str(args.kernel_version),
        extra_args=list(args.extra_args or []),
        execute=bool(args.execute),
    )
    print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
