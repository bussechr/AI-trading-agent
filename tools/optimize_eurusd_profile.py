#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import json
import random
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.walk_forward_tune import load_ohlc, run_simulation, compute_metrics


TUNE_KEYS = [
    "score_threshold",
    "min_predictive_sharpe",
    "exec_min_confidence",
    "exec_min_score_ratio",
    "exec_min_sharpe_ratio",
    "risk_per_trade_pct",
    "risk_trailing_mult",
    "risk_reward_target",
    "risk_time_limit_hours",
    "risk_stagnation_minutes",
    "risk_exit_min_hold_secs",
    "use_ai_indicator_model",
    "ai_score_weight",
    "use_horizon_hold_policy",
]


def _pnl_stats(trades: list[dict[str, Any]], i0: int, i1: int) -> dict[str, float]:
    rows = [t for t in trades if i0 <= int(t.get("exit_bar", -1)) < i1]
    pnls = [float(t.get("pnl", 0.0)) for t in rows]
    wins = [p for p in pnls if p > 0.0]
    losses = [p for p in pnls if p < 0.0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    pl_ratio = (avg_win / abs(avg_loss)) if (avg_win > 0.0 and avg_loss < 0.0) else 0.0
    return {
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "pl_ratio": float(pl_ratio),
    }


def _score_one(metrics: dict[str, float]) -> float:
    ret = float(metrics.get("return_pct", 0.0))
    dd = float(metrics.get("max_dd_pct", 0.0))
    pf = float(metrics.get("profit_factor", 0.0))
    wr = float(metrics.get("win_rate", 0.0)) * 100.0
    tr = float(metrics.get("trade_count", 0.0))
    plr = float(metrics.get("pl_ratio", 0.0))

    s = ret - (1.20 * dd) + (6.0 * (pf - 1.0)) + (0.25 * (wr - 50.0)) + (1.5 * (plr - 1.0))
    if tr < 80.0:
        s -= (80.0 - tr) * 0.06
    if pf < 0.95:
        s -= (0.95 - pf) * 20.0
    return float(s)


def _robust_score(m_train: dict[str, float], m_valid: dict[str, float]) -> float:
    s_train = _score_one(m_train)
    s_valid = _score_one(m_valid)
    return float((0.45 * s_train) + (0.55 * s_valid) - (0.20 * abs(s_train - s_valid)))


def _build_candidate(base: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    c = {}
    c["score_threshold"] = round(rng.uniform(0.16, 0.44), 3)
    c["min_predictive_sharpe"] = round(rng.uniform(0.02, 0.35), 3)
    c["exec_min_confidence"] = round(rng.uniform(20.0, 70.0), 2)
    c["exec_min_score_ratio"] = round(rng.uniform(0.35, 1.35), 3)
    c["exec_min_sharpe_ratio"] = round(rng.uniform(0.15, 1.10), 3)
    c["risk_per_trade_pct"] = round(rng.uniform(0.008, 0.030), 4)
    c["risk_trailing_mult"] = round(rng.uniform(2.2, 5.2), 2)
    c["risk_reward_target"] = round(rng.uniform(1.3, 3.8), 2)
    c["risk_time_limit_hours"] = round(rng.uniform(10.0, 72.0), 1)
    c["risk_stagnation_minutes"] = int(rng.uniform(20, 220))
    c["risk_exit_min_hold_secs"] = int(rng.uniform(180, 7200))
    c["use_ai_indicator_model"] = bool(rng.random() < 0.55)
    if c["use_ai_indicator_model"]:
        c["ai_score_weight"] = round(rng.uniform(0.05, 0.35), 3)
    else:
        c["ai_score_weight"] = 0.0
    c["use_horizon_hold_policy"] = bool(rng.random() < 0.70)

    # Keep strategy in compatible gate mode.
    c["entry_gate_mode"] = str(base.get("entry_gate_mode", "soft"))
    c["execution_gate_mode"] = str(base.get("execution_gate_mode", "soft"))
    return c


def _evaluate_candidate(
    *,
    candidate_id: int,
    patch: dict[str, Any],
    cfg_base: dict[str, Any],
    data_path: str,
    symbol: str,
    bars_total: int,
    warmup: int,
    sim_mode: str,
    leverage: float,
) -> dict[str, Any]:
    cfg = copy.deepcopy(cfg_base)
    cfg.update(patch)
    cfg["leverage"] = float(leverage)
    # Deterministic replay isolation is handled inside run_simulation.
    df = load_ohlc(Path(data_path))
    if bars_total > 0 and len(df) > bars_total:
        df = df.tail(bars_total).copy()
    n = len(df)
    if n < (warmup + 200):
        raise RuntimeError(f"Insufficient rows {n} for warmup={warmup}")

    split = max(warmup + 120, int(n * 0.5))
    split = min(split, n - 120)
    df_train = df.iloc[:split].copy()
    df_valid = df.iloc[split - warmup :].copy()

    t0 = time.time()
    eq_train, tr_train = run_simulation(
        df=df_train,
        symbol=symbol,
        cfg=cfg,
        warmup=warmup,
        end_bar=len(df_train),
        simulation_mode=sim_mode,
    )
    m_train = compute_metrics(eq_train, tr_train, warmup, len(df_train))
    m_train.update(_pnl_stats(tr_train, warmup, len(df_train)))

    eq_valid, tr_valid = run_simulation(
        df=df_valid,
        symbol=symbol,
        cfg=cfg,
        warmup=warmup,
        end_bar=len(df_valid),
        simulation_mode=sim_mode,
    )
    m_valid = compute_metrics(eq_valid, tr_valid, warmup, len(df_valid))
    m_valid.update(_pnl_stats(tr_valid, warmup, len(df_valid)))

    robust = _robust_score(m_train, m_valid)
    return {
        "candidate_id": int(candidate_id),
        "patch": dict(patch),
        "train": m_train,
        "valid": m_valid,
        "robust_score": float(robust),
        "elapsed_secs": float(time.time() - t0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="EURUSD robust profile optimizer.")
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--data", default="data/fx_minis/EURUSD.csv")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--bars-total", type=int, default=3000)
    ap.add_argument("--warmup", type=int, default=252)
    ap.add_argument("--candidates", type=int, default=14)
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--simulation-mode", choices=["offline", "live_like"], default="live_like")
    ap.add_argument("--leverage", type=float, default=30.0)
    ap.add_argument("--output-json", default="data/state/eurusd_optimization.json")
    ap.add_argument("--output-yaml", default="data/state/eurusd_optimized_patch.yaml")
    args = ap.parse_args()

    cfg_base = yaml.safe_load(Path(args.config).read_text())
    rng = random.Random(int(args.seed))

    patches: list[dict[str, Any]] = []
    # Candidate 0: baseline (but enforce leverage for account realism)
    patches.append({})
    # Candidate 1: AI off baseline ablation
    patches.append({"use_ai_indicator_model": False, "ai_score_weight": 0.0})
    while len(patches) < int(args.candidates):
        p = _build_candidate(cfg_base, rng)
        if p not in patches:
            patches.append(p)

    print(
        f"Optimization start: candidates={len(patches)} bars={args.bars_total} "
        f"mode={args.simulation_mode} leverage={args.leverage}"
    )
    started = time.time()
    rows: list[dict[str, Any]] = []
    with cf.ProcessPoolExecutor(max_workers=max(1, int(args.jobs))) as ex:
        futs = [
            ex.submit(
                _evaluate_candidate,
                candidate_id=i,
                patch=patch,
                cfg_base=cfg_base,
                data_path=str(args.data),
                symbol=str(args.symbol).upper(),
                bars_total=int(args.bars_total),
                warmup=int(args.warmup),
                sim_mode=str(args.simulation_mode),
                leverage=float(args.leverage),
            )
            for i, patch in enumerate(patches)
        ]
        for j, fut in enumerate(cf.as_completed(futs), start=1):
            try:
                row = fut.result()
                rows.append(row)
                vt = row["valid"]
                print(
                    f"[{j:03d}/{len(futs):03d}] c{row['candidate_id']:02d} "
                    f"score={row['robust_score']:.3f} "
                    f"valid ret={float(vt.get('return_pct',0.0)):.2f}% "
                    f"pf={float(vt.get('profit_factor',0.0)):.3f} "
                    f"wr={float(vt.get('win_rate',0.0))*100.0:.1f}% "
                    f"plr={float(vt.get('pl_ratio',0.0)):.3f} "
                    f"tr={float(vt.get('trade_count',0.0)):.0f} "
                    f"t={row['elapsed_secs']:.1f}s"
                )
            except Exception as exc:
                print(f"[{j:03d}/{len(futs):03d}] FAIL ({exc})")

    rows.sort(key=lambda r: float(r.get("robust_score", -1e18)), reverse=True)
    best = rows[0] if rows else {}
    baseline = next((r for r in rows if int(r.get("candidate_id", -1)) == 0), {})
    ai_off = next((r for r in rows if int(r.get("candidate_id", -1)) == 1), {})

    out = {
        "meta": {
            "config": str(args.config),
            "data": str(args.data),
            "symbol": str(args.symbol).upper(),
            "bars_total": int(args.bars_total),
            "warmup": int(args.warmup),
            "candidates": int(len(patches)),
            "jobs": int(args.jobs),
            "seed": int(args.seed),
            "simulation_mode": str(args.simulation_mode),
            "leverage": float(args.leverage),
            "elapsed_secs": float(time.time() - started),
        },
        "best": best,
        "baseline": baseline,
        "ai_off_baseline": ai_off,
        "ranking": rows,
    }
    out_json = Path(args.output_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(out, indent=2))

    patch = dict(best.get("patch", {}))
    out_yaml = Path(args.output_yaml)
    out_yaml.parent.mkdir(parents=True, exist_ok=True)
    out_yaml.write_text(yaml.safe_dump(patch, sort_keys=True))

    print(f"Wrote JSON: {out_json}")
    print(f"Wrote patch YAML: {out_yaml}")
    if best:
        v = best.get("valid", {})
        print(
            f"BEST c{best.get('candidate_id')} score={float(best.get('robust_score',0.0)):.3f} | "
            f"valid ret={float(v.get('return_pct',0.0)):.2f}% pf={float(v.get('profit_factor',0.0)):.3f} "
            f"wr={float(v.get('win_rate',0.0))*100.0:.1f}% plr={float(v.get('pl_ratio',0.0)):.3f}"
        )


if __name__ == "__main__":
    main()
