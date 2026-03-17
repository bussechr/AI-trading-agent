from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _clip(value: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, value)))


def _fetch_json(base_url: str, paths: list[str], timeout: float = 2.0) -> dict[str, Any]:
    last_err: Exception | None = None
    base = str(base_url).rstrip("/")
    for path in paths:
        url = f"{base}{path}"
        try:
            r = requests.get(url, timeout=timeout)
            if not r.ok:
                last_err = RuntimeError(f"HTTP {r.status_code} for {url}")
                continue
            payload = r.json()
            if isinstance(payload, dict):
                return payload
            return {}
        except Exception as exc:
            last_err = exc
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("No endpoint paths provided")


def _fetch_state(base_url: str) -> dict[str, Any]:
    return _fetch_json(base_url, ["/v2/state"])


def _fetch_metrics(base_url: str) -> dict[str, Any]:
    return _fetch_json(base_url, ["/v2/metrics"])


def _fetch_commands(base_url: str, limit: int) -> list[dict[str, Any]]:
    payload = _fetch_json(base_url, [f"/v2/commands/history?limit={int(max(1, min(limit, 5000)))}"])
    rows = payload.get("commands", [])
    return list(rows) if isinstance(rows, list) else []


def _fetch_governance_events(base_url: str, limit: int) -> list[dict[str, Any]]:
    payload = _fetch_json(base_url, [f"/v2/governance/events?limit={int(max(1, min(limit, 2000)))}"])
    rows = payload.get("events", [])
    return list(rows) if isinstance(rows, list) else []


@dataclass(slots=True)
class PollSample:
    ts: float
    decisions: int
    pending: int
    timeout_rate: float
    drawdown_pct: float
    hard_dd_pct: float
    daily_breaker_active: bool
    governance_paused: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CommandSummary:
    entries_sent: int
    entries_acked: int
    entries_failed: int
    control_sent: int
    control_acked: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SystemSummary:
    name: str
    url: str
    command_summary: CommandSummary
    samples: int
    avg_decisions: float
    avg_pending: float
    max_timeout_rate: float
    max_drawdown_pct: float
    hard_breach_seen: bool
    daily_breaker_seen: bool
    governance_pause_seen: bool
    governance_events_window: int

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["command_summary"] = self.command_summary.to_dict()
        return out


@dataclass(slots=True)
class GateResult:
    passed: bool
    throughput_delta_entries_acked: int
    checks: dict[str, bool]
    rollback_triggers: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RollbackAction:
    attempted: bool
    command: str
    success: bool
    return_code: int
    timed_out: bool
    duration_secs: float
    stdout_tail: str
    stderr_tail: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ShadowRunReport:
    generated_at: str
    started_at: float
    ended_at: float
    duration_secs: float
    baseline: SystemSummary
    candidate: SystemSummary
    gates: GateResult
    rollback: RollbackAction | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "started_at": float(self.started_at),
            "ended_at": float(self.ended_at),
            "duration_secs": float(self.duration_secs),
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "gates": self.gates.to_dict(),
            "rollback": (self.rollback.to_dict() if self.rollback is not None else None),
        }


def summarize_commands(commands: list[dict[str, Any]], *, start_ts: float, end_ts: float) -> CommandSummary:
    entries_sent = 0
    entries_acked = 0
    entries_failed = 0
    control_sent = 0
    control_acked = 0

    for row in list(commands or []):
        created = _safe_float(row.get("created_at", row.get("updated_at", 0.0)), 0.0)
        if created > 0 and (created < float(start_ts) or created > float(end_ts)):
            continue
        cmd = str(row.get("cmd", "")).upper().strip()
        status = str(row.get("status", "")).lower().strip()
        is_entry = cmd in {"BUY", "SELL"}
        if is_entry:
            entries_sent += 1
            if status == "acked":
                entries_acked += 1
            elif status in {"failed", "expired"}:
                entries_failed += 1
        else:
            control_sent += 1
            if status == "acked":
                control_acked += 1

    return CommandSummary(
        entries_sent=int(entries_sent),
        entries_acked=int(entries_acked),
        entries_failed=int(entries_failed),
        control_sent=int(control_sent),
        control_acked=int(control_acked),
    )


def _collect_sample(base_url: str, timeout: float) -> PollSample:
    state = _fetch_state(base_url)
    metrics = _fetch_metrics(base_url)

    governance = dict(state.get("governance", {}) or {})
    monitor = dict(state.get("monitor", {}) or {})
    envelope = dict(state.get("risk_envelope", {}) or {})
    timeouts = dict(metrics.get("timeouts", {}) or {})
    pending = dict(metrics.get("pending", {}) or {})

    hard_dd_pct = _safe_float(governance.get("hard_dd_pct", envelope.get("hard_dd_pct", 0.12)), 0.12)
    drawdown_pct = _safe_float(governance.get("drawdown_pct", 0.0), 0.0)
    decisions = len(list(state.get("agent_decisions", []) or []))
    daily_breaker = bool(
        governance.get("daily_breaker_active", False)
        or monitor.get("daily_breaker_active", False)
        or ("daily_loss_breaker" in list(governance.get("reasons", []) or []))
    )

    return PollSample(
        ts=float(time.time()),
        decisions=int(decisions),
        pending=_safe_int(pending.get("count", pending.get("pending_count", 0)), 0),
        timeout_rate=_clip(_safe_float(timeouts.get("ack_timeout_rate_5m", 0.0), 0.0), 0.0, 1.0),
        drawdown_pct=max(0.0, drawdown_pct),
        hard_dd_pct=max(0.0, hard_dd_pct),
        daily_breaker_active=bool(daily_breaker),
        governance_paused=bool(governance.get("paused", False)),
    )


def _summarize_system(
    *,
    name: str,
    url: str,
    start_ts: float,
    end_ts: float,
    samples: list[PollSample],
    commands: list[dict[str, Any]],
    governance_events: list[dict[str, Any]],
) -> SystemSummary:
    cmd_summary = summarize_commands(commands, start_ts=start_ts, end_ts=end_ts)
    if samples:
        avg_decisions = float(sum(float(s.decisions) for s in samples) / len(samples))
        avg_pending = float(sum(float(s.pending) for s in samples) / len(samples))
        max_timeout_rate = float(max(float(s.timeout_rate) for s in samples))
        max_drawdown_pct = float(max(float(s.drawdown_pct) for s in samples))
        hard_breach_seen = any(float(s.drawdown_pct) >= float(max(s.hard_dd_pct, 1e-9)) for s in samples)
        daily_breaker_seen = any(bool(s.daily_breaker_active) for s in samples)
        governance_pause_seen = any(bool(s.governance_paused) for s in samples)
    else:
        avg_decisions = 0.0
        avg_pending = 0.0
        max_timeout_rate = 0.0
        max_drawdown_pct = 0.0
        hard_breach_seen = False
        daily_breaker_seen = False
        governance_pause_seen = False

    ge_window = 0
    for ev in list(governance_events or []):
        ts = _safe_float(ev.get("time", 0.0), 0.0)
        if ts <= 0:
            continue
        if start_ts <= ts <= end_ts:
            ge_window += 1

    return SystemSummary(
        name=str(name),
        url=str(url),
        command_summary=cmd_summary,
        samples=int(len(samples)),
        avg_decisions=float(avg_decisions),
        avg_pending=float(avg_pending),
        max_timeout_rate=float(max_timeout_rate),
        max_drawdown_pct=float(max_drawdown_pct),
        hard_breach_seen=bool(hard_breach_seen),
        daily_breaker_seen=bool(daily_breaker_seen),
        governance_pause_seen=bool(governance_pause_seen),
        governance_events_window=int(ge_window),
    )


def evaluate_gates(
    *,
    baseline: SystemSummary,
    candidate: SystemSummary,
    min_throughput_delta: int,
    max_timeout_rate: float,
    require_nonzero: bool,
) -> GateResult:
    throughput_delta = int(candidate.command_summary.entries_acked - baseline.command_summary.entries_acked)
    throughput_ok = throughput_delta >= int(min_throughput_delta)
    if require_nonzero:
        throughput_ok = throughput_ok and int(candidate.command_summary.entries_acked) > 0

    reliability_ok = float(candidate.max_timeout_rate) <= float(max_timeout_rate)
    risk_ok = (not bool(candidate.hard_breach_seen)) and (not bool(candidate.daily_breaker_seen))
    operability_ok = bool(candidate.samples > 0)

    checks = {
        "throughput": bool(throughput_ok),
        "reliability": bool(reliability_ok),
        "risk": bool(risk_ok),
        "operability": bool(operability_ok),
    }
    rollback_triggers: list[str] = []
    if not checks["throughput"]:
        rollback_triggers.append("throughput_gate_failed")
    if not checks["reliability"]:
        rollback_triggers.append("reliability_gate_failed")
    if not checks["risk"]:
        rollback_triggers.append("risk_gate_failed")
    if not checks["operability"]:
        rollback_triggers.append("operability_gate_failed")

    passed = all(bool(v) for v in checks.values())
    return GateResult(
        passed=bool(passed),
        throughput_delta_entries_acked=int(throughput_delta),
        checks=checks,
        rollback_triggers=rollback_triggers,
    )


def _tail_text(raw: Any, *, max_chars: int = 4000) -> str:
    text = str(raw or "")
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def execute_rollback_command(command: str, *, timeout_secs: float = 45.0) -> RollbackAction:
    cmd = str(command or "").strip()
    if not cmd:
        return RollbackAction(
            attempted=False,
            command="",
            success=False,
            return_code=0,
            timed_out=False,
            duration_secs=0.0,
            stdout_tail="",
            stderr_tail="",
        )

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=float(max(1.0, timeout_secs)),
        )
        ended = time.time()
        ok = int(proc.returncode) == 0
        return RollbackAction(
            attempted=True,
            command=cmd,
            success=bool(ok),
            return_code=int(proc.returncode),
            timed_out=False,
            duration_secs=float(max(0.0, ended - started)),
            stdout_tail=_tail_text(proc.stdout),
            stderr_tail=_tail_text(proc.stderr),
        )
    except subprocess.TimeoutExpired as exc:
        ended = time.time()
        return RollbackAction(
            attempted=True,
            command=cmd,
            success=False,
            return_code=-1,
            timed_out=True,
            duration_secs=float(max(0.0, ended - started)),
            stdout_tail=_tail_text(getattr(exc, "stdout", "")),
            stderr_tail=_tail_text(getattr(exc, "stderr", "")),
        )
    except Exception as exc:
        ended = time.time()
        return RollbackAction(
            attempted=True,
            command=cmd,
            success=False,
            return_code=-1,
            timed_out=False,
            duration_secs=float(max(0.0, ended - started)),
            stdout_tail="",
            stderr_tail=_tail_text(f"{type(exc).__name__}: {exc}"),
        )


def _render_markdown(report: ShadowRunReport) -> str:
    gates = report.gates
    base = report.baseline
    cand = report.candidate
    lines = [
        "# Shadow Dual-Run Report",
        "",
        f"Generated at: `{report.generated_at}`",
        f"Window: `{datetime.fromtimestamp(report.started_at, tz=timezone.utc).isoformat()}` -> `{datetime.fromtimestamp(report.ended_at, tz=timezone.utc).isoformat()}`",
        f"Duration: `{report.duration_secs:.1f}s`",
        "",
        "## Gate Status",
        "",
        f"- Overall: **{'PASS' if gates.passed else 'FAIL'}**",
        f"- Throughput delta (entries acked): `{gates.throughput_delta_entries_acked}`",
        f"- Throughput gate: `{gates.checks.get('throughput')}`",
        f"- Reliability gate: `{gates.checks.get('reliability')}`",
        f"- Risk gate: `{gates.checks.get('risk')}`",
        f"- Operability gate: `{gates.checks.get('operability')}`",
        "",
        "## Candidate vs Baseline",
        "",
        f"- Baseline acked entries: `{base.command_summary.entries_acked}`",
        f"- Candidate acked entries: `{cand.command_summary.entries_acked}`",
        f"- Baseline timeout max: `{base.max_timeout_rate:.4f}`",
        f"- Candidate timeout max: `{cand.max_timeout_rate:.4f}`",
        f"- Baseline governance events in window: `{base.governance_events_window}`",
        f"- Candidate governance events in window: `{cand.governance_events_window}`",
        f"- Candidate hard breach seen: `{cand.hard_breach_seen}`",
        f"- Candidate daily breaker seen: `{cand.daily_breaker_seen}`",
        "",
        "## Rollback Triggers",
        "",
    ]
    if gates.rollback_triggers:
        for reason in gates.rollback_triggers:
            lines.append(f"- `{reason}`")
    else:
        lines.append("- (none)")

    rb = report.rollback
    if rb is not None:
        lines.extend(
            [
                "",
                "## Rollback Action",
                "",
                f"- Attempted: `{rb.attempted}`",
                f"- Success: `{rb.success}`",
                f"- Return code: `{rb.return_code}`",
                f"- Timed out: `{rb.timed_out}`",
                f"- Duration secs: `{rb.duration_secs:.2f}`",
                f"- Command: `{rb.command}`",
            ]
        )
        if rb.stderr_tail:
            lines.append(f"- stderr tail: `{rb.stderr_tail}`")

    lines.append("")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    baseline_url = str(args.baseline_url).rstrip("/")
    candidate_url = str(args.candidate_url).rstrip("/")
    duration_secs = float(max(5.0, args.duration_secs))
    poll_secs = float(max(0.5, args.poll_secs))

    print(f"Starting shadow dual-run: baseline={baseline_url} candidate={candidate_url}")
    print(f"Duration={duration_secs:.1f}s poll={poll_secs:.1f}s")

    started_at = float(time.time())
    end_at_target = started_at + duration_secs

    baseline_samples: list[PollSample] = []
    candidate_samples: list[PollSample] = []

    while True:
        now = float(time.time())
        if now >= end_at_target:
            break

        try:
            baseline_samples.append(_collect_sample(baseline_url, timeout=2.0))
        except Exception as exc:
            print(f"[warn] baseline poll failed: {exc}")
        try:
            candidate_samples.append(_collect_sample(candidate_url, timeout=2.0))
        except Exception as exc:
            print(f"[warn] candidate poll failed: {exc}")

        sleep_for = max(0.0, poll_secs - (time.time() - now))
        if sleep_for > 0:
            time.sleep(sleep_for)

    ended_at = float(time.time())

    baseline_commands = _fetch_commands(baseline_url, limit=int(args.command_limit))
    candidate_commands = _fetch_commands(candidate_url, limit=int(args.command_limit))
    baseline_events = _fetch_governance_events(baseline_url, limit=int(args.event_limit))
    candidate_events = _fetch_governance_events(candidate_url, limit=int(args.event_limit))

    base_summary = _summarize_system(
        name="baseline",
        url=baseline_url,
        start_ts=started_at,
        end_ts=ended_at,
        samples=baseline_samples,
        commands=baseline_commands,
        governance_events=baseline_events,
    )
    cand_summary = _summarize_system(
        name="candidate",
        url=candidate_url,
        start_ts=started_at,
        end_ts=ended_at,
        samples=candidate_samples,
        commands=candidate_commands,
        governance_events=candidate_events,
    )

    gates = evaluate_gates(
        baseline=base_summary,
        candidate=cand_summary,
        min_throughput_delta=int(args.min_throughput_delta),
        max_timeout_rate=float(args.max_timeout_rate),
        require_nonzero=bool(args.require_nonzero_entries),
    )

    rollback_action: RollbackAction | None = None
    if (not gates.passed) and bool(args.rollback_on_fail):
        rollback_action = execute_rollback_command(
            str(args.rollback_cmd or ""),
            timeout_secs=float(args.rollback_timeout_secs),
        )

    report = ShadowRunReport(
        generated_at=_iso_now(),
        started_at=float(started_at),
        ended_at=float(ended_at),
        duration_secs=float(max(0.0, ended_at - started_at)),
        baseline=base_summary,
        candidate=cand_summary,
        gates=gates,
        rollback=rollback_action,
    )

    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = str(args.prefix).strip() or "shadow_dual_run"
    json_path = out_dir / f"{prefix}_{stamp}.json"
    md_path = out_dir / f"{prefix}_{stamp}.md"

    json_path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    print(f"Wrote shadow-run JSON: {json_path}")
    print(f"Wrote shadow-run MD:   {md_path}")
    print(f"Overall gate status:   {'PASS' if gates.passed else 'FAIL'}")
    if gates.rollback_triggers:
        print(f"Rollback triggers:     {', '.join(gates.rollback_triggers)}")
    if rollback_action is not None:
        print(
            "Rollback command:      "
            + ("SUCCESS" if rollback_action.success else "FAILED")
            + f" (rc={rollback_action.return_code}, timeout={rollback_action.timed_out})"
        )

    if gates.passed:
        return 0
    if rollback_action is not None and rollback_action.attempted and (not rollback_action.success):
        return 3
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run baseline vs candidate bridge shadow dual-run and evaluate canary gates")
    ap.add_argument("--baseline-url", default="http://127.0.0.1:58710")
    ap.add_argument("--candidate-url", default="http://127.0.0.1:58711")
    ap.add_argument("--duration-secs", type=float, default=300.0)
    ap.add_argument("--poll-secs", type=float, default=2.0)
    ap.add_argument("--command-limit", type=int, default=5000)
    ap.add_argument("--event-limit", type=int, default=2000)
    ap.add_argument("--min-throughput-delta", type=int, default=1)
    ap.add_argument("--max-timeout-rate", type=float, default=0.05)
    ap.add_argument("--require-nonzero-entries", action="store_true", default=False)
    ap.add_argument("--rollback-on-fail", action="store_true", default=False)
    ap.add_argument("--rollback-cmd", default="")
    ap.add_argument("--rollback-timeout-secs", type=float, default=45.0)
    ap.add_argument("--out-dir", default="docs")
    ap.add_argument("--prefix", default="shadow_dual_run")
    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
