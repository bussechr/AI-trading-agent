from __future__ import annotations

from fxstack.belief.composer import compose_ranked_directional_belief


def test_ranked_belief_uses_best_opposite_side_as_opposition() -> None:
    belief = compose_ranked_directional_belief(
        pair="EURUSD",
        ts="2026-03-26T12:00:00Z",
        model_version="directional_belief_v2_test",
        source_mode="artifact",
        hypotheses=[
            {
                "scenario": "trend_pullback",
                "side": "long",
                "rank_margin": 2.5,
                "p_ev_above_hurdle": 0.82,
                "expected_net_ev_bps": 10.5,
                "p_confirm_success": 0.71,
                "p_fail_fast": 0.12,
                "scenario_regime_fit_prior": 1.0,
                "uncertainty_score": 0.10,
                "model_disagreement_score": 0.10,
                "extension_penalty_score": 0.15,
            },
            {
                "scenario": "breakout_expansion",
                "side": "long",
                "rank_margin": 2.1,
                "p_ev_above_hurdle": 0.75,
                "expected_net_ev_bps": 8.0,
                "p_confirm_success": 0.63,
                "p_fail_fast": 0.20,
                "scenario_regime_fit_prior": 0.55,
                "uncertainty_score": 0.12,
                "model_disagreement_score": 0.12,
                "extension_penalty_score": 0.20,
            },
            {
                "scenario": "failed_breakout_reversal",
                "side": "short",
                "rank_margin": 1.9,
                "p_ev_above_hurdle": 0.68,
                "expected_net_ev_bps": 6.2,
                "p_confirm_success": 0.57,
                "p_fail_fast": 0.19,
                "scenario_regime_fit_prior": 0.45,
                "uncertainty_score": 0.11,
                "model_disagreement_score": 0.09,
                "extension_penalty_score": 0.18,
            },
        ],
    )

    assert belief.primary_scenario == "trend_pullback"
    assert belief.primary_side == "long"
    assert belief.opposing_side == "short"
    assert belief.opposing_scenario == "failed_breakout_reversal"
    assert belief.primary_rank_score > 0.0
    assert belief.primary_ev_above_hurdle_prob > 0.0
    assert belief.belief_gap > 0.0
    assert belief.no_edge is False


def test_ranked_belief_derives_no_edge_from_weak_outcome_profile() -> None:
    belief = compose_ranked_directional_belief(
        pair="EURUSD",
        ts="2026-03-26T12:05:00Z",
        model_version="directional_belief_v2_test",
        source_mode="artifact",
        hypotheses=[
            {
                "scenario": "trend_pullback",
                "side": "long",
                "rank_margin": 0.1,
                "p_ev_above_hurdle": 0.30,
                "expected_net_ev_bps": 0.5,
                "p_confirm_success": 0.40,
                "p_fail_fast": 0.62,
                "scenario_regime_fit_prior": 0.20,
                "uncertainty_score": 0.30,
                "model_disagreement_score": 0.25,
                "extension_penalty_score": 0.20,
            },
            {
                "scenario": "trend_pullback",
                "side": "short",
                "rank_margin": 0.05,
                "p_ev_above_hurdle": 0.28,
                "expected_net_ev_bps": -0.4,
                "p_confirm_success": 0.38,
                "p_fail_fast": 0.58,
                "scenario_regime_fit_prior": 0.20,
                "uncertainty_score": 0.30,
                "model_disagreement_score": 0.25,
                "extension_penalty_score": 0.20,
            },
        ],
    )

    assert belief.no_edge is True
    assert belief.primary_scenario == "no_edge"
    assert belief.primary_side == ""
    assert belief.primary_score == 0.0


def test_ranked_belief_nonfinite_model_outputs_fail_to_finite_no_edge() -> None:
    belief = compose_ranked_directional_belief(
        pair="EURUSD",
        ts="2026-04-07T12:00:00Z",
        hypotheses=[
            {
                "scenario": "trend_pullback",
                "side": "long",
                "rank_margin": float("nan"),
                "p_ev_above_hurdle": float("inf"),
                "expected_net_ev_bps": float("-inf"),
                "p_confirm_success": float("nan"),
                "p_fail_fast": float("nan"),
                "scenario_regime_fit_prior": 1.0,
            }
        ],
        model_version="test",
        source_mode="artifact",
    )

    assert belief.no_edge is True
    assert belief.primary_score == 0.0
    assert belief.primary_ev_above_hurdle_prob == 0.0
