from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tools.edge_audit import build_edge_audit, write_edge_audit


def _mk_runtime_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE position_snapshots (ts REAL, positions_json TEXT)")
    conn.execute("CREATE TABLE account_snapshots (ts REAL, equity REAL)")

    snapshots = [
        (1.0, [{"ticket": 1, "symbol": "EURUSD", "type": 0, "lots": 0.1, "profit": 0.0}]),
        (2.0, [{"ticket": 1, "symbol": "EURUSD", "type": 0, "lots": 0.1, "profit": 2.0}]),
        (3.0, []),
        (4.0, [{"ticket": 2, "symbol": "EURUSD", "type": 1, "lots": 0.1, "profit": 0.0}]),
        (5.0, [{"ticket": 2, "symbol": "EURUSD", "type": 1, "lots": 0.1, "profit": -1.0}]),
        (6.0, []),
        (7.0, [{"ticket": 3, "symbol": "EURUSD", "type": 1, "lots": 0.1, "profit": 0.0}]),
        (8.0, [{"ticket": 3, "symbol": "EURUSD", "type": 1, "lots": 0.1, "profit": 3.0}]),
        (9.0, []),
    ]
    for ts, positions in snapshots:
        conn.execute(
            "INSERT INTO position_snapshots(ts, positions_json) VALUES(?, ?)",
            (float(ts), json.dumps(positions)),
        )

    equities = [(1.0, 10000.0), (2.0, 10010.0), (3.0, 10008.0), (4.0, 10012.0)]
    for ts, eq in equities:
        conn.execute(
            "INSERT INTO account_snapshots(ts, equity) VALUES(?, ?)",
            (float(ts), float(eq)),
        )
    conn.commit()
    conn.close()


def test_edge_audit_builds_metrics_and_benchmarks(tmp_path: Path):
    db_path = tmp_path / "runtime.db"
    _mk_runtime_db(db_path)

    trace_path = tmp_path / "strategy_trace.jsonl"
    trace_rows = [
        {"phase": "candidate", "side": "SELL", "score_ratio_exec": 0.20},
        {"phase": "candidate", "side": "SELL", "score_ratio_exec": 0.50},
        {"phase": "candidate", "side": "NONE", "score_ratio_exec": 0.80},
        {"phase": "candidate", "side": "BUY", "score_ratio_exec": 1.20},
    ]
    trace_path.write_text("\n".join(json.dumps(r) for r in trace_rows), encoding="utf-8")

    out = build_edge_audit(runtime_db=db_path, trace_path=trace_path, seed=7)
    live = dict(out.get("live_metrics", {}) or {})

    assert int(live.get("closed_trades", 0)) == 3
    assert int(live.get("wins", 0)) == 2
    assert int(live.get("losses", 0)) == 1
    assert float(live.get("directional_hit_rate", 0.0)) == pytest.approx(2.0 / 3.0, abs=1e-9)
    assert float(live.get("expectancy", 0.0)) == pytest.approx((2.0 - 1.0 + 3.0) / 3.0, abs=1e-9)
    assert float(live.get("profit_factor", 0.0)) == pytest.approx(5.0, abs=1e-9)
    assert float(live.get("abstain_rate", 0.0)) == pytest.approx(0.25, abs=1e-9)
    entry_quality = dict(live.get("entry_quality_distribution", {}) or {})
    assert int(entry_quality.get("samples", 0)) == 4
    assert int((entry_quality.get("weak_lt_0_35", {}) or {}).get("count", 0)) == 1
    assert int((entry_quality.get("medium_0_35_to_0_70", {}) or {}).get("count", 0)) == 1
    assert int((entry_quality.get("strong_gte_0_70", {}) or {}).get("count", 0)) == 2

    benches = dict(out.get("benchmarks", {}) or {})
    assert int((benches.get("random_side_baseline", {}) or {}).get("closed_trades", 0)) == 3
    assert int((benches.get("always_sell_baseline", {}) or {}).get("closed_trades", 0)) == 3
    sig = dict(out.get("significance", {}) or {})
    assert int(sig.get("n_directional", 0)) == 3
    assert 0.0 <= float(sig.get("p_value", 1.0)) <= 1.0


def test_edge_audit_writes_latest_json_and_markdown(tmp_path: Path):
    report = {
        "status": "ok",
        "inputs": {"runtime_db": "x", "strategy_trace": "y"},
        "live_metrics": {
            "closed_trades": 1,
            "directional_hit_rate": 1.0,
            "expectancy": 2.0,
            "profit_factor": 3.0,
            "max_drawdown_pct": 0.5,
            "abstain_rate": 0.0,
        },
        "benchmarks": {
            "random_side_baseline": {"expectancy": 0.0},
            "always_sell_baseline": {"expectancy": -1.0},
        },
        "significance": {"p_value": 0.1, "significant": False},
        "deltas": {
            "edge_vs_random_hit_delta": 0.2,
            "edge_vs_random_expectancy_delta": 2.0,
            "edge_vs_always_sell_expectancy_delta": 3.0,
        },
        "recommendation": {"full_gate_pass": False, "interim_fail_at_25": False, "summary": "x"},
    }

    json_path, md_path = write_edge_audit(report, tmp_path / "edge")
    assert json_path.exists()
    assert md_path.exists()
    assert "Edge Audit" in md_path.read_text(encoding="utf-8")
