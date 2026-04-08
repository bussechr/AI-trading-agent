from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from fxstack.backtest.harness.contracts import EconomicReport


GateStatus = Literal["pass", "warn", "fail", "skip"]


@dataclass(slots=True)
class GateDecision:
    gate: str
    status: GateStatus
    passed: bool
    reason: str = ""
    score: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)
    evidence_refs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Phase5GateBundle:
    bundle_version: str
    pair: str
    research_gate: GateDecision
    economic_gate: GateDecision
    operational_gate: GateDecision
    shadow_gate: GateDecision
    canary_gate: GateDecision
    canary_closeout: GateDecision
    overall_status: GateStatus
    scorecard: dict[str, Any] = field(default_factory=dict)
    evidence_refs: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["research_gate"] = self.research_gate.to_dict()
        payload["economic_gate"] = self.economic_gate.to_dict()
        payload["operational_gate"] = self.operational_gate.to_dict()
        payload["shadow_gate"] = self.shadow_gate.to_dict()
        payload["canary_gate"] = self.canary_gate.to_dict()
        payload["canary_closeout"] = self.canary_closeout.to_dict()
        return payload


def _status(passed: bool, *, warn: bool = False) -> GateStatus:
    if passed:
        return "pass"
    return "warn" if warn else "fail"


def _presence(value: str | Path | None) -> bool:
    return bool(str(value or "").strip()) and Path(str(value)).exists()


def _read_json(path: str | Path | None) -> dict[str, Any]:
    if not _presence(path):
        return {}
    import json

    try:
        return dict(json.loads(Path(str(path)).read_text(encoding="utf-8")) or {})
    except Exception:
        return {}


def _economic_report(backtest_summary: dict[str, Any], stress_summary: dict[str, Any] | None) -> tuple[GateDecision, dict[str, Any]]:
    realized = float(backtest_summary.get("net_pnl_usd", backtest_summary.get("realized_pnl_usd", 0.0)) or 0.0)
    drawdown = float(backtest_summary.get("max_drawdown_pct", 0.0) or 0.0)
    turnover = float(backtest_summary.get("turnover_lots", 0.0) or 0.0)
    worst_stress_pnl = None
    worst_stress_drawdown = None
    scenario_count = 0
    if stress_summary:
        worst_stress_pnl = float(stress_summary.get("worst_realized_pnl_usd", realized) or realized)
        worst_stress_drawdown = float(stress_summary.get("worst_drawdown_pct", drawdown) or drawdown)
        scenario_count = int(stress_summary.get("scenario_count", 0) or 0)
    passed = realized > 0.0 and drawdown < 25.0 and (worst_stress_pnl is None or worst_stress_pnl > -50.0)
    return (
        GateDecision(
            gate="economic_gate",
            status=_status(passed, warn=not passed and realized > 0.0),
            passed=bool(passed),
            reason="economic_sufficiency" if passed else "economic_shortfall",
            score=float(realized),
            details={
                "realized_pnl_usd": realized,
                "max_drawdown_pct": drawdown,
                "turnover_lots": turnover,
                "worst_stress_pnl": worst_stress_pnl,
                "worst_stress_drawdown_pct": worst_stress_drawdown,
                "scenario_count": scenario_count,
            },
        ),
        {
            "realized_pnl_usd": realized,
            "max_drawdown_pct": drawdown,
            "turnover_lots": turnover,
            "worst_stress_pnl": worst_stress_pnl,
            "worst_stress_drawdown_pct": worst_stress_drawdown,
            "scenario_count": scenario_count,
        },
    )


def build_phase5_gate_bundle(
    *,
    pair: str,
    reports_root: str | Path,
    backtest_summary: dict[str, Any],
    promotion_status: str,
    training_window_summary: dict[str, Any],
    capabilities: dict[str, Any],
    training_eval_reports: dict[str, str],
    phase3_evidence_refs: dict[str, str],
    feature_schema_path: str | Path,
    lineage_path: str | Path,
    model_manifest_path: str | Path,
    backtest_summary_path: str | Path,
    stress_summary_path: str | Path | None = None,
    harness_comparison_path: str | Path | None = None,
    execution_metrics_path: str | Path | None = None,
    risk_trace_schema_path: str | Path | None = None,
    phase3_execution_required: bool = True,
    phase4_shadow_only: bool = True,
    phase4_sequence_dataset_manifests: dict[str, str] | None = None,
    phase4_portfolio_reports: dict[str, str] | None = None,
    phase4_challenger_reports: dict[str, str] | None = None,
) -> Phase5GateBundle:
    reports_root_path = Path(reports_root)
    phase5_root = reports_root_path / "phase5"
    phase5_root.mkdir(parents=True, exist_ok=True)

    feature_schema_ok = _presence(feature_schema_path)
    lineage_ok = _presence(lineage_path)
    model_manifest_ok = _presence(model_manifest_path)
    backtest_ok = _presence(backtest_summary_path)
    stress_summary = _read_json(stress_summary_path)
    harness_comparison = _read_json(harness_comparison_path)
    execution_metrics = _read_json(execution_metrics_path)
    risk_trace_schema = _read_json(risk_trace_schema_path)

    report_bundle_present = all(
        _presence(path)
        for path in [
            backtest_summary_path,
            feature_schema_path,
            lineage_path,
            model_manifest_path,
        ]
    )
    research_gate = GateDecision(
        gate="research_gate",
        status=_status(report_bundle_present and promotion_status != "research_only", warn=report_bundle_present),
        passed=bool(report_bundle_present and promotion_status != "research_only"),
        reason="research_bundle_ready" if report_bundle_present else "research_bundle_incomplete",
        score=float(len([feature_schema_ok, lineage_ok, model_manifest_ok, backtest_ok])),
        details={
            "feature_schema_ok": feature_schema_ok,
            "lineage_ok": lineage_ok,
            "model_manifest_ok": model_manifest_ok,
            "backtest_summary_ok": backtest_ok,
            "promotion_status": str(promotion_status),
            "training_window_summary": dict(training_window_summary or {}),
        },
        evidence_refs={
            "feature_schema": str(feature_schema_path),
            "lineage": str(lineage_path),
            "model_manifest": str(model_manifest_path),
            "backtest_summary": str(backtest_summary_path),
        },
    )

    economic_gate, economic_scorecard = _economic_report(backtest_summary, stress_summary)
    economic_gate.evidence_refs.update(
        {
            "backtest_summary": str(backtest_summary_path),
            "stress_harness_summary": str(stress_summary_path or ""),
            "harness_comparison": str(harness_comparison_path or ""),
            "execution_metrics": str(execution_metrics_path or ""),
        }
    )

    operational_pass = all(
        [
            model_manifest_ok,
            feature_schema_ok,
            lineage_ok,
            backtest_ok,
            bool(phase3_evidence_refs),
            bool(training_eval_reports),
        ]
    )
    if phase3_execution_required and not _presence(execution_metrics_path):
        operational_pass = False
    operational_gate = GateDecision(
        gate="operational_gate",
        status=_status(operational_pass),
        passed=bool(operational_pass),
        reason="operator_evidence_complete" if operational_pass else "operator_evidence_missing",
        score=float(sum(1 for flag in [model_manifest_ok, feature_schema_ok, lineage_ok, backtest_ok, _presence(execution_metrics_path)] if flag)),
        details={
            "capabilities": dict(capabilities or {}),
            "training_eval_reports": dict(training_eval_reports or {}),
            "phase3_evidence_refs": dict(phase3_evidence_refs or {}),
            "phase3_execution_required": bool(phase3_execution_required),
            "risk_trace_schema_present": bool(risk_trace_schema),
            "feature_schema_present": feature_schema_ok,
            "lineage_present": lineage_ok,
            "model_manifest_present": model_manifest_ok,
            "execution_metrics_present": _presence(execution_metrics_path),
        },
        evidence_refs={
            "execution_metrics": str(execution_metrics_path or ""),
            "risk_trace_schema": str(risk_trace_schema_path or ""),
            "harness_comparison": str(harness_comparison_path or ""),
            "stress_harness_summary": str(stress_summary_path or ""),
            **{k: str(v) for k, v in dict(phase3_evidence_refs or {}).items()},
        },
    )

    shadow_ready = bool(phase4_shadow_only) and bool(promotion_status == "eligible") and bool(capabilities.get("lifecycle_complete", False))
    shadow_gate = GateDecision(
        gate="shadow_gate",
        status=_status(shadow_ready, warn=promotion_status == "eligible"),
        passed=bool(shadow_ready),
        reason="shadow_ready" if shadow_ready else "shadow_not_ready",
        score=float(1.0 if shadow_ready else 0.0),
        details={
            "phase4_shadow_only": bool(phase4_shadow_only),
            "promotion_status": str(promotion_status),
            "lifecycle_complete": bool(capabilities.get("lifecycle_complete", False)),
            "sequence_dataset_manifests": dict(phase4_sequence_dataset_manifests or {}),
            "portfolio_reports": dict(phase4_portfolio_reports or {}),
            "challenger_reports": dict(phase4_challenger_reports or {}),
        },
    )

    canary_gate_pass = bool(economic_gate.passed and operational_gate.passed and shadow_gate.passed)
    canary_gate = GateDecision(
        gate="canary_gate",
        status=_status(canary_gate_pass, warn=not canary_gate_pass and shadow_gate.passed),
        passed=bool(canary_gate_pass),
        reason="canary_ready" if canary_gate_pass else "canary_blocked",
        score=float(economic_scorecard.get("realized_pnl_usd", 0.0)),
        details={
            "economic_gate": economic_gate.to_dict(),
            "operational_gate": operational_gate.to_dict(),
            "shadow_gate": shadow_gate.to_dict(),
            "harness_comparison": harness_comparison,
        },
    )

    canary_closeout_pass = bool(canary_gate_pass and float(backtest_summary.get("net_pnl_usd", backtest_summary.get("realized_pnl_usd", 0.0)) or 0.0) > 0.0)
    canary_closeout = GateDecision(
        gate="canary_closeout",
        status=_status(canary_closeout_pass, warn=canary_gate_pass),
        passed=bool(canary_closeout_pass),
        reason="canary_closeout_ready" if canary_closeout_pass else "canary_closeout_blocked",
        score=float(backtest_summary.get("net_pnl_usd", backtest_summary.get("realized_pnl_usd", 0.0)) or 0.0),
        details={
            "canary_gate": canary_gate.to_dict(),
            "economic_gate": economic_gate.to_dict(),
            "execution_metrics": execution_metrics,
            "risk_trace_schema": risk_trace_schema,
        },
    )

    bundle = Phase5GateBundle(
        bundle_version="phase5_gate_bundle_v1",
        pair=str(pair).upper(),
        research_gate=research_gate,
        economic_gate=economic_gate,
        operational_gate=operational_gate,
        shadow_gate=shadow_gate,
        canary_gate=canary_gate,
        canary_closeout=canary_closeout,
        overall_status="pass" if all(item.passed for item in [research_gate, economic_gate, operational_gate, shadow_gate, canary_gate, canary_closeout]) else "warn",
        scorecard={
            "research": research_gate.score,
            "economic": economic_gate.score,
            "operational": operational_gate.score,
            "shadow": shadow_gate.score,
            "canary": canary_gate.score,
            "canary_closeout": canary_closeout.score,
            "economic_scorecard": economic_scorecard,
        },
        evidence_refs={
            "feature_schema": str(feature_schema_path),
            "lineage": str(lineage_path),
            "model_manifest": str(model_manifest_path),
            "backtest_summary": str(backtest_summary_path),
            "stress_harness_summary": str(stress_summary_path or ""),
            "harness_comparison": str(harness_comparison_path or ""),
            "execution_metrics": str(execution_metrics_path or ""),
            "risk_trace_schema": str(risk_trace_schema_path or ""),
            **{k: str(v) for k, v in dict(phase3_evidence_refs or {}).items()},
        },
    )

    return bundle


def write_phase5_gate_bundle(bundle: Phase5GateBundle, *, reports_root: str | Path) -> dict[str, str]:
    reports_root_path = Path(reports_root)
    phase5_root = reports_root_path / "phase5"
    phase5_root.mkdir(parents=True, exist_ok=True)

    import json

    payload = bundle.to_dict()
    out: dict[str, str] = {}
    for name in [
        "research_gate",
        "economic_gate",
        "operational_gate",
        "shadow_gate",
        "canary_gate",
        "canary_closeout",
    ]:
        path = phase5_root / f"{name}.json"
        path.write_text(json.dumps(payload[name], indent=2, sort_keys=True), encoding="utf-8")
        out[name] = str(path)
    bundle_path = phase5_root / "phase5_gate_bundle.json"
    bundle_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    out["phase5_gate_bundle"] = str(bundle_path)
    return out

