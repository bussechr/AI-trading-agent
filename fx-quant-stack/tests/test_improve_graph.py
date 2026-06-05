"""LangGraph expression of the improvement loop: deterministic + improving."""

from __future__ import annotations

from fxstack.improve.evaluator import build_synthetic_dataset
from fxstack.improve.graph import ImprovementGraph, run_improvement_graph
from fxstack.improve.knobs import KNOBS_BY_NAME, default_config, knob_values


def test_graph_runs_and_improves():
    ds = build_synthetic_dataset(rows=3000, seed=42)
    out = run_improvement_graph(dataset=ds, seed=42, max_iterations=12)
    assert out["runner"] == "langgraph"
    assert out["best_objective"] >= out["baseline_objective"]
    assert isinstance(out["best_change_set"], dict)


def test_graph_is_deterministic():
    ds = build_synthetic_dataset(rows=2000, seed=7)
    a = run_improvement_graph(dataset=ds, seed=7, max_iterations=10)
    b = run_improvement_graph(dataset=ds, seed=7, max_iterations=10)
    assert a["best_change_set"] == b["best_change_set"]
    assert a["best_objective"] == b["best_objective"]
    assert a["accepted"] == b["accepted"]


def test_graph_only_touches_allowlisted_knobs_and_never_loosens_risk():
    ds = build_synthetic_dataset(rows=3000, seed=99)
    base_values = knob_values(default_config())
    out = run_improvement_graph(dataset=ds, seed=99, max_iterations=15)
    for name, value in out["best_change_set"].items():
        assert name in KNOBS_BY_NAME
        knob = KNOBS_BY_NAME[name]
        if knob.risk_locked and knob.safe_direction == "decrease":
            assert value <= base_values[name] + 1e-9


def test_graph_matches_iteration_count():
    ds = build_synthetic_dataset(rows=1500, seed=5)
    g = ImprovementGraph(dataset=ds, seed=5, max_iterations=8)
    out = g.run()
    # One entry per iteration (no baseline entry in the graph runner).
    assert len([e for e in out["entries"] if e.get("iteration", 0) >= 1]) == 8
