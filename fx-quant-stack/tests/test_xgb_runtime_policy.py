from __future__ import annotations

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
    monkeypatch.setattr("fxstack.models._xgb_base.probe_xgb_cuda_capability", lambda: {"ok": False, "detail": "no_cuda"})
    monkeypatch.setattr("fxstack.models._xgb_base.xgb.XGBClassifier", _FakeXGBClassifier)

    X, y = _xy()
    m = XGBBinaryModel(params={"use_calibration": False})
    m.fit(X, y)
    assert str(m.runtime["selected_device"]) == "cpu"
    assert str(m.runtime["used_device"]).startswith("cpu")



def test_xgb_cuda_falls_back_to_cpu_when_enabled(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FXSTACK_XGB_DEVICE", "cuda")
    monkeypatch.setenv("FXSTACK_XGB_ALLOW_CPU_FALLBACK", "1")
    monkeypatch.setattr("fxstack.models._xgb_base.probe_xgb_cuda_capability", lambda: {"ok": True, "detail": "ok"})
    monkeypatch.setattr("fxstack.models._xgb_base.xgb.XGBClassifier", _FakeXGBClassifier)

    X, y = _xy()
    m = XGBBinaryModel(params={"use_calibration": False})
    m.fit(X, y)
    assert bool(m.runtime["fallback_used"]) is True
    assert str(m.runtime["used_device"]).startswith("cpu")



def test_xgb_cuda_strict_fails_without_fallback(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("FXSTACK_XGB_DEVICE", "cuda")
    monkeypatch.setenv("FXSTACK_XGB_ALLOW_CPU_FALLBACK", "0")
    monkeypatch.setattr("fxstack.models._xgb_base.probe_xgb_cuda_capability", lambda: {"ok": False, "detail": "no_cuda"})

    with pytest.raises(RuntimeError, match="CUDA requested"):
        XGBBinaryModel(params={"use_calibration": False})
