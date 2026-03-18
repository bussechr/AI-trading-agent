from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class LiveSignal:
    pair: str
    ts: str
    regime_prob: float
    swing_prob: float
    entry_prob: float
    trade_prob: float
    side: str
    expected_edge_bps: float
    spread_bps: float
    allowed: bool
    rejection_reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "pair": self.pair,
            "ts": self.ts,
            "regime_prob": float(self.regime_prob),
            "swing_prob": float(self.swing_prob),
            "entry_prob": float(self.entry_prob),
            "trade_prob": float(self.trade_prob),
            "side": self.side,
            "expected_edge_bps": float(self.expected_edge_bps),
            "spread_bps": float(self.spread_bps),
            "allowed": bool(self.allowed),
            "rejection_reason": self.rejection_reason,
        }
