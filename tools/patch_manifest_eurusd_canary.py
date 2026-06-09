"""Durably anchor the EURUSD canary gate bundle in the model activation manifest.

The runtime re-seeds active model sets from fx-quant-stack/artifacts/active_models.json
on every startup (runner.py:_seed_active_model_sets -> upsert_active_model_set), which
overwrites any DB-only metadata edit. To make the EURUSD canary survive restarts, the
regenerated phase5_gate_bundle (economic gate passing on the OOS-validated lifecycle-off
evidence) must live in active_model_sets.EURUSD.metadata.phase5_gate_bundle here.

Idempotent: writes a .bak once, then sets the key.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path("D:/Development/Trading Agent")
MANIFEST = REPO / "fx-quant-stack" / "artifacts" / "active_models.json"
BUNDLE = REPO / "fx-quant-stack" / "artifacts" / "releases" / "eurusd" / "6275f820-b835-4f90-a29d-88393d59f41a" / "phase5_gate_bundle.json"


def main() -> None:
    bundle = json.loads(BUNDLE.read_text(encoding="utf-8"))
    assert bundle.get("canary_gate", {}).get("passed") is True, "canary_gate must be passing in the bundle"
    assert bundle.get("economic_gate", {}).get("passed") is True, "economic_gate must be passing in the bundle"

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    eur = dict(manifest.get("active_model_sets", {}).get("EURUSD") or {})
    if not eur:
        raise SystemExit("EURUSD not found in manifest")

    bak = MANIFEST.with_suffix(".json.bak")
    if not bak.exists():
        bak.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"backup written: {bak.name}")

    meta = dict(eur.get("metadata") or {})
    meta["phase5_gate_bundle"] = bundle
    eur["metadata"] = meta
    manifest["active_model_sets"]["EURUSD"] = eur
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # verify
    reread = json.loads(MANIFEST.read_text(encoding="utf-8"))
    gb = reread["active_model_sets"]["EURUSD"]["metadata"].get("phase5_gate_bundle") or {}
    print(f"manifest patched: EURUSD.metadata.phase5_gate_bundle present={bool(gb)} "
          f"economic_passed={gb.get('economic_gate', {}).get('passed')} "
          f"canary_passed={gb.get('canary_gate', {}).get('passed')} overall={gb.get('overall_status')}")


if __name__ == "__main__":
    main()
