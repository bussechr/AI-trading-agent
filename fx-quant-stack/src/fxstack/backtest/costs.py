from __future__ import annotations


def all_in_cost_bps(*, spread_bps: float, slippage_bps: float) -> float:
    return float(max(0.0, spread_bps) + max(0.0, slippage_bps))
