from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.features.session_contract import current_feature_schema, feature_contract_metadata
from fxstack.models.artifact_contract import stamp_artifact_payload_digest
from fxstack.mlops.types import ActivationPackage, BundleManifest, CanaryPlan, ReleaseNote, RollbackPlan
from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.service import RuntimeService
from fxstack.training.activation import activate_registry_file, parse_registry_entry


def _make_artifact(root: Path, name: str) -> str:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "model.bin").write_bytes(f"payload:{name}".encode("utf-8"))
    (path / "meta.json").write_text(
        json.dumps({"name": name, **feature_contract_metadata()}, indent=2),
        encoding="utf-8",
    )
    stamp_artifact_payload_digest(path)
    return str(path)


def _make_directional_belief_v2_artifact(root: Path) -> str:
    path = root / "directional_belief_v2"
    path.mkdir(parents=True, exist_ok=True)
    for name in [
        "ranker_xgb",
        "ev_above_hurdle_xgb",
        "expected_net_ev_bps_xgb",
        "confirm_success_xgb",
        "fail_fast_xgb",
    ]:
        subdir = path / name
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "model.bin").write_bytes(f"payload:{name}".encode("utf-8"))
        (subdir / "meta.json").write_text(
            json.dumps({"name": name, **feature_contract_metadata()}, indent=2),
            encoding="utf-8",
        )
        stamp_artifact_payload_digest(subdir)
    (path / "meta.json").write_text(
        json.dumps(
            {
                "model_version": "directional_belief_v2",
                "belief_contract": "directional_belief_v2",
                **feature_contract_metadata(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    stamp_artifact_payload_digest(path)
    return str(path)


def _write_phase3_bundle(root: Path, *, pair: str, dataset_hash: str, manifest_dataset_hash: str | None = None) -> dict[str, str]:
    phase3_dir = root / "phase3"
    phase3_dir.mkdir(parents=True, exist_ok=True)
    feature_service_name = f"fx_{pair.lower()}_execution_grade_m5"
    feature_service_version = "svc-v1"
    kernel_version = "phase3_risk_kernel_v1"
    manifest_hash = str(manifest_dataset_hash or dataset_hash)
    manifest_payloads = {
        "internal_harness_manifest.json": {
            "engine": "internal",
            "status": "planned",
            "pair": pair,
            "manifest_version": "phase3_harness_manifest_v1",
            "dataset_hash": manifest_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "engine_version": "3.12.0",
        },
        "nautilus_harness_manifest.json": {
            "engine": "nautilus",
            "status": "planned",
            "pair": pair,
            "manifest_version": "phase3_harness_manifest_v1",
            "dataset_hash": manifest_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "engine_version": "1.0.0",
        },
        "lean_harness_manifest.json": {
            "engine": "lean",
            "status": "planned",
            "pair": pair,
            "manifest_version": "phase3_harness_manifest_v1",
            "dataset_hash": manifest_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "engine_version": "2.0.0",
        },
    }
    payloads = {
        "execution_metrics.json": {
            "status": "planned",
            "engine": "internal",
            "pair": pair,
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
        },
        "market_replay_bundle.json": {
            "pair": pair,
            "timeframe": "M5",
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
        },
        "intent_replay_bundle.json": {
            "pair": pair,
            "intents_path": str(phase3_dir / "intent_replay_bundle.json"),
            "policy_version": "phase3_policy_v1",
            "kernel_version": kernel_version,
        },
        "golden_dataset_report.json": {
            "status": "ok",
            "market": {
                "pair": pair,
                "dataset_hash": dataset_hash,
                "feature_service_name": feature_service_name,
                "feature_service_version": feature_service_version,
            },
            "intents": {"pair": pair, "kernel_version": kernel_version},
        },
        "stress_harness_summary.json": {
            "status": "planned",
            "base_engine": "internal",
            "dataset_hash": dataset_hash,
            "feature_service_name": feature_service_name,
            "feature_service_version": feature_service_version,
            "kernel_version": kernel_version,
            "scenario_count": 1,
            "scenarios": [{"name": "BaseCase"}],
        },
        "harness_comparison.json": {"status": "ok", "manifests": list(manifest_payloads.values())},
        "risk_trace_schema.json": {
            "schema_version": "phase3_risk_trace_schema_v1",
            "kernel_version": kernel_version,
            "rule_order": ["data_freshness", "marketability"],
        },
        **manifest_payloads,
    }
    refs: dict[str, str] = {}
    for name, payload in payloads.items():
        path = phase3_dir / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        refs[name.replace(".json", "")] = str(path)
    return refs


def _write_minimal_registry(
    root: Path,
    *,
    feature_schema: dict[str, object],
) -> tuple[Path, dict[str, str]]:
    artifacts_root = root / "artifacts"
    artifact_paths = {
        name: _make_artifact(artifacts_root, name)
        for name in ("regime_hmm", "meta_filter", "swing_xgb", "intraday_xgb")
    }
    registry = root / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "run_id": "contract-check",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": artifact_paths["regime_hmm"]},
                    "meta": {"path": artifact_paths["meta_filter"]},
                    "swing_xgb": {"path": artifact_paths["swing_xgb"]},
                    "intraday_xgb": {"path": artifact_paths["intraday_xgb"]},
                },
                "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
                "feature_schema": feature_schema,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return registry, artifact_paths


def test_activation_rejects_unversioned_feature_schema(tmp_path: Path) -> None:
    registry, _ = _write_minimal_registry(
        tmp_path,
        feature_schema={"intraday_contract": "hierarchical_v1"},
    )

    with pytest.raises(ValueError, match="feature_contract_mismatch:registry"):
        parse_registry_entry(registry)


def test_activation_rejects_old_model_artifact_under_current_schema(tmp_path: Path) -> None:
    registry, artifact_paths = _write_minimal_registry(
        tmp_path,
        feature_schema=current_feature_schema(),
    )
    stale_meta_path = Path(artifact_paths["regime_hmm"]) / "meta.json"
    stale_meta = json.loads(stale_meta_path.read_text(encoding="utf-8"))
    stale_meta["session_contract_version"] = "utc_session_buckets_v1"
    stale_meta_path.write_text(json.dumps(stale_meta, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="feature_contract_mismatch:artifact:regime"):
        parse_registry_entry(registry)


@pytest.mark.parametrize("sidecar_payload", ["{}", "{"])
def test_activation_rejects_empty_or_malformed_artifact_sidecar(
    tmp_path: Path,
    sidecar_payload: str,
) -> None:
    registry, artifact_paths = _write_minimal_registry(
        tmp_path,
        feature_schema=current_feature_schema(),
    )
    (Path(artifact_paths["regime_hmm"]) / "meta.json").write_text(
        sidecar_payload,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="artifact_sidecar_invalid:artifact:regime"):
        parse_registry_entry(registry)


def test_activation_rejects_malformed_directional_belief_component_sidecar(
    tmp_path: Path,
) -> None:
    registry, _ = _write_minimal_registry(
        tmp_path,
        feature_schema=current_feature_schema({"belief_contract": "directional_belief_v2"}),
    )
    belief_path = Path(_make_directional_belief_v2_artifact(tmp_path / "artifacts"))
    (belief_path / "ranker_xgb" / "meta.json").write_text("{", encoding="utf-8")
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["artifacts"]["directional_belief"] = {"path": str(belief_path)}
    for component_name in ("exit_policy", "reversal_failure", "reversal_opportunity"):
        payload["artifacts"][component_name] = {
            "path": _make_artifact(tmp_path / "artifacts", component_name)
        }
    registry.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="artifact_sidecar_invalid:directional_belief:ranker_xgb",
    ):
        parse_registry_entry(registry)


def test_activation_requires_registry_belief_contract_when_belief_is_configured(
    tmp_path: Path,
) -> None:
    registry, _ = _write_minimal_registry(
        tmp_path,
        feature_schema=current_feature_schema(),
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["artifacts"]["directional_belief"] = {
        "path": _make_directional_belief_v2_artifact(tmp_path / "artifacts")
    }
    for component_name in ("exit_policy", "reversal_failure", "reversal_opportunity"):
        payload["artifacts"][component_name] = {
            "path": _make_artifact(tmp_path / "artifacts", component_name)
        }
    registry.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="belief_contract_invalid:directional_belief:registry",
    ):
        parse_registry_entry(registry)


def test_activation_rejects_registry_and_artifact_belief_contract_mismatch(
    tmp_path: Path,
) -> None:
    registry, _ = _write_minimal_registry(
        tmp_path,
        feature_schema=current_feature_schema(
            {"belief_contract": "directional_belief_v1"}
        ),
    )
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["artifacts"]["directional_belief"] = {
        "path": _make_directional_belief_v2_artifact(tmp_path / "artifacts")
    }
    for component_name in ("exit_policy", "reversal_failure", "reversal_opportunity"):
        payload["artifacts"][component_name] = {
            "path": _make_artifact(tmp_path / "artifacts", component_name)
        }
    registry.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="belief_contract_mismatch:directional_belief"):
        parse_registry_entry(registry)


def test_runtime_loader_rejects_legacy_active_row_before_model_deserialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime import service as service_module
    from fxstack.runtime.runner import _load_model_sets

    class FakeRuntimeService:
        def __init__(self, **_: object) -> None:
            pass

        def get_active_model_sets(self, *, enabled_only: bool = True) -> dict[str, object]:
            assert enabled_only is True
            return {
                "EURUSD": {
                    "model_set_id": "legacy-active-row",
                    "artifacts_json": {},
                    "metadata_json": {
                        "feature_schema": {"intraday_contract": "hierarchical_v1"}
                    },
                }
            }

    monkeypatch.setattr(service_module, "RuntimeService", FakeRuntimeService)

    loaded, diagnostics = _load_model_sets(
        pairs=["EURUSD"],
        require_all=False,
        project_root=tmp_path,
    )

    assert loaded == {}
    assert diagnostics["failed_pairs"] == ["EURUSD"]
    assert diagnostics["pairs"]["EURUSD"]["failure_component"] == "feature_contract"


def test_runtime_loader_rejects_malformed_artifact_sidecar_before_deserialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime import service as service_module
    from fxstack.runtime.runner import _load_model_sets

    artifacts_root = tmp_path / "artifacts"
    artifact_paths = {
        name: _make_artifact(artifacts_root, name)
        for name in ("regime_hmm", "meta_filter", "swing_xgb", "intraday_xgb")
    }
    (Path(artifact_paths["regime_hmm"]) / "meta.json").write_text("{", encoding="utf-8")

    class FakeRuntimeService:
        def __init__(self, **_: object) -> None:
            pass

        def get_active_model_sets(self, *, enabled_only: bool = True) -> dict[str, object]:
            assert enabled_only is True
            return {
                "EURUSD": {
                    "model_set_id": "malformed-artifact-sidecar",
                    "artifacts_json": {
                        "regime": {"path": artifact_paths["regime_hmm"]},
                        "meta": {"path": artifact_paths["meta_filter"]},
                        "swing_xgb": {"path": artifact_paths["swing_xgb"]},
                        "intraday_xgb": {"path": artifact_paths["intraday_xgb"]},
                    },
                    "metadata_json": {
                        "feature_schema": current_feature_schema(),
                        "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
                    },
                }
            }

    monkeypatch.setattr(service_module, "RuntimeService", FakeRuntimeService)

    loaded, diagnostics = _load_model_sets(
        pairs=["EURUSD"],
        require_all=False,
        project_root=tmp_path,
    )

    assert loaded == {}
    assert diagnostics["failed_pairs"] == ["EURUSD"]
    assert diagnostics["pairs"]["EURUSD"]["failure_component"] == "artifact_contract"
    assert "artifact_sidecar_invalid" in diagnostics["pairs"]["EURUSD"]["failure_reason"]


def test_activate_registry_file_updates_db_and_manifest(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    artifacts_root = tmp_path / "artifacts"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    reg = tmp_path / "registry" / "eurusd_run1.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "run_id": "run1",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
                    "meta": {"path": _make_artifact(artifacts_root, "meta_filter")},
                    "swing_transformer": {"path": _make_artifact(artifacts_root, "swing_transformer")},
                    "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
                    "intraday_tcn": {"path": _make_artifact(artifacts_root, "intraday_tcn")},
                    "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
                    "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy")},
                    "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure")},
                    "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity")},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": current_feature_schema(),
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "active_models.json"

    item = activate_registry_file(
        database_url=db_url,
        registry_file=reg,
        manifest_path=manifest,
    )
    assert str(item.get("pair")) == "EURUSD"

    svc = RuntimeService(database_url=db_url)
    active = svc.get_active_model_set("EURUSD")
    assert active is not None
    assert str(active.get("model_set_id")) == "run1"

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert str(payload["active_model_sets"]["EURUSD"]["model_set_id"]) == "run1"
    assert str(payload["active_model_sets"]["EURUSD"]["policies"]["swing"]) == "transformer_primary_xgb_fallback"


def test_activate_registry_file_rejects_configured_missing_directional_belief(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    artifacts_root = tmp_path / "artifacts"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    reg = tmp_path / "registry" / "eurusd_run_belief_optional.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "run_id": "run-belief-optional",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
                    "meta": {"path": _make_artifact(artifacts_root, "meta_filter")},
                    "swing_transformer": {"path": _make_artifact(artifacts_root, "swing_transformer")},
                    "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
                    "intraday_tcn": {"path": _make_artifact(artifacts_root, "intraday_tcn")},
                    "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
                    "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy")},
                    "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure")},
                    "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity")},
                    "directional_belief": {"path": str(artifacts_root / "directional_belief_missing")},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": current_feature_schema(),
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "active_models.json"

    with pytest.raises(ValueError, match="unresolved directional belief artifact"):
        activate_registry_file(
            database_url=db_url,
            registry_file=reg,
            manifest_path=manifest,
        )


def test_activate_registry_file_accepts_directional_belief_v2_artifact(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    artifacts_root = tmp_path / "artifacts"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    reg = tmp_path / "registry" / "eurusd_run_belief_v2.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "run_id": "run-belief-v2",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
                    "meta": {"path": _make_artifact(artifacts_root, "meta_filter")},
                    "swing_transformer": {"path": _make_artifact(artifacts_root, "swing_transformer")},
                    "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
                    "intraday_tcn": {"path": _make_artifact(artifacts_root, "intraday_tcn")},
                    "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
                    "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy")},
                    "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure")},
                    "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity")},
                    "directional_belief": {"path": _make_directional_belief_v2_artifact(artifacts_root)},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": current_feature_schema({"belief_contract": "directional_belief_v2"}),
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "active_models.json"

    item = activate_registry_file(
        database_url=db_url,
        registry_file=reg,
        manifest_path=manifest,
    )

    assert str(item.get("pair")) == "EURUSD"
    assert bool(item.get("metadata", {}).get("capabilities", {}).get("has_directional_belief", False)) is True


def test_activate_registry_file_rejects_phase3_evidence_dataset_mismatch(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    artifacts_root = tmp_path / "artifacts"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    reg = tmp_path / "registry" / "eurusd_run_phase3_mismatch.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    phase3_refs = _write_phase3_bundle(
        tmp_path / "artifacts" / "eurusd_phase3",
        pair="EURUSD",
        dataset_hash="dataset-a",
        manifest_dataset_hash="dataset-b",
    )
    reg.write_text(
        json.dumps(
            {
                "run_id": "run-phase3-mismatch",
                "pair": "EURUSD",
                "phase3_execution_required": True,
                "phase3_evidence": phase3_refs,
                "artifacts": {
                    "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
                    "meta": {"path": _make_artifact(artifacts_root, "meta_filter")},
                    "swing_transformer": {"path": _make_artifact(artifacts_root, "swing_transformer")},
                    "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
                    "intraday_tcn": {"path": _make_artifact(artifacts_root, "intraday_tcn")},
                    "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
                    "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy")},
                    "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure")},
                    "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity")},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": current_feature_schema(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "active_models.json"

    try:
        activate_registry_file(
            database_url=db_url,
            registry_file=reg,
            manifest_path=manifest,
        )
    except ValueError as exc:
        assert "phase3_evidence_mismatch:dataset_hash" in str(exc)
    else:
        raise AssertionError("expected activation to reject mismatched Phase 3 dataset hashes")


def test_activation_package_round_trips_release_lineage_refs() -> None:
    package = ActivationPackage(
        bundle_run_id="bundle-1",
        pair="EURUSD",
        target_alias="shadow",
        release_status="staged",
        promotion_status="eligible",
        experiment_id="exp-1",
        promotion_id="promo-1",
        experiment_lineage_ref="/tmp/experiment_lineage.json",
        paper_pack_ref="/tmp/paper_pack.md",
        canary_pack_ref="/tmp/canary_pack.md",
        rollback_plan_ref="/tmp/rollback_plan.json",
        rollback_target=RollbackPlan(
            target_bundle_run_id="bundle-0",
            target_alias="champion",
            target_registry_path="mlflow://EURUSD@champion",
        ),
        canary_plan=CanaryPlan(plan_id="plan-1", scope="pair_allowlist", status="planned"),
        evidence_refs={
            "experiment_lineage": "/tmp/experiment_lineage.json",
            "paper_pack": "/tmp/paper_pack.md",
            "canary_pack": "/tmp/canary_pack.md",
            "rollback_plan": "/tmp/rollback_plan.json",
        },
        metadata={"experiment_id": "exp-1", "promotion_id": "promo-1"},
    )

    round_tripped = ActivationPackage.from_dict(package.to_dict())
    assert round_tripped.experiment_id == "exp-1"
    assert round_tripped.promotion_id == "promo-1"
    assert round_tripped.experiment_lineage_ref == "/tmp/experiment_lineage.json"
    assert round_tripped.paper_pack_ref == "/tmp/paper_pack.md"
    assert round_tripped.canary_pack_ref == "/tmp/canary_pack.md"
    assert round_tripped.rollback_plan_ref == "/tmp/rollback_plan.json"


def test_release_workflow_persists_release_lineage_refs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_PHASE5_RELEASE_ROOT", str(tmp_path / "releases"))

    from fxstack.training.release_workflow import _build_release_package, _persist_release_artifacts

    phase5_dir = tmp_path / "phase5"
    phase5_dir.mkdir(parents=True, exist_ok=True)
    phase5_bundle_path = phase5_dir / "phase5_gate_bundle.json"
    phase5_bundle_path.write_text(
        json.dumps(
            {
                "research_gate": {"gate": "research_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "economic_gate": {"gate": "economic_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "operational_gate": {"gate": "operational_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "shadow_gate": {"gate": "shadow_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    bundle = BundleManifest(
        bundle_run_id="bundle-1",
        pair="EURUSD",
        tier="tier2",
        dataset_fingerprint="fp-1",
        feature_service_version="fs-1",
        label_version="lv-1",
        risk_config_version="rv-1",
        promotion_status="eligible",
        metadata={
            "experiment_id": "exp-1",
            "promotion_id": "promo-1",
            "phase5_gates": {
                "phase5_gate_bundle": str(phase5_bundle_path),
                "research_gate": str(phase5_dir / "research_gate.json"),
                "economic_gate": str(phase5_dir / "economic_gate.json"),
                "operational_gate": str(phase5_dir / "operational_gate.json"),
                "shadow_gate": str(phase5_dir / "shadow_gate.json"),
            },
        },
    )
    package = _build_release_package(
        bundle=bundle,
        release_status="staged",
        target_alias="shadow",
        rollback_target=RollbackPlan(target_bundle_run_id="bundle-0", target_alias="champion", target_registry_path="mlflow://EURUSD@champion"),
        canary_plan=CanaryPlan(plan_id="plan-1", scope="pair_allowlist", status="planned"),
        release_notes=[ReleaseNote(title="note", summary="summary", category="promotion")],
        operator_signoff={"approvers": ["ops"]},
    )
    written = _persist_release_artifacts(package=package, note=None, phase5_bundle={"research_gate": {}, "economic_gate": {}, "operational_gate": {}, "shadow_gate": {}})
    release_dir = Path(written["release_dir"])
    activation_package = json.loads((release_dir / "activation_package.json").read_text(encoding="utf-8"))

    assert activation_package["experiment_id"] == "exp-1"
    assert activation_package["promotion_id"] == "promo-1"
    assert Path(activation_package["experiment_lineage_ref"]).exists()
    assert Path(activation_package["paper_pack_ref"]).exists()
    assert Path(activation_package["canary_pack_ref"]).exists()
    assert Path(activation_package["rollback_plan_ref"]).exists()
    assert (release_dir / "experiment_lineage.json").exists()
    assert (release_dir / "paper_pack.md").exists()
    assert (release_dir / "canary_pack.md").exists()
    assert (release_dir / "rollback_plan.json").exists()
