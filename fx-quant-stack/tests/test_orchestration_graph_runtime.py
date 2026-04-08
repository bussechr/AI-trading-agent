from __future__ import annotations

from datetime import UTC, datetime
import threading
import time

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.orchestration.context_builder import build_decision_context, build_version_bundle
from fxstack.orchestration import graph_runtime as shadow_graph_runtime
from fxstack.orchestration.graph_runtime import ShadowGraphRuntime


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
    context = build_decision_context(
        pair="EURUSD",
        cycle_id="123",
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
            "symbol": "EURUSD",
            "pair": "EURUSD",
            "side": "BUY",
            "action": "enter",
            "intent": "enter",
            "score": 0.61,
            "position_open": False,
            "blocking_reasons": [],
            "command_preview": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1},
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
    call_count = {"count": 0}

    def _slow_invoke(state, config=None, durability=None):  # noqa: ANN001
        call_count["count"] += 1
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
    assert call_count["count"] == 1
    assert result.state["fault_classification"] == "latency_budget_exceeded"
    assert result.state["shadow_action"]["action"] == "no_trade"
    assert result.state["latency_budget_state"]["budget_exceeded"] is True
    assert result.state["node_spans"] == []
    assert service.bundles[0]["fallback_used"] is True
    assert service.bundles[0]["packet"]["fallback_used"] is True
    assert result.checkpoint_json["thread_id"] == "EURUSD:123:shadow"
    assert result.state["packet"]["fallback_used"] is True
    assert service.bundles[0]["fallback_used"] is True

    started_at_2 = time.perf_counter()
    result_2 = runtime.invoke(
        thread_id=str(state["thread_id"]),
        state=state,
        service=service,
        runtime_mode="shadow",
        durability="async",
    )
    elapsed_ms_2 = (time.perf_counter() - started_at_2) * 1000.0

    assert elapsed_ms_2 < 50.0
    assert call_count["count"] == 1
    assert result_2.state["fault_classification"] == "latency_budget_exceeded"
    release.set()
    for _ in range(50):
        if shadow_graph_runtime._SHADOW_INVOKE_FLIGHT is None:
            break
        time.sleep(0.01)
