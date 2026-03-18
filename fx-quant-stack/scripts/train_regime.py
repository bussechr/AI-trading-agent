from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.io.parquet_store import ParquetStore
from fxstack.models.regime_hmm import RegimeHMM
from fxstack.settings import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Train HMM regime model")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="H4")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--out", default="artifacts/regime_hmm")
    args = ap.parse_args()
    provider = get_settings().normalized_data_provider

    df = ParquetStore(Path(args.feature_root)).read_pair_timeframe(
        provider=provider,
        pair=args.pair.upper(),
        timeframe=args.timeframe,
    )
    if df.empty:
        raise SystemExit("No features found")

    cols = [c for c in ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"] if c in df.columns]
    X = df[cols]
    model = RegimeHMM()
    model.fit(X)
    model.save(Path(args.out))
    print({"model": "regime_hmm", "rows": len(X), "path": args.out})


if __name__ == "__main__":
    main()
