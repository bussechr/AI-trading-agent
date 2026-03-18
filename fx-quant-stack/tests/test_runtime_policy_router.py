from __future__ import annotations

import pandas as pd
import pytest

from fxstack.runtime.runner import _PolicyModelRouter


class _GoodModel:
    def __init__(self, p1: float) -> None:
        self.p1 = float(p1)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        p1 = pd.Series([self.p1] * len(X), index=X.index)
        return pd.DataFrame({"p0": 1.0 - p1, "p1": p1}, index=X.index)


class _BadModel:
    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        raise RuntimeError("boom")


def test_policy_router_uses_primary_when_available():
    router = _PolicyModelRouter(
        policy="transformer_primary_xgb_fallback",
        family="swing",
        primary_name="swing_transformer",
        primary_model=_GoodModel(0.9),
        fallback_name="swing_xgb",
        fallback_model=_GoodModel(0.1),
    )
    out = router.predict_proba(pd.DataFrame({"x": [1.0, 2.0]}))
    diag = router.diagnostics()
    assert float(out["p1"].iloc[0]) == 0.9
    assert str(diag["selected_model"]) == "swing_transformer"
    assert bool(diag["used_fallback"]) is False
    assert str(diag["fallback_reason"]) == "none"


def test_policy_router_falls_back_when_primary_errors():
    router = _PolicyModelRouter(
        policy="tcn_primary_xgb_fallback",
        family="intraday",
        primary_name="intraday_tcn",
        primary_model=_BadModel(),
        fallback_name="intraday_xgb",
        fallback_model=_GoodModel(0.4),
    )
    out = router.predict_proba(pd.DataFrame({"x": [3.0]}))
    diag = router.diagnostics()
    assert float(out["p1"].iloc[0]) == 0.4
    assert str(diag["selected_model"]) == "intraday_xgb"
    assert bool(diag["used_fallback"]) is True
    assert "intraday_tcn_inference_error" in str(diag["fallback_reason"])


def test_policy_router_raises_when_no_model_available():
    router = _PolicyModelRouter(
        policy="transformer_primary_xgb_fallback",
        family="swing",
        primary_name="swing_transformer",
        primary_model=None,
        fallback_name="swing_xgb",
        fallback_model=None,
    )
    with pytest.raises(RuntimeError):
        router.predict_proba(pd.DataFrame({"x": [1.0]}))
