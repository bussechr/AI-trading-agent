from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.backtest.engine import evaluate_signals
from fxstack.backtest.reports import summarize_backtest
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Run baseline cost-aware backtest summary")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="data/features")
    args = ap.parse_args()
    provider = get_settings().normalized_data_provider

    feats = ParquetStore(Path(args.feature_root)).read_pair_timeframe(
        provider=provider,
        pair=args.pair.upper(),
        timeframe=args.timeframe,
    )
    if feats.empty:
        raise SystemExit("No feature rows")

    # Minimal placeholder signal frame using feature-derived proxy edge.
    signals = feats[["pair", "ts"]].copy()
    signals["expected_edge_bps"] = feats["ret_1"].astype(float) * 10000.0
    signals["spread_bps"] = feats.get("spread", 0.0).astype(float) * 10000.0
    signals["allowed"] = True

    scored = evaluate_signals(signals)
    print(summarize_backtest(scored))


if __name__ == "__main__":
    main()
