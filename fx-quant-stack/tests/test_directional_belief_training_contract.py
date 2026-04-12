from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fxstack.belief import dataset as belief_dataset
from fxstack.training import belief as belief_training


def _dataset_with_context(*, available: bool) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "pair": ["EURUSD"],
            "ts": ["2026-04-07T12:00:00Z"],
            "scenario": ["trend_pullback"],
            "side": ["long"],
            "query_id": ["EURUSD|2026-04-07T12:00:00Z"],
            "relevance": [1.0],
            "net_ev_bps": [4.0],
            "confirm_success": [1],
            "fail_fast": [0],
            "ev_above_hurdle": [1],
            "local_feasible": [True],
        }
    )
    df.attrs["cross_pair_context"] = {
        "available": bool(available),
        "required_columns": ["usd_strength_basket_ret_1", "cross_pair_dispersion"],
        "missing_pairs": [] if available else ["EURUSD"],
        "retrievals": [],
    }
    df.attrs["feature_retrieval"] = {"source": "single_frame_parquet"}
    return df


def test_train_directional_belief_rejects_missing_cross_pair_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        belief_training,
        "build_directional_belief_dataset",
        lambda **_: _dataset_with_context(available=False),
    )

    with pytest.raises(RuntimeError, match="validated cross-pair context"):
        belief_training.train_directional_belief(
            feature_root=str(tmp_path),
            out=str(tmp_path / "belief_artifact"),
        )


def test_export_directional_belief_dataset_reports_cross_pair_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        belief_training,
        "build_directional_belief_dataset",
        lambda **_: _dataset_with_context(available=True),
    )

    result = belief_training.export_directional_belief_dataset(
        feature_root=str(tmp_path),
        out=str(tmp_path / "belief_dataset.csv"),
    )

    assert result["cross_pair_context"]["available"] is True
    assert result["cross_pair_context"]["required_columns"] == ["usd_strength_basket_ret_1", "cross_pair_dispersion"]


def test_export_directional_belief_dataset_rejects_contract_drift(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        belief_training,
        "build_directional_belief_dataset",
        lambda **_: _dataset_with_context(available=False),
    )

    with pytest.raises(RuntimeError, match="validated cross-pair context"):
        belief_training.export_directional_belief_dataset(
            feature_root=str(tmp_path),
            out=str(tmp_path / "belief_dataset.csv"),
        )


def test_build_directional_belief_dataset_marks_requested_pairs_with_empty_retrievals(monkeypatch) -> None:
    def _build_historical_feature_frame(*, pair: str, **_: object):
        if pair == "GBPUSD":
            return pd.DataFrame(), {"pair": "GBPUSD", "source": "empty"}
        feats = pd.DataFrame(
            {
                "pair": ["EURUSD"],
                "ts": ["2026-04-07T12:00:00Z"],
                "spread_bps": [0.9],
                "ret_1": [0.0002],
                "ret_5": [0.0004],
                "vol_20": [0.0001],
                "vol_60": [0.0001],
                "pullback_depth_20": [0.0015],
                "pushup_depth_20": [0.0014],
                "trend_slope_20": [0.0010],
                "trend_slope_60": [0.0011],
                "trend_strength_20": [0.8],
                "trend_strength_60": [0.7],
                "bar_imbalance": [0.2],
                "micro_pressure": [0.2],
                "edge_decay_12": [0.0001],
                "vol_term_ratio": [1.05],
                "hostility_score": [0.1],
                "macro_coherence_score": [0.7],
                "session_bucket": ["london_open"],
                "regime_bucket": ["trend"],
                "scenario_bucket": ["trend_continuation"],
                "position_count_pair": [0],
                "usd_strength_basket_ret_1": [0.0002],
                "cross_pair_dispersion": [0.12],
            }
        )
        return feats, {"pair": "EURUSD", "source": "single_frame_parquet"}

    def _label_hypothesis_outcomes(candidates: pd.DataFrame, **_: object) -> pd.DataFrame:
        labeled = candidates.copy()
        labeled["all_in_cost_bps"] = 0.25
        labeled["net_ev_bps"] = 2.0
        labeled["confirm_success"] = 1
        labeled["fail_fast"] = 0
        labeled["mfe_bps"] = 3.0
        labeled["mae_bps"] = 1.0
        labeled["relevance"] = 1.0
        labeled["ev_above_hurdle"] = 1
        return labeled

    monkeypatch.setattr(belief_dataset, "build_historical_feature_frame", _build_historical_feature_frame)
    monkeypatch.setattr(belief_dataset, "label_hypothesis_outcomes", _label_hypothesis_outcomes)

    dataset = belief_dataset.build_directional_belief_dataset(
        feature_root="unused",
        pairs=["EURUSD", "GBPUSD"],
        max_queries_per_pair=10,
    )

    assert dataset.attrs["cross_pair_context"]["available"] is False
    assert dataset.attrs["cross_pair_context"]["missing_pairs"] == ["GBPUSD"]
