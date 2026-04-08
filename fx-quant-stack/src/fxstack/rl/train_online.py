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


def _sample_online_frame(frame: pd.DataFrame, *, max_rows: int) -> pd.DataFrame:
    if frame.empty:
        return frame
    ordered = frame.sort_values([c for c in ["episode_id", "ts"] if c in frame.columns]).reset_index(drop=True)
    return ordered.tail(max(1, int(max_rows))).copy()


def run_online_training(
    *,
    dataset_path: Path,
    out_dir: Path,
    run_name: str = "online_rl_research",
    max_rows: int = 5000,
    exploration_rate: float = 0.1,
) -> dict[str, Any]:
    out_dir = _ensure_dir(out_dir)
    frame = _load_frame(dataset_path)
    sampled = _sample_online_frame(frame, max_rows=int(max_rows))
    reward_estimate = float(sampled.get("reward", pd.Series(dtype=float)).mean()) if not sampled.empty else 0.0
    fingerprint = _dataset_fingerprint(sampled, namespace={"dataset_path": str(dataset_path), "mode": "online"})
    training = fit_replay_policy(sampled, out_dir=out_dir, run_name=run_name, target_name="reward", validation_fraction=0.25)
    summary = {
        "status": "ok",
        "mode": "online",
        "dataset_path": str(dataset_path),
        "rows": int(len(sampled)),
        "fingerprint": fingerprint,
        "exploration_rate": float(exploration_rate),
        "reward_estimate": reward_estimate,
        "checkpoint_path": str(training["checkpoint_path"]),
        "trainer_metrics": dict(training.get("metrics") or {}),
    }
    metrics = {
        "rl.online.rows": float(len(sampled)),
        "rl.online.reward_mean": reward_estimate,
        "rl.online.exploration_rate": float(exploration_rate),
        "rl.online.train_mse": float((training.get("metrics") or {}).get("rl.train.mse", 0.0)),
        "rl.online.val_mse": float((training.get("metrics") or {}).get("rl.train.val_mse", 0.0)),
        "rl.online.directional_accuracy": float((training.get("metrics") or {}).get("rl.train.directional_accuracy", 0.0)),
    }
    summary_path = _json_dump(out_dir / "online_summary.json", summary)
    transitions_path = _csv_dump(
        out_dir / "sampled_transitions.csv",
        sampled.to_dict(orient="records"),
    )
    metrics_path = _json_dump(out_dir / "metrics.json", metrics)
    metadata_path = _json_dump(
        out_dir / "metadata.json",
        {
            "dataset_fingerprint": fingerprint,
            "dataset_path": str(dataset_path),
            "run_name": run_name,
            "exploration_rate": float(exploration_rate),
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
        artifact_kind="online_training",
        artifact_manifest_path=artifact_manifest_path,
        policy_manifest_path=policy_manifest_path,
        checkpoint_path=Path(training["checkpoint_path"]),
        artifacts=[summary_path, transitions_path, metrics_path, metadata_path, artifact_manifest_path, policy_manifest_path, Path(training["checkpoint_path"])],
        checkpoint_summary=checkpoint_summary,
    )
    mlflow = _maybe_mlflow_log(
        run_name=run_name,
        metrics={k: float(v) for k, v in metrics.items()},
        params={"mode": "online", "dataset_path": str(dataset_path), "exploration_rate": float(exploration_rate)},
        artifacts=[summary_path, transitions_path, metrics_path, metadata_path, artifact_manifest_path, policy_manifest_path, Path(training["checkpoint_path"])],
    )
    artifact_bundle.mlflow = mlflow
    policy_manifest = build_rl_policy_manifest(
        artifact_bundle=artifact_bundle,
        policy_name=run_name,
        stage="online_training",
        dataset_path=str(dataset_path),
        dataset_fingerprint=fingerprint,
        policy_role="primary",
        policy_manifest_path=policy_manifest_path,
        extra_metadata={
            "mode": "online",
            "exploration_rate": float(exploration_rate),
            "checkpoint_summary": checkpoint_summary,
        },
    )
    _json_dump(policy_manifest_path, policy_manifest)
    _json_dump(artifact_manifest_path, artifact_bundle.to_dict())
    return artifact_bundle.to_dict()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Phase 6 online RL research trainer")
    ap.add_argument("--dataset", required=True, help="Parquet dataset with replay rows")
    ap.add_argument("--out-dir", default="artifacts/rl/online")
    ap.add_argument("--run-name", default="online_rl_research")
    ap.add_argument("--max-rows", type=int, default=5000)
    ap.add_argument("--exploration-rate", type=float, default=0.1)
    args = ap.parse_args(argv)
    out = run_online_training(
        dataset_path=Path(args.dataset),
        out_dir=Path(args.out_dir),
        run_name=str(args.run_name),
        max_rows=int(args.max_rows),
        exploration_rate=float(args.exploration_rate),
    )
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
