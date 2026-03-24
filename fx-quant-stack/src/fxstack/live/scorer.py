from __future__ import annotations

import pandas as pd

from fxstack.live.execution_gate import should_trade
from fxstack.live.policy import compute_expected_edge_bps, normalize_spread_bps
from fxstack.schemas.signals import LiveSignal
from fxstack.settings import get_settings


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

    @staticmethod
    def _enrich_meta_input(
        model,
        x_in: pd.DataFrame,
        *,
        regime_prob: float,
        swing_prob: float,
        entry_prob: float,
        side: str,
    ) -> pd.DataFrame:
        x = x_in.copy()
        required = set(getattr(model, "feature_columns", []) or [])
        side_norm = str(side).strip().lower()
        side_flag = 1.0 if side_norm == "long" else -1.0
        derived: dict[str, float] = {
            "regime_prob": float(regime_prob),
            "swing_prob": float(swing_prob),
            "entry_prob": float(entry_prob),
            "candidate_side": float(side_flag),
            "side_long": 1.0 if side_norm == "long" else 0.0,
            "side_short": 1.0 if side_norm == "short" else 0.0,
        }
        for key, value in derived.items():
            if key in x.columns:
                continue
            if required and key not in required:
                continue
            x[key] = float(value)
        return x.select_dtypes(include=["number"]).copy()

    def score(
        self,
        row: pd.DataFrame | None = None,
        *,
        regime_row: pd.DataFrame | None = None,
        swing_row: pd.DataFrame | None = None,
        intraday_row: pd.DataFrame | None = None,
        meta_row: pd.DataFrame | None = None,
        spread_bps: float | None,
        expected_edge_bps: float | None,
        spread_unit_source: str = "provided",
    ) -> LiveSignal:
        base_row = intraday_row if intraday_row is not None else row
        if base_row is None or base_row.empty:
            raise ValueError("missing intraday/base feature row")
        regime_input_row = regime_row if regime_row is not None else base_row
        swing_input_row = swing_row if swing_row is not None else base_row
        intraday_input_row = intraday_row if intraday_row is not None else base_row
        meta_input_row = meta_row if meta_row is not None else intraday_input_row

        regime = self.regime_model.predict_proba(
            self._model_input(self.regime_model, regime_input_row.select_dtypes(include=["number"]).copy())
        )
        swing = self.swing_model.predict_proba(
            self._model_input(self.swing_model, swing_input_row.select_dtypes(include=["number"]).copy())
        )
        intraday = self.intraday_model.predict_proba(
            self._model_input(self.intraday_model, intraday_input_row.select_dtypes(include=["number"]).copy())
        )

        regime_prob = float(regime.iloc[0].max())
        swing_prob = float(swing.iloc[0]["p1"])
        entry_prob = float(intraday.iloc[0]["p1"])
        side = "long" if swing_prob >= 0.5 else "short"
        meta = self.meta_model.predict_proba(
            self._model_input(
                self.meta_model,
                self._enrich_meta_input(
                    self.meta_model,
                    meta_input_row,
                    regime_prob=regime_prob,
                    swing_prob=swing_prob,
                    entry_prob=entry_prob,
                    side=side,
                ),
            )
        )
        trade_prob = float(meta.iloc[0]["p1"])

        s = get_settings()
        edge = float(
            compute_expected_edge_bps(
                intraday_input_row,
                swing_prob=float(swing_prob),
                entry_prob=float(entry_prob),
                trade_prob=float(trade_prob),
                regime_prob=float(regime_prob),
                side=side,
            )
            if expected_edge_bps is None
            else expected_edge_bps
        )
        if spread_bps is None:
            spread, spread_source = normalize_spread_bps(
                row=intraday_input_row.iloc[0],
                pair=str(intraday_input_row.iloc[0].get("pair", "")),
            )
        else:
            spread = float(spread_bps)
            spread_source = str(spread_unit_source or "provided")

        gate = should_trade(
            swing_prob=swing_prob,
            entry_prob=entry_prob,
            trade_prob=trade_prob,
            spread_bps=float(spread),
            expected_edge_bps=float(edge),
            side=side,
            min_swing_prob=float(s.min_swing_prob),
            min_entry_prob=float(s.min_entry_prob),
            min_trade_prob=float(s.min_trade_prob),
            max_spread_bps=float(s.max_allowed_spread_bps),
            min_expected_edge_bps=float(s.min_expected_edge_bps),
            spread_unit_source=spread_source,
        )

        return LiveSignal(
            pair=str(intraday_input_row.iloc[0].get("pair", "")),
            ts=str(intraday_input_row.iloc[0].get("ts", "")),
            regime_prob=regime_prob,
            swing_prob=swing_prob,
            entry_prob=entry_prob,
            trade_prob=trade_prob,
            side=side,
            expected_edge_bps=float(edge),
            spread_bps=float(spread),
            allowed=bool(gate.allowed),
            rejection_reason=str(gate.reason if not gate.allowed else "none"),
            policy_version=str(gate.policy_version),
            edge_formula_id=str(gate.edge_formula_id),
            threshold_snapshot=dict(gate.threshold_snapshot),
            spread_unit_source=str(gate.spread_unit_source),
            scenario_bucket=str(intraday_input_row.iloc[0].get("scenario_bucket", "unknown")),
            context_frame_profile=str(intraday_input_row.iloc[0].get("context_frame_profile", "baseline_v2")),
            uncertainty_score=float(intraday_input_row.iloc[0].get("uncertainty_score", 0.0) or 0.0),
        )
