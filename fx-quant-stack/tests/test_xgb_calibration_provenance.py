from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import fxstack.models._xgb_base as binary_module
import fxstack.models._xgb_multiclass as multiclass_module


def _disable_cuda_probe(monkeypatch) -> None:
    result = {"ok": False, "detail": "test_cpu_only"}
    monkeypatch.setattr(binary_module, "probe_xgb_cuda_capability", lambda: result)
    monkeypatch.setattr(multiclass_module, "probe_xgb_cuda_capability", lambda: result)


def test_binary_xgb_calibration_uses_chronological_holdout(monkeypatch, tmp_path: Path) -> None:
    _disable_cuda_probe(monkeypatch)
    rows = 180
    rng = np.random.default_rng(7)
    X = pd.DataFrame({"x1": rng.normal(size=rows), "x2": rng.normal(size=rows)})
    y = pd.Series(np.tile([0, 1], rows // 2), dtype=int)
    model = binary_module.XGBBinaryModel(
        params={
            "device": "cpu",
            "n_estimators": 4,
            "max_depth": 2,
            "calibration_min_fit_rows": 64,
            "calibration_min_rows": 32,
        }
    )

    model.fit(X, y)
    provenance = model.calibration_provenance
    assert provenance["status"] == "fitted"
    assert provenance["fit_rows"] + provenance["calibration_rows"] == rows
    assert provenance["fit_rows"] < provenance["refit_rows"]

    model.save(tmp_path)
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["calibration_provenance"]["status"] == "fitted"
    assert meta["calibration_config"]["fraction"] == 0.2


def test_multiclass_xgb_calibration_uses_class_complete_holdout(monkeypatch) -> None:
    _disable_cuda_probe(monkeypatch)
    rows = 240
    rng = np.random.default_rng(11)
    X = pd.DataFrame({"x1": rng.normal(size=rows), "x2": rng.normal(size=rows)})
    y = pd.Series(np.tile([0, 1, 2], rows // 3), dtype=int)
    model = multiclass_module.XGBMulticlassModel(
        params={
            "device": "cpu",
            "n_estimators": 4,
            "max_depth": 2,
            "calibration_min_fit_rows": 80,
            "calibration_min_rows": 40,
        }
    )

    model.fit(X, y)
    provenance = model.calibration_provenance
    assert provenance["status"] == "fitted"
    assert set(model.calibrators) == {0, 1, 2}
    probabilities = model.predict_proba(X.iloc[:12])
    assert np.allclose(probabilities.sum(axis=1).to_numpy(), 1.0)
