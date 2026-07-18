# AGENT: ROLE: Digital twin replay CLI for strict live-mirror and adaptive multi-playbook backtests with diagnostics and guardrails.
# AGENT: ENTRYPOINT: `python tools/fxstack_digital_twin_backtest.py ...`.
# AGENT: PRIMARY INPUTS: active manifest, feature parquet, settings, bridge validation endpoints, adaptive policy module.
# AGENT: PRIMARY OUTPUTS: aggregate metrics, decision history, validation artifacts, parity comparisons, recommendations.
# AGENT: DEPENDS ON: `fxstack/backtest/adaptive_policy.py`, `fxstack/backtest/twin_types.py`, `fxstack/runtime/runner.py`, `fxstack/settings.py`.
# AGENT: CALLED BY: operators and research scripts.
# AGENT: STATE / SIDE EFFECTS: reads features and optional bridge snapshots; writes artifact directories only.
# AGENT: HANDSHAKES: `/v2/decision-snapshots` validation reads, shared runtime helper imports, adaptive baseline comparison artifacts.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `fxstack/backtest/adaptive_policy.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

import argparse
import copy
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.belief import build_cross_pair_influence_records  # noqa: E402
from fxstack.belief.engine import compute_directional_belief, empty_directional_belief  # noqa: E402
from fxstack.backtest.twin_types import (  # noqa: E402
    TwinAggregateMetrics,
    TwinClosedTrade,
    TwinDecisionRecord,
    TwinOpenPosition,
    TwinRecommendation,
    TwinValidationResult,
)
from fxstack.backtest.adaptive_policy import (  # noqa: E402
    ADAPTIVE_EXEC_MODE,
    PLAYBOOK_BREAKOUT_EXPANSION,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
    PLAYBOOK_NO_TRADE,
    PLAYBOOK_RANGE_MEAN_REVERSION,
    PLAYBOOK_TREND_PULLBACK,
    STRICT_EXEC_MODE,
    adaptive_replacement_keep_score,
    adaptive_reentry_block,
    adaptive_tempo_gap_active,
    adaptive_lifecycle_decision,
    attach_adaptive_context,
    evaluate_adaptive_entry,
    parse_enabled_playbooks,
    summarize_playbook_mix,
)
from fxstack.features.fx_lifecycle import timeframe_to_timedelta  # noqa: E402
from fxstack.live.policy import POLICY_VERSION, EDGE_FORMULA_ID  # noqa: E402
from fxstack.runtime.runner import (  # noqa: E402
    _apply_shadow_entry_ranking,
    _evaluate_adaptive_entry_with_quality_override,
    _reversal_blocking_reasons,
    _shadow_pair_tier,
)
from fxstack.settings import get_settings  # noqa: E402
from fxstack.strategy.allocator import (  # noqa: E402
    allocate_candidates,
    allocator_config_from_settings,
    build_allocator_candidate,
    playbook_to_sleeve,
)
from fxstack.strategy.allocator_types import AllocatorOpenPosition  # noqa: E402
from fxstack.strategy.campaign import (  # noqa: E402
    CAMPAIGN_STATE_ABANDONED,
    CAMPAIGN_STATE_HARVEST,
    CAMPAIGN_STATE_INACTIVE,
    CAMPAIGN_STATE_PROBE,
    apply_campaign_lifecycle_overrides,
    apply_campaign_registry_snapshot,
    build_thesis_id,
    campaign_config_from_settings,
    campaign_cooldown_scale,
    campaign_state_after_close,
    campaign_transition_if_changed,
    evaluate_entry_campaign_memory,
    evaluate_open_campaign,
    serialize_campaign_entry,
    start_campaign_on_entry,
)
from fxstack.strategy.campaign_types import CampaignRegistryEntry  # noqa: E402
from fxstack.strategy.desk_overlay import build_desk_overlay  # noqa: E402
from fxstack.strategy.desk_overlay_types import DeskOverlayInputs  # noqa: E402
from fxstack.strategy.sleeve_governance import (  # noqa: E402
    SleeveGovernanceTracker,
    serialize_sleeve_snapshots,
)


TWIN_VERSION = "fxstack_digital_twin_v1"
DECISION_HISTORY_FILE = "decision_history.csv.gz"
ALLOCATOR_DECISION_HISTORY_FILE = "allocator_decisions.csv.gz"


def _load_base_module() -> Any:
    base_path = REPO_ROOT / "tools" / "fxstack_lifecycle_equity_backtest.py"
    spec = importlib.util.spec_from_file_location("fxstack_lifecycle_equity_backtest", base_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load base replay module: {base_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


BASE = _load_base_module()
LOT_UNITS = float(BASE.LOT_UNITS)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if math.isnan(out) or math.isinf(out):
        return float(default)
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _to_utc_ts(value: Any) -> pd.Timestamp:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"invalid timestamp: {value}")
    return pd.Timestamp(ts)


def _series_or_default(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").fillna(default).astype(float)
    return pd.Series(float(default), index=df.index, dtype=float)


def _string_series_or_default(df: pd.DataFrame, col: str, default: str = "") -> pd.Series:
    if col in df.columns:
        return df[col].fillna(default).astype(str)
    return pd.Series(str(default), index=df.index, dtype="object")


def _clamp01_array(values: np.ndarray | pd.Series) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return np.clip(arr, 0.0, 1.0)


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _finite_series(df: pd.DataFrame, col: str) -> pd.Series | None:
    if col not in df.columns:
        return None
    series = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    if not series.notna().any():
        return None
    return series.astype(float)


def _directional_value_array(values: np.ndarray, side_sign: np.ndarray) -> np.ndarray:
    return np.asarray(values, dtype=float) * np.asarray(side_sign, dtype=float)


def _directional_component_score_array(values: np.ndarray, *, side_sign: np.ndarray, scale: float | np.ndarray) -> np.ndarray:
    denom = np.maximum(1e-9, np.asarray(scale, dtype=float))
    scaled = _directional_value_array(values, side_sign) / denom
    return _clamp01_array(0.5 + (0.5 * np.clip(scaled, -1.0, 1.0)))


def _triangular_score_array(values: np.ndarray, *, target: float, width: float) -> np.ndarray:
    if width <= 0.0:
        return np.zeros_like(np.asarray(values, dtype=float))
    return _clamp01_array(1.0 - (np.abs(np.asarray(values, dtype=float) - float(target)) / float(width)))


def _htf_alignment_score_series(df: pd.DataFrame, *, side_sign: np.ndarray) -> pd.Series:
    component_arrays: list[np.ndarray] = []
    for key, scale in (
        ("h1_trend_slope_20", 0.0015),
        ("h4_trend_slope_20", 0.0025),
        ("d_trend_slope_20", 0.0035),
        ("h1_trend_strength_20", 1.25),
        ("h4_trend_strength_20", 1.50),
        ("d_trend_strength_20", 1.75),
    ):
        series = _finite_series(df, key)
        if series is None:
            continue
        valid_mask = series.notna().to_numpy(dtype=bool)
        scores = _directional_component_score_array(
            series.fillna(0.0).to_numpy(dtype=float),
            side_sign=side_sign,
            scale=scale,
        )
        component_arrays.append(np.where(valid_mask, scores, np.nan))

    fallback = np.column_stack(
        [
            _directional_component_score_array(
                _series_or_default(df, "trend_slope_60", 0.0).to_numpy(dtype=float),
                side_sign=side_sign,
                scale=0.0020,
            ),
            _directional_component_score_array(
                _series_or_default(df, "trend_strength_60", 0.0).to_numpy(dtype=float),
                side_sign=side_sign,
                scale=1.50,
            ),
        ]
    )
    fallback_values = np.mean(fallback, axis=1)
    if not component_arrays:
        return pd.Series(fallback_values, index=df.index, dtype=float)

    stacked = np.column_stack(component_arrays)
    valid_counts = np.sum(np.isfinite(stacked), axis=1)
    sums = np.nansum(stacked, axis=1)
    values = np.divide(
        sums,
        valid_counts,
        out=np.zeros(len(df), dtype=float),
        where=valid_counts > 0,
    )
    values = np.where(valid_counts > 0, values, fallback_values)
    return pd.Series(values, index=df.index, dtype=float)


def _session_bucket_series(ts_series: pd.Series) -> pd.Series:
    hours = pd.to_datetime(ts_series, utc=True, errors="coerce").dt.hour.fillna(-1).astype(int)
    values = np.select(
        [
            (hours >= 0) & (hours < 7),
            (hours >= 7) & (hours < 12),
            (hours >= 12) & (hours < 16),
            (hours >= 16) & (hours < 21),
        ],
        ["asia", "london_open", "london_ny_overlap", "new_york"],
        default="pacific",
    )
    return pd.Series(values, index=ts_series.index, dtype="object")


def _threshold_snapshot(settings: Any) -> dict[str, float]:
    return {
        "max_spread_bps": float(getattr(settings, "max_allowed_spread_bps", 0.0)),
        "min_expected_edge_bps": float(getattr(settings, "min_expected_edge_bps", 0.0)),
        "min_swing_prob": float(getattr(settings, "min_swing_prob", 0.0)),
        "min_entry_prob": float(getattr(settings, "min_entry_prob", 0.0)),
        "min_trade_prob": float(getattr(settings, "min_trade_prob", 0.0)),
        "max_entry_uncertainty": float(getattr(settings, "max_entry_uncertainty", 0.0)),
    }


def _regime_bucket_series(regime_prob: pd.Series) -> pd.Series:
    arr = np.asarray(regime_prob, dtype=float)
    values = np.select(
        [arr >= 0.75, arr >= 0.60, arr >= 0.45],
        ["regime_high_conf", "regime_trending", "regime_neutral"],
        default="regime_low_conf",
    )
    return pd.Series(values, index=regime_prob.index, dtype="object")


def _bucket_label(value: float, edges: list[float], labels: list[str]) -> str:
    v = float(value)
    for idx, edge in enumerate(edges):
        if v < float(edge):
            return labels[idx]
    return labels[-1]


def _edge_bucket(value: float) -> str:
    return _bucket_label(value, [0.0, 3.0, 6.0, 10.0, 20.0], ["lt0", "0_3", "3_6", "6_10", "10_20", "20_plus"])


def _uncertainty_bucket(value: float) -> str:
    return _bucket_label(value, [0.10, 0.20, 0.30, 0.40, 0.50, 0.75], ["0_0.10", "0.10_0.20", "0.20_0.30", "0.30_0.40", "0.40_0.50", "0.50_0.75", "0.75_plus"])


def _structure_bucket(value: float) -> str:
    return _bucket_label(value, [0.40, 0.55, 0.70, 0.85], ["lt0.40", "0.40_0.55", "0.55_0.70", "0.70_0.85", "0.85_plus"])


def _belief_signal_proxy(*, pair: str, ts: str, side: str, signal_row: dict[str, np.ndarray], bar_idx: int) -> Any:
    side_norm = "short" if str(side).upper() == "SELL" else "long"
    return SimpleNamespace(
        pair=str(pair),
        ts=str(ts),
        side=str(side_norm),
        regime_prob=float(signal_row["regime_prob"][bar_idx]),
        swing_prob=float(signal_row["swing_prob"][bar_idx]),
        entry_prob=float(signal_row["entry_prob"][bar_idx]),
        trade_prob=float(signal_row["trade_prob"][bar_idx]),
        uncertainty_score=float(signal_row["uncertainty_score"][bar_idx]),
        model_disagreement_score=float(signal_row["model_disagreement_score"][bar_idx]),
        directional_swing_confidence=float(signal_row["directional_swing_confidence"][bar_idx]),
        htf_alignment_score=float(signal_row["htf_alignment_score"][bar_idx]),
        pullback_quality_score=float(signal_row["pullback_quality_score"][bar_idx]),
        resume_trigger_score=float(signal_row["resume_trigger_score"][bar_idx]),
        extension_penalty_score=float(signal_row["extension_penalty_score"][bar_idx]),
        structure_timing_score=float(signal_row["structure_timing_score"][bar_idx]),
        expected_edge_bps=float(signal_row["expected_edge_bps"][bar_idx]),
        spread_bps=float(signal_row["spread_bps"][bar_idx]),
        scenario_bucket=str(signal_row["scenario_bucket"][bar_idx]) if "scenario_bucket" in signal_row else "",
        context_frame_profile="hierarchical_v1",
    )


def _belief_overlay_adjustment(
    *,
    belief_gap: float,
    ev_above_hurdle_prob: float,
    fail_fast_prob: float,
    no_edge: bool,
) -> float:
    adjustment = 0.0
    if bool(no_edge):
        adjustment -= 0.10
    if float(ev_above_hurdle_prob) >= 0.65:
        adjustment += 0.08
    if float(belief_gap) >= 0.12:
        adjustment += 0.05
    if float(fail_fast_prob) >= 0.55:
        adjustment -= 0.06
    return float(adjustment)


def _apply_cross_pair_admission_overlay(
    *,
    pending_actions: list[dict[str, Any]],
    collector_rows_for_bar: list[dict[str, Any]],
    shadow_inputs_for_bar: list[dict[str, Any]],
    open_positions: dict[str, Any],
    exit_registry: dict[str, dict[str, Any]],
    campaign_registry: dict[str, CampaignRegistryEntry],
    campaign_config: Any,
    bar_idx: int,
    settings: Any,
    fallback_margin: float,
) -> dict[str, Any]:
    influence_mode = str(getattr(settings, "belief_influence_mode", "off") or "off").strip().lower()
    records = build_cross_pair_influence_records(
        [
            {
                **dict(action or {}),
                "pair": str(action.get("pair") or "").upper(),
                "ts": str(action.get("ts") or ""),
            }
            for action in pending_actions
            if str(action.get("pair") or "").strip()
        ]
    )
    record_by_pair = {str(record.pair).upper(): record for record in records if str(record.pair).strip()}
    gated_count = 0
    for idx_action, action in enumerate(pending_actions):
        pair = str(action.get("pair") or "").upper()
        record = record_by_pair.get(pair)
        if record is None:
            continue
        telemetry_only = str(getattr(record, "source_mode", "") or "").strip().lower() == "telemetry_only"
        meta_updates = {
            "cross_pair_rank_position": int(record.rank_position),
            "cross_pair_influence_score": float(record.influence_score),
            "cross_pair_recommendation_strength": float(record.recommendation_strength),
            "cross_pair_influenced_by_pairs": list(record.influenced_by_pairs),
            "cross_pair_reason_codes": list(record.cross_pair_reason_codes),
            "cross_pair_source_mode": str(record.source_mode),
            "cross_pair_influence_mode": str(influence_mode or "off"),
            "cross_pair_influence_adjustment": float(0.0 if telemetry_only else (float(record.recommendation_strength) - 0.5) * 0.16),
            "cross_pair_soft_block": bool(
                (not telemetry_only)
                and influence_mode in {"soft_gate", "hard_gate"}
                and float(record.recommendation_strength) < 0.30
            ),
            "cross_pair_hard_block": bool(
                (not telemetry_only)
                and influence_mode == "hard_gate"
                and float(record.recommendation_strength) < 0.20
            ),
        }
        action.update(meta_updates)
        collector_rows_for_bar[idx_action].update(meta_updates)
        shadow_inputs_for_bar[idx_action]["metadata"].update(meta_updates)

        if action.get("pos_snapshot") is not None:
            continue

        current_row = dict(action.get("adaptive_eval_row") or {})
        if not current_row:
            continue
        cross_pair_adjustment = float(meta_updates["cross_pair_influence_adjustment"])
        adjusted_quality = float(_clip01(float(_safe_float(action.get("adaptive_entry_quality", 0.0), 0.0)) + cross_pair_adjustment))
        if bool(meta_updates["cross_pair_soft_block"]):
            adjusted_quality = float(_clip01(float(adjusted_quality) * 0.85))
        if cross_pair_adjustment or bool(meta_updates["cross_pair_soft_block"]):
            adaptive_eval = _evaluate_adaptive_entry_with_quality_override(
                row=current_row,
                strict_ready=bool(action.get("baseline_allowed", False)),
                open_positions=open_positions,
                settings=settings,
                fallback_margin=float(fallback_margin),
                quality_override=float(adjusted_quality),
            )
        else:
            adaptive_eval = dict(action.get("adaptive_eval") or {})
            adaptive_eval["adaptive_entry_quality"] = float(adjusted_quality)
        adaptive_eval["cross_pair_rank_position"] = int(meta_updates["cross_pair_rank_position"])
        adaptive_eval["cross_pair_influence_score"] = float(meta_updates["cross_pair_influence_score"])
        adaptive_eval["cross_pair_recommendation_strength"] = float(meta_updates["cross_pair_recommendation_strength"])
        adaptive_eval["cross_pair_reason_codes"] = list(meta_updates["cross_pair_reason_codes"])
        if bool(meta_updates["cross_pair_hard_block"]):
            adaptive_eval["adaptive_allowed"] = False
            adaptive_eval["adaptive_rejection_reason"] = "cross_pair_hard_gate"
            gated_count += 1

        action["adaptive_eval"] = dict(adaptive_eval)
        action.update(dict(adaptive_eval))
        action["adaptive_entry_quality"] = float(adaptive_eval.get("adaptive_entry_quality", adjusted_quality))
        action["adaptive_allowed"] = bool(adaptive_eval.get("adaptive_allowed", action.get("adaptive_allowed", False)))
        action["adaptive_rejection_reason"] = str(adaptive_eval.get("adaptive_rejection_reason", action.get("adaptive_rejection_reason", "")))
        if str(adaptive_eval.get("playbook") or "").strip():
            action["entry_playbook"] = str(adaptive_eval.get("playbook") or "")
            action["sleeve"] = playbook_to_sleeve(str(action["entry_playbook"]))
        if bool(action.get("adaptive_allowed", False)):
            campaign_candidate = evaluate_entry_campaign_memory(
                pair=pair,
                side="long" if str(action.get("side") or "").upper() == "BUY" else "short",
                sleeve=str(action.get("sleeve") or playbook_to_sleeve(str(action.get("entry_playbook") or ""))),
                row={
                    "playbook_score": float(action.get("playbook_score", 0.0)),
                    "location_score": float(action.get("location_score", 0.0)),
                    "trigger_score": float(action.get("trigger_score", 0.0)),
                    "macro_coherence_score": float(action.get("entry_macro_coherence_score", action.get("macro_coherence_score", 0.0))),
                    "hostility_score": float(action.get("hostility_score", 0.0)),
                    "extension_penalty_score": float(action.get("extension_penalty_score", 0.0)),
                    "environment_state": str(action.get("environment_state") or ""),
                    "trade_prob": float(action.get("trade_prob", 0.0)),
                },
                bar_idx=int(bar_idx),
                ts=str(action.get("ts") or ""),
                registry=campaign_registry,
                config=campaign_config,
            )
            action["thesis_id"] = str(campaign_candidate.thesis_id)
            action["campaign_seq"] = int(campaign_candidate.campaign_seq)
            action["campaign_entry_kind"] = str(campaign_candidate.entry_kind)
            action["campaign_state"] = str(campaign_candidate.state)
            action["campaign_state_reason"] = str(campaign_candidate.state_reason)
            action["campaign_proof_score"] = float(campaign_candidate.proof_score)
            action["campaign_maturity_score"] = float(campaign_candidate.maturity_score)
            action["campaign_reset_quality"] = float(campaign_candidate.reset_quality)
            action["campaign_priority_boost"] = float(campaign_candidate.priority_boost)
            action["campaign_reentry_blocked"] = bool(campaign_candidate.reentry_blocked)
            reentry_eval = adaptive_reentry_block(
                pair=pair,
                side="long" if str(action.get("side") or "").upper() == "BUY" else "short",
                playbook=str(action.get("entry_playbook") or action.get("playbook") or PLAYBOOK_NO_TRADE),
                bar_idx=int(bar_idx),
                exit_registry=exit_registry,
                cooldown_scale=campaign_cooldown_scale(campaign_candidate.state, campaign_config),
            )
            if bool(reentry_eval.get("blocked")):
                action["adaptive_allowed"] = False
                action["adaptive_rejection_reason"] = str(reentry_eval.get("reason") or "adaptive_reentry_cooldown")
                action["adaptive_eval"]["adaptive_allowed"] = False
                action["adaptive_eval"]["adaptive_rejection_reason"] = str(action["adaptive_rejection_reason"])
            if bool(campaign_candidate.reentry_blocked):
                action["adaptive_allowed"] = False
                action["adaptive_rejection_reason"] = str(campaign_candidate.reentry_block_reason or "campaign_abandon_cooldown")
                action["adaptive_eval"]["adaptive_allowed"] = False
                action["adaptive_eval"]["adaptive_rejection_reason"] = str(action["adaptive_rejection_reason"])

        hard_reasons = [str(reason) for reason in list(action.get("entry_hard_reasons") or []) if str(reason)]
        ready = bool(action.get("adaptive_allowed", False)) and not hard_reasons
        rejection_reasons = hard_reasons if hard_reasons else ([] if ready else [str(action.get("adaptive_rejection_reason") or "adaptive_rejected")])
        rejection_reason = "none" if ready else (rejection_reasons[0] if rejection_reasons else "adaptive_rejected")
        action["ready"] = bool(ready)
        action["decision_reasons"] = list(rejection_reasons)

        collector_rows_for_bar[idx_action]["allowed"] = bool(ready)
        collector_rows_for_bar[idx_action]["rejection_reason"] = str(rejection_reason)
        collector_rows_for_bar[idx_action]["rejection_reasons"] = list(rejection_reasons)
        collector_rows_for_bar[idx_action]["adaptive_allowed"] = bool(action.get("adaptive_allowed", False))
        collector_rows_for_bar[idx_action]["adaptive_rejection_reason"] = str(action.get("adaptive_rejection_reason") or "")
        collector_rows_for_bar[idx_action]["adaptive_entry_quality"] = float(action.get("adaptive_entry_quality", 0.0))
        collector_rows_for_bar[idx_action]["lifecycle_action"] = "entry" if ready else "hold"
        collector_rows_for_bar[idx_action]["lifecycle_reason"] = "entry_approved" if ready else str(rejection_reason)
        collector_rows_for_bar[idx_action]["playbook"] = str(action.get("playbook") or action.get("entry_playbook") or "")
        collector_rows_for_bar[idx_action]["sleeve"] = str(action.get("sleeve") or "")
        collector_rows_for_bar[idx_action]["aggressive_fallback_used"] = bool(action.get("aggressive_fallback_used", False))
        collector_rows_for_bar[idx_action]["thesis_id"] = str(action.get("thesis_id") or "")
        collector_rows_for_bar[idx_action]["campaign_seq"] = int(_safe_int(action.get("campaign_seq", 0), 0))
        collector_rows_for_bar[idx_action]["campaign_entry_kind"] = str(action.get("campaign_entry_kind") or "")
        collector_rows_for_bar[idx_action]["campaign_state"] = str(action.get("campaign_state") or CAMPAIGN_STATE_INACTIVE)
        collector_rows_for_bar[idx_action]["campaign_state_reason"] = str(action.get("campaign_state_reason") or "")
        collector_rows_for_bar[idx_action]["campaign_proof_score"] = float(action.get("campaign_proof_score", 0.0))
        collector_rows_for_bar[idx_action]["campaign_maturity_score"] = float(action.get("campaign_maturity_score", 0.0))
        collector_rows_for_bar[idx_action]["campaign_reset_quality"] = float(action.get("campaign_reset_quality", 0.0))
        collector_rows_for_bar[idx_action]["campaign_priority_boost"] = float(action.get("campaign_priority_boost", 0.0))
        collector_rows_for_bar[idx_action]["campaign_reentry_blocked"] = bool(action.get("campaign_reentry_blocked", False))

        shadow_inputs_for_bar[idx_action]["execution_ready"] = bool(ready)
        shadow_inputs_for_bar[idx_action]["reasons"] = list(rejection_reasons)
        shadow_inputs_for_bar[idx_action]["metadata"]["entry_blocking_reasons"] = list(rejection_reasons)
        shadow_inputs_for_bar[idx_action]["metadata"]["adaptive_allowed"] = bool(action.get("adaptive_allowed", False))
        shadow_inputs_for_bar[idx_action]["metadata"]["adaptive_rejection_reason"] = str(action.get("adaptive_rejection_reason") or "")
        shadow_inputs_for_bar[idx_action]["metadata"]["adaptive_entry_quality"] = float(action.get("adaptive_entry_quality", 0.0))
        shadow_inputs_for_bar[idx_action]["metadata"]["playbook"] = str(action.get("playbook") or action.get("entry_playbook") or "")
        shadow_inputs_for_bar[idx_action]["metadata"]["sleeve"] = str(action.get("sleeve") or "")
        shadow_inputs_for_bar[idx_action]["metadata"]["aggressive_fallback_used"] = bool(action.get("aggressive_fallback_used", False))
        shadow_inputs_for_bar[idx_action]["metadata"]["thesis_id"] = str(action.get("thesis_id") or "")
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_seq"] = int(_safe_int(action.get("campaign_seq", 0), 0))
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_entry_kind"] = str(action.get("campaign_entry_kind") or "")
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_state"] = str(action.get("campaign_state") or CAMPAIGN_STATE_INACTIVE)
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_state_reason"] = str(action.get("campaign_state_reason") or "")
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_proof_score"] = float(action.get("campaign_proof_score", 0.0))
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_maturity_score"] = float(action.get("campaign_maturity_score", 0.0))
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_reset_quality"] = float(action.get("campaign_reset_quality", 0.0))
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_priority_boost"] = float(action.get("campaign_priority_boost", 0.0))
        shadow_inputs_for_bar[idx_action]["metadata"]["campaign_reentry_blocked"] = bool(action.get("campaign_reentry_blocked", False))

    return {
        "cross_pair_influence_mode": str(influence_mode or "off"),
        "cross_pair_gated_count": int(gated_count),
        "cross_pair_ranked_pairs": [str(item.pair) for item in records[:5]],
    }


def _shared_overlay_diagnostics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    overlay_rows = [
        row
        for row in rows
        if str(row.get("belief_source_mode") or "") not in {"", "disabled", "artifact_missing"}
    ]
    overlay_adjustments = [float(_safe_float(row.get("belief_overlay_adjustment"), 0.0)) for row in overlay_rows]
    source_modes = Counter(str(row.get("belief_source_mode") or "") for row in overlay_rows)
    scenario_counts = Counter(str(row.get("belief_primary_scenario") or "") for row in overlay_rows if str(row.get("belief_primary_scenario") or ""))
    return {
        "overlay_enabled_rows": int(len(overlay_rows)),
        "overlay_adjustment": {
            "avg_adjustment": float(sum(overlay_adjustments) / max(1, len(overlay_adjustments))) if overlay_adjustments else 0.0,
            "positive_count": int(sum(1 for value in overlay_adjustments if value > 0.0)),
            "negative_count": int(sum(1 for value in overlay_adjustments if value < 0.0)),
        },
        "overlay_source_modes": {k: int(v) for k, v in sorted(source_modes.items(), key=lambda item: (-item[1], item[0]))},
        "overlay_primary_scenario_counts": {k: int(v) for k, v in sorted(scenario_counts.items(), key=lambda item: (-item[1], item[0]))},
    }


def _csv_fieldnames(rows: Sequence[dict[str, Any]]) -> list[str]:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in dict(row or {}).keys():
            name = str(key)
            if name and name not in seen:
                seen.add(name)
                fieldnames.append(name)
    return fieldnames


def _desk_overlay_inputs_for_action(
    *,
    action: dict[str, Any],
    sleeve_snapshot: Any,
    open_position_count: int,
    allocator_open_positions: list[AllocatorOpenPosition],
    settings: Any,
) -> DeskOverlayInputs:
    pair_slots = max(1.0, float(max(1, int(getattr(settings, "max_pair_positions", 1) or 1))))
    total_slots = max(1.0, float(max(1, int(getattr(settings, "max_total_positions", 1) or 1))))
    replacement_pressure = 0.0
    if allocator_open_positions:
        replacement_pressure = _clip01(
            sum(max(0.0, 1.0 - float(item.keep_score)) for item in allocator_open_positions)
            / max(1, len(allocator_open_positions))
        )
    sleeve_name = str(action.get("sleeve") or playbook_to_sleeve(action.get("entry_playbook") or ""))
    secondary_sleeve = str(action.get("belief_opposing_scenario") or "").strip()
    secondary_sleeve = playbook_to_sleeve(secondary_sleeve) if secondary_sleeve and secondary_sleeve != "no_edge" else ""
    return DeskOverlayInputs(
        belief_metrics={
            "directional_belief": _clip01(action.get("belief_primary_rank_score", action.get("belief_primary_score", 0.0))),
            "belief_gap": _clip01(action.get("belief_gap", 0.0)),
            "confidence": _clip01(action.get("belief_primary_ev_above_hurdle_prob", action.get("trade_prob", 0.0))),
            "confirm_prob": _clip01(action.get("belief_primary_confirm_prob", action.get("trade_prob", 0.0))),
            "model_agreement": _clip01(1.0 - _safe_float(action.get("belief_fragility_score", action.get("model_disagreement_score", 0.0)), 0.0)),
            "signal_quality": _clip01(action.get("entry_structure_timing_score", action.get("adaptive_entry_quality", 0.0))),
            "fail_fast_risk": _clip01(action.get("belief_primary_fail_fast_prob", 0.0)),
            "expected_net_ev_bps": float(
                _safe_float(
                    action.get("belief_primary_expected_net_ev_bps", action.get("expected_edge_bps", action.get("calibrated_ev_bps_shadow", 0.0))),
                    0.0,
                )
            ),
        },
        adaptive_playbook_metrics={
            "sleeve": sleeve_name,
            "adaptive_entry_quality": _clip01(action.get("adaptive_entry_quality", 0.0)),
            "playbook_score": _clip01(action.get("playbook_score", 0.0)),
            "location_score": _clip01(action.get("location_score", 0.0)),
            "trigger_score": _clip01(action.get("trigger_score", 0.0)),
            "hostility_score": _clip01(action.get("hostility_score", 0.0)),
        },
        campaign_state={
            "state": str(action.get("campaign_state") or ""),
            "proof_score": _clip01(action.get("campaign_proof_score", 0.0)),
            "maturity_score": _clip01(action.get("campaign_maturity_score", 0.0)),
            "reset_quality": _clip01(action.get("campaign_reset_quality", 0.0)),
            "priority_boost": _clip01(action.get("campaign_priority_boost", 0.0)),
        },
        sleeve_health={
            "sleeve": sleeve_name,
            "score": _clip01(getattr(sleeve_snapshot, "score", action.get("sleeve_health_score", 0.5))),
            "state": str(getattr(sleeve_snapshot, "state", action.get("sleeve_health_state", "healthy"))),
        },
        crowding={
            "currency_crowding": _clip01(action.get("currency_crowding_penalty", 0.0)),
            "pair_crowding": _clip01(float(_safe_int(action.get("position_count_pair", 0), 0)) / pair_slots),
            "portfolio_concentration": _clip01(float(open_position_count) / total_slots),
        },
        recent_performance={
            "win_rate": _clip01(getattr(sleeve_snapshot, "win_rate", 0.5)),
            "expectancy_usd": float(getattr(sleeve_snapshot, "expectancy_usd", 0.0)),
            "profit_factor": float(getattr(sleeve_snapshot, "profit_factor", 1.0)),
            "recent_pnl_trend": _clip01((float(getattr(sleeve_snapshot, "expectancy_usd", 0.0)) + 25.0) / 50.0),
        },
        portfolio={
            "replacement_pressure": float(replacement_pressure),
            "secondary_sleeve": secondary_sleeve,
        },
    )


def _overlay_budget_targets(
    *,
    overlays: dict[int, Any],
    remaining_slots: int,
    candidate_counts: dict[str, int],
) -> dict[str, int]:
    slots = max(0, int(remaining_slots))
    if slots <= 0:
        return {}
    weights: dict[str, float] = {}
    for overlay in overlays.values():
        for sleeve_key, guidance in dict(getattr(overlay, "sleeve_budget_guidance", {}) or {}).items():
            weights[str(sleeve_key)] = float(weights.get(str(sleeve_key), 0.0)) + float(getattr(guidance, "target_share", 0.0))
    weights = {k: float(v) for k, v in weights.items() if float(v) > 0.0 and int(candidate_counts.get(k, 0)) > 0}
    if not weights:
        return {}
    total_weight = float(sum(weights.values())) or 1.0
    raw_targets = {k: float(slots) * float(v) / total_weight for k, v in weights.items()}
    targets = {k: min(int(candidate_counts.get(k, 0)), int(raw_targets[k])) for k in raw_targets}
    used_slots = int(sum(targets.values()))
    if used_slots < slots:
        fractional = sorted(
            [
                (raw_targets[k] - float(targets[k]), k)
                for k in raw_targets
                if int(targets[k]) < int(candidate_counts.get(k, 0))
            ],
            reverse=True,
        )
        for _frac, sleeve_key in fractional:
            if used_slots >= slots:
                break
            targets[sleeve_key] = int(targets.get(sleeve_key, 0)) + 1
            used_slots += 1
    return {k: int(v) for k, v in sorted(targets.items()) if int(v) > 0}


def _metric_deciles(
    *,
    decisions_df: pd.DataFrame,
    value_col: str,
    trade_pnl_by_key: dict[tuple[str, str], float],
) -> list[dict[str, Any]]:
    if decisions_df.empty or value_col not in decisions_df.columns:
        return []
    df = decisions_df.copy()
    df["value"] = pd.to_numeric(df[value_col], errors="coerce")
    df = df[df["value"].notna()].copy()
    if df.empty:
        return []
    if len(df) >= 10:
        df["decile"] = pd.qcut(df["value"], q=10, labels=False, duplicates="drop") + 1
    else:
        df["decile"] = pd.Series(np.arange(len(df)) % max(1, min(10, len(df))), index=df.index) + 1
    rows: list[dict[str, Any]] = []
    for decile, grp in df.groupby("decile"):
        pnl_values = [
            float(trade_pnl_by_key.get((str(row.pair), str(row.ts)), 0.0))
            for row in grp.itertuples(index=False)
        ]
        rows.append(
            {
                "decile": int(decile),
                "count": int(len(grp)),
                "avg_value": float(grp["value"].mean()),
                "expectancy_usd": float(sum(pnl_values) / max(1, len(pnl_values))),
                "net_pnl_usd": float(sum(pnl_values)),
            }
        )
    return sorted(rows, key=lambda item: int(item["decile"]))


def _mean_or_zero(series: pd.Series) -> float:
    if series.empty:
        return 0.0
    value = series.mean()
    return 0.0 if pd.isna(value) else float(value)


def _experiment_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "belief_overlay": bool(getattr(args, "belief_overlay", True)),
        "shadow_tier1_structure_rescue_margin": (
            None if getattr(args, "shadow_tier1_structure_rescue_margin", None) is None else float(args.shadow_tier1_structure_rescue_margin)
        ),
        "shadow_pair_aware_spread_caps": bool(getattr(args, "shadow_pair_aware_spread_caps", False)),
        "shadow_spread_cap_quantile": float(getattr(args, "shadow_spread_cap_quantile", 0.75)),
        "shadow_spread_cap_multiplier": float(getattr(args, "shadow_spread_cap_multiplier", 1.25)),
        "shadow_spread_cap_max_bps": float(getattr(args, "shadow_spread_cap_max_bps", 5.0)),
    }


class ReservoirSampler:
    def __init__(self, max_rows: int, seed: int = 0) -> None:
        self.max_rows = max(0, int(max_rows))
        self.rows: list[dict[str, Any]] = []
        self.seen = 0
        self.rand = random.Random(seed)

    def offer(self, row: dict[str, Any]) -> None:
        if self.max_rows <= 0:
            return
        self.seen += 1
        if len(self.rows) < self.max_rows:
            self.rows.append(dict(row))
            return
        slot = self.rand.randrange(self.seen)
        if slot < self.max_rows:
            self.rows[slot] = dict(row)


class DecisionMetricsCollector:
    def __init__(self, *, max_history_rows: int, emit_history: bool) -> None:
        self.emit_history = bool(emit_history)
        self.history = ReservoirSampler(max_rows=max_history_rows, seed=42)
        self.validation_records: dict[tuple[str, str], dict[str, Any]] = {}
        self.total = 0
        self.allowed = 0
        self.shadow_candidates = 0
        self.shadow_would_trade = 0
        self.structure_rescues = 0
        self.by_pair: dict[str, dict[str, Any]] = defaultdict(lambda: {"decisions": 0, "allowed": 0, "reasons": Counter(), "shadow_reasons": Counter()})
        self.by_session: dict[str, dict[str, Any]] = defaultdict(lambda: {"decisions": 0, "allowed": 0, "reasons": Counter(), "pairs": Counter()})
        self.by_environment: dict[str, dict[str, Any]] = defaultdict(lambda: {"decisions": 0, "allowed": 0, "reasons": Counter()})
        self.by_playbook: dict[str, dict[str, Any]] = defaultdict(lambda: {"decisions": 0, "allowed": 0, "reasons": Counter(), "pairs": Counter(), "aggressive_fallbacks": 0})
        self.by_sleeve: dict[str, dict[str, Any]] = defaultdict(lambda: {"decisions": 0, "allowed": 0, "reasons": Counter(), "pairs": Counter()})
        self.by_campaign_state: dict[str, dict[str, Any]] = defaultdict(lambda: {"decisions": 0, "allowed": 0, "reasons": Counter(), "pairs": Counter()})
        self.primary_rejections: Counter[str] = Counter()
        self.shadow_rejections: Counter[str] = Counter()
        self.uncertainty_buckets: Counter[str] = Counter()
        self.structure_buckets: Counter[str] = Counter()
        self.pair_tier_breakdown: dict[str, Counter[str]] = defaultdict(Counter)
        self.spread_rejects_by_pair_session: dict[str, Counter[str]] = defaultdict(Counter)
        self.lifecycle_action_counts: Counter[str] = Counter()
        self.lifecycle_reason_counts: Counter[str] = Counter()
        self.shadow_divergence_counts: Counter[str] = Counter()
        self.structure_near_miss_rows: list[dict[str, Any]] = []
        self.live_validation_keys: set[tuple[str, str]] = set()
        self.aggressive_fallback_count = 0
        self.crowding_penalty_sum = 0.0
        self.diversification_penalty_sum = 0.0
        self.crowding_penalty_nonzero = 0
        self.diversification_penalty_nonzero = 0
        self.allocator_candidates = 0
        self.allocator_selected = 0
        self.allocator_ranked_out = 0
        self.allocator_replacements = 0

    def set_validation_keys(self, keys: set[tuple[str, str]]) -> None:
        self.live_validation_keys = set(keys)

    def consume(self, row: dict[str, Any]) -> None:
        pair = str(row.get("pair") or "")
        session = str(row.get("session_bucket") or "")
        allowed = bool(row.get("allowed", False))
        portfolio_rank_shadow = _safe_int(row.get("portfolio_rank_shadow"), 0)
        shadow_would_trade = bool(row.get("shadow_would_trade", False))
        structure_rescue_active = bool(row.get("structure_rescue_active", False))
        rejection_reason = str(row.get("rejection_reason") or "none")
        rejection_reasons = list(row.get("rejection_reasons", []) or [])
        shadow_rejection_reason = str(row.get("shadow_rejection_reason") or "none")
        pair_tier = str(row.get("pair_tier") or "")
        lifecycle_action = str(row.get("lifecycle_action") or "hold")
        lifecycle_reason = str(row.get("lifecycle_reason") or "hold")
        uncertainty_score = float(_safe_float(row.get("uncertainty_score"), 0.0))
        structure_timing_score = float(_safe_float(row.get("structure_timing_score"), 0.0))
        entry_margin = float(_safe_float(row.get("entry_margin"), 0.0))
        meta_margin = float(_safe_float(row.get("meta_margin"), 0.0))
        calibrated_ev_bps_shadow = float(_safe_float(row.get("calibrated_ev_bps_shadow"), 0.0))
        entry_quality_score_shadow = float(_safe_float(row.get("entry_quality_score_shadow"), 0.0))
        environment_state = str(row.get("environment_state") or "")
        playbook = str(row.get("playbook") or PLAYBOOK_NO_TRADE)
        sleeve = str(row.get("sleeve") or playbook_to_sleeve(playbook))
        campaign_state = str(row.get("campaign_state") or CAMPAIGN_STATE_INACTIVE)
        aggressive_fallback_used = bool(row.get("aggressive_fallback_used", False))
        crowd_penalty = float(_safe_float(row.get("currency_crowding_penalty"), 0.0))
        diversify_penalty = float(_safe_float(row.get("playbook_diversification_penalty"), 0.0))
        allocator_rank = _safe_int(row.get("allocator_rank"), 0)
        allocator_selected = bool(row.get("allocator_selected", False))
        allocator_rejection_reason = str(row.get("allocator_rejection_reason") or "")
        replacement_value = float(_safe_float(row.get("replacement_value"), 0.0))
        ts = str(row.get("ts") or "")
        self.total += 1
        if allowed:
            self.allowed += 1
        if portfolio_rank_shadow > 0:
            self.shadow_candidates += 1
        if shadow_would_trade:
            self.shadow_would_trade += 1
        if structure_rescue_active:
            self.structure_rescues += 1
        self.by_pair[pair]["decisions"] += 1
        self.by_session[session]["decisions"] += 1
        self.by_session[session]["pairs"][pair] += 1
        self.by_environment[environment_state]["decisions"] += 1
        self.by_playbook[playbook]["decisions"] += 1
        self.by_playbook[playbook]["pairs"][pair] += 1
        self.by_sleeve[sleeve]["decisions"] += 1
        self.by_sleeve[sleeve]["pairs"][pair] += 1
        self.by_campaign_state[campaign_state]["decisions"] += 1
        self.by_campaign_state[campaign_state]["pairs"][pair] += 1
        if allowed:
            self.by_pair[pair]["allowed"] += 1
            self.by_session[session]["allowed"] += 1
            self.by_environment[environment_state]["allowed"] += 1
            self.by_playbook[playbook]["allowed"] += 1
            self.by_sleeve[sleeve]["allowed"] += 1
            self.by_campaign_state[campaign_state]["allowed"] += 1
        reason = rejection_reason
        self.by_pair[pair]["reasons"][reason] += 1
        self.by_session[session]["reasons"][reason] += 1
        self.by_environment[environment_state]["reasons"][reason] += 1
        self.by_playbook[playbook]["reasons"][reason] += 1
        self.by_sleeve[sleeve]["reasons"][reason] += 1
        self.by_campaign_state[campaign_state]["reasons"][reason] += 1
        if reason != "none":
            self.primary_rejections[reason] += 1
        shadow_reason = shadow_rejection_reason
        self.by_pair[pair]["shadow_reasons"][shadow_reason] += 1
        if shadow_reason != "none":
            self.shadow_rejections[shadow_reason] += 1
        self.uncertainty_buckets[_uncertainty_bucket(uncertainty_score)] += 1
        self.structure_buckets[_structure_bucket(structure_timing_score)] += 1
        self.pair_tier_breakdown[pair_tier]["decisions"] += 1
        if allowed:
            self.pair_tier_breakdown[pair_tier]["allowed"] += 1
        if reason == "spread_too_wide" or "spread_too_wide" in set(rejection_reasons):
            self.spread_rejects_by_pair_session[pair][session] += 1
        self.lifecycle_action_counts[lifecycle_action] += 1
        self.lifecycle_reason_counts[lifecycle_reason] += 1
        if aggressive_fallback_used:
            self.aggressive_fallback_count += 1
            self.by_playbook[playbook]["aggressive_fallbacks"] += 1
        self.crowding_penalty_sum += crowd_penalty
        self.diversification_penalty_sum += diversify_penalty
        if crowd_penalty > 0.0:
            self.crowding_penalty_nonzero += 1
        if diversify_penalty > 0.0:
            self.diversification_penalty_nonzero += 1
        if allocator_rank > 0:
            self.allocator_candidates += 1
        if allocator_selected:
            self.allocator_selected += 1
        elif allocator_rejection_reason:
            self.allocator_ranked_out += 1
        if replacement_value > 0.0:
            self.allocator_replacements += 1
        if shadow_reason == "shadow_position_open":
            self.shadow_divergence_counts["open_position"] += 1
        elif allowed and not shadow_would_trade:
            self.shadow_divergence_counts["live_only"] += 1
        elif (not allowed) and shadow_would_trade:
            self.shadow_divergence_counts["shadow_only"] += 1
        elif allowed and shadow_would_trade:
            self.shadow_divergence_counts["agree_ready"] += 1
        else:
            self.shadow_divergence_counts["agree_blocked"] += 1

        if self.emit_history:
            hist_row = dict(row)
            hist_row["rejection_reasons"] = "|".join(str(item) for item in rejection_reasons)
            if portfolio_rank_shadow <= 0:
                hist_row["portfolio_rank_shadow"] = ""
            self.history.offer(hist_row)
        key = (pair, ts)
        if key in self.live_validation_keys:
            self.validation_records[key] = {
                "pair": pair,
                "ts": ts,
                "side": str(row.get("side") or ""),
                "allowed": allowed,
                "rejection_reason": reason,
                "expected_edge_bps": float(_safe_float(row.get("expected_edge_bps"), 0.0)),
                "lifecycle_action": lifecycle_action,
            }

        if (
            structure_timing_score >= 0.70
            and shadow_reason in {"shadow_weak_entry", "shadow_meta_reject", "shadow_ev_below_floor"}
        ):
            self.structure_near_miss_rows.append(
                {
                    "pair": pair,
                    "ts": ts,
                    "shadow_rejection_reason": shadow_reason,
                    "structure_timing_score": structure_timing_score,
                    "entry_margin": entry_margin,
                    "meta_margin": meta_margin,
                    "calibrated_ev_bps_shadow": calibrated_ev_bps_shadow,
                    "entry_quality_score_shadow": entry_quality_score_shadow,
                    "htf_alignment_score": float(_safe_float(row.get("htf_alignment_score"), 0.0)),
                    "pullback_quality_score": float(_safe_float(row.get("pullback_quality_score"), 0.0)),
                    "resume_trigger_score": float(_safe_float(row.get("resume_trigger_score"), 0.0)),
                }
            )



# AGENT HANDSHAKE: Live snapshot fetch uses the bridge decision-snapshot contract; keep this aligned with prod bridge routes when parity validation changes.
def _fetch_live_snapshots(*, bridge_url: str, api_key: str, limit: int) -> dict[str, Any]:
    url = f"{str(bridge_url).rstrip('/')}/v2/decision-snapshots?{urlencode({'limit': max(1, min(int(limit), 5000))})}"
    req = Request(url)
    if str(api_key or "").strip():
        req.add_header("X-API-Key", str(api_key).strip())
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {"status": "ok", "items": list(payload.get("items") or [])}
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return {"status": f"error:{type(exc).__name__}", "items": [], "error": str(exc)}


def _flatten_live_snapshot_items(items: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[str, Any]]:
    flat: dict[tuple[str, str], dict[str, Any]] = {}
    mismatch_examples: list[str] = []
    decision_total = 0
    for snap in list(items or []):
        snap_id = _safe_int(snap.get("id"), 0)
        inserted_ts = str(snap.get("ts") or "")
        diagnostics_json = dict(snap.get("diagnostics_json") or {})
        decisions = list(snap.get("decisions_json") or [])
        for decision in decisions:
            meta = dict(decision.get("metadata") or {})
            pair = str(meta.get("pair") or decision.get("symbol") or "").upper().strip()
            ts = str(meta.get("ts") or "").strip()
            if not pair or not ts:
                if len(mismatch_examples) < 10:
                    mismatch_examples.append(f"missing_key snap={snap_id} pair={pair} ts={ts}")
                continue
            key = (pair, ts)
            if key in flat:
                continue
            reasons = list(meta.get("entry_blocking_reasons", decision.get("reasons", [])) or [])
            flat[key] = {
                "pair": pair,
                "ts": ts,
                "side": str(decision.get("side") or "").upper(),
                "allowed": bool(meta.get("allowed", decision.get("execution_ready", False))),
                "rejection_reason": str(meta.get("rejection_reason") or (reasons[0] if reasons else "none")),
                "lifecycle_action": str(meta.get("lifecycle_action") or "hold"),
                "expected_edge_bucket": _edge_bucket(_safe_float(meta.get("expected_edge_bps", decision.get("score", 0.0)), 0.0)),
                "reasons": reasons,
                "snapshot_id": snap_id,
                "snapshot_inserted_ts": inserted_ts,
                "diagnostics": diagnostics_json,
            }
            decision_total += 1
    return flat, {"snapshot_count": len(list(items or [])), "decision_count": decision_total, "warnings": mismatch_examples}


def _compare_live_overlap(*, live_flat: dict[tuple[str, str], dict[str, Any]], twin_rows: dict[tuple[str, str], dict[str, Any]]) -> tuple[TwinValidationResult, dict[str, Any]]:
    if not live_flat:
        result = TwinValidationResult(
            status="insufficient_live_history",
            compared_rows=0,
            exact_match_rate=0.0,
            side_match_rate=0.0,
            allowed_match_rate=0.0,
            rejection_reason_match_rate=0.0,
            lifecycle_action_match_rate=0.0,
            mismatch_reasons={},
            mismatch_examples=[],
        )
        return result, {"status": "insufficient_live_history", "compared_snapshots": 0, "compared_decisions": 0, "mismatch_reasons": {}, "examples_by_pair": {}}

    compared = 0
    exact = 0
    side_matches = 0
    allowed_matches = 0
    reason_matches = 0
    lifecycle_matches = 0
    mismatch_reasons: Counter[str] = Counter()
    mismatch_examples: list[dict[str, Any]] = []
    examples_by_pair: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for key, live in live_flat.items():
        twin = twin_rows.get(key)
        if twin is None:
            mismatch_reasons["missing_twin_record"] += 1
            if len(mismatch_examples) < 25:
                example = {"pair": key[0], "ts": key[1], "reason": "missing_twin_record", "live": live}
                mismatch_examples.append(example)
                examples_by_pair[key[0]].append(example)
            continue
        compared += 1
        pair = str(key[0])
        live_side = str(live.get("side") or "").upper()
        twin_side = str(twin.get("side") or "").upper()
        live_allowed = bool(live.get("allowed"))
        twin_allowed = bool(twin.get("allowed"))
        live_reason = str(live.get("rejection_reason") or "none")
        twin_reason = str(twin.get("rejection_reason") or "none")
        live_lifecycle = str(live.get("lifecycle_action") or "hold")
        twin_lifecycle = str(twin.get("lifecycle_action") or "hold")
        live_edge_bucket = str(live.get("expected_edge_bucket") or "")
        twin_edge_bucket = _edge_bucket(_safe_float(twin.get("expected_edge_bps"), 0.0))

        side_ok = live_side == twin_side
        allowed_ok = live_allowed == twin_allowed
        reason_ok = live_reason == twin_reason
        lifecycle_ok = live_lifecycle == twin_lifecycle
        edge_ok = live_edge_bucket == twin_edge_bucket

        side_matches += int(side_ok)
        allowed_matches += int(allowed_ok)
        reason_matches += int(reason_ok)
        lifecycle_matches += int(lifecycle_ok)
        if side_ok and allowed_ok and reason_ok and lifecycle_ok and edge_ok:
            exact += 1
        else:
            if not side_ok:
                mismatch_reasons["side_mismatch"] += 1
            if not allowed_ok:
                mismatch_reasons["allowed_mismatch"] += 1
            if not reason_ok:
                mismatch_reasons["rejection_reason_mismatch"] += 1
            if not lifecycle_ok:
                mismatch_reasons["lifecycle_action_mismatch"] += 1
            if not edge_ok:
                mismatch_reasons["expected_edge_bucket_mismatch"] += 1
            if len(mismatch_examples) < 25:
                example = {
                    "pair": pair,
                    "ts": key[1],
                    "live": {
                        "side": live_side,
                        "allowed": live_allowed,
                        "rejection_reason": live_reason,
                        "lifecycle_action": live_lifecycle,
                        "expected_edge_bucket": live_edge_bucket,
                    },
                    "twin": {
                        "side": twin_side,
                        "allowed": twin_allowed,
                        "rejection_reason": twin_reason,
                        "lifecycle_action": twin_lifecycle,
                        "expected_edge_bucket": twin_edge_bucket,
                    },
                }
                mismatch_examples.append(example)
                examples_by_pair[pair].append(example)

    if compared == 0:
        status = "insufficient_live_history"
    else:
        side_rate = side_matches / compared
        allowed_rate = allowed_matches / compared
        reason_rate = reason_matches / compared
        status = "ok" if side_rate >= 0.98 and allowed_rate >= 0.95 and reason_rate >= 0.90 else "validation_degraded"

    result = TwinValidationResult(
        status=str(status),
        compared_rows=int(compared),
        exact_match_rate=float(exact / compared) if compared else 0.0,
        side_match_rate=float(side_matches / compared) if compared else 0.0,
        allowed_match_rate=float(allowed_matches / compared) if compared else 0.0,
        rejection_reason_match_rate=float(reason_matches / compared) if compared else 0.0,
        lifecycle_action_match_rate=float(lifecycle_matches / compared) if compared else 0.0,
        mismatch_reasons={k: int(v) for k, v in mismatch_reasons.items()},
        mismatch_examples=mismatch_examples,
    )
    recent = {
        "status": str(status),
        "compared_snapshots": int(len(live_flat)),
        "compared_decisions": int(compared),
        "match_rates": {
            "exact": float(result.exact_match_rate),
            "side": float(result.side_match_rate),
            "allowed": float(result.allowed_match_rate),
            "rejection_reason": float(result.rejection_reason_match_rate),
            "lifecycle_action": float(result.lifecycle_action_match_rate),
        },
        "mismatch_reasons": {k: int(v) for k, v in mismatch_reasons.items()},
        "mismatch_examples": mismatch_examples,
        "examples_by_pair": {pair: rows[:5] for pair, rows in examples_by_pair.items()},
    }
    return result, recent


def _manifest_fingerprint(settings: Any, project_root: Path, model_sets: dict[str, Any]) -> dict[str, Any]:
    manifest_path = BASE._resolve_optional_path(str(settings.model_activation_manifest), project_root)
    manifest_hash = ""
    if manifest_path is not None and Path(manifest_path).exists():
        manifest_hash = hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
    registry_paths = sorted(str(getattr(v, "registry_path", "")) for v in model_sets.values())
    registry_hash = hashlib.sha256("\n".join(registry_paths).encode("utf-8")).hexdigest() if registry_paths else ""
    return {
        "manifest_path": str(manifest_path) if manifest_path is not None else "",
        "manifest_sha256": manifest_hash,
        "registry_paths": registry_paths,
        "registry_paths_sha256": registry_hash,
    }


def _prepare_twin_pair_data(
    *,
    pair: str,
    loaded: Any,
    feature_store: Any,
    provider: str,
    intraday_timeframe: str,
    all_pairs: list[str],
    start_ts: pd.Timestamp | None,
    end_ts: pd.Timestamp | None,
    settings: Any,
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    df = BASE._load_historical_contract_frame(
        raw_store_root=Path(feature_store.root),
        pair=pair,
        provider=provider,
        intraday_timeframe=intraday_timeframe,
        all_pairs=all_pairs,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    regime_input = BASE._context_input(df, model=loaded.scorer.regime_model, prefix="h4_")
    swing_input = BASE._context_input(df, model=loaded.swing_router.primary_model or loaded.swing_router.fallback_model, prefix="d_")
    intraday_input = df.select_dtypes(include=["number"]).copy()
    scorer = loaded.scorer

    regime_proba = scorer.regime_model.predict_proba(regime_input)
    swing_proba = scorer.swing_model.predict_proba(swing_input)
    intraday_proba = scorer.intraday_model.predict_proba(scorer._model_input(scorer.intraday_model, intraday_input))

    regime_prob = regime_proba.max(axis=1).astype(float)
    swing_prob = swing_proba["p1"].astype(float)
    entry_prob = intraday_proba["p1"].astype(float)
    side = pd.Series(np.where(swing_prob >= 0.5, "long", "short"), index=df.index, dtype="object")

    meta_input = BASE._vector_meta_input(
        loaded.scorer.meta_model,
        df,
        regime_prob=regime_prob,
        swing_prob=swing_prob,
        entry_prob=entry_prob,
        side=side,
    )
    meta_proba = scorer.meta_model.predict_proba(scorer._model_input(scorer.meta_model, meta_input))
    trade_prob = meta_proba["p1"].astype(float)

    if "spread_bps" in df.columns:
        spread_bps = pd.to_numeric(df["spread_bps"], errors="coerce").fillna(0.0).astype(float)
        spread_unit_source = pd.Series("feature", index=df.index, dtype="object")
    else:
        spread_bps = ((pd.to_numeric(df["ask_close"], errors="coerce") - pd.to_numeric(df["bid_close"], errors="coerce")).abs() / pd.to_numeric(df["mid_close"], errors="coerce").abs().clip(lower=1e-9) * 10000.0)
        spread_unit_source = pd.Series("reconstructed_bid_ask", index=df.index, dtype="object")
    expected_edge_bps = BASE._expected_edge_bps_frame(
        df,
        regime_prob=regime_prob,
        swing_prob=swing_prob,
        entry_prob=entry_prob,
        trade_prob=trade_prob,
        side=side,
    )
    gate = BASE._gate_frame(
        spread_bps=spread_bps,
        expected_edge_bps=expected_edge_bps,
        swing_prob=swing_prob,
        entry_prob=entry_prob,
        trade_prob=trade_prob,
        side=side,
        settings=settings,
    )

    side_sign = np.where(side.astype(str).str.lower().eq("short"), -1.0, 1.0)
    directional_swing_confidence = np.where(side.astype(str).str.lower().eq("short"), 1.0 - swing_prob, swing_prob)

    if "uncertainty_score" in df.columns:
        uncertainty_score = pd.to_numeric(df["uncertainty_score"], errors="coerce").fillna(0.0).astype(float).clip(lower=0.0, upper=1.0)
    else:
        ambiguity_components = np.column_stack(
            [
                1.0 - (np.abs(np.asarray(regime_prob, dtype=float) - 0.5) * 2.0),
                1.0 - (np.abs(np.asarray(entry_prob, dtype=float) - 0.5) * 2.0),
                1.0 - (np.abs(np.asarray(trade_prob, dtype=float) - 0.5) * 2.0),
                2.0 * np.maximum(0.0, 1.0 - np.asarray(directional_swing_confidence, dtype=float)),
            ]
        )
        probability_ambiguity = np.mean(np.clip(ambiguity_components, 0.0, 1.0), axis=1)
        spread_z20 = _series_or_default(df, "spread_z20", 0.0).to_numpy(dtype=float)
        normalized_spread = _series_or_default(df, "normalized_spread", 0.0).to_numpy(dtype=float)
        vol_term_ratio = _series_or_default(df, "vol_term_ratio", 1.0).to_numpy(dtype=float)
        bar_imbalance = _series_or_default(df, "bar_imbalance", 0.0).to_numpy(dtype=float)
        h1_available = _series_or_default(df, "h1_available", 1.0).to_numpy(dtype=float)
        anomaly_components = np.column_stack(
            [
                np.minimum(np.abs(spread_z20) / 3.0, 1.0),
                np.where(normalized_spread > 0.0, np.minimum(normalized_spread / 2.0, 1.0), 0.0),
                np.where(vol_term_ratio > 0.0, np.minimum(np.abs(vol_term_ratio - 1.0) / 1.5, 1.0), 0.0),
                np.minimum(np.abs(bar_imbalance), 1.0),
                np.where(h1_available >= 1.0, 0.0, 1.0),
            ]
        )
        feature_anomaly = np.mean(np.clip(anomaly_components, 0.0, 1.0), axis=1)
        uncertainty_score = pd.Series(np.clip((0.65 * probability_ambiguity) + (0.35 * feature_anomaly), 0.0, 1.0), index=df.index, dtype=float)

    disagreement_score = pd.Series(
        np.clip(
            np.mean(
                np.column_stack(
                    [
                        np.abs(np.asarray(directional_swing_confidence, dtype=float) - np.asarray(entry_prob, dtype=float)),
                        np.abs(np.asarray(directional_swing_confidence, dtype=float) - np.asarray(trade_prob, dtype=float)),
                        np.abs(np.asarray(entry_prob, dtype=float) - np.asarray(trade_prob, dtype=float)),
                        np.abs(np.asarray(trade_prob, dtype=float) - np.asarray(regime_prob, dtype=float)),
                    ]
                ),
                axis=1,
            ),
            0.0,
            1.0,
        ),
        index=df.index,
        dtype=float,
    )

    htf_alignment_score = _htf_alignment_score_series(df, side_sign=side_sign)

    pullback_depth_long = _series_or_default(df, "pullback_depth_20", 0.0).to_numpy(dtype=float)
    pushup_depth_short = _series_or_default(df, "pushup_depth_20", 0.0).to_numpy(dtype=float)
    pullback_depth = np.where(side_sign < 0.0, pushup_depth_short, pullback_depth_long)
    pullback_quality_score = _triangular_score_array(pullback_depth, target=0.0018, width=0.0036)
    pullback_quality_score = pd.Series(np.clip(pullback_quality_score * (0.5 + (0.5 * np.asarray(htf_alignment_score, dtype=float))), 0.0, 1.0), index=df.index, dtype=float)

    vol_ref = np.maximum(np.maximum(np.abs(_series_or_default(df, "vol_20", 0.0).to_numpy(dtype=float)), np.abs(_series_or_default(df, "vol_60", 0.0).to_numpy(dtype=float))), 1e-6)
    resume_components = np.column_stack(
        [
            _directional_component_score_array(_series_or_default(df, "ret_1", 0.0).to_numpy(dtype=float), side_sign=side_sign, scale=vol_ref * 1.5),
            _directional_component_score_array(_series_or_default(df, "edge_decay_12", 0.0).to_numpy(dtype=float), side_sign=side_sign, scale=vol_ref * 1.5),
            _directional_component_score_array(_series_or_default(df, "bar_imbalance", 0.0).to_numpy(dtype=float), side_sign=side_sign, scale=0.80),
            _directional_component_score_array(_series_or_default(df, "micro_pressure", 0.0).to_numpy(dtype=float), side_sign=side_sign, scale=0.80),
        ]
    )
    resume_trigger_score = pd.Series(np.mean(resume_components, axis=1), index=df.index, dtype=float)

    extension_components = np.column_stack(
        [
            np.clip(np.maximum(0.0, (_directional_value_array(_series_or_default(df, "trend_strength_20", 0.0).to_numpy(dtype=float), side_sign) - 1.25) / 2.0), 0.0, 1.0),
            np.clip(np.maximum(0.0, (_directional_value_array(_series_or_default(df, "trend_strength_60", 0.0).to_numpy(dtype=float), side_sign) - 1.00) / 2.5), 0.0, 1.0),
            np.clip(np.maximum(0.0, (_directional_value_array(_series_or_default(df, "ret_5", 0.0).to_numpy(dtype=float), side_sign) - 0.0012) / 0.0030), 0.0, 1.0),
            np.clip(np.maximum(0.0, (_directional_value_array(_series_or_default(df, "ret_20", 0.0).to_numpy(dtype=float), side_sign) - 0.0030) / 0.0070), 0.0, 1.0),
            np.clip(np.maximum(0.0, (_directional_value_array(_series_or_default(df, "h1_trend_strength_20", 0.0).to_numpy(dtype=float), side_sign) - 1.10) / 2.0), 0.0, 1.0),
        ]
    )
    extension_penalty_score = pd.Series(np.mean(extension_components, axis=1), index=df.index, dtype=float)
    structure_timing_score = pd.Series(
        np.clip(
            (0.40 * np.asarray(htf_alignment_score, dtype=float))
            + (0.25 * np.asarray(pullback_quality_score, dtype=float))
            + (0.25 * np.asarray(resume_trigger_score, dtype=float))
            + (0.10 * (1.0 - np.asarray(extension_penalty_score, dtype=float))),
            0.0,
            1.0,
        ),
        index=df.index,
        dtype=float,
    )

    pair_tier = str(_shadow_pair_tier(settings, pair))
    rescue_margin = float(settings.structure_timing_entry_rescue_margin)
    tier1_rescue_override = getattr(args, "shadow_tier1_structure_rescue_margin", None)
    if pair_tier == "tier1" and tier1_rescue_override is not None:
        rescue_margin = float(tier1_rescue_override)
    raw_calibrated_ev = np.asarray(expected_edge_bps, dtype=float) - np.asarray(spread_bps, dtype=float)
    pair_quality_multiplier = 1.05 if bool(getattr(settings, "enable_pair_quality_prior", False)) and pair_tier == "tier1" else 1.0
    calibrated_ev = raw_calibrated_ev * pair_quality_multiplier
    structure_bonus_bps = np.zeros(len(df), dtype=float)
    chase_penalty_bps = np.zeros(len(df), dtype=float)
    if bool(getattr(settings, "use_structure_timing_shadow", True)):
        quality_scale = np.maximum.reduce(
            [
                np.ones(len(df), dtype=float),
                np.full(len(df), float(settings.min_expected_edge_bps), dtype=float),
                np.abs(calibrated_ev) * 0.75,
            ]
        )
        structure_bonus_bps = np.maximum(0.0, np.asarray(structure_timing_score, dtype=float) - 0.5) * quality_scale
        chase_penalty_bps = np.asarray(extension_penalty_score, dtype=float) * quality_scale
        calibrated_ev = calibrated_ev + structure_bonus_bps - chase_penalty_bps
    uncertainty_penalty_bps = np.asarray(uncertainty_score, dtype=float) * np.maximum.reduce(
        [
            np.ones(len(df), dtype=float),
            np.full(len(df), float(settings.min_expected_edge_bps), dtype=float),
            np.abs(calibrated_ev) * 0.5,
        ]
    )
    disagreement_penalty_bps = np.asarray(disagreement_score, dtype=float) * np.maximum.reduce(
        [
            np.ones(len(df), dtype=float),
            np.full(len(df), float(settings.min_expected_edge_bps), dtype=float),
            np.abs(calibrated_ev) * 0.75,
        ]
    )
    entry_quality_score_shadow = calibrated_ev - uncertainty_penalty_bps - disagreement_penalty_bps

    directional_conf = np.asarray(directional_swing_confidence, dtype=float)
    entry_margin = np.asarray(entry_prob, dtype=float) - float(settings.min_entry_prob)
    meta_margin = np.asarray(trade_prob, dtype=float) - float(settings.min_trade_prob)
    structure_rescue_eligible = (
        bool(getattr(settings, "use_structure_timing_shadow", True))
        and (np.asarray(htf_alignment_score, dtype=float) >= 0.60)
        & (np.asarray(structure_timing_score, dtype=float) >= float(settings.structure_timing_rescue_min_score))
        & (np.asarray(extension_penalty_score, dtype=float) <= float(settings.structure_timing_max_chase_risk))
    )
    floor_ok = np.ones(len(df), dtype=bool)
    floor_reason = np.full(len(df), "approved", dtype=object)
    structure_rescue_active = np.zeros(len(df), dtype=bool)

    weak_swing = directional_conf < float(settings.min_swing_prob)
    floor_ok[weak_swing] = False
    floor_reason[weak_swing] = "shadow_weak_swing"

    weak_entry = (~weak_swing) & (np.asarray(entry_prob, dtype=float) < float(settings.min_entry_prob))
    weak_entry_rescue = weak_entry & structure_rescue_eligible & (np.asarray(entry_prob, dtype=float) >= float(settings.min_entry_prob) - float(rescue_margin))
    structure_rescue_active[weak_entry_rescue] = True
    floor_reason[weak_entry_rescue] = "structure_timing_rescue"
    weak_entry_block = weak_entry & (~weak_entry_rescue)
    floor_ok[weak_entry_block] = False
    floor_reason[weak_entry_block] = "shadow_weak_entry"

    meta_block = (~weak_swing) & (~weak_entry) & (np.asarray(trade_prob, dtype=float) < float(settings.min_trade_prob))
    floor_ok[meta_block] = False
    floor_reason[meta_block] = "shadow_meta_reject"

    ev_block = (~weak_swing) & (~weak_entry) & (~meta_block) & (np.asarray(calibrated_ev, dtype=float) < float(settings.min_expected_edge_bps))
    ev_rescue = ev_block & structure_rescue_eligible & (np.asarray(calibrated_ev, dtype=float) >= float(settings.min_expected_edge_bps) - float(max(0.0, settings.entry_hysteresis_margin_bps)))
    structure_rescue_active[ev_rescue] = True
    floor_reason[ev_rescue] = "structure_timing_rescue"
    ev_block_final = ev_block & (~ev_rescue)
    floor_ok[ev_block_final] = False
    floor_reason[ev_block_final] = "shadow_ev_below_floor"

    tier1_override = (pair_tier == "tier1") & (np.asarray(calibrated_ev, dtype=float) >= float(settings.min_expected_edge_bps) + float(max(0.0, settings.entry_hysteresis_margin_bps)))
    uncertainty_block = (
        (~weak_swing)
        & (~weak_entry)
        & (~meta_block)
        & (~ev_block)
        & bool(getattr(settings, "use_uncertainty_gate", True))
        & (np.asarray(uncertainty_score, dtype=float) > float(settings.max_entry_uncertainty))
        & (~tier1_override)
    )
    floor_ok[uncertainty_block] = False
    floor_reason[uncertainty_block] = "shadow_uncertainty_gate"

    session_bucket = _session_bucket_series(df["ts"])
    blocked_sessions = {str(item).strip().lower() for item in list(getattr(settings, "blocked_entry_sessions", []) or []) if str(item).strip()}
    session_entry_blocked = session_bucket.astype(str).str.lower().isin(blocked_sessions)
    session_entry_block_reason = np.where(session_entry_blocked, "session_blocked:" + session_bucket.astype(str), "")

    shadow_pair_spread_cap_bps = np.full(len(df), float(settings.max_allowed_spread_bps), dtype=float)
    shadow_spread_relaxed = np.zeros(len(df), dtype=bool)
    if bool(getattr(args, "shadow_pair_aware_spread_caps", False)):
        quantile = min(0.99, max(0.01, float(getattr(args, "shadow_spread_cap_quantile", 0.75))))
        multiplier = max(1.0, float(getattr(args, "shadow_spread_cap_multiplier", 1.25)))
        max_cap = max(float(settings.max_allowed_spread_bps), float(getattr(args, "shadow_spread_cap_max_bps", 5.0)))
        non_pacific_mask = session_bucket.astype(str).ne("pacific").to_numpy(dtype=bool)
        non_pacific_spreads = np.asarray(spread_bps, dtype=float)[non_pacific_mask]
        if non_pacific_spreads.size:
            derived_cap = float(np.quantile(non_pacific_spreads, quantile) * multiplier)
            derived_cap = max(float(settings.max_allowed_spread_bps), min(max_cap, derived_cap))
            shadow_pair_spread_cap_bps[:] = derived_cap
            shadow_spread_relaxed = non_pacific_mask & (np.asarray(spread_bps, dtype=float) <= derived_cap)

    scenario_bucket = _string_series_or_default(df, "scenario_bucket", "unknown")
    regime_bucket = _string_series_or_default(df, "regime_bucket", "")
    if regime_bucket.astype(str).eq("").all():
        regime_bucket = _regime_bucket_series(regime_prob)

    out = pd.DataFrame(
        {
            "ts": df["ts"],
            "side": np.where(side.eq("long"), "BUY", "SELL"),
            "signal_side": side.astype("category"),
            "expected_edge_bps": expected_edge_bps.astype(float),
            "spread_bps": spread_bps.astype(float),
            "regime_prob": regime_prob.astype(float),
            "swing_prob": swing_prob.astype(float),
            "entry_prob": entry_prob.astype(float),
            "trade_prob": trade_prob.astype(float),
            "allowed": gate["allowed"].astype(bool),
            "rejection_reason": gate["rejection_reason"].astype("category"),
            "directional_swing_prob": gate["directional_swing_prob"].astype(float),
            "uncertainty_score": uncertainty_score.astype(float),
            "directional_swing_confidence": pd.Series(directional_conf, index=df.index, dtype=float),
            "entry_margin": pd.Series(entry_margin, index=df.index, dtype=float),
            "meta_margin": pd.Series(meta_margin, index=df.index, dtype=float),
            "model_disagreement_score": disagreement_score.astype(float),
            "htf_alignment_score": htf_alignment_score.astype(float),
            "pullback_quality_score": pullback_quality_score.astype(float),
            "resume_trigger_score": resume_trigger_score.astype(float),
            "extension_penalty_score": extension_penalty_score.astype(float),
            "structure_timing_score": structure_timing_score.astype(float),
            "structure_bonus_bps": pd.Series(structure_bonus_bps, index=df.index, dtype=float),
            "chase_penalty_bps": pd.Series(chase_penalty_bps, index=df.index, dtype=float),
            "calibrated_ev_bps_shadow": pd.Series(calibrated_ev, index=df.index, dtype=float),
            "entry_quality_score_shadow": pd.Series(entry_quality_score_shadow, index=df.index, dtype=float),
            "structure_rescue_active": pd.Series(structure_rescue_active, index=df.index, dtype=bool),
            "shadow_floor_ok": pd.Series(floor_ok, index=df.index, dtype=bool),
            "shadow_floor_rejection_reason": pd.Series(floor_reason, index=df.index, dtype="object").astype("category"),
            "session_bucket": session_bucket.astype("category"),
            "session_entry_blocked": pd.Series(session_entry_blocked, index=df.index, dtype=bool),
            "session_entry_block_reason": pd.Series(session_entry_block_reason, index=df.index, dtype="object").astype("category"),
            "shadow_pair_spread_cap_bps": pd.Series(shadow_pair_spread_cap_bps, index=df.index, dtype=float),
            "shadow_spread_relaxed": pd.Series(shadow_spread_relaxed, index=df.index, dtype=bool),
            "scenario_bucket": scenario_bucket.astype("category"),
            "regime_bucket": regime_bucket.astype("category"),
            "spread_unit_source": spread_unit_source.astype("category"),
            "pair_tier": pd.Series(pair_tier, index=df.index, dtype="object").astype("category"),
            "ret_1": _series_or_default(df, "ret_1", 0.0).astype(float),
            "ret_5": _series_or_default(df, "ret_5", 0.0).astype(float),
            "ret_20": _series_or_default(df, "ret_20", 0.0).astype(float),
            "vol_term_ratio": _series_or_default(df, "vol_term_ratio", 1.0).astype(float),
            "atr_14": _series_or_default(df, "atr_14", 0.0).astype(float),
            "bar_imbalance": _series_or_default(df, "bar_imbalance", 0.0).astype(float),
            "micro_pressure": _series_or_default(df, "micro_pressure", 0.0).astype(float),
            "pullback_depth_20": _series_or_default(df, "pullback_depth_20", 0.0).astype(float),
            "pushup_depth_20": _series_or_default(df, "pushup_depth_20", 0.0).astype(float),
            "cross_pair_dispersion": _series_or_default(df, "cross_pair_dispersion", 0.0).astype(float),
            "trend_strength_20": _series_or_default(df, "trend_strength_20", 0.0).astype(float),
            "trend_strength_60": _series_or_default(df, "trend_strength_60", 0.0).astype(float),
            "h1_trend_strength_20": _series_or_default(df, "h1_trend_strength_20", 0.0).astype(float),
            "h4_trend_strength_20": _series_or_default(df, "h4_trend_strength_20", 0.0).astype(float),
            "d_trend_strength_20": _series_or_default(df, "d_trend_strength_20", 0.0).astype(float),
            "bid_close": pd.to_numeric(df["bid_close"], errors="coerce").fillna(0.0).astype(float),
            "ask_close": pd.to_numeric(df["ask_close"], errors="coerce").fillna(0.0).astype(float),
            "mid_close": pd.to_numeric(df["mid_close"], errors="coerce").fillna(0.0).astype(float),
        }
    ).set_index("ts")

    lifecycle_columns = sorted(
        set(BASE._required_model_feature_columns(loaded.exit_model, loaded.reversal_failure_model, loaded.reversal_opportunity_model))
        | {"pair", "ts", "bid_close", "ask_close", "mid_close"}
    )
    lifecycle_columns = [col for col in lifecycle_columns if col in df.columns]
    return out, df[["ts", "bid_close", "ask_close", "mid_close"]].copy(), lifecycle_columns


def _max_drawdown_duration_bars(drawdown_usd: np.ndarray) -> int:
    max_run = 0
    run = 0
    for val in np.asarray(drawdown_usd, dtype=float):
        if float(val) < 0.0:
            run += 1
            if run > max_run:
                max_run = run
        else:
            run = 0
    return int(max_run)


def _ulcer_index(drawdown_pct: np.ndarray) -> float:
    dd = np.abs(np.minimum(np.asarray(drawdown_pct, dtype=float), 0.0))
    if dd.size == 0:
        return 0.0
    return float(math.sqrt(float(np.mean(np.square(dd)))))


def _sharpe_like(equity_usd: np.ndarray) -> float:
    eq = np.asarray(equity_usd, dtype=float)
    if eq.size < 2:
        return 0.0
    prev = np.where(eq[:-1] == 0.0, np.nan, eq[:-1])
    ret = np.diff(eq) / prev
    ret = ret[np.isfinite(ret)]
    if ret.size < 2:
        return 0.0
    std = float(np.std(ret, ddof=1))
    if std <= 0.0:
        return 0.0
    return float(np.mean(ret) / std * math.sqrt(len(ret)))


def _to_record(decision: dict[str, Any]) -> TwinDecisionRecord:
    meta = dict(decision.get("metadata") or {})
    return TwinDecisionRecord(
        pair=str(meta.get("pair") or decision.get("symbol") or ""),
        ts=str(meta.get("ts") or ""),
        side=str(decision.get("side") or ""),
        allowed=bool(meta.get("allowed", decision.get("execution_ready", False))),
        rejection_reason=str(meta.get("rejection_reason") or "none"),
        rejection_reasons=list(meta.get("entry_blocking_reasons", decision.get("reasons", [])) or []),
        expected_edge_bps=float(_safe_float(meta.get("expected_edge_bps", decision.get("score", 0.0)), 0.0)),
        spread_bps=float(_safe_float(meta.get("spread_bps", 0.0), 0.0)),
        regime_prob=float(_safe_float(meta.get("regime_prob", 0.0), 0.0)),
        swing_prob=float(_safe_float(meta.get("swing_prob", 0.0), 0.0)),
        entry_prob=float(_safe_float(meta.get("entry_prob", 0.0), 0.0)),
        trade_prob=float(_safe_float(meta.get("trade_prob", 0.0), 0.0)),
        uncertainty_score=float(_safe_float(meta.get("uncertainty_score", 0.0), 0.0)),
        model_disagreement_score=float(_safe_float(meta.get("model_disagreement_score", 0.0), 0.0)),
        directional_swing_confidence=float(_safe_float(meta.get("directional_swing_confidence", 0.0), 0.0)),
        entry_margin=float(_safe_float(meta.get("entry_margin", 0.0), 0.0)),
        meta_margin=float(_safe_float(meta.get("meta_margin", 0.0), 0.0)),
        session_bucket=str(meta.get("session_bucket") or ""),
        session_entry_blocked=bool(meta.get("session_entry_blocked", False)),
        session_entry_block_reason=str(meta.get("session_entry_block_reason") or ""),
        htf_alignment_score=float(_safe_float(meta.get("htf_alignment_score", 0.0), 0.0)),
        pullback_quality_score=float(_safe_float(meta.get("pullback_quality_score", 0.0), 0.0)),
        resume_trigger_score=float(_safe_float(meta.get("resume_trigger_score", 0.0), 0.0)),
        extension_penalty_score=float(_safe_float(meta.get("extension_penalty_score", 0.0), 0.0)),
        structure_timing_score=float(_safe_float(meta.get("structure_timing_score", 0.0), 0.0)),
        structure_bonus_bps=float(_safe_float(meta.get("structure_bonus_bps", 0.0), 0.0)),
        chase_penalty_bps=float(_safe_float(meta.get("chase_penalty_bps", 0.0), 0.0)),
        calibrated_ev_bps_shadow=float(_safe_float(meta.get("calibrated_ev_bps_shadow", 0.0), 0.0)),
        entry_quality_score_shadow=float(_safe_float(meta.get("entry_quality_score_shadow", 0.0), 0.0)),
        structure_rescue_active=bool(meta.get("structure_rescue_active", False)),
        shadow_floor_ok=bool(meta.get("shadow_floor_ok", False)),
        shadow_floor_rejection_reason=str(meta.get("shadow_floor_rejection_reason") or ""),
        portfolio_rank_shadow=(_safe_int(meta.get("portfolio_rank_shadow"), 0) or None),
        shadow_would_trade=bool(meta.get("shadow_would_trade", False)),
        shadow_rejection_reason=str(meta.get("shadow_rejection_reason") or ""),
        pair_tier=str(meta.get("pair_tier") or ""),
        position_side=str(meta.get("position_side") or "flat"),
        position_count_pair=int(_safe_int(meta.get("position_count_pair"), 0)),
        total_open_positions=int(_safe_int(meta.get("total_open_positions"), 0)),
        lifecycle_action=str(meta.get("lifecycle_action") or "hold"),
        lifecycle_reason=str(meta.get("lifecycle_reason") or "hold"),
        exit_action_selected=str(meta.get("exit_action_selected") or "hold"),
        reversal_context_active=bool(meta.get("reversal_context_active", False)),
        reversal_ready=bool(meta.get("reversal_ready", False)),
        reversal_failure_prob=float(_safe_float(meta.get("reversal_failure_prob", 0.0), 0.0)),
        reversal_opportunity_prob=float(_safe_float(meta.get("reversal_opportunity_prob", 0.0), 0.0)),
        baseline_allowed=bool(meta.get("baseline_allowed", False)),
        baseline_rejection_reason=str(meta.get("baseline_rejection_reason") or "none"),
        exec_mode=str(meta.get("exec_mode") or STRICT_EXEC_MODE),
        environment_state=str(meta.get("environment_state") or ""),
        trend_persistence_score=float(_safe_float(meta.get("trend_persistence_score", 0.0), 0.0)),
        compression_score=float(_safe_float(meta.get("compression_score", 0.0), 0.0)),
        expansion_score=float(_safe_float(meta.get("expansion_score", 0.0), 0.0)),
        range_score=float(_safe_float(meta.get("range_score", 0.0), 0.0)),
        hostility_score=float(_safe_float(meta.get("hostility_score", 0.0), 0.0)),
        macro_coherence_score=float(_safe_float(meta.get("macro_coherence_score", 0.0), 0.0)),
        pair_strength_score=float(_safe_float(meta.get("pair_strength_score", 0.0), 0.0)),
        playbook=str(meta.get("playbook") or ""),
        sleeve=str(meta.get("sleeve") or playbook_to_sleeve(meta.get("playbook") or "")),
        playbook_score=float(_safe_float(meta.get("playbook_score", 0.0), 0.0)),
        location_score=float(_safe_float(meta.get("location_score", 0.0), 0.0)),
        trigger_score=float(_safe_float(meta.get("trigger_score", 0.0), 0.0)),
        adaptive_entry_quality=float(_safe_float(meta.get("adaptive_entry_quality", 0.0), 0.0)),
        thesis_id=str(meta.get("thesis_id") or ""),
        campaign_seq=int(_safe_int(meta.get("campaign_seq"), 0)),
        campaign_entry_kind=str(meta.get("campaign_entry_kind") or ""),
        campaign_state=str(meta.get("campaign_state") or CAMPAIGN_STATE_INACTIVE),
        campaign_state_reason=str(meta.get("campaign_state_reason") or ""),
        campaign_proof_score=float(_safe_float(meta.get("campaign_proof_score", 0.0), 0.0)),
        campaign_maturity_score=float(_safe_float(meta.get("campaign_maturity_score", 0.0), 0.0)),
        campaign_reset_quality=float(_safe_float(meta.get("campaign_reset_quality", 0.0), 0.0)),
        campaign_priority_boost=float(_safe_float(meta.get("campaign_priority_boost", 0.0), 0.0)),
        campaign_reentry_blocked=bool(meta.get("campaign_reentry_blocked", False)),
        currency_crowding_penalty=float(_safe_float(meta.get("currency_crowding_penalty", 0.0), 0.0)),
        playbook_diversification_penalty=float(_safe_float(meta.get("playbook_diversification_penalty", 0.0), 0.0)),
        allocator_score=float(_safe_float(meta.get("allocator_score", 0.0), 0.0)),
        allocator_rank=(_safe_int(meta.get("allocator_rank"), 0) or None),
        allocator_selected=bool(meta.get("allocator_selected", False)),
        allocator_rejection_reason=str(meta.get("allocator_rejection_reason") or ""),
        replacement_value=float(_safe_float(meta.get("replacement_value", 0.0), 0.0)),
        sleeve_health_score=float(_safe_float(meta.get("sleeve_health_score", 0.0), 0.0)),
        sleeve_health_state=str(meta.get("sleeve_health_state") or "healthy"),
        aggressive_fallback_used=bool(meta.get("aggressive_fallback_used", False)),
        adaptive_allowed=bool(meta.get("adaptive_allowed", False)),
        adaptive_rejection_reason=str(meta.get("adaptive_rejection_reason") or ""),
        scenario_bucket=str(meta.get("scenario_bucket") or ""),
        regime_bucket=str(meta.get("regime_bucket") or ""),
    )


def _build_recommendations(
    *,
    aggregate: dict[str, Any],
    trades_df: pd.DataFrame,
    structure_summary: dict[str, Any],
    uncertainty_summary: dict[str, Any],
    lifecycle_summary: dict[str, Any],
    rejections_by_session: dict[str, Any],
    per_pair_records: list[dict[str, Any]],
) -> list[TwinRecommendation]:
    recs: list[TwinRecommendation] = []

    near_miss = int(structure_summary.get("near_miss_count", 0))
    rescue_count = int(structure_summary.get("structure_rescue_count", 0))
    weak_entry_near_miss = int(structure_summary.get("near_miss_reasons", {}).get("shadow_weak_entry", 0))
    meta_near_miss = int(structure_summary.get("near_miss_reasons", {}).get("shadow_meta_reject", 0))
    if near_miss >= 25 and weak_entry_near_miss >= meta_near_miss:
        recs.append(
            TwinRecommendation(
                category="entry_timing",
                severity="high",
                finding="High-structure setups are still being lost at the entry floor.",
                evidence=[
                    f"high-structure near misses={near_miss}",
                    f"shadow_weak_entry near misses={weak_entry_near_miss}",
                    f"structure rescues observed={rescue_count}",
                ],
                proposed_change="Expand timing-conditioned rescue only in shadow for Tier 1 pairs and validate whether those rescues improve realized expectancy without loosening the global entry floor.",
                validation_plan="Replay the same window with a small increase to structure_timing_entry_rescue_margin for Tier 1 only and compare per-pair expectancy and live-overlap drift.",
            )
        )

    session_rows = sorted(
        ((session, row) for session, row in rejections_by_session.items()),
        key=lambda item: (-int(item[1].get("spread_rejects", 0)), -int(item[1].get("reject_count", 0)), item[0]),
    )
    if session_rows:
        top_session, top_row = session_rows[0]
        profitable_sessions = [row for row in aggregate.get("pnl_by_session", []) if _safe_float(row.get("net_pnl_usd"), 0.0) > 0.0]
        if int(top_row.get("spread_rejects", 0)) >= 50 and any(str(row.get("session_bucket")) != str(top_session) for row in profitable_sessions):
            recs.append(
                TwinRecommendation(
                    category="spread_session_policy",
                    severity="high",
                    finding="Spread pressure is concentrated in one session while realized profits come from others.",
                    evidence=[
                        f"top spread reject session={top_session}",
                        f"spread rejects in top session={int(top_row.get('spread_rejects', 0))}",
                        f"profitable sessions={[str(row.get('session_bucket')) for row in profitable_sessions][:4]}",
                    ],
                    proposed_change="Keep the hard session block in the worst session and test pair-specific spread caps in shadow mode for the remaining sessions rather than loosening the global spread cap.",
                    validation_plan="Compare spread reject counts and realized expectancy by pair/session on the next twin run with shadow-only pair-aware caps.",
                )
            )

    uncertainty_buckets = list(uncertainty_summary.get("buckets", []))
    high_unc = [row for row in uncertainty_buckets if str(row.get("bucket")) in {"0.40_0.50", "0.50_0.75", "0.75_plus"}]
    if high_unc and sum(int(row.get("count", 0)) for row in high_unc) > 0:
        high_unc_pnl = sum(_safe_float(row.get("net_pnl_usd", 0.0), 0.0) for row in high_unc)
        if high_unc_pnl < 0.0:
            recs.append(
                TwinRecommendation(
                    category="uncertainty_handling",
                    severity="medium",
                    finding="Higher-uncertainty entries are underperforming.",
                    evidence=[
                        f"high-uncertainty bucket pnl={high_unc_pnl:.2f}",
                        f"uncertainty gate rejects={int(uncertainty_summary.get('uncertainty_gate_rejects', 0))}",
                    ],
                    proposed_change="Promote the uncertainty penalty analysis before widening any entry rescue logic and review whether Tier 2 pairs need a stricter uncertainty ceiling than Tier 1.",
                    validation_plan="Run the twin with a shadow-only stricter Tier 2 uncertainty cap and compare match drift plus expectancy by uncertainty bucket.",
                )
            )

    trades = int(aggregate.get("trades", 0))
    partial_exit_events = int(aggregate.get("partial_exit_events", 0))
    avg_holding_bars = _safe_float(aggregate.get("avg_holding_bars", 0.0), 0.0)
    pnl_after_partial = _safe_float(lifecycle_summary.get("pnl_after_partial_exit_trades_usd", 0.0), 0.0)
    if trades > 0 and partial_exit_events >= max(5, trades // 3) and avg_holding_bars <= 12.0 and pnl_after_partial <= 0.0:
        recs.append(
            TwinRecommendation(
                category="lifecycle_behavior",
                severity="high",
                finding="Lifecycle partial exits are too active relative to holding time and are not improving trade outcomes.",
                evidence=[
                    f"partial exit events={partial_exit_events}",
                    f"avg holding bars={avg_holding_bars:.2f}",
                    f"pnl after partial-exit trades={pnl_after_partial:.2f}",
                ],
                proposed_change="Increase lifecycle hysteresis or cooldown for repeated partial reductions before changing the entry stack again.",
                validation_plan="Run a shadow lifecycle pass with stricter partial action hysteresis and compare holding time, churn count, and net pnl by close reason.",
            )
        )

    degrading_pairs = [row for row in per_pair_records if int(row.get("trades", 0)) >= 5 and _safe_float(row.get("net_pnl_usd", 0.0), 0.0) < 0.0]
    if degrading_pairs:
        worst = sorted(degrading_pairs, key=lambda row: (_safe_float(row.get("net_pnl_usd", 0.0), 0.0), -int(row.get("trades", 0))))[:3]
        recs.append(
            TwinRecommendation(
                category="pair_selection",
                severity="medium",
                finding="Some active pairs are persistently negative on realized expectancy.",
                evidence=[f"worst pairs={[{'pair': row['pair'], 'net_pnl_usd': row['net_pnl_usd'], 'trades': row['trades']} for row in worst]}"],
                proposed_change="Quarantine the worst pairs in shadow analysis first and inspect pair-specific spread regime plus calibration drift before removing them from the live set.",
                validation_plan="Replay the twin without the worst pairs and compare portfolio return, drawdown, and slot utilization to the strict live mirror baseline.",
            )
        )

    slot_util = _safe_float(aggregate.get("slot_utilization_rate", 0.0), 0.0)
    shadow_ranked_out = int(aggregate.get("shadow_rejection_counts", {}).get("shadow_ranked_out", 0))
    if slot_util < 0.25 and shadow_ranked_out == 0 and int(aggregate.get("entries", 0)) == 0:
        recs.append(
            TwinRecommendation(
                category="portfolio_allocation",
                severity="low",
                finding="Portfolio ranking is not the current bottleneck; the system is not generating enough qualified entries.",
                evidence=[
                    f"slot utilization rate={slot_util:.3f}",
                    f"shadow ranked-out count={shadow_ranked_out}",
                    f"entries={int(aggregate.get('entries', 0))}",
                ],
                proposed_change="Prioritize entry-quality and spread/session analysis before spending more effort on allocation heuristics.",
                validation_plan="Track whether candidate_count and structure_rescue_count increase after entry-timing and spread-policy changes before revisiting allocation.",
            )
        )

    if not recs:
        recs.append(
            TwinRecommendation(
                category="summary",
                severity="low",
                finding="No single dominant pathology crossed the recommendation thresholds.",
                evidence=[
                    f"trades={trades}",
                    f"net_pnl_usd={_safe_float(aggregate.get('net_pnl_usd', 0.0), 0.0):.2f}",
                    f"max_drawdown_pct={_safe_float(aggregate.get('max_drawdown_pct', 0.0), 0.0):.2f}",
                ],
                proposed_change="Use the generated per-pair, session, uncertainty, and structure summaries to choose the next targeted shadow experiment rather than loosening global thresholds.",
                validation_plan="Review the richest negative cluster in the artifacts and run one isolated shadow perturbation against the strict twin baseline.",
            )
        )
    return recs


def _recommendations_markdown(recommendations: list[TwinRecommendation]) -> str:
    lines = ["# Digital Twin Improvements", ""]
    for idx, rec in enumerate(recommendations, start=1):
        lines.append(f"## {idx}. {rec.category} [{rec.severity}]")
        lines.append("")
        lines.append(f"Finding: {rec.finding}")
        lines.append("")
        lines.append("Evidence:")
        for item in rec.evidence:
            lines.append(f"- {item}")
        lines.append("")
        lines.append(f"Proposed change: {rec.proposed_change}")
        lines.append("")
        lines.append(f"Validation plan: {rec.validation_plan}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _clone_args(args: argparse.Namespace, **overrides: Any) -> argparse.Namespace:
    payload = vars(copy.deepcopy(args))
    payload.update(overrides)
    return argparse.Namespace(**payload)


def _adaptive_context_timeline(
    decision_frames: dict[str, pd.DataFrame],
    *,
    scoring_timeline: pd.Index,
    end_ts: pd.Timestamp,
    history_bars: int,
) -> pd.Index:
    """Return the common scoring timeline plus bounded pre-start context."""

    if len(scoring_timeline) == 0 or not decision_frames:
        return pd.Index([])
    common = next(iter(decision_frames.values())).index
    for frame in list(decision_frames.values())[1:]:
        common = common.intersection(frame.index)
    common = pd.Index(common.sort_values())
    common = common[common <= end_ts]
    if len(common) == 0:
        return pd.Index(scoring_timeline)
    first_score_pos = int(common.searchsorted(scoring_timeline[0], side="left"))
    context_start_pos = max(0, first_score_pos - max(1, int(history_bars)))
    return pd.Index(common[context_start_pos:])


def _causal_execution_timelines(
    timeline: pd.Index,
    *,
    fill_delay_bars: int,
) -> tuple[pd.Index, pd.Index]:
    delay = int(fill_delay_bars)
    if delay < 1:
        raise ValueError("fill_delay_bars must be at least 1 for causal replay")
    ordered = pd.Index(timeline).sort_values()
    if len(ordered) <= delay:
        raise RuntimeError("insufficient replay bars for causal fill delay")
    decision_timeline = pd.Index(ordered[:-delay])
    execution_timeline = pd.Index(ordered[delay:])
    if not bool(np.all(execution_timeline.to_numpy() > decision_timeline.to_numpy())):
        raise RuntimeError("causal replay requires every execution timestamp after its decision timestamp")
    return decision_timeline, execution_timeline


def _adaptive_context_diagnostics(
    *,
    context_timeline: pd.Index,
    scoring_timeline: pd.Index,
    history_bars: int,
) -> dict[str, Any]:
    requested_history = max(1, int(history_bars))
    scoring_start = scoring_timeline[0] if len(scoring_timeline) else None
    warmup_observations = int(
        sum(value < scoring_start for value in context_timeline)
        if scoring_start is not None
        else 0
    )
    return {
        "causal_normalization": True,
        "timeline_alignment": "common_pair_intersection",
        "requested_history_bars": requested_history,
        "context_observation_count": int(len(context_timeline)),
        "scoring_observation_count": int(len(scoring_timeline)),
        "warmup_observation_count": warmup_observations,
        "context_start_ts": "" if not len(context_timeline) else str(context_timeline[0]),
        "scoring_start_ts": "" if scoring_start is None else str(scoring_start),
        "scoring_end_ts": "" if not len(scoring_timeline) else str(scoring_timeline[-1]),
    }


def _adaptive_context_start_bound(
    start_bound: pd.Timestamp | None,
    *,
    timeframe: str,
    history_bars: int,
) -> pd.Timestamp | None:
    if start_bound is None:
        return None
    bar_horizon = timeframe_to_timedelta(timeframe) * max(1, int(history_bars))
    # Twice the nominal bar horizon covers ordinary market closures; the
    # seven-day floor ensures intraday replays starting after a weekend still
    # load enough prior observations for the causal normalizer.
    gap_safe_padding = max(bar_horizon * 2, pd.Timedelta(days=7))
    return start_bound - gap_safe_padding


# AGENT PARITY: Adaptive-vs-strict comparison is the main divergence artifact; prod does not emit this, so the twin remains the promotion yardstick.
def _adaptive_baseline_comparison_payload(adaptive_result: dict[str, Any], baseline_result: dict[str, Any]) -> dict[str, Any]:
    adaptive = dict(adaptive_result["aggregate"])
    baseline = dict(baseline_result["aggregate"])
    strict_metrics = {
        "entries": int(baseline.get("entries", 0) or 0),
        "trades": int(baseline.get("trades", 0) or 0),
        "net_pnl_usd": float(_safe_float(baseline.get("net_pnl_usd", 0.0), 0.0)),
        "profit_factor": float(_safe_float(baseline.get("profit_factor", 0.0), 0.0)),
        "max_drawdown_pct": float(_safe_float(baseline.get("max_drawdown_pct", 0.0), 0.0)),
        "slot_utilization_rate": float(_safe_float(baseline.get("slot_utilization_rate", 0.0), 0.0)),
        "avg_open_positions": float(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0)),
        "expectancy_per_trade": float(_safe_float(baseline.get("expectancy_per_trade_usd", 0.0), 0.0)),
    }
    adaptive_metrics = {
        "entries": int(adaptive.get("entries", 0) or 0),
        "trades": int(adaptive.get("trades", 0) or 0),
        "net_pnl_usd": float(_safe_float(adaptive.get("net_pnl_usd", 0.0), 0.0)),
        "profit_factor": float(_safe_float(adaptive.get("profit_factor", 0.0), 0.0)),
        "max_drawdown_pct": float(_safe_float(adaptive.get("max_drawdown_pct", 0.0), 0.0)),
        "slot_utilization_rate": float(_safe_float(adaptive.get("slot_utilization_rate", 0.0), 0.0)),
        "avg_open_positions": float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0)),
        "expectancy_per_trade": float(_safe_float(adaptive.get("expectancy_per_trade_usd", 0.0), 0.0)),
    }
    baseline_pairs = {str(row.get("pair")): dict(row) for row in baseline_result.get("per_pair_records", [])}
    adaptive_pairs = {str(row.get("pair")): dict(row) for row in adaptive_result.get("per_pair_records", [])}
    pair_deltas = []
    for pair in sorted(set(baseline_pairs) | set(adaptive_pairs)):
        base_row = baseline_pairs.get(pair, {})
        adapt_row = adaptive_pairs.get(pair, {})
        pair_deltas.append(
            {
                "pair": pair,
                "baseline_net_pnl_usd": float(_safe_float(base_row.get("net_pnl_usd", 0.0), 0.0)),
                "adaptive_net_pnl_usd": float(_safe_float(adapt_row.get("net_pnl_usd", 0.0), 0.0)),
                "delta_net_pnl_usd": float(_safe_float(adapt_row.get("net_pnl_usd", 0.0), 0.0) - _safe_float(base_row.get("net_pnl_usd", 0.0), 0.0)),
                "baseline_trades": int(base_row.get("trades", 0) or 0),
                "adaptive_trades": int(adapt_row.get("trades", 0) or 0),
            }
        )
    pair_deltas = sorted(pair_deltas, key=lambda row: row["delta_net_pnl_usd"], reverse=True)
    baseline_rejects = dict(baseline.get("rejection_counts", {}))
    adaptive_rejects = dict(adaptive.get("rejection_counts", {}))
    rejection_delta = []
    for reason in sorted(set(baseline_rejects) | set(adaptive_rejects)):
        rejection_delta.append(
            {
                "reason": reason,
                "baseline": int(baseline_rejects.get(reason, 0)),
                "adaptive": int(adaptive_rejects.get(reason, 0)),
                "delta": int(adaptive_rejects.get(reason, 0)) - int(baseline_rejects.get(reason, 0)),
            }
        )
    return {
        "strict_headline": baseline,
        "adaptive_headline": adaptive,
        "strict_metrics": strict_metrics,
        "adaptive_metrics": adaptive_metrics,
        "entry_count_ratio": float(adaptive.get("entries", 0) / max(1, baseline.get("entries", 0))),
        "entry_ratio": float(adaptive.get("entries", 0) / max(1, baseline.get("entries", 0))),
        "slot_utilization_ratio": float(_safe_float(adaptive.get("slot_utilization_rate", 0.0), 0.0) / max(_safe_float(baseline.get("slot_utilization_rate", 0.0), 0.0), 1e-9)),
        "avg_open_positions_ratio": float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0) / max(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0), 1e-9)),
        "average_open_positions_ratio": float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0) / max(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0), 1e-9)),
        "exposure_minutes_ratio": float((float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0)) * max(1, int(adaptive.get("decision_count", 0)))) / max((float(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0)) * max(1, int(baseline.get("decision_count", 0)))), 1e-9)),
        "partial_exit_trade_share_delta": float(_safe_float(adaptive_result.get("lifecycle_summary", {}).get("partial_exit_trade_share", 0.0), 0.0) - _safe_float(baseline_result.get("lifecycle_summary", {}).get("partial_exit_trade_share", 0.0), 0.0)),
        "win_rate_delta": float(_safe_float(adaptive.get("win_rate", 0.0), 0.0) - _safe_float(baseline.get("win_rate", 0.0), 0.0)),
        "profit_factor_delta": float(_safe_float(adaptive.get("profit_factor", 0.0), 0.0) - _safe_float(baseline.get("profit_factor", 0.0), 0.0)),
        "net_pnl_usd_delta": float(_safe_float(adaptive.get("net_pnl_usd", 0.0), 0.0) - _safe_float(baseline.get("net_pnl_usd", 0.0), 0.0)),
        "max_drawdown_delta": float(_safe_float(adaptive.get("max_drawdown_pct", 0.0), 0.0) - _safe_float(baseline.get("max_drawdown_pct", 0.0), 0.0)),
        "max_drawdown_pct_delta": float(_safe_float(adaptive.get("max_drawdown_pct", 0.0), 0.0) - _safe_float(baseline.get("max_drawdown_pct", 0.0), 0.0)),
        "expectancy_per_trade_delta": float(_safe_float(adaptive.get("expectancy_per_trade_usd", 0.0), 0.0) - _safe_float(baseline.get("expectancy_per_trade_usd", 0.0), 0.0)),
        "playbook_mix": dict(adaptive_result.get("playbook_summary", {})),
        "top_pair_deltas": pair_deltas[:10],
        "top_rejection_reason_deltas": sorted(rejection_delta, key=lambda row: abs(int(row["delta"])), reverse=True)[:10],
    }


# AGENT PARITY: Guardrails quantify whether adaptive behavior stayed aggressive enough relative to strict baseline exposure and entry tempo.
def _adaptive_guardrails_payload(args: argparse.Namespace, adaptive_result: dict[str, Any], baseline_result: dict[str, Any]) -> dict[str, Any]:
    adaptive = dict(adaptive_result["aggregate"])
    baseline = dict(baseline_result["aggregate"])
    entry_ratio = float(adaptive.get("entries", 0) / max(1, baseline.get("entries", 0)))
    slot_ratio = float(_safe_float(adaptive.get("slot_utilization_rate", 0.0), 0.0) / max(_safe_float(baseline.get("slot_utilization_rate", 0.0), 0.0), 1e-9))
    avg_open_ratio = float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0) / max(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0), 1e-9))
    exposure_ratio = float((float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0)) * max(1, int(adaptive.get("decision_count", 0)))) / max((float(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0)) * max(1, int(baseline.get("decision_count", 0)))), 1e-9))
    failures: list[str] = []
    if entry_ratio < float(getattr(args, "adaptive_entry_ratio_floor", 0.90)):
        failures.append("entry_ratio_below_floor")
    if entry_ratio > float(getattr(args, "adaptive_entry_ratio_cap", 1.35)):
        failures.append("entry_ratio_above_cap")
    if slot_ratio < float(getattr(args, "adaptive_slot_util_floor", 0.90)):
        failures.append("slot_utilization_ratio_below_floor")
    if slot_ratio > float(getattr(args, "adaptive_slot_util_cap", 1.20)):
        failures.append("slot_utilization_ratio_above_cap")
    if avg_open_ratio < 0.85:
        failures.append("avg_open_positions_ratio_below_floor")
    return {
        "baseline_entries": int(baseline.get("entries", 0)),
        "adaptive_entries": int(adaptive.get("entries", 0)),
        "entry_ratio": float(entry_ratio),
        "baseline_slot_utilization": float(_safe_float(baseline.get("slot_utilization_rate", 0.0), 0.0)),
        "adaptive_slot_utilization": float(_safe_float(adaptive.get("slot_utilization_rate", 0.0), 0.0)),
        "slot_utilization_ratio": float(slot_ratio),
        "baseline_avg_open_positions": float(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0)),
        "adaptive_avg_open_positions": float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0)),
        "avg_open_positions_ratio": float(avg_open_ratio),
        "baseline_exposure_minutes": float(_safe_float(baseline.get("avg_open_positions", 0.0), 0.0) * max(1, int(baseline.get("decision_count", 0)))),
        "adaptive_exposure_minutes": float(_safe_float(adaptive.get("avg_open_positions", 0.0), 0.0) * max(1, int(adaptive.get("decision_count", 0)))),
        "exposure_minutes_ratio": float(exposure_ratio),
        "guardrails_passed": bool(len(failures) == 0),
        "guardrail_failures": failures,
    }


def _sleeve_snapshot_for(
    snapshots: dict[str, Any],
    playbook: str,
) -> Any:
    sleeve = playbook_to_sleeve(playbook)
    return snapshots.get(sleeve)


def _allocator_open_position_from_action(
    *,
    action: dict[str, Any],
    position: TwinOpenPosition,
    protected_hold: bool,
    replaceable_hold: bool,
    exposure_crowding_burden: float = 0.0,
) -> AllocatorOpenPosition:
    sleeve = str(getattr(position, "sleeve", "") or playbook_to_sleeve(getattr(position, "playbook", "")))
    return AllocatorOpenPosition(
        position_id=str(action.get("pair") or position.pair),
        pair=str(position.pair),
        side=str(position.side),
        sleeve=str(sleeve),
        session_bucket=str(getattr(position, "entry_session_bucket", "")),
        keep_score=float(action.get("replacement_keep_score", 0.0)),
        age_bars=float(action.get("age_bars", 0.0)),
        protected_hold=bool(protected_hold),
        replaceable_hold=bool(replaceable_hold),
        thesis_id=str(getattr(position, "thesis_id", "") or action.get("thesis_id") or build_thesis_id(position.pair, position.side, sleeve)),
        campaign_seq=int(getattr(position, "campaign_seq", 0) or action.get("campaign_seq") or 0),
        campaign_entry_kind=str(getattr(position, "campaign_entry_kind", "") or action.get("campaign_entry_kind") or ""),
        campaign_state=str(getattr(position, "campaign_state", CAMPAIGN_STATE_INACTIVE) or action.get("campaign_state") or CAMPAIGN_STATE_INACTIVE),
        exposure_crowding_burden=float(exposure_crowding_burden),
        macro_coherence_decay=float(
            max(
                0.0,
                float(getattr(position, "entry_macro_coherence_score", 0.0))
                - float(action.get("entry_macro_coherence_score", getattr(position, "entry_macro_coherence_score", 0.0))),
            )
        ),
        rolling_profit_decay=float(max(0.0, -float(action.get("unrealized_pnl_usd", 0.0)))),
    )


# AGENT FLOW: `_run_twin_once` owns one replay pass: load inputs, attach adaptive context, simulate lifecycle/portfolio state, then emit artifacts and summaries.
def _run_twin_once(args: argparse.Namespace, *, baseline_result: dict[str, Any] | None = None) -> dict[str, Any]:
    s = get_settings()
    project_root = Path(s.project_root)
    feature_root = project_root / "data" / "raw"
    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = BASE._parse_pairs(args.pairs, s.pairs)
    provider = str(s.normalized_data_provider)
    intraday_timeframe = str(s.intraday_timeframe).upper()
    start_bound = pd.to_datetime(args.start_ts, utc=True) if str(args.start_ts or "").strip() else None
    end_bound = pd.to_datetime(args.end_ts, utc=True) if str(args.end_ts or "").strip() else None

    feature_store = BASE.ParquetStore(feature_root)
    model_sets = BASE._load_model_sets_from_manifest(pairs=pairs, project_root=project_root)
    manifest_info = _manifest_fingerprint(s, project_root, model_sets)
    adaptive_context_requested = bool(
        str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE) == ADAPTIVE_EXEC_MODE
        or any(getattr(model_sets.get(pair), "belief_model", None) is not None for pair in pairs)
    )
    context_start_bound = start_bound
    if context_start_bound is not None and adaptive_context_requested:
        history_bars = max(1, int(getattr(s, "adaptive_shadow_history_bars", 128) or 128))
        context_start_bound = _adaptive_context_start_bound(
            context_start_bound,
            timeframe=intraday_timeframe,
            history_bars=history_bars,
        )

    live_fetch = {"status": "disabled", "items": []}
    live_flat: dict[tuple[str, str], dict[str, Any]] = {}
    live_meta: dict[str, Any] = {"snapshot_count": 0, "decision_count": 0, "warnings": []}
    if bool(args.validate_live_overlap):
        live_fetch = _fetch_live_snapshots(bridge_url=str(args.bridge_url), api_key=str(args.live_api_key or s.bridge_api_key), limit=int(args.validation_limit))
        live_flat, live_meta = _flatten_live_snapshot_items(list(live_fetch.get("items") or []))

    decision_frames: dict[str, pd.DataFrame] = {}
    price_frames: dict[str, pd.DataFrame] = {}
    lifecycle_columns: dict[str, list[str]] = {}
    for pair in pairs:
        print(f"[twin] precompute pair={pair}", flush=True)
        decisions, prices, life_cols = _prepare_twin_pair_data(
            pair=pair,
            loaded=model_sets[pair],
            feature_store=feature_store,
            provider=provider,
            intraday_timeframe=intraday_timeframe,
            all_pairs=pairs,
            start_ts=context_start_bound,
            end_ts=end_bound,
            settings=s,
            args=args,
        )
        decision_frames[pair] = decisions
        price_frames[pair] = prices.set_index("ts")
        lifecycle_columns[pair] = life_cols

    start_ts = max(df.index.min() for df in decision_frames.values())
    end_ts = min(df.index.max() for df in decision_frames.values())
    if str(args.start_ts or "").strip():
        start_ts = max(start_ts, pd.to_datetime(args.start_ts, utc=True))
    if str(args.end_ts or "").strip():
        end_ts = min(end_ts, pd.to_datetime(args.end_ts, utc=True))
    if start_ts >= end_ts:
        raise RuntimeError("invalid backtest range after overlap trim")

    timeline = decision_frames[pairs[0]].loc[
        (decision_frames[pairs[0]].index >= start_ts)
        & (decision_frames[pairs[0]].index <= end_ts)
    ].index
    for pair in pairs[1:]:
        pair_scoring_index = decision_frames[pair].loc[
            (decision_frames[pair].index >= start_ts)
            & (decision_frames[pair].index <= end_ts)
        ].index
        timeline = timeline.intersection(pair_scoring_index)
    timeline = pd.Index(timeline.sort_values())
    if len(timeline) == 0:
        raise RuntimeError("no common timestamps across selected pairs")
    for pair in pairs:
        price_frames[pair] = price_frames[pair].loc[
            (price_frames[pair].index >= start_ts) & (price_frames[pair].index <= end_ts)
        ]

    adaptive_enabled = str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE) == ADAPTIVE_EXEC_MODE
    belief_enabled = any(getattr(model_sets.get(pair), "belief_model", None) is not None for pair in pairs)
    belief_overlay_enabled = bool(getattr(args, "belief_overlay", True))
    adaptive_context_meta: dict[str, Any] = {}
    if adaptive_enabled or belief_enabled:
        context_timeline = _adaptive_context_timeline(
            decision_frames,
            scoring_timeline=timeline,
            end_ts=end_ts,
            history_bars=max(1, int(getattr(s, "adaptive_shadow_history_bars", 128) or 128)),
        )
        for pair in pairs:
            decision_frames[pair] = decision_frames[pair].reindex(context_timeline).copy()
        adaptive_context_meta = attach_adaptive_context(
            decision_frames,
            pairs=list(pairs),
            settings=s,
            enabled_playbooks=parse_enabled_playbooks(getattr(args, "adaptive_playbooks", None)),
        )
        adaptive_context_meta.update(
            _adaptive_context_diagnostics(
                context_timeline=context_timeline,
                scoring_timeline=timeline,
                history_bars=max(1, int(getattr(s, "adaptive_shadow_history_bars", 128) or 128)),
            )
        )
    else:
        for pair in pairs:
            decision_frames[pair] = decision_frames[pair].reindex(timeline).copy()
    baseline_entry_cumulative_by_ts = dict((baseline_result or {}).get("entry_cumulative_by_ts") or {}) if adaptive_enabled else {}

    fill_delay_bars = int(getattr(args, "fill_delay_bars", 1))
    decision_timeline, execution_timeline = _causal_execution_timelines(
        timeline,
        fill_delay_bars=fill_delay_bars,
    )
    decision_arrays: dict[str, dict[str, np.ndarray]] = {}
    bid_arrays: dict[str, np.ndarray] = {}
    ask_arrays: dict[str, np.ndarray] = {}
    mid_arrays: dict[str, np.ndarray] = {}
    for pair in pairs:
        frame = decision_frames[pair].reindex(decision_timeline)
        decision_arrays[pair] = {col: frame[col].to_numpy() for col in frame.columns}
        prices = price_frames[pair].reindex(execution_timeline).ffill()
        bid_arrays[pair] = prices["bid_close"].to_numpy(dtype=float)
        ask_arrays[pair] = prices["ask_close"].to_numpy(dtype=float)
        mid_arrays[pair] = prices["mid_close"].to_numpy(dtype=float)
        del decision_frames[pair]
        del price_frames[pair]

    lifecycle_cache = BASE.LifecycleFrameCache(
        feature_store=feature_store,
        provider=provider,
        timeframe=intraday_timeframe,
        column_map=lifecycle_columns,
        timeline=decision_timeline,
        max_pairs=max(6, int(args.lifecycle_cache_pairs)),
    )
    timeline = execution_timeline
    start_ts = timeline[0]
    end_ts = timeline[-1]

    collector = DecisionMetricsCollector(
        max_history_rows=int(args.max_decision_history_rows),
        emit_history=bool(args.emit_decision_history or belief_enabled),
    )
    collector.set_validation_keys(set(live_flat.keys()))
    allocator_config = allocator_config_from_settings(s)
    campaign_config = campaign_config_from_settings(s)
    campaign_config.enabled = bool(adaptive_enabled)
    campaign_registry: dict[str, CampaignRegistryEntry] = {}
    campaign_events: list[dict[str, Any]] = []
    belief_hypothesis_rows: list[dict[str, Any]] = []
    sleeve_tracker = SleeveGovernanceTracker(
        sleeves=[
            playbook_to_sleeve(PLAYBOOK_TREND_PULLBACK),
            playbook_to_sleeve(PLAYBOOK_RANGE_MEAN_REVERSION),
            playbook_to_sleeve(PLAYBOOK_BREAKOUT_EXPANSION),
            playbook_to_sleeve(PLAYBOOK_FAILED_BREAKOUT_REVERSAL),
        ]
        + [playbook_to_sleeve(PLAYBOOK_NO_TRADE)]
    )

    cash_balance = float(args.start_equity)
    equity_curve: list[dict[str, Any]] = []
    open_positions: dict[str, TwinOpenPosition] = {}
    recent_exit_registry: dict[str, dict[str, Any]] = {}
    closed_trades: list[TwinClosedTrade] = []
    rejection_counts: Counter[str] = Counter()
    entry_count = 0
    entry_events_by_ts: Counter[str] = Counter()
    entry_cumulative_by_ts: dict[str, int] = {}
    partial_exit_count = 0
    reversal_exit_count = 0
    action_counts: Counter[str] = Counter()
    close_reason_counts: Counter[str] = Counter()
    pnl_by_close_reason: Counter[str] = Counter()
    exposure_samples = 0
    open_position_total = 0
    peak_open_positions = 0
    holding_bar_secs = max(1, int(BASE._timeframe_to_seconds(intraday_timeframe) or 300))
    threshold_snapshot = _threshold_snapshot(s)

    timeline_total = int(len(timeline))
    for idx, ts in enumerate(timeline, start=1):
        if idx == 1 or idx % 5000 == 0 or idx == timeline_total:
            print(f"[twin] simulate bars={idx}/{timeline_total} open_positions={len(open_positions)}", flush=True)
        ts_dt = pd.Timestamp(ts).tz_convert("UTC") if pd.Timestamp(ts).tzinfo else pd.Timestamp(ts, tz="UTC")
        ts_str = str(ts_dt)
        bar_idx = idx - 1
        decision_source_ts = str(decision_timeline[bar_idx])
        baseline_entries_so_far = int(_safe_int(baseline_entry_cumulative_by_ts.get(ts_str), 0)) if adaptive_enabled else 0
        tempo_gap_active = bool(
            adaptive_enabled
            and adaptive_tempo_gap_active(
                baseline_entries_so_far=baseline_entries_so_far,
                adaptive_entries_so_far=entry_count,
            )
        )
        current_equity = BASE._mark_equity(
            cash_balance=cash_balance,
            open_positions=open_positions,
            bar_idx=bar_idx,
            bid_arrays=bid_arrays,
            ask_arrays=ask_arrays,
            mid_arrays=mid_arrays,
        )
        sleeve_health_snapshots = sleeve_tracker.snapshot() if adaptive_enabled else {}
        positions_snapshot = dict(open_positions)
        total_count_snapshot = len(positions_snapshot)
        shadow_inputs_for_bar: list[dict[str, Any]] = []
        collector_rows_for_bar: list[dict[str, Any]] = []
        pending_actions: list[dict[str, Any]] = []

        for pair in pairs:
            signal_row = decision_arrays[pair]
            loaded = model_sets[pair]
            pos_snapshot = positions_snapshot.get(pair)
            live_pos = open_positions.get(pair)
            pair_count = 1 if pos_snapshot is not None else 0
            total_count = int(total_count_snapshot)
            gate_allowed = bool(signal_row["allowed"][bar_idx])
            gate_reason = str(signal_row["rejection_reason"][bar_idx])
            session_blocked = bool(signal_row["session_entry_blocked"][bar_idx])
            session_block_reason = str(signal_row["session_entry_block_reason"][bar_idx])
            strict_decision_reasons: list[str] = []
            if pos_snapshot is None and session_blocked:
                strict_decision_reasons.append(session_block_reason or f"session_blocked:{signal_row['session_bucket'][bar_idx]}")
            if not gate_allowed:
                strict_decision_reasons.append(gate_reason)
            if pair_count >= int(s.max_pair_positions):
                strict_decision_reasons.append("pair_exposure_cap")
            if total_count >= int(s.max_total_positions):
                strict_decision_reasons.append("portfolio_exposure_cap")
            strict_decision_reasons = list(dict.fromkeys([str(x) for x in strict_decision_reasons if str(x)]))
            strict_ready = len(strict_decision_reasons) == 0
            decision_reasons = list(strict_decision_reasons)
            ready = bool(strict_ready)
            side = str(signal_row["side"][bar_idx])
            desired_side = "long" if side == "BUY" else "short"
            pos_side = str(pos_snapshot.side) if pos_snapshot is not None else "flat"
            context_enabled = "playbook" in signal_row
            current_playbook = str(signal_row["playbook"][bar_idx]) if context_enabled else PLAYBOOK_NO_TRADE
            current_sleeve = playbook_to_sleeve(current_playbook)
            current_sleeve_snapshot = _sleeve_snapshot_for(sleeve_health_snapshots, current_playbook)
            adaptive_fields = {
                "environment_state": str(signal_row["environment_state"][bar_idx]) if context_enabled and "environment_state" in signal_row else "",
                "trend_persistence_score": float(signal_row["trend_persistence_score"][bar_idx]) if context_enabled and "trend_persistence_score" in signal_row else 0.0,
                "compression_score": float(signal_row["compression_score"][bar_idx]) if context_enabled and "compression_score" in signal_row else 0.0,
                "expansion_score": float(signal_row["expansion_score"][bar_idx]) if context_enabled and "expansion_score" in signal_row else 0.0,
                "range_score": float(signal_row["range_score"][bar_idx]) if context_enabled and "range_score" in signal_row else 0.0,
                "hostility_score": float(signal_row["hostility_score"][bar_idx]) if context_enabled and "hostility_score" in signal_row else 0.0,
                "macro_coherence_score": float(signal_row["macro_coherence_score"][bar_idx]) if context_enabled and "macro_coherence_score" in signal_row else 0.0,
                "pair_strength_score": float(signal_row["pair_strength_score"][bar_idx]) if context_enabled and "pair_strength_score" in signal_row else 0.0,
                "playbook": str(current_playbook),
                "sleeve": str(current_sleeve),
                "playbook_score": float(signal_row["playbook_score"][bar_idx]) if context_enabled and "playbook_score" in signal_row else 0.0,
                "location_score": float(signal_row["location_score"][bar_idx]) if context_enabled and "location_score" in signal_row else 0.0,
                "trigger_score": float(signal_row["trigger_score"][bar_idx]) if context_enabled and "trigger_score" in signal_row else 0.0,
                "adaptive_entry_quality": 0.0,
                "thesis_id": build_thesis_id(pair, desired_side, current_sleeve),
                "campaign_seq": 0,
                "campaign_entry_kind": "",
                "campaign_state": CAMPAIGN_STATE_INACTIVE,
                "campaign_state_reason": "",
                "campaign_proof_score": 0.0,
                "campaign_maturity_score": 0.0,
                "campaign_reset_quality": 0.0,
                "campaign_priority_boost": 0.0,
                "campaign_reentry_blocked": False,
                "currency_crowding_penalty": 0.0,
                "playbook_diversification_penalty": 0.0,
                "allocator_score": 0.0,
                "allocator_rank": None,
                "allocator_selected": False,
                "allocator_rejection_reason": "",
                "replacement_value": 0.0,
                "sleeve_health_score": float(getattr(current_sleeve_snapshot, "score", 0.5)),
                "sleeve_health_state": str(getattr(current_sleeve_snapshot, "state", "healthy")),
                "aggressive_fallback_used": False,
                "adaptive_allowed": False,
                "adaptive_rejection_reason": "",
                **empty_directional_belief(pair=pair, ts=ts_str, source_mode="disabled").to_dict(),
            }
            adaptive_eval: dict[str, Any] = {}
            hard_reasons: list[str] = []
            if adaptive_enabled:
                if pos_snapshot is None and session_blocked:
                    hard_reasons.append(session_block_reason or f"session_blocked:{signal_row['session_bucket'][bar_idx]}")
                if gate_reason == "spread_too_wide":
                    hard_reasons.append("spread_too_wide")
                if pair_count >= int(s.max_pair_positions):
                    hard_reasons.append("pair_exposure_cap")
                if total_count >= int(s.max_total_positions):
                    hard_reasons.append("portfolio_exposure_cap")
                hard_reasons = list(dict.fromkeys([str(x) for x in hard_reasons if str(x)]))
                adaptive_eval = evaluate_adaptive_entry(
                    row={
                        "pair": pair,
                        "side": desired_side,
                        "signal_side": desired_side,
                        "baseline_rejection_reason": gate_reason if not gate_allowed else "none",
                        "session_bucket": str(signal_row["session_bucket"][bar_idx]),
                        "session_entry_blocked": bool(signal_row["session_entry_blocked"][bar_idx]),
                        "session_entry_block_reason": str(signal_row["session_entry_block_reason"][bar_idx]),
                        "spread_bps": float(signal_row["spread_bps"][bar_idx]),
                        "uncertainty_score": float(signal_row["uncertainty_score"][bar_idx]),
                        "playbook": adaptive_fields["playbook"],
                        "playbook_score": adaptive_fields["playbook_score"],
                        "location_score": adaptive_fields["location_score"],
                        "trigger_score": adaptive_fields["trigger_score"],
                        "macro_coherence_score": adaptive_fields["macro_coherence_score"],
                        "environment_state": adaptive_fields["environment_state"],
                        "extreme_chase": bool(signal_row["extreme_chase"][bar_idx]) if "extreme_chase" in signal_row else False,
                        "adaptive_base_rejection_reason": str(signal_row["adaptive_base_rejection_reason"][bar_idx]) if "adaptive_base_rejection_reason" in signal_row else "approved",
                        "calibrated_ev_bps_shadow": float(signal_row["calibrated_ev_bps_shadow"][bar_idx]),
                    },
                    strict_ready=bool(strict_ready),
                    open_positions=open_positions,
                    settings=s,
                    fallback_margin=float(getattr(args, "adaptive_aggressive_fallback_margin", 0.08)),
                )
                if bool(adaptive_eval.get("adaptive_allowed")) and pos_snapshot is None:
                    campaign_candidate = evaluate_entry_campaign_memory(
                        pair=pair,
                        side=desired_side,
                        sleeve=playbook_to_sleeve(str(adaptive_eval.get("playbook") or adaptive_fields["playbook"])),
                        row={
                            "playbook_score": adaptive_fields["playbook_score"],
                            "location_score": adaptive_fields["location_score"],
                            "trigger_score": adaptive_fields["trigger_score"],
                            "macro_coherence_score": adaptive_fields["macro_coherence_score"],
                            "hostility_score": adaptive_fields["hostility_score"],
                            "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                            "environment_state": adaptive_fields["environment_state"],
                            "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                        },
                        bar_idx=int(bar_idx),
                        ts=str(ts_str),
                        registry=campaign_registry,
                        config=campaign_config,
                    )
                    reentry_eval = adaptive_reentry_block(
                        pair=pair,
                        side=desired_side,
                        playbook=str(adaptive_eval.get("playbook") or adaptive_fields["playbook"]),
                        bar_idx=int(bar_idx),
                        exit_registry=recent_exit_registry,
                        cooldown_scale=campaign_cooldown_scale(campaign_candidate.state, campaign_config),
                    )
                    if bool(reentry_eval.get("blocked")):
                        adaptive_eval["adaptive_allowed"] = False
                        adaptive_eval["adaptive_rejection_reason"] = str(reentry_eval.get("reason") or "adaptive_reentry_cooldown")
                    if bool(campaign_candidate.reentry_blocked):
                        adaptive_eval["adaptive_allowed"] = False
                        adaptive_eval["adaptive_rejection_reason"] = str(campaign_candidate.reentry_block_reason or "campaign_abandon_cooldown")
                adaptive_fields.update(adaptive_eval)
                adaptive_fields["sleeve"] = playbook_to_sleeve(str(adaptive_fields["playbook"]))
                campaign_candidate = evaluate_entry_campaign_memory(
                    pair=pair,
                    side=desired_side,
                    sleeve=str(adaptive_fields["sleeve"]),
                    row={
                        "playbook_score": adaptive_fields["playbook_score"],
                        "location_score": adaptive_fields["location_score"],
                        "trigger_score": adaptive_fields["trigger_score"],
                        "macro_coherence_score": adaptive_fields["macro_coherence_score"],
                        "hostility_score": adaptive_fields["hostility_score"],
                        "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                        "environment_state": adaptive_fields["environment_state"],
                        "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                    },
                    bar_idx=int(bar_idx),
                    ts=str(ts_str),
                    registry=campaign_registry,
                    config=campaign_config,
                )
                adaptive_fields["thesis_id"] = str(campaign_candidate.thesis_id)
                adaptive_fields["campaign_seq"] = int(campaign_candidate.campaign_seq)
                adaptive_fields["campaign_entry_kind"] = str(campaign_candidate.entry_kind)
                adaptive_fields["campaign_state"] = str(campaign_candidate.state)
                adaptive_fields["campaign_state_reason"] = str(campaign_candidate.state_reason)
                adaptive_fields["campaign_proof_score"] = float(campaign_candidate.proof_score)
                adaptive_fields["campaign_maturity_score"] = float(campaign_candidate.maturity_score)
                adaptive_fields["campaign_reset_quality"] = float(campaign_candidate.reset_quality)
                adaptive_fields["campaign_priority_boost"] = float(campaign_candidate.priority_boost)
                adaptive_fields["campaign_reentry_blocked"] = bool(campaign_candidate.reentry_blocked)
                current_sleeve_snapshot = _sleeve_snapshot_for(sleeve_health_snapshots, str(adaptive_fields["playbook"]))
                adaptive_fields["sleeve_health_score"] = float(getattr(current_sleeve_snapshot, "score", 0.5))
                adaptive_fields["sleeve_health_state"] = str(getattr(current_sleeve_snapshot, "state", "healthy"))
                decision_reasons = list(hard_reasons)
                if pos_snapshot is None:
                    ready = bool(adaptive_eval["adaptive_allowed"]) and len(hard_reasons) == 0
                    if not ready:
                        reason = str(hard_reasons[0]) if hard_reasons else str(adaptive_eval["adaptive_rejection_reason"] or "adaptive_rejected")
                        if reason:
                            decision_reasons = [reason]
                else:
                    ready = False
                    if "pair_exposure_cap" not in decision_reasons:
                        decision_reasons = ["pair_exposure_cap"]
            belief_meta = {
                "pair": str(pair),
                "ts": str(ts_str),
                "adaptive_environment_state": str(adaptive_fields["environment_state"]),
                "adaptive_playbook": str(adaptive_fields["playbook"]),
                "playbook_score": float(adaptive_fields["playbook_score"]),
                "location_score": float(adaptive_fields["location_score"]),
                "trigger_score": float(adaptive_fields["trigger_score"]),
                "macro_coherence_score": float(adaptive_fields["macro_coherence_score"]),
                "hostility_score": float(adaptive_fields["hostility_score"]),
                "uncertainty_score": float(signal_row["uncertainty_score"][bar_idx]),
                "model_disagreement_score": float(signal_row["model_disagreement_score"][bar_idx]),
                "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                "scenario_bucket": str(signal_row["scenario_bucket"][bar_idx]) if "scenario_bucket" in signal_row else "",
                "regime_bucket": str(signal_row["regime_bucket"][bar_idx]) if "regime_bucket" in signal_row else "",
            }
            belief = empty_directional_belief(pair=pair, ts=ts_str, source_mode="disabled")
            if getattr(loaded, "belief_model", None) is not None:
                belief = compute_directional_belief(
                    row={
                        **belief_meta,
                        **{key: signal_row[key][bar_idx] for key in signal_row.keys()},
                    },
                    signal=_belief_signal_proxy(pair=pair, ts=ts_str, side=side, signal_row=signal_row, bar_idx=bar_idx),
                    adaptive_meta=belief_meta,
                    model_set=getattr(loaded, "belief_model", None),
                )
            if belief.hypotheses:
                for idx_h, hypothesis in enumerate(list(belief.hypotheses)):
                    belief_hypothesis_rows.append(
                        {
                            "pair": str(pair),
                            "ts": str(ts_str),
                            "bar_index": int(bar_idx),
                            "query_rank": int(idx_h + 1),
                            "position_count_pair": int(pair_count),
                            "signal_side": str(side),
                            "entry_side": str(desired_side),
                            "belief_primary_scenario": str(belief.primary_scenario),
                            "belief_primary_side": str(belief.primary_side),
                            "belief_model_version": str(belief.model_version),
                            "belief_source_mode": str(belief.source_mode),
                            **{str(k): v for k, v in dict(hypothesis).items()},
                        }
                    )
            adaptive_fields.update(belief.to_dict())
            reversal_blocking_reasons = _reversal_blocking_reasons(decision_reasons)
            reversal_context_active = desired_side != "flat" and pos_side != "flat" and desired_side != pos_side
            lifecycle_action = "hold"
            lifecycle_reason = "hold"
            exit_action_selected = "hold"
            exit_action_score = 0.0
            exit_action_probs: dict[str, float] = {}
            reversal_failure_prob = 0.0
            reversal_opportunity_prob = 0.0
            close_lots = 0.0
            reversal_ready = False
            unrealized_pnl = 0.0
            age_bars = 0.0
            campaign_keep_adjustment = 0.0

            if pos_snapshot is not None and bool(s.enable_lifecycle_actions):
                life_entry = lifecycle_cache.get(pair)
                if bar_idx < len(life_entry.matrix):
                    lifecycle_row = life_entry.matrix[bar_idx].copy()
                    time_idx = life_entry.col_index.get("time_in_trade_bars")
                    if time_idx is not None:
                        lifecycle_row[time_idx] = max(0.0, (ts_dt.timestamp() - _to_utc_ts(pos_snapshot.open_ts).timestamp()) / float(holding_bar_secs))
                    count_idx = life_entry.col_index.get("open_position_count")
                    if count_idx is not None:
                        lifecycle_row[count_idx] = float(total_count)
                    if loaded.exit_model is not None:
                        exit_diag = BASE._score_exit_policy_model_fast(
                            loaded.exit_model,
                            lifecycle_row,
                            life_entry,
                            action_labels=loaded.exit_action_labels,
                        )
                        exit_action_selected = str(exit_diag.get("selected") or "hold")
                        exit_action_score = float(exit_diag.get("score") or 0.0)
                        exit_action_probs = {str(k): float(v) for k, v in dict(exit_diag.get("probs") or {}).items()}
                    if loaded.reversal_failure_model is not None:
                        reversal_failure_prob = BASE._score_binary_lifecycle_model_fast(loaded.reversal_failure_model, lifecycle_row, life_entry)
                    if loaded.reversal_opportunity_model is not None:
                        reversal_opportunity_prob = BASE._score_binary_lifecycle_model_fast(loaded.reversal_opportunity_model, lifecycle_row, life_entry)

                if reversal_context_active and loaded.has_reversal_models:
                    if float(reversal_failure_prob) < float(s.reversal_failure_min_prob):
                        reversal_blocking_reasons.append("reversal_failure_below_threshold")
                    if float(reversal_opportunity_prob) < float(s.reversal_opportunity_min_prob):
                        reversal_blocking_reasons.append("reversal_opportunity_below_threshold")
                reversal_blocking_reasons = list(dict.fromkeys(reversal_blocking_reasons))
                reversal_ready = (
                    reversal_context_active
                    and gate_allowed
                    and len(reversal_blocking_reasons) == 0
                    and (
                        not loaded.has_reversal_models
                        or (
                            float(reversal_failure_prob) >= float(s.reversal_failure_min_prob)
                            and float(reversal_opportunity_prob) >= float(s.reversal_opportunity_min_prob)
                        )
                    )
                )

                if reversal_ready:
                    lifecycle_action = "exit"
                    lifecycle_reason = "reversal_models_exit"
                elif loaded.has_exit_model and str(exit_action_selected) in {"partial_tp", "exit"} and float(exit_action_score) >= float(s.lifecycle_model_action_min_prob):
                    if str(exit_action_selected) == "partial_tp":
                        lifecycle_action, close_lots = BASE._partial_close_plan(
                            lots_open=float(pos_snapshot.lots),
                            fraction=float(s.partial_close_fraction),
                            settings=s,
                        )
                        if lifecycle_action in {"partial_tp", "exit"} and close_lots > 0.0:
                            lifecycle_reason = "exit_model_partial_tp" if lifecycle_action == "partial_tp" else "exit_model_reduce_to_flat"
                    else:
                        lifecycle_action = "exit"
                        lifecycle_reason = "exit_model_exit"
                elif not loaded.has_exit_model and float(signal_row["trade_prob"][bar_idx]) < float(s.min_trade_prob * 0.8):
                    lifecycle_action, close_lots = BASE._partial_close_plan(
                        lots_open=float(pos_snapshot.lots),
                        fraction=float(s.partial_close_fraction),
                        settings=s,
                    )
                    if lifecycle_action in {"partial_tp", "exit"} and close_lots > 0.0:
                        lifecycle_reason = "exit_model_reduce" if lifecycle_action == "partial_tp" else "exit_model_reduce_to_flat"
                else:
                    lifecycle_reason = "position_open_hold"
            elif pos_snapshot is not None:
                lifecycle_reason = "position_open_hold"

            if adaptive_enabled and pos_snapshot is not None:
                baseline_lifecycle_action = str(lifecycle_action)
                baseline_lifecycle_reason = str(lifecycle_reason)
                baseline_close_lots = float(close_lots)
                if str(pos_snapshot.side) == "long":
                    mark_exit_price = float(bid_arrays[pair][bar_idx])
                else:
                    mark_exit_price = float(ask_arrays[pair][bar_idx])
                unrealized_pnl = BASE._realized_pnl_usd(
                    pair=pair,
                    side=str(pos_snapshot.side),
                    entry_price=float(pos_snapshot.entry_price),
                    exit_price=float(mark_exit_price),
                    lots=float(pos_snapshot.lots),
                    bar_idx=bar_idx,
                    mid_arrays=mid_arrays,
                )
                age_bars = max(1.0, float((ts_dt.timestamp() - _to_utc_ts(pos_snapshot.open_ts).timestamp()) / float(holding_bar_secs)))
                adaptive_lifecycle = adaptive_lifecycle_decision(
                    position=pos_snapshot,
                    row={
                        "playbook": adaptive_fields["playbook"],
                        "playbook_score": adaptive_fields["playbook_score"],
                        "location_score": adaptive_fields["location_score"],
                        "trigger_score": adaptive_fields["trigger_score"],
                        "hostility_score": adaptive_fields["hostility_score"],
                        "macro_coherence_score": adaptive_fields["macro_coherence_score"],
                        "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                        "environment_state": adaptive_fields["environment_state"],
                    },
                    unrealized_pnl_usd=float(unrealized_pnl),
                    age_bars=float(age_bars),
                    bar_idx=bar_idx,
                    exit_action_probs=exit_action_probs,
                    reversal_context_active=bool(reversal_context_active),
                    reversal_ready=bool(reversal_ready),
                    reversal_failure_prob=float(reversal_failure_prob),
                    reversal_opportunity_prob=float(reversal_opportunity_prob),
                )
                lifecycle_action = str(adaptive_lifecycle["action"])
                lifecycle_reason = str(adaptive_lifecycle["reason"])
                close_lots = 0.0
                if lifecycle_action == "partial_tp":
                    lifecycle_action, close_lots = BASE._partial_close_plan(
                        lots_open=float(pos_snapshot.lots),
                        fraction=float(s.partial_close_fraction),
                        settings=s,
                    )
                    if lifecycle_action not in {"partial_tp", "exit"} or close_lots <= 0.0:
                        lifecycle_action = "hold"
                        lifecycle_reason = "adaptive_hold"
                        close_lots = 0.0
                prior_campaign_state = str(getattr(pos_snapshot, "campaign_state", CAMPAIGN_STATE_PROBE) or CAMPAIGN_STATE_PROBE)
                campaign_open = evaluate_open_campaign(
                    pair=pair,
                    side=str(pos_snapshot.side),
                    sleeve=str(getattr(pos_snapshot, "sleeve", playbook_to_sleeve(getattr(pos_snapshot, "playbook", "")))),
                    current_state=prior_campaign_state,
                    row={
                        "playbook_score": adaptive_fields["playbook_score"],
                        "location_score": adaptive_fields["location_score"],
                        "trigger_score": adaptive_fields["trigger_score"],
                        "macro_coherence_score": adaptive_fields["macro_coherence_score"],
                        "hostility_score": adaptive_fields["hostility_score"],
                        "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                        "environment_state": adaptive_fields["environment_state"],
                    },
                    unrealized_pnl_usd=float(unrealized_pnl),
                    age_bars=float(age_bars),
                    open_equity_usd=float(getattr(pos_snapshot, "open_equity_usd", current_equity)),
                    bar_idx=int(bar_idx),
                    ts=str(ts_str),
                    lifecycle_action=str(lifecycle_action),
                    lifecycle_reason=str(lifecycle_reason),
                    reversal_ready=bool(reversal_ready),
                    severe_invalidation=bool(lifecycle_reason in {"adaptive_breakout_follow_through_failed", "adaptive_failed_breakout_invalidated", "adaptive_reverse_ready"}),
                    config=campaign_config,
                    campaign_seq=int(getattr(pos_snapshot, "campaign_seq", 0) or 0),
                    entry_kind=str(getattr(pos_snapshot, "campaign_entry_kind", "") or ""),
                )
                adaptive_fields["thesis_id"] = str(campaign_open.thesis_id)
                adaptive_fields["campaign_seq"] = int(campaign_open.campaign_seq)
                adaptive_fields["campaign_entry_kind"] = str(campaign_open.entry_kind)
                adaptive_fields["campaign_state"] = str(campaign_open.state)
                adaptive_fields["campaign_state_reason"] = str(campaign_open.state_reason)
                adaptive_fields["campaign_proof_score"] = float(campaign_open.proof_score)
                adaptive_fields["campaign_maturity_score"] = float(campaign_open.maturity_score)
                adaptive_fields["campaign_reset_quality"] = float(campaign_open.reset_quality)
                adaptive_fields["campaign_priority_boost"] = float(campaign_open.priority_boost)
                adaptive_fields["campaign_reentry_blocked"] = bool(campaign_open.reentry_blocked)
                campaign_keep_adjustment = float(campaign_open.keep_adjustment)
                campaign_override = apply_campaign_lifecycle_overrides(
                    snapshot=campaign_open,
                    lifecycle_action=str(lifecycle_action),
                    lifecycle_reason=str(lifecycle_reason),
                    unrealized_pnl_usd=float(unrealized_pnl),
                    severe_invalidation=bool(campaign_open.state == CAMPAIGN_STATE_ABANDONED),
                )
                lifecycle_action = str(campaign_override["lifecycle_action"])
                lifecycle_reason = str(campaign_override["lifecycle_reason"])
                transition = campaign_transition_if_changed(
                    prior_state=prior_campaign_state,
                    snapshot=campaign_open,
                    bar_idx=int(bar_idx),
                    ts=str(ts_str),
                    unrealized_pnl_usd=float(unrealized_pnl),
                    holding_bars=float(age_bars),
                )
                if transition is not None:
                    campaign_events.append(asdict(transition))
                apply_campaign_registry_snapshot(
                    campaign_registry,
                    snapshot=campaign_open,
                    bar_idx=int(bar_idx),
                    ts=str(ts_str),
                    active_position=True,
                )
                pos_snapshot.thesis_id = str(campaign_open.thesis_id)
                pos_snapshot.campaign_seq = int(campaign_open.campaign_seq)
                pos_snapshot.campaign_entry_kind = str(campaign_open.entry_kind)
                pos_snapshot.campaign_state = str(campaign_open.state)
                pos_snapshot.campaign_state_reason = str(campaign_open.state_reason)
                pos_snapshot.campaign_state_entered_bar = int(bar_idx) if transition is not None else int(getattr(pos_snapshot, "campaign_state_entered_bar", bar_idx))
                severe_adaptive_exit = lifecycle_reason in {
                    "adaptive_breakout_follow_through_failed",
                    "adaptive_failed_breakout_invalidated",
                    "adaptive_reverse_ready",
                    "adaptive_campaign_probe_failed",
                }
                tempo_rotation_release = bool(
                    tempo_gap_active
                    and age_bars >= 12.0
                    and lifecycle_action in {"partial_tp", "exit"}
                    and (not severe_adaptive_exit)
                    and (
                        str(getattr(pos_snapshot, "playbook", PLAYBOOK_TREND_PULLBACK))
                        in {PLAYBOOK_RANGE_MEAN_REVERSION, PLAYBOOK_BREAKOUT_EXPANSION}
                        or float(adaptive_replacement_keep_score(
                            lifecycle_action=str(lifecycle_action),
                            lifecycle_reason=str(lifecycle_reason),
                            playbook_score=float(adaptive_fields["playbook_score"]),
                            location_score=float(adaptive_fields["location_score"]),
                            trigger_score=float(adaptive_fields["trigger_score"]),
                            entry_trade_prob=float(getattr(pos_snapshot, "entry_trade_prob", 0.0)),
                            entry_macro_coherence_score=float(getattr(pos_snapshot, "entry_macro_coherence_score", 0.0)),
                            aggressive_fallback_used=bool(getattr(pos_snapshot, "aggressive_fallback_used", False)),
                        )) <= 0.48
                    )
                )
                if tempo_rotation_release and lifecycle_action == "partial_tp":
                    lifecycle_action = "exit"
                    lifecycle_reason = "adaptive_tempo_rotation_exit"
                    close_lots = 0.0
                if (not severe_adaptive_exit) and baseline_lifecycle_action in {"partial_tp", "exit"}:
                    lifecycle_action = baseline_lifecycle_action
                    lifecycle_reason = baseline_lifecycle_reason
                    close_lots = baseline_close_lots
                if (
                    baseline_lifecycle_action == "hold"
                    and lifecycle_action in {"partial_tp", "exit"}
                    and (not severe_adaptive_exit)
                    and (not tempo_rotation_release)
                ):
                    lifecycle_action = "hold"
                    lifecycle_reason = "adaptive_hold_baseline_floor"
                    close_lots = 0.0

            lifecycle_action_final = str("entry" if (pos_snapshot is None and ready) else lifecycle_action)
            lifecycle_reason_final = str("entry_approved" if (pos_snapshot is None and ready) else lifecycle_reason)
            shadow_entry_blocking_reasons = list(decision_reasons)
            if bool(signal_row["shadow_spread_relaxed"][bar_idx]):
                shadow_entry_blocking_reasons = [reason for reason in shadow_entry_blocking_reasons if reason != "spread_too_wide"]
            shadow_meta = {
                "pair": pair,
                "ts": ts_str,
                "decision_source_ts": decision_source_ts,
                "fill_delay_bars": int(fill_delay_bars),
                "pair_tier": str(signal_row["pair_tier"][bar_idx]),
                "entry_blocking_reasons": list(decision_reasons),
                "position_count_pair": int(pair_count),
                "position_signature": pair if pos_snapshot is not None else "",
                "shadow_floor_ok": bool(signal_row["shadow_floor_ok"][bar_idx]),
                "shadow_floor_rejection_reason": str(signal_row["shadow_floor_rejection_reason"][bar_idx]),
                "structure_rescue_active": bool(signal_row["structure_rescue_active"][bar_idx]),
                "entry_quality_score_shadow": float(signal_row["entry_quality_score_shadow"][bar_idx]),
                "calibrated_ev_bps_shadow": float(signal_row["calibrated_ev_bps_shadow"][bar_idx]),
                "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                "expected_edge_bps": float(signal_row["expected_edge_bps"][bar_idx]),
                "spread_bps": float(signal_row["spread_bps"][bar_idx]),
                "shadow_pair_spread_cap_bps": float(signal_row["shadow_pair_spread_cap_bps"][bar_idx]),
                "shadow_spread_relaxed": bool(signal_row["shadow_spread_relaxed"][bar_idx]),
                "threshold_snapshot": dict(threshold_snapshot),
                "session_bucket": str(signal_row["session_bucket"][bar_idx]),
                "baseline_allowed": bool(strict_ready),
                "baseline_rejection_reason": "none" if strict_ready else (strict_decision_reasons[0] if strict_decision_reasons else "none"),
                "exec_mode": str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE),
                **adaptive_fields,
                "belief_overlay_adjustment": 0.0,
            }
            shadow_inputs_for_bar.append(
                {
                    "symbol": pair,
                    "side": side,
                    "score": float(signal_row["expected_edge_bps"][bar_idx]),
                    "confidence": float(max(0.0, min(100.0, float(signal_row["trade_prob"][bar_idx]) * 100.0))),
                    "execution_ready": bool(ready),
                    "reasons": list(decision_reasons),
                    "metadata": shadow_meta,
                }
            )
            collector_rows_for_bar.append(
                {
                    "pair": pair,
                    "ts": ts_str,
                    "decision_source_ts": decision_source_ts,
                    "fill_delay_bars": int(fill_delay_bars),
                    "side": side,
                    "allowed": bool(ready),
                    "rejection_reason": "none" if ready else decision_reasons[0],
                    "rejection_reasons": list(decision_reasons),
                    "expected_edge_bps": float(signal_row["expected_edge_bps"][bar_idx]),
                    "spread_bps": float(signal_row["spread_bps"][bar_idx]),
                    "regime_prob": float(signal_row["regime_prob"][bar_idx]),
                    "swing_prob": float(signal_row["swing_prob"][bar_idx]),
                    "entry_prob": float(signal_row["entry_prob"][bar_idx]),
                    "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                    "uncertainty_score": float(signal_row["uncertainty_score"][bar_idx]),
                    "model_disagreement_score": float(signal_row["model_disagreement_score"][bar_idx]),
                    "directional_swing_confidence": float(signal_row["directional_swing_confidence"][bar_idx]),
                    "entry_margin": float(signal_row["entry_margin"][bar_idx]),
                    "meta_margin": float(signal_row["meta_margin"][bar_idx]),
                    "session_bucket": str(signal_row["session_bucket"][bar_idx]),
                    "session_entry_blocked": bool(signal_row["session_entry_blocked"][bar_idx]),
                    "session_entry_block_reason": str(signal_row["session_entry_block_reason"][bar_idx]),
                    "htf_alignment_score": float(signal_row["htf_alignment_score"][bar_idx]),
                    "pullback_quality_score": float(signal_row["pullback_quality_score"][bar_idx]),
                    "resume_trigger_score": float(signal_row["resume_trigger_score"][bar_idx]),
                    "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                    "structure_timing_score": float(signal_row["structure_timing_score"][bar_idx]),
                    "structure_bonus_bps": float(signal_row["structure_bonus_bps"][bar_idx]),
                    "chase_penalty_bps": float(signal_row["chase_penalty_bps"][bar_idx]),
                    "calibrated_ev_bps_shadow": float(signal_row["calibrated_ev_bps_shadow"][bar_idx]),
                    "entry_quality_score_shadow": float(signal_row["entry_quality_score_shadow"][bar_idx]),
                    "structure_rescue_active": bool(signal_row["structure_rescue_active"][bar_idx]),
                    "shadow_floor_ok": bool(signal_row["shadow_floor_ok"][bar_idx]),
                    "shadow_floor_rejection_reason": str(signal_row["shadow_floor_rejection_reason"][bar_idx]),
                    "portfolio_rank_shadow": 0,
                    "shadow_would_trade": False,
                    "shadow_rejection_reason": "",
                    "pair_tier": str(signal_row["pair_tier"][bar_idx]),
                    "position_side": pos_side,
                    "position_count_pair": int(pair_count),
                    "total_open_positions": int(total_count),
                    "lifecycle_action": lifecycle_action_final,
                    "lifecycle_reason": lifecycle_reason_final,
                    "exit_action_selected": str(exit_action_selected),
                    "reversal_context_active": bool(reversal_context_active),
                    "reversal_ready": bool(reversal_ready),
                    "reversal_failure_prob": float(reversal_failure_prob),
                    "reversal_opportunity_prob": float(reversal_opportunity_prob),
                    "baseline_allowed": bool(strict_ready),
                    "baseline_rejection_reason": "none" if strict_ready else (strict_decision_reasons[0] if strict_decision_reasons else "none"),
                    "exec_mode": str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE),
                    **adaptive_fields,
                    "belief_overlay_adjustment": 0.0,
                    "scenario_bucket": str(signal_row["scenario_bucket"][bar_idx]),
                    "regime_bucket": str(signal_row["regime_bucket"][bar_idx]),
                }
            )
            pending_actions.append(
                {
                    "pair": pair,
                    "ts": ts_str,
                    "pos_snapshot": pos_snapshot,
                    "live_pos": live_pos,
                    "ready": ready,
                    "decision_reasons": list(decision_reasons),
                    "entry_hard_reasons": list(hard_reasons),
                    "side": side,
                    "lifecycle_action": lifecycle_action,
                    "lifecycle_reason": lifecycle_reason,
                    "close_lots": float(close_lots),
                    "exit_action_selected": exit_action_selected,
                    "reversal_failure_prob": float(reversal_failure_prob),
                    "reversal_opportunity_prob": float(reversal_opportunity_prob),
                    "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                    "expected_edge_bps": float(signal_row["expected_edge_bps"][bar_idx]),
                    "spread_bps": float(signal_row["spread_bps"][bar_idx]),
                    "uncertainty_score": float(signal_row["uncertainty_score"][bar_idx]),
                    "entry_session_bucket": str(signal_row["session_bucket"][bar_idx]),
                    "entry_scenario_bucket": str(signal_row["scenario_bucket"][bar_idx]),
                    "entry_regime_bucket": str(signal_row["regime_bucket"][bar_idx]),
                    "entry_uncertainty_score": float(signal_row["uncertainty_score"][bar_idx]),
                    "entry_structure_timing_score": float(signal_row["structure_timing_score"][bar_idx]),
                    "pair_tier": str(signal_row["pair_tier"][bar_idx]),
                    "entry_playbook": str(adaptive_fields["playbook"]),
                    "sleeve": str(adaptive_fields["sleeve"]),
                    "environment_state": str(adaptive_fields["environment_state"]),
                    "belief_primary_side": str(adaptive_fields["belief_primary_side"]),
                    "belief_primary_scenario": str(adaptive_fields["belief_primary_scenario"]),
                    "belief_primary_thesis": str(adaptive_fields["belief_primary_thesis"]),
                    "belief_primary_score": float(adaptive_fields["belief_primary_score"]),
                    "belief_primary_rank_score": float(adaptive_fields["belief_primary_rank_score"]),
                    "belief_primary_ev_above_hurdle_prob": float(adaptive_fields["belief_primary_ev_above_hurdle_prob"]),
                    "belief_primary_expected_net_ev_bps": float(adaptive_fields["belief_primary_expected_net_ev_bps"]),
                    "belief_primary_confirm_prob": float(adaptive_fields["belief_primary_confirm_prob"]),
                    "belief_primary_fail_fast_prob": float(adaptive_fields["belief_primary_fail_fast_prob"]),
                    "belief_no_edge": bool(adaptive_fields["belief_no_edge"]),
                    "belief_opposing_side": str(adaptive_fields["belief_opposing_side"]),
                    "belief_opposing_scenario": str(adaptive_fields["belief_opposing_scenario"]),
                    "belief_opposing_thesis": str(adaptive_fields["belief_opposing_thesis"]),
                    "belief_opposing_score": float(adaptive_fields["belief_opposing_score"]),
                    "belief_gap": float(adaptive_fields["belief_gap"]),
                    "belief_fragility_score": float(adaptive_fields["belief_fragility_score"]),
                    "belief_horizon_alignment_score": float(adaptive_fields["belief_horizon_alignment_score"]),
                    "belief_short_up_prob": float(adaptive_fields["belief_short_up_prob"]),
                    "belief_trade_up_prob": float(adaptive_fields["belief_trade_up_prob"]),
                    "belief_structural_up_prob": float(adaptive_fields["belief_structural_up_prob"]),
                    "belief_regime_fit_score": float(adaptive_fields["belief_regime_fit_score"]),
                    "belief_expected_confirmation_window_bars": int(adaptive_fields["belief_expected_confirmation_window_bars"]),
                    "belief_expected_path_shape": str(adaptive_fields["belief_expected_path_shape"]),
                    "belief_invalidation_reason": str(adaptive_fields["belief_invalidation_reason"]),
                    "belief_model_version": str(adaptive_fields["belief_model_version"]),
                    "belief_source_mode": str(adaptive_fields["belief_source_mode"]),
                    "entry_location_score": float(adaptive_fields["location_score"]),
                    "entry_trigger_score": float(adaptive_fields["trigger_score"]),
                    "entry_macro_coherence_score": float(adaptive_fields["macro_coherence_score"]),
                    "adaptive_entry_quality": float(adaptive_fields["adaptive_entry_quality"]),
                    "thesis_id": str(adaptive_fields["thesis_id"]),
                    "campaign_seq": int(adaptive_fields["campaign_seq"]),
                    "campaign_entry_kind": str(adaptive_fields["campaign_entry_kind"]),
                    "campaign_state": str(adaptive_fields["campaign_state"]),
                    "campaign_state_reason": str(adaptive_fields["campaign_state_reason"]),
                    "campaign_proof_score": float(adaptive_fields["campaign_proof_score"]),
                    "campaign_maturity_score": float(adaptive_fields["campaign_maturity_score"]),
                    "campaign_reset_quality": float(adaptive_fields["campaign_reset_quality"]),
                    "campaign_priority_boost": float(adaptive_fields["campaign_priority_boost"]),
                    "campaign_reentry_blocked": bool(adaptive_fields["campaign_reentry_blocked"]),
                    "playbook_score": float(adaptive_fields["playbook_score"]),
                    "location_score": float(adaptive_fields["location_score"]),
                    "trigger_score": float(adaptive_fields["trigger_score"]),
                    "hostility_score": float(adaptive_fields["hostility_score"]),
                    "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                    "calibrated_ev_bps_shadow": float(signal_row["calibrated_ev_bps_shadow"][bar_idx]),
                    "aggressive_fallback_used": bool(adaptive_fields["aggressive_fallback_used"]),
                    "baseline_allowed": bool(strict_ready),
                    "adaptive_allowed": bool(adaptive_fields["adaptive_allowed"]),
                    "adaptive_eval": dict(adaptive_eval),
                    "adaptive_eval_row": {
                        "pair": pair,
                        "side": desired_side,
                        "signal_side": desired_side,
                        "baseline_rejection_reason": gate_reason if not gate_allowed else "none",
                        "session_bucket": str(signal_row["session_bucket"][bar_idx]),
                        "session_entry_blocked": bool(signal_row["session_entry_blocked"][bar_idx]),
                        "session_entry_block_reason": str(signal_row["session_entry_block_reason"][bar_idx]),
                        "spread_bps": float(signal_row["spread_bps"][bar_idx]),
                        "uncertainty_score": float(signal_row["uncertainty_score"][bar_idx]),
                        "model_disagreement_score": float(signal_row["model_disagreement_score"][bar_idx]),
                        "playbook": str(adaptive_fields["playbook"]),
                        "playbook_score": float(adaptive_fields["playbook_score"]),
                        "location_score": float(adaptive_fields["location_score"]),
                        "trigger_score": float(adaptive_fields["trigger_score"]),
                        "macro_coherence_score": float(adaptive_fields["macro_coherence_score"]),
                        "environment_state": str(adaptive_fields["environment_state"]),
                        "extreme_chase": bool(signal_row["extreme_chase"][bar_idx]) if "extreme_chase" in signal_row else False,
                        "adaptive_base_rejection_reason": str(signal_row["adaptive_base_rejection_reason"][bar_idx]) if "adaptive_base_rejection_reason" in signal_row else "approved",
                        "calibrated_ev_bps_shadow": float(signal_row["calibrated_ev_bps_shadow"][bar_idx]),
                        "regime_prob": float(signal_row["regime_prob"][bar_idx]),
                        "swing_prob": float(signal_row["swing_prob"][bar_idx]),
                        "entry_prob": float(signal_row["entry_prob"][bar_idx]),
                        "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                        "expected_edge_bps": float(signal_row["expected_edge_bps"][bar_idx]),
                        "structure_timing_score": float(signal_row["structure_timing_score"][bar_idx]),
                        "extension_penalty_score": float(signal_row["extension_penalty_score"][bar_idx]),
                        "adaptive_entry_quality": float(adaptive_fields["adaptive_entry_quality"]),
                    },
                    "age_bars": float(age_bars),
                    "unrealized_pnl_usd": float(unrealized_pnl),
                    "sleeve_health_score": float(adaptive_fields["sleeve_health_score"]),
                    "sleeve_health_state": str(adaptive_fields["sleeve_health_state"]),
                    "allocator_score": float(adaptive_fields["allocator_score"]),
                    "allocator_rank": adaptive_fields["allocator_rank"],
                        "allocator_selected": bool(adaptive_fields["allocator_selected"]),
                        "allocator_rejection_reason": str(adaptive_fields["allocator_rejection_reason"]),
                        "replacement_value": float(adaptive_fields["replacement_value"]),
                        "replacement_keep_score": float(
                            max(
                                0.0,
                                min(
                                    1.0,
                                    adaptive_replacement_keep_score(
                                        lifecycle_action=str(lifecycle_action),
                                        lifecycle_reason=str(lifecycle_reason),
                                        playbook_score=float(adaptive_fields["playbook_score"]),
                                        location_score=float(adaptive_fields["location_score"]),
                                        trigger_score=float(adaptive_fields["trigger_score"]),
                                        entry_trade_prob=float(signal_row["trade_prob"][bar_idx]),
                                        entry_macro_coherence_score=float(adaptive_fields["macro_coherence_score"]),
                                        aggressive_fallback_used=bool(adaptive_fields["aggressive_fallback_used"]),
                                    )
                                    + float(campaign_keep_adjustment),
                                ),
                            )
                        ),
                    }
                )

        if adaptive_enabled:
            _apply_cross_pair_admission_overlay(
                pending_actions=pending_actions,
                collector_rows_for_bar=collector_rows_for_bar,
                shadow_inputs_for_bar=shadow_inputs_for_bar,
                open_positions=open_positions,
                exit_registry=recent_exit_registry,
                campaign_registry=campaign_registry,
                campaign_config=campaign_config,
                bar_idx=int(bar_idx),
                settings=s,
                fallback_margin=float(getattr(args, "adaptive_aggressive_fallback_margin", 0.08)),
            )
            projected_exit_indices = {
                idx_action
                for idx_action, action in enumerate(pending_actions)
                if action["pos_snapshot"] is not None and str(action.get("lifecycle_action") or "hold") == "exit"
            }
            projected_open_count = max(0, len(positions_snapshot) - len(projected_exit_indices))
            remaining_slots = max(0, int(s.max_total_positions) - projected_open_count)
            allocator_open_positions: list[AllocatorOpenPosition] = []
            position_index_by_pair: dict[str, int] = {}
            for idx_action, action in enumerate(pending_actions):
                pos_snapshot = action["pos_snapshot"]
                if pos_snapshot is None or idx_action in projected_exit_indices:
                    continue
                playbook = str(action.get("entry_playbook") or getattr(pos_snapshot, "playbook", PLAYBOOK_TREND_PULLBACK))
                keep_score = float(action.get("replacement_keep_score", 0.0))
                age = float(action.get("age_bars", 0.0))
                campaign_state = str(getattr(pos_snapshot, "campaign_state", action.get("campaign_state") or CAMPAIGN_STATE_INACTIVE))
                protected_hold = bool(
                    age < 3.0
                    or (
                        campaign_state == "press"
                        and age <= float(campaign_config.press_protected_bars)
                    )
                    or (
                        playbook == PLAYBOOK_BREAKOUT_EXPANSION
                        and str(action.get("lifecycle_reason") or "") == "adaptive_hold"
                        and float(action.get("entry_trigger_score", 0.0)) >= 0.55
                    )
                    or (
                        playbook == PLAYBOOK_TREND_PULLBACK
                        and str(action.get("lifecycle_reason") or "") == "adaptive_hold"
                        and keep_score >= 0.62
                    )
                )
                replaceable_hold = bool(
                    (not protected_hold)
                    and (
                        campaign_state == CAMPAIGN_STATE_HARVEST
                        or str(action.get("lifecycle_reason") or "") == "adaptive_campaign_harvest"
                        or str(action.get("lifecycle_reason") or "") == "adaptive_hold_baseline_floor"
                        or (tempo_gap_active and str(action.get("lifecycle_action") or "hold") == "hold" and keep_score <= 0.48)
                        or keep_score < 0.62
                    )
                )
                allocator_open_positions.append(
                    _allocator_open_position_from_action(
                        action=action,
                        position=pos_snapshot,
                        protected_hold=protected_hold,
                        replaceable_hold=replaceable_hold,
                    )
                )
                position_index_by_pair[str(pos_snapshot.pair)] = int(idx_action)

            allocator_candidates = []
            overlay_outputs: dict[int, Any] = {}
            for idx_action, action in enumerate(pending_actions):
                if action["pos_snapshot"] is not None or not bool(action["ready"]):
                    continue
                sleeve_snapshot = _sleeve_snapshot_for(sleeve_health_snapshots, str(action.get("entry_playbook") or ""))
                overlay_out = build_desk_overlay(
                    _desk_overlay_inputs_for_action(
                        action=action,
                        sleeve_snapshot=sleeve_snapshot,
                        open_position_count=int(projected_open_count),
                        allocator_open_positions=allocator_open_positions,
                        settings=s,
                    )
                )
                overlay_outputs[int(idx_action)] = overlay_out
                overlay_guidance = {
                    key: asdict(value) for key, value in dict(getattr(overlay_out, "sleeve_budget_guidance", {}) or {}).items()
                }
                action["conviction_score"] = float(getattr(overlay_out, "conviction_score", 0.0))
                action["conviction_band"] = str(getattr(overlay_out, "conviction_band", ""))
                action["thesis_stage"] = str(getattr(overlay_out, "thesis_stage", "stand_down"))
                action["portfolio_posture"] = str(getattr(overlay_out, "portfolio_posture", "balanced_probe"))
                action["replacement_urgency"] = float(getattr(overlay_out, "replacement_urgency", 0.0))
                primary_guidance = overlay_guidance.get(str(action.get("sleeve") or ""), {})
                action["sleeve_budget_target"] = int(
                    float(_safe_float(primary_guidance.get("target_share", 0.0), 0.0))
                    * max(1, int(remaining_slots))
                )
                action["overlay_metadata"] = {
                    "sleeve_budget_guidance": dict(overlay_guidance),
                    "trace": [asdict(stage) for stage in list(getattr(overlay_out, "trace", []) or [])],
                }
                action["overlay_diagnostics"] = {
                    "belief_gap": float(_safe_float(action.get("belief_gap", 0.0), 0.0)),
                    "fail_fast_risk": float(_safe_float(action.get("belief_primary_fail_fast_prob", 0.0), 0.0)),
                    "portfolio_posture": str(action.get("portfolio_posture") or ""),
                    "replacement_urgency": float(action.get("replacement_urgency", 0.0)),
                }
                overlay_reason = "overlay_active"
                action_allowed = bool(action.get("adaptive_allowed", False))
                if action_allowed and float(action.get("conviction_score", 0.0)) < 0.35:
                    action_allowed = False
                    overlay_reason = "overlay_low_conviction"
                elif action_allowed and str(action.get("thesis_stage") or "") == "stand_down":
                    action_allowed = False
                    overlay_reason = "overlay_stand_down"
                action["adaptive_allowed"] = bool(action_allowed)
                if (not action_allowed) and str(action.get("adaptive_rejection_reason") or "") in {"", "approved", "none"}:
                    action["adaptive_rejection_reason"] = str(overlay_reason)
                    action["ready"] = False
                    action["decision_reasons"] = [str(overlay_reason)]
                    collector_rows_for_bar[idx_action]["allowed"] = False
                    collector_rows_for_bar[idx_action]["rejection_reason"] = str(overlay_reason)
                    collector_rows_for_bar[idx_action]["rejection_reasons"] = [str(overlay_reason)]
                    shadow_inputs_for_bar[idx_action]["execution_ready"] = False
                    shadow_inputs_for_bar[idx_action]["reasons"] = [str(overlay_reason)]
                collector_rows_for_bar[idx_action]["conviction_score"] = float(action.get("conviction_score", 0.0))
                collector_rows_for_bar[idx_action]["conviction_band"] = str(action.get("conviction_band") or "")
                collector_rows_for_bar[idx_action]["thesis_stage"] = str(action.get("thesis_stage") or "stand_down")
                collector_rows_for_bar[idx_action]["portfolio_posture"] = str(action.get("portfolio_posture") or "balanced_probe")
                collector_rows_for_bar[idx_action]["replacement_urgency"] = float(action.get("replacement_urgency", 0.0))
                collector_rows_for_bar[idx_action]["sleeve_budget_target"] = int(_safe_int(action.get("sleeve_budget_target", 0), 0))
                shadow_inputs_for_bar[idx_action]["metadata"]["conviction_score"] = float(action.get("conviction_score", 0.0))
                shadow_inputs_for_bar[idx_action]["metadata"]["conviction_band"] = str(action.get("conviction_band") or "")
                shadow_inputs_for_bar[idx_action]["metadata"]["thesis_stage"] = str(action.get("thesis_stage") or "stand_down")
                shadow_inputs_for_bar[idx_action]["metadata"]["portfolio_posture"] = str(action.get("portfolio_posture") or "balanced_probe")
                shadow_inputs_for_bar[idx_action]["metadata"]["replacement_urgency"] = float(action.get("replacement_urgency", 0.0))
                shadow_inputs_for_bar[idx_action]["metadata"]["sleeve_budget_target"] = int(_safe_int(action.get("sleeve_budget_target", 0), 0))
                shadow_inputs_for_bar[idx_action]["metadata"]["overlay_metadata"] = dict(action.get("overlay_metadata") or {})
                shadow_inputs_for_bar[idx_action]["metadata"]["overlay_diagnostics"] = dict(action.get("overlay_diagnostics") or {})
                if not action_allowed:
                    continue
                candidate = build_allocator_candidate(
                    candidate_id=f"{action['pair']}:{ts_str}:{idx_action}",
                    index=int(idx_action),
                    pair=str(action["pair"]),
                    ts=str(ts_str),
                    side=str(action["side"]),
                    sleeve=str(action.get("sleeve") or playbook_to_sleeve(action.get("entry_playbook") or "")),
                    environment_state=str(action.get("environment_state") or ""),
                    session_bucket=str(action.get("entry_session_bucket") or ""),
                    baseline_allowed=bool(action.get("baseline_allowed", False)),
                    adaptive_allowed=bool(action.get("adaptive_allowed", False)),
                    playbook_score=float(action.get("playbook_score", 0.0)),
                    location_score=float(action.get("location_score", 0.0)),
                    trigger_score=float(action.get("trigger_score", 0.0)),
                    adaptive_entry_quality=float(action.get("adaptive_entry_quality", 0.0)),
                    expected_edge_bps=float(action.get("expected_edge_bps", action.get("calibrated_ev_bps_shadow", 0.0))),
                    uncertainty_score=float(action.get("uncertainty_score", 0.0)),
                    spread_bps=float(action.get("spread_bps", 0.0)),
                    max_spread_bps=float(getattr(s, "max_allowed_spread_bps", 0.0)),
                    macro_coherence_score=float(action.get("entry_macro_coherence_score", 0.0)),
                    currency_crowding_penalty=float(action.get("currency_crowding_penalty", 0.0)),
                    playbook_diversification_penalty=float(action.get("playbook_diversification_penalty", 0.0)),
                    thesis_id=str(action.get("thesis_id") or ""),
                    campaign_seq=int(action.get("campaign_seq", 0) or 0),
                    campaign_entry_kind=str(action.get("campaign_entry_kind") or ""),
                    campaign_state=str(action.get("campaign_state") or CAMPAIGN_STATE_INACTIVE),
                    campaign_state_reason=str(action.get("campaign_state_reason") or ""),
                    campaign_priority_boost=float(action.get("campaign_priority_boost", 0.0)),
                    campaign_proof_score=float(action.get("campaign_proof_score", 0.0)),
                    campaign_maturity_score=float(action.get("campaign_maturity_score", 0.0)),
                    campaign_reset_quality=float(action.get("campaign_reset_quality", 0.0)),
                    campaign_reentry_blocked=bool(action.get("campaign_reentry_blocked", False)),
                    conviction_score=float(action.get("conviction_score", 0.0)),
                    conviction_band=str(action.get("conviction_band") or "low"),
                    thesis_stage=str(action.get("thesis_stage") or "stand_down"),
                    portfolio_posture=str(action.get("portfolio_posture") or "balanced_probe"),
                    replacement_urgency=float(action.get("replacement_urgency", 0.0)),
                    sleeve_budget_target=int(_safe_int(action.get("sleeve_budget_target", 0), 0)),
                    sleeve_budget_used=int(_safe_int(action.get("sleeve_budget_used", 0), 0)),
                    config=allocator_config,
                    open_positions=allocator_open_positions,
                    sleeve_health=sleeve_snapshot,
                )
                belief_overlay_adjustment = 0.0
                if belief_overlay_enabled and str(action.get("belief_source_mode") or "") not in {"", "disabled", "artifact_missing"}:
                    belief_overlay_adjustment = _belief_overlay_adjustment(
                        belief_gap=float(action.get("belief_gap", 0.0)),
                        ev_above_hurdle_prob=float(action.get("belief_primary_ev_above_hurdle_prob", 0.0)),
                        fail_fast_prob=float(action.get("belief_primary_fail_fast_prob", 0.0)),
                        no_edge=bool(action.get("belief_no_edge", False)),
                    )
                    candidate.allocator_score = float(_clip01(candidate.allocator_score + belief_overlay_adjustment))
                action["belief_overlay_adjustment"] = float(belief_overlay_adjustment)
                allocator_candidates.append(candidate)

            sleeve_budget_targets = _overlay_budget_targets(
                overlays={int(item.index): overlay_outputs.get(int(item.index)) for item in allocator_candidates if overlay_outputs.get(int(item.index)) is not None},
                remaining_slots=int(remaining_slots),
                candidate_counts=dict(Counter(str(item.sleeve) for item in allocator_candidates)),
            )
            ranked_candidates, allocator_cycle = allocate_candidates(
                candidates=allocator_candidates,
                open_positions=allocator_open_positions,
                remaining_slots=int(remaining_slots),
                config=allocator_config,
                tempo_gap_active=bool(tempo_gap_active),
                sleeve_budget_targets=dict(sleeve_budget_targets),
            )
            replacement_targets = {
                str(item.replacement_target_pair): item
                for item in ranked_candidates
                if bool(item.allocator_selected) and str(item.replacement_target_pair or "")
            }
            candidate_by_index = {int(item.index): item for item in ranked_candidates}
            for idx_action, action in enumerate(pending_actions):
                candidate = candidate_by_index.get(int(idx_action))
                if candidate is None:
                    continue
                action["allocator_score"] = float(candidate.allocator_score)
                action["allocator_rank"] = int(candidate.allocator_rank or 0)
                action["allocator_selected"] = bool(candidate.allocator_selected)
                action["allocator_rejection_reason"] = str(candidate.allocator_rejection_reason)
                action["replacement_value"] = float(candidate.replacement_value)
                action["sleeve_health_score"] = float(candidate.sleeve_health_score)
                action["sleeve_health_state"] = str(candidate.sleeve_health_state)
                action["thesis_id"] = str(candidate.thesis_id)
                action["campaign_seq"] = int(candidate.campaign_seq)
                action["campaign_entry_kind"] = str(candidate.campaign_entry_kind)
                action["campaign_state"] = str(candidate.campaign_state)
                action["campaign_state_reason"] = str(candidate.campaign_state_reason)
                action["campaign_proof_score"] = float(candidate.campaign_proof_score)
                action["campaign_maturity_score"] = float(candidate.campaign_maturity_score)
                action["campaign_reset_quality"] = float(candidate.campaign_reset_quality)
                action["campaign_priority_boost"] = float(candidate.campaign_priority_boost)
                action["campaign_reentry_blocked"] = bool(candidate.campaign_reentry_blocked)
                collector_rows_for_bar[idx_action]["belief_overlay_adjustment"] = float(action.get("belief_overlay_adjustment", 0.0))
                collector_rows_for_bar[idx_action]["allocator_score"] = float(candidate.allocator_score)
                collector_rows_for_bar[idx_action]["allocator_rank"] = int(candidate.allocator_rank or 0)
                collector_rows_for_bar[idx_action]["allocator_selected"] = bool(candidate.allocator_selected)
                collector_rows_for_bar[idx_action]["allocator_rejection_reason"] = str(candidate.allocator_rejection_reason)
                collector_rows_for_bar[idx_action]["replacement_value"] = float(candidate.replacement_value)
                collector_rows_for_bar[idx_action]["sleeve"] = str(candidate.sleeve)
                collector_rows_for_bar[idx_action]["sleeve_health_score"] = float(candidate.sleeve_health_score)
                collector_rows_for_bar[idx_action]["sleeve_health_state"] = str(candidate.sleeve_health_state)
                collector_rows_for_bar[idx_action]["thesis_id"] = str(candidate.thesis_id)
                collector_rows_for_bar[idx_action]["campaign_seq"] = int(candidate.campaign_seq)
                collector_rows_for_bar[idx_action]["campaign_entry_kind"] = str(candidate.campaign_entry_kind)
                collector_rows_for_bar[idx_action]["campaign_state"] = str(candidate.campaign_state)
                collector_rows_for_bar[idx_action]["campaign_state_reason"] = str(candidate.campaign_state_reason)
                collector_rows_for_bar[idx_action]["campaign_proof_score"] = float(candidate.campaign_proof_score)
                collector_rows_for_bar[idx_action]["campaign_maturity_score"] = float(candidate.campaign_maturity_score)
                collector_rows_for_bar[idx_action]["campaign_reset_quality"] = float(candidate.campaign_reset_quality)
                collector_rows_for_bar[idx_action]["campaign_priority_boost"] = float(candidate.campaign_priority_boost)
                collector_rows_for_bar[idx_action]["campaign_reentry_blocked"] = bool(candidate.campaign_reentry_blocked)
                collector_rows_for_bar[idx_action]["conviction_score"] = float(candidate.conviction_score)
                collector_rows_for_bar[idx_action]["conviction_band"] = str(candidate.conviction_band)
                collector_rows_for_bar[idx_action]["thesis_stage"] = str(candidate.thesis_stage)
                collector_rows_for_bar[idx_action]["portfolio_posture"] = str(candidate.portfolio_posture)
                collector_rows_for_bar[idx_action]["replacement_urgency"] = float(candidate.replacement_urgency)
                collector_rows_for_bar[idx_action]["sleeve_budget_target"] = int(candidate.sleeve_budget_target)
                collector_rows_for_bar[idx_action]["sleeve_budget_used"] = int(candidate.sleeve_budget_used)
                shadow_inputs_for_bar[idx_action]["metadata"]["allocator_score"] = float(candidate.allocator_score)
                shadow_inputs_for_bar[idx_action]["metadata"]["allocator_rank"] = int(candidate.allocator_rank or 0)
                shadow_inputs_for_bar[idx_action]["metadata"]["allocator_selected"] = bool(candidate.allocator_selected)
                shadow_inputs_for_bar[idx_action]["metadata"]["allocator_rejection_reason"] = str(candidate.allocator_rejection_reason)
                shadow_inputs_for_bar[idx_action]["metadata"]["replacement_value"] = float(candidate.replacement_value)
                shadow_inputs_for_bar[idx_action]["metadata"]["sleeve"] = str(candidate.sleeve)
                shadow_inputs_for_bar[idx_action]["metadata"]["sleeve_health_score"] = float(candidate.sleeve_health_score)
                shadow_inputs_for_bar[idx_action]["metadata"]["sleeve_health_state"] = str(candidate.sleeve_health_state)
                shadow_inputs_for_bar[idx_action]["metadata"]["thesis_id"] = str(candidate.thesis_id)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_seq"] = int(candidate.campaign_seq)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_entry_kind"] = str(candidate.campaign_entry_kind)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_state"] = str(candidate.campaign_state)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_state_reason"] = str(candidate.campaign_state_reason)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_proof_score"] = float(candidate.campaign_proof_score)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_maturity_score"] = float(candidate.campaign_maturity_score)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_reset_quality"] = float(candidate.campaign_reset_quality)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_priority_boost"] = float(candidate.campaign_priority_boost)
                shadow_inputs_for_bar[idx_action]["metadata"]["campaign_reentry_blocked"] = bool(candidate.campaign_reentry_blocked)
                shadow_inputs_for_bar[idx_action]["metadata"]["conviction_score"] = float(candidate.conviction_score)
                shadow_inputs_for_bar[idx_action]["metadata"]["conviction_band"] = str(candidate.conviction_band)
                shadow_inputs_for_bar[idx_action]["metadata"]["thesis_stage"] = str(candidate.thesis_stage)
                shadow_inputs_for_bar[idx_action]["metadata"]["portfolio_posture"] = str(candidate.portfolio_posture)
                shadow_inputs_for_bar[idx_action]["metadata"]["replacement_urgency"] = float(candidate.replacement_urgency)
                shadow_inputs_for_bar[idx_action]["metadata"]["sleeve_budget_target"] = int(candidate.sleeve_budget_target)
                shadow_inputs_for_bar[idx_action]["metadata"]["sleeve_budget_used"] = int(candidate.sleeve_budget_used)
                shadow_inputs_for_bar[idx_action]["metadata"]["belief_overlay_adjustment"] = float(action.get("belief_overlay_adjustment", 0.0))
                if not bool(candidate.allocator_selected):
                    pending_actions[idx_action]["ready"] = False
                    pending_actions[idx_action]["decision_reasons"] = [str(candidate.allocator_rejection_reason or "allocator_ranked_out")]
                    collector_rows_for_bar[idx_action]["allowed"] = False
                    collector_rows_for_bar[idx_action]["rejection_reason"] = str(candidate.allocator_rejection_reason or "allocator_ranked_out")
                    collector_rows_for_bar[idx_action]["rejection_reasons"] = [str(candidate.allocator_rejection_reason or "allocator_ranked_out")]
                    shadow_inputs_for_bar[idx_action]["execution_ready"] = False
                    shadow_inputs_for_bar[idx_action]["reasons"] = [str(candidate.allocator_rejection_reason or "allocator_ranked_out")]

            for pair_to_replace, candidate in replacement_targets.items():
                weakest_idx = position_index_by_pair.get(str(pair_to_replace))
                if weakest_idx is None:
                    continue
                pending_actions[weakest_idx]["lifecycle_action"] = "exit"
                pending_actions[weakest_idx]["lifecycle_reason"] = "adaptive_replacement_exit"
                pending_actions[weakest_idx]["close_lots"] = 0.0
                collector_rows_for_bar[weakest_idx]["lifecycle_action"] = "exit"
                collector_rows_for_bar[weakest_idx]["lifecycle_reason"] = "adaptive_replacement_exit"
                collector_rows_for_bar[weakest_idx]["replacement_value"] = float(candidate.replacement_value)

        _apply_shadow_entry_ranking(shadow_inputs_for_bar, settings=s, open_position_count=len(positions_snapshot))
        for shadow_input, collector_row in zip(shadow_inputs_for_bar, collector_rows_for_bar, strict=False):
            shadow_meta = dict(shadow_input.get("metadata") or {})
            collector_row["portfolio_rank_shadow"] = int(_safe_int(shadow_meta.get("portfolio_rank_shadow"), 0))
            collector_row["shadow_would_trade"] = bool(shadow_meta.get("shadow_would_trade", False))
            collector_row["shadow_rejection_reason"] = str(shadow_meta.get("shadow_rejection_reason") or "")
            if bool(collector_row["allowed"]) and not bool(collector_row["shadow_would_trade"]):
                divergence = "live_only"
            elif (not bool(collector_row["allowed"])) and bool(collector_row["shadow_would_trade"]):
                divergence = "shadow_only"
            elif bool(collector_row["allowed"]) and bool(collector_row["shadow_would_trade"]):
                divergence = "agree_ready"
            else:
                divergence = "agree_blocked"
            sleeve_tracker.record_divergence(
                sleeve=str(collector_row.get("sleeve") or playbook_to_sleeve(collector_row.get("playbook") or "")),
                divergence=str(divergence),
            )
            collector.consume(collector_row)

        action_counts.update(Counter(str(row.get("lifecycle_action") or "hold") for row in collector_rows_for_bar))

        for action in pending_actions:
            pair = str(action["pair"])
            pos_snapshot = action["pos_snapshot"]
            live_pos = action["live_pos"]
            lifecycle_action = str(action["lifecycle_action"])
            lifecycle_reason = str(action["lifecycle_reason"])
            close_lots = float(action["close_lots"])
            exit_action_selected = str(action["exit_action_selected"])

            if pos_snapshot is None:
                continue
            if lifecycle_action not in {"partial_tp", "exit"}:
                continue
            if live_pos is None:
                continue
            if str(live_pos.side) == "long":
                raw_exit = float(bid_arrays[pair][bar_idx])
                exit_price = BASE._apply_slippage(price=raw_exit, action="long_close", slippage_bps=float(args.slippage_bps))
            else:
                raw_exit = float(ask_arrays[pair][bar_idx])
                exit_price = BASE._apply_slippage(price=raw_exit, action="short_close", slippage_bps=float(args.slippage_bps))
            lots_to_close = float(live_pos.lots) if lifecycle_action == "exit" else float(close_lots)
            realized = BASE._realized_pnl_usd(
                pair=pair,
                side=str(live_pos.side),
                entry_price=float(live_pos.entry_price),
                exit_price=float(exit_price),
                lots=lots_to_close,
                bar_idx=bar_idx,
                mid_arrays=mid_arrays,
            )
            cash_balance += realized
            live_pos.realized_pnl_usd += realized
            if lifecycle_action == "partial_tp":
                live_pos.lots = round(max(0.0, float(live_pos.lots) - lots_to_close), 8)
                live_pos.partial_exit_events += 1
                live_pos.partial_count = int(getattr(live_pos, "partial_count", 0) or 0) + 1
                live_pos.last_partial_bar_index = int(bar_idx)
                partial_exit_count += 1
                if live_pos.lots <= 0.0:
                    lifecycle_action = "exit"
            if lifecycle_action == "exit":
                if lifecycle_reason == "reversal_models_exit":
                    reversal_exit_count += 1
                close_campaign = campaign_state_after_close(
                    position_state=str(getattr(live_pos, "campaign_state", CAMPAIGN_STATE_INACTIVE)),
                    pair=pair,
                    side=str(live_pos.side),
                    sleeve=str(getattr(live_pos, "sleeve", playbook_to_sleeve(getattr(live_pos, "playbook", "")))),
                    row={
                        "playbook_score": float(action.get("playbook_score", 0.0)),
                        "location_score": float(action.get("location_score", 0.0)),
                        "trigger_score": float(action.get("trigger_score", 0.0)),
                        "macro_coherence_score": float(action.get("entry_macro_coherence_score", 0.0)),
                        "hostility_score": float(action.get("hostility_score", 0.0)),
                        "extension_penalty_score": float(action.get("extension_penalty_score", 0.0)),
                        "environment_state": str(action.get("environment_state") or ""),
                    },
                    lifecycle_reason=str(lifecycle_reason),
                    realized_pnl_usd=float(live_pos.realized_pnl_usd),
                    bar_idx=int(bar_idx),
                    ts=str(ts_str),
                    config=campaign_config,
                    campaign_seq=int(getattr(live_pos, "campaign_seq", 0) or 0),
                    entry_kind=str(getattr(live_pos, "campaign_entry_kind", "") or ""),
                )
                trade = TwinClosedTrade(
                    pair=pair,
                    side=str(live_pos.side),
                    open_ts=str(live_pos.open_ts),
                    close_ts=str(ts_dt),
                    entry_price=float(live_pos.entry_price),
                    exit_price=float(exit_price),
                    lots=float(live_pos.entry_lots),
                    realized_pnl_usd=float(live_pos.realized_pnl_usd),
                    holding_bars=max(1, int((ts_dt - _to_utc_ts(live_pos.open_ts)).total_seconds() // holding_bar_secs)),
                    partial_exit_events=int(live_pos.partial_exit_events),
                    close_reason=str(lifecycle_reason),
                    entry_trade_prob=float(live_pos.entry_trade_prob),
                    exit_action_selected=str(exit_action_selected),
                    reversal_failure_prob=float(action["reversal_failure_prob"]),
                    reversal_opportunity_prob=float(action["reversal_opportunity_prob"]),
                    entry_session_bucket=str(live_pos.entry_session_bucket),
                    entry_scenario_bucket=str(live_pos.entry_scenario_bucket),
                    entry_regime_bucket=str(live_pos.entry_regime_bucket),
                    entry_uncertainty_score=float(live_pos.entry_uncertainty_score),
                    entry_structure_timing_score=float(live_pos.entry_structure_timing_score),
                    pair_tier=str(live_pos.pair_tier),
                    playbook=str(getattr(live_pos, "playbook", PLAYBOOK_TREND_PULLBACK)),
                    sleeve=str(getattr(live_pos, "sleeve", playbook_to_sleeve(getattr(live_pos, "playbook", PLAYBOOK_TREND_PULLBACK)))),
                    environment_state_at_entry=str(getattr(live_pos, "environment_state_at_entry", "")),
                    environment_state_at_exit=str(action["environment_state"] if adaptive_enabled else ""),
                    lifecycle_exit_reason=str(lifecycle_reason),
                    thesis_id=str(getattr(live_pos, "thesis_id", "") or close_campaign.thesis_id),
                    campaign_seq=int(getattr(live_pos, "campaign_seq", 0) or close_campaign.campaign_seq or 0),
                    campaign_entry_kind=str(getattr(live_pos, "campaign_entry_kind", "") or close_campaign.entry_kind or ""),
                    campaign_state=str(close_campaign.state),
                    campaign_state_reason=str(close_campaign.state_reason),
                    campaign_proof_score=float(close_campaign.proof_score),
                    campaign_maturity_score=float(close_campaign.maturity_score),
                    campaign_reset_quality=float(close_campaign.reset_quality),
                    campaign_priority_boost=float(close_campaign.priority_boost),
                    allocator_score=float(getattr(live_pos, "allocator_score", 0.0)),
                    replacement_value=float(action.get("replacement_value", 0.0)),
                    sleeve_health_score=float(getattr(live_pos, "sleeve_health_score", 0.5)),
                    sleeve_health_state=str(getattr(live_pos, "sleeve_health_state", "healthy")),
                    aggressive_fallback_used=bool(getattr(live_pos, "aggressive_fallback_used", False)),
                )
                closed_trades.append(trade)
                sleeve_tracker.record_trade(
                    sleeve=str(getattr(trade, "sleeve", playbook_to_sleeve(getattr(trade, "playbook", "")))),
                    realized_pnl_usd=float(getattr(trade, "realized_pnl_usd", 0.0)),
                    holding_bars=float(getattr(trade, "holding_bars", 0.0)),
                    partial_exit_events=int(getattr(trade, "partial_exit_events", 0)),
                    close_reason=str(getattr(trade, "close_reason", "")),
                    session_bucket=str(getattr(trade, "entry_session_bucket", "")),
                    pair=str(getattr(trade, "pair", "")),
                )
                close_reason_counts[str(lifecycle_reason)] += 1
                pnl_by_close_reason[str(lifecycle_reason)] += float(live_pos.realized_pnl_usd)
                recent_exit_registry[pair] = {
                    "bar_idx": int(bar_idx),
                    "side": str(live_pos.side),
                    "playbook": str(getattr(live_pos, "playbook", PLAYBOOK_TREND_PULLBACK)),
                    "reason": str(lifecycle_reason),
                    "thesis_id": str(getattr(live_pos, "thesis_id", "") or close_campaign.thesis_id),
                    "campaign_state": str(close_campaign.state),
                }
                apply_campaign_registry_snapshot(
                    campaign_registry,
                    snapshot=close_campaign,
                    bar_idx=int(bar_idx),
                    ts=str(ts_str),
                    active_position=False,
                    realized_pnl_usd=float(live_pos.realized_pnl_usd),
                )
                transition = campaign_transition_if_changed(
                    prior_state=str(getattr(live_pos, "campaign_state", CAMPAIGN_STATE_INACTIVE)),
                    snapshot=close_campaign,
                    bar_idx=int(bar_idx),
                    ts=str(ts_str),
                    realized_pnl_usd=float(live_pos.realized_pnl_usd),
                    holding_bars=float(max(1, int((ts_dt - _to_utc_ts(live_pos.open_ts)).total_seconds() // holding_bar_secs))),
                )
                if transition is not None:
                    campaign_events.append(asdict(transition))
                open_positions.pop(pair, None)

        for action in pending_actions:
            pair = str(action["pair"])
            pos_snapshot = action["pos_snapshot"]
            ready = bool(action["ready"])
            if pos_snapshot is not None:
                continue
            if ready:
                lots, _ = BASE._entry_order_lots(state={"equity": current_equity}, settings=s, equity_seed=float(args.start_equity))
                if float(lots) >= float(s.min_order_lots):
                    if str(action["side"]) == "BUY":
                        entry_price = BASE._apply_slippage(price=float(ask_arrays[pair][bar_idx]), action="buy_open", slippage_bps=float(args.slippage_bps))
                        side_txt = "long"
                    else:
                        entry_price = BASE._apply_slippage(price=float(bid_arrays[pair][bar_idx]), action="sell_open", slippage_bps=float(args.slippage_bps))
                        side_txt = "short"
                    entry_snapshot = start_campaign_on_entry(
                        pair=pair,
                        side=side_txt,
                        sleeve=str(action.get("sleeve") or playbook_to_sleeve(action["entry_playbook"] or PLAYBOOK_TREND_PULLBACK)),
                        row={
                            "playbook_score": float(action.get("playbook_score", 0.0)),
                            "location_score": float(action.get("location_score", 0.0)),
                            "trigger_score": float(action.get("trigger_score", 0.0)),
                            "macro_coherence_score": float(action.get("entry_macro_coherence_score", 0.0)),
                            "hostility_score": float(action.get("hostility_score", 0.0)),
                            "extension_penalty_score": float(action.get("extension_penalty_score", 0.0)),
                            "environment_state": str(action.get("environment_state") or ""),
                            "trade_prob": float(action.get("trade_prob", 0.0)),
                        },
                        bar_idx=int(bar_idx),
                        ts=str(ts_str),
                        registry=campaign_registry,
                        prior_snapshot=evaluate_entry_campaign_memory(
                            pair=pair,
                            side=side_txt,
                            sleeve=str(action.get("sleeve") or playbook_to_sleeve(action["entry_playbook"] or PLAYBOOK_TREND_PULLBACK)),
                            row={
                                "playbook_score": float(action.get("playbook_score", 0.0)),
                                "location_score": float(action.get("location_score", 0.0)),
                                "trigger_score": float(action.get("trigger_score", 0.0)),
                                "macro_coherence_score": float(action.get("entry_macro_coherence_score", 0.0)),
                                "hostility_score": float(action.get("hostility_score", 0.0)),
                                "extension_penalty_score": float(action.get("extension_penalty_score", 0.0)),
                                "environment_state": str(action.get("environment_state") or ""),
                                "trade_prob": float(action.get("trade_prob", 0.0)),
                            },
                            bar_idx=int(bar_idx),
                            ts=str(ts_str),
                            registry=campaign_registry,
                            config=campaign_config,
                        ),
                    )
                    open_positions[pair] = TwinOpenPosition(
                        pair=pair,
                        side=side_txt,
                        lots=float(lots),
                        entry_lots=float(lots),
                        entry_price=float(entry_price),
                        open_ts=str(ts_dt),
                        open_equity_usd=float(current_equity),
                        entry_trade_prob=float(action["trade_prob"]),
                        entry_session_bucket=str(action["entry_session_bucket"]),
                        entry_scenario_bucket=str(action["entry_scenario_bucket"]),
                        entry_regime_bucket=str(action["entry_regime_bucket"]),
                        entry_uncertainty_score=float(action["entry_uncertainty_score"]),
                        entry_structure_timing_score=float(action["entry_structure_timing_score"]),
                        pair_tier=str(action["pair_tier"]),
                        playbook=str(action["entry_playbook"] or PLAYBOOK_TREND_PULLBACK),
                        sleeve=str(action.get("sleeve") or playbook_to_sleeve(action["entry_playbook"] or PLAYBOOK_TREND_PULLBACK)),
                        environment_state_at_entry=str(action["environment_state"]),
                        entry_location_score=float(action["entry_location_score"]),
                        entry_trigger_score=float(action["entry_trigger_score"]),
                        entry_macro_coherence_score=float(action["entry_macro_coherence_score"]),
                        thesis_id=str(entry_snapshot.thesis_id),
                        campaign_seq=int(entry_snapshot.campaign_seq),
                        campaign_entry_kind=str(entry_snapshot.entry_kind),
                        campaign_state=str(entry_snapshot.state),
                        campaign_state_reason=str(entry_snapshot.state_reason),
                        campaign_state_entered_bar=int(bar_idx),
                        campaign_harvest_count=0,
                        campaign_reattack_count=1 if str(entry_snapshot.entry_kind) == "re_attack_entry" else 0,
                        campaign_abandoned_at_bar=None,
                        sleeve_health_score=float(action.get("sleeve_health_score", 0.5)),
                        sleeve_health_state=str(action.get("sleeve_health_state", "healthy")),
                        allocator_score=float(action.get("allocator_score", 0.0)),
                        aggressive_fallback_used=bool(action["aggressive_fallback_used"]),
                    )
                    prior_campaign_state = str(action.get("campaign_state") or CAMPAIGN_STATE_INACTIVE)
                    apply_campaign_registry_snapshot(
                        campaign_registry,
                        snapshot=entry_snapshot,
                        bar_idx=int(bar_idx),
                        ts=str(ts_str),
                        active_position=True,
                    )
                    transition = campaign_transition_if_changed(
                        prior_state=prior_campaign_state,
                        snapshot=entry_snapshot,
                        bar_idx=int(bar_idx),
                        ts=str(ts_str),
                    )
                    if transition is not None:
                        campaign_events.append(asdict(transition))
                    entry_count += 1
                    entry_events_by_ts[ts_str] += 1
            else:
                for reason in list(action.get("decision_reasons") or []):
                    rejection_counts[str(reason)] += 1

        entry_cumulative_by_ts[ts_str] = int(entry_count)

        open_count = int(len(open_positions))
        exposure_samples += 1
        open_position_total += open_count
        peak_open_positions = max(peak_open_positions, open_count)
        equity_curve.append(
            {
                "ts": ts_str,
                "balance_usd": float(cash_balance),
                "equity_usd": float(
                    BASE._mark_equity(
                        cash_balance=cash_balance,
                        open_positions=open_positions,
                        bar_idx=bar_idx,
                        bid_arrays=bid_arrays,
                        ask_arrays=ask_arrays,
                        mid_arrays=mid_arrays,
                    )
                ),
                "open_positions": open_count,
            }
        )

    final_ts = timeline[-1]
    final_ts_str = str(final_ts)
    final_bar_idx = len(timeline) - 1
    for pair, pos in list(open_positions.items()):
        if str(pos.side) == "long":
            exit_price = BASE._apply_slippage(price=float(bid_arrays[pair][final_bar_idx]), action="long_close", slippage_bps=float(args.slippage_bps))
        else:
            exit_price = BASE._apply_slippage(price=float(ask_arrays[pair][final_bar_idx]), action="short_close", slippage_bps=float(args.slippage_bps))
        realized = BASE._realized_pnl_usd(
            pair=pair,
            side=str(pos.side),
            entry_price=float(pos.entry_price),
            exit_price=float(exit_price),
            lots=float(pos.lots),
            bar_idx=final_bar_idx,
            mid_arrays=mid_arrays,
        )
        cash_balance += realized
        pos.realized_pnl_usd += realized
        close_campaign = campaign_state_after_close(
            position_state=str(getattr(pos, "campaign_state", CAMPAIGN_STATE_INACTIVE)),
            pair=pair,
            side=str(pos.side),
            sleeve=str(getattr(pos, "sleeve", playbook_to_sleeve(getattr(pos, "playbook", "")))),
            row={
                "playbook_score": 0.0,
                "location_score": float(getattr(pos, "entry_location_score", 0.0)),
                "trigger_score": float(getattr(pos, "entry_trigger_score", 0.0)),
                "macro_coherence_score": float(getattr(pos, "entry_macro_coherence_score", 0.0)),
                "hostility_score": 0.0,
                "extension_penalty_score": 0.0,
                "environment_state": "forced_final_close",
            },
            lifecycle_reason="forced_final_close",
            realized_pnl_usd=float(pos.realized_pnl_usd),
            bar_idx=int(final_bar_idx),
            ts=str(final_ts_str),
            config=campaign_config,
            campaign_seq=int(getattr(pos, "campaign_seq", 0) or 0),
            entry_kind=str(getattr(pos, "campaign_entry_kind", "") or ""),
        )
        trade = TwinClosedTrade(
            pair=pair,
            side=str(pos.side),
            open_ts=str(pos.open_ts),
            close_ts=final_ts_str,
            entry_price=float(pos.entry_price),
            exit_price=float(exit_price),
            lots=float(pos.entry_lots),
            realized_pnl_usd=float(pos.realized_pnl_usd),
            holding_bars=max(1, int((_to_utc_ts(final_ts) - _to_utc_ts(pos.open_ts)).total_seconds() // holding_bar_secs)),
            partial_exit_events=int(pos.partial_exit_events),
            close_reason="forced_final_close",
            entry_trade_prob=float(pos.entry_trade_prob),
            exit_action_selected="forced_final_close",
            reversal_failure_prob=0.0,
            reversal_opportunity_prob=0.0,
            entry_session_bucket=str(pos.entry_session_bucket),
            entry_scenario_bucket=str(pos.entry_scenario_bucket),
            entry_regime_bucket=str(pos.entry_regime_bucket),
            entry_uncertainty_score=float(pos.entry_uncertainty_score),
            entry_structure_timing_score=float(pos.entry_structure_timing_score),
            pair_tier=str(pos.pair_tier),
            playbook=str(getattr(pos, "playbook", PLAYBOOK_TREND_PULLBACK)),
            sleeve=str(getattr(pos, "sleeve", playbook_to_sleeve(getattr(pos, "playbook", PLAYBOOK_TREND_PULLBACK)))),
            environment_state_at_entry=str(getattr(pos, "environment_state_at_entry", "")),
            environment_state_at_exit="forced_final_close",
            lifecycle_exit_reason="forced_final_close",
            thesis_id=str(getattr(pos, "thesis_id", "") or close_campaign.thesis_id),
            campaign_seq=int(getattr(pos, "campaign_seq", 0) or close_campaign.campaign_seq or 0),
            campaign_entry_kind=str(getattr(pos, "campaign_entry_kind", "") or close_campaign.entry_kind or ""),
            campaign_state=str(close_campaign.state),
            campaign_state_reason=str(close_campaign.state_reason),
            campaign_proof_score=float(close_campaign.proof_score),
            campaign_maturity_score=float(close_campaign.maturity_score),
            campaign_reset_quality=float(close_campaign.reset_quality),
            campaign_priority_boost=float(close_campaign.priority_boost),
            allocator_score=float(getattr(pos, "allocator_score", 0.0)),
            replacement_value=0.0,
            sleeve_health_score=float(getattr(pos, "sleeve_health_score", 0.5)),
            sleeve_health_state=str(getattr(pos, "sleeve_health_state", "healthy")),
            aggressive_fallback_used=bool(getattr(pos, "aggressive_fallback_used", False)),
        )
        closed_trades.append(trade)
        sleeve_tracker.record_trade(
            sleeve=str(getattr(trade, "sleeve", playbook_to_sleeve(getattr(trade, "playbook", "")))),
            realized_pnl_usd=float(getattr(trade, "realized_pnl_usd", 0.0)),
            holding_bars=float(getattr(trade, "holding_bars", 0.0)),
            partial_exit_events=int(getattr(trade, "partial_exit_events", 0)),
            close_reason=str(getattr(trade, "close_reason", "")),
            session_bucket=str(getattr(trade, "entry_session_bucket", "")),
            pair=str(getattr(trade, "pair", "")),
        )
        close_reason_counts["forced_final_close"] += 1
        pnl_by_close_reason["forced_final_close"] += float(pos.realized_pnl_usd)
        recent_exit_registry[pair] = {
            "bar_idx": int(final_bar_idx),
            "side": str(pos.side),
            "playbook": str(getattr(pos, "playbook", PLAYBOOK_TREND_PULLBACK)),
            "reason": "forced_final_close",
            "thesis_id": str(getattr(pos, "thesis_id", "") or close_campaign.thesis_id),
            "campaign_state": str(close_campaign.state),
        }
        apply_campaign_registry_snapshot(
            campaign_registry,
            snapshot=close_campaign,
            bar_idx=int(final_bar_idx),
            ts=str(final_ts_str),
            active_position=False,
            realized_pnl_usd=float(pos.realized_pnl_usd),
        )
        transition = campaign_transition_if_changed(
            prior_state=str(getattr(pos, "campaign_state", CAMPAIGN_STATE_INACTIVE)),
            snapshot=close_campaign,
            bar_idx=int(final_bar_idx),
            ts=str(final_ts_str),
            realized_pnl_usd=float(pos.realized_pnl_usd),
            holding_bars=float(max(1, int((_to_utc_ts(final_ts) - _to_utc_ts(pos.open_ts)).total_seconds() // holding_bar_secs))),
        )
        if transition is not None:
            campaign_events.append(asdict(transition))
        open_positions.pop(pair, None)

    equity_df = pd.DataFrame(equity_curve)
    equity_df = pd.concat(
        [
            equity_df,
            pd.DataFrame(
                [
                    {
                        "ts": final_ts_str,
                        "balance_usd": float(cash_balance),
                        "equity_usd": float(cash_balance),
                        "open_positions": 0,
                    }
                ]
            ),
        ],
        ignore_index=True,
    )
    trades_df = pd.DataFrame([asdict(t) for t in closed_trades])
    if not trades_df.empty:
        trades_df = trades_df.sort_values(["close_ts", "pair"]).reset_index(drop=True)
    if equity_df.empty:
        raise RuntimeError("equity curve is empty")
    equity_df["equity_peak_usd"] = equity_df["equity_usd"].cummax()
    equity_df["drawdown_usd"] = equity_df["equity_usd"] - equity_df["equity_peak_usd"]
    equity_df["drawdown_pct"] = np.where(
        equity_df["equity_peak_usd"] > 0.0,
        ((equity_df["equity_usd"] / equity_df["equity_peak_usd"]) - 1.0) * 100.0,
        0.0,
    )

    gross_profit = float(trades_df.loc[trades_df["realized_pnl_usd"] > 0.0, "realized_pnl_usd"].sum()) if not trades_df.empty else 0.0
    gross_loss = float(trades_df.loc[trades_df["realized_pnl_usd"] < 0.0, "realized_pnl_usd"].sum()) if not trades_df.empty else 0.0
    wins = int((trades_df["realized_pnl_usd"] > 0.0).sum()) if not trades_df.empty else 0
    losses = int((trades_df["realized_pnl_usd"] < 0.0).sum()) if not trades_df.empty else 0
    flats = int((trades_df["realized_pnl_usd"] == 0.0).sum()) if not trades_df.empty else 0
    total_trades = int(len(trades_df))
    total_return_pct = ((float(cash_balance) / float(args.start_equity)) - 1.0) * 100.0 if float(args.start_equity) > 0.0 else 0.0
    net_pnl_usd = float(cash_balance - float(args.start_equity))
    test_days = max(1e-9, float((_to_utc_ts(end_ts) - _to_utc_ts(start_ts)).total_seconds()) / 86400.0)
    cagr_equiv_pct = float(((float(cash_balance) / float(args.start_equity)) ** (365.25 / test_days) - 1.0) * 100.0) if float(args.start_equity) > 0.0 else 0.0
    max_drawdown_pct = float(equity_df["drawdown_pct"].min()) if not equity_df.empty else 0.0
    max_drawdown_usd = float(equity_df["drawdown_usd"].min()) if not equity_df.empty else 0.0
    max_drawdown_duration_bars = _max_drawdown_duration_bars(equity_df["drawdown_usd"].to_numpy(dtype=float))
    ulcer_index = _ulcer_index(equity_df["drawdown_pct"].to_numpy(dtype=float))
    sharpe_like = _sharpe_like(equity_df["equity_usd"].to_numpy(dtype=float))
    recovery_factor = float(net_pnl_usd / abs(max_drawdown_usd)) if max_drawdown_usd < 0.0 else 0.0
    avg_open_positions = float(open_position_total / max(1, exposure_samples))
    slot_utilization_rate = float(avg_open_positions / max(1, int(s.max_total_positions)))
    expectancy_per_trade = float(trades_df["realized_pnl_usd"].mean()) if not trades_df.empty else 0.0

    per_pair_records: list[dict[str, Any]] = []
    for pair in pairs:
        pair_df = trades_df[trades_df["pair"] == pair].copy() if not trades_df.empty else pd.DataFrame()
        gross_profit_pair = float(pair_df.loc[pair_df["realized_pnl_usd"] > 0.0, "realized_pnl_usd"].sum()) if not pair_df.empty else 0.0
        gross_loss_pair = float(pair_df.loc[pair_df["realized_pnl_usd"] < 0.0, "realized_pnl_usd"].sum()) if not pair_df.empty else 0.0
        pair_dec = collector.by_pair.get(pair, {"decisions": 0, "allowed": 0, "reasons": Counter(), "shadow_reasons": Counter()})
        pair_expectancy = float(pair_df["realized_pnl_usd"].mean()) if not pair_df.empty else 0.0
        per_pair_records.append(
            {
                "pair": pair,
                "pair_tier": str(_shadow_pair_tier(s, pair)),
                "decisions": int(pair_dec.get("decisions", 0)),
                "allow_rate": float(pair_dec.get("allowed", 0) / max(1, pair_dec.get("decisions", 0))),
                "trades": int(len(pair_df)),
                "wins": int((pair_df["realized_pnl_usd"] > 0.0).sum()) if not pair_df.empty else 0,
                "losses": int((pair_df["realized_pnl_usd"] < 0.0).sum()) if not pair_df.empty else 0,
                "win_rate": float((pair_df["realized_pnl_usd"] > 0.0).mean()) if not pair_df.empty else 0.0,
                "net_pnl_usd": float(pair_df["realized_pnl_usd"].sum()) if not pair_df.empty else 0.0,
                "expectancy_usd": float(pair_expectancy),
                "profit_factor": float(gross_profit_pair / abs(gross_loss_pair)) if gross_loss_pair < 0.0 else (float("inf") if gross_profit_pair > 0.0 else 0.0),
                "avg_trade_pnl_usd": float(pair_expectancy),
                "median_trade_pnl_usd": float(pair_df["realized_pnl_usd"].median()) if not pair_df.empty else 0.0,
                "avg_holding_bars": float(pair_df["holding_bars"].mean()) if not pair_df.empty else 0.0,
                "partial_exit_events": int(pair_df["partial_exit_events"].sum()) if not pair_df.empty else 0,
                "long_trades": int((pair_df["side"] == "long").sum()) if not pair_df.empty else 0,
                "short_trades": int((pair_df["side"] == "short").sum()) if not pair_df.empty else 0,
                "primary_rejections": dict(pair_dec.get("reasons", Counter())),
                "shadow_rejections": dict(pair_dec.get("shadow_reasons", Counter())),
            }
        )
    per_pair_records = sorted(per_pair_records, key=lambda row: (_safe_float(row.get("net_pnl_usd"), 0.0), _safe_float(row.get("expectancy_usd"), 0.0)), reverse=True)

    if not trades_df.empty:
        side_breakdown_df = trades_df.groupby("side").agg(
            trades=("side", "count"),
            net_pnl_usd=("realized_pnl_usd", "sum"),
            avg_trade_pnl_usd=("realized_pnl_usd", "mean"),
            expectancy_usd=("realized_pnl_usd", "mean"),
            win_rate=("realized_pnl_usd", lambda s: float((s > 0.0).mean())),
        ).reset_index()
    else:
        side_breakdown_df = pd.DataFrame(columns=["side", "trades", "net_pnl_usd", "avg_trade_pnl_usd", "expectancy_usd", "win_rate"])

    rejections_by_pair = {}
    for pair, row in collector.by_pair.items():
        decisions = int(row["decisions"])
        allowed = int(row["allowed"])
        reject_count = max(0, decisions - allowed)
        rejections_by_pair[pair] = {
            "decisions": decisions,
            "allow_count": allowed,
            "reject_count": reject_count,
            "reasons": {k: int(v) for k, v in Counter(row["reasons"]).items()},
            "shadow_reasons": {k: int(v) for k, v in Counter(row["shadow_reasons"]).items()},
            "spread_reject_sessions": {k: int(v) for k, v in collector.spread_rejects_by_pair_session.get(pair, Counter()).items()},
        }
    rejections_by_session = {}
    for session, row in collector.by_session.items():
        decisions = int(row["decisions"])
        allowed = int(row["allowed"])
        reject_count = max(0, decisions - allowed)
        rejections_by_session[session] = {
            "decisions": decisions,
            "allow_count": allowed,
            "reject_count": reject_count,
            "reasons": {k: int(v) for k, v in Counter(row["reasons"]).items()},
            "pairs": {k: int(v) for k, v in Counter(row["pairs"]).items()},
            "spread_rejects": int(sum(int(v) for pair_counter in collector.spread_rejects_by_pair_session.values() for sess, v in pair_counter.items() if sess == session)),
        }

    pnl_by_close_reason_rows = []
    if not trades_df.empty:
        for close_reason, grp in trades_df.groupby("close_reason"):
            pnl_by_close_reason_rows.append(
                {
                    "close_reason": str(close_reason),
                    "trades": int(len(grp)),
                    "net_pnl_usd": float(grp["realized_pnl_usd"].sum()),
                    "avg_trade_pnl_usd": float(grp["realized_pnl_usd"].mean()),
                }
            )
    pnl_by_session = []
    pnl_by_scenario = []
    pnl_by_regime = []
    if not trades_df.empty:
        for field_name, target in [
            ("entry_session_bucket", pnl_by_session),
            ("entry_scenario_bucket", pnl_by_scenario),
            ("entry_regime_bucket", pnl_by_regime),
        ]:
            for bucket, grp in trades_df.groupby(field_name):
                target.append(
                    {
                        field_name.replace("entry_", ""): str(bucket),
                        "trades": int(len(grp)),
                        "net_pnl_usd": float(grp["realized_pnl_usd"].sum()),
                        "avg_trade_pnl_usd": float(grp["realized_pnl_usd"].mean()),
                        "win_rate": float((grp["realized_pnl_usd"] > 0.0).mean()),
                    }
                )

    uncertainty_summary = {
        "uncertainty_gate_rejects": int(collector.shadow_rejections.get("shadow_uncertainty_gate", 0)),
        "buckets": [],
    }
    if not trades_df.empty:
        trades_df["entry_uncertainty_bucket"] = trades_df["entry_uncertainty_score"].map(_uncertainty_bucket)
    for bucket, count in sorted(collector.uncertainty_buckets.items()):
        bucket_df = trades_df[trades_df["entry_uncertainty_bucket"] == bucket] if not trades_df.empty else pd.DataFrame()
        uncertainty_summary["buckets"].append(
            {
                "bucket": bucket,
                "count": int(count),
                "trades": int(len(bucket_df)) if not bucket_df.empty else 0,
                "net_pnl_usd": float(bucket_df["realized_pnl_usd"].sum()) if not bucket_df.empty else 0.0,
                "avg_trade_pnl_usd": float(bucket_df["realized_pnl_usd"].mean()) if not bucket_df.empty else 0.0,
                "primary_rejects": {k: int(v) for k, v in collector.primary_rejections.items() if k != "none"},
            }
        )

    structure_rescues_by_pair = Counter()
    for row in collector.history.rows:
        if bool(row.get("structure_rescue_active")):
            structure_rescues_by_pair[str(row.get("pair") or "")] += 1
    near_miss_rows = sorted(
        collector.structure_near_miss_rows,
        key=lambda row: (-_safe_float(row.get("structure_timing_score"), 0.0), -_safe_float(row.get("entry_quality_score_shadow"), 0.0), row.get("pair", "")),
    )[:50]
    structure_summary = {
        "structure_rescue_count": int(collector.structure_rescues),
        "count_by_bucket": {bucket: int(count) for bucket, count in sorted(collector.structure_buckets.items())},
        "count_by_rescue_flag": {
            "rescued": int(collector.structure_rescues),
            "not_rescued": int(max(0, collector.total - collector.structure_rescues)),
        },
        "near_miss_count": int(len(collector.structure_near_miss_rows)),
        "near_miss_reasons": dict(Counter(str(row.get("shadow_rejection_reason") or "") for row in collector.structure_near_miss_rows)),
        "near_miss_candidates": near_miss_rows,
        "top_rescued_pairs": {pair: int(count) for pair, count in structure_rescues_by_pair.most_common(10)},
        "top_unrecovered_high_structure_rejects": {
            pair: int(count)
            for pair, count in Counter(str(row.get("pair") or "") for row in collector.structure_near_miss_rows).most_common(10)
        },
    }

    lifecycle_summary = {
        "policy_mode": "model_driven",
        "action_counts": {k: int(v) for k, v in action_counts.items()},
        "close_reason_counts": {k: int(v) for k, v in close_reason_counts.items()},
        "pnl_by_close_reason": {k: float(v) for k, v in pnl_by_close_reason.items()},
        "repeated_partial_reduce_trades": int((trades_df["partial_exit_events"] > 1).sum()) if not trades_df.empty else 0,
        "partial_exit_trade_share": float((trades_df["partial_exit_events"] > 0).mean()) if not trades_df.empty else 0.0,
        "pnl_after_partial_exit_trades_usd": float(trades_df.loc[trades_df["partial_exit_events"] > 0, "realized_pnl_usd"].sum()) if not trades_df.empty else 0.0,
        "counter_signal_exit_count": int((trades_df["close_reason"] == "reversal_models_exit").sum()) if not trades_df.empty else 0,
        "model_exit_count": int(trades_df["close_reason"].isin(["exit_model_exit", "exit_model_partial_tp", "exit_model_reduce", "exit_model_reduce_to_flat"]).sum()) if not trades_df.empty else 0,
    }
    sleeve_health_snapshots_final = sleeve_tracker.snapshot()

    environment_summary = {}
    for environment_state, row in collector.by_environment.items():
        state_trades = trades_df[trades_df["environment_state_at_entry"] == environment_state] if not trades_df.empty and "environment_state_at_entry" in trades_df.columns else pd.DataFrame()
        environment_summary[environment_state] = {
            "decisions": int(row["decisions"]),
            "allow_count": int(row["allowed"]),
            "entries": int(row["allowed"]),
            "trades": int(len(state_trades)) if not state_trades.empty else 0,
            "net_pnl_usd": float(state_trades["realized_pnl_usd"].sum()) if not state_trades.empty else 0.0,
            "win_rate": float((state_trades["realized_pnl_usd"] > 0.0).mean()) if not state_trades.empty else 0.0,
            "reasons": {k: int(v) for k, v in Counter(row["reasons"]).items()},
        }

    playbook_summary = {}
    for playbook, row in collector.by_playbook.items():
        sleeve = playbook_to_sleeve(playbook)
        pb_trades = trades_df[trades_df["playbook"] == playbook] if not trades_df.empty and "playbook" in trades_df.columns else pd.DataFrame()
        sleeve_snapshot = sleeve_health_snapshots_final.get(sleeve)
        exit_reason_mix = dict(Counter(pb_trades["close_reason"])) if not pb_trades.empty else {}
        playbook_summary[playbook] = {
            "decisions": int(row["decisions"]),
            "allow_count": int(row["allowed"]),
            "entries": int(row["allowed"]),
            "trades": int(len(pb_trades)) if not pb_trades.empty else 0,
            "net_pnl_usd": float(pb_trades["realized_pnl_usd"].sum()) if not pb_trades.empty else 0.0,
            "expectancy_per_trade_usd": float(pb_trades["realized_pnl_usd"].mean()) if not pb_trades.empty else 0.0,
            "profit_factor": float(
                pb_trades.loc[pb_trades["realized_pnl_usd"] > 0.0, "realized_pnl_usd"].sum()
                / max(abs(pb_trades.loc[pb_trades["realized_pnl_usd"] < 0.0, "realized_pnl_usd"].sum()), 1e-9)
            ) if not pb_trades.empty and float(abs(pb_trades.loc[pb_trades["realized_pnl_usd"] < 0.0, "realized_pnl_usd"].sum())) > 0.0 else (float(pb_trades.loc[pb_trades["realized_pnl_usd"] > 0.0, "realized_pnl_usd"].sum()) if not pb_trades.empty else 0.0),
            "win_rate": float((pb_trades["realized_pnl_usd"] > 0.0).mean()) if not pb_trades.empty else 0.0,
            "avg_holding_bars": float(pb_trades["holding_bars"].mean()) if not pb_trades.empty else 0.0,
            "partial_frequency": float((pb_trades["partial_exit_events"] > 0).mean()) if not pb_trades.empty else 0.0,
            "exit_reason_mix": {k: int(v) for k, v in exit_reason_mix.items()},
            "aggressive_fallback_share": float(row["aggressive_fallbacks"] / max(1, row["allowed"])),
            "drawdown_contribution_usd": float(abs(pb_trades.loc[pb_trades["realized_pnl_usd"] < 0.0, "realized_pnl_usd"].sum())) if not pb_trades.empty else 0.0,
            "replacement_exit_count": int((pb_trades["close_reason"] == "adaptive_replacement_exit").sum()) if not pb_trades.empty else 0,
            "health_score": float(getattr(sleeve_snapshot, "score", 0.5)),
            "health_state": str(getattr(sleeve_snapshot, "state", "healthy")),
            "pairs": {k: int(v) for k, v in Counter(row["pairs"]).items()},
        }

    sleeve_health_summary = serialize_sleeve_snapshots(sleeve_health_snapshots_final)
    allocator_summary = {
        "allocator_candidate_count": int(collector.allocator_candidates),
        "allocator_selected_count": int(collector.allocator_selected),
        "allocator_ranked_out_count": int(collector.allocator_ranked_out),
        "allocator_replacement_count": int(collector.allocator_replacements),
        "sleeve_decision_counts": {
            sleeve: int(data["decisions"])
            for sleeve, data in sorted(collector.by_sleeve.items())
        },
        "sleeve_allow_counts": {
            sleeve: int(data["allowed"])
            for sleeve, data in sorted(collector.by_sleeve.items())
        },
    }
    replacement_trades = trades_df[trades_df["close_reason"] == "adaptive_replacement_exit"] if not trades_df.empty else pd.DataFrame()
    replacement_summary = {
        "replacement_exit_count": int(len(replacement_trades)) if not replacement_trades.empty else 0,
        "replacement_exit_net_pnl_usd": float(replacement_trades["realized_pnl_usd"].sum()) if not replacement_trades.empty else 0.0,
        "replacement_exit_pairs": dict(Counter(replacement_trades["pair"])) if not replacement_trades.empty else {},
        "replacement_value_total": float(trades_df.get("replacement_value", pd.Series(dtype=float)).sum()) if not trades_df.empty and "replacement_value" in trades_df.columns else 0.0,
    }
    closed_campaign_trades_df = (
        trades_df[trades_df.get("campaign_seq", pd.Series(dtype=int)).fillna(0).astype(int) > 0]
        if not trades_df.empty and "campaign_seq" in trades_df.columns
        else pd.DataFrame()
    )
    campaign_entry_events = [
        event
        for event in campaign_events
        if str(event.get("new_state") or "") == CAMPAIGN_STATE_PROBE
    ]
    campaign_summary = {
        "campaigns_started": int(len(campaign_entry_events)),
        "campaigns_completed": int(len(closed_campaign_trades_df)) if not closed_campaign_trades_df.empty else 0,
        "campaigns_abandoned": int(
            sum(
                1
                for event in campaign_events
                if str(event.get("new_state") or "") == CAMPAIGN_STATE_ABANDONED
                and str(event.get("prior_state") or "") in {CAMPAIGN_STATE_PROBE, "confirmed", "press", CAMPAIGN_STATE_HARVEST}
            )
        ),
        "re_attacks_by_sleeve": dict(
            Counter(
                str(event.get("sleeve") or "")
                for event in campaign_entry_events
                if str(event.get("entry_kind") or "") == "re_attack_entry"
            )
        ),
        "harvest_counts_by_sleeve": dict(Counter(str(event.get("sleeve") or "") for event in campaign_events if str(event.get("new_state") or "") == CAMPAIGN_STATE_HARVEST)),
        "avg_campaign_length_bars": float(closed_campaign_trades_df["holding_bars"].mean()) if not closed_campaign_trades_df.empty and "holding_bars" in closed_campaign_trades_df.columns else 0.0,
        "avg_campaign_pnl_usd": float(closed_campaign_trades_df["realized_pnl_usd"].mean()) if not closed_campaign_trades_df.empty and "realized_pnl_usd" in closed_campaign_trades_df.columns else 0.0,
        "fresh_probe_expectancy_usd": float(
            closed_campaign_trades_df.loc[
                closed_campaign_trades_df["campaign_entry_kind"].fillna("") == "fresh_probe",
                "realized_pnl_usd",
            ].mean()
        ) if not closed_campaign_trades_df.empty and "campaign_entry_kind" in closed_campaign_trades_df.columns else 0.0,
        "re_attack_expectancy_usd": float(
            closed_campaign_trades_df.loc[
                closed_campaign_trades_df["campaign_entry_kind"].fillna("") == "re_attack_entry",
                "realized_pnl_usd",
            ].mean()
        ) if not closed_campaign_trades_df.empty and "campaign_entry_kind" in closed_campaign_trades_df.columns else 0.0,
    }
    campaign_state_summary = {
        "state_counts": {
            state: int(data["decisions"])
            for state, data in sorted(collector.by_campaign_state.items())
        },
        "allow_counts": {
            state: int(data["allowed"])
            for state, data in sorted(collector.by_campaign_state.items())
        },
        "transition_counts": dict(
            Counter(
                f"{str(event.get('prior_state') or CAMPAIGN_STATE_INACTIVE)}->{str(event.get('new_state') or CAMPAIGN_STATE_INACTIVE)}"
                for event in campaign_events
            )
        ),
        "abandon_reasons": dict(Counter(str(event.get("reason") or "") for event in campaign_events if str(event.get("new_state") or "") == CAMPAIGN_STATE_ABANDONED)),
        "re_attack_reasons": dict(Counter(str(event.get("reason") or "") for event in campaign_events if str(event.get("new_state") or "") == "re_attack_ready")),
        "memory_state_counts": dict(
            Counter(
                str(row.get("campaign_state") or CAMPAIGN_STATE_INACTIVE)
                for row in collector.history.rows
                if str(row.get("campaign_state") or CAMPAIGN_STATE_INACTIVE) in {CAMPAIGN_STATE_INACTIVE, "re_attack_ready", CAMPAIGN_STATE_ABANDONED}
            )
        ),
        "active_state_counts": dict(
            Counter(
                str(row.get("campaign_state") or CAMPAIGN_STATE_INACTIVE)
                for row in collector.history.rows
                if str(row.get("campaign_state") or CAMPAIGN_STATE_INACTIVE) in {CAMPAIGN_STATE_PROBE, "confirmed", "press", CAMPAIGN_STATE_HARVEST}
            )
        ),
        "campaign_entry_kind_counts": dict(
            Counter(str(event.get("entry_kind") or "") for event in campaign_entry_events if str(event.get("entry_kind") or ""))
        ),
        "replacement_protected_holds": int(sum(1 for row in collector.history.rows if str(row.get("campaign_state") or "") == "press" and str(row.get("lifecycle_action") or "") == "hold")),
        "campaign_rotation_exits": int(sum(1 for trade in closed_trades if str(getattr(trade, "close_reason", "")) in {"adaptive_replacement_exit", "adaptive_tempo_rotation_exit"})),
        "registry": {k: serialize_campaign_entry(v) for k, v in sorted(campaign_registry.items())},
    }
    history_rows = sorted(collector.history.rows, key=lambda row: (str(row.get("ts") or ""), str(row.get("pair") or "")))
    trade_pnl_by_key = {(str(trade.pair), str(trade.open_ts)): float(trade.realized_pnl_usd) for trade in closed_trades}
    belief_rows = [
        row
        for row in history_rows
        if str(row.get("belief_source_mode") or "") not in {"", "disabled", "artifact_missing"}
    ]
    belief_entry_rows = [
        row
        for row in belief_rows
        if str(row.get("lifecycle_action") or "") == "entry"
        and int(_safe_int(row.get("position_count_pair"), 0)) == 0
    ]
    belief_entries_df = pd.DataFrame(belief_entry_rows)
    if not belief_entries_df.empty:
        belief_entries_df["trade_pnl_usd"] = [
            float(trade_pnl_by_key.get((str(row.pair), str(row.ts)), 0.0))
            for row in belief_entries_df.itertuples(index=False)
        ]
        belief_entries_df["belief_primary_side_match"] = [
            str(getattr(row, "belief_primary_side", "") or "").strip().lower()
            == ("long" if str(getattr(row, "side", "")).upper() == "BUY" else "short")
            for row in belief_entries_df.itertuples(index=False)
        ]
        belief_entries_df["belief_directional_hit"] = [
            bool(getattr(row, "belief_primary_side_match", False)) and float(getattr(row, "trade_pnl_usd", 0.0)) > 0.0
            for row in belief_entries_df.itertuples(index=False)
        ]
    shared_overlay_diagnostics = _shared_overlay_diagnostics(history_rows if collector.emit_history else [])
    belief_gap_deciles = _metric_deciles(
        decisions_df=belief_entries_df if not belief_entries_df.empty else pd.DataFrame(),
        value_col="belief_gap",
        trade_pnl_by_key=trade_pnl_by_key,
    )
    fragility_deciles = _metric_deciles(
        decisions_df=belief_entries_df if not belief_entries_df.empty else pd.DataFrame(),
        value_col="belief_fragility_score",
        trade_pnl_by_key=trade_pnl_by_key,
    )
    ev_prob_deciles = _metric_deciles(
        decisions_df=belief_entries_df if not belief_entries_df.empty else pd.DataFrame(),
        value_col="belief_primary_ev_above_hurdle_prob",
        trade_pnl_by_key=trade_pnl_by_key,
    )
    fail_fast_prob_deciles = _metric_deciles(
        decisions_df=belief_entries_df if not belief_entries_df.empty else pd.DataFrame(),
        value_col="belief_primary_fail_fast_prob",
        trade_pnl_by_key=trade_pnl_by_key,
    )
    scenario_expectancy = []
    if not belief_entries_df.empty and "belief_primary_scenario" in belief_entries_df.columns:
        for scenario_name, grp in belief_entries_df.groupby("belief_primary_scenario"):
            scenario_expectancy.append(
                {
                    "scenario": str(scenario_name),
                    "count": int(len(grp)),
                    "expectancy_usd": float(grp["trade_pnl_usd"].mean()),
                    "hit_rate": float(grp["trade_pnl_usd"].gt(0.0).mean()),
                }
            )
    belief_summary = {
        "belief_enabled": bool(belief_enabled),
        "belief_overlay_enabled": bool(belief_overlay_enabled),
        "decision_rows_with_belief": int(len(belief_rows)),
        "entry_rows_with_belief": int(len(belief_entry_rows)),
        "avg_belief_gap": _mean_or_zero(belief_entries_df["belief_gap"]) if not belief_entries_df.empty and "belief_gap" in belief_entries_df.columns else 0.0,
        "avg_fragility_score": _mean_or_zero(belief_entries_df["belief_fragility_score"]) if not belief_entries_df.empty and "belief_fragility_score" in belief_entries_df.columns else 0.0,
        "avg_primary_rank_score": _mean_or_zero(belief_entries_df["belief_primary_rank_score"]) if not belief_entries_df.empty and "belief_primary_rank_score" in belief_entries_df.columns else 0.0,
        "avg_primary_ev_above_hurdle_prob": _mean_or_zero(belief_entries_df["belief_primary_ev_above_hurdle_prob"]) if not belief_entries_df.empty and "belief_primary_ev_above_hurdle_prob" in belief_entries_df.columns else 0.0,
        "avg_primary_expected_net_ev_bps": _mean_or_zero(belief_entries_df["belief_primary_expected_net_ev_bps"]) if not belief_entries_df.empty and "belief_primary_expected_net_ev_bps" in belief_entries_df.columns else 0.0,
        "avg_primary_fail_fast_prob": _mean_or_zero(belief_entries_df["belief_primary_fail_fast_prob"]) if not belief_entries_df.empty and "belief_primary_fail_fast_prob" in belief_entries_df.columns else 0.0,
        "belief_primary_side_hit_rate": _mean_or_zero(belief_entries_df["belief_directional_hit"]) if not belief_entries_df.empty and "belief_directional_hit" in belief_entries_df.columns else 0.0,
        "directional_swing_proxy_hit_rate": _mean_or_zero(belief_entries_df["trade_pnl_usd"].gt(0.0).astype(float)) if not belief_entries_df.empty else 0.0,
        "belief_vs_proxy_hit_rate_delta": (
            _mean_or_zero(belief_entries_df["belief_directional_hit"]) - _mean_or_zero(belief_entries_df["trade_pnl_usd"].gt(0.0).astype(float))
        ) if not belief_entries_df.empty and "belief_directional_hit" in belief_entries_df.columns else 0.0,
        "top_belief_gap_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_gap"].rank(pct=True) >= 0.80, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_gap" in belief_entries_df.columns else 0.0,
        "bottom_belief_gap_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_gap"].rank(pct=True) <= 0.20, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_gap" in belief_entries_df.columns else 0.0,
        "low_fragility_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_fragility_score"].rank(pct=True) <= 0.20, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_fragility_score" in belief_entries_df.columns else 0.0,
        "high_fragility_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_fragility_score"].rank(pct=True) >= 0.80, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_fragility_score" in belief_entries_df.columns else 0.0,
        "top_ev_prob_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_primary_ev_above_hurdle_prob"].rank(pct=True) >= 0.80, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_primary_ev_above_hurdle_prob" in belief_entries_df.columns else 0.0,
        "bottom_ev_prob_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_primary_ev_above_hurdle_prob"].rank(pct=True) <= 0.20, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_primary_ev_above_hurdle_prob" in belief_entries_df.columns else 0.0,
        "top_fail_fast_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_primary_fail_fast_prob"].rank(pct=True) >= 0.80, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_primary_fail_fast_prob" in belief_entries_df.columns else 0.0,
        "bottom_fail_fast_quintile_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_primary_fail_fast_prob"].rank(pct=True) <= 0.20, "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_primary_fail_fast_prob" in belief_entries_df.columns else 0.0,
        "no_edge_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_primary_scenario"].fillna("") == "no_edge", "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_primary_scenario" in belief_entries_df.columns else 0.0,
        "tradeable_expectancy_usd": _mean_or_zero(belief_entries_df.loc[belief_entries_df["belief_primary_scenario"].fillna("") != "no_edge", "trade_pnl_usd"]) if not belief_entries_df.empty and "belief_primary_scenario" in belief_entries_df.columns else 0.0,
        "primary_scenario_counts": dict(Counter(str(row.get("belief_primary_scenario") or "") for row in belief_rows if str(row.get("belief_primary_scenario") or ""))),
        "opposing_scenario_counts": dict(Counter(str(row.get("belief_opposing_scenario") or "") for row in belief_rows if str(row.get("belief_opposing_scenario") or ""))),
        "opposition_side_counts": dict(Counter(str(row.get("belief_opposing_side") or "") for row in belief_rows if str(row.get("belief_opposing_side") or ""))),
        "scenario_expectancy": sorted(scenario_expectancy, key=lambda item: (-float(item["expectancy_usd"]), item["scenario"])),
        "overlay_adjustment": dict(shared_overlay_diagnostics["overlay_adjustment"]),
    }
    belief_deciles = {
        "belief_gap": belief_gap_deciles,
        "fragility_score": fragility_deciles,
        "ev_above_hurdle_prob": ev_prob_deciles,
        "fail_fast_prob": fail_fast_prob_deciles,
    }

    portfolio_crowding_summary = {
        "currency_crowding_penalty_sum": float(collector.crowding_penalty_sum),
        "currency_crowding_penalty_nonzero": int(collector.crowding_penalty_nonzero),
        "avg_currency_crowding_penalty": float(collector.crowding_penalty_sum / max(1, collector.total)),
        "playbook_diversification_penalty_sum": float(collector.diversification_penalty_sum),
        "playbook_diversification_penalty_nonzero": int(collector.diversification_penalty_nonzero),
        "avg_playbook_diversification_penalty": float(collector.diversification_penalty_sum / max(1, collector.total)),
        "aggressive_fallback_count": int(collector.aggressive_fallback_count),
        "playbook_mix": summarize_playbook_mix(history_rows if collector.emit_history else []),
        "sleeve_mix": {
            sleeve: int(data["decisions"])
            for sleeve, data in sorted(collector.by_sleeve.items())
        },
        "same_direction_usd_playbook_counts": {
            playbook: int(sum(1 for trade in closed_trades if str(getattr(trade, "playbook", "")) == playbook and "USD" in str(trade.pair)))
            for playbook in sorted({str(getattr(trade, "playbook", "")) for trade in closed_trades})
        },
    }

    validation_result, recent_live_comparison = _compare_live_overlap(live_flat=live_flat, twin_rows=collector.validation_records)
    run_status = "ok" if validation_result.status == "ok" else str(validation_result.status)

    aggregate_metrics = TwinAggregateMetrics(
        run_status=str(run_status),
        start_equity_usd=float(args.start_equity),
        end_equity_usd=float(cash_balance),
        total_return_pct=float(total_return_pct),
        net_pnl_usd=float(net_pnl_usd),
        trades=int(total_trades),
        entries=int(entry_count),
        wins=int(wins),
        losses=int(losses),
        flats=int(flats),
        win_rate=float((wins / total_trades) if total_trades > 0 else 0.0),
        profit_factor=float(gross_profit / abs(gross_loss)) if gross_loss < 0.0 else (float("inf") if gross_profit > 0.0 else 0.0),
        max_drawdown_pct=float(max_drawdown_pct),
        max_drawdown_usd=float(max_drawdown_usd),
        max_drawdown_duration_bars=int(max_drawdown_duration_bars),
        ulcer_index=float(ulcer_index),
        sharpe_like=float(sharpe_like),
        recovery_factor=float(recovery_factor),
        avg_open_positions=float(avg_open_positions),
        peak_open_positions=int(peak_open_positions),
        slot_utilization_rate=float(slot_utilization_rate),
        expectancy_per_trade_usd=float(expectancy_per_trade),
        partial_exit_events=int(partial_exit_count),
        reversal_exit_events=int(reversal_exit_count),
        forced_final_close_share=float((trades_df["close_reason"] == "forced_final_close").mean()) if not trades_df.empty else 0.0,
        rejection_counts={k: int(v) for k, v in sorted(rejection_counts.items(), key=lambda item: (-item[1], item[0]))},
        metadata={
            "twin_version": TWIN_VERSION,
            "policy_version": POLICY_VERSION,
            "edge_formula_id": EDGE_FORMULA_ID,
            "exec_mode": str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE),
            "pairs": list(pairs),
            "start_ts": str(start_ts),
            "end_ts": str(end_ts),
            "cagr_equivalent_pct": float(cagr_equiv_pct),
            "gross_profit_usd": float(gross_profit),
            "gross_loss_usd": float(gross_loss),
            "avg_trade_pnl_usd": float(trades_df["realized_pnl_usd"].mean()) if not trades_df.empty else 0.0,
            "median_trade_pnl_usd": float(trades_df["realized_pnl_usd"].median()) if not trades_df.empty else 0.0,
            "avg_holding_bars": float(trades_df["holding_bars"].mean()) if not trades_df.empty else 0.0,
            "slippage_bps_per_execution": float(args.slippage_bps),
            "causal_replay": {
                "enabled": True,
                "future_data_access": "forbidden",
                "fill_delay_bars": int(fill_delay_bars),
                "decision_price_basis": "prior_closed_bar",
                "execution_price_basis": "delayed_bar_bid_ask_close",
                "decision_start_ts": str(decision_timeline[0]),
                "decision_end_ts": str(decision_timeline[-1]),
                "execution_start_ts": str(timeline[0]),
                "execution_end_ts": str(timeline[-1]),
            },
            "average_open_positions": float(avg_open_positions),
            "shadow_candidate_rate": float(collector.shadow_candidates / max(1, collector.total)),
            "shadow_would_trade_rate": float(collector.shadow_would_trade / max(1, collector.total)),
            "structure_rescue_share": float(collector.structure_rescues / max(1, collector.total)),
            "shadow_rejection_counts": {k: int(v) for k, v in collector.shadow_rejections.items()},
            "shadow_divergence_counts": {k: int(v) for k, v in collector.shadow_divergence_counts.items()},
            "pair_tier_breakdown": {tier: {k: int(v) for k, v in counts.items()} for tier, counts in collector.pair_tier_breakdown.items()},
            "manifest": manifest_info,
            "settings_snapshot": dict(s.to_public_dict()),
            "experiment_overrides": _experiment_overrides(args),
            "adaptive_context": dict(adaptive_context_meta),
            "data_roots": {"feature_root": str(feature_root), "project_root": str(project_root)},
            "live_validation_status": str(validation_result.status),
            "live_validation_compared_rows": int(validation_result.compared_rows),
            "decision_history_total_rows": int(collector.total),
            "decision_history_retained_rows": int(len(collector.history.rows)),
            "decision_history_sampling": "reservoir" if collector.emit_history else "disabled",
        },
    )
    aggregate = asdict(aggregate_metrics)
    aggregate.update(aggregate.pop("metadata", {}))
    aggregate["allow_rate"] = float(collector.allowed / max(1, collector.total))
    aggregate["reject_rate"] = float((collector.total - collector.allowed) / max(1, collector.total))
    aggregate["decision_count"] = int(collector.total)
    aggregate["shadow_candidate_count"] = int(collector.shadow_candidates)
    aggregate["shadow_would_trade_count"] = int(collector.shadow_would_trade)
    aggregate["structure_rescue_count"] = int(collector.structure_rescues)
    aggregate["pnl_by_close_reason"] = pnl_by_close_reason_rows
    aggregate["pnl_by_session"] = pnl_by_session
    aggregate["pnl_by_scenario"] = pnl_by_scenario
    aggregate["pnl_by_regime"] = pnl_by_regime
    aggregate["slot_utilization_rate"] = float(slot_utilization_rate)
    aggregate["cagr_equivalent_pct"] = float(cagr_equiv_pct)
    aggregate["shared_overlay_diagnostics"] = dict(shared_overlay_diagnostics)

    recommendations = _build_recommendations(
        aggregate=aggregate,
        trades_df=trades_df,
        structure_summary=structure_summary,
        uncertainty_summary=uncertainty_summary,
        lifecycle_summary=lifecycle_summary,
        rejections_by_session=rejections_by_session,
        per_pair_records=per_pair_records,
    ) if bool(args.recommendations) else []

    trades_path = out_dir / "trades.csv"
    equity_path = out_dir / "equity_curve.csv"
    aggregate_path = out_dir / "aggregate.json"
    per_pair_path = out_dir / "per_pair.json"
    side_path = out_dir / "by_side.json"
    rejections_by_pair_path = out_dir / "rejections_by_pair.json"
    rejections_by_session_path = out_dir / "rejections_by_session.json"
    lifecycle_summary_path = out_dir / "lifecycle_summary.json"
    structure_summary_path = out_dir / "structure_timing_summary.json"
    uncertainty_summary_path = out_dir / "uncertainty_summary.json"
    environment_summary_path = out_dir / "environment_summary.json"
    playbook_summary_path = out_dir / "playbook_summary.json"
    portfolio_crowding_summary_path = out_dir / "portfolio_crowding_summary.json"
    allocator_summary_path = out_dir / "allocator_summary.json"
    sleeve_health_summary_path = out_dir / "sleeve_health_summary.json"
    replacement_summary_path = out_dir / "replacement_summary.json"
    campaign_summary_path = out_dir / "campaign_summary.json"
    campaign_state_summary_path = out_dir / "campaign_state_summary.json"
    belief_summary_path = out_dir / "belief_summary.json"
    belief_deciles_path = out_dir / "belief_deciles.json"
    belief_overlay_comparison_path = out_dir / "belief_overlay_comparison.json"
    twin_validation_path = out_dir / "twin_validation.json"
    recent_live_comparison_path = out_dir / "recent_live_comparison.json"
    improvements_path = out_dir / "improvements.md"
    decision_history_path = out_dir / DECISION_HISTORY_FILE
    allocator_decision_history_path = out_dir / ALLOCATOR_DECISION_HISTORY_FILE
    belief_decision_history_path = out_dir / "belief_decisions.csv.gz"
    hypothesis_rows_path = out_dir / "hypothesis_rows.csv.gz"
    thesis_campaigns_path = out_dir / "thesis_campaigns.csv.gz"

    trades_df.to_csv(trades_path, index=False)
    equity_df.to_csv(equity_path, index=False)
    aggregate_path.write_text(json.dumps(aggregate, indent=2, sort_keys=True), encoding="utf-8")
    per_pair_path.write_text(json.dumps(per_pair_records, indent=2), encoding="utf-8")
    side_path.write_text(json.dumps(side_breakdown_df.to_dict(orient="records"), indent=2), encoding="utf-8")
    rejections_by_pair_path.write_text(json.dumps(rejections_by_pair, indent=2, sort_keys=True), encoding="utf-8")
    rejections_by_session_path.write_text(json.dumps(rejections_by_session, indent=2, sort_keys=True), encoding="utf-8")
    lifecycle_summary_path.write_text(json.dumps(lifecycle_summary, indent=2, sort_keys=True), encoding="utf-8")
    structure_summary_path.write_text(json.dumps(structure_summary, indent=2, sort_keys=True), encoding="utf-8")
    uncertainty_summary_path.write_text(json.dumps(uncertainty_summary, indent=2, sort_keys=True), encoding="utf-8")
    environment_summary_path.write_text(json.dumps(environment_summary, indent=2, sort_keys=True), encoding="utf-8")
    playbook_summary_path.write_text(json.dumps(playbook_summary, indent=2, sort_keys=True), encoding="utf-8")
    portfolio_crowding_summary_path.write_text(json.dumps(portfolio_crowding_summary, indent=2, sort_keys=True), encoding="utf-8")
    allocator_summary_path.write_text(json.dumps(allocator_summary, indent=2, sort_keys=True), encoding="utf-8")
    sleeve_health_summary_path.write_text(json.dumps(sleeve_health_summary, indent=2, sort_keys=True), encoding="utf-8")
    replacement_summary_path.write_text(json.dumps(replacement_summary, indent=2, sort_keys=True), encoding="utf-8")
    campaign_summary_path.write_text(json.dumps(campaign_summary, indent=2, sort_keys=True), encoding="utf-8")
    campaign_state_summary_path.write_text(json.dumps(campaign_state_summary, indent=2, sort_keys=True), encoding="utf-8")
    belief_summary_path.write_text(json.dumps(belief_summary, indent=2, sort_keys=True), encoding="utf-8")
    belief_deciles_path.write_text(json.dumps(belief_deciles, indent=2, sort_keys=True), encoding="utf-8")
    belief_overlay_comparison_path.write_text(json.dumps({}, indent=2, sort_keys=True), encoding="utf-8")
    twin_validation_path.write_text(json.dumps(asdict(validation_result), indent=2, sort_keys=True), encoding="utf-8")
    recent_live_comparison_payload = dict(recent_live_comparison)
    recent_live_comparison_payload["live_fetch"] = {k: v for k, v in live_fetch.items() if k != "items"}
    recent_live_comparison_payload["live_meta"] = dict(live_meta)
    recent_live_comparison_path.write_text(json.dumps(recent_live_comparison_payload, indent=2, sort_keys=True), encoding="utf-8")
    improvements_path.write_text(_recommendations_markdown(recommendations), encoding="utf-8")

    if bool(collector.emit_history):
        with gzip.open(decision_history_path, "wt", encoding="utf-8", newline="") as fh:
            if history_rows:
                writer = csv.DictWriter(fh, fieldnames=_csv_fieldnames(history_rows))
                writer.writeheader()
                writer.writerows(history_rows)
            else:
                fh.write("")
        allocator_rows = [row for row in history_rows if str(row.get("allocator_rank") or "") not in {"", "0"} or bool(row.get("allocator_selected", False))]
        with gzip.open(allocator_decision_history_path, "wt", encoding="utf-8", newline="") as fh:
            if allocator_rows:
                writer = csv.DictWriter(fh, fieldnames=_csv_fieldnames(allocator_rows))
                writer.writeheader()
                writer.writerows(allocator_rows)
            else:
                fh.write("")
        belief_decision_rows = [
            row
            for row in history_rows
            if str(row.get("belief_source_mode") or "") not in {"", "disabled", "artifact_missing"}
        ]
        with gzip.open(belief_decision_history_path, "wt", encoding="utf-8", newline="") as fh:
            if belief_decision_rows:
                writer = csv.DictWriter(fh, fieldnames=_csv_fieldnames(belief_decision_rows))
                writer.writeheader()
                writer.writerows(belief_decision_rows)
            else:
                fh.write("")
        with gzip.open(hypothesis_rows_path, "wt", encoding="utf-8", newline="") as fh:
            if belief_hypothesis_rows:
                writer = csv.DictWriter(fh, fieldnames=_csv_fieldnames(belief_hypothesis_rows))
                writer.writeheader()
                writer.writerows(belief_hypothesis_rows)
            else:
                fh.write("")
        with gzip.open(thesis_campaigns_path, "wt", encoding="utf-8", newline="") as fh:
            if campaign_events:
                writer = csv.DictWriter(fh, fieldnames=_csv_fieldnames(campaign_events))
                writer.writeheader()
                writer.writerows(campaign_events)
            else:
                fh.write("")
    else:
        with gzip.open(decision_history_path, "wt", encoding="utf-8", newline="") as fh:
            fh.write("")
        with gzip.open(allocator_decision_history_path, "wt", encoding="utf-8", newline="") as fh:
            fh.write("")
        with gzip.open(belief_decision_history_path, "wt", encoding="utf-8", newline="") as fh:
            fh.write("")
        with gzip.open(hypothesis_rows_path, "wt", encoding="utf-8", newline="") as fh:
            fh.write("")
        with gzip.open(thesis_campaigns_path, "wt", encoding="utf-8", newline="") as fh:
            fh.write("")

    return {
        "aggregate": aggregate,
        "aggregate_path": aggregate_path,
        "trades_path": trades_path,
        "equity_path": equity_path,
        "per_pair_path": per_pair_path,
        "side_path": side_path,
        "rejections_by_pair_path": rejections_by_pair_path,
        "rejections_by_session_path": rejections_by_session_path,
        "lifecycle_summary_path": lifecycle_summary_path,
        "structure_summary_path": structure_summary_path,
        "uncertainty_summary_path": uncertainty_summary_path,
        "environment_summary_path": environment_summary_path,
        "playbook_summary_path": playbook_summary_path,
        "portfolio_crowding_summary_path": portfolio_crowding_summary_path,
        "allocator_summary_path": allocator_summary_path,
        "sleeve_health_summary_path": sleeve_health_summary_path,
        "replacement_summary_path": replacement_summary_path,
        "campaign_summary_path": campaign_summary_path,
        "campaign_state_summary_path": campaign_state_summary_path,
        "belief_summary_path": belief_summary_path,
        "belief_deciles_path": belief_deciles_path,
        "belief_overlay_comparison_path": belief_overlay_comparison_path,
        "belief_decision_history_path": belief_decision_history_path,
        "hypothesis_rows_path": hypothesis_rows_path,
        "thesis_campaigns_path": thesis_campaigns_path,
        "twin_validation_path": twin_validation_path,
        "recent_live_comparison_path": recent_live_comparison_path,
        "improvements_path": improvements_path,
        "decision_history_path": decision_history_path if bool(collector.emit_history) else None,
        "allocator_decision_history_path": allocator_decision_history_path,
        "per_pair_records": per_pair_records,
        "rejections_by_session": rejections_by_session,
        "rejections_by_pair": rejections_by_pair,
        "lifecycle_summary": lifecycle_summary,
        "environment_summary": environment_summary,
        "playbook_summary": playbook_summary,
        "portfolio_crowding_summary": portfolio_crowding_summary,
        "allocator_summary": allocator_summary,
        "sleeve_health_summary": sleeve_health_summary,
        "replacement_summary": replacement_summary,
        "belief_summary": belief_summary,
        "shared_overlay_diagnostics": shared_overlay_diagnostics,
        "belief_deciles": belief_deciles,
        "hypothesis_row_count": int(len(belief_hypothesis_rows)),
        "validation_result": asdict(validation_result),
        "recent_live_comparison_payload": recent_live_comparison_payload,
        "entry_cumulative_by_ts": dict(entry_cumulative_by_ts),
    }


# AGENT FLOW: `run_twin` wraps strict/adaptive orchestration; adaptive mode can spawn a same-window strict baseline for comparison and guardrails.
def run_twin(args: argparse.Namespace) -> dict[str, Any]:
    exec_mode = str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE)
    if exec_mode != ADAPTIVE_EXEC_MODE or not bool(getattr(args, "adaptive_compare_baseline", True)):
        return _run_twin_once(args)

    adaptive_out_dir = Path(str(args.out_dir))
    baseline_out_dir = adaptive_out_dir / "_baseline_strict"
    baseline_args = _clone_args(
        args,
        exec_mode=STRICT_EXEC_MODE,
        belief_overlay=False,
        out_dir=str(baseline_out_dir),
        adaptive_compare_baseline=False,
        validate_live_overlap=bool(getattr(args, "validate_live_overlap", True)),
    )
    baseline_result = _run_twin_once(baseline_args)

    overlay_enabled = bool(getattr(args, "belief_overlay", True))
    adaptive_overlay_baseline_dir = adaptive_out_dir / "_adaptive_no_belief_overlay"
    adaptive_overlay_baseline_args = _clone_args(
        args,
        exec_mode=ADAPTIVE_EXEC_MODE,
        belief_overlay=False,
        adaptive_compare_baseline=False,
        validate_live_overlap=False,
        out_dir=str(adaptive_overlay_baseline_dir),
    )
    adaptive_overlay_baseline_result = _run_twin_once(adaptive_overlay_baseline_args, baseline_result=baseline_result)

    adaptive_args = _clone_args(
        args,
        exec_mode=ADAPTIVE_EXEC_MODE,
        belief_overlay=overlay_enabled,
        adaptive_compare_baseline=False,
        validate_live_overlap=False,
        out_dir=str(adaptive_out_dir),
    )
    adaptive_result = _run_twin_once(adaptive_args, baseline_result=baseline_result)

    comparison_payload = _adaptive_baseline_comparison_payload(adaptive_result=adaptive_result, baseline_result=baseline_result)
    guardrails_payload = _adaptive_guardrails_payload(args=args, adaptive_result=adaptive_result, baseline_result=baseline_result)
    comparison_path = Path(str(args.out_dir)) / "adaptive_baseline_comparison.json"
    guardrails_path = Path(str(args.out_dir)) / "adaptive_aggressiveness_guardrails.json"
    belief_overlay_comparison_path = Path(str(args.out_dir)) / "belief_overlay_comparison.json"
    comparison_path.write_text(json.dumps(comparison_payload, indent=2, sort_keys=True), encoding="utf-8")
    guardrails_path.write_text(json.dumps(guardrails_payload, indent=2, sort_keys=True), encoding="utf-8")
    overlay_baseline_agg = dict(adaptive_overlay_baseline_result["aggregate"])
    overlay_agg = dict(adaptive_result["aggregate"])
    overlay_belief = dict(adaptive_result.get("belief_summary") or {})
    overlay_baseline_belief = dict(adaptive_overlay_baseline_result.get("belief_summary") or {})
    belief_overlay_comparison = {
        "overlay_enabled": bool(overlay_enabled),
        "baseline_overlay_enabled": False,
        "baseline_out_dir": str(adaptive_overlay_baseline_dir),
        "overlay_out_dir": str(adaptive_out_dir),
        "net_pnl_usd_delta": float(_safe_float(overlay_agg.get("net_pnl_usd", 0.0), 0.0) - _safe_float(overlay_baseline_agg.get("net_pnl_usd", 0.0), 0.0)),
        "profit_factor_delta": float(_safe_float(overlay_agg.get("profit_factor", 0.0), 0.0) - _safe_float(overlay_baseline_agg.get("profit_factor", 0.0), 0.0)),
        "max_drawdown_pct_delta": float(_safe_float(overlay_agg.get("max_drawdown_pct", 0.0), 0.0) - _safe_float(overlay_baseline_agg.get("max_drawdown_pct", 0.0), 0.0)),
        "expectancy_per_trade_usd_delta": float(_safe_float(overlay_agg.get("expectancy_per_trade_usd", 0.0), 0.0) - _safe_float(overlay_baseline_agg.get("expectancy_per_trade_usd", 0.0), 0.0)),
        "entries_delta": int(overlay_agg.get("entries", 0)) - int(overlay_baseline_agg.get("entries", 0)),
        "slot_utilization_rate_delta": float(_safe_float(overlay_agg.get("slot_utilization_rate", 0.0), 0.0) - _safe_float(overlay_baseline_agg.get("slot_utilization_rate", 0.0), 0.0)),
        "avg_open_positions_delta": float(_safe_float(overlay_agg.get("avg_open_positions", 0.0), 0.0) - _safe_float(overlay_baseline_agg.get("avg_open_positions", 0.0), 0.0)),
        "belief_gap_expectancy_delta": float(_safe_float(overlay_belief.get("top_belief_gap_quintile_expectancy_usd", 0.0), 0.0) - _safe_float(overlay_baseline_belief.get("top_belief_gap_quintile_expectancy_usd", 0.0), 0.0)),
        "belief_hit_rate_delta": float(_safe_float(overlay_belief.get("belief_primary_side_hit_rate", 0.0), 0.0) - _safe_float(overlay_baseline_belief.get("belief_primary_side_hit_rate", 0.0), 0.0)),
        "belief_vs_proxy_hit_rate_delta": float(_safe_float(overlay_belief.get("belief_vs_proxy_hit_rate_delta", 0.0), 0.0) - _safe_float(overlay_baseline_belief.get("belief_vs_proxy_hit_rate_delta", 0.0), 0.0)),
        "top_ev_prob_expectancy_delta": float(_safe_float(overlay_belief.get("top_ev_prob_quintile_expectancy_usd", 0.0), 0.0) - _safe_float(overlay_baseline_belief.get("top_ev_prob_quintile_expectancy_usd", 0.0), 0.0)),
        "bottom_ev_prob_expectancy_delta": float(_safe_float(overlay_belief.get("bottom_ev_prob_quintile_expectancy_usd", 0.0), 0.0) - _safe_float(overlay_baseline_belief.get("bottom_ev_prob_quintile_expectancy_usd", 0.0), 0.0)),
        "top_fail_fast_expectancy_delta": float(_safe_float(overlay_belief.get("top_fail_fast_quintile_expectancy_usd", 0.0), 0.0) - _safe_float(overlay_baseline_belief.get("top_fail_fast_quintile_expectancy_usd", 0.0), 0.0)),
        "bottom_fail_fast_expectancy_delta": float(_safe_float(overlay_belief.get("bottom_fail_fast_quintile_expectancy_usd", 0.0), 0.0) - _safe_float(overlay_baseline_belief.get("bottom_fail_fast_quintile_expectancy_usd", 0.0), 0.0)),
        "overlay_adjustment": dict(overlay_belief.get("overlay_adjustment") or {}),
    }
    belief_overlay_comparison_path.write_text(json.dumps(belief_overlay_comparison, indent=2, sort_keys=True), encoding="utf-8")

    twin_validation_path = Path(str(args.out_dir)) / "twin_validation.json"
    recent_live_comparison_path = Path(str(args.out_dir)) / "recent_live_comparison.json"
    twin_validation_path.write_text(json.dumps(baseline_result["validation_result"], indent=2, sort_keys=True), encoding="utf-8")
    recent_live_comparison_path.write_text(json.dumps(baseline_result["recent_live_comparison_payload"], indent=2, sort_keys=True), encoding="utf-8")

    adaptive_result["twin_validation_path"] = twin_validation_path
    adaptive_result["recent_live_comparison_path"] = recent_live_comparison_path
    adaptive_result["adaptive_baseline_comparison_path"] = comparison_path
    adaptive_result["adaptive_aggressiveness_guardrails_path"] = guardrails_path
    adaptive_result["belief_overlay_comparison_path"] = belief_overlay_comparison_path
    adaptive_result["baseline_result"] = baseline_result
    adaptive_result["adaptive_overlay_baseline_result"] = adaptive_overlay_baseline_result
    adaptive_result["adaptive_baseline_comparison"] = comparison_payload
    adaptive_result["adaptive_aggressiveness_guardrails"] = guardrails_payload
    adaptive_result["belief_overlay_comparison"] = belief_overlay_comparison
    adaptive_result["aggregate"]["baseline_compare"] = {
        "baseline_out_dir": str(baseline_out_dir),
        "guardrails_passed": bool(guardrails_payload.get("guardrails_passed", False)),
    }
    adaptive_result["aggregate"]["live_validation_status"] = str(baseline_result["aggregate"].get("live_validation_status", "disabled"))
    adaptive_result["aggregate"]["live_validation_compared_rows"] = int(baseline_result["aggregate"].get("live_validation_compared_rows", 0))
    return adaptive_result


def build_parser() -> argparse.ArgumentParser:
    s = get_settings()
    default_out = Path(s.project_root) / "artifacts" / "reports" / "backtests" / f"digital_twin_{pd.Timestamp.utcnow().strftime('%Y%m%d_%H%M%S')}"
    parser = argparse.ArgumentParser(description="Run an FXStack digital twin backtest from the active manifest.")
    parser.add_argument("--pairs", default=",".join(s.pairs))
    parser.add_argument("--feature-root", default=str(Path(s.project_root) / "data" / "features"))
    parser.add_argument("--start-equity", type=float, default=10000.0)
    parser.add_argument("--slippage-bps", type=float, default=0.25)
    parser.add_argument(
        "--fill-delay-bars",
        type=int,
        default=int(os.environ.get("FXSTACK_TWIN_FILL_DELAY_BARS", "1") or "1"),
        help="Bars between a closed-bar decision and its executable fill; must be >= 1.",
    )
    parser.add_argument("--start-ts", default="2024-01-14")
    parser.add_argument("--end-ts", default="2026-03-25")
    parser.add_argument("--exec-mode", choices=[STRICT_EXEC_MODE, ADAPTIVE_EXEC_MODE], default=STRICT_EXEC_MODE)
    parser.add_argument("--lifecycle-cache-pairs", type=int, default=6)
    parser.add_argument("--out-dir", default=str(default_out))
    parser.add_argument("--validate-live-overlap", dest="validate_live_overlap", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--validation-limit", type=int, default=500)
    parser.add_argument("--emit-decision-history", dest="emit_decision_history", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-decision-history-rows", type=int, default=500000)
    parser.add_argument("--recommendations", dest="recommendations", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--adaptive-compare-baseline", dest="adaptive_compare_baseline", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--adaptive-playbooks", default="trend_pullback,range_mean_reversion,breakout_expansion,failed_breakout_reversal")
    parser.add_argument("--adaptive-entry-ratio-floor", type=float, default=0.90)
    parser.add_argument("--adaptive-entry-ratio-cap", type=float, default=1.35)
    parser.add_argument("--adaptive-slot-util-floor", type=float, default=0.90)
    parser.add_argument("--adaptive-slot-util-cap", type=float, default=1.20)
    parser.add_argument("--adaptive-aggressive-fallback-margin", type=float, default=0.08)
    parser.add_argument("--adaptive-use-risk-multipliers", dest="adaptive_use_risk_multipliers", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--belief-overlay", dest="belief_overlay", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--bridge-url", default=str(s.mt4_bridge_url))
    parser.add_argument("--live-api-key", default=str(s.bridge_api_key))
    parser.add_argument("--shadow-tier1-structure-rescue-margin", type=float, default=None)
    parser.add_argument("--shadow-pair-aware-spread-caps", dest="shadow_pair_aware_spread_caps", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--shadow-spread-cap-quantile", type=float, default=0.75)
    parser.add_argument("--shadow-spread-cap-multiplier", type=float, default=1.25)
    parser.add_argument("--shadow-spread-cap-max-bps", type=float, default=5.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_twin(args)
    print(json.dumps(result["aggregate"], indent=2, sort_keys=True))
    print(f"aggregate_json={result['aggregate_path']}")
    print(f"trades_csv={result['trades_path']}")
    print(f"equity_curve_csv={result['equity_path']}")
    print(f"per_pair_json={result['per_pair_path']}")
    print(f"by_side_json={result['side_path']}")
    print(f"twin_validation_json={result['twin_validation_path']}")
    print(f"recent_live_comparison_json={result['recent_live_comparison_path']}")
    print(f"improvements_md={result['improvements_path']}")
    if result.get("adaptive_baseline_comparison_path"):
        print(f"adaptive_baseline_comparison_json={result['adaptive_baseline_comparison_path']}")
    if result.get("adaptive_aggressiveness_guardrails_path"):
        print(f"adaptive_aggressiveness_guardrails_json={result['adaptive_aggressiveness_guardrails_path']}")
    if result.get("belief_overlay_comparison_path"):
        print(f"belief_overlay_comparison_json={result['belief_overlay_comparison_path']}")
    if result.get("decision_history_path"):
        print(f"decision_history_csv_gz={result['decision_history_path']}")
    if result.get("belief_decision_history_path"):
        print(f"belief_decisions_csv_gz={result['belief_decision_history_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
