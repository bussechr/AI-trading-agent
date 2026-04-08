from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from fxstack.rl.export_replay import TRANSITION_COLUMNS, export_replay_dataset, normalize_replay_transitions
from fxstack.rl.contracts import RLReplayContext
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
            "market_by_pair": {
                "EURUSD": {"spread_bps": 1.0, "freshness_secs": 9.0, "volatility": 0.2, "liquidity_score": 0.8, "session_bucket": "london_open"},
                "GBPUSD": {"spread_bps": 1.2, "freshness_secs": 10.0, "volatility": 0.25, "liquidity_score": 0.76, "session_bucket": "london_open"},
            },
            "features_by_pair": {
                "EURUSD": {"vol_20": 0.2, "liquidity_score": 0.8},
                "GBPUSD": {"vol_20": 0.25, "liquidity_score": 0.76},
            },
            "portfolio": {"equity": 10_000.0, "gross_exposure": 0.5, "net_exposure": 0.25, "open_position_count": 1},
            "policy_context": {
                "portfolio_concentration": 0.5,
                "portfolio_risk_pressure": 0.27,
                "portfolio_pair_pressure": 0.31,
                "portfolio_session_pressure": 0.18,
                "portfolio_sleeve_pressure": 0.44,
                "portfolio_correlation_pressure": 0.42,
                "concentration": {"top_symbol": "EURUSD", "top_symbol_share": 0.58},
                "correlation": {"mode": "hybrid", "value": 0.61},
                "budget": {"risk_budget_pct": 0.12},
                "stress": {"replacement_pressure": 0.33},
                "governance": {"current_sleeve": "swing"},
                "session_counts": {"london_open": 1},
            },
            "decisions_json": [
                {
                    "symbol": "EURUSD",
                    "side": "BUY",
                    "score": 4.2,
                    "confidence": 0.78,
                    "execution_ready": True,
                    "reasons": [],
                    "pair_actions": {"EURUSD": {"target_position": 0.5, "tighten_stop": True}},
                    "metadata": {
                        "lifecycle_action": "entry",
                        "lifecycle_reason": "cross_pair_stack",
                        "lifecycle_route_reason": "rl_primary_flip_position",
                        "rl_lifecycle_reason": "rl_primary_flip_position",
                        "lifecycle_action_score": 0.81,
                        "replacement_urgency": 0.91,
                        "flip_intent": True,
                        "resize_intent": False,
                        "rl_lifecycle_target_position": -0.5,
                        "position_side": "long",
                        "cross_pair_rank_position": 1,
                        "cross_pair_influence_score": 0.93,
                        "cross_pair_recommendation_strength": 0.95,
                        "cross_pair_influenced_by_pairs": ["GBPUSD"],
                        "cross_pair_reason_codes": ["local_edge", "peer_confluence"],
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
            "portfolio": {
                "equity": 10_000.0,
                "gross_exposure": 0.75,
                "net_exposure": -0.1,
                "open_position_count": 2,
                "metadata": {
                    "concentration": {"top_symbol": "GBPUSD", "top_symbol_share": 0.44},
                    "correlation": {"mode": "realized", "value": 0.57},
                    "budget": {"risk_budget_pct": 0.1},
                    "stress": {"replacement_pressure": 0.67},
                    "governance": {"current_sleeve": "intraday"},
                    "portfolio_risk_pressure": 0.58,
                    "portfolio_pair_pressure": 0.61,
                    "portfolio_session_pressure": 0.22,
                    "portfolio_sleeve_pressure": 0.49,
                    "portfolio_correlation_pressure": 0.58,
                },
            },
            "policy_context": {
                "portfolio_concentration": 0.4,
                "session_counts": {"new_york": 2},
                "portfolio_risk_pressure": 0.58,
                "portfolio_pair_pressure": 0.61,
                "portfolio_session_pressure": 0.22,
                "portfolio_sleeve_pressure": 0.49,
            },
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
                    "metadata": {
                        "risk_trace": {"risk": "tight"},
                        "execution_trace": {"fill": "exit"},
                        "lifecycle_action": "partial_tp",
                        "lifecycle_reason": "rl_primary_resize_down",
                        "lifecycle_route_reason": "rl_primary_resize_down",
                        "rl_lifecycle_reason": "rl_primary_resize_down",
                        "resize_intent": True,
                        "flip_intent": False,
                        "rl_lifecycle_target_position": 0.25,
                        "position_side": "long",
                        "close_lots": 0.25,
                        "replacement_urgency": 0.67,
                    },
                }
            ],
        },
    ]


def test_export_replay_dataset_creates_deterministic_parquet_and_manifest(tmp_path: Path) -> None:
    out = export_replay_dataset(_snapshots(), out_dir=tmp_path / "bundle", metadata={"phase": 6})
    manifest = out["manifest"]
    assert out["row_count"] == 3
    assert manifest["manifest_version"] == "phase6_replay_export_v2"
    assert manifest["transition_schema_version"] == "replay_transition_v2"
    assert manifest["dataset_hash"]
    assert Path(out["dataset_path"]).exists()
    assert Path(out["manifest_path"]).exists()
    assert Path(out["schema_path"]).exists()
    assert manifest["metadata"]["replay_context_schema_version"] == "portfolio_rl_context_v2"
    assert manifest["metadata"]["replay_context_columns"] == ["lifecycle_json", "portfolio_context_json"]

    frame, loaded_manifest = load_offline_dataset(tmp_path / "bundle")
    assert loaded_manifest["dataset_hash"] == manifest["dataset_hash"]
    assert list(frame.columns)[:5] == ["episode_id", "step_id", "ts", "pair", "schema_version"]
    assert {"state_json", "market_by_pair_json", "portfolio_json", "pair_actions_json", "risk_trace_json", "execution_trace_json"}.issubset(frame.columns)
    assert frame.sort_values(["episode_id", "step_id"]).reset_index(drop=True).equals(frame.reset_index(drop=True))

    summary = summarize_offline_dataset(frame, manifest=loaded_manifest)
    assert summary["rows"] == 3
    assert summary["episodes"] == 2
    assert summary["pairs"] == 2
    assert summary["terminal_reasons"]["session_end"] == 1
    assert summary["portfolio_rows"] == 3
    assert summary["pair_action_rows"] >= 1
    assert summary["schema_version"] == "replay_transition_v2"


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
    assert manifest["transition_schema_version"] == "replay_transition_v2"

    frame, loaded_manifest = load_offline_dataset(tmp_path / "bundle")
    assert loaded_manifest["dataset_hash"] == manifest["dataset_hash"]
    assert len(frame) == 2
    assert list(frame.columns) == TRANSITION_COLUMNS

    if expected_source_paths:
        assert frame.iloc[0]["episode_id"] == "EURUSD:2026-04-03T00:00:00Z"
        assert frame.iloc[0]["risk_trace_json"] == "{\"risk\":\"ok\"}"
        assert frame.iloc[0]["execution_trace_json"] == "{\"fill\":\"ok\"}"
        assert frame.iloc[0]["portfolio_json"].startswith("{")
    else:
        assert frame.iloc[0]["episode_id"] == "sim-1"
        assert frame.iloc[0]["feature_service_version"] == "svc-sim"
        assert frame.iloc[0]["feature_contract_hash"] == "sim-hash"
        assert frame.iloc[0]["state_json"].startswith("{")


def test_normalize_replay_transitions_and_stress_harness(tmp_path: Path) -> None:
    df, meta = normalize_replay_transitions(_snapshots(), source_name="decision_snapshots")
    assert meta["row_count"] == 3
    assert df.iloc[0]["episode_id"] == "ep-1"
    assert df.iloc[0]["step_id"] == 0
    assert df.iloc[0]["policy_version"] == "p6-policy"
    assert df.iloc[0]["feature_service_version"] == "svc-1"
    assert df.iloc[0]["feature_contract_hash"] == "hash-1"
    assert df.iloc[0]["portfolio_json"].startswith("{")
    assert "\"cross_pair_rank_position\":1" in df.iloc[0]["metadata_json"]
    assert meta["transition_schema_version"] == "replay_transition_v2"

    stressed = apply_stress_scenario(df, DEFAULT_RL_STRESS_SCENARIOS[-1])
    assert "stress_scenario" in stressed.columns
    assert float(stressed["reward"].iloc[0]) < float(df["reward"].iloc[0])

    bundle = build_stress_bundle(_snapshots(), out_dir=tmp_path / "stress", metadata={"phase": 6})
    assert Path(bundle["stress_summary_path"]).exists()
    assert bundle["summary"]["scenario_count"] == len(DEFAULT_RL_STRESS_SCENARIOS)
    assert bundle["summary"]["base"]["rows"] == 3


def test_replay_context_columns_preserve_lifecycle_and_portfolio_context(tmp_path: Path) -> None:
    export_replay_dataset(_snapshots(), out_dir=tmp_path / "bundle", metadata={"phase": 6})
    frame, manifest = load_offline_dataset(tmp_path / "bundle")

    assert manifest["metadata"]["replay_context_schema_version"] == "portfolio_rl_context_v2"
    assert manifest["metadata"]["replay_context_columns"] == ["lifecycle_json", "portfolio_context_json"]
    assert list(frame.columns) == TRANSITION_COLUMNS

    first = frame.iloc[0]
    second = frame.iloc[2]
    lifecycle = json.loads(first["lifecycle_json"])
    portfolio_context = json.loads(first["portfolio_context_json"])
    fallback_portfolio_context = json.loads(second["portfolio_context_json"])
    metadata = json.loads(first["metadata_json"])

    assert lifecycle["lifecycle_action"] == "entry"
    assert lifecycle["lifecycle_reason"] == "cross_pair_stack"
    assert lifecycle["lifecycle_route_reason"] == "rl_primary_flip_position"
    assert lifecycle["rl_lifecycle_reason"] == "rl_primary_flip_position"
    assert lifecycle["flip_intent"] is True
    assert lifecycle["resize_intent"] is False
    assert lifecycle["rl_lifecycle_target_position"] == pytest.approx(-0.5)
    assert lifecycle["replacement_urgency"] == pytest.approx(0.91)
    assert portfolio_context["concentration"]["top_symbol"] == "EURUSD"
    assert portfolio_context["correlation"]["mode"] == "hybrid"
    assert portfolio_context["portfolio_correlation_pressure"] == pytest.approx(0.42)
    assert fallback_portfolio_context["governance"]["current_sleeve"] == "intraday"
    assert fallback_portfolio_context["portfolio_risk_pressure"] == pytest.approx(0.58)
    assert fallback_portfolio_context["replacement_urgency"] == pytest.approx(0.67)
    assert metadata["lifecycle_action"] == "entry"
    assert metadata["replacement_urgency"] == pytest.approx(0.91)
    assert metadata["replay_context_schema_version"] == "portfolio_rl_context_v2"
    assert metadata["lifecycle_route_reason"] == "rl_primary_flip_position"
    assert metadata["flip_intent"] is True
    assert metadata["resize_intent"] is False

    ctx = RLReplayContext.from_dict(
        {
            "lifecycle_json": lifecycle,
            "portfolio_context_json": portfolio_context,
            "metadata_json": metadata,
        }
    )
    assert ctx.lifecycle_json["lifecycle_action"] == "entry"
    assert ctx.portfolio_context_json["budget"]["risk_budget_pct"] == pytest.approx(0.12)

    second_lifecycle = json.loads(second["lifecycle_json"])
    second_metadata = json.loads(second["metadata_json"])
    assert second_lifecycle["lifecycle_action"] == "partial_tp"
    assert second_lifecycle["lifecycle_route_reason"] == "rl_primary_resize_down"
    assert second_lifecycle["resize_intent"] is True
    assert second_lifecycle["flip_intent"] is False
    assert second_metadata["rl_lifecycle_reason"] == "rl_primary_resize_down"
