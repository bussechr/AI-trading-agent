# AGENT: ROLE: Focused external intraday-TCN training CLI.
# AGENT: ENTRYPOINT: invoked by `ops/windows/17_train_intraday_tcn.bat` outside production runtime.
# AGENT: ISOLATION: help and argument validation run before task or model imports.
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description="Train intraday TCN model")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--out", default="artifacts/intraday_tcn")
    args = ap.parse_args()

    from fxstack.tasks import train_intraday_tcn_task

    out = train_intraday_tcn_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)


if __name__ == "__main__":
    main()
