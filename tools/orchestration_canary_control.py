from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


def _maybe_reexec_repo_python() -> None:
    """Re-exec under a repo-local venv python if the current interpreter lacks deps.

    Includes both POSIX (``bin/python``) and Windows (``Scripts/python.exe``)
    venv layouts, and ignores any candidate whose ``.exists()`` raises (e.g.
    a stale WSL symlink that Windows ``os.stat`` rejects with WinError 1920).
    """
    current = Path(sys.executable)
    current_abs = current if current.is_absolute() else current.resolve()
    candidates = [
        REPO_ROOT / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / "fx-quant-stack" / ".venv" / "bin" / "python",
        REPO_ROOT / ".venv" / "Scripts" / "python.exe",
        REPO_ROOT / ".venv" / "bin" / "python",
    ]

    def _safe_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    existing_candidates = [candidate for candidate in candidates if _safe_exists(candidate)]
    if any(current_abs == candidate or current == candidate for candidate in existing_candidates):
        return
    try:
        import pydantic  # type: ignore  # noqa: F401
        return
    except Exception:
        pass
    for candidate in existing_candidates:
        os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])


_maybe_reexec_repo_python()

from fxstack.runtime.service import RuntimeService
from fxstack.training.release_workflow import advance_canary_stage, release_status


def _patch_live_runtime_state(*, svc: RuntimeService, updates: dict[str, Any]) -> dict[str, Any]:
    state = svc.get_state()
    runtime_diag = dict(state.get("runtime_diag") or {})
    live = dict(runtime_diag.get("orchestration_live") or {})
    live.update(dict(updates or {}))
    runtime_diag["orchestration_live"] = live
    svc.patch_state({"runtime_diag": runtime_diag})
    return live


def _runtime_kill(*, database_url: str, pair: str, reason: str) -> dict[str, Any]:
    svc = RuntimeService(database_url=database_url)
    live = _patch_live_runtime_state(
        svc=svc,
        updates={
            "runtime_enabled": False,
            "last_kill_reason": str(reason or ""),
            "last_kill_at": float(time.time()),
        },
    )
    svc.record_governance_event(
        event_type="orchestration_live_runtime_killed",
        reason=str(reason or "runtime_killed"),
        payload={"pair": str(pair).upper(), "runtime_enabled": False},
    )
    return {"ok": True, "pair": str(pair).upper(), "reason": str(reason or ""), "orchestration_live": live}


def _queue_kill(*, database_url: str, pair: str, reason: str) -> dict[str, Any]:
    svc = RuntimeService(database_url=database_url)
    purged = int(svc.purge_pending_commands(reason=str(reason or "orchestration_live_queue_kill"), include_delivered=False))
    live = _patch_live_runtime_state(
        svc=svc,
        updates={
            "runtime_enabled": False,
            "queue_kill_active": True,
            "queue_kill_reason": str(reason or ""),
            "queue_killed_at": float(time.time()),
            "purged_command_count": int(purged),
        },
    )
    svc.record_governance_event(
        event_type="orchestration_live_queue_killed",
        reason=str(reason or "queue_killed"),
        payload={"pair": str(pair).upper(), "purged_command_count": int(purged)},
    )
    return {
        "ok": True,
        "pair": str(pair).upper(),
        "reason": str(reason or ""),
        "purged_command_count": int(purged),
        "orchestration_live": live,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Operate the Phase 6B orchestration live canary control plane.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="Show canary release and runtime status.")
    status_parser.add_argument("--pair", required=True)
    status_parser.add_argument("--database-url", required=True)
    status_parser.add_argument("--bundle-run-id", default="")

    runtime_kill_parser = subparsers.add_parser("runtime-kill", help="Disable governed live emission and fall back to baseline.")
    runtime_kill_parser.add_argument("--pair", required=True)
    runtime_kill_parser.add_argument("--database-url", required=True)
    runtime_kill_parser.add_argument("--reason", default="manual_runtime_kill")

    queue_kill_parser = subparsers.add_parser("queue-kill", help="Purge pending commands and disable governed live emission.")
    queue_kill_parser.add_argument("--pair", required=True)
    queue_kill_parser.add_argument("--database-url", required=True)
    queue_kill_parser.add_argument("--reason", default="manual_queue_kill")

    advance_parser = subparsers.add_parser("advance-stage", help="Advance the live canary ramp after a signed promotion pack.")
    advance_parser.add_argument("--pair", required=True)
    advance_parser.add_argument("--database-url", required=True)
    advance_parser.add_argument("--manifest-path", required=True)
    advance_parser.add_argument("--promotion-pack-path", required=True)
    advance_parser.add_argument("--author", required=True)
    advance_parser.add_argument("--bundle-run-id", default="")
    advance_parser.add_argument("--note", default="")

    args = parser.parse_args()

    if args.command == "status":
        result = release_status(pair=args.pair, database_url=args.database_url, bundle_run_id=args.bundle_run_id)
    elif args.command == "runtime-kill":
        result = _runtime_kill(database_url=args.database_url, pair=args.pair, reason=args.reason)
    elif args.command == "queue-kill":
        result = _queue_kill(database_url=args.database_url, pair=args.pair, reason=args.reason)
    else:
        result = advance_canary_stage(
            pair=args.pair,
            database_url=args.database_url,
            manifest_path=Path(args.manifest_path),
            promotion_pack_path=args.promotion_pack_path,
            author=args.author,
            bundle_run_id=args.bundle_run_id,
            note=args.note,
        )

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if bool(result.get("ok", True)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
