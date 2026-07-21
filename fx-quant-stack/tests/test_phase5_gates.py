from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

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
from fxstack.training import phase5_gates
from fxstack.training.phase5_gates import bind_phase5_release_evidence, build_phase5_gate_bundle, write_phase5_gate_bundle
from fxstack.training.release_evidence import (
    EvidenceValidation,
    ReleaseEvidenceIdentity,
    active_manifest_identity,
    artifact_set_sha256,
    candidate_manifest_identity,
    file_sha256,
    mapping_sha256,
)


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
    model_manifest = write_json("model_manifest.json", {"bundle_run_id": "bundle-1", "components": {"meta": {"sha256": "a" * 64}}})
    candidate_identity = candidate_manifest_identity(
        manifest_path=model_manifest,
        pair="EURUSD",
        model_set_id="bundle-1",
    )
    manifest_sha = candidate_identity.model_manifest_sha256
    artifact_set_sha = candidate_identity.artifact_set_sha256
    backtest_summary = write_json(
        "backtest_summary.json",
        {
            "status": "complete",
            "net_pnl_usd": 125.0,
            "max_drawdown_pct": 2.5,
            "turnover_lots": 1.5,
            "trade_count": 12,
            "stress_summary": {
                "status": "ok",
                "scenario_count": 2,
                "worst_realized_pnl_usd": 75.0,
                "worst_drawdown_pct": 4.0,
            },
            "evidence_identity": ReleaseEvidenceIdentity(
                pair="EURUSD",
                bundle_run_id="bundle-1",
                model_set_id="bundle-1",
                model_manifest_sha256=manifest_sha,
                artifact_set_sha256=artifact_set_sha,
                evidence_kind="economic_validation",
                source_kind="independent_execution_harness",
                advisory_only=False,
            ).to_dict(),
        },
    )
    shadow_runtime = write_json(
        "shadow_runtime.json",
        {
            "schema_version": "fxstack_shadow_runtime_evidence_v2",
            "started_at": 1_000.0,
            "ended_at": 87_400.0,
            "evidence_identity": ReleaseEvidenceIdentity(
                pair="EURUSD",
                bundle_run_id="bundle-1",
                model_set_id="bundle-1",
                model_manifest_sha256=manifest_sha,
                artifact_set_sha256=artifact_set_sha,
                evidence_kind="runtime_shadow",
                source_kind="production_runtime_shadow",
                advisory_only=False,
            ).to_dict(),
            "gates": {"passed": True},
            "runtime_boundary": {
                "agent_mode": "shadow",
                "broker_emission_disabled": True,
                "entry_commands_emitted": 0,
                "active_manifest_matches_db": True,
                "runtime_loaded_matches_db": True,
                "activation_identity_consistent": True,
                "startup_lifecycle": {
                    "startup_inference_ok": True,
                    "model_set_id": "bundle-1",
                    "pair_readiness_status": "ready",
                    "has_exit_model": True,
                    "has_reversal_models": True,
                    "lifecycle_activation_mode": "model_driven",
                    "lifecycle_ready": True,
                },
            },
            "candidate": {"samples": 10, "runtime_ready_seen": True, "feature_ready_seen": True},
        },
    )
    stress_summary = write_json("stress_harness_summary.json", {"scenario_count": 3, "worst_realized_pnl_usd": 75.0, "worst_drawdown_pct": 4.0})
    harness_comparison = write_json("harness_comparison.json", {"within_tolerance": True})
    execution_metrics = write_json("execution_metrics.json", {"filled_orders": 2})
    risk_trace_schema = write_json("risk_trace_schema.json", {"version": "p5"})
    training_eval = write_json("meta_report.json", {"status": "eligible"})
    phase3_refs = {
        "stress_harness_summary": str(stress_summary),
        "harness_comparison": str(harness_comparison),
        "execution_metrics": str(execution_metrics),
        "risk_trace_schema": str(risk_trace_schema),
    }

    bundle = build_phase5_gate_bundle(
        pair="EURUSD",
        reports_root=reports_root,
        backtest_summary={
            "net_pnl_usd": 125.0,
            "max_drawdown_pct": 2.5,
            "turnover_lots": 1.5,
            "trade_count": 12,
        },
        promotion_status="eligible",
        training_window_summary={"start_ts": "2025-01-01", "end_ts": "2025-02-01"},
        capabilities={"lifecycle_complete": True},
        training_eval_reports={"meta": str(training_eval)},
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
        bundle_run_id="bundle-1",
        model_set_id="bundle-1",
        shadow_runtime_evidence_path=shadow_runtime,
    )

    payload = bundle.to_dict()
    assert payload["research_gate"]["gate"] == "research_gate"
    # Hand-authored economics cannot claim independent external execution.
    assert payload["economic_gate"]["passed"] is False
    # Placeholder support JSON does not satisfy the tightened semantic
    # support-evidence contracts, so operational authority stays closed.
    assert payload["operational_gate"]["passed"] is False
    assert payload["shadow_gate"]["passed"] is False
    assert payload["canary_gate"]["passed"] is False
    assert payload["canary_closeout"]["passed"] is False
    assert payload["scorecard"]["economic_scorecard"]["realized_pnl_usd"] == 125.0
    assert payload["evidence_refs"]["feature_schema"] == str(feature_schema)
    assert payload["evidence_hashes"]["backtest_summary"] == file_sha256(backtest_summary)
    assert payload["evidence_hashes"]["shadow_runtime_evidence"] == file_sha256(shadow_runtime)

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
    assert bundle_json["canary_closeout"]["passed"] is False


def test_phase5_rejects_offline_causal_research_and_missing_runtime_shadow(tmp_path: Path) -> None:
    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    manifest = reports_root / "model_manifest.json"
    components = {"meta": {"sha256": "b" * 64}}
    manifest.write_text(json.dumps({"bundle_run_id": "bundle-research", "components": components}), encoding="utf-8")
    manifest_sha = file_sha256(manifest)
    artifact_set_sha = mapping_sha256(components)
    economic = reports_root / "economic.json"
    economic.write_text(
        json.dumps(
            {
                "status": "complete",
                "net_pnl_usd": 500.0,
                "max_drawdown_pct": 1.0,
                "evidence_identity": ReleaseEvidenceIdentity(
                    pair="EURUSD",
                    bundle_run_id="bundle-research",
                    model_set_id="bundle-research",
                    model_manifest_sha256=manifest_sha,
                    artifact_set_sha256=artifact_set_sha,
                    evidence_kind="economic_validation",
                    source_kind="offline_causal_research",
                    advisory_only=True,
                ).to_dict(),
            }
        ),
        encoding="utf-8",
    )
    support = {}
    for name in ("feature_schema", "lineage", "execution_metrics"):
        path = reports_root / f"{name}.json"
        path.write_text("{}", encoding="utf-8")
        support[name] = path

    bundle = build_phase5_gate_bundle(
        pair="EURUSD",
        reports_root=reports_root,
        backtest_summary={"net_pnl_usd": 500.0, "max_drawdown_pct": 1.0},
        promotion_status="eligible",
        training_window_summary={},
        capabilities={"lifecycle_complete": True},
        training_eval_reports={"meta": "meta.json"},
        phase3_evidence_refs={"execution_metrics": str(support["execution_metrics"])},
        feature_schema_path=support["feature_schema"],
        lineage_path=support["lineage"],
        model_manifest_path=manifest,
        backtest_summary_path=economic,
        execution_metrics_path=support["execution_metrics"],
        bundle_run_id="bundle-research",
        model_set_id="bundle-research",
    )

    assert bundle.economic_gate.passed is False
    assert "offline_research_cannot_authorize_activation" in bundle.economic_gate.details["evidence_validation"]["errors"]
    assert bundle.shadow_gate.passed is False
    assert "evidence_artifact_missing" in bundle.shadow_gate.details["evidence_validation"]["errors"]


def test_phase5_binder_recomputes_fixed_support_and_rejects_weakened_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_manifest = tmp_path / "candidate_manifest.json"
    candidate_components = {"meta": {"content_sha256": "1" * 64}}
    candidate_manifest.write_text(
        json.dumps({"bundle_run_id": "bundle-1", "components": candidate_components}),
        encoding="utf-8",
    )
    support_paths: dict[str, Path] = {}
    for key in ("feature_schema", "lineage", "execution_metrics", "risk_trace_schema", "stress_harness_summary"):
        path = tmp_path / f"{key}.json"
        path.write_text(json.dumps({"status": "complete", "key": key}), encoding="utf-8")
        support_paths[key] = path
    training_eval = tmp_path / "training_eval.json"
    training_eval.write_text(json.dumps({"status": "eligible"}), encoding="utf-8")
    candidate_identity = candidate_manifest_identity(
        manifest_path=candidate_manifest,
        pair="EURUSD",
        model_set_id="model-1",
    )
    refs = {key: str(path) for key, path in support_paths.items()}
    refs.update(
        {
            "model_manifest": str(candidate_manifest),
            "backtest_summary": "",
            "shadow_runtime_evidence": "",
            "training_eval:meta": str(training_eval),
        }
    )
    support_artifacts = {
        **{key: {"path": str(path), "sha256": file_sha256(path)} for key, path in support_paths.items()},
        "training_eval:meta": {"path": str(training_eval), "sha256": file_sha256(training_eval)},
    }
    support_binding = tmp_path / "support_evidence_binding.json"
    support_binding.write_text(
        json.dumps(
            {
                "schema_version": "phase5_support_evidence_binding_v1",
                "evidence_identity": candidate_identity.to_dict(),
                "artifacts": support_artifacts,
            }
        ),
        encoding="utf-8",
    )
    refs["support_evidence_binding"] = str(support_binding)
    required = sorted([*support_paths, "training_eval:meta", "support_evidence_binding"])
    original = {
        "bundle_version": "phase5_gate_bundle_v2",
        "pair": "EURUSD",
        "evidence_identity": {
            "schema_version": "phase5_release_evidence_identity_v1",
            "pair": "EURUSD",
            "bundle_run_id": "bundle-1",
            "model_set_id": "model-1",
            "model_manifest_sha256": candidate_identity.model_manifest_sha256,
            "artifact_set_sha256": candidate_identity.artifact_set_sha256,
        },
        "binding_required_evidence": required,
        "evidence_refs": refs,
        "evidence_hashes": {
            "model_manifest": file_sha256(candidate_manifest),
            **{key: file_sha256(path) for key, path in support_paths.items()},
            "training_eval:meta": file_sha256(training_eval),
            "support_evidence_binding": file_sha256(support_binding),
        },
        "research_gate": {"gate": "research_gate", "passed": True, "details": {"promotion_status": "eligible"}},
        "economic_gate": {"gate": "economic_gate", "passed": False, "details": {}},
        "operational_gate": {"gate": "operational_gate", "passed": True, "details": {"phase3_execution_required": True}},
        # This serialized prerequisite is deliberately false. The binder must
        # derive authority from runtime lifecycle evidence, not this field.
        "shadow_gate": {"gate": "shadow_gate", "passed": False, "details": {"prerequisites_ready": False}},
        "canary_gate": {"gate": "canary_gate", "passed": False, "details": {}},
        "canary_closeout": {"gate": "canary_closeout", "passed": False, "details": {}},
    }
    phase5_path = tmp_path / "phase5_gate_bundle.json"
    phase5_path.write_text(json.dumps(original), encoding="utf-8")

    active_manifest = tmp_path / "active_models.json"
    active_manifest.write_text(
        json.dumps(
            {
                "active_model_sets": {
                    "EURUSD": {
                        "model_set_id": "model-1",
                        "metadata": {
                            "bundle_run_id": "bundle-1",
                            "promotion_status": "eligible",
                            "lifecycle_complete": True,
                            "capabilities": {"lifecycle_complete": True},
                        },
                        "artifacts": {"meta": {"content_sha256": "1" * 64}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    active = active_manifest_identity(
        manifest_path=active_manifest,
        pair="EURUSD",
    )
    economic = tmp_path / "economic.json"
    economic.write_text(
        json.dumps(
            {
                "status": "complete",
                "realized_pnl_usd": 125.0,
                "max_drawdown_pct": 2.0,
                "turnover_lots": 1.0,
                "trade_count": 10,
                "stress_summary": {
                    "status": "ok",
                    "scenario_count": 2,
                    "worst_realized_pnl_usd": 75.0,
                    "worst_drawdown_pct": 4.0,
                },
                "evidence_identity": {
                    **active.to_dict(),
                    "evidence_kind": "economic_validation",
                    "source_kind": "independent_execution_harness",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase5_gates,
        "validate_economic_evidence",
        lambda **_kwargs: EvidenceValidation(
            valid=True,
            errors=(),
            artifact_sha256=file_sha256(economic),
            identity=ReleaseEvidenceIdentity.from_dict(
                {
                    **active.to_dict(),
                    "evidence_kind": "economic_validation",
                    "source_kind": "independent_execution_harness",
                }
            ),
        ),
    )
    shadow = tmp_path / "shadow.json"
    shadow.write_text(
        json.dumps(
            {
                "schema_version": "fxstack_shadow_runtime_evidence_v2",
                "started_at": 1_000.0,
                "ended_at": 87_400.0,
                "evidence_identity": active.to_dict(),
                "gates": {"passed": True},
                "runtime_boundary": {
                    "agent_mode": "shadow",
                    "broker_emission_disabled": True,
                    "entry_commands_emitted": 0,
                    "active_manifest_matches_db": True,
                    "runtime_loaded_matches_db": True,
                    "activation_identity_consistent": True,
                    "startup_lifecycle": {
                        "startup_inference_ok": True,
                        "model_set_id": "model-1",
                        "pair_readiness_status": "ready",
                        "has_exit_model": True,
                        "has_reversal_models": True,
                        "lifecycle_activation_mode": "model_driven",
                        "lifecycle_ready": True,
                    },
                },
                "candidate": {"samples": 10, "runtime_ready_seen": True, "feature_ready_seen": True},
            }
        ),
        encoding="utf-8",
    )

    release_validation = tmp_path / "release_validation_bundle.json"
    release_validation.write_text(
        json.dumps(
            {
                "artifacts": {
                    "long_shadow": {
                        "path": str(shadow),
                        "sha256": file_sha256(shadow),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase5_gates,
        "validate_release_validation_bundle",
        lambda **_kwargs: EvidenceValidation(
            valid=True,
            errors=(),
            artifact_sha256=file_sha256(release_validation),
            identity=ReleaseEvidenceIdentity.from_dict(
                {
                    **active.to_dict(),
                    "evidence_kind": "release_validation",
                    "source_kind": "external_finalization",
                }
            ),
        ),
    )

    # These deliberately tiny support artifacts are structurally hashed but do
    # not satisfy the semantic schemas. Binding must fail closed rather than
    # upgrading their serialized pass booleans.
    with pytest.raises(ValueError, match="phase5_support_semantic_invalid"):
        bind_phase5_release_evidence(
            phase5_bundle_path=phase5_path,
            model_manifest_path=active_manifest,
            economic_evidence_path=economic,
            release_validation_bundle_path=release_validation,
            expected_pair="EURUSD",
            expected_bundle_run_id="bundle-1",
        )

    weakened = json.loads(json.dumps(original))
    weakened["binding_required_evidence"].remove("risk_trace_schema")
    weakened["evidence_refs"].pop("risk_trace_schema")
    weakened["evidence_hashes"].pop("risk_trace_schema")
    weakened["operational_gate"]["details"]["phase3_execution_required"] = False
    phase5_path.write_text(json.dumps(weakened), encoding="utf-8")
    with pytest.raises(ValueError, match="phase5_support_contract_weakened:risk_trace_schema"):
        bind_phase5_release_evidence(
            phase5_bundle_path=phase5_path,
            model_manifest_path=active_manifest,
            economic_evidence_path=economic,
            release_validation_bundle_path=release_validation,
            expected_pair="EURUSD",
            expected_bundle_run_id="bundle-1",
        )


def test_release_workflow_does_not_trust_serialized_pass_booleans() -> None:
    from fxstack.training.release_workflow import _promotion_gate_results_from_phase5_bundle

    forged = {
        "bundle_version": "phase5_gate_bundle_v1",
        "pair": "EURUSD",
        "bundle_run_id": "bundle-1",
        "economic_gate": {
            "gate": "economic_gate",
            "status": "pass",
            "passed": True,
            "reason": "serialized pass",
            "score": 1.0,
        },
        "shadow_gate": {
            "gate": "shadow_gate",
            "status": "pass",
            "passed": True,
            "reason": "serialized pass",
            "score": 1.0,
        },
        "canary_gate": {
            "gate": "canary_gate",
            "status": "pass",
            "passed": True,
            "reason": "serialized pass",
            "score": 1.0,
        },
    }

    results = _promotion_gate_results_from_phase5_bundle(
        forged,
        expected_pair="EURUSD",
        expected_bundle_run_id="bundle-1",
    )

    assert results
    assert all(result.passed is False for result in results)
    assert all("release_evidence_binding_invalid:" in result.reason for result in results)
