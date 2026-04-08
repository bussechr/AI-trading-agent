from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.rl._common import RLArtifactBundle, _csv_dump, _dataset_fingerprint, _ensure_dir, _json_dump, _maybe_mlflow_log


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
    frame = pd.read_parquet(dataset_path)
    sampled = _sample_online_frame(frame, max_rows=int(max_rows))
    reward_estimate = float(sampled.get("reward", pd.Series(dtype=float)).mean()) if not sampled.empty else 0.0
    fingerprint = _dataset_fingerprint(sampled, namespace={"dataset_path": str(dataset_path), "mode": "online"})
    summary = {
        "status": "ok",
        "mode": "online",
        "dataset_path": str(dataset_path),
        "rows": int(len(sampled)),
        "fingerprint": fingerprint,
        "exploration_rate": float(exploration_rate),
        "reward_estimate": reward_estimate,
    }
    metrics = {
        "rl.online.rows": float(len(sampled)),
        "rl.online.reward_mean": reward_estimate,
        "rl.online.exploration_rate": float(exploration_rate),
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
        },
    )
    mlflow = _maybe_mlflow_log(
        run_name=run_name,
        metrics={k: float(v) for k, v in metrics.items()},
        params={"mode": "online", "dataset_path": str(dataset_path), "exploration_rate": float(exploration_rate)},
        artifacts=[summary_path, transitions_path, metrics_path, metadata_path],
    )
    return RLArtifactBundle(
        root=out_dir,
        run_name=run_name,
        status="ok",
        summary_path=summary_path,
        transitions_path=transitions_path,
        metrics_path=metrics_path,
        metadata_path=metadata_path,
        mlflow=mlflow,
    ).to_dict()


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
