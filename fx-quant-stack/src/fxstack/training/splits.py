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
    train_idx: list[int] | None = None
    valid_idx: list[int] | None = None


def walk_forward_windows(index: pd.DatetimeIndex, train_size: int, valid_size: int, step: int) -> Iterator[SplitWindow]:
    for start in range(0, len(index) - train_size - valid_size + 1, step):
        train_idx = index[start : start + train_size]
        valid_idx = index[start + train_size : start + train_size + valid_size]
        yield SplitWindow(
            train_start=str(train_idx[0]),
            train_end=str(train_idx[-1]),
            valid_start=str(valid_idx[0]),
            valid_end=str(valid_idx[-1]),
            train_idx=list(range(start, start + train_size)),
            valid_idx=list(range(start + train_size, start + train_size + valid_size)),
        )


def calendar_walk_forward_windows(
    index: pd.DatetimeIndex,
    *,
    train_months: int,
    valid_months: int,
    step_months: int,
) -> Iterator[SplitWindow]:
    if len(index) == 0:
        return
    ts = pd.DatetimeIndex(index).sort_values()
    cursor = ts.min()
    end_limit = ts.max()
    while True:
        train_end = cursor + pd.DateOffset(months=int(train_months))
        valid_end = train_end + pd.DateOffset(months=int(valid_months))
        if valid_end > end_limit + pd.Timedelta(seconds=1):
            break

        train_mask = (ts >= cursor) & (ts < train_end)
        valid_mask = (ts >= train_end) & (ts < valid_end)
        train_idx = ts[train_mask]
        valid_idx = ts[valid_mask]
        if len(train_idx) > 0 and len(valid_idx) > 0:
            yield SplitWindow(
                train_start=str(train_idx[0]),
                train_end=str(train_idx[-1]),
                valid_start=str(valid_idx[0]),
                valid_end=str(valid_idx[-1]),
                train_idx=list(pd.Index(range(len(ts)))[train_mask]),
                valid_idx=list(pd.Index(range(len(ts)))[valid_mask]),
            )
        cursor = cursor + pd.DateOffset(months=int(step_months))
