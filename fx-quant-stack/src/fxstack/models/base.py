from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd


class ModelBase(ABC):
    name: str

    @abstractmethod
    def fit(self, X: pd.DataFrame, y: pd.Series | None = None) -> None:
        raise NotImplementedError

    @abstractmethod
    def predict(self, X: pd.DataFrame) -> pd.Series:
        raise NotImplementedError

    @abstractmethod
    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def save(self, path: Path) -> None:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def load(cls, path: Path) -> "ModelBase":
        raise NotImplementedError
