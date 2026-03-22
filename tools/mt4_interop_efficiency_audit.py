#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from src.audit.interop_efficiency import (
    load_jsonl,
    summarize_capacity_curve,
    summarize_error_budget,
    summarize_interop_kpis,
    summarize_stage_latency,
)

DEFAULT_SYMBOLS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
    "EURJPY",
]

PROFILE_DEFAULTS = {
    "idle_30m": {"duration_secs": 30 * 60, "rps": 0.0, "kind": "idle"},
    "steady_1rps_30m": {"duration_secs": 30 * 60, "rps": 1.0, "kind": "steady"},
    "burst_10rps_5m": {"duration_secs": 5 * 60, "rps": 10.0, "kind": "burst"},
    "mixed_entry_exit_5rps_10m": {"duration_secs": 10 * 60, "rps": 5.0, "kind": "mixed"},
}


def _safe_get_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        out = r.json()
        return dict(out if isinstance(out, dict) else {})
    except Exception:
        return {}


def _safe_get_json_paths(bridge_url: str, paths: list[str], timeout: float = 2.0) -> dict[str, Any]:
    base = str(bridge_url).rstrip("/")
    for path in list(paths or []):
        payload = _safe_get_json(f"{base}{str(path)}", timeout=timeout)
        if payload:
            return payload
    return {}


def _post_probe(
    bridge_url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 2.0,
) -> tuple[int, dict[str, Any]]:
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    try:
        body_payload = dict(payload)
        command_id = str(body_payload.get("command_id") or body_payload.get("signal_id") or uuid.uuid4())
        body_payload["command_id"] = command_id
        body_payload.setdefault("session_id", str(body_payload.get("audit_session_id") or "interop-audit"))
        r = requests.post(f"{bridge_url}/v2/commands", json=body_payload, headers=headers, timeout=timeout)
        body: dict[str, Any]
        try:
            j = r.json()
            body = dict(j if isinstance(j, dict) else {})
        except Exception:
            body = {"raw": r.text}
        return int(r.status_code), body
    except Exception as exc:
        return 0, {"error": str(exc)}


def _profile_names(raw: str) -> list[str]:
    names = [s.strip() for s in str(raw).split(",") if s.strip()]
    if not names:
        names = list(PROFILE_DEFAULTS.keys())
    out = [n for n in names if n in PROFILE_DEFAULTS]
    return out or list(PROFILE_DEFAULTS.keys())


def _build_probe_payload(
    *,
    idx: int,
    profile: str,
    session_id: str,
    mode: str,
) -> dict[str, Any]:
    base = {
        "signal_id": f"audit-{session_id}-{profile}-{idx}",
        "trace_id": str(uuid.uuid4()),
        "interop_mode": str(mode),
        "audit_session_id": str(session_id),
        "audit_profile": str(profile),
        "cycle_id": int(idx),
        "intent": "AUDIT_PROBE",
    }
    if profile == "mixed_entry_exit_5rps_10m" and (idx % 2 == 1):
        base.update(
            {
                "cmd": "CLOSE",
                "symbol": "ZZZ_AUDIT",
                "magic": 999998,
            }
        )
    else:
        base.update(
            {
                "cmd": "INFO",
                "thought": f"interop audit probe {profile} #{idx}",
            }
        )
    return base


def _run_live_profiles(
    *,
    bridge_url: str,
    profiles: list[str],
    duration_scale: float,
    session_id: str,
    mode: str,
) -> list[dict[str, Any]]:
    stats: list[dict[str, Any]] = []
    for profile in profiles:
        spec = dict(PROFILE_DEFAULTS[profile])
        duration = max(1.0, float(spec["duration_secs"]) * float(max(0.001, duration_scale)))
        rps = float(spec["rps"])
        kind = str(spec["kind"])

        started = time.time()
        sent = 0
        status_counts: dict[str, int] = {}
        next_send_ts = started

        if rps <= 0.0 or kind == "idle":
            time.sleep(duration)
        else:
            interval = 1.0 / max(rps, 1e-6)
            while time.time() < started + duration:
                now = time.time()
                if now < next_send_ts:
                    time.sleep(min(0.02, next_send_ts - now))
                    continue
                payload = _build_probe_payload(
                    idx=sent,
                    profile=profile,
                    session_id=session_id,
                    mode=mode,
                )
                code, _ = _post_probe(
                    bridge_url,
                    payload,
                )
                key = str(code)
                status_counts[key] = int(status_counts.get(key, 0)) + 1
                sent += 1
                next_send_ts += interval

        ended = time.time()
        stats.append(
            {
                "profile": profile,
                "kind": kind,
                "duration_secs": float(ended - started),
                "target_rps": float(rps),
                "sent": int(sent),
                "status_counts": status_counts,
            }
        )
    return stats


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    session_id: str,
    mode: str,
    ts_start: float,
    ts_end: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        row_mode = str(row.get("mode", "")).strip().lower()
        if row_mode and mode and row_mode != mode:
            continue
        sid = str(row.get("audit_session_id", "")).strip()
        ts_raw = row.get("ts", row.get("t_bridge_queued", 0.0))
        try:
            ts = float(ts_raw)
        except Exception:
            ts = 0.0
        if session_id:
            if sid and sid != session_id:
                continue
            if (not sid) and (ts < ts_start or ts > ts_end):
                continue
        out.append(row)
    return out


def _write_outputs(
    *,
    output_dir: Path,
    summary: dict[str, Any],
    stage_latency: dict[str, Any],
    error_budget: dict[str, int],
    capacity_curve: list[dict[str, Any]],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "interop_audit_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    stage_rows = []
    for stage, vals in stage_latency.items():
        p = dict(vals.get("percentiles", {}) or {})
        row = {
            "stage": stage,
            "count": int(vals.get("count", 0)),
            "mean": float(vals.get("mean", 0.0)),
            "p50": float(p.get("p50", 0.0)),
            "p95": float(p.get("p95", 0.0)),
            "p99": float(p.get("p99", 0.0)),
        }
        for b, c in (vals.get("buckets", {}) or {}).items():
            row[f"bucket_le_{b}"] = int(c)
        stage_rows.append(row)
    pd.DataFrame(stage_rows).to_csv(output_dir / "interop_stage_latency.csv", index=False)

    eb_rows = [{"key": k, "count": int(v)} for k, v in sorted(error_budget.items())]
    pd.DataFrame(eb_rows).to_csv(output_dir / "interop_error_budget.csv", index=False)

    pd.DataFrame(list(capacity_curve)).to_csv(output_dir / "interop_capacity_curve.csv", index=False)


def _acceptance(kpis: dict[str, Any]) -> dict[str, Any]:
    p95_e2e = float(((kpis.get("signal_post_to_ack_ms") or {}).get("p95", 0.0)))
    unfinalized = float(kpis.get("unfinalized_signal_rate", 1.0))
    timeout_rate = float(kpis.get("timeout_rate", 1.0))
    cpu_util = float(kpis.get("agent_cycle_cpu_util_p95", 1.0))

    checks = {
        "p95_signal_post_to_ack_le_1600": bool(p95_e2e <= 1600.0),
        "unfinalized_signal_rate_le_0_005": bool(unfinalized <= 0.005),
        "timeout_rate_le_0_01": bool(timeout_rate <= 0.01),
        "agent_cycle_cpu_util_p95_le_0_60": bool(cpu_util <= 0.60),
    }
    return {
        "checks": checks,
        "pass": bool(all(checks.values())),
    }


def run_audit(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.output_dir)
    mode = str(args.mode).strip().lower()
    if mode not in {"live_shadow", "replay_live_like", "replay_offline"}:
        mode = "live_shadow"

    session_id = str(args.session_id).strip() or time.strftime("%Y%m%d%H%M%S")
    ts_start = time.time()

    pre_metrics = _safe_get_json_paths(args.bridge_url, ["/v2/metrics"])
    profile_stats: list[dict[str, Any]] = []

    if mode == "live_shadow":
        profile_stats = _run_live_profiles(
            bridge_url=args.bridge_url,
            profiles=_profile_names(args.profiles),
            duration_scale=float(args.duration_scale),
            session_id=session_id,
            mode=mode,
        )

    # Allow pending signals to finalize after probes.
    time.sleep(float(max(0.0, args.finalize_wait_secs)))

    ts_end = time.time()
    post_metrics = _safe_get_json_paths(args.bridge_url, ["/v2/metrics"])

    transport_rows_all = load_jsonl(args.transport_trace)
    compute_rows_all = load_jsonl(args.compute_trace)

    transport_rows = _filter_rows(
        transport_rows_all,
        session_id=session_id,
        mode=mode,
        ts_start=ts_start,
        ts_end=ts_end,
    )
    compute_rows = _filter_rows(
        compute_rows_all,
        session_id="",
        mode=mode,
        ts_start=ts_start,
        ts_end=ts_end,
    )

    stage_latency = summarize_stage_latency(transport_rows, buckets_ms=args.latency_buckets)
    error_budget = summarize_error_budget(transport_rows)
    capacity_curve = summarize_capacity_curve(transport_rows, window_secs=int(args.capacity_window_secs))

    merged_for_kpi = list(transport_rows)
    merged_for_kpi.extend([{"agent_cycle_ms": r.get("agent_cycle_ms", 0.0)} for r in compute_rows])
    kpis = summarize_interop_kpis(merged_for_kpi, loop_interval_secs=float(args.loop_interval_secs))
    acceptance = _acceptance(kpis)
    pre_counters = dict((pre_metrics.get("counters") or {}) if isinstance(pre_metrics, dict) else {})
    post_counters = dict((post_metrics.get("counters") or {}) if isinstance(post_metrics, dict) else {})
    bridge_rate_limited_delta = int(post_counters.get("signals_rate_limited", 0)) - int(
        pre_counters.get("signals_rate_limited", 0)
    )
    agent_cmd_rate_rejections = 0
    for row in compute_rows:
        rej = dict(row.get("rejection_stats_cycle", {}) or {})
        agent_cmd_rate_rejections += int(rej.get("command_rate_entry", 0))
        agent_cmd_rate_rejections += int(rej.get("command_rate_total", 0))
    dominant = "balanced"
    if agent_cmd_rate_rejections > max(bridge_rate_limited_delta, 0):
        dominant = "agent_budget"
    elif max(bridge_rate_limited_delta, 0) > agent_cmd_rate_rejections:
        dominant = "bridge_rate_limit"
    throttle_interference = {
        "agent_command_budget_rejections": int(agent_cmd_rate_rejections),
        "bridge_rate_limited_delta": int(max(bridge_rate_limited_delta, 0)),
        "combined": int(agent_cmd_rate_rejections + max(bridge_rate_limited_delta, 0)),
        "dominant_source": dominant,
    }

    replay_companion: dict[str, Any] = {}
    if mode in {"replay_live_like", "replay_offline"}:
        replay_companion = {
            "status": "skipped",
            "reason": "replay_companion_removed_in_v2_only_runtime",
        }

    summary = {
        "meta": {
            "session_id": session_id,
            "mode": mode,
            "bridge_url": str(args.bridge_url),
            "transport_trace": str(args.transport_trace),
            "compute_trace": str(args.compute_trace),
            "time_start": float(ts_start),
            "time_end": float(ts_end),
            "duration_secs": float(ts_end - ts_start),
            "profiles": _profile_names(args.profiles),
            "duration_scale": float(args.duration_scale),
        },
        "profile_stats": profile_stats,
        "kpis": kpis,
        "acceptance": acceptance,
        "stage_latency": stage_latency,
        "error_budget": error_budget,
        "capacity_curve_points": int(len(capacity_curve)),
        "rows": {
            "transport": int(len(transport_rows)),
            "compute": int(len(compute_rows)),
        },
        "throttle_interference": throttle_interference,
        "metrics_snapshots": {
            "before": pre_metrics,
            "after": post_metrics,
        },
        "replay_companion": replay_companion,
    }

    _write_outputs(
        output_dir=out_dir,
        summary=summary,
        stage_latency=stage_latency,
        error_budget=error_budget,
        capacity_curve=capacity_curve,
    )
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Python<->MT4 interop efficiency audit runner")
    ap.add_argument("--mode", default="live_shadow", help="live_shadow|replay_live_like|replay_offline")
    ap.add_argument("--bridge-url", default=os.getenv("MT4_BRIDGE_URL", "http://127.0.0.1:58710"))
    ap.add_argument("--transport-trace", default="data/state/audit/interop/transport_trace.jsonl")
    ap.add_argument("--compute-trace", default="data/state/audit/interop/compute_trace.jsonl")
    ap.add_argument("--output-dir", default="data/state/audit/interop")
    ap.add_argument(
        "--profiles",
        default="idle_30m,steady_1rps_30m,burst_10rps_5m,mixed_entry_exit_5rps_10m",
        help="Comma-separated profile list",
    )
    ap.add_argument("--duration-scale", type=float, default=1.0, help="Scale default profile durations")
    ap.add_argument("--finalize-wait-secs", type=float, default=5.0)
    ap.add_argument("--capacity-window-secs", type=int, default=60)
    ap.add_argument("--loop-interval-secs", type=float, default=5.0)
    ap.add_argument("--session-id", default="")
    ap.add_argument("--latency-buckets", default="25,50,100,250,500,1000,1600,2500,5000")

    # Replay companion options
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--data-dir", default="data/fx_minis")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--replay-bars", type=int, default=700)
    ap.add_argument("--replay-warmup", type=int, default=252)

    args = ap.parse_args()
    args.latency_buckets = [
        int(float(x)) for x in str(args.latency_buckets).replace(";", ",").split(",") if str(x).strip()
    ]

    out = run_audit(args)
    print(f"Wrote {Path(args.output_dir) / 'interop_audit_summary.json'}")
    print(f"Transport rows: {out.get('rows', {}).get('transport', 0)}")
    print(f"Acceptance pass: {bool((out.get('acceptance') or {}).get('pass', False))}")


if __name__ == "__main__":
    main()
