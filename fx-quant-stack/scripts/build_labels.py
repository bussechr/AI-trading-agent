# AGENT: ROLE: External triple-barrier label-generation CLI.
# AGENT: ISOLATION: help and argument validation run before settings, storage, or label imports.
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description="Build triple-barrier labels")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--horizon-bars", type=int, default=24)
    ap.add_argument("--tp-atr-mult", type=float, default=2.0)
    ap.add_argument("--sl-atr-mult", type=float, default=1.5)
    args = ap.parse_args()

    from fxstack.tasks import build_labels_task

    result = build_labels_task(
        pair=str(args.pair).upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        horizon_bars=int(args.horizon_bars),
        tp_mult=float(args.tp_atr_mult),
        sl_mult=float(args.sl_atr_mult),
    )
    print(result)


if __name__ == "__main__":
    main()
