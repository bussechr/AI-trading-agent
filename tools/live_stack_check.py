from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import os


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


def _extract_last_heartbeat(state: dict[str, Any]) -> str:
    return str(state.get("last_heartbeat", "") or "")


def run(args: argparse.Namespace) -> int:
    base_url = str(args.base_url).rstrip("/")
    timeout_secs = float(max(5.0, args.timeout_secs))
    poll_secs = float(max(0.2, args.poll_secs))
    min_heartbeat_advances = int(max(1, args.min_heartbeat_advances))
    min_observation_secs = float(max(0.0, float(getattr(args, "min_observation_secs", 0.0) or 0.0)))
    require_ticks = bool(args.require_ticks)
    require_acked_command = bool(args.require_acked_command)

    started = time.time()
    deadline = started + timeout_secs
    observe_until = started + min_observation_secs

    result: dict[str, Any] = {
        "generated_at": _now_iso(),
        "base_url": base_url,
        "timeout_secs": timeout_secs,
        "poll_secs": poll_secs,
        "min_observation_secs": min_observation_secs,
        "require_ticks": require_ticks,
        "require_acked_command": require_acked_command,
        "checks": {
            "health_ok": False,
            "heartbeat_advances": False,
            "observation_window_met": (min_observation_secs <= 0.0),
            "reports_present": False,
            "ticks_present": (not require_ticks),
            "command_acked": (not require_acked_command),
        },
        "details": {
            "heartbeat_values": [],
            "report_sample": [],
            "tick_symbols": [],
            "command_id": "",
            "command_statuses": [],
            "observation_elapsed_secs": 0.0,
        },
        "errors": [],
        "passed": False,
    }

    try:
        health = _fetch_json(base_url, "/v2/health", timeout=3.0)
        status_ok = str(health.get("status", "")).lower() == "ok"
        system_connected = str(health.get("system_status", "")).lower() == "connected"
        result["checks"]["health_ok"] = bool(status_ok and system_connected)
        result["details"]["health"] = health
    except Exception as exc:
        result["errors"].append(f"health_error:{type(exc).__name__}:{exc}")
        _finalize_and_write(result, args.out)
        return 2

    last_hb = ""
    advances = 0
    while time.time() < deadline:
        now = time.time()
        try:
            state = _fetch_json(base_url, "/v2/state", timeout=3.0)
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
    result["checks"]["heartbeat_advances"] = bool(advances >= min_heartbeat_advances)
    result["checks"]["observation_window_met"] = bool(elapsed >= min_observation_secs)

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
        except Exception as exc:
            result["errors"].append(f"ticks_error:{type(exc).__name__}:{exc}")

    if require_acked_command:
        cmd_id = f"stackcheck-{uuid.uuid4().hex[:16]}"
        result["details"]["command_id"] = cmd_id
        payload = {
            "command_id": cmd_id,
            "cmd": str(args.command).strip().upper(),
            "symbol": str(args.symbol).strip().upper(),
            "intent": "CONTROL",
        }
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
                statuses = [str(row.get("status", "")).lower() for row in events]
                result["details"]["command_statuses"] = statuses
                if "acked" in statuses:
                    result["checks"]["command_acked"] = True
                    break
            except Exception as exc:
                result["errors"].append(f"command_events_error:{type(exc).__name__}:{exc}")
            time.sleep(poll_secs)

    result["passed"] = all(bool(v) for v in dict(result.get("checks", {})).values())
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
    ap = argparse.ArgumentParser(description="Verify live v2 stack health, heartbeat progression, and command ACK lifecycle.")
    ap.add_argument("--base-url", default="http://127.0.0.1:58710")
    ap.add_argument("--timeout-secs", type=float, default=180.0)
    ap.add_argument("--poll-secs", type=float, default=2.0)
    ap.add_argument("--min-heartbeat-advances", type=int, default=2)
    ap.add_argument("--min-observation-secs", type=float, default=0.0)
    ap.add_argument("--require-ticks", action="store_true", default=False)
    ap.add_argument("--require-acked-command", action="store_true", default=False)
    ap.add_argument("--command", default="CLOSE_ALL")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--command-timeout-secs", type=float, default=120.0)
    ap.add_argument("--out", default="")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
