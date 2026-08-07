# AGENT: ROLE: Focused external CUDA capability check for training hosts.
# AGENT: ENTRYPOINT: invoked by `ops/windows/05_gpu_check.bat`; never a production runtime gate.
# AGENT: ISOLATION: help and argument validation run before settings or torch imports.
from __future__ import annotations

import argparse


def main() -> int:
    argparse.ArgumentParser(description="Validate CUDA availability for external deep-model training").parse_args()

    from fxstack.training.environment import gpu_diagnostics

    result = gpu_diagnostics()
    print(result)
    return 0 if bool(result["ok"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
