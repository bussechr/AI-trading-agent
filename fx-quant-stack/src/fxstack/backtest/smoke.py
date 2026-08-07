"""Shared cost-aware baseline smoke evaluation for external CLIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def run_baseline_smoke(*, pair: str, timeframe: str, feature_root: str) -> tuple[dict[str, Any], int]:
    from fxstack.backtest.engine import evaluate_signals
    from fxstack.backtest.reports import summarize_backtest
    from fxstack.io.parquet_store import ParquetStore
    from fxstack.live.policy import EDGE_FORMULA_ID, compute_expected_edge_bps, normalize_spread_bps
    from fxstack.settings import get_settings

    settings = get_settings()
    normalized_pair = str(pair).upper()
    normalized_timeframe = str(timeframe).upper()
    features = ParquetStore(Path(str(feature_root))).read_pair_timeframe(
        provider=settings.normalized_data_provider,
        pair=normalized_pair,
        timeframe=normalized_timeframe,
    )
    if features.empty:
        return {"error": "no feature rows"}, 1

    signals = features[["pair", "ts"]].copy()
    signals["expected_edge_bps"] = features.apply(compute_expected_edge_bps, axis=1).astype(float)
    spread_normalized = features.apply(
        lambda row: normalize_spread_bps(row=row, pair=str(row.get("pair", "")).upper()),
        axis=1,
        result_type="expand",
    )
    signals["spread_bps"] = spread_normalized[0].astype(float)
    signals["spread_unit_source"] = spread_normalized[1].astype(str)
    signals["allowed"] = True
    summary: dict[str, Any] = dict(summarize_backtest(evaluate_signals(signals)))
    summary["policy_version"] = str(settings.policy_version)
    summary["edge_formula_id"] = EDGE_FORMULA_ID
    summary["spread_conversion_method"] = "normalize_spread_bps"
    return summary, 0
