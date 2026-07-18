from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

import numpy as np
import pandas as pd


@dataclass(slots=True)
class PurgedFold:
    train_idx: np.ndarray
    valid_idx: np.ndarray
    valid_start_ts: pd.Timestamp | None = None
    valid_end_ts: pd.Timestamp | None = None
    purged_event_count: int = 0
    embargo_count: int = 0


def event_end_times_from_indices(
    index: pd.DatetimeIndex,
    event_end_index: Sequence[int] | pd.Series | np.ndarray,
) -> pd.DatetimeIndex:
    """Resolve integer event-end positions onto an ordered timestamp index.

    Invalid, missing, or backwards end positions are reduced to the event's own
    start position. This keeps the interval contract total and prevents malformed
    labels from expanding a purge window unpredictably.
    """

    ordered = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    raw = pd.to_numeric(pd.Series(event_end_index).reset_index(drop=True), errors="coerce")
    if len(raw) != len(ordered):
        raise ValueError("event_end_index must have the same length as index")

    starts = np.arange(len(ordered), dtype=int)
    ends = raw.fillna(pd.Series(starts, dtype=float)).to_numpy(dtype=int)
    ends = np.maximum(starts, ends)
    ends = np.clip(ends, 0, max(0, len(ordered) - 1))
    return pd.DatetimeIndex(ordered.take(ends))


def _normalise_event_end(
    index: pd.DatetimeIndex,
    event_end: Sequence[object] | pd.Series | pd.DatetimeIndex | np.ndarray,
) -> pd.DatetimeIndex:
    raw = pd.Series(event_end).reset_index(drop=True)
    if len(raw) != len(index):
        raise ValueError("event_end must have the same length as index")
    if pd.api.types.is_numeric_dtype(raw):
        return event_end_times_from_indices(index, raw)

    parsed = pd.to_datetime(raw, utc=True, errors="coerce")
    starts = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
    resolved: list[pd.Timestamp] = []
    for start, end in zip(starts, parsed, strict=True):
        if pd.isna(end) or end < start:
            resolved.append(start)
        else:
            resolved.append(pd.Timestamp(end))
    return pd.DatetimeIndex(resolved)


class PurgedKFold:
    """Contiguous temporal folds with optional event-aware purging.

    When ``event_end`` is supplied, each observation is treated as an interval
    ``[index[i], event_end[i]]``. Training observations whose intervals overlap
    the validation interval are removed, followed by a forward embargo. Without
    event ends, the legacy symmetric row embargo remains in force.
    """

    def __init__(self, n_splits: int = 5, embargo_pct: float = 0.02) -> None:
        self.n_splits = int(max(2, n_splits))
        self.embargo_pct = float(max(0.0, min(0.2, embargo_pct)))

    def _embargo_size(self, rows: int) -> int:
        if self.embargo_pct <= 0.0:
            return 0
        return int(max(1, np.ceil(float(rows) * self.embargo_pct)))

    def split(
        self,
        index: pd.DatetimeIndex,
        *,
        event_end: Sequence[object] | pd.Series | pd.DatetimeIndex | np.ndarray | None = None,
    ) -> Iterator[PurgedFold]:
        ordered = pd.DatetimeIndex(pd.to_datetime(index, utc=True))
        n = len(ordered)
        if n < self.n_splits:
            raise ValueError("Not enough rows for PurgedKFold")
        if not ordered.is_monotonic_increasing:
            raise ValueError("PurgedKFold requires a monotonically increasing index")
        if ordered.has_duplicates:
            raise ValueError("PurgedKFold requires unique timestamps")

        fold_size = n // self.n_splits
        embargo = self._embargo_size(n)
        all_idx = np.arange(n)
        event_end_ts = None if event_end is None else _normalise_event_end(ordered, event_end)

        for fold_id in range(self.n_splits):
            start = fold_id * fold_size
            end = n if fold_id == self.n_splits - 1 else (fold_id + 1) * fold_size
            valid = all_idx[start:end]

            if event_end_ts is None:
                left = max(0, start - embargo)
                right = min(n, end + embargo)
                mask = np.ones(n, dtype=bool)
                mask[left:right] = False
                train = all_idx[mask]
                yield PurgedFold(
                    train_idx=train,
                    valid_idx=valid,
                    valid_start_ts=ordered[valid[0]],
                    valid_end_ts=ordered[valid[-1]],
                    purged_event_count=0,
                    embargo_count=max(0, (right - left) - len(valid)),
                )
                continue

            valid_start = ordered[valid].min()
            valid_end = event_end_ts[valid].max()
            overlap = (ordered <= valid_end) & (event_end_ts >= valid_start)
            valid_mask = np.zeros(n, dtype=bool)
            valid_mask[valid] = True

            mask = ~overlap
            purged_event_count = int(np.sum(overlap & ~valid_mask))

            embargo_mask = np.zeros(n, dtype=bool)
            if embargo > 0:
                embargo_mask[end : min(n, end + embargo)] = True
            embargo_count = int(np.sum(embargo_mask & mask))
            mask[embargo_mask] = False
            mask[valid_mask] = False
            train = all_idx[mask]

            yield PurgedFold(
                train_idx=train,
                valid_idx=valid,
                valid_start_ts=pd.Timestamp(valid_start),
                valid_end_ts=pd.Timestamp(valid_end),
                purged_event_count=purged_event_count,
                embargo_count=embargo_count,
            )
