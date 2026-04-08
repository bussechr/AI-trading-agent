from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


def _configure_mlflow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    tracking_db = tmp_path / "mlflow.db"
    tracking_uri = f"sqlite:///{tracking_db}"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FXSTACK_MLFLOW_ENABLED", "1")
    monkeypatch.setenv("FXSTACK_MLFLOW_TRACKING_URI", tracking_uri)
    monkeypatch.setenv("FXSTACK_MLFLOW_REGISTRY_URI", tracking_uri)
    monkeypatch.setenv("FXSTACK_MLFLOW_CACHE_ROOT", str(tmp_path / "mlflow_cache"))
    get_settings.cache_clear()
    return tracking_uri


def _make_artifact(root: Path, name: str, *, with_reports: bool = False) -> str:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    meta = {
        "name": name,
        "trained_at": 1775433600.0,
        "data_window_end": "2026-04-05T00:00:00+00:00",
        "feature_columns": ["ret_1", "spread_bps"],
        "training_window_summary": {
            "rows": 128,
            "start_ts": "2026-01-01T00:00:00+00:00",
            "end_ts": "2026-04-05T00:00:00+00:00",
        },
    }
    (path / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if with_reports:
        reports = path / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "training_report.json").write_text(json.dumps({"status": "ok"}, indent=2), encoding="utf-8")
        (reports / "promotion_decision.json").write_text(
            json.dumps({"status": "eligible", "candidate_metric": 0.62}, indent=2),
            encoding="utf-8",
        )
    return str(path)


def _compat_payload(tmp_path: Path, *, pair: str, run_id: str) -> dict:
    artifacts_root = tmp_path / f"artifacts_{run_id}"
    return {
        "run_id": run_id,
        "bundle_run_id": run_id,
        "pair": pair,
        "tier": "tier2",
        "trained_at": 1775433600.0,
        "data_window_end": "2026-04-05T00:00:00+00:00",
        "dataset_fingerprint": f"{run_id}-fp",
        "feature_service_version": f"{run_id}-feature",
        "label_version": f"{run_id}-label",
        "risk_config_version": f"{run_id}-risk",
        "feature_schema": {
            "intraday_contract": "hierarchical_v1",
            "belief_contract": "directional_belief_v2",
        },
        "training_window_summary": {
            "regime": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "swing_xgb": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "intraday_xgb": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "meta": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "exit_policy": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "reversal_failure": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
            "reversal_opportunity": {"rows": 128, "start_ts": "2026-01-01T00:00:00+00:00", "end_ts": "2026-04-05T00:00:00+00:00"},
        },
        "promotion_status": "eligible",
        "artifacts": {
            "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
            "meta": {"path": _make_artifact(artifacts_root, "meta_filter", with_reports=True)},
            "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
            "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
            "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy_xgb", with_reports=True)},
            "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure_xgb", with_reports=True)},
            "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity_xgb", with_reports=True)},
        },
        "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
        "capabilities": {
            "has_exit_model": True,
            "has_reversal_models": True,
            "lifecycle_complete": True,
            "has_directional_belief": False,
        },
        "lifecycle_complete": True,
        "training_config": {"labeling": {"intraday": {"horizon_bars": 18}}},
        "promotion_components": {
            "meta": "eligible",
            "exit": "eligible",
            "reversal_failure": "eligible",
            "reversal_opportunity": "eligible",
        },
        "training_eval_reports": {
            "meta": str(artifacts_root / "meta_filter" / "reports" / "training_report.json"),
            "exit": str(artifacts_root / "exit_policy_xgb" / "reports" / "training_report.json"),
            "reversal_failure": str(artifacts_root / "reversal_failure_xgb" / "reports" / "training_report.json"),
            "reversal_opportunity": str(artifacts_root / "reversal_opportunity_xgb" / "reports" / "training_report.json"),
        },
        "timeframes": {"regime": "H4", "swing": "D", "intraday": "M5"},
    }


def test_lineage_snapshot_is_deterministic_and_changes_with_inputs(tmp_path: Path):
    from fxstack.mlops.lineage import compute_lineage_snapshot

    data_root = tmp_path / "features"
    data_root.mkdir(parents=True, exist_ok=True)
    first = data_root / "part-000.parquet"
    first.write_text("a", encoding="utf-8")

    one = compute_lineage_snapshot(
        feature_paths=[data_root],
        feature_schema={"version": 1, "columns": ["a"]},
        label_config={"horizon": 12},
        risk_config={"promotion_policy": "balanced"},
        training_config={"epochs": 1},
        pair="EURUSD",
        timeframes={"intraday": "M5"},
        project_root=Path(__file__).resolve().parents[1],
    )
    two = compute_lineage_snapshot(
        feature_paths=[data_root],
        feature_schema={"version": 1, "columns": ["a"]},
        label_config={"horizon": 12},
        risk_config={"promotion_policy": "balanced"},
        training_config={"epochs": 1},
        pair="EURUSD",
        timeframes={"intraday": "M5"},
        project_root=Path(__file__).resolve().parents[1],
    )
    assert one.dataset_fingerprint == two.dataset_fingerprint

    first.write_text("b", encoding="utf-8")
    changed = compute_lineage_snapshot(
        feature_paths=[data_root],
        feature_schema={"version": 1, "columns": ["a"]},
        label_config={"horizon": 12},
        risk_config={"promotion_policy": "balanced"},
        training_config={"epochs": 1},
        pair="EURUSD",
        timeframes={"intraday": "M5"},
        project_root=Path(__file__).resolve().parents[1],
    )
    assert changed.dataset_fingerprint != one.dataset_fingerprint


def test_model_uri_resolves_registered_alias_to_legacy_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    tracking_uri = _configure_mlflow_env(tmp_path, monkeypatch)
    assert tracking_uri.startswith("sqlite:///")

    from fxstack.mlops.model_uri import resolve_model_artifact_path
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias

    bundle = import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-a"), intended_alias="champion")
    resolved_bundle = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="champion")
    assert resolved_bundle.bundle_run_id == bundle.bundle_run_id

    artifact_path = resolve_model_artifact_path(resolved_bundle.components["meta"].model_uri)
    assert (artifact_path / "meta.json").exists()
    meta = json.loads((artifact_path / "meta.json").read_text(encoding="utf-8"))
    assert meta["name"] == "meta_filter"


def test_resolve_model_artifact_path_prefers_local_path_when_mlflow_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fxstack.mlops.model_uri import resolve_model_artifact_path

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FXSTACK_MLFLOW_ENABLED", "0")
    get_settings.cache_clear()
    try:
        artifact_dir = tmp_path / "artifact_local"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "meta.json").write_text(json.dumps({"name": "local-artifact"}), encoding="utf-8")

        resolved = resolve_model_artifact_path(
            {
                "path": str(artifact_dir),
                "model_uri": "models:/fx.meta_filter.EURUSD.M5@champion",
            },
            project_root=tmp_path,
        )

        assert resolved == artifact_dir.resolve()
    finally:
        get_settings.cache_clear()


def test_resolve_model_artifact_path_prefers_local_path_when_mlflow_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.model_uri import resolve_model_artifact_path

    try:
        artifact_dir = tmp_path / "artifact_local"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "meta.json").write_text(json.dumps({"name": "local-artifact"}), encoding="utf-8")

        resolved = resolve_model_artifact_path(
            {
                "path": str(artifact_dir),
                "model_uri": "models:/fx.meta_filter.EURUSD.M5@champion",
            },
            project_root=tmp_path,
        )

        assert resolved == artifact_dir.resolve()
    finally:
        get_settings.cache_clear()


def test_shadow_alias_resolves_patchtst_components(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias

    payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-shadow-patchtst")
    patch_root = tmp_path / "artifacts_patchtst"
    payload["artifacts"]["swing_patchtst"] = {"path": _make_artifact(patch_root, "swing_patchtst", with_reports=True)}
    payload["artifacts"]["intraday_patchtst"] = {"path": _make_artifact(patch_root, "intraday_patchtst", with_reports=True)}
    import_compat_bundle_to_mlflow(payload, intended_alias="shadow")

    resolved = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="shadow")

    assert "swing_patchtst" in resolved.components
    assert "intraday_patchtst" in resolved.components
    assert resolved.components["swing_patchtst"].model_uri.endswith("@shadow")


def test_shadow_alias_preserves_patchtst_phase4_metadata_and_report_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias

    payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-shadow-patchtst-phase4")
    patch_root = tmp_path / "artifacts_patchtst_phase4"
    swing_path = Path(_make_artifact(patch_root, "swing_patchtst", with_reports=True))
    intraday_path = Path(_make_artifact(patch_root, "intraday_patchtst", with_reports=True))
    for artifact_path, prefix in [(swing_path, "swing"), (intraday_path, "intraday")]:
        reports = artifact_path / "reports"
        sequence_dataset_manifest = artifact_path / f"{prefix}_sequence_dataset_manifest.json"
        portfolio_report = artifact_path / f"{prefix}_portfolio_report.json"
        head_to_head = artifact_path / f"{prefix}_challenger_head_to_head.json"
        disagreement = artifact_path / f"{prefix}_portfolio_disagreement.json"
        for path in [sequence_dataset_manifest, portfolio_report, head_to_head, disagreement]:
            path.write_text("{}", encoding="utf-8")
        meta = json.loads((artifact_path / "meta.json").read_text(encoding="utf-8"))
        meta.update(
            {
                "sequence_dataset_manifest": str(sequence_dataset_manifest),
                "portfolio_report": str(portfolio_report),
                "challenger_head_to_head": str(head_to_head),
                "portfolio_disagreement": str(disagreement),
                "model_manifest": str(tmp_path / f"{prefix}_bundle_manifest.json"),
            }
        )
        (artifact_path / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        assert reports.exists()
    payload["phase4_shadow_only"] = True
    payload["phase4_sequence_dataset_manifests"] = {"swing_patchtst": "seq-swing.json", "intraday_patchtst": "seq-intraday.json"}
    payload["phase4_portfolio_reports"] = {"swing_patchtst": "portfolio-swing.json", "intraday_patchtst": "portfolio-intraday.json"}
    payload["phase4_challenger_reports"] = {"swing_patchtst": "head-swing.json", "intraday_patchtst": "head-intraday.json"}
    payload["artifacts"]["swing_patchtst"] = {"path": str(swing_path)}
    payload["artifacts"]["intraday_patchtst"] = {"path": str(intraday_path)}

    import_compat_bundle_to_mlflow(payload, intended_alias="shadow")
    resolved = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="shadow")

    assert bool(resolved.metadata["phase4_shadow_only"]) is True
    assert resolved.metadata["phase4_sequence_dataset_manifests"]["swing_patchtst"] == "seq-swing.json"
    assert resolved.metadata["phase4_portfolio_reports"]["intraday_patchtst"] == "portfolio-intraday.json"
    swing_refs = resolved.components["swing_patchtst"].evidence_refs
    assert swing_refs["training_report"].endswith("training_report.json")
    assert swing_refs["model_manifest"].endswith("bundle_manifest.json")
    assert swing_refs["sequence_dataset_manifest"].endswith("swing_sequence_dataset_manifest.json")
    assert swing_refs["portfolio_report"].endswith("swing_portfolio_report.json")


def test_activate_mlflow_alias_populates_runtime_store_and_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.training.activation import activate_mlflow_alias

    bundle = import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-live"), intended_alias="champion")
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    manifest_path = tmp_path / "active_models.json"
    activated = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=manifest_path,
        pairs=["EURUSD"],
        alias="champion",
    )
    assert str(activated[0]["model_set_id"]) == bundle.bundle_run_id

    svc = RuntimeService(database_url=db_url)
    active = svc.get_active_model_set("EURUSD")
    assert active is not None
    artifacts = dict(active.get("artifacts_json") or {})
    assert str((artifacts["meta"] or {}).get("model_uri") or "").endswith("@champion")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = payload["active_model_sets"]["EURUSD"]
    assert str(entry["metadata"]["bundle_run_id"]) == bundle.bundle_run_id
    assert str(entry["artifacts"]["meta"]["model_uri"]).endswith("@champion")


def test_activate_mlflow_alias_rejects_runtime_incompatible_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.training.activation import activate_mlflow_alias

    payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-incompatible")
    payload["runtime_compatible"] = False
    import_compat_bundle_to_mlflow(payload, intended_alias="champion")
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    with pytest.raises(ValueError, match="runtime_incompatible"):
        activate_mlflow_alias(
            database_url=db_url,
            manifest_path=tmp_path / "active_models.json",
            pairs=["EURUSD"],
            alias="champion",
        )


def test_backfill_and_alias_reassignment_supports_rollback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, set_bundle_alias
    from fxstack.training.activation import activate_mlflow_alias, backfill_mlflow_state

    champion_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-champion")
    shadow_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-shadow")
    champion_registry = tmp_path / "registry" / "eurusd_champion.json"
    champion_registry.parent.mkdir(parents=True, exist_ok=True)
    champion_registry.write_text(json.dumps(champion_payload, indent=2), encoding="utf-8")
    shadow_registry = tmp_path / "artifacts_shadow" / "registry_full_20260405_1200_manual" / "eurusd_shadow.json"
    shadow_registry.parent.mkdir(parents=True, exist_ok=True)
    shadow_registry.write_text(json.dumps(shadow_payload, indent=2), encoding="utf-8")

    active_manifest = tmp_path / "active_models.json"
    active_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_model_sets": {
                    "EURUSD": {
                        "model_set_id": "bundle-champion",
                        "registry_path": str(champion_registry),
                        "artifacts": champion_payload["artifacts"],
                        "policies": champion_payload["policies"],
                        "metadata": champion_payload,
                        "enabled": True,
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    backfill = backfill_mlflow_state(
        active_manifest_path=active_manifest,
        registry_root=champion_registry.parent,
        shadow_root=tmp_path / "artifacts_shadow",
    )
    assert bool(backfill.get("ok")) is True
    assert "EURUSD" in backfill["active_pairs"]
    assert "EURUSD" in backfill["shadow_pairs"]

    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    champion_active = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=tmp_path / "activated_models.json",
        pairs=["EURUSD"],
        alias="champion",
    )
    shadow_active = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=tmp_path / "activated_models_shadow.json",
        pairs=["EURUSD"],
        alias="shadow",
    )
    assert champion_active[0]["model_set_id"] != shadow_active[0]["model_set_id"]

    old_bundle = import_compat_bundle_to_mlflow(champion_payload, intended_alias="")
    moved = set_bundle_alias(bundle=old_bundle, alias="champion")
    assert bool(moved.get("ok")) is True

    rolled_back = activate_mlflow_alias(
        database_url=db_url,
        manifest_path=tmp_path / "activated_models_rollback.json",
        pairs=["EURUSD"],
        alias="champion",
    )
    assert rolled_back[0]["model_set_id"] == champion_active[0]["model_set_id"]


def test_phase5_release_workflow_stages_canaries_graduates_and_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow, resolve_bundle_manifest_by_alias
    from fxstack.training.release_workflow import (
        canary_start,
        close_canary,
        promote_release,
        release_status,
        rollback_release,
        shadow_accept,
        stage_release,
    )

    champion_bundle = import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-p5-champion"), intended_alias="champion")
    shadow_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-p5-shadow")
    phase5_dir = tmp_path / "phase5_shadow_reports"
    phase5_dir.mkdir(parents=True, exist_ok=True)
    phase5_bundle_path = phase5_dir / "phase5_gate_bundle.json"
    phase5_bundle_path.write_text(
        json.dumps(
            {
                "research_gate": {"gate": "research_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "economic_gate": {"gate": "economic_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "operational_gate": {"gate": "operational_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "shadow_gate": {"gate": "shadow_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "canary_gate": {"gate": "canary_gate", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
                "canary_closeout": {"gate": "canary_closeout", "status": "pass", "passed": True, "reason": "ok", "score": 1.0},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    shadow_payload["phase5_gates"] = {
        "phase5_gate_bundle": str(phase5_bundle_path),
        "research_gate": str(phase5_dir / "research_gate.json"),
        "economic_gate": str(phase5_dir / "economic_gate.json"),
        "operational_gate": str(phase5_dir / "operational_gate.json"),
        "shadow_gate": str(phase5_dir / "shadow_gate.json"),
        "canary_gate": str(phase5_dir / "canary_gate.json"),
        "canary_closeout": str(phase5_dir / "canary_closeout.json"),
    }
    shadow_bundle = import_compat_bundle_to_mlflow(shadow_payload, intended_alias="shadow")

    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    manifest_path = tmp_path / "active_models.json"

    staged = stage_release(pair="EURUSD", alias="shadow", author="ops", allowlisted_pairs=["EURUSD"])
    assert bool(staged.get("ok")) is True

    promoted = promote_release(pair="EURUSD", author="ops")
    assert promoted["release_status"] == "staged"
    assert "ops" in promoted["signed_off_by"]

    shadow_ok = shadow_accept(pair="EURUSD")
    assert bool(shadow_ok.get("ok")) is True
    assert shadow_ok["release_status"] == "shadow_accepted"
    assert shadow_ok["shadow_acceptance_summary"]["ready"] is True
    assert shadow_ok["phase5_gate_summary"]["all_required_passed"] is True

    started = canary_start(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
    )
    assert bool(started.get("ok")) is True
    assert started["release_status"] == "canary_active"
    assert started["canary_prep"]["status"] == "active"
    assert started["canary_prep"]["allowlisted_pairs"] == ["EURUSD"]

    svc = RuntimeService(database_url=db_url)
    active = svc.get_active_model_set("EURUSD")
    assert active is not None
    active_meta = dict(active.get("metadata_json") or {})
    assert str(active_meta.get("release_status") or "") == "canary_active"
    assert list(dict(active_meta.get("main_runtime_rollout") or {}).get("allowlisted_pairs") or []) == ["EURUSD"]

    graduated = close_canary(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        outcome="graduate",
    )
    assert bool(graduated.get("ok")) is True
    assert graduated["release_status"] == "graduated"
    resolved_champion = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="champion")
    assert resolved_champion.bundle_run_id == shadow_bundle.bundle_run_id

    rolled_back = rollback_release(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        reason="test_rollback",
    )
    assert bool(rolled_back.get("ok")) is True
    assert rolled_back["release_status"] == "rolled_back"
    restored = resolve_bundle_manifest_by_alias(pair="EURUSD", alias="champion")
    assert restored.bundle_run_id == champion_bundle.bundle_run_id

    status = release_status(pair="EURUSD", database_url=db_url)
    assert bool(status.get("ok")) is True
    assert status["release_status"] == "rolled_back"
    assert status["shadow_acceptance_summary"]["release_status"] == "rolled_back"
    assert status["canary_prep"]["status"] == "rolled_back"


def test_canary_start_blocks_when_release_is_not_shadow_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release_root = tmp_path / "releases"
    package_dir = release_root / "eurusd" / "bundle-canary-blocked"
    package_dir.mkdir(parents=True, exist_ok=True)
    package_dir.joinpath("activation_package.json").write_text(
        json.dumps(
            {
                "schema_version": "phase5_activation_package_v1",
                "bundle_run_id": "bundle-canary-blocked",
                "pair": "EURUSD",
                "target_alias": "shadow",
                "model_alias": "shadow",
                "release_status": "staged",
                "promotion_status": "eligible",
                "runtime_compatible": True,
                "canary_plan": {
                    "plan_id": "eurusd-bundle-canary-blocked-canary",
                    "scope": "pair_allowlist",
                    "status": "planned",
                    "traffic_fraction": 1.0,
                    "duration_minutes": 60,
                    "metrics_window_minutes": 60,
                    "success_criteria": {
                        "latency_budget_ms": 5000.0,
                        "stale_feature_limit": 1,
                        "drawdown_limit_pct": 5.0,
                        "calibration_drift_limit": 0.05,
                    },
                    "abort_conditions": [
                        "latency_breach",
                        "stale_features",
                        "rollout_breach",
                        "drawdown_breach",
                        "calibration_drift",
                    ],
                    "metadata": {"allowlisted_pairs": ["EURUSD"], "budget_scale": 0.25},
                },
                "promotion_gates": [],
                "evidence_refs": {},
                "metadata": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("FXSTACK_PHASE5_RELEASE_ROOT", str(release_root))
    get_settings.cache_clear()
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    from fxstack.training import release_workflow

    called = {"activate": False}

    def _unexpected_activate(*args, **kwargs):
        called["activate"] = True
        raise AssertionError("activate_mlflow_alias should not be called when canary start is blocked")

    monkeypatch.setattr(release_workflow, "activate_mlflow_alias", _unexpected_activate)

    try:
        blocked = release_workflow.canary_start(
            pair="EURUSD",
            database_url=db_url,
            manifest_path=tmp_path / "active_models.json",
            bundle_run_id="bundle-canary-blocked",
        )
        status = release_workflow.release_status(
            pair="EURUSD",
            database_url=db_url,
            bundle_run_id="bundle-canary-blocked",
        )
    finally:
        get_settings.cache_clear()

    assert called["activate"] is False
    assert bool(blocked["ok"]) is False
    assert blocked["error"] == "canary_start_blocked"
    assert "release_status:staged" in blocked["blockers"]
    assert "canary_plan_status:planned" in blocked["blockers"]
    assert any(str(item).startswith("missing_phase5_gates:") for item in blocked["blockers"])
    assert blocked["shadow_acceptance_summary"]["ready"] is False
    assert blocked["canary_prep"]["status"] == "planned"
    assert status["canary_ready"] is False
    assert "release_status:staged" in status["canary_blockers"]


def test_canary_monitor_surfaces_pair_readiness_blockers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _configure_mlflow_env(tmp_path, monkeypatch)
    monkeypatch.setenv("FXSTACK_PHASE5_AUTO_ROLLBACK", "0")
    get_settings.cache_clear()
    from fxstack.mlops.registry import import_compat_bundle_to_mlflow
    from fxstack.training.release_workflow import canary_start, monitor_canary, release_status, shadow_accept, stage_release

    import_compat_bundle_to_mlflow(_compat_payload(tmp_path, pair="EURUSD", run_id="bundle-pair-readiness-champion"), intended_alias="champion")
    shadow_payload = _compat_payload(tmp_path, pair="EURUSD", run_id="bundle-pair-readiness-shadow")
    phase5_dir = tmp_path / "phase5_pair_readiness"
    phase5_dir.mkdir(parents=True, exist_ok=True)
    phase5_bundle_path = phase5_dir / "phase5_gate_bundle.json"
    phase5_bundle_path.write_text(
        json.dumps(
            {
                "research_gate": {"gate": "research_gate", "status": "pass", "passed": True},
                "economic_gate": {"gate": "economic_gate", "status": "pass", "passed": True},
                "operational_gate": {"gate": "operational_gate", "status": "pass", "passed": True},
                "shadow_gate": {"gate": "shadow_gate", "status": "pass", "passed": True},
                "canary_gate": {"gate": "canary_gate", "status": "pass", "passed": True},
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    shadow_payload["phase5_gates"] = {
        "phase5_gate_bundle": str(phase5_bundle_path),
        "research_gate": str(phase5_dir / "research_gate.json"),
        "economic_gate": str(phase5_dir / "economic_gate.json"),
        "operational_gate": str(phase5_dir / "operational_gate.json"),
        "shadow_gate": str(phase5_dir / "shadow_gate.json"),
        "canary_gate": str(phase5_dir / "canary_gate.json"),
    }
    import_compat_bundle_to_mlflow(shadow_payload, intended_alias="shadow")

    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    manifest_path = tmp_path / "active_models.json"

    stage_release(pair="EURUSD", alias="shadow", author="ops", allowlisted_pairs=["EURUSD"])
    shadow_accept(pair="EURUSD")
    started = canary_start(pair="EURUSD", database_url=db_url, manifest_path=manifest_path)
    assert bool(started.get("ok")) is True

    svc = RuntimeService(database_url=db_url)
    svc.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": 1775433600.0,
            "symbol_readiness": {"EURUSD": {"supported": True, "broker_symbol": "EURUSD"}},
            "runtime_diag": {
                "loop_latency_ms": 25.0,
                "strategy_engine_mode": "rl_primary",
                "supervised_fallback": {"enabled": True, "fallback_count": 1, "fallback_reasons": ["signal_fallback"], "primary_reason": "signal_fallback"},
                "challenger_conflict": {
                    "mode": "hard_gate",
                    "active": True,
                    "max_gap": 0.44,
                    "active_pairs": ["EURUSD"],
                    "verdict_counts": {"hard_conflict": 1},
                    "dominant_verdict": "hard_conflict",
                },
                "feature_serving": {
                    "source": "feast_online",
                    "stale": True,
                    "reason": "stale",
                },
                "feature_serving_by_pair": {
                    "EURUSD:M5": {
                        "source": "feast_online",
                        "stale": True,
                        "reason": "stale",
                    }
                },
                "startup_inference": {
                    "EURUSD": {
                        "ok": False,
                        "reason": "model_load_timeout",
                    }
                },
                "pair_readiness": {
                    "EURUSD": {
                        "pair": "EURUSD",
                        "ready": False,
                        "status": "blocked",
                        "reason": "startup_inference:model_load_timeout",
                        "blockers": ["startup_inference:model_load_timeout", "feature_serving:stale"],
                        "startup_inference_ok": False,
                        "feature_serving_source": "feast_online",
                        "feature_serving_stale": True,
                        "symbol_supported": True,
                    }
                },
                "entry_execution_policy": {
                    "execution_mode": "rl_primary",
                    "strategy_engine_mode": "rl_primary",
                    "rl_checkpoint_loaded": True,
                    "rl_checkpoint_path": "mlruns/eurusd/rl.chkpt",
                    "rl_proposal_source": "rl_checkpoint",
                    "rl_routed_entry_count": 4,
                    "rl_blocked_entry_count": 1,
                    "rl_fallback_entry_count": 2,
                    "rl_scaled_entry_count": 1,
                    "rl_lifecycle_reviewed_count": 6,
                    "rl_lifecycle_applied_count": 3,
                    "rl_lifecycle_exit_count": 1,
                    "rl_lifecycle_resize_count": 1,
                    "rl_lifecycle_tighten_stop_count": 1,
                    "rl_lifecycle_preserved_exit_count": 1,
                    "rl_lifecycle_fallback_count": 1,
                    "rl_lifecycle_pairs": ["EURUSD"],
                },
                "rl_portfolio_proposal": {
                    "ts": "2026-04-08T00:00:00Z",
                    "pair_universe": ["EURUSD"],
                    "source": "rl_checkpoint",
                    "supervised_fallback_used": False,
                    "fallback_reason": "",
                    "checkpoint_path": "mlruns/eurusd/rl.chkpt",
                    "checkpoint_loaded": True,
                    "checkpoint_summary": {"feature_count": 8, "schema_version": "rl_linear_checkpoint_v1"},
                    "proposals_by_pair": {
                        "EURUSD": {
                            "source": "rl_checkpoint",
                            "supervised_fallback_used": False,
                            "action": {"target_position": 0.5, "close_position": False, "tighten_stop": False},
                        }
                    },
                    "diagnostics": {
                        "decision_count": 1,
                        "candidate_count": 1,
                        "checkpoint_summary": {"feature_count": 8, "schema_version": "rl_linear_checkpoint_v1"},
                        "artifact_discovery": {
                            "checkpoint_loaded": True,
                            "checkpoint_path": "mlruns/eurusd/rl.chkpt",
                            "fallback_reason": "",
                        },
                    },
                },
                "risk_cycle_summary": {"rollout": {"breach_count": 0}},
            },
        }
    )

    monitor = monitor_canary(
        pair="EURUSD",
        database_url=db_url,
        manifest_path=manifest_path,
        bundle_run_id=str(started["bundle_run_id"]),
    )
    assert monitor["status"] == "breach"
    assert monitor["pair_readiness"]["status"] == "blocked"
    assert any(str(item).startswith("pair_readiness:") for item in monitor["breaches"])
    assert monitor["strategy_state"]["strategy_engine_mode"] == "rl_primary"
    assert monitor["strategy_state"]["supervised_fallback"]["enabled"] is True
    assert monitor["strategy_state"]["challenger_conflict"]["mode"] == "hard_gate"
    assert monitor["runtime_rl_state"]["checkpoint_loaded"] is True
    assert monitor["runtime_rl_state"]["proposal_source"] == "rl_checkpoint"
    assert monitor["runtime_rl_state"]["routed_entry_count"] == 4
    assert monitor["runtime_rl_state"]["fallback_entry_count"] == 2
    assert monitor["runtime_rl_state"]["pair_universe"] == ["EURUSD"]
    assert monitor["runtime_rl_state"]["lifecycle_summary"]["applied_count"] == 3
    assert monitor["runtime_rl_state"]["artifact_readiness"]["ready"] is True

    status = release_status(pair="EURUSD", database_url=db_url, bundle_run_id=str(started["bundle_run_id"]))
    assert status["canary_ready"] is False
    assert status["runtime_pair_readiness"]["status"] == "blocked"
    assert any(str(item).startswith("runtime_pair_readiness:") for item in status["canary_blockers"])
    assert status["strategy_state"]["strategy_engine_mode"] == "rl_primary"
    assert status["runtime_rl_state"]["checkpoint_loaded"] is True
    assert status["runtime_rl_state"]["proposal_source"] == "rl_checkpoint"
    assert status["runtime_rl_state"]["routed_entry_count"] == 4
    assert status["runtime_rl_state"]["fallback_entry_count"] == 2
    assert status["runtime_rl_state"]["rebalance_summary"]["exit_count"] == 1
    assert status["runtime_rl_state"]["flip_intent"]["non_flat_target_count"] == 1
