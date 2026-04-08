from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fxstack.backtest.harness.contracts import EconomicReport, HarnessRunManifest, IntentReplayBundle, MarketReplayBundle, ParityReport


def build_golden_dataset_report(
    *,
    market: MarketReplayBundle,
    intents: IntentReplayBundle,
    row_count: int = 0,
    schema_hash: str = "",
    feature_parity_score: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "market": market.to_dict(),
        "intents": intents.to_dict(),
        "row_count": int(row_count),
        "schema_hash": str(schema_hash or market.dataset_hash),
        "feature_parity_score": float(feature_parity_score),
        "metadata": dict(metadata or {}),
    }


def build_harness_comparison(
    *,
    internal_report: EconomicReport,
    nautilus_report: EconomicReport,
    lean_report: EconomicReport,
    parity_reports: list[ParityReport],
    manifests: list[HarnessRunManifest],
) -> dict[str, Any]:
    return {
        "status": "ok",
        "reports": {
            "internal": internal_report.to_dict(),
            "nautilus": nautilus_report.to_dict(),
            "lean": lean_report.to_dict(),
        },
        "parity": [item.to_dict() for item in parity_reports],
        "manifests": [item.to_dict() for item in manifests],
    }


def parity_from_reports(
    *,
    base_engine: str,
    comparison_engine: str,
    pair: str,
    base: EconomicReport,
    comparison: EconomicReport,
    tolerance: dict[str, float] | None = None,
) -> ParityReport:
    limits = dict(tolerance or {"realized_pnl_usd": 50.0, "max_drawdown_pct": 0.5, "turnover_lots": 0.25})
    deltas = {
        "realized_pnl_usd": float(comparison.realized_pnl_usd) - float(base.realized_pnl_usd),
        "max_drawdown_pct": float(comparison.max_drawdown_pct) - float(base.max_drawdown_pct),
        "turnover_lots": float(comparison.turnover_lots) - float(base.turnover_lots),
        "trade_count": float(comparison.trade_count) - float(base.trade_count),
    }
    within = all(abs(float(value)) <= float(limits.get(key, float("inf"))) for key, value in deltas.items())
    return ParityReport(
        base_engine=str(base_engine),
        comparison_engine=str(comparison_engine),
        pair=str(pair).upper(),
        within_tolerance=bool(within),
        tolerance={str(k): float(v) for k, v in limits.items()},
        deltas={str(k): float(v) for k, v in deltas.items()},
        metadata={"base_status": str(base.status), "comparison_status": str(comparison.status)},
    )
