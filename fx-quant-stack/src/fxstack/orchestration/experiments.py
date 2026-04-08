from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fxstack.orchestration.contracts import ExperimentLineage, ExperimentProposal
from fxstack.settings import get_settings
from fxstack.utils.hashing import hash_mapping


PHASE7_EXPERIMENT_BUNDLE_SCHEMA_VERSION = "fxstack.orchestration.phase7.experiment_bundle.v1"
PHASE7_EXPERIMENT_LINEAGE_SCHEMA_VERSION = "fxstack.orchestration.phase7.experiment_lineage.v1"


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
            seed = namespace_seed or "fxstack.orchestration.phase7.experiment"
            return uuid5(NAMESPACE_URL, f"{seed}:{text}")
    seed = namespace_seed or "fxstack.orchestration.phase7.experiment"
    return uuid5(NAMESPACE_URL, f"{seed}:default")


def _coerce_datetime(value: Any | None, *, default: datetime | None = None) -> datetime:
    if isinstance(value, datetime):
        return value
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


def _stitch_scalar(values: list[str], *, prefix: str = "stitched") -> str:
    unique = sorted({str(item).strip() for item in values if str(item).strip()})
    if not unique:
        return ""
    if len(unique) == 1:
        return unique[0]
    return f"{prefix}:{hash_mapping({'values': unique})[:16]}"


def collect_window_artifact(window_result: dict[str, Any]) -> dict[str, Any]:
    window = dict(window_result or {})
    window_dir_text = str(window.get("window_dir") or "").strip()
    window_dir = Path(window_dir_text).resolve() if window_dir_text else None
    aggregate = dict(window.get("aggregate") or {})
    guardrails = dict(window.get("guardrails") or {})
    window_id = str(window.get("window_id") or aggregate.get("window_id") or guardrails.get("window_id") or "").strip()
    status = str(guardrails.get("status") or aggregate.get("window_status", {}).get("status") or aggregate.get("status") or "").strip()
    if not status:
        status = "GO" if bool(guardrails.get("passed", False)) else "HOLD"
    passed = bool(guardrails.get("passed", status == "GO"))
    failures = list(guardrails.get("failures") or aggregate.get("window_status", {}).get("failures") or [])
    aggregate_path = window_dir / "aggregate.json" if window_dir is not None else None
    guardrails_path = window_dir / "guardrails.json" if window_dir is not None else None
    divergence_path = window_dir / "divergence.csv" if window_dir is not None else None
    proposal_votes_path = window_dir / "proposal_votes.json" if window_dir is not None else None
    config_path = window_dir / "config.json" if window_dir is not None else None
    promotion_pack_path = window_dir / "promotion_pack.md" if window_dir is not None else None
    refs = {
        "aggregate": str(aggregate_path) if aggregate_path is not None else "",
        "guardrails": str(guardrails_path) if guardrails_path is not None else "",
        "divergence": str(divergence_path) if divergence_path is not None else "",
        "proposal_votes": str(proposal_votes_path) if proposal_votes_path is not None else "",
        "config": str(config_path) if config_path is not None else "",
        "promotion_pack": str(promotion_pack_path) if promotion_pack_path is not None else "",
    }
    refs = {str(key): str(value) for key, value in refs.items() if str(value).strip()}
    return {
        "window_id": window_id,
        "window_dir": str(window_dir) if window_dir is not None else "",
        "status": status,
        "passed": passed,
        "failures": [str(item) for item in failures if str(item).strip()],
        "metrics": _dict_payload(guardrails.get("metrics")) or _dict_payload(aggregate.get("comparison")),
        "aggregate_path": str(aggregate_path) if aggregate_path is not None else "",
        "guardrails_path": str(guardrails_path) if guardrails_path is not None else "",
        "divergence_path": str(divergence_path) if divergence_path is not None else "",
        "proposal_votes_path": str(proposal_votes_path) if proposal_votes_path is not None else "",
        "config_path": str(config_path) if config_path is not None else "",
        "promotion_pack_path": str(promotion_pack_path) if promotion_pack_path is not None else "",
        "artifact_refs": refs,
    }


def build_experiment_lineage(
    *,
    experiment_id: UUID | str,
    proposal_ref: str = "",
    review_ref: str = "",
    replay_refs: list[str] | None = None,
    paper_pack_ref: str = "",
    canary_pack_ref: str = "",
    promotion_decision_ref: str = "",
    rollback_plan_ref: str = "",
    release_manifest_ref: str = "",
    reflection_memory_ref: str = "",
    latest_stage: str = "",
    latest_promotion_id: str = "",
    approval_status: str = "",
    evidence_refs: list[str] | None = None,
    promotion_ids: list[str] | None = None,
    approval_event_ids: list[str] | None = None,
    partial_lineages: list[ExperimentLineage | dict[str, Any]] | None = None,
    updated_at: datetime | str | None = None,
) -> ExperimentLineage:
    lineage_inputs = [item if isinstance(item, ExperimentLineage) else ExperimentLineage.model_validate(item) for item in list(partial_lineages or [])]
    replay_refs_out = _stable_unique(
        [
            *[str(item) for item in list(replay_refs or [])],
            *[str(item) for lineage in lineage_inputs for item in list(lineage.replay_refs or [])],
        ]
    )
    evidence_refs_out = _stable_unique(
        [
            *[str(item) for item in list(evidence_refs or [])],
            *[str(item) for lineage in lineage_inputs for item in list(lineage.evidence_refs or [])],
        ]
    )
    promotion_ids_out = _stable_unique(
        [
            *[str(item) for item in list(promotion_ids or [])],
            *[str(item) for lineage in lineage_inputs for item in list(lineage.promotion_ids or [])],
        ]
    )
    approval_event_ids_out = _stable_unique(
        [
            *[str(item) for item in list(approval_event_ids or [])],
            *[str(item) for lineage in lineage_inputs for item in list(lineage.approval_event_ids or [])],
        ]
    )

    def _choose_scalar(explicit: str, attr: str) -> str:
        if str(explicit or "").strip():
            return str(explicit)
        for lineage in lineage_inputs:
            candidate = str(getattr(lineage, attr, "") or "").strip()
            if candidate:
                return candidate
        return ""

    lineage = ExperimentLineage(
        experiment_id=_coerce_uuid(experiment_id, namespace_seed="fxstack.orchestration.phase7.experiment_lineage"),
        proposal_ref=_choose_scalar(proposal_ref, "proposal_ref"),
        review_ref=_choose_scalar(review_ref, "review_ref"),
        replay_refs=replay_refs_out,
        paper_pack_ref=_choose_scalar(paper_pack_ref, "paper_pack_ref"),
        canary_pack_ref=_choose_scalar(canary_pack_ref, "canary_pack_ref"),
        promotion_decision_ref=_choose_scalar(promotion_decision_ref, "promotion_decision_ref"),
        rollback_plan_ref=_choose_scalar(rollback_plan_ref, "rollback_plan_ref"),
        release_manifest_ref=_choose_scalar(release_manifest_ref, "release_manifest_ref"),
        reflection_memory_ref=_choose_scalar(reflection_memory_ref, "reflection_memory_ref"),
        latest_stage=_choose_scalar(latest_stage, "latest_stage"),
        latest_promotion_id=_choose_scalar(latest_promotion_id, "latest_promotion_id"),
        approval_status=_choose_scalar(approval_status, "approval_status"),
        evidence_refs=evidence_refs_out,
        promotion_ids=promotion_ids_out,
        approval_event_ids=approval_event_ids_out,
        updated_at=_coerce_datetime(
            updated_at
            or next((lineage.updated_at for lineage in lineage_inputs if getattr(lineage, "updated_at", None) is not None), None)
        ),
    )
    return lineage


def build_experiment_bundle(
    *,
    experiment_id: UUID | str,
    profile_id: str,
    config_path: str = "",
    window_results: list[dict[str, Any]] | None = None,
    lineage: ExperimentLineage | dict[str, Any] | None = None,
    partial_lineages: list[ExperimentLineage | dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    windows = [collect_window_artifact(item) for item in list(window_results or [])]
    window_ids = [str(item.get("window_id") or "") for item in windows if str(item.get("window_id") or "").strip()]
    window_statuses = {str(item.get("window_id") or ""): str(item.get("status") or "") for item in windows if str(item.get("window_id") or "").strip()}
    passed_window_count = int(sum(1 for item in windows if bool(item.get("passed"))))
    overall_passed = bool(windows) and passed_window_count == len(windows)
    summary = {
        "experiment_id": str(experiment_id or ""),
        "profile_id": str(profile_id or ""),
        "config_path": str(config_path or ""),
        "window_count": len(windows),
        "passed_window_count": passed_window_count,
        "failed_window_count": max(0, len(windows) - passed_window_count),
        "window_ids": window_ids,
        "window_statuses": window_statuses,
        "passed": overall_passed,
        "status": "GO" if overall_passed else "HOLD",
    }
    resolved_lineage = (
        lineage if isinstance(lineage, ExperimentLineage) else ExperimentLineage.model_validate(lineage)
        if lineage is not None
        else None
    )
    if resolved_lineage is None:
        resolved_lineage = build_experiment_lineage(
            experiment_id=experiment_id,
            replay_refs=[ref for item in windows for ref in list(dict(item.get("artifact_refs") or {}).values()) if str(ref).strip()],
            evidence_refs=[ref for item in windows for ref in list(dict(item.get("artifact_refs") or {}).values()) if str(ref).strip()],
            partial_lineages=partial_lineages,
        )
    else:
        resolved_lineage = build_experiment_lineage(
            experiment_id=experiment_id,
            replay_refs=[ref for item in windows for ref in list(dict(item.get("artifact_refs") or {}).values()) if str(ref).strip()],
            evidence_refs=[ref for item in windows for ref in list(dict(item.get("artifact_refs") or {}).values()) if str(ref).strip()],
            partial_lineages=[resolved_lineage, *list(partial_lineages or [])],
        )
    lineage_payload = resolved_lineage.model_dump(mode="json")
    metadata_payload = _dict_payload(metadata)
    bundle_hash = hash_mapping(
        {
            "schema_version": PHASE7_EXPERIMENT_BUNDLE_SCHEMA_VERSION,
            "experiment_id": str(experiment_id or ""),
            "profile_id": str(profile_id or ""),
            "config_path": str(config_path or ""),
            "windows": windows,
            "lineage": lineage_payload,
            "summary": summary,
            "metadata": metadata_payload,
        }
    )
    evidence_refs = _stable_unique(
        [
            str(config_path or ""),
            *[ref for item in windows for ref in list(dict(item.get("artifact_refs") or {}).values())],
            *[str(item) for item in list(metadata_payload.get("evidence_refs") or [])],
            str(metadata_payload.get("promotion_pack_path") or ""),
        ]
    )
    return {
        "schema_version": PHASE7_EXPERIMENT_BUNDLE_SCHEMA_VERSION,
        "experiment_id": str(experiment_id or ""),
        "bundle_id": bundle_hash,
        "profile_id": str(profile_id or ""),
        "config_path": str(config_path or ""),
        "generated_at": _coerce_datetime(generated_at).isoformat() if generated_at is not None else "",
        "windows": windows,
        "window_count": len(windows),
        "summary": summary,
        "lineage": lineage_payload,
        "evidence_refs": evidence_refs,
        "metadata": metadata_payload,
    }


def render_experiment_promotion_pack(bundle: dict[str, Any]) -> str:
    bundle = dict(bundle or {})
    summary = dict(bundle.get("summary") or {})
    lineage = dict(bundle.get("lineage") or {})
    windows = list(bundle.get("windows") or [])
    lines = [
        f"# Phase 7 Experiment Bundle: {bundle.get('experiment_id') or ''}",
        "",
        f"- Bundle ID: `{bundle.get('bundle_id') or ''}`",
        f"- Profile ID: `{bundle.get('profile_id') or ''}`",
        f"- Config Path: `{bundle.get('config_path') or ''}`",
        f"- Window Count: `{summary.get('window_count', len(windows))}`",
        f"- Status: `{summary.get('status') or 'HOLD'}`",
        "",
        "## Windows",
        "",
    ]
    for window in windows:
        window_id = str(window.get("window_id") or "")
        status = str(window.get("status") or "")
        lines.append(f"- `{window_id}`: `{status}`")
    lines.extend(
        [
            "",
            "## Lineage",
            "",
            f"- Experiment ID: `{lineage.get('experiment_id') or bundle.get('experiment_id') or ''}`",
            f"- Proposal Ref: `{lineage.get('proposal_ref') or ''}`",
            f"- Promotion Decision Ref: `{lineage.get('promotion_decision_ref') or ''}`",
            f"- Replay Ref Count: `{len(list(lineage.get('replay_refs') or []))}`",
            f"- Evidence Ref Count: `{len(list(lineage.get('evidence_refs') or []))}`",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def write_experiment_bundle(bundle: dict[str, Any], out_dir: str | Path) -> dict[str, str]:
    bundle = dict(bundle or {})
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    lineage = dict(bundle.get("lineage") or {})
    summary = dict(bundle.get("summary") or {})
    out = {
        "bundle_dir": str(out_path),
        "experiment_bundle": _write_json(out_path / "experiment_bundle.json", bundle),
        "experiment_summary": _write_json(out_path / "experiment_summary.json", summary),
        "lineage": _write_json(out_path / "lineage.json", lineage),
    }
    (out_path / "promotion_pack.md").write_text(render_experiment_promotion_pack(bundle), encoding="utf-8")
    out["promotion_pack"] = str(out_path / "promotion_pack.md")
    return out


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _write_bundle_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return str(path)


def experiments_root(base_dir: str | Path | None = None) -> Path:
    if base_dir is not None and str(base_dir).strip():
        candidate = Path(base_dir).resolve()
        return candidate if candidate.name == "experiments" else (candidate / "experiments").resolve()
    settings = get_settings()
    return (Path(settings.project_root) / "artifacts" / "orchestration" / "experiments").resolve()


def experiment_bundle_root(*, experiment_id: str, base_dir: str | Path | None = None) -> Path:
    return experiments_root(base_dir) / str(experiment_id)


def _replay_experiment_dir(*, experiment_id: str, out_dir: str | Path) -> Path:
    return Path(out_dir).resolve() / str(experiment_id)


def _collect_window_inputs(*, experiment_id: str, window: str, out_dir: str | Path) -> list[dict[str, Any]]:
    experiment_dir = _replay_experiment_dir(experiment_id=experiment_id, out_dir=out_dir)
    if not experiment_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for child in sorted(experiment_dir.iterdir()):
        if not child.is_dir():
            continue
        if str(window or "").strip() not in {"", "all"} and child.name != str(window):
            continue
        aggregate = _read_json(child / "aggregate.json")
        guardrails = _read_json(child / "guardrails.json")
        if not aggregate and not guardrails:
            continue
        items.append(
            {
                "window_id": child.name,
                "window_dir": str(child.resolve()),
                "aggregate": aggregate,
                "guardrails": guardrails,
            }
        )
    return items


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


def _load_proposal(path: Path) -> ExperimentProposal:
    return ExperimentProposal.model_validate(_read_json(path))


def _update_lineage_file(
    *,
    bundle_root: Path,
    experiment_id: str,
    proposal: ExperimentProposal | None = None,
    review_ref: str = "",
    replay_refs: list[str] | None = None,
    paper_pack_ref: str = "",
    canary_pack_ref: str = "",
    promotion_decision_ref: str = "",
    rollback_plan_ref: str = "",
    release_manifest_ref: str = "",
    reflection_memory_ref: str = "",
    latest_stage: str = "",
    latest_promotion_id: str = "",
    approval_status: str = "",
    approval_event_ids: list[str] | None = None,
) -> dict[str, Any]:
    lineage_path = bundle_root / "experiment_lineage.json"
    current = _read_json(lineage_path)
    partials = [current] if current else []
    if proposal is not None:
        partials.append(
            {
                "experiment_id": str(proposal.experiment_id),
                "proposal_ref": str(bundle_root / "proposal.json"),
                "latest_stage": str(proposal.latest_stage or ""),
                "latest_promotion_id": str(proposal.latest_promotion_id or ""),
                "approval_status": str(proposal.approval_status or ""),
                "evidence_refs": [*list(proposal.evidence_refs or []), *list(proposal.input_artefact_refs or [])],
            }
        )
    lineage = build_experiment_lineage(
        experiment_id=experiment_id,
        review_ref=review_ref,
        replay_refs=replay_refs,
        paper_pack_ref=paper_pack_ref,
        canary_pack_ref=canary_pack_ref,
        promotion_decision_ref=promotion_decision_ref,
        rollback_plan_ref=rollback_plan_ref,
        release_manifest_ref=release_manifest_ref,
        reflection_memory_ref=reflection_memory_ref,
        latest_stage=latest_stage,
        latest_promotion_id=latest_promotion_id,
        approval_status=approval_status,
        approval_event_ids=approval_event_ids,
        partial_lineages=partials,
        updated_at=datetime.now(UTC),
    )
    payload = lineage.model_dump(mode="json")
    _write_bundle_json(lineage_path, payload)
    return payload


def draft_experiment(
    *,
    config_path: str,
    experiment_id: str,
    window: str = "all",
    out_dir: str,
    pair: str = "",
    author: str = "",
    note: str = "",
    bundle_run_id: str = "",
    manifest_path: str = "",
    promotion_pack_path: str = "",
) -> dict[str, Any]:
    from fxstack.orchestration.replay import load_replay_profile

    replay_inputs = _collect_window_inputs(experiment_id=experiment_id, window=window, out_dir=out_dir)
    profile_id = ""
    try:
        profile_id = str(load_replay_profile(config_path).profile_id or "")
    except Exception:
        profile_id = "unknown"
    bundle_root = experiment_bundle_root(experiment_id=experiment_id)
    metadata = {
        "artifact_root": str(bundle_root),
        "pair": str(pair or "").upper(),
        "author": str(author or ""),
        "note": str(note or ""),
        "bundle_run_id": str(bundle_run_id or ""),
        "manifest_path": str(manifest_path or ""),
        "promotion_pack_path": str(promotion_pack_path or ""),
        "evidence_refs": [str(manifest_path or ""), str(promotion_pack_path or "")],
    }
    bundle = build_experiment_bundle(
        experiment_id=experiment_id,
        profile_id=profile_id,
        config_path=config_path,
        window_results=replay_inputs,
        metadata=metadata,
        generated_at=datetime.now(UTC),
    )
    proposal = build_experiment_proposal(
        bundle=bundle,
        hypothesis=str(note or f"Phase 7 experiment {experiment_id}"),
        approval_status="draft",
    )
    proposal_payload = proposal.model_dump(mode="json")
    proposal_payload["artifact_root"] = str(bundle_root)
    proposal_payload["latest_stage"] = "drafted"
    proposal_payload["replay_window"] = str(window)
    proposal = ExperimentProposal.model_validate(proposal_payload)
    written = write_experiment_bundle(bundle, bundle_root)
    proposal_path = _write_bundle_json(bundle_root / "proposal.json", proposal.model_dump(mode="json"))
    reflection_memory_path = _write_bundle_json(
        bundle_root / "reflection_memory.json",
        {
            "schema_version": PHASE7_EXPERIMENT_BUNDLE_SCHEMA_VERSION,
            "experiment_id": str(experiment_id),
            "entries": [],
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    lineage = _update_lineage_file(
        bundle_root=bundle_root,
        experiment_id=experiment_id,
        proposal=proposal,
        latest_stage="drafted",
        approval_status=str(proposal.approval_status),
        reflection_memory_ref=reflection_memory_path,
    )
    service = _runtime_service()
    if service is not None:
        try:
            service.upsert_experiment_proposal(proposal.model_dump(mode="json"))
        except Exception:
            pass
    return {
        "ok": True,
        "experiment_id": str(experiment_id),
        "bundle_root": str(bundle_root),
        "proposal_path": proposal_path,
        "lineage_path": str(bundle_root / "experiment_lineage.json"),
        "reflection_memory_path": reflection_memory_path,
        "written": written,
        "lineage": lineage,
    }


def review_experiment(
    *,
    experiment_id: str,
    out_dir: str = "",
    author: str = "",
    note: str = "",
    decision: str = "approved",
) -> dict[str, Any]:
    bundle_root = experiment_bundle_root(experiment_id=experiment_id, base_dir=out_dir or None)
    proposal = _load_proposal(bundle_root / "proposal.json")
    review = {
        "experiment_id": str(experiment_id),
        "author": str(author or "reviewer"),
        "decision": str(decision or "approved").strip().lower(),
        "note": str(note or ""),
        "created_at": datetime.now(UTC).isoformat(),
    }
    review_path = _write_bundle_json(bundle_root / "review.json", review)
    updated_payload = proposal.model_dump(mode="json")
    updated_payload["approval_status"] = "approved" if review["decision"] == "approved" else "rejected" if review["decision"] == "rejected" else "draft"
    updated_payload["latest_stage"] = "reviewed"
    updated = ExperimentProposal.model_validate(updated_payload)
    _write_bundle_json(bundle_root / "proposal.json", updated.model_dump(mode="json"))
    service = _runtime_service()
    approval_event: dict[str, Any] | None = None
    if service is not None:
        try:
            service.upsert_experiment_proposal(updated.model_dump(mode="json"))
            approval_event = service.record_approval_event(
                subject_type="experiment",
                subject_id=str(experiment_id),
                approver=str(review["author"]),
                decision=str(review["decision"]),
                reason=str(review["note"]),
            )
        except Exception:
            approval_event = None
    lineage = _update_lineage_file(
        bundle_root=bundle_root,
        experiment_id=experiment_id,
        proposal=updated,
        review_ref=review_path,
        latest_stage="reviewed",
        approval_status=str(updated.approval_status),
        approval_event_ids=[str((approval_event or {}).get("event_id") or "")] if approval_event else [],
    )
    return {
        "ok": True,
        "experiment_id": str(experiment_id),
        "review_path": review_path,
        "approval_event": approval_event or {},
        "lineage": lineage,
    }


def replay_experiment(
    *,
    config_path: str,
    experiment_id: str,
    window: str = "all",
    out_dir: str,
    seed: int | None = None,
) -> dict[str, Any]:
    from fxstack.orchestration.replay import run_experiment

    result = run_experiment(
        config_path=config_path,
        experiment_id=experiment_id,
        window_name=window,
        out_dir=out_dir,
        seed=seed,
    )
    replay_inputs = _collect_window_inputs(experiment_id=experiment_id, window=window, out_dir=out_dir)
    bundle_root = experiment_bundle_root(experiment_id=experiment_id)
    lineage = _update_lineage_file(
        bundle_root=bundle_root,
        experiment_id=experiment_id,
        replay_refs=[ref for item in replay_inputs for ref in list(collect_window_artifact(item).get("artifact_refs", {}).values())],
        latest_stage="replayed",
    )
    return {
        "ok": True,
        "experiment_id": str(experiment_id),
        "result": result,
        "lineage": lineage,
    }


def paper_pack_experiment(
    *,
    experiment_id: str,
    out_dir: str = "",
    pair: str = "",
    bundle_run_id: str = "",
    manifest_path: str = "",
    promotion_pack_path: str = "",
    note: str = "",
) -> dict[str, Any]:
    bundle_root = experiment_bundle_root(experiment_id=experiment_id, base_dir=out_dir or None)
    paper_pack = {
        "experiment_id": str(experiment_id),
        "pair": str(pair or "").upper(),
        "bundle_run_id": str(bundle_run_id or ""),
        "manifest_path": str(manifest_path or ""),
        "promotion_pack_path": str(promotion_pack_path or ""),
        "note": str(note or ""),
        "status": "ready",
        "created_at": datetime.now(UTC).isoformat(),
    }
    paper_path = _write_bundle_json(bundle_root / "paper_pack.json", paper_pack)
    lineage = _update_lineage_file(
        bundle_root=bundle_root,
        experiment_id=experiment_id,
        paper_pack_ref=paper_path,
        latest_stage="paper_ready",
    )
    return {"ok": True, "experiment_id": str(experiment_id), "paper_pack_path": paper_path, "lineage": lineage}


def canary_pack_experiment(
    *,
    experiment_id: str,
    out_dir: str = "",
    manifest_path: str = "",
    promotion_pack_path: str = "",
    note: str = "",
) -> dict[str, Any]:
    bundle_root = experiment_bundle_root(experiment_id=experiment_id, base_dir=out_dir or None)
    canary_pack = {
        "experiment_id": str(experiment_id),
        "manifest_path": str(manifest_path or ""),
        "promotion_pack_path": str(promotion_pack_path or ""),
        "note": str(note or ""),
        "status": "ready",
        "created_at": datetime.now(UTC).isoformat(),
    }
    canary_path = _write_bundle_json(bundle_root / "canary_pack.json", canary_pack)
    lineage = _update_lineage_file(
        bundle_root=bundle_root,
        experiment_id=experiment_id,
        canary_pack_ref=canary_path,
        latest_stage="canary_ready",
    )
    return {"ok": True, "experiment_id": str(experiment_id), "canary_pack_path": canary_path, "lineage": lineage}


def promote_experiment(
    *,
    experiment_id: str,
    out_dir: str = "",
    manifest_path: str = "",
    promotion_pack_path: str = "",
) -> dict[str, Any]:
    from fxstack.orchestration.promotion import promote_experiment_bundle

    bundle_root = experiment_bundle_root(experiment_id=experiment_id, base_dir=out_dir or None)
    return promote_experiment_bundle(
        experiment_id=experiment_id,
        bundle_root=bundle_root,
        manifest_path=manifest_path,
        promotion_pack_path=promotion_pack_path,
    )


def trace_experiment(
    *,
    experiment_id: str,
    out_dir: str = "",
) -> dict[str, Any]:
    bundle_root = experiment_bundle_root(experiment_id=experiment_id, base_dir=out_dir or None)
    proposal = _read_json(bundle_root / "proposal.json")
    review = _read_json(bundle_root / "review.json")
    lineage = _read_json(bundle_root / "experiment_lineage.json")
    promotion = _read_json(bundle_root / "promotion_decision.json")
    rollback = _read_json(bundle_root / "rollback_plan.json")
    paper_pack = _read_json(bundle_root / "paper_pack.json")
    canary_pack = _read_json(bundle_root / "canary_pack.json")
    return {
        "ok": True,
        "experiment_id": str(experiment_id),
        "bundle_root": str(bundle_root),
        "proposal": proposal,
        "review": review,
        "lineage": lineage,
        "promotion_decision": promotion,
        "rollback_plan": rollback,
        "paper_pack": paper_pack,
        "canary_pack": canary_pack,
    }
