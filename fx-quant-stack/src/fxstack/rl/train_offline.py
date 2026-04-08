from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.rl._common import RLArtifactBundle, _csv_dump, _dataset_fingerprint, _ensure_dir, _json_dump, _maybe_mlflow_log


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
    frame = pd.read_parquet(dataset_path)
    transitions = _build_transitions(frame)
    fingerprint = _dataset_fingerprint(frame, namespace={"dataset_path": str(dataset_path), "mode": "offline"})
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
    }
    metrics = {
        "rl.offline.rows": float(len(frame)),
        "rl.offline.transitions": float(len(transitions)),
        "rl.offline.reward_mean": float(transitions["reward_scaled"].mean()) if not transitions.empty else 0.0,
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
        },
    )
    mlflow = _maybe_mlflow_log(
        run_name=run_name,
        metrics={k: float(v) for k, v in metrics.items()},
        params={"mode": "offline", "dataset_path": str(dataset_path), "reward_scale": float(reward_scale)},
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
