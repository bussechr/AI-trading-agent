from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from fxstack.rl.evaluate import evaluate_replay
from fxstack.rl.train_offline import run_offline_training
from fxstack.rl.train_online import run_online_training


def _write_dataset(path: Path) -> Path:
    frame = pd.DataFrame(
        {
            "episode_id": ["e1", "e1", "e2"],
            "step_id": [1, 2, 1],
            "ts": pd.to_datetime(["2026-04-01T00:00:00Z", "2026-04-01T00:05:00Z", "2026-04-02T00:00:00Z"], utc=True),
            "state_price": [1.0, 1.1, 1.2],
            "state_vol": [0.2, 0.3, 0.4],
            "action_side": ["buy", "hold", "sell"],
            "action_size": [1.0, 0.0, 0.5],
            "reward": [0.5, -0.1, 0.3],
            "done": [False, True, True],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False)
    return path


def test_offline_training_emits_mlflow_friendly_artifacts(tmp_path: Path):
    dataset = _write_dataset(tmp_path / "dataset.parquet")
    out = run_offline_training(dataset_path=dataset, out_dir=tmp_path / "offline", run_name="rl-offline-test")

    assert out["status"] == "ok"
    assert Path(out["summary_path"]).exists()
    assert Path(out["transitions_path"]).exists()
    assert Path(out["metrics_path"]).exists()
    metadata = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["dataset_path"] == str(dataset)
    assert metadata["dataset_fingerprint"]


def test_online_training_samples_rows_and_emits_artifacts(tmp_path: Path):
    dataset = _write_dataset(tmp_path / "dataset.parquet")
    out = run_online_training(dataset_path=dataset, out_dir=tmp_path / "online", run_name="rl-online-test", max_rows=2)

    assert out["status"] == "ok"
    assert Path(out["summary_path"]).exists()
    assert Path(out["transitions_path"]).exists()
    metadata = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["dataset_path"] == str(dataset)
    assert metadata["dataset_fingerprint"]


def test_evaluation_compares_against_benchmark(tmp_path: Path):
    dataset = _write_dataset(tmp_path / "dataset.parquet")
    benchmark = _write_dataset(tmp_path / "benchmark.parquet")
    out = evaluate_replay(
        dataset_path=dataset,
        benchmark_path=benchmark,
        out_dir=tmp_path / "eval",
        run_name="rl-eval-test",
    )

    assert out["status"] == "ok"
    assert Path(out["summary_path"]).exists()
    assert Path(out["metrics_path"]).exists()
    assert Path(out["comparison_path"]).exists()
    summary = json.loads(Path(out["summary_path"]).read_text(encoding="utf-8"))
    assert summary["benchmark_rows"] == 3
    assert summary["reward_delta"] == 0.0
    assert summary["state_columns"] == ["state_price", "state_vol"]
    payload = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert payload["dataset_path"] == str(dataset)
    assert payload["benchmark_path"] == str(benchmark)
