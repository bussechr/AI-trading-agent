"""Phase 3 orchestration replay helpers for offline causal research."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from fxstack.settings import get_settings


PHASE3_REPLAY_SCHEMA_VERSION = "fxstack.orchestration.phase3.research.v1"
DEFAULT_PROFILE_PATH = "fx-quant-stack/config/orchestration_replay_profiles.json"
REPO_ROOT = Path(__file__).resolve().parents[4]
OFFLINE_SOURCE_KINDS = frozenset({"capture_dir", "immutable_bundle"})
FORBIDDEN_SOURCE_FIELDS = frozenset(
    {
        "bridge_url",
        "database_url",
        "live_api_key",
    }
)
REPLAY_PROFILE_FIELDS = frozenset(
    {
        "feature_contract_id",
        "feature_root",
        "orchestration_source",
        "pairs",
        "profile_id",
        "reduce_fraction",
        "research_manifest_path",
        "research_thresholds",
        "research_validation_limit",
        "seed",
        "slippage_bps",
        "start_equity",
        "windows",
    }
)


@dataclass(slots=True)
class ResearchThresholds:
    entry_ratio_floor: float
    slot_utilisation_floor: float
    trace_completeness_floor: float
    action_overlap_floor: float
    decision_divergence_rate_ceiling: float
    max_drawdown_deterioration_pct: float


@dataclass(slots=True)
class ReplayWindow:
    window_id: str
    start_ts: str
    end_ts: str


@dataclass(slots=True)
class ReplayProfile:
    profile_id: str
    pairs: list[str]
    feature_contract_id: str
    feature_root: str
    research_manifest_path: str
    start_equity: float
    slippage_bps: float
    seed: int
    reduce_fraction: float
    research_validation_limit: int
    orchestration_source: dict[str, Any]
    thresholds: ResearchThresholds
    windows: dict[str, ReplayWindow]
    metadata: dict[str, Any]


@dataclass(slots=True)
class OrchestrationCycle:
    pair: str
    ts: str
    feature_contract_id: str
    context_source: str
    trace_complete: bool
    decision_seed: int
    run_id: str
    trace_id: str
    baseline_action_class: str
    orchestrated_action_class: str
    governor_outcome: str
    divergence_reason: str
    blocking_reasons: list[str]
    latency_ms: float
    proposal_votes: dict[str, Any]
    proposals: list[dict[str, Any]]
    fallback_used: bool
    packet: dict[str, Any]
    trace: dict[str, Any]
    winning_proposal_id: str = ""
    winning_agent: str = ""
    arbiter_stage: str = ""
    arbiter_rationale: str = ""

    def to_row(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "ts": self.ts,
            "feature_contract_id": self.feature_contract_id,
            "context_source": self.context_source,
            "trace_complete": bool(self.trace_complete),
            "decision_seed": int(self.decision_seed),
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "baseline_action_class": self.baseline_action_class,
            "orchestrated_action_class": self.orchestrated_action_class,
            "governor_outcome": self.governor_outcome,
            "divergence_reason": self.divergence_reason,
            "blocking_reasons": list(self.blocking_reasons),
            "latency_ms": float(self.latency_ms),
            "fallback_used": bool(self.fallback_used),
            "winning_proposal_id": str(self.winning_proposal_id),
            "winning_agent": str(self.winning_agent),
            "arbiter_stage": str(self.arbiter_stage),
        }


def _load_research_tool() -> Any:
    tool_path = REPO_ROOT / "tools" / "fxstack_causal_research_backtest.py"
    spec = importlib.util.spec_from_file_location("fxstack_causal_research_backtest_phase3", tool_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load causal research tool at {tool_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    required_api = ("_run_research_once", "run_research_backtest")
    missing_api = [name for name in required_api if not callable(getattr(module, name, None))]
    if missing_api:
        raise RuntimeError(
            "causal research tool is missing required API: " + ", ".join(missing_api)
        )
    return module


def _seed_everything(seed: int) -> None:
    random.seed(int(seed))
    np.random.seed(int(seed))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    if math.isnan(out) or math.isinf(out):
        return float(default)
    return float(out)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    txt = str(value).strip().lower()
    if txt in {"1", "true", "yes", "y", "on"}:
        return True
    if txt in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(value)


def _utc_iso(value: Any) -> str:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"invalid timestamp: {value}")
    return pd.Timestamp(ts).isoformat()


def _utc_epoch(value: Any) -> float:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"invalid timestamp: {value}")
    return float(pd.Timestamp(ts).timestamp())


def _window_bounds(window: ReplayWindow) -> tuple[float, float]:
    return _utc_epoch(window.start_ts), _utc_epoch(window.end_ts)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_side(value: Any) -> str:
    txt = str(value or "").strip().upper()
    if txt in {"BUY", "LONG"}:
        return "BUY"
    if txt in {"SELL", "SHORT"}:
        return "SELL"
    return "FLAT"


def _normalize_action_class(
    *,
    action: dict[str, Any] | None = None,
    selected_action: str = "",
    command_preview: dict[str, Any] | None = None,
    allowed: Any | None = None,
    side: Any = "",
    lifecycle_action: Any = "",
    position_side: Any = "",
) -> str:
    payload = dict(action or {})
    preview = dict(command_preview or {})
    action_key = str(
        payload.get("action")
        or payload.get("intent")
        or selected_action
        or preview.get("action")
        or preview.get("intent")
        or preview.get("cmd")
        or ""
    ).strip().lower()
    side_key = _normalize_side(payload.get("side") or preview.get("side") or preview.get("cmd") or side)
    lifecycle_key = str(lifecycle_action or "").strip().lower()
    position_key = _normalize_side(position_side)
    if lifecycle_key in {"exit", "close"}:
        return "exit"
    if lifecycle_key in {"reduce", "partial_tp", "partial", "trim"}:
        return "reduce"
    if action_key in {"buy", "enter_buy"}:
        return "enter_buy"
    if action_key in {"sell", "enter_sell"}:
        return "enter_sell"
    if action_key in {"enter", "entry", "open"}:
        return "enter_buy" if side_key == "BUY" else "enter_sell" if side_key == "SELL" else "no_trade"
    if action_key in {"exit", "close", "flat", "close_position"}:
        return "exit"
    if action_key in {"reduce", "partial_tp", "partial", "trim", "resize_down"}:
        return "reduce"
    if action_key in {"hold", "wait"}:
        return "hold"
    if action_key in {"no_trade", "blocked", "disable", "disabled"}:
        return "no_trade"
    if allowed is not None and _safe_bool(allowed):
        return "enter_buy" if side_key == "BUY" else "enter_sell" if side_key == "SELL" else "hold"
    if position_key in {"BUY", "SELL"}:
        return "hold"
    return "no_trade"


def normalize_research_decision_row(row: dict[str, Any]) -> str:
    return _normalize_action_class(
        allowed=row.get("allowed"),
        side=row.get("side"),
        lifecycle_action=row.get("lifecycle_action"),
        position_side=row.get("position_side"),
    )


def load_replay_profile(config_path: str | Path) -> ReplayProfile:
    path = Path(config_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    profiles = dict(payload.get("profiles") or {})
    if profiles:
        default_profile = str(payload.get("default_profile") or "")
        selected = dict(profiles.get(default_profile) or next(iter(profiles.values())))
        profile_id = str(default_profile or next(iter(profiles.keys())))
    else:
        selected = dict(payload.get("profile") or payload)
        profile_id = str(selected.get("profile_id") or "default")

    unsupported_fields = sorted(set(selected).difference(REPLAY_PROFILE_FIELDS))
    if unsupported_fields:
        raise ValueError(
            "offline orchestration research profile contains unsupported fields: "
            + ", ".join(unsupported_fields)
        )
    legacy_fields = sorted(FORBIDDEN_SOURCE_FIELDS.intersection(selected))
    if legacy_fields:
        raise ValueError(
            "offline orchestration research profiles forbid live connection fields: "
            + ", ".join(legacy_fields)
        )

    orchestration_source = dict(selected.get("orchestration_source") or {})
    _offline_source_spec(orchestration_source)
    _offline_local_path(selected.get("feature_root"), field_name="feature_root")
    _offline_local_path(selected.get("research_manifest_path"), field_name="research_manifest_path")
    research_validation_limit = int(selected.get("research_validation_limit", 500))
    if research_validation_limit < 1:
        raise ValueError("research_validation_limit must be at least 1")

    thresholds_payload = dict(selected.get("research_thresholds") or {})
    threshold_fields = {
        "action_overlap_floor",
        "decision_divergence_rate_ceiling",
        "entry_ratio_floor",
        "max_drawdown_deterioration_pct",
        "slot_utilisation_floor",
        "trace_completeness_floor",
    }
    unsupported_thresholds = sorted(set(thresholds_payload).difference(threshold_fields))
    if unsupported_thresholds:
        raise ValueError(
            "offline orchestration research profile contains unsupported research thresholds: "
            + ", ".join(unsupported_thresholds)
        )
    max_drawdown_deterioration_pct = thresholds_payload.get("max_drawdown_deterioration_pct")
    if max_drawdown_deterioration_pct is None:
        raise ValueError("research_thresholds.max_drawdown_deterioration_pct must be set explicitly")
    windows = {
        str(window_id): ReplayWindow(
            window_id=str(window_id),
            start_ts=str(window_payload.get("start_ts") or ""),
            end_ts=str(window_payload.get("end_ts") or ""),
        )
        for window_id, window_payload in dict(selected.get("windows") or {}).items()
    }
    if not windows:
        raise ValueError("at least one replay window must be defined")
    return ReplayProfile(
        profile_id=profile_id,
        pairs=[str(pair).upper() for pair in list(selected.get("pairs") or [])],
        feature_contract_id=str(selected.get("feature_contract_id") or ""),
        feature_root=str(selected.get("feature_root") or ""),
        research_manifest_path=str(selected.get("research_manifest_path") or ""),
        start_equity=float(selected.get("start_equity", 10_000.0)),
        slippage_bps=float(selected.get("slippage_bps", 0.25)),
        seed=int(selected.get("seed", 42)),
        reduce_fraction=float(selected.get("reduce_fraction", 0.5)),
        research_validation_limit=research_validation_limit,
        orchestration_source=orchestration_source,
        thresholds=ResearchThresholds(
            entry_ratio_floor=float(thresholds_payload.get("entry_ratio_floor", 0.90)),
            slot_utilisation_floor=float(thresholds_payload.get("slot_utilisation_floor", 0.90)),
            trace_completeness_floor=float(thresholds_payload.get("trace_completeness_floor", 0.99)),
            action_overlap_floor=float(thresholds_payload.get("action_overlap_floor", 0.95)),
            decision_divergence_rate_ceiling=float(thresholds_payload.get("decision_divergence_rate_ceiling", 0.05)),
            max_drawdown_deterioration_pct=float(max_drawdown_deterioration_pct),
        ),
        windows=windows,
        metadata={
            "schema_version": str(payload.get("schema_version") or ""),
            "raw_profile": selected,
        },
    )


def _offline_local_path(value: Any, *, field_name: str) -> Path:
    raw_value = str(value or "").strip()
    normalized = raw_value.replace("\\", "/")
    if not raw_value:
        raise ValueError(f"{field_name} must be an explicit local offline path")
    if "://" in normalized or normalized.startswith("//"):
        raise ValueError(f"{field_name} must not be a URL or network path")
    path = Path(raw_value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def _offline_source_spec(source: dict[str, Any]) -> tuple[str, Path]:
    forbidden_fields = sorted(FORBIDDEN_SOURCE_FIELDS.intersection(source))
    if forbidden_fields:
        raise ValueError(
            "offline orchestration research sources forbid live connection fields: "
            + ", ".join(forbidden_fields)
        )
    unsupported_fields = sorted(set(source).difference({"kind", "path"}))
    if unsupported_fields:
        raise ValueError(
            "offline orchestration research source contains unsupported fields: "
            + ", ".join(unsupported_fields)
        )
    kind = str(source.get("kind") or "").strip().lower()
    if kind not in OFFLINE_SOURCE_KINDS:
        allowed = ", ".join(sorted(OFFLINE_SOURCE_KINDS))
        raise ValueError(
            f"offline orchestration research source kind must be one of: {allowed}; got {kind or '<missing>'}"
        )
    return kind, _offline_local_path(source.get("path"), field_name="orchestration_source.path")


def _bundle_items(raw: dict[str, Any], key: str) -> list[dict[str, Any]]:
    payload = raw.get(key) or []
    if isinstance(payload, dict):
        payload = payload.get("items") or []
    if not isinstance(payload, list):
        raise ValueError(f"offline orchestration bundle field {key!r} must be a list or items envelope")
    return [dict(item) for item in payload if isinstance(item, dict)]


def load_source_bundle(
    *,
    profile: ReplayProfile,
    window: ReplayWindow,
) -> dict[str, Any]:
    source = dict(profile.orchestration_source or {})
    kind, source_path = _offline_source_spec(source)
    if kind == "capture_dir":
        bundle_path = source_path / "baseline-pack.json"
    else:
        bundle_path = source_path
    if not bundle_path.is_file():
        raise FileNotFoundError(f"offline orchestration bundle not found: {bundle_path}")
    pack = json.loads(bundle_path.read_text(encoding="utf-8"))
    if not isinstance(pack, dict):
        raise ValueError(f"offline orchestration bundle must contain a JSON object: {bundle_path}")
    raw_payload = pack.get("raw") or pack.get("bundle") or pack
    if not isinstance(raw_payload, dict):
        raise ValueError(f"offline orchestration bundle payload must be a JSON object: {bundle_path}")
    raw = dict(raw_payload)
    all_runs = _bundle_items(raw, "orchestration_runs") if "orchestration_runs" in raw else _bundle_items(raw, "runs")
    all_traces = _bundle_items(raw, "orchestration_traces") if "orchestration_traces" in raw else _bundle_items(raw, "traces")
    all_snapshots = _bundle_items(raw, "decision_snapshots") if "decision_snapshots" in raw else _bundle_items(raw, "snapshots")
    start_epoch, end_epoch = _window_bounds(window)

    def _in_window(row: dict[str, Any], field: str) -> bool:
        value = row.get(field)
        try:
            epoch = float(value)
        except Exception:
            epoch = _utc_epoch(value)
        return start_epoch <= epoch <= end_epoch

    eligible_runs = [row for row in all_runs if _in_window(row, "ts_utc")]
    eligible_snapshots = [row for row in all_snapshots if _in_window(row, "ts")]
    limit = max(1, int(profile.research_validation_limit))
    runs = eligible_runs[:limit]
    run_ids = {
        str(row.get("run_id") or "")
        for row in runs
        if str(row.get("run_id") or "").strip()
    }
    traces = [row for row in all_traces if not run_ids or str(row.get("run_id") or "") in run_ids][:limit]
    snapshots = eligible_snapshots[:limit]
    return {
        "runs": runs,
        "traces": traces,
        "snapshots": snapshots,
        "state": dict(raw.get("state") or {}),
        "source_kind": kind,
        "source_path": str(bundle_path),
        "research_validation_limit": limit,
        "source_item_counts": {
            "runs": len(all_runs),
            "traces": len(all_traces),
            "snapshots": len(all_snapshots),
        },
        "eligible_item_counts": {
            "runs": len(eligible_runs),
            "snapshots": len(eligible_snapshots),
        },
        "loaded_item_counts": {
            "runs": len(runs),
            "traces": len(traces),
            "snapshots": len(snapshots),
        },
    }


def _extract_feature_contract_id(
    *,
    default_feature_contract_id: str,
    packet: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    snapshot_meta: dict[str, Any] | None = None,
    state_snapshot: dict[str, Any] | None = None,
) -> str:
    candidates = [
        dict(packet or {}).get("feature_contract_id"),
        dict(packet or {}).get("feature_service"),
        dict(snapshot_meta or {}).get("feature_contract_id"),
        dict(snapshot_meta or {}).get("feature_contract"),
        dict(snapshot_meta or {}).get("feature_service"),
        dict(snapshot_meta or {}).get("feature_refs", {}).get("feature_contract_id"),
        dict(snapshot_meta or {}).get("feature_refs", {}).get("feature_service"),
        dict(trace or {}).get("feature_contract_id"),
        dict(state_snapshot or {}).get("feature_contract_id"),
    ]
    for candidate in candidates:
        txt = str(candidate or "").strip()
        if txt:
            return txt
    return str(default_feature_contract_id or "")


def build_orchestration_cycles(
    *,
    profile: ReplayProfile,
    window: ReplayWindow,
    bundle: dict[str, Any],
    seed: int,
) -> tuple[list[OrchestrationCycle], dict[str, Any]]:
    start_epoch, end_epoch = _window_bounds(window)
    trace_by_run = {
        str(row.get("run_id") or ""): dict(row.get("trace_json") or row.get("trace") or {})
        for row in list(bundle.get("traces") or [])
    }
    trace_by_id = {
        str(dict(row.get("trace_json") or row.get("trace") or {}).get("trace_id") or row.get("trace_id") or ""): dict(
            row.get("trace_json") or row.get("trace") or {}
        )
        for row in list(bundle.get("traces") or [])
    }
    cycles_by_key: dict[tuple[str, str], OrchestrationCycle] = {}
    snapshot_keys: set[tuple[str, str]] = set()
    state_snapshot = dict(bundle.get("state") or {})

    for row in list(bundle.get("runs") or []):
        pair = str(row.get("pair") or "").upper().strip()
        if pair not in profile.pairs:
            continue
        ts_value = row.get("ts_utc")
        try:
            ts_epoch = float(ts_value)
        except Exception:
            ts_epoch = _utc_epoch(ts_value)
        if ts_epoch < start_epoch or ts_epoch > end_epoch:
            continue
        packet = dict(row.get("packet_json") or row.get("packet") or {})
        trace = trace_by_run.get(str(row.get("run_id") or ""), {})
        ts = _utc_iso(packet.get("ts_utc") or ts_epoch)
        governed = dict(packet.get("governed_decision") or {})
        ranked_ids = list(packet.get("ranked_proposal_ids") or governed.get("ranked_proposal_ids") or [])
        score_path = list(packet.get("score_path") or governed.get("score_path") or [])
        winning_proposal_id = str(packet.get("winning_proposal_id") or governed.get("winning_proposal_id") or "")
        winning_agent = ""
        if winning_proposal_id:
            winning_agent = str(
                next(
                    (
                        item.get("agent_id")
                        for item in list(score_path or [])
                        if str(item.get("proposal_id") or "") == winning_proposal_id
                    ),
                    "",
                )
            )
        key = (pair, ts)
        cycles_by_key[key] = OrchestrationCycle(
            pair=pair,
            ts=ts,
            feature_contract_id=_extract_feature_contract_id(
                default_feature_contract_id=profile.feature_contract_id,
                packet=packet,
                trace=trace,
                state_snapshot=state_snapshot,
            ),
            context_source="persisted",
            trace_complete=bool(packet and trace),
            decision_seed=int(seed),
            run_id=str(row.get("run_id") or ""),
            trace_id=str(trace.get("trace_id") or packet.get("trace_id") or ""),
            baseline_action_class=_normalize_action_class(action=dict(packet.get("baseline_action") or {})),
            orchestrated_action_class=_normalize_action_class(
                action=dict(packet.get("shadow_action") or {}),
                selected_action=str(governed.get("selected_action") or ""),
                command_preview=dict(governed.get("command_preview") or {}),
            ),
            governor_outcome=_normalize_action_class(
                selected_action=str(governed.get("selected_action") or ""),
                command_preview=dict(governed.get("command_preview") or {}),
                action=dict(packet.get("shadow_action") or {}),
            ),
            divergence_reason=str(packet.get("divergence_reason") or ""),
            blocking_reasons=list(governed.get("blocking_reasons") or []),
            latency_ms=float(_safe_float(packet.get("latency_ms"), 0.0)),
            proposal_votes=dict(packet.get("proposal_votes") or {}),
            proposals=list(packet.get("proposals") or []),
            fallback_used=bool(packet.get("fallback_used", False)),
            packet=packet,
            trace=trace,
            winning_proposal_id=winning_proposal_id,
            winning_agent=winning_agent,
            arbiter_stage=str(packet.get("arbiter_stage") or governed.get("arbiter_stage") or ""),
            arbiter_rationale=str(packet.get("arbiter_rationale") or governed.get("arbiter_rationale") or ""),
        )

    for row in list(bundle.get("snapshots") or []):
        snapshot_ts = row.get("ts")
        try:
            snapshot_epoch = float(snapshot_ts)
        except Exception:
            snapshot_epoch = _utc_epoch(snapshot_ts)
        if snapshot_epoch < start_epoch or snapshot_epoch > end_epoch:
            continue
        decisions = list(row.get("decisions_json") or [])
        for decision in decisions:
            metadata = dict(decision.get("metadata") or {})
            pair = str(metadata.get("pair") or decision.get("symbol") or "").upper().strip()
            if pair not in profile.pairs:
                continue
            orchestration_shadow = dict(metadata.get("orchestration_shadow") or metadata.get("orchestrationShadow") or {})
            if not orchestration_shadow:
                continue
            ts = _utc_iso(metadata.get("ts") or snapshot_epoch)
            key = (pair, ts)
            snapshot_keys.add(key)
            if key in cycles_by_key:
                continue
            trace_id = str(orchestration_shadow.get("trace_id") or "")
            run_id = str(orchestration_shadow.get("run_id") or "")
            trace = dict(trace_by_run.get(run_id) or trace_by_id.get(trace_id) or {})
            shadow_action = dict(orchestration_shadow.get("shadow_action") or {})
            committee = dict(orchestration_shadow.get("committee") or {})
            cycles_by_key[key] = OrchestrationCycle(
                pair=pair,
                ts=ts,
                feature_contract_id=_extract_feature_contract_id(
                    default_feature_contract_id=profile.feature_contract_id,
                    snapshot_meta=metadata,
                    trace=trace,
                    state_snapshot=state_snapshot,
                ),
                context_source="reconstructed",
                trace_complete=bool(trace),
                decision_seed=int(seed),
                run_id=run_id,
                trace_id=trace_id,
                baseline_action_class=_normalize_action_class(action=dict(orchestration_shadow.get("baseline_action") or {})),
                orchestrated_action_class=_normalize_action_class(action=shadow_action),
                governor_outcome=_normalize_action_class(action=shadow_action),
                divergence_reason=str(orchestration_shadow.get("divergence_reason") or ""),
                blocking_reasons=list(orchestration_shadow.get("blocking_reasons") or []),
                latency_ms=float(_safe_float(orchestration_shadow.get("latency_ms"), 0.0)),
                proposal_votes=dict(orchestration_shadow.get("proposal_votes") or {}),
                proposals=[],
                fallback_used=bool(orchestration_shadow.get("fault_classification")),
                packet={},
                trace=trace,
                winning_proposal_id=str(committee.get("winning_proposal_id") or ""),
                winning_agent=str(committee.get("winning_agent") or ""),
                arbiter_stage=str(committee.get("arbiter_stage") or ""),
                arbiter_rationale=str(committee.get("rationale") or ""),
            )

    cycles = sorted(cycles_by_key.values(), key=lambda item: (item.ts, item.pair))
    seen_feature_contracts = {item.feature_contract_id for item in cycles if str(item.feature_contract_id).strip()}
    if seen_feature_contracts and any(contract != profile.feature_contract_id for contract in seen_feature_contracts):
        raise RuntimeError(
            f"window {window.window_id} mixes feature contracts: expected {profile.feature_contract_id}, saw {sorted(seen_feature_contracts)}"
        )
    reconstruction_summary = {
        "cycle_count": int(len(cycles)),
        "persisted_count": int(sum(1 for item in cycles if item.context_source == "persisted")),
        "reconstructed_count": int(sum(1 for item in cycles if item.context_source == "reconstructed")),
        "trace_complete_count": int(sum(1 for item in cycles if item.trace_complete)),
        "source_kind": str(bundle.get("source_kind") or ""),
        "snapshot_overlap_valid": bool(all((item.pair, item.ts) in snapshot_keys for item in cycles if item.context_source == "persisted")),
    }
    return cycles, reconstruction_summary


def aggregate_proposal_votes(cycles: Iterable[OrchestrationCycle]) -> dict[str, Any]:
    by_pair: dict[str, Counter[str]] = defaultdict(Counter)
    by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    by_role: dict[str, Counter[str]] = defaultdict(Counter)
    by_action: Counter[str] = Counter()
    total = 0
    for cycle in list(cycles):
        proposals = list(cycle.proposals or [])
        if not proposals and cycle.proposal_votes:
            by_intent = dict(cycle.proposal_votes.get("by_intent") or {})
            for intent, count in by_intent.items():
                action_class = _normalize_action_class(selected_action=str(intent))
                by_action[action_class] += int(count)
                total += int(count)
            continue
        for proposal in proposals:
            action_class = _normalize_action_class(
                action={"action": proposal.get("intent"), "side": proposal.get("side")}
            )
            agent_id = str(proposal.get("agent_id") or "")
            role = str(proposal.get("proposal_role") or agent_id)
            by_pair[cycle.pair][action_class] += 1
            by_agent[agent_id][action_class] += 1
            by_role[role][action_class] += 1
            by_action[action_class] += 1
            total += 1
    return {
        "total": int(total),
        "by_action_class": dict(sorted((key, int(value)) for key, value in by_action.items())),
        "by_pair": {
            key: dict(sorted((action, int(value)) for action, value in counter.items()))
            for key, counter in sorted(by_pair.items())
        },
        "by_agent": {
            key: dict(sorted((action, int(value)) for action, value in counter.items()))
            for key, counter in sorted(by_agent.items())
        },
        "by_role": {
            key: dict(sorted((action, int(value)) for action, value in counter.items()))
            for key, counter in sorted(by_role.items())
        },
    }


def _load_price_lookup(
    *,
    profile: ReplayProfile,
    window: ReplayWindow,
) -> dict[str, dict[str, dict[str, float]]]:
    research_mod = _load_research_tool()
    settings = research_mod._research_settings()
    feature_root = _offline_local_path(profile.feature_root, field_name="feature_root")
    start_ts = pd.to_datetime(window.start_ts, utc=True)
    end_ts = pd.to_datetime(window.end_ts, utc=True)
    lookup: dict[str, dict[str, dict[str, float]]] = {}
    for pair in profile.pairs:
        frame = research_mod.BASE._load_historical_contract_frame(
            raw_store_root=feature_root,
            pair=pair,
            provider=str(settings.normalized_data_provider),
            intraday_timeframe=str(settings.intraday_timeframe).upper(),
            all_pairs=list(profile.pairs),
            start_ts=start_ts,
            end_ts=end_ts,
        )
        rows: dict[str, dict[str, float]] = {}
        for row in frame[["ts", "bid_close", "ask_close", "mid_close"]].itertuples(index=False):
            rows[_utc_iso(row.ts)] = {
                "bid": float(_safe_float(row.bid_close, 0.0)),
                "ask": float(_safe_float(row.ask_close, 0.0)),
                "mid": float(_safe_float(row.mid_close, 0.0)),
            }
        lookup[str(pair).upper()] = rows
    return lookup


def _slipped_price(price: float, *, side: str, slippage_bps: float, opening: bool) -> float:
    slip = max(0.0, float(slippage_bps)) / 10_000.0
    if opening:
        if side == "BUY":
            return price * (1.0 + slip)
        if side == "SELL":
            return price * (1.0 - slip)
    else:
        if side == "BUY":
            return price * (1.0 - slip)
        if side == "SELL":
            return price * (1.0 + slip)
    return price


def _pnl_usd(*, side: str, entry_price: float, exit_price: float, lots: float) -> float:
    units = 100_000.0 * float(lots)
    if side == "BUY":
        return float((exit_price - entry_price) * units)
    return float((entry_price - exit_price) * units)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.quantile(np.asarray(values, dtype=float), float(q)))


def simulate_orchestration_reconstruction(
    *,
    profile: ReplayProfile,
    cycles: list[OrchestrationCycle],
    price_lookup: dict[str, dict[str, dict[str, float]]],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    settings = get_settings()
    max_total_positions = max(1, int(getattr(settings, "max_total_positions", 1) or 1))
    default_lots = max(0.01, float(getattr(settings, "default_order_lots", 0.1) or 0.1))
    open_positions: dict[str, dict[str, Any]] = {}
    cash_balance = float(profile.start_equity)
    equity_points: list[float] = []
    open_counts: list[int] = []
    latency_points: list[float] = []
    history_rows: list[dict[str, Any]] = []
    trace_complete_count = 0
    entries = 0
    partial_exit_events = 0
    wins = 0
    losses = 0
    flats = 0
    rejection_counts: Counter[str] = Counter()
    closed_trades: list[float] = []
    last_ts = ""

    grouped: dict[str, list[OrchestrationCycle]] = defaultdict(list)
    for cycle in cycles:
        grouped[cycle.ts].append(cycle)

    for ts in sorted(grouped.keys()):
        last_ts = ts
        for cycle in sorted(grouped[ts], key=lambda item: item.pair):
            prices = dict(price_lookup.get(cycle.pair, {}).get(ts) or {})
            bid = float(_safe_float(prices.get("bid"), prices.get("mid", 0.0)))
            ask = float(_safe_float(prices.get("ask"), prices.get("mid", 0.0)))
            mid = float(_safe_float(prices.get("mid"), (bid + ask) / 2.0 if bid or ask else 0.0))
            action_class = cycle.orchestrated_action_class
            position = open_positions.get(cycle.pair)
            if cycle.trace_complete:
                trace_complete_count += 1
            latency_points.append(float(cycle.latency_ms))

            if action_class == "enter_buy" and position is None and len(open_positions) < max_total_positions:
                lots = float(_safe_float(dict(cycle.packet.get("governed_decision") or {}).get("command_preview", {}).get("lots"), default_lots))
                open_positions[cycle.pair] = {
                    "side": "BUY",
                    "lots": lots,
                    "entry_price": _slipped_price(ask or mid, side="BUY", slippage_bps=profile.slippage_bps, opening=True),
                    "open_ts": ts,
                }
                entries += 1
            elif action_class == "enter_sell" and position is None and len(open_positions) < max_total_positions:
                lots = float(_safe_float(dict(cycle.packet.get("governed_decision") or {}).get("command_preview", {}).get("lots"), default_lots))
                open_positions[cycle.pair] = {
                    "side": "SELL",
                    "lots": lots,
                    "entry_price": _slipped_price(bid or mid, side="SELL", slippage_bps=profile.slippage_bps, opening=True),
                    "open_ts": ts,
                }
                entries += 1
            elif action_class == "exit" and position is not None:
                exit_price = _slipped_price((bid if position["side"] == "BUY" else ask) or mid, side=position["side"], slippage_bps=profile.slippage_bps, opening=False)
                pnl = _pnl_usd(side=str(position["side"]), entry_price=float(position["entry_price"]), exit_price=exit_price, lots=float(position["lots"]))
                cash_balance += pnl
                closed_trades.append(float(pnl))
                wins += int(pnl > 0.0)
                losses += int(pnl < 0.0)
                flats += int(abs(pnl) <= 1e-9)
                del open_positions[cycle.pair]
            elif action_class == "reduce" and position is not None:
                close_lots = max(0.0, float(position["lots"]) * float(profile.reduce_fraction))
                if close_lots >= float(position["lots"]):
                    exit_price = _slipped_price((bid if position["side"] == "BUY" else ask) or mid, side=position["side"], slippage_bps=profile.slippage_bps, opening=False)
                    pnl = _pnl_usd(side=str(position["side"]), entry_price=float(position["entry_price"]), exit_price=exit_price, lots=float(position["lots"]))
                    cash_balance += pnl
                    closed_trades.append(float(pnl))
                    wins += int(pnl > 0.0)
                    losses += int(pnl < 0.0)
                    flats += int(abs(pnl) <= 1e-9)
                    del open_positions[cycle.pair]
                elif close_lots > 0.0:
                    exit_price = _slipped_price((bid if position["side"] == "BUY" else ask) or mid, side=position["side"], slippage_bps=profile.slippage_bps, opening=False)
                    pnl = _pnl_usd(side=str(position["side"]), entry_price=float(position["entry_price"]), exit_price=exit_price, lots=close_lots)
                    cash_balance += pnl
                    closed_trades.append(float(pnl))
                    wins += int(pnl > 0.0)
                    losses += int(pnl < 0.0)
                    flats += int(abs(pnl) <= 1e-9)
                    position["lots"] = max(0.0, float(position["lots"]) - close_lots)
                    partial_exit_events += 1

            if action_class == "no_trade":
                reason = cycle.blocking_reasons[0] if cycle.blocking_reasons else "policy_block"
                rejection_counts[str(reason)] += 1

            history_rows.append(
                {
                    "pair": cycle.pair,
                    "ts": cycle.ts,
                    "action_class": action_class,
                    "baseline_action_class": cycle.baseline_action_class,
                    "governor_outcome": cycle.governor_outcome,
                    "context_source": cycle.context_source,
                    "trace_complete": bool(cycle.trace_complete),
                    "feature_contract_id": cycle.feature_contract_id,
                    "run_id": cycle.run_id,
                    "trace_id": cycle.trace_id,
                    "divergence_reason": cycle.divergence_reason,
                    "blocking_reasons": "|".join(cycle.blocking_reasons),
                    "latency_ms": float(cycle.latency_ms),
                    "fallback_used": bool(cycle.fallback_used),
                }
            )

        marked_equity = cash_balance
        for pair, position in open_positions.items():
            prices = dict(price_lookup.get(pair, {}).get(ts) or {})
            bid = float(_safe_float(prices.get("bid"), prices.get("mid", 0.0)))
            ask = float(_safe_float(prices.get("ask"), prices.get("mid", 0.0)))
            mid = float(_safe_float(prices.get("mid"), (bid + ask) / 2.0 if bid or ask else 0.0))
            exit_price = (bid if position["side"] == "BUY" else ask) or mid
            marked_equity += _pnl_usd(
                side=str(position["side"]),
                entry_price=float(position["entry_price"]),
                exit_price=float(exit_price),
                lots=float(position["lots"]),
            )
        equity_points.append(float(marked_equity))
        open_counts.append(int(len(open_positions)))

    if last_ts:
        for pair, position in list(open_positions.items()):
            prices = dict(price_lookup.get(pair, {}).get(last_ts) or {})
            bid = float(_safe_float(prices.get("bid"), prices.get("mid", 0.0)))
            ask = float(_safe_float(prices.get("ask"), prices.get("mid", 0.0)))
            mid = float(_safe_float(prices.get("mid"), (bid + ask) / 2.0 if bid or ask else 0.0))
            exit_price = _slipped_price((bid if position["side"] == "BUY" else ask) or mid, side=position["side"], slippage_bps=profile.slippage_bps, opening=False)
            pnl = _pnl_usd(side=str(position["side"]), entry_price=float(position["entry_price"]), exit_price=exit_price, lots=float(position["lots"]))
            cash_balance += pnl
            closed_trades.append(float(pnl))
            wins += int(pnl > 0.0)
            losses += int(pnl < 0.0)
            flats += int(abs(pnl) <= 1e-9)
            del open_positions[pair]

    equity_curve = np.asarray(equity_points if equity_points else [profile.start_equity], dtype=float)
    peaks = np.maximum.accumulate(equity_curve)
    drawdown_usd = equity_curve - peaks
    drawdown_pct = np.where(peaks > 0.0, drawdown_usd / peaks * 100.0, 0.0)
    gross_profit = sum(max(0.0, value) for value in closed_trades)
    gross_loss = abs(sum(min(0.0, value) for value in closed_trades))
    profit_factor = float(gross_profit / gross_loss) if gross_loss > 0.0 else float(gross_profit)
    trades = int(len(closed_trades))
    aggregate = {
        "run_status": "ok",
        "start_equity_usd": float(profile.start_equity),
        "end_equity_usd": float(cash_balance),
        "total_return_pct": float(((cash_balance / max(profile.start_equity, 1e-9)) - 1.0) * 100.0),
        "net_pnl_usd": float(cash_balance - profile.start_equity),
        "trades": trades,
        "entries": int(entries),
        "wins": int(wins),
        "losses": int(losses),
        "flats": int(flats),
        "win_rate": float((wins / trades) if trades else 0.0),
        "profit_factor": float(profit_factor),
        "max_drawdown_pct": float(abs(np.min(drawdown_pct))) if drawdown_pct.size else 0.0,
        "max_drawdown_usd": float(abs(np.min(drawdown_usd))) if drawdown_usd.size else 0.0,
        "avg_open_positions": float(sum(open_counts) / max(1, len(open_counts))),
        "peak_open_positions": int(max(open_counts) if open_counts else 0),
        "slot_utilization_rate": float((sum(open_counts) / max(1, len(open_counts))) / max_total_positions),
        "expectancy_per_trade_usd": float(sum(closed_trades) / max(1, trades)),
        "partial_exit_events": int(partial_exit_events),
        "rejection_counts": dict(sorted((key, int(value)) for key, value in rejection_counts.items())),
        "latency_p95_ms": float(_quantile(latency_points, 0.95)),
        "trace_completeness_rate": float(trace_complete_count / max(1, len(cycles))),
        "decision_count": int(len(cycles)),
    }
    trace_summary = {
        "trace_complete_count": int(trace_complete_count),
        "eligible_cycle_count": int(len(cycles)),
        "trace_completeness_rate": float(trace_complete_count / max(1, len(cycles))),
        "latency_ms": {
            "p50": float(_quantile(latency_points, 0.50)),
            "p95": float(_quantile(latency_points, 0.95)),
            "p99": float(_quantile(latency_points, 0.99)),
            "max": float(max(latency_points) if latency_points else 0.0),
        },
    }
    return aggregate, history_rows, trace_summary


def _read_history_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def _build_research_args(
    *,
    research_mod: Any,
    profile: ReplayProfile,
    window: ReplayWindow,
    out_dir: Path,
    exec_mode: str,
) -> argparse.Namespace:
    parser = research_mod.build_parser()
    args = parser.parse_args(
        [
            "--pairs",
            ",".join(profile.pairs),
            "--raw-root",
            str(_offline_local_path(profile.feature_root, field_name="feature_root")),
            "--manifest-path",
            str(
                _offline_local_path(
                    profile.research_manifest_path,
                    field_name="research_manifest_path",
                )
            ),
            "--start-ts",
            str(window.start_ts),
            "--end-ts",
            str(window.end_ts),
            "--start-equity",
            str(profile.start_equity),
            "--slippage-bps",
            str(profile.slippage_bps),
            "--out-dir",
            str(out_dir),
            "--exec-mode",
            str(exec_mode),
            "--fill-delay-bars",
            "1",
            "--emit-decision-history",
            "--recommendations",
            "--no-adaptive-compare-baseline",
        ]
    )
    return args


def run_baseline_and_adaptive_lanes(
    *,
    profile: ReplayProfile,
    window: ReplayWindow,
    out_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    research_mod = _load_research_tool()
    baseline_dir = out_dir / "baseline"
    adaptive_dir = out_dir / "adaptive"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    adaptive_dir.mkdir(parents=True, exist_ok=True)

    _seed_everything(profile.seed)
    baseline_args = _build_research_args(
        research_mod=research_mod,
        profile=profile,
        window=window,
        out_dir=baseline_dir,
        exec_mode=str(research_mod.STRICT_EXEC_MODE),
    )
    baseline_result = research_mod._run_research_once(baseline_args)

    _seed_everything(profile.seed)
    adaptive_args = _build_research_args(
        research_mod=research_mod,
        profile=profile,
        window=window,
        out_dir=adaptive_dir,
        exec_mode=str(research_mod.ADAPTIVE_EXEC_MODE),
    )
    adaptive_result = research_mod._run_research_once(adaptive_args, baseline_result=baseline_result)

    comparison_payload = research_mod._adaptive_baseline_comparison_payload(
        adaptive_result=adaptive_result,
        baseline_result=baseline_result,
    )
    aggressiveness_assessment = research_mod._adaptive_guardrails_payload(
        args=adaptive_args,
        adaptive_result=adaptive_result,
        baseline_result=baseline_result,
    )
    (adaptive_dir / "adaptive_baseline_comparison.json").write_text(
        json.dumps(comparison_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (adaptive_dir / "adaptive_aggressiveness_assessment.json").write_text(
        json.dumps(aggressiveness_assessment, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    adaptive_result["adaptive_baseline_comparison"] = comparison_payload
    adaptive_result["adaptive_aggressiveness_assessment"] = aggressiveness_assessment
    return baseline_result, adaptive_result


def build_divergence_rows(
    *,
    baseline_history_rows: list[dict[str, Any]],
    adaptive_history_rows: list[dict[str, Any]],
    cycles: list[OrchestrationCycle],
    feature_contract_id: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    baseline_by_key = {
        (str(row.get("pair") or "").upper(), _utc_iso(row.get("ts"))): dict(row)
        for row in baseline_history_rows
        if str(row.get("pair") or "").strip() and str(row.get("ts") or "").strip()
    }
    adaptive_by_key = {
        (str(row.get("pair") or "").upper(), _utc_iso(row.get("ts"))): dict(row)
        for row in adaptive_history_rows
        if str(row.get("pair") or "").strip() and str(row.get("ts") or "").strip()
    }
    orchestration_by_key = {(cycle.pair, cycle.ts): cycle for cycle in cycles}
    comparable_keys = sorted(set(baseline_by_key) & set(adaptive_by_key) & set(orchestration_by_key))
    rows: list[dict[str, Any]] = []
    overlap_matches = 0
    divergence_count = 0
    baseline_policy_blocks = 0
    adaptive_policy_blocks = 0
    orchestration_policy_blocks = 0
    for key in comparable_keys:
        baseline = baseline_by_key[key]
        adaptive = adaptive_by_key[key]
        cycle = orchestration_by_key[key]
        baseline_action_class = normalize_research_decision_row(baseline)
        adaptive_action_class = normalize_research_decision_row(adaptive)
        orchestrated_action_class = cycle.orchestrated_action_class
        baseline_governor_outcome = baseline_action_class
        orchestrated_governor_outcome = cycle.governor_outcome or orchestrated_action_class
        overlap_matches += int(baseline_action_class == orchestrated_action_class)
        divergence_count += int(baseline_governor_outcome != orchestrated_governor_outcome)
        baseline_policy_blocks += int(baseline_action_class == "no_trade")
        adaptive_policy_blocks += int(adaptive_action_class == "no_trade")
        orchestration_policy_blocks += int(orchestrated_action_class == "no_trade")
        rows.append(
            {
                "pair": key[0],
                "ts": key[1],
                "feature_contract_id": feature_contract_id,
                "baseline_action_class": baseline_action_class,
                "adaptive_action_class": adaptive_action_class,
                "orchestrated_action_class": orchestrated_action_class,
                "baseline_governor_outcome": baseline_governor_outcome,
                "orchestrated_governor_outcome": orchestrated_governor_outcome,
                "divergence_reason": cycle.divergence_reason or ("agree" if baseline_action_class == orchestrated_action_class else "action_mismatch"),
                "blocking_reasons": "|".join(cycle.blocking_reasons),
                "context_source": cycle.context_source,
                "trace_complete": bool(cycle.trace_complete),
                "winning_proposal_id": str(cycle.winning_proposal_id),
                "winning_agent": str(cycle.winning_agent),
                "arbiter_stage": str(cycle.arbiter_stage),
            }
        )
    metrics = {
        "comparable_cycle_count": int(len(comparable_keys)),
        "action_overlap_rate": float(overlap_matches / max(1, len(comparable_keys))),
        "decision_divergence_rate": float(divergence_count / max(1, len(comparable_keys))),
        "baseline_policy_block_rate": float(baseline_policy_blocks / max(1, len(comparable_keys))),
        "adaptive_policy_block_rate": float(adaptive_policy_blocks / max(1, len(comparable_keys))),
        "orchestrated_policy_block_rate": float(orchestration_policy_blocks / max(1, len(comparable_keys))),
    }
    return rows, metrics


def build_window_assessment(
    *,
    profile: ReplayProfile,
    baseline_aggregate: dict[str, Any],
    orchestration_aggregate: dict[str, Any],
    reconstruction_summary: dict[str, Any],
    divergence_metrics: dict[str, Any],
    stability_passed: bool,
) -> dict[str, Any]:
    baseline_entries = max(1, int(baseline_aggregate.get("entries", 0) or 0))
    baseline_slot_util = max(float(_safe_float(baseline_aggregate.get("slot_utilization_rate"), 0.0)), 1e-9)
    entry_ratio = float(_safe_float(orchestration_aggregate.get("entries"), 0.0) / baseline_entries)
    slot_utilisation = float(_safe_float(orchestration_aggregate.get("slot_utilization_rate"), 0.0) / baseline_slot_util)
    drawdown_deterioration = float(
        _safe_float(orchestration_aggregate.get("max_drawdown_pct"), 0.0)
        - _safe_float(baseline_aggregate.get("max_drawdown_pct"), 0.0)
    )
    trace_completeness_rate = float(reconstruction_summary.get("trace_complete_count", 0) / max(1, reconstruction_summary.get("cycle_count", 0)))
    metrics = {
        "entry_ratio": float(entry_ratio),
        "slot_utilisation": float(slot_utilisation),
        "trace_completeness_rate": float(trace_completeness_rate),
        "action_overlap_rate": float(divergence_metrics.get("action_overlap_rate", 0.0)),
        "decision_divergence_rate": float(divergence_metrics.get("decision_divergence_rate", 1.0)),
        "max_drawdown_deterioration_pct": float(drawdown_deterioration),
    }
    checks = {
        "entry_ratio_floor": metrics["entry_ratio"] >= profile.thresholds.entry_ratio_floor,
        "slot_utilisation_floor": metrics["slot_utilisation"] >= profile.thresholds.slot_utilisation_floor,
        "trace_completeness_floor": metrics["trace_completeness_rate"] >= profile.thresholds.trace_completeness_floor,
        "action_overlap_floor": metrics["action_overlap_rate"] >= profile.thresholds.action_overlap_floor,
        "decision_divergence_rate_ceiling": metrics["decision_divergence_rate"] <= profile.thresholds.decision_divergence_rate_ceiling,
        "max_drawdown_deterioration_pct": metrics["max_drawdown_deterioration_pct"] <= profile.thresholds.max_drawdown_deterioration_pct,
        "seeded_stability": bool(stability_passed),
        "snapshot_overlap": bool(reconstruction_summary.get("snapshot_overlap_valid", False)),
    }
    failures = [key for key, passed in checks.items() if not bool(passed)]
    return {
        "thresholds": asdict(profile.thresholds),
        "metrics": metrics,
        "checks": checks,
        "failures": failures,
        "meets_research_thresholds": bool(not failures),
        "status": "ADVISORY_PASS" if not failures else "ADVISORY_REVIEW",
        "advisory_only": True,
        "authorizes_activation": False,
        "runtime_equivalence": "not_assessed",
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        if not rows:
            fh.write("")
            return
        fieldnames: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row.keys():
                key_txt = str(key)
                if key_txt not in seen:
                    seen.add(key_txt)
                    fieldnames.append(key_txt)
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_csv_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        if not rows:
            fh.write("")
            return
        fieldnames: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row.keys():
                key_txt = str(key)
                if key_txt not in seen:
                    seen.add(key_txt)
                    fieldnames.append(key_txt)
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _research_assessment_markdown(
    *,
    window: ReplayWindow,
    aggregate: dict[str, Any],
    assessment: dict[str, Any],
) -> str:
    lines = [
        f"# Orchestration Research Assessment: {window.window_id}",
        "",
        "> Advisory offline evidence only. This assessment does not validate runtime behavior or authorize activation.",
        "",
        f"Feature contract: `{aggregate['resolved_config']['feature_contract_id']}`",
        f"Comparable cycles: `{aggregate['diagnostics']['comparable_cycle_count']}`",
        "",
        "## Lane Metrics",
        "",
        f"- Baseline net pnl usd: `{aggregate['lanes']['baseline']['raw_metrics']['net_pnl_usd']:.2f}`",
        f"- Adaptive net pnl usd: `{aggregate['lanes']['adaptive']['raw_metrics']['net_pnl_usd']:.2f}`",
        f"- Orchestration reconstruction net pnl usd: `{aggregate['lanes']['orchestration_reconstruction']['raw_metrics']['net_pnl_usd']:.2f}`",
        f"- Entry ratio: `{assessment['metrics']['entry_ratio']:.4f}`",
        f"- Slot utilisation: `{assessment['metrics']['slot_utilisation']:.4f}`",
        f"- Action overlap rate: `{assessment['metrics']['action_overlap_rate']:.4f}`",
        f"- Decision divergence rate: `{assessment['metrics']['decision_divergence_rate']:.4f}`",
        f"- Trace completeness rate: `{assessment['metrics']['trace_completeness_rate']:.4f}`",
        "",
        "## Research Threshold Checks",
        "",
    ]
    for key, passed in dict(assessment.get("checks") or {}).items():
        lines.append(f"- {key}: `{'PASS' if passed else 'FAIL'}`")
    lines.extend(
        [
            "",
            f"Advisory threshold result: `{assessment['status']}`",
            "",
            "Runtime equivalence: `NOT ASSESSED`",
            "",
            "Activation authority: `NONE`",
            "",
        ]
    )
    return "\n".join(lines)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def run_window_replay(
    *,
    profile: ReplayProfile,
    window: ReplayWindow,
    experiment_id: str,
    output_root: Path,
    seed: int | None = None,
) -> dict[str, Any]:
    resolved_seed = int(profile.seed if seed is None else seed)
    window_dir = output_root / experiment_id / window.window_id
    window_dir.mkdir(parents=True, exist_ok=True)
    (window_dir / "baseline").mkdir(parents=True, exist_ok=True)
    (window_dir / "adaptive").mkdir(parents=True, exist_ok=True)
    (window_dir / "orchestration_reconstruction").mkdir(parents=True, exist_ok=True)

    baseline_result, adaptive_result = run_baseline_and_adaptive_lanes(profile=profile, window=window, out_dir=window_dir)
    bundle = load_source_bundle(profile=profile, window=window)
    cycles, reconstruction_summary = build_orchestration_cycles(profile=profile, window=window, bundle=bundle, seed=resolved_seed)
    price_lookup = _load_price_lookup(profile=profile, window=window)
    orchestration_aggregate, orchestration_history, trace_summary = simulate_orchestration_reconstruction(
        profile=profile,
        cycles=cycles,
        price_lookup=price_lookup,
    )
    proposal_votes = aggregate_proposal_votes(cycles)
    baseline_history_rows = _read_history_rows(Path(baseline_result["decision_history_path"]))
    adaptive_history_rows = _read_history_rows(Path(adaptive_result["decision_history_path"]))
    divergence_rows, divergence_metrics = build_divergence_rows(
        baseline_history_rows=baseline_history_rows,
        adaptive_history_rows=adaptive_history_rows,
        cycles=cycles,
        feature_contract_id=profile.feature_contract_id,
    )

    repeat_cycles, _ = build_orchestration_cycles(profile=profile, window=window, bundle=bundle, seed=resolved_seed)
    repeat_votes = aggregate_proposal_votes(repeat_cycles)
    stability_digest = _sha256(
        {
            "cycles": [cycle.to_row() for cycle in cycles],
            "votes": proposal_votes,
            "divergence": divergence_rows,
            "aggregate": orchestration_aggregate,
        }
    )
    repeat_digest = _sha256(
        {
            "cycles": [cycle.to_row() for cycle in repeat_cycles],
            "votes": repeat_votes,
            "divergence": divergence_rows,
            "aggregate": orchestration_aggregate,
        }
    )
    stability_passed = stability_digest == repeat_digest

    assessment = build_window_assessment(
        profile=profile,
        baseline_aggregate=dict(baseline_result["aggregate"]),
        orchestration_aggregate=orchestration_aggregate,
        reconstruction_summary=reconstruction_summary,
        divergence_metrics=divergence_metrics,
        stability_passed=stability_passed,
    )

    baseline_raw = dict(baseline_result["aggregate"])
    adaptive_raw = dict(adaptive_result["aggregate"])
    baseline_raw["entry_ratio"] = 1.0
    baseline_raw["slot_utilisation"] = 1.0
    baseline_raw["policy_block_rate"] = float(divergence_metrics["baseline_policy_block_rate"])
    adaptive_raw["entry_ratio"] = float(_safe_float(adaptive_raw.get("entries"), 0.0) / max(1, baseline_raw.get("entries", 0)))
    adaptive_raw["slot_utilisation"] = float(
        _safe_float(adaptive_raw.get("slot_utilization_rate"), 0.0) / max(_safe_float(baseline_raw.get("slot_utilization_rate"), 0.0), 1e-9)
    )
    adaptive_raw["policy_block_rate"] = float(divergence_metrics["adaptive_policy_block_rate"])
    orchestration_raw = dict(orchestration_aggregate)
    orchestration_raw["entry_ratio"] = float(assessment["metrics"]["entry_ratio"])
    orchestration_raw["slot_utilisation"] = float(assessment["metrics"]["slot_utilisation"])
    orchestration_raw["action_overlap_rate"] = float(divergence_metrics["action_overlap_rate"])
    orchestration_raw["decision_divergence_rate"] = float(divergence_metrics["decision_divergence_rate"])
    orchestration_raw["policy_block_rate"] = float(divergence_metrics["orchestrated_policy_block_rate"])
    orchestration_raw["trace_completeness_rate"] = float(reconstruction_summary["trace_complete_count"] / max(1, reconstruction_summary["cycle_count"]))

    resolved_config = {
        "profile_id": profile.profile_id,
        "pairs": list(profile.pairs),
        "feature_contract_id": profile.feature_contract_id,
        "feature_root": profile.feature_root,
        "research_manifest_path": profile.research_manifest_path,
        "start_equity": profile.start_equity,
        "slippage_bps": profile.slippage_bps,
        "seed": resolved_seed,
        "research_validation_limit": profile.research_validation_limit,
        "window": asdict(window),
        "research_thresholds": asdict(profile.thresholds),
        "orchestration_source": dict(profile.orchestration_source),
    }
    aggregate = {
        "schema_version": PHASE3_REPLAY_SCHEMA_VERSION,
        "experiment_id": str(experiment_id),
        "window_id": str(window.window_id),
        "research_contract": {
            "advisory_only": True,
            "authorizes_activation": False,
            "runtime_equivalence": "not_assessed",
        },
        "resolved_config": resolved_config,
        "lanes": {
            "baseline": {
                "raw_metrics": baseline_raw,
                "artifact_paths": {key: str(value) for key, value in baseline_result.items() if str(key).endswith("_path")},
            },
            "adaptive": {
                "raw_metrics": adaptive_raw,
                "artifact_paths": {key: str(value) for key, value in adaptive_result.items() if str(key).endswith("_path")},
            },
            "orchestration_reconstruction": {
                "raw_metrics": orchestration_raw,
                "artifact_paths": {
                    "aggregate_path": str(window_dir / "orchestration_reconstruction" / "aggregate.json"),
                    "decision_history_path": str(window_dir / "orchestration_reconstruction" / "decision_history.csv.gz"),
                    "trace_summary_path": str(window_dir / "orchestration_reconstruction" / "trace_summary.json"),
                    "reconstruction_summary_path": str(window_dir / "orchestration_reconstruction" / "reconstruction_summary.json"),
                },
            },
        },
        "diagnostics": {
            **divergence_metrics,
            "trace_completeness_rate": float(orchestration_raw["trace_completeness_rate"]),
            "latency_p95_ms": float(orchestration_raw["latency_p95_ms"]),
            "snapshot_overlap_valid": bool(reconstruction_summary["snapshot_overlap_valid"]),
            "seeded_stability": {
                "passed": bool(stability_passed),
                "digest": stability_digest,
                "repeat_digest": repeat_digest,
            },
        },
        "research_assessment": {
            "meets_research_thresholds": bool(assessment["meets_research_thresholds"]),
            "status": str(assessment["status"]),
            "failures": list(assessment["failures"]),
            "advisory_only": True,
            "authorizes_activation": False,
        },
    }

    reconstruction_dir = window_dir / "orchestration_reconstruction"
    _write_json(reconstruction_dir / "aggregate.json", orchestration_aggregate)
    _write_csv_gz(reconstruction_dir / "decision_history.csv.gz", orchestration_history)
    _write_json(reconstruction_dir / "trace_summary.json", trace_summary)
    _write_json(reconstruction_dir / "reconstruction_summary.json", reconstruction_summary)
    _write_json(window_dir / "aggregate.json", aggregate)
    _write_json(window_dir / "research_assessment.json", assessment)
    _write_csv(window_dir / "divergence.csv", divergence_rows)
    _write_json(window_dir / "proposal_votes.json", proposal_votes)
    _write_json(window_dir / "config.json", resolved_config)
    (window_dir / "research_assessment.md").write_text(
        _research_assessment_markdown(window=window, aggregate=aggregate, assessment=assessment),
        encoding="utf-8",
    )
    return {
        "window_id": str(window.window_id),
        "window_dir": str(window_dir),
        "aggregate": aggregate,
        "assessment": assessment,
    }


def run_experiment(
    *,
    config_path: str | Path,
    experiment_id: str,
    window_name: str,
    out_dir: str | Path,
    seed: int | None = None,
) -> dict[str, Any]:
    profile = load_replay_profile(config_path)
    target_root = Path(out_dir)
    target_root.mkdir(parents=True, exist_ok=True)
    if str(window_name) == "all":
        windows = [profile.windows[key] for key in ["calm", "trend", "shock"] if key in profile.windows]
    else:
        if str(window_name) not in profile.windows:
            raise KeyError(f"unknown replay window: {window_name}")
        windows = [profile.windows[str(window_name)]]
    results = [run_window_replay(profile=profile, window=window, experiment_id=experiment_id, output_root=target_root, seed=seed) for window in windows]
    all_thresholds_met = all(bool(item["assessment"]["meets_research_thresholds"]) for item in results)
    research_summary = {
        "schema_version": PHASE3_REPLAY_SCHEMA_VERSION,
        "experiment_id": str(experiment_id),
        "config_path": str(Path(config_path)),
        "windows": {item["window_id"]: item["assessment"]["status"] for item in results},
        "meets_research_thresholds": bool(all_thresholds_met),
        "status": "ADVISORY_PASS" if all_thresholds_met else "ADVISORY_REVIEW",
        "advisory_only": True,
        "authorizes_activation": False,
        "runtime_equivalence": "not_assessed",
    }
    experiment_dir = target_root / experiment_id
    _write_json(experiment_dir / "research_summary.json", research_summary)
    lines = [
        "# Orchestration Research Summary",
        "",
        "> Advisory offline evidence only. Runtime validation and activation decisions happen elsewhere.",
        "",
    ]
    for item in results:
        lines.append(f"- {item['window_id']}: `{item['assessment']['status']}`")
    lines.extend(
        [
            "",
            f"Research threshold result: `{research_summary['status']}`",
            "",
            "Activation authority: `NONE`",
            "",
        ]
    )
    (experiment_dir / "research_assessment.md").write_text("\n".join(lines), encoding="utf-8")
    return {"profile": profile, "results": results, "summary": research_summary}
