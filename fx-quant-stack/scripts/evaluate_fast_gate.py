from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import requests


@dataclass(slots=True)
class GateResult:
    generated_at: str
    baseline_url: str
    candidate_url: str
    duration_secs: int
    checks: dict[str, bool]
    metrics: dict[str, Any]
    passed: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_json(base_url: str, path: str, timeout: float = 2.0) -> dict[str, Any]:
    r = requests.get(f"{base_url.rstrip('/')}{path}", timeout=timeout)
    r.raise_for_status()
    out = r.json()
    return dict(out if isinstance(out, dict) else {})


def _acked_count(metrics: dict[str, Any]) -> int:
    return int((metrics.get("commands", {}) or {}).get("acked", 0) or 0)


def _pending_count(metrics: dict[str, Any]) -> int:
    return int((metrics.get("pending", {}) or {}).get("count", 0) or 0)


def _critical_failures(events: list[dict[str, Any]]) -> int:
    n = 0
    for e in events:
        st = str(e.get("event_status", "")).lower()
        rs = str(e.get("reason", "")).lower()
        if st == "failed" and "duplicate" not in rs:
            n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate fast promotion gate for baseline vs candidate runtime")
    ap.add_argument("--baseline-url", required=True)
    ap.add_argument("--candidate-url", required=True)
    ap.add_argument("--duration-secs", type=int, default=300)
    ap.add_argument("--min-throughput-ratio", type=float, default=0.8)
    ap.add_argument("--max-critical-failures", type=int, default=0)
    ap.add_argument("--max-pending", type=int, default=50)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t0 = time.time()
    b0 = _fetch_json(args.baseline_url, "/v2/metrics")
    c0 = _fetch_json(args.candidate_url, "/v2/metrics")

    time.sleep(max(1, int(args.duration_secs)))

    b1 = _fetch_json(args.baseline_url, "/v2/metrics")
    c1 = _fetch_json(args.candidate_url, "/v2/metrics")

    b_events = list(_fetch_json(args.baseline_url, "/v2/commands/events?limit=500").get("events", []))
    c_events = list(_fetch_json(args.candidate_url, "/v2/commands/events?limit=500").get("events", []))

    b_delta = _acked_count(b1) - _acked_count(b0)
    c_delta = _acked_count(c1) - _acked_count(c0)
    throughput_ratio = float(c_delta / max(1, b_delta))

    checks = {
        "contract_health": _fetch_json(args.candidate_url, "/v2/health").get("status") == "ok",
        "reliability": _critical_failures(c_events) <= int(args.max_critical_failures),
        "throughput": throughput_ratio >= float(args.min_throughput_ratio),
        "pending_load": _pending_count(c1) <= int(args.max_pending),
    }

    metrics = {
        "elapsed_secs": int(time.time() - t0),
        "baseline_acked_delta": int(b_delta),
        "candidate_acked_delta": int(c_delta),
        "throughput_ratio": throughput_ratio,
        "baseline_pending": _pending_count(b1),
        "candidate_pending": _pending_count(c1),
        "baseline_critical_failures": _critical_failures(b_events),
        "candidate_critical_failures": _critical_failures(c_events),
    }

    result = GateResult(
        generated_at=_now_iso(),
        baseline_url=str(args.baseline_url),
        candidate_url=str(args.candidate_url),
        duration_secs=int(args.duration_secs),
        checks=checks,
        metrics=metrics,
        passed=all(checks.values()),
    )

    out = asdict(result)
    print(json.dumps(out, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)

    raise SystemExit(0 if result.passed else 2)


if __name__ == "__main__":
    main()
