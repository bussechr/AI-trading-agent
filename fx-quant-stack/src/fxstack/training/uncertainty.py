from __future__ import annotations

import numpy as np
import pandas as pd


class UncertaintyModel:
    def __init__(self) -> None:
        self.mean_: np.ndarray | None = None
        self.std_: np.ndarray | None = None

    def fit(self, X: pd.DataFrame) -> None:
        x_num = X.astype(float).to_numpy(dtype=float)
        self.mean_ = np.nanmean(x_num, axis=0)
        self.std_ = np.nanstd(x_num, axis=0)
        self.std_[self.std_ <= 1e-9] = 1.0

    def ood_score(self, X: pd.DataFrame) -> np.ndarray:
        if self.mean_ is None or self.std_ is None:
            raise RuntimeError("uncertainty model is not fitted")
        x_num = X.astype(float).to_numpy(dtype=float)
        z = np.abs((x_num - self.mean_) / self.std_)
        return np.nanmean(z, axis=1)


def ensemble_disagreement(probabilities: list[np.ndarray]) -> np.ndarray:
    if not probabilities:
        return np.zeros(0, dtype=float)
    stack = np.stack([np.asarray(p, dtype=float).reshape(-1) for p in probabilities], axis=1)
    return np.nanstd(stack, axis=1)


def summarize_uncertainty(
    *,
    ood_score: np.ndarray,
    disagreement: np.ndarray,
    threshold: float,
) -> dict[str, float]:
    ood = np.asarray(ood_score, dtype=float).reshape(-1)
    dis = np.asarray(disagreement, dtype=float).reshape(-1)
    combined = (ood / (np.nanmax(ood) + 1e-9)) + dis
    return {
        "ood_mean": float(np.nanmean(ood)) if ood.size else 0.0,
        "disagreement_mean": float(np.nanmean(dis)) if dis.size else 0.0,
        "combined_mean": float(np.nanmean(combined)) if combined.size else 0.0,
        "high_uncertainty_share": float(np.mean(combined >= float(threshold))) if combined.size else 0.0,
    }
