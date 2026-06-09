"""Watch the EURUSD live path: report trade_prob/readiness each cycle and alert when a
governed_live command is submitted (and its MT4 ack). Run in the background.

Confirms the live wiring is firing: when EURUSD's trade_prob clears the entry bar and
the live gates pass, a governed_live command is enqueued for the MT4 demo EA.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path("D:/Development/Trading Agent")
sys.path.insert(0, str(REPO / "fx-quant-stack" / "src"))

from fxstack.runtime.service import RuntimeService  # noqa: E402
from sqlalchemy import text  # noqa: E402

DB = "postgresql+psycopg://fx:fx@localhost:5432/fxstack"
MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0


def _eurusd_decision(st: dict) -> dict:
    dec = st.get("agent_decisions") or {}
    e = None
    if isinstance(dec, dict):
        e = dec.get("EURUSD")
    elif isinstance(dec, list):
        e = next((d for d in dec if str(d.get("symbol") or d.get("pair") or "").upper() == "EURUSD"), None)
    if not e:
        return {}
    m = dict(e.get("metadata") or e.get("metadata_json") or {})
    return {
        "action": e.get("action"), "side": e.get("side"),
        "trade_prob": m.get("trade_prob"), "entry_prob": m.get("entry_prob"),
        "edge_bps": m.get("expected_edge_bps"), "spread_bps": m.get("spread_bps"),
        "entry_ready": m.get("entry_ready"), "reject": m.get("rejection_reason"),
        "live_mode": m.get("orchestration_live_mode"), "cmd_source": m.get("orchestration_live_command_source"),
        "fallback": m.get("orchestration_live_fallback_reason"),
    }


def main() -> None:
    svc = RuntimeService(database_url=DB)
    eng = svc.store._engine if hasattr(svc.store, "_engine") else None
    from sqlalchemy import create_engine
    eng = create_engine(DB)
    start = time.time()
    seen_cmds: set[str] = set()
    best_prob = 0.0
    print(f"[watch] EURUSD live watcher for {MINUTES} min", flush=True)
    while time.time() - start < MINUTES * 60:
        st = svc.get_state()
        d = _eurusd_decision(st)
        tp = d.get("trade_prob")
        if isinstance(tp, (int, float)):
            best_prob = max(best_prob, float(tp))
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] EURUSD tp={tp} entry_prob={d.get('entry_prob')} edge={d.get('edge_bps')} "
              f"spread={d.get('spread_bps')} ready={d.get('entry_ready')} reject={d.get('reject')} "
              f"live={d.get('live_mode')} src={d.get('cmd_source')} (best_tp={round(best_prob,3)})", flush=True)
        # any fresh EURUSD commands?
        with eng.connect() as c:
            rows = list(c.execute(text(
                "select command_id, status, cmd, intent, created_at, orchestration_meta_json "
                "from commands where upper(symbol)='EURUSD' and created_at > :t order by created_at desc limit 10"
            ), {"t": time.time() - 120}))
        fired = False
        for r in rows:
            cid = str(r.command_id)
            if cid in seen_cmds:
                continue
            seen_cmds.add(cid)
            meta = r.orchestration_meta_json or {}
            if isinstance(meta, str):
                try: meta = json.loads(meta)
                except Exception: meta = {}
            src = str((meta or {}).get("command_source") or (meta or {}).get("agent_mode") or "")
            print(f"  *** NEW EURUSD COMMAND cid={cid[:12]} status={r.status} cmd={r.cmd} intent={r.intent} source={src} ***", flush=True)
            fired = True
        if fired:
            print("[watch] EURUSD order detected -> exiting early to report.", flush=True)
            break
        time.sleep(15)
    print(f"[watch] done. best EURUSD trade_prob seen = {round(best_prob,3)}", flush=True)


if __name__ == "__main__":
    main()
