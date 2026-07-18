from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxstack.labels.validation import PurgedKFold, event_end_times_from_indices


def test_purged_kfold_no_overlap_between_train_and_valid():
    idx = pd.date_range("2026-01-01", periods=200, freq="5min", tz="UTC")
    splitter = PurgedKFold(n_splits=5, embargo_pct=0.05)
    for fold in splitter.split(idx):
        assert set(fold.train_idx).isdisjoint(set(fold.valid_idx))
        assert len(fold.train_idx) > 0
        assert len(fold.valid_idx) > 0


def test_purged_kfold_removes_overlapping_event_windows():
    idx = pd.date_range("2026-01-01", periods=60, freq="h", tz="UTC")
    end_positions = np.minimum(np.arange(len(idx)) + 6, len(idx) - 1)
    event_end = event_end_times_from_indices(idx, end_positions)

    splitter = PurgedKFold(n_splits=3, embargo_pct=0.02)
    for fold in splitter.split(idx, event_end=end_positions):
        valid_start = idx[fold.valid_idx].min()
        valid_end = event_end[fold.valid_idx].max()
        assert fold.valid_start_ts == valid_start
        assert fold.valid_end_ts == valid_end
        assert fold.purged_event_count > 0
        for train_idx in fold.train_idx:
            overlaps = idx[train_idx] <= valid_end and event_end[train_idx] >= valid_start
            assert not overlaps


def test_purged_kfold_rejects_unsorted_timestamps():
    idx = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-01-02", tz="UTC"),
            pd.Timestamp("2026-01-01", tz="UTC"),
            pd.Timestamp("2026-01-03", tz="UTC"),
        ]
    )
    splitter = PurgedKFold(n_splits=2)
    with pytest.raises(ValueError, match="monotonically increasing"):
        list(splitter.split(idx))
