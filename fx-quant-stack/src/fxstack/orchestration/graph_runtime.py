"""Phase 2 shadow LangGraph runtime used by the live runtime seam."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import time
import threading
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from fxstack.orchestration.agents.base import AgentInputs
from fxstack.orchestration.agents.committee import (
    BreakoutExpansionAgent,
    ExecutionQualityAgent,
    PortfolioRiskAgent,
    RangeMeanReversionAgent,
    ReversalExitAgent,
    SpreadMicrostructureAgent,
    TrendPullbackAgent,
)
from fxstack.orchestration.agents.execution_shape import ExecutionShapeAgent
from fxstack.orchestration.agents.lifecycle import LifecycleAgent
from fxstack.orchestration.agents.portfolio import PortfolioAgent
from fxstack.orchestration.agents.risk import RiskAgent
from fxstack.orchestration.agents.signal import SignalAgent
from fxstack.orchestration.checkpointer import DurableCheckpointAdapter
from fxstack.orchestration.contracts import AgentProposal, AgentTrace, DecisionContext, DecisionPacket
from fxstack.orchestration.governor import build_proposal_votes, enrich_proposal_scores, govern_shadow
from fxstack.orchestration.packet_builder import (
    build_agent_trace,
    build_decision_packet,
    build_governed_decision_from_shadow,
)
from fxstack.orchestration.persistence import persist_orchestration_artifacts


class ShadowGraphState(TypedDict, total=False):
    thread_id: str
    run_id: str
    pair: str
    cycle_id: str
    runtime_mode: str
    trace_id: str
    decision_context: dict[str, Any]
    baseline_action: dict[str, Any]
    summary_proposals: dict[str, Any]
    agent_proposals: list[dict[str, Any]]
    ranked_proposals: list[dict[str, Any]]
    proposal_votes: dict[str, Any]
    shadow_action: dict[str, Any]
    divergence_reason: str
    blocking_reasons: list[str]
    fault_classification: str | None
    latency_budget_state: dict[str, Any]
    node_spans: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    model_calls: list[dict[str, Any]]
    command_preview: dict[str, Any]
    committee_summary: dict[str, Any]
    checkpoint_json: dict[str, Any]
    packet: dict[str, Any]
    trace: dict[str, Any]
    persisted: bool


def _copy_state(state: ShadowGraphState) -> ShadowGraphState:
    return dict(state or {})


@dataclass(slots=True)
class GraphRuntimeResult:
    state: dict[str, Any]
    checkpoint_json: dict[str, Any]
    latency_ms: int


@dataclass(slots=True)
class _ShadowInvokeFlight:
    thread_id: str = ""
    worker_done: threading.Event = field(default_factory=threading.Event)
    finalized: threading.Event = field(default_factory=threading.Event)
    result: GraphRuntimeResult | None = None
    error: BaseException | None = None
    final_result: GraphRuntimeResult | None = None
    final_error: BaseException | None = None
    thread: threading.Thread | None = None


_SHADOW_INVOKE_LOCK = threading.Lock()
_SHADOW_INVOKE_FLIGHT: _ShadowInvokeFlight | None = None
_SHADOW_INVOKE_FLIGHTS: dict[str, _ShadowInvokeFlight] = {}


def _sync_shadow_invoke_snapshot_locked() -> None:
    global _SHADOW_INVOKE_FLIGHT
    _SHADOW_INVOKE_FLIGHT = next(iter(_SHADOW_INVOKE_FLIGHTS.values()), None) if len(_SHADOW_INVOKE_FLIGHTS) == 1 else None


class ShadowGraphRuntime:
    def __init__(self) -> None:
        self.checkpointer = DurableCheckpointAdapter()
        self._signal_agent = SignalAgent()
        self._risk_agent = RiskAgent()
        self._portfolio_agent = PortfolioAgent()
        self._lifecycle_agent = LifecycleAgent()
        self._committee_agents = (
            TrendPullbackAgent(),
            RangeMeanReversionAgent(),
            BreakoutExpansionAgent(),
            ReversalExitAgent(),
            SpreadMicrostructureAgent(),
            PortfolioRiskAgent(),
            ExecutionQualityAgent(),
        )
        self._execution_shape_agent = ExecutionShapeAgent()
        self._service: Any = None
        self._persist_runtime_mode = "shadow"
        self._persist_fallback_used = False

        graph = StateGraph(ShadowGraphState)
        graph.add_node("assemble_context", self._wrap_node("assemble_context", self._assemble_context))
        graph.add_node("run_signal_agent", self._wrap_node("run_signal_agent", self._run_signal_agent))
        graph.add_node("run_risk_agent", self._wrap_node("run_risk_agent", self._run_risk_agent))
        graph.add_node("run_portfolio_agent", self._wrap_node("run_portfolio_agent", self._run_portfolio_agent))
        graph.add_node("run_lifecycle_agent", self._wrap_node("run_lifecycle_agent", self._run_lifecycle_agent))
        graph.add_node("run_committee_agents", self._wrap_node("run_committee_agents", self._run_committee_agents))
        graph.add_node("aggregate_packet", self._wrap_node("aggregate_packet", self._aggregate_packet))
        graph.add_node("govern_shadow", self._wrap_node("govern_shadow", self._govern_shadow))
        graph.add_node("finalize_shadow_trace", self._wrap_node("finalize_shadow_trace", self._finalize_shadow_trace))
        graph.add_edge(START, "assemble_context")
        graph.add_edge("assemble_context", "run_signal_agent")
        graph.add_edge("run_signal_agent", "run_risk_agent")
        graph.add_edge("run_risk_agent", "run_portfolio_agent")
        graph.add_edge("run_portfolio_agent", "run_lifecycle_agent")
        graph.add_edge("run_lifecycle_agent", "run_committee_agents")
        graph.add_edge("run_committee_agents", "aggregate_packet")
        graph.add_edge("aggregate_packet", "govern_shadow")
        graph.add_edge("govern_shadow", "finalize_shadow_trace")
        graph.add_edge("finalize_shadow_trace", END)
        self._compiled = graph.compile(checkpointer=self.checkpointer.saver)

    @staticmethod
    def _hash_payload(value: Any) -> str:
        raw = repr(value).encode("utf-8")
        return f"sha256:{hashlib.sha256(raw).hexdigest()}"

    @staticmethod
    def _shadow_cycle_timeout_ms(state: dict[str, Any]) -> int:
        budget = dict((state or {}).get("latency_budget_state") or {})
        raw_timeout = budget.get("cycle_budget_ms") or budget.get("decision_timeout_ms") or 250
        try:
            return max(1, int(float(raw_timeout)))
        except Exception:
            return 250

    @staticmethod
    def _shadow_timeout_result(
        *,
        thread_id: str,
        state: dict[str, Any],
        elapsed_ms: int,
    ) -> GraphRuntimeResult:
        out_state = dict(state or {})
        trace_id = str(out_state.get("trace_id") or f"orch-{uuid4()}")
        out_state["trace_id"] = trace_id
        shadow_action = {
            "action": "no_trade",
            "intent": "no_trade",
            "side": "FLAT",
            "confidence": 0.0,
            "advisory_only": True,
        }
        out_state["shadow_action"] = dict(shadow_action)
        out_state["divergence_reason"] = "shadow_fault"
        out_state["blocking_reasons"] = ["latency_budget_exceeded"]
        out_state["proposal_votes"] = dict(out_state.get("proposal_votes") or {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}})
        out_state["fault_classification"] = "latency_budget_exceeded"
        context_payload = dict(out_state.get("decision_context") or {})
        baseline_action = dict(out_state.get("baseline_action") or {})
        if context_payload:
            context = DecisionContext.model_validate(context_payload)
            governed = build_governed_decision_from_shadow(
                context=context,
                shadow_action=shadow_action,
                blocking_reasons=list(out_state.get("blocking_reasons") or []),
                command_preview={},
            )
            trace = build_agent_trace(
                context=context,
                trace_id=trace_id,
                node_spans=list(out_state.get("node_spans") or []),
                tool_calls=list(out_state.get("tool_calls") or []),
                model_calls=list(out_state.get("model_calls") or []),
                persistence_refs=[f"run://{context.run_id}", f"trace://{trace_id}"],
                prompt_hashes=[],
                error_class="latency_budget_exceeded",
            )
            packet = build_decision_packet(
                context=context,
                baseline_action=baseline_action,
                shadow_action=shadow_action,
                proposals=[],
                divergence_reason="shadow_fault",
                proposal_votes=dict(out_state.get("proposal_votes") or {}),
                fault_classification="latency_budget_exceeded",
                governed_decision=governed,
                latency_ms=max(0, int(elapsed_ms)),
                trace_id=trace_id,
                fallback_used=True,
            )
            out_state["packet"] = packet.model_dump(mode="json")
            out_state["trace"] = trace.model_dump(mode="json")
        else:
            out_state["packet"] = {}
            out_state["trace"] = {}
        out_state["persisted"] = False
        budget = dict(out_state.get("latency_budget_state") or {})
        budget["budget_exceeded"] = True
        budget["fault_count"] = int(budget.get("fault_count", 0)) + 1
        out_state["latency_budget_state"] = budget
        return GraphRuntimeResult(
            state=out_state,
            checkpoint_json={"thread_id": str(thread_id), "checkpoint": None},
            latency_ms=max(0, int(elapsed_ms)),
        )

    def _invoke_unbounded(
        self,
        *,
        thread_id: str,
        state: dict[str, Any],
        service: Any,
        runtime_mode: str,
        fallback_used: bool = False,
        durability: str | None = None,
    ) -> GraphRuntimeResult:
        started = time.perf_counter()
        self._service = service
        self._persist_runtime_mode = str(runtime_mode or "shadow")
        self._persist_fallback_used = bool(fallback_used)
        config = {"configurable": {"thread_id": str(thread_id)}}
        out = self._compiled.invoke(
            dict(state),
            config=config,
            durability=str(durability or "async"),
        )
        latency_ms = int(round((time.perf_counter() - started) * 1000.0))
        checkpoint_json = self.checkpointer.serialize_checkpoint(thread_id=str(thread_id), clear=True)
        return GraphRuntimeResult(state=dict(out or {}), checkpoint_json=checkpoint_json, latency_ms=latency_ms)

    def persist_artifacts(
        self,
        *,
        state: dict[str, Any],
        service: Any,
        runtime_mode: str,
        fallback_used: bool,
        checkpoint_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        out_state = dict(state or {})
        context_payload = dict(out_state.get("decision_context") or {})
        packet_payload = dict(out_state.get("packet") or {})
        trace_payload = dict(out_state.get("trace") or {})
        checkpoint_payload = dict(checkpoint_json or {})
        thread_id = str(out_state.get("thread_id") or context_payload.get("thread_id") or "")
        if not checkpoint_payload:
            checkpoint_payload = self.checkpointer.serialize_checkpoint(thread_id=thread_id)
        if trace_payload:
            out_state["trace"] = {**trace_payload, "checkpoint": dict(checkpoint_payload or {})}
        if not (context_payload and packet_payload and trace_payload):
            out_state["persisted"] = False
            return out_state
        try:
            context = DecisionContext.model_validate(context_payload)
            packet = DecisionPacket.model_validate(packet_payload)
            trace = AgentTrace.model_validate(trace_payload)
            persist_orchestration_artifacts(
                service=service,
                context=context,
                packet=packet,
                trace=trace,
                checkpoint_json=checkpoint_payload,
                runtime_mode=str(runtime_mode or "shadow"),
                fallback_used=bool(fallback_used),
            )
            out_state["persisted"] = True
        except Exception:
            out_state["persisted"] = False
            out_state["fault_classification"] = out_state.get("fault_classification") or "persistence_error"
        return out_state

    def _budget_state(self, state: ShadowGraphState) -> dict[str, Any]:
        budget = dict(state.get("latency_budget_state") or {})
        budget.setdefault("cycle_budget_ms", 250)
        budget.setdefault("max_node_ms", 50)
        budget.setdefault("max_parallel_proposals", self._committee_parallel_budget_floor())
        budget["max_parallel_proposals"] = self._coerce_max_parallel_proposals(budget.get("max_parallel_proposals", self._committee_parallel_budget_floor()))
        budget.setdefault("cycle_started_at", time.perf_counter())
        budget.setdefault("node_latencies_ms", {})
        budget.setdefault("nodes_over_budget", [])
        budget.setdefault("budget_exceeded", False)
        budget.setdefault("fault_count", 0)
        return budget

    def _committee_parallel_budget_floor(self) -> int:
        return max(1, len(self._committee_agents))

    def _coerce_max_parallel_proposals(self, value: Any) -> int:
        try:
            parsed = int(value)
        except Exception:
            parsed = 0
        return int(parsed) if int(parsed) > 0 else self._committee_parallel_budget_floor()

    def _maybe_mark_budget_fault(self, state: ShadowGraphState, *, node_name: str, latency_ms: int) -> ShadowGraphState:
        out = _copy_state(state)
        budget = self._budget_state(out)
        budget["node_latencies_ms"][str(node_name)] = int(latency_ms)
        elapsed_ms = int(round((time.perf_counter() - float(budget["cycle_started_at"])) * 1000.0))
        if int(latency_ms) > int(budget["max_node_ms"]):
            budget["nodes_over_budget"] = list(dict.fromkeys([*list(budget["nodes_over_budget"]), str(node_name)]))
            budget["budget_exceeded"] = True
        if elapsed_ms > int(budget["cycle_budget_ms"]):
            budget["budget_exceeded"] = True
        out["latency_budget_state"] = budget
        if bool(budget["budget_exceeded"]) and not out.get("fault_classification"):
            out["fault_classification"] = "latency_budget_exceeded"
        return out

    def _append_tool_call(self, state: ShadowGraphState, *, tool: str, status: str) -> ShadowGraphState:
        out = _copy_state(state)
        calls = list(out.get("tool_calls") or [])
        calls.append({"tool": str(tool), "status": str(status)})
        out["tool_calls"] = calls
        return out

    def _with_fault(self, state: ShadowGraphState, *, node_name: str, exc: Exception) -> ShadowGraphState:
        out = _copy_state(state)
        out["fault_classification"] = out.get("fault_classification") or f"{node_name}_error"
        budget = self._budget_state(out)
        budget["fault_count"] = int(budget.get("fault_count", 0)) + 1
        out["latency_budget_state"] = budget
        out = self._append_tool_call(out, tool=node_name, status="error")
        return out

    def _wrap_node(self, node_name: str, func):
        def _wrapped(state: ShadowGraphState) -> ShadowGraphState:
            started = time.perf_counter()
            try:
                out = func(_copy_state(state))
                out = self._append_tool_call(out, tool=node_name, status="ok")
            except Exception as exc:  # pragma: no cover - fault-path exercised from tests via monkeypatch
                out = self._with_fault(state, node_name=node_name, exc=exc)
            latency_ms = int(round((time.perf_counter() - started) * 1000.0))
            node_spans = list(out.get("node_spans") or [])
            node_spans.append({"node": str(node_name), "latency_ms": int(latency_ms)})
            out["node_spans"] = node_spans
            return self._maybe_mark_budget_fault(out, node_name=node_name, latency_ms=latency_ms)

        return _wrapped

    @staticmethod
    def _approval_state_for_governed_decision(
        *,
        context: DecisionContext,
        selected_action: str,
        winning_proposal: AgentProposal | None,
        command_preview: dict[str, Any],
        fault_classification: str | None,
    ) -> str:
        require_human = bool(dict(context.runtime_state or {}).get("require_human_approval", True))
        action = str(selected_action or "").strip().lower()
        if not require_human or action not in {"enter", "exit", "reduce"}:
            return "auto"
        if fault_classification:
            return "required"
        if winning_proposal is None:
            return "required"
        if not list(winning_proposal.evidence_refs or []):
            return "required"
        if not dict(command_preview or {}):
            return "required"
        if action == "enter" and (
            float(winning_proposal.normalized_score) < 0.0 or float(winning_proposal.confidence) < 0.55
        ):
            return "required"
        return "auto"

    def _append_proposal(self, state: ShadowGraphState, proposal: AgentProposal) -> ShadowGraphState:
        out = _copy_state(state)
        proposals = list(out.get("agent_proposals") or [])
        max_proposals = self._coerce_max_parallel_proposals(self._budget_state(out).get("max_parallel_proposals", self._committee_parallel_budget_floor()))
        if len(proposals) >= max_proposals:
            out["fault_classification"] = out.get("fault_classification") or "proposal_budget_exceeded"
            out["blocking_reasons"] = list(dict.fromkeys([*list(out.get("blocking_reasons") or []), "proposal_budget_exceeded"]))
            out["committee_summary"] = {
                **dict(out.get("committee_summary") or {}),
                "proposal_budget": {
                    "max_parallel_proposals": int(max_proposals),
                    "accepted_proposals": int(len(proposals)),
                    "rejected_agent": str(proposal.agent_id),
                },
            }
            return out
        proposals.append(proposal.model_dump(mode="json"))
        out["agent_proposals"] = proposals
        return out

    def _assemble_context(self, state: ShadowGraphState) -> ShadowGraphState:
        out = _copy_state(state)
        out.setdefault("agent_proposals", [])
        out.setdefault("summary_proposals", {})
        out.setdefault("ranked_proposals", [])
        out.setdefault("proposal_votes", {})
        out.setdefault("shadow_action", {})
        out.setdefault("divergence_reason", "")
        out.setdefault("blocking_reasons", [])
        out.setdefault("fault_classification", None)
        out.setdefault("node_spans", [])
        out.setdefault("tool_calls", [])
        out.setdefault("model_calls", [])
        out.setdefault("command_preview", {})
        out.setdefault("committee_summary", {})
        out.setdefault("packet", {})
        out.setdefault("trace", {})
        out.setdefault("persisted", False)
        out["decision_context"] = dict(out.get("decision_context") or {})
        out["baseline_action"] = dict(out.get("baseline_action") or {})
        out["latency_budget_state"] = self._budget_state(out)
        return out

    def _context_inputs(self, state: ShadowGraphState) -> AgentInputs:
        return AgentInputs(
            context=DecisionContext.model_validate(dict(state.get("decision_context") or {})),
            baseline_action=dict(state.get("baseline_action") or {}),
            summary_proposals=dict(state.get("summary_proposals") or {}),
        )

    def _run_signal_agent(self, state: ShadowGraphState) -> ShadowGraphState:
        proposal = self._signal_agent.propose(self._context_inputs(state))
        out = _copy_state(state)
        summaries = dict(out.get("summary_proposals") or {})
        summaries[str(proposal.agent_id)] = proposal.model_dump(mode="json")
        out["summary_proposals"] = summaries
        return out

    def _run_risk_agent(self, state: ShadowGraphState) -> ShadowGraphState:
        proposal = self._risk_agent.propose(self._context_inputs(state))
        out = _copy_state(state)
        summaries = dict(out.get("summary_proposals") or {})
        summaries[str(proposal.agent_id)] = proposal.model_dump(mode="json")
        out["summary_proposals"] = summaries
        return out

    def _run_portfolio_agent(self, state: ShadowGraphState) -> ShadowGraphState:
        proposal = self._portfolio_agent.propose(self._context_inputs(state))
        out = _copy_state(state)
        summaries = dict(out.get("summary_proposals") or {})
        summaries[str(proposal.agent_id)] = proposal.model_dump(mode="json")
        out["summary_proposals"] = summaries
        return out

    def _run_lifecycle_agent(self, state: ShadowGraphState) -> ShadowGraphState:
        proposal = self._lifecycle_agent.propose(self._context_inputs(state))
        out = _copy_state(state)
        summaries = dict(out.get("summary_proposals") or {})
        summaries[str(proposal.agent_id)] = proposal.model_dump(mode="json")
        out["summary_proposals"] = summaries
        return out

    def _run_committee_agents(self, state: ShadowGraphState) -> ShadowGraphState:
        out = _copy_state(state)
        inputs = self._context_inputs(out)
        for agent in self._committee_agents:
            out = self._append_proposal(out, agent.propose(inputs))
        return out

    def _aggregate_packet(self, state: ShadowGraphState) -> ShadowGraphState:
        out = _copy_state(state)
        context = DecisionContext.model_validate(dict(out.get("decision_context") or {}))
        proposals = [AgentProposal.model_validate(item) for item in list(out.get("agent_proposals") or [])]
        ranked = enrich_proposal_scores(context=context, proposals=proposals)
        proposal_votes = build_proposal_votes(proposals=ranked, winning_proposal=ranked[0] if ranked else None)
        out["ranked_proposals"] = [proposal.model_dump(mode="json") for proposal in ranked]
        out["agent_proposals"] = [proposal.model_dump(mode="json") for proposal in ranked]
        out["proposal_votes"] = dict(proposal_votes or {})
        out["committee_summary"] = {
            **dict(out.get("committee_summary") or {}),
            "winning_agent": str(ranked[0].agent_id) if ranked else "",
            "winning_proposal_id": str(ranked[0].proposal_id) if ranked else "",
            "winning_score": float(ranked[0].normalized_score) if ranked else 0.0,
            "top_ranked_proposals": [
                {
                    "proposal_id": str(proposal.proposal_id),
                    "agent_id": str(proposal.agent_id),
                    "intent": str(proposal.intent),
                    "score": float(proposal.normalized_score),
                    "role": str(proposal.proposal_role or proposal.agent_id),
                }
                for proposal in ranked[:3]
            ],
        }
        return out

    def _govern_shadow(self, state: ShadowGraphState) -> ShadowGraphState:
        out = _copy_state(state)
        context = DecisionContext.model_validate(dict(out.get("decision_context") or {}))
        baseline_action = dict(out.get("baseline_action") or {})
        ranked_proposals = [AgentProposal.model_validate(item) for item in list(out.get("ranked_proposals") or out.get("agent_proposals") or [])]
        summary_proposals = dict(out.get("summary_proposals") or {})
        fault_classification = str(out.get("fault_classification") or "").strip() or None
        arbiter = govern_shadow(
            context=context,
            baseline_action=baseline_action,
            ranked_proposals=ranked_proposals,
            summary_proposals=summary_proposals,
            fault_classification=fault_classification,
        )
        shadow_action, command_preview, blocking_reasons = self._execution_shape_agent.shape(
            baseline_action=baseline_action,
            winning_proposal=arbiter.winning_proposal,
            selected_action=arbiter.selected_action,
            blocking_reasons=arbiter.blocking_reasons,
            fault_classification=fault_classification,
        )
        baseline_key = str(baseline_action.get("action") or baseline_action.get("intent") or "hold")
        shadow_key = str(shadow_action.get("action") or shadow_action.get("intent") or "no_trade")
        if fault_classification:
            divergence_reason = "shadow_fault"
        elif baseline_key == shadow_key:
            divergence_reason = "agree"
        elif baseline_key in {"enter", "entry"} and shadow_key == "no_trade":
            divergence_reason = "baseline_enter_shadow_block"
        elif baseline_key in {"no_trade", "hold"} and shadow_key == "enter":
            divergence_reason = "shadow_only_enter"
        elif baseline_key in {"exit", "reduce"} and shadow_key not in {"exit", "reduce"}:
            divergence_reason = "baseline_lifecycle_shadow_block"
        elif shadow_key in {"exit", "reduce"} and baseline_key not in {"exit", "reduce"}:
            divergence_reason = "shadow_lifecycle_divergence"
        else:
            divergence_reason = "action_mismatch"
        trace_id = str(out.get("trace_id") or f"orch-{uuid4()}")
        governed = build_governed_decision_from_shadow(
            context=context,
            shadow_action=shadow_action,
            blocking_reasons=blocking_reasons,
            command_preview=dict(command_preview or {}),
            winning_proposal=arbiter.winning_proposal,
            ranked_proposals=arbiter.ranked_proposals,
            arbiter_stage=arbiter.arbiter_stage,
            arbiter_rationale=arbiter.arbiter_rationale,
            score_path=arbiter.score_path,
            invariant_results=arbiter.invariant_results,
        )
        governed = governed.model_copy(
            update={
                "approval_state": self._approval_state_for_governed_decision(
                    context=context,
                    selected_action=arbiter.selected_action,
                    winning_proposal=arbiter.winning_proposal,
                    command_preview=dict(command_preview or {}),
                    fault_classification=fault_classification,
                )
            }
        )
        latency_budget = dict(out.get("latency_budget_state") or {})
        total_latency_ms = int(round((time.perf_counter() - float(latency_budget.get("cycle_started_at", time.perf_counter()))) * 1000.0))
        trace = build_agent_trace(
            context=context,
            trace_id=trace_id,
            node_spans=list(out.get("node_spans") or []),
            tool_calls=list(out.get("tool_calls") or []),
            model_calls=list(out.get("model_calls") or []),
            persistence_refs=[f"run://{context.run_id}", f"trace://{trace_id}"],
            prompt_hashes=[],
            error_class=fault_classification,
        )
        packet = build_decision_packet(
            context=context,
            baseline_action=baseline_action,
            shadow_action=shadow_action,
            proposals=arbiter.ranked_proposals,
            divergence_reason=divergence_reason,
            proposal_votes=build_proposal_votes(proposals=arbiter.ranked_proposals, winning_proposal=arbiter.winning_proposal),
            fault_classification=fault_classification,
            governed_decision=governed,
            latency_ms=total_latency_ms,
            trace_id=trace_id,
            fallback_used=bool(fault_classification),
            winning_proposal=arbiter.winning_proposal,
            ranked_proposals=arbiter.ranked_proposals,
            arbiter_stage=arbiter.arbiter_stage,
            arbiter_rationale=arbiter.arbiter_rationale,
            score_path=arbiter.score_path,
            invariant_results=arbiter.invariant_results,
        )
        out["trace_id"] = trace_id
        out["shadow_action"] = dict(shadow_action or {})
        out["command_preview"] = dict(command_preview or {})
        out["divergence_reason"] = str(divergence_reason)
        out["blocking_reasons"] = list(blocking_reasons)
        out["proposal_votes"] = dict(packet.proposal_votes or {})
        out["committee_summary"] = {
            **dict(out.get("committee_summary") or {}),
            "winning_agent": str(arbiter.winning_proposal.agent_id) if arbiter.winning_proposal is not None else "",
            "winning_proposal_id": str(arbiter.winning_proposal.proposal_id) if arbiter.winning_proposal is not None else "",
            "winning_score": float(arbiter.winning_proposal.normalized_score) if arbiter.winning_proposal is not None else 0.0,
            "arbiter_stage": str(arbiter.arbiter_stage),
            "rationale": str(arbiter.arbiter_rationale),
            "blocking_reasons": list(blocking_reasons),
            "top_ranked_proposals": [
                {
                    "proposal_id": str(proposal.proposal_id),
                    "agent_id": str(proposal.agent_id),
                    "intent": str(proposal.intent),
                    "score": float(proposal.normalized_score),
                    "role": str(proposal.proposal_role or proposal.agent_id),
                }
                for proposal in arbiter.ranked_proposals[:3]
            ],
        }
        out["trace"] = trace.model_dump(mode="json")
        out["packet"] = packet.model_dump(mode="json")
        return out

    def _finalize_shadow_trace(self, state: ShadowGraphState) -> ShadowGraphState:
        # The durable write happens after invoke() returns; this node only finalizes
        # the in-graph packet/trace envelope so node spans do not imply the DB write.
        out = _copy_state(state)
        out["persisted"] = False
        return out

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return int(round((time.perf_counter() - float(started_at)) * 1000.0))

    @staticmethod
    def _state_with_cycle_started_at(state: dict[str, Any], *, started_at: float) -> dict[str, Any]:
        out = dict(state or {})
        budget = dict(out.get("latency_budget_state") or {})
        budget.setdefault("cycle_started_at", float(started_at))
        out["latency_budget_state"] = budget
        return out

    @staticmethod
    def _caller_view(result: GraphRuntimeResult, *, latency_ms: int) -> GraphRuntimeResult:
        return GraphRuntimeResult(
            state=dict(result.state or {}),
            checkpoint_json=dict(result.checkpoint_json or {}),
            latency_ms=max(0, int(latency_ms)),
        )

    def _finalize_shared_flight(
        self,
        *,
        thread_id: str,
        flight: _ShadowInvokeFlight,
        final_result: GraphRuntimeResult | None = None,
        final_error: BaseException | None = None,
    ) -> None:
        with _SHADOW_INVOKE_LOCK:
            if final_result is not None and flight.final_result is None:
                flight.final_result = final_result
            if final_error is not None and flight.final_error is None:
                flight.final_error = final_error
            flight.finalized.set()
            active = _SHADOW_INVOKE_FLIGHTS.get(str(thread_id))
            if active is flight and flight.worker_done.is_set():
                del _SHADOW_INVOKE_FLIGHTS[str(thread_id)]
            _sync_shadow_invoke_snapshot_locked()

    def _cleanup_completed_flight(self, *, thread_id: str, flight: _ShadowInvokeFlight) -> None:
        with _SHADOW_INVOKE_LOCK:
            active = _SHADOW_INVOKE_FLIGHTS.get(str(thread_id))
            if active is flight and flight.worker_done.is_set() and flight.finalized.is_set():
                del _SHADOW_INVOKE_FLIGHTS[str(thread_id)]
            _sync_shadow_invoke_snapshot_locked()

    def invoke(
        self,
        *,
        thread_id: str,
        state: dict[str, Any],
        service: Any,
        runtime_mode: str,
        fallback_used: bool = False,
        durability: str | None = None,
    ) -> GraphRuntimeResult:
        started = time.perf_counter()
        timeout_ms = self._shadow_cycle_timeout_ms(dict(state or {}))
        shared_state = self._state_with_cycle_started_at(dict(state or {}), started_at=started)
        owner = False

        with _SHADOW_INVOKE_LOCK:
            active = _SHADOW_INVOKE_FLIGHTS.get(str(thread_id))
            if active is None:
                owner = True
                flight = _ShadowInvokeFlight(thread_id=str(thread_id))

                def _run() -> None:
                    try:
                        flight.result = self._invoke_unbounded(
                            thread_id=thread_id,
                            state=shared_state,
                            service=service,
                            runtime_mode=runtime_mode,
                            fallback_used=fallback_used,
                            durability=durability,
                        )
                    except BaseException as exc:  # pragma: no cover - defensive; surfaced through invoke() below
                        flight.error = exc
                    finally:
                        self.checkpointer.serialize_checkpoint(thread_id=str(thread_id), clear=True)
                        flight.worker_done.set()
                        self._cleanup_completed_flight(thread_id=str(thread_id), flight=flight)

                worker = threading.Thread(target=_run, name=f"fxstack-shadow-graph:{thread_id}", daemon=True)
                flight.thread = worker
                _SHADOW_INVOKE_FLIGHTS[str(thread_id)] = flight
                _sync_shadow_invoke_snapshot_locked()
                worker.start()
            else:
                flight = active

        if owner:
            finished = flight.worker_done.wait(timeout_ms / 1000.0)
            elapsed_ms = self._elapsed_ms(started)
            if not finished:
                timed_out = self._shadow_timeout_result(thread_id=thread_id, state=shared_state, elapsed_ms=elapsed_ms)
                timed_out_state = self.persist_artifacts(
                    state=timed_out.state,
                    service=service,
                    runtime_mode=str(runtime_mode or "shadow"),
                    fallback_used=True,
                    checkpoint_json=timed_out.checkpoint_json,
                )
                final_result = GraphRuntimeResult(
                    state=timed_out_state,
                    checkpoint_json=timed_out.checkpoint_json,
                    latency_ms=max(0, int(elapsed_ms)),
                )
                self._finalize_shared_flight(thread_id=str(thread_id), flight=flight, final_result=final_result)
                return final_result
            if flight.error is not None:
                self._finalize_shared_flight(thread_id=str(thread_id), flight=flight, final_error=flight.error)
                raise flight.error
            if flight.result is None:  # pragma: no cover - defensive fallback
                timed_out = self._shadow_timeout_result(thread_id=thread_id, state=shared_state, elapsed_ms=elapsed_ms)
                timed_out_state = self.persist_artifacts(
                    state=timed_out.state,
                    service=service,
                    runtime_mode=str(runtime_mode or "shadow"),
                    fallback_used=True,
                    checkpoint_json=timed_out.checkpoint_json,
                )
                final_result = GraphRuntimeResult(
                    state=timed_out_state,
                    checkpoint_json=timed_out.checkpoint_json,
                    latency_ms=max(0, int(elapsed_ms)),
                )
                self._finalize_shared_flight(thread_id=str(thread_id), flight=flight, final_result=final_result)
                return final_result
            result_state = self.persist_artifacts(
                state=flight.result.state,
                service=service,
                runtime_mode=str(runtime_mode or "shadow"),
                fallback_used=bool(fallback_used or dict(flight.result.state or {}).get("fault_classification")),
                checkpoint_json=flight.result.checkpoint_json,
            )
            final_result = GraphRuntimeResult(
                state=result_state,
                checkpoint_json=flight.result.checkpoint_json,
                latency_ms=max(0, int(elapsed_ms)),
            )
            self._finalize_shared_flight(thread_id=str(thread_id), flight=flight, final_result=final_result)
            return final_result

        while not flight.finalized.is_set():
            elapsed_ms = self._elapsed_ms(started)
            remaining_ms = timeout_ms - elapsed_ms
            if remaining_ms <= 0:
                timed_out = self._shadow_timeout_result(thread_id=thread_id, state=shared_state, elapsed_ms=elapsed_ms)
                return GraphRuntimeResult(
                    state=timed_out.state,
                    checkpoint_json=timed_out.checkpoint_json,
                    latency_ms=max(0, int(elapsed_ms)),
                )
            flight.finalized.wait(remaining_ms / 1000.0)

        elapsed_ms = self._elapsed_ms(started)
        if flight.final_error is not None:
            raise flight.final_error
        if flight.final_result is not None:
            return self._caller_view(flight.final_result, latency_ms=elapsed_ms)
        timed_out = self._shadow_timeout_result(thread_id=thread_id, state=shared_state, elapsed_ms=elapsed_ms)
        return GraphRuntimeResult(
            state=timed_out.state,
            checkpoint_json=timed_out.checkpoint_json,
            latency_ms=max(0, int(elapsed_ms)),
        )
