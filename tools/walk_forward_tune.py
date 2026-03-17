#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import random
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

# Resolve repository root for direct script execution.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.agents.fx_el_hawkes_agent import FXELAgent
import src.agents.fx_el_hawkes_agent as agent_mod


LOG = logging.getLogger("walk_forward_tune")


TUNED_PARAMS = [
    "score_threshold",
    "dir_fast_weight",
    "dir_slow_weight",
    "direction_calib_strength",
    "direction_bias_penalty",
    "min_predictive_sharpe",
    "exec_min_confidence",
    "exec_min_score_ratio",
    "exec_min_sharpe_ratio",
    "conf_risk_power",
    "soft_blocked_risk_scale",
    "risk_per_trade_pct",
]


@dataclass
class Fold:
    train_end: int
    test_end: int


class SimBridge:
    def __init__(self, *, contract_size: float = 100000.0):
        self.contract_size = float(contract_size)
        self.balance = 10000.0
        self.positions: list[dict[str, Any]] = []
        self.closed_trades: list[dict[str, Any]] = []
        self._ticket = 1
        self._prices: dict[str, float] = {}
        self.current_time = 0.0
        self.current_bar_idx = -1

    def _pnl(self, pos: dict[str, Any], price: float) -> float:
        side = str(pos["side"]).upper()
        lots = float(pos["lots"])
        entry = float(pos["open_price"])
        if side == "BUY":
            return (float(price) - entry) * self.contract_size * lots
        return (entry - float(price)) * self.contract_size * lots

    def _close(self, pos: dict[str, Any], close_price: float, reason: str) -> None:
        pnl = self._pnl(pos, close_price)
        self.balance += pnl
        self.closed_trades.append(
            {
                "symbol": pos["symbol"],
                "side": pos["side"],
                "lots": float(pos["lots"]),
                "entry_price": float(pos["open_price"]),
                "exit_price": float(close_price),
                "entry_time": float(pos["open_time"]),
                "exit_time": float(self.current_time),
                "entry_bar": int(pos["open_bar"]),
                "exit_bar": int(self.current_bar_idx),
                "pnl": float(pnl),
                "reason": str(reason),
            }
        )

    def set_bar(self, symbol: str, row: pd.Series, ts: float, bar_idx: int) -> None:
        self.current_time = float(ts)
        self.current_bar_idx = int(bar_idx)
        self._prices[str(symbol).upper()] = float(row["close"])
        # Apply protective exits using OHLC of current bar for existing positions.
        # Conservative fill: SL takes precedence when both SL/TP are hit in one bar.
        high = float(row["high"])
        low = float(row["low"])
        keep: list[dict[str, Any]] = []
        for pos in self.positions:
            if str(pos["symbol"]).upper() != str(symbol).upper():
                keep.append(pos)
                continue
            if int(pos["open_bar"]) >= int(bar_idx):
                keep.append(pos)
                continue
            sl = pos.get("sl_price")
            tp = pos.get("tp_price")
            side = str(pos["side"]).upper()
            closed = False
            if side == "BUY":
                if sl is not None and low <= float(sl):
                    self._close(pos, float(sl), "sl")
                    closed = True
                elif tp is not None and high >= float(tp):
                    self._close(pos, float(tp), "tp")
                    closed = True
            else:
                if sl is not None and high >= float(sl):
                    self._close(pos, float(sl), "sl")
                    closed = True
                elif tp is not None and low <= float(tp):
                    self._close(pos, float(tp), "tp")
                    closed = True
            if not closed:
                keep.append(pos)
        self.positions = keep

    def send(
        self,
        side: str,
        symbol: str,
        *,
        lots: float = 0.0,
        tp_cash: float | None = None,
        sl_price: float | None = None,
        tp_price: float | None = None,
        magic: int = 246810,
        max_retries: int = 3,
    ) -> None:
        del tp_cash, max_retries
        sym = str(symbol).upper()
        px = float(self._prices.get(sym, 0.0))
        if px <= 0:
            return
        lots_eff = max(float(lots), 0.01)
        pos = {
            "ticket": int(self._ticket),
            "symbol": sym,
            "side": str(side).upper(),
            "type": 0 if str(side).upper() == "BUY" else 1,
            "lots": float(lots_eff),
            "open_price": float(px),
            "open_time": float(self.current_time),
            "open_bar": int(self.current_bar_idx),
            "magic": int(magic),
            "sl_price": float(sl_price) if sl_price is not None else None,
            "tp_price": float(tp_price) if tp_price is not None else None,
        }
        self._ticket += 1
        self.positions.append(pos)

    def close_position(self, symbol: str, magic: int = 246810, max_retries: int = 3) -> bool:
        del max_retries
        sym = str(symbol).upper()
        px = float(self._prices.get(sym, 0.0))
        keep: list[dict[str, Any]] = []
        closed = 0
        for pos in self.positions:
            if str(pos["symbol"]).upper() == sym and int(pos.get("magic", 0)) == int(magic):
                close_px = px if px > 0 else float(pos["open_price"])
                self._close(pos, close_px, "agent_close")
                closed += 1
            else:
                keep.append(pos)
        self.positions = keep
        return bool(closed > 0)

    def close_all(self, max_retries: int = 3) -> None:
        del max_retries
        for pos in list(self.positions):
            sym = str(pos["symbol"]).upper()
            px = float(self._prices.get(sym, float(pos["open_price"])))
            self._close(pos, px, "close_all")
        self.positions = []

    def get_positions(self, max_retries: int = 1) -> list[dict[str, Any]]:
        del max_retries
        out: list[dict[str, Any]] = []
        for pos in self.positions:
            sym = str(pos["symbol"]).upper()
            px = float(self._prices.get(sym, float(pos["open_price"])))
            out.append(
                {
                    "ticket": int(pos["ticket"]),
                    "symbol": sym,
                    "type": int(pos["type"]),
                    "lots": float(pos["lots"]),
                    "open_price": float(pos["open_price"]),
                    "open_time": float(pos["open_time"]),
                    "magic": int(pos.get("magic", 246810)),
                    "profit": float(self._pnl(pos, px)),
                }
            )
        return out

    def equity(self, symbol_prices: dict[str, float]) -> float:
        pnl = 0.0
        for pos in self.positions:
            sym = str(pos["symbol"]).upper()
            px = float(symbol_prices.get(sym, pos["open_price"]))
            pnl += self._pnl(pos, px)
        return float(self.balance + pnl)

    def force_close_all(self, symbol: str, close_price: float, ts: float, bar_idx: int) -> None:
        self.current_time = float(ts)
        self.current_bar_idx = int(bar_idx)
        self._prices[str(symbol).upper()] = float(close_price)
        self.close_all()


@contextmanager
def patched_bridge(bridge: SimBridge):
    noop = lambda *args, **kwargs: None
    originals = {
        "send": agent_mod.send,
        "post_visuals": agent_mod.post_visuals,
        "get_positions": agent_mod.get_positions,
        "update_thought": agent_mod.update_thought,
        "post_decisions": agent_mod.post_decisions,
        "bc_send": agent_mod.bridge_client.send,
        "bc_close_position": agent_mod.bridge_client.close_position,
        "bc_close_all": getattr(agent_mod.bridge_client, "close_all", None),
        "bc_get_positions": agent_mod.bridge_client.get_positions,
        "bc_post_visuals": agent_mod.bridge_client.post_visuals,
        "bc_update_thought": agent_mod.bridge_client.update_thought,
        "bc_post_decisions": agent_mod.bridge_client.post_decisions,
    }
    try:
        agent_mod.send = bridge.send
        agent_mod.post_visuals = noop
        agent_mod.get_positions = bridge.get_positions
        agent_mod.update_thought = noop
        agent_mod.post_decisions = noop

        agent_mod.bridge_client.send = bridge.send
        agent_mod.bridge_client.close_position = bridge.close_position
        agent_mod.bridge_client.close_all = bridge.close_all
        agent_mod.bridge_client.get_positions = bridge.get_positions
        agent_mod.bridge_client.post_visuals = noop
        agent_mod.bridge_client.update_thought = noop
        agent_mod.bridge_client.post_decisions = noop
        yield
    finally:
        agent_mod.send = originals["send"]
        agent_mod.post_visuals = originals["post_visuals"]
        agent_mod.get_positions = originals["get_positions"]
        agent_mod.update_thought = originals["update_thought"]
        agent_mod.post_decisions = originals["post_decisions"]

        agent_mod.bridge_client.send = originals["bc_send"]
        agent_mod.bridge_client.close_position = originals["bc_close_position"]
        if originals["bc_close_all"] is not None:
            agent_mod.bridge_client.close_all = originals["bc_close_all"]
        agent_mod.bridge_client.get_positions = originals["bc_get_positions"]
        agent_mod.bridge_client.post_visuals = originals["bc_post_visuals"]
        agent_mod.bridge_client.update_thought = originals["bc_update_thought"]
        agent_mod.bridge_client.post_decisions = originals["bc_post_decisions"]


def load_ohlc(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    cols = {c.lower(): c for c in df.columns}
    req = ["open", "high", "low", "close"]
    for c in req:
        if c not in cols:
            raise ValueError(f"Missing required column '{c}' in {csv_path}")
    if "time" in cols:
        t = pd.to_datetime(df[cols["time"]], errors="coerce")
    else:
        t = pd.RangeIndex(len(df))
    out = pd.DataFrame(
        {
            "open": pd.to_numeric(df[cols["open"]], errors="coerce").to_numpy(),
            "high": pd.to_numeric(df[cols["high"]], errors="coerce").to_numpy(),
            "low": pd.to_numeric(df[cols["low"]], errors="coerce").to_numpy(),
            "close": pd.to_numeric(df[cols["close"]], errors="coerce").to_numpy(),
        },
        index=t,
    ).dropna()
    if not isinstance(out.index, pd.DatetimeIndex):
        parsed = pd.to_datetime(out.index, errors="coerce")
        if isinstance(parsed, pd.DatetimeIndex) and parsed.notna().any():
            out.index = parsed
        else:
            out.index = pd.to_datetime(out.index, unit="h", origin="unix", errors="coerce")
    if isinstance(out.index, pd.DatetimeIndex):
        out = out[out.index.notna()]
    out = out[~out.index.duplicated(keep="first")]
    out = out.sort_index()
    return out


def compute_metrics(
    equity_trace: list[dict[str, float]],
    closed_trades: list[dict[str, Any]],
    i0: int,
    i1: int,
) -> dict[str, float]:
    curve = [float(x["equity"]) for x in equity_trace if i0 <= int(x["bar"]) < i1]
    if len(curve) < 2:
        return {
            "return_pct": 0.0,
            "max_dd_pct": 0.0,
            "sharpe": 0.0,
            "calmar": 0.0,
            "trade_count": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
        }
    start = max(curve[0], 1e-9)
    end = curve[-1]
    ret_pct = (end / start - 1.0) * 100.0

    peak = curve[0]
    max_dd = 0.0
    for v in curve:
        peak = max(peak, v)
        dd = (peak - v) / max(peak, 1e-9) * 100.0
        max_dd = max(max_dd, dd)

    r = np.diff(np.asarray(curve)) / np.maximum(np.asarray(curve[:-1]), 1e-9)
    if len(r) >= 2 and float(np.std(r, ddof=0)) > 1e-12:
        sharpe = float(np.mean(r) / np.std(r, ddof=0) * math.sqrt(24.0 * 252.0))
    else:
        sharpe = 0.0

    trades = [t for t in closed_trades if i0 <= int(t["exit_bar"]) < i1]
    n = len(trades)
    wins = [t for t in trades if float(t["pnl"]) > 0]
    losses = [t for t in trades if float(t["pnl"]) < 0]
    gross_pos = float(sum(float(t["pnl"]) for t in wins))
    gross_neg = float(abs(sum(float(t["pnl"]) for t in losses)))
    if gross_neg > 1e-9:
        pf = gross_pos / gross_neg
    elif gross_pos > 0:
        pf = 10.0
    else:
        pf = 0.0

    wr = float(len(wins) / n) if n > 0 else 0.0
    calmar = float(ret_pct / max(max_dd, 1e-9))
    return {
        "return_pct": float(ret_pct),
        "max_dd_pct": float(max_dd),
        "sharpe": float(sharpe),
        "calmar": float(calmar),
        "trade_count": float(n),
        "win_rate": float(wr),
        "profit_factor": float(pf),
    }


def objective(metrics: dict[str, float], mode: str) -> float:
    ret = float(metrics["return_pct"])
    dd = float(metrics["max_dd_pct"])
    sh = float(metrics["sharpe"])
    pf = float(metrics["profit_factor"])
    tr = float(metrics["trade_count"])
    if mode == "conservative":
        score = ret - 2.40 * dd + 0.22 * sh + 0.12 * math.log1p(max(pf, 0.0))
        if tr < 2:
            score -= 12.0
    elif mode == "aggressive":
        score = ret - 1.10 * dd + 0.20 * sh + 0.08 * pf + 0.05 * tr
        if tr < 3:
            score -= 10.0
    else:
        score = ret - 1.70 * dd + 0.28 * sh + 0.14 * math.log1p(max(pf, 0.0))
        if tr < 2:
            score -= 10.0
    return float(score)


def build_candidate_pool(base_cfg: dict[str, Any], n: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    baseline = {k: base_cfg.get(k) for k in TUNED_PARAMS}
    out = [baseline]
    while len(out) < n:
        c = dict(baseline)
        c["score_threshold"] = round(rng.uniform(0.10, 0.44), 3)
        c["dir_fast_weight"] = round(rng.uniform(0.15, 0.60), 3)
        c["dir_slow_weight"] = round(rng.uniform(0.10, 0.50), 3)
        c["direction_calib_strength"] = round(rng.uniform(0.15, 0.60), 3)
        c["direction_bias_penalty"] = round(rng.uniform(0.08, 0.45), 3)
        c["min_predictive_sharpe"] = round(rng.uniform(0.00, 0.30), 3)
        c["exec_min_confidence"] = round(rng.uniform(10.0, 60.0), 2)
        c["exec_min_score_ratio"] = round(rng.uniform(0.20, 0.90), 3)
        c["exec_min_sharpe_ratio"] = round(rng.uniform(0.00, 0.85), 3)
        c["conf_risk_power"] = round(rng.uniform(0.85, 1.55), 3)
        c["soft_blocked_risk_scale"] = round(rng.uniform(0.60, 0.95), 3)
        c["risk_per_trade_pct"] = round(rng.uniform(0.015, 0.050), 4)

        if c["exec_min_sharpe_ratio"] < c["min_predictive_sharpe"]:
            c["exec_min_sharpe_ratio"] = round(c["min_predictive_sharpe"] + 0.05, 3)

        if c not in out:
            out.append(c)
    return out


def build_folds(n_rows: int, warmup: int, min_train: int, test_size: int) -> list[Fold]:
    folds: list[Fold] = []
    train_end = warmup + min_train
    while (train_end + test_size) <= n_rows:
        folds.append(Fold(train_end=train_end, test_end=train_end + test_size))
        train_end += test_size
    return folds


def run_simulation(
    *,
    df: pd.DataFrame,
    symbol: str,
    cfg: dict[str, Any],
    warmup: int,
    end_bar: int,
    simulation_mode: str = "offline",
) -> tuple[list[dict[str, float]], list[dict[str, Any]]]:
    bridge = SimBridge()
    local_cfg = copy.deepcopy(cfg)
    mode = str(simulation_mode).strip().lower()
    if mode not in {"offline", "live_like"}:
        mode = "offline"
    # Faster deterministic tuning: keep execution engine intact, skip heavyweight crash fit.
    # Force stateless replay so persisted online learning state cannot leak across runs.
    local_cfg["use_lppls"] = False
    local_cfg["direction_state_path"] = ""
    local_cfg["ai_indicator_state_path"] = ""
    local_cfg["direction_state_save_secs"] = 10_000_000
    local_cfg["ai_indicator_state_save_secs"] = 10_000_000
    local_cfg["symbols_roots"] = [symbol]
    local_cfg["mini_suffixes"] = []
    local_cfg["use_hawkes"] = bool(local_cfg.get("use_hawkes", True))
    if mode == "offline":
        # Walk-forward replay runs on static bars, so live-feed freshness gating is invalid.
        local_cfg["require_fresh_ticks"] = False
        local_cfg["tick_stale_secs"] = max(int(local_cfg.get("tick_stale_secs", 0)), 10_000_000)
        # In bar replay there is no live order book/cost refresh; hard execution gate can over-filter.
        local_cfg["execution_gate_mode"] = "soft"
        local_cfg["tick_only_mode"] = False
    # Audit replay mode allows live_like bar-replay to emulate tick freshness
    # without changing production live behavior.
    audit_replay_mode = str(local_cfg.get("audit_replay_mode", "offline")).strip().lower()
    replay_tick_compat = bool(mode == "live_like" and audit_replay_mode == "live_like")

    with patched_bridge(bridge):
        agent = FXELAgent(local_cfg)
        equity_trace: list[dict[str, float]] = []
        symbol_u = str(symbol).upper()
        for i in range(warmup, end_bar):
            win = df.iloc[: i + 1].copy()
            win.attrs["spread"] = float(cfg.get("avg_spread_pips", 0.8))
            bar = win.iloc[-1]
            ts = float(win.index[-1].timestamp())
            if replay_tick_compat:
                win.attrs["last_tick_ts"] = float(ts)
                win.attrs["bar_integrity_ok"] = True
            bridge.set_bar(symbol_u, bar, ts, i)
            eq = bridge.equity({symbol_u: float(bar["close"])})
            agent.act(float(eq), {symbol_u: win}, all_symbols_catalog=[symbol_u])
            eq2 = bridge.equity({symbol_u: float(bar["close"])})
            equity_trace.append({"bar": int(i), "equity": float(eq2)})

        if len(df) > 0:
            last = df.iloc[end_bar - 1]
            ts_last = float(df.index[end_bar - 1].timestamp())
            bridge.force_close_all(symbol_u, float(last["close"]), ts_last, end_bar - 1)
            if equity_trace:
                equity_trace[-1]["equity"] = float(bridge.balance)

    return equity_trace, bridge.closed_trades


def aggregate_fold_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    if not rows:
        return {}
    keys = rows[0].keys()
    out: dict[str, float] = {}
    for k in keys:
        out[k] = float(np.mean([float(r[k]) for r in rows]))
    return out


def median_profile(cands: list[dict[str, Any]]) -> dict[str, Any]:
    if not cands:
        return {}
    out: dict[str, Any] = {}
    for p in TUNED_PARAMS:
        vals = [float(c[p]) for c in cands if p in c]
        if not vals:
            continue
        out[p] = float(np.median(np.asarray(vals)))
    return out


def evaluate_static_profile(
    *,
    df: pd.DataFrame,
    symbol: str,
    base_cfg: dict[str, Any],
    profile: dict[str, Any],
    warmup: int,
    simulation_mode: str = "offline",
) -> dict[str, float]:
    cfg = copy.deepcopy(base_cfg)
    cfg.update(profile)
    eq_trace, trades = run_simulation(
        df=df,
        symbol=symbol,
        cfg=cfg,
        warmup=warmup,
        end_bar=len(df),
        simulation_mode=simulation_mode,
    )
    return compute_metrics(eq_trace, trades, warmup, len(df))


def format_delta(base_cfg: dict[str, Any], tuned: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in tuned.items():
        b = base_cfg.get(k)
        if b is None:
            out[k] = {"from": None, "to": v}
            continue
        out[k] = {"from": b, "to": v}
    return out


def run_walk_forward(
    *,
    df: pd.DataFrame,
    symbol: str,
    base_cfg: dict[str, Any],
    warmup: int,
    min_train: int,
    test_size: int,
    n_candidates: int,
    seed: int,
    simulation_mode: str = "offline",
) -> dict[str, Any]:
    folds = build_folds(len(df), warmup, min_train, test_size)
    if not folds:
        raise RuntimeError("No walk-forward folds could be created with current data and settings.")

    candidates = build_candidate_pool(base_cfg, n_candidates, seed)
    modes = ["conservative", "balanced", "aggressive"]
    results: dict[str, Any] = {
        "meta": {
            "rows": len(df),
            "symbol": symbol,
            "warmup_bars": warmup,
            "min_train_bars": min_train,
            "test_size_bars": test_size,
            "folds": [{"train_end": f.train_end, "test_end": f.test_end} for f in folds],
            "candidate_count": len(candidates),
            "simulation_mode": simulation_mode,
            "note": "LPPLS guard disabled during tuning for runtime practicality; execution path remains FXELAgent.act().",
        },
        "profiles": {},
    }

    # Precompute each candidate once to max fold end; reuse for all folds and modes.
    max_end = max(int(f.test_end) for f in folds)
    candidate_runs: list[dict[str, Any]] = []
    for cid, cand in enumerate(candidates):
        LOG.info("Precompute candidate=%d/%d", cid + 1, len(candidates))
        cfg = copy.deepcopy(base_cfg)
        cfg.update(cand)
        eq_trace, trades = run_simulation(
            df=df,
            symbol=symbol,
            cfg=cfg,
            warmup=warmup,
            end_bar=max_end,
            simulation_mode=simulation_mode,
        )
        candidate_runs.append(
            {
                "candidate_id": int(cid),
                "params": copy.deepcopy(cand),
                "equity_trace": eq_trace,
                "trades": trades,
            }
        )

    # Build fold x candidate metric matrix from cached runs.
    matrix: list[dict[str, Any]] = []
    for fold_idx, f in enumerate(folds, start=1):
        row = {"fold": fold_idx, "train_end": f.train_end, "test_end": f.test_end, "candidates": []}
        for run in candidate_runs:
            m_train = compute_metrics(run["equity_trace"], run["trades"], warmup, f.train_end)
            m_test = compute_metrics(run["equity_trace"], run["trades"], f.train_end, f.test_end)
            row["candidates"].append(
                {
                    "candidate_id": int(run["candidate_id"]),
                    "params": copy.deepcopy(run["params"]),
                    "train_metrics": m_train,
                    "test_metrics": m_test,
                }
            )
        matrix.append(row)

    for mode in modes:
        LOG.info("Walk-forward mode=%s", mode)
        chosen_per_fold: list[dict[str, Any]] = []
        oos_rows: list[dict[str, float]] = []
        fold_rows: list[dict[str, Any]] = []
        for row in matrix:
            best: dict[str, Any] | None = None
            best_score = -1e18
            best_train_trades = -1.0
            best_train_return = -1e18
            for cand_row in row["candidates"]:
                s = objective(cand_row["train_metrics"], mode)
                tr_train = float(cand_row["train_metrics"].get("trade_count", 0.0))
                ret_train = float(cand_row["train_metrics"].get("return_pct", 0.0))
                is_better = False
                if s > (best_score + 1e-12):
                    is_better = True
                elif abs(s - best_score) <= 1e-12:
                    if tr_train > (best_train_trades + 1e-9):
                        is_better = True
                    elif abs(tr_train - best_train_trades) <= 1e-9 and ret_train > (best_train_return + 1e-9):
                        is_better = True
                if is_better:
                    best_score = s
                    best_train_trades = tr_train
                    best_train_return = ret_train
                    best = {
                        "candidate_id": int(cand_row["candidate_id"]),
                        "params": copy.deepcopy(cand_row["params"]),
                        "train_metrics": copy.deepcopy(cand_row["train_metrics"]),
                        "test_metrics": copy.deepcopy(cand_row["test_metrics"]),
                        "train_objective": float(s),
                    }
            assert best is not None
            chosen_per_fold.append(copy.deepcopy(best["params"]))
            oos_rows.append(copy.deepcopy(best["test_metrics"]))
            fold_rows.append(
                {
                    "fold": int(row["fold"]),
                    "train_end": int(row["train_end"]),
                    "test_end": int(row["test_end"]),
                    "candidate_id": best["candidate_id"],
                    "train_objective": best["train_objective"],
                    "train_metrics": best["train_metrics"],
                    "test_metrics": best["test_metrics"],
                }
            )
            LOG.info(
                "mode=%s fold=%d selected candidate=%d train_obj=%.3f test_ret=%.2f%% test_dd=%.2f%%",
                mode,
                int(row["fold"]),
                int(best["candidate_id"]),
                float(best["train_objective"]),
                float(best["test_metrics"]["return_pct"]),
                float(best["test_metrics"]["max_dd_pct"]),
            )

        tuned_profile = median_profile(chosen_per_fold)
        static_metrics = evaluate_static_profile(
            df=df,
            symbol=symbol,
            base_cfg=base_cfg,
            profile=tuned_profile,
            warmup=warmup,
            simulation_mode=simulation_mode,
        )
        results["profiles"][mode] = {
            "fold_selection": fold_rows,
            "oos_avg_metrics": aggregate_fold_metrics(oos_rows),
            "tuned_params": tuned_profile,
            "delta_vs_base": format_delta(base_cfg, tuned_profile),
            "static_full_sample_metrics": static_metrics,
        }
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward tuner for FXELAgent config.")
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--data", default="data/fx_minis/EURUSD.csv")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--warmup", type=int, default=252)
    ap.add_argument("--min-train", type=int, default=140)
    ap.add_argument("--test-size", type=int, default=36)
    ap.add_argument("--candidates", type=int, default=18)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--simulation-mode",
        choices=["offline", "live_like"],
        default="offline",
        help="offline: disable live-only freshness blocking + use soft execution gate for bar replay",
    )
    ap.add_argument("--output-json", default="data/state/walk_forward_tuning.json")
    ap.add_argument("--output-yaml", default="data/state/fx_el_profiles.yaml")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    LOG.setLevel(logging.INFO)

    cfg_path = Path(args.config)
    data_path = Path(args.data)
    out_json = Path(args.output_json)
    out_yaml = Path(args.output_yaml)

    base_cfg = yaml.safe_load(cfg_path.read_text())
    df = load_ohlc(data_path)
    if len(df) <= (args.warmup + args.min_train + args.test_size):
        raise RuntimeError(
            f"Not enough rows ({len(df)}) for warmup={args.warmup}, min_train={args.min_train}, test_size={args.test_size}."
        )

    results = run_walk_forward(
        df=df,
        symbol=str(args.symbol).upper(),
        base_cfg=base_cfg,
        warmup=int(args.warmup),
        min_train=int(args.min_train),
        test_size=int(args.test_size),
        n_candidates=int(args.candidates),
        seed=int(args.seed),
        simulation_mode=str(args.simulation_mode),
    )

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(results, indent=2))

    profile_yaml = {
        "conservative": results["profiles"]["conservative"]["tuned_params"],
        "balanced": results["profiles"]["balanced"]["tuned_params"],
        "aggressive": results["profiles"]["aggressive"]["tuned_params"],
    }
    out_yaml.parent.mkdir(parents=True, exist_ok=True)
    out_yaml.write_text(yaml.safe_dump(profile_yaml, sort_keys=True))

    print(f"Wrote JSON report: {out_json}")
    print(f"Wrote profile overrides: {out_yaml}")
    for mode in ("conservative", "balanced", "aggressive"):
        m = results["profiles"][mode]["oos_avg_metrics"]
        print(
            f"[{mode}] OOS avg return={m.get('return_pct', 0.0):.2f}% "
            f"dd={m.get('max_dd_pct', 0.0):.2f}% "
            f"sharpe={m.get('sharpe', 0.0):.2f} "
            f"trades={m.get('trade_count', 0.0):.1f}"
        )


if __name__ == "__main__":
    main()
