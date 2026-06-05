"""Walk-forward overfit guard: the loop must reject curve-fit-only gains."""

from __future__ import annotations

from fxstack.improve.evaluator import build_regime_shift_dataset, build_synthetic_dataset, split_dataset
from fxstack.improve.loop import run_improvement_loop


def test_split_is_time_ordered_and_disjoint():
    ds = build_synthetic_dataset(rows=1000, seed=2)
    train, test = split_dataset(ds, oos_fraction=0.3)
    assert len(train) + len(test) == len(ds)
    assert len(test) > 0
    # Test slice is strictly later in time than train slice.
    assert train["ts"].max() <= test["ts"].min()


def test_split_disabled_returns_empty_test():
    ds = build_synthetic_dataset(rows=200, seed=2)
    train, test = split_dataset(ds, oos_fraction=0.0)
    assert len(train) == len(ds)
    assert len(test) == 0


def test_walk_forward_runs_and_reports_oos():
    r = run_improvement_loop(iterations=10, seed=4, oos_fraction=0.3)
    assert r.summary["oos_fraction"] == 0.3
    assert "incumbent_oos_objective" in r.summary
    assert r.experiment_proposal["evaluation_plan"]["walk_forward"]["enabled"] is True


def test_overfit_changes_are_rejected_under_regime_shift():
    # On a dataset whose edge structure flips out-of-sample, the loop WITH the
    # overfit guard accepts no more changes than one WITHOUT it.
    ds = build_regime_shift_dataset(rows=4000, seed=11)
    guarded = run_improvement_loop(dataset=ds, iterations=25, seed=11, oos_fraction=0.4, oos_tolerance=0.05,
                                   min_trades=10)
    unguarded = run_improvement_loop(dataset=ds, iterations=25, seed=11, oos_fraction=0.0, min_trades=10)
    assert guarded.accepted <= unguarded.accepted


def test_overfit_rejection_is_recorded_in_memory(tmp_path):
    ds = build_regime_shift_dataset(rows=4000, seed=11)
    mem = tmp_path / "m.jsonl"
    run_improvement_loop(dataset=ds, iterations=25, seed=11, oos_fraction=0.4, oos_tolerance=0.05,
                         min_trades=10, memory_path=str(mem), emit_experiment=False)
    text = mem.read_text(encoding="utf-8")
    assert "rejected_overfit" in text


def test_walk_forward_is_deterministic():
    ds = build_regime_shift_dataset(rows=2000, seed=7)
    r1 = run_improvement_loop(dataset=ds, iterations=12, seed=7, oos_fraction=0.3)
    r2 = run_improvement_loop(dataset=ds, iterations=12, seed=7, oos_fraction=0.3)
    assert r1.best_change_set == r2.best_change_set
    assert r1.summary["incumbent_oos_objective"] == r2.summary["incumbent_oos_objective"]
