from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from fxstack.mlops.model_uri import (
    load_bundle_manifest_from_run,
    normalize_artifact_ref,
    resolve_model_artifact_path,
)
from fxstack.mlops.run_context import MlflowRunContext, build_standard_run_tags, configure_mlflow
from fxstack.mlops.types import BundleManifest, LineageSnapshot, ModelVersionRef
from fxstack.models.artifact_contract import (
    ARTIFACT_PAYLOAD_DIGEST_KEY,
    artifact_io_locked,
    validate_artifact_contract,
)
from fxstack.settings import get_settings
from fxstack.training.release_package import release_metadata_payload, sync_bundle_release_package
from fxstack.utils.hashing import hash_mapping

REQUIRED_COMPONENT_KEYS = (
    "regime",
    "swing_xgb",
    "intraday_xgb",
    "meta",
    "exit_policy",
    "reversal_failure",
    "reversal_opportunity",
)
OPTIONAL_COMPONENT_KEYS = (
    "swing_transformer",
    "swing_patchtst",
    "intraday_tcn",
    "intraday_patchtst",
    "directional_belief",
)
COMPONENT_FAMILIES = {
    "regime": "regime_hmm",
    "swing_xgb": "swing_xgb",
    "intraday_xgb": "intraday_xgb",
    "meta": "meta_filter",
    "exit_policy": "exit_policy_xgb",
    "reversal_failure": "reversal_failure_xgb",
    "reversal_opportunity": "reversal_opportunity_xgb",
    "swing_transformer": "swing_transformer",
    "swing_patchtst": "swing_patchtst",
    "intraday_tcn": "intraday_tcn",
    "intraday_patchtst": "intraday_patchtst",
    "directional_belief": "directional_belief",
}
COMPONENT_ARTIFACT_NAMES = {
    **COMPONENT_FAMILIES,
    "meta": "meta_filter_xgb",
    "directional_belief": "",
}


def experiment_name_for_component(*, family: str, pair: str, timeframe: str) -> str:
    return f"fx/{str(family).lower()}/{str(pair).upper()}/{str(timeframe).upper()}"


def registered_model_name(*, family: str, pair: str, timeframe: str) -> str:
    return f"fx.{str(family).lower()}.{str(pair).upper()}.{str(timeframe).upper()}"


def _timeframe_for_component(component_key: str, *, timeframes: dict[str, str]) -> str:
    if component_key == "regime":
        return str(timeframes.get("regime") or "")
    if component_key in {"swing_xgb", "swing_transformer", "swing_patchtst"}:
        return str(timeframes.get("swing") or "")
    return str(timeframes.get("intraday") or "")


def _required_component_keys_from_bundle(bundle: BundleManifest) -> list[str]:
    out = list(REQUIRED_COMPONENT_KEYS)
    caps = dict(bundle.capabilities or {})
    if bool(caps.get("has_directional_belief")) or "directional_belief" in bundle.components:
        out.append("directional_belief")
    return out


def _bundle_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = dict((payload or {}).get("metadata") or {})
    for key, value in dict(payload or {}).items():
        if str(key).startswith("phase4_") or str(key).startswith("phase5_"):
            metadata.setdefault(str(key), value)
    return metadata


def _artifact_reporting_evidence_refs(path: Path) -> dict[str, str]:
    meta = _load_json(path / "meta.json")
    report_dir = path / "reports"
    refs: dict[str, str] = {}
    for key in [
        "training_report",
        "promotion_decision",
        "model_manifest",
        "sequence_dataset_manifest",
        "portfolio_report",
        "challenger_head_to_head",
        "portfolio_disagreement",
    ]:
        value = str(meta.get(key) or "").strip()
        if value:
            refs[key] = value
    for key, filename in [
        ("training_report", "training_report.json"),
        ("promotion_decision", "promotion_decision.json"),
    ]:
        file_path = report_dir / filename
        if key not in refs and file_path.exists():
            refs[key] = str(file_path)
    return refs


def _ensure_registered_model(client: Any, name: str) -> None:
    try:
        client.create_registered_model(name)
    except Exception:
        return


def _save_fxstack_model_package(
    *,
    artifact_path: Path,
    package_root: Path,
    component_key: str,
    model_family: str,
    pair: str,
    timeframe: str,
    expected_digest: str,
    expected_name: str | None,
) -> None:
    package_root.mkdir(parents=True, exist_ok=True)
    legacy_root = package_root / "legacy_artifact"
    shutil.copytree(artifact_path, legacy_root, dirs_exist_ok=True)
    _validate_component_artifact(
        legacy_root,
        component_key=component_key,
        expected_digest=expected_digest,
        expected_name=expected_name,
        label=f"registration_copy:{component_key}",
    )

    configure_mlflow()
    from mlflow.models import Model  # type: ignore
    from mlflow.models.model import MLMODEL_FILE_NAME  # type: ignore

    mlmodel = Model()
    mlmodel.add_flavor(
        "fxstack",
        artifact_path="legacy_artifact",
        component_key=str(component_key),
        model_family=str(model_family),
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
    )
    mlmodel.save(str(package_root / MLMODEL_FILE_NAME))
    # Keep a small sidecar for non-MLflow readers and tests.
    (package_root / "fxstack_flavor.json").write_text(
        json.dumps(
            {
                "component_key": str(component_key),
                "model_family": str(model_family),
                "pair": str(pair).upper(),
                "timeframe": str(timeframe).upper(),
                "artifact_path": "legacy_artifact",
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _search_existing_version(
    *,
    client: Any,
    model_name: str,
    dataset_fingerprint: str,
    artifact_hash: str,
    bundle_run_id: str,
) -> Any | None:
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
        if str(tags.get("fxstack.dataset_fingerprint") or "") != str(dataset_fingerprint or ""):
            continue
        if str(tags.get("fxstack.artifact_hash") or "") != str(artifact_hash or ""):
            continue
        if bundle_run_id and str(tags.get("fxstack.bundle_run_id") or "") != str(bundle_run_id):
            continue
        return detail
    return None


def _wait_model_version_ready(client: Any, *, model_name: str, version: str, timeout_secs: float = 60.0) -> Any:
    deadline = time.time() + max(1.0, float(timeout_secs))
    last = None
    while time.time() < deadline:
        last = client.get_model_version(name=model_name, version=str(version))
        status = str(getattr(last, "status", "") or "").upper()
        if status in {"READY", ""}:
            return last
        if status in {"FAILED_REGISTRATION", "FAILED"}:
            raise RuntimeError(f"MLflow model version failed registration: {model_name} v{version}")
        time.sleep(0.5)
    return last if last is not None else client.get_model_version(name=model_name, version=str(version))


def _validate_component_artifact(
    artifact_path: Path,
    *,
    component_key: str,
    expected_digest: str | None = None,
    expected_name: str | None = None,
    label: str,
) -> dict[str, Any]:
    if str(component_key) == "directional_belief":
        from fxstack.belief.engine import validate_directional_belief_artifact_contract

        return validate_directional_belief_artifact_contract(
            artifact_path,
            expected_digest=expected_digest,
        )
    return validate_artifact_contract(
        artifact_path,
        label=label,
        expected_digest=expected_digest,
        expected_name=expected_name,
    )


@artifact_io_locked
def register_component_version(
    *,
    run: MlflowRunContext,
    component_key: str,
    pair: str,
    timeframe: str,
    artifact_path: Path,
    lineage: LineageSnapshot,
    bundle_run_id: str,
    evidence_refs: dict[str, str] | None = None,
    runtime_compatible: bool = True,
    intended_alias: str = "",
    extra_tags: dict[str, Any] | None = None,
) -> ModelVersionRef:
    family = str(COMPONENT_FAMILIES.get(component_key) or component_key)
    expected_name = str(COMPONENT_ARTIFACT_NAMES.get(component_key) or "") or None
    artifact_meta = _validate_component_artifact(
        artifact_path,
        component_key=component_key,
        label=f"registration:{component_key}",
        expected_name=expected_name,
    )
    artifact_hash = str(artifact_meta.get(ARTIFACT_PAYLOAD_DIGEST_KEY) or "").strip()
    if not artifact_hash:
        raise ValueError(f"artifact_registry_hash_missing:registration:{component_key}")
    if not run.enabled:
        return ModelVersionRef(
            component_key=str(component_key),
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
            model_family=family,
            bundle_run_id=str(bundle_run_id),
            dataset_fingerprint=str(lineage.dataset_fingerprint),
            path=str(artifact_path),
            artifact_hash=artifact_hash,
            runtime_compatible=bool(runtime_compatible),
            evidence_refs={str(k): str(v) for k, v in dict(evidence_refs or {}).items()},
        )

    if not run.run_id:
        raise RuntimeError("MLflow run context is not active")

    model_name = registered_model_name(family=family, pair=pair, timeframe=timeframe)
    client = configure_mlflow().tracking.MlflowClient()
    _ensure_registered_model(client, model_name)

    reuse = _search_existing_version(
        client=client,
        model_name=model_name,
        dataset_fingerprint=str(lineage.dataset_fingerprint),
        artifact_hash=artifact_hash,
        bundle_run_id=str(bundle_run_id),
    )
    if reuse is None:
        with tempfile.TemporaryDirectory(prefix=f"fxstack_mlflow_{component_key}_") as tmp_dir:
            package_root = Path(tmp_dir) / "model"
            _save_fxstack_model_package(
                artifact_path=artifact_path,
                package_root=package_root,
                component_key=component_key,
                model_family=family,
                pair=pair,
                timeframe=timeframe,
                expected_digest=artifact_hash,
                expected_name=expected_name,
            )
            run.log_artifacts(package_root, artifact_path="model")
        created = client.create_model_version(
            name=model_name,
            source=f"runs:/{run.run_id}/model",
            run_id=run.run_id,
        )
        detail = _wait_model_version_ready(client, model_name=model_name, version=str(created.version))
    else:
        detail = reuse

    version = str(getattr(detail, "version", "") or "")
    tags = {
        "fxstack.component_key": str(component_key),
        "fxstack.model_family": str(family),
        "fxstack.pair": str(pair).upper(),
        "fxstack.timeframe": str(timeframe).upper(),
        "fxstack.bundle_run_id": str(bundle_run_id),
        "fxstack.dataset_fingerprint": str(lineage.dataset_fingerprint),
        "fxstack.artifact_hash": str(artifact_hash),
        "fxstack.feature_service_version": str(lineage.feature_service_version),
        "fxstack.label_version": str(lineage.label_version),
        "fxstack.risk_config_version": str(lineage.risk_config_version),
        "fxstack.runtime_compatible": "1" if runtime_compatible else "0",
        "fxstack.intended_alias": str(intended_alias or ""),
    }
    for key, value in dict(extra_tags or {}).items():
        tags[str(key)] = "" if value is None else str(value)
    for key, value in tags.items():
        client.set_model_version_tag(
            name=model_name,
            version=version,
            key=str(key),
            value=str(value),
        )
    detail = client.get_model_version(name=model_name, version=version)
    published_tags = dict(getattr(detail, "tags", {}) or {})
    for key, expected_value in tags.items():
        actual_value = str(published_tags.get(key) or "")
        if actual_value != str(expected_value):
            raise RuntimeError(
                f"model_version_tag_verification_failed:{component_key}:{key}:"
                f"expected:{expected_value}|actual:{actual_value or '<missing>'}"
            )

    return ModelVersionRef(
        component_key=str(component_key),
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        model_family=family,
        model_name=model_name,
        model_version=version,
        model_uri=f"models:/{model_name}/{version}" if version else "",
        alias=str(intended_alias or ""),
        run_id=str(getattr(detail, "run_id", "") or run.run_id),
        bundle_run_id=str(bundle_run_id),
        dataset_fingerprint=str(lineage.dataset_fingerprint),
        path=str(artifact_path),
        artifact_hash=artifact_hash,
        runtime_compatible=bool(runtime_compatible),
        evidence_refs={str(k): str(v) for k, v in dict(evidence_refs or {}).items()},
        tags={str(k): str(v) for k, v in tags.items()},
    )


def _bind_ref_to_registered_version(
    *,
    ref: ModelVersionRef,
    component_key: str,
    alias: str,
    current: Any,
    bundle_run_id: str,
    trusted_pair: str,
    trusted_timeframe: str,
) -> ModelVersionRef:
    expected_pair = str(trusted_pair).upper().strip()
    expected_family = str(COMPONENT_FAMILIES.get(component_key) or component_key)
    expected_timeframe = str(trusted_timeframe).upper().strip()
    expected_model_name = registered_model_name(
        family=expected_family,
        pair=expected_pair,
        timeframe=expected_timeframe,
    )
    ref_identity = {
        "component_key": (str(ref.component_key), str(component_key)),
        "pair": (str(ref.pair).upper(), expected_pair),
        "model_family": (str(ref.model_family), expected_family),
        "timeframe": (str(ref.timeframe).upper(), expected_timeframe),
        "model_name": (str(ref.model_name), expected_model_name),
        "bundle_run_id": (str(ref.bundle_run_id), str(bundle_run_id)),
    }
    for field_name, (actual_value, expected_value) in ref_identity.items():
        if not expected_value or actual_value != expected_value:
            raise RuntimeError(
                f"component_ref_identity_mismatch:{component_key}:{field_name}:"
                f"expected:{expected_value or '<missing>'}|"
                f"actual:{actual_value or '<missing>'}"
            )
    tags = dict(getattr(current, "tags", {}) or {})
    current_bundle_run_id = str(tags.get("fxstack.bundle_run_id") or "")
    if not current_bundle_run_id:
        raise RuntimeError(f"missing_bundle_run_id:{component_key}")
    if current_bundle_run_id != str(bundle_run_id):
        raise RuntimeError(f"bundle_mismatch:{component_key}")
    expected_tags = {
        "fxstack.component_key": str(component_key),
        "fxstack.pair": expected_pair,
        "fxstack.model_family": expected_family,
        "fxstack.timeframe": expected_timeframe,
    }
    for tag_key, expected_value in expected_tags.items():
        actual_value = str(tags.get(tag_key) or "")
        if not expected_value:
            continue
        if not actual_value:
            raise RuntimeError(
                f"missing_registered_tag:{component_key}:{tag_key}"
            )
        if actual_value.upper() != expected_value.upper():
            raise RuntimeError(
                f"registered_tag_mismatch:{component_key}:{tag_key}:"
                f"expected:{expected_value}|actual:{actual_value}"
            )
    current_hash = str(tags.get("fxstack.artifact_hash") or "").strip().lower()
    if not current_hash:
        raise RuntimeError(f"missing_artifact_hash:{component_key}")
    prior_hash = str(ref.artifact_hash or "").strip().lower()
    if not prior_hash:
        raise RuntimeError(f"artifact_registry_hash_missing:{component_key}")
    if prior_hash != current_hash:
        raise RuntimeError(
            f"artifact_hash_mismatch:{component_key}:"
            f"manifest:{prior_hash}|registered:{current_hash}"
        )
    version = str(getattr(current, "version", "") or "").strip()
    run_id = str(getattr(current, "run_id", "") or ref.run_id)
    current_model_name = str(getattr(current, "name", "") or "").strip()
    if current_model_name and current_model_name != expected_model_name:
        raise RuntimeError(
            f"registered_model_name_mismatch:{component_key}:"
            f"expected:{expected_model_name}|actual:{current_model_name}"
        )
    if not version:
        raise RuntimeError(f"incomplete_registered_version:{component_key}")
    ref.model_version = version
    ref.model_uri = f"models:/{ref.model_name}/{version}"
    ref.alias = str(alias)
    ref.run_id = run_id
    ref.path = ""
    ref.artifact_hash = current_hash
    ref.tags = {str(key): str(value) for key, value in tags.items()}
    resolve_model_artifact_path(ref.to_dict())
    return ref


def set_bundle_alias(*, bundle: BundleManifest, alias: str) -> dict[str, Any]:
    s = get_settings()
    if not bool(s.mlflow_enabled):
        return {"ok": False, "reason": "mlflow_disabled", "alias": str(alias)}
    client = configure_mlflow().tracking.MlflowClient()
    refreshed: dict[str, ModelVersionRef] = {}
    planned_aliases: list[tuple[str, ModelVersionRef, str | None]] = []
    required = set(_required_component_keys_from_bundle(bundle))
    trusted_timeframes = dict(bundle.timeframes or {})
    trusted_timeframes.setdefault("regime", str(s.regime_timeframe))
    trusted_timeframes.setdefault("swing", str(s.swing_timeframe))
    trusted_timeframes.setdefault("intraday", str(s.intraday_timeframe))
    for component_key, raw_ref in dict(bundle.components or {}).items():
        ref = (
            ModelVersionRef(**raw_ref.to_dict())
            if isinstance(raw_ref, ModelVersionRef)
            else ModelVersionRef(**dict(raw_ref or {}))
        )
        if str(component_key) == "portfolio_rl":
            refreshed[str(component_key)] = ref
            continue
        if not ref.model_name or not ref.model_version:
            if str(component_key) in required:
                raise RuntimeError(f"incomplete_registered_version:{component_key}")
            refreshed[str(component_key)] = ref
            continue
        current = client.get_model_version(
            name=str(ref.model_name),
            version=str(ref.model_version),
        )
        bound_ref = _bind_ref_to_registered_version(
            ref=ref,
            component_key=str(component_key),
            alias=str(alias),
            current=current,
            bundle_run_id=str(bundle.bundle_run_id),
            trusted_pair=str(bundle.pair),
            trusted_timeframe=_timeframe_for_component(
                str(component_key),
                timeframes=trusted_timeframes,
            ),
        )
        try:
            prior = client.get_model_version_by_alias(
                name=str(bound_ref.model_name),
                alias=str(alias),
            )
            prior_version = str(getattr(prior, "version", "") or "") or None
        except Exception:
            prior_version = None
        refreshed[str(component_key)] = bound_ref
        planned_aliases.append((str(component_key), bound_ref, prior_version))

    moved: list[tuple[ModelVersionRef, str | None]] = []
    try:
        for _component_key, ref, prior_version in planned_aliases:
            client.set_registered_model_alias(
                name=str(ref.model_name),
                alias=str(alias),
                version=str(ref.model_version),
            )
            moved.append((ref, prior_version))
    except Exception as exc:
        rollback_errors: list[str] = []
        for ref, prior_version in reversed(moved):
            try:
                if prior_version:
                    client.set_registered_model_alias(
                        name=str(ref.model_name),
                        alias=str(alias),
                        version=str(prior_version),
                    )
                else:
                    client.delete_registered_model_alias(
                        name=str(ref.model_name),
                        alias=str(alias),
                    )
            except Exception as rollback_exc:
                rollback_errors.append(
                    f"{ref.model_name}:{type(rollback_exc).__name__}"
                )
        rollback_detail = (
            f"; rollback_failed:{','.join(rollback_errors)}"
            if rollback_errors
            else ""
        )
        raise RuntimeError(
            f"alias_publication_failed:{alias}:{type(exc).__name__}{rollback_detail}"
        ) from exc

    bundle.components = {
        **dict(bundle.components or {}),
        **refreshed,
    }
    bundle.intended_alias = str(alias)
    sync_bundle_release_package(bundle, target_alias=str(alias))
    return {
        "ok": True,
        "alias": str(alias),
        "bundle_run_id": str(bundle.bundle_run_id),
        "components": sorted(component_key for component_key, _, _ in planned_aliases),
    }


def _bundle_manifest_from_payload(payload: dict[str, Any]) -> BundleManifest:
    components: dict[str, ModelVersionRef] = {}
    timeframes = dict((payload or {}).get("timeframes") or {})
    artifacts = dict((payload or {}).get("artifacts") or {})
    for component_key, raw_ref in artifacts.items():
        if component_key in {"swing", "intraday"}:
            continue
        family = str(COMPONENT_FAMILIES.get(component_key) or component_key)
        ref = normalize_artifact_ref(raw_ref)
        is_file_checkpoint = str(component_key) == "portfolio_rl"
        timeframe = (
            ""
            if is_file_checkpoint
            else _timeframe_for_component(str(component_key), timeframes=timeframes)
        )
        components[str(component_key)] = ModelVersionRef(
            component_key=str(component_key),
            pair=str((payload or {}).get("pair") or "").upper(),
            timeframe=timeframe,
            model_family=family,
            model_name=str(
                ref.get("model_name")
                or (
                    ""
                    if is_file_checkpoint
                    else registered_model_name(
                        family=family,
                        pair=str((payload or {}).get("pair") or ""),
                        timeframe=timeframe,
                    )
                )
            ),
            model_version=str(ref.get("model_version") or ""),
            model_uri=str(ref.get("model_uri") or ""),
            alias=str(ref.get("alias") or ""),
            run_id=str((payload or {}).get("mlflow", {}).get("component_runs", {}).get(component_key, "")),
            bundle_run_id=str((payload or {}).get("bundle_run_id") or ""),
            dataset_fingerprint=str((payload or {}).get("dataset_fingerprint") or ""),
            path=str(ref.get("path") or ""),
            artifact_hash=str(ref.get("artifact_hash") or ""),
            content_sha256=str(ref.get("content_sha256") or ""),
            runtime_compatible=bool(ref.get("runtime_compatible", True)),
            evidence_refs={str(k): str(v) for k, v in dict(ref.get("evidence_refs") or {}).items()},
        )
    bundle = BundleManifest(
        bundle_run_id=str((payload or {}).get("bundle_run_id") or (payload or {}).get("run_id") or ""),
        pair=str((payload or {}).get("pair") or "").upper(),
        tier=str((payload or {}).get("tier") or ""),
        dataset_fingerprint=str((payload or {}).get("dataset_fingerprint") or ""),
        feature_service_version=str((payload or {}).get("feature_service_version") or ""),
        label_version=str((payload or {}).get("label_version") or ""),
        risk_config_version=str((payload or {}).get("risk_config_version") or ""),
        promotion_status=str((payload or {}).get("promotion_status") or ""),
        intended_alias=str((payload or {}).get("intended_alias") or ""),
        training_window_summary=dict((payload or {}).get("training_window_summary") or {}),
        feature_schema=dict((payload or {}).get("feature_schema") or {}),
        policies={str(k): str(v) for k, v in dict((payload or {}).get("policies") or {}).items()},
        capabilities=dict((payload or {}).get("capabilities") or {}),
        lifecycle_complete=bool((payload or {}).get("lifecycle_complete", False)),
        training_config=dict((payload or {}).get("training_config") or {}),
        promotion_components={str(k): str(v) for k, v in dict((payload or {}).get("promotion_components") or {}).items()},
        training_eval_reports={str(k): str(v) for k, v in dict((payload or {}).get("training_eval_reports") or {}).items()},
        deep_stale=dict((payload or {}).get("deep_stale") or {}),
        new_rows_since_champion=dict((payload or {}).get("new_rows_since_champion") or {}),
        new_lifecycle_events_since_champion=dict((payload or {}).get("new_lifecycle_events_since_champion") or {}),
        drift_flags=dict((payload or {}).get("drift_flags") or {}),
        live_shadow_summary=dict((payload or {}).get("live_shadow_summary") or {}),
        timeframes={str(k): str(v) for k, v in timeframes.items()},
        components=components,
        release_status=str((payload or {}).get("release_status") or ""),
        mlflow=dict((payload or {}).get("mlflow") or {}),
        metadata={
            **_bundle_metadata_from_payload(payload),
            "release_status": str((payload or {}).get("release_status") or ""),
            "rollback_target": dict((payload or {}).get("rollback_target") or {}),
            "operator_signoff": dict((payload or {}).get("operator_signoff") or {}),
            "canary_plan": dict((payload or {}).get("canary_plan") or {}),
            "promotion_gates": list((payload or {}).get("promotion_gates") or []),
            "release_notes": list((payload or {}).get("release_notes") or []),
            "activation_package": dict((payload or {}).get("activation_package") or {}),
            "runtime_compatible": bool((payload or {}).get("runtime_compatible", True)),
            "phase3_execution_required": bool((payload or {}).get("phase3_execution_required", False)),
            "phase3_evidence": dict((payload or {}).get("phase3_evidence") or {}),
            "phase5_gates": dict((payload or {}).get("phase5_gates") or {}),
            "feature_repo_manifest": str((payload or {}).get("feature_repo_manifest") or ""),
            "feature_repo_compaction": dict((payload or {}).get("feature_repo_compaction") or {}),
        },
    )
    sync_bundle_release_package(bundle, target_alias=str(bundle.intended_alias or ""))
    return bundle


def _fill_missing_timeframes(bundle: BundleManifest) -> None:
    s = get_settings()
    bundle.timeframes.setdefault("regime", str(s.regime_timeframe))
    bundle.timeframes.setdefault("swing", str(s.swing_timeframe))
    bundle.timeframes.setdefault("intraday", str(s.intraday_timeframe))
    for component_key, raw_ref in dict(bundle.components or {}).items():
        ref = raw_ref if isinstance(raw_ref, ModelVersionRef) else ModelVersionRef(**dict(raw_ref or {}))
        if str(component_key) == "portfolio_rl":
            bundle.components[str(component_key)] = ref
            continue
        if not ref.timeframe:
            ref.timeframe = _timeframe_for_component(component_key, timeframes=bundle.timeframes)
        if not ref.model_name:
            ref.model_name = registered_model_name(
                family=str(COMPONENT_FAMILIES.get(component_key) or component_key),
                pair=bundle.pair,
                timeframe=ref.timeframe,
            )
        bundle.components[str(component_key)] = ref


def import_compat_bundle_to_mlflow(payload: dict[str, Any], intended_alias: str = "") -> BundleManifest:
    s = get_settings()
    bundle = _bundle_manifest_from_payload(payload)
    _fill_missing_timeframes(bundle)
    if not bundle.bundle_run_id:
        bundle.bundle_run_id = str((payload or {}).get("run_id") or hash_mapping({"pair": bundle.pair, "payload": payload})[:24])
    if not bundle.dataset_fingerprint:
        bundle.dataset_fingerprint = str(hash_mapping({"bundle_run_id": bundle.bundle_run_id, "pair": bundle.pair, "payload": payload}))
    if not bundle.feature_service_version:
        bundle.feature_service_version = str(bundle.dataset_fingerprint)[:16]
    if not bundle.label_version:
        bundle.label_version = str(bundle.dataset_fingerprint)[16:32]
    if not bundle.risk_config_version:
        bundle.risk_config_version = str(hash_mapping(dict(bundle.training_config or {})))[:16]
    sync_bundle_release_package(bundle, target_alias=str(intended_alias or bundle.intended_alias or ""))

    lineage = LineageSnapshot(
        dataset_fingerprint=str(bundle.dataset_fingerprint),
        raw_bars_hash="",
        feature_set_hash=str(bundle.feature_service_version),
        label_config_hash=str(bundle.label_version),
        risk_config_hash=str(bundle.risk_config_version),
        training_config_hash=str(hash_mapping(dict(bundle.training_config or {}))),
        feature_service_version=str(bundle.feature_service_version),
        label_version=str(bundle.label_version),
        risk_config_version=str(bundle.risk_config_version),
        git_sha=str((bundle.metadata or {}).get("git_sha") or ""),
        git_dirty=bool((bundle.metadata or {}).get("git_dirty", False)),
        pair=str(bundle.pair).upper(),
        timeframes=dict(bundle.timeframes or {}),
        feature_schema=dict(bundle.feature_schema or {}),
        training_config=dict(bundle.training_config or {}),
    )

    if not bool(s.mlflow_enabled):
        return bundle

    with tempfile.TemporaryDirectory(prefix=f"fxstack_backfill_{bundle.pair.lower()}_") as tmp_dir:
        temp_root = Path(tmp_dir)
        feature_schema_path = temp_root / "feature_schema.json"
        feature_schema_path.write_text(json.dumps(bundle.feature_schema, indent=2, sort_keys=True), encoding="utf-8")
        lineage_path = temp_root / "lineage.json"
        lineage_path.write_text(json.dumps(lineage.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        backtest_summary_raw = str((payload or {}).get("backtest_summary") or "").strip()
        backtest_summary_path = Path(backtest_summary_raw).expanduser() if backtest_summary_raw else (temp_root / "backtest_summary.json")
        if not backtest_summary_raw or not backtest_summary_path.exists() or backtest_summary_path.is_dir():
            backtest_summary_path = temp_root / "backtest_summary.json"
            backtest_summary_path.write_text(
                json.dumps(
                    {
                        "pair": bundle.pair,
                        "bundle_run_id": bundle.bundle_run_id,
                        "promotion_status": bundle.promotion_status,
                        "summary_kind": "phase1_backfill_backtest_summary",
                    },
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

        component_runs: dict[str, str] = {}
        refreshed: dict[str, ModelVersionRef] = {}
        for component_key, raw_ref in dict(bundle.components or {}).items():
            ref = raw_ref if isinstance(raw_ref, ModelVersionRef) else ModelVersionRef(**dict(raw_ref or {}))
            if str(component_key) == "portfolio_rl":
                refreshed[str(component_key)] = ref
                continue
            if not str(ref.artifact_hash or "").strip():
                raise ValueError(
                    f"artifact_registry_hash_missing:import:{component_key}"
                )
            artifact_path = resolve_model_artifact_path(ref.to_dict())
            window_summary = dict((bundle.training_window_summary or {}).get(component_key) or {})
            model_family = str(COMPONENT_FAMILIES.get(component_key) or component_key)
            release_package = sync_bundle_release_package(bundle, target_alias=str(intended_alias or bundle.intended_alias or ""))
            training_window = (
                f"{str(window_summary.get('start_ts') or '')}->{str(window_summary.get('end_ts') or '')}"
                if window_summary
                else ""
            )
            train_end = str(window_summary.get("end_ts") or (bundle.metadata or {}).get("data_window_end") or "backfill").replace(":", "-")
            with MlflowRunContext(
                experiment_name=experiment_name_for_component(family=model_family, pair=bundle.pair, timeframe=ref.timeframe),
                run_name=f"{model_family}/{bundle.pair}/{ref.timeframe}/{train_end}",
                tags=build_standard_run_tags(
                    git_sha=str(lineage.git_sha),
                    experiment_family=model_family,
                    pair=bundle.pair,
                    timeframe=ref.timeframe,
                    training_window=training_window,
                    validation_window="backfill_import",
                    feature_service_version=str(bundle.feature_service_version),
                    label_version=str(bundle.label_version),
                    risk_config_version=str(bundle.risk_config_version),
                    model_family=model_family,
                    activation_candidate=str(bundle.promotion_status),
                    bundle_run_id=str(bundle.bundle_run_id),
                    extra={"fxstack.backfill_import": "1", "fxstack.dataset_fingerprint": str(bundle.dataset_fingerprint)},
                ),
                lineage=lineage,
                enabled=True,
            ) as run:
                run.log_params(
                    {
                        "pair": bundle.pair,
                        "timeframe": ref.timeframe,
                        "bundle_run_id": bundle.bundle_run_id,
                        "dataset_fingerprint": bundle.dataset_fingerprint,
                        "promotion_status": bundle.promotion_status,
                    }
                )
                meta_path = artifact_path / "meta.json"
                if meta_path.exists():
                    run.log_artifact(meta_path, artifact_path="evidence")
                report_dir = artifact_path / "reports"
                if report_dir.exists():
                    run.log_artifacts(report_dir, artifact_path="evidence/reports")
                run.log_artifact(backtest_summary_path, artifact_path="evidence")
                run.log_artifact(feature_schema_path)
                run.log_artifact(lineage_path)
                new_ref = register_component_version(
                    run=run,
                    component_key=component_key,
                    pair=bundle.pair,
                    timeframe=ref.timeframe,
                    artifact_path=artifact_path,
                    lineage=lineage,
                    bundle_run_id=bundle.bundle_run_id,
                    intended_alias=str(intended_alias or bundle.intended_alias or ""),
                    runtime_compatible=bool(ref.runtime_compatible),
                    evidence_refs={
                        "artifact_path": str(artifact_path),
                        "meta": str(meta_path),
                        "training_report": str(report_dir / "training_report.json"),
                        "promotion_decision": str(report_dir / "promotion_decision.json"),
                        "feature_schema": str(feature_schema_path),
                        "lineage": str(lineage_path),
                        "backtest_summary": str(backtest_summary_path),
                        **_artifact_reporting_evidence_refs(artifact_path),
                    },
                    extra_tags={
                        "fxstack.backfill_import": "1",
                        "fxstack.release_status": str(release_package.release_status or ""),
                        "fxstack.rollback_target": (
                            str(release_package.rollback_target.target_bundle_run_id or "")
                            if release_package.rollback_target is not None
                            else ""
                        ),
                        "fxstack.operator_signoff": "1" if bool(release_package.operator_signoff) else "0",
                    },
                )
                refreshed[str(component_key)] = new_ref
                if run.run_id:
                    component_runs[str(component_key)] = str(run.run_id)

        bundle.components = refreshed
        bundle.intended_alias = str(intended_alias or bundle.intended_alias or "")
        sync_bundle_release_package(bundle, target_alias=str(bundle.intended_alias or ""))
        bundle.mlflow = {
            "enabled": True,
            "tracking_uri": str(s.mlflow_tracking_uri),
            "registry_uri": str(s.mlflow_registry_uri or s.mlflow_tracking_uri),
            "component_runs": component_runs,
            "component_versions": {key: ref.to_dict() for key, ref in refreshed.items()},
            "backfill_import": True,
        }
        manifest_path = temp_root / "bundle_manifest.json"
        package = sync_bundle_release_package(bundle, target_alias=str(bundle.intended_alias or ""), model_manifest=str(manifest_path))
        bundle.metadata = {
            **dict(bundle.metadata or {}),
            **release_metadata_payload(package),
            "model_manifest": str(manifest_path),
        }
        for ref in bundle.components.values():
            ref.evidence_refs["model_manifest"] = str(manifest_path)
        manifest_path.write_text(json.dumps(bundle.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        client = configure_mlflow().tracking.MlflowClient()
        for run_id in component_runs.values():
            try:
                client.log_artifact(run_id, str(manifest_path))
            except Exception:
                continue
        if bundle.intended_alias:
            set_bundle_alias(bundle=bundle, alias=bundle.intended_alias)
    return bundle


def resolve_bundle_manifest_by_alias(
    *,
    pair: str,
    alias: str,
    timeframes: dict[str, str] | None = None,
) -> BundleManifest:
    s = get_settings()
    if not bool(s.mlflow_enabled):
        raise RuntimeError("MLflow alias resolution requires FXSTACK_MLFLOW_ENABLED=1")
    tf = dict(timeframes or {})
    tf.setdefault("regime", str(s.regime_timeframe))
    tf.setdefault("swing", str(s.swing_timeframe))
    tf.setdefault("intraday", str(s.intraday_timeframe))
    client = configure_mlflow().tracking.MlflowClient()
    anchor_names = [
        registered_model_name(family=COMPONENT_FAMILIES["meta"], pair=pair, timeframe=tf["intraday"]),
        registered_model_name(family=COMPONENT_FAMILIES["intraday_xgb"], pair=pair, timeframe=tf["intraday"]),
        registered_model_name(family=COMPONENT_FAMILIES["regime"], pair=pair, timeframe=tf["regime"]),
    ]

    seed = None
    for model_name in anchor_names:
        try:
            seed = client.get_model_version_by_alias(name=model_name, alias=str(alias))
        except Exception:
            continue
        if seed is not None:
            break
    if seed is None:
        raise RuntimeError(f"No MLflow model alias '{alias}' was found for {str(pair).upper()}")

    payload = load_bundle_manifest_from_run(str(seed.run_id))
    bundle = BundleManifest.from_dict(payload)
    if not bundle.bundle_run_id:
        raise RuntimeError(f"Bundle manifest for alias '{alias}' is missing bundle_run_id")
    if str(bundle.pair).upper() != str(pair).upper():
        raise RuntimeError(
            f"Bundle manifest pair mismatch for alias '{alias}':"
            f" expected {str(pair).upper()}, got {str(bundle.pair).upper() or '<missing>'}"
        )

    mismatches: list[str] = []
    required = set(_required_component_keys_from_bundle(bundle))
    refreshed_components: dict[str, ModelVersionRef] = {}
    for component_key, raw_ref in dict(bundle.components or {}).items():
        ref = raw_ref if isinstance(raw_ref, ModelVersionRef) else ModelVersionRef(**dict(raw_ref or {}))
        if not ref.model_name:
            if str(component_key) in required and str(component_key) != "portfolio_rl":
                mismatches.append(f"missing_model_name:{component_key}")
            refreshed_components[str(component_key)] = ref
            continue
        try:
            current = client.get_model_version_by_alias(name=str(ref.model_name), alias=str(alias))
            current = client.get_model_version(name=str(ref.model_name), version=str(current.version))
        except Exception:
            if str(component_key) in required:
                mismatches.append(f"missing_alias:{component_key}")
            refreshed_components[str(component_key)] = ref
            continue
        try:
            refreshed_components[str(component_key)] = _bind_ref_to_registered_version(
                ref=ref,
                component_key=str(component_key),
                alias=str(alias),
                current=current,
                bundle_run_id=str(bundle.bundle_run_id),
                trusted_pair=str(pair).upper(),
                trusted_timeframe=_timeframe_for_component(
                    str(component_key),
                    timeframes=tf,
                ),
            )
        except Exception as exc:
            mismatches.append(f"{component_key}:{exc}")
            refreshed_components[str(component_key)] = ref
    if mismatches:
        raise RuntimeError(
            f"MLflow alias '{alias}' for {str(pair).upper()} is inconsistent across components: {','.join(sorted(mismatches))}"
        )
    bundle.components = refreshed_components
    bundle.intended_alias = str(alias)
    package = sync_bundle_release_package(bundle, target_alias=str(alias))
    bundle.metadata = {
        **dict(bundle.metadata or {}),
        **release_metadata_payload(package),
    }
    bundle.mlflow.setdefault("tracking_uri", str(s.mlflow_tracking_uri))
    bundle.mlflow.setdefault("registry_uri", str(s.mlflow_registry_uri or s.mlflow_tracking_uri))
    return bundle


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _latest_shadow_registry_files(shadow_root: Path) -> dict[str, Path]:
    out: dict[str, tuple[float, Path]] = {}
    for path in shadow_root.glob("registry_full_*/*.json"):
        payload = _load_json(path)
        pair = str(payload.get("pair") or "").strip().upper()
        if not pair:
            continue
        try:
            ts = max(float(path.stat().st_mtime), float(payload.get("trained_at", 0.0) or 0.0))
        except Exception:
            ts = 0.0
        prev = out.get(pair)
        if prev is None or ts >= prev[0]:
            out[pair] = (ts, path)
    return {pair: item[1] for pair, item in out.items()}


def _materialize_registry_payload_from_manifest_row(pair: str, row: dict[str, Any]) -> dict[str, Any]:
    payload = dict((row or {}).get("metadata") or {})
    payload.setdefault("pair", str(pair).upper())
    payload.setdefault("run_id", str((row or {}).get("model_set_id") or f"{str(pair).lower()}-active"))
    payload.setdefault("artifacts", dict((row or {}).get("artifacts") or {}))
    payload.setdefault("policies", dict((row or {}).get("policies") or {}))
    return payload


def backfill_current_state_to_mlflow(
    *,
    active_manifest_path: Path,
    registry_root: Path,
    shadow_root: Path,
    import_fn: Any,
) -> dict[str, Any]:
    from fxstack.training.activation import load_manifest, parse_registry_entry

    manifest = load_manifest(active_manifest_path)
    active_rows = dict(manifest.get("active_model_sets") or {})
    active_imports: list[str] = []
    shadow_imports: list[str] = []

    for pair, row in active_rows.items():
        registry_path = Path(str((row or {}).get("registry_path") or "")).expanduser()
        if registry_path.exists():
            payload = parse_registry_entry(registry_path)
            import_fn(payload["metadata"], intended_alias="champion")
        else:
            payload = _materialize_registry_payload_from_manifest_row(pair, dict(row or {}))
            import_fn(payload, intended_alias="champion")
        active_imports.append(str(pair).upper())

    if shadow_root.exists():
        for pair, path in _latest_shadow_registry_files(shadow_root).items():
            payload = parse_registry_entry(path)
            import_fn(payload["metadata"], intended_alias="shadow")
            shadow_imports.append(str(pair).upper())

    return {
        "ok": True,
        "active_pairs": sorted(active_imports),
        "shadow_pairs": sorted(shadow_imports),
        "registry_root": str(registry_root),
        "shadow_root": str(shadow_root),
    }
