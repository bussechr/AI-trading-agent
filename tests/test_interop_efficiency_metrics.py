from __future__ import annotations

from src.audit.interop_efficiency import (
    parse_latency_buckets,
    summarize_interop_kpis,
    summarize_stage_latency,
)


def test_parse_latency_buckets_sanitizes_and_sorts():
    out = parse_latency_buckets("100,50,foo,250,0,-1,50")
    assert out == [50, 100, 250]


def test_stage_latency_and_kpi_ranges():
    rows = [
        {
            "stage_latencies_ms": {
                "signal_post_to_ack_ms": 120.0,
                "bridge_queue_wait_ms": 80.0,
                "poll_delivery_lag_ms": 80.0,
                "ea_handle_to_ack_ms": 35.0,
            },
            "outcome": "acked",
            "rejection_reason": "",
        },
        {
            "stage_latencies_ms": {
                "signal_post_to_ack_ms": 240.0,
                "bridge_queue_wait_ms": 110.0,
                "poll_delivery_lag_ms": 110.0,
                "ea_handle_to_ack_ms": 50.0,
            },
            "outcome": "failed",
            "rejection_reason": "close_failed",
        },
        {
            "agent_cycle_ms": 220.0,
        },
        {
            "agent_cycle_ms": 330.0,
        },
    ]

    stage = summarize_stage_latency(rows)
    assert "signal_post_to_ack_ms" in stage
    assert float(stage["signal_post_to_ack_ms"]["percentiles"]["p95"]) >= 120.0

    kpis = summarize_interop_kpis(rows, loop_interval_secs=1.0)
    assert float(kpis["signal_post_to_ack_ms"]["p95"]) >= 120.0
    assert 0.0 <= float(kpis["timeout_rate"]) <= 1.0
    assert 0.0 <= float(kpis["agent_cycle_cpu_util_p95"]) <= 1.0
