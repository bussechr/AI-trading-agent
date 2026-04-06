"""# AGENT: ROLE: Stable directional-belief dataclasses shared by training, twin replay, and runtime shadow telemetry.
# AGENT: ENTRYPOINT: imported by `fxstack/belief/*`, runtime, and twin.
# AGENT: PRIMARY INPUTS: composed thesis scores, horizon probabilities, regime-fit outputs.
# AGENT: PRIMARY OUTPUTS: `DirectionalBelief` records and signal/metadata serialization helpers.
# AGENT: DEPENDS ON: stdlib dataclasses and typing only.
# AGENT: CALLED BY: `fxstack/belief/composer.py`, `fxstack/belief/engine.py`, runtime, twin.
# AGENT: STATE / SIDE EFFECTS: pure type helpers only.
# AGENT: HANDSHAKES: `LiveSignal` belief field contract and runtime/twin metadata contract.
# AGENT: SEE: `docs/agents/model-stack-and-feature-flow.md` -> `fxstack/belief/composer.py` -> `docs/agents/runtime-loop.md`"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class DirectionalBelief:
    pair: str
    ts: str
    primary_side: str = ""
    primary_scenario: str = ""
    primary_thesis: str = ""
    primary_score: float = 0.0
    primary_rank_score: float = 0.0
    primary_ev_above_hurdle_prob: float = 0.0
    primary_expected_net_ev_bps: float = 0.0
    primary_confirm_prob: float = 0.0
    primary_fail_fast_prob: float = 0.0
    opposing_side: str = ""
    opposing_scenario: str = ""
    opposing_thesis: str = ""
    opposing_score: float = 0.0
    belief_gap: float = 0.0
    fragility_score: float = 0.0
    horizon_alignment_score: float = 0.0
    short_up_prob: float = 0.0
    trade_up_prob: float = 0.0
    structural_up_prob: float = 0.0
    scenario_probs: dict[str, float] = field(default_factory=dict)
    regime_fit_score: float = 0.0
    expected_confirmation_window_bars: int = 0
    expected_path_shape: str = ""
    invalidation_reason: str = ""
    no_edge: bool = False
    hypotheses: list[dict[str, Any]] = field(default_factory=list)
    model_version: str = ""
    source_mode: str = "disabled"

    def to_dict(self) -> dict[str, Any]:
        return {
            "belief_primary_side": str(self.primary_side),
            "belief_primary_scenario": str(self.primary_scenario),
            "belief_primary_thesis": str(self.primary_thesis),
            "belief_primary_score": float(self.primary_score),
            "belief_primary_rank_score": float(self.primary_rank_score),
            "belief_primary_ev_above_hurdle_prob": float(self.primary_ev_above_hurdle_prob),
            "belief_primary_expected_net_ev_bps": float(self.primary_expected_net_ev_bps),
            "belief_primary_confirm_prob": float(self.primary_confirm_prob),
            "belief_primary_fail_fast_prob": float(self.primary_fail_fast_prob),
            "belief_opposing_side": str(self.opposing_side),
            "belief_opposing_scenario": str(self.opposing_scenario),
            "belief_opposing_thesis": str(self.opposing_thesis),
            "belief_opposing_score": float(self.opposing_score),
            "belief_gap": float(self.belief_gap),
            "belief_fragility_score": float(self.fragility_score),
            "belief_horizon_alignment_score": float(self.horizon_alignment_score),
            "belief_short_up_prob": float(self.short_up_prob),
            "belief_trade_up_prob": float(self.trade_up_prob),
            "belief_structural_up_prob": float(self.structural_up_prob),
            "belief_scenario_probs": dict(self.scenario_probs),
            "belief_regime_fit_score": float(self.regime_fit_score),
            "belief_expected_confirmation_window_bars": int(self.expected_confirmation_window_bars),
            "belief_expected_path_shape": str(self.expected_path_shape),
            "belief_invalidation_reason": str(self.invalidation_reason),
            "belief_no_edge": bool(self.no_edge),
            "belief_model_version": str(self.model_version),
            "belief_source_mode": str(self.source_mode),
        }

    def apply_to_signal(self, signal: Any) -> Any:
        payload = {
            "belief_primary_side": str(self.primary_side),
            "belief_primary_scenario": str(self.primary_scenario),
            "belief_primary_thesis": str(self.primary_thesis),
            "belief_primary_score": float(self.primary_score),
            "belief_primary_rank_score": float(self.primary_rank_score),
            "belief_primary_ev_above_hurdle_prob": float(self.primary_ev_above_hurdle_prob),
            "belief_primary_expected_net_ev_bps": float(self.primary_expected_net_ev_bps),
            "belief_primary_confirm_prob": float(self.primary_confirm_prob),
            "belief_primary_fail_fast_prob": float(self.primary_fail_fast_prob),
            "belief_opposing_side": str(self.opposing_side),
            "belief_opposing_scenario": str(self.opposing_scenario),
            "belief_opposing_thesis": str(self.opposing_thesis),
            "belief_opposing_score": float(self.opposing_score),
            "belief_gap": float(self.belief_gap),
            "belief_fragility_score": float(self.fragility_score),
            "belief_horizon_alignment_score": float(self.horizon_alignment_score),
            "belief_short_up_prob": float(self.short_up_prob),
            "belief_trade_up_prob": float(self.trade_up_prob),
            "belief_structural_up_prob": float(self.structural_up_prob),
            "belief_scenario_probs": dict(self.scenario_probs),
            "belief_regime_fit_score": float(self.regime_fit_score),
            "belief_expected_confirmation_window_bars": int(self.expected_confirmation_window_bars),
            "belief_expected_path_shape": str(self.expected_path_shape),
            "belief_invalidation_reason": str(self.invalidation_reason),
            "belief_no_edge": bool(self.no_edge),
            "belief_model_version": str(self.model_version),
            "belief_source_mode": str(self.source_mode),
        }
        for key, value in payload.items():
            setattr(signal, key, value)
        return signal
