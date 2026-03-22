from __future__ import annotations

import argparse

from fxstack.tasks import train_exit_task


def main() -> None:
    ap = argparse.ArgumentParser(description="Train exit policy model")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ap.add_argument("--label-root", default="fx-quant-stack/data/labels")
    ap.add_argument("--out", default="fx-quant-stack/artifacts/exit_policy")
    args = ap.parse_args()
    out = train_exit_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)


if __name__ == "__main__":
    main()
