from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fxstack.rl.export_replay import TRANSITION_COLUMNS, export_replay_dataset, normalize_replay_transitions
from fxstack.rl.offline_dataset import load_offline_dataset, summarize_offline_dataset
from fxstack.rl.stress_harness import DEFAULT_RL_STRESS_SCENARIOS, apply_stress_scenario, build_stress_bundle


def _snapshots() -> list[dict[str, object]]:
    return [
        {
            "ts": "2026-04-01T00:00:00Z",
            "pair": "EURUSD",
            "episode_id": "ep-1",
            "policy_version": "p6-policy",
            "feature_service_version": "svc-1",
            "feature_contract_hash": "hash-1",
            "decisions_json": [
                {
                    "symbol": "EURUSD",
                    "side": "BUY",
                    "score": 4.2,
                    "confidence": 0.78,
                    "execution_ready": True,
                    "reasons": [],
                    "metadata": {
                        "lifecycle_action": "entry",
                        "risk_trace": {"risk": "ok"},
                        "execution_trace": {"fill": "ok"},
                    },
                },
                {
                    "symbol": "EURUSD",
                    "side": "BUY",
                    "score": 1.2,
                    "confidence": 0.11,
                    "execution_ready": False,
                    "reasons": ["stale"],
                    "metadata": {
                        "lifecycle_action": "hold",
                        "risk_trace": {"risk": "warn"},
                        "execution_trace": {"fill": "skip"},
                    },
                },
            ],
            "diagnostics_json": {"runtime": "fxstack", "feature_serving": {"source": "parquet_fallback"}},
        },
        {
            "ts": "2026-04-01T00:05:00Z",
            "pair": "GBPUSD",
            "episode_id": "ep-2",
            "policy_version": "p6-policy",
            "feature_service_version": "svc-2",
            "feature_contract_hash": "hash-2",
            "rows": [
                {
                    "symbol": "GBPUSD",
                    "side": "SELL",
                    "score": -2.0,
                    "confidence": 0.67,
                    "execution_ready": True,
                    "reward": -1.5,
                    "done": True,
                    "terminal_reason": "session_end",
                    "metadata": {"risk_trace": {"risk": "tight"}, "execution_trace": {"fill": "exit"}},
                }
            ],
        },
    ]


def test_export_replay_dataset_creates_deterministic_parquet_and_manifest(tmp_path: Path) -> None:
    out = export_replay_dataset(_snapshots(), out_dir=tmp_path / "bundle", metadata={"phase": 6})
    manifest = out["manifest"]
    assert out["row_count"] == 3
    assert manifest["manifest_version"] == "phase6_replay_export_v1"
    assert manifest["dataset_hash"]
    assert Path(out["dataset_path"]).exists()
    assert Path(out["manifest_path"]).exists()
    assert Path(out["schema_path"]).exists()

    frame, loaded_manifest = load_offline_dataset(tmp_path / "bundle")
    assert loaded_manifest["dataset_hash"] == manifest["dataset_hash"]
    assert list(frame.columns)[:5] == ["episode_id", "step_id", "ts", "pair", "state"]
    assert {"risk_trace_json", "execution_trace_json"}.issubset(frame.columns)
    assert frame.sort_values(["episode_id", "step_id"]).reset_index(drop=True).equals(frame.reset_index(drop=True))

    summary = summarize_offline_dataset(frame, manifest=loaded_manifest)
    assert summary["rows"] == 3
    assert summary["episodes"] == 2
    assert summary["pairs"] == 2
    assert summary["terminal_reasons"]["session_end"] == 1


@pytest.mark.parametrize(
    "payload, expected_source_paths",
    [
        (
            {
                "items": [
                    {
                        "ts": "2026-04-03T00:00:00Z",
                        "pair": "eurusd",
                        "source_path": "sqlite:///decision_snapshots",
                        "decisions_json": [
                            {
                                "symbol": "EURUSD",
                                "side": "BUY",
                                "score": 4.2,
                                "confidence": 0.78,
                                "execution_ready": True,
                                "metadata": {
                                    "lifecycle_action": "entry",
                                    "risk_trace": {"risk": "ok"},
                                    "execution_trace": {"fill": "ok"},
                                },
                            }
                        ],
                    },
                    {
                        "created_at": "2026-04-03T00:05:00Z",
                        "pair": "gbpusd",
                        "source_path": "sqlite:///decision_snapshots",
                        "decision_rows": [
                            {
                                "symbol": "GBPUSD",
                                "side": "SELL",
                                "score": -1.25,
                                "confidence": 0.67,
                                "execution_ready": False,
                                "reward": -1.5,
                                "done": True,
                                "terminal_reason": "session_end",
                                "metadata": {"risk_trace": {"risk": "tight"}, "execution_trace": {"fill": "exit"}},
                            }
                        ],
                    },
                ]
            },
            ["sqlite:///decision_snapshots"],
        ),
        (
            {
                "transitions": [
                    {
                        "episode_id": "sim-1",
                        "ts": "2026-04-04T00:00:00Z",
                        "pair": "USDJPY",
                        "reward": 1.5,
                        "done": True,
                        "terminal_reason": "model_load",
                        "feature_service_version": "svc-sim",
                        "feature_contract_hash": "sim-hash",
                    },
                    {
                        "episode_id": "sim-2",
                        "ts": "2026-04-04T00:05:00Z",
                        "pair": "AUDUSD",
                        "reward": -0.25,
                        "done": False,
                    },
                ]
            },
            [],
        ),
    ],
)
def test_export_replay_dataset_accepts_live_and_sim_payload_shapes(
    tmp_path: Path,
    payload: dict[str, object],
    expected_source_paths: list[str],
) -> None:
    out = export_replay_dataset(payload, out_dir=tmp_path / "bundle", metadata={"phase": 6})
    manifest = out["manifest"]
    assert out["row_count"] == 2
    assert manifest["row_count"] == 2
    assert manifest["episode_count"] == 2
    assert manifest["pair_count"] == 2
    assert manifest["source_count"] == len(payload.get("items") or payload.get("transitions") or [])
    assert manifest["source_paths"] == expected_source_paths

    frame, loaded_manifest = load_offline_dataset(tmp_path / "bundle")
    assert loaded_manifest["dataset_hash"] == manifest["dataset_hash"]
    assert len(frame) == 2
    assert list(frame.columns) == TRANSITION_COLUMNS

    if expected_source_paths:
        assert frame.iloc[0]["episode_id"] == "EURUSD:2026-04-03T00:00:00Z"
        assert frame.iloc[0]["risk_trace_json"] == "{\"risk\":\"ok\"}"
        assert frame.iloc[0]["execution_trace_json"] == "{\"fill\":\"ok\"}"
    else:
        assert frame.iloc[0]["episode_id"] == "sim-1"
        assert frame.iloc[0]["feature_service_version"] == "svc-sim"
        assert frame.iloc[0]["feature_contract_hash"] == "sim-hash"


def test_normalize_replay_transitions_and_stress_harness(tmp_path: Path) -> None:
    df, meta = normalize_replay_transitions(_snapshots(), source_name="decision_snapshots")
    assert meta["row_count"] == 3
    assert df.iloc[0]["episode_id"] == "ep-1"
    assert df.iloc[0]["step_id"] == 0
    assert df.iloc[0]["policy_version"] == "p6-policy"
    assert df.iloc[0]["feature_service_version"] == "svc-1"
    assert df.iloc[0]["feature_contract_hash"] == "hash-1"

    stressed = apply_stress_scenario(df, DEFAULT_RL_STRESS_SCENARIOS[-1])
    assert "stress_scenario" in stressed.columns
    assert float(stressed["reward"].iloc[0]) < float(df["reward"].iloc[0])

    bundle = build_stress_bundle(_snapshots(), out_dir=tmp_path / "stress", metadata={"phase": 6})
    assert Path(bundle["stress_summary_path"]).exists()
    assert bundle["summary"]["scenario_count"] == len(DEFAULT_RL_STRESS_SCENARIOS)
    assert bundle["summary"]["base"]["rows"] == 3
