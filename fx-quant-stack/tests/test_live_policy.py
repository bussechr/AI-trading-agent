from __future__ import annotations

from fxstack.live.policy import compute_expected_edge_bps, gate_decision, normalize_spread_bps


def test_compute_expected_edge_bps_from_ret_1() -> None:
    out = compute_expected_edge_bps({"ret_1": 0.0012})
    assert round(float(out), 6) == 12.0


def test_normalize_spread_bps_from_price_units_eurusd() -> None:
    spread_bps, source = normalize_spread_bps(row={"pair": "EURUSD", "mid_close": 1.1000, "spread": 0.00011})
    assert source == "row.spread_price"
    assert round(float(spread_bps), 6) == 1.0


def test_normalize_spread_bps_from_tick_pips_usdjpy() -> None:
    spread_bps, source = normalize_spread_bps(
        tick={"symbol": "USDJPY", "bid": 150.0, "ask": 150.006, "spread_pips": 0.6, "digits": 3},
        pair="USDJPY",
    )
    assert source == "tick.spread_pips"
    assert float(spread_bps) > 0.0


def test_gate_decision_emits_threshold_snapshot() -> None:
    out = gate_decision(
        swing_prob=0.7,
        entry_prob=0.7,
        trade_prob=0.7,
        spread_bps=0.6,
        expected_edge_bps=6.0,
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
    )
    assert out.allowed is True
    assert out.reason == "approved"
    assert float(out.threshold_snapshot["max_spread_bps"]) == 2.5
    assert out.spread_unit_source == "tick.spread_bps"
