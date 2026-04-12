# AGENT: ROLE: Shared adaptive policy engine for twin replay and live runtime parity.
# AGENT: ENTRYPOINT: imported by `tools/fxstack_digital_twin_backtest.py` and `fxstack/runtime/runner.py`.
# AGENT: PRIMARY INPUTS: scorer-side rows, open-position snapshots, baseline readiness flags, settings thresholds.
# AGENT: PRIMARY OUTPUTS: adaptive context columns, entry decisions, cooldown decisions, replacement scores, lifecycle actions.
# AGENT: DEPENDS ON: numpy/pandas plus settings-derived thresholds passed by callers.
# AGENT: CALLED BY: `tools/fxstack_digital_twin_backtest.py`, `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: pure calculations; caller owns persistence and position registries.
# AGENT: HANDSHAKES: twin/prod parity seam for adaptive entry and lifecycle logic.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `tools/fxstack_digital_twin_backtest.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

import math
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from fxstack.live.policy import (
    build_decision_source_chain,
    compute_heuristic_penalty_score,
    compute_model_intelligence_score,
    compose_strategy_mode_fallback_reason,
    normalize_strategy_engine_mode,
)


PLAYBOOK_TREND_PULLBACK = "trend_pullback"
PLAYBOOK_RANGE_MEAN_REVERSION = "range_mean_reversion"
PLAYBOOK_BREAKOUT_EXPANSION = "breakout_expansion"
PLAYBOOK_FAILED_BREAKOUT_REVERSAL = "failed_breakout_reversal"
PLAYBOOK_NO_TRADE = "no_trade"
STRICT_EXEC_MODE = "strict_live_mirror"
ADAPTIVE_EXEC_MODE = "adaptive_multi_playbook"
PLAYBOOK_ORDER = [
    PLAYBOOK_TREND_PULLBACK,
    PLAYBOOK_RANGE_MEAN_REVERSION,
    PLAYBOOK_BREAKOUT_EXPANSION,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
]
PLAYBOOK_THRESHOLDS = {
    PLAYBOOK_TREND_PULLBACK: 0.56,
    PLAYBOOK_RANGE_MEAN_REVERSION: 0.58,
    PLAYBOOK_BREAKOUT_EXPANSION: 0.60,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL: 0.62,
}
LOCATION_FLOOR = 0.32
TRIGGER_FLOOR = 0.45
ENTRY_QUALITY_FLOOR = 0.52
ADAPTIVE_ONLY_QUALITY_FLOOR = 0.80
ADAPTIVE_ONLY_PLAYBOOK_FLOOR = 0.70
ADAPTIVE_ONLY_LOCATION_FLOOR = 0.65
ADAPTIVE_ONLY_TRIGGER_FLOOR = 0.60
ADAPTIVE_ONLY_MACRO_FLOOR = 0.60
BASELINE_PRESERVE_QUALITY_FLOOR = 0.36
BASELINE_PRESERVE_MACRO_FLOOR = 0.45
MODEL_LED_RECOVERY_BASELINE_REASONS = {
    "",
    "none",
    "no_order_required",
    "meta_reject",
    "weak_entry",
    "weak_swing",
    "entry_blocked",
}
PLAYBOOK_REENTRY_COOLDOWNS = {
    PLAYBOOK_TREND_PULLBACK: 3,
    PLAYBOOK_RANGE_MEAN_REVERSION: 4,
    PLAYBOOK_BREAKOUT_EXPANSION: 5,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL: 4,
}
EXIT_REASON_REENTRY_ADDERS = {
    "adaptive_breakout_follow_through_failed": 2,
    "adaptive_failed_breakout_invalidated": 2,
    "adaptive_playbook_exit": 2,
    "adaptive_reverse_ready": 4,
}
TEMPO_GAP_MIN_BASELINE_ENTRIES = 8
TEMPO_GAP_RATIO_FLOOR = 0.60
TEMPO_GAP_ABSOLUTE_SLACK = 4


def clip01(value: Any) -> Any:
    return np.clip(value, 0.0, 1.0)


def _row_float(row: dict[str, Any], key: str, default: float) -> float:
    value = row.get(key, default)
    if value is None:
        return float(default)
    try:
        if pd.isna(value):
            return float(default)
    except Exception:
        pass
    try:
        return float(value)
    except Exception:
        return float(default)


def _row_has_value(row: dict[str, Any], key: str) -> bool:
    if key not in row:
        return False
    value = row.get(key)
    if value is None:
        return False
    try:
        return not bool(pd.isna(value))
    except Exception:
        return True


def _adaptive_quality_from_row(row: dict[str, Any]) -> tuple[float | None, str]:
    for key in ("adaptive_entry_quality", "entry_quality_score_shadow", "adaptive_quality_score"):
        value = _row_float(row, key, float("nan"))
        if math.isfinite(value):
            return float(clip01(value)), key
    return None, ""


def _adaptive_row_is_fresh(row: dict[str, Any]) -> tuple[bool, str]:
    feature_bar = row.get("feature_bar")
    if isinstance(feature_bar, dict):
        if bool(feature_bar.get("stale", False)):
            return False, str(feature_bar.get("reason") or "stale_feature_bar")
        if feature_bar.get("data_fresh") is False:
            return False, str(feature_bar.get("reason") or "stale_feature_bar")
        freshness_secs = _row_float(feature_bar, "freshness_secs", float("nan"))
        stale_after_secs = _row_float(feature_bar, "stale_after_secs", float("nan"))
        if math.isfinite(freshness_secs) and math.isfinite(stale_after_secs) and freshness_secs > stale_after_secs:
            return False, "stale_feature_bar"
    for key, reason in (
        ("feature_bar_stale", "stale_feature_bar"),
        ("feature_serving_stale", "feature_serving_stale"),
        ("adaptive_row_stale", "adaptive_row_stale"),
    ):
        if bool(row.get(key, False)):
            return False, reason
    freshness_secs = row.get("freshness_secs")
    freshness_limit_secs = row.get("freshness_limit_secs")
    if freshness_limit_secs is None:
        freshness_limit_secs = row.get("feature_freshness_limit_secs", row.get("stale_after_secs"))
    if freshness_secs is not None and freshness_limit_secs is not None:
        freshness_secs_f = _safe_float(freshness_secs, float("nan"))
        freshness_limit_f = _safe_float(freshness_limit_secs, float("nan"))
        if math.isfinite(freshness_secs_f) and math.isfinite(freshness_limit_f) and freshness_secs_f > freshness_limit_f:
            return False, "stale_feature_bar"
    required_keys = (
        "playbook_score",
        "location_score",
        "trigger_score",
        "macro_coherence_score",
        "extension_penalty_score",
        "environment_state",
    )
    coverage = sum(1 for key in required_keys if _row_has_value(row, key))
    if coverage < 4:
        return False, "adaptive_row_partial"
    return True, "fresh"


def _playbook_from_environment(environment_state: str) -> str:
    state = str(environment_state or "").strip()
    if state in {"CompressionPreBreakout", "ExpansionBreakout"}:
        return PLAYBOOK_BREAKOUT_EXPANSION
    if state == "BalancedRange":
        return PLAYBOOK_RANGE_MEAN_REVERSION
    if state == "FailedBreakoutReversal":
        return PLAYBOOK_FAILED_BREAKOUT_REVERSAL
    return PLAYBOOK_TREND_PULLBACK


def normalize_baseline_rejection_reason(raw_reason: Any) -> str:
    reason = str(raw_reason or "").strip().lower()
    aliases = {
        "low_entry_prob": "weak_entry",
        "low_swing_prob": "weak_swing",
        "low_trade_prob": "meta_reject",
    }
    return str(aliases.get(reason, reason))


def _adaptive_playbook_thresholds(settings: Any) -> dict[str, float]:
    # Keep the historical floors as the baseline, then allow a small configurable slack
    # so borderline but still viable setups do not collapse into no_trade.
    slack = max(0.0, float(getattr(settings, "adaptive_playbook_threshold_slack", 0.0)))
    return {
        playbook: max(0.50, float(threshold) - slack)
        for playbook, threshold in PLAYBOOK_THRESHOLDS.items()
    }


def parse_enabled_playbooks(raw: str | list[str] | tuple[str, ...] | None) -> set[str]:
    if raw is None:
        return set(PLAYBOOK_ORDER)
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item).strip().lower() for item in raw]
    else:
        parts = [str(item).strip().lower() for item in str(raw).split(",")]
    enabled = {part for part in parts if part in set(PLAYBOOK_ORDER)}
    return enabled or set(PLAYBOOK_ORDER)


def _quantile_stats(values: np.ndarray) -> tuple[float, float, float]:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return 0.0, 0.0, 1.0
    q10 = float(np.quantile(arr, 0.10))
    q50 = float(np.quantile(arr, 0.50))
    q90 = float(np.quantile(arr, 0.90))
    if not math.isfinite(q10):
        q10 = 0.0
    if not math.isfinite(q50):
        q50 = q10
    if not math.isfinite(q90) or q90 <= q10:
        q90 = q10 + 1.0
    return q10, q50, q90


def quant_norm(values: Any, stats: tuple[float, float, float]) -> np.ndarray:
    q10, _q50, q90 = stats
    arr = np.asarray(values, dtype=float)
    denom = max(float(q90) - float(q10), 1e-9)
    return clip01((arr - float(q10)) / denom)


def _pair_currencies(pair: str) -> tuple[str, str]:
    pair_txt = str(pair).upper().strip()
    if len(pair_txt) < 6:
        return pair_txt[:3], pair_txt[3:]
    return pair_txt[:3], pair_txt[3:6]


def _side_sign_from_series(side_series: pd.Series) -> np.ndarray:
    side_txt = side_series.astype(str).str.strip().str.lower()
    return np.where(side_txt.eq("short") | side_txt.eq("sell"), -1.0, 1.0)


def _directional_norm(values: Any, side_sign: np.ndarray, scale: float | np.ndarray = 1.0) -> np.ndarray:
    raw = np.asarray(values, dtype=float) * np.asarray(side_sign, dtype=float)
    denom = np.maximum(np.asarray(scale, dtype=float), 1e-9)
    return clip01(0.5 + (0.5 * np.clip(raw / denom, -1.0, 1.0)))


def _bars_since_impulse(impulse_score: np.ndarray, threshold: float) -> np.ndarray:
    out = np.zeros(len(impulse_score), dtype=float)
    last_impulse = -10_000
    for idx, value in enumerate(np.asarray(impulse_score, dtype=float)):
        if float(value) >= float(threshold):
            last_impulse = idx
            out[idx] = 0.0
        else:
            out[idx] = float(max(0, idx - last_impulse)) if last_impulse >= 0 else 99.0
    return out


def _solve_currency_strength_row(pair_returns: list[tuple[str, float]]) -> dict[str, float]:
    currencies = sorted({ccy for pair, _ret in pair_returns for ccy in _pair_currencies(pair)})
    if not currencies:
        return {}
    idx = {ccy: pos for pos, ccy in enumerate(currencies)}
    a = np.zeros((len(pair_returns) + 1, len(currencies)), dtype=float)
    b = np.zeros(len(pair_returns) + 1, dtype=float)
    for row_idx, (pair, ret_val) in enumerate(pair_returns):
        base, quote = _pair_currencies(pair)
        a[row_idx, idx[base]] = 1.0
        a[row_idx, idx[quote]] = -1.0
        b[row_idx] = float(ret_val)
    a[-1, :] = 1.0
    b[-1] = 0.0
    try:
        sol, *_ = np.linalg.lstsq(a, b, rcond=None)
    except np.linalg.LinAlgError:
        sol = np.zeros(len(currencies), dtype=float)
    sol = sol - float(np.mean(sol))
    return {ccy: float(sol[pos]) for ccy, pos in idx.items()}


def build_run_normalizers(decision_frames: dict[str, pd.DataFrame]) -> dict[str, tuple[float, float, float]]:
    flattened: dict[str, list[np.ndarray]] = {
        "vol_term_ratio": [],
        "cross_pair_dispersion": [],
        "spread_bps": [],
        "impulse_5_atr": [],
        "impulse_20_atr": [],
        "bar_imbalance_abs": [],
        "micro_pressure_abs": [],
        "pair_strength_abs": [],
        "calibrated_ev_bps_shadow": [],
        "pullback_depth_20": [],
        "pushup_depth_20": [],
    }
    for frame in decision_frames.values():
        atr_bps = np.maximum((frame["atr_14"].to_numpy(dtype=float) / np.maximum(frame["mid_close"].to_numpy(dtype=float), 1e-9)) * 10000.0, 1e-6)
        flattened["vol_term_ratio"].append(frame["vol_term_ratio"].to_numpy(dtype=float))
        flattened["cross_pair_dispersion"].append(frame["cross_pair_dispersion"].to_numpy(dtype=float))
        flattened["spread_bps"].append(frame["spread_bps"].to_numpy(dtype=float))
        flattened["impulse_5_atr"].append(np.abs(frame["ret_5"].to_numpy(dtype=float)) / atr_bps)
        flattened["impulse_20_atr"].append(np.abs(frame["ret_20"].to_numpy(dtype=float)) / atr_bps)
        flattened["bar_imbalance_abs"].append(np.abs(frame["bar_imbalance"].to_numpy(dtype=float)))
        flattened["micro_pressure_abs"].append(np.abs(frame["micro_pressure"].to_numpy(dtype=float)))
        flattened["calibrated_ev_bps_shadow"].append(frame["calibrated_ev_bps_shadow"].to_numpy(dtype=float))
        flattened["pullback_depth_20"].append(frame["pullback_depth_20"].to_numpy(dtype=float))
        flattened["pushup_depth_20"].append(frame["pushup_depth_20"].to_numpy(dtype=float))
    return {key: _quantile_stats(np.concatenate(parts) if parts else np.zeros(1, dtype=float)) for key, parts in flattened.items()}


# AGENT FLOW: `attach_adaptive_context` computes the shared environment/playbook/location/trigger columns used by both replay and live adaptive ranking.
def attach_adaptive_context(
    decision_frames: dict[str, pd.DataFrame],
    *,
    pairs: list[str],
    settings: Any,
    enabled_playbooks: set[str],
) -> dict[str, Any]:
    normalizers = build_run_normalizers(decision_frames)
    timeline = next(iter(decision_frames.values())).index
    pair_strength_by_pair = {pair: np.zeros(len(timeline), dtype=float) for pair in pairs}
    risk_tone_jpy = np.zeros(len(timeline), dtype=float)
    risk_tone_chf = np.zeros(len(timeline), dtype=float)
    risk_tone_aud_nzd = np.zeros(len(timeline), dtype=float)

    for idx in range(len(timeline)):
        pair_returns: list[tuple[str, float]] = []
        for pair in pairs:
            ret_val = float(decision_frames[pair]["ret_1"].iloc[idx])
            if math.isfinite(ret_val):
                pair_returns.append((pair, ret_val))
        strengths = _solve_currency_strength_row(pair_returns)
        risk_tone_jpy[idx] = float(strengths.get("JPY", 0.0))
        risk_tone_chf[idx] = float(strengths.get("CHF", 0.0))
        risk_tone_aud_nzd[idx] = float((strengths.get("AUD", 0.0) + strengths.get("NZD", 0.0)) / 2.0)
        for pair in pairs:
            base, quote = _pair_currencies(pair)
            pair_strength_by_pair[pair][idx] = float(strengths.get(base, 0.0) - strengths.get(quote, 0.0))

    pair_strength_scale = max(
        1e-6,
        float(np.quantile(np.abs(np.concatenate([vals for vals in pair_strength_by_pair.values()])), 0.90)) if pair_strength_by_pair else 1.0,
    )

    for pair, frame in decision_frames.items():
        side_sign = _side_sign_from_series(frame["signal_side"])
        atr_bps = np.maximum((frame["atr_14"].to_numpy(dtype=float) / np.maximum(frame["mid_close"].to_numpy(dtype=float), 1e-9)) * 10000.0, 1e-6)
        impulse_5_atr = np.abs(frame["ret_5"].to_numpy(dtype=float)) / atr_bps
        impulse_20_atr = np.abs(frame["ret_20"].to_numpy(dtype=float)) / atr_bps
        vol_term_quant = quant_norm(frame["vol_term_ratio"].to_numpy(dtype=float), normalizers["vol_term_ratio"])
        dispersion_penalty = quant_norm(frame["cross_pair_dispersion"].to_numpy(dtype=float), normalizers["cross_pair_dispersion"])
        spread_quality = 1.0 - clip01(frame["spread_bps"].to_numpy(dtype=float) / max(float(settings.max_allowed_spread_bps), 1e-9))
        pair_strength_score = pair_strength_by_pair[pair]
        macro_coherence_score = clip01(0.5 + (side_sign * pair_strength_score / pair_strength_scale))
        trend_strength_h1 = clip01(np.abs(frame["h1_trend_strength_20"].to_numpy(dtype=float)) / 1.25)
        trend_strength_h4 = clip01(np.abs(frame["h4_trend_strength_20"].to_numpy(dtype=float)) / 1.50)
        trend_strength_d = clip01(np.abs(np.where(np.abs(frame["d_trend_strength_20"].to_numpy(dtype=float)) > 0.0, frame["d_trend_strength_20"].to_numpy(dtype=float), frame["h4_trend_strength_20"].to_numpy(dtype=float))) / 1.75)
        trend_strength_score = np.mean(np.column_stack([trend_strength_h1, trend_strength_h4, trend_strength_d]), axis=1)
        scenario = frame["scenario_bucket"].astype(str)
        regime = frame["regime_bucket"].astype(str)
        scenario_is_range_like = scenario.isin(["range_mean_reversion", "asia_low_liquidity"]).to_numpy(dtype=float)
        scenario_is_breakout_like = scenario.isin(["volatility_expansion", "breakout_initiation"]).to_numpy(dtype=float)
        regime_is_range = regime.eq("range").to_numpy(dtype=float)
        trend_persistence_score = clip01(
            (0.35 * frame["htf_alignment_score"].to_numpy(dtype=float))
            + (0.20 * frame["directional_swing_confidence"].to_numpy(dtype=float))
            + (0.20 * trend_strength_score)
            + (0.15 * macro_coherence_score)
            + (0.10 * (1.0 - dispersion_penalty))
        )
        compression_score = clip01(
            (0.45 * (1.0 - vol_term_quant))
            + (0.20 * (1.0 - quant_norm(impulse_5_atr, normalizers["impulse_5_atr"])))
            + (0.20 * (1.0 - dispersion_penalty))
            + (0.15 * scenario_is_range_like)
        )
        expansion_score = clip01(
            (0.40 * vol_term_quant)
            + (0.25 * quant_norm(impulse_5_atr, normalizers["impulse_5_atr"]))
            + (0.20 * scenario_is_breakout_like)
            + (0.15 * macro_coherence_score)
        )
        range_score = clip01(
            (0.35 * (1.0 - trend_persistence_score))
            + (0.25 * regime_is_range)
            + (0.20 * (1.0 - np.abs((2.0 * macro_coherence_score) - 1.0)))
            + (0.20 * (1.0 - vol_term_quant))
        )
        hostility_score = np.maximum.reduce(
            [
                frame["session_entry_blocked"].to_numpy(dtype=float),
                0.35 * clip01(frame["spread_bps"].to_numpy(dtype=float) / max(float(settings.max_allowed_spread_bps), 1e-9)),
                0.25 * frame["uncertainty_score"].to_numpy(dtype=float),
                0.20 * frame["model_disagreement_score"].to_numpy(dtype=float),
                0.20 * dispersion_penalty,
            ]
        )
        precompression_avg_12 = pd.Series(compression_score, index=frame.index).shift(1).rolling(12, min_periods=1).mean().fillna(0.5).to_numpy(dtype=float)
        environment_state = np.full(len(frame), "DislocatedHostile", dtype=object)
        hostile_mask = (
            frame["session_entry_blocked"].to_numpy(dtype=bool)
            | (frame["spread_bps"].to_numpy(dtype=float) > float(settings.max_allowed_spread_bps))
            | (hostility_score >= 0.75)
        )
        environment_state[(~hostile_mask) & (expansion_score >= 0.67) & (precompression_avg_12 >= 0.55)] = "ExpansionBreakout"
        environment_state[(~hostile_mask) & (compression_score >= 0.67) & (expansion_score < 0.67)] = "CompressionPreBreakout"
        environment_state[(~hostile_mask) & (trend_persistence_score >= 0.68) & (frame["pullback_quality_score"].to_numpy(dtype=float) < 0.45) & (frame["extension_penalty_score"].to_numpy(dtype=float) < 0.70)] = "PersistentTrend"
        environment_state[(~hostile_mask) & (trend_persistence_score >= 0.60) & (frame["pullback_quality_score"].to_numpy(dtype=float) >= 0.45) & (frame["extension_penalty_score"].to_numpy(dtype=float) < 0.75)] = "CorrectiveTrend"
        environment_state[(~hostile_mask) & (range_score >= 0.60)] = "BalancedRange"

        fallback_scores = np.column_stack([trend_persistence_score, trend_persistence_score * (0.85 + (0.15 * frame["pullback_quality_score"].to_numpy(dtype=float))), range_score, compression_score])
        fallback_names = np.asarray(["PersistentTrend", "CorrectiveTrend", "BalancedRange", "CompressionPreBreakout"], dtype=object)
        undecided = (~hostile_mask) & (environment_state == "DislocatedHostile")
        if np.any(undecided):
            environment_state[undecided] = fallback_names[np.argmax(fallback_scores[undecided], axis=1)]

        selected_depth_quant = np.where(
            side_sign < 0.0,
            quant_norm(frame["pushup_depth_20"].to_numpy(dtype=float), normalizers["pushup_depth_20"]),
            quant_norm(frame["pullback_depth_20"].to_numpy(dtype=float), normalizers["pullback_depth_20"]),
        )
        trigger_flip_score = (0.5 * _directional_norm(-frame["bar_imbalance"].to_numpy(dtype=float), side_sign, 0.80)) + (
            0.5 * _directional_norm(-frame["micro_pressure"].to_numpy(dtype=float), side_sign, 0.80)
        )
        impulse_score = quant_norm(impulse_5_atr, normalizers["impulse_5_atr"])
        bars_since_impulse = _bars_since_impulse(impulse_score, threshold=0.75)
        breakout_proximity_score = 1.0 - clip01(bars_since_impulse / 8.0)
        recent_impulse_dir = np.sign(pd.Series(frame["ret_1"].to_numpy(dtype=float), index=frame.index).rolling(6, min_periods=1).sum().to_numpy(dtype=float))
        impulse_opposition = np.where(recent_impulse_dir == 0.0, 0.0, np.where(recent_impulse_dir == side_sign, 0.0, 1.0))
        failure_confirmation_score = clip01(
            (0.40 * frame["extension_penalty_score"].to_numpy(dtype=float))
            + (0.30 * trigger_flip_score)
            + (0.20 * impulse_opposition)
            + (0.10 * spread_quality)
        )
        breakout_trigger_score = clip01(
            (0.45 * frame["resume_trigger_score"].to_numpy(dtype=float))
            + (0.25 * quant_norm(impulse_5_atr, normalizers["impulse_5_atr"]))
            + (0.20 * spread_quality)
            + (0.10 * _directional_norm(frame["bar_imbalance"].to_numpy(dtype=float), side_sign, 0.80))
        )
        reward_path_score = clip01(frame["calibrated_ev_bps_shadow"].to_numpy(dtype=float) / max(float(settings.min_expected_edge_bps) * 3.0, 1e-9))
        stop_efficiency_score = clip01(1.0 - (frame["spread_bps"].to_numpy(dtype=float) / np.maximum(0.25 * atr_bps, 1e-9)))
        extreme_chase = (
            (frame["extension_penalty_score"].to_numpy(dtype=float) >= 0.85)
            & (frame["pullback_quality_score"].to_numpy(dtype=float) <= 0.20)
            & (bars_since_impulse <= 2.0)
        )

        trend_score = clip01(
            (0.30 * trend_persistence_score)
            + (0.25 * frame["pullback_quality_score"].to_numpy(dtype=float))
            + (0.20 * frame["resume_trigger_score"].to_numpy(dtype=float))
            + (0.15 * macro_coherence_score)
            + (0.10 * (1.0 - frame["extension_penalty_score"].to_numpy(dtype=float)))
        )
        range_pb_score = clip01(
            (0.30 * range_score)
            + (0.25 * selected_depth_quant)
            + (0.20 * trigger_flip_score)
            + (0.15 * spread_quality)
            + (0.10 * (1.0 - frame["uncertainty_score"].to_numpy(dtype=float)))
        )
        breakout_score = clip01(
            (0.30 * expansion_score)
            + (0.25 * precompression_avg_12)
            + (0.20 * breakout_trigger_score)
            + (0.15 * macro_coherence_score)
            + (0.10 * spread_quality)
        )
        reversal_score = clip01(
            (0.30 * failure_confirmation_score)
            + (0.25 * frame["extension_penalty_score"].to_numpy(dtype=float))
            + (0.20 * trigger_flip_score)
            + (0.15 * (1.0 - trend_persistence_score))
            + (0.10 * spread_quality)
        )

        score_map = {
            PLAYBOOK_TREND_PULLBACK: trend_score,
            PLAYBOOK_RANGE_MEAN_REVERSION: range_pb_score,
            PLAYBOOK_BREAKOUT_EXPANSION: breakout_score,
            PLAYBOOK_FAILED_BREAKOUT_REVERSAL: reversal_score,
        }
        eligible_map = {
            PLAYBOOK_TREND_PULLBACK: np.isin(environment_state, ["PersistentTrend", "CorrectiveTrend"]) & (frame["htf_alignment_score"].to_numpy(dtype=float) >= 0.55) & (frame["extension_penalty_score"].to_numpy(dtype=float) < 0.85),
            PLAYBOOK_RANGE_MEAN_REVERSION: (environment_state == "BalancedRange") & (hostility_score < 0.75) & (frame["extension_penalty_score"].to_numpy(dtype=float) <= 0.65),
            PLAYBOOK_BREAKOUT_EXPANSION: np.isin(environment_state, ["CompressionPreBreakout", "ExpansionBreakout"]) & (macro_coherence_score >= 0.50) & (frame["spread_bps"].to_numpy(dtype=float) <= float(settings.max_allowed_spread_bps)),
            PLAYBOOK_FAILED_BREAKOUT_REVERSAL: np.isin(environment_state, ["ExpansionBreakout", "PersistentTrend", "CorrectiveTrend"]) & (frame["extension_penalty_score"].to_numpy(dtype=float) >= 0.60) & (failure_confirmation_score >= 0.55),
        }
        for playbook in list(score_map):
            if playbook not in enabled_playbooks:
                eligible_map[playbook] = np.zeros(len(frame), dtype=bool)
                score_map[playbook] = np.zeros(len(frame), dtype=float)

        candidate_matrix = np.column_stack([np.where(eligible_map[playbook], score_map[playbook], 0.0) for playbook in PLAYBOOK_ORDER])
        best_idx = np.argmax(candidate_matrix, axis=1)
        playbook = np.asarray([PLAYBOOK_ORDER[idx] for idx in best_idx], dtype=object)
        playbook_score = candidate_matrix[np.arange(len(frame)), best_idx]
        playbook_thresholds = _adaptive_playbook_thresholds(settings)
        playbook_threshold = np.asarray([playbook_thresholds.get(str(pb), 1.0) for pb in playbook], dtype=float)
        no_trade_mask = hostile_mask | extreme_chase | (playbook_score < playbook_threshold)
        playbook[no_trade_mask] = PLAYBOOK_NO_TRADE

        location_score = np.zeros(len(frame), dtype=float)
        location_score = np.where(
            playbook == PLAYBOOK_TREND_PULLBACK,
            clip01((0.40 * frame["pullback_quality_score"].to_numpy(dtype=float)) + (0.30 * (1.0 - frame["extension_penalty_score"].to_numpy(dtype=float))) + (0.20 * reward_path_score) + (0.10 * stop_efficiency_score)),
            location_score,
        )
        location_score = np.where(
            playbook == PLAYBOOK_RANGE_MEAN_REVERSION,
            clip01((0.45 * selected_depth_quant) + (0.25 * trigger_flip_score) + (0.20 * (1.0 - vol_term_quant)) + (0.10 * reward_path_score)),
            location_score,
        )
        location_score = np.where(
            playbook == PLAYBOOK_BREAKOUT_EXPANSION,
            clip01((0.35 * precompression_avg_12) + (0.30 * breakout_proximity_score) + (0.20 * spread_quality) + (0.15 * macro_coherence_score)),
            location_score,
        )
        location_score = np.where(
            playbook == PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
            clip01((0.35 * frame["extension_penalty_score"].to_numpy(dtype=float)) + (0.30 * failure_confirmation_score) + (0.20 * breakout_proximity_score) + (0.15 * stop_efficiency_score)),
            location_score,
        )

        trigger_score = np.zeros(len(frame), dtype=float)
        trigger_score = np.where(
            playbook == PLAYBOOK_TREND_PULLBACK,
            clip01((0.50 * frame["resume_trigger_score"].to_numpy(dtype=float)) + (0.25 * _directional_norm(frame["bar_imbalance"].to_numpy(dtype=float), side_sign, 0.80)) + (0.15 * _directional_norm(frame["micro_pressure"].to_numpy(dtype=float), side_sign, 0.80)) + (0.10 * spread_quality)),
            trigger_score,
        )
        trigger_score = np.where(
            playbook == PLAYBOOK_RANGE_MEAN_REVERSION,
            clip01((0.40 * trigger_flip_score) + (0.25 * breakout_proximity_score) + (0.20 * spread_quality) + (0.15 * (1.0 - dispersion_penalty))),
            trigger_score,
        )
        trigger_score = np.where(
            playbook == PLAYBOOK_BREAKOUT_EXPANSION,
            clip01((0.45 * breakout_trigger_score) + (0.20 * _directional_norm(frame["bar_imbalance"].to_numpy(dtype=float), side_sign, 0.80)) + (0.20 * spread_quality) + (0.15 * macro_coherence_score)),
            trigger_score,
        )
        trigger_score = np.where(
            playbook == PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
            clip01((0.45 * failure_confirmation_score) + (0.20 * trigger_flip_score) + (0.20 * spread_quality) + (0.15 * breakout_proximity_score)),
            trigger_score,
        )

        base_reason = np.full(len(frame), "approved", dtype=object)
        base_reason[hostile_mask] = "hostile_environment"
        base_reason[extreme_chase & (~hostile_mask)] = "extreme_chase"
        base_reason[(playbook == PLAYBOOK_NO_TRADE) & (~hostile_mask) & (~extreme_chase)] = "low_playbook_score"

        decision_frames[pair] = frame.assign(
            pair_strength_score=pd.Series(pair_strength_score, index=frame.index, dtype=float),
            macro_coherence_score=pd.Series(macro_coherence_score, index=frame.index, dtype=float),
            currency_dispersion_penalty=pd.Series(dispersion_penalty, index=frame.index, dtype=float),
            risk_tone_jpy=pd.Series(risk_tone_jpy, index=frame.index, dtype=float),
            risk_tone_chf=pd.Series(risk_tone_chf, index=frame.index, dtype=float),
            risk_tone_aud_nzd=pd.Series(risk_tone_aud_nzd, index=frame.index, dtype=float),
            trend_persistence_score=pd.Series(trend_persistence_score, index=frame.index, dtype=float),
            compression_score=pd.Series(compression_score, index=frame.index, dtype=float),
            expansion_score=pd.Series(expansion_score, index=frame.index, dtype=float),
            range_score=pd.Series(range_score, index=frame.index, dtype=float),
            hostility_score=pd.Series(hostility_score, index=frame.index, dtype=float),
            precompression_avg_12=pd.Series(precompression_avg_12, index=frame.index, dtype=float),
            selected_depth_quant=pd.Series(selected_depth_quant, index=frame.index, dtype=float),
            trigger_flip_score=pd.Series(trigger_flip_score, index=frame.index, dtype=float),
            failure_confirmation_score=pd.Series(failure_confirmation_score, index=frame.index, dtype=float),
            breakout_trigger_score=pd.Series(breakout_trigger_score, index=frame.index, dtype=float),
            reward_path_score=pd.Series(reward_path_score, index=frame.index, dtype=float),
            stop_efficiency_score=pd.Series(stop_efficiency_score, index=frame.index, dtype=float),
            impulse_5_atr=pd.Series(impulse_5_atr, index=frame.index, dtype=float),
            impulse_20_atr=pd.Series(impulse_20_atr, index=frame.index, dtype=float),
            bars_since_impulse=pd.Series(bars_since_impulse, index=frame.index, dtype=float),
            breakout_proximity_score=pd.Series(breakout_proximity_score, index=frame.index, dtype=float),
            environment_state=pd.Series(environment_state, index=frame.index, dtype="object").astype("category"),
            playbook=pd.Series(playbook, index=frame.index, dtype="object").astype("category"),
            playbook_score=pd.Series(playbook_score, index=frame.index, dtype=float),
            location_score=pd.Series(location_score, index=frame.index, dtype=float),
            trigger_score=pd.Series(trigger_score, index=frame.index, dtype=float),
            adaptive_base_rejection_reason=pd.Series(base_reason, index=frame.index, dtype="object").astype("category"),
            extreme_chase=pd.Series(extreme_chase, index=frame.index, dtype=bool),
        )

    return {
        "normalizers": normalizers,
        "pair_strength_scale": float(pair_strength_scale),
    }


def currency_crowding_penalty(pair: str, side: str, open_positions: dict[str, Any]) -> float:
    base, quote = _pair_currencies(pair)
    side_txt = str(side).strip().lower()
    base_count = 0
    quote_count = 0
    usd_cohort = 0
    defensive_cohort = 0
    for pos in open_positions.values():
        p_base, p_quote = _pair_currencies(getattr(pos, "pair", ""))
        if p_base == base or p_quote == base:
            base_count += 1
        if p_base == quote or p_quote == quote:
            quote_count += 1
        if str(getattr(pos, "side", "")).strip().lower() == side_txt:
            if "USD" in {p_base, p_quote} and "USD" in {base, quote}:
                usd_cohort += 1
            if ({p_base, p_quote} & {"JPY", "CHF"}) and ({base, quote} & {"JPY", "CHF"}):
                defensive_cohort += 1
    penalty = 0.0
    if base_count >= 2:
        penalty += 0.20
    if quote_count >= 2:
        penalty += 0.20
    if usd_cohort >= 1 and ("USD" in {base, quote}):
        penalty += 0.15
    if defensive_cohort >= 1 and ({base, quote} & {"JPY", "CHF"}):
        penalty += 0.10
    return float(min(0.60, penalty))


def playbook_diversification_penalty(playbook: str, session_bucket: str, open_positions: dict[str, Any]) -> float:
    same_playbook = 0
    same_playbook_session = 0
    for pos in open_positions.values():
        if str(getattr(pos, "playbook", "")) == str(playbook):
            same_playbook += 1
            if str(getattr(pos, "entry_session_bucket", "")) == str(session_bucket):
                same_playbook_session += 1
    penalty = 0.0
    if same_playbook >= 3:
        penalty += 0.10
    if same_playbook_session >= 2:
        penalty += 0.05
    return float(min(0.20, penalty))


# AGENT STATE: Re-entry cooldowns consume the caller-owned recent-exit registry; this keeps the policy pure while preserving live/twin parity.
def adaptive_reentry_block(
    *,
    pair: str,
    side: str,
    playbook: str,
    bar_idx: int,
    exit_registry: dict[str, dict[str, Any]],
    cooldown_scale: float = 1.0,
) -> dict[str, Any]:
    state = dict(exit_registry.get(str(pair), {}))
    if not state:
        return {"blocked": False, "reason": "", "bars_remaining": 0}
    since = max(0, int(bar_idx) - int(state.get("bar_idx", -10_000)))
    prior_side = str(state.get("side") or "")
    # Re-entry cooldowns are keyed off the playbook that was exited, not the candidate
    # playbook we are evaluating now. Fall back to the candidate only when the registry
    # entry is missing legacy data.
    exit_playbook = str(state.get("playbook") or playbook or PLAYBOOK_TREND_PULLBACK)
    cooldown = int(PLAYBOOK_REENTRY_COOLDOWNS.get(exit_playbook, PLAYBOOK_REENTRY_COOLDOWNS[PLAYBOOK_TREND_PULLBACK]))
    cooldown += int(EXIT_REASON_REENTRY_ADDERS.get(str(state.get("reason") or ""), 0))
    cooldown = max(1, int(math.ceil(float(cooldown) * max(0.1, float(cooldown_scale)))))
    if prior_side == str(side) and since < cooldown:
        return {"blocked": True, "reason": "adaptive_reentry_cooldown", "bars_remaining": int(cooldown - since)}
    if prior_side and prior_side != str(side) and since < 2:
        return {"blocked": True, "reason": "adaptive_side_flip_cooldown", "bars_remaining": int(2 - since)}
    return {"blocked": False, "reason": "", "bars_remaining": 0}


# AGENT PARITY: Tempo-gap detection keeps adaptive execution aggressive relative to the strict baseline without loosening hard safety.
def adaptive_tempo_gap_active(*, baseline_entries_so_far: int, adaptive_entries_so_far: int) -> bool:
    baseline_entries = max(0, int(baseline_entries_so_far))
    adaptive_entries = max(0, int(adaptive_entries_so_far))
    if baseline_entries < TEMPO_GAP_MIN_BASELINE_ENTRIES:
        return False
    ratio_threshold = int(math.floor(float(baseline_entries) * float(TEMPO_GAP_RATIO_FLOOR)))
    absolute_threshold = int(baseline_entries - TEMPO_GAP_ABSOLUTE_SLACK)
    required_min = max(ratio_threshold, absolute_threshold)
    return adaptive_entries < required_min


# AGENT FLOW: Replacement keep score is the portfolio rotation primitive; lower scores mark positions that can be evicted for better candidates.
def adaptive_replacement_keep_score(
    *,
    lifecycle_action: str,
    lifecycle_reason: str,
    playbook_score: float,
    location_score: float,
    trigger_score: float,
    entry_trade_prob: float,
    entry_macro_coherence_score: float,
    aggressive_fallback_used: bool,
) -> float:
    action = str(lifecycle_action or "hold")
    reason = str(lifecycle_reason or "hold")
    if action == "exit":
        action_component = 0.05
    elif action == "partial_tp":
        action_component = 0.22
    else:
        action_component = 0.34
    keep_score = (
        action_component
        + (0.22 * clip01(max(float(playbook_score), float(entry_trade_prob))))
        + (0.18 * clip01(float(location_score)))
        + (0.16 * clip01(float(trigger_score)))
        + (0.10 * clip01(float(entry_macro_coherence_score)))
    )
    if reason == "adaptive_hold_baseline_floor":
        keep_score -= 0.14
    if bool(aggressive_fallback_used):
        keep_score -= 0.04
    return float(clip01(keep_score))


# AGENT HOT PATH: Adaptive entry evaluation is the main parity seam shared by runtime and twin for playbook routing, quality scoring, and fallback logic.
def evaluate_adaptive_entry(
    *,
    row: dict[str, Any],
    strict_ready: bool,
    open_positions: dict[str, Any],
    settings: Any,
    fallback_margin: float,
) -> dict[str, Any]:
    strategy_engine_mode = normalize_strategy_engine_mode(getattr(settings, "strategy_engine_mode", "supervised_legacy"))
    spread_bps = float(row.get("spread_bps", 0.0) or 0.0)
    max_spread = float(getattr(settings, "max_allowed_spread_bps", 0.0) or 0.0)
    playbook = str(row.get("playbook") or PLAYBOOK_NO_TRADE)
    session_bucket = str(row.get("session_bucket") or "")
    pair = str(row.get("pair") or "")
    side = str(row.get("position_side") or row.get("signal_side") or row.get("side") or "").strip().lower()
    session_blocked = bool(row.get("session_entry_blocked", False))
    hostile = str(row.get("environment_state") or "") == "DislocatedHostile"
    extreme_chase = bool(row.get("extreme_chase", False))
    baseline_rejection_reason = normalize_baseline_rejection_reason(
        row.get("baseline_rejection_reason") or row.get("strict_rejection_reason") or ""
    )
    crowd_penalty = currency_crowding_penalty(pair, side, open_positions)
    diversify_penalty = playbook_diversification_penalty(playbook, session_bucket, open_positions)
    macro_coherence = float(row.get("macro_coherence_score", 0.0) or 0.0)
    playbook_score = float(row.get("playbook_score", 0.0) or 0.0)
    location_score = float(row.get("location_score", 0.0) or 0.0)
    trigger_score = float(row.get("trigger_score", 0.0) or 0.0)
    environment_state = str(row.get("environment_state") or "")
    scorer_quality, scorer_quality_source = _adaptive_quality_from_row(row)
    regime_prob = _row_float(row, "regime_prob", max(macro_coherence, 0.5))
    swing_prob = _row_float(row, "swing_prob", max(playbook_score, 0.5))
    entry_prob = _row_float(row, "entry_prob", max(location_score, 0.5))
    trade_prob = _row_float(row, "trade_prob", max(trigger_score, 0.5))
    expected_edge_bps = _row_float(row, "expected_edge_bps", _row_float(row, "calibrated_ev_bps_shadow", 0.0))
    model_intelligence_score = float(
        compute_model_intelligence_score(
            regime_prob=regime_prob,
            swing_prob=swing_prob,
            entry_prob=entry_prob,
            trade_prob=trade_prob,
            expected_edge_bps=expected_edge_bps,
            min_expected_edge_bps=float(getattr(settings, "min_expected_edge_bps", 0.0) or 0.0),
            side=side,
        )
    )
    heuristic_penalty_score = float(
        compute_heuristic_penalty_score(
            spread_bps=spread_bps,
            max_spread_bps=max_spread,
            uncertainty_score=float(row.get("uncertainty_score", 0.0) or 0.0),
            model_disagreement_score=float(row.get("model_disagreement_score", 0.0) or 0.0),
            structure_timing_score=float(row.get("structure_timing_score", 0.0) or 0.0),
            extension_penalty_score=float(row.get("extension_penalty_score", 0.0) or 0.0),
            session_blocked=session_blocked,
        )
    )
    heuristic_support = float(
        clip01(
            (0.35 * playbook_score)
            + (0.25 * location_score)
            + (0.20 * trigger_score)
            + (0.20 * macro_coherence)
        )
    )
    computed_adaptive_quality = float(
        clip01((0.75 * model_intelligence_score) + (0.10 * heuristic_support) - (0.45 * heuristic_penalty_score))
    )
    adaptive_quality = float(scorer_quality if scorer_quality is not None else computed_adaptive_quality)
    rejection_reason = str(row.get("adaptive_base_rejection_reason") or "approved")
    allowed = False
    quality_support = max(float(model_intelligence_score), float(adaptive_quality))
    strong_model_setup = bool(
        quality_support >= float(ENTRY_QUALITY_FLOOR)
        and float(expected_edge_bps) > float(getattr(settings, "min_expected_edge_bps", 0.0) or 0.0)
        and float(heuristic_penalty_score) <= 0.45
    )
    if session_blocked:
        rejection_reason = str(row.get("session_entry_block_reason") or "session_blocked")
    elif spread_bps > max_spread:
        rejection_reason = "spread_too_wide"
    elif hostile:
        rejection_reason = "hostile_environment"
    elif extreme_chase:
        rejection_reason = "extreme_chase"
    elif playbook == PLAYBOOK_NO_TRADE and not strong_model_setup:
        rejection_reason = "low_playbook_score"
    elif float(row.get("trigger_score", 0.0) or 0.0) < TRIGGER_FLOOR:
        rejection_reason = "low_trigger_score"
    elif float(row.get("location_score", 0.0) or 0.0) < LOCATION_FLOOR:
        rejection_reason = "low_location_score"
    elif adaptive_quality >= ENTRY_QUALITY_FLOOR:
        allowed = True
        rejection_reason = "approved"
        if playbook == PLAYBOOK_NO_TRADE and strong_model_setup:
            playbook = _playbook_from_environment(environment_state)
    else:
        rejection_reason = "low_adaptive_quality"
    aggressive_fallback = False
    fallback_reason = compose_strategy_mode_fallback_reason(
        strategy_engine_mode=strategy_engine_mode,
        fallback_reason="none",
    )
    near_threshold_model = float(model_intelligence_score) >= float(ENTRY_QUALITY_FLOOR - float(fallback_margin))
    heuristic_reasonable = float(heuristic_penalty_score) <= 0.45
    standard_fallback_ready = (
        strict_ready
        and rejection_reason in {"low_playbook_score", "low_location_score", "low_trigger_score", "low_adaptive_quality"}
        and (not hostile)
        and (not extreme_chase)
        and adaptive_quality >= (ENTRY_QUALITY_FLOOR - float(fallback_margin))
        and near_threshold_model
        and heuristic_reasonable
    )
    baseline_preserve_fallback_ready = (
        strict_ready
        and rejection_reason == "low_playbook_score"
        and playbook == PLAYBOOK_NO_TRADE
        and (not hostile)
        and (not extreme_chase)
        and adaptive_quality >= BASELINE_PRESERVE_QUALITY_FLOOR
        and macro_coherence >= BASELINE_PRESERVE_MACRO_FLOOR
        and near_threshold_model
        and heuristic_reasonable
    )
    model_led_recovery_ready = (
        (not allowed)
        and (not strict_ready)
        and rejection_reason in {"low_playbook_score", "low_trigger_score", "low_adaptive_quality"}
        and baseline_rejection_reason in MODEL_LED_RECOVERY_BASELINE_REASONS
        and (not hostile)
        and (not extreme_chase)
        and strong_model_setup
        and adaptive_quality >= BASELINE_PRESERVE_QUALITY_FLOOR
        and max(location_score, trigger_score, macro_coherence) >= 0.58
    )
    adaptive_only_exception_ready = (
        allowed
        and (not strict_ready)
        and baseline_rejection_reason in MODEL_LED_RECOVERY_BASELINE_REASONS
        and strong_model_setup
        and adaptive_quality >= ENTRY_QUALITY_FLOOR
        and max(location_score, trigger_score, macro_coherence) >= 0.58
    )
    if (
        (not allowed)
        and (standard_fallback_ready or baseline_preserve_fallback_ready or model_led_recovery_ready)
    ):
        allowed = True
        aggressive_fallback = True
        fallback_reason = compose_strategy_mode_fallback_reason(
            strategy_engine_mode=strategy_engine_mode,
            fallback_reason="aggressive_fallback",
        )
        rejection_reason = "approved"
        if playbook == PLAYBOOK_NO_TRADE:
            if environment_state in {"CompressionPreBreakout", "ExpansionBreakout"}:
                playbook = PLAYBOOK_BREAKOUT_EXPANSION
            elif environment_state == "BalancedRange":
                playbook = PLAYBOOK_RANGE_MEAN_REVERSION
            else:
                playbook = PLAYBOOK_TREND_PULLBACK
    elif (
        allowed
        and (not strict_ready)
        and (not adaptive_only_exception_ready)
        and (
            adaptive_quality < ADAPTIVE_ONLY_QUALITY_FLOOR
            or playbook_score < ADAPTIVE_ONLY_PLAYBOOK_FLOOR
            or location_score < ADAPTIVE_ONLY_LOCATION_FLOOR
            or trigger_score < ADAPTIVE_ONLY_TRIGGER_FLOOR
            or macro_coherence < ADAPTIVE_ONLY_MACRO_FLOOR
            or baseline_rejection_reason not in {"meta_reject", "weak_entry", "weak_swing"}
        )
    ):
        allowed = False
        rejection_reason = "adaptive_only_quality_gate"
    return {
        "adaptive_allowed": bool(allowed),
        "adaptive_rejection_reason": str(rejection_reason),
        "playbook": str(playbook),
        "adaptive_entry_quality": float(adaptive_quality),
        "adaptive_entry_quality_source": str(scorer_quality_source or "computed"),
        "adaptive_entry_quality_computed": float(computed_adaptive_quality),
        "strategy_engine_mode": str(strategy_engine_mode),
        "model_intelligence_score": float(model_intelligence_score),
        "heuristic_penalty_score": float(heuristic_penalty_score),
        "currency_crowding_penalty": float(crowd_penalty),
        "playbook_diversification_penalty": float(diversify_penalty),
        "aggressive_fallback_used": bool(aggressive_fallback),
        "fallback_used": bool(aggressive_fallback),
        "fallback_reason": str(fallback_reason),
        "decision_source_chain": build_decision_source_chain(
            gate_reason=str(rejection_reason),
            fallback_used=bool(aggressive_fallback),
            fallback_reason=str(fallback_reason),
            strategy_engine_mode=strategy_engine_mode,
            model_sources=("regime_model", "swing_model", "entry_model", "trade_model"),
        ),
    }


# AGENT HOT PATH: Adaptive lifecycle decisions reinterpret model outputs into playbook-aware hold/reduce/exit/reverse actions.
def adaptive_lifecycle_decision(
    *,
    position: Any,
    row: dict[str, Any],
    unrealized_pnl_usd: float,
    age_bars: float,
    bar_idx: int,
    exit_action_probs: dict[str, float],
    reversal_context_active: bool,
    reversal_ready: bool,
    reversal_failure_prob: float,
    reversal_opportunity_prob: float,
) -> dict[str, Any]:
    playbook = str(getattr(position, "playbook", PLAYBOOK_TREND_PULLBACK) or PLAYBOOK_TREND_PULLBACK)
    hold_component = float(exit_action_probs.get("hold", 0.0))
    reduce_component = float(exit_action_probs.get("partial_tp", exit_action_probs.get("reduce", 0.0)))
    exit_component = float(exit_action_probs.get("exit", 0.0))
    reversal_component = float((reversal_failure_prob + reversal_opportunity_prob) / 2.0) if reversal_context_active else 0.0
    trigger_score = float(row.get("trigger_score", 0.0) or 0.0)
    playbook_score = float(row.get("playbook_score", 0.0) or 0.0)
    location_score = float(row.get("location_score", 0.0) or 0.0)
    hostility_score = float(row.get("hostility_score", 0.0) or 0.0)
    macro_coherence = float(row.get("macro_coherence_score", 0.0) or 0.0)
    extension_penalty = float(row.get("extension_penalty_score", 0.0) or 0.0)
    environment_state = str(row.get("environment_state") or "")
    row_fresh, row_freshness_reason = _adaptive_row_is_fresh(row)
    entry_environment = str(getattr(position, "environment_state_at_entry", "") or "")
    unrealized_progress_score = float(clip01(unrealized_pnl_usd / max(float(getattr(position, "open_equity_usd", 0.0)) * 0.005, 1.0))) if unrealized_pnl_usd > 0.0 else 0.0
    thesis_integrity = float(clip01((0.45 * playbook_score) + (0.30 * location_score) + (0.15 * trigger_score) + (0.10 * macro_coherence)))
    environment_stability = float(clip01(1.0 - hostility_score))
    thesis_decay = float(clip01(1.0 - thesis_integrity))
    profit_extension_score = float(clip01(unrealized_progress_score * extension_penalty))
    age_decay_score = float(clip01(age_bars / 16.0))
    playbook_invalidation_score = float(clip01((0.45 * thesis_decay) + (0.30 * hostility_score) + (0.25 * (1.0 - macro_coherence))))
    environment_deterioration_score = float(clip01(max(hostility_score, 0.0 if not entry_environment or environment_state == entry_environment else 0.6)))
    trigger_failure_score = float(clip01(1.0 - trigger_score))
    opposite_playbook_score = float(playbook_score if reversal_context_active else 0.0)
    environment_flip_score = float(clip01(max(hostility_score, 0.7 if reversal_context_active and reversal_ready else 0.0)))

    hold_score = float((0.45 * hold_component) + (0.30 * thesis_integrity) + (0.15 * environment_stability) + (0.10 * max(unrealized_progress_score, 0.0)))
    reduce_score = float((0.35 * reduce_component) + (0.25 * thesis_decay) + (0.20 * profit_extension_score) + (0.20 * age_decay_score))
    exit_score = float((0.40 * exit_component) + (0.25 * playbook_invalidation_score) + (0.20 * environment_deterioration_score) + (0.15 * trigger_failure_score))
    reverse_score = float((0.35 * reversal_component) + (0.25 * opposite_playbook_score) + (0.20 * environment_flip_score) + (0.20 * trigger_failure_score))

    reduce_margin = 0.10
    exit_margin = 0.12
    reverse_margin = 0.18
    partial_cap = 2
    partial_cooldown = 6
    min_hold_bars = 6.0
    if playbook == PLAYBOOK_RANGE_MEAN_REVERSION:
        reduce_margin, exit_margin, reverse_margin = 0.06, 0.08, 0.14
        partial_cap, partial_cooldown = 1, 8
        min_hold_bars = 3.0
    elif playbook == PLAYBOOK_BREAKOUT_EXPANSION:
        reduce_margin, exit_margin, reverse_margin = 0.05, 0.06, 0.14
        partial_cap, partial_cooldown = 1, 12
        if row_fresh and age_bars <= 3.0 and trigger_score < 0.30:
            return {"action": "exit", "reason": "adaptive_breakout_follow_through_failed"}
        min_hold_bars = 2.0
    elif playbook == PLAYBOOK_FAILED_BREAKOUT_REVERSAL:
        reduce_margin, exit_margin, reverse_margin = 1e6, 0.07, 1e6
        partial_cap, partial_cooldown = 0, 9999
        if row_fresh and (trigger_score < 0.35 or playbook_score < 0.45):
            return {"action": "exit", "reason": "adaptive_failed_breakout_invalidated"}
        min_hold_bars = 2.0

    partial_count = int(getattr(position, "partial_count", 0) or 0)
    last_partial_bar_index = getattr(position, "last_partial_bar_index", None)
    cooldown_ok = last_partial_bar_index is None or (int(bar_idx) - int(last_partial_bar_index)) >= int(partial_cooldown)
    partial_allowed = partial_cap > 0 and partial_count < partial_cap and cooldown_ok and unrealized_pnl_usd > 0.0
    severe_invalidation = (
        hostility_score >= 0.80
        or playbook_invalidation_score >= 0.82
        or (trigger_score < 0.18)
        or (environment_deterioration_score >= 0.75)
    )
    if age_bars < min_hold_bars and not severe_invalidation:
        return {"action": "hold", "reason": "adaptive_hold_min_age"}

    if not row_fresh:
        if partial_allowed and reduce_score > (hold_score + reduce_margin):
            return {"action": "partial_tp", "reason": "adaptive_playbook_reduce"}
        return {"action": "hold", "reason": row_freshness_reason}

    if reversal_ready and reverse_score > (hold_score + reverse_margin):
        return {"action": "exit", "reason": "adaptive_reverse_ready"}
    if exit_score > (hold_score + exit_margin):
        return {"action": "exit", "reason": "adaptive_playbook_exit"}
    if partial_allowed and reduce_score > (hold_score + reduce_margin):
        return {"action": "partial_tp", "reason": "adaptive_playbook_reduce"}
    return {"action": "hold", "reason": "adaptive_hold"}


def summarize_playbook_mix(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {k: int(v) for k, v in Counter(str(row.get("playbook") or PLAYBOOK_NO_TRADE) for row in rows).items()}
