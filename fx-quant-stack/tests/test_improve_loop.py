"""End-to-end tests for the self-improvement loop (offline / deterministic)."""

from __future__ import annotations

import pandas as pd

from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config
from fxstack.improve.knobs import KNOBS_BY_NAME, default_config, knob_values
from fxstack.improve.loop import run_improvement_loop
from fxstack.improve.memory import ReflectionMemory
from fxstack.orchestration.contracts import ExperimentProposal


def test_loop_is_deterministic_for_fixed_seed():
    r1 = run_improvement_loop(iterations=10, seed=123, emit_experiment=True)
    r2 = run_improvement_loop(iterations=10, seed=123, emit_experiment=True)
    assert r1.best_change_set == r2.best_change_set
    assert r1.best_objective == r2.best_objective
    assert r1.experiment_proposal == r2.experiment_proposal


def test_loop_never_worsens_incumbent():
    r = run_improvement_loop(iterations=12, seed=7)
    # Incumbent only moves on strict improvement, so best >= baseline always.
    assert r.best_objective >= r.baseline_objective


def test_loop_only_touches_allowlisted_knobs_and_never_loosens_risk():
    base = default_config()
    base_values = knob_values(base)
    r = run_improvement_loop(iterations=15, seed=99)
    for name, value in r.best_change_set.items():
        assert name in KNOBS_BY_NAME, f"loop touched non-allowlisted knob {name}"
        knob = KNOBS_BY_NAME[name]
        if knob.risk_locked and knob.safe_direction == "decrease":
            assert value <= base_values[name] + 1e-9, f"{name} loosened a risk cap"


def test_experiment_proposal_conforms_to_contract():
    r = run_improvement_loop(iterations=6, seed=5, emit_experiment=True)
    assert r.experiment_proposal is not None
    # Round-trips through the strict pydantic contract without error.
    model = ExperimentProposal.model_validate(r.experiment_proposal)
    assert model.approval_status == "draft"
    assert model.decision_seed == 5
    assert isinstance(model.change_set, list)


def test_guardrails_block_acceptance_on_too_few_trades():
    # A tiny dataset can't clear min_trades, so nothing should be accepted.
    small = build_synthetic_dataset(rows=20, seed=3)
    r = run_improvement_loop(dataset=small, iterations=8, seed=3, min_trades=1000)
    assert r.accepted == 0
    assert r.best_objective == r.baseline_objective


def test_reflection_memory_persists_and_resumes(tmp_path):
    mem = tmp_path / "reflection_memory.jsonl"
    r1 = run_improvement_loop(iterations=6, seed=11, memory_path=str(mem), emit_experiment=False)
    assert mem.exists()
    loaded = ReflectionMemory(str(mem))
    assert len(loaded.entries()) >= 6
    # Resuming starts from the prior best and does not regress.
    r2 = run_improvement_loop(iterations=6, seed=11, memory_path=str(mem), emit_experiment=False)
    assert r2.best_objective >= r1.baseline_objective


def test_artifacts_written(tmp_path):
    r = run_improvement_loop(iterations=5, seed=21, artifact_dir=str(tmp_path), emit_experiment=True)
    assert (tmp_path / "best_config.json").exists()
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "proposal.json").exists()
    assert r.artifact_dir == str(tmp_path)


def test_evaluate_config_responds_to_gate_tightening():
    ds = build_synthetic_dataset(rows=3000, seed=8)
    loose = default_config()
    tight = default_config()
    tight["gates"]["min_swing_prob"] = 0.75
    tight["gates"]["min_entry_prob"] = 0.78
    m_loose = evaluate_config(loose, ds)
    m_tight = evaluate_config(tight, ds)
    # Tighter probability gates take strictly fewer trades.
    assert m_tight["trades"] < m_loose["trades"]


def test_empty_dataset_is_safe():
    r = evaluate_config(default_config(), pd.DataFrame())
    assert r["trades"] == 0.0
