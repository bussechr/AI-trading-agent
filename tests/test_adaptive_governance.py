from __future__ import annotations

import pandas as pd

from src.agents.fx_el_hawkes_agent import FXELAgent


def _cfg() -> dict:
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
        "risk_per_trade_pct": 0.01,
        "use_live_governance": True,
        "use_adaptive_risk_envelope": True,
        "gov_soft_dd_min": 0.06,
        "gov_soft_dd_max": 0.09,
        "gov_hard_dd_min": 0.10,
        "gov_hard_dd_max": 0.12,
        "daily_breaker_min": 0.02,
        "daily_breaker_max": 0.03,
        "daily_loss_breaker_pct": 0.03,
        "use_portfolio_risk_budget": False,
        "max_margin_level_per_trade_pct": 0.0,
        "avg_spread_pips": 0.6,
        "pip_value_per_lot": 10.0,
    }


def test_governance_uses_adaptive_envelope_thresholds():
    agent = FXELAgent(_cfg())
    agent.gov_equity_peak = 10_000.0
    agent.last_best_candidate = {"p_trend": 0.75, "confidence": 60.0, "score": 0.5}

    lo = agent._update_governance_state(10_000.0, volatility=0.002, trend_prob=0.75)
    hi = agent._update_governance_state(10_000.0, volatility=0.02, trend_prob=0.25)

    assert 0.06 <= float(lo.get("soft_dd_pct", 0.0)) <= 0.09
    assert 0.10 <= float(lo.get("hard_dd_pct", 0.0)) <= 0.12
    assert 0.02 <= float(lo.get("daily_breaker_pct", 0.0)) <= 0.03

    # High vol + weaker trend should tighten limits.
    assert float(hi.get("soft_dd_pct", 0.0)) <= float(lo.get("soft_dd_pct", 1.0))
    assert float(hi.get("hard_dd_pct", 0.0)) <= float(lo.get("hard_dd_pct", 1.0))
    assert float(hi.get("daily_breaker_pct", 0.0)) <= float(lo.get("daily_breaker_pct", 1.0))


def test_daily_breaker_uses_dynamic_threshold():
    agent = FXELAgent(_cfg())

    # Initialize daily anchor.
    agent._update_daily_loss_breaker(10_000.0)
    assert agent.daily_breaker_active is False

    # Tighten dynamic breaker and verify activation at 1.1% drawdown.
    agent.daily_loss_breaker_dynamic_pct = 0.01
    agent.daily_eq_anchor_day = str(pd.Timestamp.now("UTC").date())
    agent.daily_eq_anchor = 10_000.0
    agent._update_daily_loss_breaker(9_890.0)
    assert agent.daily_breaker_active is True
