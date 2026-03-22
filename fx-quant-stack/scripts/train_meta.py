from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.tasks import train_meta_task


def main() -> None:
    ap = argparse.ArgumentParser(description="Train meta-label model")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--out", default="artifacts/meta_filter")
    ap.add_argument("--regime-model", default="")
    ap.add_argument("--swing-model", default="")
    ap.add_argument("--intraday-model", default="")
    ap.add_argument("--allow-heuristic-meta-labels", action="store_true")
    args = ap.parse_args()
    out = train_meta_task(
        pair=args.pair.upper(),
        timeframe=str(args.timeframe).upper(),
        feature_root=str(args.feature_root),
        label_root=str(args.label_root),
        out=str(args.out),
        regime_model_path=str(args.regime_model),
        swing_model_path=str(args.swing_model),
        intraday_model_path=str(args.intraday_model),
        allow_heuristic_labels=bool(args.allow_heuristic_meta_labels),
    )
    print(out)


if __name__ == "__main__":
    main()
