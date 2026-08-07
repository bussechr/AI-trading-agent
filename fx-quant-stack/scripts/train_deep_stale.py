# AGENT: ROLE: Focused external stale-only deep-model training CLI.
# AGENT: ISOLATION: help and argument validation run before settings or task imports.
from __future__ import annotations

import argparse

def main() -> None:
    ap = argparse.ArgumentParser(description="Retrain deep models only when stale")
    ap.add_argument("--pair", action="append", default=[])
    ap.add_argument("--swing-timeframe", default=None)
    ap.add_argument("--intraday-timeframe", default=None)
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--artifact-root", default="artifacts")
    ap.add_argument("--stale-hours", type=float, default=None)
    args = ap.parse_args()

    from fxstack.settings import get_settings
    from fxstack.tasks import train_deep_stale_task

    s = get_settings()
    if args.swing_timeframe is None:
        args.swing_timeframe = str(s.swing_timeframe)
    if args.intraday_timeframe is None:
        args.intraday_timeframe = str(s.intraday_timeframe)
    if args.stale_hours is None:
        args.stale_hours = float(s.deep_model_stale_hours)

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
