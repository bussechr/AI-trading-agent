from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class SimSignal:
    pair: str
    ts: str
    side: str
    allowed: bool
    entry_action: str
    lifecycle_action: str = "hold"
    lifecycle_reason: str = "hold"
    expected_edge_bps: float = 0.0
    spread_bps: float = 0.0
    trade_prob: float = 0.0
    entry_prob: float = 0.0
    swing_prob: float = 0.0
    regime_prob: float = 0.0
    exit_action_selected: str = "hold"
    reversal_ready: bool = False
    reversal_failure_prob: float = 0.0
    reversal_opportunity_prob: float = 0.0
    campaign_state: str = "inactive"
    campaign_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SignalAdapter:
    side_key: str = "side"
    ts_key: str = "ts"
    pair_key: str = "pair"
    action_key: str = "entry_action"
    allowed_key: str = "allowed"

    def adapt(self, row: dict[str, Any] | Any) -> SimSignal:
        src = dict(row or {}) if isinstance(row, dict) else dict(getattr(row, "to_dict", lambda: {})() or {})
        pair = str(src.get(self.pair_key) or src.get("symbol") or "").upper()
        ts = str(src.get(self.ts_key) or "")
        side = str(src.get(self.side_key) or src.get("direction") or "hold").lower()
        allowed = bool(src.get(self.allowed_key, True))
        return SimSignal(
            pair=pair,
            ts=ts,
            side=side,
            allowed=allowed,
            entry_action=str(src.get(self.action_key) or src.get("action") or "hold"),
            lifecycle_action=str(src.get("lifecycle_action") or "hold"),
            lifecycle_reason=str(src.get("lifecycle_reason") or "hold"),
            expected_edge_bps=float(src.get("expected_edge_bps", 0.0) or 0.0),
            spread_bps=float(src.get("spread_bps", 0.0) or 0.0),
            trade_prob=float(src.get("trade_prob", 0.0) or 0.0),
            entry_prob=float(src.get("entry_prob", 0.0) or 0.0),
            swing_prob=float(src.get("swing_prob", 0.0) or 0.0),
            regime_prob=float(src.get("regime_prob", 0.0) or 0.0),
            exit_action_selected=str(src.get("exit_action_selected") or "hold"),
            reversal_ready=bool(src.get("reversal_ready", False)),
            reversal_failure_prob=float(src.get("reversal_failure_prob", 0.0) or 0.0),
            reversal_opportunity_prob=float(src.get("reversal_opportunity_prob", 0.0) or 0.0),
            campaign_state=str(src.get("campaign_state") or "inactive"),
            campaign_reason=str(src.get("campaign_reason") or src.get("campaign_state_reason") or ""),
            metadata=dict(src.get("metadata") or {}),
        )


def adapt_signal_row(row: dict[str, Any] | Any) -> SimSignal:
    return SignalAdapter().adapt(row)


def adapt_signal_rows(rows: list[dict[str, Any] | Any]) -> list[SimSignal]:
    return [adapt_signal_row(row) for row in list(rows or [])]

