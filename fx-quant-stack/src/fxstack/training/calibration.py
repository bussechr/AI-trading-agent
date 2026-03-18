from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression


class ProbabilityCalibrator:
    def __init__(self) -> None:
        self._iso = IsotonicRegression(out_of_bounds="clip")
        self._fitted = False

    def fit(self, p_raw: np.ndarray, y_true: np.ndarray) -> None:
        p = np.asarray(p_raw, dtype=float).reshape(-1)
        y = np.asarray(y_true, dtype=float).reshape(-1)

        mask = np.isfinite(p) & np.isfinite(y)
        p_fit = np.clip(p[mask], 0.0, 1.0)
        y_fit = y[mask]
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
