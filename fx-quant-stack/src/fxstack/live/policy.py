from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


POLICY_VERSION = "fxstack_policy_v1"
EDGE_FORMULA_ID = "prob_weighted_opportunity_v2"


@dataclass(slots=True)
class PolicyGateDecision:
    allowed: bool
    reason: str
    policy_version: str = POLICY_VERSION
    edge_formula_id: str = EDGE_FORMULA_ID
    threshold_snapshot: dict[str, float] = field(default_factory=dict)
    spread_unit_source: str = "unknown"


@dataclass(slots=True)
class ShadowEntryDiagnostics:
    directional_swing_confidence: float
    entry_margin: float
    meta_margin: float
    model_disagreement_score: float
    htf_alignment_score: float
    pullback_quality_score: float
    resume_trigger_score: float
    extension_penalty_score: float
    structure_timing_score: float
    structure_bonus_bps: float
    chase_penalty_bps: float
    calibrated_ev_bps: float
    uncertainty_penalty_bps: float
    disagreement_penalty_bps: float
    entry_quality_score: float
    structure_rescue_active: bool
    floor_ok: bool
    floor_rejection_reason: str


@dataclass(slots=True)
class StructureTimingDiagnostics:
    htf_alignment_score: float
    pullback_quality_score: float
    resume_trigger_score: float
    extension_penalty_score: float
    structure_timing_score: float
    structure_extreme_extension: bool


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _row_value(row: pd.DataFrame | pd.Series | dict[str, Any], key: str, default: float = 0.0) -> float:
    if isinstance(row, pd.DataFrame):
        if row.empty:
            return float(default)
        return _safe_float(row.iloc[0].get(key, default), default)
    if isinstance(row, pd.Series):
        return _safe_float(row.get(key, default), default)
    if isinstance(row, dict):
        return _safe_float(row.get(key, default), default)
    return float(default)


def _row_has_key(row: pd.DataFrame | pd.Series | dict[str, Any], key: str) -> bool:
    if isinstance(row, pd.DataFrame):
        return str(key) in set(row.columns)
    if isinstance(row, pd.Series):
        return str(key) in set(row.index)
    if isinstance(row, dict):
        return str(key) in row
    return False


def infer_pip_size(*, pair: str = "", digits: int | None = None) -> float:
    if digits is not None:
        if int(digits) in {2, 3}:
            return 0.01
        if int(digits) in {4, 5}:
            return 0.0001
    return 0.01 if str(pair).upper().endswith("JPY") else 0.0001


def directional_swing_confidence(*, swing_prob: float, side: str | None = None) -> float:
    swing_p = max(0.0, min(1.0, _safe_float(swing_prob, 0.5)))
    direction = str(side or ("long" if swing_p >= 0.5 else "short")).strip().lower()
    if direction == "short":
        return 1.0 - swing_p
    return swing_p


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _directional_value(value: float, side: str | None) -> float:
    direction = str(side or "").strip().lower()
    raw = _safe_float(value, 0.0)
    return -raw if direction == "short" else raw


def _directional_component_score(value: float, *, side: str | None, scale: float) -> float:
    scaled = _directional_value(value, side) / max(1e-9, float(scale))
    return _clamp01(0.5 + (0.5 * max(-1.0, min(1.0, float(scaled)))))


def _triangular_score(value: float, *, target: float, width: float) -> float:
    if width <= 0.0:
        return 0.0
    distance = abs(float(value) - float(target))
    return _clamp01(1.0 - (distance / float(width)))


def session_bucket_from_ts(ts_value: Any) -> str:
    parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return "unknown"
    hour = int(parsed.hour)
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london_open"
    if 12 <= hour < 16:
        return "london_ny_overlap"
    if 16 <= hour < 21:
        return "new_york"
    return "pacific"


def is_entry_session_blocked(*, session_bucket: str, blocked_sessions: list[str] | tuple[str, ...] | set[str] | str | None) -> bool:
    bucket = str(session_bucket or "").strip().lower()
    if not bucket:
        return False
    if isinstance(blocked_sessions, str):
        items = [item.strip().lower() for item in blocked_sessions.split(",")]
    else:
        items = [str(item).strip().lower() for item in list(blocked_sessions or [])]
    blocked = {item for item in items if item}
    return bucket in blocked


def compute_live_uncertainty_score(
    row: pd.DataFrame | pd.Series | dict[str, Any],
    *,
    regime_prob: float,
    swing_prob: float,
    entry_prob: float,
    trade_prob: float,
    side: str | None,
) -> float:
    directional_conf = directional_swing_confidence(swing_prob=float(swing_prob), side=side)
    ambiguity_components = [
        1.0 - abs(_clamp01(_safe_float(regime_prob, 0.5)) - 0.5) * 2.0,
        1.0 - abs(_clamp01(_safe_float(entry_prob, 0.5)) - 0.5) * 2.0,
        1.0 - abs(_clamp01(_safe_float(trade_prob, 0.5)) - 0.5) * 2.0,
        2.0 * max(0.0, 1.0 - float(directional_conf)),
    ]
    probability_ambiguity = float(sum(ambiguity_components) / max(1, len(ambiguity_components)))

    anomaly_components: list[float] = []
    spread_z20 = _row_value(row, "spread_z20", 0.0)
    if spread_z20 != 0.0:
        anomaly_components.append(min(abs(float(spread_z20)) / 3.0, 1.0))
    normalized_spread = _row_value(row, "normalized_spread", 0.0)
    if normalized_spread > 0.0:
        anomaly_components.append(min(float(normalized_spread) / 2.0, 1.0))
    vol_term_ratio = _row_value(row, "vol_term_ratio", 1.0)
    if vol_term_ratio > 0.0:
        anomaly_components.append(min(abs(float(vol_term_ratio) - 1.0) / 1.5, 1.0))
    bar_imbalance = _row_value(row, "bar_imbalance", 0.0)
    if bar_imbalance != 0.0:
        anomaly_components.append(min(abs(float(bar_imbalance)), 1.0))
    h1_available = _row_value(row, "h1_available", 1.0)
    anomaly_components.append(0.0 if float(h1_available) >= 1.0 else 1.0)
    feature_anomaly = float(sum(anomaly_components) / max(1, len(anomaly_components)))

    return _clamp01((0.65 * probability_ambiguity) + (0.35 * feature_anomaly))


def compute_model_disagreement_score(
    *,
    directional_swing_confidence_value: float,
    entry_prob: float,
    trade_prob: float,
    regime_prob: float,
) -> float:
    values = [
        max(0.0, min(1.0, _safe_float(directional_swing_confidence_value, 0.5))),
        max(0.0, min(1.0, _safe_float(entry_prob, 0.5))),
        max(0.0, min(1.0, _safe_float(trade_prob, 0.5))),
        max(0.0, min(1.0, _safe_float(regime_prob, 0.5))),
    ]
    diffs = [
        abs(values[0] - values[1]),
        abs(values[0] - values[2]),
        abs(values[1] - values[2]),
        abs(values[2] - values[3]),
    ]
    return max(0.0, min(1.0, float(sum(diffs) / max(1, len(diffs)))))


def compute_structure_timing_diagnostics(
    row: pd.DataFrame | pd.Series | dict[str, Any] | None,
    *,
    side: str | None,
) -> StructureTimingDiagnostics:
    src = row if row is not None else {}

    htf_specs = [
        ("h1_trend_slope_20", 0.0015),
        ("h4_trend_slope_20", 0.0025),
        ("d_trend_slope_20", 0.0035),
        ("h1_trend_strength_20", 1.25),
        ("h4_trend_strength_20", 1.50),
        ("d_trend_strength_20", 1.75),
    ]
    htf_components = [
        _directional_component_score(_row_value(src, key, 0.0), side=side, scale=scale)
        for key, scale in htf_specs
        if _row_has_key(src, key)
    ]
    if not htf_components:
        htf_components = [
            _directional_component_score(_row_value(src, "trend_slope_60", 0.0), side=side, scale=0.0020),
            _directional_component_score(_row_value(src, "trend_strength_60", 0.0), side=side, scale=1.50),
        ]
    htf_alignment_score = float(sum(htf_components) / max(1, len(htf_components)))

    pullback_depth = _row_value(src, "pullback_depth_20", 0.0) if str(side or "").strip().lower() != "short" else _row_value(src, "pushup_depth_20", 0.0)
    pullback_quality_score = _triangular_score(pullback_depth, target=0.0018, width=0.0036)
    pullback_quality_score = float(_clamp01(pullback_quality_score * (0.5 + (0.5 * htf_alignment_score))))

    vol_ref = max(abs(_row_value(src, "vol_20", 0.0)), abs(_row_value(src, "vol_60", 0.0)), 1e-6)
    resume_components = [
        _directional_component_score(_row_value(src, "ret_1", 0.0), side=side, scale=vol_ref * 1.5),
        _directional_component_score(_row_value(src, "edge_decay_12", 0.0), side=side, scale=vol_ref * 1.5),
        _directional_component_score(_row_value(src, "bar_imbalance", 0.0), side=side, scale=0.80),
        _directional_component_score(_row_value(src, "micro_pressure", 0.0), side=side, scale=0.80),
    ]
    resume_trigger_score = float(sum(resume_components) / max(1, len(resume_components)))

    extension_components = [
        _clamp01(max(0.0, _directional_value(_row_value(src, "trend_strength_20", 0.0), side) - 1.25) / 2.0),
        _clamp01(max(0.0, _directional_value(_row_value(src, "trend_strength_60", 0.0), side) - 1.00) / 2.5),
        _clamp01(max(0.0, _directional_value(_row_value(src, "ret_5", 0.0), side) - 0.0012) / 0.0030),
        _clamp01(max(0.0, _directional_value(_row_value(src, "ret_20", 0.0), side) - 0.0030) / 0.0070),
        _clamp01(max(0.0, _directional_value(_row_value(src, "h1_trend_strength_20", 0.0), side) - 1.10) / 2.0),
    ]
    extension_penalty_score = float(sum(extension_components) / max(1, len(extension_components)))
    structure_timing_score = _clamp01(
        (0.40 * htf_alignment_score)
        + (0.25 * pullback_quality_score)
        + (0.25 * resume_trigger_score)
        + (0.10 * (1.0 - extension_penalty_score))
    )
    return StructureTimingDiagnostics(
        htf_alignment_score=float(htf_alignment_score),
        pullback_quality_score=float(pullback_quality_score),
        resume_trigger_score=float(resume_trigger_score),
        extension_penalty_score=float(extension_penalty_score),
        structure_timing_score=float(structure_timing_score),
        structure_extreme_extension=bool(extension_penalty_score >= 0.85),
    )


def compute_expected_edge_bps(
    row: pd.DataFrame | pd.Series | dict[str, Any],
    *,
    swing_prob: float | None = None,
    entry_prob: float | None = None,
    trade_prob: float | None = None,
    regime_prob: float | None = None,
    side: str | None = None,
) -> float:
    mid = max(1e-9, abs(_row_value(row, "mid_close", 0.0)))
    atr_bps = max(0.0, (_row_value(row, "atr_14", 0.0) / mid) * 10000.0)
    trend_bps = max(
        abs(_row_value(row, "trend_slope_20", 0.0)) * 10000.0,
        abs(_row_value(row, "trend_slope_60", 0.0)) * 10000.0,
    )
    vol_bps = max(abs(_row_value(row, "vol_20", 0.0)) * 10000.0, abs(_row_value(row, "vol_60", 0.0)) * 10000.0)
    opportunity_bps = max(atr_bps, trend_bps, vol_bps)
    if opportunity_bps <= 0.0:
        return 0.0

    if swing_prob is None and entry_prob is None and trade_prob is None and regime_prob is None:
        return float(opportunity_bps)

    swing_p = max(0.0, min(1.0, _safe_float(swing_prob, 0.5)))
    entry_p = max(0.0, min(1.0, _safe_float(entry_prob, 0.5)))
    trade_p = max(0.0, min(1.0, _safe_float(trade_prob, 0.5)))
    regime_p = max(0.0, min(1.0, _safe_float(regime_prob, 0.5)))
    direction = str(side or ("long" if swing_p >= 0.5 else "short")).strip().lower()
    directional_prob = directional_swing_confidence(swing_prob=swing_p, side=direction)
    blended_confidence = (
        (0.35 * directional_prob)
        + (0.25 * entry_p)
        + (0.25 * trade_p)
        + (0.15 * regime_p)
    )
    conviction = max(0.0, min(1.0, (blended_confidence - 0.5) * 2.0))
    return float(opportunity_bps * conviction)


def compute_shadow_entry_diagnostics(
    *,
    row: pd.DataFrame | pd.Series | dict[str, Any] | None = None,
    swing_prob: float,
    entry_prob: float,
    trade_prob: float,
    regime_prob: float,
    expected_edge_bps: float,
    spread_bps: float,
    uncertainty_score: float,
    side: str | None,
    pair_tier: str,
    min_swing_prob: float,
    min_entry_prob: float,
    min_trade_prob: float,
    min_expected_edge_bps: float,
    use_uncertainty_gate: bool,
    max_entry_uncertainty: float,
    use_structure_timing_shadow: bool,
    structure_timing_rescue_min_score: float,
    structure_timing_entry_rescue_margin: float,
    structure_timing_max_chase_risk: float,
    entry_hysteresis_margin_bps: float,
    enable_pair_quality_prior: bool = False,
) -> ShadowEntryDiagnostics:
    directional_conf = directional_swing_confidence(swing_prob=float(swing_prob), side=side)
    entry_margin = float(entry_prob) - float(min_entry_prob)
    meta_margin = float(trade_prob) - float(min_trade_prob)
    disagreement = compute_model_disagreement_score(
        directional_swing_confidence_value=float(directional_conf),
        entry_prob=float(entry_prob),
        trade_prob=float(trade_prob),
        regime_prob=float(regime_prob),
    )
    structure = compute_structure_timing_diagnostics(row, side=side)
    raw_calibrated_ev = float(expected_edge_bps) - float(spread_bps)
    pair_quality_multiplier = 1.05 if enable_pair_quality_prior and str(pair_tier).lower() == "tier1" else 1.0
    calibrated_ev_bps = float(raw_calibrated_ev * pair_quality_multiplier)
    uncertainty = max(0.0, _safe_float(uncertainty_score, 0.0))
    structure_bonus_bps = 0.0
    chase_penalty_bps = 0.0
    if bool(use_structure_timing_shadow):
        quality_scale = max(1.0, float(min_expected_edge_bps), abs(float(calibrated_ev_bps)) * 0.75)
        structure_bonus_bps = float(max(0.0, float(structure.structure_timing_score) - 0.5) * quality_scale)
        chase_penalty_bps = float(float(structure.extension_penalty_score) * quality_scale)
        calibrated_ev_bps = float(calibrated_ev_bps + structure_bonus_bps - chase_penalty_bps)
    uncertainty_penalty_bps = float(
        uncertainty * max(1.0, float(min_expected_edge_bps), abs(float(calibrated_ev_bps)) * 0.5)
    )
    disagreement_penalty_bps = float(
        disagreement * max(1.0, float(min_expected_edge_bps), abs(float(calibrated_ev_bps)) * 0.75)
    )
    entry_quality_score = float(calibrated_ev_bps - uncertainty_penalty_bps - disagreement_penalty_bps)

    floor_ok = True
    floor_rejection_reason = "approved"
    structure_rescue_active = False
    structure_rescue_eligible = bool(
        use_structure_timing_shadow
        and float(structure.htf_alignment_score) >= 0.60
        and float(structure.structure_timing_score) >= float(structure_timing_rescue_min_score)
        and float(structure.extension_penalty_score) <= float(structure_timing_max_chase_risk)
    )
    if float(directional_conf) < float(min_swing_prob):
        floor_ok = False
        floor_rejection_reason = "shadow_weak_swing"
    elif float(entry_prob) < float(min_entry_prob):
        if structure_rescue_eligible and float(entry_prob) >= float(min_entry_prob) - float(structure_timing_entry_rescue_margin):
            structure_rescue_active = True
            floor_rejection_reason = "structure_timing_rescue"
        else:
            floor_ok = False
            floor_rejection_reason = "shadow_weak_entry"
    elif float(trade_prob) < float(min_trade_prob):
        floor_ok = False
        floor_rejection_reason = "shadow_meta_reject"
    elif float(calibrated_ev_bps) < float(min_expected_edge_bps):
        if structure_rescue_eligible and float(calibrated_ev_bps) >= float(min_expected_edge_bps) - float(max(0.0, entry_hysteresis_margin_bps)):
            structure_rescue_active = True
            floor_rejection_reason = "structure_timing_rescue"
        else:
            floor_ok = False
            floor_rejection_reason = "shadow_ev_below_floor"
    elif (
        bool(use_uncertainty_gate)
        and float(uncertainty) > float(max_entry_uncertainty)
        and not (
            str(pair_tier).lower() == "tier1"
            and float(calibrated_ev_bps)
            >= float(min_expected_edge_bps) + float(max(0.0, entry_hysteresis_margin_bps))
        )
    ):
        floor_ok = False
        floor_rejection_reason = "shadow_uncertainty_gate"

    return ShadowEntryDiagnostics(
        directional_swing_confidence=float(directional_conf),
        entry_margin=float(entry_margin),
        meta_margin=float(meta_margin),
        model_disagreement_score=float(disagreement),
        htf_alignment_score=float(structure.htf_alignment_score),
        pullback_quality_score=float(structure.pullback_quality_score),
        resume_trigger_score=float(structure.resume_trigger_score),
        extension_penalty_score=float(structure.extension_penalty_score),
        structure_timing_score=float(structure.structure_timing_score),
        structure_bonus_bps=float(structure_bonus_bps),
        chase_penalty_bps=float(chase_penalty_bps),
        calibrated_ev_bps=float(calibrated_ev_bps),
        uncertainty_penalty_bps=float(uncertainty_penalty_bps),
        disagreement_penalty_bps=float(disagreement_penalty_bps),
        entry_quality_score=float(entry_quality_score),
        structure_rescue_active=bool(structure_rescue_active),
        floor_ok=bool(floor_ok),
        floor_rejection_reason=str(floor_rejection_reason),
    )


def _normalize_spread_bps_from_unit(
    *,
    spread_value: float,
    unit: str,
    pair: str = "",
    digits: int | None = None,
    mid_price: float = 0.0,
) -> float:
    spread = max(0.0, _safe_float(spread_value, 0.0))
    txt = str(unit).strip().lower()
    mid = max(0.0, _safe_float(mid_price, 0.0))

    if spread <= 0.0:
        return 0.0
    if txt == "bps":
        return spread
    if mid <= 0.0:
        return 0.0
    if txt == "price":
        return float((spread / mid) * 10000.0)

    pip_size = infer_pip_size(pair=pair, digits=digits)
    if txt == "points":
        points_per_pip = 10.0 if int(digits or 0) in {3, 5} else 1.0
        spread = spread / points_per_pip
        txt = "pips"
    if txt == "pips":
        return float(((spread * pip_size) / mid) * 10000.0)
    return 0.0


def normalize_spread_bps(
    *,
    tick: dict[str, Any] | None = None,
    row: pd.DataFrame | pd.Series | dict[str, Any] | None = None,
    pair: str = "",
) -> tuple[float, str]:
    t = dict(tick or {})
    r = row
    mid_from_tick = (_safe_float(t.get("bid"), 0.0) + _safe_float(t.get("ask"), 0.0)) / 2.0
    if mid_from_tick <= 0.0 and r is not None:
        mid_from_tick = _row_value(r, "mid_close", 0.0)
    digits_raw = t.get("digits")
    digits = int(_safe_float(digits_raw, 0.0)) if digits_raw is not None else None

    if "spread_bps" in t and t.get("spread_bps") is not None:
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread_bps"), 0.0),
            unit="bps",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_bps"
    if "spread_points" in t and t.get("spread_points") is not None:
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread_points"), 0.0),
            unit="points",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_points"
    if "spread_pips" in t and t.get("spread_pips") is not None:
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread_pips"), 0.0),
            unit="pips",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_pips"
    if "spread" in t and t.get("spread") is not None:
        # Backward-compatible bridge alias: treat `spread` from ticks as pips.
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread"), 0.0),
            unit="pips",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_legacy_pips"

    if r is not None:
        mid = _row_value(r, "mid_close", 0.0)
        if _row_has_key(r, "spread_bps"):
            return _normalize_spread_bps_from_unit(
                spread_value=_row_value(r, "spread_bps", 0.0),
                unit="bps",
                pair=pair,
                digits=digits,
                mid_price=mid,
            ), "row.spread_bps"
        spread = _row_value(r, "spread", 0.0)
        if spread > 0.0 and mid > 0.0:
            return _normalize_spread_bps_from_unit(
                spread_value=spread,
                unit="price",
                pair=pair,
                digits=digits,
                mid_price=mid,
            ), "row.spread_price"

    return 0.0, "missing"


def gate_decision(
    *,
    swing_prob: float,
    entry_prob: float,
    trade_prob: float,
    spread_bps: float,
    expected_edge_bps: float,
    side: str | None = None,
    min_swing_prob: float,
    min_entry_prob: float,
    min_trade_prob: float,
    max_spread_bps: float,
    min_expected_edge_bps: float,
    spread_unit_source: str = "unknown",
) -> PolicyGateDecision:
    thresholds = {
        "min_swing_prob": float(min_swing_prob),
        "min_entry_prob": float(min_entry_prob),
        "min_trade_prob": float(min_trade_prob),
        "max_spread_bps": float(max_spread_bps),
        "min_expected_edge_bps": float(min_expected_edge_bps),
    }

    if float(spread_bps) > float(max_spread_bps):
        return PolicyGateDecision(
            allowed=False,
            reason="spread_too_wide",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    if float(expected_edge_bps) < float(min_expected_edge_bps):
        return PolicyGateDecision(
            allowed=False,
            reason="edge_below_hurdle",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    directional_swing_prob = directional_swing_confidence(swing_prob=float(swing_prob), side=side)
    if float(directional_swing_prob) < float(min_swing_prob):
        return PolicyGateDecision(
            allowed=False,
            reason="weak_swing",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    if float(entry_prob) < float(min_entry_prob):
        return PolicyGateDecision(
            allowed=False,
            reason="weak_entry",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    if float(trade_prob) < float(min_trade_prob):
        return PolicyGateDecision(
            allowed=False,
            reason="meta_reject",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    return PolicyGateDecision(
        allowed=True,
        reason="approved",
        threshold_snapshot=thresholds,
        spread_unit_source=str(spread_unit_source or "unknown"),
    )
