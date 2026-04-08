from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from fxstack.rl._common import _ensure_dir
from fxstack.rl.export_replay import TRANSITION_COLUMNS


@dataclass(slots=True)
class OfflineReplayDataset:
    dataset_path: Path
    manifest_path: Path
    schema_path: Path
    row_count: int
    episode_count: int
    pair_count: int
    dataset_hash: str
    source_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("dataset_path", "manifest_path", "schema_path"):
            payload[key] = str(payload[key])
        return payload


def load_manifest(path: Path) -> dict[str, Any]:
    return dict(json.loads(Path(path).read_text(encoding="utf-8")) or {})


def load_offline_dataset(bundle_dir: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "replay_manifest.json"
    manifest = load_manifest(manifest_path)
    dataset_path = Path(manifest.get("dataset_path") or (bundle_dir / "replay_transitions.parquet"))
    frame = pd.read_parquet(dataset_path) if dataset_path.exists() else pd.DataFrame(columns=TRANSITION_COLUMNS)
    return frame, manifest


def build_offline_dataset(
    snapshots: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    out_dir: Path,
    dataset_name: str = "replay_transitions",
    source_name: str = "decision_snapshots",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from fxstack.rl.export_replay import export_replay_dataset

    return export_replay_dataset(
        snapshots,
        out_dir=out_dir,
        dataset_name=dataset_name,
        source_name=source_name,
        metadata=metadata,
    )


def summarize_offline_dataset(frame: pd.DataFrame, *, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    if frame.empty:
        return {
            "status": "empty",
            "rows": 0,
            "episodes": 0,
            "pairs": 0,
            "reward_sum": 0.0,
            "reward_mean": 0.0,
            "done_rate": 0.0,
            "terminal_reasons": {},
            "policy_versions": {},
            "feature_service_versions": {},
            "feature_contract_hashes": {},
            "portfolio_rows": 0,
            "pair_action_rows": 0,
        }
    df = frame.copy()
    if "episode_id" not in df.columns:
        df["episode_id"] = "episode-0"
    if "done" not in df.columns:
        df["done"] = False
    if "reward" not in df.columns:
        df["reward"] = 0.0
    summary = {
        "status": "ok",
        "rows": int(len(df)),
        "episodes": int(df["episode_id"].nunique()),
        "pairs": int(df["pair"].nunique()) if "pair" in df.columns else 0,
        "reward_sum": float(df["reward"].sum()),
        "reward_mean": float(df["reward"].mean()),
        "done_rate": float(df["done"].astype(float).mean()),
        "terminal_reasons": df["terminal_reason"].value_counts(dropna=False).to_dict() if "terminal_reason" in df.columns else {},
        "policy_versions": df["policy_version"].value_counts(dropna=False).to_dict() if "policy_version" in df.columns else {},
        "feature_service_versions": df["feature_service_version"].value_counts(dropna=False).to_dict() if "feature_service_version" in df.columns else {},
        "feature_contract_hashes": df["feature_contract_hash"].value_counts(dropna=False).to_dict() if "feature_contract_hash" in df.columns else {},
        "portfolio_rows": int(df["portfolio_json"].astype(str).ne("{}").sum()) if "portfolio_json" in df.columns else 0,
        "pair_action_rows": int(df["pair_actions_json"].astype(str).ne("{}").sum()) if "pair_actions_json" in df.columns else 0,
    }
    if manifest:
        summary["manifest"] = dict(manifest)
        summary["schema_version"] = str(manifest.get("transition_schema_version") or manifest.get("schema_version") or "")
    return summary


def export_bundle_from_snapshots(
    snapshots: dict[str, Any] | Iterable[dict[str, Any]],
    *,
    out_dir: Path,
    dataset_name: str = "replay_transitions",
    source_name: str = "decision_snapshots",
    metadata: dict[str, Any] | None = None,
) -> OfflineReplayDataset:
    bundle = build_offline_dataset(
        snapshots,
        out_dir=out_dir,
        dataset_name=dataset_name,
        source_name=source_name,
        metadata=metadata,
    )
    return OfflineReplayDataset(
        dataset_path=Path(bundle["dataset_path"]),
        manifest_path=Path(bundle["manifest_path"]),
        schema_path=Path(bundle["schema_path"]),
        row_count=int(bundle.get("row_count", 0)),
        episode_count=int(bundle.get("episode_count", 0)),
        pair_count=int(bundle.get("pair_count", 0)),
        dataset_hash=str(bundle.get("dataset_hash", "")),
        source_name=str(source_name),
        metadata=dict(metadata or {}),
    )


def ensure_bundle_dir(path: Path) -> Path:
    return _ensure_dir(path)
