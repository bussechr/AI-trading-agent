from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import tools.mt4_interop_efficiency_audit as interop_tool
from tools.mt4_interop_efficiency_audit import run_audit


def test_interop_probe_sender_uses_protocol_endpoint(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _Resp:
        def __init__(self) -> None:
            self.status_code = 200
            self.text = ""
            self.headers = {"content-type": "application/json"}

        def json(self) -> dict:
            return {"status": "queued"}

    def _fake_post(url: str, json: dict, timeout: float):  # noqa: A002
        calls.append((url, dict(json or {})))
        return _Resp()

    monkeypatch.setattr(interop_tool.requests, "post", _fake_post)

    code_v2, body_v2 = interop_tool._post_probe(
        "http://127.0.0.1:58710",
        {"signal_id": "s-v2", "cmd": "INFO"},
    )

    assert int(code_v2) == 200
    assert body_v2["status"] == "queued"
    assert calls[0][0].endswith("/v2/commands")
    assert calls[0][1]["command_id"] == "s-v2"


def test_interop_audit_tool_writes_expected_outputs(tmp_path):
    out_dir = tmp_path / "out"
    transport = tmp_path / "transport_trace.jsonl"
    compute = tmp_path / "compute_trace.jsonl"

    t0 = time.time()
    transport_rows = [
        {
            "ts": t0,
            "mode": "live_shadow",
            "audit_session_id": "sess-test",
            "signal_id": "s1",
            "trace_id": "t1",
            "outcome": "acked",
            "rejection_reason": "",
            "stage_latencies_ms": {
                "signal_post_to_ack_ms": 200.0,
                "bridge_queue_wait_ms": 100.0,
                "poll_delivery_lag_ms": 100.0,
                "ea_handle_to_ack_ms": 40.0,
            },
            "t_bridge_queued": t0,
        }
    ]
    compute_rows = [
        {
            "ts": t0,
            "mode": "live_shadow",
            "phase": "agent_cycle",
            "agent_cycle_ms": 250.0,
        }
    ]

    with transport.open("w", encoding="utf-8") as f:
        for row in transport_rows:
            f.write(json.dumps(row) + "\n")
    with compute.open("w", encoding="utf-8") as f:
        for row in compute_rows:
            f.write(json.dumps(row) + "\n")

    args = argparse.Namespace(
        mode="live_shadow",
        bridge_url="http://127.0.0.1:1",
        transport_trace=str(transport),
        compute_trace=str(compute),
        output_dir=str(out_dir),
        profiles="idle_30m",
        duration_scale=0.001,
        finalize_wait_secs=0.0,
        capacity_window_secs=60,
        loop_interval_secs=5.0,
        session_id="sess-test",
        latency_buckets=[25, 50, 100, 250, 500, 1000],
        config="src/config/fx_el_minis.yaml",
        data_dir="data/fx_minis",
        symbols="EURUSD",
        replay_bars=120,
        replay_warmup=64,
    )

    summary = run_audit(args)

    assert (out_dir / "interop_audit_summary.json").exists()
    assert (out_dir / "interop_stage_latency.csv").exists()
    assert (out_dir / "interop_error_budget.csv").exists()
    assert (out_dir / "interop_capacity_curve.csv").exists()

    assert int(summary["rows"]["transport"]) >= 1
    assert "kpis" in summary
