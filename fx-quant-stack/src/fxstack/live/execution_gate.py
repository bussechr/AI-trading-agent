from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    reason: str


def should_trade(
    *,
    swing_prob: float,
    entry_prob: float,
    trade_prob: float,
    spread_bps: float,
    expected_edge_bps: float,
    min_swing_prob: float = 0.58,
    min_entry_prob: float = 0.62,
    min_trade_prob: float = 0.60,
    max_spread_bps: float = 2.5,
    min_expected_edge_bps: float = 3.0,
) -> GateDecision:
    if spread_bps > max_spread_bps:
        return GateDecision(False, "spread_too_wide")
    if expected_edge_bps < min_expected_edge_bps:
        return GateDecision(False, "edge_below_hurdle")
    if swing_prob < min_swing_prob:
        return GateDecision(False, "weak_swing")
    if entry_prob < min_entry_prob:
        return GateDecision(False, "weak_entry")
    if trade_prob < min_trade_prob:
        return GateDecision(False, "meta_reject")
    return GateDecision(True, "approved")
