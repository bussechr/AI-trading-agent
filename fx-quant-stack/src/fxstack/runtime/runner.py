# AGENT: ROLE: Live runtime orchestrator: startup bootstrap, feature refresh, scoring, lifecycle, adaptive policy, and final command submission.
# AGENT: ENTRYPOINT: isolated `python -I -m fxstack.runtime.runner` via `ops/windows/21_start_runtime.bat`.
# AGENT: PRIMARY INPUTS: settings, active model manifest, bridge ticks/bars, feature parquet rows, bridge state.
# AGENT: PRIMARY OUTPUTS: command submissions, runtime state patches, persisted decisions, runtime diagnostics.
# AGENT: DEPENDS ON: `fxstack/runtime/service.py`, `fxstack/live/scorer.py`, `fxstack/live/policy.py`, `fxstack/strategy/adaptive_policy.py`.
# AGENT: CALLED BY: `src/trader/cli.py`, `ops/windows/21_start_runtime.bat`.
# AGENT: STATE / SIDE EFFECTS: writes runtime state, queues broker commands, refreshes local feature tail state, tracks adaptive registries.
# AGENT: HANDSHAKES: `/v2/ready`, bridge ticks/bars fetch, command queue submit/ack, dashboard-facing state patch.
# AGENT: SEE: `docs/agents/runtime-loop.md` -> `fx-quant-stack/src/fxstack/runtime/service.py` -> `docs/agents/bridge-and-api-handshakes.md`
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import math
import os
import re
import signal
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pandas as pd

from fxstack.strategy.adaptive_policy import (
    PLAYBOOK_BREAKOUT_EXPANSION,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
    PLAYBOOK_NO_TRADE,
    PLAYBOOK_RANGE_MEAN_REVERSION,
    PLAYBOOK_TREND_PULLBACK,
    _evaluate_adaptive_entry_with_quality_override,
    _reversal_blocking_reasons,
    adaptive_lifecycle_decision,
    adaptive_reentry_block,
    adaptive_replacement_keep_score,
    attach_adaptive_context,
    evaluate_adaptive_entry,
    parse_enabled_playbooks,
)
from fxstack.belief import build_cross_pair_influence_records
from fxstack.belief.engine import (
    compute_directional_belief,
    empty_directional_belief,
    load_directional_belief_model_set,
    validate_directional_belief_artifact_contract,
)
from fxstack.data.live_quotes import (
    fetch_market_bars,
    fetch_market_ready,
    fetch_market_ticks,
)
from fxstack.features.fx_lifecycle import add_fx_lifecycle_features
from fxstack.features.multi_tf_contract import build_latest_multi_tf_row, build_multi_tf_rows, resample_bars
from fxstack.features.session_contract import feature_contract_mismatches
from fxstack.io.parquet_store import ParquetStore
from fxstack.live.policy import (
    EDGE_FORMULA_ID,
    infer_pip_size,
    normalize_spread_bps,
    normalize_strategy_engine_mode,
    session_bucket_from_ts,
)
from fxstack.live.scorer import LiveScorer
from fxstack.settings import get_settings, unknown_fxstack_env_warnings
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
from fxstack.strategy.complementarity import (
    ComplementaritySnapshot,
    evaluate_sleeve_complementarity,
)
from fxstack.strategy.desk_overlay import build_desk_overlay
from fxstack.strategy.desk_overlay_types import DeskOverlayInputs
from fxstack.strategy.sleeve_governance import (
    SleeveGovernanceTracker,
    serialize_sleeve_snapshots,
    sleeve_entry_block_reason,
    sleeve_expectancy_allocation_scale,
)
from fxstack.mlops.local_artifact import (
    normalize_artifact_ref,
    resolve_model_artifact_path,
)
from fxstack.models.artifact_contract import artifact_lock, validate_artifact_contract
from fxstack.orchestration.context_builder import (
    build_decision_context,
    build_idempotency_key,
    build_version_bundle,
)
from fxstack.orchestration.graph_runtime import ShadowGraphRuntime
from fxstack.orchestration.schema_version import ORCHESTRATION_SCHEMA_VERSION
from fxstack.orchestration.telemetry import (
    record_persistence_failure as _record_orchestration_persistence_failure,
    record_run as _record_orchestration_run,
    start_span as _orchestration_span,
)
from fxstack.feast.online_features import FeatureServingTelemetry, resolve_latest_feature_row
from fxstack.portfolio import build_portfolio_telemetry, evaluate_portfolio_allocation
from fxstack.providers.registry import provider_capabilities, provider_roles_from_settings
from fxstack.risk import (
    MarketState,
    PolicyIntent,
    PortfolioState,
    RiskContext,
    RiskEnvelope,
    RiskKernelConfig,
    default_envelope,
    evaluate_risk_decision,
)
from fxstack.risk.kernel import ROLLOUT_EXECUTION_MODES
from fxstack.risk.sizing import (
    account_value_per_price_unit,
    drawdown_scaled_fraction,
    kelly_fraction,
)
from fxstack.rl.checkpoint import RLLinearCheckpoint
from fxstack.rl.proposal import build_portfolio_rl_proposal_bundle
from fxstack.runtime.governance import (
    ProviderHealthSnapshot,
    capital_band_budget_scale,
    compute_binding_capital_governance_snapshot,
)
from fxstack.runtime.release_authority import (
    RELEASE_AUTHORITY_ACK_SCHEMA,
    RELEASE_AUTHORITY_STATE_SCHEMA,
    active_authority_errors,
    authority_request_errors,
    load_signed_build_provenance,
    manifest_model_identity,
    runtime_config_sha256,
)
from fxstack.runtime.startup_preflight import validate_runtime_startup
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
    component_feature_services: dict[str, Any] = field(default_factory=dict)
    rollout_policy: dict[str, Any] = field(default_factory=dict)
    artifact_identities: dict[str, Any] = field(default_factory=dict)
    rl_checkpoint_path: str = ""
    rl_checkpoint_content_sha256: str = ""




def _runtime_rl_checkpoint_ref(
    *,
    artifacts: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    for key in ("portfolio_rl", "rl_policy", "rl_checkpoint", "offline_rl"):
        raw_ref = artifacts.get(key)
        if str(_artifact_path(raw_ref) or "").strip():
            ref = normalize_artifact_ref(raw_ref)
            if isinstance(raw_ref, dict) and not str(
                ref.get("content_sha256") or ""
            ).strip():
                ref["content_sha256"] = str(
                    raw_ref.get("content_sha256")
                    or raw_ref.get("checkpoint_content_sha256")
                    or ""
                )
            return ref
    raw_ref = metadata.get("rl_checkpoint")
    if str(_artifact_path(raw_ref) or "").strip():
        ref = normalize_artifact_ref(raw_ref)
        if isinstance(raw_ref, dict) and not str(
            ref.get("content_sha256") or ""
        ).strip():
            ref["content_sha256"] = str(
                raw_ref.get("content_sha256")
                or raw_ref.get("checkpoint_content_sha256")
                or ""
            )
        return ref
    legacy_path = str(metadata.get("rl_checkpoint_path") or "").strip()
    if not legacy_path:
        return {}
    legacy_digest = str(
        metadata.get("rl_checkpoint_content_sha256") or ""
    )
    ref = normalize_artifact_ref(
        {
            "path": legacy_path,
            "content_sha256": legacy_digest,
        }
    )
    ref["content_sha256"] = legacy_digest
    return ref


def _validate_runtime_rl_checkpoint_ref(
    ref: dict[str, Any],
    *,
    project_root: Path,
) -> tuple[str, str]:
    path_text = str(ref.get("path") or "").strip()
    model_uri = str(ref.get("model_uri") or "").strip()
    if not path_text:
        if model_uri:
            raise ValueError(
                "nonlocal RL checkpoint references are unsupported; "
                "activate an exact local checkpoint file"
            )
        raise ValueError("configured RL checkpoint path is missing")
    scheme = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", path_text)
    windows_drive = re.match(r"^[A-Za-z]:[\\/]", path_text)
    if scheme is not None and windows_drive is None:
        raise ValueError(
            "nonlocal RL checkpoint references are unsupported; "
            "activate an exact local checkpoint file"
        )
    if ref.get("runtime_compatible") is False:
        raise ValueError("configured RL checkpoint is runtime-incompatible")
    expected_sha256 = str(ref.get("content_sha256") or "").strip().lower()
    if (
        len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise ValueError(
            "configured RL checkpoint content_sha256 is missing or malformed; "
            "reactivation with publisher identity is required"
        )
    resolved = _resolve_optional_path(path_text, project_root)
    if resolved is None or not resolved.is_file():
        raise FileNotFoundError(f"configured RL checkpoint not found: {path_text}")
    payload = resolved.read_bytes()
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError(
            "configured RL checkpoint content_sha256 mismatch: "
            f"expected={expected_sha256},actual={actual_sha256}"
        )
    RLLinearCheckpoint.loads(payload)
    canonical_path = os.path.normcase(str(resolved.resolve()))
    return canonical_path, expected_sha256


# Carved into fxstack.runtime.artifact_paths. Re-bound under the original
# underscored names so the ~45 internal call sites continue to work unchanged.
from fxstack.runtime.artifact_paths import (
    artifact_path as _artifact_path,
    artifact_value as _artifact_value,
    resolve_optional_path as _resolve_optional_path,
    resolve_path as _resolve_path,
)


def _resolve_runtime_rl_checkpoint(
    *,
    model_sets: dict[str, LoadedModelSet],
    project_root: Path,
) -> tuple[Path | None, str]:
    identities: set[tuple[str, str]] = set()
    for loaded in list(model_sets.values() or []):
        raw = str(getattr(loaded, "rl_checkpoint_path", "") or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = project_root / path
        canonical_path = os.path.normcase(str(path.resolve(strict=False)))
        identities.add(
            (
                canonical_path,
                str(
                    getattr(loaded, "rl_checkpoint_content_sha256", "") or ""
                ).strip().lower(),
            )
        )
    if not identities:
        return None, ""
    if len(identities) != 1:
        detail = ";".join(
            f"{path}@{digest or '<missing>'}" for path, digest in sorted(identities)
        )
        raise RuntimeError(f"runtime RL checkpoint identity disagreement: {detail}")
    canonical_path, content_sha256 = next(iter(identities))
    return Path(canonical_path), content_sha256


# AGENT FLOW: Agent-mode scoping, live command admission, governed payload
# construction, and shadow-cycle capture now live in
# fxstack.runtime.orchestration_bridge. Names are re-bound under their original
# underscored aliases so existing call sites and tests keep working.
from fxstack.runtime.orchestration_bridge import (  # noqa: E402
    OPERATIONAL_HARD_ENTRY_BLOCK_REASONS as _OPERATIONAL_HARD_ENTRY_BLOCK_REASONS,
    build_command_id as _build_command_id,
    build_orchestration_live_runtime_diag as _build_orchestration_live_runtime_diag,
    build_orchestration_phase1_diag as _build_orchestration_phase1_diag,
    build_orchestration_snapshot_payload as _build_orchestration_snapshot_payload,
    capture_orchestration_cycle as _capture_orchestration_cycle,
    decision_meta_position_open as _decision_meta_position_open,
    get_orchestration_graph_runtime as _get_orchestration_graph_runtime,
    governed_action_for_risk_approved_payload as _governed_action_for_risk_approved_payload,
    governed_command_payload_for_mode as _governed_command_payload_for_mode,
    is_operational_hard_entry_block_reason as _is_operational_hard_entry_block_reason,
    live_command_admission_diagnostics as _live_command_admission_diagnostics,
    live_governed_command_payload as _live_governed_command_payload,
    live_mode_enabled as _live_mode_enabled,
    normalize_agent_mode as _normalize_agent_mode,
    orchestration_baseline_action as _orchestration_baseline_action,
    orchestration_cycle_id as _orchestration_cycle_id,
    orchestration_live_intent_enabled as _orchestration_live_intent_enabled,
    orchestration_live_pair_enabled as _orchestration_live_pair_enabled,
    orchestration_live_runtime_state as _orchestration_live_runtime_state,
    orchestration_live_sleeve_enabled as _orchestration_live_sleeve_enabled,
    orchestration_model_bundle_version as _orchestration_model_bundle_version,
    orchestration_paper_intent_enabled as _orchestration_paper_intent_enabled,
    orchestration_paper_pair_enabled as _orchestration_paper_pair_enabled,
    orchestration_paper_sleeve_enabled as _orchestration_paper_sleeve_enabled,
    orchestration_percentile as _orchestration_percentile,
    orchestration_shadow_pair_enabled as _orchestration_shadow_pair_enabled,
    paper_command_preview_payload as _paper_command_preview_payload,
    paper_governed_command_payload as _paper_governed_command_payload,
    paper_mode_enabled as _paper_mode_enabled,
    paper_mode_rollback as _paper_mode_rollback,
    payload_from_approved_order as _payload_from_approved_order,
    reconcile_governed_payload as _reconcile_governed_payload,
    safe_authority_revision as _safe_authority_revision,
    stamp_orchestration_payload as _stamp_orchestration_payload,
    update_orchestration_shadow_command_flow as _update_orchestration_shadow_command_flow,
    validate_final_entry_payload_against_risk_approval as _validate_final_entry_payload_against_risk_approval,
    validate_final_lifecycle_payload_against_risk_approval as _validate_final_lifecycle_payload_against_risk_approval,
)



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
_FEATURE_SERVING_TIMEFRAME_PREFERENCE = ("M5", "D", "H4")


def _record_feature_serving_telemetry(pair: str, timeframe: str, telemetry: FeatureServingTelemetry) -> None:
    _FEATURE_SERVING_TELEMETRY[(str(pair).upper(), str(timeframe).upper())] = telemetry.to_dict()


def _feature_serving_snapshot() -> dict[str, Any]:
    if not _FEATURE_SERVING_TELEMETRY:
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
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for (pair, timeframe), telemetry in _FEATURE_SERVING_TELEMETRY.items():
        grouped[str(pair).upper()].append((str(timeframe).upper(), dict(telemetry or {})))

    selected: list[dict[str, Any]] = []
    selected_pairs: list[str] = []
    selected_timeframes: dict[str, str] = {}
    all_stale_pairs: list[str] = []
    all_stale_timeframes: list[str] = []
    for (pair, timeframe), telemetry in _FEATURE_SERVING_TELEMETRY.items():
        if bool(dict(telemetry or {}).get("stale", False)):
            all_stale_pairs.append(str(pair).upper())
            all_stale_timeframes.append(str(timeframe).upper())
    for pair in sorted(grouped):
        entries = list(grouped[pair])
        chosen_timeframe = ""
        chosen_payload: dict[str, Any] = {}
        for timeframe in _FEATURE_SERVING_TIMEFRAME_PREFERENCE:
            match = next((payload for tf, payload in entries if tf == timeframe), None)
            if match is not None:
                chosen_timeframe = timeframe
                chosen_payload = dict(match)
                break
        if not chosen_payload and entries:
            chosen_timeframe, chosen_payload = entries[-1]
        if not chosen_payload:
            continue
        chosen_payload.setdefault("timeframe", chosen_timeframe)
        chosen_payload.setdefault("pair", pair)
        selected.append(chosen_payload)
        selected_pairs.append(pair)
        selected_timeframes[pair] = str(chosen_timeframe or chosen_payload.get("timeframe") or "")

    if not selected:
        latest_key, latest_payload = next(reversed(list(_FEATURE_SERVING_TELEMETRY.items())))
        latest_pair, latest_timeframe = str(latest_key[0]).upper(), str(latest_key[1]).upper()
        latest = dict(latest_payload or {})
        selected = [latest]
        selected_pairs = [latest_pair]
        selected_timeframes = {latest_pair: latest_timeframe}
        if bool(latest.get("stale", False)):
            all_stale_pairs = list(selected_pairs)
            all_stale_timeframes = [latest_timeframe]

    source_chain: list[str] = []
    for item in selected:
        for source in list(item.get("source_chain") or []):
            txt = str(source or "").strip()
            if txt and txt not in source_chain:
                source_chain.append(txt)
    if not source_chain:
        source_chain = ["feast_online", "parquet_fallback", "raw_contract_fallback"]
    sources = {str(item.get("source") or "").strip() for item in selected if str(item.get("source") or "").strip()}
    feature_services = [str(item.get("feature_service") or "").strip() for item in selected if str(item.get("feature_service") or "").strip()]
    freshness_values = [
        float(item.get("freshness_secs"))
        for item in selected
        if item.get("freshness_secs") is not None and str(item.get("freshness_secs")).strip() != ""
    ]
    cache_hits = [bool(item.get("cache_hit", False)) for item in selected]
    selected_stale_count = sum(1 for item in selected if bool(item.get("stale", False)))
    all_stale_count = sum(1 for item in _FEATURE_SERVING_TELEMETRY.values() if bool(dict(item or {}).get("stale", False)))
    selected_stale = bool(selected_stale_count)
    aggregate_source = "mixed" if len(sources) > 1 else (next(iter(sources)) if sources else "")
    aggregate_feature_service = feature_services[0] if len(feature_services) == 1 else (feature_services[0] if feature_services else "")
    return {
        "source": aggregate_source,
        "source_chain": source_chain,
        "feature_service": aggregate_feature_service,
        "cache_hit": bool(all(cache_hits)) if cache_hits else False,
        "freshness_secs": (max(freshness_values) if freshness_values else selected[0].get("freshness_secs")),
        "stale": bool(selected_stale),
        "reason": "ok" if not selected_stale else "feature_serving_stale",
        "details": {
            "selection_policy": "per_pair_prefer_M5_D_H4",
            "selected_pairs_count": int(len(selected_pairs)),
            "selected_pairs": list(selected_pairs),
            "selected_timeframes": dict(selected_timeframes),
            "selected_stale_count": int(selected_stale_count),
            "all_stale_count": int(all_stale_count),
            "all_stale_pairs": sorted(dict.fromkeys(all_stale_pairs)),
            "all_stale_timeframes": sorted(dict.fromkeys(all_stale_timeframes)),
            "freshness_secs_min": (min(freshness_values) if freshness_values else None),
            "freshness_secs_max": (max(freshness_values) if freshness_values else None),
            "freshness_secs_avg": (sum(freshness_values) / len(freshness_values) if freshness_values else None),
            "selected_source_count": int(len(sources)),
        },
    }


def _feature_serving_runtime_diag() -> dict[str, Any]:
    feature_serving_by_pair = dict(sorted(((f"{pair}:{tf}", value) for (pair, tf), value in _FEATURE_SERVING_TELEMETRY.items())))
    return {
        "feature_serving": _feature_serving_snapshot(),
        "feature_serving_by_pair": feature_serving_by_pair,
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
        series = pd.to_numeric(pd.Series(series), errors="coerce")
        if "ts" in frame.columns:
            timestamps = pd.to_datetime(frame["ts"], utc=True, errors="coerce")
            valid = series.notna() & timestamps.notna()
            series = pd.Series(
                series.loc[valid].to_numpy(dtype=float),
                index=pd.DatetimeIndex(timestamps.loc[valid]),
                dtype=float,
            )
            series = series[~series.index.duplicated(keep="last")].sort_index().tail(tail_rows)
        elif isinstance(series.index, pd.DatetimeIndex):
            series = series.dropna()
            series = series[~series.index.duplicated(keep="last")].sort_index().tail(tail_rows)
        else:
            series = series.dropna().tail(tail_rows).reset_index(drop=True)
        if series.empty:
            continue
        returns_by_pair[symbol] = series.astype(float)
    return returns_by_pair


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


_RECOVERABLE_ADAPTIVE_REJECTION_REASONS = {
    "low_adaptive_quality",
    "low_trigger_score",
    "low_playbook_score",
    "low_location_score",
}


def _adaptive_quality_recovery_ready(*, signal: Any, settings: Any) -> bool:
    trade_prob = float(_safe_float(getattr(signal, "trade_prob", 0.0), 0.0))
    expected_edge_bps = float(_safe_float(getattr(signal, "expected_edge_bps", 0.0), 0.0))
    entry_quality = float(_safe_float(getattr(signal, "entry_quality_score", 0.0), 0.0))
    model_intelligence = float(_safe_float(getattr(signal, "model_intelligence_score", 0.0), 0.0))
    directional_confidence = float(_safe_float(getattr(signal, "directional_swing_confidence", 0.0), 0.0))
    belief_rank = float(_safe_float(getattr(signal, "belief_primary_rank_score", 0.0), 0.0))
    belief_strength = max(
        belief_rank,
        float(_safe_float(getattr(signal, "belief_primary_score", 0.0), 0.0)),
        float(_safe_float(getattr(signal, "belief_primary_ev_above_hurdle_prob", 0.0), 0.0)),
    )
    heuristic_penalty = float(_safe_float(getattr(signal, "heuristic_penalty_score", 0.0), 0.0))
    min_trade_prob = float(_safe_float(getattr(settings, "min_trade_prob", 0.60), 0.60))
    min_expected_edge_bps = float(_safe_float(getattr(settings, "min_expected_edge_bps", 0.0), 0.0))
    trade_floor = max(0.60, min_trade_prob)
    standard_recovery_ready = bool(
        trade_prob >= trade_floor
        and expected_edge_bps > 0.0
        and model_intelligence >= 0.55
        and heuristic_penalty <= 0.45
        and max(entry_quality, directional_confidence, belief_strength) >= 0.55
    )
    high_conviction_recovery_ready = bool(
        trade_prob >= max(0.45, trade_floor - 0.15)
        and expected_edge_bps >= (min_expected_edge_bps + max(0.75, 0.25 * max(min_expected_edge_bps, 1.0)))
        and model_intelligence >= 0.72
        and heuristic_penalty <= 0.40
        and max(entry_quality, directional_confidence, belief_strength) >= 0.60
    )
    return bool(standard_recovery_ready or high_conviction_recovery_ready)


def _adaptive_recovery_reason(*, signal: Any, settings: Any) -> str:
    signal_rejection_reason = str(getattr(signal, "rejection_reason", "") or "")
    if not signal_rejection_reason or bool(getattr(signal, "allowed", False)):
        return ""
    if signal_rejection_reason not in _RECOVERABLE_ADAPTIVE_REJECTION_REASONS:
        return ""
    if not _adaptive_quality_recovery_ready(signal=signal, settings=settings):
        return ""
    return signal_rejection_reason


def _risk_kernel_lifecycle_inputs(
    *,
    has_open_position: bool,
    lifecycle_action: str,
    lifecycle_reason: str,
    lifecycle_action_score: float,
    close_lots: float,
    sl_price: float,
    tp_price: float,
    signal: Any,
    entry_ready: bool,
) -> dict[str, float | str]:
    if has_open_position:
        return {
            "lifecycle_action": str(lifecycle_action),
            "lifecycle_reason": str(lifecycle_reason),
            "lifecycle_action_score": float(lifecycle_action_score),
            "close_lots": float(close_lots),
            "sl_price": float(sl_price),
            "tp_price": float(tp_price),
        }
    return {
        "lifecycle_action": "entry",
        "lifecycle_reason": "entry_approved" if bool(entry_ready) else "entry_pending_eval",
        "lifecycle_action_score": float(_safe_float(getattr(signal, "trade_prob", 0.0), 0.0)),
        "close_lots": 0.0,
        "sl_price": float(sl_price),
        "tp_price": float(tp_price),
    }


#: Risk per entry as a fraction of equity when the kernel sizes from the stop.
#: 0.5% is deliberately conservative: it is the first time this stack has had a
#: stated risk-per-trade at all, and the probabilities that would justify a
#: larger fraction are not yet calibrated out-of-sample.
DEFAULT_ENTRY_RISK_FRACTION = 0.005


#: Kelly can only ever CUT size here, and never below this share of base. Sizing
#: scales a decision; it does not re-litigate it. Entry-versus-abstain already
#: belongs to the adaptive utility policy and its conjunctive floors, so a thin
#: but approved edge trades small rather than not at all.
MIN_KELLY_SIZE_SCALE = 0.25

#: Quarter-Kelly fraction of a REFERENCE trade -- p=0.55 at 1.5:1 reward/risk.
#: f* = (0.55*2.5 - 1)/1.5 = 0.25, quartered = 0.0625.
#:
#: This exists because Kelly-as-a-ceiling was near-inert. Capped at a 0.5% base,
#: quarter-Kelly exceeds the cap for any edge better than roughly p=0.51 at 1:1,
#: so size pinned to the base and a 0.53 setup and a 0.75 setup were funded
#: identically. Expressing conviction is the whole point of Kelly; a ceiling
#: that binds only within a hair of breakeven expresses nothing.
#:
#: Ratioing against this reference restores the gradient across the range that
#: actually occurs.
KELLY_REFERENCE_FRACTION = 0.0625

#: Upper bound on the conviction multiplier. Pinned at 1.0 ON PURPOSE: scaling
#: ABOVE the configured base would size up on probabilities that have not been
#: calibrated out-of-sample, and this stack's own validation put EURUSD at
#: PBO 0.433 / DSR 0.147. The mechanism is two-sided and ready; raise this via
#: FXSTACK_MAX_CONVICTION_SIZE_SCALE once the probabilities have earned it.
DEFAULT_MAX_CONVICTION_SIZE_SCALE = 1.0


def _return_values(realized_returns: Any) -> list[float]:
    """Coerce a pandas Series / sequence of returns into finite floats.

    Returns ``[]`` for anything unusable so the volatility forecast reports "no
    estimate" rather than a number derived from junk.
    """

    if realized_returns is None:
        return []
    try:
        raw = (
            realized_returns.to_numpy(dtype=float).tolist()
            if isinstance(realized_returns, pd.Series)
            else [float(item) for item in realized_returns]
        )
    except (AttributeError, TypeError, ValueError):
        return []
    return [value for value in raw if math.isfinite(value)]


def _reward_risk_ratio(*, entry_price: float, sl_price: float, tp_price: float) -> float:
    """Reward-to-risk of the actual bracket, or ``0.0`` when not derivable."""

    entry = _safe_float(entry_price, 0.0)
    stop = _safe_float(sl_price, 0.0)
    target = _safe_float(tp_price, 0.0)
    if entry <= 0.0 or stop <= 0.0 or target <= 0.0:
        return 0.0
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk <= 0.0 or reward <= 0.0:
        return 0.0
    return float(reward / risk)


def _entry_risk_fraction(
    *,
    settings: Any,
    drawdown_pct: float = 0.0,
    win_probability: float = 0.0,
    reward_risk_ratio: float = 0.0,
    realized_returns: Any = None,
) -> float:
    """Risk fraction per entry, overridable via ``FXSTACK_ENTRY_RISK_FRACTION``.

    Exactly TWO multipliers compose here, and they are orthogonal because they
    read different state:

    * **Kelly** reads *edge*: the win probability and the bracket's reward-risk.
      Expressed against :data:`KELLY_REFERENCE_FRACTION` and floored at
      :data:`MIN_KELLY_SIZE_SCALE`, so a weak edge sizes down but an approved
      entry is never silently zeroed.
    * **Drawdown scaling** reads the *account*. Without it the fraction is flat
      all the way to ``risk_max_drawdown_pct``, where the kernel stops entries
      outright -- full size through the entire losing run, then a cliff.

    Instrument volatility is DELIBERATELY ABSENT. It is already handled
    downstream: ``lots_for_risk`` divides by an ATR-scaled stop, so a second
    normalisation here double-counts it -- see the measured numbers at the
    volatility comment below. ``realized_returns`` is retained in the signature
    only so callers do not have to change; it is intentionally unused.

    Every argument beyond ``settings`` is optional and degrades to the previous
    behaviour when absent, so a caller that cannot supply edge inputs gets
    exactly the unscaled base. The kernel's hard limits still bind; this only
    governs the approach to them.
    """

    value = _safe_float(getattr(settings, "entry_risk_fraction", DEFAULT_ENTRY_RISK_FRACTION), 0.0)
    if not math.isfinite(value) or value <= 0.0:
        value = DEFAULT_ENTRY_RISK_FRACTION
    # Hard ceiling: a mis-set env var must not be able to risk the account.
    base = float(min(value, 0.02))

    probability = _safe_float(win_probability, 0.0)
    payoff = _safe_float(reward_risk_ratio, 0.0)
    if 0.0 < probability < 1.0 and payoff > 0.0:
        # Compute Kelly UNCAPPED, then express it as a multiplier against a
        # reference trade. Capping it at `base` first is what made it inert:
        # every decent edge clipped to the same number, so size carried no
        # information about conviction.
        kelly = kelly_fraction(
            win_probability=probability,
            reward_risk_ratio=payoff,
            max_fraction=1.0,
        )
        max_scale = _safe_float(
            getattr(settings, "max_conviction_size_scale", DEFAULT_MAX_CONVICTION_SIZE_SCALE),
            DEFAULT_MAX_CONVICTION_SIZE_SCALE,
        )
        if not math.isfinite(max_scale) or max_scale <= 0.0:
            max_scale = DEFAULT_MAX_CONVICTION_SIZE_SCALE
        conviction_scale = kelly / KELLY_REFERENCE_FRACTION
        conviction_scale = min(max(conviction_scale, MIN_KELLY_SIZE_SCALE), max_scale)
        base = float(base * conviction_scale)

    # NO volatility targeting here. It was added on 2026-07-31 and removed the
    # same day, because it double-normalises instrument volatility.
    #
    # `lots_for_risk` sizes as equity * risk_fraction / (stop_distance * vpu),
    # and the stop distance is FXSTACK_ENTRY_STOP_ATR_MULTIPLE * ATR14. That
    # already holds money-at-risk constant across instrument volatility: when
    # ATR doubles, the stop doubles and lots halve. Multiplying risk_fraction by
    # target_vol/forecast_vol divides by instrument volatility a SECOND time.
    #
    # Measured on 3,100 samples of real EURUSD M15, realised risk per stop-out:
    #     ATR-stop sizing only      mean $49.07   CV 0.0215
    #     + volatility targeting    mean $11.58   CV 0.0610  (+184%)
    # It made realised risk nearly 3x MORE erratic -- the opposite of the point
    # -- and cut size 76%, because the target sits far below actual M15 realised
    # vol so the multiplier pins near its floor.
    #
    # The docstring of `drawdown_scaled_fraction` already warned about exactly
    # this: it argues drawdown scaling is safe to stack BECAUSE it reads ACCOUNT
    # state rather than instrument state. Volatility targeting reads instrument
    # state, which the ATR-scaled stop already owns.
    #
    # `volatility_targeted_fraction` remains correct and tested in risk/sizing.py
    # -- it belongs on a sizing path whose stop is NOT volatility-scaled.

    return float(
        drawdown_scaled_fraction(
            base_fraction=base,
            drawdown_pct=_safe_float(drawdown_pct, 0.0),
            max_drawdown_pct=_safe_float(getattr(settings, "risk_max_drawdown_pct", 0.0), 0.0),
        )
    )


def _risk_sizing_available(
    *,
    has_open_position: bool,
    tick: Any,
    side: Any,
    sl_price: Any,
    equity: Any,
) -> bool:
    """True when the kernel can convert a risk fraction into lots.

    Requires a fresh entry (protection is only computed for those), a usable
    entry price from the tick, a positive stop, and positive equity. Anything
    missing means the legacy lot path is used instead -- never a blocked order.
    """

    if bool(has_open_position):
        return False
    if _safe_float(equity, 0.0) <= 0.0:
        return False
    stop = _safe_float(sl_price, 0.0)
    if stop <= 0.0:
        return False
    quote = dict(tick or {}).get("ask" if str(side).upper() == "BUY" else "bid")
    entry = _safe_float(quote, 0.0)
    if entry <= 0.0:
        return False
    return abs(entry - stop) > 0.0


def _entry_protection_prices(
    *,
    pair: str,
    side: str,
    tick: dict[str, Any],
    row: Any,
    settings: Any,
) -> tuple[dict[str, float | str], str]:
    """Construct mandatory broker-side SL/TP from quote and closed-bar ATR.

    In adaptive managed mode the take-profit is a distant broker-side fail-safe;
    normal profit taking belongs to the lifecycle partial/exit path.  The stop
    geometry is identical in both modes.
    """

    side_up = str(side or "").strip().upper()
    tick_payload = dict(tick or {})
    bid = float(_safe_float(tick_payload.get("bid"), 0.0))
    ask = float(_safe_float(tick_payload.get("ask"), 0.0))
    if side_up not in {"BUY", "SELL"}:
        return {}, "entry_protection_invalid_side"
    if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0.0 and ask >= bid):
        return {}, "entry_protection_missing_quote"

    row_get = getattr(row, "get", None)
    atr_raw = row_get("atr_14", 0.0) if callable(row_get) else 0.0
    atr = float(_safe_float(atr_raw, 0.0))
    stop_multiple = float(_safe_float(getattr(settings, "entry_stop_atr_multiple", 1.2), 0.0))
    target_multiple = float(_safe_float(getattr(settings, "entry_take_profit_atr_multiple", 1.5), 0.0))
    managed_runner_tp_r = float(
        _safe_float(getattr(settings, "managed_runner_tp_r_multiple", 0.0), 0.0)
    )
    managed_runner_mode = bool(
        getattr(settings, "adaptive_execution_enabled", False)
        and getattr(settings, "enable_lifecycle_actions", False)
        and managed_runner_tp_r >= 1.0
    )
    min_stop_pips = float(_safe_float(getattr(settings, "entry_min_stop_pips", 5.0), 0.0))
    if not math.isfinite(atr) or atr <= 0.0:
        return {}, "entry_protection_invalid_atr"
    if stop_multiple <= 0.0 or target_multiple <= 0.0 or min_stop_pips <= 0.0:
        return {}, "entry_protection_invalid_config"

    digits_raw = int(_safe_float(tick_payload.get("digits"), 0.0))
    digits = digits_raw if digits_raw in {2, 3, 4, 5} else None
    pip_size = float(infer_pip_size(pair=str(pair), digits=digits))
    point_default = pip_size / (10.0 if digits in {3, 5} else 1.0)
    point_size = float(_safe_float(tick_payload.get("point"), point_default))
    if point_size <= 0.0:
        point_size = point_default
    stops_level = max(
        0.0,
        *[
            float(_safe_float(tick_payload.get(name), 0.0))
            for name in ("stops_level", "stop_level", "trade_stops_level")
        ],
    )
    broker_distance = max(
        float(stops_level) * float(point_size),
        float(_safe_float(tick_payload.get("min_stop_distance"), 0.0)),
    )
    stop_distance = max(float(atr) * float(stop_multiple), float(min_stop_pips) * float(pip_size), broker_distance)
    reward_ratio = float(target_multiple) / float(stop_multiple)
    target_distance = max(float(atr) * float(target_multiple), float(stop_distance) * reward_ratio, broker_distance)
    entry_price = float(ask if side_up == "BUY" else bid)

    if side_up == "BUY":
        sl_price = min(entry_price - stop_distance, bid - broker_distance)
        tp_price = max(entry_price + target_distance, ask + broker_distance)
    else:
        sl_price = max(entry_price + stop_distance, ask + broker_distance)
        tp_price = min(entry_price - target_distance, bid - broker_distance)
    if digits is not None:
        quantum = 10.0 ** (-int(digits))
        entry_price = round(entry_price, digits)
        if side_up == "BUY":
            sl_price = math.floor((sl_price / quantum) + 1e-9) * quantum
            tp_price = math.ceil((tp_price / quantum) - 1e-9) * quantum
        else:
            sl_price = math.ceil((sl_price / quantum) - 1e-9) * quantum
            tp_price = math.floor((tp_price / quantum) + 1e-9) * quantum
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)

    # Broker distance and spread can move the resolved SL farther from the
    # selected-side entry than the ATR stop request.  Managed R must therefore
    # be measured from the final submitted SL, not the pre-resolution request.
    actual_stop_distance = abs(float(entry_price) - float(sl_price))
    if managed_runner_mode:
        managed_target_distance = max(
            abs(float(tp_price) - float(entry_price)),
            float(actual_stop_distance) * float(managed_runner_tp_r),
        )
        if side_up == "BUY":
            managed_tp = float(entry_price) + float(managed_target_distance)
            if digits is not None:
                quantum = 10.0 ** (-int(digits))
                managed_tp = math.ceil((managed_tp / quantum) - 1e-9) * quantum
                managed_tp = round(managed_tp, digits)
            tp_price = max(float(tp_price), float(managed_tp))
        else:
            managed_tp = float(entry_price) - float(managed_target_distance)
            if digits is not None:
                quantum = 10.0 ** (-int(digits))
                managed_tp = math.floor((managed_tp / quantum) + 1e-9) * quantum
                managed_tp = round(managed_tp, digits)
            tp_price = min(float(tp_price), float(managed_tp))

    actual_target_distance = abs(float(tp_price) - float(entry_price))
    if digits is not None:
        quantum = 10.0 ** (-int(digits))
        entry_ticks = int(round(float(entry_price) / quantum))
        stop_ticks = abs(int(round(float(sl_price) / quantum)) - entry_ticks)
        target_ticks = abs(int(round(float(tp_price) / quantum)) - entry_ticks)
        actual_stop_distance = float(stop_ticks) * float(quantum)
        actual_target_distance = float(target_ticks) * float(quantum)
        effective_reward_ratio = float(target_ticks) / max(float(stop_ticks), 1.0)
    else:
        effective_reward_ratio = float(actual_target_distance) / max(
            float(actual_stop_distance), 1e-12
        )

    valid = (
        math.isfinite(sl_price)
        and math.isfinite(tp_price)
        and sl_price > 0.0
        and tp_price > 0.0
        and (
            (side_up == "BUY" and sl_price < bid <= ask < tp_price)
            or (side_up == "SELL" and tp_price < bid <= ask < sl_price)
        )
    )
    if not valid:
        return {}, "entry_protection_invalid_prices"
    return (
        {
            "entry_price": float(entry_price),
            "sl_price": float(sl_price),
            "tp_price": float(tp_price),
            "atr_14": float(atr),
            "stop_distance": float(actual_stop_distance),
            "target_distance": float(actual_target_distance),
            "reward_ratio": float(effective_reward_ratio),
            "managed_runner_tp_r_multiple": float(
                managed_runner_tp_r if managed_runner_mode else 0.0
            ),
            "protection_mode": (
                "managed_runner_fail_safe"
                if managed_runner_mode
                else "fixed_atr_target"
            ),
            "broker_min_distance": float(broker_distance),
            "source": "closed_bar_atr_14",
        },
        "",
    )


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
    canonical_keys = [
        "main_runtime_rollout",
        "phase5_runtime_rollout",
        "runtime_rollout",
    ]
    legacy_keys = [
        "phase5_rollout",
        "rollout",
        "canary",
    ]
    selected_keys = canonical_keys if any(isinstance(metadata.get(key), dict) and metadata.get(key) for key in canonical_keys) else [*canonical_keys, *legacy_keys]
    for key in selected_keys:
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
    enabled_locked = False
    for source, section in sections:
        if not resolved["source"]:
            resolved["source"] = str(source)
        if str(resolved.get("mode") or "").strip() == "":
            default_mode = "canary" if str(source).strip().lower() == "canary" else ""
            resolved["mode"] = str(section.get("mode") or section.get("rollout_mode") or default_mode).strip().lower()
        if not enabled_locked:
            resolved["enabled"] = bool(section.get("enabled", section.get("active", True)))
            enabled_locked = True
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
    # `live` is a rollout too -- see ROLLOUT_EXECUTION_MODES. Spelling this as
    # `mode == "canary"` made a graduated pair resolve to configured=False /
    # active=False, which the submission gates read as "rollout inactive" and
    # refused every order on.
    configured = bool(mode in ROLLOUT_EXECUTION_MODES and sections)
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


# Carved into fxstack.runtime.artifact_paths. Re-bound under original name.
from fxstack.runtime.artifact_paths import load_artifact_meta as _load_artifact_meta  # noqa: E402


def _required_model_feature_columns(*models: Any) -> list[str]:
    cols: list[str] = []
    for model in models:
        for col in list(getattr(model, "feature_columns", []) or []):
            txt = str(col or "").strip()
            if txt and txt not in cols:
                cols.append(txt)
    return cols


_META_ENRICHED_INPUT_COLUMNS = {
    "regime_prob",
    "swing_prob",
    "entry_prob",
    "candidate_side",
    "side_long",
    "side_short",
}


def _startup_intraday_required_columns(loaded: LoadedModelSet) -> list[str]:
    cols = _required_model_feature_columns(getattr(loaded.scorer, "intraday_model", None))
    meta_model = getattr(loaded.scorer, "meta_model", None)
    for col in list(getattr(meta_model, "feature_columns", []) or []):
        txt = str(col or "").strip()
        if not txt or txt in _META_ENRICHED_INPUT_COLUMNS:
            continue
        if txt not in cols:
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


# Shared coercion helpers — see fxstack.runtime._util for the canonical impl.
# Re-bound under the original underscored names so 100+ existing call sites
# in this module continue to work unchanged.
from fxstack.runtime._util import (
    clip01 as _clip01,
    safe_float as _safe_float,
)


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


# Risk-envelope singleton lives in fxstack.runtime.decisions. Re-bind the
# accessor under its original underscored name so the rest of this module
# (and tests that imported from runner) keep working.
from fxstack.runtime.decisions import (
    runtime_risk_envelope as _runtime_risk_envelope,
    set_runtime_risk_envelope,
)


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
        require_entry_protection=True,
        rollout_mode=(str(rollout.get("mode") or "") if rollout_enabled else ""),
        rollout_pair_allowlisted=bool(rollout_enabled and rollout.get("pair_allowlisted", False)),
        rollout_budget_scale=float(_clip01(rollout.get("budget_scale", 1.0))) if rollout_enabled else 1.0,
        rollout_max_total_positions=max(0, int(rollout.get("max_total_positions", 0) or 0)) if rollout_enabled else 0,
        rollout_max_pair_positions=max(0, int(rollout.get("max_pair_positions", 0) or 0)) if rollout_enabled else 0,
        rollout_max_gross_exposure=float(_safe_float(rollout.get("max_gross_exposure"), 0.0)) if rollout_enabled else 0.0,
        rollout_max_net_exposure=float(_safe_float(rollout.get("max_net_exposure"), 0.0)) if rollout_enabled else 0.0,
    )




def _submission_has_active_queue_record(enqueue_out: dict[str, Any] | None) -> bool:
    status = str(dict(enqueue_out or {}).get("status") or "").strip().lower()
    if status == "queued":
        return True
    if status == "duplicate":
        existing_state = str(dict(enqueue_out or {}).get("state") or "").strip().lower()
        return existing_state in {"queued", "delivered"}
    return False


def _submission_is_accepted(enqueue_out: dict[str, Any] | None) -> bool:
    # Queue admission is positive evidence, so fail closed on every status the
    # runtime does not explicitly understand (including 403/409/503 replies).
    return _submission_has_active_queue_record(enqueue_out)


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


def _resolve_hard_lifecycle_floor(
    *,
    lifecycle_action: str,
    lifecycle_reason: str,
    lifecycle_action_score: float,
    close_lots: float,
    sl_price: float,
    hard_lifecycle_action: str,
    hard_lifecycle_reason: str,
    hard_lifecycle_action_score: float,
    hard_lifecycle_close_lots: float = 0.0,
    hard_lifecycle_sl_price: float = 0.0,
) -> dict[str, Any]:
    """Apply only monotonic hard protection over one canonical lifecycle intent."""

    action = str(lifecycle_action or "hold").strip().lower()
    reason = str(lifecycle_reason or "hold")
    score = float(_safe_float(lifecycle_action_score, 0.0))
    planned_close_lots = float(_safe_float(close_lots, 0.0))
    planned_sl_price = float(_safe_float(sl_price, 0.0))
    hard_action = str(hard_lifecycle_action or "hold").strip().lower()

    if hard_action == "exit":
        return {
            "lifecycle_action": "exit",
            "lifecycle_reason": str(hard_lifecycle_reason or "hard_lifecycle_exit"),
            "lifecycle_action_score": max(score, float(_safe_float(hard_lifecycle_action_score, 1.0))),
            "close_lots": float(_safe_float(hard_lifecycle_close_lots, 0.0)),
            "sl_price": 0.0,
            "hard_lifecycle_applied": True,
        }
    if hard_action == "tighten_stop" and action in {"hold", "tighten_stop"}:
        return {
            "lifecycle_action": "tighten_stop",
            "lifecycle_reason": str(hard_lifecycle_reason or "hard_lifecycle_tighten_stop"),
            "lifecycle_action_score": max(score, float(_safe_float(hard_lifecycle_action_score, 1.0))),
            "close_lots": 0.0,
            "sl_price": float(_safe_float(hard_lifecycle_sl_price, planned_sl_price)),
            "hard_lifecycle_applied": True,
        }
    return {
        "lifecycle_action": action,
        "lifecycle_reason": reason,
        "lifecycle_action_score": score,
        "close_lots": planned_close_lots,
        "sl_price": planned_sl_price,
        "hard_lifecycle_applied": False,
    }


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
    # Adaptive and RL routing runs after the first risk evaluation.  A changed
    # lifecycle intent is deliberately left unapproved until the final
    # re-approval pass; this helper must never fabricate a risk approval.
    action_item["approved_order"] = {}
    action_item["final_risk_approved"] = False
    action_item["final_risk_reapproval_required"] = True
    meta["approved_order"] = {}
    meta["final_lifecycle_risk_approved"] = False
    meta["final_lifecycle_risk_reapproval_required"] = True
    meta["lifecycle_action"] = str(lifecycle_action)
    meta["lifecycle_reason"] = str(lifecycle_reason)
    meta["close_lots"] = float(close_lots)
    meta["sl_price"] = float(sl_price)
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
    tp_price: float,
    rejection_reasons: list[str],
    state: dict[str, Any],
    settings: Any,
    portfolio_positions: list[dict[str, Any]] | None = None,
    rollout_policy: dict[str, Any] | None = None,
    governance_policy: dict[str, Any] | None = None,
    pending_entries: list[dict[str, Any]] | None = None,
    realized_returns_by_pair: dict[str, pd.Series] | None = None,
    quote_rates: dict[str, float] | None = None,
    entry_size_scale: float = 1.0,
) -> dict[str, Any]:
    has_open_position = bool(positions)
    # Intelligent sizing (adaptive_size_scale x sleeve_expectancy_scale) from
    # the final-approval call site. The legacy path receives it baked into
    # planned_entry_lots by the caller; the risk-fraction path zeroes planned
    # lots, so without this parameter a shrinking sleeve never actually shrank
    # on the path that sizes essentially every live entry.
    entry_size_scale = float(max(0.0, min(1.0, _safe_float(entry_size_scale, 1.0))))
    portfolio_positions = list(portfolio_positions or positions or [])
    rollout = dict(rollout_policy or {})
    agent_mode = _normalize_agent_mode(getattr(settings, "agent_mode", "off"))
    if agent_mode not in {"paper", "live"}:
        rollout = {}
    rollout_enabled = bool(rollout.get("enabled", rollout.get("active", False)))
    governance_meta = dict(governance_policy or {})
    signal_trade_prob = float(_safe_float(getattr(signal, "trade_prob", 0.0), 0.0))
    signal_uncertainty = float(
        _safe_float(
            getattr(signal, "uncertainty_score", max(0.0, 1.0 - signal_trade_prob)),
            max(0.0, 1.0 - signal_trade_prob),
        )
    )
    # Stop-risk pricing contract with build_portfolio_book -- see
    # _annotate_positions_with_contract_value. Without this the stress gate
    # reads worst_case_loss_proxy=0.0 forever.
    portfolio_positions = _annotate_positions_with_contract_value(
        portfolio_positions, quote_rates=quote_rates, settings=settings
    )
    portfolio_allocation = evaluate_portfolio_allocation(
        symbol=str(pair).upper(),
        session_bucket=str(getattr(signal, "session_bucket", "")),
        expected_edge_bps=float(_safe_float(expected_edge_bps, 0.0)),
        uncertainty_score=float(max(0.0, signal_uncertainty)),
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
    # Computed here rather than at PortfolioState construction below, because the
    # entry sizing handoff needs it too: risk-per-trade ramps down as drawdown
    # deepens instead of running flat into the kernel's hard limit. One value
    # feeds both, so the number that sizes the order is the number the kernel
    # judges.
    peak_equity = (
        float(_safe_float(state.get("equity_peak", state.get("cycle_peak_equity", current_equity)), current_equity))
        if isinstance(state, dict)
        else float(current_equity)
    )
    drawdown_pct = 0.0
    if peak_equity > 0.0 and current_equity > 0.0:
        drawdown_pct = max(0.0, (1.0 - (float(current_equity) / float(peak_equity))) * 100.0)
    entry_contract_value = _entry_contract_value(
        pair=pair, quote_rates=quote_rates, settings=settings
    )
    # The composed portfolio/governance budget scale must bind on BOTH sizing
    # paths. It used to multiply only the legacy ``requested_lots`` handoff,
    # which left the primary ``target_risk_pct`` path shipping the raw risk
    # fraction -- capital bands (micro_live 0.1 / low_risk_live 0.25) and
    # market-pressure de-rating (x0.45 / x0.80) were telemetry, not control,
    # on every risk-sized entry. The same scale now multiplies the risk
    # fraction the kernel sizes from; the unscaled value is kept in metadata
    # so telemetry can show both.
    risk_sizing_engaged = bool(
        entry_contract_value > 0.0
        and _risk_sizing_available(
            has_open_position=has_open_position,
            tick=tick,
            side=side,
            sl_price=sl_price,
            equity=current_equity,
        )
    )
    entry_risk_fraction_prescale = 0.0
    if risk_sizing_engaged:
        entry_risk_fraction_prescale = float(
            _entry_risk_fraction(
                settings=settings,
                drawdown_pct=drawdown_pct,
                # Edge: the model's own trade probability against the
                # payoff of the bracket actually being submitted.
                win_probability=signal_trade_prob,
                reward_risk_ratio=_reward_risk_ratio(
                    entry_price=_safe_float(
                        dict(tick or {}).get(
                            "ask" if str(side).upper() == "BUY" else "bid"
                        ),
                        0.0,
                    ),
                    sl_price=_safe_float(sl_price, 0.0),
                    tp_price=_safe_float(tp_price, 0.0),
                ),
                # Instrument: this pair's own recent realized returns.
                realized_returns=dict(realized_returns_by_pair or {}).get(
                    str(pair).upper()
                ),
            )
        )
    if not has_open_position and portfolio_budget_scale <= 0.0:
        # Governance zeroed entry capital (paused / shadow_only). Make that a
        # first-class policy rejection instead of letting either sizing path
        # quietly produce a 0-lot approval -- the historical failure mode here
        # is trading silently disabled, not trading wrongly enabled.
        rejection_reasons = list(rejection_reasons) + ["portfolio_budget_scale_zero"]
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
            "sl_price": (
                float(_safe_float(sl_price, 0.0))
                if float(_safe_float(sl_price, 0.0)) > 0.0
                else None
            ),
            "tp_price": (
                float(_safe_float(tp_price, 0.0))
                if float(_safe_float(tp_price, 0.0)) > 0.0
                else None
            ),
            "entry_protection_required": bool(not has_open_position),
            "entry_price": (
                float(_safe_float(dict(tick or {}).get("ask" if str(side).upper() == "BUY" else "bid"), 0.0))
                if not has_open_position
                else None
            ),
            "entry_protection_source": "closed_bar_atr_14" if not has_open_position else "",
            # Sizing handoff. ``target_risk_pct`` lets the kernel size from the
            # ACTUAL stop distance (risk/sizing.py) instead of the legacy
            # ``equity * equity_lots_per_usd`` lot arithmetic, which had no
            # knowledge of the stop and therefore made money-at-risk scale
            # linearly with stop width -- the coupling that made the bracket
            # geometry unsafe to change.
            #
            # The kernel only takes the risk path when requested/planned lots are
            # <= 0, so the two are mutually exclusive by construction. Lots are
            # zeroed ONLY when a stop distance is actually derivable; otherwise
            # the legacy value is passed through unchanged so this can never
            # introduce a new "cannot size -> no order" failure.
            # ``value_per_price_unit`` converts the 100k contract into ACCOUNT
            # currency. Sizing engages only when it RESOLVES: an unresolvable
            # rate falls back to the legacy lot path rather than sizing against
            # the 100k default, which is wrong for every pair whose quote
            # currency is not the account currency and can over-risk.
            **(
                {
                    "requested_lots": 0.0,
                    "planned_entry_lots": 0.0,
                    # Scaled by the SAME portfolio/governance budget scale that
                    # multiplies the legacy lot path, so capital bands and
                    # market-pressure de-rating bind here too -- and by the
                    # intelligent entry_size_scale so sleeve-expectancy shrink
                    # sizes the order, not just the telemetry.
                    "target_risk_pct": float(
                        entry_risk_fraction_prescale
                        * portfolio_budget_scale
                        * entry_size_scale
                    ),
                    "target_risk_pct_prescale": float(entry_risk_fraction_prescale),
                    "entry_size_scale": float(entry_size_scale),
                    "value_per_price_unit": float(entry_contract_value),
                    "legacy_planned_entry_lots": float(_safe_float(planned_entry_lots, 0.0)),
                }
                if risk_sizing_engaged
                else {
                    "requested_lots": float(requested_lots),
                    "planned_entry_lots": float(_safe_float(planned_entry_lots, 0.0)),
                }
            ),
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
            # Stamped on every risk decision so an entry taken on uncertified
            # models under exploration_demo can never be mistaken, in any
            # downstream record, for one that cleared certification.
            "entry_certification_mode": _resolved_entry_certification_mode(settings),
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
    # ``peak_equity`` / ``drawdown_pct`` are computed above, before entry sizing.
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
    decision = _runtime_risk_envelope().evaluate(
        RiskContext(
            policy_intent=policy_intent,
            market_state=market_state,
            portfolio_state=portfolio_state,
            config=_risk_kernel_config_from_settings(
                settings=settings,
                freshness_limit_secs=float(_safe_float(feature_bar.get("stale_after_secs"), 0.0)),
                rollout_policy=rollout,
            ),
            governance=dict(governance_meta or {}),
            settings=settings,
        )
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


def _materialize_final_position_actions(
    *,
    decisions: list[dict[str, Any]],
    pending_position_actions: list[dict[str, Any]],
    partial_close_tracker: dict[str, dict[str, Any]],
    loop_ts: float,
    settings: Any,
) -> dict[str, Any]:
    """Bind the final lifecycle action to an executable close amount.

    Adaptive, campaign, and RL producers are all allowed to alter the action.
    This pass runs after the last producer and before final risk reapproval so
    no ``partial_tp`` can reach the kernel with zero or stale lots.
    """

    reviewed = 0
    partial_materialized = 0
    promoted_to_exit = 0
    blocked = 0
    reason_counts: dict[str, int] = {}
    for action in pending_position_actions:
        index = int(action.get("index", -1))
        if index < 0 or index >= len(decisions):
            continue
        reviewed += 1
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        lifecycle_action = str(
            action.get("lifecycle_action")
            or meta.get("lifecycle_action")
            or "hold"
        ).strip().lower()
        lifecycle_reason = str(
            action.get("lifecycle_reason")
            or meta.get("lifecycle_reason")
            or "hold"
        )
        close_lots = float(_safe_float(action.get("close_lots"), 0.0))
        if lifecycle_action == "partial_tp":
            signature = str(
                action.get("position_signature")
                or meta.get("position_signature")
                or ""
            )
            tracker_state = dict(partial_close_tracker.get(signature, {}) or {})
            allowed, guard_reason, cooldown_remaining = _partial_close_guard(
                tracker_state=tracker_state,
                loop_ts=float(loop_ts),
                settings=settings,
            )
            if not allowed:
                lifecycle_action = "hold"
                lifecycle_reason = str(guard_reason or "partial_tp_blocked")
                close_lots = 0.0
                blocked += 1
                reason_counts[lifecycle_reason] = int(
                    reason_counts.get(lifecycle_reason, 0)
                ) + 1
                action["partial_tp_blocked_reason"] = str(lifecycle_reason)
                action["partial_tp_next_eligible_secs"] = float(cooldown_remaining)
                meta["partial_tp_blocked_reason"] = str(lifecycle_reason)
                meta["partial_tp_next_eligible_secs"] = float(cooldown_remaining)
            else:
                requested_action = str(lifecycle_action)
                lifecycle_action, close_lots = _partial_close_request_plan(
                    lots_open=float(
                        _safe_float(
                            action.get("lots_open"),
                            meta.get("lots_open", 0.0),
                        )
                    ),
                    requested_close_lots=float(close_lots),
                    fraction=float(getattr(settings, "partial_close_fraction", 0.5)),
                    settings=settings,
                )
                if lifecycle_action == "partial_tp" and close_lots > 0.0:
                    partial_materialized += 1
                elif lifecycle_action == "exit" and close_lots > 0.0:
                    promoted_to_exit += 1
                    if requested_action != "exit":
                        lifecycle_reason = f"{lifecycle_reason}_reduce_to_flat"
                else:
                    lifecycle_action = "hold"
                    lifecycle_reason = "partial_tp_not_executable"
                    close_lots = 0.0
                    blocked += 1
                    reason_counts[lifecycle_reason] = int(
                        reason_counts.get(lifecycle_reason, 0)
                    ) + 1
        elif lifecycle_action in {"hold", "exit", "tighten_stop"}:
            close_lots = 0.0

        action["lifecycle_action"] = str(lifecycle_action)
        action["lifecycle_reason"] = str(lifecycle_reason)
        action["close_lots"] = float(close_lots)
        meta["lifecycle_action"] = str(lifecycle_action)
        meta["lifecycle_reason"] = str(lifecycle_reason)
        meta["close_lots"] = float(close_lots)
        decision["metadata"] = meta
        _sync_lifecycle_action_payloads(decision=decision, action_item=action)

    return {
        "reviewed_count": int(reviewed),
        "partial_materialized_count": int(partial_materialized),
        "promoted_to_exit_count": int(promoted_to_exit),
        "blocked_count": int(blocked),
        "reason_counts": dict(sorted(reason_counts.items())),
    }


def _reapprove_final_position_actions(
    *,
    decisions: list[dict[str, Any]],
    pending_position_actions: list[dict[str, Any]],
    settings: Any,
) -> dict[str, Any]:
    """Run the risk kernel on each final, post-adaptive lifecycle intent."""
    reviewed = 0
    approved = 0
    blocked = 0
    reason_counts: dict[str, int] = {}
    expected_cmd = {
        "exit": "CLOSE",
        "partial_tp": "CLOSE_PARTIAL",
        "tighten_stop": "MODIFY_SL",
    }

    for action_item in pending_position_actions:
        index = int(action_item.get("index", -1))
        if index < 0 or index >= len(decisions):
            continue
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        lifecycle_action = str(action_item.get("lifecycle_action") or "hold").strip().lower()
        action_item["approved_order"] = {}
        action_item["final_risk_approved"] = False
        meta["approved_order"] = {}
        meta["final_lifecycle_risk_approved"] = False

        if lifecycle_action not in expected_cmd:
            meta["final_lifecycle_risk_reason"] = "not_actionable"
            decision["metadata"] = meta
            continue

        reviewed += 1
        context = dict(action_item.get("risk_reapproval_context") or {})
        if not context:
            block_reason = "final_lifecycle_risk_context_missing"
            risk_out: dict[str, Any] = {}
        else:
            try:
                risk_out = _evaluate_runtime_risk_kernel(
                    **context,
                    lifecycle_action=str(lifecycle_action),
                    lifecycle_reason=str(action_item.get("lifecycle_reason") or lifecycle_action),
                    lifecycle_action_score=float(_safe_float(action_item.get("lifecycle_action_score"), 0.0)),
                    close_lots=float(_safe_float(action_item.get("close_lots"), 0.0)),
                    sl_price=float(_safe_float(action_item.get("sl_price"), 0.0)),
                    tp_price=0.0,
                    settings=settings,
                )
                approved_order = dict(risk_out.get("approved_order") or {})
                risk_action = str(risk_out.get("lifecycle_action") or "hold").strip().lower()
                approved_cmd = str(approved_order.get("cmd") or "").strip().upper()
                block_reason = ""
                if risk_action != lifecycle_action:
                    block_reason = str(risk_out.get("reason") or "final_lifecycle_risk_action_changed")
                elif not approved_order:
                    block_reason = str(risk_out.get("reason") or "final_lifecycle_risk_blocked")
                elif approved_cmd != expected_cmd[lifecycle_action]:
                    block_reason = "final_lifecycle_risk_payload_mismatch"
            except Exception as exc:
                risk_out = {}
                approved_order = {}
                block_reason = f"final_lifecycle_risk_error:{type(exc).__name__}"

        if block_reason:
            blocked += 1
            reason_counts[block_reason] = int(reason_counts.get(block_reason, 0)) + 1
            action_item["lifecycle_action"] = "hold"
            action_item["lifecycle_reason"] = str(block_reason)
            action_item["approved_order"] = {}
            action_item["final_risk_approved"] = False
            meta["lifecycle_action"] = "hold"
            meta["lifecycle_reason"] = str(block_reason)
            meta["approved_order"] = {}
            meta["final_lifecycle_risk_approved"] = False
            meta["final_lifecycle_risk_reason"] = str(block_reason)
        else:
            approved += 1
            action_item["approved_order"] = dict(approved_order)
            action_item["final_risk_approved"] = True
            action_item["final_risk_reapproval_required"] = False
            meta["approved_order"] = dict(approved_order)
            meta["risk_verdict"] = str(risk_out.get("verdict") or "")
            meta["risk_reason"] = str(risk_out.get("reason") or "")
            meta["risk_trace"] = list(risk_out.get("trace") or [])
            meta["risk_decision"] = dict(risk_out.get("decision") or {})
            meta["final_lifecycle_risk_approved"] = True
            meta["final_lifecycle_risk_reapproval_required"] = False
            meta["final_lifecycle_risk_reason"] = str(risk_out.get("reason") or "approved")
        decision["metadata"] = meta

    return {
        "reviewed_count": int(reviewed),
        "approved_count": int(approved),
        "blocked_count": int(blocked),
        "reason_counts": dict(sorted(reason_counts.items())),
    }


_ADAPTIVE_HARD_ENTRY_BLOCK_REASONS = {
    "missing_pair_identity",
    "invalid_direction_identity",
    "playbook_scope_blocked",
    "adaptive_history_unavailable",
    # The evidence-margin floor (strategy/adaptive_policy.py) rejects a candidate
    # whose INFORMATIVE evidence does not clear the neutral point. That is an
    # availability-class failure, not a strategy opinion -- there is no evidence
    # to weigh -- so it must not be filtered out and rescued by the strict path.
    "insufficient_evidence_margin",
    # The conjunctive floors, for the same reason. Each names one channel that
    # failed to clear its own bar; because admission is conjunctive, no other
    # channel is allowed to compensate -- which is exactly what letting the
    # strict path rescue these would do.
    #
    # "edge_below_cost_floor" is the load-bearing one: it fires when expected
    # edge does not cover COST_EDGE_MULTIPLE times the round-trip spread. On
    # this stack's own measurements, transaction cost is the dominant term in
    # P&L, so a rescue here would reinstate precisely the trades that lose
    # money by construction.
    "edge_below_cost_floor",
    "model_conviction_below_floor",
    "setup_quality_below_floor",
    # Churn control, promoted from advisory to binding.
    #
    # These are NOT strategy opinions about whether a setup looks good -- which
    # is the class the architecture deliberately excludes from vetoing -- they
    # are risk controls about REPEATING a bet that just failed. Previously both
    # were appended to ``adaptive_advisories`` (zero non-telemetry consumers), so
    # after a stop-out the runtime could re-enter the same pair in the same
    # direction on the very next bar, and could re-attack a thesis the campaign
    # machine had already abandoned.
    #
    # Measured justification: on this repo's own data ~69% of everything the
    # entire 118-reason gate stack buys is spread avoided rather than better
    # edge, and random-entry bracket expectancy is -0.105 R. When cost dominates
    # P&L, unchecked re-entry churn is the single most expensive behaviour
    # available, and these two cooldowns are the cheapest genuine edges present.
    "adaptive_reentry_cooldown",
    "campaign_abandon_cooldown",
}



def _portfolio_slot_reservations(
    pending_entries: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return only final-risk-approved entries that actually reserve capacity."""

    return [
        item
        for item in list(pending_entries or [])
        if bool(dict(item or {}).get("portfolio_slot_reserved", False))
        and bool(
            dict(item or {}).get("risk_approved_order")
            or dict(item or {}).get("approved_order")
        )
    ]


def _reapprove_final_entry_intents(
    *,
    decisions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    settings: Any,
    sleeve_health_snapshots: dict[str, Any] | None = None,
    enforce_sleeve_governance: bool = False,
    complementarity: ComplementaritySnapshot | None = None,
) -> dict[str, Any]:
    """Resolve one post-adaptive entry intent and bind it to an exact risk order.

    Strict scoring remains diagnostic input.  An adaptive intelligent decision
    owns strategy admission and may override every scorer/heuristic opinion;
    venue, freshness, protection, authority, and exposure invariants remain
    authoritative.  Every selected candidate is then re-evaluated by the risk
    kernel in allocator order while accounting only for earlier, genuinely
    approved reservations.
    """

    adaptive_mode = bool(getattr(settings, "adaptive_execution_enabled", False))
    sleeve_governance_enabled = bool(adaptive_mode and enforce_sleeve_governance)
    reviewed = 0
    approved = 0
    blocked = 0
    adaptive_approved = 0
    strict_approved = 0
    reason_counts: dict[str, int] = {}

    indexed_items: list[tuple[int, dict[str, Any]]] = list(enumerate(pending_entries))

    def _priority(row: tuple[int, dict[str, Any]]) -> tuple[int, int, float, int]:
        original_index, item = row
        decision_index = int(dict(item or {}).get("index", -1))
        meta = (
            dict(decisions[decision_index].get("metadata", {}) or {})
            if 0 <= decision_index < len(decisions)
            else {}
        )
        adaptive_selected = bool(adaptive_mode and meta.get("adaptive_selected", False))
        allocator_rank = int(_safe_float(meta.get("allocator_rank"), 0.0))
        return (
            0 if adaptive_selected else 1,
            allocator_rank if allocator_rank > 0 else 1_000_000,
            -float(_safe_float(meta.get("allocator_score"), 0.0)),
            int(original_index),
        )

    ordered_items = (
        sorted(indexed_items, key=_priority)
        if adaptive_mode
        else indexed_items
    )
    reservations: list[dict[str, Any]] = []
    for _, item in ordered_items:
        index = int(item.get("index", -1))
        if index < 0 or index >= len(decisions):
            continue
        reviewed += 1
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        pair_key = str(item.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
        side = str(decision.get("side") or meta.get("side") or "").upper()
        strict_ready = bool(meta.get("strict_entry_ready", meta.get("entry_ready", False)))
        strict_reasons = [
            str(reason)
            for reason in list(
                meta.get("strict_entry_blocking_reasons", meta.get("entry_blocking_reasons", []))
                or []
            )
            if str(reason).strip()
        ]
        adaptive_selected = bool(meta.get("adaptive_selected", False))
        adaptive_reason = str(meta.get("adaptive_rejection_reason") or "").strip()
        adaptive_entry_mode = str(meta.get("adaptive_entry_mode") or "standard").strip().lower()
        expected_sleeve = str(
            meta.get("adaptive_sleeve")
            or playbook_to_sleeve(meta.get("adaptive_playbook") or "")
        ).strip()
        sleeve_block_reason = ""
        if sleeve_governance_enabled:
            sleeve_block_reason = sleeve_entry_block_reason(
                snapshot=dict(sleeve_health_snapshots or {}).get(expected_sleeve),
                expected_sleeve=expected_sleeve,
            )
        # Complementarity is a risk control, not a strategy opinion -- the same
        # class as the churn controls above, and binding for the same reason. It
        # does not judge whether the setup looks good; it refuses to take a bet
        # the book already holds under another sleeve's name.
        redundancy_block_reason = (
            complementarity.block_reason(expected_sleeve)
            if complementarity is not None
            else ""
        )
        adaptive_hard_reason = (
            adaptive_reason
            if adaptive_mode and adaptive_reason in _ADAPTIVE_HARD_ENTRY_BLOCK_REASONS
            else ""
        )
        if not adaptive_hard_reason and adaptive_mode and redundancy_block_reason:
            adaptive_hard_reason = str(redundancy_block_reason)
        residual_strict_reasons = list(strict_reasons)
        if adaptive_mode and adaptive_selected:
            residual_strict_reasons = [
                reason
                for reason in strict_reasons
                if _is_operational_hard_entry_block_reason(reason)
            ]

        # With adaptive execution binding, allocator selection owns the final
        # candidate set. A strict-ready but ranked-out candidate stays out.
        strategy_selected = bool(adaptive_selected if adaptive_mode else strict_ready)
        source = (
            "intelligent"
            if adaptive_mode and adaptive_selected
            else "strict"
        )
        item["payload"] = {}
        item["approved_order"] = {}
        item["risk_approved_order"] = {}
        item["portfolio_slot_reserved"] = False
        meta["approved_order"] = {}
        meta["risk_approved_order"] = {}
        meta["final_entry_risk_approved"] = False
        meta["final_entry_source"] = str(source)
        meta["final_entry_mode"] = str(adaptive_entry_mode)
        meta["intelligent_size_scale"] = 1.0
        meta["intelligent_planned_lots_before_scale"] = 0.0
        meta["intelligent_planned_lots_after_scale"] = 0.0
        meta["sleeve_governance_enforced"] = bool(sleeve_governance_enabled)
        meta["sleeve_governance_entry_block_reason"] = str(sleeve_block_reason)
        meta["sleeve_governance_advisory"] = bool(sleeve_block_reason)
        meta["sleeve_redundancy_block_reason"] = str(redundancy_block_reason)

        risk_out: dict[str, Any] = {}
        approved_order: dict[str, Any] = {}
        if adaptive_hard_reason:
            block_reason = str(adaptive_hard_reason)
        elif not strategy_selected:
            block_reason = str(
                adaptive_reason
                if adaptive_mode and adaptive_reason
                else (
                    strict_reasons[0]
                    if strict_reasons
                    else meta.get("strict_rejection_reason") or "entry_blocked"
                )
            )
        elif residual_strict_reasons:
            block_reason = str(residual_strict_reasons[0])
        else:
            context = dict(item.get("risk_reapproval_context") or {})
            if not context:
                block_reason = "final_entry_risk_context_missing"
            else:
                planned_lots_before_scale = float(
                    _safe_float(context.get("planned_entry_lots"), 0.0)
                )
                entry_lot_scale = 1.0
                if adaptive_mode and adaptive_selected:
                    entry_lot_scale = float(
                        _clip01(meta.get("adaptive_size_scale", 1.0))
                    )
                    # Realized outcomes govern capital, not the model's opinion
                    # of its own setups. A sleeve that has been paid keeps its
                    # size; one that has been paying shrinks. Inert until the
                    # sleeve has enough closed trades to be judged.
                    expectancy_scale, expectancy_reason = sleeve_expectancy_allocation_scale(
                        dict(sleeve_health_snapshots or {}).get(expected_sleeve)
                    )
                    entry_lot_scale = float(entry_lot_scale * expectancy_scale)
                    meta["sleeve_expectancy_scale"] = float(expectancy_scale)
                    meta["sleeve_expectancy_reason"] = str(expectancy_reason)
                    context["planned_entry_lots"] = float(
                        planned_lots_before_scale * entry_lot_scale
                    )
                meta["intelligent_size_scale"] = float(entry_lot_scale)
                meta["intelligent_planned_lots_before_scale"] = float(
                    planned_lots_before_scale
                )
                meta["intelligent_planned_lots_after_scale"] = float(
                    _safe_float(
                        context.get("planned_entry_lots"),
                        planned_lots_before_scale,
                    )
                )
                try:
                    risk_out = _evaluate_runtime_risk_kernel(
                        **{
                            **context,
                            "pending_entries": list(reservations),
                            "rejection_reasons": [],
                            # Binds intelligent/expectancy sizing on the
                            # risk-fraction path; the legacy path already has
                            # it baked into planned_entry_lots above.
                            "entry_size_scale": float(entry_lot_scale),
                        },
                        lifecycle_action="entry",
                        lifecycle_reason=(
                            "intelligent_entry_selected"
                            if source == "intelligent"
                            else "strict_entry_selected"
                        ),
                        lifecycle_action_score=float(
                            _safe_float(meta.get("trade_prob"), 0.0)
                        ),
                        close_lots=0.0,
                        sl_price=float(_safe_float(item.get("sl_price"), 0.0)),
                        tp_price=float(_safe_float(item.get("tp_price"), 0.0)),
                        settings=settings,
                    )
                    approved_order = dict(risk_out.get("approved_order") or {})
                    approved_cmd = str(approved_order.get("cmd") or "").strip().upper()
                    approved_symbol = str(
                        approved_order.get("symbol") or pair_key
                    ).strip().upper()
                    if not approved_order:
                        block_reason = str(
                            risk_out.get("reason") or "final_entry_risk_blocked"
                        )
                    elif approved_cmd != side or approved_cmd not in {"BUY", "SELL"}:
                        block_reason = "final_entry_risk_side_mismatch"
                    elif approved_symbol != pair_key:
                        block_reason = "final_entry_risk_symbol_mismatch"
                    else:
                        block_reason = ""
                except Exception as exc:
                    risk_out = {}
                    approved_order = {}
                    block_reason = f"final_entry_risk_error:{type(exc).__name__}"

        if block_reason:
            blocked += 1
            reason_counts[str(block_reason)] = int(reason_counts.get(str(block_reason), 0)) + 1
            canonical_reasons = [str(block_reason)]
            meta["final_entry_risk_reason"] = str(block_reason)
            meta["canonical_entry_ready"] = False
            meta["canonical_entry_blocking_reasons"] = list(canonical_reasons)
            meta["canonical_entry_rejection_reason"] = str(block_reason)
            meta["entry_ready"] = False
            meta["entry_blocking_reasons"] = list(canonical_reasons)
            meta["execution_entry_ready"] = False
            meta["execution_blocking_reasons"] = list(canonical_reasons)
            meta["execution_rejection_reason"] = str(block_reason)
            decision["execution_ready"] = False
            decision["reasons"] = list(canonical_reasons)
            _append_policy_trace(
                meta,
                stage="final_entry_risk",
                verdict="block",
                reason=str(block_reason),
                score=float(_safe_float(meta.get("trade_prob"), 0.0)),
                changed_decision=bool(strict_ready or adaptive_selected),
                details={
                    "source": str(source),
                    "entry_mode": str(adaptive_entry_mode),
                    "residual_strict_reasons": list(residual_strict_reasons),
                    "strategy_evidence_reasons": [
                        str(reason)
                        for reason in strict_reasons
                        if not _is_operational_hard_entry_block_reason(reason)
                    ],
                    "reservation_count": int(len(reservations)),
                },
            )
            decision["metadata"] = meta
            continue

        payload = _payload_from_approved_order(
            order=approved_order,
            pair=pair_key,
            ts_value=str(item.get("ts_value") or meta.get("ts") or ""),
            action_tag="entry",
        )
        approved += 1
        if source == "intelligent":
            adaptive_approved += 1
        else:
            strict_approved += 1
        item["payload"] = dict(payload)
        item["approved_order"] = dict(approved_order)
        item["risk_approved_order"] = dict(approved_order)
        item["portfolio_slot_reserved"] = True
        reservations.append(item)
        meta["approved_order"] = dict(approved_order)
        meta["risk_approved_order"] = dict(approved_order)
        meta["risk_verdict"] = str(risk_out.get("verdict") or "")
        meta["risk_reason"] = str(risk_out.get("reason") or "")
        meta["risk_trace"] = list(risk_out.get("trace") or [])
        meta["risk_decision"] = dict(risk_out.get("decision") or {})
        meta["rollout"] = dict(risk_out.get("rollout") or {})
        meta["portfolio_allocation"] = dict(
            risk_out.get("portfolio_allocation") or {}
        )
        meta["portfolio_budget_scale"] = float(
            _safe_float(risk_out.get("portfolio_budget_scale"), 1.0)
        )
        meta["capital_budget_scale"] = float(
            _safe_float(risk_out.get("capital_budget_scale"), 1.0)
        )
        meta["capital_governance"] = dict(risk_out.get("governance") or {})
        rollout_meta = dict(risk_out.get("rollout") or {})
        meta["rollout_mode"] = str(rollout_meta.get("mode") or "")
        meta["rollout_active"] = bool(rollout_meta.get("active", False))
        meta["rollout_pair_allowlisted"] = bool(
            rollout_meta.get("pair_allowlisted", False)
        )
        meta["rollout_budget_scale"] = float(
            _safe_float(rollout_meta.get("budget_scale"), 1.0)
        )
        meta["final_entry_risk_approved"] = True
        meta["final_entry_risk_reason"] = str(
            risk_out.get("reason") or "approved"
        )
        meta["canonical_entry_ready"] = True
        meta["canonical_entry_blocking_reasons"] = []
        meta["canonical_entry_rejection_reason"] = "none"
        meta["entry_ready"] = True
        meta["entry_blocking_reasons"] = []
        meta["execution_entry_ready"] = True
        meta["execution_blocking_reasons"] = []
        meta["execution_rejection_reason"] = "none"
        decision["execution_ready"] = True
        decision["reasons"] = []
        _append_policy_trace(
            meta,
            stage="final_entry_risk",
            verdict="allow",
            reason=str(risk_out.get("reason") or "approved"),
            score=float(_safe_float(meta.get("trade_prob"), 0.0)),
                changed_decision=bool(source == "intelligent" or not strict_ready),
            details={
                "source": str(source),
                "entry_mode": str(adaptive_entry_mode),
                "approved_order": dict(approved_order),
                "entry_lot_scale": float(meta.get("intelligent_size_scale", 1.0)),
                "reservation_count": int(len(reservations)),
            },
        )
        decision["metadata"] = meta

    return {
        "reviewed_count": int(reviewed),
        "approved_count": int(approved),
        "blocked_count": int(blocked),
        "adaptive_approved_count": int(adaptive_approved),
        "strict_approved_count": int(strict_approved),
        "reservation_count": int(len(reservations)),
        "reason_counts": dict(sorted(reason_counts.items())),
    }


def _hard_lifecycle_fail_safe_action(
    *,
    positions: list[dict[str, Any]],
    loop_ts: float,
    tick: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    """Evaluate monotonic protections that survive every strategy failure."""

    if not positions:
        return {
            "lifecycle_action": "hold",
            "lifecycle_reason": "no_open_position",
            "lifecycle_action_score": 0.0,
            "sl_price": 0.0,
            "lifecycle_source": "hard_lifecycle_floor",
        }

    hard_stop_secs = float(_safe_float(getattr(settings, "hard_time_stop_secs", 0.0), 0.0))
    oldest_open_time = _position_oldest_open_time(positions)
    if hard_stop_secs > 0.0 and oldest_open_time > 0.0 and (float(loop_ts) - oldest_open_time) >= hard_stop_secs:
        return {
            "lifecycle_action": "exit",
            "lifecycle_reason": "hard_time_stop",
            "lifecycle_action_score": 1.0,
            "sl_price": 0.0,
            "lifecycle_source": "hard_lifecycle_floor",
        }

    if bool(getattr(settings, "enable_adjust_actions", False)) and float(
        _safe_float(getattr(settings, "adjust_stop_buffer_pips", 0.0), 0.0)
    ) > 0.0:
        tick_payload = dict(tick or {})
        bid = float(_safe_float(tick_payload.get("bid"), 0.0))
        ask = float(_safe_float(tick_payload.get("ask"), 0.0))
        pos_side = _position_side(positions)
        if bid > 0.0 and ask > 0.0 and pos_side in {"long", "short"}:
            digits_raw = int(_safe_float(tick_payload.get("digits"), 0.0))
            pip_size = infer_pip_size(pair=str(dict(positions[0] or {}).get("symbol") or ""), digits=digits_raw or None)
            buffer_price = float(_safe_float(getattr(settings, "adjust_stop_buffer_pips", 0.0), 0.0)) * float(pip_size)
            sl_price = (bid - buffer_price) if pos_side == "long" else (ask + buffer_price)
            current_stops: list[float] = []
            for raw_position in positions:
                position = dict(raw_position or {})
                if _position_side([position]) != pos_side:
                    continue
                for key in ("sl", "sl_price", "stop_loss", "stopLoss"):
                    if key not in position:
                        continue
                    current_stop = float(_safe_float(position.get(key), 0.0))
                    if math.isfinite(current_stop) and current_stop > 0.0:
                        current_stops.append(current_stop)
                    break
            current_sl = (
                max(current_stops)
                if pos_side == "long" and current_stops
                else min(current_stops)
                if pos_side == "short" and current_stops
                else 0.0
            )
            strictly_tightens = (
                current_sl <= 0.0
                or (pos_side == "long" and sl_price > current_sl)
                or (pos_side == "short" and sl_price < current_sl)
            )
            if math.isfinite(sl_price) and sl_price > 0.0 and strictly_tightens:
                return {
                    "lifecycle_action": "tighten_stop",
                    "lifecycle_reason": "adjust_stop_after_entry_inference_error",
                    "lifecycle_action_score": 1.0,
                    "sl_price": float(sl_price),
                    "lifecycle_source": "hard_lifecycle_floor",
                }

    return {
        "lifecycle_action": "hold",
        "lifecycle_reason": "hard_lifecycle_not_triggered",
        "lifecycle_action_score": 0.0,
        "sl_price": 0.0,
        "lifecycle_source": "hard_lifecycle_floor",
    }


def _legacy_lifecycle_failure_action(
    *,
    positions: list[dict[str, Any]],
    loop_ts: float,
    settings: Any,
    loaded: Any | None,
    intraday_row: pd.DataFrame | None,
    intraday_timeframe: str,
    total_position_count: int,
) -> dict[str, Any]:
    """Retain the legacy exit-model fallback only when adaptive execution is off."""

    lifecycle_error = ""
    if (
        positions
        and loaded is not None
        and bool(getattr(settings, "enable_lifecycle_actions", True))
        and getattr(loaded, "exit_model", None) is not None
        and intraday_row is not None
        and not intraday_row.empty
    ):
        try:
            lifecycle_row = _build_lifecycle_row(
                row=intraday_row,
                positions=positions,
                total_position_count=int(total_position_count),
                loop_ts=float(loop_ts),
                timeframe=str(intraday_timeframe),
            )
            exit_diag = _score_exit_policy_model(
                loaded.exit_model,
                lifecycle_row,
                action_labels=getattr(loaded, "exit_action_labels", None),
            )
            selected = str(exit_diag.get("selected") or "hold")
            score = float(_safe_float(exit_diag.get("score"), 0.0))
            if selected == "exit" and score >= float(
                _safe_float(getattr(settings, "lifecycle_model_action_min_prob", 0.5), 0.5)
            ):
                return {
                    "lifecycle_action": "exit",
                    "lifecycle_reason": "exit_model_exit_after_entry_inference_error",
                    "lifecycle_action_score": float(score),
                    "sl_price": 0.0,
                    "lifecycle_source": "legacy_exit_model",
                }
        except Exception as exc:
            lifecycle_error = f"{type(exc).__name__}:{exc}"
    return {
        "lifecycle_action": "hold",
        "lifecycle_reason": "position_open_entry_pipeline_unavailable",
        "lifecycle_action_score": 0.0,
        "sl_price": 0.0,
        "lifecycle_source": "legacy_lifecycle_fallback",
        "lifecycle_inference_error": str(lifecycle_error),
    }


def _append_failed_pair_decision_with_fail_safe(
    *,
    decisions: list[dict[str, Any]],
    pending_position_actions: list[dict[str, Any]],
    pair: str,
    failure_reason: str,
    state: dict[str, Any],
    tick: dict[str, Any],
    loop_ts: float,
    settings: Any,
    loaded: Any | None = None,
    intraday_row: pd.DataFrame | None = None,
    intraday_timeframe: str = "M5",
    error: str = "",
    extra_metadata: dict[str, Any] | None = None,
) -> None:
    """Record an entry-pipeline failure without suppressing protective exits."""

    pair_key = str(pair).upper()
    positions = _pair_positions(state, pair=pair_key)
    pair_count, total_count = _state_position_counts(state, pair=pair_key)
    portfolio_positions = list(state.get("positions", []) or [])
    pos_side = _position_side(positions)
    position_signature = _position_signature(dict(positions[0] or {})) if positions else ""
    hard_lifecycle_floor = _hard_lifecycle_fail_safe_action(
        positions=list(positions),
        loop_ts=float(loop_ts),
        tick=dict(tick or {}),
        settings=settings,
    )
    if bool(getattr(settings, "adaptive_execution_enabled", False)):
        fail_safe = dict(hard_lifecycle_floor)
        if str(fail_safe.get("lifecycle_action") or "hold") == "hold":
            fail_safe["lifecycle_reason"] = "adaptive_lifecycle_unavailable"
    else:
        legacy_lifecycle = _legacy_lifecycle_failure_action(
            positions=list(positions),
            loop_ts=float(loop_ts),
            settings=settings,
            loaded=loaded,
            intraday_row=intraday_row,
            intraday_timeframe=str(intraday_timeframe),
            total_position_count=int(total_count),
        )
        fail_safe = (
            dict(legacy_lifecycle)
            if str(legacy_lifecycle.get("lifecycle_action") or "hold") == "exit"
            else dict(hard_lifecycle_floor)
            if str(hard_lifecycle_floor.get("lifecycle_action") or "hold") == "tighten_stop"
            else dict(legacy_lifecycle)
        )
    lifecycle_action = str(fail_safe.get("lifecycle_action") or "hold")
    lifecycle_reason = str(fail_safe.get("lifecycle_reason") or "position_open_entry_pipeline_unavailable")
    lifecycle_score = float(_safe_float(fail_safe.get("lifecycle_action_score"), 0.0))
    sl_price = float(_safe_float(fail_safe.get("sl_price"), 0.0))
    oldest_open_time = _position_oldest_open_time(positions)
    stable_ts = (
        datetime.fromtimestamp(oldest_open_time, tz=UTC).isoformat()
        if oldest_open_time > 0.0
        else f"fail-safe:{position_signature or pair_key}"
    )
    approved_order: dict[str, Any] = {}
    risk_out: dict[str, Any] = {}
    risk_reapproval_context: dict[str, Any] = {}
    if positions and lifecycle_action in {"exit", "tighten_stop"}:
        signal_stub = SimpleNamespace(
            trade_prob=float(lifecycle_score),
            uncertainty_score=1.0,
            session_bucket="unknown",
            session_entry_blocked=False,
            reversal_ready=False,
        )
        risk_reapproval_context = {
            "pair": pair_key,
            "ts_value": str(stable_ts),
            "side": "BUY" if pos_side == "long" else "SELL",
            "signal": signal_stub,
            "expected_edge_bps": 0.0,
            "spread_bps": 0.0,
            "feature_bar": {
                "stale": True,
                "reason": str(failure_reason),
                "age_secs": None,
                "stale_after_secs": None,
            },
            "tick": dict(tick or {}),
            "spread_unit_source": "missing",
            "mt4_fresh": False,
            "ticks_fresh": False,
            "paused": bool(dict(state.get("governance") or {}).get("paused", False)),
            "positions": list(positions),
            "pair_count": int(pair_count),
            "total_count": int(len(portfolio_positions)),
            "current_equity": float(_safe_float(state.get("equity"), 0.0)),
            "planned_entry_lots": 0.0,
            "rejection_reasons": [str(failure_reason)],
            "state": dict(state),
            "portfolio_positions": list(portfolio_positions),
            "governance_policy": dict(state.get("governance") or {}),
        }
        risk_out = _evaluate_runtime_risk_kernel(
            **risk_reapproval_context,
            lifecycle_action=str(lifecycle_action),
            lifecycle_reason=str(lifecycle_reason),
            lifecycle_action_score=float(lifecycle_score),
            close_lots=0.0,
            sl_price=float(sl_price),
            tp_price=0.0,
            settings=settings,
        )
        approved_order = dict(risk_out.get("approved_order") or {})
        if not approved_order:
            lifecycle_action = "hold"
            lifecycle_reason = str(risk_out.get("reason") or "fail_safe_risk_kernel_blocked")

    decision_index = int(len(decisions))
    metadata = {
        "pair": pair_key,
        "ts": str(stable_ts),
        "runtime": "fxstack",
        "error": str(error),
        "position_open": bool(positions),
        "position_side": str(pos_side),
        "position_count_pair": int(pair_count),
        "position_signature": str(position_signature),
        "lifecycle_action": str(lifecycle_action),
        "lifecycle_reason": str(lifecycle_reason),
        "lifecycle_action_score": float(lifecycle_score),
        "lifecycle_source": str(fail_safe.get("lifecycle_source") or "hard_lifecycle_floor"),
        "hard_lifecycle_action": str(hard_lifecycle_floor.get("lifecycle_action") or "hold"),
        "hard_lifecycle_reason": str(hard_lifecycle_floor.get("lifecycle_reason") or ""),
        "hard_lifecycle_action_score": float(
            _safe_float(hard_lifecycle_floor.get("lifecycle_action_score"), 0.0)
        ),
        "hard_lifecycle_sl_price": float(_safe_float(hard_lifecycle_floor.get("sl_price"), 0.0)),
        "lifecycle_inference_error": str(fail_safe.get("lifecycle_inference_error") or ""),
        "approved_order": dict(approved_order),
        "risk_decision": dict(risk_out.get("decision") or {}),
        **dict(extra_metadata or {}),
    }
    decisions.append(
        {
            "symbol": pair_key,
            "side": "BUY" if pos_side == "long" else ("SELL" if pos_side == "short" else "N/A"),
            "score": 0.0,
            "confidence": 0.0,
            "execution_ready": False,
            "reasons": [str(failure_reason)],
            "metadata": metadata,
        }
    )
    if positions and approved_order and lifecycle_action in {"exit", "tighten_stop"}:
        pending_position_actions.append(
            {
                "index": int(decision_index),
                "pair": pair_key,
                "ts_value": str(stable_ts),
                "action_key": f"{_lifecycle_action_tag(lifecycle_action)}:{stable_ts}",
                "position_signature": str(position_signature),
                "position_side": str(pos_side),
                "lifecycle_action": str(lifecycle_action),
                "lifecycle_reason": str(lifecycle_reason),
                "lifecycle_action_score": float(lifecycle_score),
                "close_lots": 0.0,
                "sl_price": float(sl_price),
                "hard_lifecycle_action": str(hard_lifecycle_floor.get("lifecycle_action") or "hold"),
                "hard_lifecycle_reason": str(hard_lifecycle_floor.get("lifecycle_reason") or ""),
                "hard_lifecycle_action_score": float(
                    _safe_float(hard_lifecycle_floor.get("lifecycle_action_score"), 0.0)
                ),
                "hard_lifecycle_close_lots": 0.0,
                "hard_lifecycle_sl_price": float(_safe_float(hard_lifecycle_floor.get("sl_price"), 0.0)),
                "lots_open": float(_safe_float(dict(positions[0] or {}).get("lots"), 0.0)),
                "age_bars": 0.0,
                "unrealized_pnl_usd": float(_safe_float(dict(positions[0] or {}).get("profit"), 0.0)),
                "approved_order": dict(approved_order),
                "risk_reapproval_context": dict(risk_reapproval_context),
            }
        )


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
                    meta.get("belief_primary_expected_net_ev_bps", meta.get("expected_edge_bps", meta.get("calibrated_ev_bps", 0.0))),
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
            "press_count": int(stage_counts.get("press", 0)),
            "stand_down_count": int(stage_counts.get("stand_down", 0)),
        },
    }


# Feature-freshness helpers carved into fxstack.runtime.feature_freshness.
# Re-bind under the original underscored names so callers within this module
# (and the test that imports `_feature_row_is_stale` from runner) keep working.
from fxstack.runtime.feature_freshness import (
    feature_bar_freshness as _feature_bar_freshness,
    feature_row_is_stale as _feature_row_is_stale,
    latest_partition_ts as _latest_partition_ts,
    timeframe_to_seconds as _timeframe_to_seconds,
)


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


def _raw_root_for_feature_root(feature_root: str | Path) -> Path:
    """Keep runtime raw bars in the data tree that owns the feature store."""
    return Path(feature_root).expanduser().parent / "raw"


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
# Carved into fxstack.runtime.positions. Re-bound under original underscored
# names so internal callers and tests that import from runner keep working.
from fxstack.runtime.positions import (
    partial_close_guard as _partial_close_guard,
    partial_close_plan as _partial_close_plan,
    partial_close_request_plan as _partial_close_request_plan,
    position_signature as _position_signature,
    round_lot_size as _round_lot_size,
)


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


# Carved out of this module — see fxstack.runtime.startup for the actual
# implementation. Re-bound here under the original underscored names so the
# rest of the runtime (hundreds of call sites) keeps working unchanged.
from fxstack.runtime.startup import (
    perform_startup_bridge_checks as _perform_startup_bridge_checks,
    startup_log as _startup_log,
)


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
    equity_peak: float,
    pairs: list[str],
    startup_state: dict[str, Any],
    runtime_diag: dict[str, Any] | None = None,
    preserved_orchestration_live: dict[str, Any] | None = None,
) -> dict[str, Any]:
    next_runtime_diag = dict(runtime_diag or {})
    if preserved_orchestration_live is not None:
        next_runtime_diag["orchestration_live"] = dict(
            preserved_orchestration_live
        )
    patch = {
        "runtime_profile": str(runtime_profile),
        "runtime_status": "starting",
        "runtime_last_cycle_ts": 0.0,
        "runtime_equity_seed": float(equity_seed),
        "equity_peak": float(equity_peak),
        "configured_pairs": list(pairs),
        "agent_decisions": [],
        "agent_diagnostics": {},
        "monitor": {},
        "vol": 0.0,
        "runtime_diag": next_runtime_diag,
        "runtime_startup": dict(startup_state),
        "__prune_stale__": True,
    }
    if preserved_orchestration_live is not None:
        patch["__expected_orchestration_live_authority__"] = dict(
            preserved_orchestration_live
        )
    return patch


def _startup_runtime_diag_preserving_live_authority(
    *,
    svc: Any,
    runtime_diag: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_state = dict(svc.get_state() or {})
    current_runtime_diag = dict(current_state.get("runtime_diag") or {})
    current_live = dict(current_runtime_diag.get("orchestration_live") or {})
    next_runtime_diag = dict(runtime_diag or {})
    if "orchestration_live" in current_runtime_diag:
        next_runtime_diag["orchestration_live"] = current_live
    else:
        next_runtime_diag.pop("orchestration_live", None)
    return next_runtime_diag, current_live


def _advance_runtime_equity_peak(
    *,
    persisted_peak: Any,
    current_equity: Any,
    fallback_equity: Any,
) -> float:
    """Advance the persisted risk high-water mark without restart resets."""
    peak = float(_safe_float(persisted_peak, float("nan")))
    current = float(_safe_float(current_equity, float("nan")))
    fallback = float(_safe_float(fallback_equity, float("nan")))
    if math.isfinite(peak) and peak > 0.0:
        if math.isfinite(current) and current > 0.0:
            return float(max(peak, current))
        return float(peak)
    if math.isfinite(current) and current > 0.0:
        return float(current)
    if math.isfinite(fallback) and fallback > 0.0:
        return float(fallback)
    raise RuntimeError("risk_equity_peak_unavailable")


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
        next_runtime_diag, expected_live_authority = (
            _startup_runtime_diag_preserving_live_authority(
                svc=svc,
                runtime_diag=runtime_diag,
            )
        )
        patch["runtime_diag"] = next_runtime_diag
        patch["__expected_orchestration_live_authority__"] = (
            expected_live_authority
        )
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
    next_runtime_diag, expected_live_authority = (
        _startup_runtime_diag_preserving_live_authority(
            svc=svc,
            runtime_diag=runtime_diag,
        )
    )
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
            "runtime_diag": next_runtime_diag,
            "__expected_orchestration_live_authority__": (
                expected_live_authority
            ),
        },
        prune_state=True,
    )


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


def _safe_load(model_cls: Any, raw_path: Any, project_root: Path) -> tuple[Any | None, str]:
    value = str(_artifact_path(raw_path) or "").strip()
    if not value:
        return None, "missing_path"
    try:
        s = get_settings()
        timeout_secs = max(0.0, float(getattr(s, "model_load_timeout_secs", 0.0) or 0.0))
        ref = normalize_artifact_ref(raw_path)
        expected_digest = (
            str(ref.get("artifact_hash") or "").strip().lower()
            if isinstance(raw_path, dict)
            else None
        )
        expected_name = str(
            getattr(model_cls, "name", getattr(model_cls, "__name__", "")) or ""
        ).strip()
        path = resolve_model_artifact_path(raw_path, project_root=project_root)
        label = f"{expected_name or 'model'}:{path}"
        with artifact_lock(path):
            validate_artifact_contract(
                path,
                label=label,
                expected_digest=expected_digest,
                expected_name=expected_name or None,
            )
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
            else:
                model = model_cls.load(path)
            validate_artifact_contract(
                path,
                label=label,
                expected_digest=expected_digest,
                expected_name=expected_name or None,
            )
            return model, ""
    except Exception as exc:
        return None, f"load_error:{type(exc).__name__}"


def _artifact_ref_value(artifacts: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        raw_ref = artifacts.get(key)
        if str(_artifact_path(raw_ref) or "").strip():
            return raw_ref
    return ""


def _artifact_identity_map(
    artifacts: dict[str, Any],
    *,
    project_root: Path,
) -> dict[str, dict[str, Any]]:
    """Canonical artifact locators and publisher identities for startup parity."""

    identities: dict[str, dict[str, Any]] = {}
    for component, raw_ref in sorted(dict(artifacts or {}).items()):
        ref = normalize_artifact_ref(raw_ref)
        local_path = str(ref.get("path") or "").strip()
        model_uri = str(ref.get("model_uri") or "").strip()
        locator = (
            _normalized_registry_path(local_path, project_root=project_root)
            if local_path
            else model_uri
        )
        identity = {
            "locator": str(locator),
            "artifact_hash": str(ref.get("artifact_hash") or "").strip().lower(),
            "content_sha256": str(ref.get("content_sha256") or "").strip().lower(),
            "model_name": str(ref.get("model_name") or "").strip(),
            "model_version": str(ref.get("model_version") or "").strip(),
            "alias": str(ref.get("alias") or "").strip(),
            "bundle_run_id": str(ref.get("bundle_run_id") or "").strip(),
            "feature_contract_hash": str(ref.get("feature_contract_hash") or "").strip().lower(),
            "runtime_compatible": bool(ref.get("runtime_compatible", True)),
        }
        if locator or any(
            value
            for key, value in identity.items()
            if key not in {"locator", "runtime_compatible"}
        ):
            identities[str(component)] = identity
    return identities


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

    rl_preflight: dict[str, tuple[str, str]] = {}
    rl_preflight_errors: dict[str, str] = {}
    rl_configured_pairs: set[str] = set()
    for pair in pairs:
        row = dict(active.get(pair, {}) or {})
        if not row:
            continue
        artifacts = dict(row.get("artifacts_json") or {})
        metadata = dict(row.get("metadata_json") or {})
        ref = _runtime_rl_checkpoint_ref(artifacts=artifacts, metadata=metadata)
        if not str(ref.get("path") or ref.get("model_uri") or "").strip():
            continue
        rl_configured_pairs.add(pair)
        try:
            rl_preflight[pair] = _validate_runtime_rl_checkpoint_ref(
                ref,
                project_root=project_root,
            )
        except Exception as exc:
            rl_preflight_errors[pair] = f"{type(exc).__name__}:{exc}"

    rl_identities = set(rl_preflight.values())
    if len(rl_identities) > 1:
        identity_detail = ";".join(
            f"{path}@{digest}" for path, digest in sorted(rl_identities)
        )
        disagreement = f"rl_checkpoint_identity_disagreement:{identity_detail}"
        for pair in rl_configured_pairs:
            rl_preflight_errors[pair] = disagreement

    for pair in pairs:
        row = dict(active.get(pair, {}) or {})
        if not row:
            continue
        art = dict(row.get("artifacts_json") or {})
        meta_json = dict(row.get("metadata_json") or {})
        rl_checkpoint_path, rl_checkpoint_content_sha256 = rl_preflight.get(
            pair,
            ("", ""),
        )
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
        rl_preflight_error = str(rl_preflight_errors.get(pair) or "")
        if rl_preflight_error:
            pair_diag["status"] = "failed"
            pair_diag["failure_component"] = "portfolio_rl"
            pair_diag["failure_reason"] = rl_preflight_error
            pair_diag["components"]["portfolio_rl"] = {
                "path": str(
                    _artifact_path(
                        _runtime_rl_checkpoint_ref(
                            artifacts=art,
                            metadata=meta_json,
                        )
                    )
                    or ""
                ),
                "requested": True,
                "required": True,
                "status": "failed",
                "error": rl_preflight_error,
                "loaded": False,
            }
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            load_diag["model_load_errors"] = int(
                load_diag.get("model_load_errors", 0)
            ) + 1
            if require_all:
                _raise_model_load_failure(
                    message=(
                        f"failed loading portfolio RL checkpoint for {pair}: "
                        f"{rl_preflight_error}"
                    ),
                    pair=pair,
                    component="portfolio_rl",
                    reason=rl_preflight_error,
                )
            continue
        if pair in rl_preflight:
            pair_diag["components"]["portfolio_rl"] = {
                "path": rl_checkpoint_path,
                "content_sha256": rl_checkpoint_content_sha256,
                "requested": True,
                "required": True,
                "status": "loaded",
                "error": "",
                "loaded": True,
            }
        else:
            pair_diag["components"]["portfolio_rl"] = {
                "path": "",
                "requested": False,
                "required": False,
                "status": "not_configured",
                "error": "",
                "loaded": False,
            }
        feature_contract_errors = feature_contract_mismatches(dict(meta_json.get("feature_schema") or {}))
        if feature_contract_errors:
            contract_reason = ",".join(
                f"{key}=expected:{expected}|actual:{actual or '<missing>'}"
                for key, (expected, actual) in sorted(feature_contract_errors.items())
            )
            pair_diag["status"] = "failed"
            pair_diag["failure_component"] = "feature_contract"
            pair_diag["failure_reason"] = contract_reason
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            if require_all:
                _raise_model_load_failure(
                    message=f"failed loading models for {pair}: incompatible feature contract ({contract_reason})",
                    pair=pair,
                    component="feature_contract",
                    reason=contract_reason,
                )
            continue
        artifact_contract_failure = ""
        seen_artifact_paths: set[str] = set()
        model_artifact_groups = (
            ("regime", ("regime",)),
            ("meta", ("meta",)),
            ("swing_transformer", ("swing_transformer",)),
            ("swing_xgb", ("swing_xgb", "swing")),
            ("intraday_tcn", ("intraday_tcn",)),
            ("intraday_xgb", ("intraday_xgb", "intraday")),
            ("exit_policy", ("exit_policy", "exit", "exit_model")),
            ("directional_belief", ("directional_belief",)),
            ("reversal_failure", ("reversal_failure", "reversal_failure_xgb")),
            ("reversal_opportunity", ("reversal_opportunity", "reversal_opportunity_xgb")),
        )
        for component_name, artifact_keys in model_artifact_groups:
            artifact_ref = next(
                (
                    art.get(key)
                    for key in artifact_keys
                    if str(_artifact_path(art.get(key)) or "").strip()
                ),
                None,
            )
            artifact_path = str(_artifact_path(artifact_ref) or "").strip()
            if not artifact_path or artifact_path in seen_artifact_paths:
                continue
            seen_artifact_paths.add(artifact_path)
            try:
                artifact_digest = (
                    str(normalize_artifact_ref(artifact_ref).get("artifact_hash") or "")
                    .strip()
                    .lower()
                    if isinstance(artifact_ref, dict)
                    else None
                )
                resolved_artifact = resolve_model_artifact_path(
                    artifact_ref,
                    project_root=project_root,
                )
                if component_name == "directional_belief":
                    validate_directional_belief_artifact_contract(
                        resolved_artifact,
                        expected_contract=(
                            str((meta_json.get("feature_schema") or {}).get("belief_contract") or "").strip()
                        ),
                        expected_digest=artifact_digest,
                    )
                else:
                    validate_artifact_contract(
                        resolved_artifact,
                        label=f"{component_name}:{artifact_path}",
                        expected_digest=artifact_digest,
                    )
            except Exception as exc:
                artifact_contract_failure = (
                    f"{component_name}:{artifact_path}:{type(exc).__name__}:{exc}"
                )
                break
        if artifact_contract_failure:
            pair_diag["status"] = "failed"
            pair_diag["failure_component"] = "artifact_contract"
            pair_diag["failure_reason"] = artifact_contract_failure
            load_diag["pairs"][pair] = pair_diag
            load_diag["failed_pairs"].append(pair)
            load_diag["model_load_errors"] = int(load_diag.get("model_load_errors", 0)) + 1
            if require_all:
                _raise_model_load_failure(
                    message=f"failed loading models for {pair}: incompatible artifact contract ({artifact_contract_failure})",
                    pair=pair,
                    component="artifact_contract",
                    reason=artifact_contract_failure,
                )
            continue
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

        regime_ref = _artifact_ref_value(art, "regime")
        meta_ref = _artifact_ref_value(art, "meta")
        exit_ref = _artifact_ref_value(art, "exit_policy", "exit", "exit_model")
        belief_ref = _artifact_ref_value(art, "directional_belief")
        reversal_failure_ref = _artifact_ref_value(
            art,
            "reversal_failure",
            "reversal_failure_xgb",
        )
        reversal_opportunity_ref = _artifact_ref_value(
            art,
            "reversal_opportunity",
            "reversal_opportunity_xgb",
        )
        regime_path = str(_artifact_path(regime_ref) or "")
        meta_path = str(_artifact_path(meta_ref) or "")
        exit_path = str(_artifact_path(exit_ref) or "")
        belief_path = str(_artifact_path(belief_ref) or "")
        reversal_failure_path = str(_artifact_path(reversal_failure_ref) or "")
        reversal_opportunity_path = str(
            _artifact_path(reversal_opportunity_ref) or ""
        )
        regime, regime_err = _safe_load(RegimeHMM, regime_ref, project_root)
        meta, meta_err = _safe_load(MetaFilterXGB, meta_ref, project_root)
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

            swing_tf, swing_tf_err = _safe_load(
                SwingTransformer,
                _artifact_ref_value(art, "swing_transformer"),
                project_root,
            )
            swing_xgb, swing_err = _safe_load(
                SwingXGB,
                _artifact_ref_value(art, "swing_xgb", "swing"),
                project_root,
            )
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
            swing_xgb, swing_err = _safe_load(
                SwingXGB,
                _artifact_ref_value(art, "swing_xgb", "swing"),
                project_root,
            )
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

            intraday_tcn, intraday_tcn_err = _safe_load(
                IntradayTCN,
                _artifact_ref_value(art, "intraday_tcn"),
                project_root,
            )
            intraday_xgb, intraday_xgb_err = _safe_load(
                IntradayXGB,
                _artifact_ref_value(art, "intraday_xgb", "intraday"),
                project_root,
            )
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
            intraday_xgb, intraday_xgb_err = _safe_load(
                IntradayXGB,
                _artifact_ref_value(art, "intraday_xgb", "intraday"),
                project_root,
            )
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

        exit_model, exit_err = _safe_load(ExitPolicyXGB, exit_ref, project_root)
        reversal_failure_model, reversal_failure_err = _safe_load(
            ReversalFailureXGB,
            reversal_failure_ref,
            project_root,
        )
        reversal_opportunity_model, reversal_opportunity_err = _safe_load(
            ReversalOpportunityXGB,
            reversal_opportunity_ref,
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
        if bool(getattr(s, "belief_enabled", False)) and str(belief_path).strip():
            try:
                belief_digest = (
                    str(normalize_artifact_ref(belief_ref).get("artifact_hash") or "")
                    .strip()
                    .lower()
                    if isinstance(belief_ref, dict)
                    else None
                )
                belief_dir = resolve_model_artifact_path(
                    belief_ref,
                    project_root=project_root,
                )
                with artifact_lock(belief_dir):
                    belief_model = load_directional_belief_model_set(
                        belief_dir,
                        expected_contract=(
                            str((meta_json.get("feature_schema") or {}).get("belief_contract") or "").strip()
                        ),
                        expected_digest=belief_digest,
                    )
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

        exit_meta = _load_artifact_meta(exit_ref, project_root) if str(exit_path).strip() else {}
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
            component_feature_services=component_feature_services,
            rollout_policy=dict(rollout_policy),
            artifact_identities=_artifact_identity_map(art, project_root=project_root),
            rl_checkpoint_path=str(rl_checkpoint_path or ""),
            rl_checkpoint_content_sha256=str(rl_checkpoint_content_sha256 or ""),
        )
    return out, load_diag


def _seed_active_model_sets_from_manifest(
    *,
    svc: Any,
    project_root: Path,
    expected_manifest_sha256: str = "",
) -> dict[str, Any]:
    s = get_settings()
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
        manifest_bytes = manifest_candidate.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        expected_sha256 = str(expected_manifest_sha256 or "").strip().lower()
        if expected_sha256 and manifest_sha256 != expected_sha256:
            return {
                "seeded": False,
                "reason": "manifest_identity_changed",
                "path": str(manifest_candidate),
                "expected_manifest_sha256": expected_sha256,
                "manifest_sha256": manifest_sha256,
                "missing_pairs": sorted(list(configured_pairs)),
            }
        payload = json.loads(manifest_bytes.decode("utf-8"))
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
    failed_pairs: list[str] = []
    seed_errors: dict[str, str] = {}
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
        except Exception as exc:
            failed_pairs.append(pair_up)
            seed_errors[pair_up] = f"{type(exc).__name__}:{exc}"
            continue

    post = svc.get_active_model_sets(enabled_only=True)
    post_pairs = {str(p).upper() for p in list(post.keys())}
    post_missing_pairs = sorted(list(configured_pairs - post_pairs)) if configured_pairs else []
    if failed_pairs:
        reason = "seeded_partial" if seeded_pairs else "seed_failed"
    elif post_missing_pairs:
        reason = "seeded_partial"
    else:
        reason = "seeded" if seeded_pairs else "seed_failed"
    return {
        "seeded": bool(seeded_pairs),
        "reason": reason,
        "path": str(manifest_candidate),
        "manifest_sha256": str(manifest_sha256),
        "pairs": sorted(seeded_pairs),
        "failed_pairs": sorted(failed_pairs),
        "seed_errors": dict(sorted(seed_errors.items())),
        "missing_pairs": post_missing_pairs,
    }


def _load_manifest_active_rows(
    *,
    project_root: Path,
    expected_manifest_sha256: str = "",
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    s = get_settings()
    manifest_candidate = _resolve_optional_path(str(s.model_activation_manifest), project_root)
    if manifest_candidate is None:
        return {}, {"present": False, "path": str(s.model_activation_manifest)}
    try:
        manifest_bytes = manifest_candidate.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        expected_sha256 = str(expected_manifest_sha256 or "").strip().lower()
        if expected_sha256 and manifest_sha256 != expected_sha256:
            return {}, {
                "present": True,
                "path": str(manifest_candidate),
                "error": "manifest_identity_changed",
                "expected_manifest_sha256": expected_sha256,
                "manifest_sha256": manifest_sha256,
            }
        payload = json.loads(manifest_bytes.decode("utf-8"))
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
    return out, {
        "present": True,
        "path": str(manifest_candidate),
        "manifest_sha256": str(manifest_sha256),
    }


# Carved into fxstack.runtime.artifact_paths. Re-bound under original names.
from fxstack.runtime.artifact_paths import (
    common_registry_root as _common_registry_root,
    normalized_registry_path as _normalized_registry_path,
)


def _activation_consistency(
    *,
    svc: Any,
    project_root: Path,
    configured_pairs: list[str],
    loaded_model_sets: dict[str, LoadedModelSet],
    expected_manifest_sha256: str = "",
) -> dict[str, Any]:
    manifest_rows, manifest_meta = _load_manifest_active_rows(
        project_root=project_root,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    db_rows = svc.get_active_model_sets(enabled_only=True)
    configured = {str(pair).upper().strip() for pair in list(configured_pairs)}
    manifest_pairs = {pair for pair in manifest_rows.keys() if pair in configured}
    db_pairs = {str(pair).upper().strip() for pair in db_rows.keys() if str(pair).upper().strip() in configured}
    loaded_pairs = {str(pair).upper().strip() for pair in loaded_model_sets.keys() if str(pair).upper().strip() in configured}

    manifest_db_mismatch: list[str] = []
    runtime_db_mismatch: list[str] = []
    manifest_db_mismatch_details: dict[str, list[str]] = {}
    runtime_db_mismatch_details: dict[str, list[str]] = {}
    for pair in sorted(configured):
        manifest_row = dict(manifest_rows.get(pair) or {})
        db_row = dict(db_rows.get(pair) or {})
        manifest_reasons: list[str] = []
        runtime_reasons: list[str] = []
        manifest_path = _normalized_registry_path(str(manifest_row.get("registry_path") or ""), project_root=project_root)
        db_path = _normalized_registry_path(str(db_row.get("registry_path") or ""), project_root=project_root)
        if bool(manifest_row) != bool(db_row):
            manifest_reasons.append("pair_presence")
        elif manifest_row and db_row:
            if str(manifest_row.get("model_set_id") or "") != str(db_row.get("model_set_id") or ""):
                manifest_reasons.append("model_set_id")
            if manifest_path != db_path:
                manifest_reasons.append("registry_path")
            manifest_artifacts = _artifact_identity_map(
                dict(manifest_row.get("artifacts") or {}),
                project_root=project_root,
            )
            db_artifacts = _artifact_identity_map(
                dict(db_row.get("artifacts_json") or {}),
                project_root=project_root,
            )
            if manifest_artifacts != db_artifacts:
                manifest_reasons.append("artifact_identity")
        if manifest_reasons:
            manifest_db_mismatch.append(pair)
            manifest_db_mismatch_details[pair] = manifest_reasons

        loaded_row = loaded_model_sets.get(pair)
        if loaded_row is None:
            runtime_reasons.append("pair_presence")
        elif not db_row:
            runtime_reasons.append("db_pair_missing")
        else:
            loaded_path = _normalized_registry_path(str(loaded_row.registry_path or ""), project_root=project_root)
            if str(loaded_row.model_set_id or "") != str(db_row.get("model_set_id") or ""):
                runtime_reasons.append("model_set_id")
            if loaded_path != db_path:
                runtime_reasons.append("registry_path")
            db_artifacts = _artifact_identity_map(
                dict(db_row.get("artifacts_json") or {}),
                project_root=project_root,
            )
            if dict(getattr(loaded_row, "artifact_identities", {}) or {}) != db_artifacts:
                runtime_reasons.append("artifact_identity")
        if runtime_reasons:
            runtime_db_mismatch.append(pair)
            runtime_db_mismatch_details[pair] = runtime_reasons

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
        "manifest_db_mismatch_details": dict(sorted(manifest_db_mismatch_details.items())),
        "runtime_db_mismatch_details": dict(sorted(runtime_db_mismatch_details.items())),
        "configured_pairs": sorted(list(configured)),
        "manifest_active_pairs": sorted(list(manifest_pairs)),
        "db_active_pairs": sorted(list(db_pairs)),
        "runtime_loaded_pairs": sorted(list(loaded_pairs)),
        "active_pair_count": int(len(configured)),
        "active_registry_root": _common_registry_root(runtime_registry_paths),
    }


def _require_required_model_startup_consistency(
    *,
    settings: Any,
    configured_pairs: list[str],
    stage: str,
    payload: dict[str, Any],
) -> None:
    """Turn required-model seed/identity diagnostics into a startup gate."""

    if not bool(getattr(settings, "require_active_models", True)):
        return
    required = {
        str(pair).strip().upper()
        for pair in list(configured_pairs or [])
        if str(pair).strip()
    }
    data = dict(payload or {})
    failures: list[str] = []
    if str(stage) == "manifest_seed":
        seeded = {
            str(pair).strip().upper()
            for pair in list(data.get("pairs") or [])
            if str(pair).strip()
        }
        missing = sorted(required - seeded)
        failed = sorted(
            str(pair).strip().upper()
            for pair in list(data.get("failed_pairs") or [])
            if str(pair).strip()
        )
        reason = str(data.get("reason") or "seed_failed")
        if reason != "seeded":
            failures.append(f"reason={reason}")
        if missing:
            failures.append("missing_pairs=" + ",".join(missing))
        if failed:
            failures.append("failed_pairs=" + ",".join(failed))
    elif str(stage) == "activation_consistency":
        for key in ("manifest_active_pairs", "db_active_pairs", "runtime_loaded_pairs"):
            present = {
                str(pair).strip().upper()
                for pair in list(data.get(key) or [])
                if str(pair).strip()
            }
            missing = sorted(required - present)
            if missing:
                failures.append(f"{key}_missing=" + ",".join(missing))
        if not bool(data.get("active_manifest_matches_db", False)):
            failures.append(
                "manifest_db_mismatch="
                + ",".join(str(item) for item in list(data.get("manifest_db_mismatch_pairs") or []))
            )
        if not bool(data.get("runtime_loaded_matches_db", False)):
            failures.append(
                "runtime_db_mismatch="
                + ",".join(str(item) for item in list(data.get("runtime_db_mismatch_pairs") or []))
            )
    else:
        failures.append(f"unknown_stage={stage}")
    if failures:
        raise RuntimeError(
            f"required_model_{stage}_failed:" + "|".join(failures)
        )


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
    intraday_required = _startup_intraday_required_columns(loaded)
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
    intraday_required = _startup_intraday_required_columns(loaded)
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


# Carved into fxstack.runtime.positions. Re-bound under original name.
from fxstack.runtime.positions import position_side as _position_side  # noqa: E402


def _reversal_exit_ready(
    *,
    reversal_context_active: bool,
    signal_allowed: bool,
    has_reversal_models: bool,
    reversal_blocking_reasons: list[str],
    reversal_failure_prob: float,
    reversal_opportunity_prob: float,
    reversal_failure_min_prob: float,
    reversal_opportunity_min_prob: float,
) -> bool:
    return bool(
        reversal_context_active
        and signal_allowed
        and has_reversal_models
        and len(_reversal_blocking_reasons(list(reversal_blocking_reasons or []))) == 0
        and float(reversal_failure_prob) >= float(reversal_failure_min_prob)
        and float(reversal_opportunity_prob) >= float(reversal_opportunity_min_prob)
    )


def _quote_rate_map(ticks: Any) -> dict[str, float]:
    """Mid prices per pair, for converting contract value into account currency.

    Without this every pair is sized as if 1.0 of price movement on 1 lot were
    100,000 ACCOUNT-currency units, which is only true when the quote currency is
    the account currency. Measured consequence at a 30-pip stop on $10k at 0.5%:
    EURGBP over-risked by 22%, USDCHF by 7%, USDCAD under by 29%, and all six JPY
    pairs refused outright because the computed size fell below the 0.01 lot
    minimum.
    """

    out: dict[str, float] = {}
    for pair, payload in dict(ticks or {}).items():
        row = dict(payload or {})
        bid = _safe_float(row.get("bid"), 0.0)
        ask = _safe_float(row.get("ask"), 0.0)
        mid = (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else _safe_float(row.get("mid"), 0.0)
        if mid > 0.0:
            out[str(pair).strip().upper()] = float(mid)
    return out


def _entry_contract_value(*, pair: str, quote_rates: Any, settings: Any) -> float:
    """Account-currency contract value per price unit, or 0.0 if unresolvable.

    0.0 means "do not risk-size this pair on this cycle" -- the caller falls back
    to the legacy lot path rather than sizing against a value known to be wrong.
    """

    # ``account_currency`` is a declared setting (settings.py). The getattr guard
    # is only for test doubles / legacy settings objects that predate it -- it is
    # NOT a silent production default, because a wrong account currency mis-sizes
    # every pair whose quote currency differs.
    return float(
        account_value_per_price_unit(
            pair=str(pair),
            rates=dict(quote_rates or {}),
            account_currency=str(getattr(settings, "account_currency", "USD") or "USD"),
        )
    )


def _annotate_positions_with_contract_value(
    positions: list[dict[str, Any]] | None,
    *,
    quote_rates: Any,
    settings: Any,
) -> list[dict[str, Any]]:
    """Attach the sizer's per-lot contract value to open-position rows.

    `build_portfolio_book` prices a stop-out as |open - sl| * vpu * lots, but it
    has no rate service of its own -- rows without `value_per_price_unit` simply
    publish no stop risk, and `evaluate_book_stress` then reports 0.0 ("not
    measured"). This is the runner's half of that contract. A vpu of 0.0 (rate
    unresolvable this cycle) is deliberately NOT attached: a stop risk priced
    with a guessed conversion is the exact fabricated number stress.py refuses
    to invent.
    """

    out: list[dict[str, Any]] = []
    for raw in list(positions or []):
        row = dict(raw or {})
        if "value_per_price_unit" not in row:
            symbol = str(row.get("symbol") or row.get("pair") or "").strip().upper()
            if symbol:
                vpu = float(_entry_contract_value(pair=symbol, quote_rates=quote_rates, settings=settings))
                if vpu > 0.0:
                    row["value_per_price_unit"] = vpu
        out.append(row)
    return out


def _resolved_entry_certification_mode(settings: Any) -> str:
    """The binding certification mode: ``required`` or ``exploration_demo``.

    ``FXSTACK_ENTRY_CERTIFICATION_MODE`` wins when set; otherwise the legacy
    boolean ``require_certified_models_for_entry`` derives it (True -> required,
    False -> exploration_demo), so pre-enum deployments keep their behavior but
    gain the demo-attestation fence and the per-decision stamp.
    """

    mode = str(getattr(settings, "entry_certification_mode", "") or "").strip().lower()
    if mode in {"required", "exploration_demo"}:
        return mode
    return (
        "required"
        if bool(getattr(settings, "require_certified_models_for_entry", True))
        else "exploration_demo"
    )


def _exploration_demo_entry_block_reason(*, mode: str, broker_account_mode: str) -> str:
    """The fence around exploration mode: uncertified entries need a DEMO account.

    ``broker_account_mode`` is the EA's heartbeat-attested value ("demo"/"real").
    Anything other than an attested demo -- including "" before the first
    heartbeat -- fails closed. Entry-only like every other gate here.
    """

    if str(mode) != "exploration_demo":
        return ""
    if str(broker_account_mode or "").strip().lower() != "demo":
        return "exploration_demo_requires_demo_account_attestation"
    return ""


def _uncertified_entry_block_reason(*, preflight: dict[str, Any], settings: Any) -> str:
    """Block NEW entries when the live model set carries no statistical warrant.

    The certificate gate in ``training/activation.py`` is prospective: it stops
    an unvalidated model being activated, but a set activated before the gate was
    switched on keeps trading. Reporting that (``certificate_coverage``) makes it
    visible; it does not make it stop. This does.

    Entry-only by construction, matching the existing rollout/heartbeat gates:
    ``exit``, ``reduce`` and ``tighten_stop`` never consult it, so an uncertified
    set can always manage and close what it already holds. Blocking protective
    actions on a governance failure would strand open positions, which is a
    strictly worse outcome than the one being prevented.

    Returns the block reason, or "" when entries are permitted. Fails OPEN only
    under an explicitly configured ``exploration_demo`` certification mode (or
    its legacy spelling ``FXSTACK_REQUIRE_CERTIFIED_MODELS_FOR_ENTRY=0``), and
    even then the runtime loop separately requires a fresh broker DEMO
    attestation via ``_exploration_demo_entry_block_reason``.
    """

    if _resolved_entry_certification_mode(settings) == "exploration_demo":
        return ""
    coverage = dict(dict(preflight or {}).get("validation_certificates") or {})
    if not coverage:
        # No report at all is not evidence of health.
        return "models_uncertified"
    if coverage.get("error"):
        return "models_uncertified"
    return "" if bool(coverage.get("all_certified")) else "models_uncertified"


def _entry_venue_readiness_reasons(*, paper_mode: bool, mt4_fresh: bool, ticks_fresh: bool, tick_present: bool) -> list[str]:
    if paper_mode:
        return []
    reasons: list[str] = []
    if not mt4_fresh:
        reasons.append("mt4_stale")
    if not ticks_fresh:
        reasons.append("tick_feed_stale")
    if not tick_present:
        reasons.append("missing_live_tick")
    return reasons


_ADAPTIVE_NUMERIC_DEFAULTS: dict[str, float] = {
    "regime_prob": 0.0,
    "swing_prob": 0.0,
    "entry_prob": 0.0,
    "trade_prob": 0.0,
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
    "calibrated_ev_bps": 0.0,
    "pullback_depth_20": 0.0,
    "pushup_depth_20": 0.0,
    "h1_trend_strength_20": 0.0,
    "h4_trend_strength_20": 0.0,
    "d_trend_strength_20": 0.0,
    "uncertainty_score": 1.0,
    "model_disagreement_score": 1.0,
    "htf_alignment_score": 0.0,
    "directional_swing_confidence": 0.0,
    "pullback_quality_score": 0.0,
    "extension_penalty_score": 1.0,
    "resume_trigger_score": 0.0,
    "expected_edge_bps": 0.0,
}
_ADAPTIVE_BOOL_DEFAULTS: dict[str, bool] = {
    "session_entry_blocked": False,
}
_ADAPTIVE_TEXT_DEFAULTS: dict[str, str] = {
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


def _adaptive_row_snapshot(
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

    feature_ts = pd.to_datetime(ts_value or source.get("ts"), utc=True, errors="coerce")
    adaptive_bar_key = float(loop_ts) if pd.isna(feature_ts) else float(pd.Timestamp(feature_ts).timestamp())

    def _signal_metric(name: str, default: float) -> float:
        for candidate in (getattr(signal, name, None), source.get(name)):
            if candidate is None:
                continue
            value = _safe_float(candidate, float("nan"))
            if math.isfinite(value):
                return float(value)
        return float(default)

    row: dict[str, Any] = dict(source)
    row.update(
        {
            "pair": str(pair).upper(),
            "ts": str(ts_value or source.get("ts", "")),
            # This buffer is configured in market bars, not runtime polls.  Keying
            # it by loop time caused an unchanged M5 row to be counted once every
            # cycle and displaced the real causal lookback in about 21 minutes.
            "_adaptive_cycle_key": float(adaptive_bar_key),
            "signal_side": str(getattr(signal, "side", "long") or "long").strip().lower(),
            "spread_bps": float(spread_bps),
            "max_spread_bps": float(max_spread_bps),
            "scenario_bucket": str(getattr(signal, "scenario_bucket", source.get("scenario_bucket", "")) or ""),
            "regime_bucket": str(source.get("regime_bucket", "")),
            "session_bucket": str(getattr(signal, "session_bucket", source.get("session_bucket", "")) or ""),
            "session_entry_blocked": bool(getattr(signal, "session_entry_blocked", False)),
            "session_entry_block_reason": str(getattr(signal, "session_entry_block_reason", "") or ""),
            "regime_prob": _signal_metric("regime_prob", 0.0),
            "swing_prob": _signal_metric("swing_prob", 0.0),
            "entry_prob": _signal_metric("entry_prob", 0.0),
            "trade_prob": _signal_metric("trade_prob", 0.0),
            "uncertainty_score": _signal_metric("uncertainty_score", 1.0),
            "model_disagreement_score": _signal_metric("model_disagreement_score", 1.0),
            "htf_alignment_score": _signal_metric("htf_alignment_score", 0.0),
            "directional_swing_confidence": _signal_metric("directional_swing_confidence", 0.0),
            "pullback_quality_score": _signal_metric("pullback_quality_score", 0.0),
            "extension_penalty_score": _signal_metric("extension_penalty_score", 1.0),
            "resume_trigger_score": _signal_metric("resume_trigger_score", 0.0),
            "expected_edge_bps": _signal_metric("expected_edge_bps", 0.0),
            "calibrated_ev_bps": _signal_metric("calibrated_ev_bps", 0.0),
            "baseline_rejection_reason": str(baseline_rejection_reason or ""),
            "strict_rejection_reason": str(baseline_rejection_reason or ""),
        }
    )
    for col, default in _ADAPTIVE_NUMERIC_DEFAULTS.items():
        value = _safe_float(row.get(col, default), default)
        row[col] = float(value) if math.isfinite(value) else float(default)
    for col, default in _ADAPTIVE_BOOL_DEFAULTS.items():
        row[col] = bool(row.get(col, default))
    for col, default in _ADAPTIVE_TEXT_DEFAULTS.items():
        row[col] = str(row.get(col, default) or default)
    return row


def _append_adaptive_history(
    history: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    max_history: int,
) -> None:
    """Keep a bounded, ordered history of distinct feature bars."""
    bounded = max(1, int(max_history))
    rows_by_key: dict[float, dict[str, Any]] = {}
    for item in [*list(history), dict(snapshot)]:
        key = _safe_float(item.get("_adaptive_cycle_key"), 0.0)
        if key <= 0.0:
            continue
        rows_by_key[float(key)] = dict(item)
    history[:] = [rows_by_key[key] for key in sorted(rows_by_key)[-bounded:]]


def _bootstrap_adaptive_history(
    *,
    feature_store: ParquetStore,
    provider: str,
    pairs: list[str],
    timeframe: str,
    history_bars: int,
) -> dict[str, list[dict[str, Any]]]:
    """Seed adaptive normalization with distinct causal feature bars on startup."""
    bounded = max(1, int(history_bars))
    tail_files, _ = _feature_tail_spec(timeframe)
    history: dict[str, list[dict[str, Any]]] = {}
    for raw_pair in pairs:
        pair = str(raw_pair).upper()
        recent = feature_store.read_recent_rows(
            provider=str(provider),
            pair=pair,
            timeframe=str(timeframe).upper(),
            tail_files=int(tail_files),
            max_rows=int(bounded),
        )
        rows: list[dict[str, Any]] = []
        for source in recent.to_dict(orient="records"):
            feature_ts = pd.to_datetime(source.get("ts"), utc=True, errors="coerce")
            if pd.isna(feature_ts):
                continue
            row = dict(source)
            row["pair"] = pair
            row["ts"] = str(pd.Timestamp(feature_ts).isoformat())
            row["_adaptive_cycle_key"] = float(pd.Timestamp(feature_ts).timestamp())
            for col, default in _ADAPTIVE_NUMERIC_DEFAULTS.items():
                value = _safe_float(row.get(col, default), default)
                row[col] = float(value) if math.isfinite(value) else float(default)
            for col, default in _ADAPTIVE_BOOL_DEFAULTS.items():
                row[col] = bool(row.get(col, default))
            for col, default in _ADAPTIVE_TEXT_DEFAULTS.items():
                row[col] = str(row.get(col, default) or default)
            rows.append(row)
        history[pair] = rows[-bounded:]
    return history


def _adaptive_frames_from_history(
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
        for col, default in _ADAPTIVE_NUMERIC_DEFAULTS.items():
            if col not in frame.columns:
                frame[col] = float(default)
            frame[col] = (
                pd.to_numeric(frame[col], errors="coerce")
                .replace([float("inf"), float("-inf")], float(default))
                .fillna(float(default))
                .astype(float)
            )
        for col, default in _ADAPTIVE_BOOL_DEFAULTS.items():
            if col not in frame.columns:
                frame[col] = bool(default)
            frame[col] = frame[col].fillna(bool(default)).astype(bool)
        for col, default in _ADAPTIVE_TEXT_DEFAULTS.items():
            if col not in frame.columns:
                frame[col] = str(default)
            frame[col] = frame[col].fillna(default).astype(str)
        frames[pair] = frame
    return frames


def _belief_signal_proxy(meta: dict[str, Any]) -> SimpleNamespace:
    side_raw = str(meta.get("side") or meta.get("signal_side") or meta.get("position_side") or "").strip().lower()
    if side_raw in {"buy", "long"}:
        side = "long"
    elif side_raw in {"sell", "short"}:
        side = "short"
    else:
        side = "unknown"

    def _metric(name: str, default: float) -> float:
        value = _safe_float(meta.get(name), default)
        return float(value) if math.isfinite(value) else float(default)

    return SimpleNamespace(
        pair=str(meta.get("pair") or ""),
        ts=str(meta.get("ts") or ""),
        side=str(side),
        regime_prob=_metric("regime_prob", 0.0),
        swing_prob=_metric("swing_prob", 0.0),
        entry_prob=_metric("entry_prob", 0.0),
        trade_prob=_metric("trade_prob", 0.0),
        uncertainty_score=_metric("uncertainty_score", 1.0),
        model_disagreement_score=_metric("model_disagreement_score", 1.0),
        directional_swing_confidence=_metric("directional_swing_confidence", 0.0),
        htf_alignment_score=_metric("htf_alignment_score", 0.0),
        pullback_quality_score=_metric("pullback_quality_score", 0.0),
        resume_trigger_score=_metric("resume_trigger_score", 0.0),
        extension_penalty_score=_metric("extension_penalty_score", 1.0),
        structure_timing_score=_metric("structure_timing_score", 0.0),
        expected_edge_bps=_metric("expected_edge_bps", 0.0),
        spread_bps=_metric("spread_bps", 0.0),
        scenario_bucket=str(meta.get("scenario_bucket") or ""),
        context_frame_profile=str(meta.get("context_frame_profile") or ""),
    )


def _directional_belief_policy_diag(settings: Any) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(settings, "belief_enabled", False)),
        "runtime_required": bool(getattr(settings, "belief_runtime_required", False)),
        "short_horizon_bars": int(getattr(settings, "belief_short_horizon_bars", 3) or 3),
        "trade_horizon_bars": int(getattr(settings, "belief_trade_horizon_bars", 12) or 12),
        "structural_horizon_bars": int(getattr(settings, "belief_structural_horizon_bars", 48) or 48),
    }


def _attach_directional_belief(
    *,
    decisions: list[dict[str, Any]],
    loaded_model_sets: dict[str, LoadedModelSet],
    adaptive_rows_by_pair: dict[str, dict[str, Any]],
    settings: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    enabled = bool(getattr(settings, "belief_enabled", False))
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
        live_playbook = str(meta.get("adaptive_playbook") or adaptive_row.get("playbook") or "")
        live_environment_state = str(meta.get("adaptive_environment_state") or adaptive_row.get("environment_state") or "")
        belief_row = dict(adaptive_row)
        belief_row["pair"] = str(pair)
        belief_row["ts"] = str(ts_value)
        belief_row["playbook"] = str(live_playbook)
        belief_row["environment_state"] = str(live_environment_state)
        belief_row["playbook_score"] = float(_safe_float(meta.get("adaptive_playbook_score", adaptive_row.get("playbook_score", 0.0)), 0.0))
        belief_row["location_score"] = float(_safe_float(meta.get("adaptive_location_score", adaptive_row.get("location_score", 0.0)), 0.0))
        belief_row["trigger_score"] = float(_safe_float(meta.get("adaptive_trigger_score", adaptive_row.get("trigger_score", 0.0)), 0.0))
        belief_row["macro_coherence_score"] = float(_safe_float(meta.get("adaptive_macro_coherence_score", adaptive_row.get("macro_coherence_score", 0.0)), 0.0))
        belief_row["hostility_score"] = float(_safe_float(meta.get("adaptive_hostility_score", adaptive_row.get("hostility_score", 0.0)), 0.0))
        belief_row["adaptive_playbook"] = str(live_playbook)
        belief_row["adaptive_environment_state"] = str(live_environment_state)

        def _risk_metric(name: str) -> float:
            for source in (adaptive_row, meta):
                if name not in source or source.get(name) is None:
                    continue
                value = _safe_float(source.get(name), float("nan"))
                if math.isfinite(value):
                    return float(value)
            return 1.0

        belief_row["uncertainty_score"] = _risk_metric("uncertainty_score")
        belief_row["model_disagreement_score"] = _risk_metric("model_disagreement_score")
        belief_row["extension_penalty_score"] = _risk_metric("extension_penalty_score")
        belief_row["scenario_bucket"] = str(meta.get("scenario_bucket") or adaptive_row.get("scenario_bucket") or "")
        belief_row["regime_bucket"] = str(meta.get("regime_bucket") or adaptive_row.get("regime_bucket") or "")
        belief_meta = dict(belief_row)
        proxy_payload = dict(meta)
        proxy_payload.update(belief_row)
        proxy_payload["side"] = (
            decision.get("side")
            or belief_row.get("signal_side")
            or meta.get("side")
            or meta.get("position_side")
            or ""
        )
        signal_proxy = _belief_signal_proxy(proxy_payload)
        belief = empty_directional_belief(pair=pair, ts=ts_value, source_mode="disabled")
        if enabled and loaded is not None and loaded.belief_model is not None and adaptive_row:
            belief = compute_directional_belief(
                row=belief_row or meta,
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
        elif enabled and loaded is not None and loaded.belief_model is not None:
            belief = empty_directional_belief(pair=pair, ts=ts_value, source_mode="artifact_missing")
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
        record_source_mode = str(getattr(record, "source_mode", "") or "").strip().lower()
        telemetry_only = (not enabled) or record_source_mode == "telemetry_only"
        meta["cross_pair_rank_position"] = int(record.rank_position)
        meta["cross_pair_influence_score"] = float(record.influence_score)
        meta["cross_pair_recommendation_strength"] = float(record.recommendation_strength)
        meta["cross_pair_influenced_by_pairs"] = list(record.influenced_by_pairs)
        meta["cross_pair_reason_codes"] = list(record.cross_pair_reason_codes)
        meta["cross_pair_source_mode"] = "telemetry_only" if telemetry_only else str(record.source_mode)
        meta["cross_pair_influence_mode"] = str(influence_mode or "off")
        meta["cross_pair_influence_adjustment"] = float(0.0 if telemetry_only else (float(record.recommendation_strength) - 0.5) * 0.16)
        meta["cross_pair_soft_block"] = False
        meta["cross_pair_hard_block"] = False
        if not telemetry_only and influence_mode in {"soft_gate", "hard_gate"} and float(record.recommendation_strength) < 0.30:
            meta["cross_pair_soft_block"] = True
        if not telemetry_only and influence_mode == "hard_gate" and float(record.recommendation_strength) < 0.20:
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


def _adaptive_open_position_map(
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


# AGENT FLOW: The direct adaptive evaluator runs only when direct adaptive execution is enabled.
def _apply_adaptive_ranking(
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
    rejection_reason_counts: dict[str, int] = {}
    rejection_pair_map: dict[str, str] = {}
    playbook_counts: dict[str, int] = {}
    environment_counts: dict[str, int] = {}
    aggressive_fallback_count = 0
    overlay_outputs: dict[int, Any] = {}
    adaptive_policy_enabled = bool(getattr(settings, "adaptive_execution_enabled", False))
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
            "adaptive_policy_enabled": bool(adaptive_policy_enabled),
            "adaptive_candidate_count": 0,
            "adaptive_ranked_count": 0,
            "adaptive_selected_count": 0,
            "adaptive_remaining_slots": int(remaining_slots),
            "adaptive_max_new_entries": int(max_new_entries if use_ranking else remaining_slots),
            "adaptive_aggressive_fallback_count": 0,
            "adaptive_rejection_reason_counts": {},
            "adaptive_rejections_by_pair": {},
            "adaptive_playbook_counts": {},
            "adaptive_environment_counts": {},
            "adaptive_dominant_rejection_reason": "",
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
                    "press_count": 0,
                    "stand_down_count": 0,
                },
            },
            "campaign_state_counts": {},
        }

    exit_registry = recent_exit_registry or {}
    bar_index_map = pair_bar_index or {}
    open_positions = _adaptive_open_position_map(
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
        adaptive_reason = "adaptive_history_unavailable"
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
            # The adaptive history row owns market/setup context, while the
            # scorer owns the current-cycle model and execution evidence.  The
            # latter is computed after the feature row and therefore must be
            # overlaid here before the intelligent enter-vs-abstain decision.
            # Without this merge a valid live structure score, for example,
            # exists in decision metadata but is invisible to the policy.
            for evidence_field in (
                "regime_prob",
                "swing_prob",
                "entry_prob",
                "trade_prob",
                "directional_swing_confidence",
                "expected_edge_bps",
                "calibrated_ev_bps",
                "entry_quality_score",
                "adaptive_quality_score",
                "uncertainty_score",
                "model_disagreement_score",
                "htf_alignment_score",
                "pullback_quality_score",
                "resume_trigger_score",
                "structure_timing_score",
                "extension_penalty_score",
            ):
                if evidence_field not in meta or meta.get(evidence_field) is None:
                    continue
                try:
                    evidence_value = float(meta[evidence_field])
                except (TypeError, ValueError):
                    continue
                if math.isfinite(evidence_value):
                    current_row[evidence_field] = float(evidence_value)
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
        meta["adaptive_entry_mode"] = "standard"
        meta["adaptive_trend_probe_used"] = False
        meta["adaptive_recovered_strict_reasons"] = []
        meta["adaptive_size_scale"] = 1.0
        meta["intelligent_decision"] = {}
        meta["intelligent_evidence"] = {}
        meta["adaptive_advisories"] = []
        meta["trend_probe_diagnostics"] = {}
        meta["adaptive_allowed"] = False
        meta["adaptive_portfolio_rank"] = None
        meta["adaptive_selected"] = False
        meta["adaptive_rejection_reason"] = str(adaptive_reason)
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

        if not adaptive_policy_enabled:
            adaptive_reason = "adaptive_policy_disabled"
            _append_policy_trace(meta, stage="adaptive_playbook", verdict="skip", reason=str(adaptive_reason))
        elif position_open:
            adaptive_reason = "adaptive_position_open"
            _append_policy_trace(meta, stage="adaptive_playbook", verdict="skip", reason=str(adaptive_reason))
        elif not current_row:
            adaptive_reason = "adaptive_history_unavailable"
            _append_policy_trace(meta, stage="adaptive_playbook", verdict="skip", reason=str(adaptive_reason))
        else:
            adaptive_eval = evaluate_adaptive_entry(
                row=current_row,
                strict_ready=bool(meta.get("entry_ready", False)),
                open_positions=open_positions,
                settings=settings,
                fallback_margin=0.08,
            )
            cross_pair_source_mode = str(meta.get("cross_pair_source_mode") or "").strip().lower()
            telemetry_only_cross_pair = cross_pair_source_mode == "telemetry_only"
            cross_pair_adjustment = (
                0.0
                if telemetry_only_cross_pair
                else float(_safe_float(meta.get("cross_pair_influence_adjustment", 0.0), 0.0))
            )
            cross_pair_strength = float(_safe_float(meta.get("cross_pair_recommendation_strength", 0.5), 0.5))
            adjusted_quality = float(
                _clip01(float(_safe_float(adaptive_eval.get("adaptive_entry_quality"), 0.0)) + cross_pair_adjustment)
            )
            cross_pair_soft_block = bool(meta.get("cross_pair_soft_block", False)) and not telemetry_only_cross_pair
            if cross_pair_soft_block:
                adjusted_quality = float(_clip01(float(adjusted_quality) * 0.85))
            if cross_pair_adjustment or cross_pair_soft_block:
                adaptive_eval = _evaluate_adaptive_entry_with_quality_override(
                    row=current_row,
                    strict_ready=bool(meta.get("entry_ready", False)),
                    open_positions=open_positions,
                    settings=settings,
                    fallback_margin=0.08,
                    quality_override=float(adjusted_quality),
                )
            else:
                adaptive_eval["adaptive_entry_quality"] = float(adjusted_quality)
            adaptive_eval["cross_pair_rank_position"] = int(_safe_float(meta.get("cross_pair_rank_position"), 0.0))
            adaptive_eval["cross_pair_influence_score"] = float(_safe_float(meta.get("cross_pair_influence_score"), 0.0))
            adaptive_eval["cross_pair_recommendation_strength"] = float(cross_pair_strength)
            adaptive_eval["cross_pair_reason_codes"] = list(meta.get("cross_pair_reason_codes", []) or [])
            if bool(meta.get("cross_pair_hard_block", False)) and not telemetry_only_cross_pair:
                adaptive_eval["adaptive_advisories"] = [
                    *list(adaptive_eval.get("adaptive_advisories", []) or []),
                    "cross_pair_counterevidence",
                ]
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
                # Churn cooldowns. These are computed every cycle and appended to
                # ``adaptive_advisories``, a bucket with zero non-telemetry
                # consumers -- so after a stop-out the runtime can re-enter the
                # same pair in the same direction on the very next bar, and can
                # re-attack a thesis the campaign machine already abandoned.
                #
                # They now WITHHOLD the entry rather than only annotating it, and
                # the reason is recorded as an explicit greppable field so the
                # firing rate stays measurable. Strictly stricter: this can only
                # decline an entry the previous code would have taken.
                cooldown_block_reason = ""
                if bool(reentry_eval.get("blocked")):
                    adaptive_eval["adaptive_advisories"] = [
                        *list(adaptive_eval.get("adaptive_advisories", []) or []),
                        str(reentry_eval.get("reason") or "adaptive_reentry_cooldown"),
                    ]
                    cooldown_block_reason = "adaptive_reentry_cooldown"
                if bool(campaign_candidate.reentry_blocked):
                    adaptive_eval["adaptive_advisories"] = [
                        *list(adaptive_eval.get("adaptive_advisories", []) or []),
                        str(campaign_candidate.reentry_block_reason or "campaign_abandon_cooldown"),
                    ]
                    cooldown_block_reason = cooldown_block_reason or "campaign_abandon_cooldown"
                if cooldown_block_reason and bool(adaptive_eval.get("adaptive_allowed", False)):
                    adaptive_eval["adaptive_allowed"] = False
                    adaptive_eval["adaptive_rejection_reason"] = cooldown_block_reason
                adaptive_eval["adaptive_cooldown_block_reason"] = str(cooldown_block_reason)
            adaptive_allowed = bool(adaptive_eval.get("adaptive_allowed", False))
            adaptive_reason = str(adaptive_eval.get("adaptive_rejection_reason") or "adaptive_reject")
            playbook = str(adaptive_eval.get("playbook") or playbook or PLAYBOOK_NO_TRADE)
            meta["adaptive_playbook"] = str(playbook)
            meta["adaptive_sleeve"] = str(playbook_to_sleeve(playbook))
            sleeve_snapshot = (sleeve_health_snapshots or {}).get(playbook_to_sleeve(playbook))
            meta["sleeve_health_score"] = float(getattr(sleeve_snapshot, "score", 0.5))
            meta["sleeve_health_state"] = str(getattr(sleeve_snapshot, "state", "healthy"))
            meta["adaptive_entry_quality"] = float(_safe_float(adaptive_eval.get("adaptive_entry_quality", 0.0), 0.0))
            meta["adaptive_entry_mode"] = str(adaptive_eval.get("adaptive_entry_mode") or "standard")
            meta["adaptive_trend_probe_used"] = bool(adaptive_eval.get("adaptive_trend_probe_used", False))
            meta["adaptive_recovered_strict_reasons"] = [
                str(reason)
                for reason in list(adaptive_eval.get("adaptive_recovered_strict_reasons", []) or [])
                if str(reason).strip()
            ]
            meta["adaptive_size_scale"] = float(
                _clip01(adaptive_eval.get("adaptive_size_scale", 1.0))
            )
            meta["intelligent_decision"] = dict(
                adaptive_eval.get("intelligent_decision") or {}
            )
            meta["intelligent_evidence"] = dict(
                adaptive_eval.get("intelligent_evidence") or {}
            )
            meta["adaptive_advisories"] = [
                str(reason)
                for reason in list(adaptive_eval.get("adaptive_advisories", []) or [])
                if str(reason).strip()
            ]
            meta["trend_probe_diagnostics"] = dict(adaptive_eval.get("trend_probe_diagnostics") or {})
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
                    "entry_mode": str(meta.get("adaptive_entry_mode") or "standard"),
                    "recovered_strict_reasons": list(meta.get("adaptive_recovered_strict_reasons", []) or []),
                    "intelligent_decision": dict(meta.get("intelligent_decision") or {}),
                    "advisories": list(meta.get("adaptive_advisories") or []),
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
                overlay_reason = "overlay_low_conviction_advisory"
                meta["adaptive_advisories"] = list(dict.fromkeys([
                    *list(meta.get("adaptive_advisories") or []),
                    overlay_reason,
                ]))
            if adaptive_allowed and str(meta.get("thesis_stage") or "") == "stand_down":
                overlay_reason = "overlay_stand_down_advisory"
                meta["adaptive_advisories"] = list(dict.fromkeys([
                    *list(meta.get("adaptive_advisories") or []),
                    overlay_reason,
                ]))
            meta["adaptive_allowed"] = bool(adaptive_allowed)
            _append_policy_trace(
                meta,
                stage="belief_overlay",
                verdict="allow" if adaptive_allowed else "block",
                reason=str(overlay_reason),
                score=float(meta.get("conviction_score", 0.0)),
                changed_decision=False,
                details={
                    "conviction_band": str(meta.get("conviction_band") or ""),
                    "thesis_stage": str(meta.get("thesis_stage") or ""),
                    "portfolio_posture": str(meta.get("portfolio_posture") or ""),
                    "replacement_urgency": float(meta.get("replacement_urgency", 0.0)),
                    "entry_mode": str(meta.get("adaptive_entry_mode") or "standard"),
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
                        expected_edge_bps=float(_safe_float(meta.get("expected_edge_bps", meta.get("calibrated_ev_bps", 0.0)), 0.0)),
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

        meta["adaptive_rejection_reason"] = str(adaptive_reason)
        decision["metadata"] = meta
        if not position_open and not adaptive_allowed:
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
        meta["adaptive_portfolio_rank"] = int(candidate.allocator_rank or 0)
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
        adaptive_selected = bool(candidate.allocator_selected)
        meta["adaptive_selected"] = bool(adaptive_selected)
        meta["adaptive_rejection_reason"] = "none" if adaptive_selected else str(candidate.allocator_rejection_reason or "adaptive_ranked_out")
        _append_policy_trace(
            meta,
            stage="allocator",
            verdict="allow" if adaptive_selected else "block",
            reason=str("selected" if adaptive_selected else candidate.allocator_rejection_reason or "adaptive_ranked_out"),
            score=float(candidate.allocator_score),
            changed_decision=bool(not adaptive_selected),
            details={
                "allocator_rank": int(candidate.allocator_rank or 0),
                "sleeve_budget_target": int(candidate.sleeve_budget_target),
                "sleeve_budget_used": int(candidate.sleeve_budget_used),
                "replacement_target_pair": str(candidate.replacement_target_pair or ""),
            },
        )
        decision["metadata"] = meta
        if adaptive_selected:
            rejection_pair_map.pop(str(pair), None)
        else:
            reason = str(candidate.allocator_rejection_reason or "adaptive_ranked_out")
            rejection_reason_counts[reason] = int(rejection_reason_counts.get(reason, 0)) + 1
            rejection_pair_map[str(pair)] = reason

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
        "adaptive_policy_enabled": bool(adaptive_policy_enabled),
        "adaptive_candidate_count": int(len(candidates)),
        "adaptive_ranked_count": int(len(ranked_indices)),
        "adaptive_selected_count": int(
            sum(
                1
                for item in ranked_candidates
                if int(item.index) in ranked_indices
                and bool(decisions[int(item.index)]["metadata"].get("adaptive_selected", False))
            )
        ),
        "adaptive_remaining_slots": int(remaining_slots),
        "adaptive_max_new_entries": int(max_new_entries if use_ranking else remaining_slots),
        "adaptive_aggressive_fallback_count": int(aggressive_fallback_count),
        "adaptive_rejection_reason_counts": dict(sorted_rejection_counts),
        "adaptive_rejections_by_pair": dict(sorted(rejection_pair_map.items())),
        "adaptive_playbook_counts": dict(sorted(playbook_counts.items())),
        "adaptive_environment_counts": dict(sorted(environment_counts.items())),
        "adaptive_dominant_rejection_reason": next(iter(sorted_rejection_counts), ""),
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


def _ordered_pending_entries_for_submission(
    *,
    decisions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    adaptive_mode: bool,
) -> list[dict[str, Any]]:
    if not adaptive_mode:
        return list(pending_entries)

    def _priority(indexed_item: tuple[int, dict[str, Any]]) -> tuple[int, int, float, int]:
        original_index, item = indexed_item
        decision_index = int(item.get("index", -1))
        meta = (
            dict(decisions[decision_index].get("metadata") or {})
            if 0 <= decision_index < len(decisions)
            else {}
        )
        allocator_rank = int(_safe_float(meta.get("allocator_rank"), 0.0))
        return (
            0 if bool(meta.get("canonical_entry_ready", False)) else 1,
            allocator_rank if allocator_rank > 0 else 1_000_000,
            -float(_safe_float(meta.get("allocator_score"), 0.0)),
            int(original_index),
        )

    return [
        item
        for _, item in sorted(
            enumerate(pending_entries),
            key=_priority,
        )
    ]


# AGENT HANDSHAKE: Final entry submission is the last admission gate before commands hit the broker queue.
def _finalize_entry_submissions(
    *,
    decisions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    svc: Any,
    last_action_key: dict[str, str],
    settings: Any,
    runtime_state: dict[str, Any] | None = None,
    rl_portfolio_proposal: dict[str, Any] | None = None,
    adaptive_pending_entry_registry: dict[str, dict[str, Any]] | None = None,
    current_equity: float = 0.0,
    sleeve_health_snapshots: dict[str, Any] | None = None,
    enforce_sleeve_governance: bool = False,
) -> dict[str, Any]:
    adaptive_mode = bool(getattr(settings, "adaptive_execution_enabled", False))
    sleeve_governance_enabled = bool(adaptive_mode and enforce_sleeve_governance)
    paper_mode = _paper_mode_enabled(settings)
    live_mode = _live_mode_enabled(settings)
    live_runtime = _orchestration_live_runtime_state(runtime_state)
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
    rl_checkpoint_identity_failure = bool(
        proposal_bundle.get("checkpoint_identity_failure", False)
    )
    rl_bundle_source = str(proposal_bundle.get("source") or ("rl_checkpoint" if rl_checkpoint_loaded else "supervised_fallback"))
    rl_supervised_fallback_required = bool(getattr(settings, "rl_supervised_fallback_required", True))
    approved = 0
    blocked = 0
    submitted = 0
    accepted = 0
    duplicate = 0
    rl_routed_entry_count = 0
    rl_blocked_entry_count = 0
    rl_fallback_entry_count = 0
    rl_scaled_entry_count = 0
    live_entry_registry = adaptive_pending_entry_registry if adaptive_pending_entry_registry is not None else {}
    submitted_live_entry_pairs: list[str] = []
    submitted_live_entry_count = 0
    live_governed_eligible_count = 0
    live_governed_submitted_count = 0
    live_governed_blocked_count = 0
    live_baseline_fallback_count = 0
    live_fallback_reason_counts: dict[str, int] = {}
    sleeve_governance_advisory_count = 0
    entry_evidence_events: list[dict[str, Any]] = []

    ordered_pending_entries = _ordered_pending_entries_for_submission(
        decisions=decisions,
        pending_entries=pending_entries,
        adaptive_mode=adaptive_mode,
    )
    for item in ordered_pending_entries:
        index = int(item.get("index", -1))
        if index < 0 or index >= len(decisions):
            continue
        decision = decisions[index]
        meta = dict(decision.get("metadata", {}) or {})
        pair_key = str(item.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
        strict_ready = bool(meta.get("strict_entry_ready", meta.get("entry_ready", False)))
        strict_reasons = list(meta.get("strict_entry_blocking_reasons", meta.get("entry_blocking_reasons", [])) or [])
        adaptive_ready = bool(meta.get("adaptive_selected", False))
        adaptive_reason = str(meta.get("adaptive_rejection_reason") or "").strip()
        expected_sleeve = str(meta.get("adaptive_sleeve") or playbook_to_sleeve(meta.get("adaptive_playbook") or "")).strip()
        sleeve_block_reason = ""
        if sleeve_governance_enabled:
            sleeve_block_reason = sleeve_entry_block_reason(
                snapshot=dict(sleeve_health_snapshots or {}).get(expected_sleeve),
                expected_sleeve=expected_sleeve,
            )
        adaptive_hard_reason = (
            adaptive_reason
            if adaptive_mode and adaptive_reason in _ADAPTIVE_HARD_ENTRY_BLOCK_REASONS
            else ""
        )
        adaptive_hard_block = bool(adaptive_hard_reason)
        meta["sleeve_governance_enforced"] = bool(sleeve_governance_enabled)
        meta["sleeve_governance_entry_block_reason"] = str(sleeve_block_reason)
        meta["sleeve_governance_advisory"] = bool(sleeve_block_reason)
        if sleeve_block_reason:
            sleeve_governance_advisory_count += 1
        if "canonical_entry_ready" in meta:
            canonical_ready = bool(meta.get("canonical_entry_ready", False))
            canonical_reasons = list(meta.get("canonical_entry_blocking_reasons", []) or [])
            baseline_ready = bool(canonical_ready and not adaptive_hard_block)
            if adaptive_hard_reason:
                baseline_reason = str(adaptive_hard_reason)
            else:
                baseline_reason = str(
                    canonical_reasons[0]
                    if canonical_reasons
                    else meta.get("canonical_entry_rejection_reason")
                    or "entry_blocked"
                )
        else:
            # Compatibility for isolated callers that predate the canonical
            # post-adaptive resolver.  The real runtime always takes the
            # branch above, making this finalizer a monotonic veto-only stage.
            if adaptive_mode:
                baseline_ready = bool((adaptive_ready or strict_ready) and not adaptive_hard_block)
            else:
                baseline_ready = bool(strict_ready and not adaptive_hard_block)
            if adaptive_hard_reason:
                baseline_reason = str(adaptive_hard_reason)
            elif adaptive_mode:
                baseline_reason = str(adaptive_reason or "adaptive_execution_blocked")
            else:
                baseline_reason = str(
                    strict_reasons[0]
                    if strict_reasons
                    else meta.get("strict_rejection_reason") or "entry_blocked"
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
        proposal_metadata = dict(proposal_payload.get("metadata") or {})
        proposal_action_metadata = dict(proposal_action.get("metadata") or {})
        proposal_entry_supported = proposal_action_metadata.get("entry_supported", proposal_metadata.get("entry_supported"))
        rl_entry_supported = bool(proposal_entry_supported) if proposal_entry_supported is not None else None
        rl_supports_entry = (
            bool(proposal_payload)
            and bool(rl_checkpoint_pair)
            and not bool(rl_close_position)
            and float(rl_target_abs) >= 0.05
            and str(rl_requested_side) == str(decision.get("side") or meta.get("side") or "").upper()
        )
        if rl_entry_supported is not None:
            rl_supports_entry = bool(rl_supports_entry) and bool(rl_entry_supported)
        rl_router_reason = "supervised_legacy"
        if (
            strategy_engine_mode in {"rl_primary", "hybrid_candidate"}
            and rl_checkpoint_identity_failure
        ):
            rl_fallback_used = False
            rl_router_reason = str(
                rl_fallback_reason or "checkpoint_integrity_or_identity_invalid"
            )
            actual_ready = False
            actual_reason = rl_router_reason
            actual_reasons = [actual_reason]
        elif strategy_engine_mode == "hybrid_candidate":
            if bool(proposal_payload) and bool(rl_checkpoint_pair):
                if bool(rl_supports_entry):
                    rl_router_reason = "rl_candidate_confirmed"
                    rl_routed_entry_count += 1
                else:
                    rl_fallback_used = True
                    rl_fallback_reason = "hybrid_candidate_supervised_fallback"
                    rl_router_reason = str(rl_fallback_reason)
                    rl_fallback_entry_count += 1
                    actual_ready = bool(baseline_ready)
                    actual_reason = "none" if actual_ready else str(baseline_reason)
                    actual_reasons = [] if actual_ready else [actual_reason]
            else:
                rl_fallback_used = True
                rl_router_reason = str(rl_fallback_reason or "hybrid_candidate_supervised_fallback")
                rl_fallback_entry_count += 1
                actual_ready = bool(baseline_ready)
                actual_reason = "none" if actual_ready else str(baseline_reason)
                actual_reasons = [] if actual_ready else [actual_reason]
        elif strategy_engine_mode == "rl_primary":
            if bool(proposal_payload) and bool(rl_checkpoint_pair):
                if bool(rl_supports_entry):
                    rl_router_reason = "rl_primary_confirmed"
                    rl_routed_entry_count += 1
                    actual_ready = bool(baseline_ready)
                    actual_reason = "none" if actual_ready else str(baseline_reason)
                    actual_reasons = [] if actual_ready else [actual_reason]
                else:
                    rl_fallback_used = True
                    rl_fallback_reason = "rl_primary_supervised_fallback"
                    rl_router_reason = str(rl_fallback_reason)
                    rl_fallback_entry_count += 1
                    actual_ready = bool(baseline_ready)
                    actual_reason = "none" if actual_ready else str(baseline_reason)
                    actual_reasons = [] if actual_ready else [actual_reason]
            else:
                rl_fallback_used = True
                rl_router_reason = str(rl_fallback_reason or "rl_primary_supervised_fallback")
                rl_fallback_entry_count += 1
                actual_ready = bool(baseline_ready)
                actual_reason = "none" if actual_ready else str(baseline_reason)
                actual_reasons = [] if actual_ready else [actual_reason]
        rl_router_blocked = bool(
            strategy_engine_mode in {"rl_primary", "hybrid_candidate"}
            and not bool(actual_ready)
            and not bool(rl_supports_entry)
        )

        orch = dict(item.get("orchestration") or {})
        legacy_entry_compat = "canonical_entry_ready" not in meta
        risk_approved_order = dict(
            item.get("risk_approved_order")
            or meta.get("risk_approved_order")
            or item.get("approved_order")
            or meta.get("approved_order")
            or (item.get("payload") if legacy_entry_compat else {})
            or {}
        )
        approved_order = dict(item.get("approved_order") or risk_approved_order)
        baseline_payload = (
            _payload_from_approved_order(
                order=risk_approved_order,
                pair=str(item["pair"]),
                ts_value=str(item["ts_value"]),
                action_tag="entry",
            )
            if risk_approved_order
            else {}
        )
        paper_payload: dict[str, Any] = {}
        live_payload: dict[str, Any] = {}
        command_source = "baseline"
        baseline_fallback = False
        fallback_reason = ""
        if paper_mode and bool(actual_ready):
            paper_payload, paper_reason = _paper_governed_command_payload(
                decision=decision,
                orchestration=orch,
                pair=pair_key,
                ts_value=str(item.get("ts_value") or ""),
                default_payload=baseline_payload,
                default_action_tag="entry",
                settings=settings,
            )
            if paper_reason:
                actual_ready = False
                actual_reason = str(paper_reason)
                actual_reasons = [actual_reason]
            else:
                actual_ready = True
                actual_reason = "none"
                actual_reasons = []
                command_source = "governed_paper"
        elif live_mode and bool(actual_ready):
            live_payload, live_reason = _live_governed_command_payload(
                decision=decision,
                orchestration=orch,
                pair=pair_key,
                ts_value=str(item.get("ts_value") or ""),
                default_payload=baseline_payload,
                default_action_tag="entry",
                settings=settings,
                runtime_state=runtime_state,
            )
            if live_reason:
                fallback_reason = str(live_reason)
                live_governed_blocked_count += 1
                live_fallback_reason_counts[fallback_reason] = int(live_fallback_reason_counts.get(fallback_reason, 0)) + 1
                actual_ready = False
                actual_reason = str(fallback_reason or baseline_reason)
                actual_reasons = [actual_reason]
                command_source = "governed_live_blocked"
            else:
                actual_ready = True
                actual_reason = "none"
                actual_reasons = []
                command_source = "governed_live"
                live_governed_eligible_count += 1
        if live_mode and baseline_fallback:
            live_baseline_fallback_count += 1
            if str(fallback_reason).strip():
                live_fallback_reason_counts[str(fallback_reason)] = int(live_fallback_reason_counts.get(str(fallback_reason), 0)) + 1
        if strategy_engine_mode in {"rl_primary", "hybrid_candidate"} and not bool(actual_ready):
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
        meta["rl_checkpoint_identity_failure"] = bool(
            rl_checkpoint_identity_failure
        )
        meta["rl_target_position"] = float(rl_target_position)
        meta["rl_proposal_strength"] = float(rl_target_abs)
        meta["rl_requested_side"] = str(rl_requested_side)
        meta["rl_supports_entry"] = bool(rl_supports_entry)
        meta["rl_supervised_fallback_used"] = bool(rl_fallback_used)
        meta["rl_fallback_reason"] = str(rl_fallback_reason if rl_fallback_used else "")
        meta["rl_router_reason"] = str(rl_router_reason)
        meta["strategy_engine_mode"] = str(strategy_engine_mode)
        meta["orchestration_live_mode"] = bool(live_mode)
        meta["orchestration_live_runtime_enabled"] = bool(live_runtime.get("runtime_enabled", True))
        meta["orchestration_live_command_source"] = str(command_source)
        meta["orchestration_live_fallback_reason"] = str(fallback_reason)
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
                approved += 1
                entry_evidence_event: dict[str, Any] | None = None
                if live_mode:
                    entry_evidence_event = {
                        "pair": str(pair_key).upper(),
                        "action_key": str(item["action_key"]),
                        "approved": True,
                        "submitted": False,
                        "accepted": False,
                        "observed_at": float(time.time()),
                    }
                    entry_evidence_events.append(entry_evidence_event)
                if not paper_mode and not live_mode:
                    actual_ready = False
                    actual_reason = "agent_mode_advisory_only"
                    blocked += 1
                    enqueue_out = {
                        "status": "skipped",
                        "reason": actual_reason,
                        "action": "entry",
                        "command_source": "advisory_only",
                        "agent_mode": _normalize_agent_mode(getattr(settings, "agent_mode", "off")),
                    }
                    meta["execution_entry_ready"] = False
                    meta["execution_blocking_reasons"] = [actual_reason]
                    meta["execution_rejection_reason"] = actual_reason
                    meta["entry_ready"] = False
                    meta["entry_blocking_reasons"] = [actual_reason]
                    meta["rejection_reason"] = actual_reason
                    decision["execution_ready"] = False
                    decision["reasons"] = [actual_reason]
                    meta["enqueue"] = enqueue_out
                    meta = _update_orchestration_shadow_command_flow(
                        meta=meta,
                        orchestration=orch,
                        enqueue_out=enqueue_out,
                    )
                    decision["metadata"] = meta
                    continue
                payload = dict(paper_payload or live_payload or {})
                if not paper_mode and not live_payload:
                    payload = dict(baseline_payload or {})
                if payload and bool(rl_supports_entry) and strategy_engine_mode in {"hybrid_candidate", "rl_primary"}:
                    original_lots = float(_safe_float(payload.get("lots"), 0.0))
                    max_lots = float(_safe_float(getattr(settings, "max_order_lots", 0.0), 0.0))
                    min_lot = max(0.0, _safe_float(getattr(settings, "min_order_lots", 0.01), 0.01))
                    lot_step = max(1e-9, _safe_float(getattr(settings, "order_lot_step", 0.01), 0.01))
                    scaled_lots_raw = float(original_lots) * float(min(1.0, rl_target_abs))
                    rl_min_lot_underflowed = False
                    if original_lots > 0.0 and scaled_lots_raw + 1e-9 < min_lot:
                        rl_fallback_used = True
                        rl_fallback_reason = "rl_target_below_min_lot"
                        rl_fallback_entry_count += 1
                        rl_min_lot_underflowed = True
                        meta["rl_fallback_reason"] = str(rl_fallback_reason)
                        meta["rl_supervised_fallback_used"] = True
                        payload["lots"] = float(original_lots)
                        approved_order["lots"] = float(original_lots)
                        meta["approved_order"] = dict(approved_order)
                        item["approved_order"] = dict(approved_order)
                    if original_lots > 0.0 and rl_target_abs < 0.999 and not rl_min_lot_underflowed:
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
                final_payload_reason = _validate_final_entry_payload_against_risk_approval(
                    payload=payload,
                    risk_approved_payload=baseline_payload,
                )
                if final_payload_reason:
                    payload = {}
                if not payload:
                    actual_ready = False
                    if final_payload_reason:
                        actual_reason = str(final_payload_reason)
                    elif paper_mode:
                        actual_reason = "paper_missing_command_preview"
                    elif live_mode and str(fallback_reason).strip():
                        actual_reason = str(fallback_reason)
                    else:
                        actual_reason = "risk_kernel_missing_order"
                    blocked += 1
                    enqueue_out = {
                        "status": "skipped",
                        "reason": actual_reason,
                        "action": "entry",
                        "command_source": str(command_source),
                        "baseline_fallback": bool(baseline_fallback),
                        "fallback_reason": str(fallback_reason),
                    }
                    if paper_mode:
                        _paper_mode_rollback(svc=svc, reason=str(actual_reason))
                    meta["execution_entry_ready"] = False
                    meta["execution_blocking_reasons"] = [actual_reason]
                    meta["execution_rejection_reason"] = actual_reason
                    meta["entry_ready"] = False
                    meta["entry_blocking_reasons"] = [actual_reason]
                    meta["rejection_reason"] = actual_reason
                    decision["execution_ready"] = False
                    decision["reasons"] = [actual_reason]
                    meta["enqueue"] = enqueue_out
                    meta = _update_orchestration_shadow_command_flow(
                        meta=meta,
                        orchestration=orch,
                        enqueue_out=enqueue_out,
                    )
                    decision["metadata"] = meta
                    continue
                live_authority = (
                    dict(
                        dict(dict(runtime_state or {}).get("runtime_diag") or {}).get(
                            "orchestration_live"
                        )
                        or {}
                    )
                    if live_mode
                    else None
                )
                payload = _stamp_orchestration_payload(
                    payload=payload,
                    orchestration=orch,
                    live_authority=live_authority,
                    release_authority=dict(
                        dict(runtime_state or {}).get("release_authority") or {}
                    )
                    if live_mode
                    else None,
                    sleeve=str(
                        meta.get("adaptive_sleeve")
                        or playbook_to_sleeve(
                            meta.get("adaptive_playbook") or ""
                        )
                        or ""
                    ),
                )
                approved_submit = getattr(svc, "submit_approved_command", None)
                if live_mode and callable(approved_submit):
                    from fxstack.runtime.service import FinalEntryApproval

                    governed_decision = dict(orch.get("governed_decision") or {})
                    release_state = dict(
                        dict(runtime_state or {}).get("release_authority") or {}
                    )
                    release_request = dict(release_state.get("request") or {})
                    release_ack = dict(release_state.get("ack") or {})
                    approved_sleeve = str(
                        meta.get("adaptive_sleeve")
                        or playbook_to_sleeve(
                            meta.get("adaptive_playbook") or ""
                        )
                        or ""
                    ).strip().lower()
                    final_approval = FinalEntryApproval(
                        pair=str(pair_key),
                        side=str(decision.get("side") or meta.get("side") or ""),
                        risk_approved_payload=dict(baseline_payload),
                        canonical_ready=bool(meta.get("canonical_entry_ready", False)),
                        governed_allowed=bool(governed_decision.get("allowed", False)),
                        rollout_active=bool(meta.get("rollout_active", False)),
                        rollout_mode=str(meta.get("rollout_mode") or ""),
                        rollout_pair_allowlisted=bool(
                            meta.get("rollout_pair_allowlisted", False)
                        ),
                        correlation_id=str(orch.get("correlation_id") or ""),
                        trace_id=str(orch.get("trace_id") or ""),
                        broker_account_mode=str(
                            dict(runtime_state or {}).get("broker_account_mode") or ""
                        ),
                        broker_account_scope=str(
                            dict(runtime_state or {}).get("broker_account_scope") or ""
                        ),
                        authority_revision=_safe_authority_revision(
                            dict(live_authority or {}).get(
                                "authority_revision"
                            )
                        ),
                        release_generation_id=str(
                            release_request.get("generation_id") or ""
                        ),
                        release_request_sha256=str(
                            release_request.get("request_sha256") or ""
                        ),
                        model_identity_sha256=str(
                            release_request.get("model_identity_sha256") or ""
                        ),
                        manifest_file_sha256=str(
                            release_request.get("manifest_file_sha256") or ""
                        ),
                        runtime_boot_id=str(
                            release_ack.get("runtime_boot_id") or ""
                        ),
                        sleeve=approved_sleeve,
                    )
                    out, _ = approved_submit(
                        payload,
                        approval=final_approval,
                        proto="v2",
                    )
                else:
                    out, _ = svc.submit_command(payload, proto="v2")
                enqueue_out = dict(out)
                enqueue_out.setdefault("command_source", str(command_source))
                enqueue_out.setdefault("baseline_fallback", bool(baseline_fallback))
                enqueue_out.setdefault("fallback_reason", str(fallback_reason))
                enqueue_status = str(enqueue_out.get("status") or "").strip().lower()
                submission_accepted = _submission_is_accepted(enqueue_out)
                submitted += 1
                if entry_evidence_event is not None:
                    entry_evidence_event["submitted"] = True
                    entry_evidence_event["accepted"] = bool(submission_accepted)
                if submission_accepted:
                    accepted += 1
                    if live_mode and command_source == "governed_live":
                        live_governed_submitted_count += 1
                    if _submission_has_active_queue_record(enqueue_out):
                        last_action_key[str(item["pair"])] = str(item["action_key"])
                    pair_key = str(item["pair"]).upper()
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
                        "entry_mode": str(meta.get("adaptive_entry_mode") or "standard"),
                        "approved_lots": float(
                            _safe_float(
                                dict(item.get("risk_approved_order") or item.get("approved_order") or {}).get("lots"),
                                0.0,
                            )
                        ),
                        "initial_sl_price": float(
                            _safe_float(
                                dict(item.get("risk_approved_order") or item.get("approved_order") or {}).get("sl_price"),
                                0.0,
                            )
                        ),
                        "initial_tp_price": float(
                            _safe_float(
                                dict(item.get("risk_approved_order") or item.get("approved_order") or {}).get("tp_price"),
                                0.0,
                            )
                        ),
                        "partial_count": 0,
                        "last_partial_bar_index": None,
                    }
                    submitted_live_entry_pairs.append(pair_key)
                    submitted_live_entry_count += 1
                else:
                    actual_ready = False
                    actual_reason = enqueue_status or "submission_rejected"
                    actual_reasons = [actual_reason]
                    meta["execution_entry_ready"] = False
                    meta["execution_blocking_reasons"] = list(actual_reasons)
                    meta["execution_rejection_reason"] = str(actual_reason)
                    meta["entry_ready"] = False
                    meta["entry_blocking_reasons"] = list(actual_reasons)
                    meta["rejection_reason"] = str(actual_reason)
                    decision["execution_ready"] = False
                    decision["reasons"] = list(actual_reasons)
                meta = _update_orchestration_shadow_command_flow(
                    meta=meta,
                    orchestration=orch,
                    enqueue_out=enqueue_out,
                )
            else:
                enqueue_out = {
                    "status": "duplicate_action_skip",
                    "ts": str(item["ts_value"]),
                    "action": "entry",
                    "command_source": str(command_source),
                    "baseline_fallback": bool(baseline_fallback),
                    "fallback_reason": str(fallback_reason),
                }
                duplicate += 1
                actual_ready = False
                actual_reason = "duplicate_action_skip"
                actual_reasons = [actual_reason]
                meta["execution_entry_ready"] = False
                meta["execution_blocking_reasons"] = list(actual_reasons)
                meta["execution_rejection_reason"] = str(actual_reason)
                meta["entry_ready"] = False
                meta["entry_blocking_reasons"] = list(actual_reasons)
                meta["rejection_reason"] = str(actual_reason)
                decision["execution_ready"] = False
                decision["reasons"] = list(actual_reasons)
                meta = _update_orchestration_shadow_command_flow(
                    meta=meta,
                    orchestration=orch,
                    enqueue_out=enqueue_out,
                )
        else:
            blocked += 1
            meta["lifecycle_action"] = "hold"
            meta["lifecycle_reason"] = str(actual_reason)
            if paper_mode and str(actual_reason).startswith("paper_"):
                _paper_mode_rollback(svc=svc, reason=str(actual_reason))
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
                "command_source": str(command_source),
                "baseline_fallback": bool(baseline_fallback),
                "fallback_reason": str(fallback_reason),
            }
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
        meta["enqueue"] = enqueue_out
        decision["metadata"] = meta

    if live_mode and live_baseline_fallback_count > 0:
        record_event = getattr(svc, "record_governance_event", None)
        if callable(record_event):
            record_event(
                event_type="orchestration_live_fallback",
                reason="governed_live_cycle_fallback",
                payload={
                    "fallback_count": int(live_baseline_fallback_count),
                    "fallback_reason_counts": dict(sorted(live_fallback_reason_counts.items())),
                    "runtime_enabled": bool(live_runtime.get("runtime_enabled", True)),
                    "queue_kill_active": bool(live_runtime.get("queue_kill_active", False)),
                },
            )

    return {
        "execution_mode": str(execution_mode),
        "adaptive_execution_enabled": bool(adaptive_mode),
        "pending_entry_count": int(len(pending_entries)),
        "approved_entry_count": int(approved),
        "blocked_entry_count": int(blocked),
        "submitted_entry_count": int(submitted),
        "accepted_entry_count": int(accepted),
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
        "live_governed_runtime_enabled": bool(live_runtime.get("runtime_enabled", True)),
        "live_queue_kill_active": bool(live_runtime.get("queue_kill_active", False)),
        "live_governed_eligible_count": int(live_governed_eligible_count),
        "live_governed_submitted_count": int(live_governed_submitted_count),
        "live_governed_blocked_count": int(live_governed_blocked_count),
        "live_baseline_fallback_count": int(live_baseline_fallback_count),
        "live_fallback_reason_counts": dict(sorted(live_fallback_reason_counts.items())),
        "entry_evidence_events": list(entry_evidence_events),
        "sleeve_governance_enforced": bool(sleeve_governance_enabled),
        "sleeve_governance_blocked_count": 0,
        "sleeve_governance_advisory_count": int(sleeve_governance_advisory_count),
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


# AGENT STATE: Adaptive registries reconcile runtime decisions with live bridge
# positions so cooldowns and replacement logic persist across bars. The whole
# block now lives in fxstack.runtime.managed_state; each name is re-bound under
# its original underscored alias so existing call sites keep working.
from fxstack.runtime.managed_state import (  # noqa: E402
    append_tracker_command_id as _append_tracker_command_id,
    clear_pending_partial_state as _clear_pending_partial_state,
    hydrate_exit_command_ledger_from_commands as _hydrate_exit_command_ledger_from_commands,
    hydrate_partial_close_tracker_from_commands as _hydrate_partial_close_tracker_from_commands,
    managed_state_json_value as _managed_state_json_value,
    reconcile_exit_command_ledger as _reconcile_exit_command_ledger,
    reconcile_partial_close_tracker as _reconcile_partial_close_tracker,
    restore_managed_position_state as _restore_managed_position_state,
    seed_adaptive_position_state as _seed_adaptive_position_state,
    serialize_managed_position_state as _serialize_managed_position_state,
    sync_adaptive_position_registry as _sync_adaptive_position_registry,
)



# AGENT HANDSHAKE: Position actions submit exits/partials before entries so freed slots are visible to the same cycle's entry finalizer.
def _submit_position_actions(
    *,
    decisions: list[dict[str, Any]],
    pending_position_actions: list[dict[str, Any]],
    svc: Any,
    settings: Any | None = None,
    runtime_state: dict[str, Any] | None = None,
    last_action_key: dict[str, str],
    partial_close_tracker: dict[str, dict[str, Any]],
    adaptive_position_registry: dict[str, SimpleNamespace],
    adaptive_recent_exit_registry: dict[str, dict[str, Any]],
    pair_bar_index: dict[str, int],
    loop_ts: float,
    campaign_registry: dict[str, CampaignRegistryEntry] | None = None,
    campaign_transition_counts: dict[str, int] | None = None,
    campaign_config: Any | None = None,
    exit_command_ledger: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    submitted = 0
    duplicate = 0
    partial_submitted = 0
    exit_submitted = 0
    adjust_submitted = 0
    paper_mode = _paper_mode_enabled(settings)
    live_mode = _live_mode_enabled(settings)

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
        orch = dict(item.get("orchestration") or {})
        action_tag = _lifecycle_action_tag(lifecycle_action)

        enqueue_out: dict[str, Any] = {"status": "skipped", "ts": ts_value, "action": lifecycle_action}
        if lifecycle_action not in {"exit", "tighten_stop", "partial_tp"}:
            meta["enqueue"] = enqueue_out
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
            decision["metadata"] = meta
            continue

        if lifecycle_action == "exit" and any(
            not bool(dict(ledger_state or {}).get("resolved", False))
            and (
                str(dict(ledger_state or {}).get("position_signature") or "")
                == position_signature
                or (
                    not position_signature
                    and str(dict(ledger_state or {}).get("pair") or "").upper()
                    == pair
                )
            )
            for ledger_state in dict(exit_command_ledger or {}).values()
        ):
            enqueue_out = {
                "status": "skipped",
                "ts": ts_value,
                "action": lifecycle_action,
                "reason": "exit_ack_pending",
            }
            meta["enqueue"] = enqueue_out
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
            decision["metadata"] = meta
            continue

        action_key = f"{action_tag}:{ts_value}"
        if last_action_key.get(pair) == action_key:
            enqueue_out = {"status": "duplicate_action_skip", "ts": ts_value, "action": lifecycle_action}
            duplicate += 1
            meta["enqueue"] = enqueue_out
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
            decision["metadata"] = meta
            continue

        cmd_id = _build_command_id(pair=pair, ts_value=ts_value, action_tag=action_tag)
        approved_order = dict(item.get("approved_order") or meta.get("approved_order") or {})
        if not approved_order or (live_mode and not bool(item.get("final_risk_approved", False))):
            approval_reason = (
                "final_lifecycle_risk_approval_missing"
                if live_mode
                else "risk_kernel_missing_order"
            )
            enqueue_out = {
                "status": "skipped",
                "ts": ts_value,
                "action": lifecycle_action,
                "reason": str(approval_reason),
            }
            meta["enqueue"] = enqueue_out
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
            decision["metadata"] = meta
            continue
        lifecycle_payload_reason = _validate_final_lifecycle_payload_against_risk_approval(
            lifecycle_action=lifecycle_action,
            action_item=item,
            approved_order=approved_order,
        )
        if lifecycle_payload_reason:
            enqueue_out = {
                "status": "skipped",
                "ts": ts_value,
                "action": lifecycle_action,
                "reason": str(lifecycle_payload_reason),
            }
            meta["enqueue"] = enqueue_out
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
            decision["metadata"] = meta
            continue
        payload = _payload_from_approved_order(
            order=approved_order,
            pair=pair,
            ts_value=ts_value,
            action_tag=action_tag,
        )
        governance_reason = ""
        if paper_mode:
            payload, governance_reason = _paper_governed_command_payload(
                decision=decision,
                orchestration=orch,
                pair=pair,
                ts_value=ts_value,
                default_payload=payload,
                default_action_tag=action_tag,
                settings=settings,
            )
        elif live_mode:
            payload, governance_reason = _live_governed_command_payload(
                decision=decision,
                orchestration=orch,
                pair=pair,
                ts_value=ts_value,
                default_payload=payload,
                default_action_tag=action_tag,
                settings=settings,
                runtime_state=runtime_state,
            )
        if governance_reason:
            enqueue_out = {"status": "skipped", "ts": ts_value, "action": lifecycle_action, "reason": str(governance_reason)}
            meta["enqueue"] = enqueue_out
            if paper_mode and (
                str(governance_reason).startswith("paper_approval")
                or str(governance_reason).startswith("paper_missing_")
                or str(governance_reason).startswith("paper_governor_")
            ):
                _paper_mode_rollback(svc=svc, reason=str(governance_reason))
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
            decision["metadata"] = meta
            continue
        if not paper_mode and not live_mode:
            enqueue_out = {
                "status": "skipped",
                "ts": ts_value,
                "action": lifecycle_action,
                "reason": "agent_mode_advisory_only",
                "command_source": "advisory_only",
                "agent_mode": _normalize_agent_mode(getattr(settings, "agent_mode", "off")),
            }
            meta["enqueue"] = enqueue_out
            meta = _update_orchestration_shadow_command_flow(
                meta=meta,
                orchestration=orch,
                enqueue_out=enqueue_out,
            )
            decision["metadata"] = meta
            continue
        payload["management_context"] = {
            "schema": "fxstack_lifecycle_command_context_v1",
            "lifecycle_action": str(lifecycle_action),
            "lifecycle_reason": str(lifecycle_reason),
            "position_signature": str(position_signature),
            "position_signatures": [
                str(_position_signature(dict(raw_position or {})))
                for raw_position in list(
                    dict(runtime_state or {}).get("positions", []) or []
                )
                if str(dict(raw_position or {}).get("symbol") or "")
                .strip()
                .upper()
                == pair
            ]
            or ([str(position_signature)] if str(position_signature).strip() else []),
            "pair": str(pair),
            "position_side": str(
                item.get("position_side") or meta.get("position_side") or ""
            ),
            "lots_open": float(_safe_float(item.get("lots_open"), 0.0)),
            "close_lots": float(_safe_float(close_lots, 0.0)),
            "bar_index": int(pair_bar_index.get(pair, -1)),
            "submitted_ts": float(loop_ts),
            "decision_ts": str(ts_value),
            "playbook": str(
                item.get("playbook")
                or meta.get("adaptive_playbook")
                or PLAYBOOK_TREND_PULLBACK
            ),
            "sleeve": str(
                meta.get("adaptive_sleeve")
                or playbook_to_sleeve(meta.get("adaptive_playbook") or "")
                or ""
            ),
            "thesis_id": str(item.get("thesis_id") or meta.get("thesis_id") or ""),
            "campaign_state": str(
                item.get("campaign_state") or meta.get("campaign_state") or ""
            ),
            "playbook_score": float(
                _safe_float(meta.get("adaptive_playbook_score"), 0.0)
            ),
            "location_score": float(
                _safe_float(meta.get("adaptive_location_score"), 0.0)
            ),
            "trigger_score": float(
                _safe_float(meta.get("adaptive_trigger_score"), 0.0)
            ),
            "macro_coherence_score": float(
                _safe_float(meta.get("adaptive_macro_coherence_score"), 0.0)
            ),
            "hostility_score": float(
                _safe_float(meta.get("adaptive_hostility_score"), 0.0)
            ),
            "extension_penalty_score": float(
                _safe_float(meta.get("extension_penalty_score"), 0.0)
            ),
            "environment_state": str(
                meta.get("adaptive_environment_state") or ""
            ),
            "unrealized_pnl_usd": float(
                _safe_float(item.get("unrealized_pnl_usd"), 0.0)
            ),
            "age_bars": float(_safe_float(item.get("age_bars"), 0.0)),
            "session_bucket": str(meta.get("session_bucket") or ""),
            "partial_count": int(
                _safe_float(
                    getattr(adaptive_position_registry.get(pair), "partial_count", 0),
                    0.0,
                )
            ),
        }
        payload = _stamp_orchestration_payload(
            payload=payload,
            orchestration=orch,
            release_authority=dict(
                dict(runtime_state or {}).get("release_authority") or {}
            )
            if live_mode
            else None,
            sleeve=str(
                meta.get("adaptive_sleeve")
                or playbook_to_sleeve(meta.get("adaptive_playbook") or "")
                or ""
            ),
        )
        out, _ = svc.submit_command(payload, proto="v2")
        enqueue_out = dict(out)
        submission_accepted = _submission_is_accepted(enqueue_out)
        submitted += 1
        if submission_accepted:
            if _submission_has_active_queue_record(enqueue_out):
                last_action_key[pair] = action_key

        if lifecycle_action == "partial_tp":
            partial_submitted += 1
            if position_signature and submission_accepted:
                partial_state = dict(partial_close_tracker.get(position_signature, {}) or {})
                registry_state = adaptive_position_registry.get(pair)
                pending_command_id = str(
                    enqueue_out.get("command_id")
                    or payload.get("command_id")
                    or cmd_id
                ).strip()
                partial_state["count"] = max(
                    0, int(_safe_float(partial_state.get("count"), 0.0))
                )
                partial_state["pending_command_id"] = pending_command_id
                partial_state["pending_pair"] = pair
                partial_state["pending_position_signature"] = position_signature
                partial_state["pending_open_lots"] = float(
                    _safe_float(
                        item.get("lots_open"),
                        getattr(registry_state, "current_lots", 0.0)
                        if registry_state is not None
                        else 0.0,
                    )
                )
                partial_state["pending_close_lots"] = float(
                    _safe_float(
                        close_lots,
                        approved_order.get("close_lots", approved_order.get("lots", 0.0)),
                    )
                )
                partial_state["pending_submitted_ts"] = float(loop_ts)
                partial_state["pending_bar_index"] = int(pair_bar_index.get(pair, -1))
                partial_state["_missing_position_cycles"] = 0
                partial_close_tracker[position_signature] = partial_state
        elif lifecycle_action == "exit":
            exit_submitted += 1
            if submission_accepted and exit_command_ledger is not None:
                pending_command_id = str(
                    enqueue_out.get("command_id")
                    or payload.get("command_id")
                    or cmd_id
                ).strip()
                exit_command_ledger[pending_command_id] = {
                    **dict(payload.get("management_context") or {}),
                    "command_id": pending_command_id,
                    "resolved": False,
                    "resolution": "pending_broker_ack",
                }
        elif lifecycle_action == "tighten_stop":
            adjust_submitted += 1

        meta["enqueue"] = enqueue_out
        meta["lifecycle_action"] = str(lifecycle_action)
        meta["lifecycle_reason"] = str(lifecycle_reason)
        meta = _update_orchestration_shadow_command_flow(
            meta=meta,
            orchestration=orch,
            enqueue_out=enqueue_out,
        )
        decision["metadata"] = meta

    return {
        "submitted_position_action_count": int(submitted),
        "duplicate_position_action_count": int(duplicate),
        "submitted_partial_close_count": int(partial_submitted),
        "pending_partial_ack_count": int(
            sum(
                1
                for tracker_state in partial_close_tracker.values()
                if str(dict(tracker_state or {}).get("pending_command_id") or "").strip()
            )
        ),
        "submitted_exit_count": int(exit_submitted),
        "pending_exit_ack_count": int(
            sum(
                1
                for ledger_state in dict(exit_command_ledger or {}).values()
                if not bool(dict(ledger_state or {}).get("resolved", False))
            )
        ),
        "submitted_adjust_count": int(adjust_submitted),
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
    existing = store.read_latest_row(
        provider=provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        tail_files=3,
    )
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

    raw_root = _raw_root_for_feature_root(store.root)
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


def _bootstrap_pair_features_from_local_snapshot(
    *,
    feature_store: ParquetStore,
    raw_store: ParquetStore,
    provider: str,
    pair: str,
    timeframe: str,
) -> tuple[bool, str]:
    existing = feature_store.read_latest_row(
        provider=provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        tail_files=3,
    )
    if not existing.empty:
        return False, "already_present"

    refreshed = _refresh_feature_tail(
        feature_store=feature_store,
        raw_store=raw_store,
        provider=provider,
        pair=pair,
        timeframe=timeframe,
    )
    if not bool(refreshed.get("ok")):
        return False, f"raw_snapshot_unavailable:{refreshed.get('reason')}"

    latest = feature_store.read_latest_row(
        provider=provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        tail_files=3,
    )
    if latest.empty:
        return False, f"raw_snapshot_refresh_failed:{refreshed.get('reason')}"
    return True, f"rows={int(refreshed.get('rows', 0) or 0)}"


def _refresh_pair_feature_tails_from_local_snapshot(
    *,
    feature_store: ParquetStore,
    raw_store: ParquetStore,
    provider: str,
    pair: str,
    svc: Any | None = None,
) -> dict[str, Any]:
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
    ok = any(bool(dict(diag or {}).get("ok")) for diag in feature_diag.values())
    return {
        "ok": bool(ok),
        "reason": "paper_local_snapshot" if ok else "raw_recent_empty",
        "provider": str(provider or ""),
        "feature_refresh": dict(feature_diag),
        "feature_push": dict(feature_push),
        "source": "local_snapshot",
    }


def _bootstrap_pair_features_for_timeframe(
    *,
    feature_store: ParquetStore,
    raw_store: ParquetStore,
    provider: str,
    pair: str,
    timeframe: str,
    all_pairs: list[str] | None,
    paper_mode: bool,
    feature_service_name: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    row = _latest_feature_row(
        store=feature_store,
        raw_store=raw_store,
        pair=pair,
        timeframe=timeframe,
        all_pairs=all_pairs,
        feature_service_name=feature_service_name,
    )
    diag: dict[str, Any] = {
        "attempted": False,
        "ok": bool(not row.empty),
        "detail": "already_present" if not row.empty else "",
        "source": "existing" if not row.empty else "",
    }
    if not row.empty:
        return row, diag

    if paper_mode:
        ok_local, detail_local = _bootstrap_pair_features_from_local_snapshot(
            feature_store=feature_store,
            raw_store=raw_store,
            provider=provider,
            pair=pair,
            timeframe=timeframe,
        )
        diag.update(
            {
                "raw_snapshot_attempted": True,
                "raw_snapshot_ok": bool(ok_local),
                "raw_snapshot_detail": str(detail_local),
            }
        )
        row = _latest_feature_row(
            store=feature_store,
            raw_store=raw_store,
            pair=pair,
            timeframe=timeframe,
            all_pairs=all_pairs,
            feature_service_name=feature_service_name,
        )
        if not row.empty:
            diag.update({"attempted": True, "ok": True, "detail": str(detail_local), "source": "raw_snapshot"})
            return row, diag

    ok_csv, detail_csv = _bootstrap_pair_features_from_csv(store=feature_store, pair=pair, timeframe=timeframe)
    diag.update(
        {
            "csv_attempted": True,
            "csv_ok": bool(ok_csv),
            "csv_detail": str(detail_csv),
            "attempted": True,
        }
    )
    row = _latest_feature_row(
        store=feature_store,
        raw_store=raw_store,
        pair=pair,
        timeframe=timeframe,
        all_pairs=all_pairs,
        feature_service_name=feature_service_name,
    )
    if not row.empty:
        diag.update({"ok": True, "detail": str(detail_csv), "source": "dukascopy_csv"})
    else:
        diag.update({"ok": False, "detail": str(detail_csv), "source": "missing"})
    return row, diag


def _release_pair_attestation(
    runtime_attestation: dict[str, Any],
    pair: str,
) -> dict[str, Any]:
    global_attestation = dict(runtime_attestation or {})
    pair_key = str(pair or "").strip().upper()
    pair_attestation = dict(
        dict(global_attestation.get("pairs") or {}).get(pair_key) or {}
    )
    return {**global_attestation, **pair_attestation}


def _build_runtime_release_attestation(
    *,
    settings: Any,
    runtime_boot_id: str,
    model_sets: dict[str, LoadedModelSet],
    activation_consistency: dict[str, Any],
) -> dict[str, Any]:
    """Bind release ACKs to the exact already-loaded startup image/model set."""

    build = load_signed_build_provenance()
    manifest_path = Path(settings.model_activation_manifest)
    if not manifest_path.is_absolute():
        manifest_path = Path(settings.project_root) / manifest_path
    manifest_path = manifest_path.resolve()
    pairs: dict[str, dict[str, Any]] = {}
    errors = list(build.get("errors") or [])
    for raw_pair, loaded in sorted(model_sets.items()):
        pair = str(raw_pair).strip().upper()
        pair_errors: list[str] = []
        try:
            identity = manifest_model_identity(
                manifest_path=manifest_path,
                pair=pair,
            )
        except (OSError, ValueError) as exc:
            identity = {}
            pair_errors.append(
                f"runtime_manifest_identity:{type(exc).__name__}"
            )
        if str(identity.get("model_set_id") or "") != str(
            loaded.model_set_id or ""
        ):
            pair_errors.append("loaded_model_set_identity_mismatch")
        if pair in {
            str(item).strip().upper()
            for item in list(
                activation_consistency.get("activation_mismatch_pairs") or []
            )
            if str(item).strip()
        }:
            pair_errors.append("activation_consistency_mismatch")
        pairs[pair] = {
            "pair": pair,
            "bundle_run_id": str(identity.get("bundle_run_id") or ""),
            "model_set_id": str(identity.get("model_set_id") or ""),
            "model_identity_sha256": str(
                identity.get("model_identity_sha256") or ""
            ),
            "artifact_set_sha256": str(
                identity.get("artifact_set_sha256") or ""
            ),
            "manifest_file_sha256": str(
                identity.get("manifest_file_sha256") or ""
            ),
            "loaded_registry_path": str(loaded.registry_path or ""),
            "valid": not pair_errors,
            "errors": pair_errors,
        }
        errors.extend(f"{pair}:{item}" for item in pair_errors)
    return {
        "schema_version": "fxstack_runtime_boot_attestation_v1",
        "runtime_boot_id": str(runtime_boot_id),
        "runtime_pid": int(os.getpid()),
        "attested_at": float(time.time()),
        "source_sha256": str(build.get("source_sha256") or ""),
        "package_merkle_sha256": str(
            build.get("measured_package_merkle_sha256")
            or build.get("package_merkle_sha256")
            or ""
        ),
        "git_commit": str(build.get("git_commit") or ""),
        "source_clean": build.get("source_clean") is True
        and build.get("valid") is True,
        "build_provenance_file_sha256": str(build.get("file_sha256") or ""),
        "config_sha256": runtime_config_sha256(settings),
        "execution_provider": str(
            settings.normalized_execution_provider or ""
        ).strip().lower(),
        "strategy_engine_mode": str(
            settings.strategy_engine_mode or ""
        ).strip().lower(),
        "manifest_path": str(manifest_path),
        "activation_consistency": dict(activation_consistency or {}),
        "pairs": pairs,
        "valid": build.get("valid") is True and not errors,
        "errors": list(dict.fromkeys(errors)),
    }


def _apply_production_operator_rollout(
    *,
    settings: Any,
    model_sets: dict[str, LoadedModelSet],
) -> None:
    """Make explicit production settings—not research evidence—the rollout owner."""

    live_enabled = bool(
        _live_mode_enabled(settings)
        and bool(getattr(settings, "live_armed", False))
    )
    pair_scope = {
        str(item).strip().upper()
        for item in list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        if str(item).strip()
    }
    budget_scale = max(
        0.0,
        min(
            1.0,
            _safe_float(
                getattr(settings, "capital_rollout_budget_scale_full_risk", 1.0),
                1.0,
            ),
        ),
    )
    for raw_pair, loaded in model_sets.items():
        pair = str(raw_pair).strip().upper()
        pair_active = bool(live_enabled and pair in pair_scope and budget_scale > 0.0)
        loaded.rollout_policy = {
            "configured": pair_active,
            "source": "production_operator_scope",
            "mode": "live" if pair_active else "off",
            "enabled": pair_active,
            "active": pair_active,
            "pair": pair,
            "pair_allowlisted": pair_active,
            "allowlisted_pairs": [pair] if pair_active else [],
            "budget_scale": budget_scale if pair_active else 0.0,
            "budget_reason": (
                "operator_armed_production_runtime"
                if pair_active
                else "production_live_scope_inactive"
            ),
            "max_pair_positions": max(
                0,
                int(getattr(settings, "max_pair_positions", 0) or 0),
            ),
            "max_total_positions": max(
                0,
                int(getattr(settings, "max_total_positions", 0) or 0),
            ),
            "max_gross_exposure": max(
                0.0,
                _safe_float(
                    getattr(settings, "risk_max_gross_exposure", 0.0),
                    0.0,
                ),
            ),
            "max_net_exposure": max(
                0.0,
                _safe_float(
                    getattr(settings, "risk_max_net_exposure", 0.0),
                    0.0,
                ),
            ),
        }


def _arm_production_runtime_authority(
    *,
    svc: Any,
    state: dict[str, Any],
    settings: Any,
    model_sets: dict[str, LoadedModelSet],
    runtime_boot_id: str,
) -> dict[str, Any]:
    """Arm the validated production runtime without granting research a veto."""

    if not _live_mode_enabled(settings):
        return {
            "status": "not_applicable",
            "valid": True,
            "binding": "production_runtime",
            "errors": [],
        }
    admission = _live_command_admission_diagnostics(
        settings=settings,
        model_sets=model_sets,
    )
    if not bool(admission.get("allowed", False)):
        return {
            "status": "blocked",
            "valid": False,
            "binding": "production_runtime",
            "errors": list(admission.get("blockers") or []),
        }
    runtime_diag = dict(state.get("runtime_diag") or {})
    current_live = dict(runtime_diag.get("orchestration_live") or {})
    pair_scope = sorted(
        {
            str(item).strip().upper()
            for item in list(getattr(settings, "agent_live_pair_allowlist", []) or [])
            if str(item).strip()
        }
    )
    sleeve_scope = sorted(
        {
            str(item).strip().lower()
            for item in list(getattr(settings, "agent_live_sleeve_allowlist", []) or [])
            if str(item).strip()
        }
    )
    intent_scope = sorted(
        {
            str(item).strip().lower()
            for item in list(getattr(settings, "agent_live_intent_allowlist", []) or [])
            if str(item).strip()
        }
    )
    budget_scale = max(
        0.0,
        min(
            1.0,
            _safe_float(
                getattr(settings, "capital_rollout_budget_scale_full_risk", 1.0),
                1.0,
            ),
        ),
    )
    bundle_ids = sorted(
        {
            str(loaded.model_set_id or "").strip()
            for pair, loaded in model_sets.items()
            if str(pair).strip().upper() in set(pair_scope)
            and str(loaded.model_set_id or "").strip()
        }
    )
    try:
        live = svc.patch_orchestration_live_state(
            updates={
                "enabled": True,
                "mode": "live",
                "runtime_enabled": True,
                "queue_kill_active": False,
                "queue_kill_reason": "",
                "queue_killed_at": 0.0,
                "active_pair_scope": pair_scope,
                "active_sleeve_scope": sleeve_scope,
                "active_intent_scope": intent_scope,
                "active_pair_scope_configured": True,
                "active_sleeve_scope_configured": True,
                "active_intent_scope_configured": True,
                "current_stage_index": 0,
                "current_stage_pct": 100,
                "budget_scale": budget_scale,
                "bundle_run_id": ",".join(bundle_ids),
                "release_status": "advisory_only",
                "signoff_records": [],
            },
            expected_live_authority=current_live,
            allow_reenable=True,
        )
        egress = svc.enable_production_execution_egress(
            runtime_boot_id=str(runtime_boot_id),
        )
    except Exception as exc:
        return {
            "status": "blocked",
            "valid": False,
            "binding": "production_runtime",
            "errors": [f"production_authority_arm_failed:{type(exc).__name__}:{exc}"],
        }
    return {
        "status": "active",
        "valid": True,
        "binding": "production_runtime",
        "authority_revision": int(live.get("authority_revision") or 0),
        "execution_egress": dict(egress or {}),
        "errors": [],
    }


def _apply_release_authority_rollout(
    *,
    model_sets: dict[str, LoadedModelSet],
    state: dict[str, Any],
    authority_valid: bool,
) -> None:
    authority = dict(state.get("release_authority") or {})
    request = dict(authority.get("request") or {})
    authorized = dict(request.get("authorized_execution") or {})
    authority_pair = str(request.get("pair") or "").strip().upper()
    live = dict(
        dict(state.get("runtime_diag") or {}).get("orchestration_live") or {}
    )
    active = bool(
        authority_valid
        and str(authority.get("status") or "").strip().lower() == "active"
        and state.get("execution_egress_enabled") is True
    )
    for raw_pair, loaded in model_sets.items():
        pair = str(raw_pair).strip().upper()
        pair_active = bool(active and pair == authority_pair)
        loaded.rollout_policy = {
            "configured": pair_active,
            "source": "release_authority",
            "mode": "canary" if pair_active else "off",
            "active": pair_active,
            "pair": pair,
            "pair_allowlisted": pair_active
            and [
                str(item).strip().upper()
                for item in list(authorized.get("pair_scope") or [])
                if str(item).strip()
            ]
            == [pair],
            "allowlisted_pairs": [pair] if pair_active else [],
            "budget_scale": (
                max(0.0, min(1.0, _safe_float(live.get("budget_scale"), 0.0)))
                if pair_active
                else 0.0
            ),
            "budget_reason": (
                "externally_witnessed_release_generation"
                if pair_active
                else "release_authority_inactive"
            ),
            "generation_id": str(request.get("generation_id") or ""),
            "request_sha256": str(request.get("request_sha256") or ""),
            "model_identity_sha256": str(
                request.get("model_identity_sha256") or ""
            ),
            "manifest_file_sha256": str(
                request.get("manifest_file_sha256") or ""
            ),
        }


def _synchronize_release_authority(
    *,
    svc: Any,
    state: dict[str, Any],
    runtime_boot_id: str,
    runtime_attestation: dict[str, Any],
    model_sets: dict[str, LoadedModelSet],
) -> dict[str, Any]:
    """ACK pending authority and revoke any active generation on drift."""

    authority = dict(state.get("release_authority") or {})
    status = str(authority.get("status") or "").strip().lower()
    request = dict(authority.get("request") or {})
    pair = str(request.get("pair") or "").strip().upper()
    pair_attestation = _release_pair_attestation(runtime_attestation, pair)
    generation_id = str(request.get("generation_id") or "")
    request_sha256 = str(request.get("request_sha256") or "")
    active_db_row = svc.get_active_model_set(pair) if pair else None
    errors: list[str] = []

    if status in {"pending", "acknowledged"}:
        errors.extend(
            authority_request_errors(
                request,
                active_db_row=active_db_row,
                validate_evidence=True,
            )
        )
        if str(runtime_boot_id or "") != str(
            runtime_attestation.get("runtime_boot_id") or ""
        ):
            errors.append("release_authority_runtime_boot_changed")
        if runtime_attestation.get("valid") is not True:
            errors.extend(
                f"runtime_attestation:{item}"
                for item in list(runtime_attestation.get("errors") or [])
            )
        loaded = model_sets.get(pair)
        if loaded is None:
            errors.append("release_authority_loaded_pair_missing")
        elif str(loaded.model_set_id or "") != str(
            request.get("model_set_id") or ""
        ):
            errors.append("release_authority_loaded_model_set_mismatch")
        for field in (
            "source_sha256",
            "package_merkle_sha256",
            "config_sha256",
            "manifest_file_sha256",
            "model_identity_sha256",
            "artifact_set_sha256",
            "model_set_id",
        ):
            if not str(pair_attestation.get(field) or "") or str(
                pair_attestation.get(field) or ""
            ) != str(request.get(field) or ""):
                errors.append(f"release_authority_runtime_{field}_mismatch")
        if state.get("execution_egress_enabled") is True:
            errors.append("release_authority_pending_with_egress_enabled")
        errors = list(dict.fromkeys(errors))
        if errors:
            rejected = {
                **authority,
                "schema_version": RELEASE_AUTHORITY_STATE_SCHEMA,
                "status": "rejected",
                "errors": errors,
                "updated_at": float(time.time()),
            }
            svc.compare_and_set_release_authority(
                next_authority=rejected,
                expected_generation_id=generation_id,
                expected_status=status,
            )
            refreshed = svc.get_state()
            _apply_release_authority_rollout(
                model_sets=model_sets,
                state=refreshed,
                authority_valid=False,
            )
            return {
                "status": "rejected",
                "valid": False,
                "errors": errors,
                "generation_id": generation_id,
                "request_sha256": request_sha256,
            }
        if status == "pending":
            ack = {
                "schema_version": RELEASE_AUTHORITY_ACK_SCHEMA,
                "generation_id": generation_id,
                "request_sha256": request_sha256,
                "runtime_boot_id": str(runtime_boot_id),
                "runtime_pid": int(os.getpid()),
                "source_sha256": str(pair_attestation.get("source_sha256") or ""),
                "package_merkle_sha256": str(
                    pair_attestation.get("package_merkle_sha256") or ""
                ),
                "config_sha256": str(pair_attestation.get("config_sha256") or ""),
                "manifest_file_sha256": str(
                    pair_attestation.get("manifest_file_sha256") or ""
                ),
                "model_identity_sha256": str(
                    pair_attestation.get("model_identity_sha256") or ""
                ),
                "artifact_set_sha256": str(
                    pair_attestation.get("artifact_set_sha256") or ""
                ),
                "model_set_id": str(pair_attestation.get("model_set_id") or ""),
                "acked_at": float(time.time()),
            }
            acknowledged = {
                **authority,
                "status": "acknowledged",
                "ack": ack,
                "errors": [],
                "updated_at": float(time.time()),
            }
            result = svc.compare_and_set_release_authority(
                next_authority=acknowledged,
                expected_generation_id=generation_id,
                expected_status="pending",
            )
            if result.get("updated") is not True:
                errors = [
                    str(result.get("reason") or "release_ack_cas_failed"),
                    *list(result.get("errors") or []),
                ]
            status = "acknowledged" if not errors else "rejected"
        refreshed = svc.get_state()
        _apply_release_authority_rollout(
            model_sets=model_sets,
            state=refreshed,
            authority_valid=False,
        )
        return {
            "status": status,
            "valid": False,
            "errors": list(dict.fromkeys(errors)),
            "generation_id": generation_id,
            "request_sha256": request_sha256,
        }

    if status == "active":
        errors = active_authority_errors(
            authority,
            active_db_row=active_db_row,
            runtime_boot_id=runtime_boot_id,
            runtime_attestation=pair_attestation,
            expected_generation_id=generation_id,
            expected_request_sha256=request_sha256,
            validate_evidence=True,
        )
        if errors:
            svc.disable_execution_egress(
                reason="release_authority_drift:" + str(errors[0]),
                revoke_release=True,
            )
            refreshed = svc.get_state()
            _apply_release_authority_rollout(
                model_sets=model_sets,
                state=refreshed,
                authority_valid=False,
            )
            return {
                "status": "revoked",
                "valid": False,
                "errors": list(dict.fromkeys(errors)),
                "generation_id": generation_id,
                "request_sha256": request_sha256,
            }
        _apply_release_authority_rollout(
            model_sets=model_sets,
            state=state,
            authority_valid=True,
        )
        return {
            "status": "active",
            "valid": True,
            "errors": [],
            "generation_id": generation_id,
            "request_sha256": request_sha256,
        }

    _apply_release_authority_rollout(
        model_sets=model_sets,
        state=state,
        authority_valid=False,
    )
    return {
        "status": status or "absent",
        "valid": False,
        "errors": [],
        "generation_id": generation_id,
        "request_sha256": request_sha256,
    }


# AGENT FLOW: `run_loop` is the live orchestrator. Startup phases build the executable model/feature graph; each cycle then scores pairs, applies policy layers, submits actions, and patches state.
def run_loop(*, equity: float, sleep_secs: int, feature_root: str) -> None:
    s = get_settings()
    startup_model_preflight = validate_runtime_startup(s)
    # Typo'd FXSTACK_* variable NAMES silently no-op (extra="ignore" validates
    # values, not names). Warn loudly at startup with a nearest-match hint;
    # warn-first by design -- see unknown_fxstack_env_warnings.
    for env_warning in unknown_fxstack_env_warnings():
        _startup_log(f"env_warning {env_warning}")
    # Computed once at startup: the deployed model set does not change mid-run,
    # so neither can its warrant. Entry-only -- see the helper's docstring.
    uncertified_entry_block_reason = _uncertified_entry_block_reason(
        preflight=startup_model_preflight, settings=s
    )
    entry_certification_mode = _resolved_entry_certification_mode(s)
    if uncertified_entry_block_reason:
        _startup_log(
            "entries_blocked reason=models_uncertified "
            "detail=active_model_set_has_no_passing_validation_certificate "
            "note=exits_and_position_management_remain_enabled "
            "override=FXSTACK_ENTRY_CERTIFICATION_MODE=exploration_demo"
        )
    elif entry_certification_mode == "exploration_demo":
        _startup_log(
            "entry_certification_mode=exploration_demo "
            "detail=uncertified_entries_permitted_as_labeled_forward_experiment "
            "fence=requires_fresh_broker_demo_account_attestation "
            "note=every_risk_decision_is_stamped_with_this_mode"
        )
    pairs = list(s.pairs)
    if not pairs:
        raise RuntimeError("FXSTACK_PAIRS is empty")
    _startup_log(f"begin pairs={len(pairs)} bridge={s.mt4_bridge_url} db={s.database_url}")
    _perform_startup_bridge_checks(s)
    from fxstack.runtime.service import RuntimeService

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
    runtime_attestation: dict[str, Any] = {}
    release_authority_diag: dict[str, Any] = {
        "status": "absent",
        "valid": False,
        "binding": "advisory_only",
        "errors": [],
    }
    production_authority_armed = False
    startup_runtime_diag: dict[str, Any] = {
        "model_preflight": dict(startup_model_preflight),
        "pending_command_policy": "purge_queued_quarantine_delivered",
        "pending_commands_purged": 0,
        "delivered_commands_quarantined": 0,
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
        "live_command_admission": {},
    }
    runtime_running = False
    main_loop_ready_logged = False
    risk_equity_peak = float("nan")

    provider = str(s.normalized_data_provider)
    market_provider = str(provider_roles_from_settings(s).get("market_data_provider") or "mt4_bridge")
    store = ParquetStore(Path(feature_root))
    raw_store = ParquetStore(_raw_root_for_feature_root(feature_root))
    regime_timeframe = str(s.regime_timeframe).upper()
    swing_timeframe = str(s.swing_timeframe).upper()
    intraday_timeframe = str(s.intraday_timeframe).upper()
    feature_timeframes = _required_feature_timeframes()
    paper_mode = _paper_mode_enabled(s)
    last_action_key: dict[str, str] = {}
    partial_close_tracker: dict[str, dict[str, Any]] = {}
    adaptive_pending_entry_registry: dict[str, dict[str, Any]] = {}
    adaptive_position_registry: dict[str, SimpleNamespace] = {}
    adaptive_recent_exit_registry: dict[str, dict[str, Any]] = {}
    exit_command_ledger: dict[str, dict[str, Any]] = {}
    campaign_registry: dict[str, CampaignRegistryEntry] = {}
    campaign_transition_counts: dict[str, int] = {}
    campaign_state_counts_runtime: dict[str, int] = {}
    managed_position_recovery_diag: dict[str, Any] = {"status": "not_started"}
    adaptive_last_ts_by_pair: dict[str, str] = {str(pair).upper(): "" for pair in pairs}
    adaptive_bar_index_by_pair: dict[str, int] = {str(pair).upper(): -1 for pair in pairs}
    last_positions_snapshot_token = ""
    intraday_enrichment_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
    feature_bootstrap: dict[str, dict[str, dict[str, Any]]] = {}
    live_bar_refresh_cache: dict[str, str] = {}
    stale_feature_refresh_minute: dict[str, int] = {}
    live_refresh_diag: dict[str, dict[str, Any]] = {}
    adaptive_history: dict[str, list[dict[str, Any]]] = {str(pair).upper(): [] for pair in pairs}
    adaptive_playbooks = parse_enabled_playbooks(getattr(s, "adaptive_playbooks", None))
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
        pre_boot_state = svc.get_state()
        last_positions_snapshot_token = str(
            pre_boot_state.get("positions_snapshot_token") or ""
        ).strip()
        pre_boot_runtime_diag = dict(pre_boot_state.get("runtime_diag") or {})
        pre_boot_orchestration_live = dict(
            pre_boot_runtime_diag.get("orchestration_live") or {}
        )
        managed_position_recovery_diag = _restore_managed_position_state(
            payload=dict(pre_boot_runtime_diag.get("managed_position_state") or {}),
            adaptive_position_registry=adaptive_position_registry,
            partial_close_tracker=partial_close_tracker,
            campaign_registry=campaign_registry,
            allowed_pairs={str(pair).upper() for pair in pairs},
            adaptive_pending_entry_registry=adaptive_pending_entry_registry,
            adaptive_recent_exit_registry=adaptive_recent_exit_registry,
            exit_command_ledger=exit_command_ledger,
            sleeve_tracker=sleeve_tracker,
        )
        management_command_recovery_after_ts = max(
            float(
                _safe_float(managed_position_recovery_diag.get("saved_at"), 0.0)
            ),
            float(_safe_float(pre_boot_state.get("runtime_last_cycle_ts"), 0.0)),
        )
        managed_position_recovery_diag["command_recovery_after_ts"] = float(
            management_command_recovery_after_ts
        )
        durable_management_commands = list(svc.get_commands(limit=5000) or [])
        managed_position_recovery_diag.update(
            _hydrate_partial_close_tracker_from_commands(
                commands=durable_management_commands,
                partial_close_tracker=partial_close_tracker,
                adaptive_position_registry=adaptive_position_registry,
                allowed_pairs={str(pair).upper() for pair in pairs},
                after_ts=float(management_command_recovery_after_ts),
            )
        )
        managed_position_recovery_diag.update(
            _hydrate_exit_command_ledger_from_commands(
                commands=durable_management_commands,
                exit_command_ledger=exit_command_ledger,
                allowed_pairs={str(pair).upper() for pair in pairs},
                after_ts=float(management_command_recovery_after_ts),
            )
        )
        startup_runtime_diag["managed_position_recovery"] = dict(
            managed_position_recovery_diag
        )
        startup_runtime_diag["managed_position_state"] = (
            _serialize_managed_position_state(
                adaptive_position_registry=adaptive_position_registry,
                partial_close_tracker=partial_close_tracker,
                campaign_registry=campaign_registry,
                saved_at=float(time.time()),
                adaptive_pending_entry_registry=adaptive_pending_entry_registry,
                adaptive_recent_exit_registry=adaptive_recent_exit_registry,
                exit_command_ledger=exit_command_ledger,
                sleeve_governance_state=sleeve_tracker.export_state(),
            )
        )
        risk_equity_peak = _advance_runtime_equity_peak(
            persisted_peak=pre_boot_state.get("equity_peak"),
            current_equity=pre_boot_state.get("equity"),
            fallback_equity=equity,
        )
        svc.patch_state(
            _runtime_boot_reset_patch(
                runtime_profile=str(s.policy_version),
                equity_seed=float(equity),
                equity_peak=float(risk_equity_peak),
                pairs=pairs,
                startup_state=startup_state,
                runtime_diag=startup_runtime_diag,
                preserved_orchestration_live=pre_boot_orchestration_live,
            )
        )
        _startup_log("state_patched_boot")
        pending_purged = int(svc.purge_pending_commands(reason="runtime_restart_purged", include_delivered=False))
        startup_runtime_diag["pending_commands_purged"] = int(pending_purged)
        quarantined_delivered = int(svc.quarantine_stale_delivered(age_secs=s.startup_requeue_age_secs))
        startup_runtime_diag["delivered_commands_quarantined"] = int(quarantined_delivered)
        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="boot",
            runtime_diag=startup_runtime_diag,
        )
        _startup_log(f"pending_commands_purged count={pending_purged}")
        _startup_log(f"delivered_commands_quarantined count={quarantined_delivered}")

        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="manifest_seed",
            runtime_diag=startup_runtime_diag,
        )
        manifest_seed_diag = _seed_active_model_sets_from_manifest(
            svc=svc,
            project_root=s.project_root,
            expected_manifest_sha256=str(
                startup_model_preflight.get("manifest_content_sha256") or ""
            ),
        )
        startup_runtime_diag["manifest_seed"] = dict(manifest_seed_diag)
        _startup_log(f"manifest_seed reason={manifest_seed_diag.get('reason')} seeded={manifest_seed_diag.get('seeded')}")
        _require_required_model_startup_consistency(
            settings=s,
            configured_pairs=pairs,
            stage="manifest_seed",
            payload=manifest_seed_diag,
        )

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

        _apply_production_operator_rollout(
            settings=s,
            model_sets=model_sets,
        )
        live_command_admission = _live_command_admission_diagnostics(
            settings=s,
            model_sets=model_sets,
        )
        startup_runtime_diag["live_command_admission"] = dict(
            live_command_admission
        )
        if not bool(live_command_admission.get("allowed", False)):
            _startup_log(
                "live_command_admission_blocked:"
                + "|".join(
                    str(item)
                    for item in list(live_command_admission.get("blockers") or [])
                )
            )

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
            loaded = model_sets.get(pair)
            for timeframe in feature_timeframes:
                feature_service_name = (
                    _loaded_feature_service_name(
                        loaded,
                        pair=pair,
                        timeframe=timeframe,
                        regime_timeframe=regime_timeframe,
                        swing_timeframe=swing_timeframe,
                        intraday_timeframe=intraday_timeframe,
                    )
                    if loaded is not None
                    else None
                )
                row, bootstrap_diag = _bootstrap_pair_features_for_timeframe(
                    feature_store=store,
                    raw_store=raw_store,
                    provider=provider,
                    pair=pair,
                    timeframe=timeframe,
                    all_pairs=pairs,
                    paper_mode=paper_mode,
                    feature_service_name=feature_service_name,
                )
                if bootstrap_diag.get("attempted"):
                    pair_bootstrap[timeframe] = dict(bootstrap_diag)
            live_refresh_diag[pair] = (
                _refresh_pair_feature_tails_from_local_snapshot(
                    feature_store=store,
                    raw_store=raw_store,
                    provider=provider,
                    pair=pair,
                    svc=svc,
                )
                if paper_mode
                else _refresh_live_pair_market_data(
                    bridge_url=s.mt4_bridge_url,
                    raw_store=raw_store,
                    feature_store=store,
                    pair=pair,
                    provider=provider,
                    market_provider=market_provider,
                    latest_bar_cache=live_bar_refresh_cache,
                    svc=svc,
                )
            )
            startup_runtime_diag["feature_bootstrap"] = dict(feature_bootstrap)
            startup_runtime_diag["live_feature_refresh"] = dict(live_refresh_diag)
            _startup_log(f"initial_refresh_done pair={pair} reason={live_refresh_diag[pair].get('reason')}")

        startup_runtime_diag.update(_feature_serving_runtime_diag())
        startup_state = _touch_runtime_startup_progress(
            svc=svc,
            startup_state=startup_state,
            phase="initial_refresh",
            phase_total=int(len(pairs)),
            runtime_diag=startup_runtime_diag,
        )
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
        if bool(getattr(s, "adaptive_execution_enabled", False)):
            adaptive_history = _bootstrap_adaptive_history(
                feature_store=store,
                provider=provider,
                pairs=pairs,
                timeframe=intraday_timeframe,
                history_bars=max(16, int(getattr(s, "adaptive_history_bars", 128) or 128)),
            )
            startup_runtime_diag["adaptive_history"] = {
                "timeframe": str(intraday_timeframe),
                "configured_bars": max(16, int(getattr(s, "adaptive_history_bars", 128) or 128)),
                "unique_bars_by_pair": {
                    str(pair).upper(): int(len(adaptive_history.get(str(pair).upper(), [])))
                    for pair in pairs
                },
                "source": "feature_store",
            }
        _apply_production_operator_rollout(
            settings=s,
            model_sets=model_sets,
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
            expected_manifest_sha256=str(
                startup_model_preflight.get("manifest_content_sha256") or ""
            ),
        )
        startup_runtime_diag["activation_consistency"] = dict(activation_consistency)
        _startup_log(
            "activation_consistency "
            + f"manifest_db={activation_consistency.get('active_manifest_matches_db')} "
            + f"runtime_db={activation_consistency.get('runtime_loaded_matches_db')}"
        )
        _require_required_model_startup_consistency(
            settings=s,
            configured_pairs=pairs,
            stage="activation_consistency",
            payload=activation_consistency,
        )
        runtime_attestation = _build_runtime_release_attestation(
            settings=s,
            runtime_boot_id=runtime_boot_id,
            model_sets=model_sets,
            activation_consistency=activation_consistency,
        )
        svc.patch_state(
            {
                "runtime_boot_id": str(runtime_boot_id),
                "runtime_attestation": dict(runtime_attestation),
            }
        )
        startup_runtime_diag["runtime_attestation"] = dict(
            runtime_attestation
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
        startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
        runtime_running = True
        progress_touch_t0 = time.perf_counter()
        provider_roles = provider_roles_from_settings(s)
        market_provider = str(provider_roles.get("market_data_provider") or "mt4_bridge")
        paper_mode = _paper_mode_enabled(s)
        try:
            bridge_ready = fetch_market_ready(s.mt4_bridge_url, provider=market_provider, settings=s)
        except Exception as exc:
            if not paper_mode:
                raise
            bridge_ready = {
                "provider": market_provider,
                "status": "shadow_only",
                "supported": False,
                "shadow_only": True,
                "fresh": False,
                "reason": f"paper_venue_probe_failed:{type(exc).__name__}",
            }
        try:
            ticks = fetch_market_ticks(s.mt4_bridge_url, provider=market_provider, settings=s)
        except Exception:
            if not paper_mode:
                raise
            ticks = {}
        # One rate map per cycle: converts the 100k contract into ACCOUNT currency
        # so a pair whose quote currency is not the account currency is sized
        # correctly. Built from the same ticks the cycle already fetched.
        cycle_quote_rates = _quote_rate_map(ticks)
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
            pair_key = str(pair).upper()
            current_feature = _latest_feature_row(
                store=store,
                raw_store=raw_store,
                pair=pair,
                timeframe=intraday_timeframe,
                all_pairs=pairs,
            )
            current_feature_stale = _feature_row_is_stale(
                row=current_feature,
                loop_ts=float(loop_ts),
                timeframe=str(intraday_timeframe),
            )
            stale_refresh_token = int(float(loop_ts) // 60.0)
            if current_feature_stale and stale_feature_refresh_minute.get(pair_key) != stale_refresh_token:
                live_refresh_diag[pair] = (
                    _refresh_pair_feature_tails_from_local_snapshot(
                        feature_store=store,
                        raw_store=raw_store,
                        provider=provider,
                        pair=pair,
                        svc=svc,
                    )
                    if paper_mode
                    else _refresh_live_pair_market_data(
                        bridge_url=s.mt4_bridge_url,
                        raw_store=raw_store,
                        feature_store=store,
                        pair=pair,
                        provider=provider,
                        market_provider=market_provider,
                        latest_bar_cache=live_bar_refresh_cache,
                        svc=svc,
                    )
                )
                stale_feature_refresh_minute[pair_key] = stale_refresh_token
                continue
            bucket = _tick_bucket_start(tick=tick, timeframe=intraday_timeframe)
            if bucket is None:
                continue
            bucket_key = str(pd.to_datetime(float(bucket), unit="s", utc=True))
            latest_raw_ts = _latest_partition_ts(
                store=raw_store,
                provider=provider,
                pair=pair,
                timeframe=intraday_timeframe,
            )
            if latest_raw_ts is not None and str(latest_raw_ts) == bucket_key:
                if not current_feature_stale:
                    live_bar_refresh_cache[pair_key] = bucket_key
                    continue
                live_bar_refresh_cache.pop(pair_key, None)
            if live_bar_refresh_cache.get(pair_key) == bucket_key:
                live_bar_refresh_cache.pop(pair_key, None)
            live_refresh_diag[pair] = (
                _refresh_pair_feature_tails_from_local_snapshot(
                    feature_store=store,
                    raw_store=raw_store,
                    provider=provider,
                    pair=pair,
                    svc=svc,
                )
                if paper_mode
                else _refresh_live_pair_market_data(
                    bridge_url=s.mt4_bridge_url,
                    raw_store=raw_store,
                    feature_store=store,
                    pair=pair,
                    provider=provider,
                    market_provider=market_provider,
                    latest_bar_cache=live_bar_refresh_cache,
                    svc=svc,
                )
            )
        state = svc.get_state()
        if not production_authority_armed:
            production_authority_diag = _arm_production_runtime_authority(
                svc=svc,
                state=state,
                settings=s,
                model_sets=model_sets,
                runtime_boot_id=runtime_boot_id,
            )
            production_authority_armed = bool(
                production_authority_diag.get("valid", False)
                and production_authority_diag.get("status") in {
                    "active",
                    "not_applicable",
                }
            )
        else:
            production_authority_diag = {
                "status": "active",
                "valid": True,
                "binding": "production_runtime",
                "errors": [],
            }
        advisory_release = dict(state.get("release_authority") or {})
        release_authority_diag = {
            "status": str(advisory_release.get("status") or "absent"),
            "valid": False,
            "binding": "advisory_only",
            "errors": list(advisory_release.get("errors") or []),
        }
        startup_runtime_diag["production_execution_authority"] = dict(
            production_authority_diag
        )
        state = svc.get_state()
        current_live_command_admission = _live_command_admission_diagnostics(
            settings=s,
            model_sets=model_sets,
        )
        startup_runtime_diag["live_command_admission"] = dict(
            current_live_command_admission
        )
        symbol_readiness = dict(state.get("symbol_readiness", {}) or {})
        persisted_governance = dict(state.get("governance", {}) or {})
        governance_enabled = bool(getattr(s, "capital_governance_enabled", False))
        capital_band_mode = str(getattr(s, "capital_band_mode", "paper") or "paper").strip().lower()
        mt4_fresh = bool(bridge_ready.get("mt4_fresh")) if bridge_ready else _state_mt4_fresh(state)
        ticks_fresh = bool(bridge_ready.get("ticks_fresh")) if bridge_ready else bool(ticks)
        positions_snapshot_token = str(
            state.get("positions_snapshot_token") or ""
        ).strip()
        positions_snapshot_received_at = float(
            _safe_float(state.get("positions_snapshot_received_at"), 0.0)
        )
        positions_snapshot_advanced = bool(
            positions_snapshot_token
            and positions_snapshot_token != last_positions_snapshot_token
        )
        if positions_snapshot_advanced:
            last_positions_snapshot_token = positions_snapshot_token
        partial_reconciliation_diag = _reconcile_partial_close_tracker(
            partial_close_tracker=partial_close_tracker,
            adaptive_position_registry=adaptive_position_registry,
            state=state,
            svc=svc,
            loop_ts=float(loop_ts),
            settings=s,
            position_snapshot_advanced=bool(positions_snapshot_advanced),
            position_snapshot_received_at=float(positions_snapshot_received_at),
        )
        partial_reconciliation_diag.update(
            {
                "positions_snapshot_advanced": bool(positions_snapshot_advanced),
                "positions_snapshot_received_at": float(
                    positions_snapshot_received_at
                ),
                "positions_snapshot_source": str(
                    state.get("positions_snapshot_source") or ""
                ),
            }
        )
        exit_reconciliation_diag = _reconcile_exit_command_ledger(
            exit_command_ledger=exit_command_ledger,
            state=state,
            svc=svc,
            loop_ts=float(loop_ts),
            position_snapshot_advanced=bool(positions_snapshot_advanced),
            position_snapshot_received_at=float(positions_snapshot_received_at),
            adaptive_recent_exit_registry=adaptive_recent_exit_registry,
            campaign_registry=campaign_registry,
            campaign_transition_counts=campaign_transition_counts,
            campaign_config=campaign_config,
            sleeve_tracker=sleeve_tracker,
        )
        current_equity_value = _safe_float(state.get("equity"), float(equity))
        risk_equity_peak = _advance_runtime_equity_peak(
            persisted_peak=risk_equity_peak,
            current_equity=state.get("equity_peak"),
            fallback_equity=equity,
        )
        risk_equity_peak = _advance_runtime_equity_peak(
            persisted_peak=risk_equity_peak,
            current_equity=current_equity_value,
            fallback_equity=equity,
        )
        # Every risk evaluation in this cycle receives the same monotonic
        # high-water mark; the exact value is persisted again below.
        state["equity_peak"] = float(risk_equity_peak)
        equity_drawdown_pct = (
            max(0.0, (1.0 - (float(current_equity_value) / float(risk_equity_peak))) * 100.0)
            if float(current_equity_value) > 0.0
            else 100.0
        )
        live_position_pairs = {str(dict(raw or {}).get("symbol") or "").upper() for raw in list(state.get("positions", []) or [])}

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

        # AGENT HANDSHAKE: Capital governance is computed before admission from the
        # latest complete cycle plus the current book.  The exact snapshot below is
        # used by every entry gate and is persisted unchanged after finalization.
        try:
            pre_entry_portfolio = evaluate_portfolio_allocation(
                symbol=str(pairs[0] if pairs else ""),
                session_bucket="",
                expected_edge_bps=0.0,
                uncertainty_score=0.0,
                positions=_annotate_positions_with_contract_value(
                    list(state.get("positions", []) or []),
                    quote_rates=cycle_quote_rates,
                    settings=s,
                ),
                pending_entries=[],
                max_total_positions=int(getattr(s, "max_total_positions", 0) or 0),
                max_pair_positions=int(getattr(s, "max_pair_positions", 0) or 0),
                governance={},
                corr_mode=portfolio_corr_mode,
                realized_returns_by_pair=realized_returns_by_pair,
                corr_window_bars=int(getattr(s, "portfolio_realized_corr_window_bars", 0) or 0),
                corr_min_obs=int(getattr(s, "portfolio_realized_corr_min_obs", 0) or 0),
            )
            pre_entry_portfolio_diag = dict(
                build_portfolio_telemetry(
                    book=pre_entry_portfolio.book,
                    concentration=pre_entry_portfolio.concentration,
                    correlation=pre_entry_portfolio.correlation,
                    budget=pre_entry_portfolio.budget,
                    stress=pre_entry_portfolio.stress,
                    governance={},
                )
            )
        except Exception as exc:
            pre_entry_portfolio_diag = {
                "numeric_inputs_valid": False,
                "book_numeric_inputs_valid": False,
                "book_numeric_input_errors": [f"portfolio_snapshot:{type(exc).__name__}"],
                "concentration": {},
                "correlation": {},
                "budget": {},
            }
        prior_runtime_diag = dict(state.get("runtime_diag", {}) or {})
        capital_governance = compute_binding_capital_governance_snapshot(
            settings=s,
            runtime_diag={
                "loop_latency_ms": float(_safe_float(prior_runtime_diag.get("loop_latency_ms"), 0.0)),
                **_feature_serving_runtime_diag(),
                "risk_cycle_summary": dict(prior_runtime_diag.get("risk_cycle_summary") or {}),
                # Equity denominator for the tail-loss gate: the stress module
                # reports the all-stops-hit loss in account currency; the gate
                # compares it to capital_max_tail_loss_pct as a percent of this.
                "current_equity": float(_safe_float(current_equity_value, 0.0)),
            },
            metrics=svc.get_metrics(),
            portfolio_telemetry=pre_entry_portfolio_diag,
            provider_health=dict(prior_runtime_diag.get("provider_health") or {}),
            previous_governance=persisted_governance,
            previous_cycle_ts=state.get("runtime_last_cycle_ts"),
            computed_at=float(loop_ts),
            max_source_age_secs=max(60.0, float(max(1, int(sleep_secs))) * 3.0),
        )
        governance = dict(capital_governance if governance_enabled else persisted_governance)
        paused = bool(governance.get("paused", False))
        governance_entries_only = bool(governance.get("entries_only", False) or getattr(s, "capital_entries_only", False))
        governance_shadow_only = bool(governance.get("shadow_only", False) or getattr(s, "provider_shadow_only", False))
        if governance_enabled and capital_band_mode == "paper":
            governance_shadow_only = True
        risk_governance_policy = (
            dict(governance)
            if governance_enabled
            else {
                "capital_band": str(capital_band_mode),
                "mode": "paused" if paused else ("entries_only" if governance_entries_only else ("shadow_only" if governance_shadow_only else "normal")),
                "paused": bool(paused),
                "entries_only": bool(governance_entries_only),
                "shadow_only": bool(governance_shadow_only),
                "budget_scale": float(capital_band_budget_scale(str(capital_band_mode), s)),
            }
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
                _append_failed_pair_decision_with_fail_safe(
                    decisions=decisions,
                    pending_position_actions=pending_position_actions,
                    pair=str(pair),
                    failure_reason=str(reason),
                    state=dict(state),
                    tick=dict((ticks.get(pair, {}) if isinstance(ticks, dict) else {}) or {}),
                    loop_ts=float(loop_ts),
                    settings=s,
                    intraday_timeframe=str(intraday_timeframe),
                    extra_metadata={
                        "startup_inference": dict(startup_status),
                    },
                )
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue

            pair_rows: dict[str, pd.DataFrame] = {}
            pair_bootstrap = feature_bootstrap.setdefault(str(pair), {})
            missing_frames: list[str] = []
            for timeframe in feature_timeframes:
                feature_service_name = _loaded_feature_service_name(
                    loaded,
                    pair=pair,
                    timeframe=timeframe,
                    regime_timeframe=regime_timeframe,
                    swing_timeframe=swing_timeframe,
                    intraday_timeframe=intraday_timeframe,
                )
                row, bootstrap_diag = _bootstrap_pair_features_for_timeframe(
                    feature_store=store,
                    raw_store=raw_store,
                    provider=provider,
                    pair=pair,
                    timeframe=timeframe,
                    all_pairs=pairs,
                    paper_mode=paper_mode,
                    feature_service_name=feature_service_name,
                )
                if bootstrap_diag.get("attempted"):
                    pair_bootstrap[timeframe] = dict(bootstrap_diag)
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
                _append_failed_pair_decision_with_fail_safe(
                    decisions=decisions,
                    pending_position_actions=pending_position_actions,
                    pair=str(pair),
                    failure_reason=str(reason),
                    state=dict(state),
                    tick=dict((ticks.get(pair, {}) if isinstance(ticks, dict) else {}) or {}),
                    loop_ts=float(loop_ts),
                    settings=s,
                    loaded=loaded,
                    intraday_row=pair_rows.get(intraday_timeframe),
                    intraday_timeframe=str(intraday_timeframe),
                    extra_metadata=meta,
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
                _append_failed_pair_decision_with_fail_safe(
                    decisions=decisions,
                    pending_position_actions=pending_position_actions,
                    pair=str(pair),
                    failure_reason=str(reason),
                    state=dict(state),
                    tick=dict(tick),
                    loop_ts=float(loop_ts),
                    settings=s,
                    loaded=loaded,
                    intraday_row=intraday_row,
                    intraday_timeframe=str(intraday_timeframe),
                    error=str(exc),
                )
                pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)
                continue
            expected_edge_bps = float(signal.expected_edge_bps)
            swing_route = loaded.swing_router.diagnostics()
            intraday_route = loaded.intraday_router.diagnostics()
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
            decision_reasons.extend(
                _entry_venue_readiness_reasons(
                    paper_mode=paper_mode,
                    mt4_fresh=bool(mt4_fresh),
                    ticks_fresh=bool(ticks_fresh),
                    tick_present=bool(tick),
                )
            )
            # Entry-only, exactly like the rollout and heartbeat gates below: an
            # uncertified model set can still manage and close what it holds.
            if not positions and uncertified_entry_block_reason:
                decision_reasons.append(str(uncertified_entry_block_reason))
            # exploration_demo's fence: uncertified entries are only permitted
            # on a heartbeat-attested DEMO account; real/unattested fails closed.
            if not positions:
                exploration_demo_block_reason = _exploration_demo_entry_block_reason(
                    mode=entry_certification_mode,
                    broker_account_mode=str(
                        dict(state or {}).get("broker_account_mode") or ""
                    ),
                )
                if exploration_demo_block_reason:
                    decision_reasons.append(str(exploration_demo_block_reason))
            if not positions and bool(feature_bar.get("stale")):
                decision_reasons.append(str(feature_bar.get("reason") or "stale_feature_bar"))
            if not positions and bool(signal.session_entry_blocked):
                decision_reasons.append(str(signal.session_entry_block_reason or f"session_blocked:{signal.session_bucket}"))
            adaptive_recovery_reason = _adaptive_recovery_reason(signal=signal, settings=s)
            signal_rejection_reason = str(signal.rejection_reason)
            if not bool(signal.allowed) and not adaptive_recovery_reason:
                decision_reasons.append(signal_rejection_reason)
            if str(spread_unit_source) == "missing":
                decision_reasons.append("missing_spread_input")
            if paused:
                decision_reasons.append("governance_paused")
            if not positions and governance_entries_only:
                decision_reasons.append("governance_entries_only")
            if not positions and governance_shadow_only:
                decision_reasons.append("governance_shadow_only")
            # A zero budget scale zeroes every sizing path; without this reason
            # the cycle telemetry (ready/rejection_counts/baseline_rejection_
            # reason) reports "nothing rejected" while the kernel blocks every
            # entry -- the silent-zero-entries class again, from the cycle side.
            if (
                not positions
                and _safe_float(dict(risk_governance_policy or {}).get("budget_scale"), 1.0) <= 0.0
            ):
                decision_reasons.append("portfolio_budget_scale_zero")
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
            tp_price = 0.0
            hard_lifecycle_action = "hold"
            hard_lifecycle_reason = ""
            hard_lifecycle_action_score = 0.0
            hard_lifecycle_close_lots = 0.0
            hard_lifecycle_sl_price = 0.0
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
            reversal_ready = _reversal_exit_ready(
                reversal_context_active=bool(reversal_context_active),
                signal_allowed=bool(signal.allowed),
                has_reversal_models=bool(loaded.has_reversal_models),
                reversal_blocking_reasons=list(reversal_blocking_reasons),
                reversal_failure_prob=float(reversal_failure_prob),
                reversal_opportunity_prob=float(reversal_opportunity_prob),
                reversal_failure_min_prob=float(s.reversal_failure_min_prob),
                reversal_opportunity_min_prob=float(s.reversal_opportunity_min_prob),
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
                    hard_lifecycle_action = "exit"
                    hard_lifecycle_reason = "hard_time_stop"
                    hard_lifecycle_action_score = 1.0
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

            if not positions:
                entry_protection, entry_protection_reason = _entry_protection_prices(
                    pair=str(pair),
                    side=str(side),
                    tick=dict(tick),
                    row=intraday_row.iloc[0],
                    settings=s,
                )
                if entry_protection_reason:
                    decision_reasons = list(dict.fromkeys([*decision_reasons, str(entry_protection_reason)]))
                    ready = False
                else:
                    sl_price = float(entry_protection["sl_price"])
                    tp_price = float(entry_protection["tp_price"])

            risk_lifecycle_inputs = _risk_kernel_lifecycle_inputs(
                has_open_position=bool(positions),
                lifecycle_action=str(lifecycle_action),
                lifecycle_reason=str(lifecycle_reason),
                lifecycle_action_score=float(lifecycle_action_score),
                close_lots=float(close_lots),
                sl_price=float(sl_price),
                tp_price=float(tp_price),
                signal=signal,
                entry_ready=bool(ready),
            )
            risk_lifecycle_action = str(risk_lifecycle_inputs["lifecycle_action"])
            risk_lifecycle_reason = str(risk_lifecycle_inputs["lifecycle_reason"])
            risk_lifecycle_action_score = float(risk_lifecycle_inputs["lifecycle_action_score"])
            risk_close_lots = float(risk_lifecycle_inputs["close_lots"])
            risk_sl_price = float(risk_lifecycle_inputs["sl_price"])
            risk_tp_price = float(risk_lifecycle_inputs["tp_price"])
            raw_policy_suggestion = {
                "side": str(side),
                "expected_edge_bps": float(expected_edge_bps),
                "trade_prob": float(signal.trade_prob),
                "allowed": bool(ready),
                "rejection_reasons": list(decision_reasons),
                "lifecycle_action_requested": str(risk_lifecycle_action),
                "lifecycle_reason_requested": str(risk_lifecycle_reason),
                "close_lots_requested": float(risk_close_lots),
                "sl_price_requested": float(risk_sl_price),
                "tp_price_requested": float(risk_tp_price),
            }
            agent_mode = _normalize_agent_mode(getattr(s, "agent_mode", "off"))
            execution_rollout_policy = dict(getattr(loaded, "rollout_policy", {}) or {})
            sizing_rollout_policy = dict(execution_rollout_policy)
            if agent_mode not in {"paper", "live"}:
                sizing_rollout_policy = {}
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
                lifecycle_action=str(risk_lifecycle_action),
                lifecycle_reason=str(risk_lifecycle_reason),
                lifecycle_action_score=float(risk_lifecycle_action_score),
                close_lots=float(risk_close_lots),
                sl_price=float(risk_sl_price),
                tp_price=float(risk_tp_price),
                rejection_reasons=list(decision_reasons),
                state=dict(state),
                settings=s,
                portfolio_positions=list(portfolio_positions),
                rollout_policy=dict(sizing_rollout_policy),
                governance_policy=dict(risk_governance_policy),
                pending_entries=_portfolio_slot_reservations(pending_entries),
                realized_returns_by_pair=realized_returns_by_pair,
                quote_rates=dict(cycle_quote_rates),
            )
            position_risk_reapproval_context = {
                "pair": str(pair),
                "ts_value": str(ts_value),
                "side": str(side),
                "signal": signal,
                "expected_edge_bps": float(expected_edge_bps),
                "spread_bps": float(spread_bps),
                "feature_bar": dict(feature_bar),
                "tick": dict(tick),
                "spread_unit_source": str(spread_unit_source),
                "mt4_fresh": bool(mt4_fresh),
                "ticks_fresh": bool(ticks_fresh),
                "paused": bool(paused),
                "positions": list(positions),
                "pair_count": int(pair_count),
                "total_count": int(portfolio_total_count),
                "current_equity": float(current_equity_value),
                "planned_entry_lots": float(planned_entry_lots),
                "rejection_reasons": list(decision_reasons),
                "state": dict(state),
                "portfolio_positions": list(portfolio_positions),
                "rollout_policy": dict(sizing_rollout_policy),
                "governance_policy": dict(risk_governance_policy),
                "pending_entries": _portfolio_slot_reservations(pending_entries),
                "realized_returns_by_pair": realized_returns_by_pair,
                # Flows to BOTH the entry finalization and lifecycle reapproval,
                # which each rebuild their kernel call from this context.
                "quote_rates": dict(cycle_quote_rates),
            }
            approved_order_payload = dict(risk_kernel_out.get("approved_order") or {})
            rollout_meta = dict(risk_kernel_out.get("rollout") or {})
            portfolio_allocation_meta = dict(risk_kernel_out.get("portfolio_allocation") or {})
            capital_governance_meta = dict(risk_kernel_out.get("governance") or {})
            if agent_mode not in {"paper", "live"}:
                rollout_meta["advisory_sizing_unthrottled"] = True
                rollout_meta["execution_rollout_policy"] = dict(execution_rollout_policy)
                rollout_meta["execution_budget_scale"] = float(
                    _clip01(execution_rollout_policy.get("budget_scale", 1.0))
                ) if execution_rollout_policy else 1.0
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
                        "hard_lifecycle_action": str(hard_lifecycle_action),
                        "hard_lifecycle_reason": str(hard_lifecycle_reason),
                        "hard_lifecycle_action_score": float(hard_lifecycle_action_score),
                        "hard_lifecycle_close_lots": float(hard_lifecycle_close_lots),
                        "hard_lifecycle_sl_price": float(hard_lifecycle_sl_price),
                        "lots_open": float(_safe_float(dict(positions[0] or {}).get("lots"), 0.0)) if positions else 0.0,
                        "age_bars": float(_safe_float(lifecycle_row.iloc[0].get("time_in_trade_bars", 0.0), 0.0)),
                        "unrealized_pnl_usd": float(_safe_float(dict(positions[0] or {}).get("profit"), 0.0)) if positions else 0.0,
                        "exit_action_probs": dict(exit_action_probs),
                        "reversal_context_active": bool(reversal_context_active),
                        "reversal_ready": bool(reversal_ready),
                        "reversal_failure_prob": float(reversal_failure_prob),
                        "reversal_opportunity_prob": float(reversal_opportunity_prob),
                        "approved_order": dict(approved_order_payload),
                        "risk_reapproval_context": dict(position_risk_reapproval_context),
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
                        "sl_price": float(sl_price),
                        "tp_price": float(tp_price),
                        "risk_reapproval_context": dict(position_risk_reapproval_context),
                        "portfolio_slot_reserved": False,
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
                        "hard_lifecycle_action": str(hard_lifecycle_action),
                        "hard_lifecycle_reason": str(hard_lifecycle_reason),
                        "hard_lifecycle_action_score": float(hard_lifecycle_action_score),
                        "hard_lifecycle_close_lots": float(hard_lifecycle_close_lots),
                        "hard_lifecycle_sl_price": float(hard_lifecycle_sl_price),
                        "lots_open": float(_safe_float(dict(positions[0] or {}).get("lots"), 0.0)) if positions else 0.0,
                        "age_bars": float(_safe_float(lifecycle_row.iloc[0].get("time_in_trade_bars", 0.0), 0.0)),
                        "unrealized_pnl_usd": float(_safe_float(dict(positions[0] or {}).get("profit"), 0.0)) if positions else 0.0,
                        "exit_action_probs": dict(exit_action_probs),
                        "reversal_context_active": bool(reversal_context_active),
                        "reversal_ready": bool(reversal_ready),
                        "reversal_failure_prob": float(reversal_failure_prob),
                        "reversal_opportunity_prob": float(reversal_opportunity_prob),
                        "approved_order": dict(approved_order_payload),
                        "risk_reapproval_context": dict(position_risk_reapproval_context),
                    }
                )

            if not ready:
                for reason in decision_reasons:
                    rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

            adaptive_snapshot = _adaptive_row_snapshot(
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
                        "calibrated_ev_bps": float(signal.calibrated_ev_bps),
                        "entry_quality_score": float(signal.entry_quality_score),
                        "structure_rescue_active": bool(signal.structure_rescue_active),
                        "fallback_used": bool(signal.fallback_used),
                        "fallback_reason": str(signal.fallback_reason),
                        "decision_source_chain": list(signal.decision_source_chain),
                        "entry_floor_ok": bool(signal.entry_floor_ok),
                        "entry_floor_rejection_reason": str(signal.entry_floor_rejection_reason),
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
                        "adaptive_recovery_reason": str(adaptive_recovery_reason),
                        "position_open": bool(positions),
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
                        "hard_lifecycle_action": str(hard_lifecycle_action),
                        "hard_lifecycle_reason": str(hard_lifecycle_reason),
                        "hard_lifecycle_action_score": float(hard_lifecycle_action_score),
                        "hard_lifecycle_sl_price": float(hard_lifecycle_sl_price),
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
            if bool(getattr(s, "adaptive_execution_enabled", False)):
                pair_history = adaptive_history.setdefault(str(pair).upper(), [])
                max_history = max(16, int(getattr(s, "adaptive_history_bars", 128) or 128))
                _append_adaptive_history(
                    pair_history,
                    adaptive_snapshot,
                    max_history=max_history,
                )
            pair_eval_time_ms[pair] = round((time.perf_counter() - pair_t0) * 1000.0, 3)

        # AGENT FLOW: Direct adaptive policy owns the post-strict evaluator on the same bar.
        adaptive_policy_enabled = bool(getattr(s, "adaptive_execution_enabled", False))
        adaptive_engine_enabled = bool(adaptive_policy_enabled)
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
        adaptive_mode = bool(adaptive_policy_enabled)
        adaptive_policy_diag = {
            "adaptive_policy_enabled": bool(adaptive_policy_enabled),
            "adaptive_candidate_count": 0,
            "adaptive_ranked_count": 0,
            "adaptive_selected_count": 0,
            "adaptive_remaining_slots": max(0, int(getattr(s, "max_total_positions", 0) or 0) - len(list(state.get("positions", []) or []))),
            "adaptive_max_new_entries": 0,
            "adaptive_aggressive_fallback_count": 0,
            "adaptive_rejection_reason_counts": {},
            "adaptive_rejections_by_pair": {},
            "adaptive_playbook_counts": {},
            "adaptive_environment_counts": {},
            "adaptive_dominant_rejection_reason": "",
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
        if adaptive_engine_enabled:
            adaptive_frames = _adaptive_frames_from_history(history=adaptive_history, pairs=pairs)
            if adaptive_frames:
                attach_adaptive_context(
                    adaptive_frames,
                    pairs=sorted(list(adaptive_frames.keys())),
                    settings=s,
                    enabled_playbooks=set(adaptive_playbooks),
                )
                adaptive_rows_by_pair = {
                    str(pair).upper(): dict(frame.iloc[-1].to_dict())
                    for pair, frame in adaptive_frames.items()
                    if not frame.empty
                }
        directional_belief_cycle_diag, directional_belief_metrics = _attach_directional_belief(
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
            position_snapshot_authoritative=bool(positions_snapshot_advanced),
            partial_close_tracker=partial_close_tracker,
        )
        sleeve_health_snapshots = sleeve_tracker.snapshot() if adaptive_engine_enabled else {}
        for decision in decisions:
            meta = dict(decision.get("metadata", {}) or {})
            meta["execution_mode"] = "adaptive_multi_playbook" if adaptive_mode else "strict_live_mirror"
            decision["metadata"] = meta
        if adaptive_mode and pending_position_actions:
            for action in pending_position_actions:
                index = int(action.get("index", -1))
                if index < 0 or index >= len(decisions):
                    continue
                decision = decisions[index]
                meta = dict(decision.get("metadata", {}) or {})
                pair = str(action.get("pair") or meta.get("pair") or decision.get("symbol") or "").upper()
                current_row = dict(adaptive_rows_by_pair.get(pair, {}) or {})
                hard_lifecycle_action = str(action.get("hard_lifecycle_action") or "hold")
                hard_lifecycle_reason = str(action.get("hard_lifecycle_reason") or "")
                hard_lifecycle_action_score = float(
                    _safe_float(action.get("hard_lifecycle_action_score"), 0.0)
                )
                hard_lifecycle_close_lots = float(
                    _safe_float(action.get("hard_lifecycle_close_lots"), 0.0)
                )
                hard_lifecycle_sl_price = float(
                    _safe_float(action.get("hard_lifecycle_sl_price"), 0.0)
                )
                pos_state = adaptive_position_registry.get(pair)
                if pos_state is None:
                    resolved_lifecycle = _resolve_hard_lifecycle_floor(
                        lifecycle_action="hold",
                        lifecycle_reason="adaptive_position_state_missing",
                        lifecycle_action_score=0.0,
                        close_lots=0.0,
                        sl_price=0.0,
                        hard_lifecycle_action=hard_lifecycle_action,
                        hard_lifecycle_reason=hard_lifecycle_reason,
                        hard_lifecycle_action_score=hard_lifecycle_action_score,
                        hard_lifecycle_close_lots=hard_lifecycle_close_lots,
                        hard_lifecycle_sl_price=hard_lifecycle_sl_price,
                    )
                    action["lifecycle_action"] = str(resolved_lifecycle["lifecycle_action"])
                    action["lifecycle_reason"] = str(resolved_lifecycle["lifecycle_reason"])
                    action["lifecycle_action_score"] = float(resolved_lifecycle["lifecycle_action_score"])
                    action["close_lots"] = float(resolved_lifecycle["close_lots"])
                    action["sl_price"] = float(resolved_lifecycle["sl_price"])
                    meta["lifecycle_action"] = str(resolved_lifecycle["lifecycle_action"])
                    meta["lifecycle_reason"] = str(resolved_lifecycle["lifecycle_reason"])
                    meta["hard_lifecycle_applied"] = bool(resolved_lifecycle["hard_lifecycle_applied"])
                    decision["metadata"] = meta
                    _sync_lifecycle_action_payloads(decision=decision, action_item=action)
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
                lifecycle_action = str(adaptive_lifecycle.get("action") or "hold")
                lifecycle_reason = str(adaptive_lifecycle.get("reason") or "adaptive_hold")
                action_probabilities = dict(action.get("exit_action_probs") or {})
                if lifecycle_action == "exit":
                    lifecycle_action_score = max(
                        float(_safe_float(action_probabilities.get("exit"), 0.0)),
                        float(_safe_float(action.get("reversal_failure_prob"), 0.0)),
                        float(_safe_float(action.get("reversal_opportunity_prob"), 0.0)),
                    )
                elif lifecycle_action == "partial_tp":
                    lifecycle_action_score = max(
                        float(_safe_float(action_probabilities.get("partial_tp"), 0.0)),
                        float(_safe_float(action_probabilities.get("reduce"), 0.0)),
                    )
                else:
                    lifecycle_action_score = float(_safe_float(action_probabilities.get("hold"), 0.0))
                close_lots = 0.0
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
                    meta["thesis_id"] = str(campaign_open.thesis_id)
                    meta["campaign_state"] = str(campaign_open.state)
                    meta["campaign_state_reason"] = str(campaign_open.state_reason)
                    meta["campaign_proof_score"] = float(campaign_open.proof_score)
                    meta["campaign_maturity_score"] = float(campaign_open.maturity_score)
                    meta["campaign_reset_quality"] = float(campaign_open.reset_quality)
                    meta["campaign_priority_boost"] = float(campaign_open.priority_boost)
                    meta["campaign_reentry_blocked"] = bool(campaign_open.reentry_blocked)
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
                resolved_lifecycle = _resolve_hard_lifecycle_floor(
                    lifecycle_action=lifecycle_action,
                    lifecycle_reason=lifecycle_reason,
                    lifecycle_action_score=lifecycle_action_score,
                    close_lots=close_lots,
                    sl_price=0.0,
                    hard_lifecycle_action=hard_lifecycle_action,
                    hard_lifecycle_reason=hard_lifecycle_reason,
                    hard_lifecycle_action_score=hard_lifecycle_action_score,
                    hard_lifecycle_close_lots=hard_lifecycle_close_lots,
                    hard_lifecycle_sl_price=hard_lifecycle_sl_price,
                )
                lifecycle_action = str(resolved_lifecycle["lifecycle_action"])
                lifecycle_reason = str(resolved_lifecycle["lifecycle_reason"])
                lifecycle_action_score = float(resolved_lifecycle["lifecycle_action_score"])
                close_lots = float(resolved_lifecycle["close_lots"])
                action["lifecycle_action"] = str(lifecycle_action)
                action["lifecycle_reason"] = str(lifecycle_reason)
                action["lifecycle_action_score"] = float(lifecycle_action_score)
                action["close_lots"] = float(close_lots)
                action["sl_price"] = float(resolved_lifecycle["sl_price"])
                action["playbook"] = str(playbook)
                action["partial_tp_blocked_reason"] = str(partial_tp_blocked_reason)
                action["partial_tp_next_eligible_secs"] = float(partial_tp_next_eligible_secs)
                meta["lifecycle_action"] = str(lifecycle_action)
                meta["lifecycle_reason"] = str(lifecycle_reason)
                meta["lifecycle_action_score"] = float(lifecycle_action_score)
                meta["hard_lifecycle_applied"] = bool(resolved_lifecycle["hard_lifecycle_applied"])
                meta["partial_tp_blocked_reason"] = str(partial_tp_blocked_reason)
                meta["partial_tp_next_eligible_secs"] = float(partial_tp_next_eligible_secs)
                decision["metadata"] = meta
                _sync_lifecycle_action_payloads(decision=decision, action_item=action)

        projected_exit_count = int(sum(1 for item in pending_position_actions if str(item.get("lifecycle_action") or "hold") == "exit"))
        if adaptive_engine_enabled and adaptive_rows_by_pair:
            adaptive_policy_diag = _apply_adaptive_ranking(
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
        pre_entry_sleeve_health_snapshots = sleeve_tracker.snapshot()
        # Two sleeves that win and lose in the same conditions are one bet taken
        # twice: double position risk, no extra edge. Measured on realized
        # closed-trade outcomes, so it is inert until there is history to judge.
        complementarity_snapshot = evaluate_sleeve_complementarity(
            governance_state=sleeve_tracker.export_state(),
            health=pre_entry_sleeve_health_snapshots,
        )
        final_entry_risk_diag = _reapprove_final_entry_intents(
            decisions=decisions,
            pending_entries=pending_entries,
            settings=s,
            sleeve_health_snapshots=pre_entry_sleeve_health_snapshots,
            enforce_sleeve_governance=adaptive_mode,
            complementarity=complementarity_snapshot,
        )
        sleeve_metrics_diag = serialize_sleeve_snapshots(pre_entry_sleeve_health_snapshots)
        allocator_policy_diag = {
            "candidate_count": int(adaptive_policy_diag.get("allocator_candidate_count", 0)),
            "selected_count": int(adaptive_policy_diag.get("allocator_selected_count", 0)),
            "ranked_out_count": int(adaptive_policy_diag.get("allocator_ranked_out_count", 0)),
            "replacement_candidate_count": int(adaptive_policy_diag.get("allocator_replacement_candidate_count", 0)),
            "replacement_exit_count": int(adaptive_policy_diag.get("allocator_replacement_exit_count", 0)),
            "sleeve_candidate_counts": dict(adaptive_policy_diag.get("allocator_sleeve_candidate_counts", {})),
            "sleeve_selected_counts": dict(adaptive_policy_diag.get("allocator_sleeve_selected_counts", {})),
            "sleeve_budget_targets": dict(adaptive_policy_diag.get("allocator_sleeve_budget_targets", {})),
            "sleeve_budget_used": dict(adaptive_policy_diag.get("allocator_sleeve_budget_used", {})),
            "allocator_pair_pressure_avg": float(adaptive_policy_diag.get("allocator_pair_pressure_avg", 0.0)),
            "allocator_pair_pressure_max": float(adaptive_policy_diag.get("allocator_pair_pressure_max", 0.0)),
            "allocator_session_pressure_avg": float(adaptive_policy_diag.get("allocator_session_pressure_avg", 0.0)),
            "allocator_session_pressure_max": float(adaptive_policy_diag.get("allocator_session_pressure_max", 0.0)),
            "allocator_sleeve_pressure_avg": float(adaptive_policy_diag.get("allocator_sleeve_pressure_avg", 0.0)),
            "allocator_sleeve_pressure_max": float(adaptive_policy_diag.get("allocator_sleeve_pressure_max", 0.0)),
            "allocator_correlation_pressure_avg": float(adaptive_policy_diag.get("allocator_correlation_pressure_avg", 0.0)),
            "allocator_correlation_pressure_max": float(adaptive_policy_diag.get("allocator_correlation_pressure_max", 0.0)),
            "allocator_risk_pressure_avg": float(adaptive_policy_diag.get("allocator_risk_pressure_avg", 0.0)),
            "allocator_risk_pressure_max": float(adaptive_policy_diag.get("allocator_risk_pressure_max", 0.0)),
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
        portfolio_session_bucket = str(
            dict(first.get("metadata", {}) or {}).get("session_bucket")
            or first.get("session_bucket")
            or session_bucket_from_ts(first.get("ts") or dict(first.get("metadata", {}) or {}).get("ts_value") or "")
        ).strip().lower()
        portfolio_cycle = evaluate_portfolio_allocation(
            symbol=str(first.get("symbol", pairs[0] if pairs else "")),
            session_bucket=portfolio_session_bucket,
            expected_edge_bps=0.0,
            uncertainty_score=0.0,
            positions=_annotate_positions_with_contract_value(
                list(state.get("positions", []) or []),
                quote_rates=cycle_quote_rates,
                settings=s,
            ),
            pending_entries=_portfolio_slot_reservations(pending_entries),
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
        (
            runtime_rl_checkpoint_path,
            runtime_rl_checkpoint_content_sha256,
        ) = _resolve_runtime_rl_checkpoint(
            model_sets=model_sets,
            project_root=Path(s.project_root),
        )
        rl_portfolio_proposal = build_portfolio_rl_proposal_bundle(
            ts=str(first.get("ts") or first.get("ts_value") or ""),
            decisions=list(decisions),
            portfolio=dict(portfolio_cycle_diag),
            policy_context={
                "runtime_mode": str(getattr(s, "strategy_engine_mode", "supervised_legacy") or "supervised_legacy"),
                "supervised_fallback_required": bool(getattr(s, "rl_supervised_fallback_required", True)),
                "allocator_enabled": bool(getattr(s, "use_portfolio_ranking", True)),
                "adaptive_policy_enabled": bool(adaptive_policy_enabled),
            },
            checkpoint_path=runtime_rl_checkpoint_path,
            checkpoint_content_sha256=runtime_rl_checkpoint_content_sha256,
            supervised_fallback_required=bool(getattr(s, "rl_supervised_fallback_required", True)),
        ).to_dict()
        rl_lifecycle_diag = _apply_rl_lifecycle_router(
            decisions=decisions,
            pending_position_actions=pending_position_actions,
            rl_portfolio_proposal=rl_portfolio_proposal,
            settings=s,
        )
        lifecycle_materialization_diag = _materialize_final_position_actions(
            decisions=decisions,
            pending_position_actions=pending_position_actions,
            partial_close_tracker=partial_close_tracker,
            loop_ts=float(loop_ts),
            settings=s,
        )
        final_lifecycle_risk_diag = _reapprove_final_position_actions(
            decisions=decisions,
            pending_position_actions=pending_position_actions,
            settings=s,
        )
        orchestration_records, orchestration_diag = _capture_orchestration_cycle(
            decisions=decisions,
            pending_entries=pending_entries,
            pending_position_actions=pending_position_actions,
            svc=svc,
            settings=s,
            loop_ts=float(loop_ts),
            state=dict(state or {}),
            portfolio_state=dict(portfolio_cycle_diag),
            governance=dict(governance or {}),
            model_sets=model_sets,
        )
        for idx, decision in enumerate(decisions):
            orchestration = dict(orchestration_records.get(int(idx), {}) or {})
            if not orchestration:
                continue
            meta = dict(decision.get("metadata", {}) or {})
            meta["orchestration_shadow"] = {
                "enabled": bool(orchestration.get("enabled", False)),
                "baseline_action": dict(
                    orchestration.get("baseline_action_payload")
                    or {
                        "action": str(orchestration.get("baseline_action") or meta.get("lifecycle_action") or ("enter" if bool(decision.get("execution_ready", False)) else "no_trade")),
                        "side": str(decision.get("side") or meta.get("position_side") or ""),
                    }
                ),
                "shadow_action": dict(orchestration.get("shadow_action_payload") or {"action": str(orchestration.get("shadow_action") or "")}),
                "divergence_reason": str(orchestration.get("divergence_reason") or ""),
                "blocking_reasons": list(orchestration.get("blocking_reasons") or []),
                "proposal_votes": dict(orchestration.get("proposal_votes") or {}),
                "run_id": str(orchestration.get("run_id") or ""),
                "trace_id": str(orchestration.get("trace_id") or ""),
                "correlation_id": str(orchestration.get("correlation_id") or ""),
                "thread_id": str(orchestration.get("thread_id") or ""),
                "fault_classification": str(orchestration.get("fault_classification") or ""),
                "latency_ms": int(_safe_float(orchestration.get("latency_ms"), 0.0)),
                "governed_decision": {
                    "selected_action": str(orchestration.get("governed_selected_action") or ""),
                    "allowed": bool(orchestration.get("governed_allowed", False)),
                    "approval_state": str(orchestration.get("approval_state") or "auto"),
                    "blocking_reasons": list(dict(orchestration.get("governed_decision") or {}).get("blocking_reasons") or orchestration.get("blocking_reasons") or []),
                },
                "committee": {
                    "winning_agent": str(dict(orchestration.get("committee_summary") or {}).get("winning_agent") or ""),
                    "winning_proposal_id": str(orchestration.get("winning_proposal_id") or dict(orchestration.get("committee_summary") or {}).get("winning_proposal_id") or ""),
                    "winning_score": float(_safe_float(dict(orchestration.get("committee_summary") or {}).get("winning_score"), 0.0)),
                    "arbiter_stage": str(orchestration.get("arbiter_stage") or dict(orchestration.get("committee_summary") or {}).get("arbiter_stage") or ""),
                    "rationale": str(orchestration.get("arbiter_rationale") or dict(orchestration.get("committee_summary") or {}).get("rationale") or ""),
                    "blocking_reasons": list(orchestration.get("blocking_reasons") or []),
                    "top_ranked_proposals": list(dict(orchestration.get("committee_summary") or {}).get("top_ranked_proposals") or []),
                },
            }
            decision["metadata"] = meta
        for item in pending_position_actions:
            idx = int(item.get("index", -1))
            if idx in orchestration_records:
                item["orchestration"] = dict(orchestration_records[idx])
        for item in pending_entries:
            idx = int(item.get("index", -1))
            if idx in orchestration_records:
                item["orchestration"] = dict(orchestration_records[idx])

        # AGENT HANDSHAKE: Exits/partials are submitted before entries; the resulting diagnostics are folded into the state patch consumed by bridge and dashboard clients.
        position_action_diag = _submit_position_actions(
            decisions=decisions,
            pending_position_actions=pending_position_actions,
            svc=svc,
            settings=s,
            runtime_state=dict(state or {}),
            last_action_key=last_action_key,
            partial_close_tracker=partial_close_tracker,
            adaptive_position_registry=adaptive_position_registry,
            adaptive_recent_exit_registry=adaptive_recent_exit_registry,
            pair_bar_index=adaptive_bar_index_by_pair,
            loop_ts=float(loop_ts),
            campaign_registry=campaign_registry,
            campaign_transition_counts=campaign_transition_counts,
            campaign_config=campaign_config,
            exit_command_ledger=exit_command_ledger,
        )
        entry_sleeve_health_snapshots = sleeve_tracker.snapshot()
        sleeve_metrics_diag = serialize_sleeve_snapshots(entry_sleeve_health_snapshots)
        entry_execution_diag = _finalize_entry_submissions(
            decisions=decisions,
            pending_entries=pending_entries,
            svc=svc,
            last_action_key=last_action_key,
            settings=s,
            runtime_state=dict(state or {}),
            rl_portfolio_proposal=rl_portfolio_proposal,
            adaptive_pending_entry_registry=adaptive_pending_entry_registry,
            current_equity=float(current_equity_value),
            sleeve_health_snapshots=entry_sleeve_health_snapshots,
            enforce_sleeve_governance=adaptive_mode,
        )
        entry_execution_diag["final_entry_risk"] = dict(final_entry_risk_diag)
        entry_execution_diag.update(position_action_diag)
        entry_execution_diag.update(partial_reconciliation_diag)
        entry_execution_diag.update(exit_reconciliation_diag)
        entry_execution_diag.update(rl_lifecycle_diag)
        entry_execution_diag["lifecycle_materialization"] = dict(
            lifecycle_materialization_diag
        )
        entry_execution_diag["final_lifecycle_risk"] = dict(final_lifecycle_risk_diag)
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
                status="ok" if str(provider_roles.get("execution_provider") or "").strip().lower() == "paper" or bool(mt4_fresh) else "degraded",
                shadow_only=bool(str(provider_roles.get("execution_provider") or "").strip().lower() != "mt4"),
                provenance="runtime_service",
                details={
                    "paused": bool(paused),
                    "entries_only": bool(governance_entries_only),
                    "execution_provider": str(provider_roles.get("execution_provider") or ""),
                },
            ).to_dict(),
        }
        portfolio_cycle_diag["rl_portfolio_proposal"] = dict(rl_portfolio_proposal)
        monitor_entry = {"symbol": str(first.get("symbol", "N/A")), "side": str(first.get("side", "N/A"))}
        orchestration_phase1_diag, orchestration_shadow_diag = _build_orchestration_snapshot_payload(
            orchestration_diag=orchestration_diag,
            records_by_index=orchestration_records,
            phase2_sections={
                "adaptive_policy": adaptive_policy_diag,
                "allocator_policy": allocator_policy_diag,
                "portfolio_intelligence": portfolio_cycle_diag,
                "campaign_policy": campaign_policy_diag,
                "campaign_cycle_summary": campaign_cycle_diag,
                "directional_belief_policy": directional_belief_policy_diag,
                "directional_belief_cycle_summary": directional_belief_cycle_diag,
                "directional_belief_metrics": directional_belief_metrics,
                "overlay_cycle_summary": adaptive_policy_diag.get("overlay_cycle_summary", {}),
                "desk_overlay_cycle_summary": adaptive_policy_diag.get("overlay_cycle_summary", {}),
                "rollout_policy": rollout_policy_diag,
                "risk_cycle_summary": risk_cycle_diag,
                "capital_governance": capital_governance,
                "sleeve_metrics": sleeve_metrics_diag,
                "sleeve_complementarity": complementarity_snapshot.to_dict(),
                "entry_execution_policy": entry_execution_diag,
            },
        )
        orchestration_live_diag = _build_orchestration_live_runtime_diag(
            state=dict(state or {}),
            settings=s,
            orchestration_diag=orchestration_shadow_diag,
            entry_execution_diag=entry_execution_diag,
            risk_cycle_diag=risk_cycle_diag,
        )
        managed_position_state = _serialize_managed_position_state(
            adaptive_position_registry=adaptive_position_registry,
            partial_close_tracker=partial_close_tracker,
            campaign_registry=campaign_registry,
            saved_at=float(loop_ts),
            adaptive_pending_entry_registry=adaptive_pending_entry_registry,
            adaptive_recent_exit_registry=adaptive_recent_exit_registry,
            exit_command_ledger=exit_command_ledger,
            sleeve_governance_state=sleeve_tracker.export_state(),
        )
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
            **_feature_serving_runtime_diag(),
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
                feature_serving_by_pair=_feature_serving_runtime_diag()["feature_serving_by_pair"],
                symbol_readiness=symbol_readiness,
                model_load_diag=model_load_diag,
            ),
            "activation_consistency": dict(activation_consistency),
            "live_command_admission": dict(
                current_live_command_admission
            ),
            "release_authority": dict(release_authority_diag),
            "runtime_attestation": dict(runtime_attestation),
            "manifest_seed": dict(manifest_seed_diag),
            "adaptive_history": {
                "timeframe": str(intraday_timeframe),
                "configured_bars": max(16, int(getattr(s, "adaptive_history_bars", 128) or 128)),
                "unique_bars_by_pair": {
                    str(pair).upper(): int(len(adaptive_history.get(str(pair).upper(), [])))
                    for pair in pairs
                },
                "oldest_bar_ts_by_pair": {
                    str(pair).upper(): str((adaptive_history.get(str(pair).upper(), [{}]) or [{}])[0].get("ts") or "")
                    for pair in pairs
                },
                "newest_bar_ts_by_pair": {
                    str(pair).upper(): str((adaptive_history.get(str(pair).upper(), [{}]) or [{}])[-1].get("ts") or "")
                    for pair in pairs
                },
                "source": "feature_store_then_live_distinct_bars",
            },
            "adaptive_policy": dict(adaptive_policy_diag),
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
            "overlay_cycle_summary": dict(adaptive_policy_diag.get("overlay_cycle_summary", {})),
            "desk_overlay_cycle_summary": dict(adaptive_policy_diag.get("overlay_cycle_summary", {})),
            "rollout_policy": dict(rollout_policy_diag),
            "canary_rollout_policy": dict(rollout_policy_diag),
            "risk_cycle_summary": dict(risk_cycle_diag),
            "rollout_summary": dict(risk_cycle_diag.get("rollout") or {}),
            "canary_rollout_summary": dict(risk_cycle_diag.get("rollout") or {}),
            "capital_governance": dict(capital_governance),
            "sleeve_metrics": dict(sleeve_metrics_diag),
            "entry_execution_policy": dict(entry_execution_diag),
            "managed_position_recovery": dict(managed_position_recovery_diag),
            "managed_position_state": dict(managed_position_state),
            "orchestration_shadow": dict(orchestration_shadow_diag),
            "orchestration_live": dict(orchestration_live_diag),
        }

        state_patch: dict[str, Any] = {
            "__expected_orchestration_live_authority__": dict(
                dict(dict(state or {}).get("runtime_diag") or {}).get(
                    "orchestration_live"
                )
                or {}
            ),
            "runtime_profile": str(s.policy_version),
            "runtime_last_cycle_ts": float(loop_ts),
            "runtime_status": "running" if runtime_running else "starting",
            "runtime_equity_seed": float(equity),
            "equity_peak": float(risk_equity_peak),
            "equity_drawdown_pct": float(equity_drawdown_pct),
            "equity_peak_reset_policy": "persistent_until_explicit_state_reset",
            "runtime_diag": runtime_diag,
            "runtime_startup": dict(startup_state),
            "runtime_boot_id": str(runtime_boot_id),
            "runtime_attestation": dict(runtime_attestation),
            "monitor": {
                "entry": monitor_entry,
                "close": {"dominant_close_reason": "none"},
            },
        }
        if governance_enabled:
            state_patch["governance"] = dict(capital_governance)
        # AGENT STATE: The runtime patch is the bridge truth for ops, dashboard, and actual-runtime validation.
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
                "orchestration": dict(orchestration_phase1_diag),
                "orchestration_shadow": dict(orchestration_shadow_diag),
                "orchestration_live": dict(orchestration_live_diag),
                "runtime_diag": runtime_diag,
            },
        )

        startup_state = _touch_runtime_loop_progress(svc=svc, startup_state=startup_state)
        if not main_loop_ready_logged:
            _startup_log("main_loop_ready")
            main_loop_ready_logged = True

        time.sleep(max(1, int(sleep_secs)))


def _require_baseline_instance_id(instance_id: object) -> str:
    value = str(instance_id or "")
    if value != "baseline":
        raise SystemExit(
            "runtime_instance_quarantined: production admits only --instance-id baseline; "
            "run candidate validation on an external isolated host or VM"
        )
    return value


def main() -> None:
    ap = argparse.ArgumentParser(description="Run fxstack runtime loop")
    ap.add_argument("--config", default="")
    ap.add_argument("--equity", type=float, required=True)
    ap.add_argument("--sleep", type=int, default=10)
    ap.add_argument("--feature-root", default="fx-quant-stack/data/features")
    ap.add_argument("--instance-root", default="", help=argparse.SUPPRESS)
    ap.add_argument("--instance-id", default="baseline", help=argparse.SUPPRESS)
    _ = ap.parse_args()
    _require_baseline_instance_id(_.instance_id)

    run_loop(equity=_.equity, sleep_secs=_.sleep, feature_root=_.feature_root)


if __name__ == "__main__":
    main()
