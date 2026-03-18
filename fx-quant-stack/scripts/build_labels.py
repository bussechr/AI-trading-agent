from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.io.parquet_store import ParquetStore
from fxstack.labels.triple_barrier import TripleBarrierConfig, triple_barrier_labels
from fxstack.settings import get_settings


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
    provider = get_settings().normalized_data_provider

    feats = ParquetStore(Path(args.feature_root)).read_pair_timeframe(provider=provider, pair=args.pair.upper(), timeframe=args.timeframe)
    labels = triple_barrier_labels(
        feats,
        TripleBarrierConfig(
            horizon_bars=args.horizon_bars,
            tp_atr_mult=args.tp_atr_mult,
            sl_atr_mult=args.sl_atr_mult,
        ),
    )
    out = ParquetStore(Path(args.label_root)).write_partitioned(labels, provider=provider, pair=args.pair.upper(), timeframe=args.timeframe)
    print({"rows": len(labels), "path": str(out)})


if __name__ == "__main__":
    main()
