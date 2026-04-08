from __future__ import annotations

import json
from pathlib import Path

from fxstack.training.phase5_gates import build_phase5_gate_bundle, write_phase5_gate_bundle


def test_phase5_gate_bundle_emits_expected_artifacts(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    def write_json(name: str, payload: dict[str, object]) -> Path:
        path = reports_root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    feature_schema = write_json("feature_schema.json", {"version": "1"})
    lineage = write_json("lineage.json", {"git_sha": "abc123", "feature_service_version": "fs1", "label_version": "lv1", "risk_config_version": "rv1"})
    model_manifest = write_json("model_manifest.json", {"bundle_run_id": "bundle-1"})
    backtest_summary = write_json("backtest_summary.json", {"net_pnl_usd": 125.0, "max_drawdown_pct": 2.5, "turnover_lots": 1.5})
    stress_summary = write_json("stress_harness_summary.json", {"scenario_count": 3, "worst_realized_pnl_usd": 75.0, "worst_drawdown_pct": 4.0})
    harness_comparison = write_json("harness_comparison.json", {"within_tolerance": True})
    execution_metrics = write_json("execution_metrics.json", {"filled_orders": 2})
    risk_trace_schema = write_json("risk_trace_schema.json", {"version": "p5"})
    phase3_refs = {
        "stress_harness_summary": str(stress_summary),
        "harness_comparison": str(harness_comparison),
        "execution_metrics": str(execution_metrics),
        "risk_trace_schema": str(risk_trace_schema),
    }

    bundle = build_phase5_gate_bundle(
        pair="EURUSD",
        reports_root=reports_root,
        backtest_summary={"net_pnl_usd": 125.0, "max_drawdown_pct": 2.5, "turnover_lots": 1.5},
        promotion_status="eligible",
        training_window_summary={"start_ts": "2025-01-01", "end_ts": "2025-02-01"},
        capabilities={"lifecycle_complete": True},
        training_eval_reports={"meta": "meta_report.json"},
        phase3_evidence_refs=phase3_refs,
        feature_schema_path=feature_schema,
        lineage_path=lineage,
        model_manifest_path=model_manifest,
        backtest_summary_path=backtest_summary,
        stress_summary_path=stress_summary,
        harness_comparison_path=harness_comparison,
        execution_metrics_path=execution_metrics,
        risk_trace_schema_path=risk_trace_schema,
        phase3_execution_required=True,
        phase4_shadow_only=True,
        phase4_sequence_dataset_manifests={"swing_patchtst": "swing_seq.json"},
        phase4_portfolio_reports={"swing_patchtst": "swing_portfolio.json"},
        phase4_challenger_reports={"swing_patchtst": "swing_challenger.json"},
    )

    payload = bundle.to_dict()
    assert payload["research_gate"]["gate"] == "research_gate"
    assert payload["economic_gate"]["passed"] is True
    assert payload["operational_gate"]["passed"] is True
    assert payload["shadow_gate"]["passed"] is True
    assert payload["canary_gate"]["passed"] is True
    assert payload["canary_closeout"]["passed"] is True
    assert payload["scorecard"]["economic_scorecard"]["realized_pnl_usd"] == 125.0
    assert payload["evidence_refs"]["feature_schema"] == str(feature_schema)

    out = write_phase5_gate_bundle(bundle, reports_root=reports_root)
    assert set(out) == {
        "research_gate",
        "economic_gate",
        "operational_gate",
        "shadow_gate",
        "canary_gate",
        "canary_closeout",
        "phase5_gate_bundle",
    }
    for key, path in out.items():
        assert Path(path).exists(), key

    bundle_json = json.loads(Path(out["phase5_gate_bundle"]).read_text(encoding="utf-8"))
    assert bundle_json["research_gate"]["gate"] == "research_gate"
    assert bundle_json["canary_closeout"]["passed"] is True
