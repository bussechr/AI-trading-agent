from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd

from fxstack.rl.evaluate import evaluate_replay
from fxstack.rl.trainer import load_replay_checkpoint
from fxstack.rl.train_offline import run_offline_training
from fxstack.rl.train_online import run_online_training


def _write_dataset(path: Path) -> Path:
    frame = pd.DataFrame(
        {
            "episode_id": ["e1", "e1", "e2"],
            "step_id": [1, 2, 1],
            "ts": pd.to_datetime(["2026-04-01T00:00:00Z", "2026-04-01T00:05:00Z", "2026-04-02T00:00:00Z"], utc=True),
            "pair": ["EURUSD", "EURUSD", "GBPUSD"],
            "state_json": [
                json.dumps({"market": {"spread_bps": 1.0}, "portfolio": {"equity": 10_000.0}}),
                json.dumps({"market": {"spread_bps": 1.2}, "portfolio": {"equity": 10_050.0}}),
                json.dumps({"market": {"spread_bps": 1.4}, "portfolio": {"equity": 9_950.0}}),
            ],
            "action_json": [
                json.dumps({"target_position": 0.5, "close_position": False}),
                json.dumps({"target_position": 0.0, "close_position": True}),
                json.dumps({"target_position": -0.25, "close_position": False}),
            ],
            "market_by_pair_json": [
                json.dumps({"EURUSD": {"spread_bps": 1.0, "freshness_secs": 10.0}}),
                json.dumps({"EURUSD": {"spread_bps": 1.2, "freshness_secs": 10.0}}),
                json.dumps({"GBPUSD": {"spread_bps": 1.4, "freshness_secs": 12.0}}),
            ],
            "features_by_pair_json": [
                json.dumps({"EURUSD": {"vol_20": 0.2, "liquidity_score": 0.8}}),
                json.dumps({"EURUSD": {"vol_20": 0.3, "liquidity_score": 0.78}}),
                json.dumps({"GBPUSD": {"vol_20": 0.4, "liquidity_score": 0.72}}),
            ],
            "portfolio_json": [
                json.dumps({"equity": 10_000.0, "gross_exposure": 0.5, "net_exposure": 0.5, "open_position_count": 1}),
                json.dumps({"equity": 10_050.0, "gross_exposure": 0.25, "net_exposure": 0.0, "open_position_count": 0}),
                json.dumps({"equity": 9_950.0, "gross_exposure": 0.25, "net_exposure": -0.25, "open_position_count": 1}),
            ],
            "pair_actions_json": [
                json.dumps({"EURUSD": {"target_position": 0.5}}),
                json.dumps({"EURUSD": {"target_position": 0.0}}),
                json.dumps({"GBPUSD": {"target_position": -0.25}}),
            ],
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
    assert Path(out["checkpoint_path"]).exists()
    assert Path(out["artifact_manifest_path"]).exists()
    assert Path(out["policy_manifest_path"]).exists()
    manifest = json.loads(Path(out["artifact_manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["artifact_kind"] == "offline_training"
    assert manifest["checkpoint_summary"]["target_name"] == "reward"
    policy_manifest = json.loads(Path(out["policy_manifest_path"]).read_text(encoding="utf-8"))
    assert policy_manifest["artifact_kind"] == "rl_policy"
    assert policy_manifest["primary_policy"] is True
    assert policy_manifest["policy_role"] == "primary"
    assert policy_manifest["checkpoint_path"] == str(Path(out["checkpoint_path"]))
    checkpoint_sha256 = hashlib.sha256(
        Path(out["checkpoint_path"]).read_bytes()
    ).hexdigest()
    assert policy_manifest["manifest_version"] == "rl_policy_manifest_v2"
    assert policy_manifest["checkpoint_content_sha256"] == checkpoint_sha256
    assert policy_manifest["checkpoint_ref"] == {
        "path": str(Path(out["checkpoint_path"])),
        "content_sha256": checkpoint_sha256,
        "runtime_compatible": True,
    }
    metadata = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["dataset_path"] == str(dataset)
    assert metadata["dataset_fingerprint"]
    checkpoint = load_replay_checkpoint(Path(out["checkpoint_path"]))
    assert checkpoint.feature_names
    assert checkpoint.metrics["rl.train.rows"] == 3.0


def test_online_training_samples_rows_and_emits_artifacts(tmp_path: Path):
    dataset = _write_dataset(tmp_path / "dataset.parquet")
    out = run_online_training(dataset_path=dataset, out_dir=tmp_path / "online", run_name="rl-online-test", max_rows=2)

    assert out["status"] == "ok"
    assert Path(out["summary_path"]).exists()
    assert Path(out["transitions_path"]).exists()
    assert Path(out["checkpoint_path"]).exists()
    assert Path(out["artifact_manifest_path"]).exists()
    assert Path(out["policy_manifest_path"]).exists()
    manifest = json.loads(Path(out["artifact_manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["artifact_kind"] == "online_training"
    assert manifest["checkpoint_summary"]["feature_count"] > 0
    policy_manifest = json.loads(Path(out["policy_manifest_path"]).read_text(encoding="utf-8"))
    assert policy_manifest["primary_policy"] is True
    assert policy_manifest["policy_name"] == "rl-online-test"
    metadata = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["dataset_path"] == str(dataset)
    assert metadata["dataset_fingerprint"]


def test_evaluation_compares_against_benchmark(tmp_path: Path):
    dataset = _write_dataset(tmp_path / "dataset.parquet")
    benchmark = _write_dataset(tmp_path / "benchmark.parquet")
    offline = run_offline_training(dataset_path=dataset, out_dir=tmp_path / "offline", run_name="rl-offline-test")
    out = evaluate_replay(
        dataset_path=dataset,
        benchmark_path=benchmark,
        out_dir=tmp_path / "eval",
        run_name="rl-eval-test",
        checkpoint_path=Path(offline["checkpoint_path"]),
    )

    assert out["status"] == "ok"
    assert Path(out["summary_path"]).exists()
    assert Path(out["metrics_path"]).exists()
    assert Path(out["comparison_path"]).exists()
    assert Path(out["predictions_path"]).exists()
    assert Path(out["artifact_manifest_path"]).exists()
    assert Path(out["policy_manifest_path"]).exists()
    summary = json.loads(Path(out["summary_path"]).read_text(encoding="utf-8"))
    assert summary["benchmark_rows"] == 3
    assert summary["reward_delta"] == 0.0
    assert "prediction_mse" in summary
    assert summary["portfolio_rows"] == 3
    assert any(col.endswith("_json") for col in summary["state_columns"])
    manifest = json.loads(Path(out["artifact_manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["artifact_kind"] == "evaluation"
    assert manifest["checkpoint_summary"]["schema_version"] == "rl_linear_checkpoint_v2"
    policy_manifest = json.loads(Path(out["policy_manifest_path"]).read_text(encoding="utf-8"))
    assert policy_manifest["artifact_kind"] == "rl_policy"
    assert policy_manifest["primary_policy"] is True
    assert policy_manifest["discovery"]["checkpoint_path"] == str(Path(offline["checkpoint_path"]))
    payload = json.loads(Path(out["metadata_path"]).read_text(encoding="utf-8"))
    assert payload["dataset_path"] == str(dataset)
    assert payload["benchmark_path"] == str(benchmark)
    assert payload["checkpoint_path"] == str(Path(offline["checkpoint_path"]))
