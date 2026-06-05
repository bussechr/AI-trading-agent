"""Robustness analysis of tuned configs."""

from __future__ import annotations

from fxstack.improve.evaluator import build_synthetic_dataset
from fxstack.improve.knobs import default_config
from fxstack.improve.loop import run_improvement_loop
from fxstack.improve.robustness import robustness_report


def test_robustness_report_shape_and_bounds():
    ds = build_synthetic_dataset(rows=2000, seed=5)
    rep = robustness_report(default_config(), ds, knobs=["min_swing_prob", "min_entry_prob"])
    assert 0.0 < rep["robustness_score"] <= 1.0
    assert rep["max_drop"] >= 0.0
    assert set(rep["per_knob"]) == {"min_swing_prob", "min_entry_prob"}
    for k in rep["per_knob"].values():
        assert k["sensitivity"] >= 0.0


def test_robustness_is_deterministic():
    ds = build_synthetic_dataset(rows=1500, seed=9)
    a = robustness_report(default_config(), ds, knobs=["min_trade_prob"])
    b = robustness_report(default_config(), ds, knobs=["min_trade_prob"])
    assert a == b


def test_risk_locked_knob_perturbation_does_not_loosen():
    ds = build_synthetic_dataset(rows=1500, seed=3)
    cfg = default_config()  # default_order_lots already at the upper bound (0.10)
    rep = robustness_report(cfg, ds, knobs=["default_order_lots"])
    neighbours = rep["per_knob"]["default_order_lots"]["neighbours"]
    # The "up" direction loosens risk and is blocked by the allowlist, so only the
    # safe "down" perturbation is evaluated.
    assert "up" not in neighbours


def test_loop_summary_includes_robustness_when_changed():
    r = run_improvement_loop(iterations=12, seed=42)
    if r.best_change_set:
        assert "robustness" in r.summary
        assert "robustness_score" in r.summary["robustness"]
    else:
        assert r.summary["robustness"] == {}
