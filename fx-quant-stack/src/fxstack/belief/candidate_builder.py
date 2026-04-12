"""# AGENT: ROLE: Build cross-pair directional-belief v2 hypothesis rows shared by training, twin replay, and runtime shadow.
# AGENT: ENTRYPOINT: `build_hypothesis_candidates()`.
# AGENT: PRIMARY INPUTS: feature rows, optional live signal diagnostics, optional adaptive metadata.
# AGENT: PRIMARY OUTPUTS: one normalized candidate row per `(pair, ts, side, scenario)`.
# AGENT: DEPENDS ON: pandas, numpy, settings.
# AGENT: CALLED BY: `fxstack/belief/dataset.py`, `fxstack/belief/engine.py`.
# AGENT: STATE / SIDE EFFECTS: pure feature assembly only.
# AGENT: HANDSHAKES: feeds `directional_belief_v2` training and runtime shadow inference.
# AGENT: SEE: `docs/agents/model-stack-and-feature-flow.md` -> `fxstack/belief/outcome_labels.py` -> `docs/agents/runtime-loop.md`"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from fxstack.settings import get_settings

HYPOTHESIS_SCENARIOS = [
    "trend_pullback",
    "range_mean_reversion",
    "breakout_expansion",
    "failed_breakout_reversal",
]
HYPOTHESIS_SIDES = ["long", "short"]
CROSS_PAIR_CONTEXT_PREFIXES = ("usd_strength_", "cross_pair_")
KNOWN_SESSION_BUCKETS = ["asia", "london_open", "london_ny_overlap", "new_york", "pacific", "unknown"]
KNOWN_REGIME_BUCKETS = ["trend", "range", "vol_expansion", "stress", "unknown"]
KNOWN_ENVIRONMENT_STATES = [
    "PersistentTrend",
    "CorrectiveTrend",
    "BalancedRange",
    "CompressionPreBreakout",
    "ExpansionBreakout",
    "DislocatedHostile",
]
REGIME_FIT_PRIORS = {
    "PersistentTrend": {
        "trend_pullback": 1.00,
        "failed_breakout_reversal": 0.45,
        "breakout_expansion": 0.55,
        "range_mean_reversion": 0.10,
    },
    "CorrectiveTrend": {
        "trend_pullback": 0.90,
        "failed_breakout_reversal": 0.55,
        "breakout_expansion": 0.45,
        "range_mean_reversion": 0.20,
    },
    "BalancedRange": {
        "range_mean_reversion": 1.00,
        "failed_breakout_reversal": 0.55,
        "trend_pullback": 0.20,
        "breakout_expansion": 0.10,
    },
    "CompressionPreBreakout": {
        "breakout_expansion": 1.00,
        "trend_pullback": 0.50,
        "range_mean_reversion": 0.30,
        "failed_breakout_reversal": 0.20,
    },
    "ExpansionBreakout": {
        "breakout_expansion": 0.85,
        "failed_breakout_reversal": 0.70,
        "trend_pullback": 0.45,
        "range_mean_reversion": 0.10,
    },
    "DislocatedHostile": {
        "trend_pullback": 0.0,
        "range_mean_reversion": 0.0,
        "breakout_expansion": 0.0,
        "failed_breakout_reversal": 0.0,
    },
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip01(value: Any) -> float:
    return max(0.0, min(1.0, _safe_float(value, 0.0)))


def _clip01_series(values: pd.Series | np.ndarray | Any) -> pd.Series:
    return pd.Series(values).astype(float).clip(lower=0.0, upper=1.0)


def _series(frame: pd.DataFrame, key: str, default: float = 0.0) -> pd.Series:
    if key in frame.columns:
        return pd.to_numeric(frame[key], errors="coerce").fillna(default).astype(float)
    return pd.Series(default, index=frame.index, dtype=float)


def _series_text(frame: pd.DataFrame, key: str, default: str = "") -> pd.Series:
    if key in frame.columns:
        return frame[key].astype(str).fillna(default)
    return pd.Series(default, index=frame.index, dtype="object")


def _directional_component(values: pd.Series, *, side_sign: float, scale: pd.Series | float) -> pd.Series:
    if isinstance(scale, pd.Series):
        scale_arr = scale.replace(0.0, np.nan).fillna(1e-6).astype(float)
    else:
        scale_arr = pd.Series(float(max(1e-6, _safe_float(scale, 1e-6))), index=values.index, dtype=float)
    directional = side_sign * pd.to_numeric(values, errors="coerce").fillna(0.0).astype(float)
    score = 0.5 + (directional / (2.0 * scale_arr))
    return _clip01_series(score).set_axis(values.index)


def _triangular_score(values: pd.Series, *, target: float, width: float) -> pd.Series:
    width = max(1e-9, float(width))
    val = pd.to_numeric(values, errors="coerce").fillna(0.0).astype(float)
    score = 1.0 - (val.sub(float(target)).abs() / width)
    return _clip01_series(score).set_axis(values.index)


def _derive_session_bucket(frame: pd.DataFrame) -> pd.Series:
    if "session_bucket" in frame.columns:
        return frame["session_bucket"].astype(str).replace({"nan": "unknown"}).fillna("unknown")
    ts = pd.to_datetime(frame.get("ts"), utc=True, errors="coerce")
    hours = ts.dt.hour.fillna(-1).astype(int)
    out = pd.Series("unknown", index=frame.index, dtype="object")
    out.loc[hours.isin([21, 22, 23])] = "pacific"
    out.loc[hours.between(0, 6)] = "asia"
    out.loc[hours.between(7, 11)] = "london_open"
    out.loc[hours.between(12, 15)] = "london_ny_overlap"
    out.loc[hours.between(16, 20)] = "new_york"
    return out


def _derive_environment_state(frame: pd.DataFrame) -> pd.Series:
    if "environment_state" in frame.columns:
        return frame["environment_state"].astype(str).replace({"nan": ""}).fillna("")
    regime_bucket = _series_text(frame, "regime_bucket", "unknown")
    scenario_bucket = _series_text(frame, "scenario_bucket", "unknown")
    pullback_depth = _series(frame, "pullback_depth_20", 0.0)
    hostility = _series(frame, "hostility_score", 0.0)
    out = pd.Series("BalancedRange", index=frame.index, dtype="object")
    out.loc[regime_bucket.eq("stress") | hostility.ge(0.95)] = "DislocatedHostile"
    out.loc[scenario_bucket.eq("breakout_initiation")] = "CompressionPreBreakout"
    out.loc[scenario_bucket.eq("volatility_expansion") | regime_bucket.eq("vol_expansion")] = "ExpansionBreakout"
    out.loc[regime_bucket.eq("range")] = "BalancedRange"
    out.loc[regime_bucket.eq("trend") & pullback_depth.gt(0.0007)] = "CorrectiveTrend"
    out.loc[regime_bucket.eq("trend") & ~pullback_depth.gt(0.0007)] = "PersistentTrend"
    return out


def regime_fit_prior(environment_state: str, scenario: str) -> float:
    return float(REGIME_FIT_PRIORS.get(str(environment_state or ""), {}).get(str(scenario or ""), 0.0))


def _scenario_regime_fit_series(environment_state: pd.Series, *, scenario: str) -> pd.Series:
    return pd.Series(
        [regime_fit_prior(str(state), scenario) for state in environment_state.astype(str)],
        index=environment_state.index,
        dtype=float,
    )


def _one_hot(frame: pd.DataFrame, *, column: str, values: list[str], prefix: str) -> pd.DataFrame:
    series = frame[column].astype(str) if column in frame.columns else pd.Series("", index=frame.index, dtype="object")
    return pd.DataFrame({f"{prefix}__{value}": series.eq(value).astype(float) for value in values}, index=frame.index)


def _append_cross_pair_context(out: pd.DataFrame, frame: pd.DataFrame) -> None:
    for column in frame.columns:
        name = str(column)
        if name in out.columns or not any(name.startswith(prefix) for prefix in CROSS_PAIR_CONTEXT_PREFIXES):
            continue
        out[name] = pd.to_numeric(frame[name], errors="coerce").fillna(0.0).astype(float)


def _base_directional_features(frame: pd.DataFrame, *, side: str, signal: Any | None, adaptive_meta: dict[str, Any] | None) -> pd.DataFrame:
    side_norm = "short" if str(side).strip().lower() == "short" else "long"
    side_sign = -1.0 if side_norm == "short" else 1.0
    out = pd.DataFrame(index=frame.index)
    out["pair"] = _series_text(frame, "pair", str((adaptive_meta or {}).get("pair") or getattr(signal, "pair", "")))
    out["ts"] = _series_text(frame, "ts", str((adaptive_meta or {}).get("ts") or getattr(signal, "ts", "")))
    out["row_idx"] = pd.to_numeric(frame["row_idx"], errors="coerce").fillna(-1).astype(int) if "row_idx" in frame.columns else pd.Series(-1, index=frame.index, dtype=int)
    out["side"] = side_norm
    out["side_sign"] = side_sign
    out["session_bucket"] = _derive_session_bucket(frame)
    out["environment_state"] = _derive_environment_state(frame)
    out["regime_bucket"] = _series_text(frame, "regime_bucket", "unknown").replace({"nan": "unknown"})
    out["scenario_bucket"] = _series_text(frame, "scenario_bucket", "unknown").replace({"nan": "unknown"})
    out["spread_bps"] = _series(frame, "spread_bps", _safe_float(getattr(signal, "spread_bps", 0.0), 0.0))
    out["mid_close"] = _series(frame, "mid_close", 0.0)
    ret_1 = _series(frame, "ret_1", 0.0)
    ret_5 = _series(frame, "ret_5", 0.0)
    vol_20 = _series(frame, "vol_20", 0.0)
    vol_60 = _series(frame, "vol_60", 0.0)
    vol_ref_bps = np.maximum(np.maximum(vol_20.abs(), vol_60.abs()) * 10000.0, 2.0)
    vol_ref = pd.Series(vol_ref_bps / 10000.0, index=frame.index, dtype=float)
    out["vol_ref_bps"] = vol_ref_bps.astype(float)

    slope_20 = _series(frame, "trend_slope_20", 0.0)
    slope_60 = _series(frame, "trend_slope_60", 0.0)
    strength_20 = _series(frame, "trend_strength_20", 0.0)
    strength_60 = _series(frame, "trend_strength_60", 0.0)

    htf_components: list[pd.Series] = []
    for key, scale in (
        ("h1_trend_slope_20", 0.0015),
        ("h4_trend_slope_20", 0.0025),
        ("d_trend_slope_20", 0.0035),
        ("h1_trend_strength_20", 1.25),
        ("h4_trend_strength_20", 1.50),
        ("d_trend_strength_20", 1.75),
    ):
        if key in frame.columns:
            htf_components.append(_directional_component(_series(frame, key, 0.0), side_sign=side_sign, scale=scale))
    if not htf_components:
        htf_components = [
            _directional_component(slope_20, side_sign=side_sign, scale=0.0015),
            _directional_component(slope_60, side_sign=side_sign, scale=0.0020),
            _directional_component(strength_20, side_sign=side_sign, scale=1.25),
            _directional_component(strength_60, side_sign=side_sign, scale=1.50),
        ]
    htf_alignment = pd.concat(htf_components, axis=1).mean(axis=1).clip(lower=0.0, upper=1.0)
    out["htf_alignment_score"] = htf_alignment

    pullback_depth = _series(frame, "pushup_depth_20", 0.0) if side_norm == "short" else _series(frame, "pullback_depth_20", 0.0)
    pullback_quality = _triangular_score(pullback_depth, target=0.0018, width=0.0036)
    pullback_quality = (pullback_quality * (0.5 + (0.5 * htf_alignment))).clip(lower=0.0, upper=1.0)
    out["pullback_quality_score"] = pullback_quality

    resume_components = pd.concat(
        [
            _directional_component(ret_1, side_sign=side_sign, scale=vol_ref * 1.5),
            _directional_component(_series(frame, "edge_decay_12", 0.0), side_sign=side_sign, scale=vol_ref * 1.5),
            _directional_component(_series(frame, "bar_imbalance", 0.0), side_sign=side_sign, scale=0.80),
            _directional_component(_series(frame, "micro_pressure", 0.0), side_sign=side_sign, scale=0.80),
        ],
        axis=1,
    )
    resume_trigger = resume_components.mean(axis=1).clip(lower=0.0, upper=1.0)
    out["resume_trigger_score"] = resume_trigger

    extension_components = pd.concat(
        [
            (((side_sign * strength_20) - 1.25) / 2.0).clip(lower=0.0, upper=1.0),
            (((side_sign * strength_60) - 1.00) / 2.5).clip(lower=0.0, upper=1.0),
            (((side_sign * ret_5) - 0.0012) / 0.0030).clip(lower=0.0, upper=1.0),
        ],
        axis=1,
    )
    extension_penalty = extension_components.mean(axis=1).clip(lower=0.0, upper=1.0)
    out["extension_penalty_score"] = extension_penalty
    out["structure_timing_score"] = (
        (0.40 * htf_alignment)
        + (0.25 * pullback_quality)
        + (0.25 * resume_trigger)
        + (0.10 * (1.0 - extension_penalty))
    ).clip(lower=0.0, upper=1.0)

    uncertainty_raw = _safe_float(getattr(signal, "uncertainty_score", np.nan), np.nan)
    if np.isfinite(uncertainty_raw):
        uncertainty = pd.Series(float(uncertainty_raw), index=frame.index, dtype=float)
    elif "uncertainty_score" in frame.columns:
        uncertainty = _series(frame, "uncertainty_score", 0.0).clip(lower=0.0, upper=1.0)
    else:
        vol_term_ratio = _series(frame, "vol_term_ratio", 1.0)
        spread_penalty = (out["spread_bps"] / max(1e-9, float(get_settings().max_allowed_spread_bps))).clip(lower=0.0, upper=1.0)
        momentum_decay = (1.0 - resume_trigger).clip(lower=0.0, upper=1.0)
        uncertainty = ((0.40 * spread_penalty) + (0.30 * (vol_term_ratio.sub(1.0).abs() / 1.5).clip(lower=0.0, upper=1.0)) + (0.30 * momentum_decay)).clip(lower=0.0, upper=1.0)
    out["uncertainty_score"] = uncertainty

    disagreement_raw = _safe_float(getattr(signal, "model_disagreement_score", np.nan), np.nan)
    if np.isfinite(disagreement_raw):
        disagreement = pd.Series(float(disagreement_raw), index=frame.index, dtype=float)
    elif "model_disagreement_score" in frame.columns:
        disagreement = _series(frame, "model_disagreement_score", 0.0).clip(lower=0.0, upper=1.0)
    else:
        disagreement = (htf_alignment.sub(resume_trigger).abs() + pullback_quality.sub(resume_trigger).abs()).div(2.0).clip(lower=0.0, upper=1.0)
    out["model_disagreement_score"] = disagreement

    hostility = _series(frame, "hostility_score", np.nan)
    if hostility.isna().all():
        hostility = (
            (out["spread_bps"] / max(1e-9, float(get_settings().max_allowed_spread_bps))).clip(lower=0.0, upper=1.0) * 0.45
            + (_series(frame, "bar_imbalance", 0.0).abs().clip(lower=0.0, upper=1.0) * 0.25)
            + (_series(frame, "vol_term_ratio", 1.0).sub(1.0).abs() / 1.5).clip(lower=0.0, upper=1.0) * 0.30
        ).clip(lower=0.0, upper=1.0)
    else:
        hostility = hostility.fillna(0.0).clip(lower=0.0, upper=1.0)
    out["hostility_score"] = hostility

    macro = _series(frame, "macro_coherence_score", np.nan)
    if macro.isna().all():
        macro = ((0.60 * htf_alignment) + (0.20 * (1.0 - hostility)) + (0.20 * resume_trigger)).clip(lower=0.0, upper=1.0)
    else:
        macro = macro.fillna(0.0).clip(lower=0.0, upper=1.0)
    out["macro_coherence_score"] = macro

    directional_swing = _safe_float(getattr(signal, "directional_swing_confidence", np.nan), np.nan)
    if np.isfinite(directional_swing):
        out["directional_swing_confidence"] = float(directional_swing)
    elif "directional_swing_confidence" in frame.columns:
        out["directional_swing_confidence"] = _series(frame, "directional_swing_confidence", 0.0).clip(lower=0.0, upper=1.0)
    else:
        out["directional_swing_confidence"] = ((0.55 * htf_alignment) + (0.45 * resume_trigger)).clip(lower=0.0, upper=1.0)

    out["regime_prob"] = _series(frame, "regime_prob", 0.0).where(lambda s: s.notna(), htf_alignment)
    out["swing_prob"] = _series(frame, "swing_prob", 0.0).where(lambda s: s.notna(), out["directional_swing_confidence"])
    out["entry_prob"] = _series(frame, "entry_prob", 0.0).where(
        lambda s: s.notna(),
        ((0.45 * resume_trigger) + (0.30 * out["structure_timing_score"]) + (0.25 * pullback_quality)).clip(lower=0.0, upper=1.0),
    )
    out["trade_prob"] = _series(frame, "trade_prob", 0.0).where(
        lambda s: s.notna(),
        ((0.40 * htf_alignment) + (0.35 * (1.0 - extension_penalty)) + (0.25 * macro)).clip(lower=0.0, upper=1.0),
    )
    out["expected_edge_bps"] = _series(frame, "expected_edge_bps", np.nan)
    missing_expected = out["expected_edge_bps"].isna()
    if bool(missing_expected.any()):
        proxy_edge = (vol_ref_bps * ((0.35 * htf_alignment) + (0.25 * resume_trigger) + (0.20 * pullback_quality) + (0.20 * macro))) - out["spread_bps"]
        out.loc[missing_expected, "expected_edge_bps"] = proxy_edge.loc[missing_expected]
    out["expected_edge_bps"] = out["expected_edge_bps"].fillna(0.0).astype(float)
    _append_cross_pair_context(out, frame)
    return out


def _scenario_scores(base: pd.DataFrame, *, scenario: str) -> tuple[pd.Series, pd.Series, pd.Series]:
    prior = _scenario_regime_fit_series(base["environment_state"], scenario=scenario)
    htf = base["htf_alignment_score"]
    pullback = base["pullback_quality_score"]
    resume = base["resume_trigger_score"]
    extension = base["extension_penalty_score"]
    structure = base["structure_timing_score"]
    hostility = base["hostility_score"]

    if scenario == "trend_pullback":
        playbook = (0.45 * prior) + (0.30 * htf) + (0.25 * (1.0 - hostility))
        location = (0.60 * pullback) + (0.40 * (1.0 - extension))
        trigger = (0.65 * resume) + (0.35 * structure)
    elif scenario == "range_mean_reversion":
        playbook = (0.50 * prior) + (0.30 * (1.0 - htf)) + (0.20 * (1.0 - hostility))
        location = (0.45 * pullback) + (0.35 * (1.0 - htf)) + (0.20 * (1.0 - extension))
        trigger = (0.35 * resume) + (0.35 * (1.0 - htf)) + (0.30 * (1.0 - extension))
    elif scenario == "breakout_expansion":
        playbook = (0.45 * prior) + (0.25 * structure) + (0.20 * resume) + (0.10 * htf)
        location = (0.50 * structure) + (0.25 * (1.0 - extension)) + (0.25 * resume)
        trigger = (0.55 * resume) + (0.45 * htf)
    else:
        playbook = (0.40 * prior) + (0.30 * extension) + (0.30 * (1.0 - htf))
        location = (0.40 * extension) + (0.35 * (1.0 - htf)) + (0.25 * (1.0 - hostility))
        trigger = (0.55 * resume) + (0.25 * (1.0 - htf)) + (0.20 * extension)

    return (
        playbook.clip(lower=0.0, upper=1.0),
        location.clip(lower=0.0, upper=1.0),
        trigger.clip(lower=0.0, upper=1.0),
    )


def build_hypothesis_candidates(
    row_or_frame: pd.DataFrame | pd.Series | dict[str, Any],
    *,
    signal: Any | None = None,
    adaptive_meta: dict[str, Any] | None = None,
    settings: Any | None = None,
    local_feasible_only: bool = True,
) -> pd.DataFrame:
    s = settings or get_settings()
    if isinstance(row_or_frame, pd.DataFrame):
        frame = row_or_frame.copy().reset_index(drop=True)
    elif isinstance(row_or_frame, pd.Series):
        frame = pd.DataFrame([row_or_frame.to_dict()])
    else:
        frame = pd.DataFrame([dict(row_or_frame or {})])
    if frame.empty:
        return pd.DataFrame()

    pair_fallback = str((adaptive_meta or {}).get("pair") or getattr(signal, "pair", "")).upper()
    ts_fallback = str((adaptive_meta or {}).get("ts") or getattr(signal, "ts", ""))
    if "pair" not in frame.columns:
        frame["pair"] = pair_fallback
    if "ts" not in frame.columns:
        frame["ts"] = ts_fallback
    if "row_idx" not in frame.columns:
        frame["row_idx"] = np.arange(len(frame), dtype=int)

    candidate_frames: list[pd.DataFrame] = []
    for side in HYPOTHESIS_SIDES:
        base = _base_directional_features(frame, side=side, signal=signal, adaptive_meta=adaptive_meta)
        blocked_sessions = set(getattr(s, "blocked_entry_sessions", []) or [])
        pair_flat = pd.Series(True, index=base.index, dtype=bool)
        if "pair_flat" in frame.columns:
            pair_flat = frame["pair_flat"].astype(bool)
        elif "position_count_pair" in frame.columns:
            pair_flat = pd.to_numeric(frame["position_count_pair"], errors="coerce").fillna(0.0).astype(float) <= 0.0
        local_feasible = (
            (~base["session_bucket"].isin(blocked_sessions))
            & (base["spread_bps"] <= float(getattr(s, "max_allowed_spread_bps", 2.5)))
            & pair_flat
        )
        base["local_feasible"] = local_feasible.astype(bool)
        for scenario in HYPOTHESIS_SCENARIOS:
            playbook, location, trigger = _scenario_scores(base, scenario=scenario)
            cand = base.copy()
            cand["scenario"] = scenario
            cand["playbook_score_for_hypothesis"] = playbook
            cand["location_score_for_hypothesis"] = location
            cand["trigger_score_for_hypothesis"] = trigger
            cand["scenario_regime_fit_prior"] = _scenario_regime_fit_series(base["environment_state"], scenario=scenario)
            cand["hypothesis_id"] = cand["scenario"].astype(str) + ":" + cand["side"].astype(str)
            cand["query_id"] = cand["pair"].astype(str) + "|" + cand["ts"].astype(str)
            candidate_frames.append(cand)

    out = pd.concat(candidate_frames, axis=0, ignore_index=True)
    out["session_bucket"] = out["session_bucket"].fillna("unknown").astype(str)
    out["regime_bucket"] = out["regime_bucket"].fillna("unknown").astype(str)
    out["environment_state"] = out["environment_state"].fillna("BalancedRange").astype(str)

    pair_values = list(getattr(s, "pairs", []) or sorted({str(v).upper() for v in out["pair"].astype(str)}))
    out = pd.concat(
        [
            out,
            _one_hot(out, column="pair", values=pair_values, prefix="pair"),
            _one_hot(out, column="side", values=HYPOTHESIS_SIDES, prefix="side"),
            _one_hot(out, column="scenario", values=HYPOTHESIS_SCENARIOS, prefix="scenario"),
            _one_hot(out, column="session_bucket", values=KNOWN_SESSION_BUCKETS, prefix="session"),
            _one_hot(out, column="regime_bucket", values=KNOWN_REGIME_BUCKETS, prefix="regime_bucket"),
            _one_hot(out, column="environment_state", values=KNOWN_ENVIRONMENT_STATES, prefix="environment"),
        ],
        axis=1,
    )
    out = out.loc[:, ~out.columns.duplicated()].copy()
    if local_feasible_only:
        out = out.loc[out["local_feasible"].astype(bool)].reset_index(drop=True)
    return out
