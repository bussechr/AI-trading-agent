from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from scripts.train_all import (
    _ensure_hierarchical_intraday_features,
    _hierarchical_intraday_cache_is_current,
    _write_bundle_manifest,
)
from fxstack.features.multi_tf_contract import raw_multi_tf_source_contract
from fxstack.features.session_contract import (
    MULTI_TF_CONTRACT_VERSION,
    SESSION_CONTRACT_VERSION,
)
from fxstack.io.parquet_store import ParquetStore
from fxstack.mlops.types import BundleManifest
from fxstack.training.phase5_gates import build_phase5_gate_bundle, write_phase5_gate_bundle


def test_training_manifest_is_rewritten_after_phase5_evidence_exists(tmp_path: Path) -> None:
    bundle = BundleManifest(
        bundle_run_id="bundle-1",
        pair="EURUSD",
        tier="tier2",
        dataset_fingerprint="fp-1",
        feature_service_version="features-1",
        label_version="labels-1",
        risk_config_version="risk-1",
        promotion_status="eligible",
    )
    path = tmp_path / "model_manifest.json"

    _write_bundle_manifest(path, bundle)
    initial = json.loads(path.read_text(encoding="utf-8"))
    assert initial["metadata"]["phase5_gates"] == {}

    refs = {"phase5_gate_bundle": str(tmp_path / "phase5_gate_bundle.json")}
    _write_bundle_manifest(path, bundle, phase5_evidence_refs=refs)
    final = json.loads(path.read_text(encoding="utf-8"))
    assert final["metadata"]["phase5_gates"] == refs


def test_training_feature_cache_rejects_legacy_contract() -> None:
    legacy = pd.DataFrame(
        [
            {
                "context_frame_profile": "hierarchical_v1",
                "m15_ret_1": 0.1,
                "h1_ret_1": 0.1,
                "h4_trend_slope_20": 0.1,
                "d_trend_slope_20": 0.1,
            }
        ]
    )
    assert _hierarchical_intraday_cache_is_current(legacy) is False

    current = legacy.copy()
    current["context_frame_profile"] = MULTI_TF_CONTRACT_VERSION
    current["session_contract_version"] = SESSION_CONTRACT_VERSION
    for prefix in ("m15", "h1", "h4", "d"):
        current[f"{prefix}_available"] = 1
        current[f"{prefix}_fresh"] = 1
        current[f"{prefix}_age_secs"] = 0.0
    current["raw_source_watermark"] = "2025-01-01T00:00:00+00:00"
    current["raw_source_fingerprint"] = "raw-fingerprint-1"
    assert _hierarchical_intraday_cache_is_current(current) is True
    assert _hierarchical_intraday_cache_is_current(
        current,
        raw_source_contract={
            "watermark": "2025-01-01T00:00:00+00:00",
            "fingerprint": "raw-fingerprint-1",
        },
    ) is True
    assert _hierarchical_intraday_cache_is_current(
        current,
        raw_source_contract={
            "watermark": "2025-01-01T00:05:00+00:00",
            "fingerprint": "raw-fingerprint-2",
        },
    ) is False


def test_force_retrain_bypasses_current_hierarchical_feature_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import train_all as train_all_module

    current = pd.DataFrame(
        [
            {
                "context_frame_profile": MULTI_TF_CONTRACT_VERSION,
                "session_contract_version": SESSION_CONTRACT_VERSION,
                "raw_source_watermark": "2025-01-01T00:00:00+00:00",
                "raw_source_fingerprint": "raw-fingerprint-1",
                "m15_ret_1": 0.1,
                "h1_ret_1": 0.1,
                "h4_trend_slope_20": 0.1,
                "d_trend_slope_20": 0.1,
                **{
                    f"{prefix}_{suffix}": 1 if suffix != "age_secs" else 0.0
                    for prefix in ("m15", "h1", "h4", "d")
                    for suffix in ("available", "fresh", "age_secs")
                },
            }
        ]
    )
    source_contract = {
        "version": "raw_multi_tf_sources_v2",
        "watermark": "2025-01-01T00:00:00+00:00",
        "fingerprint": "raw-fingerprint-1",
        "streams": [
            {
                "provider": "dukascopy",
                "pair": "EURUSD",
                "timeframe": "M5",
            }
        ],
    }
    builds: list[dict[str, object]] = []

    class FakeStore:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def read_latest_row(self, **_: object) -> pd.DataFrame:
            return current

    monkeypatch.setattr(
        train_all_module,
        "get_settings",
        lambda: SimpleNamespace(normalized_data_provider="dukascopy", pairs=["EURUSD"]),
    )
    monkeypatch.setattr(train_all_module, "ParquetStore", FakeStore)
    monkeypatch.setattr(train_all_module, "_ensure_ingested", lambda **_: None)
    monkeypatch.setattr(
        train_all_module,
        "raw_multi_tf_source_contract",
        lambda **_: source_contract,
    )
    monkeypatch.setattr(
        train_all_module,
        "build_fx_lifecycle_features_task",
        lambda **kwargs: builds.append(kwargs),
    )

    _ensure_hierarchical_intraday_features(
        pair="EURUSD",
        timeframe="M5",
        raw_root=tmp_path / "raw",
        feature_root=str(tmp_path / "features"),
        force_rebuild=False,
    )
    assert builds == []

    _ensure_hierarchical_intraday_features(
        pair="EURUSD",
        timeframe="M5",
        raw_root=tmp_path / "raw",
        feature_root=str(tmp_path / "features"),
        force_rebuild=True,
    )
    assert len(builds) == 1


def test_training_feature_cache_rebuilds_when_raw_changes_during_cache_read(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts import train_all as train_all_module

    provider = "dukascopy"
    raw_root = tmp_path / "raw"
    raw_store = ParquetStore(raw_root)
    for timeframe in ("M5", "M15", "H1", "H4", "D"):
        raw_store.write_partitioned(
            pd.DataFrame(
                [
                    {
                        "pair": "EURUSD",
                        "ts": "2025-01-01T00:00:00Z",
                        "timeframe": timeframe,
                        "mid_close": 1.1,
                    }
                ]
            ),
            provider=provider,
            pair="EURUSD",
            timeframe=timeframe,
        )
    original_contract = raw_multi_tf_source_contract(
        raw_store_root=raw_root,
        provider=provider,
        pair="EURUSD",
        anchor_timeframe="M5",
        context_timeframes=["M15", "H1", "H4", "D"],
        all_pairs=["EURUSD"],
    )
    current = pd.DataFrame(
        [
            {
                "context_frame_profile": MULTI_TF_CONTRACT_VERSION,
                "session_contract_version": SESSION_CONTRACT_VERSION,
                "raw_source_watermark": original_contract["watermark"],
                "raw_source_fingerprint": original_contract["fingerprint"],
                "m15_ret_1": 0.1,
                "h1_ret_1": 0.1,
                "h4_trend_slope_20": 0.1,
                "d_trend_slope_20": 0.1,
                **{
                    f"{prefix}_{suffix}": 1 if suffix != "age_secs" else 0.0
                    for prefix in ("m15", "h1", "h4", "d")
                    for suffix in ("available", "fresh", "age_secs")
                },
            }
        ]
    )
    builds: list[dict[str, object]] = []
    mutated = False

    class MutatingFeatureStore:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def read_latest_row(self, **_: object) -> pd.DataFrame:
            nonlocal mutated
            revised = pd.DataFrame(
                [
                    {
                        "pair": "EURUSD",
                        "ts": "2025-01-01T00:00:00Z",
                        "timeframe": "M5",
                        "mid_close": 1.2,
                    }
                ]
            )
            raw_store.write_partitioned(
                revised,
                provider=provider,
                pair="EURUSD",
                timeframe="M5",
            )
            mutated = True
            return current

    monkeypatch.setattr(
        train_all_module,
        "get_settings",
        lambda: SimpleNamespace(normalized_data_provider=provider, pairs=["EURUSD"]),
    )
    monkeypatch.setattr(train_all_module, "ParquetStore", MutatingFeatureStore)
    monkeypatch.setattr(train_all_module, "_ensure_ingested", lambda **_: None)
    monkeypatch.setattr(
        train_all_module,
        "build_fx_lifecycle_features_task",
        lambda **kwargs: builds.append(kwargs),
    )

    _ensure_hierarchical_intraday_features(
        pair="EURUSD",
        timeframe="M5",
        raw_root=raw_root,
        feature_root=str(tmp_path / "features"),
        force_rebuild=False,
    )

    assert mutated is True
    assert len(builds) == 1
    refreshed_contract = raw_multi_tf_source_contract(
        raw_store_root=raw_root,
        provider=provider,
        pair="EURUSD",
        anchor_timeframe="M5",
        context_timeframes=["M15", "H1", "H4", "D"],
        all_pairs=["EURUSD"],
    )
    assert refreshed_contract["fingerprint"] != original_contract["fingerprint"]


def test_phase5_gate_bundle_emits_expected_artifacts(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    reports_root.mkdir()

    def write_json(name: str, payload: dict[str, object]) -> Path:
        path = reports_root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    feature_schema = write_json("feature_schema.json", {"version": "1"})
    lineage = write_json("lineage.json", {"git_sha": "abc123", "feature_service_version": "fs1", "label_version": "lv1", "risk_config_version": "rv1"})
    model_manifest = write_json("model_manifest.json", {"bundle_run_id": "bundle-1"})
    backtest_summary = write_json("backtest_summary.json", {"net_pnl_usd": 125.0, "max_drawdown_pct": 2.5, "turnover_lots": 1.5})
    stress_summary = write_json("stress_harness_summary.json", {"scenario_count": 3, "worst_realized_pnl_usd": 75.0, "worst_drawdown_pct": 4.0})
    harness_comparison = write_json("harness_comparison.json", {"within_tolerance": True})
    execution_metrics = write_json("execution_metrics.json", {"filled_orders": 2})
    risk_trace_schema = write_json("risk_trace_schema.json", {"version": "p5"})
    phase3_refs = {
        "stress_harness_summary": str(stress_summary),
        "harness_comparison": str(harness_comparison),
        "execution_metrics": str(execution_metrics),
        "risk_trace_schema": str(risk_trace_schema),
    }

    bundle = build_phase5_gate_bundle(
        pair="EURUSD",
        reports_root=reports_root,
        backtest_summary={"net_pnl_usd": 125.0, "max_drawdown_pct": 2.5, "turnover_lots": 1.5},
        promotion_status="eligible",
        training_window_summary={"start_ts": "2025-01-01", "end_ts": "2025-02-01"},
        capabilities={"lifecycle_complete": True},
        training_eval_reports={"meta": "meta_report.json"},
        phase3_evidence_refs=phase3_refs,
        feature_schema_path=feature_schema,
        lineage_path=lineage,
        model_manifest_path=model_manifest,
        backtest_summary_path=backtest_summary,
        stress_summary_path=stress_summary,
        harness_comparison_path=harness_comparison,
        execution_metrics_path=execution_metrics,
        risk_trace_schema_path=risk_trace_schema,
        phase3_execution_required=True,
        phase4_shadow_only=True,
        phase4_sequence_dataset_manifests={"swing_patchtst": "swing_seq.json"},
        phase4_portfolio_reports={"swing_patchtst": "swing_portfolio.json"},
        phase4_challenger_reports={"swing_patchtst": "swing_challenger.json"},
    )

    payload = bundle.to_dict()
    assert payload["research_gate"]["gate"] == "research_gate"
    assert payload["economic_gate"]["passed"] is True
    assert payload["operational_gate"]["passed"] is True
    assert payload["shadow_gate"]["passed"] is True
    assert payload["canary_gate"]["passed"] is True
    assert payload["canary_closeout"]["passed"] is True
    assert payload["scorecard"]["economic_scorecard"]["realized_pnl_usd"] == 125.0
    assert payload["evidence_refs"]["feature_schema"] == str(feature_schema)

    out = write_phase5_gate_bundle(bundle, reports_root=reports_root)
    assert set(out) == {
        "research_gate",
        "economic_gate",
        "operational_gate",
        "shadow_gate",
        "canary_gate",
        "canary_closeout",
        "phase5_gate_bundle",
    }
    for key, path in out.items():
        assert Path(path).exists(), key

    bundle_json = json.loads(Path(out["phase5_gate_bundle"]).read_text(encoding="utf-8"))
    assert bundle_json["research_gate"]["gate"] == "research_gate"
    assert bundle_json["canary_closeout"]["passed"] is True
