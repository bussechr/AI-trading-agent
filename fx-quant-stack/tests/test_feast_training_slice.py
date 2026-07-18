from __future__ import annotations

from pathlib import Path

import pandas as pd

from fxstack.feast import offline_builder
from fxstack.feast.offline_builder import build_historical_feature_frame, build_entity_dataframe
from fxstack.io.parquet_store import ParquetStore
from fxstack.tasks import _train_xy, build_meta_labels_task


def _write_frame(root: Path, *, pair: str, timeframe: str, frame: pd.DataFrame) -> None:
    ParquetStore(root).write_partitioned(frame, provider="dukascopy", pair=pair, timeframe=timeframe)


def test_historical_feature_frame_uses_point_in_time_rows(tmp_path: Path):
    feature_root = tmp_path / "features"
    frame = pd.DataFrame(
        {
            "pair": ["EURUSD", "EURUSD", "EURUSD"],
            "ts": pd.to_datetime(["2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z", "2026-04-03T00:00:00Z"], utc=True),
            "timeframe": ["M5", "M5", "M5"],
            "ret_1": [0.1, 0.2, 0.3],
            "ret_5": [0.4, 0.5, 0.6],
            "vol_20": [1.0, 1.1, 1.2],
            "spread_bps": [2.0, 2.1, 2.2],
        }
    )
    _write_frame(feature_root, pair="EURUSD", timeframe="M5", frame=frame)

    entity_df = build_entity_dataframe(
        pd.DataFrame({"pair": ["EURUSD", "EURUSD"], "ts": pd.to_datetime(["2026-04-02T12:00:00Z", "2026-04-03T12:00:00Z"], utc=True)}),
        pair="EURUSD",
    )
    retrieved, meta = build_historical_feature_frame(
        feature_root=feature_root,
        pair="EURUSD",
        timeframe="M5",
        entity_df=entity_df,
        feature_service_name="fx_eurusd_m5",
        feature_view_names=["fx_eurusd_m5"],
    )

    assert len(retrieved) == 2
    assert list(retrieved["ret_1"].astype(float)) == [0.2, 0.3]
    assert meta["source"] == "single_frame_parquet"
    assert meta["feature_service_name"] == "fx_eurusd_m5"


def test_historical_feature_frame_rejects_all_null_feast_result(monkeypatch, tmp_path: Path):
    feature_root = tmp_path / "features"
    frame = pd.DataFrame(
        {
            "pair": ["EURUSD", "EURUSD"],
            "ts": pd.to_datetime(["2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z"], utc=True),
            "timeframe": ["H4", "H4"],
            "ret_1": [0.1, 0.2],
            "ret_5": [0.3, 0.4],
        }
    )
    _write_frame(feature_root, pair="EURUSD", timeframe="H4", frame=frame)

    def _all_null_feast(*, service_ref, entity_df):
        return pd.DataFrame(
            {
                "pair": entity_df["pair"],
                "event_timestamp": entity_df["event_timestamp"],
                "ts": entity_df["event_timestamp"],
                "ret_1": [float("nan")] * len(entity_df),
                "ret_5": [float("nan")] * len(entity_df),
            }
        ), "feast_historical"

    monkeypatch.setattr(offline_builder, "_feast_historical", _all_null_feast)
    retrieved, meta = build_historical_feature_frame(
        feature_root=str(feature_root),
        pair="EURUSD",
        timeframe="H4",
        feature_service_name="fx_eurusd_regime_hmm_h4",
        feature_view_names=["anchor_h4"],
    )

    assert list(retrieved["ret_1"].astype(float)) == [0.1, 0.2]
    assert meta["source"] == "single_frame_parquet"
    assert meta["fallback_reason"] == "feast_historical_all_features_null"


def test_train_xy_and_meta_labels_carry_retrieval_metadata(tmp_path: Path):
    feature_root = tmp_path / "features"
    label_root = tmp_path / "labels"
    feats = pd.DataFrame(
        {
            "pair": ["EURUSD", "EURUSD", "EURUSD", "EURUSD"],
            "ts": pd.to_datetime(["2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z", "2026-04-03T00:00:00Z", "2026-04-04T00:00:00Z"], utc=True),
            "timeframe": ["M5", "M5", "M5", "M5"],
            "ret_1": [0.1, 0.2, 0.3, 0.4],
            "ret_5": [0.2, 0.3, 0.4, 0.5],
            "vol_20": [1.0, 1.1, 1.2, 1.3],
            "mid_close": [100.0, 100.5, 101.0, 101.5],
            "spread_bps": [1.0, 1.0, 1.0, 1.0],
            "scenario_bucket": ["trend", "trend", "range", "range"],
            "regime_bucket": ["risk_on", "risk_on", "risk_on", "risk_off"],
            "session_tag": ["london", "london", "ny", "ny"],
        }
    )
    _write_frame(feature_root, pair="EURUSD", timeframe="M5", frame=feats)
    labels = pd.DataFrame(
        {
            "pair": ["EURUSD", "EURUSD", "EURUSD", "EURUSD"],
            "ts": feats["ts"],
            "timeframe": ["M5", "M5", "M5", "M5"],
            "label": [1, -1, 1, -1],
        }
    )
    _write_frame(label_root, pair="EURUSD", timeframe="M5", frame=labels)

    X, y, df = _train_xy(pair="EURUSD", timeframe="M5", feature_root=str(feature_root), label_root=str(label_root))
    assert not X.empty
    assert len(y) == len(df)
    assert "feature_retrieval" in X.attrs
    assert X.attrs["feature_retrieval"]["feature_service_name"] == "fx_eurusd_m5"

    meta_out = tmp_path / "meta"
    result = build_meta_labels_task(
        pair="EURUSD",
        timeframe="M5",
        feature_root=str(feature_root),
        label_root=str(tmp_path / "meta_labels"),
        allow_heuristic_labels=True,
    )
    assert result["feature_service_name"] == "fx_eurusd_meta_filter_m5"
    assert result["source"] in {"single_frame_parquet", "feast_historical"}


def test_historical_feature_frame_reports_missing_cross_pair_context_on_fallback(tmp_path: Path):
    feature_root = tmp_path / "features"
    frame = pd.DataFrame(
        {
            "pair": ["EURUSD", "EURUSD"],
            "ts": pd.to_datetime(["2026-04-01T00:00:00Z", "2026-04-02T00:00:00Z"], utc=True),
            "timeframe": ["M5", "M5"],
            "ret_1": [0.1, 0.2],
            "spread_bps": [1.0, 1.1],
        }
    )
    _write_frame(feature_root, pair="EURUSD", timeframe="M5", frame=frame)

    retrieved, meta = build_historical_feature_frame(
        feature_root=feature_root,
        pair="EURUSD",
        timeframe="M5",
        feature_service_name="fx_eurusd_directional_belief_m5",
        feature_view_names=["anchor_m5", "cross_pair_context"],
    )

    assert not retrieved.empty
    assert meta["source"] == "single_frame_parquet"
    assert meta["cross_pair_context_requested"] is True
    assert meta["cross_pair_context_available"] is False
    assert meta["cross_pair_context_missing_columns"] == ["usd_strength_basket_ret_1", "cross_pair_dispersion"]
