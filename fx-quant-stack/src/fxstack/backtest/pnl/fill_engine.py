from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .execution_costs import ExecutionCostModel, conservative_fx_cost_model
from .signal_adapter import SimSignal


@dataclass(slots=True)
class FillPlan:
    event_type: str
    pair: str
    side: str
    ts: str
    requested_lots: float
    fill_price: float
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    expected_edge_bps: float = 0.0
    cost_bps: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FillResult:
    plan: FillPlan
    filled_lots: float
    avg_fill_price: float
    realized_pnl_usd: float = 0.0
    net_edge_bps: float = 0.0
    accepted: bool = True
    rejection_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def build_fill_plan(
    signal: SimSignal | dict[str, Any],
    *,
    bid: float,
    ask: float,
    mid: float,
    requested_lots: float,
    cost_model: ExecutionCostModel | None = None,
    event_type: str | None = None,
) -> FillPlan:
    sig = signal if isinstance(signal, SimSignal) else SimSignal(**dict(signal or {}))
    model = cost_model or conservative_fx_cost_model()
    event = str(event_type or sig.lifecycle_action or sig.entry_action or "entry").strip().lower()
    side = str(sig.side or "hold").lower()
    is_buy = side in {"long", "buy"}
    base_price = float(ask if is_buy else bid)
    cost_bps = model.cost_for(event)
    slippage_px = base_price * (cost_bps / 10000.0)
    fill_price = base_price + slippage_px if is_buy else base_price - slippage_px
    return FillPlan(
        event_type=event,
        pair=str(sig.pair).upper(),
        side=side,
        ts=str(sig.ts),
        requested_lots=max(0.0, float(requested_lots)),
        fill_price=float(fill_price if fill_price > 0.0 else mid),
        stop_price=float(sig.metadata.get("stop_price", 0.0) or 0.0),
        take_profit_price=float(sig.metadata.get("take_profit_price", 0.0) or 0.0),
        expected_edge_bps=float(sig.expected_edge_bps),
        cost_bps=float(cost_bps),
        metadata=dict(sig.metadata or {}),
    )


@dataclass(slots=True)
class FillEngine:
    cost_model: ExecutionCostModel = field(default_factory=conservative_fx_cost_model)
    min_fill_lots: float = 0.01
    lot_step: float = 0.01
    max_spread_bps: float = 3.0
    stale_penalty_bps: float = 1.0

    def execute(
        self,
        signal: SimSignal | dict[str, Any],
        *,
        bid: float,
        ask: float,
        mid: float,
        requested_lots: float,
        event_type: str | None = None,
        stale: bool = False,
    ) -> FillResult:
        plan = build_fill_plan(
            signal,
            bid=bid,
            ask=ask,
            mid=mid,
            requested_lots=requested_lots,
            cost_model=self.cost_model,
            event_type=event_type,
        )
        spread_bps = abs(float(ask) - float(bid)) / max(float(mid), 1e-9) * 10000.0
        if spread_bps > float(self.max_spread_bps):
            return FillResult(
                plan=plan,
                filled_lots=0.0,
                avg_fill_price=0.0,
                accepted=False,
                rejection_reason="spread_too_wide",
                metadata={"spread_bps": float(spread_bps)},
            )
        lots = max(0.0, float(requested_lots))
        rounded = max(0.0, round(lots / max(self.lot_step, 1e-9)) * self.lot_step)
        if rounded < float(self.min_fill_lots):
            return FillResult(plan=plan, filled_lots=0.0, avg_fill_price=0.0, accepted=False, rejection_reason="below_min_fill_lots")
        net_cost_bps = float(plan.cost_bps + (self.stale_penalty_bps if stale else 0.0))
        signed_edge = float(plan.expected_edge_bps) - net_cost_bps
        if signed_edge <= 0.0:
            return FillResult(
                plan=plan,
                filled_lots=0.0,
                avg_fill_price=0.0,
                accepted=False,
                rejection_reason="negative_net_edge",
                net_edge_bps=float(signed_edge),
                metadata={"stale": bool(stale), "net_cost_bps": net_cost_bps},
            )
        return FillResult(
            plan=plan,
            filled_lots=float(rounded),
            avg_fill_price=float(plan.fill_price),
            realized_pnl_usd=0.0,
            net_edge_bps=float(signed_edge),
            accepted=True,
            rejection_reason="",
            metadata={"stale": bool(stale), "net_cost_bps": net_cost_bps},
        )

