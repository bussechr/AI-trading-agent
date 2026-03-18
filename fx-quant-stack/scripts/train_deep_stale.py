from __future__ import annotations

import argparse

from fxstack.settings import get_settings
from fxstack.tasks import train_deep_stale_task


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser(description="Retrain deep models only when stale")
    ap.add_argument("--pair", action="append", default=[])
    ap.add_argument("--swing-timeframe", default=str(s.swing_timeframe))
    ap.add_argument("--intraday-timeframe", default=str(s.intraday_timeframe))
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--artifact-root", default="artifacts")
    ap.add_argument("--stale-hours", type=float, default=float(s.deep_model_stale_hours))
    args = ap.parse_args()

    pairs = [str(p).upper() for p in (args.pair or [])] or list(s.pairs)
    out = []
    for pair in pairs:
        out.append(
            train_deep_stale_task(
                pair=pair,
                swing_timeframe=str(args.swing_timeframe).upper(),
                intraday_timeframe=str(args.intraday_timeframe).upper(),
                feature_root=str(args.feature_root),
                label_root=str(args.label_root),
                artifact_root=str(args.artifact_root),
                stale_hours=float(args.stale_hours),
            )
        )
    print({"pairs": pairs, "stale_hours": float(args.stale_hours), "results": out})


if __name__ == "__main__":
    main()
