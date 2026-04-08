from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools import finalize_build
from tools import full_process_audit
from tools import dukascopy_coverage_gate
from tools import live_stack_check


def test_full_process_audit_bootstrap_writes_expected_artifacts(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "fx-quant-stack" / "scripts").mkdir(parents=True)
    (repo / "run_bridge.bat").write_text(
        "if not defined TRADER_BRIDGE_IMPL set TRADER_BRIDGE_IMPL=fxstack\n", encoding="utf-8"
    )
    (repo / "run_agent.bat").write_text(
        "if not defined TRADER_RUNTIME_IMPL set TRADER_RUNTIME_IMPL=fxstack\n", encoding="utf-8"
    )
    (repo / "start.bat").write_text("if not defined FXSTACK_START_PROFILE set FXSTACK_START_PROFILE=staged_safe\n", encoding="utf-8")

    monkeypatch.setattr(full_process_audit, "_repo_root", lambda: repo)
    monkeypatch.setattr(
        full_process_audit,
        "_collect_metadata",
        lambda _root: {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "git": {"sha": "deadbeef", "ok": True},
            "versions": {"python": "3.11.0", "node": "v22", "pnpm": "10", "uv": "0.10"},
            "env": {},
            "launcher_defaults": {},
        },
    )

    def _fake_run_command(**kwargs):
        log_file = Path(kwargs["logs_dir"]) / f"{kwargs['name']}.log"
        log_file.write_text("ok\n", encoding="utf-8")
        return full_process_audit.CommandResult(
            name=str(kwargs["name"]),
            command="echo ok",
            cwd=str(kwargs["cwd"]),
            return_code=0,
            passed=True,
            duration_secs=0.01,
            log_file=str(log_file),
        )

    monkeypatch.setattr(full_process_audit, "_run_command", _fake_run_command)

    args = argparse.Namespace(
        evidence_root=str(repo / "docs" / "audit"),
        runtime_db=str(repo / "data" / "state" / "runtime_v2.db"),
        audit_dir=str(repo / "data" / "state" / "audit"),
        baseline_url="http://127.0.0.1:58710",
        candidate_url="http://127.0.0.1:58711",
        profile="balanced",
        skip_static_checks=True,
        skip_frontend=True,
        strict=False,
    )
    rc = full_process_audit.run(args)
    assert rc == 0

    evidence_dirs = sorted((repo / "docs" / "audit").glob("*_full_process"))
    assert evidence_dirs
    evidence = evidence_dirs[-1]
    for rel in (
        "metadata.json",
        "master_report.md",
        "blockers.json",
        "gate_summary.json",
        "go_no_go.json",
        "cutover_checklist.md",
        "rollback_runbook.md",
    ):
        assert (evidence / rel).exists(), rel


def test_finalize_build_sets_go_when_gates_pass_and_no_high_critical(tmp_path: Path):
    evidence = tmp_path / "docs" / "audit" / "20260317_full_process"
    evidence.mkdir(parents=True)

    blockers = {
        "schema_version": 1,
        "generated_at": "2026-03-17T00:00:00+00:00",
        "blockers": [
            {"id": "B-1", "severity": "medium", "status": "open"},
        ],
    }
    (evidence / "blockers.json").write_text(json.dumps(blockers), encoding="utf-8")
    (evidence / "gate_summary.json").write_text(json.dumps({"schema_version": 1}), encoding="utf-8")

    fast_gate = {"passed": True, "checks": {"contract_health": True}, "metrics": {"throughput_ratio": 1.1}}
    shadow = {"gates": {"passed": True, "checks": {"throughput": True}, "throughput_delta_entries_acked": 2}}
    fast_path = tmp_path / "fast.json"
    shadow_path = tmp_path / "shadow.json"
    fast_path.write_text(json.dumps(fast_gate), encoding="utf-8")
    shadow_path.write_text(json.dumps(shadow), encoding="utf-8")

    args = argparse.Namespace(
        evidence_dir=str(evidence),
        evidence_root=str(tmp_path / "docs" / "audit"),
        fast_gate_artifact=str(fast_path),
        shadow_artifact=str(shadow_path),
        rollback_validated=True,
    )
    rc = finalize_build.run(args)
    assert rc == 0
    go_no_go = json.loads((evidence / "go_no_go.json").read_text(encoding="utf-8"))
    assert go_no_go["decision"] == "GO"


def _write_ohlc_csv(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["time,open,high,low,close\n"]
    for i in range(rows):
        lines.append(f"2026-01-01 00:{i%60:02d}:00,1.0,1.1,0.9,1.0\n")
    path.write_text("".join(lines), encoding="utf-8")


def test_dukascopy_coverage_gate_passes_when_all_files_meet_thresholds(tmp_path: Path):
    root = tmp_path / "dukascopy"
    _write_ohlc_csv(root / "EURUSD_M1.csv", rows=10)
    _write_ohlc_csv(root / "EURUSD_M5.csv", rows=8)
    _write_ohlc_csv(root / "USDJPY_M1.csv", rows=11)
    _write_ohlc_csv(root / "USDJPY_M5.csv", rows=9)

    out = tmp_path / "gate.json"
    args = argparse.Namespace(
        source_root=str(root),
        pairs="EURUSD,USDJPY",
        timeframes="M1,M5",
        file_pattern="{pair}_{granularity}.csv",
        min_rows_m1=10,
        min_rows_m5=8,
        min_rows_m15=1,
        min_rows_h4=1,
        min_rows_d=1,
        out=str(out),
    )
    rc = dukascopy_coverage_gate.run(args)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["summary"]["passed"]) is True
    assert int(payload["summary"]["missing_count"]) == 0
    assert int(payload["summary"]["insufficient_count"]) == 0


def test_dukascopy_coverage_gate_fails_on_missing_and_insufficient_files(tmp_path: Path):
    root = tmp_path / "dukascopy"
    _write_ohlc_csv(root / "EURUSD_M1.csv", rows=5)
    # EURUSD_M5 missing intentionally.
    _write_ohlc_csv(root / "USDJPY_M1.csv", rows=10)
    _write_ohlc_csv(root / "USDJPY_M5.csv", rows=2)

    args = argparse.Namespace(
        source_root=str(root),
        pairs="EURUSD,USDJPY",
        timeframes="M1,M5",
        file_pattern="{pair}_{granularity}.csv",
        min_rows_m1=10,
        min_rows_m5=8,
        min_rows_m15=1,
        min_rows_h4=1,
        min_rows_d=1,
        out="",
    )
    rc = dukascopy_coverage_gate.run(args)
    assert rc == 2


def test_live_stack_check_passes_with_heartbeat_ticks_and_acked_command(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])
    event_rows = iter(
        [
            {"events": [{"status": "queued"}, {"status": "delivered"}]},
            {"events": [{"status": "queued"}, {"status": "delivered"}, {"status": "acked"}]},
        ]
    )

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "ok",
                "runtime_status": "running",
                "runtime_phase": "main_loop",
                "runtime_last_progress_age_secs": 1.0,
                "runtime_failure_reason": "",
                "mt4_fresh": True,
                "ticks_fresh": True,
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "ok"},
                    "execution_provider": {"status": "ok"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT eq=10000.00"}]}
        if path == "/v2/market/ticks":
            return {"EURUSD": {"bid": 1.1, "ask": 1.1002}}
        if path.startswith("/v2/commands/events"):
            try:
                return next(event_rows)
            except StopIteration:
                return {"events": [{"status": "acked"}]}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(
        live_stack_check,
        "_fetch_dashboard_state",
        lambda *a, **k: {
            "checked": True,
            "status_code": 200,
            "ok": True,
            "payload": {"systemStatus": "connected"},
            "text": "{\"systemStatus\":\"connected\"}",
            "url": "http://127.0.0.1:3000/api/trading/state",
        },
    )
    monkeypatch.setattr(live_stack_check, "_post_json", lambda *a, **k: {"status": "queued"})
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="http://127.0.0.1:3000",
        timeout_secs=5.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        require_ticks=True,
        require_acked_command=True,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out=str(out),
    )
    rc = live_stack_check.run(args)
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["passed"]) is True
    assert bool(payload["checks"]["dashboard_state_ok"]) is True
    assert bool(payload["checks"]["runtime_startup_ok"]) is True
    assert bool(payload["checks"]["command_acked"]) is True
    assert bool(payload["checks"]["ticks_present"]) is True
    assert bool(payload["checks"]["provider_health_ok"]) is True


def test_live_stack_check_fails_when_ticks_missing(monkeypatch):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT"}]}
        if path == "/v2/market/ticks":
            return {}
        return {"events": []}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        require_ticks=True,
        require_acked_command=False,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out="",
    )
    rc = live_stack_check.run(args)
    assert rc == 2


def test_live_stack_check_reports_runtime_dashboard_and_provider_failures(monkeypatch, tmp_path: Path):
    state_rows = iter([{"last_heartbeat": "hb-1"}, {"last_heartbeat": "hb-2"}])

    def _fake_fetch(base_url: str, path: str, timeout: float = 2.0):
        del base_url, timeout
        if path == "/v2/health":
            return {"status": "ok", "system_status": "connected"}
        if path == "/v2/ready":
            return {
                "status": "ok",
                "reason": "runtime_startup_stalled",
                "runtime_status": "starting",
                "runtime_phase": "model_load",
                "runtime_phase_pair": "EURUSD",
                "runtime_last_progress_age_secs": 125.0,
                "runtime_failure_reason": "TimeoutError:model_load_timeout",
                "mt4_fresh": False,
                "ticks_fresh": False,
                "provider_health": {
                    "history_provider": {"status": "ok"},
                    "market_data_provider": {"status": "degraded"},
                    "execution_provider": {"status": "degraded"},
                },
                "provider_roles": {
                    "history_provider": "parquet",
                    "market_data_provider": "mt4_bridge",
                    "execution_provider": "mt4",
                },
            }
        if path == "/v2/state":
            try:
                return next(state_rows)
            except StopIteration:
                return {"last_heartbeat": "hb-2"}
        if path.startswith("/v2/reports"):
            return {"reports": [{"report_text": "HEARTBEAT"}]}
        if path == "/v2/market/ticks":
            return {}
        return {}

    monkeypatch.setattr(live_stack_check, "_fetch_json", _fake_fetch)
    monkeypatch.setattr(
        live_stack_check,
        "_fetch_dashboard_state",
        lambda *a, **k: {
            "checked": True,
            "status_code": 503,
            "ok": False,
            "payload": {"systemStatus": "error"},
            "text": "{\"systemStatus\":\"error\"}",
            "url": "http://127.0.0.1:3000/api/trading/state",
        },
    )
    monkeypatch.setattr(live_stack_check.time, "sleep", lambda *_a, **_k: None)

    out = tmp_path / "live_check_failure.json"
    args = argparse.Namespace(
        base_url="http://127.0.0.1:58710",
        dashboard_url="http://127.0.0.1:3000",
        timeout_secs=1.0,
        poll_secs=0.01,
        min_heartbeat_advances=1,
        runtime_stall_secs=30.0,
        require_ticks=True,
        require_acked_command=False,
        command="CLOSE_ALL",
        symbol="EURUSD",
        command_timeout_secs=1.0,
        out=str(out),
    )
    rc = live_stack_check.run(args)
    assert rc == 2
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert bool(payload["checks"]["runtime_startup_ok"]) is False
    assert bool(payload["checks"]["dashboard_state_ok"]) is False
    assert bool(payload["checks"]["provider_health_ok"]) is False
    assert bool(payload["checks"]["mt4_fresh"]) is False
    assert bool(payload["checks"]["ticks_fresh"]) is False
    assert "runtime_startup_failure_reason:TimeoutError:model_load_timeout" in payload["findings"]
    assert any(item.startswith("runtime_model_load_stalled:") for item in payload["findings"])
    assert "dashboard_state_http_503" in payload["findings"]
