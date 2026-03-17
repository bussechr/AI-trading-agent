from __future__ import annotations

from tools.shadow_dual_run import (
    CommandSummary,
    SystemSummary,
    execute_rollback_command,
    evaluate_gates,
    summarize_commands,
)


def test_summarize_commands_filters_window_and_statuses():
    rows = [
        {"created_at": 100.0, "cmd": "BUY", "status": "acked"},
        {"created_at": 101.0, "cmd": "SELL", "status": "failed"},
        {"created_at": 102.0, "cmd": "CLOSE", "status": "acked"},
        {"created_at": 80.0, "cmd": "BUY", "status": "acked"},
    ]
    out = summarize_commands(rows, start_ts=99.0, end_ts=103.0)

    assert out.entries_sent == 2
    assert out.entries_acked == 1
    assert out.entries_failed == 1
    assert out.control_sent == 1
    assert out.control_acked == 1


def _summary(name: str, acked: int, timeout_rate: float, hard: bool = False, daily: bool = False) -> SystemSummary:
    return SystemSummary(
        name=name,
        url=f"http://{name}",
        command_summary=CommandSummary(entries_sent=acked + 1, entries_acked=acked, entries_failed=0, control_sent=0, control_acked=0),
        samples=10,
        avg_decisions=1.2,
        avg_pending=0.2,
        max_timeout_rate=timeout_rate,
        max_drawdown_pct=0.05,
        hard_breach_seen=hard,
        daily_breaker_seen=daily,
        governance_pause_seen=False,
        governance_events_window=1,
    )


def test_evaluate_gates_pass():
    base = _summary("base", acked=5, timeout_rate=0.01)
    cand = _summary("cand", acked=8, timeout_rate=0.02)
    gates = evaluate_gates(
        baseline=base,
        candidate=cand,
        min_throughput_delta=1,
        max_timeout_rate=0.05,
        require_nonzero=True,
    )
    assert gates.passed is True
    assert gates.throughput_delta_entries_acked == 3
    assert gates.rollback_triggers == []


def test_evaluate_gates_fail_with_risk_breach():
    base = _summary("base", acked=5, timeout_rate=0.01)
    cand = _summary("cand", acked=4, timeout_rate=0.08, hard=True)
    gates = evaluate_gates(
        baseline=base,
        candidate=cand,
        min_throughput_delta=1,
        max_timeout_rate=0.05,
        require_nonzero=True,
    )
    assert gates.passed is False
    assert "throughput_gate_failed" in gates.rollback_triggers
    assert "reliability_gate_failed" in gates.rollback_triggers
    assert "risk_gate_failed" in gates.rollback_triggers


def test_execute_rollback_command_success():
    rb = execute_rollback_command("echo rollback_ok", timeout_secs=5.0)
    assert rb.attempted is True
    assert rb.success is True
    assert rb.return_code == 0
    assert "rollback_ok" in rb.stdout_tail


def test_execute_rollback_command_timeout():
    rb = execute_rollback_command("sleep 2", timeout_secs=1.0)
    assert rb.attempted is True
    assert rb.success is False
    assert rb.timed_out is True
    assert rb.return_code == -1
