from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


EVIDENCE_IDENTITY_SCHEMA = "phase5_release_evidence_identity_v1"
SUPPORT_BINDING_SCHEMA = "phase5_support_evidence_binding_v1"
SHADOW_EVIDENCE_SCHEMA = "fxstack_shadow_runtime_evidence_v2"
ROLLBACK_EVIDENCE_SCHEMA = "fxstack_rollback_drill_evidence_v1"
RELEASE_VALIDATION_BUNDLE_SCHEMA = "phase5_release_validation_bundle_v1"
FAST_SHADOW_MIN_DURATION_SECS = 15 * 60
PHASE5_SHADOW_MIN_DURATION_SECS = 24 * 60 * 60
PHASE5_MAX_DRAWDOWN_PCT = 25.0
_SHA256_HEX_LEN = 64


EvidenceKind = Literal["economic_validation", "runtime_shadow", "rollback_validation"]
FIXED_PHASE5_SUPPORT_EVIDENCE = frozenset(
    {
        "feature_schema",
        "lineage",
        "execution_metrics",
        "risk_trace_schema",
        "stress_harness_summary",
        "support_evidence_binding",
    }
)


def file_sha256(path: str | Path) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == _SHA256_HEX_LEN and all(char in "0123456789abcdef" for char in text)


def _as_float(value: Any, *, default: float = float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)


def _as_int(value: Any, *, default: int = 0) -> int:
    numeric = _as_float(value)
    return int(numeric) if math.isfinite(numeric) else int(default)


def _as_exact_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def mapping_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(dict(value or {}), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_artifact_projection(
    artifacts: dict[str, Any] | None,
    *,
    expected_pair: str = "",
) -> dict[str, dict[str, Any]]:
    """Project candidate and activated artifact shapes onto immutable component identity."""

    raw_artifacts = dict(artifacts or {})
    aliases_to_skip = {
        "swing" if "swing_xgb" in raw_artifacts else "",
        "intraday" if "intraday_xgb" in raw_artifacts else "",
    }
    projection: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in sorted(raw_artifacts.items()):
        key = str(raw_key).strip()
        if not key or key in aliases_to_skip:
            continue
        ref = dict(raw_value or {}) if isinstance(raw_value, dict) else {"path": str(raw_value or "")}
        component_key = str(ref.get("component_key") or key).strip()
        if component_key in {"swing", "intraday"} and f"{component_key}_xgb" in raw_artifacts:
            component_key = f"{component_key}_xgb"
        digest = str(
            ref.get("artifact_hash")
            or ref.get("content_sha256")
            or ref.get("payload_sha256")
            or ref.get("sha256")
            or ""
        ).strip().lower()
        canonical = {
            "component_key": component_key,
            "pair": str(ref.get("pair") or expected_pair or "").strip().upper(),
            "timeframe": str(ref.get("timeframe") or "").strip().upper(),
            "model_family": str(ref.get("model_family") or "").strip(),
            "model_name": str(ref.get("model_name") or "").strip(),
            "model_version": str(ref.get("model_version") or "").strip(),
            "run_id": str(ref.get("run_id") or "").strip(),
            "bundle_run_id": str(ref.get("bundle_run_id") or "").strip(),
            "dataset_fingerprint": str(ref.get("dataset_fingerprint") or "").strip(),
            "artifact_digest": digest,
            "runtime_compatible": bool(ref.get("runtime_compatible", True)),
        }
        existing = projection.get(component_key)
        if existing is not None and existing != canonical:
            projection[f"__conflicting_alias__:{key}"] = canonical
        else:
            projection[component_key] = canonical
    return projection


def artifact_set_sha256(
    artifacts: dict[str, Any] | None,
    *,
    expected_pair: str = "",
) -> str:
    projection = canonical_artifact_projection(artifacts, expected_pair=expected_pair)
    expected_pair_key = str(expected_pair or "").strip().upper()
    if any(
        str(key).startswith("__conflicting_alias__:")
        or not str(ref.get("component_key") or "").strip()
        or not str(ref.get("pair") or "").strip()
        or (expected_pair_key and str(ref.get("pair") or "").strip().upper() != expected_pair_key)
        or not is_sha256(ref.get("artifact_digest"))
        for key, ref in projection.items()
    ):
        return ""
    return mapping_sha256(projection) if projection else ""


def canonical_model_identity_sha256(
    *,
    pair: str,
    bundle_run_id: str,
    model_set_id: str,
    artifacts: dict[str, Any] | None,
) -> str:
    artifact_digest = artifact_set_sha256(artifacts, expected_pair=pair)
    if not artifact_digest:
        return ""
    return mapping_sha256(
        {
            "pair": str(pair).strip().upper(),
            "bundle_run_id": str(bundle_run_id).strip(),
            "model_set_id": str(model_set_id).strip(),
            "artifact_set_sha256": artifact_digest,
        }
    )


def economic_sufficiency(payload: dict[str, Any] | None) -> tuple[bool, dict[str, float | int]]:
    """Evaluate the immutable economic artifact with the Phase-5 thresholds."""

    report = dict(payload or {})
    stress = dict(report.get("stress_summary") or {})
    realized = _as_float(report.get("net_pnl_usd", report.get("realized_pnl_usd", 0.0)) or 0.0)
    drawdown = _as_float(report.get("max_drawdown_pct", 0.0) or 0.0)
    turnover = _as_float(report.get("turnover_lots", 0.0) or 0.0)
    trade_count = _as_int(report.get("trade_count", 0) or 0)
    worst_stress_pnl = _as_float(stress.get("worst_realized_pnl_usd", realized))
    worst_stress_drawdown = _as_float(stress.get("worst_drawdown_pct", drawdown))
    scenario_count = _as_int(stress.get("scenario_count", 0) or 0)
    finite_metrics = all(
        math.isfinite(value)
        for value in (realized, drawdown, turnover, worst_stress_pnl, worst_stress_drawdown)
    )
    passed = bool(
        finite_metrics
        and realized > 0.0
        and 0.0 <= drawdown < PHASE5_MAX_DRAWDOWN_PCT
        and trade_count > 0
        and turnover > 0.0
        and scenario_count > 0
        and worst_stress_pnl > -50.0
        and 0.0 <= worst_stress_drawdown < PHASE5_MAX_DRAWDOWN_PCT
    )
    return passed, {
        "realized_pnl_usd": realized,
        "max_drawdown_pct": drawdown,
        "turnover_lots": turnover,
        "trade_count": trade_count,
        "worst_stress_pnl": worst_stress_pnl,
        "worst_stress_drawdown_pct": worst_stress_drawdown,
        "scenario_count": scenario_count,
    }


def read_json_object(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class ReleaseEvidenceIdentity:
    pair: str
    bundle_run_id: str
    model_set_id: str
    model_manifest_sha256: str
    artifact_set_sha256: str
    evidence_kind: EvidenceKind
    source_kind: str
    advisory_only: bool
    schema_version: str = EVIDENCE_IDENTITY_SCHEMA

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ReleaseEvidenceIdentity":
        payload = dict(raw or {})
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            pair=str(payload.get("pair") or "").strip().upper(),
            bundle_run_id=str(payload.get("bundle_run_id") or "").strip(),
            model_set_id=str(payload.get("model_set_id") or "").strip(),
            model_manifest_sha256=str(payload.get("model_manifest_sha256") or "").strip().lower(),
            artifact_set_sha256=str(payload.get("artifact_set_sha256") or "").strip().lower(),
            evidence_kind=str(payload.get("evidence_kind") or ""),  # type: ignore[arg-type]
            source_kind=str(payload.get("source_kind") or "").strip().lower(),
            advisory_only=bool(payload.get("advisory_only", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def errors(
        self,
        *,
        expected_pair: str,
        expected_bundle_run_id: str,
        expected_model_set_id: str,
        expected_model_manifest_sha256: str = "",
        expected_artifact_set_sha256: str = "",
        expected_kind: EvidenceKind,
    ) -> list[str]:
        errors: list[str] = []
        if self.schema_version != EVIDENCE_IDENTITY_SCHEMA:
            errors.append("identity_schema_invalid")
        if self.pair != str(expected_pair).strip().upper():
            errors.append("identity_pair_mismatch")
        if not self.bundle_run_id or self.bundle_run_id != str(expected_bundle_run_id).strip():
            errors.append("identity_bundle_run_id_mismatch")
        if not self.model_set_id or self.model_set_id != str(expected_model_set_id).strip():
            errors.append("identity_model_set_id_mismatch")
        if self.evidence_kind != expected_kind:
            errors.append("identity_evidence_kind_mismatch")
        if not is_sha256(self.model_manifest_sha256):
            errors.append("identity_manifest_sha256_invalid")
        expected_sha = str(expected_model_manifest_sha256 or "").strip().lower()
        if expected_sha and self.model_manifest_sha256 != expected_sha:
            errors.append("identity_manifest_sha256_mismatch")
        if not is_sha256(self.artifact_set_sha256):
            errors.append("identity_artifact_set_sha256_invalid")
        expected_artifact_sha = str(expected_artifact_set_sha256 or "").strip().lower()
        if expected_artifact_sha and self.artifact_set_sha256 != expected_artifact_sha:
            errors.append("identity_artifact_set_sha256_mismatch")
        if not self.source_kind:
            errors.append("identity_source_kind_missing")
        return errors


@dataclass(frozen=True, slots=True)
class EvidenceValidation:
    valid: bool
    errors: tuple[str, ...]
    artifact_sha256: str
    identity: ReleaseEvidenceIdentity | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": bool(self.valid),
            "errors": list(self.errors),
            "artifact_sha256": str(self.artifact_sha256),
            "identity": self.identity.to_dict() if self.identity is not None else {},
        }


@dataclass(frozen=True, slots=True)
class Phase5BundleValidation:
    valid: bool
    errors: tuple[str, ...]
    gate_errors: dict[str, tuple[str, ...]]
    gate_passes: dict[str, bool]
    model_manifest_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": bool(self.valid),
            "errors": list(self.errors),
            "gate_errors": {key: list(value) for key, value in self.gate_errors.items()},
            "gate_passes": {key: bool(value) for key, value in self.gate_passes.items()},
            "model_manifest_sha256": str(self.model_manifest_sha256),
        }


def _validate_artifact_identity(
    *,
    path: str | Path | None,
    expected_pair: str,
    expected_bundle_run_id: str,
    expected_model_set_id: str,
    expected_model_manifest_sha256: str,
    expected_artifact_set_sha256: str,
    expected_kind: EvidenceKind,
) -> tuple[dict[str, Any], ReleaseEvidenceIdentity | None, list[str], str]:
    source = Path(str(path or "").strip())
    if not str(path or "").strip() or not source.is_file():
        return {}, None, ["evidence_artifact_missing"], ""
    payload = read_json_object(source)
    if not payload:
        return {}, None, ["evidence_artifact_invalid_json"], file_sha256(source)
    identity = ReleaseEvidenceIdentity.from_dict(dict(payload.get("evidence_identity") or {}))
    errors = identity.errors(
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
        expected_kind=expected_kind,
    )
    return payload, identity, errors, file_sha256(source)


def validate_economic_evidence(
    *,
    path: str | Path | None,
    expected_pair: str,
    expected_bundle_run_id: str,
    expected_model_set_id: str,
    expected_model_manifest_sha256: str,
    expected_artifact_set_sha256: str,
) -> EvidenceValidation:
    payload, identity, errors, artifact_sha = _validate_artifact_identity(
        path=path,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
        expected_kind="economic_validation",
    )
    if identity is not None:
        if identity.advisory_only:
            errors.append("economic_evidence_advisory_only")
        if any(token in identity.source_kind for token in ("causal_research", "offline_research", "research_only")):
            errors.append("offline_research_cannot_authorize_activation")
        if identity.source_kind != "independent_execution_harness":
            errors.append("economic_source_not_authoritative")
        else:
            sources = dict(payload.get("source_artifacts") or {})
            source_model_manifest = str(sources.get("model_manifest") or "").strip()
            source_harness_manifest = str(sources.get("harness_manifest") or "").strip()
            source_economic_report = str(sources.get("economic_report") or "").strip()
            source_paths = {
                "model_manifest": source_model_manifest,
                "harness_manifest": source_harness_manifest,
                "economic_report": source_economic_report,
            }
            for source_name, source_path_text in source_paths.items():
                source_path = Path(source_path_text) if source_path_text else Path()
                expected_hash = str(sources.get(f"{source_name}_sha256") or "").strip().lower()
                if not source_path_text or not source_path.is_file():
                    errors.append(f"economic_source_artifact_missing:{source_name}")
                elif not is_sha256(expected_hash) or file_sha256(source_path) != expected_hash:
                    errors.append(f"economic_source_artifact_hash_mismatch:{source_name}")
            if all(source_paths.values()):
                source_errors, source_harness, source_report, source_stress = (
                    validate_external_harness_source_chain(
                        model_manifest_path=source_model_manifest,
                        harness_manifest_path=source_harness_manifest,
                        economic_report_path=source_economic_report,
                        expected_pair=expected_pair,
                        expected_bundle_run_id=expected_bundle_run_id,
                        expected_model_set_id=expected_model_set_id,
                        expected_model_manifest_sha256=expected_model_manifest_sha256,
                        expected_artifact_set_sha256=expected_artifact_set_sha256,
                    )
                )
                errors.extend(source_errors)
                declared_source_stress = {
                    str(name): dict(value or {})
                    for name, value in dict(sources.get("stress_reports") or {}).items()
                }
                harness_stress_paths = {
                    str(name): Path(str(value)).resolve()
                    for name, value in dict(
                        dict(source_harness.get("artifacts") or {}).get("stress_reports") or {}
                    ).items()
                }
                if set(declared_source_stress) != set(source_stress):
                    errors.append("economic_source_stress_set_mismatch")
                for scenario in source_stress.keys():
                    declared = declared_source_stress.get(scenario, {})
                    declared_path_text = str(declared.get("path") or "").strip()
                    declared_path = Path(declared_path_text).resolve() if declared_path_text else Path()
                    expected_path = harness_stress_paths.get(scenario)
                    expected_hash = str(declared.get("sha256") or "").strip().lower()
                    if not declared_path_text or expected_path is None or declared_path != expected_path:
                        errors.append(f"economic_source_stress_path_mismatch:{scenario}")
                    elif not declared_path.is_file() or file_sha256(declared_path) != expected_hash:
                        errors.append(f"economic_source_stress_hash_mismatch:{scenario}")

                for key in (
                    "engine",
                    "pair",
                    "realized_pnl_usd",
                    "unrealized_pnl_usd",
                    "turnover_lots",
                    "max_drawdown_pct",
                    "trade_count",
                    "partial_fill_count",
                    "latency_ms_p95",
                    "rejection_rate",
                ):
                    if str(payload.get(key) or "") != str(source_report.get(key) or ""):
                        errors.append(f"economic_normalized_report_mismatch:{key}")

                normalized_stress = dict(payload.get("stress_summary") or {})
                normalized_scenarios = {
                    str(dict(dict(item or {}).get("metadata") or {}).get("scenario") or ""): dict(item or {})
                    for item in list(normalized_stress.get("scenarios") or [])
                    if isinstance(item, dict)
                }
                if set(normalized_scenarios) != set(source_stress):
                    errors.append("economic_normalized_stress_set_mismatch")
                for scenario, source_scenario in source_stress.items():
                    normalized = normalized_scenarios.get(scenario, {})
                    for key in (
                        "realized_pnl_usd",
                        "unrealized_pnl_usd",
                        "turnover_lots",
                        "max_drawdown_pct",
                        "trade_count",
                        "partial_fill_count",
                        "latency_ms_p95",
                        "rejection_rate",
                    ):
                        if str(normalized.get(key) or "") != str(source_scenario.get(key) or ""):
                            errors.append(f"economic_normalized_stress_mismatch:{scenario}:{key}")
                if source_stress:
                    expected_worst_pnl = min(
                        [_as_float(source_report.get("realized_pnl_usd"))]
                        + [_as_float(item.get("realized_pnl_usd")) for item in source_stress.values()]
                    )
                    expected_worst_drawdown = max(
                        [_as_float(source_report.get("max_drawdown_pct"))]
                        + [_as_float(item.get("max_drawdown_pct")) for item in source_stress.values()]
                    )
                    if _as_int(normalized_stress.get("scenario_count")) != len(source_stress):
                        errors.append("economic_normalized_stress_count_mismatch")
                    if _as_float(normalized_stress.get("worst_realized_pnl_usd")) != expected_worst_pnl:
                        errors.append("economic_normalized_worst_pnl_mismatch")
                    if _as_float(normalized_stress.get("worst_drawdown_pct")) != expected_worst_drawdown:
                        errors.append("economic_normalized_worst_drawdown_mismatch")
    status = str(payload.get("status") or payload.get("evaluation_status") or "").strip().lower()
    if status not in {"complete", "passed"}:
        errors.append("economic_evidence_not_complete")
    stress = dict(payload.get("stress_summary") or {})
    if str(stress.get("status") or "").strip().lower() not in {"ok", "complete", "passed"}:
        errors.append("economic_stress_evidence_missing")
    if int(stress.get("scenario_count", 0) or 0) <= 0:
        errors.append("economic_stress_scenarios_missing")
    return EvidenceValidation(
        valid=not errors,
        errors=tuple(dict.fromkeys(errors)),
        artifact_sha256=artifact_sha,
        identity=identity,
    )


def validate_shadow_runtime_evidence(
    *,
    path: str | Path | None,
    expected_pair: str,
    expected_bundle_run_id: str,
    expected_model_set_id: str,
    expected_model_manifest_sha256: str = "",
    expected_artifact_set_sha256: str = "",
    minimum_duration_secs: float = PHASE5_SHADOW_MIN_DURATION_SECS,
) -> EvidenceValidation:
    payload, identity, errors, artifact_sha = _validate_artifact_identity(
        path=path,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
        expected_kind="runtime_shadow",
    )
    if str(payload.get("schema_version") or "") != SHADOW_EVIDENCE_SCHEMA:
        errors.append("shadow_evidence_schema_invalid")
    producer = dict(payload.get("producer") or {})
    if (
        str(producer.get("tool") or "") != "tools.shadow_dual_run"
        or str(producer.get("version") or "") != "v2"
    ):
        errors.append("shadow_producer_contract_invalid")
    try:
        started_at = float(payload.get("started_at"))
        ended_at = float(payload.get("ended_at"))
    except (TypeError, ValueError):
        started_at = 0.0
        ended_at = 0.0
    duration_secs = ended_at - started_at
    if (
        not math.isfinite(started_at)
        or not math.isfinite(ended_at)
        or started_at <= 0.0
        or ended_at <= started_at
    ):
        errors.append("shadow_run_window_invalid")
    elif duration_secs < max(0.0, float(minimum_duration_secs)):
        errors.append("shadow_run_duration_too_short")
    if identity is not None:
        if identity.advisory_only:
            errors.append("shadow_evidence_advisory_only")
        if identity.source_kind != "production_runtime_shadow":
            errors.append("shadow_source_not_runtime")
    gates = dict(payload.get("gates") or {})
    if gates.get("passed") is not True:
        errors.append("shadow_runtime_gate_failed")
    boundary = dict(payload.get("runtime_boundary") or {})
    if boundary.get("broker_emission_disabled") is not True:
        errors.append("shadow_broker_emission_not_disabled")
    if _as_int(boundary.get("entry_commands_emitted", -1), default=-1) != 0:
        errors.append("shadow_entry_commands_emitted")
    if _as_int(boundary.get("control_commands_emitted", -1), default=-1) != 0:
        errors.append("shadow_control_commands_emitted")
    if _as_int(boundary.get("total_commands_emitted", -1), default=-1) != 0:
        errors.append("shadow_broker_commands_emitted")
    command_window = dict(boundary.get("command_window_summary") or {})
    if str(command_window.get("schema_version") or "") != "fxstack_command_window_summary_v1":
        errors.append("shadow_command_window_schema_invalid")
    if command_window.get("window_complete") is not True:
        errors.append("shadow_command_window_incomplete")
    command_start = _as_float(command_window.get("start_ts"))
    command_end = _as_float(command_window.get("end_ts"))
    command_queried = _as_float(command_window.get("queried_at"))
    if (
        not math.isfinite(command_start)
        or not math.isfinite(command_end)
        or abs(command_start - started_at) > 1e-6
        or abs(command_end - ended_at) > 1e-6
        or not math.isfinite(command_queried)
        or command_queried < command_end
    ):
        errors.append("shadow_command_window_bounds_invalid")
    command_total = _as_int(command_window.get("total_commands"), default=-1)
    command_entries = _as_int(command_window.get("entry_commands"), default=-1)
    command_controls = _as_int(command_window.get("control_commands"), default=-1)
    status_counts = dict(command_window.get("status_counts") or {})
    command_counts = dict(command_window.get("command_counts") or {})
    if (
        command_total < 0
        or command_entries < 0
        or command_controls < 0
        or command_total != command_entries + command_controls
        or sum(_as_int(value, default=-1) for value in status_counts.values()) != command_total
        or sum(_as_int(value, default=-1) for value in command_counts.values()) != command_total
    ):
        errors.append("shadow_command_window_counts_invalid")
    if command_total != 0:
        errors.append("shadow_command_window_not_isolated")
    if str(boundary.get("agent_mode") or "").strip().lower() == "live":
        errors.append("shadow_agent_mode_live")
    if boundary.get("active_manifest_matches_db") is not True:
        errors.append("shadow_manifest_db_identity_unproven")
    if boundary.get("runtime_loaded_matches_db") is not True:
        errors.append("shadow_runtime_db_identity_unproven")
    if boundary.get("activation_identity_consistent") is not True:
        errors.append("shadow_runtime_artifact_identity_unproven")
    lifecycle = dict(boundary.get("startup_lifecycle") or {})
    if lifecycle.get("startup_inference_ok") is not True:
        errors.append("shadow_startup_inference_not_ready")
    if str(lifecycle.get("model_set_id") or "").strip() != str(expected_model_set_id).strip():
        errors.append("shadow_startup_model_set_mismatch")
    if str(lifecycle.get("pair_readiness_status") or "").strip().lower() != "ready":
        errors.append("shadow_pair_readiness_not_ready")
    if lifecycle.get("has_exit_model") is not True:
        errors.append("shadow_exit_model_not_loaded")
    if lifecycle.get("has_reversal_models") is not True:
        errors.append("shadow_reversal_models_not_loaded")
    if str(lifecycle.get("lifecycle_activation_mode") or "").strip().lower() != "model_driven":
        errors.append("shadow_lifecycle_not_model_driven")
    if lifecycle.get("lifecycle_ready") is not True:
        errors.append("shadow_lifecycle_not_ready")
    candidate = dict(payload.get("candidate") or {})
    candidate_samples = _as_int(candidate.get("samples", 0) or 0)
    if candidate_samples <= 0:
        errors.append("shadow_samples_missing")
    if candidate.get("runtime_ready_seen") is not True:
        errors.append("shadow_runtime_not_ready")
    if candidate.get("feature_ready_seen") is not True:
        errors.append("shadow_features_not_ready")
    coverage = dict(payload.get("observation_coverage") or {})
    poll_interval_secs = _as_float(coverage.get("poll_interval_secs"))
    poll_attempts = _as_int(coverage.get("poll_attempts"), default=0)
    successful_samples = _as_int(coverage.get("successful_samples"), default=0)
    successful_ratio = _as_float(coverage.get("successful_sample_ratio"))
    runtime_ready_ratio = _as_float(coverage.get("runtime_ready_sample_ratio"))
    feature_ready_ratio = _as_float(coverage.get("feature_ready_sample_ratio"))
    first_sample_at = _as_float(coverage.get("first_sample_at"))
    last_sample_at = _as_float(coverage.get("last_sample_at"))
    observed_span_secs = _as_float(coverage.get("observed_span_secs"))
    max_sample_gap_secs = _as_float(coverage.get("max_sample_gap_secs"))
    if (
        not math.isfinite(poll_interval_secs)
        or poll_interval_secs <= 0.0
        or poll_interval_secs > 60.0
    ):
        errors.append("shadow_poll_interval_invalid")
    else:
        expected_attempts = max(2, int(duration_secs / poll_interval_secs))
        if poll_attempts < max(2, int(expected_attempts * 0.95)):
            errors.append("shadow_poll_attempt_coverage_insufficient")
        tolerance_secs = max(60.0, poll_interval_secs * 2.0)
        if (
            not math.isfinite(first_sample_at)
            or not math.isfinite(last_sample_at)
            or first_sample_at < started_at - tolerance_secs
            or first_sample_at > started_at + tolerance_secs
            or last_sample_at < ended_at - tolerance_secs
            or last_sample_at > ended_at + tolerance_secs
        ):
            errors.append("shadow_sample_window_coverage_invalid")
        if (
            not math.isfinite(observed_span_secs)
            or observed_span_secs < max(0.0, duration_secs - (2.0 * tolerance_secs))
        ):
            errors.append("shadow_observed_span_insufficient")
        if (
            not math.isfinite(max_sample_gap_secs)
            or max_sample_gap_secs < 0.0
            or max_sample_gap_secs > max(60.0, poll_interval_secs * 3.0)
        ):
            errors.append("shadow_sample_gap_excessive")
    if poll_attempts < 2:
        errors.append("shadow_poll_attempts_missing")
    if successful_samples != candidate_samples:
        errors.append("shadow_successful_sample_count_mismatch")
    for name, value in (
        ("successful", successful_ratio),
        ("runtime_ready", runtime_ready_ratio),
        ("feature_ready", feature_ready_ratio),
    ):
        if not math.isfinite(value) or value < 0.95 or value > 1.0:
            errors.append(f"shadow_{name}_coverage_insufficient")
    if not str(coverage.get("runtime_boot_id") or "").strip():
        errors.append("shadow_runtime_boot_id_missing")
    if coverage.get("continuous_boot") is not True:
        errors.append("shadow_runtime_boot_not_continuous")
    raw_samples_value = payload.get("candidate_samples")
    raw_samples = (
        [dict(item) for item in raw_samples_value if isinstance(item, dict)]
        if isinstance(raw_samples_value, list)
        else []
    )
    if not isinstance(raw_samples_value, list) or len(raw_samples) != len(raw_samples_value):
        errors.append("shadow_raw_samples_invalid")
    if len(raw_samples) != candidate_samples or len(raw_samples) != successful_samples:
        errors.append("shadow_raw_sample_count_mismatch")
    if raw_samples:
        raw_timestamps = [_as_float(item.get("ts")) for item in raw_samples]
        if (
            any(not math.isfinite(value) for value in raw_timestamps)
            or raw_timestamps != sorted(raw_timestamps)
            or any(value < started_at or value > ended_at + max(5.0, poll_interval_secs) for value in raw_timestamps)
        ):
            errors.append("shadow_raw_sample_timestamps_invalid")
        raw_boot_ids = [str(item.get("runtime_boot_id") or "").strip() for item in raw_samples]
        raw_ready_ratio = sum(item.get("runtime_ready") is True for item in raw_samples) / len(raw_samples)
        raw_feature_ratio = sum(item.get("feature_ready") is True for item in raw_samples) / len(raw_samples)
        raw_first = min(raw_timestamps)
        raw_last = max(raw_timestamps)
        raw_max_gap = max(
            [
                raw_timestamps[index] - raw_timestamps[index - 1]
                for index in range(1, len(raw_timestamps))
            ]
            or [0.0]
        )

        def _same_float(left: Any, right: float) -> bool:
            observed = _as_float(left)
            return math.isfinite(observed) and abs(observed - right) <= 1e-9

        if not _same_float(coverage.get("runtime_ready_sample_ratio"), raw_ready_ratio):
            errors.append("shadow_raw_runtime_ready_ratio_mismatch")
        if not _same_float(coverage.get("feature_ready_sample_ratio"), raw_feature_ratio):
            errors.append("shadow_raw_feature_ready_ratio_mismatch")
        if not _same_float(coverage.get("first_sample_at"), raw_first):
            errors.append("shadow_raw_first_sample_mismatch")
        if not _same_float(coverage.get("last_sample_at"), raw_last):
            errors.append("shadow_raw_last_sample_mismatch")
        if not _same_float(coverage.get("observed_span_secs"), raw_last - raw_first):
            errors.append("shadow_raw_observed_span_mismatch")
        if not _same_float(coverage.get("max_sample_gap_secs"), raw_max_gap):
            errors.append("shadow_raw_max_gap_mismatch")
        if not raw_boot_ids or any(not item for item in raw_boot_ids) or len(set(raw_boot_ids)) != 1:
            errors.append("shadow_raw_boot_continuity_failed")
        elif raw_boot_ids[0] != str(coverage.get("runtime_boot_id") or ""):
            errors.append("shadow_raw_boot_id_mismatch")
        if candidate.get("runtime_ready_seen") is not any(
            item.get("runtime_ready") is True for item in raw_samples
        ):
            errors.append("shadow_raw_runtime_ready_summary_mismatch")
        if candidate.get("feature_ready_seen") is not any(
            item.get("feature_ready") is True for item in raw_samples
        ):
            errors.append("shadow_raw_feature_ready_summary_mismatch")
        raw_hard_breach = any(
            _as_float(item.get("drawdown_pct"), default=0.0)
            >= max(_as_float(item.get("hard_dd_pct"), default=0.0), 1e-9)
            for item in raw_samples
        )
        raw_daily_breaker = any(item.get("daily_breaker_active") is True for item in raw_samples)
        raw_operability = bool(
            len(raw_samples) > 1
            and successful_ratio >= 0.95
            and raw_ready_ratio >= 0.95
            and raw_feature_ratio >= 0.95
            and len(set(raw_boot_ids)) == 1
            and bool(raw_boot_ids[0])
        )
        gate_checks = dict(gates.get("checks") or {})
        if gate_checks.get("risk") is not bool(not raw_hard_breach and not raw_daily_breaker):
            errors.append("shadow_raw_risk_gate_mismatch")
        if gate_checks.get("operability") is not raw_operability:
            errors.append("shadow_raw_operability_gate_mismatch")
    return EvidenceValidation(
        valid=not errors,
        errors=tuple(dict.fromkeys(errors)),
        artifact_sha256=artifact_sha,
        identity=identity,
    )


def validate_rollback_evidence(
    *,
    path: str | Path | None,
    expected_pair: str,
    expected_bundle_run_id: str,
    expected_model_set_id: str,
    expected_model_manifest_sha256: str,
    expected_artifact_set_sha256: str,
) -> EvidenceValidation:
    payload, identity, errors, artifact_sha = _validate_artifact_identity(
        path=path,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
        expected_kind="rollback_validation",
    )
    if str(payload.get("schema_version") or "") != ROLLBACK_EVIDENCE_SCHEMA:
        errors.append("rollback_evidence_schema_invalid")
    if str(payload.get("status") or "").strip().lower() != "passed":
        errors.append("rollback_drill_not_passed")
    tested_at = _as_float(payload.get("tested_at"))
    if not math.isfinite(tested_at) or tested_at <= 0.0:
        errors.append("rollback_tested_at_invalid")
    if identity is not None:
        if identity.advisory_only:
            errors.append("rollback_evidence_advisory_only")
        if identity.source_kind != "production_rollback_drill":
            errors.append("rollback_source_not_production_drill")
    drill = dict(payload.get("drill") or {})
    if drill.get("executed") is not True:
        errors.append("rollback_drill_execution_unproven")
    if _as_int(drill.get("return_code"), default=-1) != 0:
        errors.append("rollback_drill_return_code_failed")
    if not list(drill.get("command") or []):
        errors.append("rollback_drill_command_missing")
    if drill.get("runtime_disabled_during_drill") is not True:
        errors.append("rollback_drill_runtime_not_disabled")
    if drill.get("target_activated") is not True:
        errors.append("rollback_target_activation_unproven")
    if drill.get("candidate_restored") is not True:
        errors.append("rollback_candidate_restore_unproven")
    if str(drill.get("candidate_bundle_run_id") or "") != str(expected_bundle_run_id):
        errors.append("rollback_candidate_bundle_mismatch")
    return EvidenceValidation(
        valid=not errors,
        errors=tuple(dict.fromkeys(errors)),
        artifact_sha256=artifact_sha,
        identity=identity,
    )


def validate_blockers_artifact(
    path: str | Path | None,
) -> tuple[list[str], list[dict[str, Any]], str]:
    source_text = str(path or "").strip()
    source = Path(source_text) if source_text else Path()
    if not source_text or not source.is_file():
        return ["blockers_artifact_missing"], [], ""
    payload = read_json_object(source)
    errors: list[str] = []
    if not payload:
        errors.append("blockers_artifact_invalid_json")
    if payload.get("schema_version") not in {1, "1", "full_process_blockers_v1"}:
        errors.append("blockers_schema_invalid")
    if not str(payload.get("generated_at") or "").strip():
        errors.append("blockers_generated_at_missing")
    rows = payload.get("blockers")
    if not isinstance(rows, list):
        errors.append("blockers_list_invalid")
        rows = []
    open_critical_high: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            errors.append(f"blocker_row_invalid:{index}")
            continue
        row = dict(raw)
        severity = str(row.get("severity") or "").strip().lower()
        status = str(row.get("status") or "").strip().lower()
        if not str(row.get("id") or "").strip() or severity not in {
            "critical",
            "high",
            "medium",
            "low",
            "info",
        }:
            errors.append(f"blocker_row_contract_invalid:{index}")
        if severity in {"critical", "high"} and status not in {"closed", "resolved", "done"}:
            open_critical_high.append(row)
    if open_critical_high:
        errors.append("open_critical_high_blockers")
    return list(dict.fromkeys(errors)), open_critical_high, file_sha256(source)


def validate_release_validation_bundle(
    *,
    path: str | Path | None,
    expected_pair: str,
    expected_bundle_run_id: str,
    expected_model_set_id: str,
    expected_model_manifest_sha256: str,
    expected_artifact_set_sha256: str,
) -> EvidenceValidation:
    source_text = str(path or "").strip()
    source = Path(source_text) if source_text else Path()
    if not source_text or not source.is_file():
        return EvidenceValidation(False, ("release_validation_bundle_missing",), "", None)
    payload = read_json_object(source)
    artifact_sha = file_sha256(source)
    expected_authority_name = f"release_validation_bundle_{artifact_sha}.json"
    if source.name != expected_authority_name:
        return EvidenceValidation(
            False,
            ("release_validation_bundle_not_content_addressed",),
            artifact_sha,
            None,
        )
    try:
        attributes = int(getattr(source.lstat(), "st_file_attributes", 0) or 0)
    except FileNotFoundError:
        attributes = 0
    if source.is_symlink() or attributes & 0x400:
        return EvidenceValidation(
            False,
            ("release_validation_bundle_link_or_reparse_invalid",),
            artifact_sha,
            None,
        )
    identity = ReleaseEvidenceIdentity.from_dict(dict(payload.get("evidence_identity") or {}))
    errors = identity.errors(
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
        expected_kind="runtime_shadow",
    )
    if identity.source_kind != "production_release_finalization":
        errors.append("release_validation_source_invalid")
    if identity.advisory_only:
        errors.append("release_validation_advisory_only")
    if str(payload.get("schema_version") or "") != RELEASE_VALIDATION_BUNDLE_SCHEMA:
        errors.append("release_validation_schema_invalid")
    if payload.get("valid") is not True or str(payload.get("status") or "").lower() != "passed":
        errors.append("release_validation_not_passed")

    artifacts = dict(payload.get("artifacts") or {})

    def _artifact_ref(name: str) -> tuple[Path | None, str]:
        ref = dict(artifacts.get(name) or {})
        ref_path_text = str(ref.get("path") or "").strip()
        ref_sha = str(ref.get("sha256") or "").strip().lower()
        ref_path = Path(ref_path_text) if ref_path_text else None
        if ref_path is None or not ref_path.is_file():
            errors.append(f"release_validation_artifact_missing:{name}")
        elif not is_sha256(ref_sha) or file_sha256(ref_path) != ref_sha:
            errors.append(f"release_validation_artifact_hash_mismatch:{name}")
        return ref_path, ref_sha

    model_manifest, _model_sha = _artifact_ref("model_manifest")
    fast_shadow, _fast_sha = _artifact_ref("fast_shadow")
    long_shadow, _long_sha = _artifact_ref("long_shadow")
    rollback_path, _rollback_sha = _artifact_ref("rollback_evidence")
    blockers_path, _blockers_sha = _artifact_ref("blockers")
    if model_manifest is not None and model_manifest.is_file():
        active = active_manifest_identity(manifest_path=model_manifest, pair=expected_pair)
        if active.model_manifest_sha256 != str(expected_model_manifest_sha256).strip().lower():
            errors.append("release_validation_model_identity_mismatch")
        if active.artifact_set_sha256 != str(expected_artifact_set_sha256).strip().lower():
            errors.append("release_validation_artifact_identity_mismatch")
        if active.bundle_run_id != str(expected_bundle_run_id) or active.model_set_id != str(expected_model_set_id):
            errors.append("release_validation_active_model_mismatch")

    fast_validation = validate_shadow_runtime_evidence(
        path=fast_shadow,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
        minimum_duration_secs=FAST_SHADOW_MIN_DURATION_SECS,
    )
    long_validation = validate_shadow_runtime_evidence(
        path=long_shadow,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
        minimum_duration_secs=PHASE5_SHADOW_MIN_DURATION_SECS,
    )
    errors.extend(f"fast:{item}" for item in fast_validation.errors)
    errors.extend(f"long:{item}" for item in long_validation.errors)
    rollback_validation = validate_rollback_evidence(
        path=rollback_path,
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=expected_model_set_id,
        expected_model_manifest_sha256=expected_model_manifest_sha256,
        expected_artifact_set_sha256=expected_artifact_set_sha256,
    )
    errors.extend(f"rollback:{item}" for item in rollback_validation.errors)
    blocker_errors, _open_blockers, _blocker_hash = validate_blockers_artifact(blockers_path)
    errors.extend(f"blockers:{item}" for item in blocker_errors)

    fast_payload = read_json_object(fast_shadow) if fast_shadow is not None else {}
    long_payload = read_json_object(long_shadow) if long_shadow is not None else {}
    fast_window = (_as_float(fast_payload.get("started_at")), _as_float(fast_payload.get("ended_at")))
    long_window = (_as_float(long_payload.get("started_at")), _as_float(long_payload.get("ended_at")))
    if fast_shadow is not None and long_shadow is not None and fast_shadow.resolve() == long_shadow.resolve():
        errors.append("release_validation_shadow_paths_duplicate")
    if fast_validation.artifact_sha256 and fast_validation.artifact_sha256 == long_validation.artifact_sha256:
        errors.append("release_validation_shadow_bytes_duplicate")
    if all(math.isfinite(value) for value in (*fast_window, *long_window)):
        if not (fast_window[1] <= long_window[0] or long_window[1] <= fast_window[0]):
            errors.append("release_validation_shadow_windows_overlap")
    else:
        errors.append("release_validation_shadow_windows_invalid")
    return EvidenceValidation(
        valid=not errors,
        errors=tuple(dict.fromkeys(errors)),
        artifact_sha256=artifact_sha,
        identity=identity,
    )


def active_manifest_identity(*, manifest_path: str | Path, pair: str) -> ReleaseEvidenceIdentity:
    path = Path(manifest_path)
    payload = read_json_object(path)
    pair_key = str(pair).strip().upper()
    row = dict(dict(payload.get("active_model_sets") or {}).get(pair_key) or {})
    model_set_id = str(row.get("model_set_id") or "").strip()
    metadata = dict(row.get("metadata") or {})
    bundle_run_id = str(metadata.get("bundle_run_id") or model_set_id).strip()
    artifacts = dict(row.get("artifacts") or {})
    return ReleaseEvidenceIdentity(
        pair=pair_key,
        bundle_run_id=bundle_run_id,
        model_set_id=model_set_id,
        model_manifest_sha256=canonical_model_identity_sha256(
            pair=pair_key,
            bundle_run_id=bundle_run_id,
            model_set_id=model_set_id,
            artifacts=artifacts,
        ),
        artifact_set_sha256=artifact_set_sha256(artifacts, expected_pair=pair_key),
        evidence_kind="runtime_shadow",
        source_kind="production_runtime_shadow",
        advisory_only=False,
    )


def candidate_manifest_identity(
    *,
    manifest_path: str | Path,
    pair: str,
    model_set_id: str = "",
) -> ReleaseEvidenceIdentity:
    path = Path(manifest_path)
    payload = read_json_object(path)
    pair_key = str(pair or payload.get("pair") or "").strip().upper()
    bundle_run_id = str(payload.get("bundle_run_id") or payload.get("run_id") or "").strip()
    resolved_model_set_id = str(model_set_id or payload.get("model_set_id") or bundle_run_id).strip()
    artifacts = dict(payload.get("components") or payload.get("artifacts") or {})
    return ReleaseEvidenceIdentity(
        pair=pair_key,
        bundle_run_id=bundle_run_id,
        model_set_id=resolved_model_set_id,
        model_manifest_sha256=canonical_model_identity_sha256(
            pair=pair_key,
            bundle_run_id=bundle_run_id,
            model_set_id=resolved_model_set_id,
            artifacts=artifacts,
        ),
        artifact_set_sha256=artifact_set_sha256(artifacts, expected_pair=pair_key),
        evidence_kind="runtime_shadow",
        source_kind="candidate_model_manifest",
        advisory_only=False,
    )


def validate_external_harness_source_chain(
    *,
    model_manifest_path: str | Path,
    harness_manifest_path: str | Path,
    economic_report_path: str | Path,
    expected_pair: str,
    expected_bundle_run_id: str,
    expected_model_set_id: str,
    expected_model_manifest_sha256: str,
    expected_artifact_set_sha256: str,
) -> tuple[list[str], dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    """Reopen and validate every byte that claims an external executed result."""

    from fxstack.backtest.harness.contracts import (
        EXTERNAL_ECONOMIC_REPORT_VERSION,
        EXTERNAL_STRESS_REPORT_VERSION,
        PHASE3_HARNESS_MANIFEST_VERSION,
        REQUIRED_EXTERNAL_STRESS_SCENARIOS,
    )

    errors: list[str] = []
    manifest_path = Path(model_manifest_path).resolve()
    harness_path = Path(harness_manifest_path).resolve()
    report_path = Path(economic_report_path).resolve()
    harness = read_json_object(harness_path) if harness_path.is_file() else {}
    report = read_json_object(report_path) if report_path.is_file() else {}
    stress_reports: dict[str, dict[str, Any]] = {}

    if not manifest_path.is_file():
        errors.append("source_model_manifest_missing")
        active_identity = None
    else:
        active_identity = active_manifest_identity(manifest_path=manifest_path, pair=expected_pair)
        if active_identity.model_manifest_sha256 != str(expected_model_manifest_sha256).strip().lower():
            errors.append("source_model_manifest_hash_mismatch")
        if active_identity.pair != str(expected_pair).strip().upper():
            errors.append("source_model_manifest_pair_mismatch")
        if active_identity.bundle_run_id != str(expected_bundle_run_id).strip():
            errors.append("source_model_manifest_bundle_mismatch")
        if active_identity.model_set_id != str(expected_model_set_id).strip():
            errors.append("source_model_manifest_model_set_mismatch")
        if active_identity.artifact_set_sha256 != str(expected_artifact_set_sha256).strip().lower():
            errors.append("source_model_manifest_artifact_set_mismatch")

    if not harness_path.is_file() or not harness:
        errors.append("source_harness_manifest_missing")
    if not report_path.is_file() or not report or report_path.stat().st_size <= 0:
        errors.append("source_economic_report_missing")
    if str(harness.get("manifest_version") or "") != PHASE3_HARNESS_MANIFEST_VERSION:
        errors.append("harness_manifest_schema_invalid")
    if str(harness.get("status") or "").strip().lower() != "completed":
        errors.append("harness_not_completed")
    engine = str(harness.get("engine") or "").strip().lower()
    if engine not in {"lean", "nautilus"}:
        errors.append("harness_not_independent")
    pair = str(harness.get("pair") or "").strip().upper()
    if pair != str(expected_pair).strip().upper():
        errors.append("harness_pair_mismatch")
    dataset_hash = str(harness.get("dataset_hash") or "").strip()
    engine_version = str(harness.get("engine_version") or "").strip()
    if not dataset_hash:
        errors.append("harness_dataset_hash_missing")
    if not engine_version:
        errors.append("harness_engine_version_missing")

    metadata = dict(harness.get("metadata") or {})
    if metadata.get("execute") is not True:
        errors.append("harness_execute_not_proven")
    if metadata.get("fresh_output_dir") is not True:
        errors.append("harness_fresh_output_not_proven")
    if _as_int(metadata.get("process_returncode"), default=-1) != 0:
        errors.append("harness_process_not_successful")
    if bool(metadata.get("advisory_only", False)) or bool(metadata.get("research_only", False)):
        errors.append("advisory_harness_cannot_authorize_activation")

    linkage = {
        "bundle_run_id": str(expected_bundle_run_id).strip(),
        "model_set_id": str(expected_model_set_id).strip(),
        "model_manifest_sha256": str(expected_model_manifest_sha256).strip().lower(),
        "artifact_set_sha256": str(expected_artifact_set_sha256).strip().lower(),
    }
    for field_name, expected_value in linkage.items():
        if str(metadata.get(field_name) or "").strip().lower() != str(expected_value).lower():
            errors.append(f"harness_{field_name}_mismatch")
    if Path(str(metadata.get("model_manifest") or "")).resolve() != manifest_path:
        errors.append("harness_model_manifest_path_mismatch")

    harness_run_id = str(metadata.get("harness_run_id") or "").strip()
    input_bundle_sha = str(metadata.get("input_bundle_sha256") or "").strip().lower()
    input_bundle_files = {
        str(key): str(value).strip().lower()
        for key, value in dict(metadata.get("input_bundle_files") or {}).items()
    }
    if len(harness_run_id) < 16:
        errors.append("harness_run_id_invalid")
    if not is_sha256(input_bundle_sha):
        errors.append("harness_input_bundle_sha256_invalid")
    if not input_bundle_files:
        errors.append("harness_input_bundle_inventory_missing")
    elif any(not is_sha256(value) for value in input_bundle_files.values()):
        errors.append("harness_input_bundle_inventory_hash_invalid")
    if input_bundle_files and mapping_sha256(input_bundle_files) != input_bundle_sha:
        errors.append("harness_input_bundle_inventory_digest_mismatch")

    working_directory_text = str(harness.get("working_directory") or "").strip()
    working_directory = Path(working_directory_text).resolve() if working_directory_text else Path()
    if not working_directory_text or not working_directory.is_dir():
        errors.append("harness_working_directory_missing")
    else:
        for relative, expected_sha in input_bundle_files.items():
            candidate = (working_directory / relative).resolve()
            if not candidate.is_relative_to(working_directory) or not candidate.is_file():
                errors.append(f"harness_input_file_missing:{relative}")
            elif file_sha256(candidate) != expected_sha:
                errors.append(f"harness_input_file_hash_mismatch:{relative}")

    artifacts = dict(harness.get("artifacts") or {})
    output_dir_text = str(artifacts.get("output_dir") or "").strip()
    output_dir = Path(output_dir_text).resolve() if output_dir_text else Path()
    declared_report_text = str(artifacts.get("economic_report") or "").strip()
    declared_report = Path(declared_report_text).resolve() if declared_report_text else Path()
    if not output_dir_text or not output_dir.is_dir():
        errors.append("harness_output_dir_missing")
    if not declared_report_text or declared_report != report_path:
        errors.append("economic_report_path_mismatch")
    elif not report_path.is_relative_to(output_dir):
        errors.append("economic_report_outside_output_dir")

    artifact_hashes = dict(metadata.get("artifact_sha256") or {})
    report_sha = file_sha256(report_path) if report_path.is_file() else ""
    if str(artifact_hashes.get("economic_report") or "").strip().lower() != report_sha:
        errors.append("economic_report_hash_mismatch")
    artifact_stats = dict(metadata.get("artifact_stats") or {})
    report_stats = dict(artifact_stats.get("economic_report") or {})
    started_at_ns = _as_exact_int(metadata.get("process_started_at_ns"), default=0)
    finished_at_ns = _as_exact_int(metadata.get("process_finished_at_ns"), default=0)
    if started_at_ns <= 0 or finished_at_ns < started_at_ns:
        errors.append("harness_process_window_invalid")
    if report_path.is_file():
        stat = report_path.stat()
        if _as_exact_int(report_stats.get("size"), default=-1) != int(stat.st_size):
            errors.append("economic_report_size_mismatch")
        if _as_exact_int(report_stats.get("mtime_ns"), default=-1) != int(stat.st_mtime_ns):
            errors.append("economic_report_mtime_mismatch")
        if int(stat.st_mtime_ns) < started_at_ns or int(stat.st_mtime_ns) > finished_at_ns + 5_000_000_000:
            errors.append("economic_report_not_created_by_run_window")

    expected_report_linkage = {
        "harness_run_id": harness_run_id,
        "engine": engine,
        "engine_version": engine_version,
        "pair": str(expected_pair).strip().upper(),
        "dataset_hash": dataset_hash,
        "input_bundle_sha256": input_bundle_sha,
        **linkage,
    }
    if str(report.get("schema_version") or "") != EXTERNAL_ECONOMIC_REPORT_VERSION:
        errors.append("economic_report_schema_invalid")
    if str(report.get("status") or "").strip().lower() not in {"completed", "passed"}:
        errors.append("economic_report_not_completed")
    for field_name, expected_value in expected_report_linkage.items():
        if str(report.get(field_name) or "") != str(expected_value):
            errors.append(f"economic_report_linkage_mismatch:{field_name}")
    errors.extend(
        f"economic_report_{key}_non_finite"
        for key in ("realized_pnl_usd", "max_drawdown_pct", "turnover_lots", "trade_count")
        if not math.isfinite(_as_float(report.get(key)))
    )

    command = [str(item) for item in list(harness.get("command") or [])]

    def _command_binds(flag: str, value: str) -> bool:
        return any(
            command[index] == flag and command[index + 1] == value
            for index in range(max(0, len(command) - 1))
        )

    command_contract = {
        "--fxstack-economic-report": str(report_path),
        "--fxstack-harness-run-id": harness_run_id,
        "--fxstack-pair": str(expected_pair).strip().upper(),
        "--fxstack-dataset-hash": dataset_hash,
        "--fxstack-engine-version": engine_version,
        "--fxstack-input-bundle-sha256": input_bundle_sha,
        "--fxstack-model-manifest": str(manifest_path),
        "--fxstack-bundle-run-id": linkage["bundle_run_id"],
        "--fxstack-model-set-id": linkage["model_set_id"],
        "--fxstack-model-manifest-sha256": linkage["model_manifest_sha256"],
        "--fxstack-artifact-set-sha256": linkage["artifact_set_sha256"],
    }
    for flag, value in command_contract.items():
        if not _command_binds(flag, value):
            errors.append(f"harness_command_contract_missing:{flag}")

    declared_stress = {
        str(name): Path(str(path)).resolve()
        for name, path in dict(artifacts.get("stress_reports") or {}).items()
        if str(name).strip() and str(path).strip()
    }
    stress_hashes = {
        str(name): str(value).strip().lower()
        for name, value in dict(artifact_hashes.get("stress_reports") or {}).items()
    }
    stress_stats = dict(artifact_stats.get("stress_reports") or {})
    required_scenarios = set(REQUIRED_EXTERNAL_STRESS_SCENARIOS)
    if set(declared_stress) != required_scenarios:
        errors.append("external_stress_report_set_mismatch")
    if set(str(item) for item in list(metadata.get("required_stress_scenarios") or [])) != required_scenarios:
        errors.append("external_stress_contract_mismatch")
    for scenario in REQUIRED_EXTERNAL_STRESS_SCENARIOS:
        stress_path = declared_stress.get(scenario)
        if stress_path is None or not stress_path.is_file() or stress_path.stat().st_size <= 0:
            errors.append(f"external_stress_report_missing:{scenario}")
            continue
        if not stress_path.is_relative_to(output_dir):
            errors.append(f"external_stress_report_outside_output_dir:{scenario}")
        if stress_hashes.get(scenario) != file_sha256(stress_path):
            errors.append(f"external_stress_report_hash_mismatch:{scenario}")
        scenario_stats = dict(stress_stats.get(scenario) or {})
        stat = stress_path.stat()
        if _as_exact_int(scenario_stats.get("size"), default=-1) != int(stat.st_size):
            errors.append(f"external_stress_report_size_mismatch:{scenario}")
        if _as_exact_int(scenario_stats.get("mtime_ns"), default=-1) != int(stat.st_mtime_ns):
            errors.append(f"external_stress_report_mtime_mismatch:{scenario}")
        if int(stat.st_mtime_ns) < started_at_ns or int(stat.st_mtime_ns) > finished_at_ns + 5_000_000_000:
            errors.append(f"external_stress_report_not_created_by_run_window:{scenario}")
        if not _command_binds("--fxstack-stress-report", f"{scenario}={stress_path}"):
            errors.append(f"harness_command_stress_contract_missing:{scenario}")
        stress_payload = read_json_object(stress_path)
        stress_reports[scenario] = stress_payload
        if str(stress_payload.get("schema_version") or "") != EXTERNAL_STRESS_REPORT_VERSION:
            errors.append(f"external_stress_report_schema_invalid:{scenario}")
        if str(stress_payload.get("scenario") or "") != scenario:
            errors.append(f"external_stress_scenario_mismatch:{scenario}")
        if str(stress_payload.get("status") or "").strip().lower() not in {"completed", "passed"}:
            errors.append(f"external_stress_report_not_completed:{scenario}")
        for field_name, expected_value in expected_report_linkage.items():
            if str(stress_payload.get(field_name) or "") != str(expected_value):
                errors.append(f"external_stress_linkage_mismatch:{scenario}:{field_name}")
        errors.extend(
            f"external_stress_{key}_non_finite:{scenario}"
            for key in (
                "realized_pnl_usd",
                "max_drawdown_pct",
                "turnover_lots",
                "trade_count",
            )
            if not math.isfinite(_as_float(stress_payload.get(key)))
        )

    return list(dict.fromkeys(errors)), harness, report, stress_reports


def support_evidence_errors(payload: dict[str, Any] | None) -> list[str]:
    bundle = dict(payload or {})
    refs = dict(bundle.get("evidence_refs") or {})
    hashes = {str(key): str(value).strip().lower() for key, value in dict(bundle.get("evidence_hashes") or {}).items()}
    required = [str(key) for key in list(bundle.get("binding_required_evidence") or []) if str(key).strip()]
    required_set = set(required)
    errors: list[str] = []
    missing_fixed_contract = sorted(FIXED_PHASE5_SUPPORT_EVIDENCE - required_set)
    if missing_fixed_contract:
        errors.append("phase5_support_contract_weakened:" + ",".join(missing_fixed_contract))
    training_keys = sorted(key for key in refs if str(key).startswith("training_eval:"))
    if not training_keys:
        errors.append("phase5_training_evidence_missing")
    keys_to_validate = sorted(required_set | set(FIXED_PHASE5_SUPPORT_EVIDENCE) | set(training_keys))
    for key in keys_to_validate:
        path = Path(str(refs.get(key) or "").strip())
        if not path.is_file():
            errors.append(f"phase5_support_evidence_missing:{key}")
            continue
        actual_sha = file_sha256(path)
        if not is_sha256(hashes.get(key)) or hashes.get(key) != actual_sha:
            errors.append(f"phase5_support_evidence_hash_mismatch:{key}")
        if key != "support_evidence_binding":
            evidence = read_json_object(path)
            if not evidence:
                errors.append(f"phase5_support_evidence_invalid_json:{key}")
            else:
                errors.extend(
                    f"phase5_support_semantic_invalid:{key}:{reason}"
                    for reason in _support_evidence_semantic_errors(
                        key=key,
                        evidence=evidence,
                        expected_pair=str(
                            dict(
                                bundle.get("candidate_evidence_identity")
                                or bundle.get("evidence_identity")
                                or {}
                            ).get("pair")
                            or ""
                        ),
                    )
                )
    binding_path = Path(str(refs.get("support_evidence_binding") or "").strip())
    binding = read_json_object(binding_path) if binding_path.is_file() else {}
    if str(binding.get("schema_version") or "") != SUPPORT_BINDING_SCHEMA:
        errors.append("phase5_support_binding_schema_invalid")
    candidate_identity = dict(bundle.get("candidate_evidence_identity") or bundle.get("evidence_identity") or {})
    bound_identity = dict(binding.get("evidence_identity") or {})
    errors.extend(
        f"phase5_support_binding_identity_mismatch:{field}"
        for field in (
            "schema_version",
            "pair",
            "bundle_run_id",
            "model_set_id",
            "model_manifest_sha256",
            "artifact_set_sha256",
        )
        if str(bound_identity.get(field) or "")
        != str(candidate_identity.get(field) or "")
    )
    declared_artifacts = dict(binding.get("artifacts") or {})
    support_keys = sorted((set(FIXED_PHASE5_SUPPORT_EVIDENCE) - {"support_evidence_binding"}) | set(training_keys))
    if set(declared_artifacts) != set(support_keys):
        errors.append("phase5_support_binding_artifact_set_mismatch")
    for key in support_keys:
        declared = dict(declared_artifacts.get(key) or {})
        if str(declared.get("path") or "") != str(refs.get(key) or ""):
            errors.append(f"phase5_support_binding_path_mismatch:{key}")
        if str(declared.get("sha256") or "").strip().lower() != str(hashes.get(key) or "").strip().lower():
            errors.append(f"phase5_support_binding_hash_mismatch:{key}")
    return list(dict.fromkeys(errors))


def _support_evidence_semantic_errors(
    *,
    key: str,
    evidence: dict[str, Any],
    expected_pair: str,
) -> list[str]:
    errors: list[str] = []
    if key == "feature_schema":
        from fxstack.features.session_contract import feature_contract_metadata

        for field_name, expected in feature_contract_metadata().items():
            if str(evidence.get(field_name) or "") != str(expected):
                errors.append(f"{field_name}_mismatch")
    elif key == "lineage":
        errors.extend(
            f"{field_name}_missing"
            for field_name in (
                "dataset_fingerprint",
                "feature_set_hash",
                "label_config_hash",
                "risk_config_hash",
                "training_config_hash",
                "feature_service_version",
                "label_version",
                "risk_config_version",
            )
            if not str(evidence.get(field_name) or "").strip()
        )
        if str(evidence.get("pair") or "").strip().upper() != str(expected_pair).strip().upper():
            errors.append("pair_mismatch")
    elif key == "execution_metrics":
        if str(evidence.get("status") or "").strip().lower() not in {"completed", "passed", "ok"}:
            errors.append("status_not_completed")
        if str(evidence.get("pair") or "").strip().upper() != str(expected_pair).strip().upper():
            errors.append("pair_mismatch")
        errors.extend(
            f"{field_name}_missing"
            for field_name in (
                "engine",
                "dataset_hash",
                "feature_service_version",
                "kernel_version",
            )
            if not str(evidence.get(field_name) or "").strip()
        )
        errors.extend(
            f"{field_name}_non_finite"
            for field_name in (
                "realized_pnl_usd",
                "trade_count",
                "max_drawdown_pct",
                "turnover_lots",
                "latency_ms_p95",
                "rejection_rate",
            )
            if not math.isfinite(_as_float(evidence.get(field_name)))
        )
    elif key == "risk_trace_schema":
        if str(evidence.get("schema_version") or "") != "phase3_risk_trace_schema_v1":
            errors.append("schema_version_mismatch")
        if not str(evidence.get("kernel_version") or "").strip():
            errors.append("kernel_version_missing")
        rule_order = [str(item).strip() for item in list(evidence.get("rule_order") or [])]
        if len(rule_order) < 4 or len(set(rule_order)) != len(rule_order):
            errors.append("rule_order_invalid")
    elif key == "stress_harness_summary":
        if str(evidence.get("status") or "").strip().lower() not in {"completed", "passed", "ok"}:
            errors.append("status_not_completed")
        scenario_count = _as_int(evidence.get("scenario_count"), default=0)
        scenarios = list(evidence.get("scenarios") or [])
        if scenario_count <= 0 or len(scenarios) != scenario_count:
            errors.append("scenario_contract_invalid")
        errors.extend(
            f"{field_name}_missing"
            for field_name in (
                "dataset_hash",
                "feature_service_version",
                "kernel_version",
            )
            if not str(evidence.get(field_name) or "").strip()
        )
    elif key.startswith("training_eval:"):
        promotion = dict(evidence.get("promotion_decision") or {})
        if str(promotion.get("status") or "").strip().lower() != "eligible":
            errors.append("promotion_not_eligible")
        candidate_metric = _as_float(
            promotion.get("candidate_metric", evidence.get("candidate_metric"))
        )
        if not math.isfinite(candidate_metric):
            errors.append("candidate_metric_non_finite")
        label_quality = dict(evidence.get("label_quality") or {})
        if _as_int(label_quality.get("rows"), default=0) <= 0:
            errors.append("label_rows_missing")
    return errors


def validate_phase5_gate_bundle(
    payload: dict[str, Any] | None,
    *,
    expected_pair: str,
    expected_bundle_run_id: str,
) -> Phase5BundleValidation:
    bundle = dict(payload or {})
    errors: list[str] = []
    gate_errors: dict[str, tuple[str, ...]] = {}
    if str(bundle.get("bundle_version") or "") != "phase5_gate_bundle_v2":
        errors.append("phase5_bundle_schema_invalid")
    identity = dict(bundle.get("evidence_identity") or {})
    if str(identity.get("schema_version") or "") != EVIDENCE_IDENTITY_SCHEMA:
        errors.append("phase5_bundle_identity_schema_invalid")
    pair = str(identity.get("pair") or "").strip().upper()
    bundle_run_id = str(identity.get("bundle_run_id") or "").strip()
    model_set_id = str(identity.get("model_set_id") or "").strip()
    manifest_sha = str(identity.get("model_manifest_sha256") or "").strip().lower()
    artifact_set_sha = str(identity.get("artifact_set_sha256") or "").strip().lower()
    if pair != str(expected_pair).strip().upper():
        errors.append("phase5_bundle_pair_mismatch")
    if bundle_run_id != str(expected_bundle_run_id).strip():
        errors.append("phase5_bundle_run_id_mismatch")
    if not model_set_id:
        errors.append("phase5_bundle_model_set_id_missing")
    if not is_sha256(manifest_sha):
        errors.append("phase5_bundle_manifest_sha256_invalid")
    if not is_sha256(artifact_set_sha):
        errors.append("phase5_bundle_artifact_set_sha256_invalid")

    refs = dict(bundle.get("evidence_refs") or {})
    hashes = {str(key): str(value).strip().lower() for key, value in dict(bundle.get("evidence_hashes") or {}).items()}
    errors.extend(support_evidence_errors(bundle))
    manifest_path = Path(str(refs.get("model_manifest") or "").strip())
    active_metadata: dict[str, Any] = {}
    if not manifest_path.is_file():
        errors.append("phase5_bundle_model_manifest_missing")
    else:
        actual_manifest_sha = file_sha256(manifest_path)
        if hashes.get("model_manifest") != actual_manifest_sha:
            errors.append("phase5_bundle_model_manifest_hash_mismatch")
        manifest_payload = read_json_object(manifest_path)
        active_rows = dict(manifest_payload.get("active_model_sets") or {})
        if active_rows:
            active = active_manifest_identity(manifest_path=manifest_path, pair=expected_pair)
            active_row = dict(active_rows.get(str(expected_pair).strip().upper()) or {})
            active_metadata = dict(active_row.get("metadata") or {})
            if active.model_manifest_sha256 != manifest_sha:
                errors.append("phase5_bundle_model_identity_mismatch")
            if active.bundle_run_id != bundle_run_id:
                errors.append("phase5_bundle_active_bundle_mismatch")
            if active.model_set_id != model_set_id:
                errors.append("phase5_bundle_active_model_set_mismatch")
            if active.artifact_set_sha256 != artifact_set_sha:
                errors.append("phase5_bundle_active_artifact_set_mismatch")
        else:
            errors.append("phase5_bundle_active_manifest_required")

    research_errors: list[str] = []
    if str(active_metadata.get("promotion_status") or "").strip().lower() != "eligible":
        research_errors.append("active_manifest_promotion_not_eligible")
    active_capabilities = dict(active_metadata.get("capabilities") or {})
    if not bool(active_metadata.get("lifecycle_complete", active_capabilities.get("lifecycle_complete", False))):
        research_errors.append("active_manifest_lifecycle_incomplete")
    if research_errors:
        gate_errors["research_gate"] = tuple(dict.fromkeys(research_errors))

    economic = validate_economic_evidence(
        path=refs.get("backtest_summary"),
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=model_set_id,
        expected_model_manifest_sha256=manifest_sha,
        expected_artifact_set_sha256=artifact_set_sha,
    )
    economic_errors = list(economic.errors)
    if economic.artifact_sha256 != hashes.get("backtest_summary", ""):
        economic_errors.append("economic_artifact_hash_mismatch")
    economic_payload = read_json_object(refs.get("backtest_summary") or "")
    economic_passed, _economic_metrics = economic_sufficiency(economic_payload)
    if not economic_passed:
        economic_errors.append("economic_sufficiency_failed")
    if economic_errors:
        gate_errors["economic_gate"] = tuple(dict.fromkeys(economic_errors))

    shadow = validate_shadow_runtime_evidence(
        path=refs.get("shadow_runtime_evidence"),
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=model_set_id,
        expected_model_manifest_sha256=manifest_sha,
        expected_artifact_set_sha256=artifact_set_sha,
    )
    shadow_errors = list(shadow.errors)
    if shadow.artifact_sha256 != hashes.get("shadow_runtime_evidence", ""):
        shadow_errors.append("shadow_artifact_hash_mismatch")
    release_validation = validate_release_validation_bundle(
        path=refs.get("release_validation_bundle"),
        expected_pair=expected_pair,
        expected_bundle_run_id=expected_bundle_run_id,
        expected_model_set_id=model_set_id,
        expected_model_manifest_sha256=manifest_sha,
        expected_artifact_set_sha256=artifact_set_sha,
    )
    if release_validation.artifact_sha256 != hashes.get("release_validation_bundle", ""):
        shadow_errors.append("release_validation_bundle_hash_mismatch")
    shadow_errors.extend(release_validation.errors)
    required_finalization = {
        "release_validation_bundle",
        "finalization:fast_shadow",
        "finalization:long_shadow",
        "finalization:rollback_evidence",
        "finalization:blockers",
    }
    missing_finalization = sorted(
        required_finalization - set(str(item) for item in list(bundle.get("binding_required_evidence") or []))
    )
    if missing_finalization:
        shadow_errors.append("release_finalization_contract_missing:" + ",".join(missing_finalization))
    if shadow_errors:
        gate_errors["shadow_gate"] = tuple(dict.fromkeys(shadow_errors))

    global_valid = not errors
    research_passed = bool(global_valid and not research_errors)
    economic_gate_passed = bool(global_valid and not economic_errors)
    shadow_passed = bool(global_valid and not shadow_errors)
    operational_passed = bool(global_valid and shadow_passed)
    canary_passed = bool(research_passed and economic_gate_passed and operational_passed and shadow_passed)
    gate_passes = {
        "research_gate": research_passed,
        "economic_gate": economic_gate_passed,
        "operational_gate": operational_passed,
        "shadow_gate": shadow_passed,
        "canary_gate": canary_passed,
        # Closeout is runtime state after a canary, not release-evidence authority.
        "canary_closeout": False,
    }

    return Phase5BundleValidation(
        valid=not errors and not gate_errors,
        errors=tuple(dict.fromkeys(errors)),
        gate_errors=gate_errors,
        gate_passes=gate_passes,
        model_manifest_sha256=manifest_sha,
    )
