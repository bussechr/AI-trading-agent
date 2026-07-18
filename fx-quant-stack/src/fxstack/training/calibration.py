from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression


@dataclass(frozen=True, slots=True)
class CalibrationSplit:
    fit_idx: np.ndarray
    calibration_idx: np.ndarray
    requested_fraction: float
    actual_fraction: float
    strategy: str = "time_ordered_holdout"


def build_time_ordered_calibration_split(
    y_true: pd.Series | np.ndarray,
    *,
    fraction: float = 0.2,
    min_fit_rows: int = 64,
    min_calibration_rows: int = 32,
) -> CalibrationSplit | None:
    """Build a class-complete chronological calibration holdout.

    The calibration rows are always later than the model-fit rows. The split is
    expanded backwards until both slices contain every observed class. When that
    condition cannot be met, calibration is skipped rather than fitted in-sample.
    """

    labels = pd.Series(y_true).reset_index(drop=True)
    n = len(labels)
    min_fit = int(max(1, min_fit_rows))
    min_cal = int(max(1, min_calibration_rows))
    if n < min_fit + min_cal:
        return None

    observed = set(labels.dropna().tolist())
    if len(observed) < 2:
        return None

    frac = float(max(0.05, min(0.5, fraction)))
    max_calibration_rows = n - min_fit
    requested_rows = int(max(min_cal, np.ceil(n * frac)))
    requested_rows = int(min(max_calibration_rows, requested_rows))

    for calibration_rows in range(requested_rows, max_calibration_rows + 1):
        split_at = n - calibration_rows
        fit_classes = set(labels.iloc[:split_at].dropna().tolist())
        calibration_classes = set(labels.iloc[split_at:].dropna().tolist())
        if observed.issubset(fit_classes) and observed.issubset(calibration_classes):
            return CalibrationSplit(
                fit_idx=np.arange(0, split_at, dtype=int),
                calibration_idx=np.arange(split_at, n, dtype=int),
                requested_fraction=frac,
                actual_fraction=float(calibration_rows / n),
            )
    return None


class ProbabilityCalibrator:
    def __init__(self) -> None:
        self._iso = IsotonicRegression(out_of_bounds="clip")
        self._fitted = False
        self.fit_rows = 0

    @property
    def is_fitted(self) -> bool:
        return bool(self._fitted)

    def fit(self, p_raw: np.ndarray, y_true: np.ndarray) -> None:
        p = np.asarray(p_raw, dtype=float).reshape(-1)
        y = np.asarray(y_true, dtype=float).reshape(-1)

        mask = np.isfinite(p) & np.isfinite(y)
        p_fit = np.clip(p[mask], 0.0, 1.0)
        y_fit = y[mask]
        self.fit_rows = int(p_fit.size)
        if p_fit.size == 0:
            self._fitted = False
            return

        self._iso.fit(p_fit, y_fit)
        self._fitted = True

    def transform(self, p_raw: np.ndarray) -> np.ndarray:
        p = np.asarray(p_raw, dtype=float)
        shape = p.shape
        flat = p.reshape(-1).copy()

        finite_mask = np.isfinite(flat)
        flat[~finite_mask] = 0.5
        flat = np.clip(flat, 0.0, 1.0)

        if self._fitted:
            flat = np.asarray(self._iso.transform(flat), dtype=float)

        flat[~np.isfinite(flat)] = 0.5
        flat = np.clip(flat, 0.0, 1.0)
        return flat.reshape(shape)
