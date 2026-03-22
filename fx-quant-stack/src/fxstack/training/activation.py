from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid registry payload: {path}")
    return payload


def _artifact_path(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("path") or "")
    return ""


def _resolve_artifact_dir(path_value: str) -> Path:
    s = get_settings()
    workspace_root = Path(s.project_root).parent
    raw = Path(str(path_value).replace("\\", "/")).expanduser()
    candidates = [
        raw,
        workspace_root / raw,
        Path(s.project_root) / raw,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return workspace_root / raw


def _validate_artifact_dirs(
    *,
    registry_path: Path,
    artifacts: dict[str, str],
    required: list[str],
    strict_activation: bool,
) -> list[str]:
    warnings: list[str] = []
    keys = set(required) | {k for k, v in artifacts.items() if str(v).strip()}
    for key in sorted(keys):
        txt = str(artifacts.get(key, "")).strip()
        if not txt:
            continue
        candidate = _resolve_artifact_dir(txt)
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


def _artifact_meta(path_value: str) -> dict[str, Any]:
    txt = str(path_value or "").strip()
    if not txt:
        return {}
    return _load_json_if_exists(_resolve_artifact_dir(txt) / "meta.json")


def _artifact_age_hours(path_value: str) -> float | None:
    meta = _artifact_meta(path_value)
    if not meta:
        return None
    created = float(meta.get("trained_at", meta.get("created_at", 0.0)) or 0.0)
    if created <= 0.0:
        return None
    return max(0.0, (time.time() - created) / 3600.0)


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
        "regime": _artifact_path(artifacts_raw.get("regime")),
        "meta": _artifact_path(artifacts_raw.get("meta")),
        "swing_transformer": _artifact_path(artifacts_raw.get("swing_transformer")),
        "swing_xgb": _artifact_path(artifacts_raw.get("swing_xgb")) or _artifact_path(artifacts_raw.get("swing")),
        "intraday_tcn": _artifact_path(artifacts_raw.get("intraday_tcn")),
        "intraday_xgb": _artifact_path(artifacts_raw.get("intraday_xgb")) or _artifact_path(artifacts_raw.get("intraday")),
        "exit_policy": _artifact_path(artifacts_raw.get("exit_policy")) or _artifact_path(artifacts_raw.get("exit")),
        "reversal_failure": _artifact_path(artifacts_raw.get("reversal_failure")),
        "reversal_opportunity": _artifact_path(artifacts_raw.get("reversal_opportunity")),
    }
    # Compatibility aliases for loaders expecting generic keys.
    artifacts["swing"] = str(artifacts["swing_xgb"])
    artifacts["intraday"] = str(artifacts["intraday_xgb"])

    required = sorted(list(_required_artifacts(policies)))
    missing = [k for k in required if not str(artifacts.get(k, "")).strip()]
    if missing:
        raise ValueError(f"Registry file missing artifact paths ({','.join(missing)}): {path}")

    warnings: list[str] = []
    warnings.extend(
        _validate_artifact_dirs(
            registry_path=path,
            artifacts=artifacts,
            required=required,
            strict_activation=bool(s.strict_activation),
        )
    )

    feature_schema = _feature_schema(raw)
    intraday_contract = str(feature_schema.get("intraday_contract") or "").strip()
    if bool(s.require_hierarchical_intraday_contract):
        if intraday_contract != "hierarchical_v1":
            raise ValueError(
                f"Registry entry missing required intraday_contract=hierarchical_v1: {path}"
            )
    elif intraday_contract != "hierarchical_v1":
        warnings.append("intraday_contract_missing_or_non_hierarchical")

    has_exit_model = bool(str(artifacts.get("exit_policy", "")).strip())
    has_reversal_models = bool(
        str(artifacts.get("reversal_failure", "")).strip()
        and str(artifacts.get("reversal_opportunity", "")).strip()
    )
    lifecycle_complete = bool(has_exit_model and has_reversal_models)
    lifecycle_required = bool(s.require_lifecycle_artifacts) and tier == "tier1"
    if lifecycle_required:
        if not has_exit_model or not has_reversal_models:
            raise ValueError(f"Registry entry missing required lifecycle artifacts: {path}")
    else:
        if not has_exit_model:
            warnings.append("exit_policy_missing")
        if not has_reversal_models:
            warnings.append("reversal_models_missing")

    capabilities = dict(raw.get("capabilities") or {})
    capabilities.setdefault("has_exit_model", has_exit_model)
    capabilities.setdefault("has_reversal_models", has_reversal_models)
    capabilities.setdefault("lifecycle_complete", lifecycle_complete)

    primary_intraday_path = str(artifacts.get("intraday_tcn") or artifacts.get("intraday_xgb") or "").strip()
    artifact_age_hours = _artifact_age_hours(primary_intraday_path)
    promotion_status = _promotion_status(raw)
    trained_at = raw.get("trained_at")
    data_window_end = raw.get("data_window_end")
    training_window_summary = dict(raw.get("training_window_summary") or {})

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
            "promotion_status": promotion_status,
            "artifact_age_hours": artifact_age_hours,
            "intraday_contract": intraday_contract,
            "lifecycle_complete": lifecycle_complete,
            "training_window_summary": training_window_summary,
            "feature_schema": feature_schema,
            "activation_warnings": warnings,
            "warnings": warnings,
            "capabilities": {
                "has_exit_model": bool(capabilities.get("has_exit_model")),
                "has_reversal_models": bool(capabilities.get("has_reversal_models")),
                "lifecycle_complete": bool(capabilities.get("lifecycle_complete")),
            },
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


def activate_registry_file(
    *,
    database_url: str,
    registry_file: Path,
    manifest_path: Path,
    default_session_id: str = "default",
    command_ttl_secs: float = 120.0,
    enabled: bool = True,
) -> dict[str, Any]:
    item = parse_registry_entry(registry_file)
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
