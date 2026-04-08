from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fxstack.models.patchtst import SwingPatchTST, patchtst_dependencies_available
from fxstack.training.sequence_dataset import build_sequence_dataset_manifest


def _frame(rows: int = 48) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    ts = pd.date_range("2026-01-01", periods=rows, freq="D", tz="UTC")
    x1 = np.linspace(-1.0, 1.0, rows)
    x2 = np.cos(np.linspace(0.0, 6.0, rows))
    X = pd.DataFrame(
        {
            "ret_1": x1,
            "vol_20": np.abs(x2) + 0.1,
            "trend_strength_20": x1 * 0.5 + x2 * 0.2,
        }
    )
    y = pd.Series((x1 + x2 > 0.0).astype(int))
    return X, y, pd.Series(ts)


@pytest.mark.skipif(not patchtst_dependencies_available(), reason="PatchTST research dependencies are unavailable")
def test_swing_patchtst_fit_predict_save_load(tmp_path: Path) -> None:
    X, y, _ = _frame()
    model = SwingPatchTST(
        window_size=8,
        patch_length=4,
        stride=2,
        d_model=16,
        num_layers=1,
        num_heads=1,
        epochs=1,
        batch_size=8,
        require_cuda=False,
    )
    model.fit(X, y)
    out = model.predict_proba(X)
    assert list(out.columns) == ["p0", "p1"]
    assert len(out) == len(X)

    path = tmp_path / "swing_patchtst"
    model.save(path)
    assert (path / "config.json").exists()
    assert (path / "calibrator.joblib").exists()
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    assert meta["feature_columns"] == list(X.columns)

    loaded = SwingPatchTST.load(path)
    with pytest.raises(ValueError, match="missing feature columns"):
        loaded.predict_proba(X.drop(columns=["trend_strength_20"]))
    loaded_out = loaded.predict_proba(X)
    assert len(loaded_out) == len(X)


def test_sequence_dataset_manifest_is_stable_and_cached(tmp_path: Path) -> None:
    X, y, ts = _frame(rows=24)
    retrieval = {
        "feature_service_name": "fx_eurusd_swing_patchtst_d",
        "feature_service_version": "svc-123",
        "feature_contract_hash": "hash-123",
        "source": "feast_historical",
    }
    one = build_sequence_dataset_manifest(
        X=X,
        y=y,
        timestamps=ts,
        pair="EURUSD",
        timeframe="D",
        window_size=8,
        feature_retrieval=retrieval,
        label_config={"task": "binary"},
        cache_root=tmp_path,
    )
    two = build_sequence_dataset_manifest(
        X=X,
        y=y,
        timestamps=ts,
        pair="EURUSD",
        timeframe="D",
        window_size=8,
        feature_retrieval=retrieval,
        label_config={"task": "binary"},
        cache_root=tmp_path,
    )
    assert one.cache_key == two.cache_key
    assert Path(one.tensor_bundle_path).exists()
    assert Path(one.manifest_path).exists()


def test_sequence_dataset_manifest_records_temporal_metadata(tmp_path: Path) -> None:
    X, y, ts = _frame(rows=12)
    manifest = build_sequence_dataset_manifest(
        X=X,
        y=y,
        timestamps=ts,
        pair="EURUSD",
        timeframe="D",
        window_size=6,
        feature_retrieval={
            "feature_service_name": "fx_eurusd_swing_patchtst_d",
            "feature_service_version": "svc-123",
            "feature_contract_hash": "hash-123",
            "source": "feast_historical",
        },
        label_config={"task": "binary"},
        cache_root=tmp_path,
    )
    payload = json.loads(Path(manifest.manifest_path).read_text(encoding="utf-8"))
    temporal = dict(payload["label_config"]["temporal_metadata"])
    assert temporal["timestamps_count"] == len(X)
    assert temporal["timestamps_monotonic_increasing"] is True
    assert temporal["step_count"] == len(X) - 1
    assert temporal["median_step_seconds"] == pytest.approx(86400.0)

    with np.load(manifest.tensor_bundle_path, allow_pickle=False) as bundle:
        assert "temporal_metadata_json" in bundle.files
        npz_temporal = json.loads(str(bundle["temporal_metadata_json"][0]))
    assert npz_temporal["timestamps_count"] == len(X)
    assert npz_temporal["timestamps_monotonic_increasing"] is True
