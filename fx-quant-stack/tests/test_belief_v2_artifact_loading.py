from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from fxstack.belief.engine import compute_directional_belief, load_directional_belief_model_set
from fxstack.models.belief_horizon_xgb import BeliefHorizonXGB
from fxstack.models.belief_ranker_xgb import BeliefRankerXGB
from fxstack.models.belief_regressor_xgb import BeliefRegressorXGB


def _build_v2_artifact(root: Path) -> Path:
    rows: list[dict[str, float]] = []
    relevance: list[int] = []
    ev_above: list[int] = []
    expected_ev: list[float] = []
    confirm: list[int] = []
    fail_fast: list[int] = []
    qid: list[int] = []
    feature_columns = [
        "scenario_regime_fit_prior",
        "uncertainty_score",
        "model_disagreement_score",
        "extension_penalty_score",
        "playbook_score_for_hypothesis",
        "location_score_for_hypothesis",
        "trigger_score_for_hypothesis",
        "expected_edge_bps",
    ]
    scenarios = [
        "trend_pullback",
        "range_mean_reversion",
        "breakout_expansion",
        "failed_breakout_reversal",
    ]
    sides = ["long", "short"]
    for query_id in range(4):
        best_long = query_id % 2 == 0
        for scenario in scenarios:
            for side in sides:
                is_best = (best_long and scenario == "trend_pullback" and side == "long") or (
                    (not best_long) and scenario == "breakout_expansion" and side == "short"
                )
                rows.append(
                    {
                        "scenario_regime_fit_prior": 1.0 if is_best else 0.25,
                        "uncertainty_score": 0.08 if is_best else 0.35,
                        "model_disagreement_score": 0.10 if is_best else 0.30,
                        "extension_penalty_score": 0.12 if is_best else 0.38,
                        "playbook_score_for_hypothesis": 0.86 if is_best else 0.28,
                        "location_score_for_hypothesis": 0.74 if is_best else 0.32,
                        "trigger_score_for_hypothesis": 0.78 if is_best else 0.30,
                        "expected_edge_bps": 9.5 if is_best else 1.2,
                    }
                )
                relevance.append(4 if is_best else 0)
                ev_above.append(1 if is_best else 0)
                expected_ev.append(10.0 if is_best else -1.5)
                confirm.append(1 if is_best else 0)
                fail_fast.append(0 if is_best else 1)
                qid.append(query_id)
    X = pd.DataFrame(rows, columns=feature_columns)
    ranker = BeliefRankerXGB(params={"device": "cpu", "n_estimators": 8, "max_depth": 2, "learning_rate": 0.2})
    ranker.fit(X, pd.Series(relevance), qid=qid)
    ev_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 8, "max_depth": 2, "learning_rate": 0.2})
    ev_model.fit(X, pd.Series(ev_above))
    ev_reg = BeliefRegressorXGB(params={"device": "cpu", "n_estimators": 8, "max_depth": 2, "learning_rate": 0.2})
    ev_reg.fit(X, pd.Series(expected_ev))
    confirm_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 8, "max_depth": 2, "learning_rate": 0.2})
    confirm_model.fit(X, pd.Series(confirm))
    fail_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 8, "max_depth": 2, "learning_rate": 0.2})
    fail_model.fit(X, pd.Series(fail_fast))

    root.mkdir(parents=True, exist_ok=True)
    ranker.save(root / "ranker_xgb")
    ev_model.save(root / "ev_above_hurdle_xgb")
    ev_reg.save(root / "expected_net_ev_bps_xgb")
    confirm_model.save(root / "confirm_success_xgb")
    fail_model.save(root / "fail_fast_xgb")
    (root / "meta.json").write_text(
        json.dumps(
            {
                "model_version": "directional_belief_v2_test",
                "belief_contract": "directional_belief_v2",
                "model_scope": "global_cross_pair",
                "query_granularity": "pair_ts_8_hypotheses",
                "label_kernel_version": "entry_ev_v1",
                "hypothesis_scenarios": scenarios,
                "hypothesis_sides": sides,
                "feature_columns": feature_columns,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return root


def test_directional_belief_v2_artifact_loading_and_compute(tmp_path: Path) -> None:
    artifact_root = _build_v2_artifact(tmp_path / "directional_belief_v2")
    model_set = load_directional_belief_model_set(artifact_root)

    belief = compute_directional_belief(
        row={
            "pair": "EURUSD",
            "ts": "2026-03-26T12:00:00Z",
            "session_bucket": "london_open",
            "regime_bucket": "trend",
            "scenario_bucket": "trend_continuation",
            "spread_bps": 0.8,
            "mid_close": 1.1025,
            "ret_1": 0.0002,
            "ret_5": 0.0005,
            "vol_20": 0.00015,
            "vol_60": 0.00018,
            "pullback_depth_20": 0.0017,
            "pushup_depth_20": 0.0015,
            "trend_slope_20": 0.0012,
            "trend_slope_60": 0.0014,
            "trend_strength_20": 0.84,
            "trend_strength_60": 0.76,
            "bar_imbalance": 0.22,
            "micro_pressure": 0.24,
            "edge_decay_12": 0.0001,
            "vol_term_ratio": 1.02,
            "hostility_score": 0.08,
            "macro_coherence_score": 0.72,
        },
        signal=SimpleNamespace(
            pair="EURUSD",
            ts="2026-03-26T12:00:00Z",
            side="long",
            uncertainty_score=0.10,
            model_disagreement_score=0.11,
            extension_penalty_score=0.16,
            regime_prob=0.78,
            swing_prob=0.70,
            entry_prob=0.66,
            trade_prob=0.72,
            directional_swing_confidence=0.69,
            htf_alignment_score=0.71,
            pullback_quality_score=0.68,
            resume_trigger_score=0.64,
            structure_timing_score=0.66,
            expected_edge_bps=7.5,
            spread_bps=0.8,
            scenario_bucket="trend_continuation",
        ),
        adaptive_meta={
            "pair": "EURUSD",
            "ts": "2026-03-26T12:00:00Z",
            "environment_state": "PersistentTrend",
            "adaptive_playbook": "trend_pullback",
            "playbook_score": 0.82,
            "location_score": 0.71,
            "trigger_score": 0.69,
            "macro_coherence_score": 0.72,
            "hostility_score": 0.08,
            "uncertainty_score": 0.10,
            "model_disagreement_score": 0.11,
            "extension_penalty_score": 0.16,
        },
        model_set=model_set,
    )

    assert model_set.belief_contract == "directional_belief_v2"
    assert belief.source_mode == "artifact"
    assert belief.model_version == "directional_belief_v2_test"
    assert len(belief.hypotheses) == 8
    assert belief.primary_rank_score >= 0.0
    assert belief.primary_ev_above_hurdle_prob >= 0.0
