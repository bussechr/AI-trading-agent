from __future__ import annotations

import pytest

from fxstack.tasks import build_meta_labels_task, train_meta_task


def test_build_meta_labels_requires_model_paths_when_heuristics_disabled(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="requires trained regime/swing/intraday model paths"):
        build_meta_labels_task(
            pair="EURUSD",
            timeframe="M5",
            feature_root=str(tmp_path / "features"),
            label_root=str(tmp_path / "labels"),
            allow_heuristic_labels=False,
        )


def test_train_meta_rejects_missing_scored_labels_when_heuristics_disabled(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="meta labels are missing"):
        train_meta_task(
            pair="EURUSD",
            timeframe="M5",
            feature_root=str(tmp_path / "features"),
            label_root=str(tmp_path / "labels"),
            out=str(tmp_path / "artifacts" / "meta_filter"),
            allow_heuristic_labels=False,
        )
