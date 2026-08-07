# AGENT: ROLE: Focused external model-registry activation CLI.
# AGENT: ENTRYPOINT: invoked by `ops/windows/14_activate_models.bat` before package assembly.
# AGENT: SIDE EFFECTS: writes the selected activation manifest and activation database records.
# AGENT: ISOLATION: help and argument validation run before settings, database, registry, or MLflow imports.
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Activate validated model registry entries")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--registry-root", default="")
    parser.add_argument("--manifest", default="")
    parser.add_argument("--registry-file", default="")
    parser.add_argument("--pair", action="append", default=[])
    parser.add_argument("--source", choices=["compat", "mlflow"], default="compat")
    parser.add_argument("--alias", choices=["champion", "shadow"], default="champion")
    parser.add_argument("--require-all", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from fxstack.training.activation_cli import activate_models

    report, return_code = activate_models(
        database_url=args.database_url,
        registry_root=args.registry_root,
        manifest=args.manifest,
        registry_file=args.registry_file,
        pairs=args.pair,
        source=args.source,
        alias=args.alias,
        require_all=args.require_all,
    )
    print(report)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
