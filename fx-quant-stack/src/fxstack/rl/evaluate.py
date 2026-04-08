from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.rl._common import _csv_dump, _dataset_fingerprint, _ensure_dir, _json_dump, _maybe_mlflow_log


def evaluate_replay(
    *,
    dataset_path: Path,
    out_dir: Path,
    run_name: str = "rl_research_eval",
    benchmark_path: Path | None = None,
) -> dict[str, Any]:
    out_dir = _ensure_dir(out_dir)
    frame = pd.read_parquet(dataset_path)
    benchmark = pd.read_parquet(benchmark_path) if benchmark_path and benchmark_path.exists() else pd.DataFrame()
    dataset_fp = _dataset_fingerprint(frame, namespace={"dataset_path": str(dataset_path), "mode": "evaluate"})
    reward = float(frame.get("reward", pd.Series(dtype=float)).sum()) if not frame.empty else 0.0
    benchmark_reward = float(benchmark.get("reward", pd.Series(dtype=float)).sum()) if not benchmark.empty else 0.0
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
        "state_columns": [c for c in frame.columns if c.startswith("state_")],
    }
    metrics = {
        "rl.eval.rows": float(len(frame)),
        "rl.eval.reward_sum": reward,
        "rl.eval.reward_delta": float(reward - benchmark_reward),
        "rl.eval.done_rate": float(compare["done_rate"]),
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
    metadata_path = _json_dump(
        out_dir / "metadata.json",
        {
            "dataset_fingerprint": dataset_fp,
            "dataset_path": str(dataset_path),
            "benchmark_path": str(benchmark_path) if benchmark_path else "",
            "run_name": run_name,
        },
    )
    mlflow = _maybe_mlflow_log(
        run_name=run_name,
        metrics={k: float(v) for k, v in metrics.items()},
        params={"dataset_path": str(dataset_path), "benchmark_path": str(benchmark_path or ""), "mode": "evaluation"},
        artifacts=[summary_path, metrics_path, compare_path, metadata_path],
    )
    return {
        "status": "ok",
        "summary_path": str(summary_path),
        "metrics_path": str(metrics_path),
        "comparison_path": str(compare_path),
        "metadata_path": str(metadata_path),
        "dataset_fingerprint": dataset_fp,
        "mlflow": mlflow,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Phase 6 RL research evaluator")
    ap.add_argument("--dataset", required=True, help="Parquet dataset with replay transitions")
    ap.add_argument("--benchmark", default="", help="Optional benchmark parquet dataset")
    ap.add_argument("--out-dir", default="artifacts/rl/eval")
    ap.add_argument("--run-name", default="rl_research_eval")
    args = ap.parse_args(argv)
    out = evaluate_replay(
        dataset_path=Path(args.dataset),
        benchmark_path=Path(args.benchmark) if str(args.benchmark).strip() else None,
        out_dir=Path(args.out_dir),
        run_name=str(args.run_name),
    )
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
