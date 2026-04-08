from __future__ import annotations

import argparse
import json
from typing import Any

from fxstack.backtest.harness.contracts import EconomicReport, ScenarioSpec


DEFAULT_PHASE3_SCENARIOS = [
    ScenarioSpec(name="BaseCase"),
    ScenarioSpec(name="WideSpread", spread_multiplier=1.75),
    ScenarioSpec(name="SlippageShock", slippage_multiplier=2.0),
    ScenarioSpec(name="LatencyShock", latency_ms=750.0),
    ScenarioSpec(name="PartialFills", partial_fill_probability=0.35),
    ScenarioSpec(name="QuoteGap", quote_gap_probability=0.10),
    ScenarioSpec(name="SessionCutover", session_cutover_penalty_bps=1.5),
]


def apply_stress_scenario(*, report: EconomicReport, scenario: ScenarioSpec) -> EconomicReport:
    pnl_penalty = (
        (float(scenario.spread_multiplier) - 1.0) * 25.0
        + (float(scenario.slippage_multiplier) - 1.0) * 25.0
        + (float(scenario.latency_ms) / 1000.0) * 10.0
        + float(scenario.session_cutover_penalty_bps) * 4.0
        + float(scenario.quote_gap_probability) * 200.0
    )
    turnover_penalty = max(0.0, float(scenario.partial_fill_probability)) * 0.25
    return EconomicReport(
        engine=str(report.engine),
        pair=str(report.pair).upper(),
        status=str(report.status),
        realized_pnl_usd=float(report.realized_pnl_usd) - float(pnl_penalty),
        unrealized_pnl_usd=float(report.unrealized_pnl_usd),
        turnover_lots=float(report.turnover_lots) + turnover_penalty,
        max_drawdown_pct=float(report.max_drawdown_pct) + max(0.0, float(pnl_penalty) / 100.0),
        margin_utilization_peak=float(report.margin_utilization_peak) + max(0.0, float(turnover_penalty)),
        trade_count=int(report.trade_count),
        partial_fill_count=int(report.partial_fill_count) + int(max(0.0, float(scenario.partial_fill_probability)) * max(1, int(report.trade_count))),
        latency_ms_p95=float(report.latency_ms_p95) + float(scenario.latency_ms),
        rejection_rate=min(1.0, float(report.rejection_rate) + max(0.0, float(scenario.quote_gap_probability))),
        notes=list(report.notes) + [str(scenario.name)],
        metadata={**dict(report.metadata or {}), "scenario": scenario.to_dict()},
    )


def summarize_stress_results(*, base_report: EconomicReport, stressed_reports: list[EconomicReport]) -> dict[str, Any]:
    return {
        "status": "ok",
        "base": base_report.to_dict(),
        "scenarios": [item.to_dict() for item in stressed_reports],
        "worst_realized_pnl_usd": min([float(base_report.realized_pnl_usd)] + [float(item.realized_pnl_usd) for item in stressed_reports]),
        "worst_drawdown_pct": max([float(base_report.max_drawdown_pct)] + [float(item.max_drawdown_pct) for item in stressed_reports]),
        "scenario_count": int(len(stressed_reports)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply Phase 3 stress scenarios to a normalized economic report")
    ap.add_argument("--report-json", required=True)
    args = ap.parse_args()
    payload = json.loads(str(args.report_json))
    report = EconomicReport(**dict(payload or {}))
    stressed = [apply_stress_scenario(report=report, scenario=scenario) for scenario in DEFAULT_PHASE3_SCENARIOS]
    print(json.dumps(summarize_stress_results(base_report=report, stressed_reports=stressed), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
