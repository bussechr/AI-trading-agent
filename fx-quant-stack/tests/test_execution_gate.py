from __future__ import annotations

from fxstack.live.execution_gate import should_trade


def test_should_trade_rejects_wide_spread():
    out = should_trade(
        swing_prob=0.8,
        entry_prob=0.8,
        trade_prob=0.8,
        spread_bps=3.0,
        expected_edge_bps=10.0,
    )
    assert out.allowed is False
    assert out.reason == "spread_too_wide"


def test_should_trade_accepts_valid_setup():
    out = should_trade(
        swing_prob=0.7,
        entry_prob=0.7,
        trade_prob=0.7,
        spread_bps=1.0,
        expected_edge_bps=6.0,
    )
    assert out.allowed is True
    assert out.reason == "approved"


def test_should_trade_allows_small_edge_shortfall_with_rescue_margin():
    out = should_trade(
        swing_prob=0.72,
        entry_prob=0.74,
        trade_prob=0.73,
        spread_bps=0.9,
        expected_edge_bps=2.8,
        min_expected_edge_bps=3.0,
        min_expected_edge_rescue_margin_bps=0.5,
    )
    assert out.allowed is True
    assert out.reason == "approved"
