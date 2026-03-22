from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


def _parse_ts(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v = v / 1000.0
        return v if v > 0 else None
    txt = str(value).strip()
    if not txt:
        return None
    try:
        return datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _utc_now_ts() -> float:
    return float(datetime.now(timezone.utc).timestamp())


def main() -> None:
    ap = argparse.ArgumentParser(description="One-time runtime state remediation for stale heartbeat/equity snapshots")
    ap.add_argument("--apply", action="store_true", help="Write remediation patch to runtime state")
    ap.add_argument(
        "--clear-decisions",
        action="store_true",
        help="Also clear cached agent decisions and diagnostics",
    )
    ap.add_argument(
        "--stale-secs",
        type=float,
        default=0.0,
        help="Heartbeat stale threshold override (default: FXSTACK_BRIDGE_STALE_HEARTBEAT_SECS)",
    )
    args = ap.parse_args()

    s = get_settings()
    svc = RuntimeService(
        database_url=s.database_url,
        default_session_id=s.default_session_id,
        command_ttl_secs=s.command_ttl_secs,
        requeue_age_secs=s.startup_requeue_age_secs,
        db_connect_retries=s.db_connect_retries,
    )

    state = svc.get_state()
    stale_after = float(args.stale_secs) if float(args.stale_secs) > 0 else float(s.bridge_stale_heartbeat_secs)
    heartbeat_ts = _parse_ts(state.get("last_heartbeat"))
    heartbeat_age_secs = None if heartbeat_ts is None else max(0.0, _utc_now_ts() - heartbeat_ts)
    heartbeat_stale = heartbeat_age_secs is None or heartbeat_age_secs > stale_after

    equity_source = str(state.get("equity_source") or "").strip().lower()
    runtime_seed_equity = equity_source in {"runtime_seed", "runtime_constant", "seed"}

    patch: dict[str, Any] = {"__prune_stale__": True}
    actions: list[str] = []

    if heartbeat_stale:
        patch["last_heartbeat"] = None
        patch["system_status"] = "disconnected"
        actions.append("cleared_stale_heartbeat")

    if heartbeat_stale or runtime_seed_equity:
        patch["equity"] = 0.0
        patch["equity_source"] = "remediated_stale_or_seed"
        actions.append("cleared_equity")

    if args.clear_decisions:
        patch["agent_decisions"] = []
        patch["agent_diagnostics"] = {}
        patch["vol"] = 0.0
        actions.append("cleared_cached_decisions")

    summary = {
        "apply": bool(args.apply),
        "stale_after_secs": float(stale_after),
        "heartbeat_age_secs": heartbeat_age_secs,
        "heartbeat_stale": bool(heartbeat_stale),
        "equity_source": equity_source or "unknown",
        "runtime_seed_equity": bool(runtime_seed_equity),
        "actions": actions,
        "patch": patch,
    }

    if args.apply and actions:
        svc.patch_state(patch)
        summary["status"] = "applied"
    elif args.apply:
        summary["status"] = "no_changes_needed"
    else:
        summary["status"] = "dry_run"

    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
