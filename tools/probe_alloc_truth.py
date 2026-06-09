"""Ground-truth the allocator block: runtime's own adaptive-shadow diag + the
settings the runtime loaded + latest EURUSD decision allocator metadata."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path("D:/Development/Trading Agent")
sys.path.insert(0, str(REPO / "fx-quant-stack" / "src"))

from fxstack.runtime.service import RuntimeService  # noqa: E402

DB = "postgresql+psycopg://fx:fx@localhost:5432/fxstack"

KEYS = {
    "adaptive_shadow_remaining_slots", "adaptive_shadow_max_new_entries",
    "allocator_candidate_count", "allocator_selected_count", "allocator_ranked_out_count",
    "adaptive_shadow_dominant_rejection_reason", "adaptive_shadow_rejection_reason_counts",
    "max_total_positions", "max_new_entries_per_cycle", "use_portfolio_ranking",
    "open_position_count", "remaining_slots",
}


def walk(obj, out, depth=0, path=""):
    if depth > 9:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in KEYS and isinstance(v, (str, int, float, bool, dict)) and not isinstance(v, bool) or (k in KEYS and isinstance(v, bool)):
                key = path + "/" + k
                if key not in out:
                    out[key] = v if not isinstance(v, dict) else json.dumps(v)[:160]
            walk(v, out, depth + 1, path + "/" + str(k))
    elif isinstance(obj, list):
        for i, it in enumerate(obj[:20]):
            walk(it, out, depth + 1, path + f"[{i}]")


def main():
    svc = RuntimeService(database_url=DB)

    # 1) Settings the runtime would load from THIS env (no overrides => defaults)
    try:
        from fxstack.settings import Settings
        s = Settings()
        print("=== Settings() in this shell (no runtime env) ===")
        print(f"  max_total_positions       = {s.max_total_positions}")
        print(f"  max_pair_positions        = {getattr(s,'max_pair_positions',None)}")
        print(f"  max_new_entries_per_cycle = {s.max_new_entries_per_cycle}")
        print(f"  use_portfolio_ranking     = {s.use_portfolio_ranking}")
        print(f"  adaptive_shadow_enabled   = {getattr(s,'adaptive_shadow_enabled',None)}")
    except Exception as e:
        print(f"  settings load error: {e}")

    state = svc.get_state()
    print("\n=== runtime state: matched allocator/slot keys ===")
    found = {}
    walk(state, found)
    for k in sorted(found.keys()):
        print(f"  {k} = {found[k]}")

    # latest EURUSD decision metadata allocator fields
    print("\n=== latest EURUSD decision allocator metadata ===")
    try:
        decs = state.get("agent_decisions") or state.get("decisions") or []
        eur = [d for d in decs if str((d.get("symbol") or (d.get("metadata") or {}).get("pair") or "")).upper() == "EURUSD"]
        target = (eur or decs)[:2]
        for d in target:
            meta = dict(d.get("metadata") or {})
            print(f"  symbol={d.get('symbol')} action={d.get('action')} "
                  f"baseline_intent={(meta.get('baseline_action') or {}).get('intent') if isinstance(meta.get('baseline_action'),dict) else meta.get('baseline_intent')}")
            for kk in ("allocator_selected", "allocator_rejection_reason", "allocator_rank", "allocator_score",
                       "sleeve_budget_target", "sleeve_budget_used", "adaptive_shadow_remaining_slots",
                       "adaptive_shadow_would_trade", "adaptive_shadow_rejection_reason", "entry_ready",
                       "playbook", "adaptive_playbook_score", "trade_prob"):
                if kk in meta:
                    print(f"      {kk} = {meta.get(kk)}")
    except Exception as e:
        print(f"  decision read error: {e}")


if __name__ == "__main__":
    main()
