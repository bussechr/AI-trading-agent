from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from fxstack.belief.adapters import optional_research_capabilities
from fxstack.belief.engine import compute_directional_belief, load_directional_belief_model_set
from fxstack.belief.labels import SCENARIO_LABELS
from fxstack.models.belief_horizon_xgb import BeliefHorizonXGB
from fxstack.models.belief_scenario_xgb import BeliefScenarioXGB


def _build_artifact(root: Path) -> Path:
    X = pd.DataFrame(
        {
            "ret_1": [0.10, 0.12, 0.18, -0.12, -0.10, -0.18, 0.02, -0.02, 0.07, -0.07],
            "playbook_score": [0.8, 0.75, 0.7, 0.35, 0.3, 0.25, 0.45, 0.4, 0.65, 0.2],
            "location_score": [0.7, 0.68, 0.66, 0.4, 0.38, 0.35, 0.5, 0.45, 0.6, 0.32],
            "trigger_score": [0.72, 0.7, 0.69, 0.42, 0.4, 0.38, 0.48, 0.46, 0.62, 0.36],
            "macro_coherence_score": [0.75, 0.73, 0.7, 0.42, 0.4, 0.38, 0.52, 0.49, 0.64, 0.35],
            "uncertainty_score": [0.12, 0.15, 0.18, 0.45, 0.42, 0.48, 0.30, 0.33, 0.20, 0.40],
        }
    )
    y_scenario = pd.Series([0, 0, 2, 1, 1, 3, 4, 4, 2, 3], dtype=int)
    y_short = pd.Series([1, 1, 1, 0, 0, 0, 1, 0, 1, 0], dtype=int)
    y_trade = pd.Series([1, 1, 1, 0, 0, 0, 1, 0, 1, 0], dtype=int)
    y_structural = pd.Series([1, 1, 1, 0, 0, 0, 0, 0, 1, 0], dtype=int)

    scenario_model = BeliefScenarioXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 12, "max_depth": 2, "learning_rate": 0.2})
    scenario_model.fit(X, y_scenario)
    short_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 12, "max_depth": 2, "learning_rate": 0.2})
    short_model.fit(X, y_short)
    trade_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 12, "max_depth": 2, "learning_rate": 0.2})
    trade_model.fit(X, y_trade)
    structural_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 12, "max_depth": 2, "learning_rate": 0.2})
    structural_model.fit(X, y_structural)

    root.mkdir(parents=True, exist_ok=True)
    scenario_model.save(root / "scenario_xgb")
    short_model.save(root / "horizon_short_xgb")
    trade_model.save(root / "horizon_trade_xgb")
    structural_model.save(root / "horizon_structural_xgb")
    (root / "meta.json").write_text(
        json.dumps(
            {
                "model_version": "directional_belief_test_v1",
                "belief_contract": "directional_belief_v1",
                "feature_columns": list(X.columns),
                "scenario_labels": list(SCENARIO_LABELS),
                "horizons_bars": {"short": 3, "trade": 12, "structural": 48},
                "trained_at": 0,
                "training_window_summary": {},
                "validation_metrics": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def test_directional_belief_artifact_loading_and_compute(tmp_path: Path) -> None:
    artifact_root = _build_artifact(tmp_path / "directional_belief")
    model_set = load_directional_belief_model_set(artifact_root)

    belief = compute_directional_belief(
        row={
            "pair": "EURUSD",
            "ts": "2026-03-26T12:00:00Z",
            "ret_1": 0.11,
            "playbook_score": 0.8,
            "location_score": 0.71,
            "trigger_score": 0.69,
            "macro_coherence_score": 0.74,
            "uncertainty_score": 0.12,
        },
        signal=SimpleNamespace(
            pair="EURUSD",
            ts="2026-03-26T12:00:00Z",
            uncertainty_score=0.12,
            model_disagreement_score=0.10,
            extension_penalty_score=0.15,
        ),
        adaptive_meta={
            "pair": "EURUSD",
            "ts": "2026-03-26T12:00:00Z",
            "environment_state": "PersistentTrend",
            "adaptive_playbook": "trend_pullback",
            "playbook_score": 0.8,
            "location_score": 0.71,
            "trigger_score": 0.69,
            "macro_coherence_score": 0.74,
            "hostility_score": 0.08,
            "uncertainty_score": 0.12,
            "model_disagreement_score": 0.10,
            "extension_penalty_score": 0.15,
        },
        model_set=model_set,
    )

    assert belief.source_mode == "artifact"
    assert belief.model_version == "directional_belief_test_v1"
    assert belief.primary_scenario in set(SCENARIO_LABELS)
    assert belief.primary_thesis.endswith(f":{belief.primary_side}")


def test_optional_research_capabilities_fall_back_cleanly(monkeypatch) -> None:
    original_import = builtins.__import__

    def _raising_import(name, *args, **kwargs):
        if name in {"neuralforecast", "ruptures", "river"}:
            raise ImportError(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _raising_import)
    caps = optional_research_capabilities()
    assert caps == {"neuralforecast": False, "ruptures": False, "river": False}
