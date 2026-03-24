from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fxstack_src() -> Path:
    return _repo_root() / "fx-quant-stack" / "src"


if str(_fxstack_src()) not in sys.path:
    sys.path.insert(0, str(_fxstack_src()))

from fxstack.settings import get_settings  # noqa: E402
from fxstack.training.activation import latest_registry_for_pair, parse_registry_entry  # noqa: E402


def _headers() -> dict[str, str]:
    api_key = str(os.environ.get("FXSTACK_BRIDGE_API_KEY", "")).strip()
    return {"X-API-Key": api_key} if api_key else {}


def _run(cmd: list[str], *, cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True)


def _rel(path: Path, *, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def _validate_registry_root(*, registry_root: Path, pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"pairs": {}, "ok": True, "eligible_count": 0, "lifecycle_complete_count": 0}
    for pair in pairs:
        entry_path = latest_registry_for_pair(registry_root=registry_root, pair=pair)
        if entry_path is None:
            out["pairs"][pair] = {"ok": False, "reason": "missing_registry_entry"}
            out["ok"] = False
            continue
        parsed = parse_registry_entry(entry_path)
        metadata = dict(parsed.get("metadata") or {})
        capabilities = dict(metadata.get("capabilities") or {})
        pair_ok = bool(
            str(metadata.get("promotion_status") or "").strip().lower() == "eligible"
            and bool(metadata.get("lifecycle_complete"))
            and bool(capabilities.get("has_exit_model"))
            and bool(capabilities.get("has_reversal_models"))
        )
        out["pairs"][pair] = {
            "ok": pair_ok,
            "registry_path": str(entry_path),
            "promotion_status": str(metadata.get("promotion_status") or ""),
            "lifecycle_complete": bool(metadata.get("lifecycle_complete")),
            "has_exit_model": bool(capabilities.get("has_exit_model")),
            "has_reversal_models": bool(capabilities.get("has_reversal_models")),
        }
        if str(metadata.get("promotion_status") or "").strip().lower() == "eligible":
            out["eligible_count"] = int(out["eligible_count"]) + 1
        if bool(metadata.get("lifecycle_complete")):
            out["lifecycle_complete_count"] = int(out["lifecycle_complete_count"]) + 1
        if not pair_ok:
            out["ok"] = False
    return out


def _verify_live_stack(*, bridge_url: str, pairs: list[str], timeout_secs: float) -> dict[str, Any]:
    deadline = time.time() + float(timeout_secs)
    last: dict[str, Any] = {}
    workflow_url = f"{bridge_url.rstrip('/')}/v2/ops/workflows/status"
    state_url = f"{bridge_url.rstrip('/')}/v2/state"
    while time.time() < deadline:
        state = requests.get(state_url, headers=_headers(), timeout=5).json()
        workflows = requests.get(workflow_url, headers=_headers(), params={"limit": 500}, timeout=5).json()
        workflow_map: dict[str, dict[str, Any]] = {}
        for workflow in list(workflows.get("workflows", []) or []):
            workflow_id = str(workflow.get("workflow_id") or "")
            pair = workflow_id.split("-training-eval", 1)[0].upper()
            if pair:
                workflow_map[pair] = dict(workflow or {})
        pair_failures: list[str] = []
        for pair in pairs:
            workflow = dict(workflow_map.get(pair) or {})
            if str(workflow.get("status") or "").strip().lower() != "eligible":
                pair_failures.append(f"{pair}:workflow_status")
                continue
            if not bool(workflow.get("startup_inference_ok")):
                pair_failures.append(f"{pair}:startup_inference")
        broker_symbol_failures = list(state.get("broker_symbol_failures", []) or [])
        ok = bool(
            int(state.get("active_pair_count") or 0) == len(pairs)
            and int(state.get("activation_mismatch_count") or 0) == 0
            and int(state.get("startup_inference_failures") or 0) == 0
            and not pair_failures
            and not broker_symbol_failures
        )
        last = {
            "ok": ok,
            "state": state,
            "pair_failures": pair_failures,
            "broker_symbol_failures": broker_symbol_failures,
        }
        if ok:
            return last
        time.sleep(5.0)
    return last


def main() -> None:
    s = get_settings()
    repo_root = _repo_root()
    ap = argparse.ArgumentParser(description="Run weekly full shadow retrain with gated activation and rollback.")
    ap.add_argument("--pairs", default=",".join(s.pairs))
    ap.add_argument("--fetch", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--activate", action=argparse.BooleanOptionalAction, default=bool(s.weekly_auto_activate))
    ap.add_argument("--restart", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--bridge-url", default=str(s.mt4_bridge_url))
    ap.add_argument("--summary-out", default="")
    ap.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    args = ap.parse_args()

    pairs = [str(x).strip().upper() for x in str(args.pairs).split(",") if str(x).strip()]
    shadow_artifact_root = repo_root / "fx-quant-stack" / "artifacts_shadow" / f"full_{args.stamp}"
    shadow_registry_root = repo_root / "fx-quant-stack" / "artifacts_shadow" / f"registry_full_{args.stamp}"
    manifest_path = repo_root / "fx-quant-stack" / "artifacts" / "active_models.json"
    backup_manifest = manifest_path.with_name(f"active_models.backup_{args.stamp}.json")
    summary_out = Path(args.summary_out) if str(args.summary_out).strip() else (
        repo_root / "fx-quant-stack" / "artifacts" / "reports" / f"weekly_full_retrain_{args.stamp}.json"
    )

    summary: dict[str, Any] = {
        "stamp": args.stamp,
        "pairs": pairs,
        "shadow_artifact_root": str(shadow_artifact_root),
        "shadow_registry_root": str(shadow_registry_root),
        "steps": [],
        "status": "running",
    }
    summary_out.parent.mkdir(parents=True, exist_ok=True)

    try:
        if bool(args.fetch):
            cmd = [
                sys.executable,
                "-m",
                "tools.fetch_dukascopy_matrix",
                "--source-root",
                "fx-quant-stack/data/dukascopy",
                "--pairs",
                ",".join(pairs),
                "--timeframes",
                "M1,M5,M15,H4,D",
                "--resume",
            ]
            _run(cmd, cwd=repo_root)
            summary["steps"].append({"name": "fetch", "status": "ok"})

        for pair in pairs:
            cmd = [
                sys.executable,
                "-m",
                "src.trader.cli",
                "train",
                "all",
                "--pair",
                pair,
                "--swing-timeframe",
                "D",
                "--intraday-timeframe",
                "M5",
                "--regime-timeframe",
                "H4",
                "--feature-root",
                "fx-quant-stack/data/features",
                "--label-root",
                "fx-quant-stack/data/labels",
                "--artifact-root",
                _rel(shadow_artifact_root, root=repo_root),
                "--training-config",
                "fx-quant-stack/configs/training.yaml",
                "--registry-root",
                _rel(shadow_registry_root, root=repo_root),
                "--deep-stale-hours",
                str(float(s.deep_retrain_max_age_hours)),
                "--force-retrain",
            ]
            _run(cmd, cwd=repo_root)
            summary["steps"].append({"name": f"train:{pair}", "status": "ok"})

        validation = _validate_registry_root(registry_root=shadow_registry_root, pairs=pairs)
        summary["validation"] = validation
        if not bool(validation.get("ok")):
            raise RuntimeError("shadow registry validation failed")

        if bool(args.activate):
            shutil.copy2(manifest_path, backup_manifest)
            summary["backup_manifest"] = str(backup_manifest)
            activate_cmd = [
                sys.executable,
                "-m",
                "src.trader.cli",
                "models",
                "activate",
                "--registry-root",
                _rel(shadow_registry_root, root=repo_root),
                "--manifest",
                "fx-quant-stack/artifacts/active_models.json",
                "--require-all",
            ]
            _run(activate_cmd, cwd=repo_root)
            summary["steps"].append({"name": "activate", "status": "ok"})

            if bool(args.restart):
                _run(
                    [
                        "cmd.exe",
                        "/c",
                        f"set LAUNCH_NO_PAUSE=1&& call launch_all.bat stop&& call launch_all.bat live {int(args.equity)}",
                    ],
                    cwd=repo_root,
                )
                summary["steps"].append({"name": "restart", "status": "ok"})

            live_verify = _verify_live_stack(
                bridge_url=str(args.bridge_url),
                pairs=pairs,
                timeout_secs=120.0,
            )
            summary["live_verify"] = live_verify
            if not bool(live_verify.get("ok")):
                if backup_manifest.exists():
                    shutil.copy2(backup_manifest, manifest_path)
                    if bool(args.restart):
                        _run(
                            [
                                "cmd.exe",
                                "/c",
                                f"set LAUNCH_NO_PAUSE=1&& call launch_all.bat stop&& call launch_all.bat live {int(args.equity)}",
                            ],
                            cwd=repo_root,
                        )
                    raise RuntimeError("post-activation live verification failed; rollback applied")
                raise RuntimeError("post-activation live verification failed")

        summary["status"] = "ok"
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        raise

    summary_out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
