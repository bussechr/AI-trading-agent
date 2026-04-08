from __future__ import annotations

import pandas as pd

from fxstack.live.scorer import LiveScorer
from fxstack.settings import get_settings


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
    assert signal.model_intelligence_score > signal.heuristic_penalty_score
    assert signal.fallback_used is False
    assert signal.fallback_reason == "none"
    assert signal.decision_source_chain[-1] == "gate:approved"
    payload = signal.to_dict()
    assert payload["belief_source_mode"] == "disabled"
    assert payload["belief_primary_scenario"] == ""
    assert payload["belief_primary_rank_score"] == 0.0
    assert payload["belief_primary_ev_above_hurdle_prob"] == 0.0
    assert payload["belief_primary_expected_net_ev_bps"] == 0.0
    assert payload["belief_primary_fail_fast_prob"] == 0.0
    assert payload["belief_no_edge"] is False
    assert payload["model_intelligence_score"] == signal.model_intelligence_score
    assert payload["heuristic_penalty_score"] == signal.heuristic_penalty_score
    assert payload["strategy_engine_mode"] == "supervised_legacy"
    assert payload["fallback_used"] is False
    assert payload["fallback_reason"] == "none"
    assert payload["decision_source_chain"][-1] == "gate:approved"


def test_live_scorer_reflects_non_legacy_strategy_engine_mode(monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_STRATEGY_ENGINE_MODE", "rl_primary")
    get_settings.cache_clear()
    try:
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
    finally:
        get_settings.cache_clear()

    assert signal.strategy_engine_mode == "rl_primary"
    assert signal.rl_lifecycle_intent == "entry_intent"
    assert signal.rl_lifecycle_reason == "rl_primary_entry_approved"
    assert signal.rl_lifecycle_fallback_reason == "rl_primary:none"
    assert signal.rl_flip_intent is False
    assert signal.rl_rebalance_intent is False
    assert signal.fallback_reason == "rl_primary:none"
    assert signal.decision_source_chain[0] == "strategy_engine_mode:rl_primary"
    assert "lifecycle:rl_primary_entry_approved" in signal.decision_source_chain


def test_live_scorer_reflects_rl_flip_and_rebalance_intents(monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_STRATEGY_ENGINE_MODE", "rl_primary")
    get_settings.cache_clear()
    try:
        regime = _DummyModel(name="regime_hmm", feature_columns=["ret_1"], out={"p0": 0.2, "p1": 0.8})
        swing = _DummyModel(name="swing_xgb", feature_columns=["ret_1"], out={"p0": 0.3, "p1": 0.7})
        intraday = _DummyModel(name="intraday_xgb", feature_columns=["ret_1"], out={"p0": 0.4, "p1": 0.6})
        meta = _DummyModel(
            name="meta_filter_xgb",
            feature_columns=["regime_prob", "swing_prob", "entry_prob", "spread_bps"],
            out={"p0": 0.1, "p1": 0.9},
        )
        scorer = LiveScorer(regime_model=regime, swing_model=swing, intraday_model=intraday, meta_model=meta)

        flip_row = pd.DataFrame(
            [
                {
                    "pair": "GBPUSD",
                    "ts": "2026-03-23T12:00:00Z",
                    "ret_1": 0.001,
                    "spread_bps": 0.8,
                    "scenario_bucket": "trend",
                    "current_position_side": "short",
                    "current_position_size": 0.4,
                    "rl_target_position": 0.5,
                }
            ]
        )
        rebalance_row = pd.DataFrame(
            [
                {
                    "pair": "EURUSD",
                    "ts": "2026-03-23T12:00:00Z",
                    "ret_1": 0.001,
                    "spread_bps": 0.8,
                    "scenario_bucket": "trend",
                    "current_position_side": "long",
                    "current_position_size": 0.7,
                    "rl_target_position": 0.3,
                }
            ]
        )

        flip_signal = scorer.score(
            regime_row=flip_row,
            swing_row=flip_row,
            intraday_row=flip_row,
            meta_row=flip_row,
            spread_bps=0.8,
            expected_edge_bps=4.0,
            spread_unit_source="provided",
        )
        rebalance_signal = scorer.score(
            regime_row=rebalance_row,
            swing_row=rebalance_row,
            intraday_row=rebalance_row,
            meta_row=rebalance_row,
            spread_bps=0.8,
            expected_edge_bps=4.0,
            spread_unit_source="provided",
        )
    finally:
        get_settings.cache_clear()

    assert flip_signal.rl_lifecycle_intent == "flip_intent"
    assert flip_signal.rl_flip_intent is True
    assert flip_signal.rl_rebalance_intent is False
    assert flip_signal.rl_lifecycle_reason == "rl_primary_flip_intent"
    assert "lifecycle:rl_primary_flip_intent" in flip_signal.decision_source_chain
    assert rebalance_signal.rl_lifecycle_intent == "rebalance_intent"
    assert rebalance_signal.rl_flip_intent is False
    assert rebalance_signal.rl_rebalance_intent is True
    assert rebalance_signal.rl_lifecycle_reason == "rl_primary_rebalance_intent"
    assert "lifecycle:rl_primary_rebalance_intent" in rebalance_signal.decision_source_chain
