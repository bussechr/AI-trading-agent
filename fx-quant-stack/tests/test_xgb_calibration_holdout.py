from __future__ import annotations

import json

import numpy as np
import pandas as pd

from fxstack.models._xgb_base import XGBBinaryModel


def _frame(rows: int) -> tuple[pd.DataFrame, pd.Series]:
    idx = pd.RangeIndex(rows)
    x = pd.DataFrame(
        {
            "trend": np.linspace(-1.0, 1.0, rows),
            "cycle": np.sin(np.arange(rows, dtype=float) / 5.0),
        },
        index=idx,
    )
    y = pd.Series((np.arange(rows) % 3 == 0).astype(int), index=idx)
    return x, y


def test_binary_xgb_calibration_uses_embargoed_chronological_tail(tmp_path) -> None:
    x, y = _frame(120)
    model = XGBBinaryModel(
        params={
            "device": "cpu",
            "n_estimators": 8,
            "max_depth": 2,
            "use_calibration": True,
            "calibration_fraction": 0.20,
            "calibration_min_rows": 20,
            "calibration_embargo_rows": 5,
        }
    )

    model.fit(x, y)

    assert model.calibrator is not None
    assert model.calibration_diagnostics["method"] == "chronological_holdout_calibration_v2"
    assert model.calibration_diagnostics["fitted"] is True
    assert model.calibration_diagnostics["calibrator_method"] == "sigmoid"
    assert model.calibration_diagnostics["calibration_start"] == 96
    assert model.calibration_diagnostics["fit_rows"] == 91
    assert model.calibration_diagnostics["embargo_rows"] == 5
    assert model.calibration_diagnostics["calibration_rows"] == 24

    artifact = tmp_path / "model"
    model.save(artifact)
    meta = json.loads((artifact / "meta.json").read_text(encoding="utf-8"))
    assert meta["calibration_config"] == {
        "fraction": 0.2,
        "min_fit_rows": 64,
        "min_calibration_rows": 20,
        "embargo_rows": 5,
    }
    loaded = XGBBinaryModel.load(artifact)
    probs = loaded.predict_proba(x.tail(8))
    assert np.isfinite(probs.to_numpy()).all()
    assert ((probs.to_numpy() >= 0.0) & (probs.to_numpy() <= 1.0)).all()


def test_binary_xgb_disables_calibration_when_temporal_split_is_too_small() -> None:
    x, y = _frame(30)
    model = XGBBinaryModel(
        params={
            "device": "cpu",
            "n_estimators": 4,
            "max_depth": 2,
            "use_calibration": True,
            "calibration_fraction": 0.20,
            "calibration_min_rows": 20,
            "calibration_embargo_rows": 5,
        }
    )

    model.fit(x, y)

    assert model.calibrator is None
    assert model.calibration_diagnostics["fitted"] is False
    assert model.calibration_diagnostics["reason"] == "split_invalid"
    assert np.isfinite(model.predict_proba(x).to_numpy()).all()
