"""Self-healing watchdog — the health/diagnose/remediate brain of the agent.

Polls the live bridge + DB for health signals, classifies each subsystem
(ok/warn/fail), and emits a structured remediation plan. It is deliberately
NON-destructive by default: it detects and PLANS the fix (and can log/print the
exact command), leaving execution of process restarts / risk changes to an
operator or an explicitly-authorised auto mode (--auto), because autonomously
restarting live trading processes is a decision that must be opted into.

Checks:
  * bridge_up / database_ok        (/v2/health, /v2/ready)
  * mt4_fresh / ticks_fresh        (heartbeat + tick age)
  * runtime running & not stalled  (runtime_status, progress age)
  * feature freshness              (feature_serving_stale)
  * drawdown within band           (capital governance band / dd)
  * recent command failure rate    (commands table)

Run once for a snapshot, or on an interval (cron / loop) as the healing loop.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path("D:/Development/Trading Agent")
BRIDGE = "http://127.0.0.1:58710"


def _get(path: str, timeout: float = 5.0) -> dict:
    try:
        with urllib.request.urlopen(f"{BRIDGE}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as ex:
        return {"__error__": str(ex)}


def _check(name: str, ok: bool, detail: str, remedy: str | None = None, severity: str = "fail") -> dict:
    return {"check": name, "status": "ok" if ok else severity, "detail": detail,
            "remedy": (None if ok else remedy)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="(reserved) execute safe remediations; default is plan-only")
    ap.add_argument("--db", default="postgresql+psycopg://fx:fx@localhost:5432/fxstack")
    args = ap.parse_args()

    health = _get("/v2/health")
    ready = _get("/v2/ready")
    checks: list[dict] = []

    bridge_up = not health.get("__error__") and str(health.get("status")) == "ok"
    checks.append(_check("bridge_up", bridge_up, str(health.get("status") or health.get("__error__")),
                         remedy="ops/windows/20_start_bridge.bat --background 58710"))
    checks.append(_check("database_ok", str(health.get("database")) == "up", str(health.get("database")),
                         remedy="ops/windows/03_postgres_start.bat"))

    hb_age = float(health.get("heartbeat_age_secs", 9e9) or 9e9)
    checks.append(_check("mt4_fresh", bool(health.get("ticks_fresh")) and hb_age < 30,
                         f"hb_age={hb_age:.1f}s ticks={health.get('tick_status')} syms={health.get('tick_symbols_count')}",
                         remedy="check MT4 terminal + EA attached (chart) + WinInet/DLL imports allowed", severity="warn"))

    rstatus = str(ready.get("runtime_status") or "unknown")
    prog_age = float(ready.get("runtime_last_progress_age_secs", 9e9) or 9e9)
    runtime_ok = rstatus == "running" and prog_age < 180
    checks.append(_check("runtime_running", runtime_ok, f"status={rstatus} progress_age={prog_age:.0f}s phase={ready.get('runtime_phase')}",
                         remedy="ops/windows/21_start_runtime.bat --background 10000 58710"))

    feat_stale = bool(ready.get("feature_serving_stale", False))
    checks.append(_check("features_fresh", not feat_stale, f"feature_serving_stale={feat_stale} source={ready.get('feature_serving_source')}",
                         remedy="restart feature-push worker (24_start_feature_push_worker.bat) / re-run feature build", severity="warn"))

    band = str(ready.get("capitalBand") or ready.get("capital_band") or "")
    qkill = bool((ready.get("orchestration_live") or {}).get("queue_kill_active", False))
    checks.append(_check("capital_band", band not in {"halt", "rollback"} and not qkill,
                         f"band={band} queue_kill={qkill}",
                         remedy="reduce risk (capital entries-only) / investigate breach; auto-rollback should arm", severity="warn"))

    # recent command failure rate
    try:
        import sys
        sys.path.insert(0, str(ROOT / "fx-quant-stack" / "src"))
        from sqlalchemy import create_engine, text
        e = create_engine(args.db)
        now = time.time()
        with e.connect() as c:
            recent = list(c.execute(text("select status, count(*) n from commands where created_at > :t group by status"), {"t": now - 3600}))
            counts = {str(r.status): int(r.n) for r in recent}
        tot = sum(counts.values())
        fail = counts.get("failed", 0) + counts.get("expired", 0)
        rate = (fail / tot) if tot else 0.0
        checks.append(_check("command_health", rate < 0.5 or tot == 0, f"last1h={counts} fail/expire_rate={rate:.2f}",
                             remedy="inspect EA execution / bridge command TTL; clock skew check", severity="warn"))
    except Exception as ex:
        checks.append(_check("command_health", True, f"db check skipped: {ex}", severity="warn"))

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    overall = "fail" if fails else ("warn" if warns else "ok")
    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "overall": overall, "checks": checks,
              "remediation_plan": [{"check": c["check"], "severity": c["status"], "remedy": c["remedy"]} for c in (fails + warns)]}

    out = ROOT / "artifacts" / "self_healing_report.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"=== SELF-HEALING WATCHDOG — overall: {overall.upper()} ({time.strftime('%H:%M:%S')}) ===")
    for c in checks:
        mark = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}[c["status"]]
        print(f"  [{mark}] {c['check']:18} {c['detail']}")
        if c["remedy"]:
            print(f"         remedy: {c['remedy']}")
    if args.auto and (fails or warns):
        print("\n[--auto] reserved: safe remediations would execute here (plan-only in this build).")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
