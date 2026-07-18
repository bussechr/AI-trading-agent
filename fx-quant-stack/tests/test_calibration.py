from __future__ import annotations

import numpy as np
import pandas as pd

from fxstack.training.calibration import ProbabilityCalibrator, build_time_ordered_calibration_split


def test_calibrator_fit_ignores_non_finite_inputs() -> None:
    cal = ProbabilityCalibrator()
    p_raw = np.array([0.1, np.nan, 0.8, np.inf, -np.inf], dtype=float)
    y_true = np.array([0.0, 1.0, 1.0, 0.0, np.nan], dtype=float)
    cal.fit(p_raw, y_true)

    out = cal.transform(np.array([0.2, np.nan, 2.0, -1.0], dtype=float))
    assert cal.is_fitted is True
    assert cal.fit_rows == 2
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
    assert cal.is_fitted is False
    assert cal.fit_rows == 0
    assert np.all(np.isfinite(out))
    assert np.all(out >= 0.0)
    assert np.all(out <= 1.0)
    assert float(out[-1]) == 0.75


def test_time_ordered_calibration_split_is_class_complete() -> None:
    y = pd.Series(([0, 1] * 100), dtype=int)
    split = build_time_ordered_calibration_split(
        y,
        fraction=0.2,
        min_fit_rows=64,
        min_calibration_rows=32,
    )

    assert split is not None
    assert int(split.fit_idx.max()) < int(split.calibration_idx.min())
    assert set(y.iloc[split.fit_idx]) == {0, 1}
    assert set(y.iloc[split.calibration_idx]) == {0, 1}
    assert len(split.fit_idx) + len(split.calibration_idx) == len(y)


def test_time_ordered_calibration_split_skips_single_class_data() -> None:
    y = pd.Series(np.zeros(200, dtype=int))
    assert build_time_ordered_calibration_split(y) is None
