from __future__ import annotations

from datetime import UTC, datetime
import threading
import time
from uuid import uuid4

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.orchestration.context_builder import build_decision_context, build_version_bundle
from fxstack.orchestration import graph_runtime as shadow_graph_runtime
from fxstack.orchestration.graph_runtime import ShadowGraphRuntime
from fxstack.orchestration.contracts import AgentProposal


class _DummyService:
    def __init__(self) -> None:
        self.bundles: list[dict[str, object]] = []

    def store_orchestration_bundle(self, *, context, packet, trace, runtime_mode, fallback_used) -> None:
        self.bundles.append(
            {
                "context": context,
                "packet": packet,
                "trace": trace,
                "runtime_mode": runtime_mode,
                "fallback_used": fallback_used,
            }
        )


def _shadow_state() -> dict[str, object]:
    return _shadow_state_for(pair="EURUSD", cycle_id="123")


def _shadow_state_for(*, pair: str, cycle_id: str) -> dict[str, object]:
    context = build_decision_context(
        pair=pair,
        cycle_id=cycle_id,
        runtime_mode="shadow",
        tick={"bid": 1.1, "ask": 1.1002},
        feature_refs={"feature_ts": "2026-04-08T12:00:00+00:00"},
        live_signal={"side": "BUY", "score": 0.61, "confidence": 0.74, "trade_prob": 0.68, "expected_edge_bps": 4.2},
        policy_state={
            "execution_ready": True,
            "reasons": [],
            "position_open": False,
            "position_side": "",
            "lifecycle_action": "",
            "allocator_selected": True,
        },
        portfolio_state={"replacement_pressure": 0.1, "portfolio_posture": "balanced_probe"},
        risk_envelope={"governance": {"paused": False, "entries_only": False}},
        runtime_state={"runtime_status": "running", "decision_timeout_ms": 250, "max_node_ms": 50, "max_parallel_proposals": 8},
        version_bundle=build_version_bundle(policy_version="fxstack_policy_v1", model_bundle_version="bundle-v1"),
        ts_utc=datetime(2026, 4, 8, 12, 0, tzinfo=UTC),
    )
    return {
        "thread_id": context.thread_id,
        "run_id": str(context.run_id),
        "pair": context.pair,
        "cycle_id": context.cycle_id,
        "runtime_mode": context.runtime_mode,
        "trace_id": "trace-1",
        "decision_context": context.model_dump(mode="json"),
        "baseline_action": {
            "symbol": context.pair,
            "pair": context.pair,
            "side": "BUY",
            "action": "enter",
            "intent": "enter",
            "score": 0.61,
            "position_open": False,
            "blocking_reasons": [],
            "command_preview": {"cmd": "BUY", "symbol": context.pair, "lots": 0.1},
        },
        "latency_budget_state": {"cycle_budget_ms": 250, "max_node_ms": 50, "max_parallel_proposals": 8},
        "agent_proposals": [],
        "proposal_votes": {},
        "shadow_action": {},
        "divergence_reason": "",
        "blocking_reasons": [],
        "fault_classification": None,
        "node_spans": [],
        "tool_calls": [],
        "model_calls": [],
    }


def test_shadow_graph_runtime_builds_packet_trace_and_checkpoint() -> None:
    runtime = ShadowGraphRuntime()
    service = _DummyService()
    state = _shadow_state()
    result = runtime.invoke(
        thread_id=str(state["thread_id"]),
        state=state,
        service=service,
        runtime_mode="shadow",
        durability="async",
    )
    assert result.latency_ms >= 0
    assert result.state["shadow_action"]["action"] == "enter"
    assert result.state["divergence_reason"] == "agree"
    assert result.state["persisted"] is True
    assert result.checkpoint_json["thread_id"] == "EURUSD:123:shadow"
    assert result.state["packet"]["fallback_used"] is False
    assert result.state["node_spans"][-1]["node"] == "finalize_shadow_trace"
    assert service.bundles[0]["fallback_used"] is False
    assert service.bundles[0]["context"]["version_bundle"]["schema_version"] == ORCHESTRATION_SCHEMA_VERSION
    assert service.bundles[0]["packet"]["baseline_action"]["action"] == "enter"
    assert service.bundles[0]["packet"]["shadow_action"]["action"] == "enter"
    assert result.checkpoint_json["checkpoint"] is not None
    assert runtime.checkpointer.serialize_checkpoint(thread_id=str(state["thread_id"]))["checkpoint"] is None


def test_shadow_graph_runtime_faults_to_no_trade_and_persists_fault_classification(monkeypatch) -> None:
    runtime = ShadowGraphRuntime()
    service = _DummyService()
    monkeypatch.setattr(runtime._signal_agent, "propose", lambda inputs: (_ for _ in ()).throw(RuntimeError("boom")))
    state = _shadow_state()
    result = runtime.invoke(
        thread_id=str(state["thread_id"]),
        state=state,
        service=service,
        runtime_mode="shadow",
        durability="async",
    )
    assert result.state["fault_classification"] == "run_signal_agent_error"
    assert result.state["shadow_action"]["action"] == "no_trade"
    assert result.state["packet"]["fallback_used"] is True
    assert service.bundles[0]["fallback_used"] is True
    assert service.bundles[0]["packet"]["fault_classification"] == "run_signal_agent_error"


def test_shadow_graph_runtime_returns_bounded_timeout_fallback(monkeypatch) -> None:
    runtime = ShadowGraphRuntime()
    service = _DummyService()
    started = threading.Event()
    release = threading.Event()

    def _slow_invoke(state, config=None, durability=None):  # noqa: ANN001
        started.set()
        release.wait(timeout=1.0)
        return {
            "trace_id": str(state.get("trace_id") or "trace-timeout"),
            "shadow_action": {"action": "no_trade", "intent": "no_trade", "side": "FLAT"},
            "divergence_reason": "shadow_fault",
            "blocking_reasons": ["latency_budget_exceeded"],
            "proposal_votes": {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}},
            "fault_classification": "latency_budget_exceeded",
            "node_spans": [],
            "tool_calls": [],
            "model_calls": [],
            "packet": {},
            "trace": {},
            "persisted": False,
        }

    monkeypatch.setattr(runtime._compiled, "invoke", _slow_invoke)
    state = _shadow_state()
    state["latency_budget_state"]["cycle_budget_ms"] = 10
    state["latency_budget_state"]["max_node_ms"] = 5

    started_at = time.perf_counter()
    result = runtime.invoke(
        thread_id=str(state["thread_id"]),
        state=state,
        service=service,
        runtime_mode="shadow",
        durability="async",
    )
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0

    assert elapsed_ms < 250.0
    assert started.wait(timeout=0.1) is True
    assert abs(result.latency_ms - elapsed_ms) < 40.0
    assert result.state["fault_classification"] == "latency_budget_exceeded"
    assert result.state["shadow_action"]["action"] == "no_trade"
    assert result.state["latency_budget_state"]["budget_exceeded"] is True
    assert result.state["node_spans"] == []
    assert service.bundles[0]["fallback_used"] is True
    assert service.bundles[0]["packet"]["fallback_used"] is True
    assert result.checkpoint_json["thread_id"] == "EURUSD:123:shadow"
    assert result.state["packet"]["fallback_used"] is True
    assert service.bundles[0]["fallback_used"] is True

    release.set()
    for _ in range(50):
        if shadow_graph_runtime._SHADOW_INVOKE_FLIGHT is None:
            break
        time.sleep(0.01)


def test_shadow_graph_runtime_serializes_concurrent_invocations_without_synthetic_fault(monkeypatch) -> None:
    runtime = ShadowGraphRuntime()
    service = _DummyService()
    state = _shadow_state()
    state["latency_budget_state"]["cycle_budget_ms"] = 1000
    original_invoke = runtime._compiled.invoke
    first_started = threading.Event()
    release_first = threading.Event()
    call_count = {"count": 0}

    def _wrapped_invoke(state, config=None, durability=None):  # noqa: ANN001
        call_count["count"] += 1
        first_started.set()
        release_first.wait(timeout=1.0)
        return original_invoke(state, config=config, durability=durability)

    monkeypatch.setattr(runtime._compiled, "invoke", _wrapped_invoke)

    results: dict[str, object] = {}
    errors: list[BaseException] = []
    finished = {"first": threading.Event(), "second": threading.Event()}

    def _run(label: str) -> None:
        try:
            results[label] = runtime.invoke(
                thread_id=str(state["thread_id"]),
                state=state,
                service=service,
                runtime_mode="shadow",
                durability="async",
            )
        except BaseException as exc:  # pragma: no cover - failure would fail the assertions below
            errors.append(exc)
        finally:
            finished[label].set()

    first = threading.Thread(target=_run, args=("first",), daemon=True)
    second = threading.Thread(target=_run, args=("second",), daemon=True)
    first.start()
    assert first_started.wait(timeout=0.5) is True
    second.start()

    assert finished["second"].wait(timeout=0.05) is False
    release_first.set()

    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert call_count["count"] == 1
    assert finished["first"].is_set() is True
    assert finished["second"].is_set() is True

    first_result = results["first"]
    second_result = results["second"]
    assert isinstance(first_result, shadow_graph_runtime.GraphRuntimeResult)
    assert isinstance(second_result, shadow_graph_runtime.GraphRuntimeResult)
    assert first_result.state["fault_classification"] is None
    assert second_result.state["fault_classification"] is None
    assert first_result.state["packet"]["fallback_used"] is False
    assert second_result.state["packet"]["fallback_used"] is False
    assert first_result.state["trace_id"] == second_result.state["trace_id"]
    assert first_result.latency_ms >= 0
    assert second_result.latency_ms >= 0
    assert abs(first_result.latency_ms - second_result.latency_ms) < 40
    assert len(service.bundles) == 1

    for _ in range(50):
        if not shadow_graph_runtime._SHADOW_INVOKE_FLIGHTS:
            break
        time.sleep(0.01)


def test_shadow_graph_runtime_allows_unrelated_threads_to_run_concurrently(monkeypatch) -> None:
    runtime = ShadowGraphRuntime()
    service = _DummyService()
    state_a = _shadow_state_for(pair="EURUSD", cycle_id="123")
    state_b = _shadow_state_for(pair="GBPUSD", cycle_id="456")
    release_a = threading.Event()
    started_by_thread = {
        str(state_a["thread_id"]): threading.Event(),
        str(state_b["thread_id"]): threading.Event(),
    }
    call_count: dict[str, int] = {}
    errors: list[BaseException] = []

    def _invoke(state, config=None, durability=None):  # noqa: ANN001
        active_thread_id = str((config or {}).get("configurable", {}).get("thread_id") or state.get("thread_id") or "")
        call_count[active_thread_id] = call_count.get(active_thread_id, 0) + 1
        started_by_thread[active_thread_id].set()
        if active_thread_id == str(state_a["thread_id"]):
            release_a.wait(timeout=1.0)
        return {
            "trace_id": str(state.get("trace_id") or f"trace-{active_thread_id}"),
            "shadow_action": {"action": "hold", "intent": "hold", "side": "FLAT"},
            "divergence_reason": "agree",
            "blocking_reasons": [],
            "proposal_votes": {"total": 0, "by_intent": {}, "by_side": {}, "by_agent": {}},
            "fault_classification": None,
            "node_spans": [],
            "tool_calls": [],
            "model_calls": [],
            "packet": {},
            "trace": {},
            "persisted": False,
        }

    monkeypatch.setattr(runtime._compiled, "invoke", _invoke)

    def _run(state: dict[str, object]) -> None:
        try:
            runtime.invoke(
                thread_id=str(state["thread_id"]),
                state=state,
                service=service,
                runtime_mode="shadow",
                durability="async",
            )
        except BaseException as exc:  # pragma: no cover - failure would fail the assertions below
            errors.append(exc)

    first = threading.Thread(
        target=_run,
        args=(state_a,),
        daemon=True,
    )
    second = threading.Thread(
        target=_run,
        args=(state_b,),
        daemon=True,
    )
    first.start()
    assert started_by_thread[str(state_a["thread_id"])].wait(timeout=0.5) is True
    second.start()
    assert started_by_thread[str(state_b["thread_id"])].wait(timeout=0.5) is True
    release_a.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert call_count[str(state_a["thread_id"])] == 1
    assert call_count[str(state_b["thread_id"])] == 1


def test_shadow_graph_runtime_respects_configured_proposal_budget() -> None:
    class _TestCommitteeAgent:
        def __init__(self, index: int) -> None:
            self.agent_id = f"committee.test_{index}"
            self.agent_index = index

        def propose(self, inputs) -> AgentProposal:
            return AgentProposal(
                proposal_id=uuid4(),
                run_id=inputs.context.run_id,
                agent_id=self.agent_id,
                phase="committee",
                intent="enter",
                side="BUY",
                confidence=0.55 + (self.agent_index * 0.01),
                expected_edge_bps=4.2 + self.agent_index,
                uncertainty=0.05,
                risk_cost=0.0,
                ttl_ms=250,
                evidence_refs=[f"test://committee/{self.agent_id}"],
                constraints={"playbook": "trend_pullback"},
                proposal_role="playbook_entry",
                score_components={},
                blocking_reasons=[],
                rationale="test committee proposal",
            )

    runtime = ShadowGraphRuntime()
    runtime._committee_agents = tuple(_TestCommitteeAgent(i) for i in range(9))
    service = _DummyService()
    state = _shadow_state()
    state["latency_budget_state"]["max_parallel_proposals"] = 8

    result = runtime.invoke(
        thread_id=str(state["thread_id"]),
        state=state,
        service=service,
        runtime_mode="shadow",
        durability="async",
    )

    assert result.state["fault_classification"] == "proposal_budget_exceeded"
    assert result.state["proposal_votes"]["total"] == 8
    assert result.state["committee_summary"]["proposal_budget"]["max_parallel_proposals"] == 8
    assert result.state["committee_summary"]["proposal_budget"]["accepted_proposals"] == 8
