from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import pandas as pd


@dataclass(slots=True)
class SplitWindow:
    train_start: str
    train_end: str
    valid_start: str
    valid_end: str


def walk_forward_windows(index: pd.DatetimeIndex, train_size: int, valid_size: int, step: int) -> Iterator[SplitWindow]:
    for start in range(0, len(index) - train_size - valid_size + 1, step):
        train_idx = index[start : start + train_size]
        valid_idx = index[start + train_size : start + train_size + valid_size]
        yield SplitWindow(
            train_start=str(train_idx[0]),
            train_end=str(train_idx[-1]),
            valid_start=str(valid_idx[0]),
            valid_end=str(valid_idx[-1]),
        )
