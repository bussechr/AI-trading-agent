# AGENT: ROLE: Focused external build/training-host preflight CLI.
# AGENT: ENTRYPOINT: invoked by offline ops before data or model mutation.
# AGENT: ISOLATION: help and argument validation run before settings or dependency probes.
from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an external fxstack build/training host")
    parser.add_argument("--allow-sqlite", action="store_true")
    args = parser.parse_args()

    from fxstack.training.environment import stack_preflight

    report = stack_preflight(allow_sqlite=bool(args.allow_sqlite))
    print(report)
    return 0 if bool(report["ok"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
