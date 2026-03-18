from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from fxstack.io.parquet_store import ParquetStore
from fxstack.models.swing_xgb import SwingXGB
from fxstack.settings import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Train swing XGBoost model")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="D")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--out", default="artifacts/swing_xgb")
    args = ap.parse_args()
    provider = get_settings().normalized_data_provider

    store_f = ParquetStore(Path(args.feature_root))
    store_l = ParquetStore(Path(args.label_root))
    feats = store_f.read_pair_timeframe(provider=provider, pair=args.pair.upper(), timeframe=args.timeframe)
    labels = store_l.read_pair_timeframe(provider=provider, pair=args.pair.upper(), timeframe=args.timeframe)
    if feats.empty or labels.empty:
        raise SystemExit("Missing features or labels")

    df = feats.merge(labels[["ts", "label"]], on="ts", how="inner")
    df = df[df["label"].isin([-1, 1])].copy()
    df["y"] = (df["label"] > 0).astype(int)
    drop = {"pair", "timeframe", "date", "label", "y", "t1_index"}
    X = df[[c for c in df.columns if c not in drop and c != "ts"]]
    y = df["y"]

    model = SwingXGB()
    model.fit(X, y)
    model.save(Path(args.out))
    print({"model": "swing_xgb", "rows": len(X), "path": args.out})


if __name__ == "__main__":
    main()
