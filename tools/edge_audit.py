#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _load_json(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _position_key(pos: dict[str, Any]) -> str:
    ticket = _safe_int(pos.get("ticket", 0), 0)
    if ticket > 0:
        return f"ticket:{ticket}"
    symbol = str(pos.get("symbol", "")).strip().upper()
    ptype = str(pos.get("type", "")).strip()
    open_time = f"{_safe_float(pos.get('open_time', 0.0), 0.0):.0f}"
    open_price = f"{_safe_float(pos.get('open_price', 0.0), 0.0):.6f}"
    lots = f"{_safe_float(pos.get('lots', 0.0), 0.0):.3f}"
    magic = str(_safe_int(pos.get("magic", 0), 0))
    return f"fallback:{symbol}:{ptype}:{open_time}:{open_price}:{lots}:{magic}"


def _side_from_position(pos: dict[str, Any]) -> str:
    ptype = pos.get("type", -1)
    if isinstance(ptype, str):
        up = str(ptype).strip().upper()
        if up in {"BUY", "SELL"}:
            return up
    ptype_i = _safe_int(ptype, -1)
    if ptype_i == 0:
        return "BUY"
    if ptype_i == 1:
        return "SELL"
    return "NONE"


def _drawdown_pct(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = float(values[0])
    max_dd = 0.0
    for v in values:
        x = float(v)
        if x > peak:
            peak = x
        if peak > 0:
            dd = (peak - x) / peak
            if dd > max_dd:
                max_dd = dd
    return float(max_dd * 100.0)


def _profit_factor(pnls: list[float]) -> float:
    gross_profit = float(sum(p for p in pnls if p > 0.0))
    gross_loss = float(abs(sum(p for p in pnls if p < 0.0)))
    if gross_loss <= 1e-12:
        return float("inf") if gross_profit > 0 else 0.0
    return float(gross_profit / gross_loss)


def _metrics_from_pnls(
    pnls: list[float],
    sides: list[str],
    *,
    max_drawdown_pct: float,
) -> dict[str, Any]:
    n = len(pnls)
    wins = int(sum(1 for p in pnls if p > 0.0))
    losses = int(sum(1 for p in pnls if p < 0.0))
    directional_n = int(wins + losses)
    hit_rate = float(wins / directional_n) if directional_n > 0 else 0.0
    expectancy = float(sum(pnls) / n) if n > 0 else 0.0
    side_buy = int(sum(1 for s in sides if str(s).upper() == "BUY"))
    side_sell = int(sum(1 for s in sides if str(s).upper() == "SELL"))
    side_total = int(max(side_buy + side_sell, 1))
    return {
        "closed_trades": int(n),
        "wins": int(wins),
        "losses": int(losses),
        "directional_hit_rate": float(hit_rate),
        "expectancy": float(expectancy),
        "profit_factor": float(_profit_factor(pnls)),
        "max_drawdown_pct": float(max_drawdown_pct),
        "side_distribution": {
            "buy_count": int(side_buy),
            "sell_count": int(side_sell),
            "buy_share": float(side_buy / side_total),
            "sell_share": float(side_sell / side_total),
        },
    }


def _binom_two_sided_pvalue(k: int, n: int, p0: float = 0.5) -> float:
    if n <= 0:
        return 1.0

    def _pmf(i: int) -> float:
        return math.comb(n, i) * (p0**i) * ((1.0 - p0) ** (n - i))

    obs = _pmf(int(k))
    acc = 0.0
    for i in range(0, n + 1):
        v = _pmf(i)
        if v <= obs + 1e-15:
            acc += v
    return float(min(max(acc, 0.0), 1.0))


def _load_trace_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            txt = line.strip()
            if not txt:
                continue
            obj = _load_json(txt, {})
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _entry_quality_distribution(candidate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    ratios = [_safe_float(r.get("score_ratio_exec", r.get("score_ratio", 0.0)), 0.0) for r in candidate_rows]
    n = int(len(ratios))
    weak_n = int(sum(1 for v in ratios if v < 0.35))
    medium_n = int(sum(1 for v in ratios if 0.35 <= v < 0.70))
    strong_n = int(sum(1 for v in ratios if v >= 0.70))
    denom = float(max(n, 1))
    return {
        "samples": int(n),
        "weak_lt_0_35": {"count": int(weak_n), "share": float(weak_n / denom)},
        "medium_0_35_to_0_70": {"count": int(medium_n), "share": float(medium_n / denom)},
        "strong_gte_0_70": {"count": int(strong_n), "share": float(strong_n / denom)},
    }


def _abstain_rate(candidate_rows: list[dict[str, Any]]) -> float:
    n = int(len(candidate_rows))
    if n <= 0:
        return 0.0
    abstain_n = int(sum(1 for r in candidate_rows if str(r.get("side", "NONE")).upper() not in {"BUY", "SELL"}))
    return float(abstain_n / n)


def _load_equity_curve(conn: sqlite3.Connection) -> list[float]:
    out: list[float] = []
    try:
        rows = conn.execute(
            "SELECT equity FROM account_snapshots WHERE equity IS NOT NULL ORDER BY ts ASC"
        ).fetchall()
    except Exception:
        return out
    for row in rows:
        eq = _safe_float(row[0], float("nan"))
        if math.isfinite(eq) and eq > 0:
            out.append(eq)
    return out


def _load_closed_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT ts, positions_json FROM position_snapshots ORDER BY ts ASC"
        ).fetchall()
    except Exception:
        return []

    active: dict[str, dict[str, Any]] = {}
    closed: list[dict[str, Any]] = []
    for row in rows:
        ts = _safe_float(row[0], 0.0)
        positions = _load_json(row[1], [])
        if not isinstance(positions, list):
            positions = []
        current: dict[str, dict[str, Any]] = {}
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            key = _position_key(pos)
            side = _side_from_position(pos)
            info = {
                "symbol": str(pos.get("symbol", "")).strip().upper(),
                "side": str(side),
                "lots": _safe_float(pos.get("lots", 0.0), 0.0),
                "open_price": _safe_float(pos.get("open_price", 0.0), 0.0),
                "open_time": _safe_float(pos.get("open_time", 0.0), 0.0),
                "profit": _safe_float(pos.get("profit", 0.0), 0.0),
            }
            current[key] = info
            if key not in active:
                active[key] = {
                    "key": key,
                    "symbol": info["symbol"],
                    "side": info["side"],
                    "open_ts": ts,
                    "open_time": info["open_time"],
                    "open_price": info["open_price"],
                    "lots": info["lots"],
                    "last_profit": info["profit"],
                    "last_seen_ts": ts,
                }
            else:
                rec = active[key]
                rec["last_profit"] = info["profit"]
                rec["last_seen_ts"] = ts

        for key in list(active.keys()):
            if key in current:
                continue
            rec = active.pop(key)
            pnl = _safe_float(rec.get("last_profit", 0.0), 0.0)
            side = str(rec.get("side", "NONE")).upper()
            if side not in {"BUY", "SELL"}:
                continue
            closed.append(
                {
                    "symbol": str(rec.get("symbol", "")),
                    "side": side,
                    "open_ts": _safe_float(rec.get("open_ts", 0.0), 0.0),
                    "close_ts": float(ts),
                    "lots": _safe_float(rec.get("lots", 0.0), 0.0),
                    "pnl": float(pnl),
                }
            )
    return closed


def _market_side_from_trade(side: str, pnl: float) -> str:
    side_up = str(side).upper()
    if side_up not in {"BUY", "SELL"}:
        return "NONE"
    if pnl > 0.0:
        return side_up
    if pnl < 0.0:
        return "SELL" if side_up == "BUY" else "BUY"
    return "NONE"


def _baseline_from_trades(
    trades: list[dict[str, Any]],
    *,
    chooser: Callable[[int, dict[str, Any]], str],
    max_drawdown_pct: float,
) -> dict[str, Any]:
    baseline_pnls: list[float] = []
    baseline_sides: list[str] = []
    for idx, tr in enumerate(trades):
        pnl = float(_safe_float(tr.get("pnl", 0.0), 0.0))
        abs_pnl = abs(pnl)
        market_side = _market_side_from_trade(str(tr.get("side", "NONE")), pnl)
        pick = str(chooser(idx, tr)).upper()
        if pick not in {"BUY", "SELL"}:
            pick = "SELL"
        if market_side == "NONE":
            bpnl = 0.0
        elif pick == market_side:
            bpnl = abs_pnl
        else:
            bpnl = -abs_pnl
        baseline_sides.append(pick)
        baseline_pnls.append(float(bpnl))
    return _metrics_from_pnls(
        baseline_pnls,
        baseline_sides,
        max_drawdown_pct=max_drawdown_pct,
    )


def build_edge_audit(
    *,
    runtime_db: Path,
    trace_path: Path,
    seed: int = 42,
) -> dict[str, Any]:
    db_err = ""
    closed_trades: list[dict[str, Any]] = []
    equity_curve: list[float] = []
    if runtime_db.exists():
        try:
            conn = sqlite3.connect(str(runtime_db))
            closed_trades = _load_closed_trades(conn)
            equity_curve = _load_equity_curve(conn)
            conn.close()
        except Exception as exc:
            db_err = f"{exc.__class__.__name__}: {exc}"
    else:
        db_err = f"Runtime DB not found: {runtime_db}"

    trace_rows = _load_trace_rows(trace_path)
    candidate_rows = [r for r in trace_rows if str(r.get("phase", "")).lower() == "candidate"]

    pnls = [float(_safe_float(t.get("pnl", 0.0), 0.0)) for t in closed_trades]
    sides = [str(t.get("side", "NONE")).upper() for t in closed_trades]

    if equity_curve:
        max_dd_pct = _drawdown_pct(equity_curve)
    else:
        pnl_curve = []
        cur = 0.0
        for p in pnls:
            cur += float(p)
            pnl_curve.append(cur)
        max_dd_pct = _drawdown_pct(pnl_curve)

    live_metrics = _metrics_from_pnls(pnls, sides, max_drawdown_pct=max_dd_pct)
    live_metrics["abstain_rate"] = float(_abstain_rate(candidate_rows))
    live_metrics["entry_quality_distribution"] = _entry_quality_distribution(candidate_rows)

    rng = random.Random(int(seed))
    random_baseline = _baseline_from_trades(
        closed_trades,
        chooser=lambda _i, _t: rng.choice(["BUY", "SELL"]),
        max_drawdown_pct=max_dd_pct,
    )
    always_sell_baseline = _baseline_from_trades(
        closed_trades,
        chooser=lambda _i, _t: "SELL",
        max_drawdown_pct=max_dd_pct,
    )

    wins = int(live_metrics.get("wins", 0))
    losses = int(live_metrics.get("losses", 0))
    directional_n = int(wins + losses)
    hit_rate = float(live_metrics.get("directional_hit_rate", 0.0))
    pvalue = _binom_two_sided_pvalue(wins, directional_n, p0=0.5)

    hit_delta_vs_random = float(
        live_metrics.get("directional_hit_rate", 0.0) - random_baseline.get("directional_hit_rate", 0.0)
    )
    exp_delta_vs_random = float(live_metrics.get("expectancy", 0.0) - random_baseline.get("expectancy", 0.0))
    exp_delta_vs_always_sell = float(
        live_metrics.get("expectancy", 0.0) - always_sell_baseline.get("expectancy", 0.0)
    )

    buy_share = float((live_metrics.get("side_distribution", {}) or {}).get("buy_share", 0.5))
    sell_share = float((live_metrics.get("side_distribution", {}) or {}).get("sell_share", 0.5))
    side_guard_ok = bool(max(buy_share, sell_share) <= 0.85)
    base_gate = {
        "closed_trades_min": 50,
        "directional_hit_rate_min": 0.53,
        "expectancy_min": 0.0,
        "max_drawdown_pct_max": 6.0,
    }
    closed_n = int(live_metrics.get("closed_trades", 0))
    full_gate_pass = bool(
        closed_n >= int(base_gate["closed_trades_min"])
        and float(live_metrics.get("directional_hit_rate", 0.0)) >= float(base_gate["directional_hit_rate_min"])
        and float(live_metrics.get("expectancy", 0.0)) > float(base_gate["expectancy_min"])
        and float(live_metrics.get("max_drawdown_pct", 0.0)) <= float(base_gate["max_drawdown_pct_max"])
        and exp_delta_vs_random > 0.0
        and exp_delta_vs_always_sell > 0.0
        and side_guard_ok
    )
    interim_fail_at_25 = bool(closed_n >= 25 and not full_gate_pass)

    return {
        "generated_at": float(time.time()),
        "inputs": {
            "runtime_db": str(runtime_db),
            "strategy_trace": str(trace_path),
            "seed": int(seed),
        },
        "status": "ok" if not db_err else "partial",
        "errors": {"runtime_db": str(db_err)} if db_err else {},
        "live_metrics": live_metrics,
        "benchmarks": {
            "random_side_baseline": random_baseline,
            "always_sell_baseline": always_sell_baseline,
        },
        "significance": {
            "test": "binomial_two_sided_hit_rate_vs_0.50",
            "wins": int(wins),
            "losses": int(losses),
            "n_directional": int(directional_n),
            "hit_rate": float(hit_rate),
            "p_value": float(pvalue),
            "alpha": 0.05,
            "significant": bool(pvalue < 0.05),
        },
        "deltas": {
            "edge_vs_random_hit_delta": float(hit_delta_vs_random),
            "edge_vs_random_expectancy_delta": float(exp_delta_vs_random),
            "edge_vs_always_sell_expectancy_delta": float(exp_delta_vs_always_sell),
        },
        "recommendation": {
            "full_gate_pass": bool(full_gate_pass),
            "full_gate": dict(base_gate),
            "side_guard_ok": bool(side_guard_ok),
            "interim_fail_at_25": bool(interim_fail_at_25),
            "tighter_profile_if_fail": (
                {"score_threshold_delta": +0.02, "exec_min_score_ratio_delta": +0.05}
                if interim_fail_at_25
                else {}
            ),
            "summary": (
                "PASS: live edge exceeds benchmarks and risk gate."
                if full_gate_pass
                else "FAIL/INCOMPLETE: continue retune and collect more closed trades."
            ),
        },
    }


def _as_md(report: dict[str, Any]) -> str:
    live = dict(report.get("live_metrics", {}) or {})
    benches = dict(report.get("benchmarks", {}) or {})
    rand = dict(benches.get("random_side_baseline", {}) or {})
    sell = dict(benches.get("always_sell_baseline", {}) or {})
    sig = dict(report.get("significance", {}) or {})
    deltas = dict(report.get("deltas", {}) or {})
    rec = dict(report.get("recommendation", {}) or {})
    lines = [
        "# Edge Audit",
        "",
        f"- Status: `{report.get('status', 'unknown')}`",
        f"- Runtime DB: `{(report.get('inputs', {}) or {}).get('runtime_db', '')}`",
        f"- Strategy Trace: `{(report.get('inputs', {}) or {}).get('strategy_trace', '')}`",
        "",
        "## Live Metrics",
        f"- Closed trades: `{int(live.get('closed_trades', 0))}`",
        f"- Directional hit-rate: `{float(live.get('directional_hit_rate', 0.0)):.4f}`",
        f"- Expectancy: `{float(live.get('expectancy', 0.0)):.6f}`",
        f"- Profit factor: `{float(live.get('profit_factor', 0.0)):.4f}`",
        f"- Max DD (%): `{float(live.get('max_drawdown_pct', 0.0)):.4f}`",
        f"- Abstain rate: `{float(live.get('abstain_rate', 0.0)):.4f}`",
        "",
        "## Benchmarks",
        f"- Random side expectancy: `{float(rand.get('expectancy', 0.0)):.6f}`",
        f"- Always-sell expectancy: `{float(sell.get('expectancy', 0.0)):.6f}`",
        "",
        "## Deltas",
        f"- Hit delta vs random: `{float(deltas.get('edge_vs_random_hit_delta', 0.0)):.6f}`",
        f"- Expectancy delta vs random: `{float(deltas.get('edge_vs_random_expectancy_delta', 0.0)):.6f}`",
        f"- Expectancy delta vs always-sell: `{float(deltas.get('edge_vs_always_sell_expectancy_delta', 0.0)):.6f}`",
        "",
        "## Significance",
        f"- Binomial p-value: `{float(sig.get('p_value', 1.0)):.6f}`",
        f"- Significant @ 0.05: `{bool(sig.get('significant', False))}`",
        "",
        "## Recommendation",
        f"- Full gate pass: `{bool(rec.get('full_gate_pass', False))}`",
        f"- Interim fail @25: `{bool(rec.get('interim_fail_at_25', False))}`",
        f"- Summary: {str(rec.get('summary', ''))}",
        "",
    ]
    return "\n".join(lines)


def write_edge_audit(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "edge_audit_latest.json"
    md_path = out_dir / "edge_audit_latest.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_as_md(report), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Deterministic edge audit against runtime DB and strategy trace.")
    ap.add_argument("--runtime-db", default="data/state/runtime_v2.db")
    ap.add_argument("--trace", default="data/state/audit/strategy_trace.jsonl")
    ap.add_argument("--out-dir", default="data/state/audit/edge")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    report = build_edge_audit(
        runtime_db=Path(args.runtime_db),
        trace_path=Path(args.trace),
        seed=int(args.seed),
    )
    json_path, md_path = write_edge_audit(report, Path(args.out_dir))
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
