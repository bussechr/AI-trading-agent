from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fxstack.rl import build_portfolio_rl_proposal_bundle
from fxstack.rl.trainer import RLLinearCheckpoint


def _write_checkpoint(
    path: Path,
    *,
    bias: float,
    train_rows: int,
    val_rows: int,
    mse: float,
    run_name: str,
) -> Path:
    return RLLinearCheckpoint(
        target_name="reward",
        feature_names=["allocator_score"],
        feature_means=[0.0],
        feature_scales=[1.0],
        weights=[1.0],
        bias=bias,
        train_rows=train_rows,
        val_rows=val_rows,
        metrics={"rl.train.mse": mse},
        metadata={"run_name": run_name},
    ).save(path)


def test_portfolio_rl_proposal_bundle_uses_supervised_fallback_when_checkpoint_missing() -> None:
    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "metadata": {
                "ts": "2026-04-08T00:00:00Z",
                "entry_ready": True,
                "strict_entry_ready": True,
                "adaptive_shadow_would_trade": True,
                "allocator_score": 0.82,
                "conviction_score": 0.74,
                "trade_prob": 0.79,
                "spread_bps": 1.1,
                "freshness_secs": 10.0,
                "vol_20": 0.25,
                "liquidity_score": 0.77,
                "session_bucket": "london_open",
                "regime_bucket": "trend",
                "cross_pair_rank_position": 1,
                "cross_pair_influence_score": 0.91,
                "cross_pair_recommendation_strength": 0.94,
                "cross_pair_influenced_by_pairs": ["GBPUSD", "USDJPY"],
                "cross_pair_reason_codes": ["local_edge", "basket_alignment"],
                "cross_pair_soft_block": False,
                "cross_pair_hard_block": False,
            },
        },
        {
            "symbol": "GBPUSD",
            "side": "SELL",
            "metadata": {
                "ts": "2026-04-08T00:00:00Z",
                "has_open_position": True,
                "adaptive_shadow_live_divergence": "open_position",
                "lifecycle_action": "hold",
                "allocator_score": 0.31,
                "conviction_score": 0.22,
                "trade_prob": 0.28,
                "spread_bps": 1.4,
                "freshness_secs": 9.0,
                "vol_20": 0.18,
                "liquidity_score": 0.71,
                "session_bucket": "london_open",
                "regime_bucket": "mean_revert",
                "cross_pair_rank_position": 2,
                "cross_pair_influence_score": 0.22,
                "cross_pair_recommendation_strength": 0.27,
                "cross_pair_influenced_by_pairs": ["EURUSD"],
                "cross_pair_reason_codes": ["weak_cross_pair_signal"],
                "cross_pair_soft_block": True,
                "cross_pair_hard_block": False,
            },
        },
    ]
    bundle = build_portfolio_rl_proposal_bundle(
        ts="2026-04-08T00:00:00Z",
        decisions=decisions,
        portfolio={
            "equity": 10_000.0,
            "open_position_count": 1,
            "pair_position_count": 1,
            "gross_exposure": 0.5,
            "net_exposure": 0.25,
            "concentration": {"hhI": 0.41},
            "correlation": {"mode": "hybrid"},
            "budget": {"risk_budget_pct": 0.12},
            "stress": {"replacement_pressure": 0.33},
            "governance": {"current_sleeve": "trend"},
        },
        policy_context={"runtime_mode": "supervised_legacy", "supervised_fallback_required": True},
        supervised_fallback_required=True,
    )

    assert bundle.source == "supervised_fallback"
    assert bundle.supervised_fallback_used is True
    assert bundle.checkpoint_summary == {}
    assert bundle.observation["portfolio"]["equity"] == 10_000.0
    assert bundle.observation["portfolio"]["open_position_count"] == 1
    assert bundle.observation["portfolio"]["metadata"]["concentration"] == {"hhI": 0.41}
    assert bundle.observation["portfolio"]["metadata"]["correlation"] == {"mode": "hybrid"}
    assert bundle.observation["portfolio"]["metadata"]["budget"] == {"risk_budget_pct": 0.12}
    assert bundle.observation["portfolio"]["metadata"]["stress"] == {"replacement_pressure": 0.33}
    assert bundle.observation["portfolio"]["metadata"]["governance"] == {"current_sleeve": "trend"}
    assert bundle.observation["policy_context"]["supervised_fallback_required"] is True
    assert bundle.diagnostics["execution_authority"] == "risk_kernel"
    assert bundle.diagnostics["artifact_discovery"]["checkpoint_loaded"] is False
    assert bundle.proposals_by_pair["EURUSD"].metadata["entry_supported"] is True
    assert bundle.proposals_by_pair["GBPUSD"].metadata["entry_supported"] is False
    assert set(bundle.proposals_by_pair) == {"EURUSD", "GBPUSD"}


def test_portfolio_rl_proposal_bundle_discovers_checkpoint_from_policy_context(tmp_path: Path) -> None:
    checkpoint_path = _write_checkpoint(
        tmp_path / "checkpoint.json",
        bias=0.25,
        train_rows=1,
        val_rows=0,
        mse=0.0,
        run_name="policy-context-checkpoint",
    )
    checkpoint_content_sha256 = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()

    bundle = build_portfolio_rl_proposal_bundle(
        ts="2026-04-08T00:00:00Z",
        decisions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "metadata": {
                    "ts": "2026-04-08T00:00:00Z",
                    "entry_ready": True,
                    "strict_entry_ready": True,
                    "allocator_score": 0.6,
                    "trade_prob": 0.5,
                    "spread_bps": 1.0,
                    "freshness_secs": 8.0,
                    "liquidity_score": 0.75,
                },
            }
        ],
        portfolio={"equity": 10_000.0},
        policy_context={
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_content_sha256": checkpoint_content_sha256,
        },
        supervised_fallback_required=True,
    )

    assert bundle.source == "rl_checkpoint"
    assert bundle.supervised_fallback_used is False
    assert bundle.checkpoint_loaded is True
    assert bundle.checkpoint_summary["feature_count"] == 1
    assert bundle.checkpoint_summary["metadata"]["run_name"] == "policy-context-checkpoint"
    eurusd = bundle.proposals_by_pair["EURUSD"]
    assert eurusd.source == "rl_checkpoint"
    assert eurusd.supervised_fallback_used is False
    assert eurusd.metadata["checkpoint_score"] > 0.0


def test_portfolio_rl_proposal_bundle_discovers_checkpoint_from_policy_manifest(tmp_path: Path) -> None:
    checkpoint_path = _write_checkpoint(
        tmp_path / "checkpoint.json",
        bias=0.5,
        train_rows=2,
        val_rows=1,
        mse=0.01,
        run_name="manifest-checkpoint",
    )
    checkpoint_content_sha256 = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    policy_manifest_path = tmp_path / "policy_manifest.json"
    policy_manifest_path.write_text(
        json.dumps(
            {
                "manifest_version": "rl_policy_manifest_v2",
                "artifact_kind": "rl_policy",
                "policy_role": "primary",
                "primary_policy": True,
                "policy_name": "manifest-policy",
                "policy_family": "rl_replay_linear",
                "stage": "offline_training",
                "checkpoint_path": str(checkpoint_path),
                "checkpoint_content_sha256": checkpoint_content_sha256,
                "checkpoint_ref": {
                    "path": str(checkpoint_path),
                    "content_sha256": checkpoint_content_sha256,
                    "runtime_compatible": True,
                },
                "checkpoint_exists": True,
                "checkpoint_summary": {"schema_version": "rl_linear_checkpoint_v2"},
                "artifact_paths": {"checkpoint_path": str(checkpoint_path)},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    bundle = build_portfolio_rl_proposal_bundle(
        ts="2026-04-08T00:00:00Z",
        decisions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "metadata": {
                    "ts": "2026-04-08T00:00:00Z",
                    "entry_ready": True,
                    "strict_entry_ready": True,
                    "allocator_score": 0.4,
                    "trade_prob": 0.25,
                    "spread_bps": 1.0,
                    "freshness_secs": 8.0,
                    "liquidity_score": 0.75,
                },
            }
        ],
        portfolio={"equity": 10_000.0},
        policy_context={"policy_manifest_path": str(policy_manifest_path)},
        supervised_fallback_required=True,
    )

    assert bundle.policy_manifest_path == str(policy_manifest_path)
    assert bundle.policy_manifest["policy_name"] == "manifest-policy"
    assert bundle.diagnostics["artifact_discovery"]["policy_manifest_path"] == str(policy_manifest_path)
    assert bundle.diagnostics["artifact_discovery"]["primary_policy"] is True
    assert bundle.source == "rl_checkpoint"
    assert bundle.checkpoint_loaded is True
    assert bundle.checkpoint_summary["metadata"]["run_name"] == "manifest-checkpoint"
    assert bundle.proposals_by_pair["EURUSD"].metadata["entry_supported"] is True
