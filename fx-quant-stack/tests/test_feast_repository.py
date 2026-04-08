from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from fxstack.feast.compaction import compact_feature_lake_to_feast
from fxstack.feast.parquet_adapter import build_stable_feast_parquet_outputs
from fxstack.feast.repository import (
    default_feature_repo,
    derive_feature_services_from_artifacts,
    feature_views_for_component,
    load_model_artifact_specs,
)
from fxstack.io.parquet_store import ParquetStore


def _write_bars(root: Path, *, provider: str, pair: str, timeframe: str, start: str, periods: int, freq: str) -> None:
    idx = pd.date_range(start=start, periods=periods, freq=freq, tz="UTC")
    df = pd.DataFrame(
        {
            "pair": pair,
            "ts": idx,
            "timeframe": timeframe,
            "bid_open": range(periods),
            "bid_high": [x + 1 for x in range(periods)],
            "bid_low": range(periods),
            "bid_close": [x + 0.5 for x in range(periods)],
            "ask_open": [x + 0.1 for x in range(periods)],
            "ask_high": [x + 1.1 for x in range(periods)],
            "ask_low": [x + 0.1 for x in range(periods)],
            "ask_close": [x + 0.6 for x in range(periods)],
            "mid_open": [x + 0.05 for x in range(periods)],
            "mid_high": [x + 1.05 for x in range(periods)],
            "mid_low": [x + 0.05 for x in range(periods)],
            "mid_close": [x + 0.55 for x in range(periods)],
            "volume": [100 + x for x in range(periods)],
            "spread": [0.2 for _ in range(periods)],
        }
    )
    ParquetStore(root).write_partitioned(df, provider=provider, pair=pair, timeframe=timeframe)


def test_repository_derives_services_from_artifacts() -> None:
    repo = default_feature_repo(Path.cwd())
    names = [svc.name for svc in repo.services]
    assert any(name.startswith("fx.swing") for name in names)
    assert any(name.startswith("fx.intraday") for name in names)
    assert any(name.startswith("fx.directional_belief") for name in names)
    assert any(name.startswith("fx.cross_pair_intelligence") for name in names)
    assert "cross_pair_intelligence" in {view.name for view in repo.views}
    assert feature_views_for_component("cross_pair_intelligence") == ["cross_pair_intelligence"]


def test_repository_write_renders_expected_structure(tmp_path: Path) -> None:
    repo = default_feature_repo(tmp_path / "feature_repo")
    out = repo.write()
    assert out["feature_store"].exists()
    assert out["entity"].exists()
    assert out["views_dir"].is_dir()
    assert out["services_dir"].is_dir()
    text = out["feature_store"].read_text(encoding="utf-8")
    assert "project: fxstack" in text
    assert "feature_views:" in text and "feature_services:" in text


def test_load_model_artifact_specs_handles_artifacts(tmp_path: Path) -> None:
    art_root = tmp_path / "artifacts" / "eurusd" / "swing_xgb"
    art_root.mkdir(parents=True)
    (art_root / "meta.json").write_text(json.dumps({"model_family": "swing", "feature_columns": ["a", "b"]}), encoding="utf-8")
    specs = load_model_artifact_specs(tmp_path / "artifacts")
    assert len(specs) == 1
    assert specs[0].service_name == "fx.swing.EURUSD.na"


def test_derives_feature_services_from_model_columns(tmp_path: Path) -> None:
    art_root = tmp_path / "artifacts" / "eurusd" / "intraday_xgb"
    art_root.mkdir(parents=True)
    (art_root / "meta.json").write_text(json.dumps({"model_family": "intraday_xgb", "feature_columns": ["x", "y", "x"]}), encoding="utf-8")
    specs = load_model_artifact_specs(tmp_path / "artifacts")
    services = derive_feature_services_from_artifacts(specs)
    assert any(svc.name == "fx.intraday_xgb.EURUSD.na" for svc in services)
    svc = next(svc for svc in services if svc.name == "fx.intraday_xgb.EURUSD.na")
    assert svc.features == ("x", "y")


def test_compaction_writes_stable_parquet_outputs(tmp_path: Path) -> None:
    source = tmp_path / "lake"
    output = tmp_path / "feast"
    _write_bars(source, provider="dukascopy", pair="EURUSD", timeframe="M5", start="2024-01-01", periods=120, freq="5min")
    _write_bars(source, provider="dukascopy", pair="EURUSD", timeframe="H4", start="2024-01-01", periods=60, freq="4h")
    _write_bars(source, provider="dukascopy", pair="EURUSD", timeframe="D", start="2024-01-01", periods=40, freq="1d")
    _write_bars(source, provider="dukascopy", pair="GBPUSD", timeframe="M5", start="2024-01-01", periods=120, freq="5min")

    result = compact_feature_lake_to_feast(
        source_root=source,
        output_root=output,
        provider="dukascopy",
        pairs=["EURUSD", "GBPUSD"],
    )
    assert result.output_root == output
    assert len(result.artifacts) == 8
    assert all(artifact.output_path.exists() for artifact in result.artifacts)


def test_build_stable_feast_parquet_outputs_can_compact_single_pair(tmp_path: Path) -> None:
    source = tmp_path / "lake"
    output = tmp_path / "feast"
    _write_bars(source, provider="dukascopy", pair="EURUSD", timeframe="M5", start="2024-01-01", periods=120, freq="5min")
    _write_bars(source, provider="dukascopy", pair="EURUSD", timeframe="H4", start="2024-01-01", periods=60, freq="4h")
    _write_bars(source, provider="dukascopy", pair="EURUSD", timeframe="D", start="2024-01-01", periods=40, freq="1d")
    artifacts = build_stable_feast_parquet_outputs(
        source_root=source,
        output_root=output,
        provider="dukascopy",
        pairs=["EURUSD"],
    )
    assert {artifact.view_name for artifact in artifacts} == {
        "anchor_lifecycle_m5",
        "higher_timeframe_context",
        "cross_pair_context",
        "live_diagnostics",
    }
