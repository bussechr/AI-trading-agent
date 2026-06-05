"""Bridge from the self-improvement loop into the Phase-7 experiment factory.

The loop emits a contract-valid ``ExperimentProposal``; this module lands it in the
factory's canonical bundle directory (and best-effort into the runtime service) so
the existing ``review -> replay -> paper -> canary -> promote`` chain can act on it
unchanged. Nothing here promotes anything -- it only *registers a draft*. Promotion
still flows through the established gates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fxstack.orchestration.contracts import ExperimentProposal
from fxstack.orchestration.experiments import build_experiment_lineage, experiment_bundle_root


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return str(path)


def _best_effort_upsert(proposal_payload: dict[str, Any]) -> bool:
    """Upsert the proposal into the runtime service if one is reachable."""

    try:
        from fxstack.runtime.service import RuntimeService
        from fxstack.settings import get_settings

        settings = get_settings()
        service = RuntimeService(
            database_url=str(settings.database_url),
            execution_provider=str(settings.normalized_execution_provider),
        )
        service.upsert_experiment_proposal(dict(proposal_payload))
        return True
    except Exception:
        return False


def register_to_factory(
    *,
    experiment_id: str,
    proposal_payload: dict[str, Any],
    reflection_payload: dict[str, Any] | None = None,
    best_config: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    base_dir: str | Path | None = None,
    upsert_service: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write the loop's proposal into the factory bundle root and update lineage.

    Returns the written paths plus whether the service upsert succeeded. The proposal
    is validated against the strict contract before anything is written, so a
    malformed payload fails loudly here rather than deep in the promotion chain.
    """

    # Fail fast if the payload is not a valid ExperimentProposal.
    proposal = ExperimentProposal.model_validate(proposal_payload)
    bundle_root = experiment_bundle_root(experiment_id=experiment_id, base_dir=base_dir)
    ts = (now or datetime.now(UTC)).isoformat()

    written: dict[str, str] = {}
    written["proposal"] = _write_json(bundle_root / "proposal.json", proposal.model_dump(mode="json"))
    if reflection_payload is not None:
        written["reflection_memory"] = _write_json(bundle_root / "reflection_memory.json", reflection_payload)
    if best_config is not None:
        written["best_config"] = _write_json(bundle_root / "best_config.json", best_config)
    if summary is not None:
        written["summary"] = _write_json(bundle_root / "summary.json", summary)

    lineage = build_experiment_lineage(
        experiment_id=str(proposal.experiment_id),
        proposal_ref=written["proposal"],
        reflection_memory_ref=written.get("reflection_memory", ""),
        latest_stage="drafted",
        approval_status=str(proposal.approval_status),
        evidence_refs=[*list(proposal.evidence_refs or []), *written.values()],
        updated_at=ts,
    )
    written["lineage"] = _write_json(bundle_root / "experiment_lineage.json", lineage.model_dump(mode="json"))

    upserted = _best_effort_upsert(proposal.model_dump(mode="json")) if upsert_service else False
    return {
        "ok": True,
        "experiment_id": str(experiment_id),
        "bundle_root": str(bundle_root),
        "written": written,
        "service_upserted": bool(upserted),
    }
