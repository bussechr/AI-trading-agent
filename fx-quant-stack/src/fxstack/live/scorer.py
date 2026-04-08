# AGENT: ROLE: Live scoring adapter that aligns model inputs, enriches meta inputs, and emits probabilities plus policy diagnostics.
# AGENT: ENTRYPOINT: instantiated per pair/model set by runtime and twin loaders.
# AGENT: PRIMARY INPUTS: regime/swing/intraday/meta rows, spread input, model artifacts with declared feature columns.
# AGENT: PRIMARY OUTPUTS: `LiveSignal` with probabilities, expected edge, uncertainty, structure timing, and gate decisions.
# AGENT: DEPENDS ON: `fxstack/live/policy.py`, `fxstack/live/execution_gate.py`, `fxstack/settings.py`, `fxstack/schemas/signals.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py`, `tools/fxstack_digital_twin_backtest.py`.
# AGENT: STATE / SIDE EFFECTS: pure scoring; no persistence.
# AGENT: HANDSHAKES: model feature-column contract, policy diagnostic handoff, execution gate decision contract.
# AGENT: SEE: `docs/agents/model-stack-and-feature-flow.md` -> `fxstack/live/policy.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

import pandas as pd

from fxstack.live.execution_gate import should_trade
from fxstack.live.policy import (
    build_decision_source_chain,
    compute_expected_edge_bps,
    compute_live_uncertainty_score,
    compute_shadow_entry_diagnostics,
    infer_rl_lifecycle_intent,
    is_entry_session_blocked,
    normalize_spread_bps,
    normalize_strategy_engine_mode,
    session_bucket_from_ts,
)
from fxstack.schemas.signals import LiveSignal
from fxstack.settings import get_settings


class LiveScorer:
    def __init__(self, regime_model, swing_model, intraday_model, meta_model) -> None:
        self.regime_model = regime_model
        self.swing_model = swing_model
        self.intraday_model = intraday_model
        self.meta_model = meta_model

    # AGENT FLOW: `_model_input` enforces artifact-declared feature columns and is the main guard against silent schema drift.
    @staticmethod
    def _model_input(model, x_num: pd.DataFrame) -> pd.DataFrame:
        cols = list(getattr(model, "feature_columns", []) or [])
        if cols:
            missing = [c for c in cols if c not in x_num.columns]
            if missing:
                raise ValueError(f"missing feature columns: {','.join(missing)}")
            return x_num[cols]

        # Backward compatibility for older RegimeHMM artifacts without persisted feature columns.
        if str(getattr(model, "name", "")) == "regime_hmm":
            regime_cols = ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"]
            if all(c in x_num.columns for c in regime_cols):
                return x_num[regime_cols]
        return x_num

    # AGENT FLOW: Meta enrichment adds scorer-side context features only when the artifact expects them, preserving backward compatibility.
    @staticmethod
    def _enrich_meta_input(
        model,
        x_in: pd.DataFrame,
        *,
        regime_prob: float,
        swing_prob: float,
        entry_prob: float,
        side: str,
    ) -> pd.DataFrame:
        x = x_in.copy()
        required = set(getattr(model, "feature_columns", []) or [])
        side_norm = str(side).strip().lower()
        side_flag = 1.0 if side_norm == "long" else -1.0
        derived: dict[str, float] = {
            "regime_prob": float(regime_prob),
            "swing_prob": float(swing_prob),
            "entry_prob": float(entry_prob),
            "candidate_side": float(side_flag),
            "side_long": 1.0 if side_norm == "long" else 0.0,
            "side_short": 1.0 if side_norm == "short" else 0.0,
        }
        for key, value in derived.items():
            if key in x.columns:
                continue
            if required and key not in required:
                continue
            x[key] = float(value)
        return x.select_dtypes(include=["number"]).copy()

    # AGENT HOT PATH: `score` is the probability pipeline: model inference -> policy diagnostics -> gate decision -> `LiveSignal`.
    def score(
        self,
        row: pd.DataFrame | None = None,
        *,
        regime_row: pd.DataFrame | None = None,
        swing_row: pd.DataFrame | None = None,
        intraday_row: pd.DataFrame | None = None,
        meta_row: pd.DataFrame | None = None,
        spread_bps: float | None,
        expected_edge_bps: float | None,
        spread_unit_source: str = "provided",
    ) -> LiveSignal:
        base_row = intraday_row if intraday_row is not None else row
        if base_row is None or base_row.empty:
            raise ValueError("missing intraday/base feature row")
        regime_input_row = regime_row if regime_row is not None else base_row
        swing_input_row = swing_row if swing_row is not None else base_row
        intraday_input_row = intraday_row if intraday_row is not None else base_row
        meta_input_row = meta_row if meta_row is not None else intraday_input_row
        strategy_engine_mode = normalize_strategy_engine_mode(get_settings().strategy_engine_mode)
        intraday_row0 = intraday_input_row.iloc[0]
        meta_row0 = meta_input_row.iloc[0]

        def _hint(*keys: str) -> object | None:
            for key in keys:
                if key in intraday_row0.index:
                    value = intraday_row0.get(key)
                    if value is not None:
                        return value
                if key in meta_row0.index:
                    value = meta_row0.get(key)
                    if value is not None:
                        return value
            return None

        rl_target_position = _hint("rl_target_position", "target_position")
        rl_current_position_side = _hint("rl_current_position_side", "current_position_side", "position_side", "side")
        rl_current_position_size = _hint("rl_current_position_size", "current_position_size", "position_size", "lots_open")
        rl_close_position = _hint("rl_close_position", "close_position")
        rl_lifecycle_intent = infer_rl_lifecycle_intent(
            rl_lifecycle_intent=_hint("rl_lifecycle_intent"),
            rl_target_position=(None if rl_target_position is None else float(rl_target_position)),
            rl_current_position_side=(None if rl_current_position_side is None else str(rl_current_position_side)),
            rl_current_position_size=(None if rl_current_position_size is None else float(rl_current_position_size)),
            rl_close_position=(None if rl_close_position is None else bool(rl_close_position)),
        )

        regime = self.regime_model.predict_proba(
            self._model_input(self.regime_model, regime_input_row.select_dtypes(include=["number"]).copy())
        )
        swing = self.swing_model.predict_proba(
            self._model_input(self.swing_model, swing_input_row.select_dtypes(include=["number"]).copy())
        )
        intraday = self.intraday_model.predict_proba(
            self._model_input(self.intraday_model, intraday_input_row.select_dtypes(include=["number"]).copy())
        )

        regime_prob = float(regime.iloc[0].max())
        swing_prob = float(swing.iloc[0]["p1"])
        entry_prob = float(intraday.iloc[0]["p1"])
        side = "long" if swing_prob >= 0.5 else "short"
        meta = self.meta_model.predict_proba(
            self._model_input(
                self.meta_model,
                self._enrich_meta_input(
                    self.meta_model,
                    meta_input_row,
                    regime_prob=regime_prob,
                    swing_prob=swing_prob,
                    entry_prob=entry_prob,
                    side=side,
                ),
            )
        )
        trade_prob = float(meta.iloc[0]["p1"])
        s = get_settings()
        signal_ts = str(intraday_input_row.iloc[0].get("ts", ""))
        session_bucket = str(session_bucket_from_ts(signal_ts))
        session_entry_blocked = bool(
            is_entry_session_blocked(
                session_bucket=session_bucket,
                blocked_sessions=s.blocked_entry_sessions,
            )
        )
        session_entry_block_reason = f"session_blocked:{session_bucket}" if session_entry_blocked else ""

        edge = float(
            compute_expected_edge_bps(
                intraday_input_row,
                swing_prob=float(swing_prob),
                entry_prob=float(entry_prob),
                trade_prob=float(trade_prob),
                regime_prob=float(regime_prob),
                side=side,
            )
            if expected_edge_bps is None
            else expected_edge_bps
        )
        if spread_bps is None:
            spread, spread_source = normalize_spread_bps(
                row=intraday_input_row.iloc[0],
                pair=str(intraday_input_row.iloc[0].get("pair", "")),
            )
        else:
            spread = float(spread_bps)
            spread_source = str(spread_unit_source or "provided")

        raw_uncertainty = float(intraday_input_row.iloc[0].get("uncertainty_score", 0.0) or 0.0)
        live_uncertainty = float(
            raw_uncertainty
            if raw_uncertainty > 0.0
            else compute_live_uncertainty_score(
                intraday_input_row.iloc[0],
                regime_prob=float(regime_prob),
                swing_prob=float(swing_prob),
                entry_prob=float(entry_prob),
                trade_prob=float(trade_prob),
                side=side,
            )
        )

        shadow = compute_shadow_entry_diagnostics(
            row=intraday_input_row.iloc[0],
            swing_prob=float(swing_prob),
            entry_prob=float(entry_prob),
            trade_prob=float(trade_prob),
            regime_prob=float(regime_prob),
            expected_edge_bps=float(edge),
            spread_bps=float(spread),
            uncertainty_score=float(live_uncertainty),
            side=side,
            pair_tier=str(s.pair_tier(str(intraday_input_row.iloc[0].get("pair", "")))),
            min_swing_prob=float(s.min_swing_prob),
            min_entry_prob=float(s.min_entry_prob),
            min_trade_prob=float(s.min_trade_prob),
            min_expected_edge_bps=float(s.min_expected_edge_bps),
            use_uncertainty_gate=bool(s.use_uncertainty_gate),
            max_entry_uncertainty=float(s.max_entry_uncertainty),
            use_structure_timing_shadow=bool(s.use_structure_timing_shadow),
            structure_timing_rescue_min_score=float(s.structure_timing_rescue_min_score),
            structure_timing_entry_rescue_margin=float(s.structure_timing_entry_rescue_margin),
            structure_timing_max_chase_risk=float(s.structure_timing_max_chase_risk),
            entry_hysteresis_margin_bps=float(s.entry_hysteresis_margin_bps),
            enable_pair_quality_prior=bool(s.enable_pair_quality_prior),
            session_blocked=bool(session_entry_blocked),
            strategy_engine_mode=strategy_engine_mode,
        )
        gate = should_trade(
            swing_prob=swing_prob,
            entry_prob=entry_prob,
            trade_prob=trade_prob,
            regime_prob=regime_prob,
            spread_bps=float(spread),
            expected_edge_bps=float(edge),
            side=side,
            min_swing_prob=float(s.min_swing_prob),
            min_entry_prob=float(s.min_entry_prob),
            min_trade_prob=float(s.min_trade_prob),
            max_spread_bps=float(s.max_allowed_spread_bps),
            min_expected_edge_bps=float(s.min_expected_edge_bps),
            spread_unit_source=spread_source,
            model_intelligence_score=float(shadow.model_intelligence_score),
            strategy_engine_mode=strategy_engine_mode,
            rl_lifecycle_intent=rl_lifecycle_intent,
            rl_target_position=(None if rl_target_position is None else float(rl_target_position)),
            rl_current_position_side=(None if rl_current_position_side is None else str(rl_current_position_side)),
            rl_current_position_size=(None if rl_current_position_size is None else float(rl_current_position_size)),
            rl_close_position=(None if rl_close_position is None else bool(rl_close_position)),
        )
        fallback_reason = str(shadow.fallback_reason)
        decision_source_chain = build_decision_source_chain(
            gate_reason=str(gate.reason if not gate.allowed else "approved"),
            fallback_used=bool(shadow.fallback_used),
            fallback_reason=fallback_reason,
            strategy_engine_mode=strategy_engine_mode,
            rl_lifecycle_intent=str(gate.rl_lifecycle_intent),
            model_sources=("regime_model", "swing_model", "intraday_model", "meta_model"),
        )

        return LiveSignal(
            pair=str(intraday_input_row.iloc[0].get("pair", "")),
            ts=signal_ts,
            strategy_engine_mode=strategy_engine_mode,
            regime_prob=regime_prob,
            swing_prob=swing_prob,
            entry_prob=entry_prob,
            trade_prob=trade_prob,
            side=side,
            expected_edge_bps=float(edge),
            spread_bps=float(spread),
            allowed=bool(gate.allowed),
            rejection_reason=str(gate.reason if not gate.allowed else "none"),
            policy_version=str(gate.policy_version),
            edge_formula_id=str(gate.edge_formula_id),
            threshold_snapshot=dict(gate.threshold_snapshot),
            spread_unit_source=str(gate.spread_unit_source),
            scenario_bucket=str(intraday_input_row.iloc[0].get("scenario_bucket", "unknown")),
            context_frame_profile=str(intraday_input_row.iloc[0].get("context_frame_profile", "baseline_v2")),
            uncertainty_score=float(live_uncertainty),
            directional_swing_confidence=float(shadow.directional_swing_confidence),
            model_intelligence_score=float(shadow.model_intelligence_score),
            heuristic_penalty_score=float(shadow.heuristic_penalty_score),
            entry_margin=float(shadow.entry_margin),
            meta_margin=float(shadow.meta_margin),
            model_disagreement_score=float(shadow.model_disagreement_score),
            htf_alignment_score=float(shadow.htf_alignment_score),
            pullback_quality_score=float(shadow.pullback_quality_score),
            resume_trigger_score=float(shadow.resume_trigger_score),
            extension_penalty_score=float(shadow.extension_penalty_score),
            structure_timing_score=float(shadow.structure_timing_score),
            structure_bonus_bps=float(shadow.structure_bonus_bps),
            chase_penalty_bps=float(shadow.chase_penalty_bps),
            calibrated_ev_bps_shadow=float(shadow.calibrated_ev_bps),
            entry_quality_score_shadow=float(shadow.entry_quality_score),
            structure_rescue_active=bool(shadow.structure_rescue_active),
            fallback_used=bool(shadow.fallback_used),
            fallback_reason=fallback_reason,
            decision_source_chain=list(decision_source_chain),
            rl_lifecycle_intent=str(gate.rl_lifecycle_intent),
            rl_lifecycle_reason=str(gate.rl_lifecycle_reason),
            rl_lifecycle_fallback_reason=str(fallback_reason),
            rl_flip_intent=bool(gate.rl_flip_intent),
            rl_rebalance_intent=bool(gate.rl_rebalance_intent),
            shadow_floor_ok=bool(shadow.floor_ok),
            shadow_floor_rejection_reason=str(shadow.floor_rejection_reason),
            session_bucket=str(session_bucket),
            session_entry_blocked=bool(session_entry_blocked),
            session_entry_block_reason=str(session_entry_block_reason),
        )
