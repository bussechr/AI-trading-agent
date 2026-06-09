"""Enable governed-live canary rollout for every configured active FX pair.

This fixes a runtime state where only EURUSD can ever reach governed_live. The
script updates the durable activation manifest and the runtime DB active model-set
metadata with explicit main_runtime_rollout sections for each enabled configured
pair, then patches orchestration_live scopes to the same pair universe.

It does not force entries: live signals still need to pass policy, spread,
portfolio, and risk-kernel gates before any BUY/SELL command is submitted.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path("D:/Development/Trading Agent")
MANIFEST = REPO / "fx-quant-stack" / "artifacts" / "active_models.json"
DB = "postgresql+psycopg://fx:fx@localhost:5432/fxstack"
SLEEVES = [
    "trend_pullback",
    "range_mean_reversion",
    "breakout_expansion",
    "failed_breakout_reversal",
]

sys.path.insert(0, str(REPO / "fx-quant-stack" / "src"))

from fxstack.runtime.runner import _resolve_main_runtime_rollout_policy  # noqa: E402
from fxstack.runtime.service import RuntimeService  # noqa: E402
from fxstack.settings import get_settings  # noqa: E402


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def _pair_universe(manifest: dict[str, Any]) -> list[str]:
    settings_pairs = [str(pair).upper().strip() for pair in list(get_settings().pairs or []) if str(pair).strip()]
    active = dict(manifest.get("active_model_sets") or {})
    enabled_pairs = {
        str(pair).upper().strip()
        for pair, row in active.items()
        if str(pair).strip() and bool(dict(row or {}).get("enabled", True))
    }
    if settings_pairs:
        return [pair for pair in settings_pairs if pair in enabled_pairs]
    return sorted(enabled_pairs)


def _rollout_for_pair(pair: str) -> dict[str, Any]:
    return {
        "mode": "canary",
        "enabled": True,
        "active": True,
        "runtime_enabled": True,
        "allowlisted_pairs": [str(pair).upper()],
        "budget_scale": 0.25,
        "budget_reason": "all_pairs_canary_operator_scope",
        "live_pair_allowlist": [str(pair).upper()],
        "live_sleeve_allowlist": list(SLEEVES),
        "live_intent_allowlist": ["enter"],
    }


def _patch_manifest(manifest: dict[str, Any], pairs: list[str]) -> list[str]:
    active = dict(manifest.get("active_model_sets") or {})
    patched: list[str] = []
    for pair in pairs:
        row = dict(active.get(pair) or {})
        if not row:
            continue
        metadata = dict(row.get("metadata") or {})
        metadata["main_runtime_rollout"] = _rollout_for_pair(pair)
        metadata["all_pairs_canary_enabled_at"] = float(time.time())
        metadata["all_pairs_canary_source"] = "tools/enable_all_pairs_canary.py"
        row["metadata"] = metadata
        active[pair] = row
        patched.append(pair)
    manifest["active_model_sets"] = active
    return patched


def _patch_db_active_sets(svc: RuntimeService, pairs: list[str]) -> list[str]:
    patched: list[str] = []
    for pair in pairs:
        row = svc.get_active_model_set(pair)
        if not row:
            continue
        metadata = dict(row.get("metadata_json") or {})
        metadata["main_runtime_rollout"] = _rollout_for_pair(pair)
        metadata["all_pairs_canary_enabled_at"] = float(time.time())
        metadata["all_pairs_canary_source"] = "tools/enable_all_pairs_canary.py"
        svc.upsert_active_model_set(
            pair=pair,
            model_set_id=str(row.get("model_set_id") or f"{pair.lower()}-active"),
            registry_path=str(row.get("registry_path") or ""),
            artifacts=dict(row.get("artifacts_json") or {}),
            metadata=metadata,
            enabled=bool(row.get("enabled", True)),
        )
        patched.append(pair)
    return patched


def _patch_live_scope(svc: RuntimeService, pairs: list[str]) -> dict[str, Any]:
    state = svc.get_state()
    runtime_diag = dict(state.get("runtime_diag") or {})
    live = dict(runtime_diag.get("orchestration_live") or {})
    before = {
        "active_pair_scope": list(live.get("active_pair_scope") or []),
        "active_sleeve_scope": list(live.get("active_sleeve_scope") or []),
        "active_intent_scope": list(live.get("active_intent_scope") or []),
    }
    live["active_pair_scope"] = list(pairs)
    live["active_sleeve_scope"] = list(SLEEVES)
    live["active_intent_scope"] = ["enter"]
    live["runtime_enabled"] = True
    live["queue_kill_active"] = False
    live["queue_kill_reason"] = ""
    live["all_pairs_canary_scope_patched_at"] = float(time.time())
    runtime_diag["orchestration_live"] = live
    svc.patch_state({"runtime_diag": runtime_diag})
    return {
        "before": before,
        "after": {
            "active_pair_scope": list(live.get("active_pair_scope") or []),
            "active_sleeve_scope": list(live.get("active_sleeve_scope") or []),
            "active_intent_scope": list(live.get("active_intent_scope") or []),
        },
    }


def main() -> None:
    manifest = _read_json(MANIFEST)
    if not manifest:
        raise SystemExit(f"missing activation manifest: {MANIFEST}")
    pairs = _pair_universe(manifest)
    if not pairs:
        raise SystemExit("no enabled configured pairs found in active_models.json")

    backup = MANIFEST.with_suffix(".json.all_pairs_canary.bak")
    if not backup.exists():
        backup.write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")

    manifest_patched = _patch_manifest(manifest, pairs)
    _write_json(MANIFEST, manifest)

    svc = RuntimeService(database_url=DB)
    db_patched = _patch_db_active_sets(svc, pairs)
    scope = _patch_live_scope(svc, pairs)

    unresolved: list[str] = []
    for pair in pairs:
        row = svc.get_active_model_set(pair) or {}
        policy = _resolve_main_runtime_rollout_policy(pair=pair, metadata=dict(row.get("metadata_json") or {}))
        if not (policy.get("active") and policy.get("mode") == "canary" and policy.get("pair_allowlisted")):
            unresolved.append(pair)

    print(
        json.dumps(
            {
                "pairs": pairs,
                "manifest_patched": manifest_patched,
                "db_patched": db_patched,
                "scope": scope,
                "unresolved": unresolved,
                "backup": str(backup),
            },
            indent=2,
        )
    )
    if unresolved:
        raise SystemExit(f"rollout did not resolve active for: {','.join(unresolved)}")


if __name__ == "__main__":
    main()
