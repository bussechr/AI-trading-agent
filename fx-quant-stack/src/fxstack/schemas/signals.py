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
    directional_swing_confidence: float = 0.0
    entry_margin: float = 0.0
    meta_margin: float = 0.0
    model_disagreement_score: float = 0.0
    htf_alignment_score: float = 0.0
    pullback_quality_score: float = 0.0
    resume_trigger_score: float = 0.0
    extension_penalty_score: float = 0.0
    structure_timing_score: float = 0.0
    structure_bonus_bps: float = 0.0
    chase_penalty_bps: float = 0.0
    calibrated_ev_bps_shadow: float = 0.0
    entry_quality_score_shadow: float = 0.0
    structure_rescue_active: bool = False
    shadow_floor_ok: bool = False
    shadow_floor_rejection_reason: str = ""
    session_bucket: str = "unknown"
    session_entry_blocked: bool = False
    session_entry_block_reason: str = ""

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
            "directional_swing_confidence": float(self.directional_swing_confidence),
            "entry_margin": float(self.entry_margin),
            "meta_margin": float(self.meta_margin),
            "model_disagreement_score": float(self.model_disagreement_score),
            "htf_alignment_score": float(self.htf_alignment_score),
            "pullback_quality_score": float(self.pullback_quality_score),
            "resume_trigger_score": float(self.resume_trigger_score),
            "extension_penalty_score": float(self.extension_penalty_score),
            "structure_timing_score": float(self.structure_timing_score),
            "structure_bonus_bps": float(self.structure_bonus_bps),
            "chase_penalty_bps": float(self.chase_penalty_bps),
            "calibrated_ev_bps_shadow": float(self.calibrated_ev_bps_shadow),
            "entry_quality_score_shadow": float(self.entry_quality_score_shadow),
            "structure_rescue_active": bool(self.structure_rescue_active),
            "shadow_floor_ok": bool(self.shadow_floor_ok),
            "shadow_floor_rejection_reason": str(self.shadow_floor_rejection_reason),
            "session_bucket": str(self.session_bucket),
            "session_entry_blocked": bool(self.session_entry_blocked),
            "session_entry_block_reason": str(self.session_entry_block_reason),
        }
