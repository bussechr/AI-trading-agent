from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.rl._common import RLArtifactBundle, build_rl_policy_manifest, _csv_dump, _dataset_fingerprint, _ensure_dir, _json_dump, _maybe_mlflow_log
from fxstack.rl.offline_dataset import load_offline_dataset
from fxstack.rl.trainer import load_replay_checkpoint, score_replay_frame


def _load_frame(path: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    path = Path(path)
    if path.is_dir():
        return load_offline_dataset(path)
    return pd.read_parquet(path), {}


def evaluate_replay(
    *,
    dataset_path: Path,
    out_dir: Path,
    run_name: str = "rl_research_eval",
    benchmark_path: Path | None = None,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    out_dir = _ensure_dir(out_dir)
    frame, manifest = _load_frame(dataset_path)
    benchmark, _ = _load_frame(benchmark_path) if benchmark_path and Path(benchmark_path).exists() else (pd.DataFrame(), {})
    dataset_fp = _dataset_fingerprint(frame, namespace={"dataset_path": str(dataset_path), "mode": "evaluate"})
    reward = float(frame.get("reward", pd.Series(dtype=float)).sum()) if not frame.empty else 0.0
    benchmark_reward = float(benchmark.get("reward", pd.Series(dtype=float)).sum()) if not benchmark.empty else 0.0
    scored = pd.DataFrame()
    checkpoint = load_replay_checkpoint(checkpoint_path) if checkpoint_path and Path(checkpoint_path).exists() else None
    prediction_metrics: dict[str, float] = {}
    if checkpoint is not None and not frame.empty:
        scored = score_replay_frame(frame, checkpoint)
        target = pd.to_numeric(scored.get("reward", pd.Series(dtype=float)), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        prediction = pd.to_numeric(scored.get("prediction", pd.Series(dtype=float)), errors="coerce").fillna(0.0).to_numpy(dtype=float)
        residual = target - prediction
        prediction_metrics = {
            "prediction_mse": float((residual ** 2).mean()) if len(residual) else 0.0,
            "prediction_mae": float(abs(residual).mean()) if len(residual) else 0.0,
            "prediction_correlation": float(scored["prediction"].corr(scored["reward"])) if len(scored) > 1 and "reward" in scored.columns else 0.0,
            "prediction_directional_accuracy": float((np.sign(target[target != 0.0]) == np.sign(prediction[target != 0.0])).mean()) if (target != 0.0).any() else 0.0,
            "rl.eval.prediction_mse": float((residual ** 2).mean()) if len(residual) else 0.0,
            "rl.eval.prediction_mae": float(abs(residual).mean()) if len(residual) else 0.0,
            "rl.eval.prediction_correlation": float(scored["prediction"].corr(scored["reward"])) if len(scored) > 1 and "reward" in scored.columns else 0.0,
            "rl.eval.prediction_directional_accuracy": float((np.sign(target[target != 0.0]) == np.sign(prediction[target != 0.0])).mean()) if (target != 0.0).any() else 0.0,
        }
    compare = {
        "status": "ok",
        "dataset_path": str(dataset_path),
        "benchmark_path": str(benchmark_path) if benchmark_path else "",
        "rows": int(len(frame)),
        "benchmark_rows": int(len(benchmark)),
        "dataset_fingerprint": dataset_fp,
        "reward_sum": reward,
        "benchmark_reward_sum": benchmark_reward,
        "reward_delta": float(reward - benchmark_reward),
        "done_rate": float(frame.get("done", pd.Series(dtype=bool)).astype(float).mean()) if not frame.empty and "done" in frame.columns else 0.0,
        "portfolio_rows": int(frame["portfolio_json"].astype(str).ne("{}").sum()) if "portfolio_json" in frame.columns else 0,
        "pair_action_rows": int(frame["pair_actions_json"].astype(str).ne("{}").sum()) if "pair_actions_json" in frame.columns else 0,
        "schema_version": str(manifest.get("transition_schema_version") or manifest.get("schema_version") or ""),
        "state_columns": [c for c in frame.columns if c.startswith("state_") or c.endswith("_json")],
    }
    compare.update(prediction_metrics)
    metrics = {
        "rl.eval.rows": float(len(frame)),
        "rl.eval.reward_sum": reward,
        "rl.eval.reward_delta": float(reward - benchmark_reward),
        "rl.eval.done_rate": float(compare["done_rate"]),
        **prediction_metrics,
    }
    comparison_rows = [
        {
            "metric": "reward_sum",
            "candidate": reward,
            "benchmark": benchmark_reward,
            "delta": float(reward - benchmark_reward),
        },
        {
            "metric": "rows",
            "candidate": int(len(frame)),
            "benchmark": int(len(benchmark)),
            "delta": int(len(frame) - len(benchmark)),
        },
    ]
    summary_path = _json_dump(out_dir / "evaluation_summary.json", compare)
    metrics_path = _json_dump(out_dir / "metrics.json", metrics)
    compare_path = _csv_dump(out_dir / "comparison.csv", comparison_rows)
    predictions_path = _csv_dump(out_dir / "predictions.csv", scored.to_dict(orient="records")) if not scored.empty else _csv_dump(out_dir / "predictions.csv", [])
    metadata_path = _json_dump(
        out_dir / "metadata.json",
        {
            "dataset_fingerprint": dataset_fp,
            "dataset_path": str(dataset_path),
            "benchmark_path": str(benchmark_path) if benchmark_path else "",
            "run_name": run_name,
            "checkpoint_path": str(checkpoint_path) if checkpoint_path else "",
        },
    )
    artifact_manifest_path = out_dir / "artifact_bundle.json"
    policy_manifest_path = out_dir / "policy_manifest.json"
    checkpoint_summary = {
        "schema_version": str(getattr(checkpoint, "schema_version", "") or ""),
        "target_name": str(getattr(checkpoint, "target_name", "") or ""),
        "feature_count": int(len(getattr(checkpoint, "feature_names", []) or [])),
        "train_rows": int(getattr(checkpoint, "train_rows", 0) or 0),
        "val_rows": int(getattr(checkpoint, "val_rows", 0) or 0),
        "metrics": dict(getattr(checkpoint, "metrics", {}) or {}),
    } if checkpoint is not None else {}
    artifact_bundle = RLArtifactBundle(
        root=out_dir,
        run_name=run_name,
        status="ok",
        summary_path=summary_path,
        transitions_path=compare_path,
        metrics_path=metrics_path,
        metadata_path=metadata_path,
        mlflow={},
        artifact_kind="evaluation",
        artifact_manifest_path=artifact_manifest_path,
        policy_manifest_path=policy_manifest_path,
        checkpoint_path=Path(checkpoint_path) if checkpoint_path else None,
        artifacts=[summary_path, metrics_path, compare_path, predictions_path, metadata_path, artifact_manifest_path, policy_manifest_path] + ([Path(checkpoint_path)] if checkpoint_path else []),
        checkpoint_summary=checkpoint_summary,
    )
    policy_manifest = build_rl_policy_manifest(
        artifact_bundle=artifact_bundle,
        policy_name=run_name,
        stage="evaluation",
        dataset_path=str(dataset_path),
        dataset_fingerprint=dataset_fp,
        policy_role="primary" if checkpoint is not None else "evaluation",
        policy_manifest_path=policy_manifest_path,
        extra_metadata={
            "mode": "evaluation",
            "benchmark_path": str(benchmark_path or ""),
            "checkpoint_summary": checkpoint_summary,
        },
    )
    mlflow = _maybe_mlflow_log(
        run_name=run_name,
        metrics={k: float(v) for k, v in metrics.items()},
        params={"dataset_path": str(dataset_path), "benchmark_path": str(benchmark_path or ""), "mode": "evaluation"},
        artifacts=[summary_path, metrics_path, compare_path, predictions_path, metadata_path, artifact_manifest_path, policy_manifest_path] + ([Path(checkpoint_path)] if checkpoint_path else []),
    )
    artifact_bundle.mlflow = mlflow
    _json_dump(policy_manifest_path, policy_manifest)
    _json_dump(artifact_manifest_path, artifact_bundle.to_dict())
    return {
        "status": "ok",
        "summary_path": str(summary_path),
        "metrics_path": str(metrics_path),
        "comparison_path": str(compare_path),
        "predictions_path": str(predictions_path),
        "metadata_path": str(metadata_path),
        "artifact_manifest_path": str(artifact_manifest_path),
        "policy_manifest_path": str(policy_manifest_path),
        "dataset_fingerprint": dataset_fp,
        "mlflow": mlflow,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Phase 6 RL research evaluator")
    ap.add_argument("--dataset", required=True, help="Parquet dataset with replay transitions")
    ap.add_argument("--benchmark", default="", help="Optional benchmark parquet dataset")
    ap.add_argument("--checkpoint", default="", help="Optional checkpoint JSON for prediction scoring")
    ap.add_argument("--out-dir", default="artifacts/rl/eval")
    ap.add_argument("--run-name", default="rl_research_eval")
    args = ap.parse_args(argv)
    out = evaluate_replay(
        dataset_path=Path(args.dataset),
        benchmark_path=Path(args.benchmark) if str(args.benchmark).strip() else None,
        checkpoint_path=Path(args.checkpoint) if str(args.checkpoint).strip() else None,
        out_dir=Path(args.out_dir),
        run_name=str(args.run_name),
    )
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
