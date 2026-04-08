from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from fxstack.models.meta_filter import MetaFilterXGB
from fxstack.training.lifecycle_validation import validate_candidate


def test_validate_candidate_emits_report_bundle(tmp_path: Path) -> None:
    rows = 220
    ts = pd.date_range("2025-01-01", periods=rows, freq="D", tz="UTC")
    x1 = np.linspace(-1.0, 1.0, rows)
    x2 = np.sin(np.linspace(0.0, 8.0, rows))
    X = pd.DataFrame(
        {
            "ret_1": x1,
            "vol_20": np.abs(x2) + 0.1,
            "spread_bps": 0.5 + np.abs(x2) * 0.2,
        }
    )
    y = pd.Series((x1 + x2 > 0.1).astype(int))
    meta = pd.DataFrame(
        {
            "ts": ts,
            "pair": ["EURUSD"] * rows,
            "session_tag": ["london_open" if i % 3 == 0 else "ny_overlap" for i in range(rows)],
            "regime_bucket": ["trend" if i % 4 else "range" for i in range(rows)],
            "scenario_bucket": ["trend_continuation" if i % 5 else "high_spread_stress" for i in range(rows)],
            "realized_edge_after_costs_1_25": np.where(y == 1, 1.0, -1.0),
        }
    )
    weights = pd.Series(np.where(y == 1, 1.5, 1.0))

    report = validate_candidate(
        model_factory=lambda: MetaFilterXGB(params={"n_estimators": 16, "max_depth": 2, "random_state": 7}),
        challenger_factories=[
            lambda: MetaFilterXGB(params={"n_estimators": 16, "max_depth": 2, "random_state": 11}),
            lambda: MetaFilterXGB(params={"n_estimators": 16, "max_depth": 2, "random_state": 19}),
        ],
        X=X,
        y=y,
        timestamps=meta["ts"],
        meta=meta,
        sample_weight=weights,
        task="binary",
        report_root=tmp_path,
        champion_metric=0.2,
        cost_stress_cols=["realized_edge_after_costs_1_25"],
        cv_splits=4,
        embargo_pct=0.02,
        wf_train_months=2,
        wf_test_months=1,
        wf_step_months=1,
    )

    assert "promotion_decision" in report
    assert "reliability_by_segment" in report
    assert "uncertainty" in report
    assert "portfolio_report" in report
    assert "challenger_head_to_head" in report
    assert (tmp_path / "training_report.json").exists()
    assert (tmp_path / "promotion_decision.json").exists()
    assert (tmp_path / "portfolio_report.json").exists()
    assert (tmp_path / "challenger_head_to_head.json").exists()
    assert (tmp_path / "portfolio_disagreement.json").exists()
