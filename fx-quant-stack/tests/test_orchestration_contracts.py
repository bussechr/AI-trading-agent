from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fxstack.orchestration.contracts import (
    AgentProposal,
    AgentTrace,
    DecisionContext,
    DecisionPacket,
    ExperimentLineage,
    ExperimentPromotion,
    ExperimentProposal,
    GovernedDecision,
    VersionBundle,
)
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION


def _load_schema(name: str) -> dict:
    root = Path(__file__).resolve().parents[2]
    path = root / "docs" / "schemas" / "orchestration" / name
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_store() -> dict[str, dict[str, Any]]:
    names = [
        "version_bundle.schema.json",
        "decision_context.schema.json",
        "agent_proposal.schema.json",
        "governed_decision.schema.json",
        "decision_packet.schema.json",
        "agent_trace.schema.json",
        "experiment_proposal.schema.json",
        "experiment_promotion.schema.json",
        "experiment_lineage.schema.json",
    ]
    return {name: _load_schema(name) for name in names}


def _resolve_ref(ref: str, schemas: dict[str, dict[str, Any]]) -> dict[str, Any]:
    target = ref.split("/")[-1]
    return dict(schemas[target])


def _assert_matches_schema(value: Any, schema: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    if "$ref" in schema:
        _assert_matches_schema(value, _resolve_ref(str(schema["$ref"]), schemas), schemas)
        return

    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        for candidate in expected_type:
            try:
                _assert_matches_schema(value, {**schema, "type": candidate}, schemas)
                return
            except AssertionError:
                continue
        raise AssertionError(f"value {value!r} did not match any of {expected_type!r}")

    if expected_type == "object":
        assert isinstance(value, dict)
        for key in list(schema.get("required") or []):
            assert key in value
        for key, prop_schema in dict(schema.get("properties") or {}).items():
            if key in value:
                _assert_matches_schema(value[key], dict(prop_schema), schemas)
        return

    if expected_type == "array":
        assert isinstance(value, list)
        item_schema = dict(schema.get("items") or {})
        for item in value:
            _assert_matches_schema(item, item_schema, schemas)
        return

    if expected_type == "string":
        assert isinstance(value, str)
        if "enum" in schema:
            assert value in list(schema.get("enum") or [])
        min_length = schema.get("minLength")
        if min_length is not None:
            assert len(value) >= int(min_length)
        return

    if expected_type == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        return

    if expected_type == "integer":
        assert isinstance(value, int) and not isinstance(value, bool)
        minimum = schema.get("minimum")
        if minimum is not None:
            assert value >= int(minimum)
        return

    if expected_type == "boolean":
        assert isinstance(value, bool)
        return

    if expected_type == "null":
        assert value is None
        return

    raise AssertionError(f"unsupported schema type: {expected_type!r}")


def test_phase0_contract_examples_validate_against_checked_in_json_schemas() -> None:
    schemas = _schema_store()
    version_bundle = VersionBundle(
        schema_version=ORCHESTRATION_SCHEMA_VERSION,
        policy_version="fxstack_policy_v1",
        model_bundle_version="bundle-v1",
        orchestrator_version="phase0",
    )
    context = DecisionContext(
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        cycle_id="cycle-1",
        thread_id="thread-1",
        correlation_id="corr-1",
        ts_utc=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
        pair="EURUSD",
        runtime_mode="shadow",
        tick={"bid": 1.1, "ask": 1.1002},
        feature_refs={"m5": "feature://m5"},
        live_signal={"score": 0.61},
        policy_state={"mode": "baseline"},
        portfolio_state={"gross_exposure": 0.2},
        risk_envelope={"max_gross_exposure": 1.0},
        runtime_state={"runtime_status": "running"},
        version_bundle=version_bundle,
    )
    proposal = AgentProposal(
        proposal_id=UUID("00000000-0000-0000-0000-000000000002"),
        run_id=context.run_id,
        agent_id="signal.trend_pullback",
        phase="shadow",
        intent="enter",
        side="BUY",
        confidence=0.71,
        expected_edge_bps=4.2,
        uncertainty=0.12,
        risk_cost=0.8,
        ttl_ms=250,
        evidence_refs=["snapshot://1"],
        constraints={"max_lots": 0.25},
        proposal_role="playbook_entry",
        normalized_score=2.4,
        score_components={"spread_penalty": 0.0},
        blocking_reasons=[],
        rationale="trend pullback aligned",
    )
    governed = GovernedDecision(
        decision_id=UUID("00000000-0000-0000-0000-000000000003"),
        run_id=context.run_id,
        allowed=False,
        selected_action="no_trade",
        command_preview=None,
        blocking_reasons=["shadow_only"],
        approval_state="auto",
        governor_version="phase0",
        invariants_ok=True,
        winning_proposal_id=str(proposal.proposal_id),
        ranked_proposal_ids=[str(proposal.proposal_id)],
        arbiter_stage="entry_ranking",
        arbiter_rationale="highest ranked proposal",
        score_path=[{"proposal_id": str(proposal.proposal_id), "rank": 1}],
        invariant_results={"hard_policy_block_suppresses_command": True},
    )
    packet = DecisionPacket(
        packet_id=UUID("00000000-0000-0000-0000-000000000004"),
        run_id=context.run_id,
        pair="EURUSD",
        ts_utc=context.ts_utc,
        baseline_action={"side": "BUY", "intent": "enter"},
        shadow_action={"side": "BUY", "intent": "enter", "action": "enter"},
        divergence_reason="agree",
        proposal_votes={"total": 1, "by_intent": {"enter": 1}, "by_side": {"BUY": 1}, "by_agent": {"signal.trend_pullback": "enter"}},
        fault_classification=None,
        proposals=[proposal],
        governed_decision=governed,
        latency_ms=32,
        fallback_used=False,
        trace_id="trace-1",
        schema_version=ORCHESTRATION_SCHEMA_VERSION,
        winning_proposal_id=str(proposal.proposal_id),
        ranked_proposal_ids=[str(proposal.proposal_id)],
        arbiter_stage="entry_ranking",
        arbiter_rationale="highest ranked proposal",
        score_path=[{"proposal_id": str(proposal.proposal_id), "rank": 1}],
        invariant_results={"hard_policy_block_suppresses_command": True},
    )
    trace = AgentTrace(
        trace_id="trace-1",
        run_id=context.run_id,
        node_spans=[{"node": "signal", "latency_ms": 8}],
        tool_calls=[],
        model_calls=[],
        persistence_refs=["run://1"],
        prompt_hashes=["sha256:abc"],
        input_hash="sha256:in",
        output_hash="sha256:out",
        error_class=None,
        created_at=context.ts_utc,
    )
    experiment = ExperimentProposal(
        experiment_id=UUID("00000000-0000-0000-0000-000000000005"),
        source_run_id=context.run_id,
        hypothesis="Agent routing improves parity diagnostics",
        change_set=[{"path": "fxstack/orchestration/contracts.py", "change": "add"}],
        evaluation_plan={"replay": "golden-pack"},
        risk_notes=["No live activation in phase 0"],
        evidence_refs=["snapshot://1"],
        prompt_hash="sha256:proposal",
        tool_trace_hash="sha256:trace",
        model_id="fxstack.phase7.proposal",
        decision_seed=13,
        input_artefact_refs=["artifact://proposal"],
        config_diff={"prompt": "redacted"},
        replay_window="2026-04-08T12:00:00Z/2026-04-09T12:00:00Z",
        artifact_root="/tmp/artifacts",
        latest_stage="draft",
        latest_promotion_id="",
        approval_status="draft",
    )
    promotion = ExperimentPromotion(
        promotion_id=UUID("00000000-0000-0000-0000-000000000006"),
        experiment_id=experiment.experiment_id,
        prompt_hash="sha256:proposal",
        tool_trace_hash="sha256:trace",
        model_id="fxstack.phase7.proposal",
        config_diff={"prompt": "redacted"},
        replay_window="2026-04-08T12:00:00Z/2026-04-09T12:00:00Z",
        replay_results={"status": "eligible"},
        approval_records=[{"event_id": "approval-1", "decision": "approved"}],
        paper_results={"status": "pass"},
        canary_results={"status": "pass"},
        release_manifest_ref="release://manifest-1",
        rollback_metadata={"enabled": False},
        artefact_hashes={"proposal": "sha256:proposal"},
        status="promoted",
        created_at=datetime(2026, 4, 8, 12, 5, tzinfo=UTC),
        updated_at=datetime(2026, 4, 8, 12, 6, tzinfo=UTC),
    )
    lineage = ExperimentLineage(
        experiment_id=experiment.experiment_id,
        proposal_ref="proposal://1",
        review_ref="review://1",
        replay_refs=["replay://1"],
        paper_pack_ref="paper://1",
        canary_pack_ref="canary://1",
        promotion_decision_ref="promotion://1",
        rollback_plan_ref="rollback://1",
        release_manifest_ref="release://manifest-1",
        reflection_memory_ref="memory://1",
        latest_stage="promoted",
        latest_promotion_id=str(promotion.promotion_id),
        approval_status="promoted",
        evidence_refs=["snapshot://1"],
        promotion_ids=[str(promotion.promotion_id)],
        approval_event_ids=["approval-1"],
        updated_at=datetime(2026, 4, 8, 12, 6, tzinfo=UTC),
    )

    samples = {
        "version_bundle.schema.json": version_bundle.model_dump(mode="json"),
        "decision_context.schema.json": context.model_dump(mode="json"),
        "agent_proposal.schema.json": proposal.model_dump(mode="json"),
        "governed_decision.schema.json": governed.model_dump(mode="json"),
        "decision_packet.schema.json": packet.model_dump(mode="json"),
        "agent_trace.schema.json": trace.model_dump(mode="json"),
        "experiment_proposal.schema.json": experiment.model_dump(mode="json"),
        "experiment_promotion.schema.json": promotion.model_dump(mode="json"),
        "experiment_lineage.schema.json": lineage.model_dump(mode="json"),
    }

    for name, payload in samples.items():
        _assert_matches_schema(payload, schemas[name], schemas)
