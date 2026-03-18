from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from fxstack.io.parquet_store import ParquetStore
from fxstack.labels.meta_label import build_meta_labels
from fxstack.models.meta_filter import MetaFilterXGB
from fxstack.settings import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Train meta-label model")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--out", default="artifacts/meta_filter")
    args = ap.parse_args()
    provider = get_settings().normalized_data_provider

    feats = ParquetStore(Path(args.feature_root)).read_pair_timeframe(
        provider=provider,
        pair=args.pair.upper(),
        timeframe=args.timeframe,
    )
    if feats.empty:
        raise SystemExit("Missing features")

    # Placeholder realized edge proxy for training bootstrapping.
    df = feats.copy()
    df["realized_edge_bps"] = (df["ret_1"].astype(float) * 10000.0) - (df.get("spread", 0.0).astype(float) * 10000.0)
    meta_df = build_meta_labels(df, pnl_col="realized_edge_bps")

    drop = {"pair", "timeframe", "date", "meta_label", "realized_edge_bps"}
    X = meta_df[[c for c in meta_df.columns if c not in drop and c != "ts"]]
    y = meta_df["meta_label"]

    model = MetaFilterXGB()
    model.fit(X, y)
    model.save(Path(args.out))
    print({"model": "meta_filter", "rows": len(X), "path": args.out})


if __name__ == "__main__":
    main()
