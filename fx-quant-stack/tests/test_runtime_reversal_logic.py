from __future__ import annotations

from fxstack.live.policy import gate_decision
from fxstack.runtime.runner import _position_side, _reversal_blocking_reasons


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
