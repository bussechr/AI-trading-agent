"""Pinpoint why EURUSD entries are ranked out: open-position count vs remaining_slots,
and the latest decision's allocator metadata. Reads the live runtime via /v2/ready."""
from __future__ import annotations

import json
import urllib.request

URL = "http://127.0.0.1:58710/v2/ready"

WANT = {
    "max_total_positions", "max_new_entries_per_cycle", "use_portfolio_ranking",
    "adaptive_shadow_remaining_slots", "adaptive_shadow_max_new_entries",
    "allocator_candidate_count", "allocator_selected_count", "allocator_ranked_out_count",
    "allocator_rejection_reason", "allocator_selected", "allocator_rank", "allocator_score",
    "sleeve_budget_target", "sleeve_budget_used",
    "open_position_count", "open_positions", "position_count",
    "adaptive_shadow_dominant_rejection_reason", "adaptive_shadow_rejection_reason_counts",
}


def walk(obj, out, depth=0, path=""):
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in WANT and isinstance(v, (str, int, float, bool)) and path + "/" + k not in out:
                out[path + "/" + k] = v
            walk(v, out, depth + 1, path + "/" + str(k))
    elif isinstance(obj, list):
        # surface list lengths for position-like keys
        if path.split("/")[-1] in {"open_positions", "positions", "active_positions"}:
            out[path + " (len)"] = len(obj)
        for i, it in enumerate(obj[:30]):
            walk(it, out, depth + 1, path + f"[{i}]")


def main():
    with urllib.request.urlopen(URL, timeout=15) as r:
        data = json.load(r)
    found = {}
    walk(data, found)
    print("=== matched fields (path = value) ===")
    for k in sorted(found.keys()):
        print(f"  {k} = {found[k]}")

    # explicit: try common position containers
    print("\n=== position containers ===")
    for key in ("open_positions", "positions", "active_positions", "portfolio_positions",
                "live_positions", "open_position_count", "position_count"):
        v = data.get(key)
        if v is not None:
            if isinstance(v, list):
                print(f"  {key}: list len={len(v)}")
                for it in v[:10]:
                    if isinstance(it, dict):
                        print("     ", {kk: it.get(kk) for kk in ("pair", "symbol", "side", "lots", "ticket", "status", "sleeve") if kk in it})
            else:
                print(f"  {key} = {v}")

    # top-level keys for orientation
    print("\n=== top-level keys (sample) ===")
    print(", ".join(sorted([k for k in data.keys() if "posit" in k.lower() or "decis" in k.lower() or "slot" in k.lower() or "alloc" in k.lower()])))


if __name__ == "__main__":
    main()
