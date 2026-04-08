from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _list_payload(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


@dataclass(slots=True)
class ReleaseNote:
    title: str = ""
    summary: str = ""
    category: str = ""
    author: str = ""
    created_at: float = 0.0
    references: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "ReleaseNote":
        raw = _dict_payload(payload)
        return cls(
            title=str(raw.get("title") or ""),
            summary=str(raw.get("summary") or ""),
            category=str(raw.get("category") or ""),
            author=str(raw.get("author") or ""),
            created_at=float(raw.get("created_at", 0.0) or 0.0),
            references=[str(item) for item in _list_payload(raw.get("references")) if str(item).strip()],
            metadata=_dict_payload(raw.get("metadata")),
        )


@dataclass(slots=True)
class PromotionGateResult:
    gate_id: str = ""
    status: str = ""
    passed: bool | None = None
    required: bool = True
    reason: str = ""
    evaluated_at: float = 0.0
    evidence_refs: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "PromotionGateResult":
        raw = _dict_payload(payload)
        passed_value = raw.get("passed")
        passed = bool(passed_value) if isinstance(passed_value, bool) else None
        return cls(
            gate_id=str(raw.get("gate_id") or raw.get("name") or ""),
            status=str(raw.get("status") or ""),
            passed=passed,
            required=bool(raw.get("required", True)),
            reason=str(raw.get("reason") or ""),
            evaluated_at=float(raw.get("evaluated_at", 0.0) or 0.0),
            evidence_refs={
                str(key): str(value)
                for key, value in _dict_payload(raw.get("evidence_refs")).items()
                if str(key).strip() and value not in (None, "")
            },
            metrics=_dict_payload(raw.get("metrics")),
            metadata=_dict_payload(raw.get("metadata")),
        )


@dataclass(slots=True)
class CanaryPlan:
    plan_id: str = ""
    scope: str = ""
    status: str = ""
    traffic_fraction: float = 0.0
    duration_minutes: int = 0
    metrics_window_minutes: int = 0
    success_criteria: dict[str, Any] = field(default_factory=dict)
    abort_conditions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "CanaryPlan":
        raw = _dict_payload(payload)
        return cls(
            plan_id=str(raw.get("plan_id") or raw.get("name") or ""),
            scope=str(raw.get("scope") or ""),
            status=str(raw.get("status") or ""),
            traffic_fraction=float(raw.get("traffic_fraction", 0.0) or 0.0),
            duration_minutes=int(raw.get("duration_minutes", 0) or 0),
            metrics_window_minutes=int(raw.get("metrics_window_minutes", 0) or 0),
            success_criteria=_dict_payload(raw.get("success_criteria")),
            abort_conditions=[str(item) for item in _list_payload(raw.get("abort_conditions")) if str(item).strip()],
            metadata=_dict_payload(raw.get("metadata")),
        )


@dataclass(slots=True)
class RollbackPlan:
    target_bundle_run_id: str = ""
    target_alias: str = ""
    target_registry_path: str = ""
    strategy: str = "alias_reassignment"
    reason: str = ""
    trigger_conditions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "RollbackPlan":
        raw = _dict_payload(payload)
        return cls(
            target_bundle_run_id=str(raw.get("target_bundle_run_id") or raw.get("bundle_run_id") or ""),
            target_alias=str(raw.get("target_alias") or raw.get("alias") or ""),
            target_registry_path=str(raw.get("target_registry_path") or raw.get("registry_path") or ""),
            strategy=str(raw.get("strategy") or "alias_reassignment"),
            reason=str(raw.get("reason") or ""),
            trigger_conditions=[str(item) for item in _list_payload(raw.get("trigger_conditions")) if str(item).strip()],
            metadata=_dict_payload(raw.get("metadata")),
        )


@dataclass(slots=True)
class ActivationPackage:
    schema_version: str = "phase5_activation_package_v1"
    bundle_run_id: str = ""
    pair: str = ""
    target_alias: str = ""
    model_uri: str = ""
    model_alias: str = ""
    release_status: str = ""
    promotion_status: str = ""
    runtime_compatible: bool = True
    runtime_compatibility: str = ""
    dataset_fingerprint: str = ""
    feature_service_name: str = ""
    feature_service_version: str = ""
    feature_service_hash: str = ""
    feature_schema_hash: str = ""
    label_version: str = ""
    risk_config_version: str = ""
    risk_profile_id: str = ""
    training_window: dict[str, Any] = field(default_factory=dict)
    validation_summary_uri: str = ""
    backtest_summary_uri: str = ""
    calibrator_uri: str = ""
    hardware_profile: str = ""
    observation_window: dict[str, Any] = field(default_factory=dict)
    signed_off_by: list[str] = field(default_factory=list)
    rollback_target: RollbackPlan | None = None
    operator_signoff: dict[str, Any] = field(default_factory=dict)
    canary_plan: CanaryPlan | None = None
    promotion_gates: list[PromotionGateResult] = field(default_factory=list)
    release_notes: list[ReleaseNote] = field(default_factory=list)
    evidence_refs: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Any) -> "ActivationPackage":
        raw = _dict_payload(payload)
        rollback_target = _dict_payload(raw.get("rollback_target"))
        canary_plan = _dict_payload(raw.get("canary_plan"))
        promotion_gates = _list_payload(raw.get("promotion_gates"))
        release_notes = _list_payload(raw.get("release_notes"))
        return cls(
            schema_version=str(raw.get("schema_version") or "phase5_activation_package_v1"),
            bundle_run_id=str(raw.get("bundle_run_id") or ""),
            pair=str(raw.get("pair") or "").upper(),
            target_alias=str(raw.get("target_alias") or raw.get("model_alias") or ""),
            model_uri=str(raw.get("model_uri") or ""),
            model_alias=str(raw.get("model_alias") or raw.get("target_alias") or ""),
            release_status=str(raw.get("release_status") or ""),
            promotion_status=str(raw.get("promotion_status") or ""),
            runtime_compatible=bool(raw.get("runtime_compatible", True)),
            runtime_compatibility=str(raw.get("runtime_compatibility") or ""),
            dataset_fingerprint=str(raw.get("dataset_fingerprint") or ""),
            feature_service_name=str(raw.get("feature_service_name") or ""),
            feature_service_version=str(raw.get("feature_service_version") or ""),
            feature_service_hash=str(raw.get("feature_service_hash") or ""),
            feature_schema_hash=str(raw.get("feature_schema_hash") or ""),
            label_version=str(raw.get("label_version") or ""),
            risk_config_version=str(raw.get("risk_config_version") or ""),
            risk_profile_id=str(raw.get("risk_profile_id") or ""),
            training_window=_dict_payload(raw.get("training_window")),
            validation_summary_uri=str(raw.get("validation_summary_uri") or ""),
            backtest_summary_uri=str(raw.get("backtest_summary_uri") or ""),
            calibrator_uri=str(raw.get("calibrator_uri") or ""),
            hardware_profile=str(raw.get("hardware_profile") or ""),
            observation_window=_dict_payload(raw.get("observation_window")),
            signed_off_by=[str(item) for item in _list_payload(raw.get("signed_off_by")) if str(item).strip()],
            rollback_target=RollbackPlan.from_dict(rollback_target) if rollback_target else None,
            operator_signoff=_dict_payload(raw.get("operator_signoff")),
            canary_plan=CanaryPlan.from_dict(canary_plan) if canary_plan else None,
            promotion_gates=[PromotionGateResult.from_dict(item) for item in promotion_gates],
            release_notes=[ReleaseNote.from_dict(item) for item in release_notes],
            evidence_refs={
                str(key): str(value)
                for key, value in _dict_payload(raw.get("evidence_refs")).items()
                if str(key).strip() and value not in (None, "")
            },
            metadata=_dict_payload(raw.get("metadata")),
        )


@dataclass(slots=True)
class LineageSnapshot:
    dataset_fingerprint: str
    raw_bars_hash: str
    feature_set_hash: str
    label_config_hash: str
    risk_config_hash: str
    training_config_hash: str
    feature_service_version: str
    label_version: str
    risk_config_version: str
    git_sha: str
    git_dirty: bool
    pair: str = ""
    raw_inputs: list[str] = field(default_factory=list)
    feature_inputs: list[str] = field(default_factory=list)
    label_inputs: list[str] = field(default_factory=list)
    timeframes: dict[str, str] = field(default_factory=dict)
    feature_schema: dict[str, Any] = field(default_factory=dict)
    label_config: dict[str, Any] = field(default_factory=dict)
    risk_config: dict[str, Any] = field(default_factory=dict)
    training_config: dict[str, Any] = field(default_factory=dict)
    git_branch: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModelVersionRef:
    component_key: str
    pair: str
    timeframe: str
    model_family: str
    model_name: str = ""
    model_version: str = ""
    model_uri: str = ""
    alias: str = ""
    run_id: str = ""
    bundle_run_id: str = ""
    dataset_fingerprint: str = ""
    path: str = ""
    artifact_hash: str = ""
    runtime_compatible: bool = True
    evidence_refs: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BundleManifest:
    bundle_run_id: str
    pair: str
    tier: str
    dataset_fingerprint: str
    feature_service_version: str
    label_version: str
    risk_config_version: str
    promotion_status: str
    intended_alias: str = ""
    training_window_summary: dict[str, Any] = field(default_factory=dict)
    feature_schema: dict[str, Any] = field(default_factory=dict)
    policies: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    lifecycle_complete: bool = False
    training_config: dict[str, Any] = field(default_factory=dict)
    promotion_components: dict[str, str] = field(default_factory=dict)
    training_eval_reports: dict[str, str] = field(default_factory=dict)
    deep_stale: dict[str, Any] = field(default_factory=dict)
    new_rows_since_champion: dict[str, Any] = field(default_factory=dict)
    new_lifecycle_events_since_champion: dict[str, Any] = field(default_factory=dict)
    drift_flags: dict[str, Any] = field(default_factory=dict)
    live_shadow_summary: dict[str, Any] = field(default_factory=dict)
    timeframes: dict[str, str] = field(default_factory=dict)
    components: dict[str, ModelVersionRef] = field(default_factory=dict)
    release_status: str = ""
    rollback_target: RollbackPlan | None = None
    operator_signoff: dict[str, Any] = field(default_factory=dict)
    canary_plan: CanaryPlan | None = None
    promotion_gates: list[PromotionGateResult] = field(default_factory=list)
    release_notes: list[ReleaseNote] = field(default_factory=list)
    activation_package: ActivationPackage | None = None
    mlflow: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["components"] = {
            key: value.to_dict() if isinstance(value, ModelVersionRef) else dict(value or {})
            for key, value in dict(self.components or {}).items()
        }
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "BundleManifest":
        metadata = dict((payload or {}).get("metadata") or {})
        activation_package_raw = (payload or {}).get("activation_package")
        if not activation_package_raw:
            activation_package_raw = metadata.get("activation_package")
        activation_package = ActivationPackage.from_dict(activation_package_raw) if activation_package_raw else None
        release_status = str(
            (payload or {}).get("release_status")
            or metadata.get("release_status")
            or (activation_package.release_status if activation_package is not None else "")
            or ""
        )
        rollback_target_raw = (payload or {}).get("rollback_target")
        if not rollback_target_raw:
            rollback_target_raw = metadata.get("rollback_target")
        if not rollback_target_raw and activation_package is not None and activation_package.rollback_target is not None:
            rollback_target_raw = activation_package.rollback_target.to_dict()
        canary_plan_raw = (payload or {}).get("canary_plan")
        if not canary_plan_raw:
            canary_plan_raw = metadata.get("canary_plan")
        if not canary_plan_raw and activation_package is not None and activation_package.canary_plan is not None:
            canary_plan_raw = activation_package.canary_plan.to_dict()
        promotion_gates_raw = (payload or {}).get("promotion_gates")
        if not promotion_gates_raw:
            promotion_gates_raw = metadata.get("promotion_gates")
        if not promotion_gates_raw and activation_package is not None:
            promotion_gates_raw = [item.to_dict() for item in activation_package.promotion_gates]
        release_notes_raw = (payload or {}).get("release_notes")
        if not release_notes_raw:
            release_notes_raw = metadata.get("release_notes")
        if not release_notes_raw and activation_package is not None:
            release_notes_raw = [item.to_dict() for item in activation_package.release_notes]
        operator_signoff = _dict_payload((payload or {}).get("operator_signoff"))
        if not operator_signoff:
            operator_signoff = _dict_payload(metadata.get("operator_signoff"))
        if not operator_signoff and activation_package is not None:
            operator_signoff = dict(activation_package.operator_signoff or {})
        components = {
            str(key): (
                value
                if isinstance(value, ModelVersionRef)
                else ModelVersionRef(**dict(value or {}))
            )
            for key, value in dict((payload or {}).get("components") or {}).items()
        }
        return cls(
            bundle_run_id=str((payload or {}).get("bundle_run_id") or ""),
            pair=str((payload or {}).get("pair") or ""),
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
            timeframes={str(k): str(v) for k, v in dict((payload or {}).get("timeframes") or {}).items()},
            components=components,
            release_status=release_status,
            rollback_target=RollbackPlan.from_dict(rollback_target_raw) if rollback_target_raw else None,
            operator_signoff=operator_signoff,
            canary_plan=CanaryPlan.from_dict(canary_plan_raw) if canary_plan_raw else None,
            promotion_gates=[PromotionGateResult.from_dict(item) for item in _list_payload(promotion_gates_raw)],
            release_notes=[ReleaseNote.from_dict(item) for item in _list_payload(release_notes_raw)],
            activation_package=activation_package,
            mlflow=dict((payload or {}).get("mlflow") or {}),
            metadata=metadata,
        )
