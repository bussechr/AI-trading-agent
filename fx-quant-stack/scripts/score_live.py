from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from fxstack.data.live_quotes import fetch_bridge_ticks
from fxstack.io.parquet_store import ParquetStore
from fxstack.live.policy import compute_expected_edge_bps, normalize_spread_bps
from fxstack.live.scorer import LiveScorer
from fxstack.models.intraday_xgb import IntradayXGB
from fxstack.models.meta_filter import MetaFilterXGB
from fxstack.models.regime_hmm import RegimeHMM
from fxstack.models.swing_xgb import SwingXGB
from fxstack.settings import get_settings


def _latest_feature_row(*, pair: str, timeframe: str, feature_root: str) -> pd.DataFrame:
    provider = get_settings().normalized_data_provider
    df = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
    if df.empty:
        raise RuntimeError("No feature rows available")
    row = df.tail(1).copy()
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description="Score latest live row using trained baseline models")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--timeframe", default="M5")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--regime-model", default="artifacts/regime_hmm")
    ap.add_argument("--swing-model", default="artifacts/swing_xgb")
    ap.add_argument("--intraday-model", default="artifacts/intraday_xgb")
    ap.add_argument("--meta-model", default="artifacts/meta_filter")
    args = ap.parse_args()

    s = get_settings()
    ticks = fetch_bridge_ticks(s.mt4_bridge_url)
    tick = dict(ticks.get(args.pair.upper(), {}))

    row = _latest_feature_row(pair=args.pair.upper(), timeframe=args.timeframe, feature_root=args.feature_root)
    spread_bps, spread_unit_source = normalize_spread_bps(tick=tick, row=row.iloc[0], pair=args.pair.upper())
    model_features = row.drop(columns=[c for c in ["pair", "timeframe", "date", "ts"] if c in row.columns])

    regime = RegimeHMM.load(Path(args.regime_model))
    swing = SwingXGB.load(Path(args.swing_model))
    intraday = IntradayXGB.load(Path(args.intraday_model))
    meta = MetaFilterXGB.load(Path(args.meta_model))

    scorer = LiveScorer(regime_model=regime, swing_model=swing, intraday_model=intraday, meta_model=meta)
    signal = scorer.score(
        pd.concat([row[["pair", "ts"]].reset_index(drop=True), model_features.reset_index(drop=True)], axis=1),
        spread_bps=float(spread_bps),
        expected_edge_bps=float(compute_expected_edge_bps(row)),
        spread_unit_source=str(spread_unit_source),
    )
    print(signal.to_dict())


if __name__ == "__main__":
    main()
