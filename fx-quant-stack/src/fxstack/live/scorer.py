from __future__ import annotations

import pandas as pd

from fxstack.live.execution_gate import should_trade
from fxstack.schemas.signals import LiveSignal


class LiveScorer:
    def __init__(self, regime_model, swing_model, intraday_model, meta_model) -> None:
        self.regime_model = regime_model
        self.swing_model = swing_model
        self.intraday_model = intraday_model
        self.meta_model = meta_model

    @staticmethod
    def _model_input(model, x_num: pd.DataFrame) -> pd.DataFrame:
        cols = list(getattr(model, "feature_columns", []) or [])
        if cols:
            missing = [c for c in cols if c not in x_num.columns]
            if missing:
                raise ValueError(f"missing feature columns: {','.join(missing)}")
            return x_num[cols]

        # Backward compatibility for older RegimeHMM artifacts without persisted feature columns.
        if str(getattr(model, "name", "")) == "regime_hmm":
            regime_cols = ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"]
            if all(c in x_num.columns for c in regime_cols):
                return x_num[regime_cols]
        return x_num

    def score(self, row: pd.DataFrame, *, spread_bps: float, expected_edge_bps: float) -> LiveSignal:
        x_num = row.select_dtypes(include=["number"]).copy()
        regime = self.regime_model.predict_proba(self._model_input(self.regime_model, x_num))
        swing = self.swing_model.predict_proba(self._model_input(self.swing_model, x_num))
        intraday = self.intraday_model.predict_proba(self._model_input(self.intraday_model, x_num))
        meta = self.meta_model.predict_proba(self._model_input(self.meta_model, x_num))

        regime_prob = float(regime.iloc[0].max())
        swing_prob = float(swing.iloc[0]["p1"])
        entry_prob = float(intraday.iloc[0]["p1"])
        trade_prob = float(meta.iloc[0]["p1"])
        side = "long" if swing_prob >= 0.5 else "short"

        gate = should_trade(
            swing_prob=swing_prob,
            entry_prob=entry_prob,
            trade_prob=trade_prob,
            spread_bps=float(spread_bps),
            expected_edge_bps=float(expected_edge_bps),
        )

        return LiveSignal(
            pair=str(row.iloc[0].get("pair", "")),
            ts=str(row.iloc[0].get("ts", "")),
            regime_prob=regime_prob,
            swing_prob=swing_prob,
            entry_prob=entry_prob,
            trade_prob=trade_prob,
            side=side,
            expected_edge_bps=float(expected_edge_bps),
            spread_bps=float(spread_bps),
            allowed=bool(gate.allowed),
            rejection_reason=str(gate.reason if not gate.allowed else "none"),
        )
