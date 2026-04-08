from __future__ import annotations

import json
from pathlib import Path

from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.service import RuntimeService
from fxstack.training.activation import activate_registry_file


def _make_artifact(root: Path, name: str) -> str:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({"name": name}, indent=2), encoding="utf-8")
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
        (subdir / "meta.json").write_text(json.dumps({"name": name}, indent=2), encoding="utf-8")
    (path / "meta.json").write_text(
        json.dumps(
            {
                "model_version": "directional_belief_v2",
                "belief_contract": "directional_belief_v2",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
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
                "feature_schema": {"intraday_contract": "hierarchical_v1"},
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


def test_activate_registry_file_allows_missing_optional_directional_belief(tmp_path: Path):
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
                "feature_schema": {"intraday_contract": "hierarchical_v1"},
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
    assert bool(item.get("capabilities", {}).get("has_directional_belief", False)) is False


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
                "feature_schema": {"intraday_contract": "hierarchical_v1", "belief_contract": "directional_belief_v2"},
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
                "feature_schema": {"intraday_contract": "hierarchical_v1"},
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
