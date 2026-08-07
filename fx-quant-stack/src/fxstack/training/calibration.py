from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression


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
    def __init__(self, *, isotonic_min_rows: int = 1000) -> None:
        self._iso = IsotonicRegression(out_of_bounds="clip")
        self._sigmoid: LogisticRegression | None = None
        self._method = ""
        self._isotonic_min_rows = max(100, int(isotonic_min_rows))
        self._fitted = False
        self.fit_rows = 0

    @property
    def is_fitted(self) -> bool:
        return bool(self._fitted)

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
        self.fit_rows = int(p_fit.size)
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
        # Laplace bound for the output clamp in transform(): with n observations
        # the tightest honest probability claim is ~1/(n+2), never exactly 0 or 1.
        self._fit_rows = int(p_fit.size)
        self._fitted = True

    def transform(self, p_raw: np.ndarray) -> np.ndarray:
        p = np.asarray(p_raw, dtype=float)
        shape = p.shape
        flat = p.reshape(-1).copy()

        finite_mask = np.isfinite(flat)
        flat[~finite_mask] = 0.5
        flat = np.clip(flat, 0.0, 1.0)

        method = self.method
        raw = flat.copy()
        if bool(getattr(self, "_fitted", False)) and method == "sigmoid" and getattr(self, "_sigmoid", None) is not None:
            logits = np.log(np.clip(flat, 1e-6, 1.0 - 1e-6) / np.clip(1.0 - flat, 1e-6, 1.0))
            flat = np.asarray(self._sigmoid.predict_proba(logits.reshape(-1, 1))[:, 1], dtype=float)
        elif bool(getattr(self, "_fitted", False)):
            flat = np.asarray(self._iso.transform(flat), dtype=float)

        flat[~np.isfinite(flat)] = 0.5

        # Two guards against calibration degeneracy, measured live 2026-07-31 on
        # the shipped swing_xgb calibrator (isotonic, fit on 715 daily rows):
        # 400 raw scores spanning [0.0008, 0.9916] with 398 distinct values came
        # out as 14 distinct values, 66.8% of them EXACTLY 0.0 and 30.2% EXACTLY
        # 1.0. Downstream, `directional_swing_confidence` turned that constant
        # 0.0 into side=short at confidence 1.0 -- 24h of decisions, 1,036 SELL,
        # 0 BUY, and a fabricated 0.46 model-disagreement (real model spread was
        # 0.063). A saturated calibrator must never read as certainty.
        #
        # 1. RANKING TIEBREAK: isotonic maps whole input ranges to one constant,
        #    destroying the model's ordering inside each flat segment. Blend a
        #    hair of the raw score back in so ordering survives; the shift is
        #    <= RANK_TIEBREAK_WEIGHT (0.001), far below any decision floor.
        # 2. LAPLACE CLAMP: with n fit rows, the tightest honest probability is
        #    ~1/(n+2); exact 0.0/1.0 is an artifact of empty isotonic tail
        #    buckets, not evidence. Legacy pickles (pre-`_fit_rows`) get a
        #    conservative fixed epsilon -- pickle binds THESE methods at load,
        #    so the live artifacts are healed by this code without retraining.
        # Both guards apply only to a FITTED estimator: an unfitted calibrator is
        # a sanitizing passthrough by contract (0.75 in, 0.75 out) and has no
        # isotonic plateau to repair.
        if bool(getattr(self, "_fitted", False)):
            RANK_TIEBREAK_WEIGHT = 1e-3
            flat = ((1.0 - RANK_TIEBREAK_WEIGHT) * flat) + (RANK_TIEBREAK_WEIGHT * raw)
            fit_rows = int(getattr(self, "_fit_rows", 0) or 0)
            eps = (1.0 / (fit_rows + 2.0)) if fit_rows > 0 else 1e-3
            eps = float(min(max(eps, 1e-6), 0.05))
            # AFFINE squash into [eps, 1-eps], not a hard clip: clipping would
            # collapse the whole sub-eps tail onto one constant and re-create the
            # plateau this guard exists to remove. The affine map keeps every
            # ordering strict and shifts any probability by at most eps.
            flat = eps + ((1.0 - (2.0 * eps)) * np.clip(flat, 0.0, 1.0))
        else:
            flat = np.clip(flat, 0.0, 1.0)
        return flat.reshape(shape)
