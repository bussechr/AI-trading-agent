from __future__ import annotations

import pandas as pd

from fxstack.live.policy import gate_decision
from fxstack.runtime.runner import (
    _build_lifecycle_row,
    _position_side,
    _reversal_blocking_reasons,
    _score_binary_lifecycle_model,
    _score_exit_policy_model,
)


def test_directional_short_swing_gate_uses_directional_confidence() -> None:
    out = gate_decision(
        swing_prob=0.35,
        entry_prob=0.72,
        trade_prob=0.62,
        spread_bps=1.08,
        expected_edge_bps=9.99,
        side="short",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.6,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
    )
    assert out.allowed is True
    assert out.reason == "approved"


def test_reversal_blocking_reasons_ignore_exposure_caps() -> None:
    assert _reversal_blocking_reasons(["pair_exposure_cap"]) == []
    assert _reversal_blocking_reasons(["portfolio_exposure_cap"]) == []
    assert _reversal_blocking_reasons(["pair_exposure_cap", "weak_swing"]) == ["weak_swing"]


def test_position_side_recognizes_type_and_side_variants() -> None:
    assert _position_side([{"type": 0, "symbol": "EURAUD"}]) == "long"
    assert _position_side([{"type": "1", "symbol": "EURAUD"}]) == "short"
    assert _position_side([{"side": "BUY", "symbol": "EURAUD"}]) == "long"
    assert _position_side([{"position_side": "short", "symbol": "EURAUD"}]) == "short"


def test_reversal_context_requires_opposite_open_side() -> None:
    desired_side = "long"
    pos_side = "long"
    reversal_context_active = desired_side != "flat" and pos_side != "flat" and desired_side != pos_side
    reversal_ready = reversal_context_active and True and len(_reversal_blocking_reasons(["pair_exposure_cap"])) == 0
    assert reversal_context_active is False
    assert reversal_ready is False


def test_build_lifecycle_row_injects_live_position_state() -> None:
    row = pd.DataFrame([{"ts": "2026-03-24T10:00:00Z", "edge_decay_12": 0.25, "h1_ret_1": 0.01}])
    out = _build_lifecycle_row(
        row=row,
        positions=[{"open_time": 1_800.0}],
        total_position_count=2,
        loop_ts=2_400.0,
        timeframe="M5",
    )
    assert float(out.iloc[0]["time_in_trade_bars"]) == 2.0
    assert float(out.iloc[0]["open_position_count"]) == 2.0
    assert float(out.iloc[0]["live_edge_decay"]) == 0.25
    assert float(out.iloc[0]["h1_available"]) == 1.0


def test_score_exit_policy_model_maps_class_ids_to_actions() -> None:
    class DummyExitModel:
        def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame([{"p0": 0.1, "p1": 0.7, "p2": 0.2}], index=X.index)

    out = _score_exit_policy_model(
        DummyExitModel(),
        pd.DataFrame([{"x": 1.0}]),
        action_labels={0: "hold", 1: "partial_tp", 2: "exit"},
    )
    assert out["selected"] == "partial_tp"
    assert out["score"] == 0.7
    assert out["probs"]["partial_tp"] == 0.7


def test_score_binary_lifecycle_model_returns_p1() -> None:
    class DummyBinaryModel:
        def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
            return pd.DataFrame([{"p0": 0.35, "p1": 0.65}], index=X.index)

    assert _score_binary_lifecycle_model(DummyBinaryModel(), pd.DataFrame([{"x": 1.0}])) == 0.65
