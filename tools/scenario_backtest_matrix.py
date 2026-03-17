#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import copy
import json
import sys
import time
from pathlib import Path

import numpy as np
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.walk_forward_tune import load_ohlc, run_simulation, compute_metrics


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


def _run_one(
    *,
    symbol: str,
    csv_path: str,
    base_cfg: dict,
    scenario_name: str,
    scenario_overrides: dict,
    bars: int,
    warmup: int,
    simulation_mode: str,
    force_disable_heston: bool,
) -> dict:
    cfg = copy.deepcopy(base_cfg)
    cfg.update(dict(scenario_overrides or {}))
    if force_disable_heston:
        cfg["use_heston_guard"] = False
    # Replay compatibility: allow live_like mode to emulate fresh ticks.
    if str(simulation_mode).strip().lower() == "live_like":
        cfg["audit_replay_mode"] = "live_like"

    df = load_ohlc(Path(csv_path))
    if bars > 0 and len(df) > bars:
        df = df.tail(bars).copy()

    # Keep warmup valid for shortened series.
    w = int(max(32, min(int(warmup), max(len(df) - 60, 32))))
    t0 = time.time()
    eq, trades = run_simulation(
        df=df,
        symbol=symbol,
        cfg=cfg,
        warmup=w,
        end_bar=len(df),
        simulation_mode=simulation_mode,
    )
    m = compute_metrics(eq, trades, w, len(df))
    out = {
        "symbol": symbol,
        "scenario": scenario_name,
        "rows": int(len(df)),
        "warmup": int(w),
        "elapsed_secs": float(time.time() - t0),
        "metrics": m,
    }
    return out


def _aggregate(rows: list[dict]) -> dict:
    if not rows:
        return {}
    metric_keys = [
        "return_pct",
        "max_dd_pct",
        "sharpe",
        "calmar",
        "trade_count",
        "win_rate",
        "profit_factor",
    ]
    out = {}
    for k in metric_keys:
        vals = [float(r["metrics"].get(k, 0.0)) for r in rows]
        out[f"{k}_mean"] = float(np.mean(vals))
        out[f"{k}_median"] = float(np.median(vals))
    # Trade-weighted versions for win rate and PF.
    total_trades = float(sum(float(r["metrics"].get("trade_count", 0.0)) for r in rows))
    out["trade_count_total"] = total_trades
    if total_trades > 0:
        weighted_wr = sum(
            float(r["metrics"].get("win_rate", 0.0)) * float(r["metrics"].get("trade_count", 0.0))
            for r in rows
        ) / total_trades
        out["win_rate_trade_weighted"] = float(weighted_wr)
    else:
        out["win_rate_trade_weighted"] = 0.0
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Parallel scenario backtest matrix over selected FX symbols.")
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--data-dir", default="data/fx_minis")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--bars", type=int, default=700)
    ap.add_argument("--warmup", type=int, default=252)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--simulation-mode", choices=["offline", "live_like"], default="live_like")
    ap.add_argument("--disable-heston", action="store_true")
    ap.add_argument("--output-json", default="data/state/scenario_backtest_matrix.json")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text())
    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    data_dir = Path(args.data_dir)
    scenarios = {
        "base": {},
        "ai_off": {"use_ai_indicator_model": False},
        "horizon_off": {"use_horizon_hold_policy": False},
        "ai_horizon_off": {"use_ai_indicator_model": False, "use_horizon_hold_policy": False},
    }

    jobs: list[tuple[str, str, str, dict]] = []
    missing = []
    for sym in symbols:
        p = data_dir / f"{sym}.csv"
        if not p.exists():
            missing.append(sym)
            continue
        for scen_name, scen_overrides in scenarios.items():
            jobs.append((sym, str(p), scen_name, scen_overrides))

    print(
        f"Scenario matrix start: symbols={len(symbols)} found={len(symbols)-len(missing)} "
        f"scenarios={len(scenarios)} jobs={len(jobs)} bars={args.bars} mode={args.simulation_mode}"
    )
    if missing:
        print(f"Missing symbols skipped: {','.join(missing)}")

    started = time.time()
    results: list[dict] = []
    with cf.ProcessPoolExecutor(max_workers=max(1, int(args.jobs))) as ex:
        fut_map = {
            ex.submit(
                _run_one,
                symbol=sym,
                csv_path=csv_path,
                base_cfg=cfg,
                scenario_name=scen_name,
                scenario_overrides=scen_overrides,
                bars=int(args.bars),
                warmup=int(args.warmup),
                simulation_mode=str(args.simulation_mode),
                force_disable_heston=bool(args.disable_heston),
            ): (sym, scen_name)
            for sym, csv_path, scen_name, scen_overrides in jobs
        }
        done = 0
        total = len(fut_map)
        for fut in cf.as_completed(fut_map):
            sym, scen = fut_map[fut]
            done += 1
            try:
                row = fut.result()
                m = row.get("metrics", {})
                print(
                    f"[{done:03d}/{total:03d}] {sym} {scen}: "
                    f"ret={float(m.get('return_pct',0.0)):.2f}% "
                    f"wr={float(m.get('win_rate',0.0))*100.0:.1f}% "
                    f"pf={float(m.get('profit_factor',0.0)):.2f} "
                    f"tr={float(m.get('trade_count',0.0)):.0f} "
                    f"t={float(row.get('elapsed_secs',0.0)):.1f}s"
                )
                results.append(row)
            except Exception as exc:
                print(f"[{done:03d}/{total:03d}] {sym} {scen}: FAIL ({exc})")

    per_scenario = {}
    for scen_name in scenarios.keys():
        rows = [r for r in results if r.get("scenario") == scen_name]
        per_scenario[scen_name] = {
            "count": len(rows),
            "aggregate": _aggregate(rows),
            "rows": rows,
        }

    summary = {
        "meta": {
            "config": args.config,
            "data_dir": str(data_dir),
            "symbols_requested": symbols,
            "symbols_missing": missing,
            "scenarios": list(scenarios.keys()),
            "bars": int(args.bars),
            "warmup": int(args.warmup),
            "simulation_mode": str(args.simulation_mode),
            "disable_heston": bool(args.disable_heston),
            "elapsed_secs": float(time.time() - started),
        },
        "scenario_results": per_scenario,
    }

    out = Path(args.output_json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2))
    print(f"Wrote: {out}")
    for scen_name in scenarios.keys():
        agg = per_scenario.get(scen_name, {}).get("aggregate", {})
        print(
            f"[{scen_name}] "
            f"ret_mean={float(agg.get('return_pct_mean',0.0)):.3f}% "
            f"wr_w={float(agg.get('win_rate_trade_weighted',0.0))*100.0:.2f}% "
            f"pf_mean={float(agg.get('profit_factor_mean',0.0)):.3f} "
            f"tr_total={float(agg.get('trade_count_total',0.0)):.1f}"
        )


if __name__ == "__main__":
    main()
