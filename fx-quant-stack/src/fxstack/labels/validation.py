from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np
import pandas as pd


@dataclass(slots=True)
class PurgedFold:
    train_idx: np.ndarray
    valid_idx: np.ndarray


class PurgedKFold:
    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.02) -> None:
        self.n_splits = int(max(2, n_splits))
        self.embargo_pct = float(max(0.0, min(0.2, embargo_pct)))

    def split(self, index: pd.DatetimeIndex) -> Iterator[PurgedFold]:
        n = len(index)
        if n < self.n_splits:
            raise ValueError("Not enough rows for PurgedKFold")

        fold_size = n // self.n_splits
        embargo = int(max(1, n * self.embargo_pct))

        all_idx = np.arange(n)
        for i in range(self.n_splits):
            start = i * fold_size
            end = n if i == self.n_splits - 1 else (i + 1) * fold_size
            valid = all_idx[start:end]

            left = max(0, start - embargo)
            right = min(n, end + embargo)
            mask = np.ones(n, dtype=bool)
            mask[left:right] = False
            train = all_idx[mask]

            yield PurgedFold(train_idx=train, valid_idx=valid)
