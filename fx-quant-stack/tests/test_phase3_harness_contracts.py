from __future__ import annotations

from pathlib import Path

from fxstack.backtest.harness import (
    DEFAULT_PHASE3_SCENARIOS,
    EconomicReport,
    IntentReplayBundle,
    MarketReplayBundle,
    apply_stress_scenario,
    build_golden_dataset_report,
    parity_from_reports,
    run_lean_harness,
    run_nautilus_harness,
)
from fxstack.backtest.harness.contracts import PHASE3_HARNESS_MANIFEST_VERSION


def test_harness_reports_and_manifests_are_normalized(tmp_path: Path) -> None:
    base = EconomicReport(
        engine="internal",
        pair="EURUSD",
        status="completed",
        realized_pnl_usd=125.0,
        turnover_lots=1.5,
        max_drawdown_pct=2.0,
        trade_count=5,
    )
    comparison = EconomicReport(
        engine="nautilus",
        pair="EURUSD",
        status="completed",
        realized_pnl_usd=135.0,
        turnover_lots=1.55,
        max_drawdown_pct=2.1,
        trade_count=5,
    )
    parity = parity_from_reports(
        base_engine="internal",
        comparison_engine="nautilus",
        pair="EURUSD",
        base=base,
        comparison=comparison,
        tolerance={"realized_pnl_usd": 25.0, "max_drawdown_pct": 0.5, "turnover_lots": 0.25, "trade_count": 1.0},
    )
    assert parity.within_tolerance is True
    assert parity.deltas["realized_pnl_usd"] == 10.0

    stressed = apply_stress_scenario(report=base, scenario=DEFAULT_PHASE3_SCENARIOS[1])
    assert stressed.realized_pnl_usd < base.realized_pnl_usd

    market = MarketReplayBundle(pair="EURUSD", timeframe="M5", dataset_hash="abc123", feature_service_name="fx_eurusd_exec_m5", feature_service_version="v1")
    intents = IntentReplayBundle(pair="EURUSD", intents_path=str(tmp_path / "intents.json"), policy_version="p3", kernel_version="p3")
    golden = build_golden_dataset_report(market=market, intents=intents, row_count=42, schema_hash="schema42", feature_parity_score=0.99)
    assert golden["row_count"] == 42
    assert golden["market"]["pair"] == "EURUSD"

    nautilus = run_nautilus_harness(bundle_dir=tmp_path, output_dir=tmp_path / "nautilus", pair="EURUSD", execute=False)
    lean = run_lean_harness(bundle_dir=tmp_path, output_dir=tmp_path / "lean", pair="EURUSD", execute=False)
    assert nautilus.status == "planned"
    assert lean.status == "planned"
    assert nautilus.engine == "nautilus"
    assert lean.engine == "lean"
    assert nautilus.manifest_version == PHASE3_HARNESS_MANIFEST_VERSION
    assert lean.manifest_version == PHASE3_HARNESS_MANIFEST_VERSION
