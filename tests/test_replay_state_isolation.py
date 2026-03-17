from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _ensure_dummy_risk_manager() -> None:
    """
    walk_forward_tune imports FXELAgent at module import time. On this branch,
    src.agents.risk_manager may be absent, so provide a tiny test double.
    """
    if "src.agents.risk_manager" in sys.modules:
        return
    mod = types.ModuleType("src.agents.risk_manager")

    class RiskManager:
        def __init__(self, cfg: dict):
            self.trailing_mult = float(cfg.get("risk_trailing_mult", 3.0))
            self.risk_per_trade = float(cfg.get("risk_per_trade_pct", 0.01))
            self.target_r = float(cfg.get("risk_reward_target", 2.0))
            self.positions: dict = {}

        def update_position_state(self, **kwargs) -> None:
            del kwargs

        def check_exit(self, *args, **kwargs) -> tuple[bool, str]:
            del args, kwargs
            return False, ""

    mod.RiskManager = RiskManager
    sys.modules["src.agents.risk_manager"] = mod


def _ensure_dummy_bridge_client() -> None:
    """
    Provide the bridge API symbols expected by fx_el_hawkes_agent import-time wiring.
    """
    if "src.execution.mt4_bridge_client" in sys.modules:
        return
    mod = types.ModuleType("src.execution.mt4_bridge_client")

    def _noop(*args, **kwargs):
        del args, kwargs
        return None

    def _positions(*args, **kwargs):
        del args, kwargs
        return []

    mod.send = _noop
    mod.post_visuals = _noop
    mod.get_positions = _positions
    mod.update_thought = _noop
    mod.post_decisions = _noop
    mod.close_position = _noop
    mod.close_all = _noop
    mod.get_state_meta = lambda *args, **kwargs: {}
    mod.get_cached_signal_ids = lambda *args, **kwargs: set()
    sys.modules["src.execution.mt4_bridge_client"] = mod


def _import_walk_forward_tune():
    _ensure_dummy_risk_manager()
    _ensure_dummy_bridge_client()
    import tools.walk_forward_tune as wft

    return importlib.reload(wft)


def _synthetic_df(n: int = 96) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=int(n), freq="h")
    close = 1.1000 + np.linspace(0.0, 0.0400, int(n))
    open_px = np.concatenate(([close[0]], close[:-1]))
    high = np.maximum(open_px, close) + 0.0002
    low = np.minimum(open_px, close) - 0.0002
    return pd.DataFrame(
        {
            "open": open_px,
            "high": high,
            "low": low,
            "close": close,
        },
        index=idx,
    )


class _StateSensitiveProbeAgent:
    """
    Opens one position on first act(). Side depends on ai_indicator_state_path file
    *if and only if* the path is non-empty.
    """

    seen_paths: list[tuple[str, str]] = []

    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        self._opened = False
        self._side = "BUY"

        ai_path = str(self.cfg.get("ai_indicator_state_path", "") or "")
        dir_path = str(self.cfg.get("direction_state_path", "") or "")
        self.__class__.seen_paths.append((ai_path, dir_path))

        if ai_path:
            payload = json.loads(Path(ai_path).read_text(encoding="utf-8"))
            side = str(payload.get("force_side", "BUY")).upper()
            if side in {"BUY", "SELL"}:
                self._side = side

    def act(self, equity: float, md: dict[str, pd.DataFrame], all_symbols_catalog=None) -> None:
        del equity, all_symbols_catalog
        if self._opened or (not md):
            return
        import tools.walk_forward_tune as wft

        symbol = next(iter(md.keys()))
        wft.agent_mod.send(self._side, symbol, lots=1.0, magic=246810)
        self._opened = True


def test_run_simulation_is_stateless_against_on_disk_ai_state(tmp_path, monkeypatch):
    """
    Regression guard:
    if replay stops forcing ai_indicator_state_path="" the metrics below diverge
    because the probe agent uses BUY vs SELL from two different state files.
    """
    wft = _import_walk_forward_tune()
    monkeypatch.setattr(wft, "FXELAgent", _StateSensitiveProbeAgent)
    _StateSensitiveProbeAgent.seen_paths.clear()

    buy_state = tmp_path / "ai_buy.json"
    sell_state = tmp_path / "ai_sell.json"
    buy_state.write_text(json.dumps({"force_side": "BUY"}), encoding="utf-8")
    sell_state.write_text(json.dumps({"force_side": "SELL"}), encoding="utf-8")

    df = _synthetic_df(96)
    warmup = 12
    end_bar = len(df)
    base_cfg = {
        "avg_spread_pips": 0.8,
        "use_ai_indicator_model": True,
        "symbols_roots": ["EURUSD"],
        "mini_suffixes": [],
    }

    eq_buy, trades_buy = wft.run_simulation(
        df=df,
        symbol="EURUSD",
        cfg={**base_cfg, "ai_indicator_state_path": str(buy_state)},
        warmup=warmup,
        end_bar=end_bar,
        simulation_mode="live_like",
    )
    eq_sell, trades_sell = wft.run_simulation(
        df=df,
        symbol="EURUSD",
        cfg={**base_cfg, "ai_indicator_state_path": str(sell_state)},
        warmup=warmup,
        end_bar=end_bar,
        simulation_mode="live_like",
    )

    m_buy = wft.compute_metrics(eq_buy, trades_buy, warmup, end_bar)
    m_sell = wft.compute_metrics(eq_sell, trades_sell, warmup, end_bar)

    # Integration assertion: replay path must blank persistence paths in agent cfg.
    assert _StateSensitiveProbeAgent.seen_paths
    assert all(ai == "" and direction == "" for ai, direction in _StateSensitiveProbeAgent.seen_paths)

    # Ensure there is a meaningful replay outcome, then require path-independent metrics.
    assert float(m_buy.get("trade_count", 0.0)) > 0.0
    assert m_buy == pytest.approx(m_sell, rel=0.0, abs=1e-12)
