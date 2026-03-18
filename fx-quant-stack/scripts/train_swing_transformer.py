from __future__ import annotations

import argparse

from fxstack.tasks import train_swing_transformer_task


def main() -> None:
    ap = argparse.ArgumentParser(description="Train swing transformer model")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="D")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--out", default="artifacts/swing_transformer")
    args = ap.parse_args()

    out = train_swing_transformer_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
    )
    print(out)


if __name__ == "__main__":
    main()
