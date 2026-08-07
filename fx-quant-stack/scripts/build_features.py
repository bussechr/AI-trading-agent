# AGENT: ROLE: External point-in-time feature-generation CLI.
# AGENT: ISOLATION: help and argument validation run before settings, storage, ingestion, or feature imports.
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description="Build PIT features")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--input-root", default="data/raw")
    ap.add_argument("--output-root", default="data/features")
    args = ap.parse_args()

    from fxstack.tasks import build_features_task

    result = build_features_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        input_root=str(args.input_root),
        output_root=str(args.output_root),
    )
    print(result)


if __name__ == "__main__":
    main()
