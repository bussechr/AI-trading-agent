from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.belief.candidate_builder import build_hypothesis_candidates
from fxstack.belief.composer import SCENARIOS, compose_directional_belief, compose_ranked_directional_belief
from fxstack.belief.types import DirectionalBelief
from fxstack.models.belief_horizon_xgb import BeliefHorizonXGB
from fxstack.models.belief_ranker_xgb import BeliefRankerXGB
from fxstack.models.belief_regressor_xgb import BeliefRegressorXGB
from fxstack.models.belief_scenario_xgb import BeliefScenarioXGB


@dataclass(slots=True)
class DirectionalBeliefModelSet:
    scenario_model: Any | None = None
    short_model: Any | None = None
    trade_model: Any | None = None
    structural_model: Any | None = None
    ranker_model: Any | None = None
    ev_above_hurdle_model: Any | None = None
    expected_net_ev_model: Any | None = None
    confirm_success_model: Any | None = None
    fail_fast_model: Any | None = None
    model_version: str = ""
    belief_contract: str = "directional_belief_v1"
    feature_columns: list[str] | None = None
    scenario_labels: list[str] | None = None
    horizons_bars: dict[str, int] | None = None
    model_scope: str = ""
    query_granularity: str = ""
    label_kernel_version: str = ""
    hypothesis_scenarios: list[str] | None = None
    hypothesis_sides: list[str] | None = None
    source_mode: str = "artifact"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _row_to_series(row: pd.DataFrame | pd.Series | dict[str, Any]) -> pd.Series:
    if isinstance(row, pd.DataFrame):
        if row.empty:
            return pd.Series(dtype=object)
        return row.iloc[0].copy()
    if isinstance(row, pd.Series):
        return row.copy()
    return pd.Series(dict(row or {}))


def build_belief_feature_frame(
    row: pd.DataFrame | pd.Series | dict[str, Any],
    *,
    signal: Any | None = None,
    adaptive_meta: dict[str, Any] | None = None,
) -> pd.DataFrame:
    src = _row_to_series(row)
    meta = dict(adaptive_meta or {})
    out: dict[str, float] = {}
    for key, value in src.items():
        try:
            out[str(key)] = float(value)
        except Exception:
            continue
    if signal is not None:
        for key in (
            "regime_prob",
            "swing_prob",
            "entry_prob",
            "trade_prob",
            "uncertainty_score",
            "directional_swing_confidence",
            "model_disagreement_score",
            "htf_alignment_score",
            "pullback_quality_score",
            "resume_trigger_score",
            "extension_penalty_score",
            "structure_timing_score",
            "expected_edge_bps",
            "spread_bps",
        ):
            out[str(key)] = _safe_float(getattr(signal, key, out.get(key, 0.0)), out.get(key, 0.0))
    for key in (
        "playbook_score",
        "location_score",
        "trigger_score",
        "macro_coherence_score",
        "hostility_score",
    ):
        if key in meta:
            out[str(key)] = _safe_float(meta.get(key), 0.0)
    environment_state = str(meta.get("environment_state") or meta.get("adaptive_environment_state") or "")
    playbook = str(meta.get("adaptive_playbook") or meta.get("playbook") or "")
    scenario_bucket = str(src.get("scenario_bucket") or meta.get("scenario_bucket") or "")
    regime_bucket = str(src.get("regime_bucket") or meta.get("regime_bucket") or "")
    categories = {
        "environment": [
            "PersistentTrend",
            "CorrectiveTrend",
            "BalancedRange",
            "CompressionPreBreakout",
            "ExpansionBreakout",
            "DislocatedHostile",
        ],
        "playbook": ["trend_pullback", "range_mean_reversion", "breakout_expansion", "failed_breakout_reversal", "no_trade"],
        "regime_bucket": ["trend", "range", "vol_expansion", "stress"],
        "scenario_bucket": [
            "trend_continuation",
            "range_mean_reversion",
            "breakout_initiation",
            "volatility_expansion",
            "asia_low_liquidity",
            "london_open",
            "ny_overlap",
            "high_spread_stress",
            "rollover_spread_shock",
        ],
    }
    for item in categories["environment"]:
        out[f"environment__{item}"] = 1.0 if environment_state == item else 0.0
    for item in categories["playbook"]:
        out[f"playbook__{item}"] = 1.0 if playbook == item else 0.0
    for item in categories["regime_bucket"]:
        out[f"regime_bucket__{item}"] = 1.0 if regime_bucket == item else 0.0
    for item in categories["scenario_bucket"]:
        out[f"scenario_bucket__{item}"] = 1.0 if scenario_bucket == item else 0.0
    return pd.DataFrame([out])


def empty_directional_belief(*, pair: str = "", ts: str = "", source_mode: str = "disabled") -> DirectionalBelief:
    return DirectionalBelief(pair=str(pair), ts=str(ts), source_mode=str(source_mode))


def load_directional_belief_model_set(raw_path: str | Path) -> DirectionalBeliefModelSet:
    path = Path(str(raw_path))
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    contract = str(meta.get("belief_contract") or "directional_belief_v1")
    if contract == "directional_belief_v2":
        return DirectionalBeliefModelSet(
            ranker_model=BeliefRankerXGB.load(path / "ranker_xgb"),
            ev_above_hurdle_model=BeliefHorizonXGB.load(path / "ev_above_hurdle_xgb"),
            expected_net_ev_model=BeliefRegressorXGB.load(path / "expected_net_ev_bps_xgb"),
            confirm_success_model=BeliefHorizonXGB.load(path / "confirm_success_xgb"),
            fail_fast_model=BeliefHorizonXGB.load(path / "fail_fast_xgb"),
            model_version=str(meta.get("model_version") or "directional_belief_v2"),
            belief_contract=contract,
            feature_columns=list(meta.get("feature_columns") or []),
            model_scope=str(meta.get("model_scope") or "global_cross_pair"),
            query_granularity=str(meta.get("query_granularity") or "pair_ts_8_hypotheses"),
            label_kernel_version=str(meta.get("label_kernel_version") or "entry_ev_v1"),
            hypothesis_scenarios=list(meta.get("hypothesis_scenarios") or []),
            hypothesis_sides=list(meta.get("hypothesis_sides") or []),
            source_mode="artifact",
        )
    return DirectionalBeliefModelSet(
        scenario_model=BeliefScenarioXGB.load(path / "scenario_xgb"),
        short_model=BeliefHorizonXGB.load(path / "horizon_short_xgb"),
        trade_model=BeliefHorizonXGB.load(path / "horizon_trade_xgb"),
        structural_model=BeliefHorizonXGB.load(path / "horizon_structural_xgb"),
        model_version=str(meta.get("model_version") or "directional_belief_v1"),
        belief_contract=contract,
        feature_columns=list(meta.get("feature_columns") or []),
        scenario_labels=list(meta.get("scenario_labels") or SCENARIOS),
        horizons_bars=dict(meta.get("horizons_bars") or {"short": 3, "trade": 12, "structural": 48}),
        source_mode="artifact",
    )


def _compute_v1(
    *,
    row: pd.DataFrame | pd.Series | dict[str, Any],
    signal: Any,
    adaptive_meta: dict[str, Any] | None,
    model_set: DirectionalBeliefModelSet,
    pair: str,
    ts: str,
) -> DirectionalBelief:
    features = build_belief_feature_frame(row, signal=signal, adaptive_meta=adaptive_meta)
    scenario_input = features.copy()
    if model_set.feature_columns:
        for col in model_set.feature_columns:
            if col not in scenario_input.columns:
                scenario_input[col] = 0.0
        scenario_input = scenario_input[list(model_set.feature_columns)].copy()
    scenario_proba = model_set.scenario_model.predict_proba(scenario_input).iloc[0].to_dict()
    scenario_probs = {
        label: _safe_float(scenario_proba.get(f"p{idx}"), 0.0)
        for idx, label in enumerate(list(model_set.scenario_labels or SCENARIOS))
    }
    short_up = _safe_float(model_set.short_model.predict_proba(scenario_input).iloc[0].get("p1", 0.0), 0.0)
    trade_up = _safe_float(model_set.trade_model.predict_proba(scenario_input).iloc[0].get("p1", 0.0), 0.0)
    structural_up = _safe_float(model_set.structural_model.predict_proba(scenario_input).iloc[0].get("p1", 0.0), 0.0)
    return compose_directional_belief(
        pair=pair,
        ts=ts,
        signal=signal,
        adaptive_meta=dict(adaptive_meta or {}),
        scenario_probs=scenario_probs,
        short_up_prob=short_up,
        trade_up_prob=trade_up,
        structural_up_prob=structural_up,
        model_version=str(model_set.model_version),
        source_mode=str(model_set.source_mode),
    )


def _compute_v2(
    *,
    row: pd.DataFrame | pd.Series | dict[str, Any],
    signal: Any,
    adaptive_meta: dict[str, Any] | None,
    model_set: DirectionalBeliefModelSet,
    pair: str,
    ts: str,
) -> DirectionalBelief:
    hypotheses = build_hypothesis_candidates(row, signal=signal, adaptive_meta=adaptive_meta, local_feasible_only=True)
    if hypotheses.empty:
        return compose_ranked_directional_belief(pair=pair, ts=ts, hypotheses=[], model_version=str(model_set.model_version), source_mode=str(model_set.source_mode))
    infer_frame = hypotheses.copy()
    if model_set.feature_columns:
        for col in model_set.feature_columns:
            if col not in infer_frame.columns:
                infer_frame[col] = 0.0
        infer_frame = infer_frame[list(model_set.feature_columns)].copy()
    else:
        infer_frame = infer_frame.select_dtypes(include=["number", "bool"]).astype(float)
    rank_margin = model_set.ranker_model.predict(infer_frame)
    ev_prob = model_set.ev_above_hurdle_model.predict_proba(infer_frame)["p1"].astype(float)
    expected_net_ev = model_set.expected_net_ev_model.predict(infer_frame).astype(float)
    confirm_prob = model_set.confirm_success_model.predict_proba(infer_frame)["p1"].astype(float)
    fail_fast_prob = model_set.fail_fast_model.predict_proba(infer_frame)["p1"].astype(float)
    hypothesis_rows: list[dict[str, Any]] = []
    for idx, cand in hypotheses.reset_index(drop=True).iterrows():
        payload = dict(cand.to_dict())
        payload.update(
            {
                "rank_margin": float(rank_margin.iloc[idx]),
                "p_ev_above_hurdle": float(ev_prob.iloc[idx]),
                "expected_net_ev_bps": float(expected_net_ev.iloc[idx]),
                "p_confirm_success": float(confirm_prob.iloc[idx]),
                "p_fail_fast": float(fail_fast_prob.iloc[idx]),
            }
        )
        hypothesis_rows.append(payload)
    return compose_ranked_directional_belief(
        pair=pair,
        ts=ts,
        hypotheses=hypothesis_rows,
        model_version=str(model_set.model_version),
        source_mode=str(model_set.source_mode),
    )


def compute_directional_belief(
    *,
    row: pd.DataFrame | pd.Series | dict[str, Any],
    signal: Any,
    adaptive_meta: dict[str, Any] | None,
    model_set: DirectionalBeliefModelSet | None,
) -> DirectionalBelief:
    pair = str((adaptive_meta or {}).get("pair") or getattr(signal, "pair", "") or _row_to_series(row).get("pair", ""))
    ts = str((adaptive_meta or {}).get("ts") or getattr(signal, "ts", "") or _row_to_series(row).get("ts", ""))
    if model_set is None:
        return empty_directional_belief(pair=pair, ts=ts, source_mode="disabled")
    if str(model_set.belief_contract or "directional_belief_v1") == "directional_belief_v2":
        return _compute_v2(row=row, signal=signal, adaptive_meta=adaptive_meta, model_set=model_set, pair=pair, ts=ts)
    return _compute_v1(row=row, signal=signal, adaptive_meta=adaptive_meta, model_set=model_set, pair=pair, ts=ts)
