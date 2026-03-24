from __future__ import annotations

from dataclasses import dataclass, field

from fxstack.live.policy import EDGE_FORMULA_ID, POLICY_VERSION, gate_decision
from fxstack.settings import get_settings

@dataclass(slots=True)
class GateDecision:
    allowed: bool
    reason: str
    policy_version: str = POLICY_VERSION
    edge_formula_id: str = EDGE_FORMULA_ID
    threshold_snapshot: dict[str, float] = field(default_factory=dict)
    spread_unit_source: str = "unknown"


def should_trade(
    *,
    swing_prob: float,
    entry_prob: float,
    trade_prob: float,
    spread_bps: float,
    expected_edge_bps: float,
    side: str | None = None,
    min_swing_prob: float | None = None,
    min_entry_prob: float | None = None,
    min_trade_prob: float | None = None,
    max_spread_bps: float | None = None,
    min_expected_edge_bps: float | None = None,
    spread_unit_source: str = "unknown",
) -> GateDecision:
    s = get_settings()
    out = gate_decision(
        swing_prob=float(swing_prob),
        entry_prob=float(entry_prob),
        trade_prob=float(trade_prob),
        spread_bps=float(spread_bps),
        expected_edge_bps=float(expected_edge_bps),
        side=side,
        min_swing_prob=float(s.min_swing_prob if min_swing_prob is None else min_swing_prob),
        min_entry_prob=float(s.min_entry_prob if min_entry_prob is None else min_entry_prob),
        min_trade_prob=float(s.min_trade_prob if min_trade_prob is None else min_trade_prob),
        max_spread_bps=float(s.max_allowed_spread_bps if max_spread_bps is None else max_spread_bps),
        min_expected_edge_bps=float(
            s.min_expected_edge_bps if min_expected_edge_bps is None else min_expected_edge_bps
        ),
        spread_unit_source=str(spread_unit_source or "unknown"),
    )
    return GateDecision(
        allowed=bool(out.allowed),
        reason=str(out.reason),
        policy_version=str(out.policy_version),
        edge_formula_id=str(out.edge_formula_id),
        threshold_snapshot=dict(out.threshold_snapshot),
        spread_unit_source=str(out.spread_unit_source),
    )
