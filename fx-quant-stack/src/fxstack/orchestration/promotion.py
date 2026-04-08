from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fxstack.orchestration.contracts import ExperimentPromotion, ExperimentProposal
from fxstack.orchestration.experiments import render_experiment_promotion_pack
from fxstack.settings import get_settings
from fxstack.utils.hashing import hash_mapping


PHASE7_PROMOTION_SCHEMA_VERSION = "fxstack.orchestration.phase7.promotion.v1"
ALLOWED_CONFIG_DIFF_TARGETS = {
    "orchestration.profile",
    "release.metadata",
    "activation_package.metadata",
    "paper.canary.plan",
}


def _dict_payload(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _list_payload(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _string_list_payload(value: Any) -> list[str]:
    return [str(item) for item in _list_payload(value) if str(item).strip()]


def _coerce_uuid(value: Any, *, namespace_seed: str = "") -> UUID:
    if isinstance(value, UUID):
        return value
    text = str(value or "").strip()
    if text:
        try:
            return UUID(text)
        except Exception:
            seed = namespace_seed or "fxstack.orchestration.phase7.promotion"
            return uuid5(NAMESPACE_URL, f"{seed}:{text}")
    seed = namespace_seed or "fxstack.orchestration.phase7.promotion"
    return uuid5(NAMESPACE_URL, f"{seed}:default")


def _coerce_datetime(value: Any | None, *, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value is not None:
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts.astimezone(UTC)
        except Exception:
            pass
    return default or datetime.fromtimestamp(0, tz=UTC)


def _stable_unique(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        txt = str(item or "").strip()
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return out


def _bundle_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    return dict(bundle.get("summary") or {})


def _bundle_lineage(bundle: dict[str, Any]) -> dict[str, Any]:
    return dict(bundle.get("lineage") or {})


def validate_promotion_bundle(
    bundle: dict[str, Any],
    *,
    require_paths_exist: bool = False,
) -> dict[str, Any]:
    bundle = dict(bundle or {})
    summary = _bundle_summary(bundle)
    lineage = _bundle_lineage(bundle)
    windows = [dict(item or {}) for item in list(bundle.get("windows") or [])]
    checks: dict[str, bool] = {
        "schema_version_known": str(bundle.get("schema_version") or "") == "fxstack.orchestration.phase7.experiment_bundle.v1",
        "bundle_has_windows": bool(windows),
        "bundle_has_lineage": bool(lineage),
        "summary_passed": bool(summary.get("passed", False)),
        "summary_go_status": str(summary.get("status") or "").upper() == "GO",
        "window_statuses_consistent": all(
            str(item.get("status") or "").upper() == "GO" if bool(item.get("passed", False)) else str(item.get("status") or "").upper() != "GO"
            for item in windows
        ),
    }
    if require_paths_exist:
        referenced_paths = []
        for window in windows:
            for key in ("aggregate_path", "guardrails_path", "divergence_path", "proposal_votes_path", "config_path", "promotion_pack_path"):
                txt = str(window.get(key) or "").strip()
                if txt:
                    referenced_paths.append(Path(txt))
        checks["referenced_paths_exist"] = all(path.exists() for path in referenced_paths)
    failures = [name for name, passed in checks.items() if not bool(passed)]
    return {
        "schema_version": PHASE7_PROMOTION_SCHEMA_VERSION,
        "status": "GO" if not failures else "HOLD",
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "window_count": len(windows),
        "passed_window_count": int(sum(1 for item in windows if bool(item.get("passed", False)))),
        "failed_window_count": int(sum(1 for item in windows if not bool(item.get("passed", False)))),
        "bundle_id": str(bundle.get("bundle_id") or ""),
        "experiment_id": str(bundle.get("experiment_id") or ""),
        "evidence_refs": _stable_unique(_string_list_payload(bundle.get("evidence_refs"))),
    }


def build_experiment_proposal(
    *,
    bundle: dict[str, Any],
    source_run_id: UUID | str | None = None,
    hypothesis: str = "",
    change_set: list[dict[str, Any]] | None = None,
    evaluation_plan: dict[str, Any] | None = None,
    risk_notes: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    approval_status: str | None = None,
) -> ExperimentProposal:
    bundle = dict(bundle or {})
    validation = validate_promotion_bundle(bundle, require_paths_exist=False)
    summary = _bundle_summary(bundle)
    lineage = _bundle_lineage(bundle)
    windows = [dict(item or {}) for item in list(bundle.get("windows") or [])]
    bundle_id = str(bundle.get("bundle_id") or "")
    experiment_uuid = _coerce_uuid(
        bundle.get("experiment_id"),
        namespace_seed=f"fxstack.orchestration.phase7.experiment:{bundle_id or 'bundle'}",
    )
    source_uuid = None
    if source_run_id is not None:
        source_uuid = _coerce_uuid(
            source_run_id,
            namespace_seed=f"fxstack.orchestration.phase7.source:{bundle_id or 'bundle'}",
        )
    resolved_hypothesis = str(hypothesis or bundle.get("metadata", {}).get("hypothesis") or "").strip()
    if not resolved_hypothesis:
        resolved_hypothesis = f"Promote replay bundle {bundle_id or experiment_uuid}"
    resolved_change_set = list(change_set or bundle.get("metadata", {}).get("change_set") or [])
    if not resolved_change_set:
        resolved_change_set = [
            {
                "window_id": str(window.get("window_id") or ""),
                "window_dir": str(window.get("window_dir") or ""),
                "status": str(window.get("status") or ""),
                "passed": bool(window.get("passed", False)),
                "artifact_refs": dict(window.get("artifact_refs") or {}),
            }
            for window in windows
        ]
    resolved_evaluation_plan = dict(evaluation_plan or bundle.get("metadata", {}).get("evaluation_plan") or {})
    if not resolved_evaluation_plan:
        resolved_evaluation_plan = {
            "bundle_id": bundle_id,
            "window_statuses": dict(summary.get("window_statuses") or {}),
            "validation": dict(validation),
            "lineage": {
                "proposal_ref": str(lineage.get("proposal_ref") or ""),
                "promotion_decision_ref": str(lineage.get("promotion_decision_ref") or ""),
                "latest_stage": str(lineage.get("latest_stage") or ""),
            },
        }
    resolved_risk_notes = _stable_unique(
        [
            *[str(item) for item in list(risk_notes or [])],
            *[str(item) for item in list(validation.get("failures") or [])],
            *[f"{window.get('window_id')}: {window.get('status')}" for window in windows if not bool(window.get("passed", False))],
        ]
    )
    resolved_evidence_refs = _stable_unique(
        [
            *[str(item) for item in list(evidence_refs or [])],
            *[str(item) for item in list(bundle.get("evidence_refs") or [])],
            *[str(item) for item in list(validation.get("evidence_refs") or [])],
        ]
    )
    if resolved_evidence_refs:
        resolved_evidence_refs = _stable_unique(
            [
                *resolved_evidence_refs,
                str(bundle.get("metadata", {}).get("promotion_pack_path") or ""),
            ]
        )
    resolved_status = str(approval_status or "").strip().lower()
    if not resolved_status:
        resolved_status = "approved" if bool(validation.get("passed", False)) else "draft"
    if resolved_status not in {"draft", "approved", "rejected", "promoted"}:
        resolved_status = "approved" if bool(validation.get("passed", False)) else "draft"
    if not bool(validation.get("passed", False)) and resolved_status in {"approved", "promoted"}:
        resolved_status = "rejected"
    return ExperimentProposal(
        experiment_id=experiment_uuid,
        source_run_id=source_uuid,
        hypothesis=resolved_hypothesis,
        change_set=resolved_change_set,
        evaluation_plan=resolved_evaluation_plan,
        risk_notes=resolved_risk_notes,
        evidence_refs=resolved_evidence_refs,
        prompt_hash=str(bundle.get("metadata", {}).get("prompt_hash") or ""),
        tool_trace_hash=str(bundle.get("metadata", {}).get("tool_trace_hash") or ""),
        model_id=str(bundle.get("metadata", {}).get("model_id") or ""),
        decision_seed=int(bundle.get("metadata", {}).get("decision_seed") or 0),
        input_artefact_refs=_stable_unique(
            [
                *[str(item) for item in list(bundle.get("evidence_refs") or [])],
                *[str(item) for item in list(validation.get("evidence_refs") or [])],
            ]
        ),
        config_diff=dict(bundle.get("metadata", {}).get("config_diff") or {}),
        replay_window=str(summary.get("window_id") or ",".join(summary.get("window_ids") or [])),
        artifact_root=str(bundle.get("metadata", {}).get("artifact_root") or bundle.get("config_path") or ""),
        latest_stage=str(lineage.get("latest_stage") or ""),
        latest_promotion_id=str(lineage.get("latest_promotion_id") or ""),
        approval_status=resolved_status,
    )


def build_experiment_promotion(
    *,
    bundle: dict[str, Any],
    proposal: ExperimentProposal | dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    promotion_id: UUID | str | None = None,
    created_at: datetime | str | None = None,
    updated_at: datetime | str | None = None,
) -> ExperimentPromotion:
    bundle = dict(bundle or {})
    validation_payload = dict(validation or validate_promotion_bundle(bundle, require_paths_exist=False))
    proposal_model = proposal if isinstance(proposal, ExperimentProposal) else ExperimentProposal.model_validate(proposal or build_experiment_proposal(bundle=bundle).model_dump(mode="json"))
    summary = _bundle_summary(bundle)
    bundle_id = str(bundle.get("bundle_id") or "")
    promotion_uuid = _coerce_uuid(
        promotion_id or bundle_id,
        namespace_seed=f"fxstack.orchestration.phase7.promotion:{bundle_id or proposal_model.experiment_id}",
    )
    created = _coerce_datetime(created_at)
    updated = _coerce_datetime(updated_at, default=created)
    artefact_hashes = {
        "bundle": hash_mapping(bundle),
        "proposal": hash_mapping(proposal_model.model_dump(mode="json")),
        "validation": hash_mapping(validation_payload),
    }
    config_diff = dict(bundle.get("metadata", {}).get("config_diff") or {})
    if not config_diff:
        config_diff = {
            "bundle_id": bundle_id,
            "window_statuses": dict(summary.get("window_statuses") or {}),
        }
    return ExperimentPromotion(
        promotion_id=promotion_uuid,
        experiment_id=proposal_model.experiment_id,
        prompt_hash=str(proposal_model.prompt_hash or bundle.get("metadata", {}).get("prompt_hash") or ""),
        tool_trace_hash=str(proposal_model.tool_trace_hash or bundle.get("metadata", {}).get("tool_trace_hash") or ""),
        model_id=str(proposal_model.model_id or bundle.get("metadata", {}).get("model_id") or ""),
        config_diff=config_diff,
        replay_window=str(proposal_model.replay_window or summary.get("window_id") or ",".join(summary.get("window_ids") or [])),
        replay_results={"bundle": bundle, "validation": validation_payload, "proposal": proposal_model.model_dump(mode="json")},
        approval_records=[
            {"check": str(name), "passed": bool(passed)}
            for name, passed in dict(validation_payload.get("checks") or {}).items()
        ],
        paper_results={},
        canary_results={},
        release_manifest_ref=str(bundle.get("metadata", {}).get("release_manifest_ref") or ""),
        rollback_metadata={
            "latest_stage": str(bundle.get("lineage", {}).get("latest_stage") or ""),
            "latest_promotion_id": str(bundle.get("lineage", {}).get("latest_promotion_id") or ""),
        },
        artefact_hashes=artefact_hashes,
        status=str(validation_payload.get("status") or "HOLD"),
        created_at=created,
        updated_at=updated,
    )


def render_promotion_pack(
    *,
    bundle: dict[str, Any],
    validation: dict[str, Any],
    proposal: ExperimentProposal,
    promotion: ExperimentPromotion,
) -> str:
    bundle = dict(bundle or {})
    validation = dict(validation or {})
    summary = _bundle_summary(bundle)
    lineage = _bundle_lineage(bundle)
    lines = [
        f"# Phase 7 Promotion Pack: {bundle.get('experiment_id') or ''}",
        "",
        f"- Bundle ID: `{bundle.get('bundle_id') or ''}`",
        f"- Promotion ID: `{promotion.promotion_id}`",
        f"- Experiment ID: `{proposal.experiment_id}`",
        f"- Proposal Status: `{proposal.approval_status}`",
        f"- Validation Status: `{validation.get('status') or 'HOLD'}`",
        f"- Bundle Status: `{summary.get('status') or 'HOLD'}`",
        "",
        "## Validation",
        "",
    ]
    for key, passed in dict(validation.get("checks") or {}).items():
        lines.append(f"- {key}: `{'PASS' if passed else 'FAIL'}`")
    lines.extend(
        [
            "",
            "## Lineage",
            "",
            f"- Proposal Ref: `{lineage.get('proposal_ref') or ''}`",
            f"- Review Ref: `{lineage.get('review_ref') or ''}`",
            f"- Promotion Decision Ref: `{lineage.get('promotion_decision_ref') or ''}`",
            f"- Latest Stage: `{lineage.get('latest_stage') or ''}`",
            "",
            "## Windows",
            "",
        ]
    )
    for window in list(bundle.get("windows") or []):
        lines.append(f"- `{window.get('window_id') or ''}`: `{window.get('status') or ''}`")
    lines.extend(
        [
            "",
            "## Proposal",
            "",
            f"- Hypothesis: {proposal.hypothesis}",
            f"- Change Set Items: `{len(list(proposal.change_set or []))}`",
            f"- Evidence Refs: `{len(list(proposal.evidence_refs or []))}`",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _artifact_sha(path: Path) -> str:
    return hash_mapping({"path": str(path.resolve()), "text": path.read_text(encoding="utf-8")})


def _runtime_service() -> Any | None:
    try:
        from fxstack.runtime.service import RuntimeService

        settings = get_settings()
        return RuntimeService(
            database_url=str(settings.database_url),
            execution_provider=str(settings.normalized_execution_provider),
        )
    except Exception:
        return None


def _config_diff_targets_allowed(config_diff: dict[str, Any]) -> bool:
    targets = [
        str(item.get("target") or "").strip()
        for item in list(dict(config_diff or {}).get("targets") or [])
        if isinstance(item, dict)
    ]
    if not targets:
        return True
    return all(target in ALLOWED_CONFIG_DIFF_TARGETS for target in targets)


def _validate_expected_hashes(expected: dict[str, Any], actual: dict[str, str]) -> bool:
    if not expected:
        return True
    for key, value in dict(expected or {}).items():
        if str(actual.get(str(key)) or "") != str(value or ""):
            return False
    return True


def write_promotion_artifacts(
    *,
    bundle: dict[str, Any],
    out_dir: str | Path,
    proposal: ExperimentProposal | dict[str, Any] | None = None,
    promotion: ExperimentPromotion | dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    created_at: datetime | str | None = None,
    updated_at: datetime | str | None = None,
) -> dict[str, str]:
    bundle = dict(bundle or {})
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    validation_payload = dict(validation or validate_promotion_bundle(bundle, require_paths_exist=True))
    proposal_model = proposal if isinstance(proposal, ExperimentProposal) else build_experiment_proposal(bundle=bundle)
    promotion_model = (
        promotion
        if isinstance(promotion, ExperimentPromotion)
        else build_experiment_promotion(
            bundle=bundle,
            proposal=proposal_model,
            validation=validation_payload,
            created_at=created_at,
            updated_at=updated_at,
        )
    )
    out = {
        "validation": _write_json(out_path / "promotion_validation.json", validation_payload),
        "experiment_proposal": _write_json(out_path / "experiment_proposal.json", proposal_model.model_dump(mode="json")),
        "promotion_decision": _write_json(out_path / "promotion_decision.json", promotion_model.model_dump(mode="json")),
    }
    (out_path / "promotion_pack.md").write_text(
        render_promotion_pack(bundle=bundle, validation=validation_payload, proposal=proposal_model, promotion=promotion_model),
        encoding="utf-8",
    )
    out["promotion_pack"] = str(out_path / "promotion_pack.md")
    return out


def promote_experiment_bundle(
    *,
    experiment_id: str,
    bundle_root: str | Path,
    manifest_path: str = "",
    promotion_pack_path: str = "",
) -> dict[str, Any]:
    root = Path(bundle_root).resolve()
    proposal_path = root / "proposal.json"
    review_path = root / "review.json"
    bundle_path = root / "experiment_bundle.json"
    lineage_path = root / "experiment_lineage.json"
    paper_pack_path = root / "paper_pack.json"
    canary_pack_path = root / "canary_pack.json"

    proposal_payload = _read_json(proposal_path)
    bundle_payload = _read_json(bundle_path)
    review_payload = _read_json(review_path)
    lineage_payload = _read_json(lineage_path)
    paper_results = _read_json(paper_pack_path)
    canary_results = _read_json(canary_pack_path)

    proposal = ExperimentProposal.model_validate(proposal_payload)
    validation = dict(validate_promotion_bundle(bundle_payload, require_paths_exist=True))
    validation["checks"] = {
        **dict(validation.get("checks") or {}),
        "proposal_exists": proposal_path.exists(),
        "review_present": review_path.exists(),
        "review_approved": str(review_payload.get("decision") or "").strip().lower() == "approved",
        "paper_pack_present": paper_pack_path.exists(),
        "paper_pack_ready": str(paper_results.get("status") or "").strip().lower() in {"ready", "approved", "completed"},
        "canary_pack_present": canary_pack_path.exists(),
        "canary_pack_ready": str(canary_results.get("status") or "").strip().lower() in {"ready", "approved", "completed"},
        "config_diff_targets_allowed": _config_diff_targets_allowed(dict(proposal.config_diff or {})),
    }
    if str(manifest_path or "").strip():
        validation["checks"]["release_manifest_ref_present"] = Path(str(manifest_path)).exists()
    artefact_hashes = {
        "proposal.json": _artifact_sha(proposal_path) if proposal_path.exists() else "",
        "review.json": _artifact_sha(review_path) if review_path.exists() else "",
        "experiment_bundle.json": _artifact_sha(bundle_path) if bundle_path.exists() else "",
        "paper_pack.json": _artifact_sha(paper_pack_path) if paper_pack_path.exists() else "",
        "canary_pack.json": _artifact_sha(canary_pack_path) if canary_pack_path.exists() else "",
    }
    expected_hashes = dict(dict(proposal.config_diff or {}).get("expected_artefact_hashes") or {})
    validation["checks"]["artefact_hashes_match"] = _validate_expected_hashes(expected_hashes, artefact_hashes)
    validation["failures"] = [name for name, passed in dict(validation.get("checks") or {}).items() if not bool(passed)]
    validation["passed"] = not bool(validation["failures"])
    validation["status"] = "GO" if bool(validation["passed"]) else "HOLD"

    promotion = build_experiment_promotion(
        bundle=bundle_payload,
        proposal=proposal,
        validation=validation,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    promotion_payload = promotion.model_dump(mode="json")
    promotion_payload["paper_results"] = paper_results
    promotion_payload["canary_results"] = canary_results
    promotion_payload["release_manifest_ref"] = str(manifest_path or promotion.release_manifest_ref or "")
    promotion_payload["artefact_hashes"] = artefact_hashes
    promotion_payload["status"] = "promoted" if bool(validation["passed"]) else "rejected"
    promotion = ExperimentPromotion.model_validate(promotion_payload)

    config_delta = {
        "schema_version": PHASE7_PROMOTION_SCHEMA_VERSION,
        "experiment_id": str(proposal.experiment_id),
        "promotion_id": str(promotion.promotion_id),
        "config_diff": dict(proposal.config_diff or {}),
        "created_at": datetime.now(UTC).isoformat(),
    }
    rollback_plan = {
        "experiment_id": str(proposal.experiment_id),
        "promotion_id": str(promotion.promotion_id),
        "release_manifest_ref": str(manifest_path or promotion.release_manifest_ref or ""),
        "rollback_target": str(lineage_payload.get("release_manifest_ref") or ""),
        "latest_stage": str(lineage_payload.get("latest_stage") or ""),
        "created_at": datetime.now(UTC).isoformat(),
    }
    promotion_decision = promotion.model_dump(mode="json")
    promotion_decision["validation"] = validation
    promotion_decision["config_delta_ref"] = str(root / "config_delta.json")
    promotion_decision["rollback_plan_ref"] = str(root / "rollback_plan.json")
    promotion_decision["source_promotion_pack_path"] = str(promotion_pack_path or "")
    _write_json(root / "promotion_validation.json", validation)
    _write_json(root / "config_delta.json", config_delta)
    _write_json(root / "rollback_plan.json", rollback_plan)
    _write_json(root / "promotion_decision.json", promotion_decision)
    (root / "promotion_pack.md").write_text(
        render_promotion_pack(bundle=bundle_payload, validation=validation, proposal=proposal, promotion=promotion),
        encoding="utf-8",
    )

    lineage_payload.update(
        {
            "promotion_decision_ref": str(root / "promotion_decision.json"),
            "rollback_plan_ref": str(root / "rollback_plan.json"),
            "release_manifest_ref": str(manifest_path or promotion.release_manifest_ref or ""),
            "latest_stage": "promoted" if bool(validation["passed"]) else "promotion_rejected",
            "latest_promotion_id": str(promotion.promotion_id),
            "approval_status": str(proposal.approval_status),
            "promotion_ids": sorted({*list(lineage_payload.get("promotion_ids") or []), str(promotion.promotion_id)}),
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    _write_json(root / "experiment_lineage.json", lineage_payload)

    service = _runtime_service()
    if service is not None:
        try:
            updated_proposal = proposal.model_dump(mode="json")
            updated_proposal["latest_stage"] = str(lineage_payload.get("latest_stage") or "")
            updated_proposal["latest_promotion_id"] = str(promotion.promotion_id)
            updated_proposal["approval_status"] = "promoted" if bool(validation["passed"]) else proposal.approval_status
            service.upsert_experiment_proposal(updated_proposal)
            service.upsert_experiment_promotion(promotion.model_dump(mode="json"))
        except Exception:
            pass

    return {
        "ok": bool(validation["passed"]),
        "experiment_id": str(experiment_id),
        "promotion_id": str(promotion.promotion_id),
        "status": str(promotion.status),
        "validation": validation,
        "promotion_decision_path": str(root / "promotion_decision.json"),
        "rollback_plan_path": str(root / "rollback_plan.json"),
        "config_delta_path": str(root / "config_delta.json"),
        "lineage_path": str(root / "experiment_lineage.json"),
    }
