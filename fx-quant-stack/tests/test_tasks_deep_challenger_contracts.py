from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from fxstack import tasks


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        transformer_window_size=16,
        deep_train_epochs=1,
        deep_batch_size=4,
        require_cuda=False,
        patchtst_patch_length=4,
        patchtst_stride=2,
        patchtst_d_model=16,
        patchtst_num_layers=1,
        patchtst_num_heads=1,
        patchtst_dropout=0.1,
        cv_splits=2,
        cv_embargo_pct=0.0,
        wf_train_months=3,
        wf_test_months=1,
        wf_step_months=1,
    )


def _train_frame(rows: int = 12) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    ts = pd.date_range("2026-01-01", periods=rows, freq="D", tz="UTC")
    X = pd.DataFrame({"ret_1": range(rows), "vol_20": [float(i % 3) + 0.5 for i in range(rows)]})
    y = pd.Series([0 if i % 2 else 1 for i in range(rows)])
    df = pd.DataFrame(
        {
            "ts": ts,
            "pair": ["EURUSD"] * rows,
            "timeframe": ["D"] * rows,
            "session_tag": ["unknown"] * rows,
            "regime_bucket": ["unknown"] * rows,
            "scenario_bucket": ["unknown"] * rows,
        }
    )
    return X, y, df


class _DummyDeepModel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.saved_path: Path | None = None

    def fit(self, X, y) -> None:
        self.fit_rows = len(X)

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.saved_path = path
        (path / "meta.json").write_text(json.dumps({"created_at": 1.0}), encoding="utf-8")


def test_train_swing_transformer_task_requests_cross_pair_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    X, y, df = _train_frame()
    captured: dict[str, object] = {}

    def fake_train_xy(**kwargs):
        captured["feature_view_names"] = list(kwargs["feature_view_names"])
        return X, y, df

    monkeypatch.setattr(tasks, "_train_xy", fake_train_xy)
    monkeypatch.setattr(tasks, "get_settings", _settings)
    monkeypatch.setattr(tasks, "SwingTransformer", _DummyDeepModel)
    monkeypatch.setattr(tasks, "_annotate_supervised_artifact", lambda **kwargs: None)
    monkeypatch.setattr(tasks, "_with_mlops_fields", lambda payload: payload)

    result = tasks.train_swing_transformer_task(
        pair="EURUSD",
        timeframe="D",
        feature_root=str(tmp_path / "features"),
        label_root=str(tmp_path / "labels"),
        out=str(tmp_path / "swing_transformer"),
    )

    assert captured["feature_view_names"] == ["anchor_d", "cross_pair_context"]
    assert result["model"] == "swing_transformer"


def test_train_swing_patchtst_task_requests_cross_pair_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    X, y, df = _train_frame()
    captured: dict[str, object] = {}

    def fake_train_xy(**kwargs):
        captured["feature_view_names"] = list(kwargs["feature_view_names"])
        return X, y, df

    def fake_sequence_manifest(**kwargs):
        manifest_path = tmp_path / "sequence_dataset_manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(manifest_path=str(manifest_path))

    monkeypatch.setattr(tasks, "_ensure_patchtst_stack", lambda: None)
    monkeypatch.setattr(tasks, "_train_xy", fake_train_xy)
    monkeypatch.setattr(tasks, "build_sequence_dataset_manifest", fake_sequence_manifest)
    monkeypatch.setattr(tasks, "get_settings", _settings)
    monkeypatch.setattr(tasks, "SwingPatchTST", _DummyDeepModel)
    monkeypatch.setattr(tasks, "validate_candidate", lambda **kwargs: {"status": "ok"})
    monkeypatch.setattr(tasks, "_annotate_validation_result", lambda **kwargs: "promoted")
    monkeypatch.setattr(tasks, "_annotate_supervised_artifact", lambda **kwargs: None)
    monkeypatch.setattr(tasks, "_with_mlops_fields", lambda payload: payload)

    result = tasks.train_swing_patchtst_task(
        pair="EURUSD",
        timeframe="D",
        feature_root=str(tmp_path / "features"),
        label_root=str(tmp_path / "labels"),
        out=str(tmp_path / "swing_patchtst"),
    )

    assert captured["feature_view_names"] == ["anchor_d", "cross_pair_context"]
    assert result["model"] == "swing_patchtst"
