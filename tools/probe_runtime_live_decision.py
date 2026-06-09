"""Read the RUNTIME's own self-reported state + latest EURUSD decision metadata.

The /v2/ready agent_mode is the BRIDGE process's settings, not the runtime's. This
reads what the runtime itself wrote: its orchestration diag (agent_mode from the
runtime's settings) and the latest EURUSD decision's execution/live metadata
(orchestration_live_mode, command_source, rollout_active/mode, blocking reasons).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path("D:/Development/Trading Agent")
sys.path.insert(0, str(REPO / "fx-quant-stack" / "src"))

from fxstack.runtime.service import RuntimeService  # noqa: E402

DB = "postgresql+psycopg://fx:fx@localhost:5432/fxstack"


def _walk_find(obj, keys: set[str], out: dict, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and k not in out:
                out[k] = v
            _walk_find(v, keys, out, depth + 1)
    elif isinstance(obj, list):
        for it in obj[:50]:
            _walk_find(it, keys, out, depth + 1)


def main() -> None:
    svc = RuntimeService(database_url=DB)
    state = svc.get_state()
    diag = dict(state.get("runtime_diag") or {})

    # Runtime's self-reported orchestration agent_mode (runner writes this from ITS settings)
    found: dict = {}
    _walk_find(diag, {"agent_mode", "runtime_mode", "rollout_mode", "rollout_active",
                      "orchestration_live_mode", "orchestration_live_command_source",
                      "live_governed_eligible_count", "live_governed_submitted_count",
                      "live_governed_blocked_count", "live_fallback_reason_counts"}, found)
    print("=== runtime self-reported (from runtime_diag) ===")
    for k in sorted(found.keys()):
        print(f"  {k} = {json.dumps(found[k], default=str)[:200]}")

    # rollout policy summary if the runtime published it
    rp = dict(diag.get("rollout_policy") or diag.get("canary_rollout_policy") or {})
    if rp:
        print("\n=== runtime rollout_policy diag ===")
        print(json.dumps(rp, indent=2, default=str)[:1200])

    # Latest EURUSD decision metadata
    try:
        decs = svc.store.get_recent_decisions(pair="EURUSD", limit=3) if hasattr(svc.store, "get_recent_decisions") else None
    except Exception:
        decs = None
    if decs:
        print("\n=== latest EURUSD decisions ===")
        for d in decs:
            meta = dict(d.get("metadata_json") or d.get("metadata") or {})
            print(f"  ts={d.get('ts_value') or d.get('created_at')} action={d.get('action')} "
                  f"live_mode={meta.get('orchestration_live_mode')} cmd_source={meta.get('orchestration_live_command_source')} "
                  f"fallback={meta.get('orchestration_live_fallback_reason')} entry_ready={meta.get('entry_ready')} "
                  f"reject={meta.get('rejection_reason')} trade_prob={meta.get('trade_prob')}")
    else:
        print("\n(no get_recent_decisions API; dumping orchestration_live govern decision from state)")
        ol = dict(diag.get("orchestration_live") or {})
        print(json.dumps(ol.get("governed_decision") or {}, indent=2, default=str)[:800])


if __name__ == "__main__":
    main()
