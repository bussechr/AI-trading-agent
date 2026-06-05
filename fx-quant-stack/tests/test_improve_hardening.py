"""Regression tests for hardening fixes from the adversarial review."""

from __future__ import annotations

import numpy as np
import pandas as pd

from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config
from fxstack.improve.knobs import default_config
from fxstack.improve.loop import run_improvement_loop
from fxstack.improve.memory import ReflectionEntry, ReflectionMemory
from fxstack.improve.objective import score_metrics
from fxstack.improve.proposer import HeuristicProposer, ImprovementContext
from fxstack.llm.client import is_local_url


def test_heuristic_proposer_is_deterministic_at_unit_level():
    cfg = default_config()
    metrics = evaluate_config(cfg, build_synthetic_dataset(rows=500, seed=1))
    ctx = ImprovementContext(
        incumbent_config=cfg, incumbent_metrics=metrics, incumbent_objective=0.0,
        iteration=3, seed=99, recent_reflections=[], tried_signatures=set(),
    )
    p1 = HeuristicProposer().propose(ctx)
    p2 = HeuristicProposer().propose(ctx)
    assert p1.change_set == p2.change_set
    assert p1.hypothesis == p2.hypothesis


def test_drawdown_is_finite_on_extreme_negative_returns():
    # A pathological external row must not produce negative equity / NaN drawdown.
    df = pd.DataFrame(
        {
            "swing_prob": [0.9, 0.9, 0.9],
            "entry_prob": [0.9, 0.9, 0.9],
            "trade_prob": [0.9, 0.9, 0.9],
            "expected_edge_bps": [50.0, 50.0, 50.0],
            "spread_bps": [0.5, 0.5, 0.5],
            "fwd_ret_bps": [-100000.0, 20.0, 30.0],
        }
    )
    m = evaluate_config(default_config(), df)
    assert np.isfinite(m["max_drawdown_pct"])
    assert m["max_drawdown_pct"] >= 0.0


def test_objective_flags_non_finite_sharpe():
    score = score_metrics(
        {"trades": 100, "win_rate": 0.5, "mean_net_bps": 1.0, "total_net_bps": 100.0,
         "sharpe": float("nan"), "max_drawdown_pct": 1.0},
        min_trades=10, max_drawdown_pct=50.0,
    )
    assert score.passed_guardrails is False
    assert "non_finite_sharpe" in score.guardrail_failures


def test_summary_accepted_matches_result_accepted():
    r = run_improvement_loop(iterations=12, seed=42)
    assert r.summary["accepted"] == r.accepted
    assert "resume_adjusted" in r.summary


def test_bare_ipv6_loopback_is_detected_local():
    assert is_local_url("http://[::1]:11434")
    assert is_local_url("http://::1")  # bare IPv6 form recovered


def test_resume_surfaces_forced_tightening(tmp_path):
    mem = tmp_path / "m.jsonl"
    memory = ReflectionMemory(str(mem))
    # A prior 'best' that loosens a risk cap relative to a stricter base.
    memory.append(ReflectionEntry(
        iteration=1, hypothesis="loose", change_set={}, sanitized={"max_total_positions": 6},
        objective=5.0, accepted=True, reason="prior",
    ))
    stricter_base = default_config()
    stricter_base["risk"]["max_total_positions"] = 3
    r = run_improvement_loop(base_config=stricter_base, memory_path=str(mem), iterations=2, seed=1)
    # The resumed cap must have been forced back to the stricter base and recorded.
    assert any(a.get("reason") == "risk_loosening_blocked" for a in r.summary["resume_adjusted"])
