from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fxstack.mlops.model_uri import load_bundle_manifest_from_run
from fxstack.mlops.registry import (
    COMPONENT_FAMILIES,
    configure_mlflow,
    registered_model_name,
    resolve_bundle_manifest_by_alias,
    set_bundle_alias,
)
from fxstack.mlops.types import (
    ActivationPackage,
    BundleManifest,
    CanaryPlan,
    PromotionGateResult,
    ReleaseNote,
    RollbackPlan,
)
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings
from fxstack.training.activation import activate_mlflow_alias
from fxstack.training.release_package import (
    build_activation_package,
    canary_prep_metadata,
    release_metadata_payload,
    summarize_promotion_gates,
    summarize_shadow_acceptance,
)
from fxstack.utils.hashing import hash_mapping


def _now_ts() -> float:
    return float(time.time())


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def _write_markdown(path: Path, lines: list[str]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return str(path)


def _release_root() -> Path:
    return Path(str(get_settings().phase5_release_root))


def _release_dir(*, pair: str, bundle_run_id: str) -> Path:
    return _release_root() / str(pair).lower() / str(bundle_run_id)


def _release_note_markdown(note: ReleaseNote, package: ActivationPackage) -> list[str]:
    return [
        f"# {note.title or 'Release Note'}",
        "",
        f"- Pair: `{package.pair}`",
        f"- Bundle Run ID: `{package.bundle_run_id}`",
        f"- Alias: `{package.model_alias or package.target_alias}`",
        f"- Release Status: `{package.release_status}`",
        f"- Runtime Compatibility: `{package.runtime_compatibility or ('compatible' if package.runtime_compatible else 'incompatible')}`",
        f"- Signed Off By: `{', '.join(package.signed_off_by) or 'pending'}`",
        "",
        note.summary or "",
    ]


def _default_feature_service_name(bundle: BundleManifest) -> str:
    explicit = str((bundle.metadata or {}).get("feature_service_name") or "").strip()
    if explicit:
        return explicit
    intraday_tf = str((bundle.timeframes or {}).get("intraday") or "M5").lower()
    return f"fx_{str(bundle.pair).lower()}_execution_grade_{intraday_tf}"


def _best_validation_summary_uri(bundle: BundleManifest) -> str:
    training_eval_reports = dict(bundle.training_eval_reports or {})
    for key in ["meta", "exit", "reversal_failure", "reversal_opportunity"]:
        candidate = str(training_eval_reports.get(key) or "").strip()
        if candidate:
            return candidate
    for value in training_eval_reports.values():
        candidate = str(value or "").strip()
        if candidate:
            return candidate
    return ""


def _best_calibrator_uri(bundle: BundleManifest) -> str:
    for ref in dict(bundle.components or {}).values():
        evidence_refs = dict(getattr(ref, "evidence_refs", {}) or {})
        candidate = str(
            evidence_refs.get("calibrator")
            or evidence_refs.get("calibrator_uri")
            or evidence_refs.get("calibrator_path")
            or ""
        ).strip()
        if candidate:
            return candidate
    return ""


def _phase5_gate_bundle(bundle: BundleManifest) -> dict[str, Any]:
    refs = dict((bundle.metadata or {}).get("phase5_gates") or {})
    bundle_path = Path(str(refs.get("phase5_gate_bundle") or "").strip())
    if bundle_path.exists():
        return _read_json(bundle_path)
    return {}


def _phase5_gate_results(bundle: BundleManifest) -> list[PromotionGateResult]:
    phase5_bundle = _phase5_gate_bundle(bundle)
    return _promotion_gate_results_from_phase5_bundle(
        phase5_bundle,
        gate_refs=dict((bundle.metadata or {}).get("phase5_gates") or {}),
    )


def _phase5_gate_summary(package: ActivationPackage) -> dict[str, Any]:
    return summarize_promotion_gates(list(package.promotion_gates or []))


def _promotion_gate_results_from_phase5_bundle(
    phase5_bundle: dict[str, Any],
    *,
    gate_refs: dict[str, Any] | None = None,
) -> list[PromotionGateResult]:
    out: list[PromotionGateResult] = []
    refs = {str(key): str(value) for key, value in dict(gate_refs or {}).items() if str(key).strip() and value not in (None, "")}
    for key in [
        "research_gate",
        "economic_gate",
        "operational_gate",
        "shadow_gate",
        "canary_gate",
        "canary_closeout",
    ]:
        payload = dict(phase5_bundle.get(key) or {})
        if not payload:
            continue
        details = dict(payload.get("details") or {})
        metrics = {"score": float(payload.get("score", 0.0) or 0.0), **details}
        out.append(
            PromotionGateResult(
                gate_id=str(payload.get("gate") or key),
                status=str(payload.get("status") or ""),
                passed=bool(payload.get("passed", False)),
                required=True,
                reason=str(payload.get("reason") or ""),
                evaluated_at=_now_ts(),
                evidence_refs={
                    "phase5_gate_bundle": str(refs.get("phase5_gate_bundle") or ""),
                    key: str(refs.get(key) or ""),
                },
                metrics=metrics,
                metadata={"details": details},
            )
        )
    return out


def _hydrate_release_package_gates(
    package: ActivationPackage,
    *,
    release_dir: Path,
) -> ActivationPackage:
    if list(package.promotion_gates or []):
        return package
    gate_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    if not gate_bundle:
        gate_bundle_ref = Path(str(dict(package.evidence_refs or {}).get("phase5_gate_bundle") or "").strip())
        if gate_bundle_ref.exists():
            gate_bundle = _read_json(gate_bundle_ref)
    if gate_bundle:
        package.promotion_gates = _promotion_gate_results_from_phase5_bundle(
            gate_bundle,
            gate_refs={
                "phase5_gate_bundle": str(release_dir / "phase5_gate_bundle.json"),
                **dict(package.evidence_refs or {}),
            },
        )
    return package


def _rollback_bundle(
    *,
    pair: str,
    current_alias: str = "champion",
) -> BundleManifest | None:
    try:
        return resolve_bundle_manifest_by_alias(pair=str(pair).upper(), alias=str(current_alias))
    except Exception:
        return None


def _operator_signoff(author: str, package: ActivationPackage | None = None) -> dict[str, Any]:
    current = dict(package.operator_signoff or {}) if package is not None else {}
    approvers = [str(item) for item in list(current.get("approvers") or []) if str(item).strip()]
    author_txt = str(author or "").strip()
    if author_txt and author_txt not in approvers:
        approvers.append(author_txt)
    return {
        **current,
        "approvers": approvers,
        "last_updated_at": _now_ts(),
        "last_updated_by": author_txt or str(current.get("last_updated_by") or ""),
    }


def _build_release_package(
    *,
    bundle: BundleManifest,
    release_status: str,
    target_alias: str,
    rollback_target: RollbackPlan | None,
    canary_plan: CanaryPlan | None,
    release_notes: list[ReleaseNote],
    operator_signoff: dict[str, Any],
) -> ActivationPackage:
    package = build_activation_package(
        bundle_run_id=str(bundle.bundle_run_id),
        pair=str(bundle.pair).upper(),
        target_alias=str(target_alias),
        promotion_status=str(bundle.promotion_status or ""),
        runtime_compatible=all(bool(getattr(ref, "runtime_compatible", True)) for ref in dict(bundle.components or {}).values()),
        dataset_fingerprint=str(bundle.dataset_fingerprint or ""),
        feature_service_version=str(bundle.feature_service_version or ""),
        risk_config_version=str(bundle.risk_config_version or ""),
        release_status=str(release_status),
        rollback_target=rollback_target.to_dict() if rollback_target is not None else {},
        operator_signoff=operator_signoff,
        canary_plan=canary_plan.to_dict() if canary_plan is not None else {},
        promotion_gates=[item.to_dict() for item in _phase5_gate_results(bundle)],
        release_notes=[item.to_dict() for item in release_notes],
        activation_package=(bundle.activation_package.to_dict() if bundle.activation_package is not None else {}),
        evidence_refs={
            **dict((bundle.metadata or {}).get("phase5_gates") or {}),
            **dict((bundle.metadata or {}).get("phase3_evidence") or {}),
            "backtest_summary": str((bundle.metadata or {}).get("backtest_summary") or ""),
            "model_manifest": str((bundle.metadata or {}).get("model_manifest") or ""),
        },
        metadata={
            "registry_path": f"mlflow://{str(bundle.pair).upper()}@{str(target_alias)}",
            "feature_repo_manifest": str((bundle.metadata or {}).get("feature_repo_manifest") or ""),
            "capital_band": str(get_settings().capital_band_mode),
            "governance_mode": "shadow_only" if bool(get_settings().provider_shadow_only) else "normal",
            "provider_shadow_only": bool(get_settings().provider_shadow_only),
        },
    )
    package.model_uri = f"mlflow://{str(bundle.pair).upper()}@{str(target_alias)}"
    package.model_alias = str(target_alias)
    package.feature_service_name = _default_feature_service_name(bundle)
    package.feature_service_hash = str(bundle.feature_service_version or "")
    package.feature_schema_hash = str(hash_mapping(dict(bundle.feature_schema or {})))
    package.label_version = str(bundle.label_version or "")
    package.risk_profile_id = str(bundle.risk_config_version or "")
    package.training_window = dict(bundle.training_window_summary or {})
    package.validation_summary_uri = _best_validation_summary_uri(bundle)
    package.backtest_summary_uri = str((bundle.metadata or {}).get("backtest_summary") or "")
    package.calibrator_uri = _best_calibrator_uri(bundle)
    package.hardware_profile = str(
        (bundle.training_config or {}).get("hardware_profile")
        or (bundle.metadata or {}).get("hardware_profile")
        or ""
    )
    package.runtime_compatibility = "compatible" if bool(package.runtime_compatible) else "incompatible"
    package.observation_window = {
        "duration_minutes": int(canary_plan.duration_minutes if canary_plan is not None else get_settings().phase5_observation_window_minutes),
        "metrics_window_minutes": int(canary_plan.metrics_window_minutes if canary_plan is not None else get_settings().phase5_observation_window_minutes),
        "status": str(release_status),
    }
    approvers = [str(item) for item in list(operator_signoff.get("approvers") or []) if str(item).strip()]
    package.signed_off_by = approvers
    return package


def _persist_release_artifacts(
    *,
    package: ActivationPackage,
    note: ReleaseNote | None,
    phase5_bundle: dict[str, Any],
) -> dict[str, str]:
    release_dir = _release_dir(pair=str(package.pair), bundle_run_id=str(package.bundle_run_id))
    out = {
        "release_dir": str(release_dir),
        "activation_package": _write_json(release_dir / "activation_package.json", package.to_dict()),
    }
    if note is not None:
        out["release_note"] = _write_json(release_dir / "release_note.json", note.to_dict())
        out["release_note_md"] = _write_markdown(release_dir / "release_note.md", _release_note_markdown(note, package))
    if phase5_bundle:
        out["phase5_gate_bundle"] = _write_json(release_dir / "phase5_gate_bundle.json", phase5_bundle)
    out["release_status"] = _write_json(
        release_dir / "release_status.json",
        {
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "release_status": str(package.release_status),
            "model_alias": str(package.model_alias or package.target_alias),
            "signed_off_by": list(package.signed_off_by or []),
            "observation_window": dict(package.observation_window or {}),
            "updated_at": _now_ts(),
            "capital_band": str(dict(package.metadata or {}).get("capital_band") or ""),
            "governance_mode": str(dict(package.metadata or {}).get("governance_mode") or ""),
            "provider_shadow_only": bool(dict(package.metadata or {}).get("provider_shadow_only", False)),
        },
    )
    return out


def load_release_package(*, pair: str, bundle_run_id: str = "") -> tuple[ActivationPackage, Path]:
    pair_root = _release_root() / str(pair).lower()
    if bundle_run_id:
        package_path = pair_root / str(bundle_run_id) / "activation_package.json"
        if not package_path.exists():
            raise FileNotFoundError(f"release package not found for {str(pair).upper()} bundle {bundle_run_id}")
        return _hydrate_release_package_gates(ActivationPackage.from_dict(_read_json(package_path)), release_dir=package_path.parent), package_path.parent
    candidates = sorted(pair_root.glob("*/activation_package.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"no release package found for {str(pair).upper()}")
    package_path = candidates[0]
    return _hydrate_release_package_gates(ActivationPackage.from_dict(_read_json(package_path)), release_dir=package_path.parent), package_path.parent


def resolve_bundle_manifest_by_bundle_run_id(
    *,
    pair: str,
    bundle_run_id: str,
) -> BundleManifest:
    s = get_settings()
    tf = {
        "regime": str(s.regime_timeframe),
        "swing": str(s.swing_timeframe),
        "intraday": str(s.intraday_timeframe),
    }
    client = configure_mlflow().tracking.MlflowClient()
    anchor_names = [
        registered_model_name(family=COMPONENT_FAMILIES["meta"], pair=pair, timeframe=tf["intraday"]),
        registered_model_name(family=COMPONENT_FAMILIES["intraday_xgb"], pair=pair, timeframe=tf["intraday"]),
        registered_model_name(family=COMPONENT_FAMILIES["regime"], pair=pair, timeframe=tf["regime"]),
    ]
    seed = None
    for model_name in anchor_names:
        try:
            versions = list(client.search_model_versions(f"name='{model_name}'"))
        except Exception:
            versions = []
        for version in versions:
            try:
                detail = client.get_model_version(name=model_name, version=str(version.version))
            except Exception:
                detail = version
            tags = dict(getattr(detail, "tags", {}) or {})
            if str(tags.get("fxstack.bundle_run_id") or "") == str(bundle_run_id):
                seed = detail
                break
        if seed is not None:
            break
    if seed is None:
        raise RuntimeError(f"bundle_run_id '{bundle_run_id}' not found for {str(pair).upper()}")
    payload = load_bundle_manifest_from_run(str(seed.run_id))
    return BundleManifest.from_dict(payload)


def stage_release(
    *,
    pair: str,
    alias: str = "shadow",
    title: str = "",
    summary: str = "",
    author: str = "",
    allowlisted_pairs: list[str] | None = None,
    budget_scale: float | None = None,
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    s = get_settings()
    pair_key = str(pair).upper()
    bundle = resolve_bundle_manifest_by_alias(pair=pair_key, alias=str(alias))
    rollback_bundle = _rollback_bundle(pair=pair_key, current_alias="champion")
    allowlist = [str(item).upper() for item in list(allowlisted_pairs or [pair_key]) if str(item).strip()]
    canary = CanaryPlan(
        plan_id=f"{pair_key.lower()}-{str(bundle.bundle_run_id)}-canary",
        scope="pair_allowlist",
        status="planned",
        traffic_fraction=1.0,
        duration_minutes=int(duration_minutes or s.phase5_observation_window_minutes),
        metrics_window_minutes=int(duration_minutes or s.phase5_observation_window_minutes),
        success_criteria={
            "latency_budget_ms": float(s.phase5_canary_latency_budget_ms),
            "stale_feature_limit": int(s.phase5_canary_stale_feature_limit),
            "drawdown_limit_pct": float(s.phase5_canary_drawdown_limit_pct),
            "calibration_drift_limit": float(s.phase5_canary_calibration_drift_limit),
        },
        abort_conditions=[
            "latency_breach",
            "stale_features",
            "rollout_breach",
            "drawdown_breach",
            "calibration_drift",
        ],
        metadata={
            "allowlisted_pairs": allowlist,
            "budget_scale": float(budget_scale if budget_scale is not None else s.phase5_canary_budget_scale),
        },
    )
    rollback = RollbackPlan(
        target_bundle_run_id=str(rollback_bundle.bundle_run_id if rollback_bundle is not None else ""),
        target_alias="champion",
        target_registry_path=f"mlflow://{pair_key}@champion",
        strategy="alias_reassignment",
        reason="phase5_release_rollback",
        trigger_conditions=list(canary.abort_conditions),
        metadata={"pair": pair_key},
    )
    note = ReleaseNote(
        title=str(title or f"{pair_key} release candidate"),
        summary=str(summary or f"Stage {pair_key} {bundle.bundle_run_id} from alias {alias} for Phase 5 rollout."),
        category="promotion",
        author=str(author or ""),
        created_at=_now_ts(),
        references=[str(item) for item in dict((bundle.metadata or {}).get("phase5_gates") or {}).values() if str(item).strip()],
        metadata={"pair": pair_key, "bundle_run_id": str(bundle.bundle_run_id), "alias": str(alias)},
    )
    package = _build_release_package(
        bundle=bundle,
        release_status="staged",
        target_alias=str(alias),
        rollback_target=rollback,
        canary_plan=canary,
        release_notes=[note],
        operator_signoff={},
    )
    phase5_bundle = _phase5_gate_bundle(bundle)
    written = _persist_release_artifacts(package=package, note=note, phase5_bundle=phase5_bundle)
    return {
        "ok": True,
        "pair": pair_key,
        "bundle_run_id": str(bundle.bundle_run_id),
        "release_status": str(package.release_status),
        "shadow_acceptance_summary": summarize_shadow_acceptance(package),
        "canary_prep": canary_prep_metadata(package),
        "release_dir": written["release_dir"],
        "activation_package": written["activation_package"],
        "release_note": written.get("release_note", ""),
        "phase5_gate_bundle": written.get("phase5_gate_bundle", ""),
    }


def promote_release(*, pair: str, author: str, bundle_run_id: str = "") -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    package.release_status = "staged"
    package.operator_signoff = _operator_signoff(author, package)
    package.signed_off_by = [str(item) for item in list(package.operator_signoff.get("approvers") or []) if str(item).strip()]
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    return {
        "ok": True,
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "signed_off_by": list(package.signed_off_by or []),
        "shadow_acceptance_summary": summarize_shadow_acceptance(package),
        "phase5_gate_summary": _phase5_gate_summary(package),
        "activation_package": written["activation_package"],
    }


def shadow_accept(*, pair: str, bundle_run_id: str = "") -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    required = {"research_gate", "economic_gate", "operational_gate", "shadow_gate"}
    gates = {str(item.gate_id): item for item in list(package.promotion_gates or [])}
    missing = sorted([gate for gate in required if gate not in gates])
    failed = sorted([gate for gate in required if gate in gates and gates[gate].passed is not True])
    if missing or failed:
        return {"ok": False, "error": "shadow_gate_blocked", "missing": missing, "failed": failed}
    package.release_status = "shadow_accepted"
    if package.canary_plan is not None:
        package.canary_plan.status = "shadow_accepted"
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    return {
        "ok": True,
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "shadow_acceptance_summary": summarize_shadow_acceptance(package),
        "phase5_gate_summary": _phase5_gate_summary(package),
        "activation_package": written["activation_package"],
    }


def _release_metadata_patch(
    *,
    package: ActivationPackage,
    phase5_bundle: dict[str, Any],
) -> dict[str, Any]:
    allowlisted_pairs = [str(item).upper() for item in list((package.canary_plan.metadata if package.canary_plan is not None else {}).get("allowlisted_pairs") or []) if str(item).strip()]
    budget_scale = float(
        (package.canary_plan.metadata if package.canary_plan is not None else {}).get("budget_scale")
        or get_settings().phase5_canary_budget_scale
    )
    patch = {
        **release_metadata_payload(package),
        "phase5_gate_bundle": dict(phase5_bundle or {}),
        "capital_band": str(dict(package.metadata or {}).get("capital_band") or ""),
        "governance_mode": str(dict(package.metadata or {}).get("governance_mode") or ""),
        "provider_shadow_only": bool(dict(package.metadata or {}).get("provider_shadow_only", False)),
        "main_runtime_rollout": {
            "mode": "canary",
            "enabled": bool(str(package.release_status).strip().lower() == "canary_active"),
            "allowlisted_pairs": allowlisted_pairs,
            "budget_scale": budget_scale,
            "budget_reason": "phase5_canary",
        },
    }
    return patch


def _canary_start_blockers(package: ActivationPackage) -> list[str]:
    blockers: list[str] = []
    release_status = str(package.release_status or "").strip().lower()
    if release_status != "shadow_accepted":
        blockers.append(f"release_status:{release_status or 'missing'}")

    canary_plan = package.canary_plan
    if canary_plan is None:
        blockers.append("canary_plan_missing")
    else:
        plan_status = str(canary_plan.status or "").strip().lower()
        if plan_status not in {"shadow_accepted", "active"}:
            blockers.append(f"canary_plan_status:{plan_status or 'missing'}")
        allowlisted_pairs = [
            str(item).upper()
            for item in list((canary_plan.metadata or {}).get("allowlisted_pairs") or [])
            if str(item).strip()
        ]
        if not allowlisted_pairs:
            blockers.append("canary_allowlist_missing")

    gates = {str(item.gate_id): item for item in list(package.promotion_gates or []) if str(item.gate_id).strip()}
    required = {"research_gate", "economic_gate", "operational_gate", "shadow_gate"}
    missing = sorted(required - set(gates))
    if missing:
        blockers.append(f"missing_phase5_gates:{','.join(missing)}")
    failed = sorted([gate for gate in required if gate in gates and gates[gate].passed is not True])
    if failed:
        blockers.append(f"failed_phase5_gates:{','.join(failed)}")
    return blockers


def _runtime_pair_readiness(state: dict[str, Any], pair: str) -> dict[str, Any]:
    pair_key = str(pair).upper().strip()
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    top_level = dict((state or {}).get("pair_readiness") or {})
    nested = dict(runtime_diag.get("pair_readiness") or {})
    if pair_key in top_level and isinstance(top_level.get(pair_key), dict):
        return dict(top_level.get(pair_key) or {})
    if pair_key in nested and isinstance(nested.get(pair_key), dict):
        return dict(nested.get(pair_key) or {})

    startup = dict(dict(runtime_diag.get("startup_inference") or {}).get(pair_key) or {})
    feature_serving_by_pair = {
        str(key).upper(): dict(value or {})
        for key, value in dict(runtime_diag.get("feature_serving_by_pair") or {}).items()
        if str(key).strip()
    }
    symbol_readiness = {
        str(key).upper(): dict(value or {})
        for key, value in dict((state or {}).get("symbol_readiness") or {}).items()
        if str(key).strip()
    }
    model_load = dict(runtime_diag.get("model_load") or {})
    feature_serving = dict(
        feature_serving_by_pair.get(f"{pair_key}:M5")
        or feature_serving_by_pair.get(f"{pair_key}:D")
        or feature_serving_by_pair.get(f"{pair_key}:H4")
        or {}
    )
    symbol = dict(symbol_readiness.get(pair_key) or {})
    model_pair = dict(dict(model_load.get("pairs") or {}).get(pair_key) or {})
    blockers: list[str] = []
    if not startup:
        blockers.append("startup_inference:missing")
    elif not bool(startup.get("ok", False)):
        blockers.append(f"startup_inference:{str(startup.get('reason') or 'blocked')}")
    if feature_serving:
        if not str(feature_serving.get("source") or "").strip():
            blockers.append("feature_serving:missing_source")
        if bool(feature_serving.get("stale", False)):
            blockers.append("feature_serving:stale")
    elif pair_key in feature_serving_by_pair:
        blockers.append("feature_serving:missing")
    if symbol and not bool(symbol.get("supported", True)):
        blockers.append(f"symbol_readiness:{str(symbol.get('broker_symbol') or 'unsupported')}")
    if not symbol and pair_key in symbol_readiness:
        blockers.append("symbol_readiness:missing")
    if str(model_pair.get("failure_reason") or "").strip():
        blockers.append(f"model_load:{str(model_pair.get('failure_reason') or 'error')}")
    return {
        "pair": pair_key,
        "startup_inference": startup,
        "feature_serving": feature_serving,
        "symbol_readiness": symbol,
        "model_load": model_pair,
        "ready": bool(not blockers),
        "status": "ready" if not blockers else "blocked",
        "blockers": blockers,
        "reason": "ok" if not blockers else blockers[0],
        "startup_inference_ok": bool(startup.get("ok", False)),
        "feature_serving_source": str(feature_serving.get("source") or ""),
        "feature_serving_stale": bool(feature_serving.get("stale", False)),
        "symbol_supported": bool(symbol.get("supported", True)) if symbol else True,
    }


def _runtime_strategy_state(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    return {
        "strategy_engine_mode": str(
            (state or {}).get("strategy_engine_mode")
            or (state or {}).get("strategyEngineMode")
            or runtime_diag.get("strategy_engine_mode")
            or "supervised_legacy"
        ),
        "supervised_fallback": dict((state or {}).get("supervised_fallback") or runtime_diag.get("supervised_fallback") or {}),
        "challenger_conflict": dict((state or {}).get("challenger_conflict") or runtime_diag.get("challenger_conflict") or {}),
    }


def _runtime_rl_state(state: dict[str, Any]) -> dict[str, Any]:
    runtime_diag = dict((state or {}).get("runtime_diag") or {})
    rl_portfolio_proposal = dict((state or {}).get("rl_portfolio_proposal") or runtime_diag.get("rl_portfolio_proposal") or {})
    entry_execution_policy = dict((state or {}).get("rl_execution_policy") or runtime_diag.get("entry_execution_policy") or {})
    proposal_diagnostics = dict(rl_portfolio_proposal.get("diagnostics") or {})
    checkpoint_summary = dict(rl_portfolio_proposal.get("checkpoint_summary") or proposal_diagnostics.get("checkpoint_summary") or {})
    artifact_discovery = dict(proposal_diagnostics.get("artifact_discovery") or {})
    proposals_by_pair = {
        str(key).upper(): dict(value or {})
        for key, value in dict(rl_portfolio_proposal.get("proposals_by_pair") or {}).items()
        if str(key).strip()
    }
    pair_universe = [str(pair).upper() for pair in list(rl_portfolio_proposal.get("pair_universe") or []) if str(pair).strip()]
    close_intent_count = 0
    tighten_stop_intent_count = 0
    non_flat_target_count = 0
    for proposal in proposals_by_pair.values():
        action = dict(proposal.get("action") or {})
        target_position = float(action.get("target_position") or 0.0)
        if abs(target_position) > 0.0:
            non_flat_target_count += 1
        if bool(action.get("close_position", False)):
            close_intent_count += 1
        if bool(action.get("tighten_stop", False)):
            tighten_stop_intent_count += 1
    lifecycle_summary = {
        "checkpoint_loaded": bool(entry_execution_policy.get("rl_lifecycle_checkpoint_loaded", rl_portfolio_proposal.get("checkpoint_loaded", False))),
        "proposal_source": str(entry_execution_policy.get("rl_lifecycle_proposal_source") or rl_portfolio_proposal.get("source") or ""),
        "reviewed_count": int(entry_execution_policy.get("rl_lifecycle_reviewed_count") or 0),
        "applied_count": int(entry_execution_policy.get("rl_lifecycle_applied_count") or 0),
        "exit_count": int(entry_execution_policy.get("rl_lifecycle_exit_count") or 0),
        "resize_count": int(entry_execution_policy.get("rl_lifecycle_resize_count") or 0),
        "tighten_stop_count": int(entry_execution_policy.get("rl_lifecycle_tighten_stop_count") or 0),
        "preserved_exit_count": int(entry_execution_policy.get("rl_lifecycle_preserved_exit_count") or 0),
        "fallback_count": int(entry_execution_policy.get("rl_lifecycle_fallback_count") or 0),
        "pairs": list(entry_execution_policy.get("rl_lifecycle_pairs") or []),
        "strategy_engine_mode": str(entry_execution_policy.get("strategy_engine_mode") or runtime_diag.get("strategy_engine_mode") or "supervised_legacy"),
    }
    return {
        "checkpoint_loaded": bool(entry_execution_policy.get("rl_checkpoint_loaded", rl_portfolio_proposal.get("checkpoint_loaded", False))),
        "checkpoint_path": str(rl_portfolio_proposal.get("checkpoint_path") or entry_execution_policy.get("rl_checkpoint_path") or ""),
        "proposal_source": str(entry_execution_policy.get("rl_proposal_source") or rl_portfolio_proposal.get("source") or ""),
        "supervised_fallback_used": bool(
            entry_execution_policy.get("rl_fallback_entry_count", 0) or rl_portfolio_proposal.get("supervised_fallback_used", False)
        ),
        "fallback_reason": str(entry_execution_policy.get("rl_fallback_reason") or rl_portfolio_proposal.get("fallback_reason") or ""),
        "routed_entry_count": int(entry_execution_policy.get("rl_routed_entry_count") or 0),
        "blocked_entry_count": int(entry_execution_policy.get("rl_blocked_entry_count") or 0),
        "fallback_entry_count": int(entry_execution_policy.get("rl_fallback_entry_count") or 0),
        "scaled_entry_count": int(entry_execution_policy.get("rl_scaled_entry_count") or 0),
        "lifecycle_reviewed_count": int(entry_execution_policy.get("rl_lifecycle_reviewed_count") or 0),
        "lifecycle_applied_count": int(entry_execution_policy.get("rl_lifecycle_applied_count") or 0),
        "lifecycle_exit_count": int(entry_execution_policy.get("rl_lifecycle_exit_count") or 0),
        "lifecycle_flip_exit_count": int(entry_execution_policy.get("rl_lifecycle_flip_exit_count") or 0),
        "lifecycle_resize_count": int(entry_execution_policy.get("rl_lifecycle_resize_count") or 0),
        "lifecycle_tighten_stop_count": int(entry_execution_policy.get("rl_lifecycle_tighten_stop_count") or 0),
        "lifecycle_preserved_exit_count": int(entry_execution_policy.get("rl_lifecycle_preserved_exit_count") or 0),
        "lifecycle_fallback_count": int(entry_execution_policy.get("rl_lifecycle_fallback_count") or 0),
        "lifecycle_pairs": [str(pair).upper() for pair in list(entry_execution_policy.get("rl_lifecycle_pairs") or []) if str(pair).strip()],
        "execution_mode": str(entry_execution_policy.get("execution_mode") or ""),
        "strategy_engine_mode": str(
            entry_execution_policy.get("strategy_engine_mode")
            or runtime_diag.get("strategy_engine_mode")
            or "supervised_legacy"
        ),
        "proposal_count": int(proposal_diagnostics.get("decision_count") or len(proposals_by_pair)),
        "candidate_count": int(proposal_diagnostics.get("candidate_count") or 0),
        "pair_universe": pair_universe,
        "diagnostics": proposal_diagnostics,
        "checkpoint_summary": checkpoint_summary,
        "artifact_readiness": {
            "ready": bool(
                bool(rl_portfolio_proposal.get("checkpoint_loaded", False))
                or bool(str(rl_portfolio_proposal.get("checkpoint_path") or "").strip())
            ),
            "checkpoint_loaded": bool(artifact_discovery.get("checkpoint_loaded", rl_portfolio_proposal.get("checkpoint_loaded", False))),
            "checkpoint_path": str(artifact_discovery.get("checkpoint_path") or rl_portfolio_proposal.get("checkpoint_path") or ""),
            "fallback_reason": str(artifact_discovery.get("fallback_reason") or rl_portfolio_proposal.get("fallback_reason") or ""),
            "source": str(rl_portfolio_proposal.get("source") or ""),
        },
        "flip_intent": {
            "pair_universe": pair_universe,
            "proposal_count": int(len(proposals_by_pair)),
            "non_flat_target_count": int(non_flat_target_count),
            "close_intent_count": int(close_intent_count),
            "tighten_stop_intent_count": int(tighten_stop_intent_count),
        },
        "rebalance_summary": {
            "reviewed_count": lifecycle_summary["reviewed_count"],
            "applied_count": lifecycle_summary["applied_count"],
            "exit_count": lifecycle_summary["exit_count"],
            "resize_count": lifecycle_summary["resize_count"],
            "tighten_stop_count": lifecycle_summary["tighten_stop_count"],
            "preserved_exit_count": lifecycle_summary["preserved_exit_count"],
            "fallback_count": lifecycle_summary["fallback_count"],
            "pairs": list(lifecycle_summary["pairs"]),
        },
        "lifecycle_summary": lifecycle_summary,
        "reviewed_count": lifecycle_summary["reviewed_count"],
        "applied_count": lifecycle_summary["applied_count"],
        "exit_count": lifecycle_summary["exit_count"],
        "resize_count": lifecycle_summary["resize_count"],
        "tighten_stop_count": lifecycle_summary["tighten_stop_count"],
        "preserved_exit_count": lifecycle_summary["preserved_exit_count"],
        "fallback_count": lifecycle_summary["fallback_count"],
        "pairs": list(lifecycle_summary["pairs"]),
        "strategy_engine_mode": lifecycle_summary["strategy_engine_mode"],
    }


def canary_start(
    *,
    pair: str,
    database_url: str,
    manifest_path: Path,
    bundle_run_id: str = "",
) -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    runtime_state = RuntimeService(database_url=database_url).get_state()
    runtime_pair_readiness = _runtime_pair_readiness(runtime_state, pair)
    runtime_rl_state = _runtime_rl_state(runtime_state)
    blockers = _canary_start_blockers(package)
    if blockers:
        return {
            "ok": False,
            "error": "canary_start_blocked",
            "blockers": blockers,
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "release_status": str(package.release_status),
            "runtime_pair_readiness": runtime_pair_readiness,
            "runtime_rl_state": runtime_rl_state,
            "shadow_acceptance_summary": summarize_shadow_acceptance(package),
            "canary_prep": canary_prep_metadata(package),
            "activation_package": package.to_dict(),
        }
    if package.canary_plan is not None:
        package.canary_plan.status = "active"
        package.canary_plan.metadata = {
            **dict(package.canary_plan.metadata or {}),
            "started_at": _now_ts(),
        }
    package.release_status = "canary_active"
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    pairs = [
        str(item).upper()
        for item in list((package.canary_plan.metadata if package.canary_plan is not None else {}).get("allowlisted_pairs") or [package.pair])
        if str(item).strip()
    ]
    activated = activate_mlflow_alias(
        database_url=database_url,
        manifest_path=manifest_path,
        pairs=pairs,
        alias=str(package.model_alias or package.target_alias or "shadow"),
        metadata_patch=_release_metadata_patch(package=package, phase5_bundle=phase5_bundle),
    )
    RuntimeService(database_url=database_url).record_governance_event(
        event_type="canary_started",
        reason=f"{str(package.pair).upper()} canary started",
        payload={
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "allowlisted_pairs": pairs,
            "release_status": str(package.release_status),
            "runtime_pair_readiness": runtime_pair_readiness,
        },
    )
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    return {
        "ok": True,
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "runtime_pair_readiness": runtime_pair_readiness,
        "runtime_rl_state": runtime_rl_state,
        "activated_pairs": [str(item.get("pair") or "").upper() for item in activated],
        "shadow_acceptance_summary": summarize_shadow_acceptance(package),
        "canary_prep": canary_prep_metadata(package),
        "activation_package": written["activation_package"],
    }


def monitor_canary(
    *,
    pair: str,
    database_url: str,
    manifest_path: Path,
    bundle_run_id: str = "",
) -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    svc = RuntimeService(database_url=database_url)
    state = svc.get_state()
    metrics = svc.get_metrics()
    runtime_diag = dict(state.get("runtime_diag") or {})
    feature_serving = dict(runtime_diag.get("feature_serving") or {})
    pair_readiness = _runtime_pair_readiness(state, pair)
    strategy_state = _runtime_strategy_state(state)
    runtime_rl_state = _runtime_rl_state(state)
    risk_cycle_summary = dict(runtime_diag.get("risk_cycle_summary") or {})
    rollout_summary = dict(risk_cycle_summary.get("rollout") or {})
    breaches: list[str] = []
    if float(runtime_diag.get("loop_latency_ms", 0.0) or 0.0) > float(get_settings().phase5_canary_latency_budget_ms):
        breaches.append("latency_breach")
    if bool(feature_serving.get("stale", False)):
        breaches.append("stale_features")
    if int(dict(metrics.get("feature_parity") or {}).get("breaches") or 0) > 0:
        breaches.append("calibration_drift")
    if int(rollout_summary.get("breach_count") or 0) > 0:
        breaches.append("rollout_breach")
    if not bool(pair_readiness.get("ready", False)):
        breaches.append(f"pair_readiness:{str(pair_readiness.get('reason') or 'blocked')}")
    status = "ok" if not breaches else "breach"
    if package.canary_plan is not None:
        package.canary_plan.status = status
        package.canary_plan.metadata = {
            **dict(package.canary_plan.metadata or {}),
            "last_checked_at": _now_ts(),
            "breaches": list(breaches),
        }
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    payload = {
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "status": status,
        "breaches": breaches,
        "pair_readiness": pair_readiness,
        "strategy_state": strategy_state,
        "runtime_rl_state": runtime_rl_state,
        "canary_prep": canary_prep_metadata(package),
        "activation_package": written["activation_package"],
    }
    if breaches:
        svc.record_governance_event(
            event_type="canary_breach",
            reason=";".join(breaches),
            payload=payload,
        )
        if bool(get_settings().phase5_auto_rollback):
            rollback = rollback_release(
                pair=str(package.pair).upper(),
                database_url=database_url,
                manifest_path=manifest_path,
                bundle_run_id=str(package.bundle_run_id),
                reason=";".join(breaches),
            )
            payload["rollback"] = rollback
    return payload


def close_canary(
    *,
    pair: str,
    database_url: str,
    manifest_path: Path,
    outcome: str,
    bundle_run_id: str = "",
) -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    outcome_txt = str(outcome or "").strip().lower()
    allowlisted_pairs = [
        str(item).upper()
        for item in list((package.canary_plan.metadata if package.canary_plan is not None else {}).get("allowlisted_pairs") or [package.pair])
        if str(item).strip()
    ]
    if outcome_txt == "graduate":
        package.release_status = "graduated"
        if package.canary_plan is not None:
            package.canary_plan.status = "graduated"
        bundle = resolve_bundle_manifest_by_alias(pair=str(package.pair).upper(), alias=str(package.model_alias or package.target_alias or "shadow"))
        set_bundle_alias(bundle=bundle, alias="champion")
        activate_mlflow_alias(
            database_url=database_url,
            manifest_path=manifest_path,
            pairs=allowlisted_pairs,
            alias="champion",
            metadata_patch=_release_metadata_patch(package=package, phase5_bundle=phase5_bundle),
        )
        RuntimeService(database_url=database_url).record_governance_event(
            event_type="canary_graduated",
            reason=f"{str(package.pair).upper()} canary graduated",
            payload={"pair": str(package.pair).upper(), "bundle_run_id": str(package.bundle_run_id)},
        )
    else:
        activate_mlflow_alias(
            database_url=database_url,
            manifest_path=manifest_path,
            pairs=allowlisted_pairs,
            alias="champion",
        )
        package.release_status = "rejected"
        if package.canary_plan is not None:
            package.canary_plan.status = "rejected"
        RuntimeService(database_url=database_url).record_governance_event(
            event_type="canary_rejected",
            reason=f"{str(package.pair).upper()} canary rejected",
            payload={"pair": str(package.pair).upper(), "bundle_run_id": str(package.bundle_run_id)},
        )
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    return {
        "ok": True,
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "shadow_acceptance_summary": summarize_shadow_acceptance(package),
        "canary_prep": canary_prep_metadata(package),
        "activation_package": written["activation_package"],
    }


def rollback_release(
    *,
    pair: str,
    database_url: str,
    manifest_path: Path,
    bundle_run_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    target_bundle_id = str(
        package.rollback_target.target_bundle_run_id
        if package.rollback_target is not None
        else ""
    ).strip()
    if target_bundle_id:
        bundle = resolve_bundle_manifest_by_bundle_run_id(pair=str(package.pair).upper(), bundle_run_id=target_bundle_id)
        set_bundle_alias(bundle=bundle, alias="champion")
    package.release_status = "rolled_back"
    if package.canary_plan is not None:
        package.canary_plan.status = "rolled_back"
        package.canary_plan.metadata = {
            **dict(package.canary_plan.metadata or {}),
            "rollback_reason": str(reason or "rollback"),
            "rolled_back_at": _now_ts(),
        }
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    activate_mlflow_alias(
        database_url=database_url,
        manifest_path=manifest_path,
        pairs=[str(package.pair).upper()],
        alias="champion",
        metadata_patch=_release_metadata_patch(package=package, phase5_bundle=phase5_bundle),
    )
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    RuntimeService(database_url=database_url).record_governance_event(
        event_type="rolled_back",
        reason=str(reason or "manual_rollback"),
        payload={
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "rollback_target": target_bundle_id,
        },
    )
    _write_json(
        release_dir / "rollback_note.json",
        {
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "reason": str(reason or "manual_rollback"),
            "rollback_target": target_bundle_id,
            "rolled_back_at": _now_ts(),
        },
    )
    return {
        "ok": True,
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "rollback_target": target_bundle_id,
        "shadow_acceptance_summary": summarize_shadow_acceptance(package),
        "canary_prep": canary_prep_metadata(package),
        "activation_package": written["activation_package"],
    }


def release_status(
    *,
    pair: str,
    database_url: str,
    bundle_run_id: str = "",
) -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    svc = RuntimeService(database_url=database_url)
    state = svc.get_state()
    active = svc.get_active_model_set(str(pair).upper()) or {}
    metadata = dict(active.get("metadata_json") or {})
    runtime_pair_readiness = _runtime_pair_readiness(state, pair)
    strategy_state = _runtime_strategy_state(state)
    runtime_rl_state = _runtime_rl_state(state)
    blockers = _canary_start_blockers(package)
    if not bool(runtime_pair_readiness.get("ready", False)):
        blockers.append(f"runtime_pair_readiness:{str(runtime_pair_readiness.get('reason') or 'blocked')}")
    return {
        "ok": True,
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "release_dir": str(release_dir),
        "canary_ready": not bool(blockers),
        "canary_blockers": blockers,
        "runtime_pair_readiness": runtime_pair_readiness,
        "strategy_state": strategy_state,
        "runtime_rl_state": runtime_rl_state,
        "signed_off_by": list(package.signed_off_by or []),
        "model_alias": str(package.model_alias or package.target_alias),
        "active_model_set_id": str(active.get("model_set_id") or ""),
        "active_registry_path": str(active.get("registry_path") or ""),
        "active_release_status": str(metadata.get("release_status") or ""),
        "active_canary_plan": dict(metadata.get("canary_plan") or {}),
        "shadow_acceptance_summary": dict(metadata.get("shadow_acceptance_summary") or summarize_shadow_acceptance(package)),
        "canary_prep": dict(metadata.get("canary_prep") or canary_prep_metadata(package)),
        "activation_package": package.to_dict(),
    }
