from __future__ import annotations

from src.trader.domain.decision_pipeline import DecisionPipeline


def test_decision_pipeline_stage_order_and_dispatch():
    pipe = DecisionPipeline()
    res = pipe.run_candidate(
        decision={"symbol": "EURUSD", "side": "BUY", "score": 0.8, "confidence": 72.0},
        diagnostics={
            "last_diag": {"p_trend": 0.66, "vol": 0.0022, "gate_penalty": 1.0},
            "governance": {"paused": False, "risk_scale": 1.0},
        },
        plugin_cfg={},
    )

    stages = [t.stage for t in res.traces]
    assert stages == [
        "feature_extraction",
        "model_scoring",
        "gating",
        "readiness",
        "sizing",
        "dispatch_intent",
    ]
    assert res.outcome.execution_ready is True
    assert str(res.outcome.metadata.get("intent")) == "ENTRY"
    assert float(res.outcome.metadata.get("lots", 0.0)) > 0.0


def test_decision_pipeline_rejects_blocked_candidate():
    pipe = DecisionPipeline()
    res = pipe.run_candidate(
        decision={"symbol": "EURUSD", "side": "BUY", "score": 0.7, "confidence": 70.0, "blocked_by": "spread_gate"},
        diagnostics={"last_diag": {"p_trend": 0.55, "vol": 0.0030}, "governance": {"paused": False, "risk_scale": 1.0}},
        plugin_cfg={},
    )

    assert res.outcome.execution_ready is False
    assert "spread_gate" in list(res.outcome.reasons)
    assert str(res.traces[-1].rejection_reason) == "spread_gate"


def test_decision_pipeline_plugin_fault_isolation():
    pipe = DecisionPipeline()

    def _boom(stage: str, candidate: dict, diag: dict):
        raise RuntimeError("boom")

    pipe._plugin_handlers["hawkes"] = _boom
    res = pipe.run_candidate(
        decision={"symbol": "GBPUSD", "side": "SELL", "score": 0.8, "confidence": 80.0},
        diagnostics={"last_diag": {"hawkes_n": 1.3, "p_trend": 0.4, "vol": 0.002}},
        plugin_cfg={"use_hawkes": True},
    )

    assert res.traces
    assert isinstance(res.plugin_errors, list)
    assert len(res.plugin_errors) >= 1
    assert str(res.plugin_errors[0].get("plugin")) == "hawkes"


def test_decision_pipeline_ai_plugin_adjusts_confidence():
    pipe = DecisionPipeline()
    res = pipe.run_candidate(
        decision={"symbol": "USDJPY", "side": "BUY", "score": 0.6, "confidence": 50.0},
        diagnostics={
            "last_diag": {"direction_hit_rate": 1.0, "p_trend": 0.6, "vol": 0.0018},
            "governance": {"paused": False, "risk_scale": 1.0},
        },
        plugin_cfg={"use_ai_indicator_model": True},
    )

    assert float(res.outcome.confidence) > 50.0
