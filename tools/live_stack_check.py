from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_json(base_url: str, path: str, timeout: float = 2.0) -> dict[str, Any]:
    base = str(base_url).rstrip("/")
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    r = requests.get(f"{base}{path}", headers=headers, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if isinstance(payload, dict):
        return payload
    return {}


def _post_json(base_url: str, path: str, payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
    base = str(base_url).rstrip("/")
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    r = requests.post(f"{base}{path}", json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    out = r.json()
    if isinstance(out, dict):
        return out
    return {}


def _post_keepalive_report(base_url: str, *, source: str, timeout: float = 2.0) -> dict[str, Any]:
    return _post_json(
        base_url,
        "/v2/reports",
        payload={
            "report_type": "heartbeat_keepalive",
            "source": str(source or "live_stack_check"),
            "generated_at": _now_iso(),
        },
        timeout=timeout,
    )


def _fetch_dashboard_state(dashboard_url: str, path: str = "/api/trading/state", timeout: float = 3.0) -> dict[str, Any]:
    base = str(dashboard_url).rstrip("/")
    if not base:
        return {"checked": False, "status_code": None, "ok": False, "payload": {}, "text": "", "url": ""}
    url = f"{base}{path}"
    try:
        r = requests.get(url, timeout=timeout)
        payload: dict[str, Any] = {}
        try:
            raw = r.json()
            if isinstance(raw, dict):
                payload = raw
        except Exception:
            payload = {}
        return {
            "checked": True,
            "status_code": int(r.status_code),
            "ok": bool(r.ok),
            "payload": payload,
            "text": str(r.text or ""),
            "url": url,
        }
    except Exception as exc:
        return {
            "checked": True,
            "status_code": None,
            "ok": False,
            "payload": {},
            "text": "",
            "url": url,
            "error": f"{type(exc).__name__}:{exc}",
        }


def _extract_last_heartbeat(state: dict[str, Any]) -> str:
    return str(state.get("last_heartbeat", "") or "")


def _provider_health_is_ok(provider_health: dict[str, Any]) -> bool:
    if not provider_health:
        return False
    allowed = {"ok", "shadow_only"}
    seen = False
    for item in provider_health.values():
        if not isinstance(item, dict):
            continue
        if not any(key in item for key in ("status", "role", "provider")):
            continue
        seen = True
        status = str(item.get("status") or "").strip().lower()
        if status not in allowed:
            return False
    return seen


def _feature_serving_summary(ready: dict[str, Any]) -> dict[str, Any]:
    blocker_reasons = list(ready.get("feature_blocker_reasons") or ready.get("featureBlockerReasons") or [])
    online_ready = bool(ready.get("feature_online_ready", ready.get("featureOnlineReady", False)))
    data_fresh = bool(ready.get("feature_data_fresh", ready.get("featureDataFresh", False)))
    bar_status = str(
        ready.get("feature_bar_status", ready.get("featureBarStatus", ""))
        or ("fresh" if data_fresh else "stale" if online_ready else "missing")
    ).strip().lower()
    blocker_source = str(ready.get("feature_blocker_source", ready.get("featureBlockerSource", "")) or "").strip().lower()
    return {
        "online_ready": online_ready,
        "data_fresh": data_fresh,
        "push_backlog": int(ready.get("feature_push_backlog", ready.get("featurePushBacklog", 0)) or 0),
        "push_backlog_ok": bool(ready.get("feature_push_backlog_ok", ready.get("featurePushBacklogOk", False))),
        "blocker_reason": str(ready.get("feature_blocker_reason", ready.get("featureBlockerReason", "")) or ""),
        "blocker_source": blocker_source,
        "blocker_reasons": [str(item) for item in blocker_reasons if str(item).strip()],
        "bar_status": bar_status,
        "serving_reason": str(ready.get("feature_serving_reason", ready.get("featureServingReason", "")) or ""),
        "serving_source": str(ready.get("feature_serving_source", ready.get("featureServingSource", "")) or ""),
    }


def _normalize_command_name(command: str) -> str:
    return str(command or "").strip().upper()


def _is_paper_safe_command(command: str) -> bool:
    return _normalize_command_name(command) == "INFO"


def _paper_boundary_summary(*, ready: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    provider_roles = dict(ready.get("provider_roles") or {})
    provider_health = dict(ready.get("provider_health") or {})
    execution_role = str(provider_roles.get("execution_provider") or "").strip().lower()
    execution_provider_health = dict(provider_health.get("execution_provider") or {})
    execution_provider_name = str(
        execution_provider_health.get("provider")
        or execution_provider_health.get("name")
        or execution_role
        or ""
    ).strip().lower()
    paper_execution = dict(state.get("paper_execution") or state.get("paperExecution") or {})
    paper_execution_provider = str(paper_execution.get("execution_provider") or "").strip().lower()
    paper_execution_mode = str(paper_execution.get("agent_mode") or "").strip().lower()
    paper_execution_enabled = bool(paper_execution.get("enabled", False))
    ok = bool(
        paper_execution_enabled
        and (
            execution_role == "paper"
            or execution_provider_name == "paper"
            or paper_execution_provider == "paper"
            or paper_execution_mode == "paper"
        )
    )
    return {
        "ok": bool(ok),
        "execution_provider_role": execution_role,
        "execution_provider_name": execution_provider_name,
        "paper_execution_provider": paper_execution_provider,
        "paper_execution_mode": paper_execution_mode,
        "paper_execution_enabled": paper_execution_enabled,
    }


def _runtime_startup_summary(ready: dict[str, Any], *, runtime_stall_secs: float) -> tuple[bool, list[str], dict[str, Any]]:
    runtime_status = str(ready.get("runtime_status") or "").strip().lower()
    runtime_phase = str(ready.get("runtime_phase") or "").strip().lower()
    runtime_phase_pair = str(ready.get("runtime_phase_pair") or "").strip().upper()
    runtime_last_progress_age_secs = ready.get("runtime_last_progress_age_secs")
    runtime_failure_reason = str(ready.get("runtime_failure_reason") or "").strip()
    runtime_reason = str(ready.get("reason") or "").strip().lower()

    findings: list[str] = []
    stalled_in_model_load = False
    try:
        last_progress_age = None if runtime_last_progress_age_secs in (None, "") else float(runtime_last_progress_age_secs)
    except Exception:
        last_progress_age = None

    if runtime_failure_reason:
        findings.append(f"runtime_startup_failure_reason:{runtime_failure_reason}")
    if runtime_status in {"failed", "stalled"}:
        findings.append(f"runtime_status:{runtime_status}")
    if runtime_phase == "model_load":
        findings.append(f"runtime_phase:model_load")
        if last_progress_age is not None and last_progress_age > float(runtime_stall_secs):
            stalled_in_model_load = True
            findings.append(
                "runtime_model_load_stalled:"
                + f"age_secs={last_progress_age:.1f}"
                + f":threshold_secs={float(runtime_stall_secs):.1f}"
            )
    if runtime_reason in {"runtime_startup_failed", "runtime_startup_stalled"}:
        findings.append(f"bridge_reason:{runtime_reason}")

    runtime_ok = bool(
        not runtime_failure_reason
        and runtime_status not in {"failed", "stalled"}
        and runtime_reason not in {"runtime_startup_failed", "runtime_startup_stalled"}
        and not stalled_in_model_load
    )
    summary = {
        "status": runtime_status,
        "phase": runtime_phase,
        "phase_pair": runtime_phase_pair,
        "last_progress_age_secs": runtime_last_progress_age_secs,
        "failure_reason": runtime_failure_reason,
        "reason": runtime_reason,
        "stalled_in_model_load": stalled_in_model_load,
    }
    return runtime_ok, findings, summary


def _apply_ready_snapshot(
    result: dict[str, Any],
    ready: dict[str, Any],
    *,
    require_feature_serving: bool,
    runtime_stall_secs: float,
) -> None:
    result["details"]["ready"] = ready
    ready_status_ok = str(ready.get("status", "")).lower() == "ok"
    ready_reason = str(ready.get("reason", "")).lower()
    result["checks"]["ready_ok"] = bool(ready_status_ok and ready_reason == "ok")
    result["checks"]["mt4_fresh"] = bool(ready.get("mt4_fresh", False))
    result["checks"]["ticks_fresh"] = bool(ready.get("ticks_fresh", False))
    feature_serving = _feature_serving_summary(ready)
    provider_health = dict(ready.get("provider_health") or {})
    provider_roles = dict(ready.get("provider_roles") or {})
    result["details"]["provider_health"] = provider_health
    result["details"]["provider_roles"] = provider_roles
    result["details"]["feature_serving"] = feature_serving
    result["checks"]["provider_health_ok"] = _provider_health_is_ok(provider_health)
    result["checks"]["feature_ready_ok"] = bool(
        feature_serving["online_ready"]
        and feature_serving["data_fresh"]
        and feature_serving["push_backlog_ok"]
        and not feature_serving["blocker_reason"]
    )
    runtime_ok, runtime_findings, runtime_summary = _runtime_startup_summary(ready, runtime_stall_secs=runtime_stall_secs)
    result["checks"]["runtime_startup_ok"] = bool(runtime_ok)
    result["details"]["runtime_startup"] = runtime_summary

    findings = list(result.get("findings") or [])
    findings = [item for item in findings if not str(item).startswith(("feature_", "ready_", "runtime_", "bridge_reason:"))]
    findings.extend(runtime_findings)
    if not feature_serving["online_ready"]:
        findings.append("feature_serving_missing")
    elif not feature_serving["data_fresh"]:
        findings.append("feature_serving_stale")
    if feature_serving["blocker_source"]:
        findings.append(f"feature_blocker_source:{feature_serving['blocker_source']}")
    if feature_serving["bar_status"] and feature_serving["bar_status"] != "fresh":
        findings.append(f"feature_bar_status:{feature_serving['bar_status']}")
    if not feature_serving["online_ready"] and feature_serving["blocker_source"] == "feature_serving":
        findings.append("feature_dependency_missing:feature_serving")
    if not feature_serving["push_backlog_ok"]:
        findings.append(f"feature_push_backlog:{feature_serving['push_backlog']}")
    if feature_serving["blocker_reason"]:
        findings.append(f"feature_blocker_reason:{feature_serving['blocker_reason']}")
    if not ready_status_ok:
        findings.append(f"ready_status:{str(ready.get('status', '')).strip().lower() or 'unknown'}")
    if ready_reason and ready_reason != "ok":
        findings.append(f"ready_reason:{ready_reason}")
    if require_feature_serving and not bool(result["checks"]["feature_ready_ok"]):
        findings.append("feature_serving_not_ready")
    result["findings"] = findings


def _apply_health_snapshot(result: dict[str, Any], health: dict[str, Any]) -> None:
    status_ok = str(health.get("status", "")).lower() == "ok"
    system_connected = str(health.get("system_status", "")).lower() == "connected"
    result["checks"]["health_ok"] = bool(status_ok and system_connected)
    result["details"]["health"] = health


def run(args: argparse.Namespace) -> int:
    base_url = str(args.base_url).rstrip("/")
    dashboard_url = str(getattr(args, "dashboard_url", "") or "").rstrip("/")
    timeout_secs = float(max(5.0, args.timeout_secs))
    poll_secs = float(max(0.2, args.poll_secs))
    min_heartbeat_advances = int(max(1, args.min_heartbeat_advances))
    min_observation_secs = float(max(0.0, float(getattr(args, "min_observation_secs", 0.0) or 0.0)))
    require_ticks = bool(args.require_ticks)
    require_acked_command = bool(args.require_acked_command)
    require_feature_serving = bool(getattr(args, "require_feature_serving", False))
    require_paper_boundary = bool(getattr(args, "require_paper_boundary", False))
    paper_safe_command_check = bool(getattr(args, "paper_safe_command_check", False))
    command_lots = float(max(0.0, float(getattr(args, "lots", 0.0) or 0.0)))
    keepalive_heartbeat_secs = float(max(0.0, float(getattr(args, "keepalive_heartbeat_secs", 0.0) or 0.0)))
    runtime_stall_secs = float(max(1.0, float(getattr(args, "runtime_stall_secs", 60.0) or 60.0)))

    started = time.time()
    deadline = started + timeout_secs
    observe_until = started + min_observation_secs

    result: dict[str, Any] = {
        "generated_at": _now_iso(),
        "base_url": base_url,
        "timeout_secs": timeout_secs,
        "poll_secs": poll_secs,
        "min_observation_secs": min_observation_secs,
        "dashboard_url": dashboard_url,
        "runtime_stall_secs": runtime_stall_secs,
        "keepalive_heartbeat_secs": keepalive_heartbeat_secs,
        "require_ticks": require_ticks,
        "require_acked_command": require_acked_command,
        "require_paper_boundary": require_paper_boundary,
        "paper_safe_command_check": paper_safe_command_check,
        "command_lots": command_lots,
        "checks": {
            "health_ok": False,
            "ready_ok": False,
            "runtime_startup_ok": False,
            "dashboard_state_ok": (not bool(dashboard_url)),
            "provider_health_ok": False,
            "mt4_fresh": False,
            "ticks_fresh": False,
            "feature_ready_ok": (not require_feature_serving),
            "heartbeat_advances": False,
            "observation_window_met": (min_observation_secs <= 0.0),
            "reports_present": False,
            "ticks_present": (not require_ticks),
            "command_acked": (not require_acked_command),
            "paper_boundary_ok": (not require_paper_boundary),
        },
        "details": {
            "heartbeat_values": [],
            "report_sample": [],
            "tick_symbols": [],
            "command_id": "",
            "command_statuses": [],
            "observation_elapsed_secs": 0.0,
            "provider_health": {},
            "provider_roles": {},
            "paper_boundary": {},
            "ready": {},
            "state": {},
            "dashboard_state": {},
            "runtime_startup": {},
        },
        "findings": [],
        "errors": [],
        "passed": False,
    }

    try:
        health = _fetch_json(base_url, "/v2/health", timeout=3.0)
        _apply_health_snapshot(result, health)
    except Exception as exc:
        result["errors"].append(f"health_error:{type(exc).__name__}:{exc}")

    try:
        ready = _fetch_json(base_url, "/v2/ready", timeout=3.0)
        _apply_ready_snapshot(
            result,
            ready,
            require_feature_serving=require_feature_serving,
            runtime_stall_secs=runtime_stall_secs,
        )
    except Exception as exc:
        result["errors"].append(f"ready_error:{type(exc).__name__}:{exc}")

    keepalive_count = 0
    next_keepalive = time.time() if keepalive_heartbeat_secs > 0.0 else None

    if dashboard_url:
        dashboard_state = _fetch_dashboard_state(dashboard_url)
        result["details"]["dashboard_state"] = dashboard_state
        dashboard_status = dashboard_state.get("status_code")
        dashboard_payload = dict(dashboard_state.get("payload") or {})
        if dashboard_payload:
            result["details"]["dashboard_state_payload"] = dashboard_payload
        result["checks"]["dashboard_state_ok"] = bool(dashboard_state.get("ok")) and int(dashboard_status or 0) == 200
        if dashboard_status == 503:
            result["findings"].append("dashboard_state_http_503")
        elif dashboard_state.get("checked") and not bool(dashboard_state.get("ok")):
            result["findings"].append(f"dashboard_state_http_{dashboard_status or 'error'}")
        if dashboard_state.get("error"):
            result["errors"].append(f"dashboard_error:{dashboard_state['error']}")

    last_hb = ""
    advances = 0
    while time.time() < deadline:
        now = time.time()
        try:
            if keepalive_heartbeat_secs > 0.0 and next_keepalive is not None and now >= next_keepalive:
                try:
                    _post_keepalive_report(base_url, source="live_stack_check", timeout=3.0)
                    keepalive_count += 1
                except Exception as exc:
                    result["errors"].append(f"keepalive_error:{type(exc).__name__}:{exc}")
                next_keepalive = now + keepalive_heartbeat_secs
            state = _fetch_json(base_url, "/v2/state", timeout=3.0)
            result["details"]["state"] = state
            hb = _extract_last_heartbeat(state)
            if hb:
                result["details"]["heartbeat_values"].append(hb)
            if hb and last_hb and hb != last_hb:
                advances += 1
            if hb:
                last_hb = hb
            if advances >= min_heartbeat_advances and now >= observe_until:
                break
        except Exception as exc:
            result["errors"].append(f"state_error:{type(exc).__name__}:{exc}")
        time.sleep(poll_secs)

    elapsed = float(max(0.0, time.time() - started))
    result["details"]["observation_elapsed_secs"] = elapsed
    result["details"]["heartbeat_keepalive_count"] = int(keepalive_count)
    result["checks"]["heartbeat_advances"] = bool(advances >= min_heartbeat_advances)
    result["checks"]["observation_window_met"] = bool(elapsed >= min_observation_secs)

    try:
        refreshed_health = _fetch_json(base_url, "/v2/health", timeout=3.0)
        _apply_health_snapshot(result, refreshed_health)
    except Exception as exc:
        result["errors"].append(f"health_refresh_error:{type(exc).__name__}:{exc}")

    try:
        refreshed_ready = _fetch_json(base_url, "/v2/ready", timeout=3.0)
        _apply_ready_snapshot(
            result,
            refreshed_ready,
            require_feature_serving=require_feature_serving,
            runtime_stall_secs=runtime_stall_secs,
        )
    except Exception as exc:
        result["errors"].append(f"ready_refresh_error:{type(exc).__name__}:{exc}")

    try:
        reports_payload = _fetch_json(base_url, "/v2/reports?limit=20", timeout=3.0)
        reports = list(reports_payload.get("reports", []) or [])
        sample = [str(row.get("report_text", "")) for row in reports[:5]]
        result["details"]["report_sample"] = sample
        result["checks"]["reports_present"] = any("HEARTBEAT" in txt for txt in sample) or len(reports) > 0
    except Exception as exc:
        result["errors"].append(f"reports_error:{type(exc).__name__}:{exc}")

    if require_ticks:
        try:
            ticks = _fetch_json(base_url, "/v2/market/ticks", timeout=3.0)
            symbols = sorted([str(k) for k in ticks.keys() if str(k).strip()]) if isinstance(ticks, dict) else []
            result["details"]["tick_symbols"] = symbols
            result["checks"]["ticks_present"] = bool(len(symbols) > 0)
            if require_ticks and not symbols:
                result["findings"].append("ticks_missing")
        except Exception as exc:
            result["errors"].append(f"ticks_error:{type(exc).__name__}:{exc}")

    if require_paper_boundary:
        boundary = _paper_boundary_summary(
            ready=dict(result["details"].get("ready") or {}),
            state=dict(result["details"].get("state") or {}),
        )
        result["details"]["paper_boundary"] = boundary
        result["checks"]["paper_boundary_ok"] = bool(boundary.get("ok", False))
        if not bool(boundary.get("ok", False)):
            result["findings"].append(
                "paper_boundary_not_verified:"
                + f"role={boundary.get('execution_provider_role') or 'unknown'}:"
                + f"provider={boundary.get('paper_execution_provider') or boundary.get('execution_provider_name') or 'unknown'}"
            )

    if require_acked_command:
        command_name = _normalize_command_name(getattr(args, "command", ""))
        if paper_safe_command_check and not _is_paper_safe_command(command_name):
            result["findings"].append(f"paper_safe_command_blocked:{command_name or 'unknown'}")
            result["errors"].append("paper_safe_command_check_requires_INFO")
        elif require_paper_boundary and not bool(result["checks"].get("paper_boundary_ok")):
            result["errors"].append("paper_boundary_not_verified")
            _finalize_and_write(result, args.out)
            return 2
        elif command_name in {"BUY", "SELL"} and command_lots <= 0.0:
            result["errors"].append("command_lots_required_for_entry_probe")
            _finalize_and_write(result, args.out)
            return 2
        else:
            cmd_id = f"stackcheck-{uuid.uuid4().hex[:16]}"
            result["details"]["command_id"] = cmd_id
            payload = {
                "command_id": cmd_id,
                "cmd": command_name,
                "symbol": str(args.symbol).strip().upper(),
                "intent": "PAPER_SAFE_ADMISSION_CHECK" if paper_safe_command_check else "CONTROL",
            }
            if command_name in {"BUY", "SELL"} and command_lots > 0.0:
                payload["lots"] = float(command_lots)
            try:
                _post_json(base_url, "/v2/commands", payload=payload, timeout=3.0)
            except Exception as exc:
                result["errors"].append(f"command_post_error:{type(exc).__name__}:{exc}")
                _finalize_and_write(result, args.out)
                return 2

            ack_deadline = time.time() + float(max(5.0, args.command_timeout_secs))
            while time.time() < ack_deadline:
                try:
                    events_payload = _fetch_json(base_url, f"/v2/commands/events?command_id={cmd_id}&limit=50", timeout=3.0)
                    events = list(events_payload.get("events", []) or [])
                    statuses = [
                        str(row.get("event_status") or row.get("status") or "").strip().lower()
                        for row in events
                        if str(row.get("event_status") or row.get("status") or "").strip()
                    ]
                    result["details"]["command_statuses"] = statuses
                    if "acked" in statuses:
                        result["checks"]["command_acked"] = True
                        break
                except Exception as exc:
                    result["errors"].append(f"command_events_error:{type(exc).__name__}:{exc}")
                time.sleep(poll_secs)

    if bool(result["checks"].get("ready_ok")) and not bool(result["checks"].get("mt4_fresh")):
        result["findings"].append("mt4_not_fresh")
    if bool(result["checks"].get("ready_ok")) and not bool(result["checks"].get("ticks_fresh")):
        result["findings"].append("ticks_not_fresh")

    checks = dict(result.get("checks", {}))
    if not require_feature_serving:
        checks.pop("feature_ready_ok", None)
    result["passed"] = all(bool(v) for v in checks.values())
    _finalize_and_write(result, args.out)
    return 0 if bool(result["passed"]) else 2


def _finalize_and_write(result: dict[str, Any], out_path: str) -> None:
    result["finished_at"] = _now_iso()
    print(json.dumps(result, indent=2, sort_keys=True))
    out_txt = str(out_path or "").strip()
    if out_txt:
        out = Path(out_txt)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Verify live v2 stack health, runtime startup progress, dashboard state, and command ACK lifecycle.")
    ap.add_argument("--base-url", default="http://127.0.0.1:58710")
    ap.add_argument("--dashboard-url", default=os.environ.get("FXSTACK_DASHBOARD_URL", ""))
    ap.add_argument("--timeout-secs", type=float, default=180.0)
    ap.add_argument("--poll-secs", type=float, default=2.0)
    ap.add_argument("--min-heartbeat-advances", type=int, default=2)
    ap.add_argument("--min-observation-secs", type=float, default=0.0)
    ap.add_argument("--runtime-stall-secs", type=float, default=60.0)
    ap.add_argument(
        "--keepalive-heartbeat-secs",
        type=float,
        default=0.0,
        help="If > 0, POST lightweight heartbeat reports at this cadence during the observation window.",
    )
    ap.add_argument("--require-ticks", action="store_true", default=False)
    ap.add_argument("--require-acked-command", action="store_true", default=False)
    ap.add_argument("--require-feature-serving", action="store_true", default=False)
    ap.add_argument(
        "--require-paper-boundary",
        action="store_true",
        default=False,
        help="Refuse to post a command probe unless state/readiness prove the execution boundary is paper-only.",
    )
    ap.add_argument(
        "--paper-safe-command-check",
        action="store_true",
        default=False,
        help="Restrict the command admission probe to a paper-safe INFO command.",
    )
    ap.add_argument("--command", default="INFO")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--lots", type=float, default=0.0)
    ap.add_argument("--command-timeout-secs", type=float, default=120.0)
    ap.add_argument("--out", default="")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
