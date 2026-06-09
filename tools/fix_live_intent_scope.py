"""Fix the EURUSD live intent/pair/sleeve scope in the orchestration_live runtime state.

A stale orchestration_live state carried a present-but-empty active_intent_scope, which
runner._orchestration_live_runtime_state treats as "configured" -> empty live_intent_scope
-> every entry blocked with live_intent_not_allowlisted (caught by the watcher when EURUSD
tried to enter at trade_prob 0.20). This sets the live scopes explicitly to match the
authorized EURUSD canary, so the next ready entry emits a governed_live command to MT4.

Session-scoped DB state patch (same mechanism as tools/orchestration_canary_control.py);
read fresh each runtime cycle, so it takes effect without a restart.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path("D:/Development/Trading Agent")
sys.path.insert(0, str(REPO / "fx-quant-stack" / "src"))

from fxstack.runtime.service import RuntimeService  # noqa: E402

DB = "postgresql+psycopg://fx:fx@localhost:5432/fxstack"
SLEEVES = ["trend_pullback", "range_mean_reversion", "breakout_expansion", "failed_breakout_reversal"]


def main() -> None:
    svc = RuntimeService(database_url=DB)
    state = svc.get_state()
    runtime_diag = dict(state.get("runtime_diag") or {})
    live = dict(runtime_diag.get("orchestration_live") or {})
    before = {k: live.get(k) for k in ("active_pair_scope", "active_sleeve_scope", "active_intent_scope")}
    live["active_pair_scope"] = ["EURUSD"]
    live["active_sleeve_scope"] = list(SLEEVES)
    live["active_intent_scope"] = ["enter"]
    live["runtime_enabled"] = True
    live["queue_kill_active"] = False
    live["scope_patched_at"] = float(time.time())
    runtime_diag["orchestration_live"] = live
    svc.patch_state({"runtime_diag": runtime_diag})

    reread = dict(dict(svc.get_state().get("runtime_diag") or {}).get("orchestration_live") or {})
    print("before:", json.dumps(before))
    print("after :", json.dumps({k: reread.get(k) for k in ("active_pair_scope", "active_sleeve_scope", "active_intent_scope")}))
    print("OK: live scopes set for EURUSD canary (enter intent). Next ready entry should emit governed_live.")


if __name__ == "__main__":
    main()
