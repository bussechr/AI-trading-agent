from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from fxstack.training.release_evidence import (
    EVIDENCE_IDENTITY_SCHEMA,
    FIXED_PHASE5_SUPPORT_EVIDENCE,
    SUPPORT_BINDING_SCHEMA,
    active_manifest_identity,
    candidate_manifest_identity,
    economic_sufficiency,
    file_sha256,
    read_json_object,
    support_evidence_errors,
    validate_economic_evidence,
    validate_release_validation_bundle,
    validate_shadow_runtime_evidence,
)


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
    evidence_identity: dict[str, Any]
    binding_required_evidence: list[str]
    research_gate: GateDecision
    economic_gate: GateDecision
    operational_gate: GateDecision
    shadow_gate: GateDecision
    canary_gate: GateDecision
    canary_closeout: GateDecision
    overall_status: GateStatus
    scorecard: dict[str, Any] = field(default_factory=dict)
    evidence_refs: dict[str, str] = field(default_factory=dict)
    evidence_hashes: dict[str, str] = field(default_factory=dict)

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
    report = {**dict(backtest_summary or {}), "stress_summary": dict(stress_summary or {})}
    passed, metrics = economic_sufficiency(report)
    realized = float(metrics["realized_pnl_usd"])
    drawdown = float(metrics["max_drawdown_pct"])
    turnover = float(metrics["turnover_lots"])
    trade_count = int(metrics["trade_count"])
    worst_stress_pnl = float(metrics["worst_stress_pnl"])
    worst_stress_drawdown = float(metrics["worst_stress_drawdown_pct"])
    scenario_count = int(metrics["scenario_count"])
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
                "trade_count": trade_count,
                "worst_stress_pnl": worst_stress_pnl,
                "worst_stress_drawdown_pct": worst_stress_drawdown,
                "scenario_count": scenario_count,
            },
        ),
        {
            "realized_pnl_usd": realized,
            "max_drawdown_pct": drawdown,
            "turnover_lots": turnover,
            "trade_count": trade_count,
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
    bundle_run_id: str = "",
    model_set_id: str,
    shadow_runtime_evidence_path: str | Path | None = None,
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
    model_manifest = _read_json(model_manifest_path)
    bound_bundle_run_id = str(bundle_run_id or model_manifest.get("bundle_run_id") or "").strip()
    bound_model_set_id = str(model_set_id or "").strip()
    candidate_identity = candidate_manifest_identity(
        manifest_path=model_manifest_path,
        pair=pair,
        model_set_id=bound_model_set_id,
    ) if model_manifest_ok else None
    model_manifest_sha256 = candidate_identity.model_manifest_sha256 if candidate_identity is not None else ""
    model_manifest_file_sha256 = file_sha256(model_manifest_path) if model_manifest_ok else ""
    artifact_set_sha256 = candidate_identity.artifact_set_sha256 if candidate_identity is not None else ""
    evidence_identity = {
        "schema_version": EVIDENCE_IDENTITY_SCHEMA,
        "pair": str(pair).upper(),
        "bundle_run_id": bound_bundle_run_id,
        "model_set_id": bound_model_set_id,
        "model_manifest_sha256": model_manifest_sha256,
        "artifact_set_sha256": artifact_set_sha256,
    }

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
    economic_validation = validate_economic_evidence(
        path=backtest_summary_path,
        expected_pair=str(pair).upper(),
        expected_bundle_run_id=bound_bundle_run_id,
        expected_model_set_id=bound_model_set_id,
        expected_model_manifest_sha256=model_manifest_sha256,
        expected_artifact_set_sha256=artifact_set_sha256,
    )
    economic_metrics_passed = bool(economic_gate.passed)
    economic_gate.passed = bool(economic_metrics_passed and economic_validation.valid)
    economic_gate.status = _status(economic_gate.passed, warn=economic_metrics_passed)
    economic_gate.reason = "economic_sufficiency_bound" if economic_gate.passed else (
        "economic_evidence_unbound" if not economic_validation.valid else "economic_shortfall"
    )
    economic_gate.details["metrics_passed"] = economic_metrics_passed
    economic_gate.details["evidence_validation"] = economic_validation.to_dict()
    economic_gate.evidence_refs.update(
        {
            "backtest_summary": str(backtest_summary_path),
            "stress_harness_summary": str(stress_summary_path or ""),
            "harness_comparison": str(harness_comparison_path or ""),
            "execution_metrics": str(execution_metrics_path or ""),
        }
    )

    training_evidence_present = bool(training_eval_reports) and all(
        _presence(path) for path in dict(training_eval_reports or {}).values()
    )
    phase3_evidence_present = bool(phase3_evidence_refs) and all(
        _presence(path) for path in dict(phase3_evidence_refs or {}).values() if str(path or "").strip()
    )
    fixed_support_present = all(
        [
            _presence(feature_schema_path),
            _presence(lineage_path),
            _presence(execution_metrics_path),
            _presence(risk_trace_schema_path),
            _presence(stress_summary_path),
        ]
    )
    operational_pass = all(
        [
            model_manifest_ok,
            feature_schema_ok,
            lineage_ok,
            backtest_ok,
            phase3_evidence_present,
            training_evidence_present,
            fixed_support_present,
        ]
    )
    operational_gate = GateDecision(
        gate="operational_gate",
        status=_status(operational_pass),
        passed=bool(operational_pass),
        reason="operator_evidence_complete" if operational_pass else "operator_evidence_missing",
        score=float(sum(1 for flag in [model_manifest_ok, feature_schema_ok, lineage_ok, backtest_ok, _presence(execution_metrics_path)] if flag)),
        details={
            "capabilities": dict(capabilities or {}),
            "training_eval_reports": dict(training_eval_reports or {}),
            "training_evidence_present": training_evidence_present,
            "phase3_evidence_refs": dict(phase3_evidence_refs or {}),
            "phase3_evidence_present": phase3_evidence_present,
            "fixed_support_present": fixed_support_present,
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

    shadow_validation = validate_shadow_runtime_evidence(
        path=shadow_runtime_evidence_path,
        expected_pair=str(pair).upper(),
        expected_bundle_run_id=bound_bundle_run_id,
        expected_model_set_id=bound_model_set_id,
        expected_artifact_set_sha256=artifact_set_sha256,
    )
    shadow_prerequisites_ready = (
        bool(phase4_shadow_only)
        and bool(promotion_status == "eligible")
        and bool(capabilities.get("lifecycle_complete", False))
    )
    shadow_ready = bool(shadow_prerequisites_ready and shadow_validation.valid)
    shadow_gate = GateDecision(
        gate="shadow_gate",
        status=_status(shadow_ready, warn=promotion_status == "eligible"),
        passed=bool(shadow_ready),
        reason="shadow_runtime_evidence_bound" if shadow_ready else "shadow_runtime_evidence_missing_or_invalid",
        score=float(1.0 if shadow_ready else 0.0),
        details={
            "phase4_shadow_only": bool(phase4_shadow_only),
            "promotion_status": str(promotion_status),
            "lifecycle_complete": bool(capabilities.get("lifecycle_complete", False)),
            "sequence_dataset_manifests": dict(phase4_sequence_dataset_manifests or {}),
            "portfolio_reports": dict(phase4_portfolio_reports or {}),
            "challenger_reports": dict(phase4_challenger_reports or {}),
            "prerequisites_ready": bool(shadow_prerequisites_ready),
            "evidence_validation": shadow_validation.to_dict(),
        },
        evidence_refs={"shadow_runtime_evidence": str(shadow_runtime_evidence_path or "")},
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

    top_evidence_refs = {
        "feature_schema": str(feature_schema_path),
        "lineage": str(lineage_path),
        "model_manifest": str(model_manifest_path),
        "backtest_summary": str(backtest_summary_path),
        "stress_harness_summary": str(stress_summary_path or ""),
        "harness_comparison": str(harness_comparison_path or ""),
        "execution_metrics": str(execution_metrics_path or ""),
        "risk_trace_schema": str(risk_trace_schema_path or ""),
        "shadow_runtime_evidence": str(shadow_runtime_evidence_path or ""),
        **{k: str(v) for k, v in dict(phase3_evidence_refs or {}).items()},
        **{f"training_eval:{k}": str(v) for k, v in dict(training_eval_reports or {}).items()},
    }
    support_keys = sorted(
        (set(FIXED_PHASE5_SUPPORT_EVIDENCE) - {"support_evidence_binding"})
        | {key for key in top_evidence_refs if key.startswith("training_eval:")}
    )
    support_binding_path = phase5_root / "support_evidence_binding.json"
    support_binding_path.write_text(
        json.dumps(
            {
                "schema_version": SUPPORT_BINDING_SCHEMA,
                "evidence_identity": dict(evidence_identity),
                "artifacts": {
                    key: {
                        "path": str(top_evidence_refs.get(key) or ""),
                        "sha256": file_sha256(top_evidence_refs[key]),
                    }
                    for key in support_keys
                    if str(top_evidence_refs.get(key) or "").strip()
                    and Path(str(top_evidence_refs[key])).is_file()
                },
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    top_evidence_refs["support_evidence_binding"] = str(support_binding_path)
    binding_required_evidence = sorted(
        key
        for key, value in top_evidence_refs.items()
        if key not in {"backtest_summary", "shadow_runtime_evidence", "model_manifest"}
        and str(value or "").strip()
    )
    evidence_hashes = {
        key: file_sha256(value)
        for key, value in top_evidence_refs.items()
        if str(value or "").strip() and Path(str(value)).is_file()
    }
    evidence_hashes.update(
        {
            "model_manifest": model_manifest_file_sha256,
            "backtest_summary": economic_validation.artifact_sha256,
            "shadow_runtime_evidence": shadow_validation.artifact_sha256,
        }
    )
    semantic_support_errors = support_evidence_errors(
        {
            "evidence_identity": evidence_identity,
            "binding_required_evidence": binding_required_evidence,
            "evidence_refs": top_evidence_refs,
            "evidence_hashes": evidence_hashes,
        }
    )
    operational_pass = bool(operational_gate.passed and not semantic_support_errors)
    operational_gate.passed = operational_pass
    operational_gate.status = _status(operational_pass)
    operational_gate.reason = (
        "operator_evidence_semantically_valid"
        if operational_pass
        else "operator_evidence_missing_or_invalid"
    )
    operational_gate.details["support_validation_errors"] = list(semantic_support_errors)
    canary_gate_pass = bool(
        research_gate.passed
        and economic_gate.passed
        and operational_gate.passed
        and shadow_gate.passed
    )
    canary_gate.passed = canary_gate_pass
    canary_gate.status = _status(canary_gate_pass, warn=not canary_gate_pass and shadow_gate.passed)
    canary_gate.reason = "canary_ready" if canary_gate_pass else "canary_blocked"
    canary_gate.details.update(
        {
            "research_gate": research_gate.to_dict(),
            "economic_gate": economic_gate.to_dict(),
            "operational_gate": operational_gate.to_dict(),
            "shadow_gate": shadow_gate.to_dict(),
        }
    )
    canary_closeout_pass = bool(
        canary_gate_pass
        and float(
            backtest_summary.get(
                "net_pnl_usd",
                backtest_summary.get("realized_pnl_usd", 0.0),
            )
            or 0.0
        )
        > 0.0
    )
    canary_closeout.passed = canary_closeout_pass
    canary_closeout.status = _status(canary_closeout_pass, warn=canary_gate_pass)
    canary_closeout.reason = (
        "canary_closeout_ready" if canary_closeout_pass else "canary_closeout_blocked"
    )
    bundle = Phase5GateBundle(
        bundle_version="phase5_gate_bundle_v2",
        pair=str(pair).upper(),
        evidence_identity=evidence_identity,
        binding_required_evidence=binding_required_evidence,
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
        evidence_refs=top_evidence_refs,
        evidence_hashes=evidence_hashes,
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


def bind_phase5_release_evidence(
    *,
    phase5_bundle_path: str | Path,
    model_manifest_path: str | Path,
    economic_evidence_path: str | Path,
    release_validation_bundle_path: str | Path,
    expected_pair: str,
    expected_bundle_run_id: str,
) -> dict[str, Any]:
    """Rebuild mutable gate decisions from immutable, identity-bound evidence bytes."""

    bundle_path = Path(phase5_bundle_path)
    payload = read_json_object(bundle_path)
    if not payload:
        raise ValueError("phase5_gate_bundle_missing_or_invalid")
    original_identity = dict(payload.get("evidence_identity") or {})
    candidate_identity = dict(payload.get("candidate_evidence_identity") or original_identity)
    prebind_errors: list[str] = []
    if str(payload.get("bundle_version") or "") != "phase5_gate_bundle_v2":
        prebind_errors.append("phase5_bundle_schema_invalid")
    if str(payload.get("pair") or "").strip().upper() != str(expected_pair).strip().upper():
        prebind_errors.append("phase5_bundle_pair_mismatch")
    if str(candidate_identity.get("schema_version") or "") != EVIDENCE_IDENTITY_SCHEMA:
        prebind_errors.append("phase5_bundle_identity_schema_invalid")
    if str(candidate_identity.get("bundle_run_id") or "") != str(expected_bundle_run_id):
        prebind_errors.append("phase5_bundle_run_id_mismatch")
    original_model_set_id = str(candidate_identity.get("model_set_id") or "").strip()
    if not original_model_set_id:
        prebind_errors.append("phase5_bundle_model_set_id_missing")
    refs_before = dict(payload.get("evidence_refs") or {})
    hashes_before = dict(payload.get("evidence_hashes") or {})
    candidate_manifest_path = Path(
        str(refs_before.get("candidate_model_manifest") or refs_before.get("model_manifest") or "").strip()
    )
    if not candidate_manifest_path.is_file():
        prebind_errors.append("candidate_model_manifest_missing")
    else:
        candidate_manifest_sha = file_sha256(candidate_manifest_path)
        if (
            candidate_manifest_sha != str(
                hashes_before.get("candidate_model_manifest")
                or hashes_before.get("model_manifest")
                or ""
            ).strip().lower()
        ):
            prebind_errors.append("candidate_model_manifest_hash_mismatch")
        resolved_candidate_identity = candidate_manifest_identity(
            manifest_path=candidate_manifest_path,
            pair=expected_pair,
            model_set_id=original_model_set_id,
        )
        if (
            resolved_candidate_identity.model_manifest_sha256
            != str(candidate_identity.get("model_manifest_sha256") or "").strip().lower()
        ):
            prebind_errors.append("candidate_model_identity_hash_mismatch")
        if (
            resolved_candidate_identity.artifact_set_sha256
            != str(candidate_identity.get("artifact_set_sha256") or "").strip().lower()
        ):
            prebind_errors.append("candidate_artifact_set_hash_mismatch")
    prebind_errors.extend(support_evidence_errors(payload))
    if prebind_errors:
        raise ValueError("phase5_prebind_invalid:" + ",".join(dict.fromkeys(prebind_errors)))
    active_identity = active_manifest_identity(
        manifest_path=model_manifest_path,
        pair=expected_pair,
    )
    if active_identity.bundle_run_id != str(expected_bundle_run_id):
        raise ValueError("active_manifest_bundle_run_id_mismatch")
    if active_identity.model_set_id != original_model_set_id:
        raise ValueError("active_manifest_model_set_id_mismatch")
    if active_identity.artifact_set_sha256 != str(candidate_identity.get("artifact_set_sha256") or "").strip().lower():
        raise ValueError("active_manifest_artifact_set_mismatch")
    if not active_identity.model_set_id or not active_identity.artifact_set_sha256:
        raise ValueError("active_manifest_model_identity_incomplete")
    active_manifest_source = Path(model_manifest_path)
    pinned_manifest_path = bundle_path.parent / f"{str(expected_pair).lower()}_bound_active_models.json"
    active_manifest_file_sha256 = file_sha256(active_manifest_source)
    if pinned_manifest_path.is_file() and file_sha256(pinned_manifest_path) != active_manifest_file_sha256:
        raise ValueError("pinned_active_manifest_conflict")
    if not pinned_manifest_path.is_file():
        pinned_manifest_path.write_bytes(active_manifest_source.read_bytes())
    if file_sha256(pinned_manifest_path) != active_manifest_file_sha256:
        raise ValueError("pinned_active_manifest_hash_mismatch")
    economic_validation = validate_economic_evidence(
        path=economic_evidence_path,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=active_identity.model_set_id,
        expected_model_manifest_sha256=active_identity.model_manifest_sha256,
        expected_artifact_set_sha256=active_identity.artifact_set_sha256,
    )
    release_validation = validate_release_validation_bundle(
        path=release_validation_bundle_path,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=active_identity.model_set_id,
        expected_model_manifest_sha256=active_identity.model_manifest_sha256,
        expected_artifact_set_sha256=active_identity.artifact_set_sha256,
    )
    finalization_payload = read_json_object(release_validation_bundle_path)
    finalization_artifacts = dict(finalization_payload.get("artifacts") or {})
    long_shadow_path = Path(
        str(dict(finalization_artifacts.get("long_shadow") or {}).get("path") or "").strip()
    )
    shadow_validation = validate_shadow_runtime_evidence(
        path=long_shadow_path,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=active_identity.model_set_id,
        expected_model_manifest_sha256=active_identity.model_manifest_sha256,
        expected_artifact_set_sha256=active_identity.artifact_set_sha256,
    )
    if not economic_validation.valid:
        raise ValueError("economic_evidence_invalid:" + ",".join(economic_validation.errors))
    if not release_validation.valid:
        raise ValueError("release_validation_bundle_invalid:" + ",".join(release_validation.errors))
    if not shadow_validation.valid:
        raise ValueError("shadow_evidence_invalid:" + ",".join(shadow_validation.errors))

    economic_payload = read_json_object(economic_evidence_path)
    canonical_economic_gate, economic_scorecard = _economic_report(
        economic_payload,
        dict(economic_payload.get("stress_summary") or {}),
    )
    economic_passed = bool(canonical_economic_gate.passed)
    realized = float(economic_scorecard.get("realized_pnl_usd", 0.0) or 0.0)
    economic_gate = dict(payload.get("economic_gate") or {})
    economic_gate.update(
        {
            "gate": "economic_gate",
            "passed": economic_passed,
            "status": "pass" if economic_passed else "fail",
            "reason": "economic_sufficiency_bound" if economic_passed else str(canonical_economic_gate.reason),
            "score": realized,
            "details": {
                **dict(economic_gate.get("details") or {}),
                "realized_pnl_usd": realized,
                **dict(canonical_economic_gate.details or {}),
                "metrics_passed": economic_passed,
                "evidence_validation": economic_validation.to_dict(),
            },
            "evidence_refs": {
                **dict(economic_gate.get("evidence_refs") or {}),
                "backtest_summary": str(Path(economic_evidence_path).resolve()),
            },
        }
    )

    shadow_gate = dict(payload.get("shadow_gate") or {})
    shadow_passed = bool(shadow_validation.valid)
    shadow_gate.update(
        {
            "gate": "shadow_gate",
            "passed": shadow_passed,
            "status": "pass" if shadow_passed else "fail",
            "reason": "shadow_runtime_evidence_bound" if shadow_passed else "shadow_runtime_evidence_invalid",
            "score": 1.0 if shadow_passed else 0.0,
            "details": {
                **dict(shadow_gate.get("details") or {}),
                "evidence_validation": shadow_validation.to_dict(),
            },
            "evidence_refs": {"shadow_runtime_evidence": str(long_shadow_path.resolve())},
        }
    )

    required_support = set(str(key) for key in list(payload.get("binding_required_evidence") or []))
    training_support = {key for key in required_support if key.startswith("training_eval:")}
    active_manifest = read_json_object(model_manifest_path)
    active_row = dict(dict(active_manifest.get("active_model_sets") or {}).get(str(expected_pair).upper()) or {})
    active_metadata = dict(active_row.get("metadata") or {})
    promotion_status = str(active_metadata.get("promotion_status") or "").strip().lower()
    active_capabilities = dict(active_metadata.get("capabilities") or {})
    lifecycle_complete = bool(
        active_metadata.get("lifecycle_complete", active_capabilities.get("lifecycle_complete", False))
    )
    research_passed = bool(promotion_status == "eligible" and lifecycle_complete)
    research_gate = dict(payload.get("research_gate") or {})
    research_gate.update(
        {
            "gate": "research_gate",
            "passed": research_passed,
            "status": "pass" if research_passed else "fail",
            "reason": "research_bundle_hashes_bound" if research_passed else "research_bundle_incomplete",
        }
    )
    operational_details = dict(dict(payload.get("operational_gate") or {}).get("details") or {})
    operational_passed = bool(
        training_support
        and FIXED_PHASE5_SUPPORT_EVIDENCE.issubset(required_support)
        and shadow_validation.valid
        and release_validation.valid
    )
    operational_gate = dict(payload.get("operational_gate") or {})
    operational_gate.update(
        {
            "gate": "operational_gate",
            "passed": operational_passed,
            "status": "pass" if operational_passed else "fail",
            "reason": "operator_evidence_hashes_bound" if operational_passed else "operator_evidence_missing",
            "details": {
                **operational_details,
                "binding_required_evidence": sorted(required_support),
                "training_evidence_present": bool(training_support),
                "release_validation": release_validation.to_dict(),
            },
        }
    )
    canary_passed = bool(research_passed and economic_passed and operational_passed and shadow_passed)
    canary_gate = dict(payload.get("canary_gate") or {})
    canary_gate.update(
        {
            "gate": "canary_gate",
            "passed": canary_passed,
            "status": "pass" if canary_passed else "fail",
            "reason": "canary_ready" if canary_passed else "canary_blocked",
            "details": {
                **dict(canary_gate.get("details") or {}),
                "research_gate": research_gate,
                "economic_gate": economic_gate,
                "operational_gate": operational_gate,
                "shadow_gate": shadow_gate,
            },
        }
    )
    closeout = dict(payload.get("canary_closeout") or {})
    closeout.update(
        {
            "gate": "canary_closeout",
            "passed": False,
            "status": "skip",
            "reason": "canary_not_started",
        }
    )
    payload.update(
        {
            "bundle_version": "phase5_gate_bundle_v2",
            "pair": active_identity.pair,
            "candidate_evidence_identity": candidate_identity,
            "evidence_identity": {
                "schema_version": EVIDENCE_IDENTITY_SCHEMA,
                "pair": active_identity.pair,
                "bundle_run_id": active_identity.bundle_run_id,
                "model_set_id": active_identity.model_set_id,
                "model_manifest_sha256": active_identity.model_manifest_sha256,
                "artifact_set_sha256": active_identity.artifact_set_sha256,
            },
            "economic_gate": economic_gate,
            "research_gate": research_gate,
            "operational_gate": operational_gate,
            "shadow_gate": shadow_gate,
            "canary_gate": canary_gate,
            "canary_closeout": closeout,
            "overall_status": "pass" if canary_passed else "warn",
            "evidence_refs": {
                **dict(payload.get("evidence_refs") or {}),
                "candidate_model_manifest": str(candidate_manifest_path.resolve()),
                "model_manifest": str(pinned_manifest_path.resolve()),
                "backtest_summary": str(Path(economic_evidence_path).resolve()),
                "shadow_runtime_evidence": str(long_shadow_path.resolve()),
                "release_validation_bundle": str(Path(release_validation_bundle_path).resolve()),
                **{
                    f"finalization:{name}": str(dict(ref or {}).get("path") or "")
                    for name, ref in finalization_artifacts.items()
                    if isinstance(ref, dict) and str(dict(ref or {}).get("path") or "").strip()
                },
            },
            "evidence_hashes": {
                **dict(payload.get("evidence_hashes") or {}),
                "candidate_model_manifest": file_sha256(candidate_manifest_path),
                "model_manifest": active_manifest_file_sha256,
                "backtest_summary": economic_validation.artifact_sha256,
                "shadow_runtime_evidence": shadow_validation.artifact_sha256,
                "release_validation_bundle": release_validation.artifact_sha256,
                **{
                    f"finalization:{name}": str(dict(ref or {}).get("sha256") or "")
                    for name, ref in finalization_artifacts.items()
                    if isinstance(ref, dict) and str(dict(ref or {}).get("sha256") or "").strip()
                },
            },
            "binding_required_evidence": sorted(
                required_support
                | {"candidate_model_manifest", "release_validation_bundle"}
                | {
                    f"finalization:{name}"
                    for name in ("fast_shadow", "long_shadow", "rollback_evidence", "blockers")
                }
            ),
        }
    )
    bundle_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    for gate_name in (
        "research_gate",
        "economic_gate",
        "operational_gate",
        "shadow_gate",
        "canary_gate",
        "canary_closeout",
    ):
        (bundle_path.parent / f"{gate_name}.json").write_text(
            json.dumps(dict(payload.get(gate_name) or {}), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    return payload
