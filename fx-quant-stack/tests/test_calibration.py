from __future__ import annotations

import numpy as np

from fxstack.training.calibration import ProbabilityCalibrator


def test_calibrator_fit_ignores_non_finite_inputs() -> None:
    cal = ProbabilityCalibrator()
    p_raw = np.array([0.1, np.nan, 0.8, np.inf, -np.inf], dtype=float)
    y_true = np.array([0.0, 1.0, 1.0, 0.0, np.nan], dtype=float)
    cal.fit(p_raw, y_true)

    out = cal.transform(np.array([0.2, np.nan, 2.0, -1.0], dtype=float))
    assert out.shape == (4,)
    assert np.all(np.isfinite(out))
    assert np.all(out >= 0.0)
    assert np.all(out <= 1.0)


def test_calibrator_with_all_invalid_fit_data_is_noop_but_sanitized() -> None:
    cal = ProbabilityCalibrator()
    p_raw = np.array([np.nan, np.inf], dtype=float)
    y_true = np.array([np.nan, np.nan], dtype=float)
    cal.fit(p_raw, y_true)

    out = cal.transform(np.array([np.nan, np.inf, -np.inf, 0.75], dtype=float))
    assert np.all(np.isfinite(out))
    assert np.all(out >= 0.0)
    assert np.all(out <= 1.0)
    assert float(out[-1]) == 0.75
