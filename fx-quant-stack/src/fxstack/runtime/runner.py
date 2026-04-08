# AGENT: ROLE: Live runtime orchestrator: startup bootstrap, feature refresh, scoring, lifecycle, adaptive parity, and final command submission.
# AGENT: ENTRYPOINT: `src.trader.cli runtime run` via `ops/windows/21_start_runtime.bat`.
# AGENT: PRIMARY INPUTS: settings, active model manifest, bridge ticks/bars, feature parquet rows, bridge state.
# AGENT: PRIMARY OUTPUTS: command submissions, runtime state patches, persisted decisions, runtime diagnostics.
# AGENT: DEPENDS ON: `fxstack/runtime/service.py`, `fxstack/live/scorer.py`, `fxstack/live/policy.py`, `fxstack/backtest/adaptive_policy.py`.
# AGENT: CALLED BY: `src/trader/cli.py`, `ops/windows/21_start_runtime.bat`.
# AGENT: STATE / SIDE EFFECTS: writes runtime state, queues broker commands, refreshes local feature tail state, tracks adaptive registries.
# AGENT: HANDSHAKES: `/v2/ready`, bridge ticks/bars fetch, command queue submit/ack, dashboard-facing state patch.
# AGENT: SEE: `docs/agents/runtime-loop.md` -> `fx-quant-stack/src/fxstack/runtime/service.py` -> `docs/agents/bridge-and-api-handshakes.md`
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pandas as pd

from fxstack.backtest.adaptive_policy import (
    PLAYBOOK_BREAKOUT_EXPANSION,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
    PLAYBOOK_NO_TRADE,
    PLAYBOOK_RANGE_MEAN_REVERSION,
    PLAYBOOK_TREND_PULLBACK,
    adaptive_lifecycle_decision,
    adaptive_reentry_block,
    adaptive_replacement_keep_score,
    adaptive_tempo_gap_active,
    attach_adaptive_context,
    evaluate_adaptive_entry,
    parse_enabled_playbooks,
)
from fxstack.belief import build_cross_pair_influence_records
from fxstack.belief.engine import (
    compute_directional_belief,
    empty_directional_belief,
    load_directional_belief_model_set,
)
from fxstack.data.live_quotes import (
    fetch_market_bars,
    fetch_market_ready,
    fetch_market_ticks,
)
from fxstack.features.fx_lifecycle import add_fx_lifecycle_features
from fxstack.features.multi_tf_contract import build_latest_multi_tf_row, build_multi_tf_rows, resample_bars
from fxstack.io.parquet_store import ParquetStore
from fxstack.live.policy import (
    EDGE_FORMULA_ID,
    infer_pip_size,
    normalize_spread_bps,
    normalize_strategy_engine_mode,
    session_bucket_from_ts,
)
from fxstack.live.scorer import LiveScorer
from fxstack.settings import get_settings
from fxstack.feast.push import build_push_payload
from fxstack.strategy.allocator import (
    allocate_candidates,
    allocator_config_from_settings,
    build_allocator_candidate,
    playbook_to_sleeve,
)
from fxstack.strategy.allocator_types import AllocatorOpenPosition
from fxstack.strategy.campaign import (
    CAMPAIGN_STATE_ABANDONED,
    CAMPAIGN_STATE_HARVEST,
    CAMPAIGN_STATE_INACTIVE,
    apply_campaign_lifecycle_overrides,
    apply_campaign_registry_snapshot,
    build_thesis_id,
    campaign_config_from_settings,
    campaign_cooldown_scale,
    campaign_state_after_close,
    campaign_transition_if_changed,
    evaluate_entry_campaign,
    evaluate_open_campaign,
    serialize_campaign_entry,
)
from fxstack.strategy.campaign_types import CampaignRegistryEntry
from fxstack.strategy.desk_overlay import build_desk_overlay
from fxstack.strategy.desk_overlay_types import DeskOverlayInputs
from fxstack.strategy.sleeve_governance import SleeveGovernanceTracker, serialize_sleeve_snapshots
from fxstack.mlops.model_uri import normalize_artifact_ref, resolve_model_artifact_path
from fxstack.mlops.registry import resolve_bundle_manifest_by_alias
from fxstack.feast.online_features import FeatureServingTelemetry, resolve_latest_feature_row
from fxstack.portfolio import build_portfolio_telemetry, evaluate_portfolio_allocation
from fxstack.providers.registry import provider_capabilities, provider_roles_from_settings
from fxstack.risk import MarketState, PolicyIntent, PortfolioState, RiskKernelConfig, evaluate_risk_decision
from fxstack.rl.proposal import build_portfolio_rl_proposal_bundle
from fxstack.runtime.governance import (
    ProviderHealthSnapshot,
    capital_band_budget_scale,
    compute_capital_governance_state,
)
from fxstack.utils.hashing import hash_mapping


@dataclass(slots=True)
class LoadedModelSet:
    pair: str
    model_set_id: str
    registry_path: str
    scorer: LiveScorer
    swing_router: "_PolicyModelRouter"
    intraday_router: "_PolicyModelRouter"
    exit_model: Any | None
    reversal_failure_model: Any | None
    reversal_opportunity_model: Any | None
    belief_model: Any | None
    exit_action_labels: dict[int, str]
    lifecycle_activation_mode: str
    has_exit_model: bool
    has_reversal_models: bool
    has_directional_belief: bool
    swing_shadow_model: Any | None = None
    intraday_shadow_model: Any | None = None
    shadow_bundle_run_id: str = ""
    shadow_component_refs: dict[str, Any] = field(default_factory=dict)
    component_feature_services: dict[str, Any] = field(default_factory=dict)
    rollout_policy: dict[str, Any] = field(default_factory=dict)
    rl_checkpoint_path: str = ""


def _resolve_path(raw: str, project_root: Path) -> Path:
    return resolve_model_artifact_path(raw, project_root=project_root)


def _resolve_optional_path(raw: str, project_root: Path) -> Path | None:
    txt = str(raw or "").strip()
    if not txt:
        return None
    variants = [txt]
    normalized = txt.replace("\\", "/")
    if normalized != txt:
        variants.append(normalized)
    for value in variants:
        p = Path(value).expanduser()
        for cand in (p, project_root / p, project_root.parent / p):
            if cand.exists():
                return cand.resolve()
    return None


def _artifact_path(raw: Any) -> str:
    ref = normalize_artifact_ref(raw)
    return str(ref.get("path") or ref.get("model_uri") or "")


def _artifact_value(artifacts: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _artifact_path(artifacts.get(key))
        if value.strip():
            return value
    return ""


def _resolve_runtime_rl_checkpoint_path(*, model_sets: dict[str, LoadedModelSet], project_root: Path) -> Path | None:
    for loaded in list(model_sets.values() or []):
        raw = str(getattr(loaded, "rl_checkpoint_path", "") or "").strip()
        if not raw:
            continue
        resolved = _resolve_optional_path(raw, project_root)
        if resolved is not None and resolved.exists():
            return resolved
    return None


def _feature_service_component_for_timeframe(
    *,
    timeframe: str,
    regime_timeframe: str,
    swing_timeframe: str,
    intraday_timeframe: str,
) -> str:
    tf = str(timeframe).upper().strip()
    if tf == str(regime_timeframe).upper().strip():
        return "regime"
    if tf == str(swing_timeframe).upper().strip():
        return "swing_xgb"
    if tf == str(intraday_timeframe).upper().strip():
        return "intraday_xgb"
    return ""


def _feature_service_component_candidates(component_key: str) -> list[str]:
    key = str(component_key).strip()
    out = [key] if key else []
    if key.endswith("_xgb"):
        out.append(key.removesuffix("_xgb"))
    if key == "intraday_xgb":
        out.extend(["meta_filter", "meta"])
    elif key == "swing_xgb":
        out.append("swing")
    elif key == "regime":
        out.append("regime_hmm")
    return [item for item in dict.fromkeys(out) if item]


def _loaded_feature_service_name(
    loaded: LoadedModelSet | None,
    *,
    pair: str,
    timeframe: str,
    regime_timeframe: str,
    swing_timeframe: str,
    intraday_timeframe: str,
) -> str:
    component_key = _feature_service_component_for_timeframe(
        timeframe=timeframe,
        regime_timeframe=regime_timeframe,
        swing_timeframe=swing_timeframe,
        intraday_timeframe=intraday_timeframe,
    )
    if not component_key:
        return ""
    component_refs = dict(getattr(loaded, "component_feature_services", {}) or {})
    for candidate_key in _feature_service_component_candidates(component_key):
        ref = dict(component_refs.get(candidate_key) or {})
        candidate = str(
            ref.get("feature_service_name")
            or ref.get("feature_service")
            or ref.get("name")
            or ""
        ).strip()
        if candidate:
            return candidate
    tf = str(timeframe).lower().strip()
    return f"fx_{str(pair).lower()}_{component_key}_{tf}"


_FEATURE_SERVING_TELEMETRY: dict[tuple[str, str], dict[str, Any]] = {}
_DEFAULT_CANARY_BUDGET_SCALE = 0.25


def _record_feature_serving_telemetry(pair: str, timeframe: str, telemetry: FeatureServingTelemetry) -> None:
    _FEATURE_SERVING_TELEMETRY[(str(pair).upper(), str(timeframe).upper())] = telemetry.to_dict()


def _feature_serving_snapshot() -> dict[str, Any]:
    values = list(_FEATURE_SERVING_TELEMETRY.values())
    if not values:
        return {
            "source": "",
            "source_chain": ["feast_online", "parquet_fallback", "raw_contract_fallback"],
            "feature_service": "",
            "cache_hit": False,
            "freshness_secs": None,
            "stale": False,
            "reason": "",
            "details": {},
        }
    latest = values[-1]
    return {
        "source": str(latest.get("source") or ""),
        "source_chain": list(latest.get("source_chain") or ["feast_online", "parquet_fallback", "raw_contract_fallback"]),
        "feature_service": str(latest.get("feature_service") or ""),
        "cache_hit": bool(latest.get("cache_hit", False)),
        "freshness_secs": latest.get("freshness_secs"),
        "stale": bool(latest.get("stale", False)),
        "reason": str(latest.get("reason") or ""),
        "details": dict(latest.get("details") or {}),
    }


def _pair_feature_serving_snapshot(
    *,
    pair: str,
    feature_serving_by_pair: dict[str, Any],
) -> dict[str, Any]:
    pair_key = str(pair).upper().strip()
    if not pair_key:
        return {}
    for timeframe in ("M5", "D", "H4"):
        entry = dict(feature_serving_by_pair.get(f"{pair_key}:{timeframe}") or {})
        if entry:
            entry.setdefault("timeframe", timeframe)
            return entry
    return {}


def _pair_readiness_summary(
    *,
    pairs: list[str],
    startup_inference: dict[str, dict[str, Any]],
    feature_serving_by_pair: dict[str, Any],
    symbol_readiness: dict[str, dict[str, Any]] | None = None,
    model_load_diag: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    load_diag = dict(model_load_diag or {})
    load_pairs = dict(load_diag.get("pairs") or {})
    for raw_pair in list(pairs or []):
        pair = str(raw_pair).upper().strip()
        if not pair:
            continue
        startup = dict(startup_inference.get(pair) or {})
        feature_serving = _pair_feature_serving_snapshot(pair=pair, feature_serving_by_pair=feature_serving_by_pair)
        symbol = dict((symbol_readiness or {}).get(pair) or {})
        pair_load = dict(load_pairs.get(pair) or {})
        blockers: list[str] = []
        if not startup:
            blockers.append("startup_inference:missing")
        elif not bool(startup.get("ok", False)):
            blockers.append(f"startup_inference:{str(startup.get('reason') or 'blocked')}")
        if feature_serving:
            if not str(feature_serving.get("source") or "").strip():
                blockers.append("feature_serving:missing_source")
            if bool(feature_serving.get("stale", False)):
                blockers.append("feature_serving:stale")
        elif pair in (feature_serving_by_pair or {}):
            blockers.append("feature_serving:missing")
        if symbol and not bool(symbol.get("supported", True)):
            blockers.append(f"symbol_readiness:{str(symbol.get('broker_symbol') or 'unsupported')}")
        if not symbol and pair in (symbol_readiness or {}):
            blockers.append("symbol_readiness:missing")
        if str(pair_load.get("failure_reason") or "").strip():
            blockers.append(f"model_load:{str(pair_load.get('failure_reason') or 'error')}")
        out[pair] = {
            "pair": pair,
            "startup_inference": startup,
            "feature_serving": feature_serving,
            "symbol_readiness": symbol,
            "model_load": pair_load,
            "ready": bool(not blockers),
            "status": "ready" if not blockers else "blocked",
            "blockers": blockers,
            "reason": "ok" if not blockers else blockers[0],
            "startup_inference_ok": bool(startup.get("ok", False)),
            "feature_serving_source": str(feature_serving.get("source") or ""),
            "feature_serving_stale": bool(feature_serving.get("stale", False)),
            "symbol_supported": bool(symbol.get("supported", True)) if symbol else True,
        }
    return out


def _pair_realized_returns_by_symbol(
    *,
    store: ParquetStore,
    provider: str,
    symbols: list[str],
    timeframe: str,
    max_rows: int,
) -> dict[str, pd.Series]:
    returns_by_pair: dict[str, pd.Series] = {}
    tail_rows = max(32, int(max_rows or 0))
    for raw_symbol in list(symbols or []):
        symbol = str(raw_symbol).upper().strip()
        if not symbol:
            continue
        frame = store.read_recent_rows(
            provider=str(provider),
            pair=symbol,
            timeframe=str(timeframe).upper(),
            max_rows=tail_rows,
        )
        if frame.empty:
            continue
        series = pd.Series(dtype=float)
        if "ret_1" in frame.columns:
            series = pd.to_numeric(frame["ret_1"], errors="coerce")
        elif "log_ret_1" in frame.columns:
            series = pd.to_numeric(frame["log_ret_1"], errors="coerce")
        elif "close" in frame.columns:
            close = pd.to_numeric(frame["close"], errors="coerce")
            series = close.pct_change()
        elif "mid" in frame.columns:
            mid = pd.to_numeric(frame["mid"], errors="coerce")
            series = mid.pct_change()
        series = pd.Series(series).dropna().tail(tail_rows).reset_index(drop=True)
        if series.empty:
            continue
        returns_by_pair[symbol] = series.astype(float)
    return returns_by_pair


def _challenger_conflict_payload(
    *,
    disagreement: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    normalized_mode = _normalize_challenger_conflict_mode(mode)
    disagreements = {
        str(key): float(_safe_float(value, 0.0))
        for key, value in dict(disagreement or {}).items()
        if str(key).strip()
    }
    max_gap = max(disagreements.values(), default=0.0)
    sign_flip = bool(max_gap >= float(_CHALLENGER_CONFLICT_HARD_GAP))
    verdict = "clear"
    gate_level = "none"
    if normalized_mode == "telemetry" and disagreements:
        verdict = "telemetry"
        gate_level = "telemetry"
    elif normalized_mode == "soft_gate" and max_gap >= float(_CHALLENGER_CONFLICT_SOFT_GAP):
        verdict = "soft_conflict"
        gate_level = "soft"
    elif normalized_mode == "hard_gate":
        if max_gap >= float(_CHALLENGER_CONFLICT_SOFT_GAP):
            verdict = "hard_conflict"
            gate_level = "hard"
        elif sign_flip:
            verdict = "hard_conflict"
            gate_level = "hard"
    return {
        "mode": normalized_mode,
        "active": bool(normalized_mode != "off" and disagreements),
        "max_gap": float(max_gap),
        "sign_flip": bool(sign_flip),
        "gate_level": str(gate_level),
        "verdict": str(verdict),
        "disagreement": disagreements,
    }


def _strategy_fallback_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    fallback_reasons: list[str] = []
    fallback_count = 0
    for decision in list(decisions or []):
        meta = dict(decision.get("metadata") or {})
        supervised_fallback = bool(meta.get("fallback_used", False))
        rl_fallback = bool(meta.get("rl_supervised_fallback_used", False))
        if not supervised_fallback and not rl_fallback:
            continue
        fallback_count += 1
        reason = str(meta.get("rl_fallback_reason") or meta.get("fallback_reason") or "").strip()
        if reason and reason not in fallback_reasons:
            fallback_reasons.append(reason)
    return {
        "enabled": bool(fallback_count > 0),
        "fallback_count": int(fallback_count),
        "fallback_reasons": list(fallback_reasons),
        "primary_reason": fallback_reasons[0] if fallback_reasons else "",
    }


def _challenger_conflict_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    active_pairs: list[str] = []
    max_gap = 0.0
    mode = "off"
    for decision in list(decisions or []):
        meta = dict(decision.get("metadata") or {})
        conflict = dict(meta.get("challenger_conflict") or {})
        current_mode = str(conflict.get("mode") or "").strip()
        if current_mode:
            mode = current_mode
        verdict = str(conflict.get("verdict") or "").strip()
        if verdict:
            counts[verdict] += 1
        gap = float(_safe_float(conflict.get("max_gap"), 0.0))
        max_gap = max(max_gap, gap)
        if bool(conflict.get("active", False)) and verdict:
            pair = str(meta.get("pair") or decision.get("pair") or "").upper().strip()
            if pair and pair not in active_pairs:
                active_pairs.append(pair)
    return {
        "mode": mode or "off",
        "active": bool(active_pairs),
        "max_gap": float(max_gap),
        "active_pairs": active_pairs,
        "verdict_counts": dict(counts),
        "dominant_verdict": counts.most_common(1)[0][0] if counts else "clear",
    }


def _symbol_list(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = []
    for raw in raw_items:
        item = str(raw or "").strip().upper()
        if item and item not in out:
            out.append(item)
    return out


def _min_positive_float(*values: Any) -> float:
    positives: list[float] = []
    for value in values:
        num = float(_safe_float(value, 0.0))
        if num > 0.0:
            positives.append(num)
    if not positives:
        return 0.0
    return float(min(positives))


def _min_positive_int(*values: Any) -> int:
    positives: list[int] = []
    for value in values:
        num = int(value or 0)
        if num > 0:
            positives.append(num)
    if not positives:
        return 0
    return int(min(positives))


def _phase5_gate_rollout_source(metadata: dict[str, Any]) -> tuple[dict[str, Any], str]:
    phase5_gate_bundle = dict(metadata.get("phase5_gate_bundle") or {})
    if phase5_gate_bundle:
        return phase5_gate_bundle, "phase5_gate_bundle"
    phase5_meta = dict(metadata.get("phase5") or {})
    if phase5_meta:
        return phase5_meta, "phase5"
    return {}, ""


def _resolve_main_runtime_rollout_policy(*, pair: str, metadata: dict[str, Any]) -> dict[str, Any]:
    pair_key = str(pair).upper().strip()
    sections: list[tuple[str, dict[str, Any]]] = []
    for key in [
        "main_runtime_rollout",
        "phase5_runtime_rollout",
        "runtime_rollout",
        "phase5_rollout",
        "rollout",
        "canary",
    ]:
        section = metadata.get(key)
        if isinstance(section, dict) and section:
            sections.append((key, dict(section)))

    phase5_gate_bundle, phase5_source = _phase5_gate_rollout_source(metadata)
    canary_gate = dict(phase5_gate_bundle.get("canary_gate") or {})
    if bool(canary_gate.get("passed", False)) and not sections:
        sections.append(
            (
                phase5_source or "phase5_gate_bundle",
                {
                    "mode": "canary",
                    "enabled": True,
                    "allowlisted_pairs": [pair_key],
                    "budget_scale": float(_DEFAULT_CANARY_BUDGET_SCALE),
                    "budget_reason": "phase5_gate_default",
                },
            )
        )

    resolved: dict[str, Any] = {
        "configured": False,
        "active": False,
        "mode": "",
        "enabled": False,
        "pair_allowlisted": False,
        "allowlisted_pairs": [],
        "budget_scale": 1.0,
        "budget_reason": "",
        "max_pair_positions": 0,
        "max_total_positions": 0,
        "max_gross_exposure": 0.0,
        "max_net_exposure": 0.0,
        "source": "",
    }
    for source, section in sections:
        if not resolved["source"]:
            resolved["source"] = str(source)
        if str(resolved.get("mode") or "").strip() == "":
            default_mode = "canary" if str(source).strip().lower() == "canary" else ""
            resolved["mode"] = str(section.get("mode") or section.get("rollout_mode") or default_mode).strip().lower()
        if "enabled" not in resolved or not bool(resolved.get("enabled")):
            resolved["enabled"] = bool(section.get("enabled", section.get("active", True)))
        allowlisted = _symbol_list(
            section.get("allowlisted_pairs")
            or section.get("pair_allowlist")
            or section.get("pairs")
            or section.get("pair_allowlisted")
        )
        if allowlisted:
            resolved["allowlisted_pairs"] = sorted(set(list(resolved.get("allowlisted_pairs") or []) + allowlisted))
        budget_scale = section.get("budget_scale", section.get("reduced_budget_scale"))
        if budget_scale is None:
            budget_scale = section.get("risk_budget_scale", section.get("entry_lot_scale"))
        if budget_scale is not None:
            resolved["budget_scale"] = _clip01(float(_safe_float(budget_scale, resolved.get("budget_scale", 1.0))))
        if not str(resolved.get("budget_reason") or "").strip():
            resolved["budget_reason"] = str(section.get("budget_reason") or "")
        resolved["max_pair_positions"] = _min_positive_int(
            resolved.get("max_pair_positions", 0),
            section.get("max_pair_positions"),
            section.get("pair_position_cap"),
        )
        resolved["max_total_positions"] = _min_positive_int(
            resolved.get("max_total_positions", 0),
            section.get("max_total_positions"),
            section.get("total_position_cap"),
        )
        resolved["max_gross_exposure"] = _min_positive_float(
            resolved.get("max_gross_exposure", 0.0),
            section.get("max_gross_exposure"),
            section.get("gross_exposure_cap"),
        )
        resolved["max_net_exposure"] = _min_positive_float(
            resolved.get("max_net_exposure", 0.0),
            section.get("max_net_exposure"),
            section.get("net_exposure_cap"),
        )

    allowlisted_pairs = _symbol_list(resolved.get("allowlisted_pairs"))
    pair_allowlisted = bool(pair_key in allowlisted_pairs) if allowlisted_pairs else bool(resolved.get("enabled", False))
    mode = str(resolved.get("mode") or "").strip().lower()
    configured = bool(mode == "canary" and sections)
    active = bool(configured and resolved.get("enabled") and pair_allowlisted)
    return {
        **resolved,
        "configured": bool(configured),
        "active": bool(active),
        "mode": mode,
        "pair_allowlisted": bool(pair_allowlisted),
        "allowlisted_pairs": allowlisted_pairs,
        "budget_scale": float(_clip01(resolved.get("budget_scale", 1.0))) if configured else 1.0,
        "source": str(resolved.get("source") or ""),
    }


def _load_artifact_meta(raw_path: str, project_root: Path) -> dict[str, Any]:
    try:
        path = resolve_model_artifact_path(str(raw_path or ""), project_root=project_root)
    except Exception:
        return {}
    meta_path = path / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return dict(json.loads(meta_path.read_text(encoding="utf-8")) or {})
    except Exception:
        return {}


def _required_model_feature_columns(*models: Any) -> list[str]:
    cols: list[str] = []
    for model in models:
        for col in list(getattr(model, "feature_columns", []) or []):
            txt = str(col or "").strip()
            if txt and txt not in cols:
                cols.append(txt)
    return cols


def _exit_action_labels(exit_meta: dict[str, Any], classes: list[int] | None) -> dict[int, str]:
    ordered = ["hold", "partial_tp", "exit"]
    class_ids = [int(x) for x in list(classes or [])] or [0, 1, 2]
    labels: dict[int, str] = {}
    for idx, class_id in enumerate(class_ids):
        labels[int(class_id)] = ordered[idx] if idx < len(ordered) else f"class_{class_id}"
    collapse = dict(exit_meta.get("exit_action_collapse") or {})
    collapse_actions = list((((collapse.get("class_balance_after") or {})).keys())) if collapse else []
    if collapse_actions and len(collapse_actions) == len(class_ids):
        for idx, class_id in enumerate(class_ids):
            labels[int(class_id)] = str(collapse_actions[idx])
    return labels


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip01(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


def _append_policy_trace(
    meta: dict[str, Any],
    *,
    stage: str,
    verdict: str,
    reason: str,
    score: float | None = None,
    changed_decision: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    entry = f"{str(stage)}:{str(verdict)}:{str(reason or 'none')}"
    trace = list(meta.get("policy_trace", []) or [])
    trace.append(entry)
    meta["policy_trace"] = trace
    overlay_diag = dict(meta.get("overlay_diagnostics", {}) or {})
    verbose = list(overlay_diag.get("policy_trace_verbose", []) or [])
    verbose.append(
        {
            "stage": str(stage),
            "verdict": str(verdict),
            "reason": str(reason or "none"),
            "score": None if score is None else float(score),
            "changed_decision": bool(changed_decision),
            "details": dict(details or {}),
        }
    )
    overlay_diag["policy_trace_verbose"] = verbose
    meta["overlay_diagnostics"] = overlay_diag


def _risk_kernel_config_from_settings(
    *,
    settings: Any,
    freshness_limit_secs: float = 0.0,
    rollout_policy: dict[str, Any] | None = None,
) -> RiskKernelConfig:
    rollout = dict(rollout_policy or {})
    rollout_enabled = bool(rollout.get("enabled", rollout.get("active", False)))
    return RiskKernelConfig(
        max_spread_bps=float(_safe_float(getattr(settings, "max_allowed_spread_bps", 0.0), 0.0)),
        freshness_limit_secs=float(_safe_float(freshness_limit_secs, 0.0)),
        max_total_positions=max(0, int(getattr(settings, "max_total_positions", 0) or 0)),
        max_pair_positions=max(0, int(getattr(settings, "max_pair_positions", 0) or 0)),
        max_drawdown_pct=float(_safe_float(getattr(settings, "risk_max_drawdown_pct", 0.0), 0.0)),
        max_gross_exposure=float(_safe_float(getattr(settings, "risk_max_gross_exposure", 0.0), 0.0)),
        max_net_exposure=float(_safe_float(getattr(settings, "risk_max_net_exposure", 0.0), 0.0)),
        min_lots=float(_safe_float(getattr(settings, "min_order_lots", 0.01), 0.01)),
        lot_step=float(_safe_float(getattr(settings, "order_lot_step", 0.01), 0.01)),
        max_lots=float(_safe_float(getattr(settings, "max_order_lots", 0.0), 0.0)),
        rollout_mode=(str(rollout.get("mode") or "") if rollout_enabled else ""),
        rollout_pair_allowlisted=bool(rollout_enabled and rollout.get("pair_allowlisted", False)),
        rollout_budget_scale=float(_clip01(rollout.get("budget_scale", 1.0))) if rollout_enabled else 1.0,
        rollout_max_total_positions=max(0, int(rollout.get("max_total_positions", 0) or 0)) if rollout_enabled else 0,
        rollout_max_pair_positions=max(0, int(rollout.get("max_pair_positions", 0) or 0)) if rollout_enabled else 0,
        rollout_max_gross_exposure=float(_safe_float(rollout.get("max_gross_exposure"), 0.0)) if rollout_enabled else 0.0,
        rollout_max_net_exposure=float(_safe_float(rollout.get("max_net_exposure"), 0.0)) if rollout_enabled else 0.0,
    )


def _payload_from_approved_order(*, order: dict[str, Any], pair: str, ts_value: str, action_tag: str) -> dict[str, Any]:
    cmd_id = _build_command_id(pair=pair, ts_value=ts_value, action_tag=action_tag)
    payload = dict(order or {})
    payload["command_id"] = str(payload.get("command_id") or cmd_id)
    payload["trace_id"] = str(payload.get("trace_id") or cmd_id)
    payload["symbol"] = str(payload.get("symbol") or pair).upper()
    payload["cmd"] = str(payload.get("cmd") or payload.get("command") or "").upper()
    payload["action"] = str(payload.get("action") or action_tag)
    payload["side"] = str(payload.get("side") or "").upper()
    payload["lots"] = float(_safe_float(payload.get("lots"), 0.0))
    payload["close_lots"] = float(_safe_float(payload.get("close_lots"), 0.0))
    return payload


def _lifecycle_action_tag(lifecycle_action: str) -> str:
    action = str(lifecycle_action or "hold")
    if action == "tighten_stop":
        return "adjust_sl"
    if action == "partial_tp":
        return "close_partial"
    if action == "exit":
        return "exit"
    if action == "entry":
        return "entry"
    return "hold"


def _approved_order_for_lifecycle_action(
    *,
    pair: str,
    ts_value: str,
    lifecycle_action: str,
    lifecycle_reason: str,
    lifecycle_action_score: float,
    close_lots: float,
    sl_price: float,
) -> dict[str, Any]:
    action = str(lifecycle_action or "hold")
    base: dict[str, Any]
    if action == "tighten_stop":
        base = {
            "cmd": "MODIFY_SL",
            "symbol": str(pair).upper(),
            "lots": 0.0,
            "close_lots": 0.0,
            "sl_price": float(_safe_float(sl_price, 0.0)),
            "intent": "ADJUST_MODEL",
            "action": "tighten_stop",
            "action_score": float(_safe_float(lifecycle_action_score, 0.0)),
            "side": "",
        }
    elif action == "partial_tp":
        planned_close_lots = float(_safe_float(close_lots, 0.0))
        base = {
            "cmd": "CLOSE_PARTIAL",
            "symbol": str(pair).upper(),
            "lots": planned_close_lots,
            "close_lots": planned_close_lots,
            "intent": "EXIT_MODEL",
            "action": "partial_tp",
            "action_score": float(_safe_float(lifecycle_action_score, 0.0)),
            "side": "",
        }
    elif action == "exit":
        reversal_exit = str(lifecycle_reason or "") == "reversal_exit"
        cmd_id = _build_command_id(pair=str(pair).upper(), ts_value=str(ts_value), action_tag="exit")
        base = {
            "cmd": "CLOSE",
            "symbol": str(pair).upper(),
            "lots": 0.0,
            "close_lots": 0.0,
            "intent": "REVERSAL_EXIT" if reversal_exit else "EXIT_MODEL",
            "action": "exit",
            "action_score": float(_safe_float(lifecycle_action_score, 0.0)),
            "side": "",
            "reversal_token": cmd_id if reversal_exit else "",
        }
    else:
        return {}
    return _payload_from_approved_order(
        order=base,
        pair=str(pair).upper(),
        ts_value=str(ts_value),
        action_tag=_lifecycle_action_tag(action),
    )


def _sync_lifecycle_action_payloads(
    *,
    decision: dict[str, Any],
    action_item: dict[str, Any],
) -> None:
    meta = dict(decision.get("metadata", {}) or {})
    pair = str(action_item.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
    ts_value = str(action_item.get("ts_value") or meta.get("ts") or "")
    lifecycle_action = str(action_item.get("lifecycle_action") or meta.get("lifecycle_action") or "hold")
    lifecycle_reason = str(action_item.get("lifecycle_reason") or meta.get("lifecycle_reason") or "hold")
    lifecycle_action_score = float(
        _safe_float(action_item.get("lifecycle_action_score"), meta.get("lifecycle_action_score", 0.0))
    )
    close_lots = float(_safe_float(action_item.get("close_lots"), meta.get("close_lots", 0.0)))
    sl_price = float(_safe_float(action_item.get("sl_price"), meta.get("sl_price", 0.0)))
    approved_order = _approved_order_for_lifecycle_action(
        pair=pair,
        ts_value=ts_value,
        lifecycle_action=lifecycle_action,
        lifecycle_reason=lifecycle_reason,
        lifecycle_action_score=lifecycle_action_score,
        close_lots=close_lots,
        sl_price=sl_price,
    )
    action_item["approved_order"] = dict(approved_order)
    meta["approved_order"] = dict(approved_order)
    meta["lifecycle_action"] = str(lifecycle_action)
    meta["lifecycle_reason"] = str(lifecycle_reason)
    meta["close_lots"] = float(close_lots)
    meta["sl_price"] = float(sl_price)
    risk_decision = dict(meta.get("risk_decision") or {})
    if risk_decision:
        risk_decision["lifecycle_action"] = str(lifecycle_action)
        risk_decision["close_lots"] = float(close_lots)
        risk_decision["approved_order"] = dict(approved_order) if approved_order else None
        risk_meta = dict(risk_decision.get("metadata") or {})
        risk_meta["lifecycle_override_reason"] = str(lifecycle_reason)
        risk_decision["metadata"] = risk_meta
        meta["risk_decision"] = risk_decision
    decision["metadata"] = meta


def _evaluate_runtime_risk_kernel(
    *,
    pair: str,
    ts_value: str,
    side: str,
    signal: Any,
    expected_edge_bps: float,
    spread_bps: float,
    feature_bar: dict[str, Any],
    tick: dict[str, Any],
    spread_unit_source: str,
    mt4_fresh: bool,
    ticks_fresh: bool,
    paused: bool,
    positions: list[dict[str, Any]],
    pair_count: int,
    total_count: int,
    current_equity: float,
    planned_entry_lots: float,
    lifecycle_action: str,
    lifecycle_reason: str,
    lifecycle_action_score: float,
    close_lots: float,
    sl_price: float,
    rejection_reasons: list[str],
    state: dict[str, Any],
    settings: Any,
    portfolio_positions: list[dict[str, Any]] | None = None,
    rollout_policy: dict[str, Any] | None = None,
    governance_policy: dict[str, Any] | None = None,
    pending_entries: list[dict[str, Any]] | None = None,
    realized_returns_by_pair: dict[str, pd.Series] | None = None,
) -> dict[str, Any]:
    has_open_position = bool(positions)
    portfolio_positions = list(portfolio_positions or positions or [])
    rollout = dict(rollout_policy or {})
    rollout_enabled = bool(rollout.get("enabled", rollout.get("active", False)))
    governance_meta = dict(governance_policy or {})
    portfolio_allocation = evaluate_portfolio_allocation(
        symbol=str(pair).upper(),
        session_bucket=str(getattr(signal, "session_bucket", "")),
        expected_edge_bps=float(_safe_float(expected_edge_bps, 0.0)),
        uncertainty_score=max(0.0, 1.0 - float(_safe_float(getattr(signal, "trade_prob", 0.0), 0.0))),
        positions=list(portfolio_positions or []),
        pending_entries=list(pending_entries or []),
        max_total_positions=max(0, int(getattr(settings, "max_total_positions", 0) or 0)),
        max_pair_positions=max(0, int(getattr(settings, "max_pair_positions", 0) or 0)),
        governance=governance_meta,
        corr_mode=str(getattr(settings, "portfolio_corr_mode", "heuristic") or "heuristic"),
        realized_returns_by_pair=realized_returns_by_pair,
        corr_window_bars=int(getattr(settings, "portfolio_realized_corr_window_bars", 0) or 0),
        corr_min_obs=int(getattr(settings, "portfolio_realized_corr_min_obs", 0) or 0),
    )
    capital_budget_scale = float(capital_band_budget_scale(str(governance_meta.get("capital_band") or ""), settings))
    portfolio_budget_scale = float(
        max(
            0.0,
            min(
                1.0,
                float(_safe_float(portfolio_allocation.budget.budget_scale, 1.0))
                * float(_safe_float(governance_meta.get("budget_scale"), capital_budget_scale)),
            ),
        )
    )
    requested_lots = float(_safe_float(planned_entry_lots, 0.0)) * float(portfolio_budget_scale)
    policy_allowed = bool(not rejection_reasons) and bool(portfolio_allocation.allowed)
    policy_rejection_reason = str(
        rejection_reasons[0]
        if rejection_reasons
        else (portfolio_allocation.budget.reason if not portfolio_allocation.allowed else "none")
    )
    policy_intent = PolicyIntent(
        pair=str(pair).upper(),
        side=str(side).upper(),
        intent="EXIT_MODEL" if has_open_position and lifecycle_action in {"exit", "partial_tp", "tighten_stop"} else "ENTRY",
        action=str(lifecycle_action if has_open_position else "entry"),
        action_score=float(_safe_float(lifecycle_action_score if has_open_position else getattr(signal, "trade_prob", 0.0), 0.0)),
        strategy="fxstack_runtime",
        expected_edge_bps=float(_safe_float(expected_edge_bps, 0.0)),
        confidence=float(_safe_float(getattr(signal, "trade_prob", 0.0), 0.0)),
        metadata={
            "ts": str(ts_value),
            "policy_allowed": bool(policy_allowed),
            "policy_block_reason": str(policy_rejection_reason),
            "rejection_reason": str(policy_rejection_reason),
            "strict_reasons": list(rejection_reasons),
            "lifecycle_action": str(lifecycle_action),
            "lifecycle_reason": str(lifecycle_reason),
            "close_lots": float(_safe_float(close_lots, 0.0)),
            "sl_price": float(_safe_float(sl_price, 0.0)),
            "requested_lots": float(requested_lots),
            "planned_entry_lots": float(_safe_float(planned_entry_lots, 0.0)),
            "has_open_position": bool(has_open_position),
            "position_count_pair": int(pair_count),
            "position_count_total": int(total_count),
            "session_bucket": str(getattr(signal, "session_bucket", "")),
            "spread_unit_source": str(spread_unit_source),
            "reversal_ready": bool(hasattr(signal, "reversal_ready") and getattr(signal, "reversal_ready")),
            "rollout_mode": str(rollout.get("mode") or "") if rollout_enabled else "",
            "rollout_active": bool(rollout_enabled and rollout.get("active", False)),
            "rollout_pair_allowlisted": bool(rollout_enabled and rollout.get("pair_allowlisted", False)),
            "rollout_budget_scale": float(_clip01(rollout.get("budget_scale", 1.0))) if rollout_enabled else 1.0,
            "rollout_source": str(rollout.get("source") or "") if rollout_enabled else "",
            "portfolio_allocation_allowed": bool(portfolio_allocation.allowed),
            "portfolio_budget_scale": float(portfolio_budget_scale),
            "capital_budget_scale": float(capital_budget_scale),
            "portfolio_concentration": dict(portfolio_allocation.concentration.to_dict()),
            "portfolio_correlation": dict(portfolio_allocation.correlation.to_dict()),
            "portfolio_stress": dict(portfolio_allocation.stress.to_dict()),
            "governance_mode": str(governance_meta.get("mode") or ""),
        },
    )
    market_state = MarketState(
        pair=str(pair).upper(),
        ts=str(ts_value),
        session_bucket=str(getattr(signal, "session_bucket", "")),
        spread_bps=float(_safe_float(spread_bps, 0.0)),
        allowed_spread_bps=float(_safe_float(getattr(settings, "max_allowed_spread_bps", 0.0), 0.0)),
        marketable=bool(tick) and str(spread_unit_source) != "missing" and (not bool(paused)),
        market_open=not bool(getattr(signal, "session_entry_blocked", False)),
        data_fresh=bool(mt4_fresh and ticks_fresh and not bool(feature_bar.get("stale", False))),
        freshness_secs=(None if feature_bar.get("age_secs") is None else float(_safe_float(feature_bar.get("age_secs"), 0.0))),
        freshness_limit_secs=(None if feature_bar.get("stale_after_secs") is None else float(_safe_float(feature_bar.get("stale_after_secs"), 0.0))),
        metadata={
            "feature_bar_reason": str(feature_bar.get("reason") or ""),
            "tick_available": bool(tick),
            "spread_unit_source": str(spread_unit_source),
            "governance_paused": bool(paused),
            "mt4_fresh": bool(mt4_fresh),
            "ticks_fresh": bool(ticks_fresh),
        },
    )
    peak_equity = float(_safe_float(state.get("equity_peak", state.get("cycle_peak_equity", current_equity)), current_equity)) if isinstance(state, dict) else float(current_equity)
    drawdown_pct = 0.0
    if peak_equity > 0.0 and current_equity > 0.0:
        drawdown_pct = max(0.0, (1.0 - (float(current_equity) / float(peak_equity))) * 100.0)
    portfolio_open_count = int(len(portfolio_positions))
    portfolio_state = PortfolioState(
        equity=float(_safe_float(current_equity, 0.0)),
        balance=float(_safe_float(state.get("balance", current_equity), current_equity)) if isinstance(state, dict) else float(_safe_float(current_equity, 0.0)),
        peak_equity=float(peak_equity),
        drawdown_pct=float(drawdown_pct),
        open_position_count=int(portfolio_open_count),
        pair_position_count=int(pair_count),
        max_total_positions=max(0, int(getattr(settings, "max_total_positions", 0) or 0)),
        max_pair_positions=max(0, int(getattr(settings, "max_pair_positions", 0) or 0)),
        gross_exposure=float(portfolio_allocation.book.gross_exposure),
        net_exposure=float(portfolio_allocation.book.net_exposure),
        metadata={
            "position_signature": _position_signature(dict(positions[0] or {})) if positions else "",
            "position_side": _position_side(positions),
            "portfolio_book": dict(portfolio_allocation.book.to_dict()),
            "portfolio_telemetry": dict(portfolio_allocation.telemetry),
        },
    )
    decision = evaluate_risk_decision(
        policy_intent=policy_intent,
        market_state=market_state,
        portfolio_state=portfolio_state,
        config=_risk_kernel_config_from_settings(
            settings=settings,
            freshness_limit_secs=float(_safe_float(feature_bar.get("stale_after_secs"), 0.0)),
            rollout_policy=rollout,
        ),
    )
    rollout_meta = dict((decision.metadata or {}).get("rollout") or {})
    return {
        "decision": decision.to_dict(),
        "trace": [item.to_dict() for item in decision.trace],
        "approved_order": None if decision.approved_order is None else decision.approved_order.to_command_payload(),
        "verdict": str(decision.verdict),
        "reason": str(decision.reason),
        "lifecycle_action": str(decision.lifecycle_action),
        "close_lots": float(_safe_float(decision.close_lots, 0.0)),
        "final_lots": float(_safe_float(decision.final_lots, 0.0)),
        "rollout": rollout_meta,
        "portfolio_allocation": dict(portfolio_allocation.to_dict()),
        "portfolio_budget_scale": float(portfolio_budget_scale),
        "capital_budget_scale": float(capital_budget_scale),
        "governance": dict(governance_meta),
    }


def _overlay_inputs_for_decision(
    *,
    meta: dict[str, Any],
    current_row: dict[str, Any],
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
    sleeve_name = str(meta.get("adaptive_sleeve") or playbook_to_sleeve(meta.get("adaptive_playbook") or ""))
    secondary_sleeve = str(meta.get("belief_opposing_scenario") or "").strip()
    secondary_sleeve = playbook_to_sleeve(secondary_sleeve) if secondary_sleeve and secondary_sleeve != "no_edge" else ""
    return DeskOverlayInputs(
        belief_metrics={
            "directional_belief": _clip01(meta.get("belief_primary_rank_score", meta.get("belief_primary_score", 0.0))),
            "belief_gap": _clip01(meta.get("belief_gap", 0.0)),
            "confidence": _clip01(meta.get("belief_primary_ev_above_hurdle_prob", meta.get("trade_prob", 0.0))),
            "confirm_prob": _clip01(meta.get("belief_primary_confirm_prob", meta.get("trade_prob", 0.0))),
            "model_agreement": _clip01(1.0 - _safe_float(meta.get("belief_fragility_score", meta.get("model_disagreement_score", 0.0)), 0.0)),
            "signal_quality": _clip01(meta.get("structure_timing_score", meta.get("adaptive_entry_quality", 0.0))),
            "fail_fast_risk": _clip01(meta.get("belief_primary_fail_fast_prob", 0.0)),
            "expected_net_ev_bps": float(
                _safe_float(
                    meta.get("belief_primary_expected_net_ev_bps", meta.get("expected_edge_bps", meta.get("calibrated_ev_bps_shadow", 0.0))),
                    0.0,
                )
            ),
        },
        adaptive_playbook_metrics={
            "sleeve": sleeve_name,
            "adaptive_entry_quality": _clip01(meta.get("adaptive_entry_quality", 0.0)),
            "playbook_score": _clip01(meta.get("adaptive_playbook_score", current_row.get("playbook_score", 0.0))),
            "location_score": _clip01(meta.get("adaptive_location_score", current_row.get("location_score", 0.0))),
            "trigger_score": _clip01(meta.get("adaptive_trigger_score", current_row.get("trigger_score", 0.0))),
            "hostility_score": _clip01(meta.get("adaptive_hostility_score", current_row.get("hostility_score", 0.0))),
        },
        campaign_state={
            "state": str(meta.get("campaign_state") or ""),
            "proof_score": _clip01(meta.get("campaign_proof_score", 0.0)),
            "maturity_score": _clip01(meta.get("campaign_maturity_score", 0.0)),
            "reset_quality": _clip01(meta.get("campaign_reset_quality", 0.0)),
            "priority_boost": _clip01(meta.get("campaign_priority_boost", 0.0)),
        },
        sleeve_health={
            "sleeve": sleeve_name,
            "score": _clip01(getattr(sleeve_snapshot, "score", meta.get("sleeve_health_score", 0.5))),
            "state": str(getattr(sleeve_snapshot, "state", meta.get("sleeve_health_state", "healthy"))),
        },
        crowding={
            "currency_crowding": _clip01(meta.get("adaptive_currency_crowding_penalty", 0.0)),
            "pair_crowding": _clip01(_safe_float(meta.get("position_count_pair", 0.0), 0.0) / pair_slots),
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


def _sleeve_budget_targets_from_overlay(
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


def _risk_cycle_summary(*, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    verdict_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    action_counts: dict[str, int] = {}
    trace_rule_counts: dict[str, int] = {}
    approved_order_count = 0
    blocked_entry_count = 0
    exit_like_count = 0
    rollout_mode_counts: dict[str, int] = {}
    rollout_budget_scale_sum = 0.0
    rollout_budget_scale_count = 0
    rollout_allowlisted_pairs: set[str] = set()
    rollout_active_pairs: set[str] = set()
    rollout_breach_pairs: set[str] = set()
    rollout_reduced_budget_count = 0
    rollout_breach_count = 0
    rollout_blocked_count = 0
    rollout_reason_counts: dict[str, int] = {}
    for decision in list(decisions or []):
        meta = dict(decision.get("metadata", {}) or {})
        verdict = str(meta.get("risk_verdict") or "")
        reason = str(meta.get("risk_reason") or "")
        action = str(meta.get("lifecycle_action") or "")
        if verdict:
            verdict_counts[verdict] = int(verdict_counts.get(verdict, 0)) + 1
        if reason:
            reason_counts[reason] = int(reason_counts.get(reason, 0)) + 1
        if action:
            action_counts[action] = int(action_counts.get(action, 0)) + 1
        if dict(meta.get("approved_order") or {}):
            approved_order_count += 1
        if str(action) in {"exit", "partial_tp", "tighten_stop"}:
            exit_like_count += 1
        if not bool(meta.get("execution_ready", decision.get("execution_ready", False))) and str(action) == "entry":
            blocked_entry_count += 1
        rollout_meta = dict(meta.get("rollout") or {})
        rollout_mode = str(rollout_meta.get("mode") or "")
        if rollout_mode:
            rollout_mode_counts[rollout_mode] = int(rollout_mode_counts.get(rollout_mode, 0)) + 1
        if bool(rollout_meta.get("pair_allowlisted", False)):
            pair_name = str(meta.get("pair") or decision.get("symbol") or "").upper()
            if pair_name:
                rollout_allowlisted_pairs.add(pair_name)
        if bool(rollout_meta.get("active", False)):
            pair_name = str(meta.get("pair") or decision.get("symbol") or "").upper()
            if pair_name:
                rollout_active_pairs.add(pair_name)
            rollout_budget_scale_sum += float(_safe_float(rollout_meta.get("budget_scale", 1.0), 1.0))
            rollout_budget_scale_count += 1
        if bool(rollout_meta.get("reduced_budget", False)):
            rollout_reduced_budget_count += 1
        if bool(rollout_meta.get("breach", False)):
            rollout_breach_count += 1
            pair_name = str(meta.get("pair") or decision.get("symbol") or "").upper()
            if pair_name:
                rollout_breach_pairs.add(pair_name)
            breach_reason = str(rollout_meta.get("breach_reason") or "rollout_breach")
            rollout_reason_counts[breach_reason] = int(rollout_reason_counts.get(breach_reason, 0)) + 1
            if str(meta.get("risk_verdict") or "") == "block":
                rollout_blocked_count += 1
        for item in list(meta.get("risk_trace") or []):
            rule = str(dict(item or {}).get("rule") or "")
            if not rule:
                continue
            trace_rule_counts[rule] = int(trace_rule_counts.get(rule, 0)) + 1
    dominant_reason = next(iter(sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))), ("", 0))[0]
    dominant_rollout_reason = next(iter(sorted(rollout_reason_counts.items(), key=lambda item: (-item[1], item[0]))), ("", 0))[0]
    rollout_summary = {
        "mode_counts": {str(k): int(v) for k, v in sorted(rollout_mode_counts.items()) if str(k)},
        "allowlisted_pairs": sorted(rollout_allowlisted_pairs),
        "active_pairs": sorted(rollout_active_pairs),
        "reduced_budget_count": int(rollout_reduced_budget_count),
        "breach_count": int(rollout_breach_count),
        "blocked_count": int(rollout_blocked_count),
        "breach_pairs": sorted(rollout_breach_pairs),
        "breach_reason_counts": {str(k): int(v) for k, v in sorted(rollout_reason_counts.items()) if str(k)},
        "dominant_breach_reason": str(dominant_rollout_reason),
        "avg_budget_scale": float(rollout_budget_scale_sum / rollout_budget_scale_count) if rollout_budget_scale_count else 0.0,
    }
    return {
        "decision_count": int(len(list(decisions or []))),
        "approved_order_count": int(approved_order_count),
        "blocked_entry_count": int(blocked_entry_count),
        "exit_like_count": int(exit_like_count),
        "verdict_counts": {str(k): int(v) for k, v in sorted(verdict_counts.items()) if str(k)},
        "reason_counts": {str(k): int(v) for k, v in sorted(reason_counts.items()) if str(k)},
        "action_counts": {str(k): int(v) for k, v in sorted(action_counts.items()) if str(k)},
        "trace_rule_counts": {str(k): int(v) for k, v in sorted(trace_rule_counts.items()) if str(k)},
        "dominant_block_reason": str(dominant_reason),
        "rollout_active_count": int(len(rollout_active_pairs)),
        "rollout_reduced_budget_count": int(rollout_reduced_budget_count),
        "rollout_breach_count": int(rollout_breach_count),
        "rollout": rollout_summary,
    }


def _rollout_policy_summary(*, model_sets: dict[str, LoadedModelSet]) -> dict[str, Any]:
    configured_pairs: list[str] = []
    active_pairs: list[str] = []
    allowlisted_pairs: list[str] = []
    mode_counts: Counter[str] = Counter()
    pair_budget_scale: dict[str, float] = {}
    sources: dict[str, str] = {}
    for pair, loaded in sorted(model_sets.items()):
        rollout = dict(getattr(loaded, "rollout_policy", {}) or {})
        if not bool(rollout.get("configured", False)):
            continue
        pair_key = str(pair).upper()
        configured_pairs.append(pair_key)
        mode = str(rollout.get("mode") or "")
        if mode:
            mode_counts[mode] += 1
        if bool(rollout.get("pair_allowlisted", False)):
            allowlisted_pairs.append(pair_key)
        if bool(rollout.get("active", False)):
            active_pairs.append(pair_key)
            pair_budget_scale[pair_key] = float(_clip01(rollout.get("budget_scale", 1.0)))
        source = str(rollout.get("source") or "")
        if source:
            sources[pair_key] = source
    return {
        "configured_pairs": configured_pairs,
        "allowlisted_pairs": allowlisted_pairs,
        "active_pairs": active_pairs,
        "configured_count": int(len(configured_pairs)),
        "active_count": int(len(active_pairs)),
        "mode_counts": {str(k): int(v) for k, v in sorted(mode_counts.items()) if str(k)},
        "pair_budget_scale": {str(k): float(v) for k, v in sorted(pair_budget_scale.items())},
        "sources": dict(sorted(sources.items())),
    }


def _adaptive_overlay_summary(
    *,
    decisions: list[dict[str, Any]],
    overlay_outputs: dict[int, Any],
    allocator_cycle: dict[str, Any],
    environment_counts: dict[str, int],
) -> dict[str, Any]:
    def _cycle_float(key: str) -> float:
        return float(_safe_float(allocator_cycle.get(key, 0.0), 0.0))

    conviction_scores = [float(getattr(out, "conviction_score", 0.0)) for out in overlay_outputs.values()]
    band_counts: Counter[str] = Counter(str(getattr(out, "conviction_band", "")) for out in overlay_outputs.values())
    stage_counts: Counter[str] = Counter(str(getattr(out, "thesis_stage", "")) for out in overlay_outputs.values())
    posture_counts: Counter[str] = Counter(str(getattr(out, "portfolio_posture", "")) for out in overlay_outputs.values())
    replacement_scores = [float(getattr(out, "replacement_urgency", 0.0)) for out in overlay_outputs.values()]
    divergence_matrix: dict[str, dict[str, dict[str, int]]] = {
        "by_pair": {},
        "by_session": {},
        "by_regime": {},
        "by_sleeve": {},
    }
    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        divergence = str(meta.get("adaptive_shadow_live_divergence") or "unknown")
        dimensions = {
            "by_pair": str(meta.get("pair") or decision.get("symbol") or "").upper(),
            "by_session": str(meta.get("session_bucket") or ""),
            "by_regime": str(meta.get("adaptive_environment_state") or ""),
            "by_sleeve": str(meta.get("adaptive_sleeve") or ""),
        }
        for bucket_name, key in dimensions.items():
            if not key:
                continue
            bucket = divergence_matrix[bucket_name].setdefault(key, {})
            bucket[divergence] = int(bucket.get(divergence, 0)) + 1
    return {
        "conviction_score_avg": float(sum(conviction_scores) / max(1, len(conviction_scores))) if conviction_scores else 0.0,
        "conviction_score_max": float(max(conviction_scores)) if conviction_scores else 0.0,
        "conviction_score_min": float(min(conviction_scores)) if conviction_scores else 0.0,
        "conviction_band_counts": {k: int(v) for k, v in sorted(band_counts.items()) if str(k)},
        "thesis_stage_counts": {k: int(v) for k, v in sorted(stage_counts.items()) if str(k)},
        "posture_counts": {k: int(v) for k, v in sorted(posture_counts.items()) if str(k)},
        "sleeve_budget_target_total": int(sum(int(v) for v in dict(allocator_cycle.get("sleeve_budget_targets", {}) or {}).values())),
        "sleeve_budget_used_total": int(sum(int(v) for v in dict(allocator_cycle.get("sleeve_budget_used", {}) or {}).values())),
        "pair_pressure_avg": _cycle_float("pair_pressure_avg"),
        "pair_pressure_max": _cycle_float("pair_pressure_max"),
        "session_pressure_avg": _cycle_float("session_pressure_avg"),
        "session_pressure_max": _cycle_float("session_pressure_max"),
        "sleeve_pressure_avg": _cycle_float("sleeve_pressure_avg"),
        "sleeve_pressure_max": _cycle_float("sleeve_pressure_max"),
        "correlation_pressure_avg": _cycle_float("correlation_pressure_avg"),
        "correlation_pressure_max": _cycle_float("correlation_pressure_max"),
        "risk_pressure_avg": _cycle_float("risk_pressure_avg"),
        "risk_pressure_max": _cycle_float("risk_pressure_max"),
        "replacement_urgency_avg": float(sum(replacement_scores) / max(1, len(replacement_scores))) if replacement_scores else 0.0,
        "policy_trace_count": int(
            sum(1 for decision in decisions if list(dict(decision.get("metadata", {}) or {}).get("policy_trace", []) or []))
        ),
        "diagnostics": {
            "environment_posture": next(iter(sorted(environment_counts.items(), key=lambda item: (-item[1], item[0]))), ("", 0))[0],
            "sleeve_budget_state": {
                key: {
                    "target": int(dict(allocator_cycle.get("sleeve_budget_targets", {}) or {}).get(key, 0)),
                    "used": int(dict(allocator_cycle.get("sleeve_budget_used", {}) or {}).get(key, 0)),
                    "candidates": int(dict(allocator_cycle.get("sleeve_candidate_counts", {}) or {}).get(key, 0)),
                }
                for key in sorted(
                    set(dict(allocator_cycle.get("sleeve_candidate_counts", {}) or {}))
                    | set(dict(allocator_cycle.get("sleeve_budget_targets", {}) or {}))
                    | set(dict(allocator_cycle.get("sleeve_budget_used", {}) or {}))
                )
            },
            "replacement_pressure_by_sleeve": {
                key: float(
                    max(
                        0.0,
                        1.0
                        - (
                            float(dict(allocator_cycle.get("sleeve_budget_used", {}) or {}).get(key, 0))
                            / max(1.0, float(dict(allocator_cycle.get("sleeve_budget_targets", {}) or {}).get(key, 1)))
                        ),
                    )
                )
                for key in sorted(set(dict(allocator_cycle.get("sleeve_budget_targets", {}) or {})))
            },
            "portfolio_pressure": {
                "pair_avg": _cycle_float("pair_pressure_avg"),
                "pair_max": _cycle_float("pair_pressure_max"),
                "session_avg": _cycle_float("session_pressure_avg"),
                "session_max": _cycle_float("session_pressure_max"),
                "sleeve_avg": _cycle_float("sleeve_pressure_avg"),
                "sleeve_max": _cycle_float("sleeve_pressure_max"),
                "correlation_avg": _cycle_float("correlation_pressure_avg"),
                "correlation_max": _cycle_float("correlation_pressure_max"),
                "risk_avg": _cycle_float("risk_pressure_avg"),
                "risk_max": _cycle_float("risk_pressure_max"),
            },
            "divergence_matrix": divergence_matrix,
            "press_count": int(stage_counts.get("press", 0)),
            "stand_down_count": int(stage_counts.get("stand_down", 0)),
        },
    }


def _timeframe_to_seconds(timeframe: str) -> int:
    txt = str(timeframe or "").strip().upper()
    if not txt:
        return 0
    if txt == "D":
        return 86_400
    if txt == "W":
        return 604_800
    if txt in {"MN", "MN1"}:
        return 2_592_000
    unit = txt[:1]
    magnitude = txt[1:] or "1"
    try:
        value = int(magnitude)
    except Exception:
        return 0
    scale = {
        "S": 1,
        "M": 60,
        "H": 3_600,
        "D": 86_400,
    }.get(unit, 0)
    return int(value * scale) if scale > 0 else 0


def _feature_bar_freshness(*, ts_value: Any, loop_ts: float, timeframe: str) -> dict[str, Any]:
    parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    timeframe_secs = max(0, _timeframe_to_seconds(timeframe))
    stale_after_secs = max(float(timeframe_secs * 2), 600.0)
    if pd.isna(parsed):
        return {
            "ts": str(ts_value or ""),
            "age_secs": None,
            "stale": True,
            "stale_after_secs": stale_after_secs,
            "reason": "missing_feature_ts",
        }
    age_secs = max(0.0, float(loop_ts) - float(parsed.timestamp()))
    return {
        "ts": str(parsed),
        "age_secs": float(age_secs),
        "stale": bool(age_secs > stale_after_secs),
        "stale_after_secs": float(stale_after_secs),
        "reason": "ok" if age_secs <= stale_after_secs else "stale_feature_bar",
    }


def _bars_to_raw_frame(*, pair: str, timeframe: str, bars: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    tf = str(timeframe).upper()
    sym = str(pair).upper()
    for bar in list(bars or []):
        ts = pd.to_datetime(bar.get("time") or bar.get("ts"), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        spread = _safe_float(bar.get("spread"), 0.0)
        mid_open = _safe_float(bar.get("mid_open", bar.get("open")), 0.0)
        mid_high = _safe_float(bar.get("mid_high", bar.get("high")), 0.0)
        mid_low = _safe_float(bar.get("mid_low", bar.get("low")), 0.0)
        mid_close = _safe_float(bar.get("mid_close", bar.get("close")), 0.0)
        if min(mid_open, mid_high, mid_low, mid_close) <= 0.0:
            continue
        half_spread = spread / 2.0
        bid_open = _safe_float(bar.get("bid_open"), mid_open - half_spread)
        bid_high = _safe_float(bar.get("bid_high"), mid_high - half_spread)
        bid_low = _safe_float(bar.get("bid_low"), mid_low - half_spread)
        bid_close = _safe_float(bar.get("bid_close"), mid_close - half_spread)
        ask_open = _safe_float(bar.get("ask_open"), mid_open + half_spread)
        ask_high = _safe_float(bar.get("ask_high"), mid_high + half_spread)
        ask_low = _safe_float(bar.get("ask_low"), mid_low + half_spread)
        ask_close = _safe_float(bar.get("ask_close"), mid_close + half_spread)
        rows.append(
            {
                "pair": sym,
                "timeframe": tf,
                "ts": ts,
                "bid_open": float(bid_open),
                "bid_high": float(bid_high),
                "bid_low": float(bid_low),
                "bid_close": float(bid_close),
                "ask_open": float(ask_open),
                "ask_high": float(ask_high),
                "ask_low": float(ask_low),
                "ask_close": float(ask_close),
                "mid_open": float(mid_open),
                "mid_high": float(mid_high),
                "mid_low": float(mid_low),
                "mid_close": float(mid_close),
                "volume": int(_safe_float(bar.get("volume"), 0.0)),
                "spread": float(spread),
                "date": pd.to_datetime(ts, utc=True).strftime("%Y-%m-%d"),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("ts").drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")


def _feature_tail_spec(timeframe: str) -> tuple[int, int]:
    tf = str(timeframe).upper()
    if tf == "M5":
        return 14, 3000
    if tf == "H4":
        return 45, 400
    if tf == "D":
        return 120, 200
    return 30, 1000


def _refresh_feature_tail(
    *,
    feature_store: ParquetStore,
    raw_store: ParquetStore,
    provider: str,
    pair: str,
    timeframe: str,
) -> dict[str, Any]:
    tail_files, max_rows = _feature_tail_spec(timeframe)
    raw_recent = raw_store.read_recent_rows(
        provider=provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        tail_files=tail_files,
        max_rows=max_rows,
    )
    if raw_recent.empty:
        return {"ok": False, "reason": "raw_recent_empty"}
    feats = add_fx_lifecycle_features(raw_recent)
    if feats.empty:
        return {"ok": False, "reason": "feature_build_empty"}
    feature_store.write_partitioned(
        feats,
        provider=provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
    )
    latest_ts = str(feats.sort_values("ts").iloc[-1]["ts"])
    return {"ok": True, "reason": "refreshed", "latest_ts": latest_ts, "rows": int(len(feats))}


def _enqueue_feature_pushes(
    *,
    svc: Any | None,
    feature_store: ParquetStore,
    provider: str,
    pair: str,
    feature_refresh: dict[str, Any],
) -> dict[str, Any]:
    s = get_settings()
    enabled = bool(getattr(s, "feature_push_enabled", False) or getattr(s, "feast_enabled", False))
    if svc is None or not enabled:
        return {"enabled": False, "queued": 0, "mode": "disabled"}

    queued: dict[str, Any] = {}
    service_names = {
        "M5": f"fx_{str(pair).lower()}_intraday_xgb_m5",
        "H4": f"fx_{str(pair).lower()}_regime_hmm_h4",
        "D": f"fx_{str(pair).lower()}_swing_xgb_d",
    }
    for timeframe, diag in dict(feature_refresh or {}).items():
        if not bool(dict(diag or {}).get("ok")):
            continue
        latest = feature_store.read_latest_row(
            provider=provider,
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
            tail_files=3,
        )
        if latest.empty:
            continue
        row = dict(latest.iloc[0].to_dict())
        ts = pd.to_datetime(row.get("ts"), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        payload = build_push_payload(
            pair=str(pair).upper(),
            feature_service=str(service_names.get(str(timeframe).upper(), f"fx_{str(pair).lower()}_{str(timeframe).lower()}")),
            entity_key=str(pair).upper(),
            event_timestamp=float(pd.Timestamp(ts).timestamp()),
            feature_values=row,
            feature_version=str(timeframe).upper(),
            checksum=hash_mapping(
                {
                    "pair": str(pair).upper(),
                    "timeframe": str(timeframe).upper(),
                    "ts": str(row.get("ts") or ""),
                }
            )[:16],
            source="runtime_feature_tail",
        )
        queued_row = svc.enqueue_feature_push(payload)
        queued[str(timeframe).upper()] = {
            "outbox_key": str(queued_row.get("outbox_key") or payload.get("outbox_key") or ""),
            "feature_service": str(queued_row.get("feature_service") or payload.get("feature_service") or ""),
        }
    return {
        "enabled": True,
        "mode": "feast_enabled" if bool(getattr(s, "feast_enabled", False)) and not bool(getattr(s, "feature_push_enabled", False)) else "feature_push_enabled",
        "queued": int(len(queued)),
        "items": queued,
    }


def _tick_bucket_start(*, tick: dict[str, Any], timeframe: str) -> int | None:
    ts = _safe_float(dict(tick or {}).get("ts_epoch"), 0.0)
    tf_secs = max(0, _timeframe_to_seconds(timeframe))
    if ts <= 0.0 or tf_secs <= 0:
        return None
    return int(ts // tf_secs) * tf_secs


def _refresh_live_pair_market_data(
    *,
    bridge_url: str,
    raw_store: ParquetStore,
    feature_store: ParquetStore,
    pair: str,
    provider: str,
    market_provider: str = "",
    latest_bar_cache: dict[str, str],
    svc: Any | None = None,
) -> dict[str, Any]:
    bars = fetch_market_bars(
        bridge_url,
        symbol=pair,
        timeframe="M5",
        limit=1000,
        provider=str(market_provider or ""),
    )
    raw_m5 = _bars_to_raw_frame(pair=pair, timeframe="M5", bars=bars)
    if raw_m5.empty:
        return {"ok": False, "reason": "no_market_bars", "provider": str(market_provider or provider or "")}

    latest_ts = str(raw_m5.sort_values("ts").iloc[-1]["ts"])
    pair_key = str(pair).upper()
    if latest_bar_cache.get(pair_key) == latest_ts:
        return {"ok": True, "reason": "already_current", "latest_ts": latest_ts}

    raw_store.write_partitioned(raw_m5, provider=provider, pair=pair_key, timeframe="M5")
    for tf in ("M15", "H1", "H4", "D"):
        resampled = resample_bars(raw_m5, tf)
        if not resampled.empty:
            raw_store.write_partitioned(resampled, provider=provider, pair=pair_key, timeframe=tf)

    feature_diag: dict[str, Any] = {}
    for tf in ("M5", "H4", "D"):
        feature_diag[tf] = _refresh_feature_tail(
            feature_store=feature_store,
            raw_store=raw_store,
            provider=provider,
            pair=pair,
            timeframe=tf,
        )
    feature_push = _enqueue_feature_pushes(
        svc=svc,
        feature_store=feature_store,
        provider=provider,
        pair=pair,
        feature_refresh=feature_diag,
    )

    latest_bar_cache[pair_key] = latest_ts
    return {
        "ok": True,
        "reason": "refreshed",
        "latest_ts": latest_ts,
        "feature_refresh": feature_diag,
        "feature_push": feature_push,
    }


# AGENT FLOW: Lot sizing, partial-close, and position signature helpers bridge lifecycle decisions to broker-safe command payloads.
def _round_lot_size(*, lots: float, min_lot: float, lot_step: float, max_lot: float) -> float:
    step = max(1e-9, float(lot_step))
    minimum = max(0.0, float(min_lot))
    maximum = max(0.0, float(max_lot))
    raw = max(0.0, float(lots))
    quantized = math.floor((raw / step) + 1e-9) * step
    quantized = max(minimum, quantized)
    if maximum > 0.0:
        quantized = min(maximum, quantized)
    decimals = max(0, int(round(-math.log10(step)))) if step < 1.0 else 0
    return round(float(quantized), decimals)


def _partial_close_plan(*, lots_open: float, fraction: float, settings: Any) -> tuple[str, float]:
    open_lots = max(0.0, float(lots_open))
    close_fraction = max(0.0, float(fraction))
    if open_lots <= 0.0 or close_fraction <= 0.0:
        return "hold", 0.0

    min_lot = max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01))
    lot_step = max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01))
    requested_close = open_lots * close_fraction
    rounded_close = _round_lot_size(
        lots=requested_close,
        min_lot=min_lot,
        lot_step=lot_step,
        max_lot=open_lots,
    )
    tolerance = max(1e-9, lot_step / 10.0)
    remaining_lots = max(0.0, open_lots - rounded_close)
    if rounded_close <= 0.0:
        return "hold", 0.0
    if rounded_close >= (open_lots - tolerance):
        return "exit", round(float(open_lots), 8)
    if 0.0 < remaining_lots < (min_lot - tolerance):
        return "exit", round(float(open_lots), 8)
    return "partial_tp", round(float(rounded_close), 8)


def _position_signature(position: dict[str, Any]) -> str:
    pos = dict(position or {})
    symbol = str(pos.get("symbol") or pos.get("broker_symbol") or "").strip().upper()
    side = _position_side([pos])
    try:
        open_time = int(float(pos.get("open_time", 0.0) or 0.0))
    except Exception:
        open_time = 0
    open_price = _safe_float(pos.get("open_price", 0.0), 0.0)
    try:
        magic = int(float(pos.get("magic", 0.0) or 0.0))
    except Exception:
        magic = 0
    return f"{symbol}|{side}|{open_time}|{float(open_price):.8f}|{magic}"


def _active_position_signatures(state: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for raw in list(state.get("positions", []) or []):
        key = _position_signature(dict(raw or {}))
        if key:
            out.add(key)
    return out


def _prune_partial_close_tracker(
    tracker: dict[str, dict[str, Any]],
    *,
    active_signatures: set[str],
) -> None:
    for key in list(tracker.keys()):
        if key not in active_signatures:
            tracker.pop(key, None)


def _partial_close_guard(
    *,
    tracker_state: dict[str, Any] | None,
    loop_ts: float,
    settings: Any,
) -> tuple[bool, str, float]:
    state = dict(tracker_state or {})
    max_partials = max(0, int(getattr(settings, "max_partial_closes_per_position", 0) or 0))
    partial_count = max(0, int(state.get("count", 0) or 0))
    if max_partials > 0 and partial_count >= max_partials:
        return False, "partial_tp_limit_reached", 0.0

    cooldown_secs = max(0.0, _safe_float(getattr(settings, "partial_close_cooldown_secs", 0.0), 0.0))
    last_partial_ts = _safe_float(state.get("last_partial_ts", 0.0), 0.0)
    if cooldown_secs > 0.0 and last_partial_ts > 0.0:
        elapsed = max(0.0, float(loop_ts) - float(last_partial_ts))
        remaining = max(0.0, float(cooldown_secs) - float(elapsed))
        if remaining > 0.0:
            return False, "partial_tp_cooldown_active", float(remaining)

    return True, "", 0.0


def _entry_order_lots(*, state: dict[str, Any], settings: Any, equity_seed: float) -> tuple[float, dict[str, Any]]:
    equity_live = _safe_float(state.get("equity", 0.0), 0.0)
    equity_value = equity_live if equity_live > 0.0 else _safe_float(equity_seed, 0.0)
    raw_lots = 0.0
    sizing_mode = "fixed_default"
    coefficient = max(0.0, _safe_float(getattr(settings, "equity_lots_per_usd", 0.0), 0.0))
    if equity_value > 0.0 and coefficient > 0.0:
        raw_lots = equity_value * coefficient
        sizing_mode = "equity_scaled"
    else:
        raw_lots = max(0.0, _safe_float(getattr(settings, "default_order_lots", 0.0), 0.0))
    rounded_lots = _round_lot_size(
        lots=raw_lots,
        min_lot=max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01)),
        lot_step=max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01)),
        max_lot=max(0.0, _safe_float(getattr(settings, "max_order_lots", 0.0), 0.0)),
    )
    return rounded_lots, {
        "mode": sizing_mode,
        "equity": float(equity_value),
        "coefficient": float(coefficient),
        "raw_lots": float(raw_lots),
        "rounded_lots": float(rounded_lots),
    }


def _startup_log(message: str) -> None:
    print(f"[runtime-startup] {str(message)}", flush=True)


def _parse_model_load_failure_context(message: str) -> dict[str, str]:
    text = str(message or "").strip()
    out = {"component": "model_load", "pair": "", "reason": text}
    if not text:
        return out

    match = re.match(r"missing active model sets for pairs:\s*(?P<pairs>.+)", text, flags=re.IGNORECASE)
    if match:
        out["component"] = "active_model_sets"
        out["pair"] = str(match.group("pairs") or "").strip()
        return out

    match = re.match(r"failed loading required models for\s+(?P<pair>[^:]+):\s*(?P<details>.+)", text, flags=re.IGNORECASE)
    if match:
        out["pair"] = str(match.group("pair") or "").strip()
        details = str(match.group("details") or "").strip()
        for chunk in details.split(","):
            item = str(chunk or "").strip()
            if not item or "=" not in item:
                continue
            component, reason = item.split("=", 1)
            component = str(component or "").strip()
            reason = str(reason or "").strip()
            if not component:
                continue
            if reason and reason.lower() not in {"ok", "none", "missing_path"}:
                out["component"] = component
                out["reason"] = reason
                return out
        out["component"] = "model_bundle"
        out["reason"] = details or text
        return out

    for pattern, component in (
        (r"failed loading exit model for\s+(?P<pair>[^:]+):\s*(?P<reason>.+)", "exit_model"),
        (r"failed loading reversal failure model for\s+(?P<pair>[^:]+):\s*(?P<reason>.+)", "reversal_failure"),
        (r"failed loading reversal opportunity model for\s+(?P<pair>[^:]+):\s*(?P<reason>.+)", "reversal_opportunity"),
        (r"failed loading swing models for\s+(?P<pair>[^ ]+)\s+under policy=(?P<reason>.+)", "swing"),
        (r"failed loading intraday models for\s+(?P<pair>[^ ]+)\s+under policy=(?P<reason>.+)", "intraday"),
        (r"failed loading directional belief model for\s+(?P<pair>[^:]+):\s*(?P<reason>.+)", "directional_belief"),
        (r"failed loading active model sets for\s+(?P<pair>[^:]+):\s*(?P<reason>.+)", "active_model_sets"),
    ):
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            out["component"] = component
            out["pair"] = str(match.groupdict().get("pair") or "").strip()
            out["reason"] = str(match.groupdict().get("reason") or text).strip() or text
            return out

    if "TimeoutError" in text or "model_load_timeout" in text:
        out["component"] = "model_load_timeout"
    return out


def _runtime_startup_state(
    *,
    boot_id: str,
    booted_at: str,
    runtime_pid: int,
    phase: str,
    phase_pair: str = "",
    phase_index: int = 0,
    phase_total: int = 0,
    last_progress_ts: float | None = None,
    failure_component: str = "",
    failure_pair: str = "",
    failure_reason: str = "",
    failed_at: str = "",
    pending_command_policy: str = "purge_and_mark_stale",
) -> dict[str, Any]:
    progress_ts = float(last_progress_ts if last_progress_ts is not None else time.time())
    return {
        "boot_id": str(boot_id),
        "booted_at": str(booted_at),
        "runtime_pid": int(runtime_pid),
        "phase": str(phase),
        "phase_pair": str(phase_pair or ""),
        "phase_index": int(phase_index),
        "phase_total": int(phase_total),
        "last_progress_ts": float(progress_ts),
        "failure_component": str(failure_component or ""),
        "failure_pair": str(failure_pair or ""),
        "failure_reason": str(failure_reason or ""),
        "failed_at": str(failed_at or ""),
        "pending_command_policy": str(pending_command_policy or "purge_and_mark_stale"),
    }


def _runtime_boot_reset_patch(
    *,
    runtime_profile: str,
    equity_seed: float,
    pairs: list[str],
    startup_state: dict[str, Any],
    runtime_diag: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "runtime_profile": str(runtime_profile),
        "runtime_status": "starting",
        "runtime_last_cycle_ts": 0.0,
        "runtime_equity_seed": float(equity_seed),
        "configured_pairs": list(pairs),
        "agent_decisions": [],
        "agent_diagnostics": {},
        "monitor": {},
        "vol": 0.0,
        "runtime_diag": dict(runtime_diag or {}),
        "runtime_startup": dict(startup_state),
        "__prune_stale__": True,
    }


def _touch_runtime_startup_progress(
    *,
    svc: Any,
    startup_state: dict[str, Any],
    phase: str,
    phase_pair: str = "",
    phase_index: int = 0,
    phase_total: int = 0,
    runtime_diag: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_state = _runtime_startup_state(
        boot_id=str(startup_state.get("boot_id") or ""),
        booted_at=str(startup_state.get("booted_at") or ""),
        runtime_pid=int(startup_state.get("runtime_pid") or 0),
        phase=str(phase),
        phase_pair=str(phase_pair or ""),
        phase_index=int(phase_index),
        phase_total=int(phase_total),
        last_progress_ts=float(time.time()),
        failure_component="",
        failure_pair="",
        failure_reason="",
        failed_at="",
        pending_command_policy=str(startup_state.get("pending_command_policy") or "purge_and_mark_stale"),
    )
    patch = {
        "runtime_status": "starting",
        "runtime_last_cycle_ts": 0.0,
        "runtime_startup": dict(next_state),
    }
    if runtime_diag is not None:
        patch["runtime_diag"] = dict(runtime_diag)
    svc.record_runtime_boot_state(boot=next_state, patch=patch, prune_state=False)
    return next_state


def _touch_runtime_loop_progress(*, svc: Any, startup_state: dict[str, Any]) -> dict[str, Any]:
    next_state = _runtime_startup_state(
        boot_id=str(startup_state.get("boot_id") or ""),
        booted_at=str(startup_state.get("booted_at") or ""),
        runtime_pid=int(startup_state.get("runtime_pid") or 0),
        phase="main_loop",
        phase_pair="",
        phase_index=0,
        phase_total=0,
        last_progress_ts=float(time.time()),
        failure_component="",
        failure_pair="",
        failure_reason="",
        failed_at="",
        pending_command_policy=str(startup_state.get("pending_command_policy") or "purge_and_mark_stale"),
    )
    svc.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": float(time.time()),
            "runtime_startup": dict(next_state),
        }
    )
    return next_state


def _record_runtime_startup_failure(
    *,
    svc: Any,
    startup_state: dict[str, Any],
    failure_reason: str,
    runtime_diag: dict[str, Any] | None = None,
) -> None:
    failure_ts = float(time.time())
    failed_iso = pd.Timestamp(failure_ts, unit="s", tz="UTC").isoformat()
    boot_state = dict(startup_state)
    boot_state["failure_reason"] = str(failure_reason or "")
    boot_state["failed_at"] = str(failed_iso)
    svc.record_runtime_boot_failure(
        boot=boot_state,
        failure_reason=str(failure_reason or ""),
        failed_at=failed_iso,
        patch={
            "runtime_status": "failed",
            "runtime_last_cycle_ts": 0.0,
            "agent_decisions": [],
            "agent_diagnostics": {},
            "monitor": {},
            "vol": 0.0,
            "runtime_diag": dict(runtime_diag or {}),
        },
        prune_state=True,
    )


_CHALLENGER_CONFLICT_SOFT_GAP = 0.20
_CHALLENGER_CONFLICT_HARD_GAP = 0.35
_CHALLENGER_CONFLICT_MODE_ALIASES = {
    "off": "off",
    "none": "off",
    "disabled": "off",
    "false": "off",
    "0": "off",
    "telemetry": "telemetry",
    "soft_gate": "soft_gate",
    "soft": "soft_gate",
    "warn": "soft_gate",
    "hard_gate": "hard_gate",
    "hard": "hard_gate",
    "block": "hard_gate",
}


def _normalize_challenger_conflict_mode(mode: str) -> str:
    return str(_CHALLENGER_CONFLICT_MODE_ALIASES.get(str(mode or "").strip().lower(), "off"))


class _PolicyModelRouter:
    def __init__(
        self,
        *,
        policy: str,
        family: str,
        primary_name: str,
        primary_model: Any | None,
        fallback_name: str,
        fallback_model: Any | None,
    ) -> None:
        self.policy = str(policy)
        self.family = str(family)
        self.primary_name = str(primary_name)
        self.primary_model = primary_model
        self.fallback_name = str(fallback_name)
        self.fallback_model = fallback_model
        self.last_selected_model = ""
        self.last_fallback_reason = ""

    @property
    def feature_columns(self) -> list[str]:
        return _required_model_feature_columns(self.primary_model, self.fallback_model)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        self.last_selected_model = ""
        self.last_fallback_reason = ""
        primary_error = ""

        if self.primary_model is not None:
            try:
                out = self.primary_model.predict_proba(X)
                self.last_selected_model = self.primary_name
                return out
            except Exception as exc:
                primary_error = f"{self.primary_name}_inference_error:{type(exc).__name__}"
                self.last_fallback_reason = primary_error

        if self.fallback_model is not None:
            try:
                out = self.fallback_model.predict_proba(X)
                self.last_selected_model = self.fallback_name
                if not self.last_fallback_reason:
                    self.last_fallback_reason = f"{self.primary_name}_missing"
                return out
            except Exception as exc:
                detail = f"{self.fallback_name}_inference_error:{type(exc).__name__}"
                if self.last_fallback_reason:
                    detail = f"{self.last_fallback_reason};{detail}"
                raise RuntimeError(f"{self.family} routing failed: {detail}") from exc

        if primary_error:
            raise RuntimeError(f"{self.family} routing failed: {primary_error}")
        raise RuntimeError(f"{self.family} routing failed: no_available_model")

    def predict(self, X: pd.DataFrame) -> pd.Series:
        p = self.predict_proba(X)
        return (p["p1"] >= 0.5).astype(int)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "selected_model": self.last_selected_model,
            "used_fallback": bool(self.last_selected_model and self.last_selected_model != self.primary_name),
            "fallback_reason": self.last_fallback_reason if self.last_fallback_reason else "none",
        }


def _safe_load(model_cls: Any, raw_path: str, project_root: Path) -> tuple[Any | None, str]:
    value = str(raw_path or "").strip()
    if not value:
        return None, "missing_path"
    try:
        s = get_settings()
        timeout_secs = max(0.0, float(getattr(s, "model_load_timeout_secs", 0.0) or 0.0))
        path = _resolve_path(value, project_root)
        if timeout_secs > 0.0 and hasattr(signal, "SIGALRM"):
            def _timeout_handler(_signum, _frame):
                raise TimeoutError("model_load_timeout")

            prev_handler = signal.getsignal(signal.SIGALRM)
            signal.signal(signal.SIGALRM, _timeout_handler)
            signal.setitimer(signal.ITIMER_REAL, timeout_secs)
            try:
                model = model_cls.load(path)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, prev_handler)
            return model, ""
        return model_cls.load(path), ""
    except Exception as exc:
        return None, f"load_error:{type(exc).__name__}"


def _load_sequence_shadow_bundle(
    *,
    pair: str,
    timeframes: dict[str, str],
    project_root: Path,
) -> tuple[dict[str, Any], str, dict[str, Any], list[str]]:
    s = get_settings()
    if not bool(getattr(s, "sequence_shadow_enabled", False)):
        return {}, "", {}, []
    if not bool(getattr(s, "mlflow_enabled", False)):
        return {}, "", {}, ["mlflow_disabled"]
    try:
        bundle = resolve_bundle_manifest_by_alias(pair=pair, alias="shadow", timeframes=timeframes)
    except Exception as exc:
        return {}, "", {}, [f"shadow_alias_unavailable:{type(exc).__name__}"]

    models: dict[str, Any] = {}
    refs: dict[str, Any] = {}
    errors: list[str] = []
    component_loaders = {
        "swing_patchtst": ("fxstack.models.patchtst", "SwingPatchTST"),
        "intraday_patchtst": ("fxstack.models.patchtst", "IntradayPatchTST"),
    }
    for component_key, import_spec in component_loaders.items():
        raw_ref = dict((bundle.components.get(component_key) or {}).to_dict() if hasattr(bundle.components.get(component_key), "to_dict") else dict(bundle.components.get(component_key) or {}))
        if not raw_ref:
            continue
        artifact_ref = str(raw_ref.get("path") or raw_ref.get("model_uri") or "").strip()
        if not artifact_ref:
            continue
        try:
            module = __import__(str(import_spec[0]), fromlist=[str(import_spec[1])])
            model_cls = getattr(module, str(import_spec[1]))
        except Exception as exc:
            errors.append(f"{component_key}_import_error:{type(exc).__name__}")
            continue
        model, load_error = _safe_load(model_cls, artifact_ref, project_root)
        if model is None:
            errors.append(f"{component_key}_{load_error or 'load_error'}")
            continue
        models[str(component_key)] = model
        refs[str(component_key)] = raw_ref
    return models, str(bundle.bundle_run_id), refs, errors


def _sequence_shadow_metrics(
    *,
    loaded: LoadedModelSet,
    swing_row: pd.DataFrame,
    intraday_row: pd.DataFrame,
    signal: Any,
) -> dict[str, Any]:
    probs: dict[str, float] = {}
    disagreement: dict[str, float] = {}
    report_refs: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    if loaded.swing_shadow_model is not None:
        try:
            probs["swing_patchtst"] = float(loaded.swing_shadow_model.predict_proba(swing_row)["p1"].iloc[0])
            disagreement["swing_patchtst_vs_live"] = abs(float(signal.swing_prob) - float(probs["swing_patchtst"]))
        except Exception as exc:
            errors.append(f"swing_patchtst_inference_error:{type(exc).__name__}")
    if loaded.intraday_shadow_model is not None:
        try:
            probs["intraday_patchtst"] = float(loaded.intraday_shadow_model.predict_proba(intraday_row)["p1"].iloc[0])
            disagreement["intraday_patchtst_vs_live"] = abs(float(signal.entry_prob) - float(probs["intraday_patchtst"]))
        except Exception as exc:
            errors.append(f"intraday_patchtst_inference_error:{type(exc).__name__}")
    for component_key in ["swing_patchtst", "intraday_patchtst"]:
        raw = dict(loaded.shadow_component_refs.get(component_key) or {})
        evidence = dict(raw.get("evidence_refs") or {})
        if evidence:
            report_refs[str(component_key)] = {
                key: str(evidence.get(key) or "")
                for key in [
                    "training_report",
                    "promotion_decision",
                    "model_manifest",
                    "sequence_dataset_manifest",
                    "portfolio_report",
                    "challenger_head_to_head",
                    "portfolio_disagreement",
                ]
                if str(evidence.get(key) or "").strip()
            }
    return {
        "enabled": bool(get_settings().sequence_shadow_enabled),
        "available": bool(probs),
        "bundle_run_id": str(loaded.shadow_bundle_run_id or ""),
        "component_refs": {key: dict(value or {}) for key, value in dict(loaded.shadow_component_refs or {}).items()},
        "probs": probs,
        "disagreement": disagreement,
        "report_refs": report_refs,
        "errors": errors,
    }


# AGENT FLOW: Manifest/model loading resolves active artifacts and seeds the scorer/lifecycle stack used by both startup inference and the live loop.
def _load_model_sets(*, pairs: list[str], require_all: bool, project_root: Path) -> tuple[dict[str, LoadedModelSet], dict[str, Any]]:
    from fxstack.models.exit_policy_xgb import ExitPolicyXGB
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.reversal_failure_xgb import ReversalFailureXGB
    from fxstack.models.reversal_opportunity_xgb import ReversalOpportunityXGB
    from fxstack.models.swing_xgb import SwingXGB
    from fxstack.runtime.service import RuntimeService

    s = get_settings()
    regime_timeframe = str(s.regime_timeframe).upper()
    swing_timeframe = str(s.swing_timeframe).upper()
    intraday_timeframe = str(s.intraday_timeframe).upper()
    svc = RuntimeService(
        database_url=s.database_url,
        default_session_id=s.default_session_id,
        command_ttl_secs=s.command_ttl_secs,
        requeue_age_secs=s.startup_requeue_age_secs,
        db_connect_retries=s.db_connect_retries,
    )
    active = svc.get_active_model_sets(enabled_only=True)
    missing = [p for p in pairs if p not in active]
    if require_all and missing:
        raise RuntimeError(f"missing active model sets for pairs: {','.join(missing)}")

    out: dict[str, LoadedModelSet] = {}
    load_diag: dict[str, Any] = {
        "model_load_timeouts": 0,
        "model_load_errors": 0,
        "pairs": {},
        "loaded_pairs": [],
        "failed_pairs": [],
        "degraded_pairs": [],
        "failure_component": "",
        "failure_pair": "",
        "failure_reason": "",
        "failure_message": "",
    }

    def _track_load_error(err: str) -> None:
        if not err:
            return
        if "TimeoutError" in str(err):
            load_diag["model_load_timeouts"] = int(load_diag.get("model_load_timeouts", 0)) + 1
        else:
            load_diag["model_load_errors"] = int(load_diag.get("model_load_errors", 0)) + 1
    
    def _raise_model_load_failure(*, message: str, pair: str, component: str, reason: str) -> None:
        load_diag["failure_component"] = str(component or "")
        load_diag["failure_pair"] = str(pair or "")
        load_diag["failure_reason"] = str(reason or message or "")
        load_diag["failure_message"] = str(message or "")
        exc = RuntimeError(message)
        setattr(exc, "model_load_diag", load_diag)
        raise exc

    def _component_diag(*, path: str, model: Any | None, err: str, requested: bool, required: bool) -> dict[str, Any]:
        configured = bool(str(path or "").strip())
        if not requested:
            status = "not_requested"
        elif model is not None:
            status = "loaded"
        elif configured:
            status = "failed" if err else "missing"
        else:
            status = "not_configured"
        return {
            "path": str(path or ""),
            "requested": bool(requested),
            "required": bool(required),
            "status": status,
            "error": str(err or ""),
            "loaded": bool(model is not None),
        }
    for pair in pairs:
        row = dict(active.get(pair, {}) or {})
        if not row:
            continue
        art = dict(row.get("artifacts_json") or {})
        meta_json = dict(row.get("metadata_json") or {})
        rollout_policy = _resolve_main_runtime_rollout_policy(pair=pair, metadata=meta_json)
        policy_json = dict(meta_json.get("policies") or {})
        pair_diag: dict[str, Any] = {
            "pair": str(pair).upper(),
            "model_set_id": str(row.get("model_set_id") or "unknown"),
            "registry_path": str(row.get("registry_path") or ""),
            "swing_policy": "",
            "intraday_policy": "",
            "status": "loaded",
            "failure_component": "",
            "failure_reason": "",
            "components": {},
        }
        pair_status = "loaded"
        pair_failure_component = ""
        pair_failure_reason = ""
        component_feature_services = {
            str(key): dict(value or {})
            for key, value in dict(meta_json.get("component_feature_services") or {}).items()
        }
        for key, value in art.items():
            if str(key).strip() and isinstance(value, dict) and key not in component_feature_services:
                component_feature_services[str(key)] = dict(value or {})

        configured_swing_policy = str(s.swing_model_policy or "").strip()
        configured_intraday_policy = str(s.intraday_model_policy or "").strip()
        manifest_swing_policy = str(policy_json.get("swing") or "").strip()
        manifest_intraday_policy = str(policy_json.get("intraday") or "").strip()

        # Allow the active ops profile to force lighter model policies, even if
        # the activated artifact metadata prefers deep primary models.
        swing_policy = configured_swing_policy or manifest_swing_policy
        intraday_policy = configured_intraday_policy or manifest_intraday_policy
        if str(configured_swing_policy).lower() != "xgb_only" and manifest_swing_policy:
            swing_policy = manifest_swing_policy
        if str(configured_intraday_policy).lower() != "xgb_only" and manifest_intraday_policy:
            intraday_policy = manifest_intraday_policy
        pair_diag["swing_policy"] = str(swing_policy)
        pair_diag["intraday_policy"] = str(intraday_policy)

        def _capture_component(
            component_name: str,
            *,
            path: str,
            model: Any | None,
            err: str,
            requested: bool,
            required: bool,
        ) -> None:
            nonlocal pair_status, pair_failure_component, pair_failure_reason
            component_diag = _component_diag(path=path, model=model, err=err, requested=requested, required=required)
            pair_diag["components"][component_name] = component_diag
            if component_diag["status"] in {"loaded", "not_requested", "not_configured"}:
                return
            _track_load_error(err or component_diag["error"])
            if requested:
                if required:
                    pair_status = "failed"
                elif pair_status == "loaded":
                    pair_status = "degraded"
                if not pair_failure_component:
                    pair_failure_component = component_name
                    pair_failure_reason = str(err or component_diag["error"] or f"{component_name}_{component_diag['status']}")

        regime_path = _artifact_value(art, "regime")
        meta_path = _artifact_value(art, "meta")
        exit_path = _artifact_value(art, "exit_policy", "exit", "exit_model")
        belief_path = _artifact_value(art, "directional_belief")
        reversal_failure_path = _artifact_value(art, "reversal_failure", "reversal_failure_xgb")
        reversal_opportunity_path = _artifact_value(art, "reversal_opportunity", "reversal_opportunity_xgb")
        rl_checkpoint_path = (
            _artifact_value(art, "portfolio_rl", "rl_policy", "rl_checkpoint", "offline_rl")
            or _artifact_path(meta_json.get("rl_checkpoint"))
            or str(meta_json.get("rl_checkpoint_path") or "")
        )
        regime, regime_err = _safe_load(RegimeHMM, regime_path, project_root)
        meta, meta_err = _safe_load(MetaFilterXGB, meta_path, project_root)
        _capture_component("regime", path=regime_path, model=regime, err=regime_err, requested=True, required=True)
        _capture_component("meta", path=meta_path, model=meta, err=meta_err, requested=True, required=True)
        if regime is None or meta is None:
            pair_status = "failed"
            if not pair_failure_component:
                pair_failure_component = "regime" if regime is None else "meta"
                pair_failure_reason = f"regime={regime_err or 'ok'},meta={meta_err or 'ok'}"
            pair_diag["status"] = pair_status
            pair_diag["failure_component"] = pair_failure_component
            pair_diag["failure_reason"] = pair_failure_reason
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            if require_all:
                _raise_model_load_failure(
                    message=f"failed loading required models for {pair}: regime={regime_err or 'ok'},meta={meta_err or 'ok'}",
                    pair=pair,
                    component=pair_failure_component,
                    reason=pair_failure_reason,
                )
            continue

        swing_tf = None
        swing_xgb = None
        intraday_tcn = None
        intraday_xgb = None

        if str(swing_policy).lower() == "transformer_primary_xgb_fallback":
            from fxstack.models.swing_transformer import SwingTransformer

            swing_tf, swing_tf_err = _safe_load(SwingTransformer, _artifact_value(art, "swing_transformer"), project_root)
            swing_xgb, swing_err = _safe_load(SwingXGB, _artifact_value(art, "swing_xgb", "swing"), project_root)
            _capture_component(
                "swing_transformer",
                path=_artifact_value(art, "swing_transformer"),
                model=swing_tf,
                err=swing_tf_err,
                requested=True,
                required=False,
            )
            _capture_component(
                "swing_xgb",
                path=_artifact_value(art, "swing_xgb", "swing"),
                model=swing_xgb,
                err=swing_err,
                requested=True,
                required=False,
            )
        else:
            swing_xgb, swing_err = _safe_load(SwingXGB, _artifact_value(art, "swing_xgb", "swing"), project_root)
            _capture_component(
                "swing_transformer",
                path=_artifact_value(art, "swing_transformer"),
                model=None,
                err="",
                requested=False,
                required=False,
            )
            _capture_component(
                "swing_xgb",
                path=_artifact_value(art, "swing_xgb", "swing"),
                model=swing_xgb,
                err=swing_err,
                requested=True,
                required=False,
            )

        if str(intraday_policy).lower() == "tcn_primary_xgb_fallback":
            from fxstack.models.intraday_tcn import IntradayTCN

            intraday_tcn, intraday_tcn_err = _safe_load(IntradayTCN, _artifact_value(art, "intraday_tcn"), project_root)
            intraday_xgb, intraday_xgb_err = _safe_load(IntradayXGB, _artifact_value(art, "intraday_xgb", "intraday"), project_root)
            _capture_component(
                "intraday_tcn",
                path=_artifact_value(art, "intraday_tcn"),
                model=intraday_tcn,
                err=intraday_tcn_err,
                requested=True,
                required=False,
            )
            _capture_component(
                "intraday_xgb",
                path=_artifact_value(art, "intraday_xgb", "intraday"),
                model=intraday_xgb,
                err=intraday_xgb_err,
                requested=True,
                required=False,
            )
        else:
            intraday_xgb, intraday_xgb_err = _safe_load(IntradayXGB, _artifact_value(art, "intraday_xgb", "intraday"), project_root)
            _capture_component(
                "intraday_tcn",
                path=_artifact_value(art, "intraday_tcn"),
                model=None,
                err="",
                requested=False,
                required=False,
            )
            _capture_component(
                "intraday_xgb",
                path=_artifact_value(art, "intraday_xgb", "intraday"),
                model=intraday_xgb,
                err=intraday_xgb_err,
                requested=True,
                required=False,
            )

        exit_model, exit_err = _safe_load(ExitPolicyXGB, exit_path, project_root)
        reversal_failure_model, reversal_failure_err = _safe_load(ReversalFailureXGB, reversal_failure_path, project_root)
        reversal_opportunity_model, reversal_opportunity_err = _safe_load(
            ReversalOpportunityXGB,
            reversal_opportunity_path,
            project_root,
        )
        _capture_component("exit_policy", path=exit_path, model=exit_model, err=exit_err, requested=bool(str(exit_path).strip()), required=bool(str(exit_path).strip()))
        _capture_component(
            "reversal_failure",
            path=reversal_failure_path,
            model=reversal_failure_model,
            err=reversal_failure_err,
            requested=bool(str(reversal_failure_path).strip()),
            required=bool(str(reversal_failure_path).strip()),
        )
        _capture_component(
            "reversal_opportunity",
            path=reversal_opportunity_path,
            model=reversal_opportunity_model,
            err=reversal_opportunity_err,
            requested=bool(str(reversal_opportunity_path).strip()),
            required=bool(str(reversal_opportunity_path).strip()),
        )

        if require_all and str(exit_path).strip() and exit_model is None:
            pair_status = "failed"
            pair_failure_component = pair_failure_component or "exit_policy"
            pair_failure_reason = str(exit_err or "unknown")
            pair_diag["status"] = pair_status
            pair_diag["failure_component"] = pair_failure_component
            pair_diag["failure_reason"] = pair_failure_reason
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            _raise_model_load_failure(
                message=f"failed loading exit model for {pair}: {exit_err or 'unknown'}",
                pair=pair,
                component=pair_failure_component,
                reason=pair_failure_reason,
            )
        if require_all and str(reversal_failure_path).strip() and reversal_failure_model is None:
            pair_status = "failed"
            pair_failure_component = pair_failure_component or "reversal_failure"
            pair_failure_reason = str(reversal_failure_err or "unknown")
            pair_diag["status"] = pair_status
            pair_diag["failure_component"] = pair_failure_component
            pair_diag["failure_reason"] = pair_failure_reason
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            _raise_model_load_failure(
                message=f"failed loading reversal failure model for {pair}: {reversal_failure_err or 'unknown'}",
                pair=pair,
                component=pair_failure_component,
                reason=pair_failure_reason,
            )
        if require_all and str(reversal_opportunity_path).strip() and reversal_opportunity_model is None:
            pair_status = "failed"
            pair_failure_component = pair_failure_component or "reversal_opportunity"
            pair_failure_reason = str(reversal_opportunity_err or "unknown")
            pair_diag["status"] = pair_status
            pair_diag["failure_component"] = pair_failure_component
            pair_diag["failure_reason"] = pair_failure_reason
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            _raise_model_load_failure(
                message=f"failed loading reversal opportunity model for {pair}: {reversal_opportunity_err or 'unknown'}",
                pair=pair,
                component=pair_failure_component,
                reason=pair_failure_reason,
            )

        belief_model = None
        has_directional_belief = False
        if bool(getattr(s, "belief_shadow_enabled", False)) and str(belief_path).strip():
            try:
                belief_model = load_directional_belief_model_set(_resolve_path(belief_path, project_root))
                has_directional_belief = True
            except Exception as exc:
                belief_err = f"load_error:{type(exc).__name__}"
                _capture_component(
                    "directional_belief",
                    path=belief_path,
                    model=None,
                    err=belief_err,
                    requested=True,
                    required=bool(getattr(s, "belief_runtime_required", False)),
                )
                if bool(getattr(s, "belief_runtime_required", False)):
                    pair_status = "failed"
                    pair_failure_component = pair_failure_component or "directional_belief"
                    pair_failure_reason = f"{type(exc).__name__}:{exc}" if str(exc) else str(type(exc).__name__)
                    pair_diag["status"] = pair_status
                    pair_diag["failure_component"] = pair_failure_component
                    pair_diag["failure_reason"] = pair_failure_reason
                    load_diag["pairs"][pair] = pair_diag
                    load_diag["failed_pairs"].append(pair)
                    _raise_model_load_failure(
                        message=f"failed loading directional belief model for {pair}: {type(exc).__name__}:{exc}",
                        pair=pair,
                        component=pair_failure_component,
                        reason=pair_failure_reason,
                    )

        exit_meta = _load_artifact_meta(exit_path, project_root) if str(exit_path).strip() else {}
        if exit_model is not None and not getattr(exit_model, "feature_columns", None):
            setattr(exit_model, "feature_columns", list(exit_meta.get("feature_columns") or []))
        exit_action_labels = _exit_action_labels(exit_meta, getattr(exit_model, "classes_", None))
        has_exit_model = bool(exit_model is not None)
        has_reversal_models = bool(reversal_failure_model is not None and reversal_opportunity_model is not None)
        lifecycle_activation_mode = "model_driven" if (has_exit_model or has_reversal_models) else "runtime_soft"

        swing_router = _PolicyModelRouter(
            policy=swing_policy,
            family="swing",
            primary_name="swing_transformer"
            if str(swing_policy).lower() == "transformer_primary_xgb_fallback"
            else "swing_xgb",
            primary_model=swing_tf if str(swing_policy).lower() == "transformer_primary_xgb_fallback" else swing_xgb,
            fallback_name="swing_xgb",
            fallback_model=swing_xgb if str(swing_policy).lower() == "transformer_primary_xgb_fallback" else None,
        )
        intraday_router = _PolicyModelRouter(
            policy=intraday_policy,
            family="intraday",
            primary_name="intraday_tcn"
            if str(intraday_policy).lower() == "tcn_primary_xgb_fallback"
            else "intraday_xgb",
            primary_model=intraday_tcn if str(intraday_policy).lower() == "tcn_primary_xgb_fallback" else intraday_xgb,
            fallback_name="intraday_xgb",
            fallback_model=intraday_xgb if str(intraday_policy).lower() == "tcn_primary_xgb_fallback" else None,
        )
        shadow_models, shadow_bundle_run_id, shadow_component_refs, shadow_errors = _load_sequence_shadow_bundle(
            pair=pair,
            timeframes={"regime": regime_timeframe, "swing": swing_timeframe, "intraday": intraday_timeframe},
            project_root=project_root,
        )
        for err in shadow_errors:
            _track_load_error(str(err))
        pair_diag["components"]["sequence_shadow"] = {
            "path": str(shadow_bundle_run_id or ""),
            "requested": bool(getattr(s, "sequence_shadow_enabled", False)),
            "required": False,
            "status": "loaded" if shadow_models else ("failed" if shadow_errors else "not_requested"),
            "error": ";".join([str(err) for err in shadow_errors if str(err).strip()]),
            "loaded": bool(shadow_models),
        }
        if shadow_errors and pair_status == "loaded":
            pair_status = "degraded"
            if not pair_failure_component:
                pair_failure_component = "sequence_shadow"
                pair_failure_reason = ";".join([str(err) for err in shadow_errors if str(err).strip()])

        # Validate that at least one model is available per family.
        if swing_router.primary_model is None and swing_router.fallback_model is None:
            if require_all:
                pair_status = "failed"
                pair_failure_component = pair_failure_component or "swing"
                pair_failure_reason = f"failed loading swing models for {pair} under policy={swing_policy}"
                pair_diag["status"] = pair_status
                pair_diag["failure_component"] = pair_failure_component
                pair_diag["failure_reason"] = pair_failure_reason
                load_diag["pairs"][pair] = pair_diag
                load_diag["failed_pairs"].append(pair)
                _raise_model_load_failure(
                    message=f"failed loading swing models for {pair} under policy={swing_policy}",
                    pair=pair,
                    component=pair_failure_component,
                    reason=pair_failure_reason,
                )
            pair_diag["status"] = pair_status
            pair_diag["failure_component"] = str(pair_failure_component or "")
            pair_diag["failure_reason"] = str(pair_failure_reason or f"failed loading swing models for {pair} under policy={swing_policy}")
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            continue
        if intraday_router.primary_model is None and intraday_router.fallback_model is None:
            if require_all:
                pair_status = "failed"
                pair_failure_component = pair_failure_component or "intraday"
                pair_failure_reason = f"failed loading intraday models for {pair} under policy={intraday_policy}"
                pair_diag["status"] = pair_status
                pair_diag["failure_component"] = pair_failure_component
                pair_diag["failure_reason"] = pair_failure_reason
                load_diag["pairs"][pair] = pair_diag
                load_diag["failed_pairs"].append(pair)
                _raise_model_load_failure(
                    message=f"failed loading intraday models for {pair} under policy={intraday_policy}",
                    pair=pair,
                    component=pair_failure_component,
                    reason=pair_failure_reason,
                )
            pair_diag["status"] = pair_status
            pair_diag["failure_component"] = str(pair_failure_component or "")
            pair_diag["failure_reason"] = str(pair_failure_reason or f"failed loading intraday models for {pair} under policy={intraday_policy}")
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            continue

        pair_diag["status"] = pair_status if pair_status != "loaded" else "loaded"
        pair_diag["failure_component"] = str(pair_failure_component or "")
        pair_diag["failure_reason"] = str(pair_failure_reason or "")
        load_diag["pairs"][pair] = pair_diag
        if pair_status == "degraded":
            load_diag["degraded_pairs"].append(pair)
        load_diag["loaded_pairs"].append(pair)
        out[pair] = LoadedModelSet(
            pair=pair,
            model_set_id=str(row.get("model_set_id") or "unknown"),
            registry_path=str(row.get("registry_path") or ""),
            scorer=LiveScorer(regime_model=regime, swing_model=swing_router, intraday_model=intraday_router, meta_model=meta),
            swing_router=swing_router,
            intraday_router=intraday_router,
            exit_model=exit_model,
            reversal_failure_model=reversal_failure_model,
            reversal_opportunity_model=reversal_opportunity_model,
            belief_model=belief_model,
            exit_action_labels=exit_action_labels,
            lifecycle_activation_mode=lifecycle_activation_mode,
            has_exit_model=has_exit_model,
            has_reversal_models=has_reversal_models,
            has_directional_belief=has_directional_belief,
            swing_shadow_model=shadow_models.get("swing_patchtst"),
            intraday_shadow_model=shadow_models.get("intraday_patchtst"),
            shadow_bundle_run_id=str(shadow_bundle_run_id),
            shadow_component_refs={key: dict(value or {}) for key, value in dict(shadow_component_refs).items()},
            component_feature_services=component_feature_services,
            rollout_policy=dict(rollout_policy),
            rl_checkpoint_path=str(rl_checkpoint_path or ""),
        )
    return out, load_diag


def _seed_active_model_sets_from_manifest(*, svc: Any, project_root: Path) -> dict[str, Any]:
    s = get_settings()
    existing = svc.get_active_model_sets(enabled_only=True)
    configured_pairs = {str(p).upper() for p in list(s.pairs)}

    manifest_candidate = _resolve_optional_path(str(s.model_activation_manifest), project_root)
    if manifest_candidate is None:
        return {
            "seeded": False,
            "reason": "manifest_missing",
            "path": str(s.model_activation_manifest),
            "missing_pairs": sorted(list(configured_pairs)) if configured_pairs else [],
        }

    try:
        payload = json.loads(manifest_candidate.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"seeded": False, "reason": f"manifest_parse_error:{type(exc).__name__}", "path": str(manifest_candidate)}

    active = dict((payload or {}).get("active_model_sets") or {})
    if not active:
        return {
            "seeded": False,
            "reason": "manifest_empty",
            "path": str(manifest_candidate),
            "missing_pairs": sorted(list(configured_pairs)) if configured_pairs else [],
        }

    seeded_pairs: list[str] = []
    target_pairs = configured_pairs if configured_pairs else {str(p).upper() for p in active.keys()}
    for pair, row in active.items():
        pair_up = str(pair).upper()
        if target_pairs and pair_up not in target_pairs:
            continue
        item = dict(row or {})
        enabled = bool(item.get("enabled", True))
        if not enabled:
            continue
        artifacts = dict(item.get("artifacts") or {})
        policies = dict(item.get("policies") or {})
        metadata = dict(item.get("metadata") or {})
        metadata["policies"] = policies
        metadata["seed_source"] = "activation_manifest"
        try:
            svc.upsert_active_model_set(
                pair=pair_up,
                model_set_id=str(item.get("model_set_id") or f"{str(pair).lower()}-manifest"),
                registry_path=str(item.get("registry_path") or ""),
                artifacts=artifacts,
                metadata=metadata,
                enabled=True,
            )
            seeded_pairs.append(pair_up)
        except Exception:
            continue

    post = svc.get_active_model_sets(enabled_only=True)
    post_pairs = {str(p).upper() for p in list(post.keys())}
    post_missing_pairs = sorted(list(configured_pairs - post_pairs)) if configured_pairs else []
    return {
        "seeded": bool(seeded_pairs),
        "reason": "seeded_partial" if (seeded_pairs and post_missing_pairs) else ("seeded" if seeded_pairs else "seed_failed"),
        "path": str(manifest_candidate),
        "pairs": sorted(seeded_pairs),
        "missing_pairs": post_missing_pairs,
    }


def _load_manifest_active_rows(*, project_root: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    s = get_settings()
    manifest_candidate = _resolve_optional_path(str(s.model_activation_manifest), project_root)
    if manifest_candidate is None:
        return {}, {"present": False, "path": str(s.model_activation_manifest)}
    try:
        payload = json.loads(manifest_candidate.read_text(encoding="utf-8"))
    except Exception as exc:
        return {}, {"present": True, "path": str(manifest_candidate), "error": f"manifest_parse_error:{type(exc).__name__}"}
    active = dict((payload or {}).get("active_model_sets") or {})
    out: dict[str, dict[str, Any]] = {}
    for pair, row in active.items():
        pair_up = str(pair).upper().strip()
        if not pair_up:
            continue
        item = dict(row or {})
        if not bool(item.get("enabled", True)):
            continue
        out[pair_up] = item
    return out, {"present": True, "path": str(manifest_candidate)}


def _normalized_registry_path(raw: str, *, project_root: Path) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    resolved = _resolve_optional_path(txt, project_root)
    if resolved is not None:
        return str(resolved)
    return txt.replace("\\", "/")


def _common_registry_root(paths: list[str]) -> str:
    roots = {str(Path(p).parent) for p in paths if str(p).strip()}
    if not roots:
        return ""
    if len(roots) == 1:
        return next(iter(roots))
    return "mixed"


def _activation_consistency(
    *,
    svc: Any,
    project_root: Path,
    configured_pairs: list[str],
    loaded_model_sets: dict[str, LoadedModelSet],
) -> dict[str, Any]:
    manifest_rows, manifest_meta = _load_manifest_active_rows(project_root=project_root)
    db_rows = svc.get_active_model_sets(enabled_only=True)
    configured = {str(pair).upper().strip() for pair in list(configured_pairs)}
    manifest_pairs = {pair for pair in manifest_rows.keys() if pair in configured}
    db_pairs = {str(pair).upper().strip() for pair in db_rows.keys() if str(pair).upper().strip() in configured}
    loaded_pairs = {str(pair).upper().strip() for pair in loaded_model_sets.keys() if str(pair).upper().strip() in configured}

    manifest_db_mismatch: list[str] = []
    runtime_db_mismatch: list[str] = []
    for pair in sorted(configured):
        manifest_row = dict(manifest_rows.get(pair) or {})
        db_row = dict(db_rows.get(pair) or {})
        manifest_path = _normalized_registry_path(str(manifest_row.get("registry_path") or ""), project_root=project_root)
        db_path = _normalized_registry_path(str(db_row.get("registry_path") or ""), project_root=project_root)
        if bool(manifest_row) != bool(db_row):
            manifest_db_mismatch.append(pair)
        elif manifest_row and db_row and manifest_path != db_path:
            manifest_db_mismatch.append(pair)

        loaded_row = loaded_model_sets.get(pair)
        if loaded_row is None:
            runtime_db_mismatch.append(pair)
            continue
        loaded_path = _normalized_registry_path(str(loaded_row.registry_path or ""), project_root=project_root)
        if not db_row or loaded_path != db_path:
            runtime_db_mismatch.append(pair)

    runtime_registry_paths = [
        _normalized_registry_path(str(item.registry_path or ""), project_root=project_root)
        for item in loaded_model_sets.values()
    ]
    return {
        "manifest": dict(manifest_meta),
        "active_manifest_matches_db": len(manifest_db_mismatch) == 0,
        "runtime_loaded_matches_db": len(runtime_db_mismatch) == 0,
        "activation_mismatch_pairs": sorted(list(set(manifest_db_mismatch) | set(runtime_db_mismatch))),
        "manifest_db_mismatch_pairs": sorted(manifest_db_mismatch),
        "runtime_db_mismatch_pairs": sorted(runtime_db_mismatch),
        "configured_pairs": sorted(list(configured)),
        "manifest_active_pairs": sorted(list(manifest_pairs)),
        "db_active_pairs": sorted(list(db_pairs)),
        "runtime_loaded_pairs": sorted(list(loaded_pairs)),
        "active_pair_count": int(len(configured)),
        "active_registry_root": _common_registry_root(runtime_registry_paths),
    }


# AGENT FLOW: Startup inference is the dry-run gate; pairs that fail here are disabled before runtime starts submitting live actions.
def _startup_inference_dry_run(
    *,
    store: ParquetStore,
    raw_store: ParquetStore,
    pairs: list[str],
    model_sets: dict[str, LoadedModelSet],
    feature_timeframes: list[str],
    regime_timeframe: str,
    swing_timeframe: str,
    intraday_timeframe: str,
    progress_cb: Callable[[str, int, int], None] | None = None,
) -> tuple[dict[str, LoadedModelSet], dict[str, dict[str, Any]]]:
    ready_model_sets: dict[str, LoadedModelSet] = {}
    startup_results: dict[str, dict[str, Any]] = {}
    intraday_cache: dict[tuple[str, str, str], pd.DataFrame] = {}

    total_pairs = int(len(pairs))
    for index, pair in enumerate(pairs, start=1):
        if progress_cb is not None:
            progress_cb(str(pair), int(index), int(total_pairs))
        loaded = model_sets.get(pair)
        if loaded is None:
            startup_results[pair] = {
                "ok": False,
                "reason": "model_not_loaded",
                "model_set_id": "",
                "registry_path": "",
                "pair_readiness": {
                    "status": "blocked",
                    "blockers": ["model_not_loaded"],
                    "required_column_gaps": {},
                    "feature_serving_source": "",
                },
            }
            continue

        pair_rows: dict[str, pd.DataFrame] = {}
        missing_frames: list[str] = []
        required_column_gaps: dict[str, list[str]] = {}
        for timeframe in feature_timeframes:
            row = _latest_feature_row(
                store=store,
                raw_store=raw_store,
                pair=pair,
                timeframe=timeframe,
                all_pairs=pairs,
                feature_service_name=_loaded_feature_service_name(
                    loaded,
                    pair=pair,
                    timeframe=timeframe,
                    regime_timeframe=regime_timeframe,
                    swing_timeframe=swing_timeframe,
                    intraday_timeframe=intraday_timeframe,
                ),
            )
            if row.empty:
                missing_frames.append(timeframe)
            else:
                pair_rows[timeframe] = row
        if missing_frames:
            startup_results[pair] = {
                "ok": False,
                "reason": f"missing_features:{','.join(missing_frames)}",
                "model_set_id": str(loaded.model_set_id),
                "registry_path": str(loaded.registry_path),
                "pair_readiness": {
                    "status": "blocked",
                    "blockers": [f"missing_features:{','.join(missing_frames)}"],
                    "required_column_gaps": {},
                    "feature_serving_source": "",
                },
            }
            continue

        pair_rows = _prepare_pair_rows_for_scoring(
            raw_store=raw_store,
            pair=pair,
            loaded=loaded,
            pair_rows=pair_rows,
            regime_timeframe=regime_timeframe,
            swing_timeframe=swing_timeframe,
            intraday_timeframe=intraday_timeframe,
            all_pairs=pairs,
            intraday_cache=intraday_cache,
        )
        required_column_gaps = _startup_required_column_gaps(
            loaded=loaded,
            pair_rows=pair_rows,
            regime_timeframe=regime_timeframe,
            swing_timeframe=swing_timeframe,
            intraday_timeframe=intraday_timeframe,
        )
        if required_column_gaps:
            gap_reason = ",".join(
                f"{timeframe}:{'/'.join(missing[:5])}"
                for timeframe, missing in sorted(required_column_gaps.items())
            )
            startup_results[pair] = {
                "ok": False,
                "reason": f"missing_required_columns:{gap_reason}",
                "missing_required_columns": required_column_gaps,
                "model_set_id": str(loaded.model_set_id),
                "registry_path": str(loaded.registry_path),
                "pair_readiness": {
                    "status": "blocked",
                    "blockers": [f"missing_required_columns:{gap_reason}"],
                    "required_column_gaps": dict(required_column_gaps),
                    "feature_serving_source": "",
                },
            }
            continue

        try:
            signal = loaded.scorer.score(
                regime_row=pair_rows[regime_timeframe],
                swing_row=pair_rows[swing_timeframe],
                intraday_row=pair_rows[intraday_timeframe],
                meta_row=pair_rows[intraday_timeframe],
                spread_bps=0.0,
                expected_edge_bps=0.0,
                spread_unit_source="startup_dry_run",
            )
            lifecycle_row = _build_lifecycle_row(
                row=pair_rows[intraday_timeframe],
                positions=[],
                total_position_count=0,
                loop_ts=time.time(),
                timeframe=str(intraday_timeframe),
            )
            exit_selected = "hold"
            exit_score = 0.0
            reversal_failure_prob = 0.0
            reversal_opportunity_prob = 0.0
            if loaded.exit_model is not None:
                exit_diag = _score_exit_policy_model(
                    loaded.exit_model,
                    lifecycle_row,
                    action_labels=loaded.exit_action_labels,
                )
                exit_selected = str(exit_diag.get("selected") or "hold")
                exit_score = float(exit_diag.get("score") or 0.0)
            if loaded.reversal_failure_model is not None:
                reversal_failure_prob = _score_binary_lifecycle_model(loaded.reversal_failure_model, lifecycle_row)
            if loaded.reversal_opportunity_model is not None:
                reversal_opportunity_prob = _score_binary_lifecycle_model(loaded.reversal_opportunity_model, lifecycle_row)
            startup_results[pair] = {
                "ok": True,
                "reason": "ok",
                "model_set_id": str(loaded.model_set_id),
                "registry_path": str(loaded.registry_path),
                "trade_prob": float(signal.trade_prob),
                "side": str(signal.side),
                "has_exit_model": bool(loaded.has_exit_model),
                "has_reversal_models": bool(loaded.has_reversal_models),
                "lifecycle_activation_mode": str(loaded.lifecycle_activation_mode),
                "exit_action_selected": str(exit_selected),
                "exit_action_score": float(exit_score),
                "reversal_failure_prob": float(reversal_failure_prob),
                "reversal_opportunity_prob": float(reversal_opportunity_prob),
            }
            ready_model_sets[pair] = loaded
        except Exception as exc:
            startup_results[pair] = {
                "ok": False,
                "reason": f"inference_error:{type(exc).__name__}",
                "error": str(exc),
                "model_set_id": str(loaded.model_set_id),
                "registry_path": str(loaded.registry_path),
                "has_exit_model": bool(loaded.has_exit_model),
                "has_reversal_models": bool(loaded.has_reversal_models),
            }
        pair_summary = startup_results[pair]
        pair_summary["pair_readiness"] = {
            "status": "ready" if bool(pair_summary.get("ok")) else "blocked",
            "blockers": [] if bool(pair_summary.get("ok")) else [str(pair_summary.get("reason") or "blocked")],
            "required_column_gaps": dict(pair_summary.get("missing_required_columns") or required_column_gaps or {}),
            "feature_serving_source": "",
        }

    return ready_model_sets, startup_results


def _latest_feature_row(
    *,
    store: ParquetStore,
    raw_store: ParquetStore,
    pair: str,
    timeframe: str,
    all_pairs: list[str] | None = None,
    feature_service_name: str | None = None,
) -> pd.DataFrame:
    provider = get_settings().normalized_data_provider
    row, telemetry = resolve_latest_feature_row(
        store=store,
        raw_store=raw_store,
        pair=pair,
        timeframe=timeframe,
        provider=provider,
        feature_service_name=feature_service_name,
        all_pairs=all_pairs,
    )
    _record_feature_serving_telemetry(pair, timeframe, telemetry)
    return row


def _merge_latest_row(base_row: pd.DataFrame, latest_row: pd.DataFrame) -> pd.DataFrame:
    if base_row.empty:
        return latest_row.copy()
    if latest_row.empty:
        return base_row.copy()
    merged = base_row.reset_index(drop=True).copy()
    src = latest_row.reset_index(drop=True).iloc[0]
    for col in latest_row.columns:
        merged.loc[0, col] = src.get(col)
    return merged


def _missing_required_row_columns(row: pd.DataFrame, required_columns: list[str] | None) -> list[str]:
    required = [str(col) for col in list(required_columns or []) if str(col).strip()]
    if row.empty:
        return required
    src = row.reset_index(drop=True).iloc[0]
    missing: list[str] = []
    for col in required:
        if col not in row.columns or pd.isna(src.get(col)):
            missing.append(col)
    return missing


def _enrich_row_from_raw_lifecycle(
    *,
    raw_store: ParquetStore,
    pair: str,
    timeframe: str,
    row: pd.DataFrame,
    required_columns: list[str] | None,
) -> pd.DataFrame:
    required = [str(col) for col in list(required_columns or []) if str(col).strip()]
    if row.empty or not required:
        return row
    missing = _missing_required_row_columns(row, required)
    if not missing:
        return row

    provider = get_settings().normalized_data_provider
    raw_df = raw_store.read_pair_timeframe(provider=provider, pair=pair, timeframe=timeframe)
    if raw_df.empty:
        return row

    enriched = add_fx_lifecycle_features(raw_df)
    if enriched.empty:
        return row
    latest = enriched.sort_values("ts").tail(1).copy()
    return _merge_latest_row(row, latest)


def _enrich_intraday_row_from_raw_contract(
    *,
    raw_store: ParquetStore,
    pair: str,
    timeframe: str,
    row: pd.DataFrame,
    required_columns: list[str] | None,
    all_pairs: list[str],
    cache: dict[tuple[str, str, str], pd.DataFrame] | None = None,
) -> pd.DataFrame:
    required = [str(col) for col in list(required_columns or []) if str(col).strip()]
    if row.empty or not required:
        return row
    missing = _missing_required_row_columns(row, required)
    if not missing:
        return row

    ts_key = str(row.iloc[0].get("ts", "") or "")
    cache_key = (str(pair).upper(), str(timeframe).upper(), ts_key)
    if cache is not None and cache_key in cache:
        return _merge_latest_row(row, cache[cache_key])

    provider = get_settings().normalized_data_provider
    enriched, _ = build_latest_multi_tf_row(
        pair=str(pair).upper(),
        raw_store_root=Path(raw_store.root),
        provider=provider,
        anchor_timeframe=str(timeframe).upper(),
        context_timeframes=["M15", "H1", "H4", "D"],
        all_pairs=list(all_pairs),
    )
    if enriched.empty:
        return row
    latest = enriched.sort_values("ts").tail(1).copy()
    if cache is not None:
        cache[cache_key] = latest.copy()
    return _merge_latest_row(row, latest)


def _startup_required_column_gaps(
    *,
    loaded: LoadedModelSet,
    pair_rows: dict[str, pd.DataFrame],
    regime_timeframe: str = "",
    swing_timeframe: str,
    intraday_timeframe: str,
) -> dict[str, list[str]]:
    gaps: dict[str, list[str]] = {}
    regime_model = getattr(loaded.scorer, "regime_model", None)
    regime_required = list(getattr(regime_model, "feature_columns", []) or [])
    if regime_timeframe and regime_timeframe in pair_rows and regime_required:
        missing = _missing_required_row_columns(pair_rows[regime_timeframe], regime_required)
        if missing:
            gaps[str(regime_timeframe).upper()] = missing
    swing_required = list(getattr(loaded.scorer.swing_model, "feature_columns", []) or [])
    if swing_timeframe in pair_rows and swing_required:
        missing = _missing_required_row_columns(pair_rows[swing_timeframe], swing_required)
        if missing:
            gaps[str(swing_timeframe).upper()] = missing
    intraday_required = _required_model_feature_columns(
        loaded.scorer.intraday_model,
        loaded.scorer.meta_model,
        loaded.exit_model,
        loaded.reversal_failure_model,
        loaded.reversal_opportunity_model,
    )
    if intraday_timeframe in pair_rows and intraday_required:
        missing = _missing_required_row_columns(pair_rows[intraday_timeframe], intraday_required)
        if missing:
            gaps[str(intraday_timeframe).upper()] = missing
    return gaps


def _prepare_pair_rows_for_scoring(
    *,
    raw_store: ParquetStore,
    pair: str,
    loaded: LoadedModelSet,
    pair_rows: dict[str, pd.DataFrame],
    regime_timeframe: str = "",
    swing_timeframe: str,
    intraday_timeframe: str,
    all_pairs: list[str],
    intraday_cache: dict[tuple[str, str, str], pd.DataFrame] | None = None,
) -> dict[str, pd.DataFrame]:
    out = dict(pair_rows)
    regime_model = getattr(loaded.scorer, "regime_model", None)
    regime_required = list(getattr(regime_model, "feature_columns", []) or [])
    if regime_timeframe and regime_timeframe in out:
        out[regime_timeframe] = _enrich_row_from_raw_lifecycle(
            raw_store=raw_store,
            pair=pair,
            timeframe=regime_timeframe,
            row=out[regime_timeframe],
            required_columns=regime_required,
        )
    swing_required = list(getattr(loaded.scorer.swing_model, "feature_columns", []) or [])
    if swing_timeframe in out:
        out[swing_timeframe] = _enrich_row_from_raw_lifecycle(
            raw_store=raw_store,
            pair=pair,
            timeframe=swing_timeframe,
            row=out[swing_timeframe],
            required_columns=swing_required,
        )
    intraday_required = _required_model_feature_columns(
        loaded.scorer.intraday_model,
        loaded.scorer.meta_model,
        loaded.exit_model,
        loaded.reversal_failure_model,
        loaded.reversal_opportunity_model,
    )
    if intraday_timeframe in out:
        out[intraday_timeframe] = _enrich_intraday_row_from_raw_contract(
            raw_store=raw_store,
            pair=pair,
            timeframe=intraday_timeframe,
            row=out[intraday_timeframe],
            required_columns=intraday_required,
            all_pairs=all_pairs,
            cache=intraday_cache,
        )
    return out


# AGENT HOT PATH: Lifecycle rows fuse the latest intraday row with open-position context before exit/reversal models score the bar.
def _build_lifecycle_row(
    *,
    row: pd.DataFrame,
    positions: list[dict[str, Any]],
    total_position_count: int,
    loop_ts: float,
    timeframe: str,
) -> pd.DataFrame:
    out = row.copy()
    timeframe_secs = max(1, _timeframe_to_seconds(timeframe))
    oldest_open_time = _position_oldest_open_time(positions)
    time_in_trade_bars = 0.0
    if positions and oldest_open_time > 0.0:
        time_in_trade_bars = max(0.0, (float(loop_ts) - float(oldest_open_time)) / float(timeframe_secs))
    out.loc[:, "time_in_trade_bars"] = float(time_in_trade_bars)
    out.loc[:, "open_position_count"] = float(max(0, int(total_position_count)))
    if "live_edge_decay" not in out.columns:
        out.loc[:, "live_edge_decay"] = float(_safe_float(out.iloc[0].get("edge_decay_12"), 0.0))
    if "h1_available" not in out.columns:
        out.loc[:, "h1_available"] = float(1.0 if any(str(col).startswith("h1_") for col in out.columns) else 0.0)
    return out


def _score_exit_policy_model(model: Any, row: pd.DataFrame, *, action_labels: dict[int, str]) -> dict[str, Any]:
    if model is None:
        return {"selected": "hold", "score": 0.0, "probs": {}}
    proba = model.predict_proba(row)
    if proba.empty:
        return {"selected": "hold", "score": 0.0, "probs": {}}
    probs: dict[str, float] = {}
    for col, value in dict(proba.iloc[0]).items():
        label = str(col)
        if str(col).startswith("p"):
            try:
                label = action_labels.get(int(str(col)[1:]), label)
            except Exception:
                label = str(col)
        probs[str(label)] = float(value)
    selected = max(probs.items(), key=lambda item: float(item[1]))[0] if probs else "hold"
    return {
        "selected": str(selected),
        "score": float(probs.get(selected, 0.0)),
        "probs": probs,
    }


def _score_binary_lifecycle_model(model: Any, row: pd.DataFrame) -> float:
    if model is None:
        return 0.0
    proba = model.predict_proba(row)
    if proba.empty:
        return 0.0
    return float(_safe_float(proba.iloc[0].get("p1"), 0.0))


def _required_feature_timeframes() -> list[str]:
    s = get_settings()
    ordered: list[str] = []
    for tf in (str(s.intraday_timeframe).upper(), str(s.swing_timeframe).upper(), str(s.regime_timeframe).upper()):
        if tf and tf not in ordered:
            ordered.append(tf)
    return ordered


def _state_mt4_fresh(state: dict[str, Any]) -> bool:
    status = str(state.get("system_status") or "").strip().lower()
    try:
        age = float(state.get("heartbeat_age_secs")) if state.get("heartbeat_age_secs") is not None else None
    except Exception:
        age = None
    try:
        stale_after = float(state.get("heartbeat_stale_after_secs") or 30.0)
    except Exception:
        stale_after = 30.0
    return bool(status == "connected" and age is not None and age <= stale_after)


def _state_position_counts(state: dict[str, Any], *, pair: str) -> tuple[int, int]:
    positions = list(state.get("positions", []) or [])
    total = len(positions)
    pair_count = 0
    for p in positions:
        sym = str((p or {}).get("symbol", "")).upper()
        if sym == str(pair).upper():
            pair_count += 1
    return pair_count, total


def _pair_positions(state: dict[str, Any], *, pair: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos in list(state.get("positions", []) or []):
        symbol = str((pos or {}).get("symbol", "")).upper()
        if symbol == str(pair).upper():
            out.append(dict(pos or {}))
    return out


def _position_side(positions: list[dict[str, Any]]) -> str:
    if not positions:
        return "flat"
    for raw in positions:
        pos = dict(raw or {})
        for key in ("type", "order_type", "position_type"):
            value = pos.get(key)
            if value is None or str(value).strip() == "":
                continue
            try:
                typ = int(float(value))
            except Exception:
                typ = -1
            if typ == 0:
                return "long"
            if typ == 1:
                return "short"
            txt = str(value).strip().lower()
            if txt in {"buy", "long", "op_buy"}:
                return "long"
            if txt in {"sell", "short", "op_sell"}:
                return "short"
        for key in ("side", "position_side", "direction", "cmd"):
            txt = str(pos.get(key) or "").strip().lower()
            if txt in {"buy", "long"}:
                return "long"
            if txt in {"sell", "short"}:
                return "short"
    return "flat"


def _reversal_blocking_reasons(reasons: list[str]) -> list[str]:
    blocked = []
    for reason in list(reasons or []):
        txt = str(reason or "").strip()
        if not txt:
            continue
        if txt in {"pair_exposure_cap", "portfolio_exposure_cap"}:
            continue
        blocked.append(txt)
    return list(dict.fromkeys(blocked))


def _shadow_entry_safety_reasons(reasons: list[str]) -> list[str]:
    hard_exact = {
        "mt4_stale",
        "tick_feed_stale",
        "missing_live_tick",
        "missing_spread_input",
        "stale_feature_bar",
        "missing_feature_ts",
        "governance_paused",
        "spread_too_wide",
    }
    out: list[str] = []
    for reason in list(reasons or []):
        txt = str(reason or "").strip()
        if not txt:
            continue
        if txt in hard_exact or txt.startswith("session_blocked:") or txt.startswith("startup_") or txt.startswith("no_features:") or txt.startswith(
            "model_inference_error:"
        ):
            out.append(txt)
    return list(dict.fromkeys(out))


def _shadow_pair_tier(settings: Any, pair: str) -> str:
    if hasattr(settings, "pair_tier"):
        try:
            return str(settings.pair_tier(pair))
        except Exception:
            pass
    tier1 = {str(item).upper().strip() for item in list(getattr(settings, "tier1_pairs", []) or [])}
    return "tier1" if str(pair).upper().strip() in tier1 else "tier2"


def _shadow_session_bucket(ts_value: Any) -> str:
    return str(session_bucket_from_ts(ts_value))


def _accumulate_spread_diag(
    *,
    pair_raw: dict[str, dict[str, Any]],
    session_raw: dict[str, dict[str, Any]],
    pair: str,
    meta: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    spread_bps = float(_safe_float(meta.get("spread_bps", decision.get("spread_bps")), 0.0))
    threshold_snapshot = dict(meta.get("threshold_snapshot", {}) or {})
    max_spread_bps = float(
        _safe_float(
            meta.get("max_spread_bps", threshold_snapshot.get("max_spread_bps", decision.get("max_spread_bps"))),
            0.0,
        )
    )
    spread_excess_bps = max(0.0, float(spread_bps) - float(max_spread_bps))
    session_bucket = _shadow_session_bucket(meta.get("ts") or meta.get("decision_ts") or decision.get("ts"))
    pair_row = pair_raw.setdefault(
        str(pair),
        {"count": 0, "spread_bps_sum": 0.0, "max_spread_bps_sum": 0.0, "spread_excess_bps_sum": 0.0, "session": session_bucket},
    )
    pair_row["count"] = int(pair_row.get("count", 0)) + 1
    pair_row["spread_bps_sum"] = float(pair_row.get("spread_bps_sum", 0.0)) + float(spread_bps)
    pair_row["max_spread_bps_sum"] = float(pair_row.get("max_spread_bps_sum", 0.0)) + float(max_spread_bps)
    pair_row["spread_excess_bps_sum"] = float(pair_row.get("spread_excess_bps_sum", 0.0)) + float(spread_excess_bps)
    session_row = session_raw.setdefault(
        str(session_bucket),
        {
            "count": 0,
            "spread_bps_sum": 0.0,
            "max_spread_bps_sum": 0.0,
            "spread_excess_bps_sum": 0.0,
            "pairs": set(),
        },
    )
    session_row["count"] = int(session_row.get("count", 0)) + 1
    session_row["spread_bps_sum"] = float(session_row.get("spread_bps_sum", 0.0)) + float(spread_bps)
    session_row["max_spread_bps_sum"] = float(session_row.get("max_spread_bps_sum", 0.0)) + float(max_spread_bps)
    session_row["spread_excess_bps_sum"] = float(session_row.get("spread_excess_bps_sum", 0.0)) + float(spread_excess_bps)
    session_pairs = session_row.setdefault("pairs", set())
    if isinstance(session_pairs, set):
        session_pairs.add(str(pair))


def _finalize_spread_diag(
    *,
    pair_raw: dict[str, dict[str, Any]],
    session_raw: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    by_pair = dict(
        sorted(
            (
                (
                    pair,
                    {
                        "count": int(row.get("count", 0)),
                        "avg_spread_bps": float(row.get("spread_bps_sum", 0.0)) / max(1, int(row.get("count", 0))),
                        "avg_max_spread_bps": float(row.get("max_spread_bps_sum", 0.0)) / max(1, int(row.get("count", 0))),
                        "avg_excess_bps": float(row.get("spread_excess_bps_sum", 0.0)) / max(1, int(row.get("count", 0))),
                        "session": str(row.get("session", "")),
                    },
                )
                for pair, row in pair_raw.items()
            ),
            key=lambda item: (-int(item[1].get("count", 0)), -float(item[1].get("avg_excess_bps", 0.0)), item[0]),
        )
    )
    by_session = dict(
        sorted(
            (
                (
                    session,
                    {
                        "count": int(row.get("count", 0)),
                        "avg_spread_bps": float(row.get("spread_bps_sum", 0.0)) / max(1, int(row.get("count", 0))),
                        "avg_max_spread_bps": float(row.get("max_spread_bps_sum", 0.0)) / max(1, int(row.get("count", 0))),
                        "avg_excess_bps": float(row.get("spread_excess_bps_sum", 0.0)) / max(1, int(row.get("count", 0))),
                        "pairs": sorted(str(item) for item in list(row.get("pairs", set()) or [])),
                    },
                )
                for session, row in session_raw.items()
            ),
            key=lambda item: (-int(item[1].get("count", 0)), -float(item[1].get("avg_excess_bps", 0.0)), item[0]),
        )
    )
    return {
        "reject_count": int(sum(int(row.get("count", 0)) for row in pair_raw.values())),
        "dominant_pair": next(iter(by_pair), ""),
        "dominant_session": next(iter(by_session), ""),
        "by_pair": by_pair,
        "by_session": by_session,
    }


_ADAPTIVE_SHADOW_NUMERIC_DEFAULTS: dict[str, float] = {
    "ret_1": 0.0,
    "ret_5": 0.0,
    "ret_20": 0.0,
    "atr_14": 1.0,
    "mid_close": 1.0,
    "vol_term_ratio": 1.0,
    "cross_pair_dispersion": 0.0,
    "spread_bps": 0.0,
    "bar_imbalance": 0.0,
    "micro_pressure": 0.0,
    "calibrated_ev_bps_shadow": 0.0,
    "pullback_depth_20": 0.0,
    "pushup_depth_20": 0.0,
    "h1_trend_strength_20": 0.0,
    "h4_trend_strength_20": 0.0,
    "d_trend_strength_20": 0.0,
    "uncertainty_score": 0.0,
    "model_disagreement_score": 0.0,
    "htf_alignment_score": 0.0,
    "directional_swing_confidence": 0.0,
    "pullback_quality_score": 0.0,
    "extension_penalty_score": 0.0,
    "resume_trigger_score": 0.0,
}
_ADAPTIVE_SHADOW_BOOL_DEFAULTS: dict[str, bool] = {
    "session_entry_blocked": False,
}
_ADAPTIVE_SHADOW_TEXT_DEFAULTS: dict[str, str] = {
    "pair": "",
    "ts": "",
    "signal_side": "long",
    "scenario_bucket": "",
    "regime_bucket": "",
    "session_bucket": "",
    "session_entry_block_reason": "",
    "baseline_rejection_reason": "",
    "strict_rejection_reason": "",
}


def _adaptive_shadow_row_snapshot(
    *,
    pair: str,
    intraday_row: pd.DataFrame,
    signal: Any,
    spread_bps: float,
    max_spread_bps: float,
    ts_value: str,
    loop_ts: float,
    baseline_rejection_reason: str,
) -> dict[str, Any]:
    source = dict(intraday_row.iloc[0].to_dict() if not intraday_row.empty else {})
    row: dict[str, Any] = dict(source)
    row.update(
        {
            "pair": str(pair).upper(),
            "ts": str(ts_value or source.get("ts", "")),
            "_adaptive_cycle_key": float(loop_ts),
            "signal_side": str(getattr(signal, "side", "long") or "long").strip().lower(),
            "spread_bps": float(spread_bps),
            "max_spread_bps": float(max_spread_bps),
            "scenario_bucket": str(getattr(signal, "scenario_bucket", source.get("scenario_bucket", "")) or ""),
            "regime_bucket": str(source.get("regime_bucket", "")),
            "session_bucket": str(getattr(signal, "session_bucket", source.get("session_bucket", "")) or ""),
            "session_entry_blocked": bool(getattr(signal, "session_entry_blocked", False)),
            "session_entry_block_reason": str(getattr(signal, "session_entry_block_reason", "") or ""),
            "uncertainty_score": float(getattr(signal, "uncertainty_score", source.get("uncertainty_score", 0.0)) or 0.0),
            "model_disagreement_score": float(getattr(signal, "model_disagreement_score", source.get("model_disagreement_score", 0.0)) or 0.0),
            "htf_alignment_score": float(getattr(signal, "htf_alignment_score", source.get("htf_alignment_score", 0.0)) or 0.0),
            "directional_swing_confidence": float(
                getattr(signal, "directional_swing_confidence", source.get("directional_swing_confidence", 0.0)) or 0.0
            ),
            "pullback_quality_score": float(getattr(signal, "pullback_quality_score", source.get("pullback_quality_score", 0.0)) or 0.0),
            "extension_penalty_score": float(
                getattr(signal, "extension_penalty_score", source.get("extension_penalty_score", 0.0)) or 0.0
            ),
            "resume_trigger_score": float(getattr(signal, "resume_trigger_score", source.get("resume_trigger_score", 0.0)) or 0.0),
            "calibrated_ev_bps_shadow": float(
                getattr(signal, "calibrated_ev_bps_shadow", source.get("calibrated_ev_bps_shadow", 0.0)) or 0.0
            ),
            "baseline_rejection_reason": str(baseline_rejection_reason or ""),
            "strict_rejection_reason": str(baseline_rejection_reason or ""),
        }
    )
    for col, default in _ADAPTIVE_SHADOW_NUMERIC_DEFAULTS.items():
        row[col] = _safe_float(row.get(col, default), default)
    for col, default in _ADAPTIVE_SHADOW_BOOL_DEFAULTS.items():
        row[col] = bool(row.get(col, default))
    for col, default in _ADAPTIVE_SHADOW_TEXT_DEFAULTS.items():
        row[col] = str(row.get(col, default) or default)
    return row


def _adaptive_shadow_frames_from_history(
    *,
    history: dict[str, list[dict[str, Any]]],
    pairs: list[str],
) -> dict[str, pd.DataFrame]:
    available_pairs = [str(pair).upper() for pair in pairs if list(history.get(str(pair).upper(), []) or [])]
    if not available_pairs:
        return {}
    timeline_values = sorted(
        {
            float(item.get("_adaptive_cycle_key", 0.0) or 0.0)
            for pair in available_pairs
            for item in list(history.get(pair, []) or [])
            if float(item.get("_adaptive_cycle_key", 0.0) or 0.0) > 0.0
        }
    )
    if not timeline_values:
        return {}
    timeline = pd.Index(timeline_values, name="_adaptive_cycle_key")
    frames: dict[str, pd.DataFrame] = {}
    for pair in available_pairs:
        raw_records = list(history.get(pair, []) or [])
        if not raw_records:
            continue
        frame = pd.DataFrame(raw_records)
        if frame.empty or "_adaptive_cycle_key" not in frame.columns:
            continue
        frame = frame.drop_duplicates(subset=["_adaptive_cycle_key"], keep="last").set_index("_adaptive_cycle_key").sort_index()
        frame = frame.reindex(timeline).ffill().bfill()
        frame["pair"] = str(pair).upper()
        for col, default in _ADAPTIVE_SHADOW_NUMERIC_DEFAULTS.items():
            if col not in frame.columns:
                frame[col] = float(default)
            frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(float(default)).astype(float)
        for col, default in _ADAPTIVE_SHADOW_BOOL_DEFAULTS.items():
            if col not in frame.columns:
                frame[col] = bool(default)
            frame[col] = frame[col].fillna(bool(default)).astype(bool)
        for col, default in _ADAPTIVE_SHADOW_TEXT_DEFAULTS.items():
            if col not in frame.columns:
                frame[col] = str(default)
            frame[col] = frame[col].fillna(default).astype(str)
        frames[pair] = frame
    return frames


def _belief_signal_proxy(meta: dict[str, Any]) -> SimpleNamespace:
    side_raw = str(meta.get("side") or meta.get("position_side") or "").strip().lower()
    if side_raw in {"buy", "long"}:
        side = "long"
    elif side_raw in {"sell", "short"}:
        side = "short"
    else:
        side = "long"
    return SimpleNamespace(
        pair=str(meta.get("pair") or ""),
        ts=str(meta.get("ts") or ""),
        side=str(side),
        regime_prob=float(_safe_float(meta.get("regime_prob", 0.0), 0.0)),
        swing_prob=float(_safe_float(meta.get("swing_prob", 0.0), 0.0)),
        entry_prob=float(_safe_float(meta.get("entry_prob", 0.0), 0.0)),
        trade_prob=float(_safe_float(meta.get("trade_prob", 0.0), 0.0)),
        uncertainty_score=float(_safe_float(meta.get("uncertainty_score", 0.0), 0.0)),
        model_disagreement_score=float(_safe_float(meta.get("model_disagreement_score", 0.0), 0.0)),
        directional_swing_confidence=float(_safe_float(meta.get("directional_swing_confidence", 0.0), 0.0)),
        htf_alignment_score=float(_safe_float(meta.get("htf_alignment_score", 0.0), 0.0)),
        pullback_quality_score=float(_safe_float(meta.get("pullback_quality_score", 0.0), 0.0)),
        resume_trigger_score=float(_safe_float(meta.get("resume_trigger_score", 0.0), 0.0)),
        extension_penalty_score=float(_safe_float(meta.get("extension_penalty_score", 0.0), 0.0)),
        structure_timing_score=float(_safe_float(meta.get("structure_timing_score", 0.0), 0.0)),
        expected_edge_bps=float(_safe_float(meta.get("expected_edge_bps", 0.0), 0.0)),
        spread_bps=float(_safe_float(meta.get("spread_bps", 0.0), 0.0)),
        scenario_bucket=str(meta.get("scenario_bucket") or ""),
        context_frame_profile=str(meta.get("context_frame_profile") or ""),
    )


def _directional_belief_policy_diag(settings: Any) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(settings, "belief_shadow_enabled", False)),
        "runtime_required": bool(getattr(settings, "belief_runtime_required", False)),
        "short_horizon_bars": int(getattr(settings, "belief_short_horizon_bars", 3) or 3),
        "trade_horizon_bars": int(getattr(settings, "belief_trade_horizon_bars", 12) or 12),
        "structural_horizon_bars": int(getattr(settings, "belief_structural_horizon_bars", 48) or 48),
    }


def _attach_directional_belief_shadow(
    *,
    decisions: list[dict[str, Any]],
    loaded_model_sets: dict[str, LoadedModelSet],
    adaptive_rows_by_pair: dict[str, dict[str, Any]],
    settings: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    enabled = bool(getattr(settings, "belief_shadow_enabled", False))
    influence_mode = str(getattr(settings, "belief_influence_mode", "off") or "off").strip().lower()
    primary_counts: Counter[str] = Counter()
    opposition_counts: Counter[str] = Counter()
    opposition_side_counts: Counter[str] = Counter()
    gaps: list[float] = []
    fragilities: list[float] = []
    primary_rank_scores: list[float] = []
    primary_ev_probs: list[float] = []
    primary_expected_net_evs: list[float] = []
    primary_fail_fast_probs: list[float] = []
    no_edge_count = 0
    versions: dict[str, str] = {}
    loaded_count = 0

    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        ts_value = str(meta.get("ts") or "")
        loaded = loaded_model_sets.get(pair)
        adaptive_row = dict(adaptive_rows_by_pair.get(pair, {}) or {})
        belief_meta = dict(adaptive_row)
        belief_meta.setdefault("pair", pair)
        belief_meta.setdefault("ts", ts_value)
        belief_meta.setdefault("playbook_score", float(_safe_float(meta.get("adaptive_playbook_score", adaptive_row.get("playbook_score", 0.0)), 0.0)))
        belief_meta.setdefault("location_score", float(_safe_float(meta.get("adaptive_location_score", adaptive_row.get("location_score", 0.0)), 0.0)))
        belief_meta.setdefault("trigger_score", float(_safe_float(meta.get("adaptive_trigger_score", adaptive_row.get("trigger_score", 0.0)), 0.0)))
        belief_meta.setdefault("macro_coherence_score", float(_safe_float(meta.get("adaptive_macro_coherence_score", adaptive_row.get("macro_coherence_score", 0.0)), 0.0)))
        belief_meta.setdefault("hostility_score", float(_safe_float(meta.get("adaptive_hostility_score", adaptive_row.get("hostility_score", 0.0)), 0.0)))
        belief_meta.setdefault("adaptive_playbook", str(meta.get("adaptive_playbook") or adaptive_row.get("playbook") or ""))
        belief_meta.setdefault("adaptive_environment_state", str(meta.get("adaptive_environment_state") or adaptive_row.get("environment_state") or ""))
        belief_meta.setdefault("uncertainty_score", float(_safe_float(meta.get("uncertainty_score", adaptive_row.get("uncertainty_score", 0.0)), 0.0)))
        belief_meta.setdefault("model_disagreement_score", float(_safe_float(meta.get("model_disagreement_score", adaptive_row.get("model_disagreement_score", 0.0)), 0.0)))
        belief_meta.setdefault("extension_penalty_score", float(_safe_float(meta.get("extension_penalty_score", adaptive_row.get("extension_penalty_score", 0.0)), 0.0)))
        belief_meta.setdefault("scenario_bucket", str(meta.get("scenario_bucket") or adaptive_row.get("scenario_bucket") or ""))
        belief_meta.setdefault("regime_bucket", str(meta.get("regime_bucket") or adaptive_row.get("regime_bucket") or ""))
        signal_proxy = _belief_signal_proxy(meta)
        belief = empty_directional_belief(pair=pair, ts=ts_value, source_mode="disabled")
        if enabled and loaded is not None and loaded.belief_model is not None:
            belief = compute_directional_belief(
                row=adaptive_row or meta,
                signal=signal_proxy,
                adaptive_meta=belief_meta,
                model_set=loaded.belief_model,
            )
            loaded_count += 1
            primary_counts[str(belief.primary_scenario or "")] += 1
            opposition_counts[str(belief.opposing_scenario or "")] += 1
            opposition_side_counts[str(belief.opposing_side or "")] += 1
            gaps.append(float(belief.belief_gap))
            fragilities.append(float(belief.fragility_score))
            primary_rank_scores.append(float(belief.primary_rank_score))
            primary_ev_probs.append(float(belief.primary_ev_above_hurdle_prob))
            primary_expected_net_evs.append(float(belief.primary_expected_net_ev_bps))
            primary_fail_fast_probs.append(float(belief.primary_fail_fast_prob))
            no_edge_count += int(bool(belief.no_edge))
            versions[pair] = str(belief.model_version or "")
        elif enabled:
            belief = empty_directional_belief(pair=pair, ts=ts_value, source_mode="artifact_missing")
        meta.update(belief.to_dict())
        decision["metadata"] = meta

    cross_pair_records = build_cross_pair_influence_records(
        [
            {
                **dict(decision.get("metadata", {}) or {}),
                "pair": str(dict(decision.get("metadata", {}) or {}).get("pair") or decision.get("symbol") or "").upper(),
                "ts": str(dict(decision.get("metadata", {}) or {}).get("ts") or ""),
            }
            for decision in decisions
        ]
    )
    cross_pair_by_pair = {
        str(record.pair).upper(): record for record in cross_pair_records if str(record.pair).strip()
    }
    cross_pair_gated_count = 0
    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        record = cross_pair_by_pair.get(pair)
        if record is None:
            continue
        meta["cross_pair_rank_position"] = int(record.rank_position)
        meta["cross_pair_influence_score"] = float(record.influence_score)
        meta["cross_pair_recommendation_strength"] = float(record.recommendation_strength)
        meta["cross_pair_influenced_by_pairs"] = list(record.influenced_by_pairs)
        meta["cross_pair_reason_codes"] = list(record.cross_pair_reason_codes)
        meta["cross_pair_source_mode"] = str(record.source_mode)
        meta["cross_pair_influence_mode"] = str(influence_mode or "off")
        meta["cross_pair_influence_adjustment"] = float((float(record.recommendation_strength) - 0.5) * 0.16)
        if influence_mode in {"soft_gate", "hard_gate"} and float(record.recommendation_strength) < 0.30:
            meta["cross_pair_soft_block"] = True
        if influence_mode == "hard_gate" and float(record.recommendation_strength) < 0.20:
            meta["cross_pair_hard_block"] = True
            cross_pair_gated_count += 1
        decision["metadata"] = meta

    cycle_summary = {
        "candidate_count_with_belief": int(loaded_count),
        "avg_belief_gap": float(sum(gaps) / max(1, len(gaps))) if gaps else 0.0,
        "avg_fragility_score": float(sum(fragilities) / max(1, len(fragilities))) if fragilities else 0.0,
        "avg_primary_rank_score": float(sum(primary_rank_scores) / max(1, len(primary_rank_scores))) if primary_rank_scores else 0.0,
        "avg_primary_ev_above_hurdle_prob": float(sum(primary_ev_probs) / max(1, len(primary_ev_probs))) if primary_ev_probs else 0.0,
        "avg_primary_expected_net_ev_bps": float(sum(primary_expected_net_evs) / max(1, len(primary_expected_net_evs))) if primary_expected_net_evs else 0.0,
        "avg_primary_fail_fast_prob": float(sum(primary_fail_fast_probs) / max(1, len(primary_fail_fast_probs))) if primary_fail_fast_probs else 0.0,
        "no_edge_share": float(no_edge_count / max(1, loaded_count)) if loaded_count else 0.0,
        "primary_scenario_counts": {k: int(v) for k, v in sorted(primary_counts.items()) if str(k)},
        "opposition_scenario_counts": {k: int(v) for k, v in sorted(opposition_counts.items()) if str(k)},
        "opposition_side_counts": {k: int(v) for k, v in sorted(opposition_side_counts.items()) if str(k)},
        "artifact_versions": {k: str(v) for k, v in sorted(versions.items()) if str(v)},
        "cross_pair_influence_mode": str(influence_mode or "off"),
        "cross_pair_ranked_pairs": [str(item.pair) for item in cross_pair_records[:5]],
        "cross_pair_gated_count": int(cross_pair_gated_count),
    }
    metrics = {
        "decision_count": int(len(decisions)),
        "belief_loaded_share": float(loaded_count / max(1, len(decisions))) if decisions else 0.0,
        "avg_belief_gap": float(cycle_summary["avg_belief_gap"]),
        "avg_fragility_score": float(cycle_summary["avg_fragility_score"]),
        "avg_primary_rank_score": float(cycle_summary["avg_primary_rank_score"]),
        "avg_primary_ev_above_hurdle_prob": float(cycle_summary["avg_primary_ev_above_hurdle_prob"]),
        "avg_primary_expected_net_ev_bps": float(cycle_summary["avg_primary_expected_net_ev_bps"]),
        "avg_primary_fail_fast_prob": float(cycle_summary["avg_primary_fail_fast_prob"]),
        "no_edge_share": float(cycle_summary["no_edge_share"]),
        "primary_scenario_counts": dict(cycle_summary["primary_scenario_counts"]),
        "opposition_scenario_counts": dict(cycle_summary["opposition_scenario_counts"]),
        "opposition_side_counts": dict(cycle_summary["opposition_side_counts"]),
        "cross_pair_gated_share": float(cross_pair_gated_count / max(1, len(cross_pair_records))) if cross_pair_records else 0.0,
    }
    return cycle_summary, metrics


def _adaptive_shadow_open_position_map(
    *,
    decisions: list[dict[str, Any]],
    adaptive_rows_by_pair: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace] | None = None,
) -> dict[str, Any]:
    open_positions: dict[str, Any] = {}
    registry = adaptive_position_registry or {}
    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        position_open = bool(int(_safe_float(meta.get("position_count_pair", 0), 0.0)) > 0 or str(meta.get("position_signature", "")).strip())
        if not pair or not position_open:
            continue
        existing = registry.get(pair)
        if existing is not None:
            open_positions[pair] = existing
            continue
        adaptive_row = dict(adaptive_rows_by_pair.get(pair, {}) or {})
        side = str(meta.get("position_side") or "").strip().lower()
        if side not in {"long", "short"}:
            signal_side = str(adaptive_row.get("signal_side") or "").strip().lower()
            side = signal_side if signal_side in {"long", "short"} else "long"
        open_positions[pair] = SimpleNamespace(
            pair=str(pair),
            side=str(side),
            playbook=str(adaptive_row.get("playbook") or PLAYBOOK_NO_TRADE),
            entry_session_bucket=str(adaptive_row.get("session_bucket") or meta.get("session_bucket") or ""),
        )
    return open_positions


def _runtime_allocator_open_position(
    *,
    pair: str,
    position: SimpleNamespace,
    current_row: dict[str, Any],
    keep_score: float,
    age_bars: float,
    protected_hold: bool,
    replaceable_hold: bool,
) -> AllocatorOpenPosition:
    return AllocatorOpenPosition(
        position_id=str(pair),
        pair=str(pair),
        side=str(getattr(position, "side", "long")),
        sleeve=str(getattr(position, "sleeve", "") or playbook_to_sleeve(getattr(position, "playbook", ""))),
        session_bucket=str(getattr(position, "entry_session_bucket", "")),
        keep_score=float(keep_score),
        age_bars=float(age_bars),
        protected_hold=bool(protected_hold),
        replaceable_hold=bool(replaceable_hold),
        thesis_id=str(getattr(position, "thesis_id", "") or build_thesis_id(pair, getattr(position, "side", "long"), getattr(position, "sleeve", "") or playbook_to_sleeve(getattr(position, "playbook", "")))),
        campaign_state=str(getattr(position, "campaign_state", CAMPAIGN_STATE_INACTIVE) or CAMPAIGN_STATE_INACTIVE),
        macro_coherence_decay=float(
            max(
                0.0,
                float(getattr(position, "entry_macro_coherence_score", 0.0))
                - float(_safe_float(current_row.get("macro_coherence_score", getattr(position, "entry_macro_coherence_score", 0.0)), 0.0)),
            )
        ),
        thesis_stage=str(getattr(position, "thesis_stage", "core") or "core"),
        replacement_urgency=float(_safe_float(getattr(position, "replacement_urgency", max(0.0, 1.0 - keep_score)), max(0.0, 1.0 - keep_score))),
    )


def _allocator_position_namespace_from_state(
    *,
    pair: str,
    position: dict[str, Any],
    current_row: dict[str, Any],
    current_equity: float,
) -> SimpleNamespace:
    raw = dict(position or {})
    side = _position_side([raw])
    if side not in {"long", "short"}:
        lots = float(_safe_float(raw.get("lots"), 0.0))
        side = "long" if lots >= 0.0 else "short"
    playbook = str(current_row.get("playbook") or raw.get("playbook") or PLAYBOOK_TREND_PULLBACK)
    sleeve = str(current_row.get("sleeve") or raw.get("sleeve") or playbook_to_sleeve(playbook))
    return SimpleNamespace(
        pair=str(pair).upper(),
        side=str(side or "long"),
        playbook=str(playbook),
        sleeve=str(sleeve),
        entry_session_bucket=str(raw.get("session_bucket") or current_row.get("session_bucket") or ""),
        entry_macro_coherence_score=float(
            _safe_float(
                raw.get("macro_coherence_score", current_row.get("macro_coherence_score", 0.0)),
                0.0,
            )
        ),
        replacement_urgency=float(
            _safe_float(
                raw.get("replacement_urgency", current_row.get("replacement_urgency", max(0.0, 1.0 - abs(float(_safe_float(raw.get("lots"), 0.0)))))),
                0.0,
            )
        ),
        thesis_id=str(current_row.get("thesis_id") or raw.get("thesis_id") or ""),
        campaign_state=str(current_row.get("campaign_state") or raw.get("campaign_state") or CAMPAIGN_STATE_INACTIVE),
        campaign_state_reason=str(current_row.get("campaign_state_reason") or raw.get("campaign_state_reason") or ""),
        campaign_seq=int(_safe_float(raw.get("campaign_seq", current_row.get("campaign_seq", 0.0)), 0.0)),
        campaign_entry_kind=str(raw.get("campaign_entry_kind") or current_row.get("campaign_entry_kind") or ""),
        thesis_stage=str(current_row.get("thesis_stage") or raw.get("thesis_stage") or "core"),
        open_equity_usd=float(_safe_float(raw.get("open_equity_usd"), current_equity)),
        entry_trade_prob=float(_safe_float(current_row.get("trade_prob", raw.get("trade_prob", 0.0)), 0.0)),
        aggressive_fallback_used=bool(
            raw.get("aggressive_fallback_used", current_row.get("adaptive_aggressive_fallback_used", False))
        ),
    )


def _build_allocator_open_positions(
    *,
    state: dict[str, Any],
    adaptive_position_registry: dict[str, SimpleNamespace],
    adaptive_rows_by_pair: dict[str, dict[str, Any]],
    current_equity: float,
) -> list[AllocatorOpenPosition]:
    allocator_open_positions: list[AllocatorOpenPosition] = []
    seen_pairs: set[str] = set()
    for raw in list(state.get("positions", []) or []):
        position = dict(raw or {})
        pair = str(position.get("symbol") or "").upper()
        if not pair or pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        current_row = dict(adaptive_rows_by_pair.get(pair, {}) or {})
        if pair in adaptive_position_registry:
            position_ns = adaptive_position_registry[pair]
        else:
            position_ns = _allocator_position_namespace_from_state(
                pair=pair,
                position=position,
                current_row=current_row,
                current_equity=current_equity,
            )
        keep_score = float(
            adaptive_replacement_keep_score(
                lifecycle_action="hold",
                lifecycle_reason="adaptive_hold",
                playbook_score=float(_safe_float(current_row.get("playbook_score", 0.0), 0.0)),
                location_score=float(_safe_float(current_row.get("location_score", 0.0), 0.0)),
                trigger_score=float(_safe_float(current_row.get("trigger_score", 0.0), 0.0)),
                entry_trade_prob=float(_safe_float(getattr(position_ns, "entry_trade_prob", 0.0), 0.0)),
                entry_macro_coherence_score=float(_safe_float(getattr(position_ns, "entry_macro_coherence_score", 0.0), 0.0)),
                aggressive_fallback_used=bool(getattr(position_ns, "aggressive_fallback_used", False)),
            )
        )
        age_bars = float(_safe_float(position.get("time_in_trade_bars", position.get("age_bars", 999.0)), 999.0))
        allocator_open_positions.append(
            _runtime_allocator_open_position(
                pair=pair,
                position=position_ns,
                current_row=current_row,
                keep_score=float(keep_score),
                age_bars=float(age_bars),
                protected_hold=bool(keep_score >= 0.62),
                replaceable_hold=bool(keep_score < 0.62),
            )
        )
    return allocator_open_positions


# AGENT PARITY: Adaptive shadow ranking uses the same shared adaptive policy module as the twin, but it runs against live-cycle exposure and freshness constraints.
def _apply_adaptive_shadow_ranking(
    decisions: list[dict[str, Any]],
    *,
    settings: Any,
    open_position_count: int,
    adaptive_rows_by_pair: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace] | None = None,
    recent_exit_registry: dict[str, dict[str, Any]] | None = None,
    pair_bar_index: dict[str, int] | None = None,
    sleeve_health_snapshots: dict[str, Any] | None = None,
    campaign_registry: dict[str, CampaignRegistryEntry] | None = None,
    state: dict[str, Any] | None = None,
    current_equity: float = 0.0,
) -> dict[str, Any]:
    divergence_counts = {"agree_ready": 0, "agree_blocked": 0, "live_only": 0, "adaptive_only": 0, "open_position": 0}
    rejection_reason_counts: dict[str, int] = {}
    rejection_pair_map: dict[str, str] = {}
    playbook_counts: dict[str, int] = {}
    environment_counts: dict[str, int] = {}
    aggressive_fallback_count = 0
    overlay_outputs: dict[int, Any] = {}
    shadow_enabled = bool(getattr(settings, "adaptive_shadow_enabled", True))
    remaining_slots = max(0, int(getattr(settings, "max_total_positions", 0) or 0) - int(open_position_count))
    max_new_entries_cfg = int(getattr(settings, "max_new_entries_per_cycle", 0) or 0)
    max_new_entries = remaining_slots if max_new_entries_cfg <= 0 else min(remaining_slots, max_new_entries_cfg)
    use_ranking = bool(getattr(settings, "use_portfolio_ranking", True))
    portfolio_corr_mode = str(getattr(settings, "portfolio_corr_mode", "heuristic") or "heuristic")
    realized_returns_by_pair = dict((state or {}).get("realized_returns_by_pair") or {}) if isinstance(state, dict) else {}
    allocator_config = allocator_config_from_settings(settings)
    campaign_config = campaign_config_from_settings(settings)
    campaign_store = campaign_registry if campaign_registry is not None else {}
    candidates: list[Any] = []

    if not decisions:
        return {
            "adaptive_shadow_enabled": bool(shadow_enabled),
            "adaptive_shadow_candidate_count": 0,
            "adaptive_shadow_ranked_count": 0,
            "adaptive_shadow_would_trade_count": 0,
            "adaptive_shadow_remaining_slots": int(remaining_slots),
            "adaptive_shadow_max_new_entries": int(max_new_entries if use_ranking else remaining_slots),
            "adaptive_shadow_aggressive_fallback_count": 0,
            "adaptive_shadow_live_divergence_counts": dict(divergence_counts),
            "adaptive_shadow_rejection_reason_counts": {},
            "adaptive_shadow_rejections_by_pair": {},
            "adaptive_shadow_playbook_counts": {},
            "adaptive_shadow_environment_counts": {},
            "adaptive_shadow_dominant_rejection_reason": "",
            "allocator_candidate_count": 0,
            "allocator_selected_count": 0,
            "allocator_ranked_out_count": 0,
            "allocator_replacement_candidate_count": 0,
            "allocator_replacement_exit_count": 0,
            "allocator_sleeve_candidate_counts": {},
            "allocator_sleeve_selected_counts": {},
            "allocator_sleeve_budget_targets": {},
            "allocator_sleeve_budget_used": {},
            "allocator_pair_pressure_avg": 0.0,
            "allocator_pair_pressure_max": 0.0,
            "allocator_session_pressure_avg": 0.0,
            "allocator_session_pressure_max": 0.0,
            "allocator_sleeve_pressure_avg": 0.0,
            "allocator_sleeve_pressure_max": 0.0,
            "allocator_correlation_pressure_avg": 0.0,
            "allocator_correlation_pressure_max": 0.0,
            "allocator_risk_pressure_avg": 0.0,
            "allocator_risk_pressure_max": 0.0,
            "overlay_cycle_summary": {
                "conviction_score_avg": 0.0,
                "conviction_score_max": 0.0,
                "conviction_score_min": 0.0,
                "conviction_band_counts": {},
                "thesis_stage_counts": {},
                "posture_counts": {},
                "sleeve_budget_target_total": 0,
                "sleeve_budget_used_total": 0,
                "pair_pressure_avg": 0.0,
                "pair_pressure_max": 0.0,
                "session_pressure_avg": 0.0,
                "session_pressure_max": 0.0,
                "sleeve_pressure_avg": 0.0,
                "sleeve_pressure_max": 0.0,
                "correlation_pressure_avg": 0.0,
                "correlation_pressure_max": 0.0,
                "risk_pressure_avg": 0.0,
                "risk_pressure_max": 0.0,
                "replacement_urgency_avg": 0.0,
                "policy_trace_count": 0,
                "diagnostics": {
                    "environment_posture": "",
                    "sleeve_budget_state": {},
                    "replacement_pressure_by_sleeve": {},
                    "portfolio_pressure": {
                        "pair_avg": 0.0,
                        "pair_max": 0.0,
                        "session_avg": 0.0,
                        "session_max": 0.0,
                        "sleeve_avg": 0.0,
                        "sleeve_max": 0.0,
                        "correlation_avg": 0.0,
                        "correlation_max": 0.0,
                        "risk_avg": 0.0,
                        "risk_max": 0.0,
                    },
                    "divergence_matrix": {
                        "by_pair": {},
                        "by_session": {},
                        "by_regime": {},
                        "by_sleeve": {},
                    },
                    "press_count": 0,
                    "stand_down_count": 0,
                },
            },
            "campaign_state_counts": {},
        }

    exit_registry = recent_exit_registry or {}
    bar_index_map = pair_bar_index or {}
    open_positions = _adaptive_shadow_open_position_map(
        decisions=decisions,
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        adaptive_position_registry=adaptive_position_registry,
    )
    live_state = dict(state or {})
    current_equity_value = float(_safe_float(current_equity, _safe_float(live_state.get("equity"), 0.0)))
    allocator_open_positions = _build_allocator_open_positions(
        state=live_state,
        adaptive_position_registry=adaptive_position_registry or {},
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        current_equity=float(current_equity_value),
    )

    for index, decision in enumerate(decisions):
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        position_open = bool(int(_safe_float(meta.get("position_count_pair", 0), 0.0)) > 0 or str(meta.get("position_signature", "")).strip())
        current_row = dict(adaptive_rows_by_pair.get(pair, {}) or {})
        environment_state = str(current_row.get("environment_state") or "")
        playbook = str(current_row.get("playbook") or PLAYBOOK_NO_TRADE)
        adaptive_reason = "adaptive_shadow_history_unavailable"
        adaptive_allowed = False

        if current_row:
            current_row["pair"] = str(pair)
            current_row["spread_bps"] = float(_safe_float(meta.get("spread_bps", current_row.get("spread_bps", 0.0)), 0.0))
            current_row["session_bucket"] = str(meta.get("session_bucket") or current_row.get("session_bucket") or "")
            current_row["session_entry_blocked"] = bool(meta.get("session_entry_blocked", current_row.get("session_entry_blocked", False)))
            current_row["session_entry_block_reason"] = str(
                meta.get("session_entry_block_reason") or current_row.get("session_entry_block_reason") or ""
            )
            current_row["baseline_rejection_reason"] = str(meta.get("rejection_reason") or "")
            current_row["strict_rejection_reason"] = str(meta.get("rejection_reason") or "")
            current_row["signal_side"] = "short" if str(decision.get("side") or "").strip().upper() == "SELL" else "long"
            current_row["position_side"] = str(current_row.get("signal_side") or "long")
            environment_state = str(current_row.get("environment_state") or "")
            playbook = str(current_row.get("playbook") or PLAYBOOK_NO_TRADE)

        if environment_state:
            environment_counts[environment_state] = int(environment_counts.get(environment_state, 0)) + 1
        if playbook:
            playbook_counts[playbook] = int(playbook_counts.get(playbook, 0)) + 1

        meta["adaptive_environment_state"] = str(environment_state)
        meta["adaptive_trend_persistence_score"] = float(_safe_float(current_row.get("trend_persistence_score", 0.0), 0.0))
        meta["adaptive_compression_score"] = float(_safe_float(current_row.get("compression_score", 0.0), 0.0))
        meta["adaptive_expansion_score"] = float(_safe_float(current_row.get("expansion_score", 0.0), 0.0))
        meta["adaptive_range_score"] = float(_safe_float(current_row.get("range_score", 0.0), 0.0))
        meta["adaptive_hostility_score"] = float(_safe_float(current_row.get("hostility_score", 0.0), 0.0))
        meta["adaptive_macro_coherence_score"] = float(_safe_float(current_row.get("macro_coherence_score", 0.0), 0.0))
        meta["adaptive_pair_strength_score"] = float(_safe_float(current_row.get("pair_strength_score", 0.0), 0.0))
        meta["adaptive_playbook"] = str(playbook)
        meta["adaptive_sleeve"] = str(playbook_to_sleeve(playbook))
        meta["adaptive_playbook_score"] = float(_safe_float(current_row.get("playbook_score", 0.0), 0.0))
        meta["adaptive_location_score"] = float(_safe_float(current_row.get("location_score", 0.0), 0.0))
        meta["adaptive_trigger_score"] = float(_safe_float(current_row.get("trigger_score", 0.0), 0.0))
        meta["adaptive_entry_quality"] = 0.0
        meta["thesis_id"] = str(build_thesis_id(pair, str(meta.get("position_side") or current_row.get("signal_side") or "long"), playbook_to_sleeve(playbook)))
        meta["campaign_state"] = CAMPAIGN_STATE_INACTIVE
        meta["campaign_state_reason"] = ""
        meta["campaign_proof_score"] = 0.0
        meta["campaign_maturity_score"] = 0.0
        meta["campaign_reset_quality"] = 0.0
        meta["campaign_priority_boost"] = 0.0
        meta["campaign_reentry_blocked"] = False
        meta["adaptive_currency_crowding_penalty"] = 0.0
        meta["adaptive_playbook_diversification_penalty"] = 0.0
        meta["allocator_score"] = 0.0
        meta["allocator_rank"] = None
        meta["allocator_selected"] = False
        meta["allocator_rejection_reason"] = ""
        meta["replacement_candidate"] = False
        meta["replacement_target_pair"] = ""
        meta["portfolio_pair_pressure"] = 0.0
        meta["portfolio_session_pressure"] = 0.0
        meta["portfolio_sleeve_pressure"] = 0.0
        meta["portfolio_correlation_pressure"] = 0.0
        meta["portfolio_risk_pressure"] = 0.0
        sleeve_snapshot = (sleeve_health_snapshots or {}).get(playbook_to_sleeve(playbook))
        meta["sleeve_health_score"] = float(getattr(sleeve_snapshot, "score", 0.5))
        meta["sleeve_health_state"] = str(getattr(sleeve_snapshot, "state", "healthy"))
        meta["adaptive_aggressive_fallback_used"] = False
        meta["adaptive_shadow_allowed"] = False
        meta["adaptive_portfolio_rank_shadow"] = None
        meta["adaptive_shadow_would_trade"] = False
        meta["adaptive_shadow_rejection_reason"] = str(adaptive_reason)
        meta["adaptive_shadow_live_divergence"] = "open_position" if position_open else ""
        meta["conviction_score"] = float(_safe_float(meta.get("conviction_score", 0.0), 0.0))
        meta["conviction_band"] = str(meta.get("conviction_band") or "")
        meta["thesis_stage"] = str(meta.get("thesis_stage") or "stand_down")
        meta["portfolio_posture"] = str(meta.get("portfolio_posture") or "balanced_probe")
        meta["sleeve_budget_target"] = int(_safe_float(meta.get("sleeve_budget_target", 0), 0.0))
        meta["sleeve_budget_used"] = int(_safe_float(meta.get("sleeve_budget_used", 0), 0.0))
        meta["replacement_urgency"] = float(_safe_float(meta.get("replacement_urgency", 0.0), 0.0))
        meta["policy_trace"] = []
        meta["overlay_metadata"] = {}
        meta["overlay_diagnostics"] = {}
        base_ready = bool(meta.get("strict_entry_ready", meta.get("entry_ready", False)))
        base_reason = str(
            (
                list(meta.get("strict_entry_blocking_reasons", meta.get("entry_blocking_reasons", [])) or [None])[0]
                if not base_ready
                else "approved"
            )
            or meta.get("strict_rejection_reason")
            or meta.get("rejection_reason")
            or ("approved" if base_ready else "entry_blocked")
        )
        _append_policy_trace(
            meta,
            stage="base_gate",
            verdict="allow" if base_ready else "block",
            reason=str(base_reason),
            score=float(_safe_float(meta.get("trade_prob", meta.get("entry_prob", 0.0)), 0.0)),
            details={
                "entry_ready": bool(meta.get("entry_ready", False)),
                "strict_entry_ready": bool(meta.get("strict_entry_ready", meta.get("entry_ready", False))),
                "session_bucket": str(meta.get("session_bucket") or ""),
            },
        )

        if not shadow_enabled:
            adaptive_reason = "adaptive_shadow_disabled"
            _append_policy_trace(meta, stage="adaptive_playbook", verdict="skip", reason=str(adaptive_reason))
        elif position_open:
            adaptive_reason = "adaptive_position_open"
            _append_policy_trace(meta, stage="adaptive_playbook", verdict="skip", reason=str(adaptive_reason))
        elif not current_row:
            adaptive_reason = "adaptive_shadow_history_unavailable"
            _append_policy_trace(meta, stage="adaptive_playbook", verdict="skip", reason=str(adaptive_reason))
        else:
            adaptive_eval = evaluate_adaptive_entry(
                row=current_row,
                strict_ready=bool(meta.get("entry_ready", False)),
                open_positions=open_positions,
                settings=settings,
                fallback_margin=0.08,
            )
            cross_pair_adjustment = float(_safe_float(meta.get("cross_pair_influence_adjustment", 0.0), 0.0))
            cross_pair_strength = float(_safe_float(meta.get("cross_pair_recommendation_strength", 0.5), 0.5))
            adaptive_eval["adaptive_entry_quality"] = float(
                _clip01(float(_safe_float(adaptive_eval.get("adaptive_entry_quality"), 0.0)) + cross_pair_adjustment)
            )
            adaptive_eval["cross_pair_rank_position"] = int(_safe_float(meta.get("cross_pair_rank_position"), 0.0))
            adaptive_eval["cross_pair_influence_score"] = float(_safe_float(meta.get("cross_pair_influence_score"), 0.0))
            adaptive_eval["cross_pair_recommendation_strength"] = float(cross_pair_strength)
            adaptive_eval["cross_pair_reason_codes"] = list(meta.get("cross_pair_reason_codes", []) or [])
            if bool(meta.get("cross_pair_soft_block", False)):
                adaptive_eval["adaptive_entry_quality"] = float(
                    _clip01(float(_safe_float(adaptive_eval.get("adaptive_entry_quality"), 0.0)) * 0.85)
                )
            if bool(meta.get("cross_pair_hard_block", False)):
                adaptive_eval["adaptive_allowed"] = False
                adaptive_eval["adaptive_rejection_reason"] = "cross_pair_hard_gate"
            if bool(adaptive_eval.get("adaptive_allowed")) and not position_open:
                campaign_candidate = evaluate_entry_campaign(
                    pair=pair,
                    side=str(meta.get("position_side") or current_row.get("signal_side") or "").strip().lower() or ("long" if str(decision.get("side")).upper() == "BUY" else "short"),
                    sleeve=playbook_to_sleeve(str(adaptive_eval.get("playbook") or playbook or PLAYBOOK_NO_TRADE)),
                    row={
                        "playbook_score": float(current_row.get("playbook_score", 0.0) or 0.0),
                        "location_score": float(current_row.get("location_score", 0.0) or 0.0),
                        "trigger_score": float(current_row.get("trigger_score", 0.0) or 0.0),
                        "macro_coherence_score": float(current_row.get("macro_coherence_score", 0.0) or 0.0),
                        "hostility_score": float(current_row.get("hostility_score", 0.0) or 0.0),
                        "extension_penalty_score": float(current_row.get("extension_penalty_score", 0.0) or 0.0),
                        "environment_state": str(current_row.get("environment_state") or ""),
                        "trade_prob": float(_safe_float(meta.get("trade_prob", current_row.get("trade_prob", 0.0)), 0.0)),
                    },
                    bar_idx=int(bar_index_map.get(pair, 0)),
                    ts=str(meta.get("ts") or ""),
                    registry=campaign_store,
                    config=campaign_config,
                )
                reentry_eval = adaptive_reentry_block(
                    pair=pair,
                    side=str(meta.get("position_side") or current_row.get("signal_side") or "").strip().lower() or ("long" if str(decision.get("side")).upper() == "BUY" else "short"),
                    playbook=str(adaptive_eval.get("playbook") or playbook or PLAYBOOK_NO_TRADE),
                    bar_idx=int(bar_index_map.get(pair, 0)),
                    exit_registry=exit_registry,
                    cooldown_scale=campaign_cooldown_scale(campaign_candidate.state, campaign_config),
                )
                if bool(reentry_eval.get("blocked")):
                    adaptive_eval["adaptive_allowed"] = False
                    adaptive_eval["adaptive_rejection_reason"] = str(reentry_eval.get("reason") or "adaptive_reentry_cooldown")
                if bool(campaign_candidate.reentry_blocked):
                    adaptive_eval["adaptive_allowed"] = False
                    adaptive_eval["adaptive_rejection_reason"] = str(campaign_candidate.reentry_block_reason or "campaign_abandon_cooldown")
            adaptive_allowed = bool(adaptive_eval.get("adaptive_allowed", False))
            adaptive_reason = str(adaptive_eval.get("adaptive_rejection_reason") or "adaptive_reject")
            playbook = str(adaptive_eval.get("playbook") or playbook or PLAYBOOK_NO_TRADE)
            meta["adaptive_playbook"] = str(playbook)
            meta["adaptive_sleeve"] = str(playbook_to_sleeve(playbook))
            sleeve_snapshot = (sleeve_health_snapshots or {}).get(playbook_to_sleeve(playbook))
            meta["sleeve_health_score"] = float(getattr(sleeve_snapshot, "score", 0.5))
            meta["sleeve_health_state"] = str(getattr(sleeve_snapshot, "state", "healthy"))
            meta["adaptive_entry_quality"] = float(_safe_float(adaptive_eval.get("adaptive_entry_quality", 0.0), 0.0))
            meta["adaptive_currency_crowding_penalty"] = float(_safe_float(adaptive_eval.get("currency_crowding_penalty", 0.0), 0.0))
            meta["adaptive_playbook_diversification_penalty"] = float(
                _safe_float(adaptive_eval.get("playbook_diversification_penalty", 0.0), 0.0)
            )
            _append_policy_trace(
                meta,
                stage="adaptive_playbook",
                verdict="allow" if adaptive_allowed else "block",
                reason=str(adaptive_reason),
                score=float(meta.get("adaptive_entry_quality", 0.0)),
                changed_decision=bool(adaptive_allowed != base_ready),
                details={
                    "playbook": str(playbook),
                    "sleeve": str(meta.get("adaptive_sleeve") or ""),
                    "playbook_score": float(meta.get("adaptive_playbook_score", 0.0)),
                    "location_score": float(meta.get("adaptive_location_score", 0.0)),
                    "trigger_score": float(meta.get("adaptive_trigger_score", 0.0)),
                },
            )
            campaign_candidate = evaluate_entry_campaign(
                pair=pair,
                side=str(meta.get("position_side") or current_row.get("signal_side") or "").strip().lower() or ("long" if str(decision.get("side")).upper() == "BUY" else "short"),
                sleeve=str(meta.get("adaptive_sleeve") or playbook_to_sleeve(playbook)),
                row={
                    "playbook_score": float(current_row.get("playbook_score", 0.0) or 0.0),
                    "location_score": float(current_row.get("location_score", 0.0) or 0.0),
                    "trigger_score": float(current_row.get("trigger_score", 0.0) or 0.0),
                    "macro_coherence_score": float(current_row.get("macro_coherence_score", 0.0) or 0.0),
                    "hostility_score": float(current_row.get("hostility_score", 0.0) or 0.0),
                    "extension_penalty_score": float(current_row.get("extension_penalty_score", 0.0) or 0.0),
                    "environment_state": str(current_row.get("environment_state") or ""),
                    "trade_prob": float(_safe_float(meta.get("trade_prob", current_row.get("trade_prob", 0.0)), 0.0)),
                },
                bar_idx=int(bar_index_map.get(pair, 0)),
                ts=str(meta.get("ts") or ""),
                registry=campaign_store,
                config=campaign_config,
            )
            meta["thesis_id"] = str(campaign_candidate.thesis_id)
            meta["campaign_state"] = str(campaign_candidate.state)
            meta["campaign_state_reason"] = str(campaign_candidate.state_reason)
            meta["campaign_proof_score"] = float(campaign_candidate.proof_score)
            meta["campaign_maturity_score"] = float(campaign_candidate.maturity_score)
            meta["campaign_reset_quality"] = float(campaign_candidate.reset_quality)
            meta["campaign_priority_boost"] = float(campaign_candidate.priority_boost)
            meta["campaign_reentry_blocked"] = bool(campaign_candidate.reentry_blocked)
            meta["adaptive_aggressive_fallback_used"] = bool(adaptive_eval.get("aggressive_fallback_used", False))
            if meta["adaptive_aggressive_fallback_used"]:
                aggressive_fallback_count += 1
            _append_policy_trace(
                meta,
                stage="campaign",
                verdict="allow" if str(meta.get("campaign_state") or "") not in {CAMPAIGN_STATE_ABANDONED} else "block",
                reason=str(meta.get("campaign_state_reason") or "campaign_inactive"),
                score=float(meta.get("campaign_proof_score", 0.0)),
                details={
                    "campaign_state": str(meta.get("campaign_state") or ""),
                    "campaign_proof_score": float(meta.get("campaign_proof_score", 0.0)),
                    "campaign_maturity_score": float(meta.get("campaign_maturity_score", 0.0)),
                },
            )
            overlay_inputs = _overlay_inputs_for_decision(
                meta=meta,
                current_row=current_row,
                sleeve_snapshot=sleeve_snapshot,
                open_position_count=int(open_position_count),
                allocator_open_positions=allocator_open_positions,
                settings=settings,
            )
            overlay_out = build_desk_overlay(overlay_inputs)
            overlay_outputs[int(index)] = overlay_out
            overlay_guidance = {
                key: asdict(value) for key, value in dict(getattr(overlay_out, "sleeve_budget_guidance", {}) or {}).items()
            }
            meta["conviction_score"] = float(getattr(overlay_out, "conviction_score", 0.0))
            meta["conviction_band"] = str(getattr(overlay_out, "conviction_band", ""))
            meta["thesis_stage"] = str(getattr(overlay_out, "thesis_stage", "stand_down"))
            meta["portfolio_posture"] = str(getattr(overlay_out, "portfolio_posture", "balanced_probe"))
            meta["replacement_urgency"] = float(getattr(overlay_out, "replacement_urgency", 0.0))
            primary_guidance = overlay_guidance.get(str(meta.get("adaptive_sleeve") or ""), {})
            meta["sleeve_budget_target"] = int(_safe_float(primary_guidance.get("target_share", 0.0), 0.0) * max(1, int(max_new_entries or remaining_slots)))
            meta["sleeve_budget_used"] = int(_safe_float(meta.get("sleeve_budget_used", 0), 0.0))
            meta["overlay_metadata"] = {
                "sleeve_budget_guidance": dict(overlay_guidance),
                "trace": [asdict(stage) for stage in list(getattr(overlay_out, "trace", []) or [])],
            }
            overlay_diag = dict(meta.get("overlay_diagnostics", {}) or {})
            overlay_diag.update(
                {
                    "belief_gap": float(_safe_float(meta.get("belief_gap", 0.0), 0.0)),
                    "fail_fast_risk": float(_safe_float(meta.get("belief_primary_fail_fast_prob", 0.0), 0.0)),
                    "portfolio_posture": str(meta.get("portfolio_posture") or ""),
                    "replacement_urgency": float(meta.get("replacement_urgency", 0.0)),
                }
            )
            meta["overlay_diagnostics"] = overlay_diag
            overlay_reason = "overlay_active"
            if adaptive_allowed and float(meta.get("conviction_score", 0.0)) < 0.35:
                adaptive_allowed = False
                overlay_reason = "overlay_low_conviction"
            elif adaptive_allowed and str(meta.get("thesis_stage") or "") == "stand_down":
                adaptive_allowed = False
                overlay_reason = "overlay_stand_down"
            meta["adaptive_shadow_allowed"] = bool(adaptive_allowed)
            if not adaptive_allowed and adaptive_reason in {"approved", "none"}:
                adaptive_reason = str(overlay_reason)
            _append_policy_trace(
                meta,
                stage="belief_overlay",
                verdict="allow" if adaptive_allowed else "block",
                reason=str(overlay_reason if not adaptive_allowed else meta.get("conviction_band") or "overlay_active"),
                score=float(meta.get("conviction_score", 0.0)),
                changed_decision=bool((overlay_reason != "overlay_active") and base_ready),
                details={
                    "conviction_band": str(meta.get("conviction_band") or ""),
                    "thesis_stage": str(meta.get("thesis_stage") or ""),
                    "portfolio_posture": str(meta.get("portfolio_posture") or ""),
                    "replacement_urgency": float(meta.get("replacement_urgency", 0.0)),
                },
            )
            if adaptive_allowed:
                candidates.append(
                    build_allocator_candidate(
                        candidate_id=f"{pair}:{meta.get('ts') or meta.get('runtime_ts') or index}",
                        index=int(index),
                        pair=str(pair),
                        ts=str(meta.get("ts") or ""),
                        side=str(meta.get("position_side") or current_row.get("signal_side") or ""),
                        sleeve=str(meta.get("adaptive_sleeve") or playbook_to_sleeve(playbook)),
                        environment_state=str(environment_state),
                        session_bucket=str(meta.get("session_bucket") or current_row.get("session_bucket") or ""),
                        baseline_allowed=bool(meta.get("entry_ready", False)),
                        adaptive_allowed=bool(adaptive_allowed),
                        playbook_score=float(meta.get("adaptive_playbook_score", 0.0)),
                        location_score=float(meta.get("adaptive_location_score", 0.0)),
                        trigger_score=float(meta.get("adaptive_trigger_score", 0.0)),
                        adaptive_entry_quality=float(meta.get("adaptive_entry_quality", 0.0)),
                        expected_edge_bps=float(_safe_float(meta.get("expected_edge_bps", meta.get("calibrated_ev_bps_shadow", 0.0)), 0.0)),
                        uncertainty_score=float(_safe_float(meta.get("uncertainty_score", current_row.get("uncertainty_score", 0.0)), 0.0)),
                        spread_bps=float(_safe_float(meta.get("spread_bps", current_row.get("spread_bps", 0.0)), 0.0)),
                        max_spread_bps=float(getattr(settings, "max_allowed_spread_bps", 0.0) or 0.0),
                        macro_coherence_score=float(meta.get("adaptive_macro_coherence_score", 0.0)),
                        currency_crowding_penalty=float(meta.get("adaptive_currency_crowding_penalty", 0.0)),
                        playbook_diversification_penalty=float(meta.get("adaptive_playbook_diversification_penalty", 0.0)),
                        cross_pair_rank_position=int(_safe_float(meta.get("cross_pair_rank_position"), 0.0)),
                        cross_pair_influence_score=float(_safe_float(meta.get("cross_pair_influence_score"), 0.5)),
                        cross_pair_recommendation_strength=float(_safe_float(meta.get("cross_pair_recommendation_strength"), 0.5)),
                        cross_pair_soft_block=bool(meta.get("cross_pair_soft_block", False)),
                        cross_pair_hard_block=bool(meta.get("cross_pair_hard_block", False)),
                        cross_pair_influenced_by_pairs=list(meta.get("cross_pair_influenced_by_pairs", []) or []),
                        cross_pair_reason_codes=list(meta.get("cross_pair_reason_codes", []) or []),
                        thesis_id=str(meta.get("thesis_id") or ""),
                        campaign_state=str(meta.get("campaign_state") or CAMPAIGN_STATE_INACTIVE),
                        campaign_state_reason=str(meta.get("campaign_state_reason") or ""),
                        campaign_priority_boost=float(meta.get("campaign_priority_boost", 0.0)),
                        campaign_proof_score=float(meta.get("campaign_proof_score", 0.0)),
                        campaign_maturity_score=float(meta.get("campaign_maturity_score", 0.0)),
                        campaign_reset_quality=float(meta.get("campaign_reset_quality", 0.0)),
                        campaign_reentry_blocked=bool(meta.get("campaign_reentry_blocked", False)),
                        conviction_score=float(meta.get("conviction_score", 0.0)),
                        conviction_band=str(meta.get("conviction_band") or "low"),
                        thesis_stage=str(meta.get("thesis_stage") or "stand_down"),
                        portfolio_posture=str(meta.get("portfolio_posture") or "balanced_probe"),
                        replacement_urgency=float(meta.get("replacement_urgency", 0.0)),
                        sleeve_budget_target=int(_safe_float(meta.get("sleeve_budget_target", 0), 0.0)),
                        sleeve_budget_used=int(_safe_float(meta.get("sleeve_budget_used", 0), 0.0)),
                        corr_mode=str(portfolio_corr_mode),
                        realized_returns_by_pair=realized_returns_by_pair,
                        corr_window_bars=int(getattr(settings, "portfolio_realized_corr_window_bars", 0) or 0),
                        corr_min_obs=int(getattr(settings, "portfolio_realized_corr_min_obs", 0) or 0),
                        config=allocator_config,
                        open_positions=allocator_open_positions,
                        sleeve_health=sleeve_snapshot,
                    )
                )

        meta["adaptive_shadow_rejection_reason"] = str(adaptive_reason)
        decision["metadata"] = meta
        if position_open:
            divergence_counts["open_position"] += 1
        elif not adaptive_allowed:
            rejection_reason_counts[str(adaptive_reason)] = int(rejection_reason_counts.get(str(adaptive_reason), 0)) + 1
            rejection_pair_map[str(pair)] = str(adaptive_reason)

    sleeve_budget_targets = _sleeve_budget_targets_from_overlay(
        overlays={int(item.index): overlay_outputs.get(int(item.index)) for item in candidates if overlay_outputs.get(int(item.index)) is not None},
        remaining_slots=int(max_new_entries if use_ranking else remaining_slots),
        candidate_counts=dict(Counter(str(item.sleeve) for item in candidates)),
    )
    ranked_candidates, allocator_cycle = allocate_candidates(
        candidates=list(candidates),
        open_positions=allocator_open_positions,
        remaining_slots=int(max_new_entries if use_ranking else remaining_slots),
        config=allocator_config,
        tempo_gap_active=False,
        sleeve_budget_targets=dict(sleeve_budget_targets),
    )
    ranked_indices: set[int] = set()
    for candidate in ranked_candidates:
        index = int(candidate.index)
        if index < 0 or index >= len(decisions):
            continue
        ranked_indices.add(index)
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        meta["adaptive_portfolio_rank_shadow"] = int(candidate.allocator_rank or 0)
        meta["allocator_score"] = float(candidate.allocator_score)
        meta["allocator_rank"] = int(candidate.allocator_rank or 0)
        meta["allocator_selected"] = bool(candidate.allocator_selected)
        meta["allocator_rejection_reason"] = str(candidate.allocator_rejection_reason)
        meta["replacement_candidate"] = bool(candidate.replacement_value > 0.0)
        meta["replacement_target_pair"] = str(candidate.replacement_target_pair or "")
        meta["portfolio_pair_pressure"] = float(candidate.portfolio_pair_pressure)
        meta["portfolio_session_pressure"] = float(candidate.portfolio_session_pressure)
        meta["portfolio_sleeve_pressure"] = float(candidate.portfolio_sleeve_pressure)
        meta["portfolio_correlation_pressure"] = float(candidate.portfolio_correlation_pressure)
        meta["portfolio_risk_pressure"] = float(candidate.portfolio_risk_pressure)
        meta["sleeve_health_score"] = float(candidate.sleeve_health_score)
        meta["sleeve_health_state"] = str(candidate.sleeve_health_state)
        meta["thesis_id"] = str(candidate.thesis_id)
        meta["campaign_state"] = str(candidate.campaign_state)
        meta["campaign_state_reason"] = str(candidate.campaign_state_reason)
        meta["campaign_proof_score"] = float(candidate.campaign_proof_score)
        meta["campaign_maturity_score"] = float(candidate.campaign_maturity_score)
        meta["campaign_reset_quality"] = float(candidate.campaign_reset_quality)
        meta["campaign_priority_boost"] = float(candidate.campaign_priority_boost)
        meta["campaign_reentry_blocked"] = bool(candidate.campaign_reentry_blocked)
        meta["conviction_score"] = float(candidate.conviction_score)
        meta["conviction_band"] = str(candidate.conviction_band)
        meta["thesis_stage"] = str(candidate.thesis_stage)
        meta["portfolio_posture"] = str(candidate.portfolio_posture)
        meta["replacement_urgency"] = float(candidate.replacement_urgency)
        meta["sleeve_budget_target"] = int(candidate.sleeve_budget_target)
        meta["sleeve_budget_used"] = int(candidate.sleeve_budget_used)
        adaptive_would_trade = bool(candidate.allocator_selected)
        meta["adaptive_shadow_would_trade"] = bool(adaptive_would_trade)
        meta["adaptive_shadow_rejection_reason"] = "none" if adaptive_would_trade else str(candidate.allocator_rejection_reason or "adaptive_shadow_ranked_out")
        _append_policy_trace(
            meta,
            stage="allocator",
            verdict="allow" if adaptive_would_trade else "block",
            reason=str("selected" if adaptive_would_trade else candidate.allocator_rejection_reason or "adaptive_shadow_ranked_out"),
            score=float(candidate.allocator_score),
            changed_decision=bool(not adaptive_would_trade),
            details={
                "allocator_rank": int(candidate.allocator_rank or 0),
                "sleeve_budget_target": int(candidate.sleeve_budget_target),
                "sleeve_budget_used": int(candidate.sleeve_budget_used),
                "replacement_target_pair": str(candidate.replacement_target_pair or ""),
            },
        )
        decision["metadata"] = meta
        if adaptive_would_trade:
            rejection_pair_map.pop(str(pair), None)
        else:
            reason = str(candidate.allocator_rejection_reason or "adaptive_shadow_ranked_out")
            rejection_reason_counts[reason] = int(rejection_reason_counts.get(reason, 0)) + 1
            rejection_pair_map[str(pair)] = reason

    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        if str(meta.get("adaptive_shadow_live_divergence", "")) == "open_position":
            decision["metadata"] = meta
            continue
        live_ready = bool(meta.get("entry_ready", False))
        adaptive_ready = bool(meta.get("adaptive_shadow_would_trade", False))
        if live_ready and adaptive_ready:
            divergence = "agree_ready"
        elif live_ready and not adaptive_ready:
            divergence = "live_only"
        elif adaptive_ready and not live_ready:
            divergence = "adaptive_only"
        else:
            divergence = "agree_blocked"
        divergence_counts[divergence] = int(divergence_counts.get(divergence, 0)) + 1
        meta["adaptive_shadow_live_divergence"] = str(divergence)
        overlay_diag = dict(meta.get("overlay_diagnostics", {}) or {})
        overlay_diag["final_divergence"] = str(divergence)
        meta["overlay_diagnostics"] = overlay_diag
        decision["metadata"] = meta

    sorted_rejection_counts = dict(sorted(rejection_reason_counts.items(), key=lambda item: (-item[1], item[0])))
    overlay_cycle_summary = _adaptive_overlay_summary(
        decisions=decisions,
        overlay_outputs=overlay_outputs,
        allocator_cycle={
            "sleeve_budget_targets": dict(allocator_cycle.sleeve_budget_targets),
            "sleeve_budget_used": dict(allocator_cycle.sleeve_budget_used),
            "sleeve_candidate_counts": dict(allocator_cycle.sleeve_candidate_counts),
            "pair_pressure_avg": float(allocator_cycle.pair_pressure_avg),
            "pair_pressure_max": float(allocator_cycle.pair_pressure_max),
            "session_pressure_avg": float(allocator_cycle.session_pressure_avg),
            "session_pressure_max": float(allocator_cycle.session_pressure_max),
            "sleeve_pressure_avg": float(allocator_cycle.sleeve_pressure_avg),
            "sleeve_pressure_max": float(allocator_cycle.sleeve_pressure_max),
            "correlation_pressure_avg": float(allocator_cycle.correlation_pressure_avg),
            "correlation_pressure_max": float(allocator_cycle.correlation_pressure_max),
            "risk_pressure_avg": float(allocator_cycle.risk_pressure_avg),
            "risk_pressure_max": float(allocator_cycle.risk_pressure_max),
        },
        environment_counts=environment_counts,
    )
    return {
        "adaptive_shadow_enabled": bool(shadow_enabled),
        "adaptive_shadow_candidate_count": int(len(candidates)),
        "adaptive_shadow_ranked_count": int(len(ranked_indices)),
        "adaptive_shadow_would_trade_count": int(
            sum(
                1
                for item in ranked_candidates
                if int(item.index) in ranked_indices
                and bool(decisions[int(item.index)]["metadata"].get("adaptive_shadow_would_trade", False))
            )
        ),
        "adaptive_shadow_remaining_slots": int(remaining_slots),
        "adaptive_shadow_max_new_entries": int(max_new_entries if use_ranking else remaining_slots),
        "adaptive_shadow_aggressive_fallback_count": int(aggressive_fallback_count),
        "adaptive_shadow_live_divergence_counts": dict(divergence_counts),
        "adaptive_shadow_rejection_reason_counts": dict(sorted_rejection_counts),
        "adaptive_shadow_rejections_by_pair": dict(sorted(rejection_pair_map.items())),
        "adaptive_shadow_playbook_counts": dict(sorted(playbook_counts.items())),
        "adaptive_shadow_environment_counts": dict(sorted(environment_counts.items())),
        "adaptive_shadow_dominant_rejection_reason": next(iter(sorted_rejection_counts), ""),
        "allocator_candidate_count": int(allocator_cycle.candidate_count),
        "allocator_selected_count": int(allocator_cycle.selected_count),
        "allocator_ranked_out_count": int(allocator_cycle.ranked_out_count),
        "allocator_replacement_candidate_count": int(allocator_cycle.replacement_candidate_count),
        "allocator_replacement_exit_count": int(allocator_cycle.replacement_exit_count),
        "allocator_sleeve_candidate_counts": dict(allocator_cycle.sleeve_candidate_counts),
        "allocator_sleeve_selected_counts": dict(allocator_cycle.sleeve_selected_counts),
        "allocator_sleeve_budget_targets": dict(allocator_cycle.sleeve_budget_targets),
        "allocator_sleeve_budget_used": dict(allocator_cycle.sleeve_budget_used),
        "allocator_pair_pressure_avg": float(allocator_cycle.pair_pressure_avg),
        "allocator_pair_pressure_max": float(allocator_cycle.pair_pressure_max),
        "allocator_session_pressure_avg": float(allocator_cycle.session_pressure_avg),
        "allocator_session_pressure_max": float(allocator_cycle.session_pressure_max),
        "allocator_sleeve_pressure_avg": float(allocator_cycle.sleeve_pressure_avg),
        "allocator_sleeve_pressure_max": float(allocator_cycle.sleeve_pressure_max),
        "allocator_correlation_pressure_avg": float(allocator_cycle.correlation_pressure_avg),
        "allocator_correlation_pressure_max": float(allocator_cycle.correlation_pressure_max),
        "allocator_risk_pressure_avg": float(allocator_cycle.risk_pressure_avg),
        "allocator_risk_pressure_max": float(allocator_cycle.risk_pressure_max),
        "overlay_cycle_summary": dict(overlay_cycle_summary),
        "campaign_state_counts": dict(allocator_cycle.campaign_state_counts),
    }


# AGENT HANDSHAKE: Final entry submission is the last admission gate before commands hit the broker queue.
def _finalize_entry_submissions(
    *,
    decisions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    svc: Any,
    last_action_key: dict[str, str],
    settings: Any,
    rl_portfolio_proposal: dict[str, Any] | None = None,
    adaptive_pending_entry_registry: dict[str, dict[str, Any]] | None = None,
    current_equity: float = 0.0,
    adaptive_seen_live_entry_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    adaptive_mode = bool(getattr(settings, "adaptive_execution_enabled", False)) and bool(
        getattr(settings, "adaptive_shadow_enabled", True)
    )
    strategy_engine_mode = normalize_strategy_engine_mode(getattr(settings, "strategy_engine_mode", "supervised_legacy"))
    execution_mode = "adaptive_multi_playbook" if adaptive_mode else "strict_live_mirror"
    if strategy_engine_mode == "rl_primary":
        execution_mode = "rl_primary"
    elif strategy_engine_mode == "hybrid_candidate":
        execution_mode = "hybrid_candidate"
    proposal_bundle = dict(rl_portfolio_proposal or {})
    proposal_by_pair = {
        str(pair).upper(): dict(value or {})
        for pair, value in dict(proposal_bundle.get("proposals_by_pair") or {}).items()
        if str(pair).strip()
    }
    rl_checkpoint_loaded = bool(proposal_bundle.get("checkpoint_loaded", False))
    rl_bundle_source = str(proposal_bundle.get("source") or ("rl_checkpoint" if rl_checkpoint_loaded else "supervised_fallback"))
    rl_supervised_fallback_required = bool(getattr(settings, "rl_supervised_fallback_required", True))
    approved = 0
    blocked = 0
    submitted = 0
    duplicate = 0
    rl_routed_entry_count = 0
    rl_blocked_entry_count = 0
    rl_fallback_entry_count = 0
    rl_scaled_entry_count = 0
    live_entry_registry = adaptive_pending_entry_registry if adaptive_pending_entry_registry is not None else {}
    seen_live_entry_keys = adaptive_seen_live_entry_keys if adaptive_seen_live_entry_keys is not None else set()
    submitted_live_entry_pairs: list[str] = []
    submitted_live_entry_count = 0

    for item in pending_entries:
        index = int(item.get("index", -1))
        if index < 0 or index >= len(decisions):
            continue
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        pair_key = str(item.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
        strict_ready = bool(meta.get("strict_entry_ready", meta.get("entry_ready", False)))
        strict_reasons = list(meta.get("strict_entry_blocking_reasons", meta.get("entry_blocking_reasons", [])) or [])
        adaptive_ready = bool(meta.get("adaptive_shadow_would_trade", False))
        adaptive_reason = str(meta.get("adaptive_shadow_rejection_reason") or "").strip()
        baseline_ready = bool(adaptive_ready) if adaptive_mode else bool(strict_ready)
        baseline_reason = (
            adaptive_reason or "adaptive_execution_blocked"
            if adaptive_mode
            else str(strict_reasons[0] if strict_reasons else meta.get("strict_rejection_reason") or "entry_blocked")
        )
        actual_ready = bool(baseline_ready)
        actual_reason = "none" if actual_ready else str(baseline_reason)
        actual_reasons = [] if actual_ready else [actual_reason]
        proposal_payload = dict(proposal_by_pair.get(pair_key) or {})
        meta = _apply_rl_cross_pair_proposal_metadata(meta=meta, proposal_payload=proposal_payload)
        proposal_action = dict(proposal_payload.get("action") or {})
        rl_target_position = float(_safe_float(proposal_action.get("target_position"), 0.0))
        rl_target_abs = float(abs(rl_target_position))
        rl_requested_side = "SELL" if rl_target_position < 0.0 else "BUY"
        rl_close_position = bool(proposal_action.get("close_position", False))
        rl_checkpoint_pair = bool(proposal_payload) and not bool(
            proposal_payload.get("supervised_fallback_used", proposal_bundle.get("supervised_fallback_used", True))
        )
        rl_pair_source = str(proposal_payload.get("source") or rl_bundle_source)
        rl_fallback_used = bool(proposal_payload.get("supervised_fallback_used", proposal_bundle.get("supervised_fallback_used", True)))
        rl_fallback_reason = str(
            proposal_payload.get("fallback_reason")
            or proposal_bundle.get("fallback_reason")
            or ("supervised_legacy" if strategy_engine_mode == "supervised_legacy" else "rl_supervised_fallback")
        )
        rl_supports_entry = (
            bool(proposal_payload)
            and bool(rl_checkpoint_pair)
            and not bool(rl_close_position)
            and float(rl_target_abs) >= 0.05
            and str(rl_requested_side) == str(decision.get("side") or meta.get("side") or "").upper()
        )
        rl_router_reason = "supervised_legacy"
        if strategy_engine_mode == "hybrid_candidate":
            if bool(proposal_payload) and bool(rl_checkpoint_pair):
                if bool(rl_supports_entry):
                    rl_router_reason = "rl_candidate_confirmed"
                    rl_routed_entry_count += 1
                else:
                    actual_ready = False
                    actual_reason = "rl_candidate_blocked"
                    actual_reasons = [actual_reason]
                    rl_router_reason = actual_reason
                    rl_blocked_entry_count += 1
            elif bool(rl_fallback_used):
                rl_router_reason = str(rl_fallback_reason)
                rl_fallback_entry_count += 1
        elif strategy_engine_mode == "rl_primary":
            if bool(proposal_payload) and bool(rl_checkpoint_pair):
                if bool(rl_supports_entry):
                    rl_router_reason = "rl_primary_confirmed"
                    rl_routed_entry_count += 1
                    actual_ready = bool(baseline_ready)
                    actual_reason = "none" if actual_ready else str(baseline_reason)
                    actual_reasons = [] if actual_ready else [actual_reason]
                else:
                    actual_ready = False
                    actual_reason = "rl_primary_blocked"
                    actual_reasons = [actual_reason]
                    rl_router_reason = actual_reason
                    rl_blocked_entry_count += 1
            elif bool(rl_supervised_fallback_required):
                actual_ready = bool(baseline_ready)
                actual_reason = "none" if actual_ready else str(baseline_reason)
                actual_reasons = [] if actual_ready else [actual_reason]
                rl_router_reason = str(rl_fallback_reason or "rl_primary_supervised_fallback")
                rl_fallback_entry_count += 1
                rl_fallback_used = True
            else:
                actual_ready = False
                actual_reason = "rl_primary_unavailable"
                actual_reasons = [actual_reason]
                rl_router_reason = actual_reason
                rl_blocked_entry_count += 1

        meta["execution_mode"] = str(execution_mode)
        meta["execution_entry_ready"] = bool(actual_ready)
        meta["execution_blocking_reasons"] = list(actual_reasons)
        meta["execution_rejection_reason"] = str(actual_reason)
        meta["entry_ready"] = bool(actual_ready)
        meta["entry_blocking_reasons"] = list(actual_reasons)
        meta["rejection_reason"] = str(actual_reason)
        meta["rl_proposal_source"] = str(rl_pair_source)
        meta["rl_checkpoint_loaded"] = bool(rl_checkpoint_loaded)
        meta["rl_target_position"] = float(rl_target_position)
        meta["rl_proposal_strength"] = float(rl_target_abs)
        meta["rl_requested_side"] = str(rl_requested_side)
        meta["rl_supports_entry"] = bool(rl_supports_entry)
        meta["rl_supervised_fallback_used"] = bool(rl_fallback_used)
        meta["rl_fallback_reason"] = str(rl_fallback_reason if rl_fallback_used else "")
        meta["rl_router_reason"] = str(rl_router_reason)
        meta["strategy_engine_mode"] = str(strategy_engine_mode)
        decision_source_chain = list(meta.get("decision_source_chain") or [])
        rl_chain_entry = f"rl_router:{rl_router_reason}"
        if rl_chain_entry not in decision_source_chain:
            decision_source_chain.append(rl_chain_entry)
        if rl_fallback_used:
            rl_fallback_entry = f"rl_fallback:{rl_fallback_reason}"
            if rl_fallback_entry not in decision_source_chain:
                decision_source_chain.append(rl_fallback_entry)
        meta["decision_source_chain"] = list(decision_source_chain)
        decision["execution_ready"] = bool(actual_ready)
        decision["reasons"] = list(actual_reasons)

        _append_policy_trace(
            meta,
            stage="rl_router",
            verdict="allow" if actual_ready else "block",
            reason=str(rl_router_reason),
            score=float(rl_target_abs),
            changed_decision=bool(strategy_engine_mode != "supervised_legacy"),
            details={
                "strategy_engine_mode": str(strategy_engine_mode),
                "proposal_source": str(rl_pair_source),
                "checkpoint_loaded": bool(rl_checkpoint_loaded),
                "supervised_fallback_used": bool(rl_fallback_used),
            },
        )

        if actual_ready:
            approved += 1
            meta["lifecycle_action"] = "entry"
            if strategy_engine_mode == "rl_primary" and rl_supports_entry:
                meta["lifecycle_reason"] = "rl_primary_entry_approved"
            elif strategy_engine_mode == "hybrid_candidate" and rl_supports_entry:
                meta["lifecycle_reason"] = "hybrid_candidate_entry_approved"
            else:
                meta["lifecycle_reason"] = "adaptive_entry_approved" if adaptive_mode else "entry_approved"
            _append_policy_trace(
                meta,
                stage="execution",
                verdict="allow",
                reason=str(meta.get("lifecycle_reason") or ("adaptive_entry_approved" if adaptive_mode else "entry_approved")),
                score=float(_safe_float(meta.get("conviction_score", meta.get("adaptive_entry_quality", 0.0)), 0.0)),
                changed_decision=bool(adaptive_mode or strategy_engine_mode != "supervised_legacy"),
                details={
                    "execution_mode": str(execution_mode),
                    "allocator_rank": int(_safe_float(meta.get("allocator_rank"), 0.0)),
                },
            )
            if last_action_key.get(str(item["pair"])) != str(item["action_key"]):
                approved_order = dict(item.get("approved_order") or meta.get("approved_order") or {})
                payload = (
                    _payload_from_approved_order(
                        order=approved_order,
                        pair=str(item["pair"]),
                        ts_value=str(item["ts_value"]),
                        action_tag="entry",
                    )
                    if approved_order
                    else dict(item["payload"])
                )
                if payload and bool(rl_supports_entry) and strategy_engine_mode in {"hybrid_candidate", "rl_primary"}:
                    original_lots = float(_safe_float(payload.get("lots"), 0.0))
                    max_lots = float(_safe_float(getattr(settings, "max_order_lots", 0.0), 0.0))
                    min_lot = max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01))
                    lot_step = max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01))
                    scaled_lots_raw = float(original_lots) * float(min(1.0, rl_target_abs))
                    if original_lots > 0.0 and scaled_lots_raw + 1e-9 < min_lot:
                        actual_ready = False
                        actual_reason = "rl_target_below_min_lot"
                        blocked += 1
                        approved = max(0, approved - 1)
                        rl_blocked_entry_count += 1
                        enqueue_out = {"status": "skipped", "reason": actual_reason, "action": "entry"}
                        meta["execution_entry_ready"] = False
                        meta["execution_blocking_reasons"] = [actual_reason]
                        meta["execution_rejection_reason"] = actual_reason
                        meta["entry_ready"] = False
                        meta["entry_blocking_reasons"] = [actual_reason]
                        meta["rejection_reason"] = actual_reason
                        meta["rl_router_reason"] = actual_reason
                        meta["enqueue"] = enqueue_out
                        decision["execution_ready"] = False
                        decision["reasons"] = [actual_reason]
                        decision["metadata"] = meta
                        continue
                    if original_lots > 0.0 and rl_target_abs < 0.999:
                        scaled_lots = _round_lot_size(
                            lots=scaled_lots_raw,
                            min_lot=min_lot,
                            lot_step=lot_step,
                            max_lot=min(original_lots, max_lots) if max_lots > 0.0 else original_lots,
                        )
                        payload["lots"] = float(scaled_lots)
                        approved_order["lots"] = float(scaled_lots)
                        meta["approved_order"] = dict(approved_order)
                        item["approved_order"] = dict(approved_order)
                        if float(scaled_lots) < float(original_lots):
                            rl_scaled_entry_count += 1
                            meta["rl_scaled_lots"] = float(scaled_lots)
                            meta["rl_original_lots"] = float(original_lots)
                if not payload:
                    actual_ready = False
                    actual_reason = "risk_kernel_missing_order"
                    blocked += 1
                    enqueue_out = {"status": "skipped", "reason": actual_reason, "action": "entry"}
                    meta["execution_entry_ready"] = False
                    meta["execution_blocking_reasons"] = [actual_reason]
                    meta["execution_rejection_reason"] = actual_reason
                    meta["entry_ready"] = False
                    meta["entry_blocking_reasons"] = [actual_reason]
                    meta["rejection_reason"] = actual_reason
                    decision["execution_ready"] = False
                    decision["reasons"] = [actual_reason]
                    meta["enqueue"] = enqueue_out
                    decision["metadata"] = meta
                    continue
                out, _ = svc.submit_command(payload, proto="v2")
                enqueue_out = dict(out)
                last_action_key[str(item["pair"])] = str(item["action_key"])
                submitted += 1
                enqueue_status = str(enqueue_out.get("status") or "").strip().lower()
                if enqueue_status not in {"failed", "invalid", "expired", "duplicate", "duplicate_action_skip", "skipped"}:
                    pair_key = str(item["pair"]).upper()
                    ts_key = str(item["ts_value"])
                    live_entry_registry[pair_key] = {
                        "playbook": str(meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK),
                        "sleeve": str(meta.get("adaptive_sleeve") or playbook_to_sleeve(meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK)),
                        "open_equity_usd": float(current_equity),
                        "entry_trade_prob": float(_safe_float(meta.get("trade_prob"), 0.0)),
                        "entry_session_bucket": str(meta.get("session_bucket") or ""),
                        "entry_scenario_bucket": str(meta.get("scenario_bucket") or ""),
                        "entry_regime_bucket": str(meta.get("regime_bucket") or ""),
                        "entry_uncertainty_score": float(_safe_float(meta.get("uncertainty_score"), 0.0)),
                        "entry_structure_timing_score": float(_safe_float(meta.get("structure_timing_score"), 0.0)),
                        "pair_tier": str(meta.get("pair_tier") or "tier2"),
                        "environment_state_at_entry": str(meta.get("adaptive_environment_state") or ""),
                        "entry_location_score": float(_safe_float(meta.get("adaptive_location_score"), 0.0)),
                        "entry_trigger_score": float(_safe_float(meta.get("adaptive_trigger_score"), 0.0)),
                        "entry_macro_coherence_score": float(_safe_float(meta.get("adaptive_macro_coherence_score"), 0.0)),
                        "thesis_id": str(meta.get("thesis_id") or build_thesis_id(pair_key, meta.get("position_side") or ("long" if str(item.get("side")).upper() == "BUY" else "short"), meta.get("adaptive_sleeve") or playbook_to_sleeve(meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK))),
                        "campaign_state": "probe",
                        "campaign_state_reason": "re_attack_entry" if str(meta.get("campaign_state") or "") == "re_attack_ready" else "fresh_probe",
                        "campaign_state_entered_bar": None,
                        "campaign_harvest_count": 0,
                        "campaign_reattack_count": 1 if str(meta.get("campaign_state") or "") == "re_attack_ready" else 0,
                        "campaign_abandoned_at_bar": None,
                        "sleeve_health_score": float(_safe_float(meta.get("sleeve_health_score"), 0.5)),
                        "sleeve_health_state": str(meta.get("sleeve_health_state") or "healthy"),
                        "allocator_score": float(_safe_float(meta.get("allocator_score"), 0.0)),
                        "conviction_score": float(_safe_float(meta.get("conviction_score"), 0.0)),
                        "conviction_band": str(meta.get("conviction_band") or ""),
                        "thesis_stage": str(meta.get("thesis_stage") or "stand_down"),
                        "portfolio_posture": str(meta.get("portfolio_posture") or "balanced_probe"),
                        "replacement_urgency": float(_safe_float(meta.get("replacement_urgency"), 0.0)),
                        "aggressive_fallback_used": bool(meta.get("adaptive_aggressive_fallback_used", False)),
                        "partial_count": 0,
                        "last_partial_bar_index": None,
                    }
                    submitted_live_entry_pairs.append(pair_key)
                    live_key = (pair_key, ts_key)
                    if live_key not in seen_live_entry_keys:
                        seen_live_entry_keys.add(live_key)
                        submitted_live_entry_count += 1
            else:
                enqueue_out = {"status": "duplicate_action_skip", "ts": str(item["ts_value"]), "action": "entry"}
                duplicate += 1
        else:
            blocked += 1
            meta["lifecycle_action"] = "hold"
            meta["lifecycle_reason"] = str(actual_reason)
            _append_policy_trace(
                meta,
                stage="execution",
                verdict="block",
                reason=str(actual_reason),
                score=float(_safe_float(meta.get("conviction_score", meta.get("adaptive_entry_quality", 0.0)), 0.0)),
                changed_decision=bool(adaptive_mode),
                details={"execution_mode": str(execution_mode)},
            )
            enqueue_out = {
                "status": "skipped",
                "ts": str(item["ts_value"]),
                "action": "entry",
                "reason": str(actual_reason),
            }
        meta["enqueue"] = enqueue_out
        decision["metadata"] = meta

    return {
        "execution_mode": str(execution_mode),
        "adaptive_execution_enabled": bool(adaptive_mode),
        "pending_entry_count": int(len(pending_entries)),
        "approved_entry_count": int(approved),
        "blocked_entry_count": int(blocked),
        "submitted_entry_count": int(submitted),
        "duplicate_entry_count": int(duplicate),
        "strategy_engine_mode": str(strategy_engine_mode),
        "rl_checkpoint_loaded": bool(rl_checkpoint_loaded),
        "rl_proposal_source": str(rl_bundle_source),
        "rl_routed_entry_count": int(rl_routed_entry_count),
        "rl_blocked_entry_count": int(rl_blocked_entry_count),
        "rl_fallback_entry_count": int(rl_fallback_entry_count),
        "rl_scaled_entry_count": int(rl_scaled_entry_count),
        "submitted_live_entry_count": int(submitted_live_entry_count),
        "submitted_live_entry_pairs": list(submitted_live_entry_pairs),
    }


def _apply_rl_cross_pair_proposal_metadata(
    *,
    meta: dict[str, Any],
    proposal_payload: dict[str, Any],
) -> dict[str, Any]:
    cross_pair_fields = {
        "cross_pair_rank_position": int(_safe_float(proposal_payload.get("cross_pair_rank_position"), meta.get("cross_pair_rank_position", 0))),
        "cross_pair_influence_score": float(_safe_float(proposal_payload.get("cross_pair_influence_score"), meta.get("cross_pair_influence_score", 0.5))),
        "cross_pair_recommendation_strength": float(
            _safe_float(proposal_payload.get("cross_pair_recommendation_strength"), meta.get("cross_pair_recommendation_strength", 0.5))
        ),
        "cross_pair_influenced_by_pairs": list(
            proposal_payload.get("cross_pair_influenced_by_pairs")
            or meta.get("cross_pair_influenced_by_pairs")
            or []
        ),
        "cross_pair_reason_codes": list(proposal_payload.get("cross_pair_reason_codes") or meta.get("cross_pair_reason_codes") or []),
        "cross_pair_soft_block": bool(proposal_payload.get("cross_pair_soft_block", meta.get("cross_pair_soft_block", False))),
        "cross_pair_hard_block": bool(proposal_payload.get("cross_pair_hard_block", meta.get("cross_pair_hard_block", False))),
    }
    meta.update(cross_pair_fields)
    meta["rl_cross_pair_rank_position"] = int(cross_pair_fields["cross_pair_rank_position"])
    meta["rl_cross_pair_influence_score"] = float(cross_pair_fields["cross_pair_influence_score"])
    meta["rl_cross_pair_recommendation_strength"] = float(cross_pair_fields["cross_pair_recommendation_strength"])
    meta["rl_cross_pair_influenced_by_pairs"] = list(cross_pair_fields["cross_pair_influenced_by_pairs"])
    meta["rl_cross_pair_reason_codes"] = list(cross_pair_fields["cross_pair_reason_codes"])
    meta["rl_cross_pair_soft_block"] = bool(cross_pair_fields["cross_pair_soft_block"])
    meta["rl_cross_pair_hard_block"] = bool(cross_pair_fields["cross_pair_hard_block"])
    return meta


def _apply_rl_lifecycle_router(
    *,
    decisions: list[dict[str, Any]],
    pending_position_actions: list[dict[str, Any]],
    rl_portfolio_proposal: dict[str, Any] | None,
    settings: Any,
) -> dict[str, Any]:
    strategy_engine_mode = normalize_strategy_engine_mode(getattr(settings, "strategy_engine_mode", "supervised_legacy"))
    proposal_bundle = dict(rl_portfolio_proposal or {})
    proposal_by_pair = {
        str(pair).upper(): dict(value or {})
        for pair, value in dict(proposal_bundle.get("proposals_by_pair") or {}).items()
        if str(pair).strip()
    }
    checkpoint_loaded = bool(proposal_bundle.get("checkpoint_loaded", False))
    proposal_source = str(proposal_bundle.get("source") or ("rl_checkpoint" if checkpoint_loaded else "supervised_fallback"))
    summary = {
        "strategy_engine_mode": str(strategy_engine_mode),
        "rl_lifecycle_checkpoint_loaded": bool(checkpoint_loaded),
        "rl_lifecycle_proposal_source": str(proposal_source),
        "rl_lifecycle_reviewed_count": 0,
        "rl_lifecycle_applied_count": 0,
        "rl_lifecycle_exit_count": 0,
        "rl_lifecycle_flip_exit_count": 0,
        "rl_lifecycle_resize_count": 0,
        "rl_lifecycle_tighten_stop_count": 0,
        "rl_lifecycle_preserved_exit_count": 0,
        "rl_lifecycle_fallback_count": 0,
        "rl_lifecycle_pairs": [],
    }
    if strategy_engine_mode == "supervised_legacy" or not pending_position_actions:
        return summary

    for action in pending_position_actions:
        index = int(action.get("index", -1))
        if index < 0 or index >= len(decisions):
            continue
        summary["rl_lifecycle_reviewed_count"] = int(summary["rl_lifecycle_reviewed_count"]) + 1
        decision = decisions[index]
        meta = dict(decision.get("metadata") or {})
        pair = str(action.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
        proposal_payload = dict(proposal_by_pair.get(pair) or {})
        meta = _apply_rl_cross_pair_proposal_metadata(meta=meta, proposal_payload=proposal_payload)
        proposal_action = dict(proposal_payload.get("action") or {})
        target_position = float(_safe_float(proposal_action.get("target_position"), 0.0))
        close_position = bool(proposal_action.get("close_position", False))
        tighten_stop = bool(proposal_action.get("tighten_stop", False))
        proposal_stop_loss = float(_safe_float(proposal_action.get("stop_loss"), 0.0))
        proposal_strength = float(abs(target_position))
        checkpoint_pair = bool(proposal_payload) and not bool(
            proposal_payload.get("supervised_fallback_used", proposal_bundle.get("supervised_fallback_used", True))
        )
        fallback_reason = str(
            proposal_payload.get("fallback_reason")
            or proposal_bundle.get("fallback_reason")
            or ("supervised_legacy" if strategy_engine_mode == "supervised_legacy" else "rl_lifecycle_supervised_fallback")
        )
        current_action = str(action.get("lifecycle_action") or meta.get("lifecycle_action") or "hold")
        current_reason = str(action.get("lifecycle_reason") or meta.get("lifecycle_reason") or "hold")
        lots_open = float(_safe_float(action.get("lots_open"), meta.get("lots_open", 0.0)))
        position_side = str(action.get("position_side") or meta.get("position_side") or "").strip().lower()
        opposite_direction = bool(
            (position_side == "long" and target_position < -0.05)
            or (position_side == "short" and target_position > 0.05)
        )
        same_direction = bool(
            (position_side == "long" and target_position > 0.05)
            or (position_side == "short" and target_position < -0.05)
        )
        wants_flat = bool(close_position or proposal_strength < 0.05 or opposite_direction)
        applied = False
        route_reason = "supervised_lifecycle"

        if not checkpoint_pair:
            summary["rl_lifecycle_fallback_count"] = int(summary["rl_lifecycle_fallback_count"]) + 1
            route_reason = str(fallback_reason)
        elif current_action in {"exit", "partial_tp"}:
            summary["rl_lifecycle_preserved_exit_count"] = int(summary["rl_lifecycle_preserved_exit_count"]) + 1
            route_reason = "supervised_exit_preserved"
        elif wants_flat:
            action["lifecycle_action"] = "exit"
            flip_intent_side = "SELL" if target_position < 0.0 else ("BUY" if target_position > 0.0 else "")
            if opposite_direction:
                action["lifecycle_reason"] = "rl_primary_flip_exit" if strategy_engine_mode == "rl_primary" else "hybrid_candidate_flip_exit"
                summary["rl_lifecycle_flip_exit_count"] = int(summary["rl_lifecycle_flip_exit_count"]) + 1
            else:
                action["lifecycle_reason"] = "rl_primary_close_position" if strategy_engine_mode == "rl_primary" else "hybrid_candidate_close_position"
            action["lifecycle_action_score"] = max(float(_safe_float(action.get("lifecycle_action_score"), 0.0)), proposal_strength)
            action["close_lots"] = 0.0
            action["rl_flip_intent_side"] = str(flip_intent_side)
            applied = True
            route_reason = str(action["lifecycle_reason"])
            summary["rl_lifecycle_exit_count"] = int(summary["rl_lifecycle_exit_count"]) + 1
        elif (
            current_action == "hold"
            and same_direction
            and lots_open > 0.0
            and (1.0 - proposal_strength) >= 0.20
        ):
            resize_action, resize_close_lots = _partial_close_plan(
                lots_open=float(lots_open),
                fraction=float(1.0 - proposal_strength),
                settings=settings,
            )
            if resize_action in {"partial_tp", "exit"} and resize_close_lots > 0.0:
                action["lifecycle_action"] = str(resize_action)
                action["lifecycle_reason"] = (
                    "rl_primary_resize_down" if strategy_engine_mode == "rl_primary" else "hybrid_candidate_resize_down"
                )
                action["lifecycle_action_score"] = max(float(_safe_float(action.get("lifecycle_action_score"), 0.0)), proposal_strength)
                action["close_lots"] = float(resize_close_lots)
                applied = True
                route_reason = str(action["lifecycle_reason"])
                summary["rl_lifecycle_resize_count"] = int(summary["rl_lifecycle_resize_count"]) + 1
        elif tighten_stop and proposal_stop_loss > 0.0 and current_action in {"hold", "tighten_stop"}:
            action["lifecycle_action"] = "tighten_stop"
            action["lifecycle_reason"] = "rl_primary_tighten_stop" if strategy_engine_mode == "rl_primary" else "hybrid_candidate_tighten_stop"
            action["lifecycle_action_score"] = max(float(_safe_float(action.get("lifecycle_action_score"), 0.0)), proposal_strength)
            action["sl_price"] = float(proposal_stop_loss)
            applied = True
            route_reason = str(action["lifecycle_reason"])
            summary["rl_lifecycle_tighten_stop_count"] = int(summary["rl_lifecycle_tighten_stop_count"]) + 1

        meta["rl_lifecycle_source"] = str(proposal_payload.get("source") or proposal_source)
        meta["rl_lifecycle_checkpoint_loaded"] = bool(checkpoint_loaded)
        meta["rl_lifecycle_applied"] = bool(applied)
        meta["rl_lifecycle_reason"] = str(route_reason)
        meta["rl_lifecycle_target_position"] = float(target_position)
        meta["rl_lifecycle_strength"] = float(proposal_strength)
        meta["rl_lifecycle_supervised_fallback_used"] = bool(not checkpoint_pair)
        meta["rl_flip_intent_active"] = bool(opposite_direction and applied)
        meta["rl_flip_intent_side"] = str(action.get("rl_flip_intent_side") or ("SELL" if target_position < 0.0 else ("BUY" if target_position > 0.0 else "")))
        if not checkpoint_pair:
            meta["rl_lifecycle_fallback_reason"] = str(fallback_reason)
        decision_source_chain = list(meta.get("decision_source_chain") or [])
        rl_chain_entry = f"rl_lifecycle:{route_reason}"
        if rl_chain_entry not in decision_source_chain:
            decision_source_chain.append(rl_chain_entry)
        if not checkpoint_pair:
            fallback_entry = f"rl_lifecycle_fallback:{fallback_reason}"
            if fallback_entry not in decision_source_chain:
                decision_source_chain.append(fallback_entry)
        meta["decision_source_chain"] = list(decision_source_chain)
        _append_policy_trace(
            meta,
            stage="rl_lifecycle",
            verdict="allow" if (applied or current_action in {"exit", "partial_tp"}) else "skip",
            reason=str(route_reason),
            score=float(proposal_strength),
            changed_decision=bool(applied),
            details={
                "strategy_engine_mode": str(strategy_engine_mode),
                "proposal_source": str(meta.get("rl_lifecycle_source") or ""),
                "target_position": float(target_position),
                "current_action": str(current_action),
                "fallback_used": bool(not checkpoint_pair),
            },
        )
        if applied:
            summary["rl_lifecycle_applied_count"] = int(summary["rl_lifecycle_applied_count"]) + 1
            summary["rl_lifecycle_pairs"] = list(dict.fromkeys([*list(summary["rl_lifecycle_pairs"]), pair]))
            meta["lifecycle_action"] = str(action.get("lifecycle_action") or current_action)
            meta["lifecycle_reason"] = str(action.get("lifecycle_reason") or current_reason)
            meta["close_lots"] = float(_safe_float(action.get("close_lots"), 0.0))
            meta["sl_price"] = float(_safe_float(action.get("sl_price"), 0.0))
            decision["metadata"] = meta
            _sync_lifecycle_action_payloads(decision=decision, action_item=action)
            meta = dict(decision.get("metadata") or {})
        else:
            meta["lifecycle_action"] = str(current_action)
            meta["lifecycle_reason"] = str(current_reason)
        decision["metadata"] = meta

    return summary


# AGENT STATE: Adaptive registries reconcile runtime decisions with live bridge positions so cooldowns and replacement logic persist across bars.
def _seed_adaptive_position_state(
    *,
    pair: str,
    position: dict[str, Any],
    pending_entry_registry: dict[str, dict[str, Any]],
    current_meta: dict[str, Any],
    current_row: dict[str, Any] | None,
    current_equity: float,
) -> SimpleNamespace:
    pair_key = str(pair).upper()
    seeded = dict(pending_entry_registry.pop(pair_key, {}) or {})
    row = dict(current_row or {})
    side = str(current_meta.get("position_side") or "").strip().lower()
    if side not in {"long", "short"}:
        pos_type = str(position.get("type", "")).strip()
        side = "long" if pos_type in {"0", "buy", "long"} else "short"
    return SimpleNamespace(
        pair=pair_key,
        side=str(side or "long"),
        playbook=str(seeded.get("playbook") or row.get("playbook") or current_meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK),
        sleeve=str(seeded.get("sleeve") or current_meta.get("adaptive_sleeve") or playbook_to_sleeve(seeded.get("playbook") or row.get("playbook") or current_meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK)),
        open_equity_usd=float(_safe_float(seeded.get("open_equity_usd"), current_equity)),
        entry_trade_prob=float(_safe_float(seeded.get("entry_trade_prob"), current_meta.get("trade_prob", 0.0))),
        entry_session_bucket=str(seeded.get("entry_session_bucket") or current_meta.get("session_bucket") or row.get("session_bucket") or ""),
        entry_scenario_bucket=str(seeded.get("entry_scenario_bucket") or current_meta.get("scenario_bucket") or row.get("scenario_bucket") or ""),
        entry_regime_bucket=str(seeded.get("entry_regime_bucket") or row.get("regime_bucket") or ""),
        entry_uncertainty_score=float(_safe_float(seeded.get("entry_uncertainty_score"), current_meta.get("uncertainty_score", 0.0))),
        entry_structure_timing_score=float(_safe_float(seeded.get("entry_structure_timing_score"), current_meta.get("structure_timing_score", 0.0))),
        pair_tier=str(seeded.get("pair_tier") or current_meta.get("pair_tier") or "tier2"),
        environment_state_at_entry=str(seeded.get("environment_state_at_entry") or row.get("environment_state") or current_meta.get("adaptive_environment_state") or ""),
        entry_location_score=float(_safe_float(seeded.get("entry_location_score"), row.get("location_score", current_meta.get("adaptive_location_score", 0.0)))),
        entry_trigger_score=float(_safe_float(seeded.get("entry_trigger_score"), row.get("trigger_score", current_meta.get("adaptive_trigger_score", 0.0)))),
        entry_macro_coherence_score=float(
            _safe_float(seeded.get("entry_macro_coherence_score"), row.get("macro_coherence_score", current_meta.get("adaptive_macro_coherence_score", 0.0)))
        ),
        thesis_id=str(seeded.get("thesis_id") or current_meta.get("thesis_id") or build_thesis_id(pair_key, side, seeded.get("sleeve") or current_meta.get("adaptive_sleeve") or playbook_to_sleeve(seeded.get("playbook") or row.get("playbook") or current_meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK))),
        campaign_state=str(seeded.get("campaign_state") or current_meta.get("campaign_state") or "probe"),
        campaign_state_reason=str(seeded.get("campaign_state_reason") or current_meta.get("campaign_state_reason") or ""),
        campaign_state_entered_bar=int(_safe_float(seeded.get("campaign_state_entered_bar"), 0.0)),
        campaign_harvest_count=int(_safe_float(seeded.get("campaign_harvest_count"), 0.0)),
        campaign_reattack_count=int(_safe_float(seeded.get("campaign_reattack_count"), 0.0)),
        campaign_abandoned_at_bar=seeded.get("campaign_abandoned_at_bar"),
        sleeve_health_score=float(_safe_float(seeded.get("sleeve_health_score"), current_meta.get("sleeve_health_score", 0.5))),
        sleeve_health_state=str(seeded.get("sleeve_health_state") or current_meta.get("sleeve_health_state") or "healthy"),
        allocator_score=float(_safe_float(seeded.get("allocator_score"), current_meta.get("allocator_score", 0.0))),
        conviction_score=float(_safe_float(seeded.get("conviction_score"), current_meta.get("conviction_score", 0.0))),
        conviction_band=str(seeded.get("conviction_band") or current_meta.get("conviction_band") or ""),
        thesis_stage=str(seeded.get("thesis_stage") or current_meta.get("thesis_stage") or "stand_down"),
        portfolio_posture=str(seeded.get("portfolio_posture") or current_meta.get("portfolio_posture") or "balanced_probe"),
        replacement_urgency=float(_safe_float(seeded.get("replacement_urgency"), current_meta.get("replacement_urgency", 0.0))),
        aggressive_fallback_used=bool(seeded.get("aggressive_fallback_used", current_meta.get("adaptive_aggressive_fallback_used", False))),
        partial_count=int(_safe_float(seeded.get("partial_count"), 0.0)),
        last_partial_bar_index=seeded.get("last_partial_bar_index"),
    )


def _sync_adaptive_position_registry(
    *,
    decisions: list[dict[str, Any]],
    state: dict[str, Any],
    adaptive_rows_by_pair: dict[str, dict[str, Any]],
    adaptive_pending_entry_registry: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace],
    current_equity: float,
) -> None:
    positions_by_pair: dict[str, dict[str, Any]] = {}
    for raw in list(state.get("positions", []) or []):
        pos = dict(raw or {})
        pair = str(pos.get("symbol") or "").upper()
        if pair and pair not in positions_by_pair:
            positions_by_pair[pair] = pos

    active_pairs: set[str] = set()
    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        if not pair:
            continue
        position_open = bool(int(_safe_float(meta.get("position_count_pair", 0), 0.0)) > 0 or str(meta.get("position_signature", "")).strip())
        if not position_open:
            continue
        position = dict(positions_by_pair.get(pair, {}) or {})
        if not position:
            continue
        adaptive_position_registry[pair] = _seed_adaptive_position_state(
            pair=pair,
            position=position,
            pending_entry_registry=adaptive_pending_entry_registry,
            current_meta=meta,
            current_row=adaptive_rows_by_pair.get(pair, {}),
            current_equity=float(current_equity),
        )
        active_pairs.add(pair)

    for pair in list(adaptive_position_registry.keys()):
        if str(pair).upper() not in active_pairs:
            adaptive_position_registry.pop(str(pair).upper(), None)


# AGENT HANDSHAKE: Position actions submit exits/partials before entries so freed slots are visible to the same cycle's entry finalizer.
def _submit_position_actions(
    *,
    decisions: list[dict[str, Any]],
    pending_position_actions: list[dict[str, Any]],
    svc: Any,
    last_action_key: dict[str, str],
    partial_close_tracker: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace],
    adaptive_recent_exit_registry: dict[str, dict[str, Any]],
    pair_bar_index: dict[str, int],
    loop_ts: float,
    campaign_registry: dict[str, CampaignRegistryEntry] | None = None,
    campaign_transition_counts: dict[str, int] | None = None,
    campaign_config: Any | None = None,
) -> dict[str, Any]:
    submitted = 0
    duplicate = 0
    partial_submitted = 0
    exit_submitted = 0
    adjust_submitted = 0

    for item in pending_position_actions:
        index = int(item.get("index", -1))
        if index < 0 or index >= len(decisions):
            continue
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(item.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
        ts_value = str(item.get("ts_value") or meta.get("ts") or "")
        lifecycle_action = str(item.get("lifecycle_action") or "hold")
        lifecycle_reason = str(item.get("lifecycle_reason") or "hold")
        lifecycle_action_score = float(_safe_float(item.get("lifecycle_action_score"), meta.get("lifecycle_action_score", 0.0)))
        position_signature = str(item.get("position_signature") or meta.get("position_signature") or "")
        close_lots = float(_safe_float(item.get("close_lots"), 0.0))
        sl_price = float(_safe_float(item.get("sl_price"), 0.0))
        action_tag = _lifecycle_action_tag(lifecycle_action)

        enqueue_out: dict[str, Any] = {"status": "skipped", "ts": ts_value, "action": lifecycle_action}
        if lifecycle_action not in {"exit", "tighten_stop", "partial_tp"}:
            meta["enqueue"] = enqueue_out
            decision["metadata"] = meta
            continue

        action_key = f"{action_tag}:{ts_value}"
        if last_action_key.get(pair) == action_key:
            enqueue_out = {"status": "duplicate_action_skip", "ts": ts_value, "action": lifecycle_action}
            duplicate += 1
            meta["enqueue"] = enqueue_out
            decision["metadata"] = meta
            continue

        cmd_id = _build_command_id(pair=pair, ts_value=ts_value, action_tag=action_tag)
        payload = _approved_order_for_lifecycle_action(
            pair=pair,
            ts_value=ts_value,
            lifecycle_action=lifecycle_action,
            lifecycle_reason=lifecycle_reason,
            lifecycle_action_score=lifecycle_action_score,
            close_lots=close_lots,
            sl_price=sl_price,
        )
        if lifecycle_action == "exit" and lifecycle_reason == "reversal_exit":
            payload["reversal_token"] = str(payload.get("reversal_token") or cmd_id)
        out, _ = svc.submit_command(payload, proto="v2")
        enqueue_out = dict(out)
        last_action_key[pair] = action_key
        enqueue_status = str(enqueue_out.get("status") or "").strip().lower()
        submitted += 1

        if lifecycle_action == "partial_tp":
            partial_submitted += 1
            if position_signature and enqueue_status not in {"failed", "invalid", "expired", "duplicate", "duplicate_action_skip", "skipped"}:
                partial_state = dict(partial_close_tracker.get(position_signature, {}) or {})
                partial_state["count"] = max(0, int(partial_state.get("count", 0) or 0)) + 1
                partial_state["last_partial_ts"] = float(loop_ts)
                partial_state["last_partial_cmd_id"] = str(cmd_id)
                partial_close_tracker[position_signature] = partial_state
                registry_state = adaptive_position_registry.get(pair)
                if registry_state is not None:
                    registry_state.partial_count = int(partial_state["count"])
                    registry_state.last_partial_bar_index = int(pair_bar_index.get(pair, -1))
        elif lifecycle_action == "exit":
            exit_submitted += 1
            adaptive_recent_exit_registry[pair] = {
                "bar_idx": int(pair_bar_index.get(pair, -1)),
                "side": str(item.get("position_side") or meta.get("position_side") or ""),
                "playbook": str(item.get("playbook") or meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK),
                "reason": str(lifecycle_reason),
                "thesis_id": str(item.get("thesis_id") or meta.get("thesis_id") or ""),
                "campaign_state": str(item.get("campaign_state") or meta.get("campaign_state") or ""),
            }
            if campaign_registry is not None and campaign_config is not None:
                close_campaign = campaign_state_after_close(
                    position_state=str(item.get("campaign_state") or meta.get("campaign_state") or CAMPAIGN_STATE_INACTIVE),
                    pair=pair,
                    side=str(item.get("position_side") or meta.get("position_side") or ""),
                    sleeve=str(item.get("playbook") or meta.get("adaptive_sleeve") or playbook_to_sleeve(meta.get("adaptive_playbook") or PLAYBOOK_TREND_PULLBACK)),
                    row={
                        "playbook_score": float(_safe_float(meta.get("adaptive_playbook_score"), 0.0)),
                        "location_score": float(_safe_float(meta.get("adaptive_location_score"), 0.0)),
                        "trigger_score": float(_safe_float(meta.get("adaptive_trigger_score"), 0.0)),
                        "macro_coherence_score": float(_safe_float(meta.get("adaptive_macro_coherence_score"), 0.0)),
                        "hostility_score": float(_safe_float(meta.get("adaptive_hostility_score"), 0.0)),
                        "extension_penalty_score": float(_safe_float(meta.get("extension_penalty_score"), 0.0)),
                        "environment_state": str(meta.get("adaptive_environment_state") or ""),
                    },
                    lifecycle_reason=str(lifecycle_reason),
                    realized_pnl_usd=float(_safe_float(item.get("unrealized_pnl_usd"), 0.0)),
                    bar_idx=int(pair_bar_index.get(pair, -1)),
                    ts=ts_value,
                    config=campaign_config,
                )
                transition = campaign_transition_if_changed(
                    prior_state=str(item.get("campaign_state") or meta.get("campaign_state") or CAMPAIGN_STATE_INACTIVE),
                    snapshot=close_campaign,
                    bar_idx=int(pair_bar_index.get(pair, -1)),
                    ts=ts_value,
                    realized_pnl_usd=float(_safe_float(item.get("unrealized_pnl_usd"), 0.0)),
                    holding_bars=float(_safe_float(item.get("age_bars"), 0.0)),
                )
                if transition is not None and campaign_transition_counts is not None:
                    key = f"{transition.prior_state}->{transition.new_state}"
                    campaign_transition_counts[key] = int(campaign_transition_counts.get(key, 0)) + 1
                apply_campaign_registry_snapshot(
                    campaign_registry,
                    snapshot=close_campaign,
                    bar_idx=int(pair_bar_index.get(pair, -1)),
                    ts=ts_value,
                    active_position=False,
                    realized_pnl_usd=float(_safe_float(item.get("unrealized_pnl_usd"), 0.0)),
                )
        elif lifecycle_action == "tighten_stop":
            adjust_submitted += 1

        meta["enqueue"] = enqueue_out
        meta["lifecycle_action"] = str(lifecycle_action)
        meta["lifecycle_reason"] = str(lifecycle_reason)
        decision["metadata"] = meta

    return {
        "submitted_position_action_count": int(submitted),
        "duplicate_position_action_count": int(duplicate),
        "submitted_partial_close_count": int(partial_submitted),
        "submitted_exit_count": int(exit_submitted),
        "submitted_adjust_count": int(adjust_submitted),
    }


def _apply_shadow_entry_ranking(
    decisions: list[dict[str, Any]],
    *,
    settings: Any,
    open_position_count: int,
) -> dict[str, Any]:
    divergence_counts = {"agree_ready": 0, "agree_blocked": 0, "live_only": 0, "shadow_only": 0, "open_position": 0}
    rejection_reason_counts: dict[str, int] = {}
    rejection_pair_map: dict[str, str] = {}
    structure_rescue_count = 0
    structure_rescues_by_pair: dict[str, int] = {}
    spread_pair_raw: dict[str, dict[str, Any]] = {}
    spread_session_raw: dict[str, dict[str, Any]] = {}
    secondary_spread_pair_raw: dict[str, dict[str, Any]] = {}
    secondary_spread_session_raw: dict[str, dict[str, Any]] = {}
    tier_summary = {
        "tier1": {"total": 0, "blocked": 0, "candidates": 0, "would_trade": 0},
        "tier2": {"total": 0, "blocked": 0, "candidates": 0, "would_trade": 0},
    }
    if not decisions:
        return {
            "shadow_policy_enabled": bool(getattr(settings, "shadow_policy_enabled", True)),
            "shadow_candidate_count": 0,
            "shadow_ranked_count": 0,
            "shadow_would_trade_count": 0,
            "shadow_remaining_slots": 0,
            "shadow_max_new_entries": 0,
            "shadow_live_divergence_counts": divergence_counts,
            "shadow_rejection_reason_counts": rejection_reason_counts,
            "shadow_rejections_by_pair": rejection_pair_map,
            "shadow_structure_rescue_count": 0,
            "shadow_structure_rescues_by_pair": {},
            "shadow_tier_summary": tier_summary,
            "shadow_dominant_rejection_reason": "",
            "shadow_spread_diagnostics": {
                "reject_count": 0,
                "dominant_pair": "",
                "dominant_session": "",
                "by_pair": {},
                "by_session": {},
            },
            "shadow_secondary_spread_diagnostics": {
                "reject_count": 0,
                "dominant_pair": "",
                "dominant_session": "",
                "by_pair": {},
                "by_session": {},
            },
        }

    shadow_enabled = bool(getattr(settings, "shadow_policy_enabled", True))
    remaining_slots = max(0, int(getattr(settings, "max_total_positions", 0) or 0) - int(open_position_count))
    max_new_entries_cfg = int(getattr(settings, "max_new_entries_per_cycle", 0) or 0)
    max_new_entries = remaining_slots if max_new_entries_cfg <= 0 else min(remaining_slots, max_new_entries_cfg)
    use_ranking = bool(getattr(settings, "use_portfolio_ranking", True))
    candidates: list[dict[str, Any]] = []

    for index, decision in enumerate(decisions):
        meta = dict(decision.get("metadata", {}) or {})
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        pair_tier = _shadow_pair_tier(settings, pair)
        tier_bucket = tier_summary.setdefault(str(pair_tier), {"total": 0, "blocked": 0, "candidates": 0, "would_trade": 0})
        tier_bucket["total"] = int(tier_bucket.get("total", 0)) + 1
        reasons = list(meta.get("entry_blocking_reasons", decision.get("reasons", [])) or [])
        safety_reasons = _shadow_entry_safety_reasons(reasons)
        position_open = bool(int(_safe_float(meta.get("position_count_pair", 0), 0.0)) > 0 or str(meta.get("position_signature", "")).strip())
        shadow_reason = "approved"
        portfolio_rank_shadow: int | None = None
        shadow_would_trade = False

        if not shadow_enabled:
            shadow_reason = "shadow_policy_disabled"
        elif position_open:
            shadow_reason = "shadow_position_open"
        elif safety_reasons:
            shadow_reason = str(safety_reasons[0])
        elif not bool(meta.get("shadow_floor_ok", False)):
            shadow_reason = str(meta.get("shadow_floor_rejection_reason") or "shadow_floor_reject")
        else:
            tier_bucket["candidates"] = int(tier_bucket.get("candidates", 0)) + 1
            candidates.append(
                {
                    "index": index,
                    "quality": float(_safe_float(meta.get("entry_quality_score_shadow"), 0.0)),
                    "calibrated_ev": float(_safe_float(meta.get("calibrated_ev_bps_shadow"), 0.0)),
                    "trade_prob": float(_safe_float(meta.get("trade_prob"), 0.0)),
                    "expected_edge": float(_safe_float(meta.get("expected_edge_bps"), 0.0)),
                }
            )

        meta["shadow_safety_blocking_reasons"] = list(safety_reasons)
        meta["pair_tier"] = str(pair_tier)
        meta["portfolio_rank_shadow"] = portfolio_rank_shadow
        meta["shadow_would_trade"] = bool(shadow_would_trade)
        meta["shadow_rejection_reason"] = str(shadow_reason)
        meta["shadow_live_divergence"] = "open_position" if position_open else ""
        if bool(meta.get("structure_rescue_active", False)):
            structure_rescue_count += 1
            structure_rescues_by_pair[str(pair)] = int(structure_rescues_by_pair.get(str(pair), 0)) + 1
        decision["metadata"] = meta
        if position_open:
            divergence_counts["open_position"] += 1
        elif str(shadow_reason) != "approved":
            rejection_reason_counts[str(shadow_reason)] = int(rejection_reason_counts.get(str(shadow_reason), 0)) + 1
            rejection_pair_map[str(pair)] = str(shadow_reason)
            tier_bucket["blocked"] = int(tier_bucket.get("blocked", 0)) + 1
            if str(shadow_reason) == "spread_too_wide":
                _accumulate_spread_diag(
                    pair_raw=spread_pair_raw,
                    session_raw=spread_session_raw,
                    pair=pair,
                    meta=meta,
                    decision=decision,
                )
            if "spread_too_wide" in {str(item) for item in safety_reasons}:
                _accumulate_spread_diag(
                    pair_raw=secondary_spread_pair_raw,
                    session_raw=secondary_spread_session_raw,
                    pair=pair,
                    meta=meta,
                    decision=decision,
                )

    candidates.sort(
        key=lambda item: (
            float(item.get("quality", 0.0)),
            float(item.get("calibrated_ev", 0.0)),
            float(item.get("trade_prob", 0.0)),
            float(item.get("expected_edge", 0.0)),
        ),
        reverse=True,
    )

    ranked_indices: set[int] = set()
    for rank, candidate in enumerate(candidates, start=1):
        index = int(candidate["index"])
        ranked_indices.add(index)
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        meta["portfolio_rank_shadow"] = int(rank)
        shadow_would_trade = bool(rank <= max_new_entries) if use_ranking else bool(rank <= remaining_slots)
        meta["shadow_would_trade"] = bool(shadow_would_trade)
        meta["shadow_rejection_reason"] = "none" if shadow_would_trade else "shadow_ranked_out"
        pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
        pair_tier = str(meta.get("pair_tier") or _shadow_pair_tier(settings, pair))
        tier_bucket = tier_summary.setdefault(str(pair_tier), {"total": 0, "blocked": 0, "candidates": 0, "would_trade": 0})
        if shadow_would_trade:
            tier_bucket["would_trade"] = int(tier_bucket.get("would_trade", 0)) + 1
            rejection_pair_map.pop(str(pair), None)
        else:
            rejection_reason_counts["shadow_ranked_out"] = int(rejection_reason_counts.get("shadow_ranked_out", 0)) + 1
            rejection_pair_map[str(pair)] = "shadow_ranked_out"
            tier_bucket["blocked"] = int(tier_bucket.get("blocked", 0)) + 1
        decision["metadata"] = meta

    for decision in decisions:
        meta = dict(decision.get("metadata", {}) or {})
        position_open = bool(meta.get("shadow_live_divergence") == "open_position")
        if position_open:
            decision["metadata"] = meta
            continue
        live_ready = bool(meta.get("entry_ready", False))
        shadow_ready = bool(meta.get("shadow_would_trade", False))
        if live_ready and shadow_ready:
            divergence = "agree_ready"
        elif live_ready and not shadow_ready:
            divergence = "live_only"
        elif shadow_ready and not live_ready:
            divergence = "shadow_only"
        else:
            divergence = "agree_blocked"
        divergence_counts[divergence] = int(divergence_counts.get(divergence, 0)) + 1
        meta["shadow_live_divergence"] = str(divergence)
        decision["metadata"] = meta

    spread_diag = _finalize_spread_diag(pair_raw=spread_pair_raw, session_raw=spread_session_raw)
    secondary_spread_diag = _finalize_spread_diag(
        pair_raw=secondary_spread_pair_raw,
        session_raw=secondary_spread_session_raw,
    )

    return {
        "shadow_policy_enabled": bool(shadow_enabled),
        "shadow_candidate_count": int(len(candidates)),
        "shadow_ranked_count": int(len(ranked_indices)),
        "shadow_would_trade_count": int(sum(1 for item in candidates if int(item["index"]) in ranked_indices and bool(decisions[int(item["index"])]["metadata"].get("shadow_would_trade", False)))),
        "shadow_remaining_slots": int(remaining_slots),
        "shadow_max_new_entries": int(max_new_entries if use_ranking else remaining_slots),
        "shadow_live_divergence_counts": dict(divergence_counts),
        "shadow_rejection_reason_counts": dict(sorted(rejection_reason_counts.items(), key=lambda item: (-item[1], item[0]))),
        "shadow_rejections_by_pair": dict(sorted(rejection_pair_map.items())),
        "shadow_structure_rescue_count": int(structure_rescue_count),
        "shadow_structure_rescues_by_pair": dict(sorted(structure_rescues_by_pair.items())),
        "shadow_tier_summary": {key: dict(value) for key, value in tier_summary.items()},
        "shadow_dominant_rejection_reason": next(iter(dict(sorted(rejection_reason_counts.items(), key=lambda item: (-item[1], item[0])))), ""),
        "shadow_spread_diagnostics": dict(spread_diag),
        "shadow_secondary_spread_diagnostics": dict(secondary_spread_diag),
    }


def _position_oldest_open_time(positions: list[dict[str, Any]]) -> float:
    out: list[float] = []
    for pos in positions:
        try:
            ts = float(pos.get("open_time", 0.0) or 0.0)
        except Exception:
            ts = 0.0
        if ts > 0.0:
            out.append(ts)
    return min(out) if out else 0.0


def _build_command_id(*, pair: str, ts_value: str, action_tag: str) -> str:
    ts_parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    if pd.isna(ts_parsed):
        # Keep fallback deterministic across processes and restarts.
        ts_key = hashlib.sha1(str(ts_value).encode("utf-8")).hexdigest()[:16]
    else:
        ts_key = str(int(ts_parsed.timestamp() * 1000.0))
    if str(action_tag).strip().lower() == "entry":
        return f"fxs-{pair.lower()}-{ts_key}"
    return f"fxs-{action_tag}-{pair.lower()}-{ts_key}"


def _resolve_dukascopy_csv(*, pair: str, timeframe: str) -> Path:
    s = get_settings()
    pattern = str(s.dukascopy_file_pattern or "{pair}_{granularity}.csv").strip()
    try:
        file_name = pattern.format(
            pair=str(pair).upper(),
            granularity=str(timeframe).upper(),
            timeframe=str(timeframe).upper(),
        )
    except Exception:
        file_name = f"{str(pair).upper()}_{str(timeframe).upper()}.csv"
    return Path(str(s.dukascopy_source_root)).expanduser() / file_name


def _bootstrap_pair_features_from_csv(*, store: ParquetStore, pair: str, timeframe: str) -> tuple[bool, str]:
    s = get_settings()
    provider = str(s.normalized_data_provider)
    existing = _latest_feature_row(store=store, raw_store=store, pair=pair, timeframe=timeframe)
    if not existing.empty:
        return False, "already_present"

    csv_path = _resolve_dukascopy_csv(pair=pair, timeframe=timeframe)
    if not csv_path.exists():
        return False, f"csv_missing:{csv_path}"

    try:
        from fxstack.data.ingest import ingest_dukascopy_csv, load_silver_bars
        from fxstack.features.build import build_features, leakage_guard
    except Exception as exc:
        return False, f"bootstrap_import_error:{type(exc).__name__}"

    raw_root = Path(s.project_root) / "data" / "raw"
    try:
        ingest_dukascopy_csv(
            store_root=raw_root,
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
            csv_path=csv_path,
            provider=provider,
        )
        bars = load_silver_bars(
            store_root=raw_root,
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
            provider=provider,
        )
        if bars.empty:
            return False, "raw_empty_after_ingest"
        feats = build_features(bars)
        leakage_guard(feats)
        if feats.empty:
            return False, "features_empty_after_build"
        store.write_partitioned(
            feats,
            provider=provider,
            pair=str(pair).upper(),
            timeframe=str(timeframe).upper(),
        )
        return True, f"rows={len(feats)}"
    except Exception as exc:
        return False, f"bootstrap_failed:{type(exc).__name__}"


# AGENT FLOW: `run_loop` is the live orchestrator. Startup phases build the executable model/feature graph; each cycle then scores pairs, applies parity layers, submits actions, and patches state.
def run_loop(*, equity: float, sleep_secs: int, feature_root: str) -> None:
    from fxstack.runtime.service import RuntimeService

    s = get_settings()
    pairs = list(s.pairs)
    if not pairs:
        raise RuntimeError("FXSTACK_PAIRS is empty")
    _startup_log(f"begin pairs={len(pairs)} bridge={s.mt4_bridge_url} db={s.database_url}")

    runtime_boot_id = str(uuid.uuid4())
    runtime_booted_at = pd.Timestamp.utcnow().isoformat()
    startup_state = _runtime_startup_state(
        boot_id=runtime_boot_id,
        booted_at=runtime_booted_at,
        runtime_pid=int(os.getpid()),
        phase="boot",
        pending_command_policy="purge_and_mark_stale",
    )
    manifest_seed_diag: dict[str, Any] = {}
    model_load_diag: dict[str, int] = {"model_load_timeouts": 0, "model_load_errors": 0}
    startup_inference: dict[str, dict[str, Any]] = {}
    startup_disabled_pairs: list[str] = []
    activation_consistency: dict[str, Any] = {}
    startup_runtime_diag: dict[str, Any] = {
        "pending_command_policy": "purge_and_mark_stale",
        "pending_commands_purged": 0,
        "manifest_seed": {},
        "model_load": {},
        "model_load_timeouts": 0,
        "model_load_errors": 0,
        "feature_bootstrap": {},
        "live_feature_refresh": {},
        "feature_serving": {},
        "startup_inference": {},
        "startup_inference_failures": 0,
        "startup_disabled_pairs": [],
        "activation_consistency": {},
    }
    runtime_running = False

    provider = str(s.normalized_data_provider)
    store = ParquetStore(Path(feature_root))
    raw_store = ParquetStore(Path(s.project_root) / "data" / "raw")
    regime_timeframe = str(s.regime_timeframe).upper()
    swing_timeframe = str(s.swing_timeframe).upper()
    intraday_timeframe = str(s.intraday_timeframe).upper()
    feature_timeframes = _required_feature_timeframes()
    last_action_key: dict[str, str] = {}
    partial_close_tracker: dict[str, dict[str, Any]] = {}
    adaptive_pending_entry_registry: dict[str, dict[str, Any]] = {}
    adaptive_position_registry: dict[str, SimpleNamespace] = {}
    adaptive_recent_exit_registry: dict[str, dict[str, Any]] = {}
    campaign_registry: dict[str, CampaignRegistryEntry] = {}
    campaign_transition_counts: dict[str, int] = {}
    campaign_state_counts_runtime: dict[str, int] = {}
    adaptive_last_ts_by_pair: dict[str, str] = {str(pair).upper(): "" for pair in pairs}
    adaptive_bar_index_by_pair: dict[str, int] = {str(pair).upper(): -1 for pair in pairs}
    adaptive_baseline_entry_count = 0
    adaptive_live_entry_count = 0
    adaptive_seen_baseline_entry_keys: set[tuple[str, str]] = set()
    adaptive_seen_live_entry_keys: set[tuple[str, str]] = set()
    intraday_enrichment_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
    feature_bootstrap: dict[str, dict[str, dict[str, Any]]] = {}
    live_bar_refresh_cache: dict[str, str] = {}
    live_refresh_diag: dict[str, dict[str, Any]] = {}
    adaptive_shadow_history: dict[str, list[dict[str, Any]]] = {str(pair).upper(): [] for pair in pairs}
    adaptive_shadow_playbooks = parse_enabled_playbooks(getattr(s, "adaptive_shadow_playbooks", None))
    campaign_config = campaign_config_from_settings(s)
    sleeve_tracker = SleeveGovernanceTracker(
        sleeves=[
            playbook_to_sleeve(PLAYBOOK_TREND_PULLBACK),
            playbook_to_sleeve(PLAYBOOK_RANGE_MEAN_REVERSION),
            playbook_to_sleeve(PLAYBOOK_BREAKOUT_EXPANSION),
            playbook_to_sleeve(PLAYBOOK_FAILED_BREAKOUT_REVERSAL),
            playbook_to_sleeve(PLAYBOOK_NO_TRADE),
        ]
    )
    # AGENT FLOW: Startup bootstrap owns service availability, manifest seeding, model loading, feature refresh, and dry-run scoring.
    try:
        svc = RuntimeService(
            database_url=s.database_url,
            default_session_id=s.default_session_id,
            command_ttl_secs=s.command_ttl_secs,
            requeue_age_secs=s.startup_requeue_age_secs,
            db_connect_retries=s.db_connect_retries,
        )
        _startup_log("runtime_service_ready")
        svc.patch_state(
            _runtime_boot_reset_patch(
                runtime_profile=str(s.policy_version),
                equity_seed=float(equity),
                pairs=pairs,
                startup_state=startup_state,
                runtime_diag=startup_runtime_diag,
            )
        )
        _startup_log("state_patched_boot")
        pending_purged = int(svc.purge_pending_commands(reason="runtime_restart_purged"))
        startup_runtime_diag["pending_commands_purged"] = int(pending_purged)
        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="boot",
            runtime_diag=startup_runtime_diag,
        )
        _startup_log(f"pending_commands_purged count={pending_purged}")

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="manifest_seed",
            runtime_diag=startup_runtime_diag,
        )
        manifest_seed_diag = _seed_active_model_sets_from_manifest(svc=svc, project_root=s.project_root)
        startup_runtime_diag["manifest_seed"] = dict(manifest_seed_diag)
        _startup_log(f"manifest_seed reason={manifest_seed_diag.get('reason')} seeded={manifest_seed_diag.get('seeded')}")

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="model_load",
            runtime_diag=startup_runtime_diag,
        )
        try:
            model_sets, model_load_diag = _load_model_sets(
                pairs=pairs,
                require_all=bool(s.require_active_models),
                project_root=s.project_root,
            )
        except Exception as exc:
            attached_diag = dict(getattr(exc, "model_load_diag", {}) or {})
            parsed_failure = _parse_model_load_failure_context(str(exc))
            if attached_diag:
                model_load_diag = attached_diag
            else:
                model_load_diag = {
                    "model_load_timeouts": 0,
                    "model_load_errors": 0,
                    "pairs": {},
                    "loaded_pairs": [],
                    "failed_pairs": [],
                    "degraded_pairs": [],
                }
            if not str(model_load_diag.get("failure_component") or "").strip():
                model_load_diag["failure_component"] = str(parsed_failure.get("component") or "model_load")
            if not str(model_load_diag.get("failure_pair") or "").strip():
                model_load_diag["failure_pair"] = str(parsed_failure.get("pair") or "")
            if not str(model_load_diag.get("failure_reason") or "").strip():
                model_load_diag["failure_reason"] = str(parsed_failure.get("reason") or str(exc))
            if not str(model_load_diag.get("failure_message") or "").strip():
                model_load_diag["failure_message"] = str(exc)
            startup_runtime_diag["model_load"] = dict(model_load_diag)
            startup_runtime_diag["model_load_timeouts"] = int(model_load_diag.get("model_load_timeouts", 0))
            startup_runtime_diag["model_load_errors"] = int(model_load_diag.get("model_load_errors", 0))
            _startup_log(
                "model_load_failed "
                + f"component={model_load_diag.get('failure_component') or 'model_load'} "
                + f"pair={model_load_diag.get('failure_pair') or ''} "
                + f"reason={model_load_diag.get('failure_reason') or str(exc)}"
            )
            raise
        startup_runtime_diag["model_load_timeouts"] = int(model_load_diag.get("model_load_timeouts", 0))
        startup_runtime_diag["model_load_errors"] = int(model_load_diag.get("model_load_errors", 0))
        startup_runtime_diag["model_load"] = dict(model_load_diag)
        _startup_log(
            "model_load "
            + f"loaded={len(model_sets)} "
            + f"failed={len(model_load_diag.get('failed_pairs', []))} "
            + f"degraded={len(model_load_diag.get('degraded_pairs', []))} "
            + f"timeouts={model_load_diag.get('model_load_timeouts', 0)} "
            + f"errors={model_load_diag.get('model_load_errors', 0)} "
            + f"failure_component={model_load_diag.get('failure_component') or 'none'} "
            + f"failure_pair={model_load_diag.get('failure_pair') or 'none'}"
        )
        for pair_name in list(model_load_diag.get("failed_pairs") or []):
            pair_diag = dict(dict(model_load_diag.get("pairs") or {}).get(pair_name) or {})
            _startup_log(
                "model_load_pair_failed "
                + f"pair={pair_name} "
                + f"component={pair_diag.get('failure_component') or model_load_diag.get('failure_component') or 'unknown'} "
                + f"reason={pair_diag.get('failure_reason') or model_load_diag.get('failure_reason') or 'unknown'}"
            )
        for pair_name in list(model_load_diag.get("degraded_pairs") or []):
            pair_diag = dict(dict(model_load_diag.get("pairs") or {}).get(pair_name) or {})
            _startup_log(
                "model_load_pair_degraded "
                + f"pair={pair_name} "
                + f"component={pair_diag.get('failure_component') or 'unknown'} "
                + f"reason={pair_diag.get('failure_reason') or 'unknown'}"
            )
        if bool(s.require_active_models) and len(model_sets) != len(pairs):
            missing = [p for p in pairs if p not in model_sets]
            raise RuntimeError(f"active model load failed for pairs: {','.join(missing)}")

        for index, pair in enumerate(pairs, start=1):
            startup_state = _touch_runtime_startup_progress(
                svc=svc,
                startup_state=startup_state,
                phase="initial_refresh",
                phase_pair=str(pair),
                phase_index=int(index),
                phase_total=int(len(pairs)),
                runtime_diag=startup_runtime_diag,
            )
            _startup_log(f"initial_refresh pair={pair}")
            pair_bootstrap = feature_bootstrap.setdefault(str(pair), {})
            for timeframe in feature_timeframes:
                row = _latest_feature_row(store=store, raw_store=raw_store, pair=pair, timeframe=timeframe, all_pairs=pairs)
                if row.empty:
                    ok, detail = _bootstrap_pair_features_from_csv(store=store, pair=pair, timeframe=timeframe)
                    pair_bootstrap[timeframe] = {"attempted": True, "ok": bool(ok), "detail": str(detail)}
            live_refresh_diag[pair] = _refresh_live_pair_market_data(
                bridge_url=s.mt4_bridge_url,
                raw_store=raw_store,
                feature_store=store,
                pair=pair,
                provider=provider,
                latest_bar_cache=live_bar_refresh_cache,
                svc=svc,
            )
            startup_runtime_diag["feature_bootstrap"] = dict(feature_bootstrap)
            startup_runtime_diag["live_feature_refresh"] = dict(live_refresh_diag)
            _startup_log(f"initial_refresh_done pair={pair} reason={live_refresh_diag[pair].get('reason')}")

        _startup_log("startup_inference_begin")

        def _startup_inference_progress(pair_name: str, pair_index: int, pair_total: int) -> None:
            nonlocal startup_state
            startup_state = _touch_runtime_startup_progress(
                svc=svc,
                startup_state=startup_state,
                phase="startup_inference",
                phase_pair=str(pair_name),
                phase_index=int(pair_index),
                phase_total=int(pair_total),
                runtime_diag=startup_runtime_diag,
            )

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="startup_inference",
            phase_total=int(len(pairs)),
            runtime_diag=startup_runtime_diag,
        )
        model_sets, startup_inference = _startup_inference_dry_run(
            store=store,
            raw_store=raw_store,
            pairs=pairs,
            model_sets=model_sets,
            feature_timeframes=feature_timeframes,
            regime_timeframe=regime_timeframe,
            swing_timeframe=swing_timeframe,
            intraday_timeframe=intraday_timeframe,
            progress_cb=_startup_inference_progress,
        )
        _startup_log("startup_inference_done")
        startup_disabled_pairs = sorted([pair for pair, result in startup_inference.items() if not bool(result.get("ok"))])
        startup_runtime_diag["startup_inference"] = dict(startup_inference)
        startup_runtime_diag["startup_inference_by_pair"] = dict(startup_inference)
        startup_runtime_diag["startup_inference_failures"] = int(len(startup_disabled_pairs))
        startup_runtime_diag["startup_disabled_pairs"] = list(startup_disabled_pairs)
        startup_runtime_diag["strategy_engine_mode"] = str(getattr(s, "strategy_engine_mode", "supervised_legacy") or "supervised_legacy")
        startup_runtime_diag["supervised_fallback"] = {
            "enabled": False,
            "fallback_count": 0,
            "fallback_reasons": [],
            "primary_reason": "",
        }
        startup_runtime_diag["challenger_conflict"] = {
            "mode": str(getattr(s, "challenger_conflict_mode", "off") or "off"),
            "active": False,
            "max_gap": 0.0,
            "active_pairs": [],
            "verdict_counts": {},
            "dominant_verdict": "clear",
        }
        startup_runtime_diag["pair_readiness"] = _pair_readiness_summary(
            pairs=pairs,
            startup_inference=startup_inference,
            feature_serving_by_pair=dict(sorted(((f"{pair}:{tf}", value) for (pair, tf), value in _FEATURE_SERVING_TELEMETRY.items()))),
            symbol_readiness={},
            model_load_diag=model_load_diag,
        )

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="activation_consistency",
            runtime_diag=startup_runtime_diag,
        )
        activation_consistency = _activation_consistency(
            svc=svc,
            project_root=s.project_root,
            configured_pairs=pairs,
            loaded_model_sets=model_sets,
        )
        startup_runtime_diag["activation_consistency"] = dict(activation_consistency)
        _startup_log(
            "activation_consistency "
            + f"manifest_db={activation_consistency.get('active_manifest_matches_db')} "
            + f"runtime_db={activation_consistency.get('runtime_loaded_matches_db')}"
        )

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="readying_state",
            runtime_diag=startup_runtime_diag,
        )
        _startup_log("state_patched_starting")
    except Exception as exc:
        failure_reason = f"{type(exc).__name__}:{exc}" if str(exc) else str(type(exc).__name__)
        failure_component = str(startup_state.get("failure_component") or "")
        failure_pair = str(startup_state.get("failure_pair") or "")
        if str(startup_state.get("phase") or "").strip().lower() == "model_load":
            model_load_state = dict(startup_runtime_diag.get("model_load") or {})
            failure_component = str(
                model_load_state.get("failure_component")
                or _parse_model_load_failure_context(str(exc)).get("component")
                or "model_load"
            )
            failure_pair = str(
                model_load_state.get("failure_pair")
                or _parse_model_load_failure_context(str(exc)).get("pair")
                or ""
            )
            startup_state = dict(startup_state)
            startup_state["failure_component"] = failure_component
            startup_state["failure_pair"] = failure_pair
            startup_state["failure_reason"] = str(
                model_load_state.get("failure_reason")
                or _parse_model_load_failure_context(str(exc)).get("reason")
                or failure_reason
            )
            startup_state["failed_at"] = str(pd.Timestamp(time.time(), unit="s", tz="UTC").isoformat())
        _startup_log(
            "startup_failed "
            + f"phase={startup_state.get('phase')} "
            + f"pair={startup_state.get('phase_pair')} "
            + f"component={failure_component or 'unknown'} "
            + f"failure_pair={failure_pair or 'none'} "
            + f"reason={failure_reason}"
        )
        if "svc" in locals():
            try:
                if str(startup_state.get("failure_component") or "").strip():
                    startup_runtime_diag["model_load"] = dict(startup_runtime_diag.get("model_load") or {})
                    startup_runtime_diag["model_load"]["failure_component"] = str(startup_state.get("failure_component") or "")
                    startup_runtime_diag["model_load"]["failure_pair"] = str(startup_state.get("failure_pair") or "")
                    startup_runtime_diag["model_load"]["failure_reason"] = str(startup_state.get("failure_reason") or failure_reason)
                _record_runtime_startup_failure(
                    svc=svc,
                    startup_state=startup_state,
                    failure_reason=failure_reason,
                    runtime_diag=startup_runtime_diag,
                )
            except Exception as record_exc:
                _startup_log(f"startup_failure_record_error {type(record_exc).__name__}:{record_exc}")
        raise

    # AGENT HOT PATH: Main loop refreshes bridge inputs, evaluates every pair, then performs exit-first / entry-second finalization before persisting diagnostics.
    while True:
        loop_ts = time.time()
        loop_t0 = time.perf_counter()
        if not runtime_running:
            _startup_log("main_loop_enter")
        progress_touch_t0 = time.perf_counter()
        if runtime_running:
            startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
        else:
            startup_state = _touch_runtime_startup_progress(
                svc=svc,
                startup_state=startup_state,
                phase="main_loop",
                runtime_diag=startup_runtime_diag,
            )
        provider_roles = provider_roles_from_settings(s)
        market_provider = str(provider_roles.get("market_data_provider") or "mt4_bridge")
        bridge_ready = fetch_market_ready(s.mt4_bridge_url, provider=market_provider, settings=s)
        ticks = fetch_market_ticks(s.mt4_bridge_url, provider=market_provider, settings=s)
        # AGENT HOT PATH: Refresh the on-disk feature tail from bridge bars before reading latest rows for scoring.
        for pair in pairs:
            if (time.perf_counter() - progress_touch_t0) >= 5.0:
                if runtime_running:
                    startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
                else:
                    startup_state = _touch_runtime_startup_progress(
                        svc=svc,
                        startup_state=startup_state,
                        phase="main_loop",
                        runtime_diag=startup_runtime_diag,
                    )
                progress_touch_t0 = time.perf_counter()
            tick = dict((ticks.get(pair, {}) if isinstance(ticks, dict) else {}) or {})
            bucket = _tick_bucket_start(tick=tick, timeframe=intraday_timeframe)
            if bucket is None:
                continue
            if live_bar_refresh_cache.get(str(pair).upper()) == str(pd.to_datetime(float(bucket), unit="s", utc=True)):
                continue
            live_refresh_diag[pair] = _refresh_live_pair_market_data(
                bridge_url=s.mt4_bridge_url,
                raw_store=raw_store,
                feature_store=store,
                pair=pair,
                provider=provider,
                market_provider=market_provider,
                latest_bar_cache=live_bar_refresh_cache,
                svc=svc,
            )
        state = svc.get_state()
        _prune_partial_close_tracker(partial_close_tracker, active_signatures=_active_position_signatures(state))
        governance = dict(state.get("governance", {}) or {})
        paused = bool(governance.get("paused", False))
        governance_entries_only = bool(governance.get("entries_only", False) or getattr(s, "capital_entries_only", False))
        governance_shadow_only = bool(governance.get("shadow_only", False) or getattr(s, "provider_shadow_only", False))
        governance_enabled = bool(getattr(s, "capital_governance_enabled", False))
        capital_band_mode = str(getattr(s, "capital_band_mode", "paper") or "paper").strip().lower()
        if governance_enabled and capital_band_mode == "paper":
            governance_shadow_only = True
        mt4_fresh = bool(bridge_ready.get("mt4_fresh")) if bridge_ready else _state_mt4_fresh(state)
        ticks_fresh = bool(bridge_ready.get("ticks_fresh")) if bridge_ready else bool(ticks)
        current_equity_value = _safe_float(state.get("equity"), float(equity))
        live_position_pairs = {str(dict(raw or {}).get("symbol") or "").upper() for raw in list(state.get("positions", []) or [])}
        for pair_key in list(adaptive_position_registry.keys()):
            if str(pair_key).upper() not in live_position_pairs:
                adaptive_position_registry.pop(str(pair_key).upper(), None)

        decisions: list[dict[str, Any]] = []
        pending_entries: list[dict[str, Any]] = []
        pending_position_actions: list[dict[str, Any]] = []
        rejection_counts: dict[str, int] = {}
        pair_eval_time_ms: dict[str, float] = {}
        inference_errors = 0
        adaptive_rows_by_pair: dict[str, dict[str, Any]] = {}
        planned_entry_lots, lot_sizing_diag = _entry_order_lots(state=state, settings=s, equity_seed=float(equity))
        portfolio_corr_mode = str(getattr(s, "portfolio_corr_mode", "heuristic") or "heuristic")
        realized_returns_by_pair = (
            _pair_realized_returns_by_symbol(
                store=store,
                provider=str(getattr(s, "normalized_data_provider", provider_roles.get("history_provider") or "dukascopy") or "dukascopy"),
                symbols=sorted(set([str(pair).upper() for pair in pairs] + list(live_position_pairs))),
                timeframe=str(intraday_timeframe),
                max_rows=max(64, int(getattr(s, "portfolio_realized_corr_window_bars", 64) or 64) + 8),
            )
            if portfolio_corr_mode in {"realized", "hybrid"}
            else {}
        )

        # AGENT HOT PATH: Per-pair evaluation builds the strict baseline decision first; shadow/adaptive layers only enrich or reinterpret that baseline.
        for pair in pairs:
            if (time.perf_counter() - progress_touch_t0) >= 5.0:
                if runtime_running:
                    startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
                else:
                    startup_state = _touch_runtime_startup_progress(
                        svc=svc,
                        startup_state=startup_state,
                        phase="main_loop",
                        runtime_diag=startup_runtime_diag,
                    )
                progress_touch_t0 = time.perf_counter()
            pair_t0 = time.perf_counter()
            loaded = model_sets.get(pair)
            startup_status = dict(startup_inference.get(pair) or {})
            if loaded is None:
                reason = str(startup_status.get("reason") or "missing_active_model_set")
                if startup_status and not bool(startup_status.get("ok")) and not str(reason).startswith("startup_"):
                    reason = f"startup_{reason}"
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": {
                            "pair": pair,
                            "runtime": "fxstack",
                            "startup_inference": startup_status,
                            "challenger_conflict_mode": str(getattr(s, "challenger_conflict_mode", "off") or "off"),
                        },
                    }
                )
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue

            pair_rows: dict[str, pd.DataFrame] = {}
            pair_bootstrap = feature_bootstrap.setdefault(str(pair), {})
            missing_frames: list[str] = []
            for timeframe in feature_timeframes:
                row = _latest_feature_row(
                    store=store,
                    raw_store=raw_store,
                    pair=pair,
                    timeframe=timeframe,
                    all_pairs=pairs,
                    feature_service_name=_loaded_feature_service_name(
                        loaded,
                        pair=pair,
                        timeframe=timeframe,
                        regime_timeframe=regime_timeframe,
                        swing_timeframe=swing_timeframe,
                        intraday_timeframe=intraday_timeframe,
                    ),
                )
                if row.empty and not bool((pair_bootstrap.get(timeframe) or {}).get("attempted")):
                    ok, detail = _bootstrap_pair_features_from_csv(store=store, pair=pair, timeframe=timeframe)
                    pair_bootstrap[timeframe] = {"attempted": True, "ok": bool(ok), "detail": str(detail)}
                    row = _latest_feature_row(
                        store=store,
                        raw_store=raw_store,
                        pair=pair,
                        timeframe=timeframe,
                        all_pairs=pairs,
                        feature_service_name=_loaded_feature_service_name(
                            loaded,
                            pair=pair,
                            timeframe=timeframe,
                            regime_timeframe=regime_timeframe,
                            swing_timeframe=swing_timeframe,
                            intraday_timeframe=intraday_timeframe,
                        ),
                    )
                if row.empty:
                    missing_frames.append(timeframe)
                else:
                    pair_rows[timeframe] = row
            if missing_frames:
                reason = f"no_features:{','.join(missing_frames)}"
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                meta = {"pair": pair, "runtime": "fxstack"}
                if pair_bootstrap:
                    meta["feature_bootstrap"] = dict(pair_bootstrap)
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": meta,
                    }
                )
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue

            pair_rows = _prepare_pair_rows_for_scoring(
                raw_store=raw_store,
                pair=pair,
                loaded=loaded,
                pair_rows=pair_rows,
                regime_timeframe=regime_timeframe,
                swing_timeframe=swing_timeframe,
                intraday_timeframe=intraday_timeframe,
                all_pairs=pairs,
                intraday_cache=intraday_enrichment_cache,
            )
            regime_row = pair_rows[regime_timeframe]
            swing_row = pair_rows[swing_timeframe]
            intraday_row = pair_rows[intraday_timeframe]
            tick = dict((ticks.get(pair, {}) if isinstance(ticks, dict) else {}) or {})
            spread_bps, spread_unit_source = normalize_spread_bps(tick=tick, row=intraday_row.iloc[0], pair=pair)

            try:
                signal = loaded.scorer.score(
                    regime_row=regime_row,
                    swing_row=swing_row,
                    intraday_row=intraday_row,
                    meta_row=intraday_row,
                    spread_bps=float(spread_bps),
                    expected_edge_bps=None,
                    spread_unit_source=str(spread_unit_source),
                )
            except Exception as exc:
                reason = f"model_inference_error:{type(exc).__name__}"
                inference_errors += 1
                rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1
                decisions.append(
                    {
                        "symbol": pair,
                        "side": "N/A",
                        "score": 0.0,
                        "confidence": 0.0,
                        "execution_ready": False,
                        "reasons": [reason],
                        "metadata": {
                            "pair": pair,
                            "runtime": "fxstack",
                            "error": str(exc),
                            "challenger_conflict_mode": str(getattr(s, "challenger_conflict_mode", "off") or "off"),
                        },
                    }
                )
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue
            expected_edge_bps = float(signal.expected_edge_bps)
            swing_route = loaded.swing_router.diagnostics()
            intraday_route = loaded.intraday_router.diagnostics()
            challenger_shadow = _sequence_shadow_metrics(
                loaded=loaded,
                swing_row=swing_row,
                intraday_row=intraday_row,
                signal=signal,
            )
            challenger_conflict = _challenger_conflict_payload(
                disagreement=dict(challenger_shadow.get("disagreement") or {}),
                mode=str(getattr(s, "challenger_conflict_mode", "off") or "off"),
            )
            challenger_conflict_mode = str(challenger_conflict.get("mode") or "off")
            decision_reasons: list[str] = []

            positions = _pair_positions(state, pair=pair)
            pair_count, total_count = _state_position_counts(state, pair=pair)
            portfolio_positions = list(state.get("positions", []) or [])
            portfolio_total_count = int(len(portfolio_positions))
            pos_side = _position_side(positions)
            position_signature = _position_signature(dict(positions[0] or {})) if positions else ""
            ts_value = str(intraday_row.iloc[0].get("ts", ""))
            pair_key = str(pair).upper()
            if str(ts_value) != str(adaptive_last_ts_by_pair.get(pair_key, "")):
                adaptive_bar_index_by_pair[pair_key] = int(adaptive_bar_index_by_pair.get(pair_key, -1)) + 1
                adaptive_last_ts_by_pair[pair_key] = str(ts_value)
            feature_bar = _feature_bar_freshness(
                ts_value=ts_value,
                loop_ts=float(loop_ts),
                timeframe=str(intraday_timeframe),
            )
            if not positions and not mt4_fresh:
                decision_reasons.append("mt4_stale")
            if not positions and not ticks_fresh:
                decision_reasons.append("tick_feed_stale")
            if not positions and not bool(tick):
                decision_reasons.append("missing_live_tick")
            if not positions and bool(feature_bar.get("stale")):
                decision_reasons.append(str(feature_bar.get("reason") or "stale_feature_bar"))
            if not positions and bool(signal.session_entry_blocked):
                decision_reasons.append(str(signal.session_entry_block_reason or f"session_blocked:{signal.session_bucket}"))
            if not bool(signal.allowed):
                decision_reasons.append(str(signal.rejection_reason))
            if not positions and challenger_conflict_mode != "telemetry" and str(challenger_conflict.get("verdict") or "") == "soft_conflict":
                decision_reasons.append("challenger_conflict_soft")
            if not positions and challenger_conflict_mode != "telemetry" and str(challenger_conflict.get("verdict") or "") == "hard_conflict":
                decision_reasons.append("challenger_conflict_hard")
            if str(spread_unit_source) == "missing":
                decision_reasons.append("missing_spread_input")
            if paused:
                decision_reasons.append("governance_paused")
            if not positions and governance_entries_only:
                decision_reasons.append("governance_entries_only")
            if not positions and governance_shadow_only:
                decision_reasons.append("governance_shadow_only")
            if pair_count >= int(s.max_pair_positions):
                decision_reasons.append("pair_exposure_cap")
            if total_count >= int(s.max_total_positions):
                decision_reasons.append("portfolio_exposure_cap")

            # Keep reasons unique while preserving evaluation order.
            decision_reasons = list(dict.fromkeys(decision_reasons))
            ready = len(decision_reasons) == 0
            side = "BUY" if str(signal.side).lower() == "long" else "SELL"
            desired_side = "long" if side == "BUY" else "short"
            reversal_blocking_reasons = _reversal_blocking_reasons(decision_reasons)
            reversal_context_active = (
                desired_side != "flat" and str(pos_side) != "flat" and desired_side != str(pos_side)
            )
            lifecycle_soft_degrade_reasons: list[str] = []
            if not bool(loaded.has_exit_model):
                lifecycle_soft_degrade_reasons.append("no_exit_model")
            if not bool(loaded.has_reversal_models):
                lifecycle_soft_degrade_reasons.append("no_reversal_model")

            enqueue_out: dict[str, Any] = {"status": "skipped"}
            lifecycle_action = "hold"
            lifecycle_action_score = 0.0
            lifecycle_reason = "hold"
            action_tag = "hold"
            close_lots = 0.0
            sl_price = 0.0
            partial_tp_count = 0
            partial_tp_next_eligible_secs = 0.0
            partial_tp_blocked_reason = ""
            lifecycle_row = _build_lifecycle_row(
                row=intraday_row,
                positions=positions,
                total_position_count=total_count,
                loop_ts=float(loop_ts),
                timeframe=str(intraday_timeframe),
            )
            exit_action_selected = "hold"
            exit_action_score = 0.0
            exit_action_probs: dict[str, float] = {}
            reversal_failure_prob = 0.0
            reversal_opportunity_prob = 0.0
            lifecycle_inference_error = ""

            if positions and bool(s.enable_lifecycle_actions):
                try:
                    if loaded.exit_model is not None:
                        exit_diag = _score_exit_policy_model(
                            loaded.exit_model,
                            lifecycle_row,
                            action_labels=loaded.exit_action_labels,
                        )
                        exit_action_selected = str(exit_diag.get("selected") or "hold")
                        exit_action_score = float(exit_diag.get("score") or 0.0)
                        exit_action_probs = {
                            str(k): float(v) for k, v in dict(exit_diag.get("probs") or {}).items()
                        }
                    if loaded.reversal_failure_model is not None:
                        reversal_failure_prob = _score_binary_lifecycle_model(loaded.reversal_failure_model, lifecycle_row)
                    if loaded.reversal_opportunity_model is not None:
                        reversal_opportunity_prob = _score_binary_lifecycle_model(
                            loaded.reversal_opportunity_model,
                            lifecycle_row,
                        )
                except Exception as exc:
                    lifecycle_inference_error = f"{type(exc).__name__}:{exc}"
                    lifecycle_soft_degrade_reasons.append(f"lifecycle_inference_error:{type(exc).__name__}")

            if reversal_context_active and loaded.has_reversal_models:
                if float(reversal_failure_prob) < float(s.reversal_failure_min_prob):
                    reversal_blocking_reasons.append("reversal_failure_below_threshold")
                if float(reversal_opportunity_prob) < float(s.reversal_opportunity_min_prob):
                    reversal_blocking_reasons.append("reversal_opportunity_below_threshold")
            reversal_blocking_reasons = list(dict.fromkeys(reversal_blocking_reasons))
            reversal_ready = (
                bool(reversal_context_active)
                and bool(signal.allowed)
                and len(reversal_blocking_reasons) == 0
                and (
                    not loaded.has_reversal_models
                    or (
                        float(reversal_failure_prob) >= float(s.reversal_failure_min_prob)
                        and float(reversal_opportunity_prob) >= float(s.reversal_opportunity_min_prob)
                    )
                )
            )

            # Action precedence:
            # 1) hard risk/time-stop emergency
            # 2) reversal-exit decision
            # 3) exit-policy action
            # 4) adjust-stop action
            # 4) entry (flat only)
            if positions and float(s.hard_time_stop_secs) > 0.0:
                oldest_open_time = _position_oldest_open_time(positions)
                if oldest_open_time > 0.0 and (float(loop_ts) - float(oldest_open_time)) >= float(s.hard_time_stop_secs):
                    lifecycle_action = "exit"
                    lifecycle_action_score = 1.0
                    lifecycle_reason = "hard_time_stop"
                    action_tag = "exit"
            if positions and lifecycle_action == "hold" and bool(s.enable_lifecycle_actions):
                if bool(reversal_ready):
                    lifecycle_action = "exit"
                    lifecycle_action_score = float(
                        min(
                            1.0,
                            (float(reversal_failure_prob) + float(reversal_opportunity_prob) + float(signal.trade_prob)) / 3.0,
                        )
                    )
                    lifecycle_reason = "reversal_models_exit"
                    action_tag = "reversal_exit"
            if (
                positions
                and lifecycle_action == "hold"
                and bool(s.enable_lifecycle_actions)
                and bool(loaded.has_exit_model)
            ):
                if (
                    str(exit_action_selected) in {"partial_tp", "exit"}
                    and float(exit_action_score) >= float(s.lifecycle_model_action_min_prob)
                ):
                    first_pos = dict(positions[0] or {})
                    lots_open = float(first_pos.get("lots", 0.0) or 0.0)
                    if str(exit_action_selected) == "partial_tp":
                        tracker_state = dict(partial_close_tracker.get(position_signature, {}) or {})
                        partial_tp_count = max(0, int(tracker_state.get("count", 0) or 0))
                        allow_partial_tp, partial_tp_blocked_reason, partial_tp_next_eligible_secs = _partial_close_guard(
                            tracker_state=tracker_state,
                            loop_ts=float(loop_ts),
                            settings=s,
                        )
                        if allow_partial_tp:
                            lifecycle_action, close_lots = _partial_close_plan(
                                lots_open=lots_open,
                                fraction=float(s.partial_close_fraction),
                                settings=s,
                            )
                            if close_lots > 0.0 and lifecycle_action in {"partial_tp", "exit"}:
                                lifecycle_action_score = float(exit_action_score)
                                lifecycle_reason = (
                                    "exit_model_reduce_to_flat" if lifecycle_action == "exit" else "exit_model_partial_tp"
                                )
                                action_tag = "exit" if lifecycle_action == "exit" else "close_partial"
                        else:
                            lifecycle_reason = str(partial_tp_blocked_reason)
                    elif str(exit_action_selected) == "exit":
                        lifecycle_action = "exit"
                        lifecycle_action_score = float(exit_action_score)
                        lifecycle_reason = "exit_model_exit"
                        action_tag = "exit"
            if (
                positions
                and lifecycle_action == "hold"
                and bool(s.enable_lifecycle_actions)
                and not bool(loaded.has_exit_model)
                and float(signal.trade_prob) < float(s.min_trade_prob * 0.8)
            ):
                first_pos = dict(positions[0] or {})
                lots_open = float(first_pos.get("lots", 0.0) or 0.0)
                tracker_state = dict(partial_close_tracker.get(position_signature, {}) or {})
                partial_tp_count = max(0, int(tracker_state.get("count", 0) or 0))
                allow_partial_tp, partial_tp_blocked_reason, partial_tp_next_eligible_secs = _partial_close_guard(
                    tracker_state=tracker_state,
                    loop_ts=float(loop_ts),
                    settings=s,
                )
                if allow_partial_tp:
                    lifecycle_action, close_lots = _partial_close_plan(
                        lots_open=lots_open,
                        fraction=float(s.partial_close_fraction),
                        settings=s,
                    )
                    if close_lots > 0.0 and lifecycle_action in {"partial_tp", "exit"}:
                        lifecycle_action_score = 0.6
                        lifecycle_reason = (
                            "exit_model_reduce_to_flat" if lifecycle_action == "exit" else "exit_model_reduce"
                        )
                        action_tag = "exit" if lifecycle_action == "exit" else "close_partial"
                else:
                    lifecycle_reason = str(partial_tp_blocked_reason)
            if (
                positions
                and lifecycle_action == "hold"
                and bool(s.enable_adjust_actions)
                and float(s.adjust_stop_buffer_pips) > 0.0
            ):
                bid = float(tick.get("bid", 0.0) or 0.0)
                ask = float(tick.get("ask", 0.0) or 0.0)
                if bid > 0.0 and ask > 0.0 and str(pos_side) in {"long", "short"}:
                    pip_size = infer_pip_size(pair=pair, digits=int(float(tick.get("digits", 0.0) or 0.0)) or None)
                    px_buffer = float(s.adjust_stop_buffer_pips) * float(pip_size)
                    sl_price = (bid - px_buffer) if str(pos_side) == "long" else (ask + px_buffer)
                    lifecycle_action = "tighten_stop"
                    lifecycle_action_score = 0.5
                    lifecycle_reason = "adjust_stop_buffer"
                    action_tag = "adjust_sl"
            if not positions:
                reversal_ready = False

            raw_policy_suggestion = {
                "side": str(side),
                "expected_edge_bps": float(expected_edge_bps),
                "trade_prob": float(signal.trade_prob),
                "allowed": bool(ready),
                "rejection_reasons": list(decision_reasons),
                "lifecycle_action_requested": str(lifecycle_action),
                "lifecycle_reason_requested": str(lifecycle_reason),
                "close_lots_requested": float(close_lots),
                "sl_price_requested": float(sl_price),
            }
            risk_kernel_out = _evaluate_runtime_risk_kernel(
                pair=pair,
                ts_value=ts_value,
                side=side,
                signal=signal,
                expected_edge_bps=float(expected_edge_bps),
                spread_bps=float(spread_bps),
                feature_bar=dict(feature_bar),
                tick=dict(tick),
                spread_unit_source=str(spread_unit_source),
                mt4_fresh=bool(mt4_fresh),
                ticks_fresh=bool(ticks_fresh),
                paused=bool(paused),
                positions=list(positions),
                pair_count=int(pair_count),
                total_count=int(portfolio_total_count),
                current_equity=float(current_equity_value),
                planned_entry_lots=float(planned_entry_lots),
                lifecycle_action=str(lifecycle_action),
                lifecycle_reason=str(lifecycle_reason),
                lifecycle_action_score=float(lifecycle_action_score),
                close_lots=float(close_lots),
                sl_price=float(sl_price),
                rejection_reasons=list(decision_reasons),
                state=dict(state),
                settings=s,
                portfolio_positions=list(portfolio_positions),
                rollout_policy=dict(getattr(loaded, "rollout_policy", {}) or {}),
                governance_policy={
                    "capital_band": str(capital_band_mode),
                    "mode": "paused" if paused else ("entries_only" if governance_entries_only else ("shadow_only" if governance_shadow_only else "normal")),
                    "paused": bool(paused),
                    "entries_only": bool(governance_entries_only),
                    "shadow_only": bool(governance_shadow_only),
                    "budget_scale": float(capital_band_budget_scale(str(capital_band_mode), s)),
                },
                pending_entries=list(pending_entries),
                realized_returns_by_pair=realized_returns_by_pair,
            )
            approved_order_payload = dict(risk_kernel_out.get("approved_order") or {})
            rollout_meta = dict(risk_kernel_out.get("rollout") or {})
            portfolio_allocation_meta = dict(risk_kernel_out.get("portfolio_allocation") or {})
            capital_governance_meta = dict(risk_kernel_out.get("governance") or {})
            if positions:
                lifecycle_action = str(risk_kernel_out.get("lifecycle_action") or lifecycle_action)
                close_lots = float(_safe_float(risk_kernel_out.get("close_lots"), close_lots))
            strict_entry_ready = bool(ready and approved_order_payload) if not positions else bool(ready)
            strict_entry_reasons = list(decision_reasons)
            if not positions and not strict_entry_ready and not strict_entry_reasons:
                strict_entry_reasons = [str(risk_kernel_out.get("reason") or "risk_kernel_blocked")]

            action_key = f"{action_tag}:{ts_value}"
            if lifecycle_action in {"exit", "tighten_stop", "partial_tp"}:
                enqueue_out = {"status": "pending_cycle_eval", "ts": ts_value, "action": lifecycle_action}
                pending_position_actions.append(
                    {
                        "index": int(len(decisions)),
                        "pair": str(pair_key),
                        "ts_value": str(ts_value),
                        "action_key": str(action_key),
                        "position_signature": str(position_signature),
                        "position_side": str(pos_side),
                        "lifecycle_action": str(lifecycle_action),
                        "lifecycle_reason": str(lifecycle_reason),
                        "lifecycle_action_score": float(lifecycle_action_score),
                        "close_lots": float(close_lots),
                        "sl_price": float(sl_price),
                        "baseline_lifecycle_action": str(lifecycle_action),
                        "baseline_lifecycle_reason": str(lifecycle_reason),
                        "baseline_close_lots": float(close_lots),
                        "lots_open": float(_safe_float(dict(positions[0] or {}).get("lots"), 0.0)) if positions else 0.0,
                        "age_bars": float(_safe_float(lifecycle_row.iloc[0].get("time_in_trade_bars", 0.0), 0.0)),
                        "unrealized_pnl_usd": float(_safe_float(dict(positions[0] or {}).get("profit"), 0.0)) if positions else 0.0,
                        "exit_action_probs": dict(exit_action_probs),
                        "reversal_context_active": bool(reversal_context_active),
                        "reversal_ready": bool(reversal_ready),
                        "reversal_failure_prob": float(reversal_failure_prob),
                        "reversal_opportunity_prob": float(reversal_opportunity_prob),
                        "approved_order": dict(approved_order_payload),
                    }
                )
            elif not positions:
                lifecycle_action = "entry"
                lifecycle_action_score = float(signal.trade_prob)
                lifecycle_reason = "entry_approved" if ready else "entry_pending_eval"
                action_key = f"entry:{ts_value}"
                payload = (
                    _payload_from_approved_order(order=approved_order_payload, pair=pair, ts_value=ts_value, action_tag="entry")
                    if approved_order_payload
                    else {}
                )
                enqueue_out = {"status": "pending_cycle_eval", "ts": ts_value, "action": "entry"}
                pending_entries.append(
                    {
                        "index": int(len(decisions)),
                        "pair": str(pair),
                        "ts_value": str(ts_value),
                        "action_key": str(action_key),
                        "payload": payload,
                        "approved_order": dict(approved_order_payload),
                    }
                )
            elif positions:
                lifecycle_reason = "position_open_hold"
                if not loaded.has_exit_model:
                    lifecycle_reason = "no_exit_model"
                    lifecycle_soft_degrade_reasons.append("no_exit_model")
                if not loaded.has_reversal_models:
                    lifecycle_soft_degrade_reasons.append("no_reversal_model")
                enqueue_out = {"status": "skipped", "ts": ts_value, "action": "hold"}

            if positions and not any(int(item.get("index", -1)) == int(len(decisions)) for item in pending_position_actions):
                pending_position_actions.append(
                    {
                        "index": int(len(decisions)),
                        "pair": str(pair_key),
                        "ts_value": str(ts_value),
                        "action_key": str(action_key),
                        "position_signature": str(position_signature),
                        "position_side": str(pos_side),
                        "lifecycle_action": str(lifecycle_action),
                        "lifecycle_reason": str(lifecycle_reason),
                        "lifecycle_action_score": float(lifecycle_action_score),
                        "close_lots": float(close_lots),
                        "sl_price": float(sl_price),
                        "baseline_lifecycle_action": str(lifecycle_action),
                        "baseline_lifecycle_reason": str(lifecycle_reason),
                        "baseline_close_lots": float(close_lots),
                        "lots_open": float(_safe_float(dict(positions[0] or {}).get("lots"), 0.0)) if positions else 0.0,
                        "age_bars": float(_safe_float(lifecycle_row.iloc[0].get("time_in_trade_bars", 0.0), 0.0)),
                        "unrealized_pnl_usd": float(_safe_float(dict(positions[0] or {}).get("profit"), 0.0)) if positions else 0.0,
                        "exit_action_probs": dict(exit_action_probs),
                        "reversal_context_active": bool(reversal_context_active),
                        "reversal_ready": bool(reversal_ready),
                        "reversal_failure_prob": float(reversal_failure_prob),
                        "reversal_opportunity_prob": float(reversal_opportunity_prob),
                        "approved_order": dict(approved_order_payload),
                    }
                )

            if not ready:
                for reason in decision_reasons:
                    rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

            adaptive_snapshot = _adaptive_shadow_row_snapshot(
                pair=pair,
                intraday_row=intraday_row,
                signal=signal,
                spread_bps=float(spread_bps),
                max_spread_bps=float(s.max_allowed_spread_bps),
                ts_value=ts_value,
                loop_ts=float(loop_ts),
                baseline_rejection_reason="none" if ready else str(decision_reasons[0]),
            )

            decisions.append(
                {
                    "symbol": pair,
                    "side": side,
                    "score": float(expected_edge_bps),
                    "confidence": float(max(0.0, min(100.0, signal.trade_prob * 100.0))),
                    "execution_ready": bool(ready),
                    "reasons": decision_reasons,
                    "metadata": {
                        "model_set_id": loaded.model_set_id,
                        "registry_path": loaded.registry_path,
                        "pair": pair,
                        "ts": ts_value,
                        "regime_prob": float(signal.regime_prob),
                        "swing_prob": float(signal.swing_prob),
                        "entry_prob": float(signal.entry_prob),
                        "trade_prob": float(signal.trade_prob),
                        "spread_bps": float(spread_bps),
                        "tick_available": bool(tick),
                        "mt4_fresh": bool(mt4_fresh),
                        "ticks_fresh": bool(ticks_fresh),
                        "expected_edge_bps": float(expected_edge_bps),
                        "policy_version": str(signal.policy_version),
                        "edge_formula_id": str(signal.edge_formula_id),
                        "threshold_snapshot": dict(signal.threshold_snapshot),
                        "spread_unit_source": str(signal.spread_unit_source),
                        "scenario_bucket": str(signal.scenario_bucket),
                        "context_frame_profile": str(signal.context_frame_profile or s.frame_profile),
                        "uncertainty_score": float(signal.uncertainty_score),
                        "directional_swing_confidence": float(signal.directional_swing_confidence),
                        "model_intelligence_score": float(signal.model_intelligence_score),
                        "heuristic_penalty_score": float(signal.heuristic_penalty_score),
                        "entry_margin": float(signal.entry_margin),
                        "meta_margin": float(signal.meta_margin),
                        "model_disagreement_score": float(signal.model_disagreement_score),
                        "htf_alignment_score": float(signal.htf_alignment_score),
                        "pullback_quality_score": float(signal.pullback_quality_score),
                        "resume_trigger_score": float(signal.resume_trigger_score),
                        "extension_penalty_score": float(signal.extension_penalty_score),
                        "structure_timing_score": float(signal.structure_timing_score),
                        "structure_bonus_bps": float(signal.structure_bonus_bps),
                        "chase_penalty_bps": float(signal.chase_penalty_bps),
                        "calibrated_ev_bps_shadow": float(signal.calibrated_ev_bps_shadow),
                        "entry_quality_score_shadow": float(signal.entry_quality_score_shadow),
                        "structure_rescue_active": bool(signal.structure_rescue_active),
                        "fallback_used": bool(signal.fallback_used),
                        "fallback_reason": str(signal.fallback_reason),
                        "decision_source_chain": list(signal.decision_source_chain),
                        "shadow_floor_ok": bool(signal.shadow_floor_ok),
                        "shadow_floor_rejection_reason": str(signal.shadow_floor_rejection_reason),
                        "session_bucket": str(signal.session_bucket),
                        "session_entry_blocked": bool(signal.session_entry_blocked),
                        "session_entry_block_reason": str(signal.session_entry_block_reason),
                        **{k: v for k, v in signal.to_dict().items() if str(k).startswith("belief_")},
                        "swing_policy": swing_route.get("policy"),
                        "swing_model_selected": swing_route.get("selected_model"),
                        "swing_fallback_reason": swing_route.get("fallback_reason"),
                        "intraday_policy": intraday_route.get("policy"),
                        "intraday_model_selected": intraday_route.get("selected_model"),
                        "intraday_fallback_reason": intraday_route.get("fallback_reason"),
                        "challenger_shadow_enabled": bool(challenger_shadow.get("enabled", False)),
                        "challenger_shadow_available": bool(challenger_shadow.get("available", False)),
                        "challenger_shadow_bundle_run_id": str(challenger_shadow.get("bundle_run_id") or ""),
                        "challenger_shadow_probs": dict(challenger_shadow.get("probs") or {}),
                        "challenger_shadow_disagreement": dict(challenger_shadow.get("disagreement") or {}),
                        "challenger_conflict": dict(challenger_conflict),
                        "challenger_conflict_mode": str(challenger_conflict_mode),
                        "challenger_conflict_gate_level": str(challenger_conflict.get("gate_level") or "none"),
                        "challenger_shadow_report_refs": dict(challenger_shadow.get("report_refs") or {}),
                        "challenger_shadow_errors": list(challenger_shadow.get("errors") or []),
                        "challenger_shadow_component_refs": {
                            key: {
                                "model_name": str((value or {}).get("model_name") or ""),
                                "model_version": str((value or {}).get("model_version") or ""),
                                "model_uri": str((value or {}).get("model_uri") or ""),
                                "bundle_run_id": str((value or {}).get("bundle_run_id") or ""),
                            }
                            for key, value in dict(challenger_shadow.get("component_refs") or {}).items()
                        },
                        "feature_timeframes": {
                            "regime": regime_timeframe,
                            "swing": swing_timeframe,
                            "intraday": intraday_timeframe,
                            "meta": intraday_timeframe,
                        },
                        "feature_bar": dict(feature_bar),
                        "entry_lot_sizing": dict(lot_sizing_diag),
                        "strategy_engine_mode": str(getattr(s, "strategy_engine_mode", "supervised_legacy") or "supervised_legacy"),
                        "startup_inference": startup_status or {"ok": True, "reason": "ok"},
                        "position_side": pos_side,
                        "position_count_pair": int(pair_count),
                        "position_signature": str(position_signature),
                        "strict_entry_ready": bool(strict_entry_ready),
                        "strict_entry_blocking_reasons": list(strict_entry_reasons),
                        "strict_rejection_reason": "none" if strict_entry_ready else strict_entry_reasons[0],
                        "entry_ready": bool(strict_entry_ready),
                        "entry_blocking_reasons": list(strict_entry_reasons),
                        "execution_mode": "strict_live_mirror",
                        "execution_entry_ready": bool(strict_entry_ready),
                        "execution_blocking_reasons": list(strict_entry_reasons),
                        "execution_rejection_reason": "none" if strict_entry_ready else strict_entry_reasons[0],
                        "reversal_should_exit": bool(reversal_ready),
                        "reversal_context_active": bool(reversal_context_active),
                        "reversal_ready": bool(reversal_ready),
                        "reversal_blocking_reasons": list(reversal_blocking_reasons),
                        "reversal_failure_prob": float(reversal_failure_prob),
                        "reversal_opportunity_prob": float(reversal_opportunity_prob),
                        "reversal_reasons": list(reversal_blocking_reasons),
                        "exit_action_selected": str(exit_action_selected),
                        "exit_action_score": float(exit_action_score),
                        "exit_action_probs": dict(exit_action_probs),
                        "partial_tp_count_position": int(partial_tp_count),
                        "partial_tp_blocked_reason": str(partial_tp_blocked_reason),
                        "partial_tp_next_eligible_secs": float(partial_tp_next_eligible_secs),
                        "lifecycle_action": str(lifecycle_action),
                        "lifecycle_action_score": float(lifecycle_action_score),
                        "lifecycle_reason": str(lifecycle_reason),
                        "lifecycle_activation_mode": str(loaded.lifecycle_activation_mode),
                        "lifecycle_capabilities": {
                            "has_exit_model": bool(loaded.has_exit_model),
                            "has_reversal_models": bool(loaded.has_reversal_models),
                        },
                        "lifecycle_inference_error": str(lifecycle_inference_error),
                        "lifecycle_soft_degrade_reasons": list(dict.fromkeys(lifecycle_soft_degrade_reasons)),
                        "allowed": bool(strict_entry_ready if not positions else ready),
                        "rejection_reason": "none" if (strict_entry_ready if not positions else ready) else strict_entry_reasons[0],
                        "raw_policy_suggestion": dict(raw_policy_suggestion),
                        "risk_verdict": str(risk_kernel_out.get("verdict") or ""),
                        "risk_reason": str(risk_kernel_out.get("reason") or ""),
                        "risk_trace": list(risk_kernel_out.get("trace") or []),
                        "risk_decision": dict(risk_kernel_out.get("decision") or {}),
                        "approved_order": dict(approved_order_payload),
                        "rollout": dict(rollout_meta),
                        "portfolio_allocation": dict(portfolio_allocation_meta),
                        "portfolio_budget_scale": float(_safe_float(risk_kernel_out.get("portfolio_budget_scale"), 1.0)),
                        "capital_budget_scale": float(_safe_float(risk_kernel_out.get("capital_budget_scale"), 1.0)),
                        "capital_governance": dict(capital_governance_meta),
                        "rollout_mode": str(rollout_meta.get("mode") or ""),
                        "rollout_active": bool(rollout_meta.get("active", False)),
                        "rollout_pair_allowlisted": bool(rollout_meta.get("pair_allowlisted", False)),
                        "rollout_budget_scale": float(_safe_float(rollout_meta.get("budget_scale"), 1.0)),
                        "rollout_reduced_budget": bool(rollout_meta.get("reduced_budget", False)),
                        "rollout_breach": bool(rollout_meta.get("breach", False)),
                        "rollout_breach_reason": str(rollout_meta.get("breach_reason") or ""),
                        "enqueue": enqueue_out,
                    },
                }
            )
            decision_meta = dict(decisions[-1].get("metadata", {}) or {})
            _append_policy_trace(
                decision_meta,
                stage="risk_kernel",
                verdict=str(risk_kernel_out.get("verdict") or "hold"),
                reason=str(risk_kernel_out.get("reason") or "none"),
                score=float(_safe_float(lifecycle_action_score if positions else signal.trade_prob, 0.0)),
                changed_decision=bool(not positions and strict_entry_ready != ready),
                details={
                    "lifecycle_action": str(risk_kernel_out.get("lifecycle_action") or lifecycle_action),
                    "trace_count": int(len(list(risk_kernel_out.get("trace") or []))),
                    "approved_order": dict(approved_order_payload),
                    "rollout": dict(rollout_meta),
                    "portfolio_allocation": dict(portfolio_allocation_meta),
                    "capital_governance": dict(capital_governance_meta),
                },
            )
            decisions[-1]["metadata"] = decision_meta
            if bool(getattr(s, "adaptive_shadow_enabled", True)):
                pair_history = adaptive_shadow_history.setdefault(str(pair).upper(), [])
                pair_history.append(dict(adaptive_snapshot))
                max_history = max(16, int(getattr(s, "adaptive_shadow_history_bars", 128) or 128))
                if len(pair_history) > max_history:
                    del pair_history[:-max_history]
            pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)

        # AGENT PARITY: Shadow and adaptive ranking run after the strict pass so runtime can compare live, shadow, and adaptive views on the same bar.
        shadow_diag = _apply_shadow_entry_ranking(
            decisions,
            settings=s,
            open_position_count=len(list(state.get("positions", []) or [])),
        )
        adaptive_shadow_enabled = bool(getattr(s, "adaptive_shadow_enabled", True))
        directional_belief_policy_diag = _directional_belief_policy_diag(s)
        directional_belief_cycle_diag = {
            "candidate_count_with_belief": 0,
            "avg_belief_gap": 0.0,
            "avg_fragility_score": 0.0,
            "avg_primary_rank_score": 0.0,
            "avg_primary_ev_above_hurdle_prob": 0.0,
            "avg_primary_expected_net_ev_bps": 0.0,
            "avg_primary_fail_fast_prob": 0.0,
            "no_edge_share": 0.0,
            "primary_scenario_counts": {},
            "opposition_scenario_counts": {},
            "opposition_side_counts": {},
            "artifact_versions": {},
        }
        directional_belief_metrics = {
            "decision_count": int(len(decisions)),
            "belief_loaded_share": 0.0,
            "avg_belief_gap": 0.0,
            "avg_fragility_score": 0.0,
            "avg_primary_rank_score": 0.0,
            "avg_primary_ev_above_hurdle_prob": 0.0,
            "avg_primary_expected_net_ev_bps": 0.0,
            "avg_primary_fail_fast_prob": 0.0,
            "no_edge_share": 0.0,
            "primary_scenario_counts": {},
            "opposition_scenario_counts": {},
            "opposition_side_counts": {},
        }
        adaptive_mode = bool(getattr(s, "adaptive_execution_enabled", False)) and adaptive_shadow_enabled
        adaptive_shadow_diag = {
            "adaptive_shadow_enabled": bool(adaptive_shadow_enabled),
            "adaptive_shadow_candidate_count": 0,
            "adaptive_shadow_ranked_count": 0,
            "adaptive_shadow_would_trade_count": 0,
            "adaptive_shadow_remaining_slots": max(0, int(getattr(s, "max_total_positions", 0) or 0) - len(list(state.get("positions", []) or []))),
            "adaptive_shadow_max_new_entries": 0,
            "adaptive_shadow_aggressive_fallback_count": 0,
            "adaptive_shadow_live_divergence_counts": {
                "agree_ready": 0,
                "agree_blocked": 0,
                "live_only": 0,
                "adaptive_only": 0,
                "open_position": 0,
            },
            "adaptive_shadow_rejection_reason_counts": {},
            "adaptive_shadow_rejections_by_pair": {},
            "adaptive_shadow_playbook_counts": {},
            "adaptive_shadow_environment_counts": {},
            "adaptive_shadow_dominant_rejection_reason": "",
            "allocator_candidate_count": 0,
            "allocator_selected_count": 0,
            "allocator_ranked_out_count": 0,
            "allocator_replacement_candidate_count": 0,
            "allocator_replacement_exit_count": 0,
            "allocator_sleeve_candidate_counts": {},
            "allocator_sleeve_selected_counts": {},
            "allocator_sleeve_budget_targets": {},
            "allocator_sleeve_budget_used": {},
            "allocator_pair_pressure_avg": 0.0,
            "allocator_pair_pressure_max": 0.0,
            "allocator_session_pressure_avg": 0.0,
            "allocator_session_pressure_max": 0.0,
            "allocator_sleeve_pressure_avg": 0.0,
            "allocator_sleeve_pressure_max": 0.0,
            "allocator_correlation_pressure_avg": 0.0,
            "allocator_correlation_pressure_max": 0.0,
            "allocator_risk_pressure_avg": 0.0,
            "allocator_risk_pressure_max": 0.0,
            "overlay_cycle_summary": {
                "conviction_score_avg": 0.0,
                "conviction_score_max": 0.0,
                "conviction_score_min": 0.0,
                "conviction_band_counts": {},
                "thesis_stage_counts": {},
                "posture_counts": {},
                "sleeve_budget_target_total": 0,
                "sleeve_budget_used_total": 0,
                "pair_pressure_avg": 0.0,
                "pair_pressure_max": 0.0,
                "session_pressure_avg": 0.0,
                "session_pressure_max": 0.0,
                "sleeve_pressure_avg": 0.0,
                "sleeve_pressure_max": 0.0,
                "correlation_pressure_avg": 0.0,
                "correlation_pressure_max": 0.0,
                "risk_pressure_avg": 0.0,
                "risk_pressure_max": 0.0,
                "replacement_urgency_avg": 0.0,
                "policy_trace_count": 0,
                "diagnostics": {
                    "environment_posture": "",
                    "sleeve_budget_state": {},
                    "replacement_pressure_by_sleeve": {},
                    "portfolio_pressure": {
                        "pair_avg": 0.0,
                        "pair_max": 0.0,
                        "session_avg": 0.0,
                        "session_max": 0.0,
                        "sleeve_avg": 0.0,
                        "sleeve_max": 0.0,
                        "correlation_avg": 0.0,
                        "correlation_max": 0.0,
                        "risk_avg": 0.0,
                        "risk_max": 0.0,
                    },
                    "divergence_matrix": {
                        "by_pair": {},
                        "by_session": {},
                        "by_regime": {},
                        "by_sleeve": {},
                    },
                    "press_count": 0,
                    "stand_down_count": 0,
                },
            },
        }
        allocator_policy_diag = {
            "candidate_count": 0,
            "selected_count": 0,
            "ranked_out_count": 0,
            "replacement_candidate_count": 0,
            "replacement_exit_count": 0,
            "sleeve_candidate_counts": {},
            "sleeve_selected_counts": {},
            "sleeve_budget_targets": {},
            "sleeve_budget_used": {},
        }
        sleeve_metrics_diag = serialize_sleeve_snapshots(sleeve_tracker.snapshot())
        if adaptive_shadow_enabled:
            adaptive_frames = _adaptive_shadow_frames_from_history(history=adaptive_shadow_history, pairs=pairs)
            if adaptive_frames:
                attach_adaptive_context(
                    adaptive_frames,
                    pairs=sorted(list(adaptive_frames.keys())),
                    settings=s,
                    enabled_playbooks=set(adaptive_shadow_playbooks),
                )
                adaptive_rows_by_pair = {
                    str(pair).upper(): dict(frame.iloc[-1].to_dict())
                    for pair, frame in adaptive_frames.items()
                    if not frame.empty
                }
        directional_belief_cycle_diag, directional_belief_metrics = _attach_directional_belief_shadow(
            decisions=decisions,
            loaded_model_sets=model_sets,
            adaptive_rows_by_pair=adaptive_rows_by_pair,
            settings=s,
        )
        _sync_adaptive_position_registry(
            decisions=decisions,
            state=state,
            adaptive_rows_by_pair=adaptive_rows_by_pair,
            adaptive_pending_entry_registry=adaptive_pending_entry_registry,
            adaptive_position_registry=adaptive_position_registry,
            current_equity=float(current_equity_value),
        )
        sleeve_health_snapshots = sleeve_tracker.snapshot() if adaptive_shadow_enabled else {}
        for decision in decisions:
            meta = dict(decision.get("metadata", {}) or {})
            pair = str(meta.get("pair") or decision.get("symbol") or "").upper()
            ts_value = str(meta.get("ts") or "")
            if not pair or not ts_value:
                continue
            if bool(meta.get("strict_entry_ready", False)) and int(_safe_float(meta.get("position_count_pair", 0), 0.0)) == 0:
                baseline_key = (pair, ts_value)
                if baseline_key not in adaptive_seen_baseline_entry_keys:
                    adaptive_seen_baseline_entry_keys.add(baseline_key)
                    adaptive_baseline_entry_count += 1
            meta["execution_mode"] = "adaptive_multi_playbook" if adaptive_mode else "strict_live_mirror"
            decision["metadata"] = meta

        tempo_gap_active = bool(
            adaptive_mode
            and adaptive_tempo_gap_active(
                baseline_entries_so_far=int(adaptive_baseline_entry_count),
                adaptive_entries_so_far=int(adaptive_live_entry_count),
            )
        )
        if adaptive_mode and pending_position_actions:
            for action in pending_position_actions:
                index = int(action.get("index", -1))
                if index < 0 or index >= len(decisions):
                    continue
                decision = decisions[index]
                meta = dict(decision.get("metadata", {}) or {})
                pair = str(action.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
                current_row = dict(adaptive_rows_by_pair.get(pair, {}) or {})
                pos_state = adaptive_position_registry.get(pair)
                if pos_state is None:
                    continue
                playbook = str(current_row.get("playbook") or getattr(pos_state, "playbook", PLAYBOOK_TREND_PULLBACK) or PLAYBOOK_TREND_PULLBACK)
                adaptive_lifecycle = adaptive_lifecycle_decision(
                    position=pos_state,
                    row={
                        "playbook": playbook,
                        "playbook_score": float(_safe_float(current_row.get("playbook_score"), meta.get("adaptive_playbook_score", 0.0))),
                        "location_score": float(_safe_float(current_row.get("location_score"), meta.get("adaptive_location_score", 0.0))),
                        "trigger_score": float(_safe_float(current_row.get("trigger_score"), meta.get("adaptive_trigger_score", 0.0))),
                        "hostility_score": float(_safe_float(current_row.get("hostility_score"), meta.get("adaptive_hostility_score", 0.0))),
                        "macro_coherence_score": float(
                            _safe_float(current_row.get("macro_coherence_score"), meta.get("adaptive_macro_coherence_score", 0.0))
                        ),
                        "extension_penalty_score": float(_safe_float(meta.get("extension_penalty_score"), current_row.get("extension_penalty_score", 0.0))),
                        "environment_state": str(current_row.get("environment_state") or meta.get("adaptive_environment_state") or ""),
                    },
                    unrealized_pnl_usd=float(_safe_float(action.get("unrealized_pnl_usd"), 0.0)),
                    age_bars=float(_safe_float(action.get("age_bars"), 0.0)),
                    bar_idx=int(adaptive_bar_index_by_pair.get(pair, -1)),
                    exit_action_probs=dict(action.get("exit_action_probs") or {}),
                    reversal_context_active=bool(action.get("reversal_context_active", False)),
                    reversal_ready=bool(action.get("reversal_ready", False)),
                    reversal_failure_prob=float(_safe_float(action.get("reversal_failure_prob"), 0.0)),
                    reversal_opportunity_prob=float(_safe_float(action.get("reversal_opportunity_prob"), 0.0)),
                )
                baseline_lifecycle_action = str(action.get("baseline_lifecycle_action") or action.get("lifecycle_action") or "hold")
                baseline_lifecycle_reason = str(action.get("baseline_lifecycle_reason") or action.get("lifecycle_reason") or "hold")
                baseline_close_lots = float(_safe_float(action.get("baseline_close_lots"), action.get("close_lots", 0.0)))
                lifecycle_action = str(adaptive_lifecycle.get("action") or "hold")
                lifecycle_reason = str(adaptive_lifecycle.get("reason") or "adaptive_hold")
                close_lots = 0.0
                campaign_keep_adjustment = 0.0
                partial_tp_blocked_reason = str(meta.get("partial_tp_blocked_reason") or "")
                partial_tp_next_eligible_secs = float(_safe_float(meta.get("partial_tp_next_eligible_secs"), 0.0))
                if lifecycle_action == "partial_tp":
                    tracker_state = dict(partial_close_tracker.get(str(action.get("position_signature") or meta.get("position_signature") or ""), {}) or {})
                    allow_partial_tp, partial_tp_blocked_reason, partial_tp_next_eligible_secs = _partial_close_guard(
                        tracker_state=tracker_state,
                        loop_ts=float(loop_ts),
                        settings=s,
                    )
                    if allow_partial_tp:
                        position_rows = _pair_positions(state, pair=pair)
                        lots_open = float(_safe_float(dict(position_rows[0] or {}).get("lots"), 0.0)) if position_rows else 0.0
                        lifecycle_action, close_lots = _partial_close_plan(
                            lots_open=lots_open,
                            fraction=float(s.partial_close_fraction),
                            settings=s,
                        )
                        if lifecycle_action not in {"partial_tp", "exit"} or close_lots <= 0.0:
                            lifecycle_action = "hold"
                            lifecycle_reason = "adaptive_hold"
                            close_lots = 0.0
                    else:
                        lifecycle_action = "hold"
                        lifecycle_reason = str(partial_tp_blocked_reason or "partial_tp_blocked")
                        close_lots = 0.0
                if bool(campaign_config.enabled):
                    prior_campaign_state = str(getattr(pos_state, "campaign_state", "probe") or "probe")
                    campaign_open = evaluate_open_campaign(
                        pair=pair,
                        side=str(getattr(pos_state, "side", "long")),
                        sleeve=str(getattr(pos_state, "sleeve", playbook_to_sleeve(getattr(pos_state, "playbook", "")))),
                        current_state=prior_campaign_state,
                        row={
                            "playbook_score": float(_safe_float(current_row.get("playbook_score"), meta.get("adaptive_playbook_score", 0.0))),
                            "location_score": float(_safe_float(current_row.get("location_score"), meta.get("adaptive_location_score", 0.0))),
                            "trigger_score": float(_safe_float(current_row.get("trigger_score"), meta.get("adaptive_trigger_score", 0.0))),
                            "macro_coherence_score": float(_safe_float(current_row.get("macro_coherence_score"), meta.get("adaptive_macro_coherence_score", 0.0))),
                            "hostility_score": float(_safe_float(current_row.get("hostility_score"), meta.get("adaptive_hostility_score", 0.0))),
                            "extension_penalty_score": float(_safe_float(meta.get("extension_penalty_score"), current_row.get("extension_penalty_score", 0.0))),
                            "environment_state": str(current_row.get("environment_state") or meta.get("adaptive_environment_state") or ""),
                        },
                        unrealized_pnl_usd=float(_safe_float(action.get("unrealized_pnl_usd"), 0.0)),
                        age_bars=float(_safe_float(action.get("age_bars"), 0.0)),
                        open_equity_usd=float(_safe_float(getattr(pos_state, "open_equity_usd", current_equity_value), current_equity_value)),
                        bar_idx=int(adaptive_bar_index_by_pair.get(pair, -1)),
                        ts=str(meta.get("ts") or ""),
                        lifecycle_action=str(lifecycle_action),
                        lifecycle_reason=str(lifecycle_reason),
                        reversal_ready=bool(action.get("reversal_ready", False)),
                        severe_invalidation=bool(lifecycle_reason in {"adaptive_breakout_follow_through_failed", "adaptive_failed_breakout_invalidated", "adaptive_reverse_ready"}),
                        config=campaign_config,
                    )
                    campaign_keep_adjustment = float(campaign_open.keep_adjustment)
                    meta["thesis_id"] = str(campaign_open.thesis_id)
                    meta["campaign_state"] = str(campaign_open.state)
                    meta["campaign_state_reason"] = str(campaign_open.state_reason)
                    meta["campaign_proof_score"] = float(campaign_open.proof_score)
                    meta["campaign_maturity_score"] = float(campaign_open.maturity_score)
                    meta["campaign_reset_quality"] = float(campaign_open.reset_quality)
                    meta["campaign_priority_boost"] = float(campaign_open.priority_boost)
                    meta["campaign_reentry_blocked"] = bool(campaign_open.reentry_blocked)
                    if not bool(campaign_config.shadow_only):
                        campaign_override = apply_campaign_lifecycle_overrides(
                            snapshot=campaign_open,
                            lifecycle_action=str(lifecycle_action),
                            lifecycle_reason=str(lifecycle_reason),
                            unrealized_pnl_usd=float(_safe_float(action.get("unrealized_pnl_usd"), 0.0)),
                            severe_invalidation=bool(campaign_open.state == CAMPAIGN_STATE_ABANDONED),
                        )
                        lifecycle_action = str(campaign_override.get("lifecycle_action") or lifecycle_action)
                        lifecycle_reason = str(campaign_override.get("lifecycle_reason") or lifecycle_reason)
                    transition = campaign_transition_if_changed(
                        prior_state=prior_campaign_state,
                        snapshot=campaign_open,
                        bar_idx=int(adaptive_bar_index_by_pair.get(pair, -1)),
                        ts=str(meta.get("ts") or ""),
                        unrealized_pnl_usd=float(_safe_float(action.get("unrealized_pnl_usd"), 0.0)),
                        holding_bars=float(_safe_float(action.get("age_bars"), 0.0)),
                    )
                    if transition is not None:
                        key = f"{transition.prior_state}->{transition.new_state}"
                        campaign_transition_counts[key] = int(campaign_transition_counts.get(key, 0)) + 1
                    apply_campaign_registry_snapshot(
                        campaign_registry,
                        snapshot=campaign_open,
                        bar_idx=int(adaptive_bar_index_by_pair.get(pair, -1)),
                        ts=str(meta.get("ts") or ""),
                        active_position=True,
                    )
                    pos_state.thesis_id = str(campaign_open.thesis_id)
                    pos_state.campaign_state = str(campaign_open.state)
                    pos_state.campaign_state_reason = str(campaign_open.state_reason)
                    pos_state.campaign_state_entered_bar = int(adaptive_bar_index_by_pair.get(pair, -1)) if transition is not None else int(getattr(pos_state, "campaign_state_entered_bar", 0) or 0)
                severe_adaptive_exit = lifecycle_reason in {
                    "adaptive_breakout_follow_through_failed",
                    "adaptive_failed_breakout_invalidated",
                    "adaptive_reverse_ready",
                    "adaptive_campaign_probe_failed",
                }
                keep_score = float(
                    max(
                        0.0,
                        min(
                            1.0,
                        adaptive_replacement_keep_score(
                            lifecycle_action=str(lifecycle_action),
                            lifecycle_reason=str(lifecycle_reason),
                            playbook_score=float(_safe_float(current_row.get("playbook_score"), meta.get("adaptive_playbook_score", 0.0))),
                            location_score=float(_safe_float(current_row.get("location_score"), meta.get("adaptive_location_score", 0.0))),
                            trigger_score=float(_safe_float(current_row.get("trigger_score"), meta.get("adaptive_trigger_score", 0.0))),
                            entry_trade_prob=float(_safe_float(getattr(pos_state, "entry_trade_prob", 0.0), 0.0)),
                            entry_macro_coherence_score=float(_safe_float(getattr(pos_state, "entry_macro_coherence_score", 0.0), 0.0)),
                            aggressive_fallback_used=bool(getattr(pos_state, "aggressive_fallback_used", False)),
                        )
                        + float(campaign_keep_adjustment)
                        ),
                    )
                )
                tempo_rotation_release = bool(
                    tempo_gap_active
                    and float(_safe_float(action.get("age_bars"), 0.0)) >= 12.0
                    and lifecycle_action in {"partial_tp", "exit"}
                    and (not severe_adaptive_exit)
                    and (
                        str(getattr(pos_state, "playbook", PLAYBOOK_TREND_PULLBACK))
                        in {PLAYBOOK_RANGE_MEAN_REVERSION, PLAYBOOK_BREAKOUT_EXPANSION}
                        or keep_score <= 0.48
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
                action["lifecycle_action"] = str(lifecycle_action)
                action["lifecycle_reason"] = str(lifecycle_reason)
                action["close_lots"] = float(close_lots)
                action["playbook"] = str(playbook)
                action["replacement_keep_score"] = float(keep_score)
                action["partial_tp_blocked_reason"] = str(partial_tp_blocked_reason)
                action["partial_tp_next_eligible_secs"] = float(partial_tp_next_eligible_secs)
                meta["lifecycle_action"] = str(lifecycle_action)
                meta["lifecycle_reason"] = str(lifecycle_reason)
                meta["partial_tp_blocked_reason"] = str(partial_tp_blocked_reason)
                meta["partial_tp_next_eligible_secs"] = float(partial_tp_next_eligible_secs)
                decision["metadata"] = meta
                _sync_lifecycle_action_payloads(decision=decision, action_item=action)

        projected_exit_count = int(sum(1 for item in pending_position_actions if str(item.get("lifecycle_action") or "hold") == "exit"))
        if adaptive_shadow_enabled and adaptive_rows_by_pair:
            adaptive_shadow_diag = _apply_adaptive_shadow_ranking(
                decisions,
                settings=s,
                open_position_count=max(0, len(list(state.get("positions", []) or [])) - projected_exit_count),
                adaptive_rows_by_pair=adaptive_rows_by_pair,
                adaptive_position_registry=adaptive_position_registry,
                recent_exit_registry=adaptive_recent_exit_registry,
                pair_bar_index=adaptive_bar_index_by_pair,
                sleeve_health_snapshots=sleeve_health_snapshots,
                campaign_registry=campaign_registry,
                state=state,
                current_equity=float(current_equity_value),
            )
            if adaptive_mode and pending_position_actions:
                evictable_actions = sorted(
                    [
                        item
                        for item in pending_position_actions
                        if str(item.get("lifecycle_action") or "hold") == "hold"
                        and (
                            str(item.get("lifecycle_reason") or "") == "adaptive_hold_baseline_floor"
                            or (
                                tempo_gap_active
                                and float(_safe_float(item.get("replacement_keep_score"), 1.0)) <= 0.48
                            )
                        )
                    ],
                    key=lambda item: float(_safe_float(item.get("replacement_keep_score"), 1.0)),
                )
                overflow_candidates = sorted(
                    [
                        int(item.get("index", -1))
                        for item in pending_entries
                        if 0 <= int(item.get("index", -1)) < len(decisions)
                        and bool(dict(decisions[int(item.get("index", -1))].get("metadata", {}) or {}).get("adaptive_shadow_allowed", False))
                        and not bool(dict(decisions[int(item.get("index", -1))].get("metadata", {}) or {}).get("adaptive_shadow_would_trade", False))
                    ],
                    key=lambda idx: int(_safe_float(dict(decisions[idx].get("metadata", {}) or {}).get("adaptive_portfolio_rank_shadow"), 10_000)),
                )
                runtime_allocator_config = allocator_config_from_settings(s)
                replacement_margin = float(
                    runtime_allocator_config.tempo_gap_replacement_margin
                    if tempo_gap_active
                    else runtime_allocator_config.replacement_margin
                )
                replacement_exit_count = 0
                while overflow_candidates and evictable_actions:
                    candidate_index = int(overflow_candidates[0])
                    candidate_meta = dict(decisions[candidate_index].get("metadata", {}) or {})
                    candidate_quality = float(_safe_float(candidate_meta.get("allocator_score"), candidate_meta.get("adaptive_entry_quality", 0.0)))
                    target_pair = str(candidate_meta.get("replacement_target_pair") or "").upper()
                    weakest = evictable_actions[0]
                    if target_pair:
                        targeted = next((item for item in evictable_actions if str(item.get("pair") or "").upper() == target_pair), None)
                        if targeted is not None:
                            weakest = targeted
                    weakest_keep = float(_safe_float(weakest.get("replacement_keep_score"), 1.0))
                    if candidate_quality < (weakest_keep + replacement_margin):
                        break
                    weakest["lifecycle_action"] = "exit"
                    weakest["lifecycle_reason"] = "adaptive_replacement_exit"
                    weakest["close_lots"] = 0.0
                    weakest_idx = int(weakest.get("index", -1))
                    if 0 <= weakest_idx < len(decisions):
                        weakest_decision = decisions[weakest_idx]
                        weakest_meta = dict(weakest_decision.get("metadata", {}) or {})
                        weakest_meta["lifecycle_action"] = "exit"
                        weakest_meta["lifecycle_reason"] = "adaptive_replacement_exit"
                        weakest_decision["metadata"] = weakest_meta
                        _sync_lifecycle_action_payloads(decision=weakest_decision, action_item=weakest)
                    replacement_exit_count += 1
                    overflow_candidates.pop(0)
                    evictable_actions = [item for item in evictable_actions if int(item.get("index", -1)) != int(weakest.get("index", -1))]
                if replacement_exit_count > 0:
                    projected_exit_count += int(replacement_exit_count)
                    adaptive_shadow_diag = _apply_adaptive_shadow_ranking(
                        decisions,
                        settings=s,
                        open_position_count=max(0, len(list(state.get("positions", []) or [])) - projected_exit_count),
                        adaptive_rows_by_pair=adaptive_rows_by_pair,
                        adaptive_position_registry=adaptive_position_registry,
                        recent_exit_registry=adaptive_recent_exit_registry,
                        pair_bar_index=adaptive_bar_index_by_pair,
                        sleeve_health_snapshots=sleeve_health_snapshots,
                        campaign_registry=campaign_registry,
                        state=state,
                        current_equity=float(current_equity_value),
                    )

        for decision in decisions:
            meta = dict(decision.get("metadata", {}) or {})
            sleeve_tracker.record_divergence(
                sleeve=str(meta.get("adaptive_sleeve") or playbook_to_sleeve(meta.get("adaptive_playbook") or "")),
                divergence=str(meta.get("adaptive_shadow_live_divergence") or ""),
            )
        sleeve_metrics_diag = serialize_sleeve_snapshots(sleeve_tracker.snapshot())
        allocator_policy_diag = {
            "candidate_count": int(adaptive_shadow_diag.get("allocator_candidate_count", 0)),
            "selected_count": int(adaptive_shadow_diag.get("allocator_selected_count", 0)),
            "ranked_out_count": int(adaptive_shadow_diag.get("allocator_ranked_out_count", 0)),
            "replacement_candidate_count": int(adaptive_shadow_diag.get("allocator_replacement_candidate_count", 0)),
            "replacement_exit_count": int(adaptive_shadow_diag.get("allocator_replacement_exit_count", 0)),
            "sleeve_candidate_counts": dict(adaptive_shadow_diag.get("allocator_sleeve_candidate_counts", {})),
            "sleeve_selected_counts": dict(adaptive_shadow_diag.get("allocator_sleeve_selected_counts", {})),
            "sleeve_budget_targets": dict(adaptive_shadow_diag.get("allocator_sleeve_budget_targets", {})),
            "sleeve_budget_used": dict(adaptive_shadow_diag.get("allocator_sleeve_budget_used", {})),
            "allocator_pair_pressure_avg": float(adaptive_shadow_diag.get("allocator_pair_pressure_avg", 0.0)),
            "allocator_pair_pressure_max": float(adaptive_shadow_diag.get("allocator_pair_pressure_max", 0.0)),
            "allocator_session_pressure_avg": float(adaptive_shadow_diag.get("allocator_session_pressure_avg", 0.0)),
            "allocator_session_pressure_max": float(adaptive_shadow_diag.get("allocator_session_pressure_max", 0.0)),
            "allocator_sleeve_pressure_avg": float(adaptive_shadow_diag.get("allocator_sleeve_pressure_avg", 0.0)),
            "allocator_sleeve_pressure_max": float(adaptive_shadow_diag.get("allocator_sleeve_pressure_max", 0.0)),
            "allocator_correlation_pressure_avg": float(adaptive_shadow_diag.get("allocator_correlation_pressure_avg", 0.0)),
            "allocator_correlation_pressure_max": float(adaptive_shadow_diag.get("allocator_correlation_pressure_max", 0.0)),
            "allocator_risk_pressure_avg": float(adaptive_shadow_diag.get("allocator_risk_pressure_avg", 0.0)),
            "allocator_risk_pressure_max": float(adaptive_shadow_diag.get("allocator_risk_pressure_max", 0.0)),
        }
        campaign_state_counts_runtime = dict(
            Counter(
                str(dict(decision.get("metadata", {}) or {}).get("campaign_state") or CAMPAIGN_STATE_INACTIVE)
                for decision in decisions
            )
        )
        campaign_metrics_by_sleeve: dict[str, Any] = {}
        for entry in campaign_registry.values():
            sleeve_key = str(entry.sleeve or "")
            bucket = campaign_metrics_by_sleeve.setdefault(
                sleeve_key,
                {
                    "state_counts": {},
                    "active_position_count": 0,
                    "harvest_count": 0,
                    "reattack_count": 0,
                    "abandoned_count": 0,
                },
            )
            state_key = str(entry.state or CAMPAIGN_STATE_INACTIVE)
            bucket["state_counts"][state_key] = int(bucket["state_counts"].get(state_key, 0)) + 1
            bucket["active_position_count"] = int(bucket["active_position_count"]) + int(bool(entry.active_position))
            bucket["harvest_count"] = int(bucket["harvest_count"]) + int(entry.harvest_count)
            bucket["reattack_count"] = int(bucket["reattack_count"]) + int(entry.reattack_count)
            bucket["abandoned_count"] = int(bucket["abandoned_count"]) + int(entry.abandoned_at_bar is not None)
        campaign_policy_diag = {
            "enabled": bool(campaign_config.enabled),
            "shadow_only": bool(campaign_config.shadow_only),
            "abandon_cooldown_bars": int(campaign_config.abandon_cooldown_bars),
            "press_protected_bars": int(campaign_config.press_protected_bars),
            "reattack_cooldown_scale": float(campaign_config.reattack_cooldown_scale),
        }
        campaign_cycle_diag = {
            "state_counts": dict(campaign_state_counts_runtime),
            "transition_counts": dict(sorted(campaign_transition_counts.items())),
            "registry_size": int(len(campaign_registry)),
            "active_position_theses": int(sum(1 for entry in campaign_registry.values() if bool(entry.active_position))),
            "reentry_blocked_count": int(
                sum(
                    1
                    for decision in decisions
                    if bool(dict(decision.get("metadata", {}) or {}).get("campaign_reentry_blocked", False))
                )
            ),
            "registry": {key: serialize_campaign_entry(value) for key, value in sorted(campaign_registry.items())},
        }
        first = decisions[0] if decisions else {"symbol": "N/A", "side": "N/A"}
        portfolio_cycle = evaluate_portfolio_allocation(
            symbol=str(first.get("symbol", pairs[0] if pairs else "")),
            session_bucket="",
            expected_edge_bps=0.0,
            uncertainty_score=0.0,
            positions=list(state.get("positions", []) or []),
            pending_entries=list(pending_entries or []),
            max_total_positions=int(getattr(s, "max_total_positions", 0) or 0),
            max_pair_positions=int(getattr(s, "max_pair_positions", 0) or 0),
            governance=governance,
            corr_mode=portfolio_corr_mode,
            realized_returns_by_pair=realized_returns_by_pair,
            corr_window_bars=int(getattr(s, "portfolio_realized_corr_window_bars", 0) or 0),
            corr_min_obs=int(getattr(s, "portfolio_realized_corr_min_obs", 0) or 0),
        )
        portfolio_cycle_diag = dict(
            build_portfolio_telemetry(
                book=portfolio_cycle.book,
                concentration=portfolio_cycle.concentration,
                correlation=portfolio_cycle.correlation,
                budget=portfolio_cycle.budget,
                stress=portfolio_cycle.stress,
                governance=governance,
            )
        )
        runtime_rl_checkpoint_path = _resolve_runtime_rl_checkpoint_path(model_sets=model_sets, project_root=Path(s.project_root))
        rl_portfolio_proposal = build_portfolio_rl_proposal_bundle(
            ts=str(first.get("ts") or first.get("ts_value") or ""),
            decisions=list(decisions),
            portfolio=dict(portfolio_cycle_diag),
            policy_context={
                "runtime_mode": str(getattr(s, "strategy_engine_mode", "supervised_legacy") or "supervised_legacy"),
                "supervised_fallback_required": bool(getattr(s, "rl_supervised_fallback_required", True)),
                "allocator_enabled": bool(getattr(s, "use_portfolio_ranking", True)),
                "adaptive_shadow_enabled": bool(adaptive_shadow_enabled),
            },
            checkpoint_path=runtime_rl_checkpoint_path,
            supervised_fallback_required=bool(getattr(s, "rl_supervised_fallback_required", True)),
        ).to_dict()
        rl_lifecycle_diag = _apply_rl_lifecycle_router(
            decisions=decisions,
            pending_position_actions=pending_position_actions,
            rl_portfolio_proposal=rl_portfolio_proposal,
            settings=s,
        )

        # AGENT HANDSHAKE: Exits/partials are submitted before entries; the resulting diagnostics are folded into the state patch consumed by bridge and dashboard clients.
        position_action_diag = _submit_position_actions(
            decisions=decisions,
            pending_position_actions=pending_position_actions,
            svc=svc,
            last_action_key=last_action_key,
            partial_close_tracker=partial_close_tracker,
            adaptive_position_registry=adaptive_position_registry,
            adaptive_recent_exit_registry=adaptive_recent_exit_registry,
            pair_bar_index=adaptive_bar_index_by_pair,
            loop_ts=float(loop_ts),
            campaign_registry=campaign_registry,
            campaign_transition_counts=campaign_transition_counts,
            campaign_config=campaign_config,
        )
        for action in pending_position_actions:
            if str(action.get("lifecycle_action") or "") != "exit":
                continue
            idx_action = int(action.get("index", -1))
            if idx_action < 0 or idx_action >= len(decisions):
                continue
            meta = dict(decisions[idx_action].get("metadata", {}) or {})
            enqueue = dict(meta.get("enqueue") or {})
            enqueue_status = str(enqueue.get("status") or "").strip().lower()
            if enqueue_status in {"failed", "invalid", "expired", "duplicate", "duplicate_action_skip", "skipped"}:
                continue
            sleeve_tracker.record_trade(
                sleeve=str(action.get("playbook") or meta.get("adaptive_sleeve") or playbook_to_sleeve(meta.get("adaptive_playbook") or "")),
                realized_pnl_usd=float(_safe_float(action.get("unrealized_pnl_usd"), 0.0)),
                holding_bars=float(_safe_float(action.get("age_bars"), 0.0)),
                partial_exit_events=0,
                close_reason=str(action.get("lifecycle_reason") or meta.get("lifecycle_reason") or ""),
                session_bucket=str(meta.get("session_bucket") or ""),
                pair=str(action.get("pair") or meta.get("pair") or ""),
            )
        sleeve_metrics_diag = serialize_sleeve_snapshots(sleeve_tracker.snapshot())
        entry_execution_diag = _finalize_entry_submissions(
            decisions=decisions,
            pending_entries=pending_entries,
            svc=svc,
            last_action_key=last_action_key,
            settings=s,
            rl_portfolio_proposal=rl_portfolio_proposal,
            adaptive_pending_entry_registry=adaptive_pending_entry_registry,
            current_equity=float(current_equity_value),
            adaptive_seen_live_entry_keys=adaptive_seen_live_entry_keys,
        )
        adaptive_live_entry_count += int(entry_execution_diag.get("submitted_live_entry_count", 0))
        entry_execution_diag["adaptive_baseline_entry_count"] = int(adaptive_baseline_entry_count)
        entry_execution_diag["adaptive_live_entry_count"] = int(adaptive_live_entry_count)
        entry_execution_diag["adaptive_tempo_gap_active"] = bool(tempo_gap_active)
        entry_execution_diag.update(position_action_diag)
        entry_execution_diag.update(rl_lifecycle_diag)
        rollout_policy_diag = _rollout_policy_summary(model_sets=model_sets)
        risk_cycle_diag = _risk_cycle_summary(decisions=decisions)
        loop_latency_ms = round((time.perf_counter() - loop_t0) * 1000.0, 3)
        provider_health = {
            "history_provider": ProviderHealthSnapshot(
                provider=str(provider_roles.get("history_provider") or ""),
                role="history",
                status="shadow_only" if bool(getattr(s, "provider_shadow_only", False)) else "ok",
                shadow_only=bool(provider_capabilities(str(provider_roles.get("history_provider") or "")).shadow_only or getattr(s, "provider_shadow_only", False)),
                provenance="parquet,feast",
                details={"live_refresh_pairs": int(len(live_refresh_diag))},
            ).to_dict(),
            "market_data_provider": ProviderHealthSnapshot(
                provider=str(provider_roles.get("market_data_provider") or ""),
                role="market_data",
                status="ok" if bool(mt4_fresh and ticks_fresh) else "degraded",
                freshness_secs=None,
                fallback_mode="bridge_ticks",
                provenance="bridge",
                details={
                    "mt4_fresh": bool(mt4_fresh),
                    "ticks_fresh": bool(ticks_fresh),
                    "symbol_count": int(len(ticks or {})),
                },
            ).to_dict(),
            "execution_provider": ProviderHealthSnapshot(
                provider=str(provider_roles.get("execution_provider") or ""),
                role="execution",
                status="ok" if bool(mt4_fresh) else "degraded",
                shadow_only=bool(str(provider_roles.get("execution_provider") or "").strip().lower() != "mt4"),
                provenance="runtime_service",
                details={"paused": bool(paused), "entries_only": bool(governance_entries_only)},
            ).to_dict(),
        }
        portfolio_cycle_diag["rl_portfolio_proposal"] = dict(rl_portfolio_proposal)
        capital_governance = compute_capital_governance_state(
            settings=s,
            runtime_diag={
                "loop_latency_ms": float(loop_latency_ms),
                "feature_serving": _feature_serving_snapshot(),
                "risk_cycle_summary": dict(risk_cycle_diag),
                "shadow_policy": dict(shadow_diag),
                "rl_portfolio_proposal": dict(rl_portfolio_proposal),
            },
            metrics=svc.get_metrics(),
            portfolio_telemetry=portfolio_cycle_diag,
            provider_health=provider_health,
        ).to_dict()
        monitor_entry = {"symbol": str(first.get("symbol", "N/A")), "side": str(first.get("side", "N/A"))}
        runtime_diag = {
            "loop_latency_ms": float(loop_latency_ms),
            "pair_eval_time_ms": dict(pair_eval_time_ms),
            "inference_errors": int(inference_errors),
            "model_load_timeouts": int(model_load_diag.get("model_load_timeouts", 0)),
            "model_load_errors": int(model_load_diag.get("model_load_errors", 0)),
            "feature_bootstrap": dict(feature_bootstrap),
            "live_feature_refresh": dict(live_refresh_diag),
            "provider_roles": dict(provider_roles),
            "provider_health": dict(provider_health),
            "feature_serving": _feature_serving_snapshot(),
            "feature_serving_by_pair": dict(sorted(((f"{pair}:{tf}", value) for (pair, tf), value in _FEATURE_SERVING_TELEMETRY.items()))),
            "entry_lot_sizing": dict(lot_sizing_diag),
            "strategy_engine_mode": str(getattr(s, "strategy_engine_mode", "supervised_legacy") or "supervised_legacy"),
            "supervised_fallback": _strategy_fallback_summary(decisions),
            "portfolio_corr_mode": str(portfolio_corr_mode),
            "rl_portfolio_proposal": dict(rl_portfolio_proposal),
            "startup_inference": dict(startup_inference),
            "startup_inference_by_pair": dict(startup_inference),
            "startup_inference_failures": int(len(startup_disabled_pairs)),
            "startup_disabled_pairs": list(startup_disabled_pairs),
            "pair_readiness": _pair_readiness_summary(
                pairs=pairs,
                startup_inference=startup_inference,
                feature_serving_by_pair=dict(sorted(((f"{pair}:{tf}", value) for (pair, tf), value in _FEATURE_SERVING_TELEMETRY.items()))),
                symbol_readiness=symbol_readiness,
                model_load_diag=model_load_diag,
            ),
            "activation_consistency": dict(activation_consistency),
            "manifest_seed": dict(manifest_seed_diag),
            "shadow_policy": dict(shadow_diag),
            "adaptive_shadow_policy": dict(adaptive_shadow_diag),
            "challenger_conflict": _challenger_conflict_summary(decisions),
            "allocator_policy": dict(allocator_policy_diag),
            "allocator_cycle_summary": dict(allocator_policy_diag),
            "portfolio_intelligence": dict(portfolio_cycle_diag),
            "campaign_policy": dict(campaign_policy_diag),
            "campaign_cycle_summary": dict(campaign_cycle_diag),
            "campaign_metrics_by_sleeve": dict(campaign_metrics_by_sleeve),
            "campaign_state_counts": dict(campaign_state_counts_runtime),
            "directional_belief_policy": dict(directional_belief_policy_diag),
            "directional_belief_cycle_summary": dict(directional_belief_cycle_diag),
            "directional_belief_metrics": dict(directional_belief_metrics),
            "overlay_cycle_summary": dict(adaptive_shadow_diag.get("overlay_cycle_summary", {})),
            "desk_overlay_cycle_summary": dict(adaptive_shadow_diag.get("overlay_cycle_summary", {})),
            "rollout_policy": dict(rollout_policy_diag),
            "canary_rollout_policy": dict(rollout_policy_diag),
            "risk_cycle_summary": dict(risk_cycle_diag),
            "rollout_summary": dict(risk_cycle_diag.get("rollout") or {}),
            "canary_rollout_summary": dict(risk_cycle_diag.get("rollout") or {}),
            "capital_governance": dict(capital_governance),
            "sleeve_metrics": dict(sleeve_metrics_diag),
            "entry_execution_policy": dict(entry_execution_diag),
        }

        state_patch: dict[str, Any] = {
            "runtime_profile": str(s.policy_version),
            "runtime_last_cycle_ts": float(loop_ts),
            "runtime_status": "running" if runtime_running else "starting",
            "runtime_equity_seed": float(equity),
            "runtime_diag": runtime_diag,
            "runtime_startup": dict(startup_state),
            "monitor": {
                "entry": monitor_entry,
                "close": {"dominant_close_reason": "none"},
            },
        }
        # AGENT STATE: The runtime patch is the bridge truth for ops, dashboard, and later twin/live validation.
        svc.patch_state(state_patch)

        svc.store_decisions(
            decisions=decisions,
            vol=0.0,
            diagnostics={
                "runtime": "fxstack",
                "pairs": pairs,
                "loop_ts": loop_ts,
                "rejection_stats": rejection_counts,
                "active_model_sets": sorted(list(model_sets.keys())),
                "policy_version": str(s.policy_version),
                "edge_formula_id": EDGE_FORMULA_ID,
                "runtime_diag": runtime_diag,
            },
        )

        startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
        if not runtime_running:
            runtime_running = True
            _startup_log("main_loop_ready")

        time.sleep(max(1, int(sleep_secs)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run fxstack runtime loop")
    ap.add_argument("--config", default="")
    ap.add_argument("--equity", type=float, required=True)
    ap.add_argument("--sleep", type=int, default=10)
    ap.add_argument("--feature-root", default="fx-quant-stack/data/features")
    _ = ap.parse_args()

    run_loop(equity=_.equity, sleep_secs=_.sleep, feature_root=_.feature_root)


if __name__ == "__main__":
    main()
