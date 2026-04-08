from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


def all_in_cost_bps(*, spread_bps: float, slippage_bps: float) -> float:
    return float(max(0.0, spread_bps) + max(0.0, slippage_bps))


def apply_bps_slippage(*, price: float, action: str, slippage_bps: float) -> float:
    px = float(price)
    bps = max(0.0, float(slippage_bps))
    if px <= 0.0 or bps <= 0.0:
        return px
    factor = bps / 10000.0
    action_key = str(action or "").strip().lower()
    if action_key in {"buy_open", "short_close"}:
        return px * (1.0 + factor)
    if action_key in {"sell_open", "long_close"}:
        return px * (1.0 - factor)
    return px


@dataclass(slots=True)
class ExecutionCostModel:
    spread_bps: float = 1.2
    slippage_bps: float = 0.3
    commission_bps: float = 0.0
    adverse_selection_bps: float = 0.0
    partial_exit_fee_bps: float = 0.0
    stop_slippage_multiplier: float = 1.25
    reversal_slippage_multiplier: float = 1.10
    stale_exit_slippage_multiplier: float = 1.35

    def all_in_cost_bps(self) -> float:
        return float(
            max(0.0, self.spread_bps)
            + max(0.0, self.slippage_bps)
            + max(0.0, self.commission_bps)
            + max(0.0, self.adverse_selection_bps)
        )

    def cost_for(self, event_type: str) -> float:
        event = str(event_type or "").strip().lower()
        mult = 1.0
        if event in {"stop", "tighten_stop"}:
            mult = self.stop_slippage_multiplier
        elif event in {"reversal_exit", "reversal"}:
            mult = self.reversal_slippage_multiplier
        elif event in {"stale_exit", "stale"}:
            mult = self.stale_exit_slippage_multiplier
        return float(
            max(0.0, self.spread_bps)
            + (max(0.0, self.slippage_bps) * float(mult))
            + max(0.0, self.commission_bps)
            + max(0.0, self.adverse_selection_bps)
            + max(0.0, self.partial_exit_fee_bps if event in {"partial_tp", "partial_close"} else 0.0)
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def conservative_fx_cost_model() -> ExecutionCostModel:
    return ExecutionCostModel(
        spread_bps=1.5,
        slippage_bps=0.5,
        commission_bps=0.0,
        adverse_selection_bps=0.15,
        partial_exit_fee_bps=0.0,
        stop_slippage_multiplier=1.35,
        reversal_slippage_multiplier=1.15,
        stale_exit_slippage_multiplier=1.50,
    )
