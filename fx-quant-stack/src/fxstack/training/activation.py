from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fxstack.feast.repository import build_feature_service_ref, component_default_timeframe
from fxstack.mlops.model_uri import artifact_ref_value, normalize_artifact_ref, resolve_model_artifact_path
from fxstack.mlops.registry import backfill_current_state_to_mlflow, resolve_bundle_manifest_by_alias, set_bundle_alias
from fxstack.mlops.types import BundleManifest, ModelVersionRef
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings
from fxstack.training.release_package import build_activation_package, release_metadata_payload, sync_bundle_release_package


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid registry payload: {path}")
    return payload


def _artifact_path(value: Any) -> str:
    return artifact_ref_value(value)


def _artifact_ref(value: Any) -> dict[str, Any]:
    return normalize_artifact_ref(value)


def _resolve_artifact_dir(path_value: Any) -> Path:
    s = get_settings()
    return resolve_model_artifact_path(path_value, project_root=Path(s.project_root))


def _resolve_optional_path(raw: str, project_root: Path) -> Path | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    candidate = Path(txt)
    if candidate.is_absolute():
        return candidate
    return (project_root / candidate).resolve()


def _validate_artifact_dirs(
    *,
    registry_path: Path,
    artifacts: dict[str, Any],
    required: list[str],
    strict_activation: bool,
) -> list[str]:
    warnings: list[str] = []
    keys = set(required) | {k for k, v in artifacts.items() if str(_artifact_path(v)).strip()}
    for key in sorted(keys):
        ref = artifacts.get(key)
        txt = str(_artifact_path(ref)).strip()
        if not txt:
            continue
        try:
            candidate = _resolve_artifact_dir(ref)
        except Exception as exc:
            message = f"artifact_unresolved:{key}:{type(exc).__name__}"
            if strict_activation:
                raise ValueError(f"Registry artifact unresolved ({key}): {registry_path} -> {txt}") from exc
            warnings.append(message)
            continue
        meta = candidate / "meta.json"
        if meta.exists():
            continue
        message = f"artifact_missing:{key}:{candidate}"
        if strict_activation:
            raise ValueError(f"Registry artifact missing meta.json ({key}): {registry_path} -> {candidate}")
        warnings.append(message)
    return warnings


def _required_artifacts(policies: dict[str, str]) -> set[str]:
    out = {"regime", "meta"}
    swing_policy = str((policies or {}).get("swing", "transformer_primary_xgb_fallback")).strip().lower()
    intraday_policy = str((policies or {}).get("intraday", "tcn_primary_xgb_fallback")).strip().lower()

    if swing_policy == "transformer_primary_xgb_fallback":
        out.update({"swing_transformer", "swing_xgb"})
    else:
        out.add("swing_xgb")

    if intraday_policy == "tcn_primary_xgb_fallback":
        out.update({"intraday_tcn", "intraday_xgb"})
    else:
        out.add("intraday_xgb")
    return out


def _feature_schema(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("feature_schema")
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _artifact_meta(path_value: Any) -> dict[str, Any]:
    txt = str(_artifact_path(path_value) or "").strip()
    if not txt:
        return {}
    try:
        return _load_json_if_exists(_resolve_artifact_dir(path_value) / "meta.json")
    except Exception:
        return {}


def _artifact_age_hours(path_value: Any) -> float | None:
    meta = _artifact_meta(path_value)
    if not meta:
        return None
    created = float(meta.get("trained_at", meta.get("created_at", 0.0)) or 0.0)
    if created <= 0.0:
        return None
    return max(0.0, (time.time() - created) / 3600.0)


def _artifact_component_key(component_key: str) -> str:
    key = str(component_key or "").strip().lower()
    aliases = {
        "regime": "regime_hmm",
        "swing": "swing_xgb",
        "intraday": "intraday_xgb",
        "meta": "meta_filter",
        "exit": "exit_policy_xgb",
        "exit_policy": "exit_policy_xgb",
    }
    return aliases.get(key, key)


def _runtime_compatible_from_raw(*, raw: dict[str, Any], artifacts: dict[str, Any]) -> bool:
    if raw.get("runtime_compatible") is False:
        return False
    for ref in dict(artifacts or {}).values():
        if isinstance(ref, dict) and ref.get("runtime_compatible") is False:
            return False
    return True


def _artifact_feature_contract(
    *,
    pair: str,
    component_key: str,
    timeframe: str | None,
    path_value: Any,
) -> dict[str, Any]:
    ref = normalize_artifact_ref(path_value)
    existing_name = str(ref.get("feature_service_name") or "").strip()
    existing_version = str(ref.get("feature_service_version") or "").strip()
    existing_hash = str(ref.get("feature_contract_hash") or "").strip()
    existing_views = [str(item) for item in list(ref.get("feature_view_names") or []) if str(item).strip()]
    meta = _artifact_meta(path_value)
    retrieval = dict(meta.get("feature_retrieval") or {})
    feature_columns = list(meta.get("feature_columns") or [])
    normalized_key = _artifact_component_key(component_key)
    tf = str(
        timeframe
        or retrieval.get("timeframe")
        or meta.get("timeframe")
        or component_default_timeframe(normalized_key)
    ).upper()
    ref_obj = build_feature_service_ref(
        pair=str(pair).upper(),
        component_key=normalized_key,
        feature_columns=feature_columns,
        timeframe=tf,
    )
    return {
        "feature_service_name": existing_name or str(retrieval.get("feature_service_name") or ref_obj.name),
        "feature_service_version": existing_version or str(retrieval.get("feature_service_version") or ref_obj.version),
        "feature_contract_hash": existing_hash or str(retrieval.get("feature_contract_hash") or ref_obj.feature_contract_hash),
        "feature_view_names": existing_views or list(retrieval.get("feature_view_names") or ref_obj.feature_view_names),
    }


def _artifact_feature_contracts(
    *,
    pair: str,
    artifacts: dict[str, Any],
    timeframes: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    tf_map = dict(timeframes or {})
    out: dict[str, dict[str, Any]] = {}
    seen_normalized: set[str] = set()
    for component_key, ref in dict(artifacts or {}).items():
        normalized_key = _artifact_component_key(component_key)
        if normalized_key in seen_normalized:
            continue
        seen_normalized.add(normalized_key)
        timeframe = tf_map.get(component_key) or tf_map.get(normalized_key) or tf_map.get(component_key.replace("_xgb", "")) or tf_map.get(component_key.replace("_hmm", ""))
        out[str(normalized_key)] = _artifact_feature_contract(
            pair=pair,
            component_key=normalized_key,
            timeframe=timeframe,
            path_value=ref,
        )
    return out


def _validate_directional_belief_artifact(*, path_value: Any, runtime_required: bool) -> tuple[bool, list[str]]:
    warnings: list[str] = []
    txt = str(_artifact_path(path_value) or "").strip()
    if not txt:
        return False, warnings
    try:
        belief_dir = _resolve_artifact_dir(path_value)
    except Exception as exc:
        if runtime_required:
            raise ValueError(f"Registry entry missing required directional belief artifact: {txt}") from exc
        warnings.append("directional_belief_unresolved")
        return False, warnings
    meta_path = belief_dir / "meta.json"
    if not meta_path.exists():
        if runtime_required:
            raise ValueError(f"Registry entry missing required directional belief artifact meta.json: {belief_dir}")
        warnings.append("directional_belief_missing")
        return False, warnings
    meta = _load_json_if_exists(meta_path)
    contract = str(meta.get("belief_contract") or "directional_belief_v1")
    required_dirs = (
        ["scenario_xgb", "horizon_short_xgb", "horizon_trade_xgb", "horizon_structural_xgb"]
        if contract != "directional_belief_v2"
        else ["ranker_xgb", "ev_above_hurdle_xgb", "expected_net_ev_bps_xgb", "confirm_success_xgb", "fail_fast_xgb"]
    )
    missing = [name for name in required_dirs if not (belief_dir / name / "meta.json").exists()]
    if missing:
        if runtime_required:
            raise ValueError(f"Registry entry missing required directional belief artifact components ({','.join(missing)}): {belief_dir}")
        warnings.append(f"directional_belief_incomplete:{','.join(missing)}")
        return False, warnings
    return True, warnings


def _promotion_status(raw: dict[str, Any]) -> str:
    direct = str(raw.get("promotion_status") or "").strip()
    if direct:
        return direct
    report_refs = dict(raw.get("training_eval_reports") or {})
    for value in report_refs.values():
        path_txt = str(value or "").strip()
        if not path_txt:
            continue
        report = _load_json_if_exists(_resolve_artifact_dir(path_txt))
        decision = dict(report.get("promotion_decision") or {})
        status = str(decision.get("status") or "").strip()
        if status:
            return status
    return "unknown"


def _load_phase3_payload(refs: dict[str, Any], key: str) -> dict[str, Any]:
    path_txt = str(refs.get(key) or "").strip()
    if not path_txt:
        return {}
    resolved = _resolve_optional_path(path_txt, get_settings().project_root)
    if resolved is None or not resolved.exists():
        return {}
    return _load_json_if_exists(resolved)


def _phase3_values_match(values: list[str]) -> bool:
    material = [str(value).strip() for value in values if str(value).strip()]
    return len(set(material)) <= 1


def _phase3_values_complete(values: list[str]) -> bool:
    return all(str(value).strip() for value in values)


def _validate_phase3_evidence(*, raw: dict[str, Any], strict_activation: bool) -> list[str]:
    warnings: list[str] = []
    required = bool(raw.get("phase3_execution_required", False))
    refs = dict(raw.get("phase3_evidence") or {})
    if not required and not refs:
        return warnings
    required_keys = {
        "execution_metrics",
        "intent_replay_bundle",
        "market_replay_bundle",
        "golden_dataset_report",
        "stress_harness_summary",
        "harness_comparison",
        "risk_trace_schema",
        "internal_harness_manifest",
        "nautilus_harness_manifest",
        "lean_harness_manifest",
    }
    missing = sorted(key for key in required_keys if not str(refs.get(key) or "").strip())
    if missing:
        message = f"phase3_evidence_missing:{','.join(missing)}"
        if strict_activation and required:
            raise ValueError(message)
        warnings.append(message)
        return warnings
    for key in sorted(required_keys):
        path_txt = str(refs.get(key) or "").strip()
        if not path_txt:
            continue
        resolved = _resolve_optional_path(path_txt, get_settings().project_root)
        if resolved is None or not resolved.exists():
            message = f"phase3_evidence_unresolved:{key}"
            if strict_activation and required:
                raise ValueError(message)
            warnings.append(message)
    execution_metrics = _load_phase3_payload(refs, "execution_metrics")
    golden_dataset = _load_phase3_payload(refs, "golden_dataset_report")
    stress_summary = _load_phase3_payload(refs, "stress_harness_summary")
    risk_trace_schema = _load_phase3_payload(refs, "risk_trace_schema")
    intent_bundle = _load_phase3_payload(refs, "intent_replay_bundle")
    market_bundle = _load_phase3_payload(refs, "market_replay_bundle")
    harness_comparison = _load_phase3_payload(refs, "harness_comparison")
    manifest_payloads = {
        "internal": _load_phase3_payload(refs, "internal_harness_manifest"),
        "nautilus": _load_phase3_payload(refs, "nautilus_harness_manifest"),
        "lean": _load_phase3_payload(refs, "lean_harness_manifest"),
    }
    dataset_hashes = [
        str(execution_metrics.get("dataset_hash") or ""),
        str((golden_dataset.get("market") or {}).get("dataset_hash") or ""),
        str(market_bundle.get("dataset_hash") or ""),
        *[str(payload.get("dataset_hash") or "") for payload in manifest_payloads.values()],
    ]
    if not _phase3_values_complete(dataset_hashes) or not _phase3_values_match(dataset_hashes):
        message = "phase3_evidence_mismatch:dataset_hash"
        if strict_activation and required:
            raise ValueError(message)
        warnings.append(message)
    feature_service_names = [
        str(execution_metrics.get("feature_service_name") or ""),
        str((golden_dataset.get("market") or {}).get("feature_service_name") or ""),
        str(market_bundle.get("feature_service_name") or ""),
        *[str(payload.get("feature_service_name") or "") for payload in manifest_payloads.values()],
    ]
    if not _phase3_values_complete(feature_service_names) or not _phase3_values_match(feature_service_names):
        message = "phase3_evidence_mismatch:feature_service_name"
        if strict_activation and required:
            raise ValueError(message)
        warnings.append(message)
    feature_service_versions = [
        str(execution_metrics.get("feature_service_version") or ""),
        str((golden_dataset.get("market") or {}).get("feature_service_version") or ""),
        str(market_bundle.get("feature_service_version") or ""),
        *[str(payload.get("feature_service_version") or "") for payload in manifest_payloads.values()],
    ]
    if not _phase3_values_complete(feature_service_versions) or not _phase3_values_match(feature_service_versions):
        message = "phase3_evidence_mismatch:feature_service_version"
        if strict_activation and required:
            raise ValueError(message)
        warnings.append(message)
    kernel_versions = [
        str(execution_metrics.get("kernel_version") or ""),
        str((golden_dataset.get("intents") or {}).get("kernel_version") or ""),
        str(intent_bundle.get("kernel_version") or ""),
        str(risk_trace_schema.get("kernel_version") or ""),
        str(stress_summary.get("kernel_version") or ""),
        *[str(payload.get("kernel_version") or "") for payload in manifest_payloads.values()],
    ]
    if not _phase3_values_complete(kernel_versions) or not _phase3_values_match(kernel_versions):
        message = "phase3_evidence_mismatch:kernel_version"
        if strict_activation and required:
            raise ValueError(message)
        warnings.append(message)
    manifest_pairs = {engine: str(payload.get("pair") or "").upper() for engine, payload in manifest_payloads.items()}
    expected_pair = str(raw.get("pair") or "").upper()
    if expected_pair and any(value and value != expected_pair for value in manifest_pairs.values()):
        message = "phase3_evidence_mismatch:pair"
        if strict_activation and required:
            raise ValueError(message)
        warnings.append(message)
    comparison_manifests = list(harness_comparison.get("manifests") or [])
    comparison_engines = sorted(
        str(dict(item or {}).get("engine") or "").strip().lower()
        for item in comparison_manifests
        if str(dict(item or {}).get("engine") or "").strip()
    )
    if comparison_engines != ["internal", "lean", "nautilus"]:
        message = "phase3_evidence_mismatch:harness_comparison_manifests"
        if strict_activation and required:
            raise ValueError(message)
        warnings.append(message)
    return warnings


def _validate_activation_contracts(
    *,
    pair: str,
    tier: str,
    registry_label: str,
    artifacts: dict[str, Any],
    policies: dict[str, str],
    raw: dict[str, Any],
    feature_schema: dict[str, Any],
) -> dict[str, Any]:
    s = get_settings()
    required = sorted(list(_required_artifacts(policies)))
    missing = [key for key in required if not str(_artifact_path(artifacts.get(key))).strip()]
    if missing:
        raise ValueError(f"Registry file missing artifact paths ({','.join(missing)}): {registry_label}")

    warnings: list[str] = []
    artifact_validation_map = dict(artifacts)
    artifact_validation_map.pop("directional_belief", None)
    warnings.extend(
        _validate_artifact_dirs(
            registry_path=Path(registry_label),
            artifacts=artifact_validation_map,
            required=required,
            strict_activation=bool(s.strict_activation),
        )
    )

    intraday_contract = str(feature_schema.get("intraday_contract") or "").strip()
    if bool(s.require_hierarchical_intraday_contract):
        if intraday_contract != "hierarchical_v1":
            raise ValueError(
                f"Registry entry missing required intraday_contract=hierarchical_v1: {registry_label}"
            )
    elif intraday_contract != "hierarchical_v1":
        warnings.append("intraday_contract_missing_or_non_hierarchical")

    has_exit_model = bool(str(_artifact_path(artifacts.get("exit_policy"))).strip())
    has_reversal_models = bool(
        str(_artifact_path(artifacts.get("reversal_failure"))).strip()
        and str(_artifact_path(artifacts.get("reversal_opportunity"))).strip()
    )
    lifecycle_complete = bool(has_exit_model and has_reversal_models)
    lifecycle_required = bool(s.require_lifecycle_artifacts) and str(tier).strip().lower() == "tier1"
    if lifecycle_required:
        if not has_exit_model or not has_reversal_models:
            raise ValueError(f"Registry entry missing required lifecycle artifacts: {registry_label}")
    else:
        if not has_exit_model:
            warnings.append("exit_policy_missing")
        if not has_reversal_models:
            warnings.append("reversal_models_missing")

    capabilities = dict(raw.get("capabilities") or {})
    belief_path = artifacts.get("directional_belief")
    has_directional_belief = False
    if str(_artifact_path(belief_path)).strip():
        has_directional_belief, belief_warnings = _validate_directional_belief_artifact(
            path_value=belief_path,
            runtime_required=bool(s.belief_runtime_required),
        )
        warnings.extend(belief_warnings)
    capabilities.setdefault("has_exit_model", has_exit_model)
    capabilities.setdefault("has_reversal_models", has_reversal_models)
    capabilities.setdefault("lifecycle_complete", lifecycle_complete)
    capabilities.setdefault("has_directional_belief", has_directional_belief)

    runtime_compatible = raw.get("runtime_compatible")
    if runtime_compatible is False:
        message = f"runtime_incompatible:{registry_label}"
        if bool(s.strict_activation):
            raise ValueError(message)
        warnings.append(message)

    primary_intraday_path = (
        artifacts.get("intraday_tcn")
        if str(_artifact_path(artifacts.get("intraday_tcn"))).strip()
        else artifacts.get("intraday_xgb")
    )
    artifact_age_hours = _artifact_age_hours(primary_intraday_path)
    promotion_status = _promotion_status(raw)
    training_window_summary = dict(raw.get("training_window_summary") or {})
    warnings.extend(_validate_phase3_evidence(raw=raw, strict_activation=bool(s.strict_activation)))
    return {
        "warnings": warnings,
        "capabilities": {
            "has_exit_model": bool(capabilities.get("has_exit_model")),
            "has_reversal_models": bool(capabilities.get("has_reversal_models")),
            "lifecycle_complete": bool(capabilities.get("lifecycle_complete")),
            "has_directional_belief": bool(capabilities.get("has_directional_belief")),
        },
        "promotion_status": promotion_status,
        "artifact_age_hours": artifact_age_hours,
        "training_window_summary": training_window_summary,
        "intraday_contract": intraday_contract,
        "lifecycle_complete": lifecycle_complete,
    }


def parse_registry_entry(path: Path) -> dict[str, Any]:
    s = get_settings()
    raw = _read_json(path)
    pair = str(raw.get("pair") or "").upper().strip()
    if not pair:
        raise ValueError(f"Registry file missing pair: {path}")

    model_set_id = str(raw.get("run_id") or path.stem)
    tier = str(raw.get("tier") or s.pair_tier(pair)).strip().lower() or "tier2"
    artifacts_raw = dict(raw.get("artifacts") or {})
    policies_raw = dict(raw.get("policies") or {})
    policies = {
        "swing": str(policies_raw.get("swing", "transformer_primary_xgb_fallback")),
        "intraday": str(policies_raw.get("intraday", "tcn_primary_xgb_fallback")),
    }
    artifacts = {
        "regime": _artifact_ref(artifacts_raw.get("regime")),
        "meta": _artifact_ref(artifacts_raw.get("meta")),
        "swing_transformer": _artifact_ref(artifacts_raw.get("swing_transformer")),
        "swing_xgb": _artifact_ref(artifacts_raw.get("swing_xgb") or artifacts_raw.get("swing")),
        "intraday_tcn": _artifact_ref(artifacts_raw.get("intraday_tcn")),
        "intraday_xgb": _artifact_ref(artifacts_raw.get("intraday_xgb") or artifacts_raw.get("intraday")),
        "directional_belief": _artifact_ref(artifacts_raw.get("directional_belief")),
        "exit_policy": _artifact_ref(artifacts_raw.get("exit_policy") or artifacts_raw.get("exit")),
        "reversal_failure": _artifact_ref(artifacts_raw.get("reversal_failure")),
        "reversal_opportunity": _artifact_ref(artifacts_raw.get("reversal_opportunity")),
    }
    # Compatibility aliases for loaders expecting generic keys.
    artifacts["swing"] = dict(artifacts["swing_xgb"])
    artifacts["intraday"] = dict(artifacts["intraday_xgb"])
    feature_contracts = _artifact_feature_contracts(
        pair=pair,
        artifacts=artifacts,
        timeframes=dict(raw.get("timeframes") or {}),
    )
    for component_key, contract in feature_contracts.items():
        if component_key in artifacts and isinstance(artifacts[component_key], dict):
            artifacts[component_key].update(contract)
    if "swing_xgb" in artifacts and isinstance(artifacts.get("swing"), dict):
        artifacts["swing"].update(dict(artifacts["swing_xgb"]))
    if "intraday_xgb" in artifacts and isinstance(artifacts.get("intraday"), dict):
        artifacts["intraday"].update(dict(artifacts["intraday_xgb"]))
    feature_schema = _feature_schema(raw)
    validation = _validate_activation_contracts(
        pair=pair,
        tier=tier,
        registry_label=str(path),
        artifacts=artifacts,
        policies=policies,
        raw=raw,
        feature_schema=feature_schema,
    )
    trained_at = raw.get("trained_at")
    data_window_end = raw.get("data_window_end")
    release_package = build_activation_package(
        bundle_run_id=str(raw.get("bundle_run_id") or model_set_id),
        pair=pair,
        target_alias=str(raw.get("intended_alias") or ""),
        promotion_status=str(validation.get("promotion_status") or ""),
        runtime_compatible=_runtime_compatible_from_raw(raw=raw, artifacts=artifacts),
        dataset_fingerprint=str(raw.get("dataset_fingerprint") or ""),
        feature_service_version=str(raw.get("feature_service_version") or ""),
        risk_config_version=str(raw.get("risk_config_version") or ""),
        release_status=str(raw.get("release_status") or ""),
        rollback_target=raw.get("rollback_target"),
        operator_signoff=raw.get("operator_signoff"),
        canary_plan=raw.get("canary_plan"),
        promotion_gates=raw.get("promotion_gates"),
        release_notes=raw.get("release_notes"),
        activation_package=raw.get("activation_package"),
        evidence_refs={
            "model_manifest": str(raw.get("model_manifest") or ""),
        },
        metadata={
            "registry_path": str(path),
            "phase3_execution_required": bool(raw.get("phase3_execution_required", False)),
        },
    )

    return {
        "pair": pair,
        "tier": tier,
        "model_set_id": model_set_id,
        "registry_path": str(path),
        "artifacts": artifacts,
        "policies": policies,
        "metadata": {
            **raw,
            "tier": tier,
            "trained_at": trained_at,
            "data_window_end": data_window_end,
            "promotion_status": str(validation.get("promotion_status") or ""),
            "artifact_age_hours": validation.get("artifact_age_hours"),
            "intraday_contract": str(validation.get("intraday_contract") or ""),
            "lifecycle_complete": bool(validation.get("lifecycle_complete")),
            "training_window_summary": dict(validation.get("training_window_summary") or {}),
            "feature_schema": feature_schema,
            "phase3_execution_required": bool(raw.get("phase3_execution_required", False)),
            "phase3_evidence": dict(raw.get("phase3_evidence") or {}),
            "component_feature_contracts": feature_contracts,
            "activation_warnings": list(validation.get("warnings") or []),
            "warnings": list(validation.get("warnings") or []),
            "capabilities": dict(validation.get("capabilities") or {}),
            **release_metadata_payload(release_package),
        },
    }


def latest_registry_for_pair(*, registry_root: Path, pair: str) -> Path | None:
    pair_u = str(pair).upper().strip()
    candidates: list[tuple[float, Path]] = []
    for p in sorted(registry_root.glob("*.json")):
        try:
            item = parse_registry_entry(p)
        except Exception:
            continue
        if str(item.get("pair", "")).upper() != pair_u:
            continue
        candidates.append((p.stat().st_mtime, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "active_model_sets": {}}
    try:
        payload = _read_json(path)
    except Exception:
        return {"schema_version": 1, "active_model_sets": {}}
    payload.setdefault("schema_version", 1)
    payload.setdefault("active_model_sets", {})
    if not isinstance(payload.get("active_model_sets"), dict):
        payload["active_model_sets"] = {}
    return payload


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _merge_metadata_patch(base: dict[str, Any], patch: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base or {})
    for key, value in dict(patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[str(key)] = {**dict(out.get(key) or {}), **dict(value or {})}
        else:
            out[str(key)] = value
    return out


def _bundle_manifest_to_item(bundle: BundleManifest, *, alias: str) -> dict[str, Any]:
    release_package = sync_bundle_release_package(bundle, target_alias=str(alias))
    artifacts: dict[str, Any] = {}
    component_feature_contracts: dict[str, dict[str, Any]] = {}
    for component_key, raw_ref in dict(bundle.components or {}).items():
        ref = raw_ref if isinstance(raw_ref, ModelVersionRef) else ModelVersionRef(**dict(raw_ref or {}))
        contract = _artifact_feature_contract(
            pair=str(bundle.pair).upper(),
            component_key=str(component_key),
            timeframe=dict(bundle.timeframes or {}).get(component_key),
            path_value={
                **ref.to_dict(),
                "path": str(ref.evidence_refs.get("artifact_path") or ""),
            },
        )
        component_feature_contracts[str(component_key)] = dict(contract)
        artifacts[str(component_key)] = {
            **ref.to_dict(),
            "model_uri": str(ref.model_uri or f"models:/{ref.model_name}@{alias}" if ref.model_name else ""),
            "alias": str(alias),
            "bundle_run_id": str(bundle.bundle_run_id),
            "dataset_fingerprint": str(bundle.dataset_fingerprint),
            **contract,
        }
    if "swing_xgb" in artifacts:
        artifacts["swing"] = dict(artifacts["swing_xgb"])
    if "intraday_xgb" in artifacts:
        artifacts["intraday"] = dict(artifacts["intraday_xgb"])
    metadata = {
        "run_id": str(bundle.bundle_run_id),
        "bundle_run_id": str(bundle.bundle_run_id),
        "dataset_fingerprint": str(bundle.dataset_fingerprint),
        "feature_service_version": str(bundle.feature_service_version),
        "label_version": str(bundle.label_version),
        "risk_config_version": str(bundle.risk_config_version),
        "pair": str(bundle.pair).upper(),
        "tier": str(bundle.tier),
        "promotion_status": str(bundle.promotion_status),
        "feature_schema": dict(bundle.feature_schema or {}),
        "component_feature_contracts": component_feature_contracts,
        "training_window_summary": dict(bundle.training_window_summary or {}),
        "policies": dict(bundle.policies or {}),
        "capabilities": dict(bundle.capabilities or {}),
        "lifecycle_complete": bool(bundle.lifecycle_complete),
        "training_config": dict(bundle.training_config or {}),
        "promotion_components": dict(bundle.promotion_components or {}),
        "training_eval_reports": dict(bundle.training_eval_reports or {}),
        "timeframes": dict(bundle.timeframes or {}),
        "intended_alias": str(alias),
        "mlflow": {**dict(bundle.mlflow or {}), "activated_alias": str(alias)},
        **dict(bundle.metadata or {}),
        **release_metadata_payload(release_package),
    }
    return {
        "pair": str(bundle.pair).upper(),
        "tier": str(bundle.tier),
        "model_set_id": str(bundle.bundle_run_id or f"{str(bundle.pair).lower()}-{alias}"),
        "registry_path": f"mlflow://{str(bundle.pair).upper()}@{str(alias)}",
        "artifacts": artifacts,
        "policies": dict(bundle.policies or {}),
        "metadata": metadata,
    }


def activate_registry_file(
    *,
    database_url: str,
    registry_file: Path,
    manifest_path: Path,
    default_session_id: str = "default",
    command_ttl_secs: float = 120.0,
    enabled: bool = True,
    metadata_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = parse_registry_entry(registry_file)
    item["metadata"] = _merge_metadata_patch(dict(item.get("metadata") or {}), metadata_patch)
    svc = RuntimeService(
        database_url=database_url,
        default_session_id=default_session_id,
        command_ttl_secs=command_ttl_secs,
    )
    svc.upsert_active_model_set(
        pair=str(item["pair"]),
        model_set_id=str(item["model_set_id"]),
        registry_path=str(item["registry_path"]),
        artifacts=dict(item["artifacts"]),
        metadata=dict(item.get("metadata") or {}),
        enabled=bool(enabled),
    )

    manifest = load_manifest(manifest_path)
    active = dict(manifest.get("active_model_sets") or {})
    active[str(item["pair"])] = {
        "model_set_id": str(item["model_set_id"]),
        "registry_path": str(item["registry_path"]),
        "artifacts": dict(item["artifacts"]),
        "policies": dict(item.get("policies") or {}),
        "metadata": dict(item.get("metadata") or {}),
        "enabled": bool(enabled),
    }
    manifest["active_model_sets"] = active
    write_manifest(manifest_path, manifest)
    return item


def activate_mlflow_alias(
    *,
    database_url: str,
    manifest_path: Path,
    pairs: list[str],
    alias: str,
    default_session_id: str = "default",
    command_ttl_secs: float = 120.0,
    timeframes: dict[str, str] | None = None,
    metadata_patch: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    svc = RuntimeService(
        database_url=database_url,
        default_session_id=default_session_id,
        command_ttl_secs=command_ttl_secs,
    )
    manifest = load_manifest(manifest_path)
    active = dict(manifest.get("active_model_sets") or {})
    out: list[dict[str, Any]] = []
    for pair in pairs:
        bundle = resolve_bundle_manifest_by_alias(pair=str(pair).upper(), alias=str(alias), timeframes=timeframes)
        item = _bundle_manifest_to_item(bundle, alias=str(alias))
        validation = _validate_activation_contracts(
            pair=str(item["pair"]),
            tier=str(item.get("tier") or ""),
            registry_label=str(item["registry_path"]),
            artifacts=dict(item.get("artifacts") or {}),
            policies=dict(item.get("policies") or {}),
            raw=dict(item.get("metadata") or {}),
            feature_schema=dict((item.get("metadata") or {}).get("feature_schema") or {}),
        )
        item["metadata"] = {
            **dict(item.get("metadata") or {}),
            "promotion_status": str(validation.get("promotion_status") or ""),
            "artifact_age_hours": validation.get("artifact_age_hours"),
            "intraday_contract": str(validation.get("intraday_contract") or ""),
            "lifecycle_complete": bool(validation.get("lifecycle_complete")),
            "training_window_summary": dict(validation.get("training_window_summary") or {}),
            "activation_warnings": list(validation.get("warnings") or []),
            "warnings": list(validation.get("warnings") or []),
            "capabilities": dict(validation.get("capabilities") or {}),
        }
        item["metadata"] = _merge_metadata_patch(dict(item.get("metadata") or {}), metadata_patch)
        svc.upsert_active_model_set(
            pair=str(item["pair"]),
            model_set_id=str(item["model_set_id"]),
            registry_path=str(item["registry_path"]),
            artifacts=dict(item["artifacts"]),
            metadata=dict(item.get("metadata") or {}),
            enabled=True,
        )
        active[str(item["pair"])] = {
            "model_set_id": str(item["model_set_id"]),
            "registry_path": str(item["registry_path"]),
            "artifacts": dict(item["artifacts"]),
            "policies": dict(item.get("policies") or {}),
            "metadata": dict(item.get("metadata") or {}),
            "enabled": True,
        }
        out.append(item)
    manifest["active_model_sets"] = active
    write_manifest(manifest_path, manifest)
    return out


def activate_pairs(
    *,
    database_url: str,
    registry_root: Path,
    manifest_path: Path,
    pairs: list[str],
    default_session_id: str = "default",
    command_ttl_secs: float = 120.0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pair in pairs:
        path = latest_registry_for_pair(registry_root=registry_root, pair=pair)
        if path is None:
            continue
        item = activate_registry_file(
            database_url=database_url,
            registry_file=path,
            manifest_path=manifest_path,
            default_session_id=default_session_id,
            command_ttl_secs=command_ttl_secs,
            enabled=True,
        )
        out.append(item)
    return out


def backfill_mlflow_state(
    *,
    active_manifest_path: Path,
    registry_root: Path,
    shadow_root: Path,
) -> dict[str, Any]:
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow

    return backfill_current_state_to_mlflow(
        active_manifest_path=active_manifest_path,
        registry_root=registry_root,
        shadow_root=shadow_root,
        import_fn=import_compat_bundle_to_mlflow,
    )


def set_mlflow_bundle_alias(
    *,
    pair: str,
    alias: str,
    bundle_run_id: str,
    timeframes: dict[str, str] | None = None,
) -> dict[str, Any]:
    bundle = resolve_bundle_manifest_by_alias(pair=str(pair).upper(), alias=str(alias), timeframes=timeframes)
    if str(bundle.bundle_run_id) != str(bundle_run_id):
        raise ValueError(
            f"bundle_run_id mismatch for {str(pair).upper()} alias {alias}: expected {bundle_run_id}, found {bundle.bundle_run_id}"
        )
    return set_bundle_alias(bundle=bundle, alias=str(alias))
