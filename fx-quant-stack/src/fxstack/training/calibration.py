from __future__ import annotations

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


class ProbabilityCalibrator:
    def __init__(self, *, isotonic_min_rows: int = 1000) -> None:
        self._iso = IsotonicRegression(out_of_bounds="clip")
        self._sigmoid: LogisticRegression | None = None
        self._method = ""
        self._isotonic_min_rows = max(100, int(isotonic_min_rows))
        self._fitted = False

    @property
    def method(self) -> str:
        method = str(getattr(self, "_method", "") or "")
        if method:
            return method
        # Backward compatibility for artifacts serialized before method
        # metadata existed: their fitted estimator was always isotonic.
        return "isotonic" if bool(getattr(self, "_fitted", False)) else ""

    def fit(self, p_raw: np.ndarray, y_true: np.ndarray) -> None:
        p = np.asarray(p_raw, dtype=float).reshape(-1)
        y = np.asarray(y_true, dtype=float).reshape(-1)

        mask = np.isfinite(p) & np.isfinite(y)
        p_fit = np.clip(p[mask], 0.0, 1.0)
        y_fit = y[mask]
        if p_fit.size == 0 or np.unique(y_fit).size < 2:
            self._fitted = False
            self._method = ""
            return

        if p_fit.size >= int(self._isotonic_min_rows) and np.unique(p_fit).size >= 20:
            self._iso.fit(p_fit, y_fit)
            self._sigmoid = None
            self._method = "isotonic"
        else:
            logits = np.log(np.clip(p_fit, 1e-6, 1.0 - 1e-6) / np.clip(1.0 - p_fit, 1e-6, 1.0))
            self._sigmoid = LogisticRegression(C=1.0, solver="lbfgs", random_state=7)
            self._sigmoid.fit(logits.reshape(-1, 1), y_fit.astype(int))
            self._method = "sigmoid"
        self._fitted = True

    def transform(self, p_raw: np.ndarray) -> np.ndarray:
        p = np.asarray(p_raw, dtype=float)
        shape = p.shape
        flat = p.reshape(-1).copy()

        finite_mask = np.isfinite(flat)
        flat[~finite_mask] = 0.5
        flat = np.clip(flat, 0.0, 1.0)

        method = self.method
        if bool(getattr(self, "_fitted", False)) and method == "sigmoid" and getattr(self, "_sigmoid", None) is not None:
            logits = np.log(np.clip(flat, 1e-6, 1.0 - 1e-6) / np.clip(1.0 - flat, 1e-6, 1.0))
            flat = np.asarray(self._sigmoid.predict_proba(logits.reshape(-1, 1))[:, 1], dtype=float)
        elif bool(getattr(self, "_fitted", False)):
            flat = np.asarray(self._iso.transform(flat), dtype=float)

        flat[~np.isfinite(flat)] = 0.5
        flat = np.clip(flat, 0.0, 1.0)
        return flat.reshape(shape)
