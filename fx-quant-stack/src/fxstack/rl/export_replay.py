from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from fxstack.rl._common import _ensure_dir, _json_dump
from fxstack.rl.contracts import RLReplayContext


LEGACY_TRANSITION_COLUMNS = [
    "episode_id",
    "step_id",
    "ts",
    "pair",
    "state",
    "action",
    "reward",
    "next_state",
    "done",
    "terminal_reason",
    "policy_version",
    "feature_service_version",
    "feature_contract_hash",
    "risk_trace_json",
    "execution_trace_json",
]

TRANSITION_COLUMNS = [
    "episode_id",
    "step_id",
    "ts",
    "pair",
    "schema_version",
    "state_json",
    "action_json",
    "next_state_json",
    "market_by_pair_json",
    "features_by_pair_json",
    "portfolio_json",
    "policy_context_json",
    "pair_actions_json",
    "reward",
    "done",
    "terminal_reason",
    "policy_version",
    "feature_service_version",
    "feature_contract_hash",
    "risk_trace_json",
    "execution_trace_json",
    "lifecycle_json",
    "portfolio_context_json",
    "metadata_json",
]

REPLAY_CONTEXT_SCHEMA_VERSION = "portfolio_rl_context_v2"
REPLAY_CONTEXT_COLUMNS = ["lifecycle_json", "portfolio_context_json"]


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_string(value: Any, *, fallback: dict[str, Any] | list[Any] | None = None) -> str:
    if value in (None, ""):
        value = fallback if fallback is not None else {}
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                json.loads(stripped)
                return stripped
            except Exception:
                pass
        return _stable_json({"value": value})
    return _stable_json(value)


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return dict(parsed)
        except Exception:
            pass
    return {}


def _mapping_value(source: Any, key: str) -> Any:
    mapping = _as_mapping(source)
    if key in mapping and mapping.get(key) not in (None, ""):
        return mapping.get(key)
    nested = _as_mapping(mapping.get("metadata"))
    if key in nested and nested.get(key) not in (None, ""):
        return nested.get(key)
    return None


def _collect_context(source_items: Iterable[Any], keys: Iterable[str]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    key_list = [str(key) for key in keys]
    for source in source_items:
        for key in key_list:
            value = _mapping_value(source, key)
            if value in (None, ""):
                continue
            context[key] = value
    return context


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "f", "no", "n", "off"}:
            return False
    return bool(value)


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _hash_rows(rows: list[dict[str, Any]]) -> str:
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(_stable_json(row).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _normalize_source_rows(payload: dict[str, Any] | Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("items", "decision_snapshots", "transitions", "rows"):
            if isinstance(payload.get(key), list):
                return [dict(item or {}) for item in payload.get(key) or []]
        return [dict(payload)]
    return [dict(item or {}) for item in payload]


def _episode_id_from_snapshot(snapshot: dict[str, Any], *, index: int) -> str:
    episode = snapshot.get("episode_id")
    if episode not in (None, ""):
        return str(episode)
    ts = str(snapshot.get("ts") or snapshot.get("runtime_last_cycle_ts") or snapshot.get("created_at") or "")
    pair = str(snapshot.get("pair") or snapshot.get("symbol") or snapshot.get("workflow_pair") or "bundle").upper()
    if ts:
        return f"{pair}:{ts}"
    return f"{pair}:episode-{index:06d}"


def _extract_snapshot_entries(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("decisions_json", "decision_rows", "rows", "transitions"):
        entries = list(snapshot.get(key) or [])
        if entries:
            return [dict(item or {}) for item in entries]
    return [dict(snapshot)]


def _infer_pair(snapshot: dict[str, Any], entry: dict[str, Any]) -> str:
    pair = entry.get("pair") or entry.get("symbol") or snapshot.get("pair") or snapshot.get("symbol") or snapshot.get("workflow_pair") or ""
    return str(pair).upper()


def _extract_context(snapshot: dict[str, Any], entry: dict[str, Any], pair: str) -> dict[str, Any]:
    metadata = _as_mapping(entry.get("metadata"))
    snapshot_metadata = _as_mapping(snapshot.get("metadata"))
    merged_metadata = {**snapshot_metadata, **metadata}
    state = _as_mapping(entry.get("state")) or _as_mapping(snapshot.get("state"))
    next_state = _as_mapping(entry.get("next_state")) or _as_mapping(metadata.get("next_state"))
    action = _as_mapping(entry.get("action")) or _as_mapping(metadata.get("action"))
    if not action:
        action = {
            "side": str(entry.get("side") or snapshot.get("side") or ""),
            "score": entry.get("score", snapshot.get("score")),
            "confidence": entry.get("confidence", snapshot.get("confidence")),
            "execution_ready": entry.get("execution_ready", snapshot.get("execution_ready")),
        }
    pair_actions = _as_mapping(entry.get("pair_actions")) or _as_mapping(snapshot.get("pair_actions"))
    if not pair_actions and pair:
        pair_actions = {pair: dict(action)}
    pair_action = _as_mapping(pair_actions.get(pair)) if pair and isinstance(pair_actions, dict) else {}
    if not pair_action and pair and isinstance(pair_actions, dict):
        pair_action = _as_mapping(pair_actions.get(pair.upper()))
    market_by_pair = _as_mapping(snapshot.get("market_by_pair")) or _as_mapping(metadata.get("market_by_pair"))
    if not market_by_pair:
        market = _as_mapping(snapshot.get("market")) or _as_mapping(entry.get("market"))
        if not market:
            market = {
                "pair": pair,
                "spread_bps": entry.get("spread_bps", snapshot.get("spread_bps", 0.0)),
                "freshness_secs": entry.get("freshness_secs", snapshot.get("freshness_secs", 0.0)),
                "volatility": entry.get("vol_20", snapshot.get("vol_20", snapshot.get("volatility", 0.0))),
                "liquidity_score": entry.get("liquidity_score", snapshot.get("liquidity_score", 0.0)),
                "session_bucket": entry.get("session_bucket", snapshot.get("session_bucket", "")),
                "regime": entry.get("regime_bucket", snapshot.get("regime_bucket", snapshot.get("regime", ""))),
            }
        market_by_pair = {pair: market}
    features_by_pair = _as_mapping(snapshot.get("features_by_pair")) or _as_mapping(metadata.get("features_by_pair"))
    if not features_by_pair:
        features = _as_mapping(entry.get("features")) or _as_mapping(entry.get("feature_values"))
        if not features:
            features = {
                "spread_bps": entry.get("spread_bps", snapshot.get("spread_bps", 0.0)),
                "freshness_secs": entry.get("freshness_secs", snapshot.get("freshness_secs", 0.0)),
                "vol_20": entry.get("vol_20", snapshot.get("vol_20", snapshot.get("volatility", 0.0))),
                "liquidity_score": entry.get("liquidity_score", snapshot.get("liquidity_score", 0.0)),
            }
        features_by_pair = {pair: features}
    portfolio = _as_mapping(snapshot.get("portfolio")) or _as_mapping(metadata.get("portfolio"))
    if not portfolio:
        portfolio = {
            "equity": snapshot.get("equity", entry.get("equity", 0.0)),
            "balance": snapshot.get("balance", entry.get("balance", 0.0)),
            "open_position_count": snapshot.get("open_position_count", entry.get("open_position_count", 0)),
            "pair_position_count": snapshot.get("pair_position_count", entry.get("pair_position_count", 0)),
            "gross_exposure": snapshot.get("gross_exposure", entry.get("gross_exposure", 0.0)),
            "net_exposure": snapshot.get("net_exposure", entry.get("net_exposure", 0.0)),
        }
    policy_context = _as_mapping(snapshot.get("policy_context")) or _as_mapping(metadata.get("policy_context"))
    if not policy_context:
        policy_context = {
            "policy_version": snapshot.get("policy_version", ""),
            "feature_contract_hash": snapshot.get("feature_contract_hash", ""),
            "session_bucket": snapshot.get("session_bucket", ""),
        }
    if "risk_trace" in metadata:
        risk_trace = metadata["risk_trace"]
    else:
        risk_trace = entry.get("risk_trace_json") or snapshot.get("risk_trace_json") or metadata.get("risk_trace") or {}
    if "execution_trace" in metadata:
        exec_trace = metadata["execution_trace"]
    else:
        exec_trace = entry.get("execution_trace_json") or snapshot.get("execution_trace_json") or metadata.get("execution_trace") or {}
    position_side = str(
        _first_nonempty(
            metadata.get("position_side"),
            entry.get("position_side"),
            snapshot.get("position_side"),
            _as_mapping(portfolio.get("metadata")).get("position_side"),
            pair_action.get("position_side"),
        )
        or ""
    ).strip().lower()
    target_position = _first_nonempty(
        _as_mapping(action).get("target_position"),
        metadata.get("target_position"),
        metadata.get("rl_lifecycle_target_position"),
        entry.get("target_position"),
        snapshot.get("target_position"),
        pair_action.get("target_position"),
    )
    close_lots = _first_nonempty(
        metadata.get("close_lots"),
        action.get("close_lots"),
        entry.get("close_lots"),
        snapshot.get("close_lots"),
        pair_action.get("close_lots"),
    )
    route_reason = str(
        _first_nonempty(
            metadata.get("rl_lifecycle_reason"),
            metadata.get("lifecycle_route_reason"),
            metadata.get("route_reason"),
            metadata.get("lifecycle_reason"),
            action.get("lifecycle_reason"),
            entry.get("lifecycle_reason"),
            snapshot.get("lifecycle_reason"),
        )
        or ""
    )
    explicit_flip_intent = _first_nonempty(
        metadata.get("flip_intent"),
        action.get("flip_intent"),
        entry.get("flip_intent"),
        snapshot.get("flip_intent"),
    )
    explicit_resize_intent = _first_nonempty(
        metadata.get("resize_intent"),
        action.get("resize_intent"),
        entry.get("resize_intent"),
        snapshot.get("resize_intent"),
    )
    flip_intent = _coerce_bool(explicit_flip_intent, default=False)
    if not flip_intent:
        if position_side == "long":
            flip_intent = float(_safe_float(target_position, 0.0)) < -0.05
        elif position_side == "short":
            flip_intent = float(_safe_float(target_position, 0.0)) > 0.05
        else:
            flip_intent = any(token in route_reason.lower() for token in ("flip", "reverse", "reversal"))
    resize_intent = _coerce_bool(explicit_resize_intent, default=False)
    if not resize_intent:
        resize_intent = any(token in route_reason.lower() for token in ("resize", "partial_tp")) or bool(
            _coerce_bool(metadata.get("tighten_stop"), default=False) or _coerce_bool(action.get("tighten_stop"), default=False)
        )
    lifecycle_context = _collect_context(
        [snapshot, entry, action, metadata],
        [
            "lifecycle_action",
            "lifecycle_reason",
            "lifecycle_route_reason",
            "rl_lifecycle_reason",
            "lifecycle_action_score",
            "replacement_urgency",
            "close_position",
            "tighten_stop",
            "has_open_position",
            "entry_ready",
            "strict_entry_ready",
            "flip_intent",
            "resize_intent",
            "target_position",
            "rl_lifecycle_target_position",
            "close_lots",
            "position_side",
        ],
    )
    lifecycle_context.update(
        {
            "lifecycle_route_reason": route_reason,
            "rl_lifecycle_reason": route_reason,
            "flip_intent": bool(flip_intent),
            "resize_intent": bool(resize_intent),
            "target_position": float(_safe_float(target_position, 0.0)) if target_position not in (None, "") else 0.0,
            "rl_lifecycle_target_position": float(_safe_float(target_position, 0.0)) if target_position not in (None, "") else 0.0,
            "close_lots": float(_safe_float(close_lots, 0.0)) if close_lots not in (None, "") else 0.0,
            "position_side": position_side,
        }
    )
    portfolio_context = _collect_context(
        [snapshot, entry, portfolio, policy_context, merged_metadata],
        [
            "concentration",
            "correlation",
            "budget",
            "stress",
            "governance",
            "portfolio_concentration",
            "portfolio_risk_pressure",
            "portfolio_correlation_pressure",
            "portfolio_pair_pressure",
            "portfolio_session_pressure",
            "portfolio_sleeve_pressure",
            "replacement_pressure",
            "replacement_urgency",
            "capital_budget_scale",
            "portfolio_budget_scale",
        ],
    )
    replay_context = RLReplayContext(
        lifecycle_json=dict(lifecycle_context),
        portfolio_context_json=dict(portfolio_context),
        metadata_json={
            **merged_metadata,
            "replay_context_schema_version": REPLAY_CONTEXT_SCHEMA_VERSION,
            "replay_context_columns": list(REPLAY_CONTEXT_COLUMNS),
            "lifecycle_route_reason": route_reason,
            "rl_lifecycle_reason": route_reason,
            "flip_intent": bool(flip_intent),
            "resize_intent": bool(resize_intent),
            "rl_lifecycle_target_position": float(_safe_float(target_position, 0.0)) if target_position not in (None, "") else 0.0,
            "lifecycle_json": dict(lifecycle_context),
            "portfolio_context_json": dict(portfolio_context),
        },
    )
    return {
        "state_json": state,
        "action_json": action,
        "next_state_json": next_state or {"pair": pair},
        "market_by_pair_json": market_by_pair,
        "features_by_pair_json": features_by_pair,
        "portfolio_json": portfolio,
        "policy_context_json": policy_context,
        "pair_actions_json": pair_actions,
        "risk_trace_json": risk_trace,
        "execution_trace_json": exec_trace,
        "lifecycle_json": replay_context.lifecycle_json,
        "portfolio_context_json": replay_context.portfolio_context_json,
        "metadata_json": replay_context.metadata_json,
    }


@dataclass(slots=True)
class ReplayTransitionV2:
    episode_id: str
    step_id: int
    ts: str
    pair: str
    schema_version: str = "replay_transition_v2"
    state_json: str = "{}"
    action_json: str = "{}"
    next_state_json: str = "{}"
    market_by_pair_json: str = "{}"
    features_by_pair_json: str = "{}"
    portfolio_json: str = "{}"
    policy_context_json: str = "{}"
    pair_actions_json: str = "{}"
    reward: float = 0.0
    done: bool = False
    terminal_reason: str = ""
    policy_version: str = ""
    feature_service_version: str = ""
    feature_contract_hash: str = ""
    risk_trace_json: str = "{}"
    execution_trace_json: str = "{}"
    lifecycle_json: str = "{}"
    portfolio_context_json: str = "{}"
    metadata_json: str = "{}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ReplayTransition = ReplayTransitionV2


@dataclass(slots=True)
class ReplayBundleManifest:
    manifest_version: str
    dataset_name: str
    dataset_hash: str
    row_count: int
    episode_count: int
    pair_count: int
    source_count: int
    dataset_path: str
    schema_path: str
    transition_schema_version: str = "replay_transition_v2"
    source_paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _transition_from_entry(
    *,
    snapshot: dict[str, Any],
    entry: dict[str, Any],
    episode_id: str,
    step_id: int,
    snapshot_index: int,
) -> ReplayTransitionV2:
    pair = _infer_pair(snapshot, entry)
    context = _extract_context(snapshot, entry, pair)
    metadata = _as_mapping(context["metadata_json"])
    terminal_reason = str(entry.get("terminal_reason") or metadata.get("terminal_reason") or snapshot.get("terminal_reason") or "")
    return ReplayTransitionV2(
        episode_id=episode_id,
        step_id=int(step_id),
        ts=str(entry.get("ts") or snapshot.get("ts") or snapshot.get("runtime_last_cycle_ts") or snapshot.get("created_at") or ""),
        pair=pair,
        state_json=_json_string(context["state_json"], fallback={}),
        action_json=_json_string(context["action_json"], fallback={}),
        next_state_json=_json_string(context["next_state_json"], fallback={"index": snapshot_index, "pair": pair}),
        market_by_pair_json=_json_string(context["market_by_pair_json"], fallback={pair: {}}),
        features_by_pair_json=_json_string(context["features_by_pair_json"], fallback={pair: {}}),
        portfolio_json=_json_string(context["portfolio_json"], fallback={}),
        policy_context_json=_json_string(context["policy_context_json"], fallback={}),
        pair_actions_json=_json_string(context["pair_actions_json"], fallback={pair: {}}),
        reward=float(entry.get("reward", entry.get("score", snapshot.get("reward", 0.0))) or 0.0),
        done=bool(entry.get("done", entry.get("terminated", snapshot.get("done", False)) or entry.get("truncated", snapshot.get("truncated", False)))),
        terminal_reason=terminal_reason,
        policy_version=str(entry.get("policy_version") or snapshot.get("policy_version") or metadata.get("policy_version") or ""),
        feature_service_version=str(entry.get("feature_service_version") or snapshot.get("feature_service_version") or metadata.get("feature_service_version") or ""),
        feature_contract_hash=str(entry.get("feature_contract_hash") or snapshot.get("feature_contract_hash") or metadata.get("feature_contract_hash") or ""),
        risk_trace_json=_json_string(context["risk_trace_json"], fallback={}),
        execution_trace_json=_json_string(context["execution_trace_json"], fallback={}),
        lifecycle_json=_json_string(context["lifecycle_json"], fallback={}),
        portfolio_context_json=_json_string(context["portfolio_context_json"], fallback={}),
        metadata_json=_json_string(context["metadata_json"], fallback={}),
    )


def normalize_replay_transitions(
    snapshots: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    source_name: str = "decision_snapshots",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows = _normalize_source_rows(snapshots)
    transitions: list[ReplayTransitionV2] = []
    source_paths: list[str] = []
    episode_counter = 0
    for snapshot_index, snapshot in enumerate(rows):
        if snapshot.get("source_path"):
            source_paths.append(str(snapshot.get("source_path")))
        episode_id = _episode_id_from_snapshot(snapshot, index=snapshot_index)
        entries = _extract_snapshot_entries(snapshot)
        for step_id, entry in enumerate(entries):
            transitions.append(
                _transition_from_entry(
                    snapshot=snapshot,
                    entry=entry,
                    episode_id=episode_id,
                    step_id=step_id,
                    snapshot_index=snapshot_index,
                )
            )
        episode_counter += 1
    records = [item.to_dict() for item in transitions]
    if records:
        records = sorted(records, key=lambda row: (str(row["episode_id"]), int(row["step_id"]), str(row["ts"]), str(row["pair"])))
    df = pd.DataFrame(records, columns=TRANSITION_COLUMNS)
    manifest = {
        "status": "ok",
        "source_name": str(source_name),
        "row_count": int(len(df)),
        "episode_count": int(len({str(row["episode_id"]) for row in records})) if records else 0,
        "pair_count": int(len({str(row["pair"]) for row in records if str(row.get("pair") or "")})) if records else 0,
        "source_count": int(len(rows)),
        "source_paths": sorted({str(path) for path in source_paths if str(path).strip()}),
        "transition_schema_version": "replay_transition_v2",
        "replay_context_schema_version": REPLAY_CONTEXT_SCHEMA_VERSION,
        "replay_context_columns": list(REPLAY_CONTEXT_COLUMNS),
    }
    return df, manifest


normalize_replay_transitions_v2 = normalize_replay_transitions


def export_replay_dataset(
    snapshots: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    out_dir: Path,
    dataset_name: str = "replay_transitions",
    source_name: str = "decision_snapshots",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out_dir = _ensure_dir(out_dir)
    df, normalized = normalize_replay_transitions(snapshots, source_name=source_name)
    dataset_hash = _hash_rows(df.to_dict(orient="records")) if not df.empty else hashlib.sha256(b"empty").hexdigest()
    dataset_path = out_dir / "replay_transitions.parquet"
    schema_path = out_dir / "replay_transitions.schema.json"
    manifest_path = out_dir / "replay_manifest.json"
    table = pa.Table.from_pandas(df if not df.empty else pd.DataFrame(columns=TRANSITION_COLUMNS), preserve_index=False)
    pq.write_table(table, dataset_path, compression="snappy")
    schema_payload = {
        "dataset_name": str(dataset_name),
        "schema_version": "replay_transition_v2",
        "columns": [
            {"name": field.name, "type": str(field.type), "nullable": True}
            for field in table.schema
        ],
    }
    _json_dump(schema_path, schema_payload)
    manifest = ReplayBundleManifest(
        manifest_version="phase6_replay_export_v2",
        dataset_name=str(dataset_name),
        dataset_hash=dataset_hash,
        row_count=int(len(df)),
        episode_count=int(normalized.get("episode_count", 0)),
        pair_count=int(normalized.get("pair_count", 0)),
        source_count=int(normalized.get("source_count", 0)),
        dataset_path=str(dataset_path),
        schema_path=str(schema_path),
        transition_schema_version=str(normalized.get("transition_schema_version", "replay_transition_v2")),
        source_paths=list(normalized.get("source_paths") or []),
        metadata={
            **dict(metadata or {}),
            "replay_context_schema_version": str(normalized.get("replay_context_schema_version") or REPLAY_CONTEXT_SCHEMA_VERSION),
            "replay_context_columns": list(normalized.get("replay_context_columns") or REPLAY_CONTEXT_COLUMNS),
        },
    )
    _json_dump(manifest_path, manifest.to_dict())
    return {
        "status": "ok",
        "dataset_path": str(dataset_path),
        "schema_path": str(schema_path),
        "manifest_path": str(manifest_path),
        "dataset_hash": dataset_hash,
        "row_count": int(len(df)),
        "episode_count": int(normalized.get("episode_count", 0)),
        "pair_count": int(normalized.get("pair_count", 0)),
        "source_count": int(normalized.get("source_count", 0)),
        "manifest": manifest.to_dict(),
    }


export_replay_dataset_v2 = export_replay_dataset
