from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.rl._common import RLArtifactBundle, build_rl_policy_manifest, _csv_dump, _dataset_fingerprint, _ensure_dir, _json_dump, _maybe_mlflow_log
from fxstack.rl.offline_dataset import load_offline_dataset
from fxstack.rl.trainer import fit_replay_policy


def _load_frame(path: Path) -> pd.DataFrame:
    path = Path(path)
    if path.is_dir():
        frame, _ = load_offline_dataset(path)
        return frame
    return pd.read_parquet(path)


def _build_transitions(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["episode_id", "step_id", "state_json", "action_json", "reward", "next_state_json", "done"])
    rows: list[dict[str, Any]] = []
    ordered = frame.sort_values([c for c in ["episode_id", "ts"] if c in frame.columns]).reset_index(drop=True)
    for idx, row in ordered.iterrows():
        next_row = ordered.iloc[idx + 1] if idx + 1 < len(ordered) else None
        state = {k: row[k] for k in ordered.columns if k not in {"reward", "done"}}
        action = {k: row[k] for k in ordered.columns if k.startswith("action_") or k in {"side", "sleeve", "policy_version"}}
        rows.append(
            {
                "episode_id": str(row.get("episode_id", "default")),
                "step_id": int(row.get("step_id", idx)),
                "ts": str(row.get("ts", "")),
                "state_json": json.dumps(state, default=str, sort_keys=True),
                "action_json": json.dumps(action, default=str, sort_keys=True),
                "reward": float(row.get("reward", 0.0) or 0.0),
                "next_state_json": json.dumps({k: next_row[k] for k in ordered.columns}, default=str, sort_keys=True) if next_row is not None else "{}",
                "done": bool(row.get("done", next_row is None)),
            }
        )
    return pd.DataFrame(rows)


def run_offline_training(
    *,
    dataset_path: Path,
    out_dir: Path,
    run_name: str = "offline_rl_research",
    reward_scale: float = 1.0,
) -> dict[str, Any]:
    out_dir = _ensure_dir(out_dir)
    frame = _load_frame(dataset_path)
    transitions = _build_transitions(frame)
    fingerprint = _dataset_fingerprint(frame, namespace={"dataset_path": str(dataset_path), "mode": "offline"})
    training = fit_replay_policy(frame, out_dir=out_dir, run_name=run_name, target_name="reward")
    if not transitions.empty:
        transitions["reward_scaled"] = transitions["reward"].astype(float) * float(reward_scale)
    summary = {
        "status": "ok",
        "mode": "offline",
        "dataset_path": str(dataset_path),
        "rows": int(len(frame)),
        "transitions": int(len(transitions)),
        "fingerprint": fingerprint,
        "reward_scale": float(reward_scale),
        "action_columns": sorted([c for c in frame.columns if c.startswith("action_")]),
        "checkpoint_path": str(training["checkpoint_path"]),
        "trainer_metrics": dict(training.get("metrics") or {}),
    }
    metrics = {
        "rl.offline.rows": float(len(frame)),
        "rl.offline.transitions": float(len(transitions)),
        "rl.offline.reward_mean": float(transitions["reward_scaled"].mean()) if not transitions.empty else 0.0,
        "rl.offline.train_mse": float((training.get("metrics") or {}).get("rl.train.mse", 0.0)),
        "rl.offline.val_mse": float((training.get("metrics") or {}).get("rl.train.val_mse", 0.0)),
        "rl.offline.directional_accuracy": float((training.get("metrics") or {}).get("rl.train.directional_accuracy", 0.0)),
    }
    summary_path = _json_dump(out_dir / "offline_summary.json", summary)
    transitions_path = _csv_dump(out_dir / "transitions.csv", transitions.to_dict(orient="records"))
    metrics_path = _json_dump(out_dir / "metrics.json", metrics)
    metadata_path = _json_dump(
        out_dir / "metadata.json",
        {
            "dataset_fingerprint": fingerprint,
            "dataset_path": str(dataset_path),
            "run_name": run_name,
            "reward_scale": float(reward_scale),
            "checkpoint_path": str(training["checkpoint_path"]),
        },
    )
    artifact_manifest_path = out_dir / "artifact_bundle.json"
    policy_manifest_path = out_dir / "policy_manifest.json"
    checkpoint_summary = {
        "schema_version": str((training.get("checkpoint") or {}).get("schema_version") or ""),
        "target_name": str((training.get("checkpoint") or {}).get("target_name") or "reward"),
        "feature_count": int(len((training.get("checkpoint") or {}).get("feature_names") or [])),
        "train_rows": int((training.get("checkpoint") or {}).get("train_rows", 0) or 0),
        "val_rows": int((training.get("checkpoint") or {}).get("val_rows", 0) or 0),
        "metrics": dict((training.get("checkpoint") or {}).get("metrics") or {}),
    }
    artifact_bundle = RLArtifactBundle(
        root=out_dir,
        run_name=run_name,
        status="ok",
        summary_path=summary_path,
        transitions_path=transitions_path,
        metrics_path=metrics_path,
        metadata_path=metadata_path,
        mlflow={},
        artifact_kind="offline_training",
        artifact_manifest_path=artifact_manifest_path,
        policy_manifest_path=policy_manifest_path,
        checkpoint_path=Path(training["checkpoint_path"]),
        artifacts=[summary_path, transitions_path, metrics_path, metadata_path, artifact_manifest_path, policy_manifest_path, Path(training["checkpoint_path"])],
        checkpoint_summary=checkpoint_summary,
    )
    mlflow = _maybe_mlflow_log(
        run_name=run_name,
        metrics={k: float(v) for k, v in metrics.items()},
        params={"mode": "offline", "dataset_path": str(dataset_path), "reward_scale": float(reward_scale)},
        artifacts=[summary_path, transitions_path, metrics_path, metadata_path, artifact_manifest_path, policy_manifest_path, Path(training["checkpoint_path"])],
    )
    artifact_bundle.mlflow = mlflow
    policy_manifest = build_rl_policy_manifest(
        artifact_bundle=artifact_bundle,
        policy_name=run_name,
        stage="offline_training",
        dataset_path=str(dataset_path),
        dataset_fingerprint=fingerprint,
        policy_role="primary",
        policy_manifest_path=policy_manifest_path,
        extra_metadata={
            "mode": "offline",
            "reward_scale": float(reward_scale),
            "checkpoint_summary": checkpoint_summary,
        },
    )
    _json_dump(policy_manifest_path, policy_manifest)
    _json_dump(artifact_manifest_path, artifact_bundle.to_dict())
    return artifact_bundle.to_dict()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Phase 6 offline RL research trainer")
    ap.add_argument("--dataset", required=True, help="Parquet dataset with transition-like rows")
    ap.add_argument("--out-dir", default="artifacts/rl/offline")
    ap.add_argument("--run-name", default="offline_rl_research")
    ap.add_argument("--reward-scale", type=float, default=1.0)
    args = ap.parse_args(argv)
    out = run_offline_training(
        dataset_path=Path(args.dataset),
        out_dir=Path(args.out_dir),
        run_name=str(args.run_name),
        reward_scale=float(args.reward_scale),
    )
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
