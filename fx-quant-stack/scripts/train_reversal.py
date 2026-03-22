from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.tasks import train_reversal_task


def main() -> None:
    ap = argparse.ArgumentParser(description="Train reversal models")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ap.add_argument("--label-root", default="fx-quant-stack/data/labels")
    ap.add_argument("--out-root", default="fx-quant-stack/artifacts")
    args = ap.parse_args()
    out_root = Path(str(args.out_root)) / str(args.pair).lower()
    out = train_reversal_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out_failure=str(out_root / "reversal_failure_xgb"),
        out_opportunity=str(out_root / "reversal_opportunity_xgb"),
    )
    print(out)


if __name__ == "__main__":
    main()
