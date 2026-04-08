"""Canonical orchestration contracts used by the live shadow runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VersionBundle(_StrictModel):
    schema_version: str
    policy_version: str
    model_bundle_version: str
    orchestrator_version: str


class DecisionContext(_StrictModel):
    run_id: UUID
    cycle_id: str
    thread_id: str
    correlation_id: str
    ts_utc: datetime
    pair: str
    runtime_mode: Literal["off", "shadow", "paper", "live"]
    tick: dict[str, Any]
    feature_refs: dict[str, Any]
    live_signal: dict[str, Any]
    policy_state: dict[str, Any]
    portfolio_state: dict[str, Any]
    risk_envelope: dict[str, Any]
    runtime_state: dict[str, Any]
    version_bundle: VersionBundle


class AgentProposal(_StrictModel):
    proposal_id: UUID
    run_id: UUID
    agent_id: str
    phase: str
    intent: Literal["enter", "exit", "reduce", "hold", "no_trade"]
    side: Literal["BUY", "SELL", "FLAT"]
    confidence: float
    expected_edge_bps: float
    uncertainty: float
    risk_cost: float
    ttl_ms: int = Field(ge=0)
    evidence_refs: list[str]
    constraints: dict[str, Any]
    advisory_only: bool = True
    proposal_role: str = ""
    normalized_score: float = 0.0
    score_components: dict[str, Any] = Field(default_factory=dict)
    blocking_reasons: list[str] = Field(default_factory=list)
    rationale: str = ""


class GovernedDecision(_StrictModel):
    decision_id: UUID
    run_id: UUID
    allowed: bool
    selected_action: str
    command_preview: dict[str, Any] | None = None
    blocking_reasons: list[str]
    approval_state: Literal["auto", "required", "approved", "rejected"]
    governor_version: str
    invariants_ok: bool
    winning_proposal_id: str = ""
    ranked_proposal_ids: list[str] = Field(default_factory=list)
    arbiter_stage: str = ""
    arbiter_rationale: str = ""
    score_path: list[dict[str, Any]] = Field(default_factory=list)
    invariant_results: dict[str, Any] = Field(default_factory=dict)


class DecisionPacket(_StrictModel):
    packet_id: UUID
    run_id: UUID
    pair: str
    ts_utc: datetime
    baseline_action: dict[str, Any]
    shadow_action: dict[str, Any]
    divergence_reason: str
    proposal_votes: dict[str, Any]
    fault_classification: str | None = None
    proposals: list[AgentProposal]
    governed_decision: GovernedDecision
    latency_ms: int = Field(ge=0)
    fallback_used: bool
    trace_id: str
    schema_version: str
    winning_proposal_id: str = ""
    ranked_proposal_ids: list[str] = Field(default_factory=list)
    arbiter_stage: str = ""
    arbiter_rationale: str = ""
    score_path: list[dict[str, Any]] = Field(default_factory=list)
    invariant_results: dict[str, Any] = Field(default_factory=dict)


class AgentTrace(_StrictModel):
    trace_id: str
    run_id: UUID
    node_spans: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    model_calls: list[dict[str, Any]]
    persistence_refs: list[str]
    prompt_hashes: list[str]
    input_hash: str
    output_hash: str
    error_class: str | None = None
    created_at: datetime


class ExperimentProposal(_StrictModel):
    experiment_id: UUID
    source_run_id: UUID | None = None
    hypothesis: str
    change_set: list[dict[str, Any]]
    evaluation_plan: dict[str, Any]
    risk_notes: list[str]
    evidence_refs: list[str]
    prompt_hash: str = ""
    tool_trace_hash: str = ""
    model_id: str = ""
    decision_seed: int = 0
    input_artefact_refs: list[str] = Field(default_factory=list)
    config_diff: dict[str, Any] = Field(default_factory=dict)
    replay_window: str = ""
    artifact_root: str = ""
    latest_stage: str = ""
    latest_promotion_id: str = ""
    approval_status: Literal["draft", "approved", "rejected", "promoted"]


class ExperimentPromotion(_StrictModel):
    promotion_id: UUID
    experiment_id: UUID
    prompt_hash: str = ""
    tool_trace_hash: str = ""
    model_id: str = ""
    config_diff: dict[str, Any] = Field(default_factory=dict)
    replay_window: str = ""
    replay_results: dict[str, Any] = Field(default_factory=dict)
    approval_records: list[dict[str, Any]] = Field(default_factory=list)
    paper_results: dict[str, Any] = Field(default_factory=dict)
    canary_results: dict[str, Any] = Field(default_factory=dict)
    release_manifest_ref: str = ""
    rollback_metadata: dict[str, Any] = Field(default_factory=dict)
    artefact_hashes: dict[str, str] = Field(default_factory=dict)
    status: str
    created_at: datetime
    updated_at: datetime


class ExperimentLineage(_StrictModel):
    experiment_id: UUID
    proposal_ref: str = ""
    review_ref: str = ""
    replay_refs: list[str] = Field(default_factory=list)
    paper_pack_ref: str = ""
    canary_pack_ref: str = ""
    promotion_decision_ref: str = ""
    rollback_plan_ref: str = ""
    release_manifest_ref: str = ""
    reflection_memory_ref: str = ""
    latest_stage: str = ""
    latest_promotion_id: str = ""
    approval_status: str = ""
    evidence_refs: list[str] = Field(default_factory=list)
    promotion_ids: list[str] = Field(default_factory=list)
    approval_event_ids: list[str] = Field(default_factory=list)
    updated_at: datetime
