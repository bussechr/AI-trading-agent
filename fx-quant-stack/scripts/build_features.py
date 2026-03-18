from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.data.ingest import load_silver_bars
from fxstack.features.build import build_features, leakage_guard
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Build PIT features")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--input-root", default="data/raw")
    ap.add_argument("--output-root", default="data/features")
    args = ap.parse_args()
    provider = get_settings().normalized_data_provider

    bars = load_silver_bars(
        store_root=Path(args.input_root),
        pair=args.pair.upper(),
        timeframe=args.timeframe,
        provider=provider,
    )
    feats = build_features(bars)
    leakage_guard(feats)

    store = ParquetStore(Path(args.output_root))
    out = store.write_partitioned(feats, provider=provider, pair=args.pair.upper(), timeframe=args.timeframe)
    print({"rows": len(feats), "path": str(out)})


if __name__ == "__main__":
    main()
