from __future__ import annotations

import pandas as pd

from fxstack.live.scorer import LiveScorer


class _DummyModel:
    def __init__(self, *, name: str, feature_columns: list[str], out: dict[str, float]) -> None:
        self.name = name
        self.feature_columns = list(feature_columns)
        self._out = dict(out)
        self.last_input: pd.DataFrame | None = None

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        self.last_input = X.copy()
        return pd.DataFrame([self._out])


def test_live_scorer_injects_meta_conditioning_features() -> None:
    regime = _DummyModel(name="regime_hmm", feature_columns=["ret_1"], out={"p0": 0.2, "p1": 0.8})
    swing = _DummyModel(name="swing_xgb", feature_columns=["ret_1"], out={"p0": 0.3, "p1": 0.7})
    intraday = _DummyModel(name="intraday_xgb", feature_columns=["ret_1"], out={"p0": 0.4, "p1": 0.6})
    meta = _DummyModel(
        name="meta_filter_xgb",
        feature_columns=["regime_prob", "swing_prob", "entry_prob", "spread_bps"],
        out={"p0": 0.1, "p1": 0.9},
    )
    scorer = LiveScorer(regime_model=regime, swing_model=swing, intraday_model=intraday, meta_model=meta)

    row = pd.DataFrame(
        [
            {
                "pair": "GBPUSD",
                "ts": "2026-03-23T12:00:00Z",
                "ret_1": 0.001,
                "spread_bps": 0.8,
                "scenario_bucket": "trend",
            }
        ]
    )

    signal = scorer.score(
        regime_row=row,
        swing_row=row,
        intraday_row=row,
        meta_row=row,
        spread_bps=0.8,
        expected_edge_bps=4.0,
        spread_unit_source="provided",
    )

    assert meta.last_input is not None
    assert list(meta.last_input.columns) == ["regime_prob", "swing_prob", "entry_prob", "spread_bps"]
    assert float(meta.last_input.iloc[0]["regime_prob"]) == 0.8
    assert float(meta.last_input.iloc[0]["swing_prob"]) == 0.7
    assert float(meta.last_input.iloc[0]["entry_prob"]) == 0.6
    assert float(signal.trade_prob) == 0.9
