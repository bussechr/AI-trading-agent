from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .signal_adapter import SimSignal


LifecycleAction = Literal["hold", "entry", "partial_tp", "exit", "tighten_stop", "reduce"]


@dataclass(slots=True)
class LifecycleState:
    pair: str
    side: str
    lots: float
    entry_price: float
    open_ts: str
    campaign_state: str = "inactive"
    campaign_reason: str = ""
    partial_close_count: int = 0
    last_partial_ts: str = ""
    stop_price: float = 0.0
    take_profit_price: float = 0.0
    unrealized_pnl_usd: float = 0.0
    age_bars: int = 0
    stale: bool = False
    reversal_ready: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LifecycleEvent:
    action: str
    reason: str
    close_lots: float = 0.0
    new_stop_price: float = 0.0
    campaign_state: str = ""
    campaign_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def next_lifecycle_event(
    *,
    state: LifecycleState,
    signal: SimSignal,
    exit_action_selected: str = "hold",
    exit_action_score: float = 0.0,
    max_partial_closes: int = 2,
    partial_close_fraction: float = 0.5,
    hard_time_stop_bars: int = 96,
    partial_close_cooldown_bars: int = 3,
) -> LifecycleEvent:
    if state.stale:
        return LifecycleEvent(action="exit", reason="stale_feature_bar", metadata={"stale": True})
    if state.age_bars >= int(hard_time_stop_bars):
        return LifecycleEvent(action="exit", reason="hard_time_stop")
    if signal.reversal_ready:
        return LifecycleEvent(action="exit", reason="reversal_models_exit")
    if state.campaign_state == "abandoned":
        return LifecycleEvent(action="exit", reason="adaptive_campaign_abandoned_exit")
    if state.campaign_state == "harvest" and state.unrealized_pnl_usd > 0.0:
        close_lots = max(0.0, state.lots * float(partial_close_fraction))
        if state.partial_close_count < int(max_partial_closes) and close_lots > 0.0:
            return LifecycleEvent(action="partial_tp", reason="adaptive_campaign_harvest", close_lots=close_lots)
    if exit_action_selected == "partial_tp" and state.partial_close_count < int(max_partial_closes) and state.age_bars >= int(partial_close_cooldown_bars):
        close_lots = max(0.0, state.lots * float(partial_close_fraction))
        return LifecycleEvent(action="partial_tp", reason="exit_model_partial_tp", close_lots=close_lots)
    if exit_action_selected == "exit" and exit_action_score > 0.0:
        return LifecycleEvent(action="exit", reason="exit_model_exit")
    if exit_action_selected == "tighten_stop":
        new_stop = float(state.stop_price) or float(state.entry_price)
        return LifecycleEvent(action="tighten_stop", reason="adjust_stop_buffer", new_stop_price=new_stop)
    return LifecycleEvent(action="hold", reason="hold")


def apply_lifecycle_event(state: LifecycleState, event: LifecycleEvent) -> LifecycleState:
    out = LifecycleState(**state.to_dict())
    if event.action in {"exit", "partial_tp"} and event.close_lots > 0.0:
        out.lots = max(0.0, float(out.lots) - float(event.close_lots))
        out.partial_close_count = int(out.partial_close_count) + 1
    if event.action == "exit":
        out.lots = 0.0
    if event.action == "tighten_stop" and event.new_stop_price > 0.0:
        out.stop_price = float(event.new_stop_price)
    if event.campaign_state:
        out.campaign_state = str(event.campaign_state)
    if event.campaign_reason:
        out.campaign_reason = str(event.campaign_reason)
    out.metadata = dict(out.metadata)
    out.metadata.update(event.metadata or {})
    return out

