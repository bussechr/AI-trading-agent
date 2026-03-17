#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import itertools
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from src.audit.strategy_conflict_metrics import summarize_trace_metrics
from tools.walk_forward_tune import compute_metrics, load_ohlc, run_simulation


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

TOGGLE_PATCHES: dict[str, dict[str, Any]] = {
    "no_execution_quality_gate": {"use_execution_quality_gate": False},
    "no_utility_objective": {"use_utility_objective": False},
    "no_governance": {"use_live_governance": False},
    "no_portfolio_risk_budget": {"use_portfolio_risk_budget": False},
    "no_direction_calibration": {"use_directional_calibration": False},
    "no_score_distribution_adaptation": {"use_score_distribution_adaptation": False},
    "no_ai_indicator": {"use_ai_indicator_model": False},
    "no_horizon_hold": {"use_horizon_hold_policy": False},
    "no_hawkes": {"use_hawkes": False},
}


def _trace_path(out_dir: Path, mode: str, scenario: str, symbol: str) -> Path:
    fn = f"trace_{mode}_{scenario}_{symbol}.jsonl".replace("/", "_")
    return out_dir / fn


def _load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            txt = line.strip()
            if not txt:
                continue
            try:
                rows.append(dict(json.loads(txt)))
            except Exception:
                continue
    return rows


def _run_case(
    *,
    base_cfg: dict[str, Any],
    symbol: str,
    df: pd.DataFrame,
    scenario: str,
    patch: dict[str, Any],
    mode: str,
    warmup: int,
    out_dir: Path,
) -> dict[str, Any]:
    cfg = copy.deepcopy(base_cfg)
    cfg.update(dict(patch or {}))
    trace_path = _trace_path(out_dir, mode, scenario, symbol)
    if trace_path.exists():
        trace_path.unlink()

    cfg["audit_trace_enabled"] = True
    cfg["audit_trace_path"] = str(trace_path)
    cfg["audit_sample_rate"] = 1.0
    cfg["audit_replay_mode"] = str(mode)

    w = int(max(32, min(int(warmup), max(len(df) - 60, 32))))
    t0 = time.time()
    eq, trades = run_simulation(
        df=df,
        symbol=symbol,
        cfg=cfg,
        warmup=w,
        end_bar=len(df),
        simulation_mode=mode,
    )
    perf = compute_metrics(eq, trades, w, len(df))
    trace_rows = _load_jsonl_rows(trace_path)
    audit = summarize_trace_metrics(trace_rows)

    return {
        "symbol": str(symbol),
        "scenario": str(scenario),
        "mode": str(mode),
        "rows": int(len(df)),
        "warmup": int(w),
        "elapsed_secs": float(time.time() - t0),
        "trace_path": str(trace_path),
        "metrics": dict(perf),
        "audit": dict(audit),
        "trace_rows": int(len(trace_rows)),
        "executed_rows": int(
            sum(
                1
                for r in trace_rows
                if str(r.get("phase", "")).lower() == "execution"
                and str(r.get("outcome", "")).lower() == "executed"
            )
        ),
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        return {}

    def _mean(path: str, default: float = 0.0) -> float:
        vals = []
        for r in rows:
            cur: Any = r
            for p in path.split("."):
                cur = cur.get(p, {}) if isinstance(cur, dict) else {}
            try:
                vals.append(float(cur))
            except Exception:
                continue
        if not vals:
            return float(default)
        return float(np.mean(vals))

    out = {
        "return_pct_mean": _mean("metrics.return_pct"),
        "max_dd_pct_mean": _mean("metrics.max_dd_pct"),
        "sharpe_mean": _mean("metrics.sharpe"),
        "profit_factor_mean": _mean("metrics.profit_factor"),
        "trade_count_mean": _mean("metrics.trade_count"),
        "throughput_suppression_ratio_mean": _mean("audit.throughput_suppression_ratio"),
        "redundant_veto_index_mean": _mean("audit.redundant_veto_index"),
        "component_nullification_index_mean": _mean("audit.component_nullification_index"),
        "dead_zone_density_mean": _mean("audit.dead_zone_density"),
    }
    out["cases"] = float(len(rows))
    return out


def _guardrail_check(base: dict[str, float], candidate: dict[str, float]) -> dict[str, Any]:
    b_trades = max(float(base.get("trade_count_mean", 0.0)), 1e-9)
    c_trades = float(candidate.get("trade_count_mean", 0.0))
    trade_ratio = c_trades / b_trades

    b_pf = max(float(base.get("profit_factor_mean", 0.0)), 1e-9)
    c_pf = float(candidate.get("profit_factor_mean", 0.0))
    pf_drop_pct = max(0.0, (b_pf - c_pf) / b_pf) * 100.0

    b_dd = max(float(base.get("max_dd_pct_mean", 0.0)), 1e-9)
    c_dd = float(candidate.get("max_dd_pct_mean", 0.0))
    dd_increase_pct = max(0.0, (c_dd - b_dd) / b_dd) * 100.0

    passed = bool(trade_ratio >= 1.30 and pf_drop_pct <= 15.0 and dd_increase_pct <= 20.0)
    return {
        "pass": passed,
        "trade_count_ratio": float(trade_ratio),
        "profit_factor_drop_pct": float(pf_drop_pct),
        "max_dd_increase_pct": float(dd_increase_pct),
    }


def _majority_mode_base(by_scenario: dict[str, dict[str, dict[str, float]]], mode: str) -> dict[str, float]:
    return dict((by_scenario.get("base", {}) or {}).get(mode, {}) or {})


def run_conflict_audit(
    *,
    config_path: Path,
    data_dir: Path,
    symbols: list[str],
    bars: int,
    warmup: int,
    modes: list[str],
    output_dir: Path,
) -> dict[str, Any]:
    cfg = yaml.safe_load(config_path.read_text())
    output_dir.mkdir(parents=True, exist_ok=True)

    datasets: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    for sym in symbols:
        p = data_dir / f"{sym}.csv"
        if not p.exists():
            missing.append(sym)
            continue
        df = load_ohlc(p)
        if bars > 0 and len(df) > bars:
            df = df.tail(bars).copy()
        datasets[sym] = df

    scenarios: dict[str, dict[str, Any]] = {"base": {}}
    scenarios.update(TOGGLE_PATCHES)

    rows: list[dict[str, Any]] = []
    for mode in modes:
        for scen_name, patch in scenarios.items():
            for sym, df in datasets.items():
                rows.append(
                    _run_case(
                        base_cfg=cfg,
                        symbol=sym,
                        df=df,
                        scenario=scen_name,
                        patch=patch,
                        mode=mode,
                        warmup=warmup,
                        out_dir=output_dir,
                    )
                )

    # Aggregate primary pass
    by_scenario: dict[str, dict[str, dict[str, float]]] = {}
    for scen_name in scenarios.keys():
        by_scenario[scen_name] = {}
        for mode in modes:
            scen_rows = [r for r in rows if r["scenario"] == scen_name and r["mode"] == mode]
            by_scenario[scen_name][mode] = _aggregate(scen_rows)

    # Select top suppressors via throughput suppression improvement over base.
    suppressor_scores: list[tuple[str, float]] = []
    for scen_name in TOGGLE_PATCHES.keys():
        score = 0.0
        for mode in modes:
            base_ag = _majority_mode_base(by_scenario, mode)
            cur_ag = dict(by_scenario.get(scen_name, {}).get(mode, {}) or {})
            base_tsr = float(base_ag.get("throughput_suppression_ratio_mean", 0.0))
            cur_tsr = float(cur_ag.get("throughput_suppression_ratio_mean", 0.0))
            score += (base_tsr - cur_tsr)
        suppressor_scores.append((scen_name, float(score / max(len(modes), 1))))
    suppressor_scores.sort(key=lambda kv: kv[1], reverse=True)
    top_scenarios = [s for s, _ in suppressor_scores[:3]]

    pair_scenarios: dict[str, dict[str, Any]] = {}
    for a, b in itertools.combinations(top_scenarios, 2):
        p = dict(TOGGLE_PATCHES.get(a, {}))
        p.update(dict(TOGGLE_PATCHES.get(b, {})))
        pair_scenarios[f"pair_{a}__{b}"] = p

    pair_rows: list[dict[str, Any]] = []
    for mode in modes:
        for scen_name, patch in pair_scenarios.items():
            for sym, df in datasets.items():
                pair_rows.append(
                    _run_case(
                        base_cfg=cfg,
                        symbol=sym,
                        df=df,
                        scenario=scen_name,
                        patch=patch,
                        mode=mode,
                        warmup=warmup,
                        out_dir=output_dir,
                    )
                )

    rows_all = rows + pair_rows
    for scen_name in pair_scenarios.keys():
        by_scenario[scen_name] = {}
        for mode in modes:
            scen_rows = [r for r in rows_all if r["scenario"] == scen_name and r["mode"] == mode]
            by_scenario[scen_name][mode] = _aggregate(scen_rows)

    guardrails: dict[str, dict[str, Any]] = {}
    for scen_name, mode_map in by_scenario.items():
        if scen_name == "base":
            continue
        guardrails[scen_name] = {}
        for mode in modes:
            base_ag = _majority_mode_base(by_scenario, mode)
            cand_ag = dict(mode_map.get(mode, {}) or {})
            guardrails[scen_name][mode] = _guardrail_check(base_ag, cand_ag)

    ranked: list[dict[str, Any]] = []
    for scen_name, mode_map in by_scenario.items():
        if scen_name == "base":
            continue
        score = 0.0
        for mode in modes:
            base_ag = _majority_mode_base(by_scenario, mode)
            cur_ag = dict(mode_map.get(mode, {}) or {})
            score += (
                float(base_ag.get("throughput_suppression_ratio_mean", 0.0))
                - float(cur_ag.get("throughput_suppression_ratio_mean", 0.0))
            )
        ranked.append({"scenario": scen_name, "suppression_improvement": float(score / max(len(modes), 1))})
    ranked.sort(key=lambda x: x["suppression_improvement"], reverse=True)

    accepted = []
    for item in ranked:
        scen = str(item["scenario"])
        if all(bool((guardrails.get(scen, {}).get(mode, {}) or {}).get("pass", False)) for mode in modes):
            accepted.append(scen)

    remediation = {
        "tier1": [
            "Replay/live_like compatibility via audit_replay_mode tick emulation",
            "Execution trace instrumentation across candidate, execution, and exit lifecycle",
            "SimBridge close_position now returns bool to avoid replay close false-negatives",
        ],
        "tier2": accepted,
        "tier3": [
            "Optional Hawkes fallback calibration refinements for sparse/zero-volume exports",
            "Optional consolidation of command-rate limiting to one source of truth",
        ],
    }

    summary = {
        "meta": {
            "config": str(config_path),
            "data_dir": str(data_dir),
            "symbols_requested": list(symbols),
            "symbols_missing": list(missing),
            "symbols_used": sorted(list(datasets.keys())),
            "bars": int(bars),
            "warmup": int(warmup),
            "modes": list(modes),
            "output_dir": str(output_dir),
            "top_suppressors": top_scenarios,
            "pair_scenarios": list(pair_scenarios.keys()),
        },
        "by_scenario": by_scenario,
        "guardrails": guardrails,
        "ranked": ranked,
        "remediation": remediation,
        "rows": rows_all,
    }

    # Write machine-readable + tabular summaries.
    out_json = output_dir / "strategy_conflict_audit.json"
    out_json.write_text(json.dumps(summary, indent=2))

    flat_rows = []
    for r in rows_all:
        fr = {
            "symbol": r["symbol"],
            "scenario": r["scenario"],
            "mode": r["mode"],
            "trace_rows": r.get("trace_rows", 0),
            "executed_rows": r.get("executed_rows", 0),
            "return_pct": float((r.get("metrics") or {}).get("return_pct", 0.0)),
            "max_dd_pct": float((r.get("metrics") or {}).get("max_dd_pct", 0.0)),
            "profit_factor": float((r.get("metrics") or {}).get("profit_factor", 0.0)),
            "trade_count": float((r.get("metrics") or {}).get("trade_count", 0.0)),
            "tsr": float((r.get("audit") or {}).get("throughput_suppression_ratio", 0.0)),
            "rvi": float((r.get("audit") or {}).get("redundant_veto_index", 0.0)),
            "cni": float((r.get("audit") or {}).get("component_nullification_index", 0.0)),
            "dead_zone": float((r.get("audit") or {}).get("dead_zone_density", 0.0)),
        }
        flat_rows.append(fr)
    pd.DataFrame(flat_rows).to_csv(output_dir / "strategy_conflict_audit_rows.csv", index=False)

    ag_rows = []
    for scen_name, mode_map in by_scenario.items():
        for mode in modes:
            row = {"scenario": scen_name, "mode": mode}
            row.update(dict(mode_map.get(mode, {}) or {}))
            g = dict((guardrails.get(scen_name, {}) or {}).get(mode, {}) or {})
            row.update({f"guardrail_{k}": v for k, v in g.items()})
            ag_rows.append(row)
    ag_df = pd.DataFrame(ag_rows)
    ag_df.to_csv(output_dir / "strategy_conflict_audit_aggregate.csv", index=False)
    try:
        ag_df.to_parquet(output_dir / "strategy_conflict_audit_aggregate.parquet", index=False)
    except Exception:
        pass

    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Throughput-first strategy conflict audit")
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--data-dir", default="data/fx_minis")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--bars", type=int, default=700)
    ap.add_argument("--warmup", type=int, default=252)
    ap.add_argument("--modes", default="offline,live_like")
    ap.add_argument("--output-dir", default="data/state/audit")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in str(args.symbols).split(",") if s.strip()]
    modes = [m.strip().lower() for m in str(args.modes).split(",") if m.strip()]
    modes = [m for m in modes if m in {"offline", "live_like"}]
    if not modes:
        modes = ["offline", "live_like"]

    out = run_conflict_audit(
        config_path=Path(args.config),
        data_dir=Path(args.data_dir),
        symbols=symbols,
        bars=int(args.bars),
        warmup=int(args.warmup),
        modes=modes,
        output_dir=Path(args.output_dir),
    )

    print(f"Wrote {Path(args.output_dir) / 'strategy_conflict_audit.json'}")
    print(f"Scenarios ranked: {len(out.get('ranked', []))}")


if __name__ == "__main__":
    main()
