"""Installed, fail-closed control for disabling all broker command egress."""

from __future__ import annotations

import argparse
import json
from typing import Any

from fxstack.runtime.service import RuntimeService
from fxstack.settings import get_settings


def disable_and_verify(*, reason: str) -> dict[str, Any]:
    """Atomically disable egress, then independently verify committed state."""

    normalized_reason = str(reason or "operator_execution_egress_disable").strip()
    if not normalized_reason or len(normalized_reason) > 160:
        return {"ok": False, "error": "invalid_disable_reason"}
    try:
        service = RuntimeService(database_url=str(get_settings().database_url))
        result = service.disable_execution_egress(
            reason=normalized_reason,
            revoke_release=False,
        )
        state = service.get_state()
        metrics = service.get_metrics()
    except Exception as exc:
        return {
            "ok": False,
            "error": "execution_egress_disable_failed",
            "detail": f"{type(exc).__name__}:{exc}",
        }
    authority_status = str(
        dict(state.get("release_authority") or {}).get("status") or ""
    ).strip().lower()
    live = dict(dict(state.get("runtime_diag") or {}).get("orchestration_live") or {})
    pending_count = int(dict(metrics.get("pending") or {}).get("count") or 0)
    egress_disabled = state.get("execution_egress_enabled") is False
    production_authority_revoked = (
        live.get("runtime_enabled") is False
        and live.get("queue_kill_active") is True
    )
    queue_quarantined = pending_count == 0
    ok = bool(
        egress_disabled and production_authority_revoked and queue_quarantined
    )
    return {
        "ok": ok,
        "error": "" if ok else "execution_egress_disable_not_committed",
        "reason": normalized_reason,
        "execution_egress_enabled": state.get("execution_egress_enabled"),
        "release_authority_status": authority_status,
        "production_runtime_enabled": live.get("runtime_enabled"),
        "production_queue_kill_active": live.get("queue_kill_active"),
        "pending_command_count": pending_count,
        "quarantined_command_count": int(
            dict(result or {}).get("quarantined_command_count") or 0
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Disable production execution authority and quarantine broker commands before shutdown"
    )
    parser.add_argument("--reason", default="operator_stop_all")
    args = parser.parse_args()
    result = disable_and_verify(reason=str(args.reason))
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result.get("ok") is True else 2)


if __name__ == "__main__":
    main()


__all__ = ["disable_and_verify", "main"]
