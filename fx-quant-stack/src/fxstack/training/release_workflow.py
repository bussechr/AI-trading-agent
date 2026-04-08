from __future__ import annotations

import json
import time
from datetime import datetime
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


def _as_float_ts(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _phase6b_live_canary_requested(settings: Any) -> bool:
    return str(getattr(settings, "agent_mode", "off") or "").strip().lower() == "live" or bool(
        list(getattr(settings, "agent_live_pair_allowlist", []) or [])
    )


def _phase6b_ramp_steps(settings: Any) -> list[int]:
    out = [int(item) for item in list(getattr(settings, "phase6b_canary_ramp_steps_pct", []) or []) if int(item) > 0]
    return out or [1, 5, 10]


def _phase6b_drawdown_tolerance(settings: Any) -> float:
    value = float(getattr(settings, "phase6b_canary_drawdown_deterioration_pct", -1.0) or -1.0)
    if value <= 0.0:
        raise ValueError("FXSTACK_PHASE6B_CANARY_DRAWDOWN_DETERIORATION_PCT must be configured for live canaries")
    return value


def _is_orchestration_live_canary(package: ActivationPackage) -> bool:
    canary_plan = package.canary_plan
    metadata = dict(canary_plan.metadata or {}) if canary_plan is not None else {}
    return str(metadata.get("mode") or "").strip().lower() == "orchestration_live"


def _patch_orchestration_live_runtime_state(
    *,
    svc: RuntimeService,
    updates: dict[str, Any],
) -> dict[str, Any]:
    state = svc.get_state()
    runtime_diag = dict(state.get("runtime_diag") or {})
    live = dict(runtime_diag.get("orchestration_live") or {})
    live.update({str(key): value for key, value in dict(updates or {}).items()})
    runtime_diag["orchestration_live"] = live
    svc.patch_state({"runtime_diag": runtime_diag})
    return live


def _orchestration_live_command_metrics(
    *,
    svc: RuntimeService,
    pair: str,
    alert_window_minutes: int,
) -> dict[str, Any]:
    now_ts = _now_ts()
    window_secs = max(60, int(alert_window_minutes) * 60)
    commands = [
        dict(item or {})
        for item in list(svc.get_commands(limit=500) or [])
        if str(dict(item or {}).get("symbol") or "").upper() == str(pair).upper()
        and str(dict(item or {}).get("orchestration_meta_json", {}).get("agent_mode") or "").strip().lower() == "live"
        and (now_ts - _as_float_ts(dict(item or {}).get("created_at"))) <= float(window_secs)
    ]
    all_events = [
        dict(item or {})
        for item in list(svc.get_command_events(limit=2000) or [])
        if (now_ts - _as_float_ts(dict(item or {}).get("created_at"))) <= float(window_secs)
    ]
    events_by_command: dict[str, list[dict[str, Any]]] = {}
    for event in all_events:
        command_id = str(event.get("command_id") or "")
        if not command_id:
            continue
        events_by_command.setdefault(command_id, []).append(event)
    terminal_statuses = {"acked", "failed", "expired", "duplicate"}
    success_statuses = {"acked", "duplicate"}
    ack_success_count = 0
    ack_timeout_count = 0
    orphan_count = 0
    for command in commands:
        command_id = str(command.get("command_id") or "")
        statuses = {
            str(dict(event or {}).get("event_status") or "").strip().lower()
            for event in list(events_by_command.get(command_id) or [])
            if str(dict(event or {}).get("event_status") or "").strip()
        }
        if statuses & success_statuses:
            ack_success_count += 1
        current_status = str(command.get("status") or "").strip().lower()
        if current_status in {"queued", "delivered"} and not statuses & terminal_statuses:
            ack_timeout_count += 1
            orphan_count += 1
    total = int(len(commands))
    ack_success_rate = 1.0 if total <= 0 else float(ack_success_count) / float(total)
    ack_timeout_rate = 0.0 if total <= 0 else float(ack_timeout_count) / float(total)
    return {
        "command_count": total,
        "ack_success_rate": float(ack_success_rate),
        "ack_timeout_rate": float(ack_timeout_rate),
        "orphan_command_count": int(orphan_count),
    }


def _orchestration_live_canary_metadata(package: ActivationPackage) -> dict[str, Any]:
    canary_plan = package.canary_plan
    return dict(canary_plan.metadata or {}) if canary_plan is not None else {}


def _runtime_kill_orchestration_live(*, svc: RuntimeService, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    updated = _patch_orchestration_live_runtime_state(
        svc=svc,
        updates={
            "runtime_enabled": False,
            "last_kill_reason": str(reason or ""),
            "last_kill_at": _now_ts(),
        },
    )
    svc.record_governance_event(
        event_type="orchestration_live_runtime_killed",
        reason=str(reason or "runtime_killed"),
        payload={
            "runtime_enabled": bool(updated.get("runtime_enabled", False)),
            "queue_kill_active": bool(updated.get("queue_kill_active", False)),
            **dict(payload or {}),
        },
    )
    return dict(updated or {})


def _queue_kill_orchestration_live(*, svc: RuntimeService, reason: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    purged = int(svc.purge_pending_commands(reason=str(reason or "orchestration_live_queue_kill"), include_delivered=False))
    updated = _patch_orchestration_live_runtime_state(
        svc=svc,
        updates={
            "runtime_enabled": False,
            "queue_kill_active": True,
            "queue_kill_reason": str(reason or ""),
            "queue_killed_at": _now_ts(),
            "purged_command_count": int(purged),
        },
    )
    svc.record_governance_event(
        event_type="orchestration_live_queue_killed",
        reason=str(reason or "queue_killed"),
        payload={
            "purged_command_count": int(purged),
            "runtime_enabled": bool(updated.get("runtime_enabled", False)),
            "queue_kill_active": bool(updated.get("queue_kill_active", False)),
            **dict(payload or {}),
        },
    )
    return {"purged_command_count": int(purged), **dict(updated or {})}


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


def _release_lineage_metadata(package: ActivationPackage, *, release_dir: Path | None = None) -> dict[str, Any]:
    release_dir = release_dir or _release_dir(pair=str(package.pair), bundle_run_id=str(package.bundle_run_id))
    return {
        "experiment_id": str(package.experiment_id or ""),
        "promotion_id": str(package.promotion_id or package.bundle_run_id or ""),
        "experiment_lineage_ref": str(package.experiment_lineage_ref or (release_dir / "experiment_lineage.json")),
        "paper_pack_ref": str(package.paper_pack_ref or (release_dir / "paper_pack.md")),
        "canary_pack_ref": str(package.canary_pack_ref or (release_dir / "canary_pack.md")),
        "rollback_plan_ref": str(package.rollback_plan_ref or (release_dir / "rollback_plan.json")),
    }


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
    bundle_metadata = dict(bundle.metadata or {})
    lineage_metadata = {
        "experiment_id": str(bundle_metadata.get("experiment_id") or ""),
        "promotion_id": str(bundle_metadata.get("promotion_id") or str(bundle.bundle_run_id) or ""),
        "experiment_lineage_ref": str(bundle_metadata.get("experiment_lineage_ref") or bundle_metadata.get("lineage_ref") or ""),
        "paper_pack_ref": str(bundle_metadata.get("paper_pack_ref") or bundle_metadata.get("promotion_pack") or ""),
        "canary_pack_ref": str(bundle_metadata.get("canary_pack_ref") or ""),
        "rollback_plan_ref": str(bundle_metadata.get("rollback_plan_ref") or ""),
    }
    evidence_refs = {
        **dict((bundle.metadata or {}).get("phase5_gates") or {}),
        **dict((bundle.metadata or {}).get("phase3_evidence") or {}),
        "backtest_summary": str((bundle.metadata or {}).get("backtest_summary") or ""),
        "model_manifest": str((bundle.metadata or {}).get("model_manifest") or ""),
    }
    for key, value in lineage_metadata.items():
        if str(value).strip():
            evidence_refs.setdefault(str(key), str(value))
    package = build_activation_package(
        bundle_run_id=str(bundle.bundle_run_id),
        pair=str(bundle.pair).upper(),
        target_alias=str(target_alias),
        promotion_status=str(bundle.promotion_status or ""),
        experiment_id=str(lineage_metadata.get("experiment_id") or ""),
        promotion_id=str(lineage_metadata.get("promotion_id") or ""),
        experiment_lineage_ref=str(lineage_metadata.get("experiment_lineage_ref") or ""),
        paper_pack_ref=str(lineage_metadata.get("paper_pack_ref") or ""),
        canary_pack_ref=str(lineage_metadata.get("canary_pack_ref") or ""),
        rollback_plan_ref=str(lineage_metadata.get("rollback_plan_ref") or ""),
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
        evidence_refs=evidence_refs,
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
    release_dir.mkdir(parents=True, exist_ok=True)
    lineage_path = release_dir / "experiment_lineage.json"
    paper_pack_path = release_dir / "paper_pack.md"
    canary_pack_path = release_dir / "canary_pack.md"
    rollback_plan_path = release_dir / "rollback_plan.json"
    lineage_payload = {
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status or ""),
        **_release_lineage_metadata(package, release_dir=release_dir),
        "evidence_refs": dict(package.evidence_refs or {}),
    }
    package.experiment_id = str(lineage_payload.get("experiment_id") or package.experiment_id or "")
    package.promotion_id = str(lineage_payload.get("promotion_id") or package.promotion_id or package.bundle_run_id or "")
    package.experiment_lineage_ref = str(lineage_path)
    package.paper_pack_ref = str(paper_pack_path)
    package.canary_pack_ref = str(canary_pack_path)
    package.rollback_plan_ref = str(rollback_plan_path)
    package.evidence_refs = {
        **dict(package.evidence_refs or {}),
        "experiment_lineage": str(lineage_path),
        "paper_pack": str(paper_pack_path),
        "canary_pack": str(canary_pack_path),
        "rollback_plan": str(rollback_plan_path),
    }
    out = {
        "release_dir": str(release_dir),
        "experiment_lineage": _write_json(lineage_path, lineage_payload),
        "activation_package": _write_json(release_dir / "activation_package.json", package.to_dict()),
    }
    paper_pack_lines = [
        "# Paper Pack",
        "",
        f"- Pair: `{str(package.pair).upper()}`",
        f"- Bundle Run ID: `{str(package.bundle_run_id)}`",
        f"- Experiment ID: `{str(package.experiment_id or '')}`",
        f"- Promotion ID: `{str(package.promotion_id or '')}`",
        f"- Release Status: `{str(package.release_status or '')}`",
        f"- Experiment Lineage Ref: `{str(package.experiment_lineage_ref or '')}`",
        f"- Rollback Plan Ref: `{str(package.rollback_plan_ref or '')}`",
    ]
    canary_pack_lines = [
        "# Canary Pack",
        "",
        f"- Pair: `{str(package.pair).upper()}`",
        f"- Bundle Run ID: `{str(package.bundle_run_id)}`",
        f"- Canary Plan Scope: `{str(package.canary_plan.scope if package.canary_plan is not None else '')}`",
        f"- Canary Plan Status: `{str(package.canary_plan.status if package.canary_plan is not None else package.release_status or '')}`",
        f"- Canary Pack Ref: `{str(package.canary_pack_ref or '')}`",
        f"- Paper Pack Ref: `{str(package.paper_pack_ref or '')}`",
    ]
    out["paper_pack"] = _write_markdown(paper_pack_path, paper_pack_lines)
    out["canary_pack"] = _write_markdown(canary_pack_path, canary_pack_lines)
    out["rollback_plan"] = _write_json(
        rollback_plan_path,
        {
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "rollback_target": package.rollback_target.to_dict() if package.rollback_target is not None else {},
            "release_status": str(package.release_status or ""),
            "experiment_id": str(package.experiment_id or ""),
            "promotion_id": str(package.promotion_id or ""),
        },
    )
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
    live_canary = _phase6b_live_canary_requested(s)
    live_pair_allowlist = [str(item).upper() for item in list(getattr(s, "agent_live_pair_allowlist", []) or []) if str(item).strip()]
    live_sleeve_allowlist = [str(item) for item in list(getattr(s, "agent_live_sleeve_allowlist", []) or []) if str(item).strip()]
    live_intent_allowlist = [str(item).lower() for item in list(getattr(s, "agent_live_intent_allowlist", []) or []) if str(item).strip()]
    if live_canary and live_pair_allowlist:
        allowlist = list(live_pair_allowlist)
    ramp_steps = _phase6b_ramp_steps(s)
    current_stage_pct = int(ramp_steps[0]) if ramp_steps else 0
    canary_success_criteria = {
        "latency_budget_ms": float(s.phase5_canary_latency_budget_ms),
        "stale_feature_limit": int(s.phase5_canary_stale_feature_limit),
        "drawdown_limit_pct": float(s.phase5_canary_drawdown_limit_pct),
        "calibration_drift_limit": float(s.phase5_canary_calibration_drift_limit),
    }
    canary_metadata: dict[str, Any] = {
        "allowlisted_pairs": allowlist,
        "budget_scale": float(budget_scale if budget_scale is not None else s.phase5_canary_budget_scale),
    }
    canary_abort_conditions = [
        "latency_breach",
        "stale_features",
        "rollout_breach",
        "drawdown_breach",
        "calibration_drift",
    ]
    canary_scope = "pair_allowlist"
    traffic_fraction = 1.0
    if live_canary:
        traffic_fraction = float(current_stage_pct) / 100.0 if current_stage_pct > 0 else 0.0
        canary_success_criteria.update(
            {
                "p95_overhead_ms": float(s.phase6b_canary_p95_overhead_ms),
                "p99_overhead_ms": float(s.phase6b_canary_p99_overhead_ms),
                "ack_success_floor": float(s.phase6b_canary_ack_success_floor),
                "orphan_command_limit": int(s.phase6b_canary_orphan_command_limit),
                "entry_ratio_floor": float(s.phase6b_canary_entry_ratio_floor),
                "slot_utilisation_floor": float(s.phase6b_canary_slot_utilisation_floor),
                "drawdown_deterioration_pct": float(_phase6b_drawdown_tolerance(s)),
                "alert_window_minutes": int(s.phase6b_canary_alert_window_minutes),
            }
        )
        canary_metadata.update(
            {
                "mode": "orchestration_live",
                "allowlisted_pairs": list(allowlist),
                "budget_scale": float(traffic_fraction),
                "live_pair_allowlist": list(live_pair_allowlist),
                "live_sleeve_allowlist": list(live_sleeve_allowlist),
                "live_intent_allowlist": list(live_intent_allowlist),
                "ramp_steps_pct": list(ramp_steps),
                "current_stage_index": 0,
                "current_stage_pct": int(current_stage_pct),
                "signoff_records": [],
                "replay_evidence_refs": [],
                "paper_evidence_refs": [],
                "rollback_drill_refs": [],
                "residual_risk_note": "",
                "promotion_pack_path": "",
                "runtime_enabled": True,
                "queue_kill_active": False,
            }
        )
        canary_abort_conditions.extend(
            [
                "ack_timeout_spike",
                "orphan_commands",
                "readiness_degradation",
                "orchestration_live_breach",
            ]
        )
        canary_scope = "orchestration_live_canary"
    canary = CanaryPlan(
        plan_id=f"{pair_key.lower()}-{str(bundle.bundle_run_id)}-canary",
        scope=canary_scope,
        status="planned",
        traffic_fraction=float(traffic_fraction),
        duration_minutes=int(duration_minutes or s.phase5_observation_window_minutes),
        metrics_window_minutes=int(duration_minutes or s.phase5_observation_window_minutes),
        success_criteria=canary_success_criteria,
        abort_conditions=canary_abort_conditions,
        metadata=canary_metadata,
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
        summary=str(
            summary
            or (
                f"Stage {pair_key} {bundle.bundle_run_id} from alias {alias} for Phase 6B orchestration-live canary."
                if live_canary
                else f"Stage {pair_key} {bundle.bundle_run_id} from alias {alias} for Phase 5 rollout."
            )
        ),
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
    canary_metadata = _orchestration_live_canary_metadata(package)
    canary_prep = canary_prep_metadata(package)
    live_canary = _is_orchestration_live_canary(package)
    allowlisted_pairs = [
        str(item).upper()
        for item in list(canary_metadata.get("allowlisted_pairs") or [])
        if str(item).strip()
    ]
    live_pair_allowlist = [
        str(item).upper()
        for item in list(canary_metadata.get("live_pair_allowlist") or [])
        if str(item).strip()
    ]
    budget_scale = float(
        canary_metadata.get("budget_scale")
        or canary_prep.get("budget_scale")
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
            "strategy": "orchestration_live" if live_canary else "phase5_shadow",
            "allowlisted_pairs": live_pair_allowlist or allowlisted_pairs,
            "budget_scale": budget_scale,
            "budget_reason": "phase6b_orchestration_live" if live_canary else "phase5_canary",
            "current_stage_index": int(canary_prep.get("current_stage_index") or 0),
            "current_stage_pct": int(canary_prep.get("current_stage_pct") or 0),
            "runtime_enabled": bool(canary_prep.get("runtime_enabled", True)),
            "queue_kill_active": bool(canary_prep.get("queue_kill_active", False)),
        },
    }
    if live_canary:
        patch["orchestration_live_canary"] = {
            "mode": "orchestration_live",
            "live_pair_allowlist": list(live_pair_allowlist),
            "live_sleeve_allowlist": list(canary_prep.get("live_sleeve_allowlist") or []),
            "live_intent_allowlist": list(canary_prep.get("live_intent_allowlist") or []),
            "ramp_steps_pct": list(canary_prep.get("ramp_steps_pct") or []),
            "current_stage_index": int(canary_prep.get("current_stage_index") or 0),
            "current_stage_pct": int(canary_prep.get("current_stage_pct") or 0),
            "promotion_pack_path": str(canary_prep.get("promotion_pack_path") or ""),
            "signoff_records": list(canary_prep.get("signoff_records") or []),
            "replay_evidence_refs": list(canary_prep.get("replay_evidence_refs") or []),
            "paper_evidence_refs": list(canary_prep.get("paper_evidence_refs") or []),
            "rollback_drill_refs": list(canary_prep.get("rollback_drill_refs") or []),
            "residual_risk_note": str(canary_prep.get("residual_risk_note") or ""),
            "runtime_enabled": bool(canary_prep.get("runtime_enabled", True)),
            "queue_kill_active": bool(canary_prep.get("queue_kill_active", False)),
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
        if _is_orchestration_live_canary(package):
            metadata = dict(canary_plan.metadata or {})
            live_pair_allowlist = [
                str(item).upper()
                for item in list(metadata.get("live_pair_allowlist") or [])
                if str(item).strip()
            ]
            live_sleeve_allowlist = [str(item) for item in list(metadata.get("live_sleeve_allowlist") or []) if str(item).strip()]
            live_intent_allowlist = [str(item).lower() for item in list(metadata.get("live_intent_allowlist") or []) if str(item).strip()]
            ramp_steps_pct = [int(item) for item in list(metadata.get("ramp_steps_pct") or []) if str(item).strip()]
            current_stage_index = int(metadata.get("current_stage_index") or 0)
            current_stage_pct = int(metadata.get("current_stage_pct") or 0)
            if not package.signed_off_by:
                blockers.append("signoff_missing")
            if not live_pair_allowlist:
                blockers.append("live_pair_allowlist_missing")
            if not live_sleeve_allowlist:
                blockers.append("live_sleeve_allowlist_missing")
            if not live_intent_allowlist:
                blockers.append("live_intent_allowlist_missing")
            if not ramp_steps_pct:
                blockers.append("live_ramp_steps_missing")
            elif current_stage_index < 0 or current_stage_index >= len(ramp_steps_pct):
                blockers.append("live_current_stage_invalid")
            if current_stage_pct <= 0:
                blockers.append("live_current_stage_pct_missing")

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
    svc = RuntimeService(database_url=database_url)
    runtime_state = svc.get_state()
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
        metadata = dict(package.canary_plan.metadata or {})
        package.canary_plan.status = "active"
        if _is_orchestration_live_canary(package):
            metadata.update(
                {
                    "runtime_enabled": True,
                    "queue_kill_active": False,
                    "queue_kill_reason": "",
                    "queue_killed_at": 0.0,
                    "budget_scale": float(package.canary_plan.traffic_fraction or metadata.get("budget_scale") or 0.0),
                }
            )
        package.canary_plan.metadata = {
            **metadata,
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
    svc.record_governance_event(
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
    live_runtime_state: dict[str, Any] = {}
    if _is_orchestration_live_canary(package):
        canary_prep = canary_prep_metadata(package)
        live_runtime_state = _patch_orchestration_live_runtime_state(
            svc=svc,
            updates={
                "enabled": True,
                "mode": "live",
                "runtime_enabled": True,
                "queue_kill_active": False,
                "queue_kill_reason": "",
                "queue_killed_at": 0.0,
                "active_pair_scope": list(canary_prep.get("live_pair_allowlist") or canary_prep.get("allowlisted_pairs") or []),
                "active_sleeve_scope": list(canary_prep.get("live_sleeve_allowlist") or []),
                "active_intent_scope": list(canary_prep.get("live_intent_allowlist") or []),
                "ramp_steps_pct": list(canary_prep.get("ramp_steps_pct") or []),
                "current_stage_index": int(canary_prep.get("current_stage_index") or 0),
                "current_stage_pct": int(canary_prep.get("current_stage_pct") or 0),
                "budget_scale": float(canary_prep.get("budget_scale") or 0.0),
                "promotion_pack_path": str(canary_prep.get("promotion_pack_path") or ""),
                "signoff_records": list(canary_prep.get("signoff_records") or []),
                "release_status": str(package.release_status or ""),
                "bundle_run_id": str(package.bundle_run_id or ""),
            },
        )
        svc.record_governance_event(
            event_type="orchestration_live_started",
            reason=f"{str(package.pair).upper()} orchestration live canary started",
            payload={
                "pair": str(package.pair).upper(),
                "bundle_run_id": str(package.bundle_run_id),
                "live_pair_allowlist": list(canary_prep.get("live_pair_allowlist") or []),
                "live_sleeve_allowlist": list(canary_prep.get("live_sleeve_allowlist") or []),
                "live_intent_allowlist": list(canary_prep.get("live_intent_allowlist") or []),
                "current_stage_pct": int(canary_prep.get("current_stage_pct") or 0),
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
        "orchestration_live": dict(live_runtime_state),
        "activation_package": written["activation_package"],
    }


def advance_canary_stage(
    *,
    pair: str,
    database_url: str,
    manifest_path: Path,
    promotion_pack_path: str,
    author: str,
    bundle_run_id: str = "",
    note: str = "",
) -> dict[str, Any]:
    package, release_dir = load_release_package(pair=pair, bundle_run_id=bundle_run_id)
    if not _is_orchestration_live_canary(package):
        return {"ok": False, "error": "not_orchestration_live_canary", "pair": str(package.pair).upper()}
    if str(package.release_status or "").strip().lower() != "canary_active":
        return {
            "ok": False,
            "error": "canary_stage_blocked",
            "reason": f"release_status:{str(package.release_status or 'missing')}",
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
        }
    canary_plan = package.canary_plan
    if canary_plan is None:
        return {"ok": False, "error": "canary_plan_missing", "pair": str(package.pair).upper()}
    metadata = dict(canary_plan.metadata or {})
    ramp_steps = [int(item) for item in list(metadata.get("ramp_steps_pct") or []) if str(item).strip()]
    if not ramp_steps:
        return {"ok": False, "error": "ramp_steps_missing", "pair": str(package.pair).upper()}
    current_stage_index = int(metadata.get("current_stage_index") or 0)
    if current_stage_index >= len(ramp_steps) - 1:
        return {
            "ok": False,
            "error": "canary_stage_complete",
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "current_stage_index": int(current_stage_index),
            "current_stage_pct": int(metadata.get("current_stage_pct") or ramp_steps[-1]),
        }
    pack_path = Path(str(promotion_pack_path or metadata.get("promotion_pack_path") or "").strip())
    if not pack_path.exists():
        return {
            "ok": False,
            "error": "promotion_pack_missing",
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "promotion_pack_path": str(pack_path),
        }
    current_stage_pct = int(metadata.get("current_stage_pct") or ramp_steps[current_stage_index])
    signoff_records = [dict(item or {}) for item in list(metadata.get("signoff_records") or []) if isinstance(item, dict)]
    signoff_records.append(
        {
            "stage_pct": int(current_stage_pct),
            "author": str(author or ""),
            "signed_at": _now_ts(),
            "promotion_pack_path": str(pack_path),
            "note": str(note or ""),
        }
    )
    next_stage_index = int(current_stage_index + 1)
    next_stage_pct = int(ramp_steps[next_stage_index])
    canary_plan.traffic_fraction = float(next_stage_pct) / 100.0
    canary_plan.metadata = {
        **metadata,
        "promotion_pack_path": str(pack_path),
        "signoff_records": signoff_records,
        "current_stage_index": int(next_stage_index),
        "current_stage_pct": int(next_stage_pct),
        "budget_scale": float(canary_plan.traffic_fraction),
        "last_ramp_advanced_at": _now_ts(),
    }
    phase5_bundle = _read_json(release_dir / "phase5_gate_bundle.json")
    pairs = [
        str(item).upper()
        for item in list((canary_plan.metadata or {}).get("allowlisted_pairs") or [package.pair])
        if str(item).strip()
    ]
    activate_mlflow_alias(
        database_url=database_url,
        manifest_path=manifest_path,
        pairs=pairs,
        alias=str(package.model_alias or package.target_alias or "shadow"),
        metadata_patch=_release_metadata_patch(package=package, phase5_bundle=phase5_bundle),
    )
    svc = RuntimeService(database_url=database_url)
    live_runtime_state = _patch_orchestration_live_runtime_state(
        svc=svc,
        updates={
            "runtime_enabled": True,
            "queue_kill_active": False,
            "queue_kill_reason": "",
            "queue_killed_at": 0.0,
            "active_pair_scope": list(canary_plan.metadata.get("live_pair_allowlist") or canary_plan.metadata.get("allowlisted_pairs") or []),
            "active_sleeve_scope": list(canary_plan.metadata.get("live_sleeve_allowlist") or []),
            "active_intent_scope": list(canary_plan.metadata.get("live_intent_allowlist") or []),
            "ramp_steps_pct": list(canary_plan.metadata.get("ramp_steps_pct") or []),
            "current_stage_index": int(next_stage_index),
            "current_stage_pct": int(next_stage_pct),
            "budget_scale": float(canary_plan.traffic_fraction),
            "promotion_pack_path": str(pack_path),
            "signoff_records": signoff_records,
        },
    )
    svc.record_governance_event(
        event_type="orchestration_live_ramp_advanced",
        reason=f"{str(package.pair).upper()} canary advanced to {next_stage_pct}%",
        payload={
            "pair": str(package.pair).upper(),
            "bundle_run_id": str(package.bundle_run_id),
            "from_stage_pct": int(current_stage_pct),
            "to_stage_pct": int(next_stage_pct),
            "promotion_pack_path": str(pack_path),
            "author": str(author or ""),
        },
    )
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    return {
        "ok": True,
        "pair": str(package.pair).upper(),
        "bundle_run_id": str(package.bundle_run_id),
        "release_status": str(package.release_status),
        "current_stage_index": int(next_stage_index),
        "current_stage_pct": int(next_stage_pct),
        "canary_prep": canary_prep_metadata(package),
        "orchestration_live": dict(live_runtime_state),
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
    live_canary = _is_orchestration_live_canary(package)
    breaches: list[str] = []
    control_action = "none"
    orchestration_live: dict[str, Any] = {}
    command_metrics: dict[str, Any] = {}
    thresholds: dict[str, Any] = {}

    if live_canary:
        canary_plan = package.canary_plan
        live_diag = dict(runtime_diag.get("orchestration_live") or {})
        metadata = dict(canary_plan.metadata or {}) if canary_plan is not None else {}
        success_criteria = dict(canary_plan.success_criteria or {}) if canary_plan is not None else {}
        thresholds = {
            "p95_overhead_ms": float(success_criteria.get("p95_overhead_ms") or get_settings().phase6b_canary_p95_overhead_ms),
            "p99_overhead_ms": float(success_criteria.get("p99_overhead_ms") or get_settings().phase6b_canary_p99_overhead_ms),
            "ack_success_floor": float(success_criteria.get("ack_success_floor") or get_settings().phase6b_canary_ack_success_floor),
            "orphan_command_limit": int(success_criteria.get("orphan_command_limit") or get_settings().phase6b_canary_orphan_command_limit),
            "entry_ratio_floor": float(success_criteria.get("entry_ratio_floor") or get_settings().phase6b_canary_entry_ratio_floor),
            "slot_utilisation_floor": float(success_criteria.get("slot_utilisation_floor") or get_settings().phase6b_canary_slot_utilisation_floor),
            "drawdown_deterioration_pct": float(success_criteria.get("drawdown_deterioration_pct") or _phase6b_drawdown_tolerance(get_settings())),
            "alert_window_minutes": int(success_criteria.get("alert_window_minutes") or get_settings().phase6b_canary_alert_window_minutes),
        }
        command_metrics = _orchestration_live_command_metrics(
            svc=svc,
            pair=str(package.pair).upper(),
            alert_window_minutes=int(thresholds["alert_window_minutes"]),
        )
        runtime_status = str(state.get("runtime_status") or "").strip().lower()
        readiness_streak = int(metadata.get("readiness_degradation_streak") or 0)
        readiness_degraded = not bool(pair_readiness.get("ready", False)) or runtime_status != "running"
        readiness_streak = int(readiness_streak + 1) if readiness_degraded else 0

        p95_ms = float(live_diag.get("p95_ms") or 0.0)
        p99_ms = float(live_diag.get("p99_ms") or 0.0)
        if p95_ms > float(thresholds["p95_overhead_ms"]):
            breaches.append("overhead_p95_breach")
        if p99_ms > float(thresholds["p99_overhead_ms"]):
            breaches.append("overhead_p99_breach")
        if int(live_diag.get("repeated_graph_fault_count") or 0) >= 3:
            breaches.append("graph_fault_spike")
        if int(live_diag.get("trace_persistence_failure_count") or 0) >= 3:
            breaches.append("trace_persistence_failure_spike")
        if int(command_metrics.get("command_count") or 0) > 0 and float(command_metrics.get("ack_success_rate") or 0.0) < float(thresholds["ack_success_floor"]):
            breaches.append("ack_timeout_spike")
        if int(command_metrics.get("orphan_command_count") or 0) > int(thresholds["orphan_command_limit"]):
            breaches.append("orphan_commands")
        entry_ratio = float(live_diag.get("entry_ratio_vs_baseline") or 0.0)
        if entry_ratio > 0.0 and entry_ratio < float(thresholds["entry_ratio_floor"]):
            breaches.append("entry_ratio_breach")
        slot_utilisation = float(live_diag.get("slot_utilisation_vs_baseline") or 0.0)
        if slot_utilisation > 0.0 and slot_utilisation < float(thresholds["slot_utilisation_floor"]):
            breaches.append("slot_utilisation_breach")
        drawdown_deterioration = float(live_diag.get("drawdown_deterioration_pct") or 0.0)
        if drawdown_deterioration > float(thresholds["drawdown_deterioration_pct"]):
            breaches.append("drawdown_deterioration_breach")
        if readiness_streak >= 2:
            breaches.append("readiness_degradation")

        if canary_plan is not None:
            canary_plan.status = "ok" if not breaches else "breach"
            canary_plan.metadata = {
                **metadata,
                "last_checked_at": _now_ts(),
                "breaches": list(breaches),
                "readiness_degradation_streak": int(readiness_streak),
                "runtime_enabled": bool(live_diag.get("runtime_enabled", metadata.get("runtime_enabled", True))),
                "queue_kill_active": bool(live_diag.get("queue_kill_active", metadata.get("queue_kill_active", False))),
                "queue_kill_reason": str(live_diag.get("queue_kill_reason") or metadata.get("queue_kill_reason") or ""),
                "queue_killed_at": float(live_diag.get("queue_killed_at") or metadata.get("queue_killed_at") or 0.0),
                "promotion_pack_path": str(metadata.get("promotion_pack_path") or ""),
                "monitor_metrics": {
                    "p95_ms": float(p95_ms),
                    "p99_ms": float(p99_ms),
                    "ack_success_rate": float(command_metrics.get("ack_success_rate") or 0.0),
                    "ack_timeout_rate": float(command_metrics.get("ack_timeout_rate") or 0.0),
                    "orphan_command_count": int(command_metrics.get("orphan_command_count") or 0),
                    "entry_ratio_vs_baseline": float(entry_ratio),
                    "slot_utilisation_vs_baseline": float(slot_utilisation),
                    "drawdown_deterioration_pct": float(drawdown_deterioration),
                    "graph_fault_count": int(live_diag.get("graph_fault_count") or 0),
                    "trace_persistence_failure_count": int(live_diag.get("trace_persistence_failure_count") or 0),
                },
            }
        status = "ok" if not breaches else "breach"
    else:
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
        "orchestration_live": orchestration_live,
        "command_metrics": command_metrics,
        "thresholds": thresholds,
        "control_action": str(control_action),
    }
    if breaches:
        if live_canary:
            queue_kill_reasons = [item for item in breaches if item in {"ack_timeout_spike", "orphan_commands"}]
            if queue_kill_reasons:
                orchestration_live = _queue_kill_orchestration_live(
                    svc=svc,
                    reason=";".join(queue_kill_reasons),
                    payload={"pair": str(package.pair).upper(), "bundle_run_id": str(package.bundle_run_id)},
                )
                control_action = "queue_kill"
            else:
                orchestration_live = _runtime_kill_orchestration_live(
                    svc=svc,
                    reason=";".join(breaches),
                    payload={"pair": str(package.pair).upper(), "bundle_run_id": str(package.bundle_run_id)},
                )
                control_action = "runtime_kill"
            if package.canary_plan is not None:
                package.canary_plan.metadata = {
                    **dict(package.canary_plan.metadata or {}),
                    "runtime_enabled": bool(dict(orchestration_live or {}).get("runtime_enabled", False)),
                    "queue_kill_active": bool(dict(orchestration_live or {}).get("queue_kill_active", False)),
                    "queue_kill_reason": str(dict(orchestration_live or {}).get("queue_kill_reason") or ""),
                    "queue_killed_at": float(dict(orchestration_live or {}).get("queue_killed_at") or 0.0),
                }
            payload["control_action"] = str(control_action)
            payload["orchestration_live"] = dict(orchestration_live)
            svc.record_governance_event(
                event_type="orchestration_live_breach",
                reason=";".join(breaches),
                payload=payload,
            )
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
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle=phase5_bundle)
    payload["canary_prep"] = canary_prep_metadata(package)
    payload["activation_package"] = written["activation_package"]
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
    live_canary = _is_orchestration_live_canary(package)
    svc = RuntimeService(database_url=database_url)
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
            if live_canary:
                package.canary_plan.metadata = {
                    **dict(package.canary_plan.metadata or {}),
                    "runtime_enabled": False,
                    "queue_kill_active": False,
                    "queue_kill_reason": "",
                    "queue_killed_at": 0.0,
                }
        bundle = resolve_bundle_manifest_by_alias(pair=str(package.pair).upper(), alias=str(package.model_alias or package.target_alias or "shadow"))
        set_bundle_alias(bundle=bundle, alias="champion")
        activate_mlflow_alias(
            database_url=database_url,
            manifest_path=manifest_path,
            pairs=allowlisted_pairs,
            alias="champion",
            metadata_patch=_release_metadata_patch(package=package, phase5_bundle=phase5_bundle),
        )
        svc.record_governance_event(
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
            if live_canary:
                package.canary_plan.metadata = {
                    **dict(package.canary_plan.metadata or {}),
                    "runtime_enabled": False,
                    "queue_kill_active": False,
                    "queue_kill_reason": "",
                    "queue_killed_at": 0.0,
                }
        svc.record_governance_event(
            event_type="canary_rejected",
            reason=f"{str(package.pair).upper()} canary rejected",
            payload={"pair": str(package.pair).upper(), "bundle_run_id": str(package.bundle_run_id)},
        )
    if live_canary:
        _patch_orchestration_live_runtime_state(
            svc=svc,
            updates={
                "enabled": False,
                "runtime_enabled": False,
                "queue_kill_active": False,
                "queue_kill_reason": "",
                "queue_killed_at": 0.0,
                "release_status": str(package.release_status or ""),
            },
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
    live_canary = _is_orchestration_live_canary(package)
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
            "runtime_enabled": False if live_canary else bool(dict(package.canary_plan.metadata or {}).get("runtime_enabled", False)),
            "queue_kill_active": False if live_canary else bool(dict(package.canary_plan.metadata or {}).get("queue_kill_active", False)),
            "queue_kill_reason": "" if live_canary else str(dict(package.canary_plan.metadata or {}).get("queue_kill_reason") or ""),
            "queue_killed_at": 0.0 if live_canary else float(dict(package.canary_plan.metadata or {}).get("queue_killed_at") or 0.0),
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
    svc = RuntimeService(database_url=database_url)
    if live_canary:
        _patch_orchestration_live_runtime_state(
            svc=svc,
            updates={
                "enabled": False,
                "runtime_enabled": False,
                "queue_kill_active": False,
                "queue_kill_reason": "",
                "queue_killed_at": 0.0,
                "release_status": str(package.release_status or ""),
            },
        )
    svc.record_governance_event(
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
    package_canary_prep = canary_prep_metadata(package)
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
        "active_main_runtime_rollout": dict(metadata.get("main_runtime_rollout") or {}),
        "shadow_acceptance_summary": dict(summarize_shadow_acceptance(package) or metadata.get("shadow_acceptance_summary") or {}),
        "canary_prep": dict(package_canary_prep or metadata.get("canary_prep") or {}),
        "activation_package": package.to_dict(),
    }
