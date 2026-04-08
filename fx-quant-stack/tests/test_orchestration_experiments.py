from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fxstack.orchestration.contracts import ExperimentLineage
from fxstack.orchestration.experiments import build_experiment_bundle, build_experiment_lineage, write_experiment_bundle


def _window_result(tmp_path: Path, window_id: str, *, passed: bool = True, status: str = "GO") -> dict[str, object]:
    window_dir = tmp_path / window_id
    window_dir.mkdir(parents=True, exist_ok=True)
    aggregate = {"window_status": {"status": status, "passed": passed}, "comparison": {"parity_overlap": 0.97}}
    guardrails = {"status": status, "passed": passed, "failures": [], "metrics": {"parity_overlap": 0.97}}
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


def test_build_experiment_lineage_stitches_partial_lineages_deterministically() -> None:
    experiment_id = UUID("00000000-0000-0000-0000-000000000701")
    partial_a = ExperimentLineage(
        experiment_id=experiment_id,
        proposal_ref="proposal-a.json",
        review_ref="review-a.json",
        replay_refs=["replay-a.json"],
        approval_status="draft",
        evidence_refs=["evidence-a.json"],
        promotion_ids=["promo-a"],
        approval_event_ids=["approval-a"],
        updated_at=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
    )
    partial_b = ExperimentLineage(
        experiment_id=experiment_id,
        proposal_ref="",
        review_ref="review-b.json",
        replay_refs=["replay-b.json", "replay-a.json"],
        promotion_decision_ref="promotion-b.json",
        latest_stage="canary",
        latest_promotion_id="promo-b",
        approval_status="approved",
        evidence_refs=["evidence-b.json"],
        promotion_ids=["promo-b"],
        approval_event_ids=["approval-b"],
        updated_at=datetime(2026, 4, 8, 12, 5, tzinfo=UTC),
    )

    lineage_one = build_experiment_lineage(
        experiment_id=experiment_id,
        proposal_ref="proposal-explicit.json",
        replay_refs=["replay-a.json", "replay-c.json"],
        evidence_refs=["evidence-explicit.json"],
        partial_lineages=[partial_a, partial_b],
        updated_at=datetime(2026, 4, 8, 12, 10, tzinfo=UTC),
    )
    lineage_two = build_experiment_lineage(
        experiment_id=experiment_id,
        proposal_ref="proposal-explicit.json",
        replay_refs=["replay-a.json", "replay-c.json"],
        evidence_refs=["evidence-explicit.json"],
        partial_lineages=[partial_a, partial_b],
        updated_at=datetime(2026, 4, 8, 12, 10, tzinfo=UTC),
    )

    assert lineage_one.model_dump(mode="json") == lineage_two.model_dump(mode="json")
    assert lineage_one.proposal_ref == "proposal-explicit.json"
    assert lineage_one.review_ref == "review-a.json"
    assert lineage_one.replay_refs == ["replay-a.json", "replay-c.json", "replay-b.json"]
    assert lineage_one.evidence_refs == ["evidence-explicit.json", "evidence-a.json", "evidence-b.json"]
    assert lineage_one.latest_stage == "canary"
    assert lineage_one.latest_promotion_id == "promo-b"
    assert lineage_one.approval_status == "draft"


def test_build_experiment_bundle_and_write_artifacts(tmp_path: Path) -> None:
    experiment_id = UUID("00000000-0000-0000-0000-000000000702")
    lineage = ExperimentLineage(
        experiment_id=experiment_id,
        proposal_ref="proposal.json",
        review_ref="review.json",
        replay_refs=["replay-a.json", "replay-b.json"],
        promotion_decision_ref="promotion_decision.json",
        latest_stage="promotion",
        approval_status="approved",
        evidence_refs=["evidence.json"],
        promotion_ids=["promotion-1"],
        approval_event_ids=["approval-1"],
        updated_at=datetime(2026, 4, 8, 12, 20, tzinfo=UTC),
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
        metadata={"evidence_refs": ["experiment://bundle"]},
        generated_at=datetime(2026, 4, 8, 12, 30, tzinfo=UTC),
    )
    repeat = build_experiment_bundle(
        experiment_id=experiment_id,
        profile_id="unit",
        config_path=str(tmp_path / "config.json"),
        window_results=windows,
        lineage=lineage,
        metadata={"evidence_refs": ["experiment://bundle"]},
        generated_at=datetime(2026, 4, 8, 12, 30, tzinfo=UTC),
    )

    assert bundle["bundle_id"] == repeat["bundle_id"]
    assert bundle["summary"]["status"] == "GO"
    assert bundle["window_count"] == 2
    assert bundle["lineage"]["approval_status"] == "approved"
    assert bundle["evidence_refs"][0] == str(tmp_path / "config.json")

    written = write_experiment_bundle(bundle, tmp_path / "bundle")
    assert Path(written["experiment_bundle"]).exists()
    assert Path(written["experiment_summary"]).exists()
    assert Path(written["lineage"]).exists()
    assert Path(written["promotion_pack"]).exists()

    disk_bundle = json.loads(Path(written["experiment_bundle"]).read_text(encoding="utf-8"))
    assert disk_bundle["bundle_id"] == bundle["bundle_id"]
    assert "Phase 7 Experiment Bundle" in Path(written["promotion_pack"]).read_text(encoding="utf-8")
