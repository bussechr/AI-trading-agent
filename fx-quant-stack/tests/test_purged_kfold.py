from __future__ import annotations

import pandas as pd

from fxstack.labels.validation import PurgedKFold


def test_purged_kfold_no_overlap_between_train_and_valid():
    idx = pd.date_range("2026-01-01", periods=200, freq="5min", tz="UTC")
    splitter = PurgedKFold(n_splits=5, embargo_pct=0.05)
    for fold in splitter.split(idx):
        assert set(fold.train_idx).isdisjoint(set(fold.valid_idx))
        assert len(fold.train_idx) > 0
        assert len(fold.valid_idx) > 0
