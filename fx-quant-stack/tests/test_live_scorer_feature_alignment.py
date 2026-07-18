from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxstack.live.scorer import LiveScorer
from fxstack.models.regime_hmm import RegimeHMM


class _DummyRegimeModel:
    name = "regime_hmm"

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        assert list(X.columns) == ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"]
        return pd.DataFrame({"state_0": [0.1], "state_1": [0.9]}, index=X.index)


class _DummyBinaryModel:
    def __init__(self, *, p1: float, feature_columns: list[str]) -> None:
        self.feature_columns = list(feature_columns)
        self._p1 = float(p1)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        assert list(X.columns) == self.feature_columns
        return pd.DataFrame({"p0": [1.0 - self._p1], "p1": [self._p1]}, index=X.index)


def test_live_scorer_aligns_features_for_models() -> None:
    scorer = LiveScorer(
        regime_model=_DummyRegimeModel(),
        swing_model=_DummyBinaryModel(p1=0.9, feature_columns=["ret_1", "ret_5"]),
        intraday_model=_DummyBinaryModel(p1=0.9, feature_columns=["ret_1", "vol_20"]),
        meta_model=_DummyBinaryModel(p1=0.9, feature_columns=["ret_1"]),
    )

    row = pd.DataFrame(
        [
            {
                "pair": "EURUSD",
                "ts": "2026-03-17T00:00:00Z",
                "ret_1": 0.001,
                "ret_5": 0.002,
                "vol_20": 0.003,
                "vol_60": 0.004,
                "trend_slope_20": 0.0005,
                "extra_numeric": 123.0,
            }
        ]
    )

    sig = scorer.score(row, spread_bps=0.1, expected_edge_bps=10.0)
    assert sig.pair == "EURUSD"
    assert sig.side == "long"
    assert sig.allowed is True


def test_regime_hmm_persists_feature_columns(tmp_path) -> None:
    X_train = pd.DataFrame(
        {
            "ret_1": [0.001, 0.002, -0.001, 0.003, -0.002, 0.0005],
            "ret_5": [0.002, 0.003, -0.002, 0.004, -0.003, 0.0008],
        }
    )
    m = RegimeHMM(n_components=2, random_state=7)
    m.fit(X_train)
    out = tmp_path / "regime_hmm"
    m.save(out)

    loaded = RegimeHMM.load(out)
    assert loaded.feature_columns == ["ret_1", "ret_5"]

    X_infer = pd.DataFrame(
        {
            "ret_1": [0.0015],
            "ret_5": [0.0025],
            "unused": [123.0],
        }
    )
    proba = loaded.predict_proba(X_infer)
    assert list(proba.columns) == ["state_0", "state_1"]
    assert len(proba) == 1


def test_regime_hmm_imputes_non_finite_values_with_persisted_training_medians(tmp_path) -> None:
    X_train = pd.DataFrame(
        {
            "ret_1": [0.001, 0.002, np.nan, 0.003, -0.002, np.inf, 0.0005, -0.0004],
            "vol_20": [0.01, 0.02, 0.03, np.nan, 0.04, 0.02, -np.inf, 0.01],
        }
    )
    model = RegimeHMM(n_components=2, random_state=7)
    model.fit(X_train)

    assert np.isclose(model.feature_fill_values["ret_1"], 0.00075)
    assert np.isclose(model.feature_fill_values["vol_20"], 0.02)
    out = tmp_path / "regime_hmm_non_finite"
    model.save(out)
    loaded = RegimeHMM.load(out)
    assert loaded.feature_fill_values == model.feature_fill_values

    proba = loaded.predict_proba(
        pd.DataFrame({"ret_1": [np.nan, np.inf], "vol_20": [-np.inf, np.nan]})
    )
    assert np.isfinite(proba.to_numpy()).all()
    np.testing.assert_allclose(proba.sum(axis=1).to_numpy(), np.ones(2))


def test_regime_hmm_rejects_constant_training_matrix() -> None:
    model = RegimeHMM(n_components=2, random_state=7)

    with pytest.raises(ValueError, match="regime features have no variance"):
        model.fit(pd.DataFrame({"ret_1": [0.0] * 8, "vol_20": [0.0] * 8}))
