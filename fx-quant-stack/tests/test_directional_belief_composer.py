from __future__ import annotations

from types import SimpleNamespace

from fxstack.belief.composer import compose_directional_belief, fragility_score, regime_fit_score


def test_compose_directional_belief_prefers_trend_pullback_long_in_trend() -> None:
    signal = SimpleNamespace(
        uncertainty_score=0.12,
        model_disagreement_score=0.10,
        extension_penalty_score=0.18,
    )

    belief = compose_directional_belief(
        pair="EURUSD",
        ts="2026-03-26T10:00:00Z",
        signal=signal,
        adaptive_meta={
            "environment_state": "PersistentTrend",
            "adaptive_playbook": "trend_pullback",
            "playbook_score": 0.82,
            "hostility_score": 0.08,
        },
        scenario_probs={
            "trend_pullback": 0.76,
            "range_mean_reversion": 0.08,
            "breakout_expansion": 0.10,
            "failed_breakout_reversal": 0.04,
            "no_edge": 0.02,
        },
        short_up_prob=0.58,
        trade_up_prob=0.71,
        structural_up_prob=0.77,
        model_version="belief-test-v1",
        source_mode="artifact",
    )

    assert belief.primary_scenario == "trend_pullback"
    assert belief.primary_side == "long"
    assert belief.primary_score > belief.opposing_score
    assert belief.belief_gap > 0.0
    assert belief.expected_confirmation_window_bars == 3
    assert belief.expected_path_shape == "pullback_then_resume"
    assert belief.invalidation_reason == "trigger_score_lt_0.35_or_trade_prob_lt_0.50"
    assert regime_fit_score("PersistentTrend", "trend_pullback") == 1.0


def test_fragility_penalizes_disagreeing_horizons() -> None:
    stable = fragility_score(
        uncertainty_score=0.10,
        model_disagreement_score=0.10,
        short_up_prob=0.60,
        trade_up_prob=0.62,
        structural_up_prob=0.64,
        extension_penalty_score=0.10,
        hostility_score=0.10,
    )
    fragile = fragility_score(
        uncertainty_score=0.65,
        model_disagreement_score=0.70,
        short_up_prob=0.85,
        trade_up_prob=0.35,
        structural_up_prob=0.70,
        extension_penalty_score=0.45,
        hostility_score=0.40,
    )

    assert stable < fragile
    assert fragile > 0.55
