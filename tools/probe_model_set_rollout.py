"""Probe the runtime's active model-set metadata + derived rollout policy.

Read-only. Shows, for each active model set, whether the metadata carries a
phase5_gate_bundle, the per-gate passed flags, and the rollout policy the runtime
derives from it (mode/active/pair_allowlisted) — i.e. exactly what gates
governed_live at runner.py:500.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path("D:/Development/Trading Agent")
sys.path.insert(0, str(REPO / "fx-quant-stack" / "src"))

from fxstack.runtime.service import RuntimeService  # noqa: E402
from fxstack.runtime.runner import _resolve_main_runtime_rollout_policy  # noqa: E402

DB = "postgresql+psycopg://fx:fx@localhost:5432/fxstack"


def main() -> None:
    svc = RuntimeService(database_url=DB)
    rows = svc.get_active_model_sets(enabled_only=False)
    if isinstance(rows, dict):
        items = list(rows.items())
    else:
        items = [(str(r.get("pair") or r.get("symbol") or "?"), r) for r in rows]
    print(f"active_model_sets: {len(items)}")
    for pair_k, row in items:
        if not isinstance(row, dict):
            print(f"  {pair_k}: <{type(row).__name__}> {str(row)[:120]}")
            continue
        pair = str(row.get("pair") or row.get("symbol") or pair_k or "?").upper()
        meta = dict(row.get("metadata_json") or row.get("metadata") or {})
        gb = dict(meta.get("phase5_gate_bundle") or {})
        gates = {k: bool(dict(gb.get(k) or {}).get("passed", False)) for k in
                 ["research_gate", "economic_gate", "operational_gate", "shadow_gate", "canary_gate", "canary_closeout"]}
        pol = _resolve_main_runtime_rollout_policy(pair=pair, metadata=meta)
        print(f"\n=== {pair} (enabled={row.get('enabled')}) ===")
        print(f"  meta keys: {sorted(meta.keys())}")
        print(f"  has phase5_gate_bundle: {bool(gb)}  gates: {gates}")
        print(f"  rollout policy: mode={pol.get('mode')!r} active={pol.get('active')} enabled={pol.get('enabled')} "
              f"pair_allowlisted={pol.get('pair_allowlisted')} allowlisted_pairs={pol.get('allowlisted_pairs')} source={pol.get('source')!r}")


if __name__ == "__main__":
    main()
