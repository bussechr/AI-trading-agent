from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

from fxstack.models.base import ModelBase


class RegimeHMM(ModelBase):
    name = "regime_hmm"

    def __init__(self, n_components: int = 3, random_state: int = 7) -> None:
        self.model = GaussianHMM(n_components=n_components, covariance_type="full", random_state=random_state)
        self.feature_columns: list[str] = []

    def _prepare_X(self, X: pd.DataFrame) -> pd.DataFrame:
        x_in = X.copy()
        if self.feature_columns:
            missing = [c for c in self.feature_columns if c not in x_in.columns]
            if missing:
                raise ValueError(f"missing feature columns: {','.join(missing)}")
            x_in = x_in[self.feature_columns]
        return x_in.astype(float)

    def fit(self, X: pd.DataFrame, y: pd.Series | None = None) -> None:
        self.feature_columns = list(X.columns)
        self.model.fit(self._prepare_X(X).to_numpy())

    def predict(self, X: pd.DataFrame) -> pd.Series:
        states = self.model.predict(self._prepare_X(X).to_numpy())
        return pd.Series(states, index=X.index, name="regime_state")

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        proba = self.model.predict_proba(self._prepare_X(X).to_numpy())
        cols = [f"state_{i}" for i in range(proba.shape[1])]
        return pd.DataFrame(proba, columns=cols, index=X.index)

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.model, path / "model.joblib")
        (path / "meta.json").write_text(
            json.dumps(
                {
                    "name": self.name,
                    "feature_columns": list(self.feature_columns),
                }
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "RegimeHMM":
        obj = cls()
        obj.model = joblib.load(path / "model.joblib")
        meta_path = path / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                obj.feature_columns = list(meta.get("feature_columns") or [])
            except Exception:
                obj.feature_columns = []
        return obj
