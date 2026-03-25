from __future__ import annotations

import argparse
import copy
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.backtest.twin_types import (  # noqa: E402
    TwinAggregateMetrics,
    TwinClosedTrade,
    TwinDecisionRecord,
    TwinOpenPosition,
    TwinRecommendation,
    TwinRunConfig,
    TwinValidationResult,
)
from fxstack.backtest.adaptive_policy import (  # noqa: E402
    ADAPTIVE_EXEC_MODE,
    ENTRY_QUALITY_FLOOR,
    PLAYBOOK_BREAKOUT_EXPANSION,
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
from fxstack.live.policy import POLICY_VERSION, EDGE_FORMULA_ID  # noqa: E402
from fxstack.runtime.runner import (  # noqa: E402
    _apply_shadow_entry_ranking,
    _reversal_blocking_reasons,
    _shadow_pair_tier,
)
from fxstack.settings import get_settings  # noqa: E402


TWIN_VERSION = "fxstack_digital_twin_v1"
DECISION_HISTORY_FILE = "decision_history.csv.gz"


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


def _experiment_overrides(args: argparse.Namespace) -> dict[str, Any]:
    return {
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
        aggressive_fallback_used = bool(row.get("aggressive_fallback_used", False))
        crowd_penalty = float(_safe_float(row.get("currency_crowding_penalty"), 0.0))
        diversify_penalty = float(_safe_float(row.get("playbook_diversification_penalty"), 0.0))
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
        if allowed:
            self.by_pair[pair]["allowed"] += 1
            self.by_session[session]["allowed"] += 1
            self.by_environment[environment_state]["allowed"] += 1
            self.by_playbook[playbook]["allowed"] += 1
        reason = rejection_reason
        self.by_pair[pair]["reasons"][reason] += 1
        self.by_session[session]["reasons"][reason] += 1
        self.by_environment[environment_state]["reasons"][reason] += 1
        self.by_playbook[playbook]["reasons"][reason] += 1
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
    start_ts: pd.Timestamp | None,
    end_ts: pd.Timestamp | None,
    settings: Any,
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    df = feature_store.read_pair_timeframe(
        provider=provider,
        pair=pair,
        timeframe=intraday_timeframe,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    if df.empty:
        raise RuntimeError(f"no feature rows for {pair} {intraday_timeframe}")
    df = df.sort_values("ts").reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df[df["ts"].notna()].reset_index(drop=True)
    if start_ts is not None:
        df = df[df["ts"] >= start_ts].reset_index(drop=True)
    if end_ts is not None:
        df = df[df["ts"] <= end_ts].reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"no timestamped feature rows for {pair}")

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

    htf_components: list[np.ndarray] = []
    for key, scale in (
        ("h1_trend_slope_20", 0.0015),
        ("h4_trend_slope_20", 0.0025),
        ("d_trend_slope_20", 0.0035),
        ("h1_trend_strength_20", 1.25),
        ("h4_trend_strength_20", 1.50),
        ("d_trend_strength_20", 1.75),
    ):
        if key in df.columns:
            htf_components.append(_directional_component_score_array(_series_or_default(df, key, 0.0).to_numpy(dtype=float), side_sign=side_sign, scale=scale))
    if not htf_components:
        htf_components = [
            _directional_component_score_array(_series_or_default(df, "trend_slope_60", 0.0).to_numpy(dtype=float), side_sign=side_sign, scale=0.0020),
            _directional_component_score_array(_series_or_default(df, "trend_strength_60", 0.0).to_numpy(dtype=float), side_sign=side_sign, scale=1.50),
        ]
    htf_alignment_score = pd.Series(np.mean(np.column_stack(htf_components), axis=1), index=df.index, dtype=float)

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
        playbook_score=float(_safe_float(meta.get("playbook_score", 0.0), 0.0)),
        location_score=float(_safe_float(meta.get("location_score", 0.0), 0.0)),
        trigger_score=float(_safe_float(meta.get("trigger_score", 0.0), 0.0)),
        adaptive_entry_quality=float(_safe_float(meta.get("adaptive_entry_quality", 0.0), 0.0)),
        currency_crowding_penalty=float(_safe_float(meta.get("currency_crowding_penalty", 0.0), 0.0)),
        playbook_diversification_penalty=float(_safe_float(meta.get("playbook_diversification_penalty", 0.0), 0.0)),
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


def _run_twin_once(args: argparse.Namespace, *, baseline_result: dict[str, Any] | None = None) -> dict[str, Any]:
    s = get_settings()
    project_root = Path(s.project_root)
    feature_root = Path(str(args.feature_root or (project_root / "data" / "features")))
    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    pairs = BASE._parse_pairs(args.pairs, s.pairs)
    provider = str(s.normalized_data_provider)
    intraday_timeframe = str(s.intraday_timeframe).upper()
    start_bound = pd.to_datetime(args.start_ts, utc=True) if str(args.start_ts or "").strip() else None
    end_bound = pd.to_datetime(args.end_ts, utc=True) if str(args.end_ts or "").strip() else None

    run_config = TwinRunConfig(
        twin_version=TWIN_VERSION,
        policy_version=POLICY_VERSION,
        pairs=list(pairs),
        feature_root=str(feature_root),
        start_ts=str(args.start_ts or ""),
        end_ts=str(args.end_ts or ""),
        start_equity=float(args.start_equity),
        slippage_bps=float(args.slippage_bps),
        validate_live_overlap=bool(args.validate_live_overlap),
        validation_limit=int(args.validation_limit),
        emit_decision_history=bool(args.emit_decision_history),
        max_decision_history_rows=int(args.max_decision_history_rows),
        recommendations=bool(args.recommendations),
        exec_mode=str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE),
        adaptive_compare_baseline=bool(getattr(args, "adaptive_compare_baseline", True)),
        adaptive_playbooks=sorted(parse_enabled_playbooks(getattr(args, "adaptive_playbooks", None))),
        adaptive_entry_ratio_floor=float(getattr(args, "adaptive_entry_ratio_floor", 0.90)),
        adaptive_entry_ratio_cap=float(getattr(args, "adaptive_entry_ratio_cap", 1.35)),
        adaptive_slot_util_floor=float(getattr(args, "adaptive_slot_util_floor", 0.90)),
        adaptive_slot_util_cap=float(getattr(args, "adaptive_slot_util_cap", 1.20)),
        adaptive_aggressive_fallback_margin=float(getattr(args, "adaptive_aggressive_fallback_margin", 0.08)),
        adaptive_use_risk_multipliers=bool(getattr(args, "adaptive_use_risk_multipliers", False)),
        metadata={
            "bridge_url": str(args.bridge_url),
            "provider": provider,
            "intraday_timeframe": intraday_timeframe,
        },
    )

    feature_store = BASE.ParquetStore(feature_root)
    model_sets = BASE._load_model_sets_from_manifest(pairs=pairs, project_root=project_root)
    manifest_info = _manifest_fingerprint(s, project_root, model_sets)

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
            start_ts=start_bound,
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

    for pair in pairs:
        decision_frames[pair] = decision_frames[pair].loc[(decision_frames[pair].index >= start_ts) & (decision_frames[pair].index <= end_ts)]
        price_frames[pair] = price_frames[pair].loc[(price_frames[pair].index >= start_ts) & (price_frames[pair].index <= end_ts)]

    timeline = decision_frames[pairs[0]].index
    for pair in pairs[1:]:
        timeline = timeline.intersection(decision_frames[pair].index)
    timeline = pd.Index(timeline.sort_values())
    if len(timeline) == 0:
        raise RuntimeError("no common timestamps across selected pairs")

    adaptive_enabled = str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE) == ADAPTIVE_EXEC_MODE
    adaptive_context_meta: dict[str, Any] = {}
    if adaptive_enabled:
        for pair in pairs:
            decision_frames[pair] = decision_frames[pair].reindex(timeline).copy()
        adaptive_context_meta = attach_adaptive_context(
            decision_frames,
            pairs=list(pairs),
            settings=s,
            enabled_playbooks=parse_enabled_playbooks(getattr(args, "adaptive_playbooks", None)),
        )
    baseline_entry_cumulative_by_ts = dict((baseline_result or {}).get("entry_cumulative_by_ts") or {}) if adaptive_enabled else {}

    decision_arrays: dict[str, dict[str, np.ndarray]] = {}
    bid_arrays: dict[str, np.ndarray] = {}
    ask_arrays: dict[str, np.ndarray] = {}
    mid_arrays: dict[str, np.ndarray] = {}
    for pair in pairs:
        frame = decision_frames[pair].reindex(timeline)
        decision_arrays[pair] = {col: frame[col].to_numpy() for col in frame.columns}
        prices = price_frames[pair].reindex(timeline).ffill()
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
        timeline=timeline,
        max_pairs=max(6, int(args.lifecycle_cache_pairs)),
    )

    collector = DecisionMetricsCollector(max_history_rows=int(args.max_decision_history_rows), emit_history=bool(args.emit_decision_history))
    collector.set_validation_keys(set(live_flat.keys()))

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
            adaptive_fields = {
                "environment_state": str(signal_row["environment_state"][bar_idx]) if adaptive_enabled and "environment_state" in signal_row else "",
                "trend_persistence_score": float(signal_row["trend_persistence_score"][bar_idx]) if adaptive_enabled and "trend_persistence_score" in signal_row else 0.0,
                "compression_score": float(signal_row["compression_score"][bar_idx]) if adaptive_enabled and "compression_score" in signal_row else 0.0,
                "expansion_score": float(signal_row["expansion_score"][bar_idx]) if adaptive_enabled and "expansion_score" in signal_row else 0.0,
                "range_score": float(signal_row["range_score"][bar_idx]) if adaptive_enabled and "range_score" in signal_row else 0.0,
                "hostility_score": float(signal_row["hostility_score"][bar_idx]) if adaptive_enabled and "hostility_score" in signal_row else 0.0,
                "macro_coherence_score": float(signal_row["macro_coherence_score"][bar_idx]) if adaptive_enabled and "macro_coherence_score" in signal_row else 0.0,
                "pair_strength_score": float(signal_row["pair_strength_score"][bar_idx]) if adaptive_enabled and "pair_strength_score" in signal_row else 0.0,
                "playbook": str(signal_row["playbook"][bar_idx]) if adaptive_enabled and "playbook" in signal_row else PLAYBOOK_NO_TRADE,
                "playbook_score": float(signal_row["playbook_score"][bar_idx]) if adaptive_enabled and "playbook_score" in signal_row else 0.0,
                "location_score": float(signal_row["location_score"][bar_idx]) if adaptive_enabled and "location_score" in signal_row else 0.0,
                "trigger_score": float(signal_row["trigger_score"][bar_idx]) if adaptive_enabled and "trigger_score" in signal_row else 0.0,
                "adaptive_entry_quality": 0.0,
                "currency_crowding_penalty": 0.0,
                "playbook_diversification_penalty": 0.0,
                "aggressive_fallback_used": False,
                "adaptive_allowed": False,
                "adaptive_rejection_reason": "",
            }
            if adaptive_enabled:
                hard_reasons: list[str] = []
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
                    reentry_eval = adaptive_reentry_block(
                        pair=pair,
                        side=desired_side,
                        playbook=str(adaptive_eval.get("playbook") or adaptive_fields["playbook"]),
                        bar_idx=int(bar_idx),
                        exit_registry=recent_exit_registry,
                    )
                    if bool(reentry_eval.get("blocked")):
                        adaptive_eval["adaptive_allowed"] = False
                        adaptive_eval["adaptive_rejection_reason"] = str(reentry_eval.get("reason") or "adaptive_reentry_cooldown")
                adaptive_fields.update(adaptive_eval)
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
                severe_adaptive_exit = lifecycle_reason in {
                    "adaptive_breakout_follow_through_failed",
                    "adaptive_failed_breakout_invalidated",
                    "adaptive_reverse_ready",
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
                    "scenario_bucket": str(signal_row["scenario_bucket"][bar_idx]),
                    "regime_bucket": str(signal_row["regime_bucket"][bar_idx]),
                }
            )
            pending_actions.append(
                {
                    "pair": pair,
                    "pos_snapshot": pos_snapshot,
                    "live_pos": live_pos,
                    "ready": ready,
                    "decision_reasons": list(decision_reasons),
                    "side": side,
                    "lifecycle_action": lifecycle_action,
                    "lifecycle_reason": lifecycle_reason,
                    "close_lots": float(close_lots),
                    "exit_action_selected": exit_action_selected,
                    "reversal_failure_prob": float(reversal_failure_prob),
                    "reversal_opportunity_prob": float(reversal_opportunity_prob),
                    "trade_prob": float(signal_row["trade_prob"][bar_idx]),
                    "entry_session_bucket": str(signal_row["session_bucket"][bar_idx]),
                    "entry_scenario_bucket": str(signal_row["scenario_bucket"][bar_idx]),
                    "entry_regime_bucket": str(signal_row["regime_bucket"][bar_idx]),
                    "entry_uncertainty_score": float(signal_row["uncertainty_score"][bar_idx]),
                    "entry_structure_timing_score": float(signal_row["structure_timing_score"][bar_idx]),
                    "pair_tier": str(signal_row["pair_tier"][bar_idx]),
                    "entry_playbook": str(adaptive_fields["playbook"]),
                    "environment_state": str(adaptive_fields["environment_state"]),
                    "entry_location_score": float(adaptive_fields["location_score"]),
                    "entry_trigger_score": float(adaptive_fields["trigger_score"]),
                    "entry_macro_coherence_score": float(adaptive_fields["macro_coherence_score"]),
                    "adaptive_entry_quality": float(adaptive_fields["adaptive_entry_quality"]),
                    "playbook_score": float(adaptive_fields["playbook_score"]),
                    "location_score": float(adaptive_fields["location_score"]),
                    "trigger_score": float(adaptive_fields["trigger_score"]),
                    "calibrated_ev_bps_shadow": float(signal_row["calibrated_ev_bps_shadow"][bar_idx]),
                    "aggressive_fallback_used": bool(adaptive_fields["aggressive_fallback_used"]),
                    "replacement_keep_score": float(
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
                    ),
                }
            )

        if adaptive_enabled:
            projected_exit_indices = {
                idx_action
                for idx_action, action in enumerate(pending_actions)
                if action["pos_snapshot"] is not None and str(action.get("lifecycle_action") or "hold") == "exit"
            }
            projected_open_count = max(0, len(positions_snapshot) - len(projected_exit_indices))
            remaining_slots = max(0, int(s.max_total_positions) - projected_open_count)
            candidate_indices = [
                idx_action
                for idx_action, action in enumerate(pending_actions)
                if action["pos_snapshot"] is None and bool(action["ready"])
            ]
            if candidate_indices:
                ranked_candidate_indices = sorted(
                    candidate_indices,
                    key=lambda idx_action: (
                        float(pending_actions[idx_action].get("adaptive_entry_quality", 0.0)),
                        float(pending_actions[idx_action].get("playbook_score", 0.0)),
                        float(pending_actions[idx_action].get("location_score", 0.0)),
                        float(pending_actions[idx_action].get("trigger_score", 0.0)),
                        float(pending_actions[idx_action].get("calibrated_ev_bps_shadow", 0.0)),
                    ),
                    reverse=True,
                )
                allowed_candidate_indices = set(ranked_candidate_indices[:remaining_slots])
                overflow_candidate_indices = list(ranked_candidate_indices[remaining_slots:])
                evictable_position_indices = sorted(
                    [
                        idx_action
                        for idx_action, action in enumerate(pending_actions)
                        if (
                            action["pos_snapshot"] is not None
                            and idx_action not in projected_exit_indices
                            and (
                                str(action.get("lifecycle_reason") or "") == "adaptive_hold_baseline_floor"
                                or (
                                    tempo_gap_active
                                    and str(action.get("lifecycle_action") or "hold") == "hold"
                                    and float(action.get("replacement_keep_score", 1.0)) <= 0.48
                                )
                            )
                        )
                    ],
                    key=lambda idx_action: float(pending_actions[idx_action].get("replacement_keep_score", 1.0)),
                )
                replacement_margin = 0.00 if tempo_gap_active else 0.08
                for idx_action in overflow_candidate_indices:
                    if not evictable_position_indices:
                        break
                    weakest_idx = evictable_position_indices[0]
                    candidate_quality = float(pending_actions[idx_action].get("adaptive_entry_quality", 0.0))
                    weakest_keep_score = float(pending_actions[weakest_idx].get("replacement_keep_score", 1.0))
                    if candidate_quality < (weakest_keep_score + replacement_margin):
                        break
                    pending_actions[weakest_idx]["lifecycle_action"] = "exit"
                    pending_actions[weakest_idx]["lifecycle_reason"] = "adaptive_replacement_exit"
                    pending_actions[weakest_idx]["close_lots"] = 0.0
                    collector_rows_for_bar[weakest_idx]["lifecycle_action"] = "exit"
                    collector_rows_for_bar[weakest_idx]["lifecycle_reason"] = "adaptive_replacement_exit"
                    projected_exit_indices.add(weakest_idx)
                    evictable_position_indices.pop(0)
                    allowed_candidate_indices.add(idx_action)
                for idx_action in candidate_indices:
                    if idx_action in allowed_candidate_indices:
                        continue
                    pending_actions[idx_action]["ready"] = False
                    pending_actions[idx_action]["decision_reasons"] = ["adaptive_ranked_out"]
                    collector_rows_for_bar[idx_action]["allowed"] = False
                    collector_rows_for_bar[idx_action]["rejection_reason"] = "adaptive_ranked_out"
                    collector_rows_for_bar[idx_action]["rejection_reasons"] = ["adaptive_ranked_out"]

        shadow_diag = _apply_shadow_entry_ranking(shadow_inputs_for_bar, settings=s, open_position_count=len(positions_snapshot))
        for shadow_input, collector_row in zip(shadow_inputs_for_bar, collector_rows_for_bar, strict=False):
            shadow_meta = dict(shadow_input.get("metadata") or {})
            collector_row["portfolio_rank_shadow"] = int(_safe_int(shadow_meta.get("portfolio_rank_shadow"), 0))
            collector_row["shadow_would_trade"] = bool(shadow_meta.get("shadow_would_trade", False))
            collector_row["shadow_rejection_reason"] = str(shadow_meta.get("shadow_rejection_reason") or "")
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
                    environment_state_at_entry=str(getattr(live_pos, "environment_state_at_entry", "")),
                    environment_state_at_exit=str(action["environment_state"] if adaptive_enabled else ""),
                    lifecycle_exit_reason=str(lifecycle_reason),
                    aggressive_fallback_used=bool(getattr(live_pos, "aggressive_fallback_used", False)),
                )
                closed_trades.append(trade)
                close_reason_counts[str(lifecycle_reason)] += 1
                pnl_by_close_reason[str(lifecycle_reason)] += float(live_pos.realized_pnl_usd)
                recent_exit_registry[pair] = {
                    "bar_idx": int(bar_idx),
                    "side": str(live_pos.side),
                    "playbook": str(getattr(live_pos, "playbook", PLAYBOOK_TREND_PULLBACK)),
                    "reason": str(lifecycle_reason),
                }
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
                        environment_state_at_entry=str(action["environment_state"]),
                        entry_location_score=float(action["entry_location_score"]),
                        entry_trigger_score=float(action["entry_trigger_score"]),
                        entry_macro_coherence_score=float(action["entry_macro_coherence_score"]),
                        aggressive_fallback_used=bool(action["aggressive_fallback_used"]),
                    )
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
            environment_state_at_entry=str(getattr(pos, "environment_state_at_entry", "")),
            environment_state_at_exit="forced_final_close",
            lifecycle_exit_reason="forced_final_close",
            aggressive_fallback_used=bool(getattr(pos, "aggressive_fallback_used", False)),
        )
        closed_trades.append(trade)
        close_reason_counts["forced_final_close"] += 1
        pnl_by_close_reason["forced_final_close"] += float(pos.realized_pnl_usd)
        recent_exit_registry[pair] = {
            "bar_idx": int(bar_idx),
            "side": str(pos.side),
            "playbook": str(getattr(pos, "playbook", PLAYBOOK_TREND_PULLBACK)),
            "reason": "forced_final_close",
        }
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
        pb_trades = trades_df[trades_df["playbook"] == playbook] if not trades_df.empty and "playbook" in trades_df.columns else pd.DataFrame()
        exit_reason_mix = dict(Counter(pb_trades["close_reason"])) if not pb_trades.empty else {}
        playbook_summary[playbook] = {
            "decisions": int(row["decisions"]),
            "allow_count": int(row["allowed"]),
            "entries": int(row["allowed"]),
            "trades": int(len(pb_trades)) if not pb_trades.empty else 0,
            "net_pnl_usd": float(pb_trades["realized_pnl_usd"].sum()) if not pb_trades.empty else 0.0,
            "win_rate": float((pb_trades["realized_pnl_usd"] > 0.0).mean()) if not pb_trades.empty else 0.0,
            "avg_holding_bars": float(pb_trades["holding_bars"].mean()) if not pb_trades.empty else 0.0,
            "partial_frequency": float((pb_trades["partial_exit_events"] > 0).mean()) if not pb_trades.empty else 0.0,
            "exit_reason_mix": {k: int(v) for k, v in exit_reason_mix.items()},
            "aggressive_fallback_share": float(row["aggressive_fallbacks"] / max(1, row["allowed"])),
            "pairs": {k: int(v) for k, v in Counter(row["pairs"]).items()},
        }

    portfolio_crowding_summary = {
        "currency_crowding_penalty_sum": float(collector.crowding_penalty_sum),
        "currency_crowding_penalty_nonzero": int(collector.crowding_penalty_nonzero),
        "avg_currency_crowding_penalty": float(collector.crowding_penalty_sum / max(1, collector.total)),
        "playbook_diversification_penalty_sum": float(collector.diversification_penalty_sum),
        "playbook_diversification_penalty_nonzero": int(collector.diversification_penalty_nonzero),
        "avg_playbook_diversification_penalty": float(collector.diversification_penalty_sum / max(1, collector.total)),
        "aggressive_fallback_count": int(collector.aggressive_fallback_count),
        "playbook_mix": summarize_playbook_mix(collector.history.rows if collector.emit_history else []),
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
    twin_validation_path = out_dir / "twin_validation.json"
    recent_live_comparison_path = out_dir / "recent_live_comparison.json"
    improvements_path = out_dir / "improvements.md"
    decision_history_path = out_dir / DECISION_HISTORY_FILE

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
    twin_validation_path.write_text(json.dumps(asdict(validation_result), indent=2, sort_keys=True), encoding="utf-8")
    recent_live_comparison_payload = dict(recent_live_comparison)
    recent_live_comparison_payload["live_fetch"] = {k: v for k, v in live_fetch.items() if k != "items"}
    recent_live_comparison_payload["live_meta"] = dict(live_meta)
    recent_live_comparison_path.write_text(json.dumps(recent_live_comparison_payload, indent=2, sort_keys=True), encoding="utf-8")
    improvements_path.write_text(_recommendations_markdown(recommendations), encoding="utf-8")

    if bool(args.emit_decision_history):
        history_rows = sorted(collector.history.rows, key=lambda row: (str(row.get("ts") or ""), str(row.get("pair") or "")))
        with gzip.open(decision_history_path, "wt", encoding="utf-8", newline="") as fh:
            if history_rows:
                writer = csv.DictWriter(fh, fieldnames=list(history_rows[0].keys()))
                writer.writeheader()
                writer.writerows(history_rows)
            else:
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
        "twin_validation_path": twin_validation_path,
        "recent_live_comparison_path": recent_live_comparison_path,
        "improvements_path": improvements_path,
        "decision_history_path": decision_history_path if bool(args.emit_decision_history) else None,
        "per_pair_records": per_pair_records,
        "rejections_by_session": rejections_by_session,
        "rejections_by_pair": rejections_by_pair,
        "lifecycle_summary": lifecycle_summary,
        "environment_summary": environment_summary,
        "playbook_summary": playbook_summary,
        "portfolio_crowding_summary": portfolio_crowding_summary,
        "validation_result": asdict(validation_result),
        "recent_live_comparison_payload": recent_live_comparison_payload,
        "entry_cumulative_by_ts": dict(entry_cumulative_by_ts),
    }


def run_twin(args: argparse.Namespace) -> dict[str, Any]:
    exec_mode = str(getattr(args, "exec_mode", STRICT_EXEC_MODE) or STRICT_EXEC_MODE)
    if exec_mode != ADAPTIVE_EXEC_MODE or not bool(getattr(args, "adaptive_compare_baseline", True)):
        return _run_twin_once(args)

    adaptive_out_dir = Path(str(args.out_dir))
    baseline_out_dir = adaptive_out_dir / "_baseline_strict"
    baseline_args = _clone_args(
        args,
        exec_mode=STRICT_EXEC_MODE,
        out_dir=str(baseline_out_dir),
        adaptive_compare_baseline=False,
        validate_live_overlap=bool(getattr(args, "validate_live_overlap", True)),
    )
    baseline_result = _run_twin_once(baseline_args)

    adaptive_args = _clone_args(
        args,
        exec_mode=ADAPTIVE_EXEC_MODE,
        adaptive_compare_baseline=False,
        validate_live_overlap=False,
        out_dir=str(adaptive_out_dir),
    )
    adaptive_result = _run_twin_once(adaptive_args, baseline_result=baseline_result)

    comparison_payload = _adaptive_baseline_comparison_payload(adaptive_result=adaptive_result, baseline_result=baseline_result)
    guardrails_payload = _adaptive_guardrails_payload(args=args, adaptive_result=adaptive_result, baseline_result=baseline_result)
    comparison_path = Path(str(args.out_dir)) / "adaptive_baseline_comparison.json"
    guardrails_path = Path(str(args.out_dir)) / "adaptive_aggressiveness_guardrails.json"
    comparison_path.write_text(json.dumps(comparison_payload, indent=2, sort_keys=True), encoding="utf-8")
    guardrails_path.write_text(json.dumps(guardrails_payload, indent=2, sort_keys=True), encoding="utf-8")

    twin_validation_path = Path(str(args.out_dir)) / "twin_validation.json"
    recent_live_comparison_path = Path(str(args.out_dir)) / "recent_live_comparison.json"
    twin_validation_path.write_text(json.dumps(baseline_result["validation_result"], indent=2, sort_keys=True), encoding="utf-8")
    recent_live_comparison_path.write_text(json.dumps(baseline_result["recent_live_comparison_payload"], indent=2, sort_keys=True), encoding="utf-8")

    adaptive_result["twin_validation_path"] = twin_validation_path
    adaptive_result["recent_live_comparison_path"] = recent_live_comparison_path
    adaptive_result["adaptive_baseline_comparison_path"] = comparison_path
    adaptive_result["adaptive_aggressiveness_guardrails_path"] = guardrails_path
    adaptive_result["baseline_result"] = baseline_result
    adaptive_result["adaptive_baseline_comparison"] = comparison_payload
    adaptive_result["adaptive_aggressiveness_guardrails"] = guardrails_payload
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
    if result.get("decision_history_path"):
        print(f"decision_history_csv_gz={result['decision_history_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
