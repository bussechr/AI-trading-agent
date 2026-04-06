from __future__ import annotations

from types import SimpleNamespace

from fxstack.belief.types import DirectionalBelief
from fxstack.runtime.runner import _attach_directional_belief_shadow


def _base_decision() -> dict[str, object]:
    return {
        "symbol": "EURUSD",
        "side": "BUY",
        "metadata": {
            "pair": "EURUSD",
            "ts": "2026-03-26T12:00:00Z",
            "adaptive_playbook": "trend_pullback",
            "adaptive_environment_state": "PersistentTrend",
            "adaptive_playbook_score": 0.78,
            "adaptive_location_score": 0.66,
            "adaptive_trigger_score": 0.61,
            "adaptive_macro_coherence_score": 0.69,
            "adaptive_hostility_score": 0.08,
            "uncertainty_score": 0.12,
            "model_disagreement_score": 0.10,
            "extension_penalty_score": 0.15,
            "regime_prob": 0.81,
            "swing_prob": 0.72,
            "entry_prob": 0.63,
            "trade_prob": 0.69,
        },
    }


def test_runtime_belief_shadow_marks_missing_artifact() -> None:
    decisions = [_base_decision()]
    cycle, metrics = _attach_directional_belief_shadow(
        decisions=decisions,
        loaded_model_sets={"EURUSD": SimpleNamespace(belief_model=None)},
        adaptive_rows_by_pair={"EURUSD": {"environment_state": "PersistentTrend", "playbook": "trend_pullback"}},
        settings=SimpleNamespace(belief_shadow_enabled=True),
    )

    meta = decisions[0]["metadata"]
    assert meta["belief_source_mode"] == "artifact_missing"
    assert cycle["candidate_count_with_belief"] == 0
    assert metrics["belief_loaded_share"] == 0.0


def test_runtime_belief_shadow_attaches_loaded_belief(monkeypatch) -> None:
    decisions = [_base_decision()]

    def _fake_compute_directional_belief(**_: object) -> DirectionalBelief:
        return DirectionalBelief(
            pair="EURUSD",
            ts="2026-03-26T12:00:00Z",
            primary_side="long",
            primary_scenario="trend_pullback",
            primary_thesis="trend_pullback:long",
            primary_score=0.44,
            primary_rank_score=0.61,
            primary_ev_above_hurdle_prob=0.73,
            primary_expected_net_ev_bps=8.4,
            primary_confirm_prob=0.66,
            primary_fail_fast_prob=0.14,
            opposing_side="short",
            opposing_scenario="failed_breakout_reversal",
            opposing_thesis="failed_breakout_reversal:short",
            opposing_score=0.18,
            belief_gap=0.26,
            fragility_score=0.21,
            horizon_alignment_score=0.88,
            short_up_prob=0.58,
            trade_up_prob=0.69,
            structural_up_prob=0.74,
            regime_fit_score=1.0,
            expected_confirmation_window_bars=3,
            expected_path_shape="pullback_then_resume",
            invalidation_reason="trigger_score_lt_0.35_or_trade_prob_lt_0.50",
            model_version="belief-shadow-test",
            source_mode="artifact",
        )

    monkeypatch.setattr("fxstack.runtime.runner.compute_directional_belief", _fake_compute_directional_belief)
    cycle, metrics = _attach_directional_belief_shadow(
        decisions=decisions,
        loaded_model_sets={"EURUSD": SimpleNamespace(belief_model=object())},
        adaptive_rows_by_pair={"EURUSD": {"environment_state": "PersistentTrend", "playbook": "trend_pullback"}},
        settings=SimpleNamespace(belief_shadow_enabled=True),
    )

    meta = decisions[0]["metadata"]
    assert meta["belief_primary_scenario"] == "trend_pullback"
    assert meta["belief_primary_side"] == "long"
    assert meta["belief_model_version"] == "belief-shadow-test"
    assert meta["belief_primary_rank_score"] == 0.61
    assert meta["belief_primary_ev_above_hurdle_prob"] == 0.73
    assert meta["belief_primary_fail_fast_prob"] == 0.14
    assert cycle["candidate_count_with_belief"] == 1
    assert cycle["primary_scenario_counts"]["trend_pullback"] == 1
    assert cycle["avg_primary_rank_score"] == 0.61
    assert cycle["avg_primary_ev_above_hurdle_prob"] == 0.73
    assert cycle["avg_primary_expected_net_ev_bps"] == 8.4
    assert cycle["avg_primary_fail_fast_prob"] == 0.14
    assert cycle["no_edge_share"] == 0.0
    assert cycle["opposition_side_counts"]["short"] == 1
    assert metrics["belief_loaded_share"] == 1.0
    assert metrics["avg_primary_rank_score"] == 0.61
    assert metrics["opposition_side_counts"]["short"] == 1
