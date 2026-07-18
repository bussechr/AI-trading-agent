from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

from fxstack.features.session_contract import feature_contract_metadata
from fxstack.models.artifact_contract import (
    artifact_io_locked,
    stamp_artifact_payload_digest,
    validate_artifact_contract,
)
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

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | None = None,
        sample_weight: pd.Series | None = None,
    ) -> None:
        self.feature_columns = list(X.columns)
        self.model.fit(self._prepare_X(X).to_numpy())

    def _fallback_proba(self, index: pd.Index) -> pd.DataFrame:
        n = int(getattr(self.model, "n_components", 0) or 0)
        if n <= 0:
            startprob = getattr(self.model, "startprob_", None)
            try:
                n = int(len(startprob)) if startprob is not None else 0
            except Exception:
                n = 0
        n = max(1, n)
        prob = 1.0 / float(n)
        arr = np.full((len(index), n), prob, dtype=float)
        cols = [f"state_{i}" for i in range(n)]
        return pd.DataFrame(arr, columns=cols, index=index)

    def predict(self, X: pd.DataFrame) -> pd.Series:
        try:
            states = self.model.predict(self._prepare_X(X).to_numpy())
        except Exception:
            # Keep runtime resilient if an artifact has numerically unstable covariances.
            p = self.predict_proba(X)
            states = p.to_numpy().argmax(axis=1).astype(int)
        return pd.Series(states, index=X.index, name="regime_state")

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        try:
            proba = self.model.predict_proba(self._prepare_X(X).to_numpy())
            cols = [f"state_{i}" for i in range(proba.shape[1])]
            return pd.DataFrame(proba, columns=cols, index=X.index)
        except Exception:
            return self._fallback_proba(X.index)

    @artifact_io_locked
    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.model, path / "model.joblib")
        (path / "meta.json").write_text(
            json.dumps(
                {
                    "name": self.name,
                    **feature_contract_metadata(),
                    "feature_columns": list(self.feature_columns),
                }
            ),
            encoding="utf-8",
        )
        stamp_artifact_payload_digest(path)

    @classmethod
    @artifact_io_locked
    def load(cls, path: Path) -> "RegimeHMM":
        meta = validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        obj = cls()
        obj.model = joblib.load(path / "model.joblib")
        obj.feature_columns = list(meta.get("feature_columns") or [])
        validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        return obj
