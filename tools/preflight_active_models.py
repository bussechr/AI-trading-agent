#!/usr/bin/env python3
"""Read-only active-model preflight for the production runtime launcher."""

# AGENT: ROLE: CLI adapter for pre-spawn model-manifest and artifact validation.
# AGENT: CALLED BY: `ops/windows/21_start_runtime.bat` and operators.
# AGENT: SIDE EFFECTS: None; reports to stdout/stderr and never changes model/runtime state.
# AGENT: SEE: `docs/agents/ops-entrypoints.md`.

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.runtime.model_manifest_preflight import (  # noqa: E402
    ModelManifestPreflightError,
    preflight_active_model_manifest,
)


def _pairs(raw: str) -> list[str]:
    return [value.strip() for value in str(raw or "").replace(";", ",").split(",") if value.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the active manifest, current feature contract, registry provenance, "
            "and local artifact payloads without loading or activating models."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root used to resolve local artifact references.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            os.environ.get(
                "FXSTACK_MODEL_ACTIVATION_MANIFEST",
                "fx-quant-stack/artifacts/active_models.json",
            )
        ),
        help="Activation manifest to inspect; the file is never modified.",
    )
    parser.add_argument(
        "--pairs",
        default=os.environ.get("FXSTACK_PAIRS", ""),
        help="Comma-separated runtime pair scope. All enabled rows are checked when omitted.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = preflight_active_model_manifest(
            manifest_path=args.manifest,
            project_root=args.project_root,
            required_pairs=_pairs(args.pairs),
        )
    except ModelManifestPreflightError as exc:
        print(f"[model-preflight] ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
