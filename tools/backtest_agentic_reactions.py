"""Backtest of AGENTIC REACTIONS and UPDATING ABILITY — not of trading P&L.

This replays the self-improvement loop as a *walk-forward over time*. The market
is sliced into a timeline of windows. At each window the agent OBSERVES the
current conditions, PROPOSES knob change-sets, JUDGES them (deterministic
backtest + guardrails + walk-forward OOS guard), ACCEPTS or rejects, and UPDATES
its config — carrying that updated config forward to the next window. We then
score each update on the *next, unseen* window and compare the adaptive agent to
a FROZEN agent that never updates.

The question this answers is not "did it make money" but:
  * Does the agent REACT to changing conditions (does it propose/accept updates)?
  * Are those UPDATES any good — do they generalize to the next unseen window?
  * When the regime flips, does it ADAPT (or keep curve-fitting the old regime)?

Headline metric: cumulative next-window objective of the ADAPTIVE agent vs the
STATIC (never-updating) agent, over the whole timeline. Positive => the agent's
updating ability adds value out-of-sample.

Usage (Windows python with full paths):
  .venv_win\\Scripts\\python.exe -m tools.backtest_agentic_reactions \
      [--data regime|synthetic|<parquet-path>] [--windows 8] [--iters 10] \
      [--rows 6000] [--seed 1729] [--oos 0.3] [--run <name>]

Engage the LLM proposer (Qwen) by exporting FXSTACK_LLM_BACKEND=ollama first;
otherwise the deterministic heuristic proposer drives the reactions (still the
full agentic propose->judge->accept->reflect->update loop, just reproducible).
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("D:/Development/Trading Agent/fx-quant-stack")
sys.path.insert(0, str(ROOT / "src"))

from fxstack.improve.evaluator import (  # noqa: E402
    build_regime_shift_dataset,
    build_synthetic_dataset,
    evaluate_config,
    load_parquet_dataset,
)
from fxstack.improve.knobs import default_config, knob_values  # noqa: E402
from fxstack.improve.loop import run_improvement_loop  # noqa: E402
from fxstack.improve.objective import score_metrics  # noqa: E402
from fxstack.settings import get_settings  # noqa: E402


def _load_timeline(data: str, rows: int, seed: int) -> tuple[pd.DataFrame, str]:
    if data == "regime":
        return build_regime_shift_dataset(rows=rows, seed=seed), "regime_shift_synthetic"
    if data == "synthetic":
        return build_synthetic_dataset(rows=rows, seed=seed), "synthetic"
    # treat as a parquet path / dir of real scored signals
    p = Path(data)
    if not p.exists():
        raise SystemExit(f"--data path not found: {p}")
    df = load_parquet_dataset(p)
    if "ts" in df.columns:
        df = df.sort_values("ts").reset_index(drop=True)
    return df, f"real:{p.name}"


def _windows(df: pd.DataFrame, n_windows: int) -> list[pd.DataFrame]:
    ordered = df.sort_values("ts").reset_index(drop=True) if "ts" in df.columns else df.reset_index(drop=True)
    return [w.reset_index(drop=True) for w in np.array_split(ordered, n_windows) if len(w) > 0]


def _obj_on(config: dict, window: pd.DataFrame, *, min_trades: int, max_dd: float) -> dict:
    m = evaluate_config(config, window)
    s = score_metrics(m, min_trades=min_trades, max_drawdown_pct=max_dd)
    return {"objective": float(s.objective), "passed": bool(s.passed_guardrails),
            "trades": float(m.get("trades", 0.0)), "sharpe": float(m.get("sharpe", 0.0))}


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest of agentic reactions and updating ability")
    ap.add_argument("--data", default="regime", help="regime|synthetic|<parquet path>")
    ap.add_argument("--windows", type=int, default=8)
    ap.add_argument("--iters", type=int, default=10, help="loop iterations (reactions) per window")
    ap.add_argument("--rows", type=int, default=6000, help="rows for synthetic timelines")
    ap.add_argument("--seed", type=int, default=1729)
    ap.add_argument("--oos", type=float, default=0.3, help="within-window walk-forward OOS fraction")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--max-dd", type=float, default=12.0)
    ap.add_argument("--run", default="reactions_demo")
    args = ap.parse_args()

    settings = get_settings()
    df, dsrc = _load_timeline(args.data, args.rows, args.seed)
    wins = _windows(df, args.windows)
    if len(wins) < 3:
        raise SystemExit(f"need >=3 windows; got {len(wins)} (rows={len(df)})")

    # regime flip lands at the midpoint row of the regime dataset
    flip_window = None
    if dsrc.startswith("regime"):
        flip_row = len(df) // 2
        sizes = np.cumsum([len(w) for w in wins])
        flip_window = int(np.searchsorted(sizes, flip_row))

    out_dir = ROOT / "artifacts" / "reports" / "agentic_reactions" / args.run
    out_dir.mkdir(parents=True, exist_ok=True)

    frozen_config = default_config(settings)      # the never-updating baseline agent
    incumbent = copy.deepcopy(frozen_config)      # the adaptive agent's carried config
    base_knobs = knob_values(frozen_config)

    backend = getattr(settings, "llm_backend", "null")
    print(f"[reactions] dataset={dsrc} rows={len(df)} windows={len(wins)} iters/window={args.iters} "
          f"oos={args.oos} llm_backend={backend} flip_window={flip_window}")
    print(f"[reactions] base knobs={ {k: round(float(v), 4) for k, v in base_knobs.items()} }")

    timeline: list[dict] = []
    cum_adaptive = 0.0
    cum_static = 0.0
    total_reactions = 0
    total_accepted = 0
    windows_updated = 0

    for t in range(len(wins) - 1):
        cur, nxt = wins[t], wins[t + 1]
        mem_path = out_dir / f"reflection_w{t:02d}.jsonl"

        pre_knobs = knob_values(incumbent)
        result = run_improvement_loop(
            dataset=cur,
            base_config=incumbent,
            settings=settings,
            memory_path=mem_path,            # fresh per-window memory: honest "react from here"
            iterations=args.iters,
            seed=args.seed + t,              # vary search per window
            min_trades=args.min_trades,
            max_drawdown_pct=args.max_dd,
            oos_fraction=args.oos,
            emit_experiment=False,           # this is a reactions backtest, not a registration run
            register_experiment=False,
        )
        new_config = result.best_config
        new_knobs = knob_values(new_config)
        # the UPDATE this window applied to the carried config
        update = {k: round(float(new_knobs[k]), 6) for k in new_knobs
                  if float(new_knobs[k]) != float(pre_knobs.get(k, new_knobs[k]))}
        reactions = int(result.summary.get("memory_entries", 0))   # proposals considered
        accepted = int(result.accepted)
        total_reactions += reactions
        total_accepted += accepted
        if update:
            windows_updated += 1

        # walk-forward: how do the three configs do on the NEXT, unseen window?
        adaptive_next = _obj_on(new_config, nxt, min_trades=args.min_trades, max_dd=args.max_dd)
        static_next = _obj_on(frozen_config, nxt, min_trades=args.min_trades, max_dd=args.max_dd)
        preupdate_next = _obj_on(incumbent, nxt, min_trades=args.min_trades, max_dd=args.max_dd)
        cum_adaptive += adaptive_next["objective"]
        cum_static += static_next["objective"]

        row = {
            "window": t,
            "is_flip_window": (flip_window is not None and t == flip_window),
            "rows": int(len(cur)),
            "reactions_considered": reactions,
            "accepted_updates": accepted,
            "proposer_usage": dict(result.proposer_usage),
            "fallback_count": int(result.fallback_count),
            "insample_baseline_obj": round(float(result.baseline_objective), 5),
            "insample_best_obj": round(float(result.best_objective), 5),
            "insample_gain": round(float(result.best_objective - result.baseline_objective), 5),
            "update_applied": update,
            "next_window_adaptive_obj": round(adaptive_next["objective"], 5),
            "next_window_preupdate_obj": round(preupdate_next["objective"], 5),
            "next_window_static_obj": round(static_next["objective"], 5),
            "update_helped_next": round(adaptive_next["objective"] - preupdate_next["objective"], 5),
            "adaptive_vs_static_next": round(adaptive_next["objective"] - static_next["objective"], 5),
        }
        timeline.append(row)
        incumbent = new_config  # carry the update forward

        flag = "  <-- REGIME FLIP" if row["is_flip_window"] else ""
        print(f"[w{t:02d}]{flag} react={reactions:2d} accept={accepted} "
              f"IS:{row['insample_baseline_obj']:+.4f}->{row['insample_best_obj']:+.4f} "
              f"update={update or '{}'} | next obj adapt={row['next_window_adaptive_obj']:+.4f} "
              f"static={row['next_window_static_obj']:+.4f} "
              f"(adapt-static={row['adaptive_vs_static_next']:+.4f})")

    final_knobs = knob_values(incumbent)
    drifted = {k: {"from": round(float(base_knobs[k]), 6), "to": round(float(final_knobs[k]), 6)}
               for k in final_knobs if float(final_knobs[k]) != float(base_knobs.get(k, final_knobs[k]))}

    summary = {
        "dataset": dsrc,
        "windows": len(wins),
        "iters_per_window": args.iters,
        "llm_backend": backend,
        "flip_window": flip_window,
        "total_reactions_considered": total_reactions,
        "total_accepted_updates": total_accepted,
        "accept_rate": round(total_accepted / total_reactions, 4) if total_reactions else 0.0,
        "windows_with_update": windows_updated,
        "update_rate": round(windows_updated / (len(wins) - 1), 4),
        "cum_next_window_obj_adaptive": round(cum_adaptive, 5),
        "cum_next_window_obj_static": round(cum_static, 5),
        "updating_ability_value": round(cum_adaptive - cum_static, 5),
        "final_config_drift": drifted,
    }
    (out_dir / "timeline.json").write_text(
        json.dumps({"summary": summary, "timeline": timeline}, indent=2, sort_keys=True), encoding="utf-8")

    print("\n=== AGENTIC REACTIONS BACKTEST — SUMMARY ===")
    print(f"  reactions considered : {total_reactions}  (accepted updates: {total_accepted}, "
          f"accept-rate {summary['accept_rate']})")
    print(f"  windows that updated : {windows_updated}/{len(wins)-1}  (update-rate {summary['update_rate']})")
    print(f"  cum next-window obj  : adaptive {cum_adaptive:+.4f}  vs  static {cum_static:+.4f}")
    print(f"  >> updating-ability value (adaptive - static, out-of-sample): {summary['updating_ability_value']:+.4f}")
    if flip_window is not None:
        post = [r for r in timeline if r["window"] >= flip_window]
        reacted = sum(1 for r in post if r["update_applied"])
        print(f"  regime flip @ window {flip_window}: agent applied an update in {reacted}/{len(post)} post-flip windows")
    print(f"  config drift over timeline: {drifted or '{} (no net drift)'}")
    print(f"  wrote {out_dir / 'timeline.json'}")


if __name__ == "__main__":
    main()
