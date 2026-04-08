from __future__ import annotations

from types import SimpleNamespace
import threading
import time

from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.orchestration import graph_runtime as shadow_graph_runtime
from fxstack.runtime import runner


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


def _settings(**overrides):
    base = {
        "agent_mode": "shadow",
        "agent_decision_timeout_ms": 250,
        "agent_max_node_ms": 50,
        "agent_max_parallel_proposals": 8,
        "agent_durability": "async",
        "policy_version": "fxstack_policy_v1",
        "strategy_engine_mode": "supervised_legacy",
        "model_bundle_version": "",
        "agent_shadow_pair_allowlist": ["EURUSD"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _decision() -> dict[str, object]:
    return {
        "symbol": "EURUSD",
        "side": "BUY",
        "score": 0.64,
        "confidence": 0.72,
        "execution_ready": True,
        "reasons": [],
        "metadata": {
            "pair": "EURUSD",
            "trade_prob": 0.68,
            "expected_edge_bps": 4.1,
            "strategy_engine_mode": "supervised_legacy",
            "execution_mode": "strict_live_mirror",
            "allocator_selected": True,
            "approved_order": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1},
        },
    }


def test_capture_orchestration_cycle_runs_shadow_packet_end_to_end() -> None:
    svc = _DummyService()
    records, summary = runner._capture_orchestration_cycle(
        decisions=[_decision()],
        pending_entries=[{"index": 0, "pair": "EURUSD", "payload": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}}],
        pending_position_actions=[],
        svc=svc,
        settings=_settings(),
        loop_ts=1_744_112_000.0,
        state={"runtime_status": "running", "runtime_last_cycle_ts": 1_744_112_000.0},
        portfolio_state={"replacement_pressure": 0.1, "portfolio_posture": "balanced_probe"},
        governance={"paused": False, "entries_only": False},
        model_sets={},
    )
    assert records[0]["enabled"] is True
    assert records[0]["shadow_action"] == "enter"
    assert records[0]["schema_version"] == ORCHESTRATION_SCHEMA_VERSION
    assert summary["enabled"] is True
    assert summary["pair_count"] == 1
    assert summary["packet_count"] == 1
    assert summary["trace_count"] == 1
    assert summary["p95_ms"] <= 250
    assert svc.bundles[0]["packet"]["divergence_reason"] == "agree"
    assert svc.bundles[0]["packet"]["arbiter_stage"] in {"entry_ranking", "governor_final_decision"}
    assert svc.bundles[0]["packet"]["winning_proposal_id"]
    assert svc.bundles[0]["packet"]["score_path"]


def test_capture_orchestration_cycle_faults_to_no_trade_without_touching_live_path(monkeypatch) -> None:
    svc = _DummyService()
    runtime = runner._get_orchestration_graph_runtime()
    monkeypatch.setattr(runtime._signal_agent, "propose", lambda inputs: (_ for _ in ()).throw(RuntimeError("boom")))
    records, summary = runner._capture_orchestration_cycle(
        decisions=[_decision()],
        pending_entries=[{"index": 0, "pair": "EURUSD", "payload": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}}],
        pending_position_actions=[],
        svc=svc,
        settings=_settings(),
        loop_ts=1_744_112_100.0,
        state={"runtime_status": "running", "runtime_last_cycle_ts": 1_744_112_100.0},
        portfolio_state={"replacement_pressure": 0.1, "portfolio_posture": "balanced_probe"},
        governance={"paused": False, "entries_only": False},
        model_sets={},
    )
    assert records[0]["shadow_action"] == "no_trade"
    assert records[0]["fault_classification"] == "run_signal_agent_error"
    assert summary["fault_count"] == 1
    assert svc.bundles[0]["packet"]["fault_classification"] == "run_signal_agent_error"


def test_capture_orchestration_cycle_uses_bounded_shadow_timeout_when_graph_is_slow(monkeypatch) -> None:
    svc = _DummyService()
    runtime = runner._get_orchestration_graph_runtime()
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
    settings = _settings(agent_decision_timeout_ms=10, agent_max_node_ms=5)
    started_at = time.perf_counter()
    records, summary = runner._capture_orchestration_cycle(
        decisions=[_decision()],
        pending_entries=[{"index": 0, "pair": "EURUSD", "payload": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}}],
        pending_position_actions=[],
        svc=svc,
        settings=settings,
        loop_ts=1_744_112_400.0,
        state={"runtime_status": "running", "runtime_last_cycle_ts": 1_744_112_400.0},
        portfolio_state={"replacement_pressure": 0.1, "portfolio_posture": "balanced_probe"},
        governance={"paused": False, "entries_only": False},
        model_sets={},
    )
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0

    assert elapsed_ms < 250.0
    assert started.wait(timeout=0.1) is True
    assert call_count["count"] == 1
    assert records[0]["fallback_used"] is True
    assert records[0]["fault_classification"] == "latency_budget_exceeded"
    assert summary["fault_count"] >= 1
    assert summary["packet_count"] == 1
    assert summary["trace_count"] == 1
    assert len(svc.bundles) == 1
    assert svc.bundles[0]["fallback_used"] is True
    assert svc.bundles[0]["packet"]["fault_classification"] == "latency_budget_exceeded"
    assert svc.bundles[0]["packet"]["fallback_used"] is True
    release.set()
    for _ in range(50):
        if shadow_graph_runtime._SHADOW_INVOKE_FLIGHT is None:
            break
        time.sleep(0.01)


def test_capture_orchestration_cycle_skips_pairs_not_on_shadow_allowlist() -> None:
    svc = _DummyService()
    records, summary = runner._capture_orchestration_cycle(
        decisions=[_decision()],
        pending_entries=[{"index": 0, "pair": "EURUSD", "payload": {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1}}],
        pending_position_actions=[],
        svc=svc,
        settings=_settings(agent_shadow_pair_allowlist=["GBPUSD"]),
        loop_ts=1_744_112_200.0,
        state={"runtime_status": "running", "runtime_last_cycle_ts": 1_744_112_200.0},
        portfolio_state={"replacement_pressure": 0.1, "portfolio_posture": "balanced_probe"},
        governance={"paused": False, "entries_only": False},
        model_sets={},
    )
    assert records[0]["enabled"] is False
    assert records[0]["shadow_action"] == "disabled"
    assert records[0]["divergence_reason"] == "pair_not_allowlisted"
    assert records[0]["blocking_reasons"] == ["pair_not_allowlisted"]
    assert summary["pair_count"] == 0
    assert summary["packet_count"] == 0
    assert summary["trace_count"] == 0
    assert svc.bundles == []


def test_build_orchestration_snapshot_payload_preserves_phase1_refs_and_phase2_summary() -> None:
    phase1, shadow = runner._build_orchestration_snapshot_payload(
        orchestration_diag={
            "enabled": True,
            "agent_mode": "shadow",
            "schema_version": ORCHESTRATION_SCHEMA_VERSION,
            "pair_count": 1,
            "packet_count": 1,
            "trace_count": 1,
            "fault_count": 0,
        },
        records_by_index={
            0: {
                "enabled": True,
                "correlation_id": "EURUSD:123:shadow",
                "thread_id": "EURUSD:123:shadow",
                "fallback_used": False,
                "run_id": "run-1",
                "trace_id": "trace-1",
            }
        },
        phase2_sections={"adaptive_shadow_policy": {"candidate_count": 1}},
    )
    assert phase1["correlation_id"] == "EURUSD:123:shadow"
    assert phase1["thread_id"] == "EURUSD:123:shadow"
    assert phase1["run_id"] == "run-1"
    assert phase1["trace_id"] == "trace-1"
    assert shadow["pair_count"] == 1
    assert shadow["phase2"]["adaptive_shadow_policy"]["candidate_count"] == 1
