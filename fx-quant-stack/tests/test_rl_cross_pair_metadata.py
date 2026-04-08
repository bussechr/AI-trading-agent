from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "fx-quant-stack" / "src"


def _ensure_stub_package(name: str) -> types.ModuleType:
    module = sys.modules.get(name)
    if module is None:
        module = types.ModuleType(name)
        module.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = module
    return module


def _load_module(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rl_module_stubs() -> None:
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    _ensure_stub_package("fxstack.rl")
    trainer_stub = types.ModuleType("fxstack.rl.trainer")

    class _Checkpoint:
        feature_names: list[str] = []

        def predict_frame(self, frame):  # pragma: no cover - not exercised
            return [0.0] * len(frame)

    trainer_stub.RLLinearCheckpoint = _Checkpoint
    trainer_stub.load_replay_checkpoint = lambda path: _Checkpoint()
    sys.modules["fxstack.rl.trainer"] = trainer_stub
    common_stub = types.ModuleType("fxstack.rl._common")
    common_stub._ensure_dir = lambda path: path
    common_stub._json_dump = lambda path, payload: path
    sys.modules["fxstack.rl._common"] = common_stub
    yield


def test_rl_proposal_bundles_cross_pair_metadata_into_action_and_proposal(rl_module_stubs) -> None:
    contracts = _load_module("fxstack.rl.contracts", SRC_ROOT / "fxstack" / "rl" / "contracts.py")
    sys.modules["fxstack.rl.contracts"] = contracts
    proposal = _load_module("fxstack.rl.proposal", SRC_ROOT / "fxstack" / "rl" / "proposal.py")

    bundle = proposal.build_portfolio_rl_proposal_bundle(
        ts="2026-04-08T00:00:00Z",
        decisions=[
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
            }
        ],
        portfolio={"equity": 10_000.0, "open_position_count": 0},
        supervised_fallback_required=True,
    )

    eurusd = bundle.proposals_by_pair["EURUSD"]
    assert eurusd.action.metadata["cross_pair_rank_position"] == 1
    assert eurusd.action.metadata["cross_pair_reason_codes"] == ["local_edge", "basket_alignment"]
    assert eurusd.metadata["cross_pair_influenced_by_pairs"] == ["GBPUSD", "USDJPY"]
    assert eurusd.metadata["cross_pair_soft_block"] is False
    assert bundle.observation["features_by_pair"]["EURUSD"]["cross_pair_rank_position"] == 1.0
    assert bundle.observation["features_by_pair"]["EURUSD"]["cross_pair_recommendation_strength"] == 0.94


def test_replay_transition_json_keeps_cross_pair_metadata() -> None:
    common_stub = sys.modules.get("fxstack.rl._common")
    if common_stub is None:
        common_stub = types.ModuleType("fxstack.rl._common")
        common_stub._ensure_dir = lambda path: path
        common_stub._json_dump = lambda path, payload: path
        sys.modules["fxstack.rl._common"] = common_stub
    export_replay = _load_module("fxstack.rl.export_replay", SRC_ROOT / "fxstack" / "rl" / "export_replay.py")

    df, meta = export_replay.normalize_replay_transitions(
        [
            {
                "ts": "2026-04-01T00:00:00Z",
                "pair": "EURUSD",
                "episode_id": "ep-1",
                "decisions_json": [
                    {
                        "symbol": "EURUSD",
                        "side": "BUY",
                        "metadata": {
                            "lifecycle_action": "entry",
                            "cross_pair_rank_position": 1,
                            "cross_pair_influence_score": 0.93,
                            "cross_pair_recommendation_strength": 0.95,
                            "cross_pair_influenced_by_pairs": ["GBPUSD"],
                            "cross_pair_reason_codes": ["local_edge", "peer_confluence"],
                        },
                    }
                ],
            }
        ],
        source_name="decision_snapshots",
    )

    assert meta["row_count"] == 1
    assert "\"cross_pair_rank_position\":1" in df.iloc[0]["metadata_json"]
