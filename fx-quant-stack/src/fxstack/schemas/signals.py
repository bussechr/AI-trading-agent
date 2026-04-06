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
    belief_primary_side: str = ""
    belief_primary_scenario: str = ""
    belief_primary_thesis: str = ""
    belief_primary_score: float = 0.0
    belief_primary_rank_score: float = 0.0
    belief_primary_ev_above_hurdle_prob: float = 0.0
    belief_primary_expected_net_ev_bps: float = 0.0
    belief_primary_confirm_prob: float = 0.0
    belief_primary_fail_fast_prob: float = 0.0
    belief_opposing_side: str = ""
    belief_opposing_scenario: str = ""
    belief_opposing_thesis: str = ""
    belief_opposing_score: float = 0.0
    belief_gap: float = 0.0
    belief_fragility_score: float = 0.0
    belief_horizon_alignment_score: float = 0.0
    belief_short_up_prob: float = 0.0
    belief_trade_up_prob: float = 0.0
    belief_structural_up_prob: float = 0.0
    belief_scenario_probs: dict[str, float] = field(default_factory=dict)
    belief_regime_fit_score: float = 0.0
    belief_expected_confirmation_window_bars: int = 0
    belief_expected_path_shape: str = ""
    belief_invalidation_reason: str = ""
    belief_no_edge: bool = False
    belief_model_version: str = ""
    belief_source_mode: str = "disabled"

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
            "belief_primary_side": str(self.belief_primary_side),
            "belief_primary_scenario": str(self.belief_primary_scenario),
            "belief_primary_thesis": str(self.belief_primary_thesis),
            "belief_primary_score": float(self.belief_primary_score),
            "belief_primary_rank_score": float(self.belief_primary_rank_score),
            "belief_primary_ev_above_hurdle_prob": float(self.belief_primary_ev_above_hurdle_prob),
            "belief_primary_expected_net_ev_bps": float(self.belief_primary_expected_net_ev_bps),
            "belief_primary_confirm_prob": float(self.belief_primary_confirm_prob),
            "belief_primary_fail_fast_prob": float(self.belief_primary_fail_fast_prob),
            "belief_opposing_side": str(self.belief_opposing_side),
            "belief_opposing_scenario": str(self.belief_opposing_scenario),
            "belief_opposing_thesis": str(self.belief_opposing_thesis),
            "belief_opposing_score": float(self.belief_opposing_score),
            "belief_gap": float(self.belief_gap),
            "belief_fragility_score": float(self.belief_fragility_score),
            "belief_horizon_alignment_score": float(self.belief_horizon_alignment_score),
            "belief_short_up_prob": float(self.belief_short_up_prob),
            "belief_trade_up_prob": float(self.belief_trade_up_prob),
            "belief_structural_up_prob": float(self.belief_structural_up_prob),
            "belief_scenario_probs": dict(self.belief_scenario_probs),
            "belief_regime_fit_score": float(self.belief_regime_fit_score),
            "belief_expected_confirmation_window_bars": int(self.belief_expected_confirmation_window_bars),
            "belief_expected_path_shape": str(self.belief_expected_path_shape),
            "belief_invalidation_reason": str(self.belief_invalidation_reason),
            "belief_no_edge": bool(self.belief_no_edge),
            "belief_model_version": str(self.belief_model_version),
            "belief_source_mode": str(self.belief_source_mode),
        }
