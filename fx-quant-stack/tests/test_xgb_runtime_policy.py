from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from fxstack.models._xgb_base import XGBBinaryModel
from fxstack.settings import get_settings


class _FakeXGBClassifier:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)

    def fit(self, X, y):
        if str(self.kwargs.get("device", "")).lower() == "cuda":
            raise RuntimeError("cuda_fit_fail")
        return self

    def predict_proba(self, X):
        n = len(X)
        return np.column_stack([np.full(n, 0.4), np.full(n, 0.6)])

    def predict(self, X):
        n = len(X)
        return np.ones(n, dtype=int)

    def save_model(self, path):
        return None

    def load_model(self, path):
        return None


def _xy():
    X = pd.DataFrame({"a": [0.1, 0.2, 0.3, 0.4], "b": [1.0, 0.9, 1.1, 1.2]})
    y = pd.Series([0, 1, 0, 1])
    return X, y


def test_xgb_auto_uses_cpu_when_cuda_probe_fails(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FXSTACK_XGB_DEVICE", "auto")
    monkeypatch.setenv("FXSTACK_XGB_ALLOW_CPU_FALLBACK", "1")
    monkeypatch.setattr(
        "fxstack.models._xgb_base.probe_xgb_cuda_capability",
        lambda: {"ok": False, "detail": "no_cuda"},
    )
    monkeypatch.setattr(
        "fxstack.models._xgb_base.xgb.XGBClassifier", _FakeXGBClassifier
    )

    X, y = _xy()
    m = XGBBinaryModel(params={"use_calibration": False})
    m.fit(X, y)
    assert str(m.runtime["selected_device"]) == "cpu"
    assert str(m.runtime["used_device"]).startswith("cpu")


def test_xgb_cuda_falls_back_to_cpu_when_enabled(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FXSTACK_XGB_DEVICE", "cuda")
    monkeypatch.setenv("FXSTACK_XGB_ALLOW_CPU_FALLBACK", "1")
    monkeypatch.setattr(
        "fxstack.models._xgb_base.probe_xgb_cuda_capability",
        lambda: {"ok": True, "detail": "ok"},
    )
    monkeypatch.setattr(
        "fxstack.models._xgb_base.xgb.XGBClassifier", _FakeXGBClassifier
    )

    X, y = _xy()
    m = XGBBinaryModel(params={"use_calibration": False})
    m.fit(X, y)
    assert bool(m.runtime["fallback_used"]) is True
    assert str(m.runtime["used_device"]).startswith("cpu")


def test_xgb_cuda_strict_fails_without_fallback(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FXSTACK_XGB_DEVICE", "cuda")
    monkeypatch.setenv("FXSTACK_XGB_ALLOW_CPU_FALLBACK", "0")
    monkeypatch.setattr(
        "fxstack.models._xgb_base.probe_xgb_cuda_capability",
        lambda: {"ok": False, "detail": "no_cuda"},
    )

    with pytest.raises(RuntimeError, match="CUDA requested"):
        XGBBinaryModel(params={"use_calibration": False})


def test_xgb_cpu_selection_does_not_probe_cuda(monkeypatch):
    def _unexpected_probe():
        raise AssertionError("CPU-only construction must not initialize CUDA")

    monkeypatch.setattr(
        "fxstack.models._xgb_base.probe_xgb_cuda_capability", _unexpected_probe
    )
    monkeypatch.setattr(
        "fxstack.models._xgb_base.xgb.XGBClassifier", _FakeXGBClassifier
    )

    model = XGBBinaryModel(params={"device": "cpu", "use_calibration": False})

    assert model.runtime["selected_device"] == "cpu"
    assert model.runtime["inference_device"] == "cpu"
    assert model.runtime["cuda_probe"] == {"ok": False, "detail": "not_requested"}


def test_xgb_strict_cuda_fit_does_not_silently_attempt_cpu(monkeypatch):
    attempted_devices: list[str] = []

    class _StrictFakeXGBClassifier(_FakeXGBClassifier):
        def fit(self, X, y):
            device = str(self.kwargs.get("device", "legacy_cpu"))
            attempted_devices.append(device)
            raise RuntimeError(f"{device}_fit_fail")

    monkeypatch.setattr(
        "fxstack.models._xgb_base.probe_xgb_cuda_capability",
        lambda: {"ok": True, "detail": "ok"},
    )
    monkeypatch.setattr(
        "fxstack.models._xgb_base.xgb.XGBClassifier", _StrictFakeXGBClassifier
    )

    X, y = _xy()
    model = XGBBinaryModel(
        params={"device": "cuda", "allow_cpu_fallback": False, "use_calibration": False}
    )
    with pytest.raises(RuntimeError, match="xgb_fit_failed:primary"):
        model.fit(X, y)

    assert attempted_devices == ["cuda"]


def test_loaded_xgb_pins_cpu_inference_without_losing_training_provenance(
    tmp_path, monkeypatch
):
    X, y = _xy()
    model = XGBBinaryModel(
        params={
            "device": "cpu",
            "n_estimators": 4,
            "max_depth": 2,
            "use_calibration": False,
        }
    )
    model.fit(X, y)
    model.runtime.update(
        {
            "requested_device": "cuda",
            "selected_device": "cuda",
            "used_device": "cuda",
            "inference_device": "cuda",
        }
    )
    artifact = tmp_path / "cuda_provenance_model"
    model.save(artifact)

    def _unexpected_probe():
        raise AssertionError("artifact loading must not probe CUDA")

    monkeypatch.setattr(
        "fxstack.models._xgb_base.probe_xgb_cuda_capability", _unexpected_probe
    )
    loaded = XGBBinaryModel.load(artifact)

    assert loaded.runtime["used_device"] == "cuda"
    assert loaded.runtime["inference_device"] == "cpu"
    assert loaded.model.get_params()["device"] == "cpu"
    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=".*mismatched devices.*")
        probabilities = loaded.predict_proba(X)
    assert probabilities.shape == (len(X), 2)


def test_cuda_prediction_uses_device_aligned_path_when_available():
    from fxstack.models._xgb_runtime import probe_xgb_cuda_capability

    capability = probe_xgb_cuda_capability()
    if not bool(capability.get("ok")):
        pytest.skip(f"CUDA unavailable: {capability.get('detail', '')}")

    rows = 96
    X = pd.DataFrame(
        {
            "a": np.linspace(-1.0, 1.0, rows),
            "b": np.sin(np.arange(rows, dtype=float) / 7.0),
        }
    )
    y = pd.Series((np.arange(rows) % 3 == 0).astype(int))
    model = XGBBinaryModel(
        params={
            "device": "cuda",
            "allow_cpu_fallback": False,
            "n_estimators": 8,
            "max_depth": 2,
            "use_calibration": True,
            "calibration_min_rows": 16,
            "calibration_embargo_rows": 2,
        }
    )

    with warnings.catch_warnings():
        warnings.filterwarnings("error", message=".*mismatched devices.*")
        model.fit(X, y)
        probabilities = model.predict_proba(X.tail(8))
        predictions = model.predict(X.tail(8))

    assert model.runtime["used_device"] == "cuda"
    assert model.runtime["inference_device"] == "cuda"
    assert probabilities.shape == (8, 2)
    assert predictions.shape == (8,)
