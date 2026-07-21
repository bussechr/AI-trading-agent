from __future__ import annotations

import argparse
import json
import sys
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import os

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.training.release_evidence import (  # noqa: E402
    SHADOW_EVIDENCE_SCHEMA,
    ReleaseEvidenceIdentity,
    active_manifest_identity,
    file_sha256,
)


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
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    for path in paths:
        url = f"{base}{path}"
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
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


def _fetch_ready(base_url: str) -> dict[str, Any]:
    return _fetch_json(base_url, ["/v2/ready"])


def _fetch_metrics(base_url: str) -> dict[str, Any]:
    return _fetch_json(base_url, ["/v2/metrics"])


def _fetch_commands(base_url: str, limit: int) -> list[dict[str, Any]]:
    payload = _fetch_json(base_url, [f"/v2/commands/history?limit={int(max(1, min(limit, 5000)))}"])
    rows = payload.get("commands", [])
    return list(rows) if isinstance(rows, list) else []


def _fetch_command_window(base_url: str, *, start_ts: float, end_ts: float) -> dict[str, Any]:
    return _fetch_json(
        base_url,
        [f"/v2/commands/window-summary?start_ts={float(start_ts):.9f}&end_ts={float(end_ts):.9f}"],
    )


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
    runtime_ready: bool
    feature_ready: bool
    canary_active: bool
    signals_sent: int
    approved_entries: int
    submitted_entries: int
    ack_success_rate: float
    divergence_spike_count: int
    trade_flow_seen: bool
    runtime_boot_id: str

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
    runtime_ready_seen: bool
    feature_ready_seen: bool
    canary_active_seen: bool
    max_signals_sent: int
    max_approved_entries: int
    max_submitted_entries: int
    max_divergence_spike_count: int
    trade_flow_seen: bool
    poll_attempts: int = 0
    successful_sample_ratio: float = 1.0
    runtime_ready_sample_ratio: float = 1.0
    feature_ready_sample_ratio: float = 1.0
    first_sample_at: float = 0.0
    last_sample_at: float = 0.0
    max_sample_gap_secs: float = 0.0
    runtime_boot_id: str = ""
    continuous_boot: bool = True

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
    schema_version: str
    producer: dict[str, Any]
    generated_at: str
    started_at: float
    ended_at: float
    duration_secs: float
    baseline: SystemSummary
    candidate: SystemSummary
    gates: GateResult
    evidence_identity: dict[str, Any]
    runtime_boundary: dict[str, Any]
    observation_coverage: dict[str, Any]
    baseline_samples: list[dict[str, Any]]
    candidate_samples: list[dict[str, Any]]
    rollback: RollbackAction | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": str(self.schema_version),
            "producer": dict(self.producer),
            "generated_at": self.generated_at,
            "started_at": float(self.started_at),
            "ended_at": float(self.ended_at),
            "duration_secs": float(self.duration_secs),
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "gates": self.gates.to_dict(),
            "evidence_identity": dict(self.evidence_identity),
            "runtime_boundary": dict(self.runtime_boundary),
            "observation_coverage": dict(self.observation_coverage),
            "baseline_samples": [dict(item) for item in self.baseline_samples],
            "candidate_samples": [dict(item) for item in self.candidate_samples],
            "rollback": (self.rollback.to_dict() if self.rollback is not None else None),
        }


def _candidate_runtime_evidence(
    *,
    state: dict[str, Any],
    expected: ReleaseEvidenceIdentity,
    candidate: SystemSummary,
    command_window_summary: dict[str, Any] | None = None,
) -> tuple[ReleaseEvidenceIdentity, dict[str, Any], list[str]]:
    pair = str(expected.pair).upper()
    runtime_diag = dict(state.get("runtime_diag") or {})
    startup = dict(state.get("startup_inference") or runtime_diag.get("startup_inference") or {})
    pair_startup = dict(startup.get(pair) or {})
    activation = dict(state.get("activation_consistency") or runtime_diag.get("activation_consistency") or {})
    manifest = dict(activation.get("manifest") or {})
    actual_model_set_id = str(pair_startup.get("model_set_id") or "").strip()
    runtime_manifest_sha256 = str(manifest.get("manifest_sha256") or "").strip().lower()
    runtime_manifest_path_text = str(manifest.get("path") or "").strip()
    runtime_manifest_path = Path(runtime_manifest_path_text).resolve() if runtime_manifest_path_text else Path()
    observed_identity: ReleaseEvidenceIdentity | None = None
    raw_manifest_matches = False
    if runtime_manifest_path_text and runtime_manifest_path.is_file():
        observed_identity = active_manifest_identity(manifest_path=runtime_manifest_path, pair=pair)
        raw_manifest_matches = bool(
            runtime_manifest_sha256 and file_sha256(runtime_manifest_path) == runtime_manifest_sha256
        )
    actual = ReleaseEvidenceIdentity(
        pair=observed_identity.pair if observed_identity is not None else pair,
        bundle_run_id=observed_identity.bundle_run_id if observed_identity is not None else "",
        model_set_id=actual_model_set_id,
        model_manifest_sha256=(
            observed_identity.model_manifest_sha256 if observed_identity is not None else ""
        ),
        artifact_set_sha256=(
            observed_identity.artifact_set_sha256 if observed_identity is not None else ""
        ),
        evidence_kind="runtime_shadow",
        source_kind="production_runtime_shadow",
        advisory_only=False,
    )

    live = dict(state.get("orchestration_live") or runtime_diag.get("orchestration_live") or {})
    governance = dict(state.get("capital_governance") or runtime_diag.get("capital_governance") or {})
    agent_mode = str(live.get("agent_mode") or live.get("mode") or "").strip().lower()
    shadow_only = bool(
        state.get("shadowOnlyMode", state.get("shadow_only_mode", False))
        or governance.get("shadow_only", False)
    )
    command_window = dict(command_window_summary or {})
    entry_commands_emitted = int(command_window.get("entry_commands", candidate.command_summary.entries_sent) or 0)
    control_commands_emitted = int(command_window.get("control_commands", candidate.command_summary.control_sent) or 0)
    total_commands_emitted = int(
        command_window.get("total_commands", entry_commands_emitted + control_commands_emitted) or 0
    )
    command_window_complete = bool(command_window.get("window_complete", False))
    manifest_matches_db = bool(activation.get("active_manifest_matches_db", False))
    runtime_matches_db = bool(activation.get("runtime_loaded_matches_db", False))
    mismatch_pairs = {str(item).upper() for item in list(activation.get("activation_mismatch_pairs") or [])}
    activation_identity_consistent = bool(
        manifest_matches_db
        and runtime_matches_db
        and pair not in mismatch_pairs
        and observed_identity is not None
        and raw_manifest_matches
        and actual_model_set_id == observed_identity.model_set_id
    )
    broker_emission_disabled = bool(
        shadow_only
        and agent_mode != "live"
        and command_window_complete
        and total_commands_emitted == 0
    )
    pair_readiness = dict(pair_startup.get("pair_readiness") or {})
    lifecycle_ready = bool(
        pair_startup.get("ok") is True
        and actual_model_set_id
        and str(pair_readiness.get("status") or "").strip().lower() == "ready"
        and pair_startup.get("has_exit_model") is True
        and pair_startup.get("has_reversal_models") is True
        and str(pair_startup.get("lifecycle_activation_mode") or "").strip().lower() == "model_driven"
    )
    startup_lifecycle = {
        "startup_inference_ok": pair_startup.get("ok") is True,
        "model_set_id": actual_model_set_id,
        "pair_readiness_status": str(pair_readiness.get("status") or "").strip().lower(),
        "has_exit_model": pair_startup.get("has_exit_model") is True,
        "has_reversal_models": pair_startup.get("has_reversal_models") is True,
        "lifecycle_activation_mode": str(pair_startup.get("lifecycle_activation_mode") or "").strip().lower(),
        "lifecycle_ready": lifecycle_ready,
    }
    boundary = {
        "agent_mode": agent_mode,
        "shadow_only": shadow_only,
        "broker_emission_disabled": broker_emission_disabled,
        "entry_commands_emitted": entry_commands_emitted,
        "control_commands_emitted": control_commands_emitted,
        "total_commands_emitted": total_commands_emitted,
        "command_window_summary": command_window,
        "execution_provider": str(live.get("execution_provider") or ""),
        "active_manifest_matches_db": manifest_matches_db,
        "runtime_loaded_matches_db": runtime_matches_db,
        "activation_identity_consistent": activation_identity_consistent,
        "observed_manifest_path": str(runtime_manifest_path) if runtime_manifest_path_text else "",
        "observed_manifest_file_sha256": runtime_manifest_sha256,
        "observed_manifest_file_sha256_matches": raw_manifest_matches,
        "startup_lifecycle": startup_lifecycle,
    }
    identity_errors = actual.errors(
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
        expected_kind="runtime_shadow",
    )
    if not activation_identity_consistent:
        identity_errors.append("runtime_activation_identity_not_consistent")
    if not lifecycle_ready:
        identity_errors.append("runtime_lifecycle_not_ready")
    if not broker_emission_disabled:
        identity_errors.append("broker_emission_boundary_not_proven")
    return actual, boundary, list(dict.fromkeys(identity_errors))


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


def summarize_command_window(payload: dict[str, Any]) -> CommandSummary:
    entry_status = dict(payload.get("entry_status_counts") or {})
    control_status = dict(payload.get("control_status_counts") or {})
    return CommandSummary(
        entries_sent=int(payload.get("entry_commands") or 0),
        entries_acked=int(entry_status.get("acked") or 0),
        entries_failed=int(entry_status.get("failed") or 0) + int(entry_status.get("expired") or 0),
        control_sent=int(payload.get("control_commands") or 0),
        control_acked=int(control_status.get("acked") or 0),
    )


def _collect_sample(base_url: str, timeout: float) -> PollSample:
    ready = _fetch_ready(base_url)
    state = _fetch_state(base_url)
    metrics = _fetch_metrics(base_url)

    governance = dict(state.get("governance", {}) or {})
    monitor = dict(state.get("monitor", {}) or {})
    envelope = dict(state.get("risk_envelope", {}) or {})
    timeouts = dict(metrics.get("timeouts", {}) or {})
    pending = dict(metrics.get("pending", {}) or {})
    trade_flow = dict(state.get("tradeFlowSummary") or state.get("trade_flow_summary") or {})
    canary_health = dict(trade_flow.get("canaryHealth") or {})
    divergence_counts = dict(trade_flow.get("divergenceCounts") or {})
    feature_online_ready = bool(
        canary_health.get("featureOnlineReady", False)
        or trade_flow.get("featureOnlineReady", False)
        or ready.get("feature_online_ready", ready.get("featureOnlineReady", False))
    )
    feature_data_fresh = bool(
        canary_health.get("featureDataFresh", False)
        or trade_flow.get("featureDataFresh", False)
        or ready.get("feature_data_fresh", ready.get("featureDataFresh", False))
    )

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
        runtime_ready=bool(ready.get("runtime_ready", False)),
        feature_ready=bool(feature_online_ready and feature_data_fresh),
        canary_active=bool(trade_flow.get("canaryActive", False)),
        signals_sent=_safe_int(trade_flow.get("signalsSent", state.get("signals_sent", state.get("signalsSent", 0))), 0),
        approved_entries=_safe_int(trade_flow.get("approvedEntryCount", state.get("approvedEntryCount", 0)), 0),
        submitted_entries=_safe_int(trade_flow.get("submittedEntryCount", state.get("submittedEntryCount", 0)), 0),
        ack_success_rate=_clip(_safe_float(trade_flow.get("ackSuccessRate", 0.0), 0.0), 0.0, 1.0),
        divergence_spike_count=int(
            _safe_int(divergence_counts.get("shadowLiveOnly", 0), 0)
            + _safe_int(divergence_counts.get("adaptiveLiveOnly", 0), 0)
            + _safe_int(divergence_counts.get("orchestratorFaultCount", 0), 0)
        ),
        trade_flow_seen=bool(trade_flow),
        runtime_boot_id=str(state.get("runtime_boot_id") or ready.get("runtime_boot_id") or "").strip(),
    )


def _summarize_system(
    *,
    name: str,
    url: str,
    start_ts: float,
    end_ts: float,
    samples: list[PollSample],
    commands: list[dict[str, Any]] | None,
    command_window_summary: dict[str, Any] | None = None,
    governance_events: list[dict[str, Any]],
    poll_attempts: int,
) -> SystemSummary:
    cmd_summary = (
        summarize_command_window(dict(command_window_summary or {}))
        if command_window_summary is not None
        else summarize_commands(list(commands or []), start_ts=start_ts, end_ts=end_ts)
    )
    if samples:
        avg_decisions = float(sum(float(s.decisions) for s in samples) / len(samples))
        avg_pending = float(sum(float(s.pending) for s in samples) / len(samples))
        max_timeout_rate = float(max(float(s.timeout_rate) for s in samples))
        max_drawdown_pct = float(max(float(s.drawdown_pct) for s in samples))
        hard_breach_seen = any(float(s.drawdown_pct) >= float(max(s.hard_dd_pct, 1e-9)) for s in samples)
        daily_breaker_seen = any(bool(s.daily_breaker_active) for s in samples)
        governance_pause_seen = any(bool(s.governance_paused) for s in samples)
        runtime_ready_seen = any(bool(s.runtime_ready) for s in samples)
        feature_ready_seen = any(bool(s.feature_ready) for s in samples)
        canary_active_seen = any(bool(s.canary_active) for s in samples)
        max_signals_sent = int(max(float(s.signals_sent) for s in samples))
        max_approved_entries = int(max(float(s.approved_entries) for s in samples))
        max_submitted_entries = int(max(float(s.submitted_entries) for s in samples))
        max_divergence_spike_count = int(max(float(s.divergence_spike_count) for s in samples))
        trade_flow_seen = any(bool(s.trade_flow_seen) for s in samples)
        first_sample_at = float(min(s.ts for s in samples))
        last_sample_at = float(max(s.ts for s in samples))
        ordered_ts = sorted(float(s.ts) for s in samples)
        max_sample_gap_secs = max(
            [ordered_ts[index] - ordered_ts[index - 1] for index in range(1, len(ordered_ts))] or [0.0]
        )
        runtime_ready_sample_ratio = float(sum(1 for s in samples if s.runtime_ready) / len(samples))
        feature_ready_sample_ratio = float(sum(1 for s in samples if s.feature_ready) / len(samples))
        boot_ids = {str(s.runtime_boot_id).strip() for s in samples if str(s.runtime_boot_id).strip()}
        runtime_boot_id = next(iter(boot_ids)) if len(boot_ids) == 1 else ""
        continuous_boot = bool(len(boot_ids) == 1 and all(str(s.runtime_boot_id).strip() for s in samples))
    else:
        avg_decisions = 0.0
        avg_pending = 0.0
        max_timeout_rate = 0.0
        max_drawdown_pct = 0.0
        hard_breach_seen = False
        daily_breaker_seen = False
        governance_pause_seen = False
        runtime_ready_seen = False
        feature_ready_seen = False
        canary_active_seen = False
        max_signals_sent = 0
        max_approved_entries = 0
        max_submitted_entries = 0
        max_divergence_spike_count = 0
        trade_flow_seen = False
        first_sample_at = 0.0
        last_sample_at = 0.0
        max_sample_gap_secs = 0.0
        runtime_ready_sample_ratio = 0.0
        feature_ready_sample_ratio = 0.0
        runtime_boot_id = ""
        continuous_boot = False

    attempts = max(0, int(poll_attempts))
    successful_sample_ratio = float(len(samples) / attempts) if attempts > 0 else 0.0

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
        runtime_ready_seen=bool(runtime_ready_seen),
        feature_ready_seen=bool(feature_ready_seen),
        canary_active_seen=bool(canary_active_seen),
        max_signals_sent=int(max_signals_sent),
        max_approved_entries=int(max_approved_entries),
        max_submitted_entries=int(max_submitted_entries),
        max_divergence_spike_count=int(max_divergence_spike_count),
        trade_flow_seen=bool(trade_flow_seen),
        poll_attempts=attempts,
        successful_sample_ratio=successful_sample_ratio,
        runtime_ready_sample_ratio=runtime_ready_sample_ratio,
        feature_ready_sample_ratio=feature_ready_sample_ratio,
        first_sample_at=first_sample_at,
        last_sample_at=last_sample_at,
        max_sample_gap_secs=max_sample_gap_secs,
        runtime_boot_id=runtime_boot_id,
        continuous_boot=continuous_boot,
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
    operability_ok = bool(
        candidate.samples > 1
        and candidate.successful_sample_ratio >= 0.95
        and candidate.runtime_ready_sample_ratio >= 0.95
        and candidate.feature_ready_sample_ratio >= 0.95
        and candidate.continuous_boot
    )
    # A physically isolated shadow must not manufacture broker orders merely to
    # prove liveness. Samples from the real runtime loop are the flow evidence;
    # trade-flow telemetry is retained as a diagnostic only.
    runtime_flow_evidence_ok = bool(candidate.samples > 0)

    checks = {
        "throughput": bool(throughput_ok),
        "reliability": bool(reliability_ok),
        "risk": bool(risk_ok),
        "operability": bool(operability_ok),
        "runtime_flow_evidence": bool(runtime_flow_evidence_ok),
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
    if not checks["runtime_flow_evidence"]:
        rollback_triggers.append("runtime_flow_evidence_gate_failed")

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
        f"- Candidate runtime ready seen: `{cand.runtime_ready_seen}`",
        f"- Candidate feature ready seen: `{cand.feature_ready_seen}`",
        f"- Candidate canary active seen: `{cand.canary_active_seen}`",
        f"- Candidate max sent/approved/submitted: `{cand.max_signals_sent}/{cand.max_approved_entries}/{cand.max_submitted_entries}`",
        f"- Candidate divergence spike max: `{cand.max_divergence_spike_count}`",
        f"- Baseline governance events in window: `{base.governance_events_window}`",
        f"- Candidate governance events in window: `{cand.governance_events_window}`",
        f"- Candidate hard breach seen: `{cand.hard_breach_seen}`",
        f"- Candidate daily breaker seen: `{cand.daily_breaker_seen}`",
        f"- Evidence pair/bundle: `{report.evidence_identity.get('pair', '')}/{report.evidence_identity.get('bundle_run_id', '')}`",
        f"- Active manifest SHA-256: `{report.evidence_identity.get('model_manifest_sha256', '')}`",
        f"- Broker emission disabled: `{report.runtime_boundary.get('broker_emission_disabled', False)}`",
        f"- Entry commands emitted: `{report.runtime_boundary.get('entry_commands_emitted', -1)}`",
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
    expected_identity = active_manifest_identity(
        manifest_path=Path(str(args.model_manifest)),
        pair=str(args.pair).upper(),
    )
    requested_bundle_run_id = str(args.bundle_run_id or "").strip()
    if requested_bundle_run_id and requested_bundle_run_id != expected_identity.bundle_run_id:
        raise SystemExit("--bundle-run-id does not match the selected pair in --model-manifest")
    if not expected_identity.bundle_run_id:
        raise SystemExit("selected pair is missing model_set_id in --model-manifest")

    print(f"Starting shadow dual-run: baseline={baseline_url} candidate={candidate_url}")
    print(f"Duration={duration_secs:.1f}s poll={poll_secs:.1f}s")

    started_at = float(time.time())
    end_at_target = started_at + duration_secs

    baseline_samples: list[PollSample] = []
    candidate_samples: list[PollSample] = []
    poll_attempts = 0

    while True:
        now = float(time.time())
        if now >= end_at_target:
            break
        poll_attempts += 1

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

    baseline_command_window = _fetch_command_window(
        baseline_url,
        start_ts=started_at,
        end_ts=ended_at,
    )
    candidate_command_window = _fetch_command_window(
        candidate_url,
        start_ts=started_at,
        end_ts=ended_at,
    )
    baseline_events = _fetch_governance_events(baseline_url, limit=int(args.event_limit))
    candidate_events = _fetch_governance_events(candidate_url, limit=int(args.event_limit))

    base_summary = _summarize_system(
        name="baseline",
        url=baseline_url,
        start_ts=started_at,
        end_ts=ended_at,
        samples=baseline_samples,
        commands=None,
        command_window_summary=baseline_command_window,
        governance_events=baseline_events,
        poll_attempts=poll_attempts,
    )
    cand_summary = _summarize_system(
        name="candidate",
        url=candidate_url,
        start_ts=started_at,
        end_ts=ended_at,
        samples=candidate_samples,
        commands=None,
        command_window_summary=candidate_command_window,
        governance_events=candidate_events,
        poll_attempts=poll_attempts,
    )

    candidate_state = _fetch_state(candidate_url)
    actual_identity, runtime_boundary, evidence_errors = _candidate_runtime_evidence(
        state=candidate_state,
        expected=expected_identity,
        candidate=cand_summary,
        command_window_summary=candidate_command_window,
    )

    gates = evaluate_gates(
        baseline=base_summary,
        candidate=cand_summary,
        min_throughput_delta=int(args.min_throughput_delta),
        max_timeout_rate=float(args.max_timeout_rate),
        require_nonzero=bool(args.require_nonzero_entries),
    )
    gates.checks["model_identity"] = not evidence_errors
    gates.checks["broker_emission_disabled"] = bool(runtime_boundary.get("broker_emission_disabled", False))
    for error in evidence_errors:
        trigger = f"release_evidence:{error}"
        if trigger not in gates.rollback_triggers:
            gates.rollback_triggers.append(trigger)
    gates.passed = all(bool(value) for value in gates.checks.values())

    rollback_action: RollbackAction | None = None
    if (not gates.passed) and bool(args.rollback_on_fail):
        rollback_action = execute_rollback_command(
            str(args.rollback_cmd or ""),
            timeout_secs=float(args.rollback_timeout_secs),
        )

    report = ShadowRunReport(
        schema_version=SHADOW_EVIDENCE_SCHEMA,
        producer={"tool": "tools.shadow_dual_run", "version": "v2"},
        generated_at=_iso_now(),
        started_at=float(started_at),
        ended_at=float(ended_at),
        duration_secs=float(max(0.0, ended_at - started_at)),
        baseline=base_summary,
        candidate=cand_summary,
        gates=gates,
        evidence_identity=actual_identity.to_dict(),
        runtime_boundary=runtime_boundary,
        observation_coverage={
            "poll_interval_secs": poll_secs,
            "poll_attempts": cand_summary.poll_attempts,
            "successful_samples": cand_summary.samples,
            "successful_sample_ratio": cand_summary.successful_sample_ratio,
            "runtime_ready_sample_ratio": cand_summary.runtime_ready_sample_ratio,
            "feature_ready_sample_ratio": cand_summary.feature_ready_sample_ratio,
            "first_sample_at": cand_summary.first_sample_at,
            "last_sample_at": cand_summary.last_sample_at,
            "observed_span_secs": max(0.0, cand_summary.last_sample_at - cand_summary.first_sample_at),
            "max_sample_gap_secs": cand_summary.max_sample_gap_secs,
            "runtime_boot_id": cand_summary.runtime_boot_id,
            "continuous_boot": cand_summary.continuous_boot,
        },
        baseline_samples=[sample.to_dict() for sample in baseline_samples],
        candidate_samples=[sample.to_dict() for sample in candidate_samples],
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
    ap.add_argument("--min-throughput-delta", type=int, default=0)
    ap.add_argument("--max-timeout-rate", type=float, default=0.05)
    ap.add_argument("--require-nonzero-entries", action="store_true", default=False)
    ap.add_argument("--rollback-on-fail", action="store_true", default=False)
    ap.add_argument("--rollback-cmd", default="")
    ap.add_argument("--rollback-timeout-secs", type=float, default=45.0)
    ap.add_argument("--out-dir", default="docs")
    ap.add_argument("--prefix", default="shadow_dual_run")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--bundle-run-id", default="")
    ap.add_argument("--model-manifest", required=True)
    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
