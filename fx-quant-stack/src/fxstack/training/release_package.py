from __future__ import annotations

from typing import Any

from fxstack.mlops.types import (
    ActivationPackage,
    BundleManifest,
    CanaryPlan,
    PromotionGateResult,
    ReleaseNote,
    RollbackPlan,
)


PHASE5_ACTIVATION_PACKAGE_VERSION = "phase5_activation_package_v1"


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _list_payload(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _string_list_payload(value: Any) -> list[str]:
    return [str(item) for item in _list_payload(value) if str(item).strip()]


def normalize_release_note(value: Any) -> ReleaseNote | None:
    payload = value.to_dict() if isinstance(value, ReleaseNote) else _dict_payload(value)
    return ReleaseNote.from_dict(payload) if payload else None


def normalize_promotion_gate(value: Any) -> PromotionGateResult | None:
    payload = value.to_dict() if isinstance(value, PromotionGateResult) else _dict_payload(value)
    return PromotionGateResult.from_dict(payload) if payload else None


def normalize_canary_plan(value: Any) -> CanaryPlan | None:
    payload = value.to_dict() if isinstance(value, CanaryPlan) else _dict_payload(value)
    return CanaryPlan.from_dict(payload) if payload else None


def normalize_rollback_plan(value: Any) -> RollbackPlan | None:
    payload = value.to_dict() if isinstance(value, RollbackPlan) else _dict_payload(value)
    return RollbackPlan.from_dict(payload) if payload else None


def summarize_promotion_gates(value: Any) -> dict[str, Any]:
    gates = [
        gate
        for gate in (normalize_promotion_gate(item) for item in _list_payload(value))
        if gate is not None
    ]
    gate_ids = [str(item.gate_id) for item in gates if str(item.gate_id).strip()]
    passed_gate_ids = [str(item.gate_id) for item in gates if item.passed is True and str(item.gate_id).strip()]
    failed_gate_ids = [str(item.gate_id) for item in gates if item.passed is False and str(item.gate_id).strip()]
    pending_gate_ids = [str(item.gate_id) for item in gates if item.passed is None and str(item.gate_id).strip()]
    required_gate_ids = [str(item.gate_id) for item in gates if item.required and str(item.gate_id).strip()]
    all_required_passed = bool(required_gate_ids) and not failed_gate_ids and not pending_gate_ids and len(passed_gate_ids) >= len(required_gate_ids)
    summary_status = "passed" if all_required_passed else "blocked" if failed_gate_ids else "pending" if pending_gate_ids else "empty"
    return {
        "status": summary_status,
        "gate_count": len(gates),
        "required_gate_count": len(required_gate_ids),
        "passed_gate_count": len(passed_gate_ids),
        "failed_gate_count": len(failed_gate_ids),
        "pending_gate_count": len(pending_gate_ids),
        "gate_ids": gate_ids,
        "passed_gate_ids": passed_gate_ids,
        "failed_gate_ids": failed_gate_ids,
        "pending_gate_ids": pending_gate_ids,
        "required_gate_ids": required_gate_ids,
        "all_required_passed": all_required_passed,
    }


def summarize_shadow_acceptance(package: ActivationPackage | None) -> dict[str, Any]:
    if package is None:
        return {
            "status": "missing",
            "ready": False,
            "bundle_run_id": "",
            "pair": "",
            "release_status": "",
            "gate_summary": summarize_promotion_gates([]),
        }
    gate_summary = summarize_promotion_gates(list(package.promotion_gates or []))
    return {
        "status": "ready" if gate_summary.get("all_required_passed") else str(package.release_status or "blocked"),
        "ready": bool(gate_summary.get("all_required_passed", False)),
        "bundle_run_id": str(package.bundle_run_id or ""),
        "pair": str(package.pair or ""),
        "release_status": str(package.release_status or ""),
        "gate_summary": gate_summary,
        "required_gate_ids": list(gate_summary.get("required_gate_ids") or []),
        "passed_gate_ids": list(gate_summary.get("passed_gate_ids") or []),
        "failed_gate_ids": list(gate_summary.get("failed_gate_ids") or []),
    }


def canary_prep_metadata(package: ActivationPackage | None) -> dict[str, Any]:
    if package is None:
        return {
            "status": "missing",
            "bundle_run_id": "",
            "pair": "",
            "allowlisted_pairs": [],
            "budget_scale": 0.0,
            "duration_minutes": 0,
            "metrics_window_minutes": 0,
            "release_status": "",
        }
    canary_plan = package.canary_plan
    metadata = dict(canary_plan.metadata or {}) if canary_plan is not None else {}
    allowlisted_pairs = [
        str(item).upper()
        for item in _string_list_payload(metadata.get("allowlisted_pairs"))
    ]
    return {
        "status": str(canary_plan.status or package.release_status or "") if canary_plan is not None else str(package.release_status or ""),
        "bundle_run_id": str(package.bundle_run_id or ""),
        "pair": str(package.pair or ""),
        "release_status": str(package.release_status or ""),
        "allowlisted_pairs": allowlisted_pairs,
        "budget_scale": float(metadata.get("budget_scale") or 0.0),
        "duration_minutes": int(canary_plan.duration_minutes if canary_plan is not None else 0),
        "metrics_window_minutes": int(canary_plan.metrics_window_minutes if canary_plan is not None else 0),
        "traffic_fraction": float(canary_plan.traffic_fraction if canary_plan is not None else 0.0),
        "plan_id": str(canary_plan.plan_id or "") if canary_plan is not None else "",
        "scope": str(canary_plan.scope or "") if canary_plan is not None else "",
        "started_at": float(metadata.get("started_at") or 0.0),
        "last_checked_at": float(metadata.get("last_checked_at") or 0.0),
        "rollback_reason": str(metadata.get("rollback_reason") or ""),
        "rolled_back_at": float(metadata.get("rolled_back_at") or 0.0),
    }


def infer_bundle_runtime_compatible(bundle: BundleManifest) -> bool:
    metadata_value = (bundle.metadata or {}).get("runtime_compatible")
    if metadata_value is False:
        return False
    return all(bool(getattr(ref, "runtime_compatible", True)) for ref in dict(bundle.components or {}).values())


def build_activation_package(
    *,
    bundle_run_id: str,
    pair: str,
    target_alias: str,
    promotion_status: str = "",
    runtime_compatible: bool = True,
    dataset_fingerprint: str = "",
    feature_service_version: str = "",
    risk_config_version: str = "",
    release_status: str = "",
    rollback_target: Any = None,
    operator_signoff: Any = None,
    canary_plan: Any = None,
    promotion_gates: Any = None,
    release_notes: Any = None,
    activation_package: Any = None,
    evidence_refs: dict[str, str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> ActivationPackage:
    base = ActivationPackage.from_dict(activation_package.to_dict()) if isinstance(activation_package, ActivationPackage) else ActivationPackage.from_dict(activation_package)
    base.schema_version = str(base.schema_version or PHASE5_ACTIVATION_PACKAGE_VERSION)
    base.bundle_run_id = str(base.bundle_run_id or bundle_run_id)
    base.pair = str(base.pair or pair).upper()
    base.target_alias = str(target_alias or base.target_alias or "")
    base.model_alias = str(base.model_alias or base.target_alias or "")
    base.model_uri = str(base.model_uri or f"mlflow://{base.pair}@{base.model_alias}" if base.model_alias else "")
    base.release_status = str(release_status or base.release_status or "")
    base.promotion_status = str(promotion_status or base.promotion_status or "")
    base.runtime_compatible = bool(runtime_compatible if runtime_compatible is not None else base.runtime_compatible)
    base.runtime_compatibility = str(
        base.runtime_compatibility
        or ("compatible" if bool(base.runtime_compatible) else "incompatible")
    )
    base.dataset_fingerprint = str(base.dataset_fingerprint or dataset_fingerprint)
    base.feature_service_version = str(base.feature_service_version or feature_service_version)
    base.feature_service_hash = str(base.feature_service_hash or base.feature_service_version or "")
    base.risk_config_version = str(base.risk_config_version or risk_config_version)
    base.risk_profile_id = str(base.risk_profile_id or base.risk_config_version or "")
    resolved_rollback = normalize_rollback_plan(rollback_target) or base.rollback_target
    base.rollback_target = resolved_rollback
    resolved_canary = normalize_canary_plan(canary_plan) or base.canary_plan
    base.canary_plan = resolved_canary
    signoff_payload = _dict_payload(operator_signoff) or dict(base.operator_signoff or {})
    base.operator_signoff = signoff_payload
    if not base.signed_off_by:
        base.signed_off_by = [str(item) for item in list(signoff_payload.get("approvers") or []) if str(item).strip()]
    gate_values = _list_payload(promotion_gates)
    if gate_values:
        base.promotion_gates = [
            gate
            for gate in (normalize_promotion_gate(item) for item in gate_values)
            if gate is not None
        ]
    elif not base.promotion_gates:
        base.promotion_gates = []
    note_values = _list_payload(release_notes)
    if note_values:
        base.release_notes = [
            note
            for note in (normalize_release_note(item) for item in note_values)
            if note is not None
        ]
    elif not base.release_notes:
        base.release_notes = []
    merged_refs = {
        str(key): str(value)
        for key, value in dict(base.evidence_refs or {}).items()
        if str(key).strip() and value not in (None, "")
    }
    for key, value in dict(evidence_refs or {}).items():
        if str(key).strip() and value not in (None, ""):
            merged_refs[str(key)] = str(value)
    base.evidence_refs = merged_refs
    merged_metadata = dict(base.metadata or {})
    merged_metadata.update(_dict_payload(metadata))
    base.metadata = merged_metadata
    return base


def sync_bundle_release_package(
    bundle: BundleManifest,
    *,
    target_alias: str = "",
    model_manifest: str = "",
) -> ActivationPackage:
    evidence_refs = {}
    if bundle.activation_package is not None:
        evidence_refs.update(dict(bundle.activation_package.evidence_refs or {}))
    metadata = dict(bundle.metadata or {})
    model_manifest_ref = str(model_manifest or metadata.get("model_manifest") or "").strip()
    if model_manifest_ref:
        evidence_refs["model_manifest"] = model_manifest_ref
    package = build_activation_package(
        bundle_run_id=str(bundle.bundle_run_id),
        pair=str(bundle.pair).upper(),
        target_alias=str(target_alias or bundle.intended_alias or ""),
        promotion_status=str(bundle.promotion_status or ""),
        runtime_compatible=infer_bundle_runtime_compatible(bundle),
        dataset_fingerprint=str(bundle.dataset_fingerprint or ""),
        feature_service_version=str(bundle.feature_service_version or ""),
        risk_config_version=str(bundle.risk_config_version or ""),
        release_status=str(bundle.release_status or metadata.get("release_status") or ""),
        rollback_target=bundle.rollback_target or metadata.get("rollback_target"),
        operator_signoff=bundle.operator_signoff or metadata.get("operator_signoff"),
        canary_plan=bundle.canary_plan or metadata.get("canary_plan"),
        promotion_gates=bundle.promotion_gates or metadata.get("promotion_gates"),
        release_notes=bundle.release_notes or metadata.get("release_notes"),
        activation_package=bundle.activation_package or metadata.get("activation_package"),
        evidence_refs=evidence_refs,
        metadata={"tier": str(bundle.tier or ""), **_dict_payload(metadata.get("release_metadata"))},
    )
    bundle.release_status = str(package.release_status or "")
    bundle.rollback_target = package.rollback_target
    bundle.operator_signoff = dict(package.operator_signoff or {})
    bundle.canary_plan = package.canary_plan
    bundle.promotion_gates = list(package.promotion_gates or [])
    bundle.release_notes = list(package.release_notes or [])
    bundle.activation_package = package
    bundle.metadata = {
        **dict(bundle.metadata or {}),
        **release_metadata_payload(package),
    }
    return package


def release_metadata_payload(package: ActivationPackage | None) -> dict[str, Any]:
    if package is None:
        return {
            "release_status": "",
            "rollback_target": {},
            "operator_signoff": {},
            "canary_plan": {},
            "canary_prep": {},
            "promotion_gates": [],
            "phase5_gate_summary": {},
            "shadow_acceptance_summary": {},
            "release_notes": [],
            "activation_package": {},
        }
    gate_summary = summarize_promotion_gates(list(package.promotion_gates or []))
    return {
        "release_status": str(package.release_status or ""),
        "rollback_target": package.rollback_target.to_dict() if package.rollback_target is not None else {},
        "operator_signoff": dict(package.operator_signoff or {}),
        "canary_plan": package.canary_plan.to_dict() if package.canary_plan is not None else {},
        "canary_prep": canary_prep_metadata(package),
        "promotion_gates": [item.to_dict() for item in list(package.promotion_gates or [])],
        "phase5_gate_summary": gate_summary,
        "shadow_acceptance_summary": summarize_shadow_acceptance(package),
        "release_notes": [item.to_dict() for item in list(package.release_notes or [])],
        "activation_package": package.to_dict(),
        "capital_band": str(dict(package.metadata or {}).get("capital_band") or ""),
        "governance_mode": str(dict(package.metadata or {}).get("governance_mode") or ""),
        "provider_shadow_only": bool(dict(package.metadata or {}).get("provider_shadow_only", False)),
    }
