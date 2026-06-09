"""Autonomous self-improvement loop over REAL digital-twin backtests.

Closes the cycle on real strategy economics:

  observe -> diagnose(pnl_by_close_reason) -> propose(config change-set)
    -> backtest(twin on TRAIN + OOS) -> evaluate(robust objective) -> accept best

It evaluates a directed set of config change-sets (informed by the diagnosis that
the model-driven lifecycle exits churn winners flat) on BOTH an in-sample and an
out-of-sample window, and selects the most robust config:

  * require net PnL > 0 in BOTH windows (no curve-fit to one window)
  * guardrail: reject if max drawdown worse than --max-dd-pct in either window
  * objective: min(train_net, oos_net)  (optimise the worst case)
  * tie-break: prefer real exits over forced-final-close (penalise configs whose
    PnL is mostly the backtest-end artifact) and more trades (less variance)

Runs the twin as a subprocess with per-candidate FXSTACK_* env overrides. Slow by
design (each twin run is minutes); meant to run in the background to completion.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path("D:/Development/Trading Agent")
PY = ROOT / "fx-quant-stack" / ".venv_win" / "Scripts" / "python.exe"
TWIN = ROOT / "tools" / "fxstack_digital_twin_backtest.py"
BT = ROOT / "artifacts" / "reports" / "backtests"
LOG = ROOT / "artifacts" / "autonomous_loop.log"

# directed change-sets (env overrides on top of defaults), informed by the diagnosis
CANDIDATES: list[dict] = [
    {"name": "baseline", "env": {}},
    {"name": "lcoff", "env": {"FXSTACK_ENABLE_LIFECYCLE_ACTIONS": "false"}},
    {"name": "lcoff_ts4h", "env": {"FXSTACK_ENABLE_LIFECYCLE_ACTIONS": "false", "FXSTACK_HARD_TIME_STOP_SECS": "14400"}},
    {"name": "lcoff_ts8h", "env": {"FXSTACK_ENABLE_LIFECYCLE_ACTIONS": "false", "FXSTACK_HARD_TIME_STOP_SECS": "28800"}},
    {"name": "lcoff_ts24h", "env": {"FXSTACK_ENABLE_LIFECYCLE_ACTIONS": "false", "FXSTACK_HARD_TIME_STOP_SECS": "86400"}},
    {"name": "lcoff_sel065", "env": {"FXSTACK_ENABLE_LIFECYCLE_ACTIONS": "false", "FXSTACK_MIN_TRADE_PROB": "0.65"}},
    {"name": "lcoff_ts8h_sel065", "env": {"FXSTACK_ENABLE_LIFECYCLE_ACTIONS": "false", "FXSTACK_HARD_TIME_STOP_SECS": "28800", "FXSTACK_MIN_TRADE_PROB": "0.65"}},
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_econ(out_dir: Path) -> dict:
    agg_path = out_dir / "aggregate.json"
    if not agg_path.exists():
        return {"ok": False, "error": "no aggregate.json"}
    agg = json.loads(agg_path.read_text(encoding="utf-8"))
    if "aggregate" in agg and isinstance(agg["aggregate"], dict):
        agg = agg["aggregate"]
    reasons = {str(r.get("close_reason")): float(r.get("net_pnl_usd", 0.0) or 0.0) for r in (agg.get("pnl_by_close_reason") or [])}
    net = float(agg.get("net_pnl_usd", 0.0) or 0.0)
    forced = float(reasons.get("forced_final_close", 0.0))
    return {
        "ok": True,
        "net": round(net, 2),
        "trades": int(agg.get("trades", 0) or 0),
        "win_rate": round(float(agg.get("win_rate", 0.0) or 0.0), 3),
        "pf": round(float(agg.get("profit_factor", 0.0) or 0.0), 3),
        "max_dd_pct": round(float(agg.get("max_drawdown_pct", 0.0) or 0.0), 2),
        "forced_close_share": round(forced / net, 2) if net > 1e-6 else (1.0 if forced > 0 else 0.0),
        "by_reason": {k: round(v, 2) for k, v in reasons.items()},
    }


def _run_twin(env_over: dict, start: str, end: str, out_dir: Path) -> dict:
    env = dict(os.environ)
    env["FXSTACK_DATABASE_URL"] = "postgresql+psycopg://fx:fx@localhost:5432/fxstack"
    env["PYTHONPATH"] = str(ROOT)
    env.update(env_over)
    cmd = [str(PY), str(TWIN), "--start-ts", start, "--end-ts", end, "--out-dir", str(out_dir),
           "--no-validate-live-overlap", "--no-emit-decision-history", "--no-recommendations",
           "--no-adaptive-compare-baseline"]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=str(ROOT), env=env, capture_output=True, text=True)
    dt = time.time() - t0
    econ = _read_econ(out_dir)
    econ["secs"] = round(dt, 1)
    econ["rc"] = proc.returncode
    if not econ.get("ok"):
        econ["stderr_tail"] = (proc.stderr or "")[-400:]
    return econ


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", nargs=2, default=["2026-03-23", "2026-03-26"])
    ap.add_argument("--oos", nargs=2, default=["2026-02-16", "2026-02-21"])
    ap.add_argument("--max-dd-pct", type=float, default=-25.0)
    ap.add_argument("--min-trades", type=int, default=3)
    args = ap.parse_args()

    LOG.write_text("", encoding="utf-8")
    log(f"AUTONOMOUS LOOP start: {len(CANDIDATES)} candidates x (train {args.train} + oos {args.oos})")
    results = []
    for c in CANDIDATES:
        name = c["name"]
        log(f"--- candidate {name} env={c['env']} ---")
        tr = _run_twin(c["env"], args.train[0], args.train[1], BT / f"auto_{name}_train")
        log(f"  train: net={tr.get('net')} trades={tr.get('trades')} win={tr.get('win_rate')} dd={tr.get('max_dd_pct')} fc_share={tr.get('forced_close_share')} ({tr.get('secs')}s rc={tr.get('rc')})")
        oo = _run_twin(c["env"], args.oos[0], args.oos[1], BT / f"auto_{name}_oos")
        log(f"  oos:   net={oo.get('net')} trades={oo.get('trades')} win={oo.get('win_rate')} dd={oo.get('max_dd_pct')} fc_share={oo.get('forced_close_share')} ({oo.get('secs')}s rc={oo.get('rc')})")
        ok = bool(tr.get("ok") and oo.get("ok"))
        robust_net = min(tr.get("net", -1e9), oo.get("net", -1e9)) if ok else -1e9
        both_pos = ok and tr.get("net", -1) > 0 and oo.get("net", -1) > 0
        dd_ok = ok and tr.get("max_dd_pct", -100) >= args.max_dd_pct and oo.get("max_dd_pct", -100) >= args.max_dd_pct
        trades_ok = ok and tr.get("trades", 0) >= args.min_trades and oo.get("trades", 0) >= args.min_trades
        # robustness penalty for forced-close reliance (artifact), averaged over windows
        fc = (float(tr.get("forced_close_share", 1.0)) + float(oo.get("forced_close_share", 1.0))) / 2.0 if ok else 1.0
        accept = bool(both_pos and dd_ok and trades_ok)
        score = robust_net * (1.0 - 0.5 * fc) if accept else -1e9  # discount artifact-heavy configs
        results.append({"name": name, "env": c["env"], "train": tr, "oos": oo,
                        "robust_net": robust_net, "fc_share_avg": round(fc, 2), "accept": accept, "score": round(score, 2)})
        log(f"  => accept={accept} robust_net={round(robust_net,2)} fc_avg={round(fc,2)} score={round(score,2)}")

    accepted = [r for r in results if r["accept"]]
    accepted.sort(key=lambda r: r["score"], reverse=True)
    best = accepted[0] if accepted else None
    out = {"train": args.train, "oos": args.oos, "results": results,
           "best": best, "ranking": [(r["name"], r["score"], r["robust_net"], r["fc_share_avg"]) for r in accepted]}
    (ROOT / "artifacts" / "autonomous_loop_result.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    log("=== RANKING (accepted, by robust score) ===")
    for r in accepted:
        log(f"  {r['name']:20} score={r['score']:9.2f} robust_net={r['robust_net']:9.2f} fc_avg={r['fc_share_avg']} "
            f"train={r['train'].get('net')}/{r['train'].get('trades')}t oos={r['oos'].get('net')}/{r['oos'].get('trades')}t")
    if best:
        log(f"BEST robust config: {best['name']} env={best['env']} score={best['score']}")
    else:
        log("NO config met robustness criteria (both windows positive + DD + trades).")
    log("wrote artifacts/autonomous_loop_result.json")


if __name__ == "__main__":
    main()
