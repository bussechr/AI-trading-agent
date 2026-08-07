# AGENT: ROLE: External baseline cost-aware backtest summary CLI.
# AGENT: ISOLATION: help and argument validation run before settings, storage, engine, or report imports.
from __future__ import annotations

import argparse


def main() -> int:
    ap = argparse.ArgumentParser(description="Run baseline cost-aware backtest summary")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="data/features")
    args = ap.parse_args()

    from fxstack.backtest.smoke import run_baseline_smoke

    report, return_code = run_baseline_smoke(
        pair=str(args.pair),
        timeframe=str(args.timeframe),
        feature_root=str(args.feature_root),
    )
    print(report)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
