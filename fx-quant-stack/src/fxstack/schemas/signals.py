from __future__ import annotations

from dataclasses import dataclass, field


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
    policy_version: str = ""
    edge_formula_id: str = ""
    threshold_snapshot: dict[str, float] = field(default_factory=dict)
    spread_unit_source: str = "unknown"
    scenario_bucket: str = "unknown"
    context_frame_profile: str = "baseline_v2"
    uncertainty_score: float = 0.0

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
            "policy_version": str(self.policy_version),
            "edge_formula_id": str(self.edge_formula_id),
            "threshold_snapshot": dict(self.threshold_snapshot),
            "spread_unit_source": str(self.spread_unit_source),
            "scenario_bucket": str(self.scenario_bucket),
            "context_frame_profile": str(self.context_frame_profile),
            "uncertainty_score": float(self.uncertainty_score),
        }
