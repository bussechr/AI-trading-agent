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


TRANSITION_COLUMNS = [
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


@dataclass(slots=True)
class ReplayTransition:
    episode_id: str
    step_id: int
    ts: str
    pair: str
    state: dict[str, Any] = field(default_factory=dict)
    action: dict[str, Any] = field(default_factory=dict)
    reward: float = 0.0
    next_state: dict[str, Any] = field(default_factory=dict)
    done: bool = False
    terminal_reason: str = ""
    policy_version: str = ""
    feature_service_version: str = ""
    feature_contract_hash: str = ""
    risk_trace_json: str = ""
    execution_trace_json: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = dict(self.state or {})
        payload["action"] = dict(self.action or {})
        payload["next_state"] = dict(self.next_state or {})
        return payload


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
    source_paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash_rows(rows: list[dict[str, Any]]) -> str:
    hasher = hashlib.sha256()
    for row in rows:
        hasher.update(_stable_json(row).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _normalize_source_rows(payload: dict[str, Any] | Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("items"), list):
            return [dict(item or {}) for item in payload.get("items") or []]
        if isinstance(payload.get("decision_snapshots"), list):
            return [dict(item or {}) for item in payload.get("decision_snapshots") or []]
        if isinstance(payload.get("transitions"), list):
            return [dict(item or {}) for item in payload.get("transitions") or []]
        if isinstance(payload.get("rows"), list):
            return [dict(item or {}) for item in payload.get("rows") or []]
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
    entries = list(snapshot.get("decisions_json") or snapshot.get("decision_rows") or snapshot.get("rows") or [])
    if entries:
        return [dict(item or {}) for item in entries]
    return [dict(snapshot)]


def _transition_from_entry(
    *,
    snapshot: dict[str, Any],
    entry: dict[str, Any],
    episode_id: str,
    step_id: int,
    snapshot_index: int,
) -> ReplayTransition:
    metadata = dict(entry.get("metadata") or {})
    diagnostics = dict(snapshot.get("diagnostics_json") or snapshot.get("diagnostics") or {})
    state = dict(metadata.get("state") or entry.get("state") or {})
    if not state:
        state = {k: v for k, v in {"metadata": metadata, "diagnostics": diagnostics}.items() if v}
        state["decision"] = {k: v for k, v in entry.items() if k not in {"metadata"}}
    action = dict(entry.get("action") or metadata.get("action") or {})
    if not action:
        action = {
            "side": str(entry.get("side") or ""),
            "score": entry.get("score"),
            "confidence": entry.get("confidence"),
            "execution_ready": entry.get("execution_ready"),
            "lifecycle_action": metadata.get("lifecycle_action"),
        }
    next_state = dict(entry.get("next_state") or metadata.get("next_state") or {})
    if not next_state:
        next_state = {
            "index": snapshot_index,
            "episode_id": episode_id,
            "pair": str(entry.get("symbol") or entry.get("pair") or snapshot.get("pair") or "").upper(),
        }
    risk_trace = entry.get("risk_trace_json")
    if risk_trace in (None, ""):
        risk_trace = metadata.get("risk_trace")
    exec_trace = entry.get("execution_trace_json")
    if exec_trace in (None, ""):
        exec_trace = metadata.get("execution_trace")
    return ReplayTransition(
        episode_id=episode_id,
        step_id=int(step_id),
        ts=str(entry.get("ts") or snapshot.get("ts") or snapshot.get("runtime_last_cycle_ts") or ""),
        pair=str(entry.get("symbol") or entry.get("pair") or snapshot.get("pair") or "").upper(),
        state=state,
        action=action,
        reward=float(entry.get("reward", entry.get("score", 0.0)) or 0.0),
        next_state=next_state,
        done=bool(entry.get("done", entry.get("terminated", False) or entry.get("truncated", False))),
        terminal_reason=str(entry.get("terminal_reason") or metadata.get("terminal_reason") or ""),
        policy_version=str(entry.get("policy_version") or metadata.get("policy_version") or snapshot.get("policy_version") or ""),
        feature_service_version=str(
            entry.get("feature_service_version")
            or metadata.get("feature_service_version")
            or snapshot.get("feature_service_version")
            or ""
        ),
        feature_contract_hash=str(
            entry.get("feature_contract_hash")
            or metadata.get("feature_contract_hash")
            or snapshot.get("feature_contract_hash")
            or ""
        ),
        risk_trace_json=_stable_json(risk_trace if isinstance(risk_trace, (dict, list)) else risk_trace or {}),
        execution_trace_json=_stable_json(exec_trace if isinstance(exec_trace, (dict, list)) else exec_trace or {}),
    )


def normalize_replay_transitions(
    snapshots: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    source_name: str = "decision_snapshots",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    rows = _normalize_source_rows(snapshots)
    transitions: list[ReplayTransition] = []
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
    }
    return df, manifest


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
    if not df.empty:
        table = pa.Table.from_pandas(df, preserve_index=False)
    else:
        table = pa.Table.from_pandas(pd.DataFrame(columns=TRANSITION_COLUMNS), preserve_index=False)
    pq.write_table(table, dataset_path, compression="snappy")
    schema_payload = {
        "dataset_name": str(dataset_name),
        "columns": [
            {"name": field.name, "type": str(field.type), "nullable": True}
            for field in table.schema
        ],
    }
    _json_dump(schema_path, schema_payload)
    manifest = ReplayBundleManifest(
        manifest_version="phase6_replay_export_v1",
        dataset_name=str(dataset_name),
        dataset_hash=dataset_hash,
        row_count=int(len(df)),
        episode_count=int(normalized.get("episode_count", 0)),
        pair_count=int(normalized.get("pair_count", 0)),
        source_count=int(normalized.get("source_count", 0)),
        dataset_path=str(dataset_path),
        schema_path=str(schema_path),
        source_paths=list(normalized.get("source_paths") or []),
        metadata=dict(metadata or {}),
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
