from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_LATENCY_BUCKETS_MS = [25, 50, 100, 250, 500, 1000, 1600, 2500, 5000]


def parse_latency_buckets(raw: Any) -> list[int]:
    """Parse comma/list latency buckets and return sorted unique positive ints."""
    if isinstance(raw, (list, tuple)):
        vals = raw
    elif raw is None:
        vals = DEFAULT_LATENCY_BUCKETS_MS
    else:
        vals = str(raw).replace(";", ",").split(",")

    out: list[int] = []
    for v in vals:
        try:
            iv = int(float(v))
        except Exception:
            continue
        if iv > 0:
            out.append(iv)
    if not out:
        return list(DEFAULT_LATENCY_BUCKETS_MS)
    return sorted(set(out))


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            txt = line.strip()
            if not txt:
                continue
            try:
                obj = json.loads(txt)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def percentile(values: list[float], pct: float) -> float:
    """Linear interpolation percentile with deterministic empty fallback."""
    if not values:
        return 0.0
    clean = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not clean:
        return 0.0
    clean.sort()
    if len(clean) == 1:
        return float(clean[0])
    p = float(max(0.0, min(100.0, pct)))
    rank = (p / 100.0) * (len(clean) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return float(clean[lo])
    frac = rank - lo
    return float(clean[lo] * (1.0 - frac) + clean[hi] * frac)


def percentile_triplet(values: list[float]) -> dict[str, float]:
    return {
        "p50": float(percentile(values, 50.0)),
        "p95": float(percentile(values, 95.0)),
        "p99": float(percentile(values, 99.0)),
    }


def bucketize(values: list[float], buckets_ms: list[int] | None = None) -> dict[str, int]:
    buckets = parse_latency_buckets(buckets_ms)
    counts = {str(b): 0 for b in buckets}
    counts["inf"] = 0
    for v in values:
        try:
            x = float(v)
        except Exception:
            continue
        if not math.isfinite(x) or x < 0:
            continue
        placed = False
        for b in buckets:
            if x <= float(b):
                counts[str(b)] += 1
                placed = True
                break
        if not placed:
            counts["inf"] += 1
    return counts


def collect_stage_samples(rows: list[dict[str, Any]]) -> dict[str, list[float]]:
    samples: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        stage = row.get("stage_latencies_ms")
        if not isinstance(stage, dict):
            continue
        for k, v in stage.items():
            try:
                fv = float(v)
            except Exception:
                continue
            if math.isfinite(fv) and fv >= 0:
                samples[str(k)].append(fv)
    return dict(samples)


def summarize_stage_latency(
    rows: list[dict[str, Any]],
    *,
    buckets_ms: list[int] | None = None,
) -> dict[str, Any]:
    samples = collect_stage_samples(rows)
    out: dict[str, Any] = {}
    for stage, vals in samples.items():
        out[stage] = {
            "count": int(len(vals)),
            "percentiles": percentile_triplet(vals),
            "buckets": bucketize(vals, buckets_ms),
            "mean": float(sum(vals) / max(len(vals), 1)),
        }
    return out


def summarize_error_budget(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        outcome = str(row.get("outcome", "unknown")).strip().lower() or "unknown"
        reason = str(row.get("rejection_reason", "")).strip().lower()
        key = outcome if not reason else f"{outcome}:{reason}"
        out[key] = out.get(key, 0) + 1
    return out


def summarize_capacity_curve(rows: list[dict[str, Any]], window_secs: int = 60) -> list[dict[str, Any]]:
    window = int(max(1, window_secs))
    bins: dict[int, dict[str, int]] = {}
    for row in rows:
        t_ref = row.get("t_bridge_queued")
        if t_ref in (None, ""):
            t_ref = row.get("ts")
        try:
            t = float(t_ref)
        except Exception:
            continue
        bucket = int(t // window) * window
        b = bins.setdefault(bucket, {"commands": 0, "executed": 0, "failed": 0, "timeout": 0})
        b["commands"] += 1
        outcome = str(row.get("outcome", "")).lower()
        reason = str(row.get("rejection_reason", "")).lower()
        if outcome in {"acked", "executed", "sent"}:
            b["executed"] += 1
        if outcome in {"failed", "rejected", "retry_exhausted", "expired"}:
            b["failed"] += 1
        if "timeout" in reason:
            b["timeout"] += 1

    curve: list[dict[str, Any]] = []
    for ts in sorted(bins.keys()):
        v = bins[ts]
        cmds = int(v.get("commands", 0))
        curve.append(
            {
                "window_start_ts": float(ts),
                "commands": cmds,
                "commands_per_sec": float(cmds / window),
                "executed": int(v.get("executed", 0)),
                "failed": int(v.get("failed", 0)),
                "timeouts": int(v.get("timeout", 0)),
            }
        )
    return curve


def summarize_interop_kpis(rows: list[dict[str, Any]], loop_interval_secs: float = 5.0) -> dict[str, Any]:
    stage = summarize_stage_latency(rows)
    e2e = (stage.get("signal_post_to_ack_ms") or {}).get("percentiles", {})
    queue = (stage.get("bridge_queue_wait_ms") or {}).get("percentiles", {})
    poll = (stage.get("poll_delivery_lag_ms") or {}).get("percentiles", {})
    ea = (stage.get("ea_handle_to_ack_ms") or {}).get("percentiles", {})

    total = len(rows)
    unfinalized = sum(1 for r in rows if str(r.get("outcome", "")).lower() in {"pending", "unfinalized"})
    timeout = sum(1 for r in rows if "timeout" in str(r.get("rejection_reason", "")).lower())
    dups = sum(1 for r in rows if "duplicate" in str(r.get("outcome", "")).lower())

    cycle_ms = [
        float(r.get("agent_cycle_ms", 0.0))
        for r in rows
        if isinstance(r.get("agent_cycle_ms", None), (int, float))
    ]
    cpu_p95 = percentile(cycle_ms, 95.0)
    loop_ms = max(float(loop_interval_secs) * 1000.0, 1.0)

    return {
        "signal_post_to_ack_ms": {
            "p50": float(e2e.get("p50", 0.0)),
            "p95": float(e2e.get("p95", 0.0)),
            "p99": float(e2e.get("p99", 0.0)),
        },
        "bridge_queue_wait_ms": {"p95": float(queue.get("p95", 0.0))},
        "poll_delivery_lag_ms": {"p95": float(poll.get("p95", 0.0))},
        "ea_handle_to_ack_ms": {"p95": float(ea.get("p95", 0.0))},
        "unfinalized_signal_rate": float(unfinalized / max(total, 1)),
        "timeout_rate": float(timeout / max(total, 1)),
        "duplicate_suppression_rate": float(dups / max(total, 1)),
        "agent_cycle_cpu_ms_p95": float(cpu_p95),
        "agent_cycle_cpu_util_p95": float(cpu_p95 / loop_ms),
    }
