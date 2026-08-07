from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.backtest.harness.contracts import (  # noqa: E402
    REQUIRED_EXTERNAL_STRESS_SCENARIOS,
    EconomicReport,
)
from fxstack.backtest.harness.stress import summarize_stress_results  # noqa: E402
from fxstack.training.release_evidence import (  # noqa: E402
    ReleaseEvidenceIdentity,
    active_manifest_identity,
    file_sha256,
    validate_external_harness_source_chain,
)


def assemble_economic_evidence(
    *,
    pair: str,
    model_manifest_path: str | Path,
    harness_manifest_path: str | Path,
    economic_report_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    expected = active_manifest_identity(manifest_path=model_manifest_path, pair=pair)
    if not expected.bundle_run_id:
        raise ValueError("selected pair is missing model_set_id in model manifest")

    harness_path = Path(harness_manifest_path).resolve()
    report_path = Path(economic_report_path).resolve()
    errors, harness, report, external_stress_reports = validate_external_harness_source_chain(
        model_manifest_path=model_manifest_path,
        harness_manifest_path=harness_path,
        economic_report_path=report_path,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
    )
    if errors:
        raise ValueError("invalid independent economic evidence:" + ",".join(dict.fromkeys(errors)))
    engine = str(harness.get("engine") or "").strip().lower()

    identity = ReleaseEvidenceIdentity(
        pair=expected.pair,
        bundle_run_id=expected.bundle_run_id,
        model_set_id=expected.model_set_id,
        model_manifest_sha256=expected.model_manifest_sha256,
        artifact_set_sha256=expected.artifact_set_sha256,
        evidence_kind="economic_validation",
        source_kind="independent_execution_harness",
        advisory_only=False,
    )
    normalized_report = EconomicReport(
        engine=engine,
        pair=expected.pair,
        status="complete",
        realized_pnl_usd=float(report.get("realized_pnl_usd", 0.0) or 0.0),
        unrealized_pnl_usd=float(report.get("unrealized_pnl_usd", 0.0) or 0.0),
        turnover_lots=float(report.get("turnover_lots", 0.0) or 0.0),
        max_drawdown_pct=float(report.get("max_drawdown_pct", 0.0) or 0.0),
        trade_count=int(report.get("trade_count", 0) or 0),
        partial_fill_count=int(report.get("partial_fill_count", 0) or 0),
        latency_ms_p95=float(report.get("latency_ms_p95", 0.0) or 0.0),
        rejection_rate=float(report.get("rejection_rate", 0.0) or 0.0),
        notes=list(report.get("notes") or []),
        metadata=dict(report.get("metadata") or {}),
    )
    normalized_stress_reports = [
        EconomicReport(
            engine=engine,
            pair=expected.pair,
            status="complete",
            realized_pnl_usd=float(external_stress_reports[name].get("realized_pnl_usd", 0.0) or 0.0),
            unrealized_pnl_usd=float(external_stress_reports[name].get("unrealized_pnl_usd", 0.0) or 0.0),
            turnover_lots=float(external_stress_reports[name].get("turnover_lots", 0.0) or 0.0),
            max_drawdown_pct=float(external_stress_reports[name].get("max_drawdown_pct", 0.0) or 0.0),
            trade_count=int(external_stress_reports[name].get("trade_count", 0) or 0),
            partial_fill_count=int(external_stress_reports[name].get("partial_fill_count", 0) or 0),
            latency_ms_p95=float(external_stress_reports[name].get("latency_ms_p95", 0.0) or 0.0),
            rejection_rate=float(external_stress_reports[name].get("rejection_rate", 0.0) or 0.0),
            notes=list(external_stress_reports[name].get("notes") or []) + [name],
            metadata={
                **dict(external_stress_reports[name].get("metadata") or {}),
                "scenario": name,
            },
        )
        for name in REQUIRED_EXTERNAL_STRESS_SCENARIOS
    ]
    stress_summary = summarize_stress_results(
        base_report=normalized_report,
        stressed_reports=normalized_stress_reports,
    )
    artifacts = dict(harness.get("artifacts") or {})
    declared_stress_paths = dict(artifacts.get("stress_reports") or {})
    report_sha = file_sha256(report_path)
    output = {
        **normalized_report.to_dict(),
        "status": "complete",
        "net_pnl_usd": float(normalized_report.realized_pnl_usd),
        "evidence_identity": identity.to_dict(),
        "stress_summary": stress_summary,
        "source_artifacts": {
            "model_manifest": str(Path(model_manifest_path).resolve()),
            "model_manifest_sha256": file_sha256(model_manifest_path),
            "harness_manifest": str(harness_path),
            "harness_manifest_sha256": file_sha256(harness_path),
            "economic_report": str(report_path),
            "economic_report_sha256": report_sha,
            "stress_reports": {
                name: {
                    "path": str(Path(declared_stress_paths[name]).resolve()),
                    "sha256": file_sha256(declared_stress_paths[name]),
                }
                for name in REQUIRED_EXTERNAL_STRESS_SCENARIOS
            },
        },
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bind a completed independent harness report to one exact active model set.")
    parser.add_argument("--pair", required=True)
    parser.add_argument("--model-manifest", required=True)
    parser.add_argument("--harness-manifest", required=True)
    parser.add_argument("--economic-report", required=True)
    parser.add_argument("--out", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    assemble_economic_evidence(
        pair=args.pair,
        model_manifest_path=args.model_manifest,
        harness_manifest_path=args.harness_manifest,
        economic_report_path=args.economic_report,
        output_path=args.out,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
