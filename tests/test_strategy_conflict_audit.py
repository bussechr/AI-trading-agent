from __future__ import annotations

import copy
import importlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.agents.fx_el_hawkes_agent import FXELAgent
from src.agents.hawkes_micro import OFIProxy
from src.audit.strategy_conflict_metrics import (
    component_nullification_index,
    dead_zone_density,
    redundant_veto_index,
    throughput_suppression_ratio,
)
from tools.strategy_conflict_audit import _guardrail_check, run_conflict_audit
from tools.walk_forward_tune import SimBridge


def _synthetic_df(n: int = 160) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=int(n), freq="h")
    close = 1.1000 + np.linspace(0.0, 0.0200, int(n))
    open_px = np.concatenate(([close[0]], close[:-1]))
    high = np.maximum(open_px, close) + 0.0002
    low = np.minimum(open_px, close) - 0.0002
    vol = np.zeros(int(n), dtype=float)
    return pd.DataFrame(
        {
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
            "volume": vol,
        },
        index=idx,
    )


def _base_agent_cfg() -> dict:
    return {
        "symbols_roots": ["EURUSD"],
        "mini_suffixes": [],
        "el_window": 20,
        "el_ema_span": 5,
        "score_threshold": 0.2,
        "max_concurrent": 2,
        "corr_max": 0.9,
        "use_regime_filter": False,
        "use_hawkes": False,
        "use_lppls": False,
        "use_heston_guard": False,
        "use_live_governance": False,
        "use_portfolio_risk_budget": False,
        "max_margin_level_per_trade_pct": 0.0,
        "max_new_entries_per_minute": 2,
        "max_total_commands_per_minute": 3,
    }


def test_redundant_veto_index_flags_duplicate_execution_vetoes():
    rows = [
        {
            "phase": "execution",
            "outcome": "rejected",
            "rejection_reason": "exec_low_score_ratio",
            "entry_blockers": "soft_low_score,cost_gate",
        },
        {
            "phase": "execution",
            "outcome": "rejected",
            "rejection_reason": "exec_low_sharpe_ratio",
            "entry_blockers": "soft_low_predictive_sharpe",
        },
        {
            "phase": "execution",
            "outcome": "rejected",
            "rejection_reason": "command_rate_entry",
            "entry_blockers": "none",
        },
    ]
    assert redundant_veto_index(rows) == pytest.approx(2.0 / 3.0, abs=1e-12)


def test_redundant_veto_index_drops_with_zero_score_collapse_taxonomy():
    rows = [
        {
            "phase": "execution",
            "outcome": "rejected",
            "rejection_reason": "exec_low_score_ratio",
            "entry_blockers": "soft_low_score,cost_gate",
        },
        {
            "phase": "execution",
            "outcome": "rejected",
            "rejection_reason": "exec_low_sharpe_ratio",
            "entry_blockers": "soft_low_predictive_sharpe",
        },
        {
            "phase": "execution",
            "outcome": "rejected",
            "rejection_reason": "zero_score_collapse",
            "entry_blockers": "zero_score_collapse",
        },
        {
            "phase": "execution",
            "outcome": "rejected",
            "rejection_reason": "command_rate_entry",
            "entry_blockers": "none",
        },
    ]
    out = redundant_veto_index(rows)
    assert out == pytest.approx(0.5, abs=1e-12)
    assert out < (2.0 / 3.0)


def test_conflict_metric_helpers_basic_ranges():
    rows = [
        {
            "phase": "candidate",
            "score_raw": 0.5,
            "score_effective": 0.1,
            "execution_ready": False,
            "score_ratio": 0.9,
            "sharpe_ratio": 0.95,
            "confidence_exec": 50.0,
        },
        {
            "phase": "candidate",
            "score_raw": 0.5,
            "score_effective": 0.5,
            "execution_ready": True,
            "score_ratio": 1.1,
            "sharpe_ratio": 1.0,
            "confidence_exec": 60.0,
        },
    ]
    assert 0.0 <= throughput_suppression_ratio(rows) <= 1.0
    assert 0.0 <= component_nullification_index(rows) <= 1.0
    assert 0.0 <= dead_zone_density(rows) <= 1.0


def test_hawkes_ofi_fallback_handles_zero_volume_without_inert_signal():
    bars = _synthetic_df(40)
    ofi = OFIProxy().compute_ofi(bars)
    assert len(ofi) == len(bars)
    # With monotonic close/open drift and zero volume, fallback should retain directional signal.
    assert float(np.abs(ofi).sum()) > 0.0


def test_command_budget_combined_entry_exit_limits():
    agent = FXELAgent(_base_agent_cfg())

    ok1, reason1 = agent._consume_command_budget(is_entry=True)
    ok2, reason2 = agent._consume_command_budget(is_entry=True)
    ok3, reason3 = agent._consume_command_budget(is_entry=True)
    ok4, reason4 = agent._consume_command_budget(is_entry=False)
    ok5, reason5 = agent._consume_command_budget(is_entry=False)

    assert (ok1, reason1) == (True, "")
    assert (ok2, reason2) == (True, "")
    assert (ok3, reason3) == (False, "command_rate_entry")
    assert (ok4, reason4) == (True, "")
    assert (ok5, reason5) == (False, "command_rate_total")


def test_sim_bridge_close_position_bool_semantics():
    bridge = SimBridge()
    idx = pd.Timestamp("2025-01-01 00:00:00")
    bar = pd.Series({"open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005})
    bridge.set_bar("EURUSD", bar, float(idx.timestamp()), 1)
    bridge.send("BUY", "EURUSD", lots=0.1, magic=246810)

    assert bridge.close_position("EURUSD", magic=246810) is True
    assert bridge.close_position("EURUSD", magic=246810) is False


def test_live_like_replay_audit_mode_emits_tick_freshness(monkeypatch):
    import tools.walk_forward_tune as wft

    wft = importlib.reload(wft)

    class TickProbeAgent:
        seen_missing = 0

        def __init__(self, cfg: dict):
            self.cfg = dict(cfg)
            self.opened = False

        def act(self, equity: float, md: dict[str, pd.DataFrame], all_symbols_catalog=None) -> None:
            del equity, all_symbols_catalog
            if not md:
                return
            sym = next(iter(md.keys()))
            df = md[sym]
            if "last_tick_ts" not in df.attrs:
                self.__class__.seen_missing += 1
                return
            if not self.opened:
                wft.agent_mod.send("BUY", sym, lots=0.1, magic=246810)
                self.opened = True

    monkeypatch.setattr(wft, "FXELAgent", TickProbeAgent)
    TickProbeAgent.seen_missing = 0

    df = _synthetic_df(140)
    cfg = {
        "symbols_roots": ["EURUSD"],
        "mini_suffixes": [],
        "avg_spread_pips": 0.8,
        "audit_replay_mode": "live_like",
    }

    eq, trades = wft.run_simulation(
        df=df,
        symbol="EURUSD",
        cfg=cfg,
        warmup=64,
        end_bar=len(df),
        simulation_mode="live_like",
    )

    assert TickProbeAgent.seen_missing == 0
    assert len(eq) > 0
    assert len(trades) > 0


def test_strategy_conflict_audit_integration_writes_outputs(tmp_path):
    data_dir = tmp_path / "fx_minis"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_dir = tmp_path / "audit"

    sym = "EURUSD"
    df = _synthetic_df(150)
    df_out = df.reset_index().rename(columns={"index": "time"})
    df_out.to_csv(data_dir / f"{sym}.csv", index=False)

    base_cfg = yaml.safe_load(Path("src/config/fx_el_minis.yaml").read_text())
    cfg = copy.deepcopy(base_cfg)
    cfg.update(
        {
            "symbols_roots": [sym],
            "active_symbols": [sym],
            "mini_suffixes": [],
            "use_heston_guard": False,
            "use_lppls": False,
            "use_hawkes": False,
            "use_live_governance": False,
            "use_portfolio_risk_budget": False,
            "use_execution_quality_gate": False,
            "require_fresh_ticks": False,
        }
    )

    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=True), encoding="utf-8")

    summary = run_conflict_audit(
        config_path=cfg_path,
        data_dir=data_dir,
        symbols=[sym],
        bars=150,
        warmup=64,
        modes=["offline"],
        output_dir=out_dir,
    )

    assert (out_dir / "strategy_conflict_audit.json").exists()
    assert (out_dir / "strategy_conflict_audit_rows.csv").exists()
    assert (out_dir / "strategy_conflict_audit_aggregate.csv").exists()
    assert "by_scenario" in summary
    assert "base" in summary["by_scenario"]

    # Guardrail structure should exist for non-base scenarios.
    guardrails = dict(summary.get("guardrails", {}) or {})
    if guardrails:
        sample = next(iter(guardrails.values()))
        assert "offline" in sample


def test_guardrail_check_thresholds():
    base = {"trade_count_mean": 10.0, "profit_factor_mean": 1.20, "max_dd_pct_mean": 5.0}
    good = {"trade_count_mean": 13.5, "profit_factor_mean": 1.05, "max_dd_pct_mean": 5.8}
    bad = {"trade_count_mean": 12.0, "profit_factor_mean": 0.90, "max_dd_pct_mean": 7.0}

    assert _guardrail_check(base, good)["pass"] is True
    assert _guardrail_check(base, bad)["pass"] is False
