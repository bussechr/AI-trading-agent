"""Multi-restart campaign search: keep the global OOS-validated best."""

from __future__ import annotations

from fxstack.improve.evaluator import build_synthetic_dataset
from fxstack.improve.loop import run_improvement_campaign, run_improvement_loop


def test_campaign_is_deterministic():
    ds = build_synthetic_dataset(rows=2000, seed=5)
    c1 = run_improvement_campaign(restarts=4, base_seed=5, dataset=ds, iterations=8, emit_experiment=True)
    c2 = run_improvement_campaign(restarts=4, base_seed=5, dataset=ds, iterations=8, emit_experiment=True)
    assert c1.best_seed == c2.best_seed
    assert c1.best.best_change_set == c2.best.best_change_set
    assert c1.summary["objective_by_seed"] == c2.summary["objective_by_seed"]


def test_campaign_best_is_at_least_any_single_restart():
    ds = build_synthetic_dataset(rows=2000, seed=9)
    campaign = run_improvement_campaign(restarts=5, base_seed=9, dataset=ds, iterations=10, emit_experiment=False)
    per_seed = campaign.summary["objective_by_seed"].values()
    # The campaign winner is the max over all restarts.
    assert campaign.best.best_objective >= max(float(v) for v in per_seed) - 1e-9


def test_campaign_winner_matches_replayed_single_run():
    ds = build_synthetic_dataset(rows=1500, seed=3)
    campaign = run_improvement_campaign(restarts=3, base_seed=3, dataset=ds, iterations=8, emit_experiment=False)
    # Replaying the winning seed alone reproduces the campaign's best (determinism).
    solo = run_improvement_loop(dataset=ds, base_config=None, seed=campaign.best_seed, iterations=8,
                                emit_experiment=False)
    assert solo.best_change_set == campaign.best.best_change_set
    assert solo.best_objective == campaign.best.best_objective


def test_campaign_emits_experiment_for_winner(tmp_path):
    ds = build_synthetic_dataset(rows=1500, seed=21)
    campaign = run_improvement_campaign(
        restarts=3, base_seed=21, dataset=ds, iterations=6,
        emit_experiment=True, register_experiment=True, experiment_base_dir=str(tmp_path),
    )
    assert campaign.best.experiment_proposal is not None
    assert campaign.best.registration is not None
    assert campaign.best.registration["ok"] is True
