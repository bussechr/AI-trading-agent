from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


CAPTURE_SCHEMA_VERSION = "phase0.baseline_capture.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_json(base_url: str, path: str, timeout: float = 3.0) -> dict[str, Any]:
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    response = requests.get(urljoin(f"{str(base_url).rstrip('/')}/", path.lstrip("/")), headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def _fetch_optional_json(base_url: str, path: str, timeout: float = 3.0) -> dict[str, Any]:
    try:
        return _fetch_json(base_url, path, timeout=timeout)
    except Exception:
        return {}


def _git_commit(project_root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        )
        return str(proc.stdout).strip()
    except Exception:
        return ""


def _first_present(payload: dict[str, Any], keys: list[str], default: Any = None) -> Any:
    for key in keys:
        if key in payload:
            return payload.get(key)
    return default


def _extract_blocking_reasons(decisions: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in decisions:
        for reason in list(row.get("reasons") or []):
            txt = str(reason).strip()
            if txt:
                counts[txt] += 1
    return dict(sorted(counts.items()))


def _extract_decision_rows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in decisions:
        metadata = dict(row.get("metadata") or {})
        rows.append(
            {
                "symbol": str(row.get("symbol") or ""),
                "side": str(row.get("side") or ""),
                "score": row.get("score"),
                "confidence": row.get("confidence"),
                "execution_ready": bool(row.get("execution_ready", False)),
                "reasons": list(row.get("reasons") or []),
                "adaptive_playbook": str(metadata.get("adaptive_playbook") or ""),
                "allocator_rank": metadata.get("allocator_rank"),
            }
        )
    return rows


def _extract_command_queue_summary(state: dict[str, Any], ready: dict[str, Any]) -> dict[str, Any]:
    return {
        "pending_command_count": _first_present(
            state,
            ["pending_command_count", "pendingCommandCount", "queued_command_count", "queuedCommandCount"],
            0,
        ),
        "submitted_entry_count": _first_present(state, ["submitted_entry_count", "submittedEntryCount"], 0),
        "submitted_live_entry_count": _first_present(state, ["submitted_live_entry_count", "submittedLiveEntryCount"], 0),
        "command_queue_status": str(
            _first_present(state, ["command_queue_status", "commandQueueStatus"], "")
            or _first_present(ready, ["command_queue_status", "commandQueueStatus"], "")
        ),
    }


def _collect_manifest_refs(payload: Any, *, refs: set[str] | None = None) -> list[str]:
    if refs is None:
        refs = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_txt = str(key).lower()
            if key_txt in {
                "registry_path",
                "model_manifest",
                "model_manifest_path",
                "model_activation_manifest",
                "activation_manifest",
            }:
                txt = str(value or "").strip()
                if txt:
                    refs.add(txt)
            else:
                _collect_manifest_refs(value, refs=refs)
    elif isinstance(payload, list):
        for value in payload:
            _collect_manifest_refs(value, refs=refs)
    return sorted(refs)


def normalize_capture(payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ready = dict(payloads.get("ready") or {})
    state = dict(payloads.get("state") or {})
    decision_snapshots = dict(payloads.get("decision_snapshots") or {})
    orchestration_runs = dict(payloads.get("orchestration_runs") or {})
    orchestration_traces = dict(payloads.get("orchestration_traces") or {})
    items = list(decision_snapshots.get("items") or [])
    run_items = list(orchestration_runs.get("items") or [])
    trace_items = list(orchestration_traces.get("items") or [])
    latest = dict(items[0] or {}) if items else {}
    latest_run = dict(run_items[0] or {}) if run_items else {}
    latest_trace = dict(trace_items[0] or {}) if trace_items else {}
    latest_version_bundle = dict(latest_run.get("version_bundle_json") or latest_run.get("version_bundle") or {})
    decisions = list(latest.get("decisions_json") or [])
    diagnostics = dict(latest.get("diagnostics_json") or {})

    return {
        "decision_snapshot_count": len(items),
        "orchestration_run_count": len(run_items),
        "orchestration_trace_count": len(trace_items),
        "latest_snapshot_vol": latest.get("vol"),
        "latest_orchestration_run_id": str(latest_run.get("run_id") or ""),
        "latest_orchestration_trace_id": str(latest_trace.get("trace_id") or ""),
        "latest_orchestration_version_bundle": latest_version_bundle,
        "decision_rows": _extract_decision_rows(decisions),
        "blocking_reasons": _extract_blocking_reasons(decisions),
        "command_queue_summary": _extract_command_queue_summary(state, ready),
        "runtime_startup": {
            "runtime_status": str(ready.get("runtime_status") or ""),
            "runtime_phase": str(ready.get("runtime_phase") or ""),
            "runtime_ready": bool(ready.get("runtime_ready", False)),
            "runtime_startup_status": str(ready.get("runtime_startup_status") or ""),
            "startup_inference_failures": ready.get("startup_inference_failures"),
        },
        "readiness": {
            "status": str(ready.get("status") or ""),
            "reason": str(ready.get("reason") or ""),
            "bridge_up": bool(ready.get("bridge_up", False)),
            "mt4_connected": bool(ready.get("mt4_connected", False)),
        },
        "active_model_manifest_refs": _collect_manifest_refs({"ready": ready, "state": state, "diagnostics": diagnostics}),
        "diagnostic_keys": sorted(list(diagnostics.keys())),
    }


def build_capture_pack(
    *,
    base_url: str,
    payloads: dict[str, dict[str, Any]],
    tag: str,
    project_root: Path,
) -> dict[str, Any]:
    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_id": f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{tag}",
        "captured_at": _utc_now_iso(),
        "base_url": str(base_url).rstrip("/"),
        "tag": tag,
        "version_metadata": {
            "tool_version": CAPTURE_SCHEMA_VERSION,
            "git_commit": _git_commit(project_root),
        },
        "environment": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": sys.version.split()[0],
            "cwd": str(project_root),
        },
        "raw": {
            "ready": dict(payloads.get("ready") or {}),
            "state": dict(payloads.get("state") or {}),
            "decision_snapshots": dict(payloads.get("decision_snapshots") or {}),
            "orchestration_runs": dict(payloads.get("orchestration_runs") or {}),
            "orchestration_traces": dict(payloads.get("orchestration_traces") or {}),
        },
        "normalized": normalize_capture(payloads),
    }


def write_capture_pack(pack: dict[str, Any], output_root: Path) -> Path:
    capture_id = str(pack.get("capture_id") or "capture")
    out_dir = output_root / capture_id
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = dict(pack.get("raw") or {})
    (out_dir / "ready.json").write_text(json.dumps(raw.get("ready", {}), indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "state.json").write_text(json.dumps(raw.get("state", {}), indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "decision-snapshots.json").write_text(
        json.dumps(raw.get("decision_snapshots", {}), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (out_dir / "normalized-summary.json").write_text(
        json.dumps(pack.get("normalized", {}), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (out_dir / "baseline-pack.json").write_text(json.dumps(pack, indent=2, sort_keys=True), encoding="utf-8")
    return out_dir


def run(args: argparse.Namespace) -> int:
    base_url = str(args.base_url).rstrip("/")
    output_root = Path(getattr(args, "output_root", "tests/golden/orchestration"))
    project_root = Path(getattr(args, "project_root", Path(__file__).resolve().parents[1]))
    tag = str(getattr(args, "tag", "baseline") or "baseline").strip().replace(" ", "_")
    timeout = float(max(0.1, float(getattr(args, "timeout", 3.0) or 3.0)))

    payloads = {
        "ready": _fetch_json(base_url, "/v2/ready", timeout=timeout),
        "state": _fetch_json(base_url, "/v2/state", timeout=timeout),
        "decision_snapshots": _fetch_json(base_url, "/v2/decision-snapshots", timeout=timeout),
        "orchestration_runs": _fetch_optional_json(base_url, "/v2/orchestration/runs", timeout=timeout),
        "orchestration_traces": _fetch_optional_json(base_url, "/v2/orchestration/traces", timeout=timeout),
    }
    pack = build_capture_pack(base_url=base_url, payloads=payloads, tag=tag, project_root=project_root)
    out_dir = write_capture_pack(pack, output_root)
    print(str(out_dir))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a Phase 0 orchestration baseline pack from the live bridge.")
    parser.add_argument("--base-url", default=os.environ.get("MT4_BRIDGE_URL", "http://127.0.0.1:58710"))
    parser.add_argument("--output-root", default="tests/golden/orchestration")
    parser.add_argument("--tag", default="baseline")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_build_parser().parse_args()))
