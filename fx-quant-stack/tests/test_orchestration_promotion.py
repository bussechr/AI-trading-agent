from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fxstack.orchestration.contracts import ExperimentLineage
from fxstack.orchestration.experiments import build_experiment_bundle
from fxstack.orchestration.promotion import (
    build_experiment_promotion,
    build_experiment_proposal,
    validate_promotion_bundle,
    write_promotion_artifacts,
)


def _window_result(tmp_path: Path, window_id: str, *, passed: bool = True, status: str = "GO") -> dict[str, object]:
    window_dir = tmp_path / window_id
    window_dir.mkdir(parents=True, exist_ok=True)
    aggregate = {"window_status": {"status": status, "passed": passed}, "comparison": {"parity_overlap": 0.99}}
    guardrails = {"status": status, "passed": passed, "failures": [], "metrics": {"parity_overlap": 0.99}}
    (window_dir / "aggregate.json").write_text(json.dumps(aggregate, indent=2), encoding="utf-8")
    (window_dir / "guardrails.json").write_text(json.dumps(guardrails, indent=2), encoding="utf-8")
    (window_dir / "divergence.csv").write_text("pair,ts\nEURUSD,2026-04-08T00:00:00Z\n", encoding="utf-8")
    (window_dir / "proposal_votes.json").write_text(json.dumps({"total": 1}, indent=2), encoding="utf-8")
    (window_dir / "config.json").write_text(json.dumps({"window_id": window_id}, indent=2), encoding="utf-8")
    (window_dir / "promotion_pack.md").write_text(f"# {window_id}\n", encoding="utf-8")
    return {
        "window_id": window_id,
        "window_dir": str(window_dir),
        "aggregate": aggregate,
        "guardrails": guardrails,
    }


def test_validate_and_build_promotion_artifacts(tmp_path: Path) -> None:
    experiment_id = UUID("00000000-0000-0000-0000-000000000703")
    lineage = ExperimentLineage(
        experiment_id=experiment_id,
        proposal_ref="proposal.json",
        review_ref="review.json",
        replay_refs=["replay-a.json"],
        promotion_decision_ref="promotion_decision.json",
        latest_stage="promotion",
        approval_status="approved",
        evidence_refs=["lineage.json"],
        promotion_ids=["promotion-1"],
        approval_event_ids=["approval-1"],
        updated_at=datetime(2026, 4, 8, 12, 40, tzinfo=UTC),
    )
    windows = [
        _window_result(tmp_path, "calm", passed=True, status="GO"),
        _window_result(tmp_path, "trend", passed=True, status="GO"),
    ]
    bundle = build_experiment_bundle(
        experiment_id=experiment_id,
        profile_id="unit",
        config_path=str(tmp_path / "config.json"),
        window_results=windows,
        lineage=lineage,
        metadata={"promotion_pack_path": str(tmp_path / "promotion_pack.md")},
        generated_at=datetime(2026, 4, 8, 12, 45, tzinfo=UTC),
    )

    validation = validate_promotion_bundle(bundle, require_paths_exist=True)
    assert validation["passed"] is True
    assert validation["status"] == "GO"
    assert validation["checks"]["bundle_has_windows"] is True
    assert validation["checks"]["referenced_paths_exist"] is True

    proposal = build_experiment_proposal(
        bundle=bundle,
        source_run_id=experiment_id,
        hypothesis="Promote stable replay bundle",
    )
    proposal_repeat = build_experiment_proposal(
        bundle=bundle,
        source_run_id=experiment_id,
        hypothesis="Promote stable replay bundle",
    )
    assert proposal.model_dump(mode="json") == proposal_repeat.model_dump(mode="json")
    assert proposal.approval_status == "approved"
    assert proposal.change_set and proposal.change_set[0]["window_id"] == "calm"
    assert proposal.evidence_refs[0] == str(tmp_path / "config.json")

    promotion = build_experiment_promotion(
        bundle=bundle,
        proposal=proposal,
        validation=validation,
        created_at=datetime(2026, 4, 8, 12, 46, tzinfo=UTC),
        updated_at=datetime(2026, 4, 8, 12, 47, tzinfo=UTC),
    )
    assert promotion.status == "GO"
    assert promotion.experiment_id == proposal.experiment_id
    assert promotion.artefact_hashes["bundle"]
    assert promotion.artefact_hashes["proposal"]
    assert promotion.artefact_hashes["validation"]

    written = write_promotion_artifacts(
        bundle=bundle,
        out_dir=tmp_path / "promotion",
        proposal=proposal,
        promotion=promotion,
        validation=validation,
    )
    assert Path(written["validation"]).exists()
    assert Path(written["experiment_proposal"]).exists()
    assert Path(written["promotion_decision"]).exists()
    assert Path(written["promotion_pack"]).exists()

    written_validation = json.loads(Path(written["validation"]).read_text(encoding="utf-8"))
    written_decision = json.loads(Path(written["promotion_decision"]).read_text(encoding="utf-8"))
    assert written_validation["status"] == "GO"
    assert written_decision["status"] == "GO"
    assert "Phase 7 Promotion Pack" in Path(written["promotion_pack"]).read_text(encoding="utf-8")


def test_validate_promotion_bundle_fails_closed_on_window_rejection(tmp_path: Path) -> None:
    experiment_id = UUID("00000000-0000-0000-0000-000000000704")
    lineage = ExperimentLineage(
        experiment_id=experiment_id,
        proposal_ref="proposal.json",
        updated_at=datetime(2026, 4, 8, 12, 50, tzinfo=UTC),
    )
    windows = [_window_result(tmp_path, "shock", passed=False, status="HOLD")]
    bundle = build_experiment_bundle(
        experiment_id=experiment_id,
        profile_id="unit",
        config_path=str(tmp_path / "config.json"),
        window_results=windows,
        lineage=lineage,
        generated_at=datetime(2026, 4, 8, 12, 55, tzinfo=UTC),
    )

    validation = validate_promotion_bundle(bundle, require_paths_exist=True)
    assert validation["passed"] is False
    assert validation["status"] == "HOLD"
    assert "summary_passed" in validation["failures"]
    assert "summary_go_status" in validation["failures"]

