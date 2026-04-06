from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient


def _fresh_client(tmp_path: Path) -> TestClient:
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    if "fxstack.api.app" in sys.modules:
        del sys.modules["fxstack.api.app"]
    from fxstack.api.app import app

    return TestClient(app)


def _make_artifact(root: Path, name: str) -> str:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({"name": name}, indent=2), encoding="utf-8")
    return str(path)


def _write_registry(
    *,
    path: Path,
    pair: str,
    run_id: str,
    artifacts_root: Path,
    promotion_status: str,
    trained_at: float,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "pair": pair,
        "trained_at": trained_at,
        "promotion_status": promotion_status,
        "artifacts": {
            "regime": {"path": _make_artifact(artifacts_root, f"{run_id}_regime_hmm")},
            "meta": {"path": _make_artifact(artifacts_root, f"{run_id}_meta_filter")},
            "swing_xgb": {"path": _make_artifact(artifacts_root, f"{run_id}_swing_xgb")},
            "intraday_xgb": {"path": _make_artifact(artifacts_root, f"{run_id}_intraday_xgb")},
            "exit_policy": {"path": _make_artifact(artifacts_root, f"{run_id}_exit_policy")},
            "reversal_failure": {"path": _make_artifact(artifacts_root, f"{run_id}_reversal_failure")},
            "reversal_opportunity": {"path": _make_artifact(artifacts_root, f"{run_id}_reversal_opportunity")},
        },
        "policies": {
            "swing": "xgb_only",
            "intraday": "xgb_only",
        },
        "feature_schema": {
            "intraday_contract": "hierarchical_v1",
            "swing_policy": "xgb_only",
            "intraday_policy": "xgb_only",
            "tier": "tier2",
        },
        "training_eval_reports": {
            "meta": str(path.parent / f"{run_id}_meta_filter" / "reports" / "training_report.json"),
            "reversal_failure": str(path.parent / f"{run_id}_reversal_failure" / "reports" / "training_report.json"),
            "reversal_opportunity": str(path.parent / f"{run_id}_reversal_opportunity" / "reports" / "training_report.json"),
            "exit": str(path.parent / f"{run_id}_exit_policy" / "reports" / "training_report.json"),
        },
        "capabilities": {
            "has_exit_model": True,
            "has_reversal_models": True,
            "lifecycle_complete": True,
        },
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def test_v2_health_state_commands_roundtrip(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    r = client.get("/v2/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"

    r = client.get("/v2/ready")
    assert r.status_code == 200
    ready = r.json()
    assert bool(ready.get("bridge_up")) is True
    assert "database_ok" in ready
    assert "runtime_ready" in ready
    assert "status_tier" in ready

    service.patch_state({"runtime_status": "running", "runtime_last_cycle_ts": time.time()})
    r = client.get("/v2/ready")
    assert r.status_code == 200
    ready = r.json()
    assert ready.get("runtime_status") == "running"
    assert isinstance(ready.get("runtime_ready"), bool)

    r = client.post("/v2/commands", json={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "command_id": "x1"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"queued", "duplicate"}

    r = client.get("/v2/commands/poll")
    assert r.status_code == 200
    assert r.json().get("status") in {"ok", "empty"}

    r = client.post("/v2/commands/ack", json={"command_id": "x1", "status": "acked"})
    assert r.status_code in {200, 409}

    r = client.get("/v2/state")
    assert r.status_code == 200
    assert isinstance(r.json(), dict)

    r = client.get("/v2/metrics")
    assert r.status_code == 200
    assert "pending" in r.json()

    r = client.get("/v2/ops/events")
    assert r.status_code == 200
    assert "events" in r.json()

    r = client.get("/v2/ops/workflows/status")
    assert r.status_code == 200
    assert "workflows" in r.json()


def test_v2_ops_events_surface_latest_shadow_training_update(tmp_path: Path, monkeypatch):
    client = _fresh_client(tmp_path)
    from fxstack.api import app as app_module

    original_resolve_repo_path = app_module._resolve_repo_path

    def _resolve_repo_path(raw: str):
        if str(raw).replace("\\", "/") == "fx-quant-stack/artifacts_shadow":
            return (tmp_path / "fx-quant-stack" / "artifacts_shadow").resolve()
        return original_resolve_repo_path(raw)

    monkeypatch.setattr(app_module, "_resolve_repo_path", _resolve_repo_path)

    shadow_report = (
        tmp_path
        / "fx-quant-stack"
        / "artifacts_shadow"
        / "full_20260405_0100_manual"
        / "eurusd"
        / "reports"
        / "training_report.json"
    )
    shadow_report.parent.mkdir(parents=True, exist_ok=True)
    shadow_report.write_text("{}", encoding="utf-8")
    ts = time.time() - 5.0
    os.utime(shadow_report, (ts, ts))

    r = client.get("/v2/ops/events")
    assert r.status_code == 200
    body = r.json()
    shadow_event = next((item for item in body.get("events", []) if item.get("event_type") == "training_shadow_update"), None)
    assert shadow_event is not None
    assert shadow_event["status"] == "running"
    assert shadow_event["payload"]["pair"] == "EURUSD"
    assert shadow_event["payload"]["run_name"] == "full_20260405_0100_manual"


def test_v2_ops_workflows_status_prefers_newest_shadow_registry(tmp_path: Path, monkeypatch):
    client = _fresh_client(tmp_path)
    from fxstack.api import app as app_module
    from fxstack.api.app import service

    original_resolve_repo_path = app_module._resolve_repo_path

    def _resolve_repo_path(raw: str):
        if str(raw).replace("\\", "/") == "fx-quant-stack/artifacts_shadow":
            return (tmp_path / "fx-quant-stack" / "artifacts_shadow").resolve()
        return original_resolve_repo_path(raw)

    monkeypatch.setattr(app_module, "_resolve_repo_path", _resolve_repo_path)
    app_module._workflow_status_cache = None

    pair = "AUDJPY"
    old_registry = _write_registry(
        path=tmp_path / "legacy_registry" / "audjpy_legacy.json",
        pair=pair,
        run_id="legacy-run",
        artifacts_root=tmp_path / "legacy_artifacts",
        promotion_status="research_only",
        trained_at=1775330000.0,
    )
    old_ts = time.time() - 3600.0
    os.utime(old_registry, (old_ts, old_ts))

    active_artifacts = {
        "regime": str(tmp_path / "legacy_artifacts" / "legacy-run_regime_hmm"),
        "meta": str(tmp_path / "legacy_artifacts" / "legacy-run_meta_filter"),
        "swing_xgb": str(tmp_path / "legacy_artifacts" / "legacy-run_swing_xgb"),
        "intraday_xgb": str(tmp_path / "legacy_artifacts" / "legacy-run_intraday_xgb"),
        "exit_policy": str(tmp_path / "legacy_artifacts" / "legacy-run_exit_policy"),
        "reversal_failure": str(tmp_path / "legacy_artifacts" / "legacy-run_reversal_failure"),
        "reversal_opportunity": str(tmp_path / "legacy_artifacts" / "legacy-run_reversal_opportunity"),
    }
    service.upsert_active_model_set(
        pair=pair,
        model_set_id="legacy-run",
        registry_path=str(old_registry),
        artifacts=active_artifacts,
        metadata={"promotion_status": "research_only", "capabilities": {"has_exit_model": True, "has_reversal_models": True, "lifecycle_complete": True}},
        enabled=True,
    )

    shadow_registry = _write_registry(
        path=tmp_path
        / "fx-quant-stack"
        / "artifacts_shadow"
        / "registry_full_20260405_1200_manual"
        / "audjpy_shadow.json",
        pair=pair,
        run_id="shadow-run",
        artifacts_root=tmp_path / "fx-quant-stack" / "artifacts_shadow" / "full_20260405_1200_manual" / "audjpy",
        promotion_status="eligible",
        trained_at=1775440000.0,
    )
    new_ts = time.time()
    os.utime(shadow_registry, (new_ts, new_ts))

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "audjpy-training-eval")
    assert workflow["status"] == "eligible"
    assert workflow["details_json"]["registry_meta"]["run_id"] == "shadow-run"
    assert str(workflow["details_json"]["lifecycle_capabilities"]["registry_path"]).endswith("audjpy_shadow.json")
    assert workflow["details_json"]["registry_meta"]["promotion_status"] == "eligible"


def test_v2_ops_workflows_status_includes_shadow_only_pairs(tmp_path: Path, monkeypatch):
    client = _fresh_client(tmp_path)
    from fxstack.api import app as app_module

    original_resolve_repo_path = app_module._resolve_repo_path

    def _resolve_repo_path(raw: str):
        if str(raw).replace("\\", "/") == "fx-quant-stack/artifacts_shadow":
            return (tmp_path / "fx-quant-stack" / "artifacts_shadow").resolve()
        return original_resolve_repo_path(raw)

    monkeypatch.setattr(app_module, "_resolve_repo_path", _resolve_repo_path)
    app_module._workflow_status_cache = None

    _write_registry(
        path=tmp_path
        / "fx-quant-stack"
        / "artifacts_shadow"
        / "registry_full_20260405_1200_manual"
        / "gbpusd_shadow.json",
        pair="GBPUSD",
        run_id="shadow-only-run",
        artifacts_root=tmp_path / "fx-quant-stack" / "artifacts_shadow" / "full_20260405_1200_manual" / "gbpusd",
        promotion_status="eligible",
        trained_at=1775443600.0,
    )

    body = client.get("/v2/ops/workflows/status").json()
    workflow = next(item for item in body["workflows"] if item["workflow_id"] == "gbpusd-training-eval")
    assert workflow["status"] == "eligible"
    assert workflow["details_json"]["registry_meta"]["run_id"] == "shadow-only-run"
    assert workflow["details_json"]["lifecycle_capabilities"]["has_exit_model"] is True
    assert workflow["details_json"]["registry_source"] == "shadow"


def test_v2_ready_surfaces_runtime_startup_progress_and_failure_states(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service
    from fxstack.settings import get_settings

    now = time.time()
    stale_secs = float(get_settings().runtime_startup_progress_stale_secs)
    service.patch_state(
        {
            "runtime_status": "starting",
            "runtime_last_cycle_ts": 0.0,
            "runtime_startup": {
                "boot_id": "boot-1",
                "booted_at": "2026-03-24T07:00:00+00:00",
                "runtime_pid": 123,
                "phase": "model_load",
                "phase_pair": "",
                "phase_index": 0,
                "phase_total": 18,
                "last_progress_ts": now,
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "starting"
    assert ready["runtime_phase"] == "model_load"
    assert ready["runtime_boot_id"] == "boot-1"
    assert ready["reason"] == "runtime_starting"

    service.patch_state(
        {
            "runtime_status": "starting",
            "runtime_last_cycle_ts": 0.0,
            "agent_decisions": [{"symbol": "EURUSD"}],
            "agent_diagnostics": {"foo": "bar"},
            "runtime_startup": {
                "boot_id": "boot-2",
                "booted_at": "2026-03-24T07:05:00+00:00",
                "runtime_pid": 456,
                "phase": "initial_refresh",
                "phase_pair": "GBPJPY",
                "phase_index": 10,
                "phase_total": 18,
                "last_progress_ts": now - (stale_secs + 15.0),
                "failure_reason": "",
                "failed_at": "",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    state = client.get("/v2/state").json()
    assert state["runtime_status"] == "stalled"
    assert state["runtime_phase"] == "initial_refresh"
    assert state["runtime_phase_pair"] == "GBPJPY"
    assert state["agent_decisions"] == []
    assert state["agent_diagnostics"] == {}

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "stalled"
    assert ready["status_tier"] == "bridge_up_runtime_stalled"
    assert ready["reason"] == "runtime_startup_stalled"
    assert ready["runtime_phase"] == "initial_refresh"
    assert ready["runtime_phase_pair"] == "GBPJPY"
    assert float(ready["runtime_last_progress_age_secs"]) >= stale_secs

    service.patch_state(
        {
            "runtime_status": "failed",
            "runtime_last_cycle_ts": 0.0,
            "runtime_startup": {
                "boot_id": "boot-3",
                "booted_at": "2026-03-24T07:10:00+00:00",
                "runtime_pid": 789,
                "phase": "model_load",
                "phase_pair": "EURUSD",
                "phase_index": 1,
                "phase_total": 18,
                "last_progress_ts": now,
                "failure_reason": "RuntimeError:boom",
                "failed_at": "2026-03-24T07:10:05+00:00",
                "pending_command_policy": "purge_and_mark_stale",
            },
        }
    )

    ready = client.get("/v2/ready").json()
    assert ready["runtime_status"] == "failed"
    assert ready["status_tier"] == "bridge_up_runtime_failed"
    assert ready["reason"] == "runtime_startup_failed"
    assert ready["runtime_failure_reason"] == "RuntimeError:boom"

    service.record_runtime_boot_failure(
        boot={
            "boot_id": "boot-3",
            "booted_at": "2026-03-24T07:10:00+00:00",
            "runtime_pid": 789,
            "phase": "model_load",
            "phase_pair": "EURUSD",
            "phase_index": 1,
            "phase_total": 18,
            "last_progress_ts": now,
            "failure_reason": "",
            "failed_at": "",
            "pending_command_policy": "purge_and_mark_stale",
        },
        failure_reason="RuntimeError:boom",
        failed_at="2026-03-24T07:10:05+00:00",
    )
    governance = client.get("/v2/governance/events").json()
    assert len(governance["events"]) >= 1
    assert governance["events"][0]["event_type"] == "runtime_startup_failed"


def test_v2_decision_snapshots_exposes_persisted_history(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.store_decisions(
        decisions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "score": 4.2,
                "confidence": 77.0,
                "execution_ready": False,
                "reasons": ["shadow_meta_reject"],
                "metadata": {
                    "structure_timing_score": 0.81,
                    "structure_rescue_active": False,
                    "shadow_rejection_reason": "shadow_meta_reject",
                    "adaptive_environment_state": "CorrectiveTrend",
                    "adaptive_playbook": "trend_pullback",
                    "adaptive_sleeve": "trend_pullback",
                    "adaptive_entry_quality": 0.67,
                    "adaptive_shadow_would_trade": True,
                    "allocator_score": 0.71,
                    "allocator_rank": 1,
                    "allocator_selected": True,
                },
            }
        ],
        vol=0.12,
        diagnostics={
            "runtime": "fxstack",
            "shadow_policy": {"candidate_count": 1},
            "adaptive_shadow_policy": {"candidate_count": 1, "would_trade_count": 1},
            "allocator_policy": {"candidate_count": 1, "selected_count": 1},
        },
    )

    body = client.get("/v2/decision-snapshots?limit=5").json()
    assert "items" in body
    assert len(body["items"]) >= 1
    latest = body["items"][0]
    assert latest["vol"] == 0.12
    assert latest["decisions_json"][0]["symbol"] == "EURUSD"
    assert latest["decisions_json"][0]["metadata"]["structure_timing_score"] == 0.81
    assert latest["decisions_json"][0]["metadata"]["adaptive_playbook"] == "trend_pullback"
    assert latest["diagnostics_json"]["adaptive_shadow_policy"]["candidate_count"] == 1
    assert latest["diagnostics_json"]["allocator_policy"]["candidate_count"] == 1


def test_v2_state_preserves_directional_belief_fields(tmp_path: Path):
    client = _fresh_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": time.time(),
            "runtime_diag": {
                "directional_belief_policy": {
                    "enabled": True,
                    "runtime_required": False,
                    "short_horizon_bars": 3,
                    "trade_horizon_bars": 12,
                    "structural_horizon_bars": 48,
                },
                "directional_belief_cycle_summary": {
                    "candidate_count_with_belief": 2,
                    "avg_belief_gap": 0.14,
                    "avg_fragility_score": 0.22,
                    "avg_primary_rank_score": 0.31,
                    "avg_primary_ev_above_hurdle_prob": 0.62,
                    "avg_primary_expected_net_ev_bps": 4.8,
                    "avg_primary_fail_fast_prob": 0.21,
                    "no_edge_share": 0.1,
                    "primary_scenario_counts": {"trend_pullback": 2},
                    "opposition_scenario_counts": {"failed_breakout_reversal": 1},
                    "opposition_side_counts": {"short": 2},
                    "artifact_versions": {"EURUSD": "belief_v1"},
                },
                "directional_belief_metrics": {
                    "decision_count": 2,
                    "belief_loaded_share": 1.0,
                    "avg_belief_gap": 0.14,
                    "avg_fragility_score": 0.22,
                    "avg_primary_rank_score": 0.31,
                    "avg_primary_ev_above_hurdle_prob": 0.62,
                    "avg_primary_expected_net_ev_bps": 4.8,
                    "avg_primary_fail_fast_prob": 0.21,
                    "no_edge_share": 0.1,
                    "primary_scenario_counts": {"trend_pullback": 2},
                    "opposition_scenario_counts": {"failed_breakout_reversal": 1},
                    "opposition_side_counts": {"short": 2},
                },
            },
            "agent_decisions": [
                {
                    "symbol": "EURUSD",
                    "side": "BUY",
                    "score": 5.1,
                    "execution_ready": False,
                    "reasons": ["shadow_meta_reject"],
                    "metadata": {
                        "pair": "EURUSD",
                        "ts": "2026-03-26T12:00:00Z",
                        "belief_primary_side": "long",
                        "belief_primary_scenario": "trend_pullback",
                        "belief_primary_thesis": "trend_pullback:long",
                        "belief_primary_score": 0.44,
                        "belief_primary_rank_score": 0.61,
                        "belief_primary_ev_above_hurdle_prob": 0.73,
                        "belief_primary_expected_net_ev_bps": 8.4,
                        "belief_primary_confirm_prob": 0.66,
                        "belief_primary_fail_fast_prob": 0.14,
                        "belief_no_edge": False,
                        "belief_opposing_side": "short",
                        "belief_opposing_scenario": "failed_breakout_reversal",
                        "belief_opposing_thesis": "failed_breakout_reversal:short",
                        "belief_opposing_score": 0.18,
                        "belief_gap": 0.26,
                        "belief_fragility_score": 0.21,
                        "belief_horizon_alignment_score": 0.88,
                        "belief_short_up_prob": 0.58,
                        "belief_trade_up_prob": 0.69,
                        "belief_structural_up_prob": 0.74,
                        "belief_regime_fit_score": 1.0,
                        "belief_expected_confirmation_window_bars": 3,
                        "belief_expected_path_shape": "pullback_then_resume",
                        "belief_invalidation_reason": "trigger_score_lt_0.35_or_trade_prob_lt_0.50",
                        "belief_model_version": "belief_v1",
                        "belief_source_mode": "artifact",
                    },
                }
            ],
        }
    )

    state = client.get("/v2/state").json()
    assert state["runtime_diag"]["directional_belief_policy"]["enabled"] is True
    assert state["runtime_diag"]["directional_belief_cycle_summary"]["candidate_count_with_belief"] == 2
    assert state["runtime_diag"]["directional_belief_cycle_summary"]["avg_primary_rank_score"] == 0.31
    assert state["runtime_diag"]["directional_belief_metrics"]["no_edge_share"] == 0.1
    assert state["agent_decisions"][0]["metadata"]["belief_primary_scenario"] == "trend_pullback"
    assert state["agent_decisions"][0]["metadata"]["belief_primary_ev_above_hurdle_prob"] == 0.73
    assert state["agent_decisions"][0]["metadata"]["belief_no_edge"] is False
    assert state["agent_decisions"][0]["metadata"]["belief_source_mode"] == "artifact"
