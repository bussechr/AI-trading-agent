import pandas as pd
import pytest
from unittest.mock import patch

from src.agents.fx_el_hawkes_agent import Decision, FXELAgent


def _base_cfg() -> dict:
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
        "use_live_governance": False,
        "use_portfolio_risk_budget": False,
        "max_margin_level_per_trade_pct": 0.0,
        "avg_spread_pips": 0.6,
        "pip_value_per_lot": 10.0,
    }


def _market_df() -> pd.DataFrame:
    prices = [1.1000 + (0.0001 * i) for i in range(320)]
    df = pd.DataFrame(
        {
            "close": prices,
            "open": prices,
            "high": [p + 0.0002 for p in prices],
            "low": [p - 0.0002 for p in prices],
            "volume": [100] * len(prices),
        }
    )
    df.attrs["spread"] = 0.5
    return df


def test_execution_quality_gate_blocks_low_confidence():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": True,
            "exec_min_confidence": 55.0,
            "exec_min_score_ratio": 0.9,
            "exec_min_sharpe_ratio": 0.8,
        }
    )
    agent = FXELAgent(cfg)

    md = {"EURUSD": _market_df()}
    decision = Decision(symbol="EURUSD", side="BUY", score=0.8)
    agent.last_candidate_map = {
        ("EURUSD", "BUY"): {
            "confidence": 40.0,
            "score_ratio": 1.4,
            "sharpe_ratio": 1.2,
            "blocked_by": "none",
        }
    }

    with patch.object(agent, "decisions", return_value=[decision]):
        with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
            with patch("src.agents.fx_el_hawkes_agent.send") as mock_send:
                with patch("src.agents.fx_el_hawkes_agent.post_visuals"):
                    with patch("src.agents.fx_el_hawkes_agent.update_thought"):
                        with patch("src.agents.fx_el_hawkes_agent.post_decisions"):
                            agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])

    assert not mock_send.called
    assert agent.rejection_stats_cycle.get("exec_low_confidence", 0) >= 1


def test_execution_quality_gate_prefers_exec_or_raw_confidence():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": True,
            "execution_gate_mode": "soft",
            "exec_min_confidence": 55.0,
            "exec_min_score_ratio": 0.9,
            "exec_min_sharpe_ratio": 0.8,
        }
    )
    agent = FXELAgent(cfg)

    md = {"EURUSD": _market_df()}
    decision = Decision(symbol="EURUSD", side="BUY", score=0.8)
    agent.last_candidate_map = {
        ("EURUSD", "BUY"): {
            "confidence": 40.0,       # display/risk confidence (penalized)
            "confidence_raw": 78.0,   # model confidence before blocker penalty
            "confidence_exec": 78.0,  # execution confidence should drive exec gate
            "score_ratio": 1.4,
            "sharpe_ratio": 1.2,
            "blocked_by": "soft_low_score",
        }
    }

    with patch.object(agent, "decisions", return_value=[decision]):
        with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
            with patch("src.agents.fx_el_hawkes_agent.send") as mock_send:
                with patch("src.agents.fx_el_hawkes_agent.post_visuals"):
                    with patch("src.agents.fx_el_hawkes_agent.update_thought"):
                        with patch("src.agents.fx_el_hawkes_agent.post_decisions"):
                            with patch("src.agents.fx_el_hawkes_agent.cost_gate", return_value=True):
                                agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])

    assert mock_send.called
    assert agent.rejection_stats_cycle.get("exec_low_confidence", 0) == 0


def test_confidence_risk_sizing_scales_risk_budget():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": False,
            "use_confidence_risk_sizing": True,
            "conf_risk_floor": 0.5,
            "conf_risk_ceiling": 1.5,
            "conf_risk_power": 1.0,
        }
    )
    agent = FXELAgent(cfg)

    md = {"EURUSD": _market_df()}
    decision = Decision(symbol="EURUSD", side="BUY", score=0.8)

    seen_risk_pcts = []

    def fake_position_size(equity, risk_pct, stop_pips, pip_value, max_lots=5.0):
        seen_risk_pcts.append(float(risk_pct))
        return 0.2

    with patch.object(agent, "decisions", return_value=[decision]):
        with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
            with patch("src.agents.fx_el_hawkes_agent.send"):
                with patch("src.agents.fx_el_hawkes_agent.post_visuals"):
                    with patch("src.agents.fx_el_hawkes_agent.update_thought"):
                        with patch("src.agents.fx_el_hawkes_agent.post_decisions"):
                            with patch("src.agents.risk_utils.calculate_position_size", side_effect=fake_position_size):
                                agent.last_candidate_map = {
                                    ("EURUSD", "BUY"): {
                                        "confidence": 100.0,
                                        "score_ratio": 1.5,
                                        "sharpe_ratio": 1.5,
                                        "blocked_by": "none",
                                    }
                                }
                                agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])

                                agent.last_candidate_map = {
                                    ("EURUSD", "BUY"): {
                                        "confidence": 0.0,
                                        "score_ratio": 1.5,
                                        "sharpe_ratio": 1.5,
                                        "blocked_by": "none",
                                    }
                                }
                                agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])

    assert len(seen_risk_pcts) >= 2
    assert seen_risk_pcts[0] == pytest.approx(0.015, abs=1e-6)
    assert seen_risk_pcts[1] == pytest.approx(0.005, abs=1e-6)


def test_equity_scaled_pip_target_sets_dynamic_min_lot():
    cfg = _base_cfg()
    cfg.update(
        {
            "min_trade_lot": 0.01,
            "lot_step_hint": 0.01,
            "use_equity_scaled_pip_target": True,
            "pip_value_target_pct_equity": 0.00004,  # $0.40/pip at $10k
        }
    )
    agent = FXELAgent(cfg)

    # EURUSD-like pip value: ~$10/pip per 1.0 lot.
    floor_10k = agent._effective_min_trade_lot(equity=10000.0, pip_value_symbol=10.0)
    floor_500 = agent._effective_min_trade_lot(equity=500.0, pip_value_symbol=10.0)

    assert floor_10k == pytest.approx(0.04, abs=1e-6)
    # At small equity, broker floor dominates.
    assert floor_500 == pytest.approx(0.01, abs=1e-6)


def test_regime_sharpe_threshold_relaxes_in_range():
    cfg = _base_cfg()
    cfg.update(
        {
            "min_predictive_sharpe": 0.20,
            "min_predictive_sharpe_trend_mult": 1.00,
            "min_predictive_sharpe_range_mult": 0.40,
            "min_predictive_sharpe_transition_mult": 0.70,
        }
    )
    agent = FXELAgent(cfg)

    assert agent._regime_sharpe_threshold("trend") == pytest.approx(0.20, abs=1e-9)
    assert agent._regime_sharpe_threshold("range") == pytest.approx(0.08, abs=1e-9)
    assert agent._regime_sharpe_threshold("transition") == pytest.approx(0.14, abs=1e-9)


def test_range_regime_does_not_block_moderate_sharpe_by_default_floor():
    cfg = _base_cfg()
    cfg.update(
        {
            "entry_gate_mode": "soft",
            "min_predictive_sharpe": 0.20,
            "min_predictive_sharpe_range_mult": 0.40,
            "use_heston_guard": False,
            "use_execution_quality_gate": False,
        }
    )
    agent = FXELAgent(cfg)

    df = _market_df()
    md = {"EURUSD": df}

    agent.score_symbol = lambda _df, _sym: (
        0.25,
        {
            "p_trend": 0.20,  # range bucket
            "vol": 0.001,
            "predictive_sharpe": 0.10,
            "hawkes_n": 1.0,
            "lppls_hazard": 0.0,
            "score": 0.25,
        },
    )

    _ = agent.decisions(md, held_symbols=set())
    top = dict(agent.last_best_candidate)
    assert top.get("blocked_by") != "low_predictive_sharpe"


def test_score_distribution_adaptation_caps_over_strict_threshold():
    cfg = _base_cfg()
    cfg.update(
        {
            "score_threshold": 0.40,
            "use_score_distribution_adaptation": True,
            "score_distribution_window": 200,
            "score_distribution_min_samples": 40,
            "score_distribution_quantile": 0.75,
            "score_distribution_mult": 1.00,
            "score_distribution_floor_mult": 0.25,
        }
    )
    agent = FXELAgent(cfg)

    sym = "EURUSD"
    # Typical recent absolute scores are much smaller than fixed threshold.
    agent.score_abs_history[sym] = [0.03] * 60 + [0.06] * 40
    adapted, q_ref, n_ref = agent._adaptive_score_threshold(sym, threshold_now=0.52, exclude_latest=False)

    assert n_ref == 100
    assert q_ref == pytest.approx(0.06, abs=1e-9)
    assert adapted < 0.52
    assert adapted == pytest.approx(0.10, abs=1e-9)  # floor: base_th(0.40) * floor_mult(0.25)


def test_score_distribution_adaptation_excludes_current_bar_sample():
    cfg = _base_cfg()
    cfg.update(
        {
            "score_threshold": 0.40,
            "use_score_distribution_adaptation": True,
            "score_distribution_window": 20,
            "score_distribution_min_samples": 10,
            "score_distribution_quantile": 0.99,
            "score_distribution_mult": 1.00,
            "score_distribution_floor_mult": 0.10,
        }
    )
    agent = FXELAgent(cfg)

    sym = "EURUSD"
    # Latest bar is an outlier spike that should not influence threshold on the same decision cycle.
    agent.score_abs_history[sym] = [0.10] * 10 + [1.00]

    adapted_excl, _, _ = agent._adaptive_score_threshold(sym, threshold_now=1.00, exclude_latest=True)
    adapted_incl, _, _ = agent._adaptive_score_threshold(sym, threshold_now=1.00, exclude_latest=False)

    assert adapted_excl == pytest.approx(0.10, abs=1e-9)
    assert adapted_incl > adapted_excl


def test_execution_confidence_base_not_double_penalized_by_score_component():
    cfg = _base_cfg()
    cfg.update({"min_predictive_sharpe": 0.2})
    agent = FXELAgent(cfg)

    diag = {
        "predictive_sharpe": 0.3,
        "predictive_sharpe_aligned": 0.3,
        "p_trend": 0.7,
        "hawkes_n": 1.0,
        "model_cohesion": 0.8,
    }
    # Intentionally weak score ratio (below threshold), but good non-score execution conditions.
    out = agent._trade_confidence_metrics(
        sc=0.05,
        diag=diag,
        spread_pips=0.5,
        expected_move=0.004,
        pip_value_per_lot=10.0,
        equity=10000.0,
        lot_fraction=0.10,
        score_threshold=0.20,
        sharpe_threshold=0.20,
        heston_ratio=1.0,
    )
    assert float(out["score_ratio"]) < 1.0
    assert float(out["confidence_exec_base"]) > float(out["confidence"])


def test_best_candidate_prefers_execution_ready_symbol():
    cfg = _base_cfg()
    cfg.update(
        {
            "symbols_roots": ["EURUSD", "GBPUSD"],
            "use_execution_quality_gate": True,
            "exec_min_confidence": 55.0,
            "exec_min_score_ratio": 0.9,
            "exec_min_sharpe_ratio": 0.8,
            "entry_gate_mode": "soft",
            "execution_gate_mode": "hard",
        }
    )
    agent = FXELAgent(cfg)

    df = _market_df()
    md = {"EURUSD": df.copy(), "GBPUSD": df.copy()}

    def fake_score(_df, sym):
        return (
            0.30,
            {
                "sym": sym,
                "p_trend": 0.8,
                "vol": 0.001,
                "predictive_sharpe": 0.4,
                "hawkes_n": 1.0,
                "lppls_hazard": 0.0,
                "score": 0.30,
            },
        )

    def fake_conf_metrics(*, diag, **_kwargs):
        sym = str(diag.get("sym", ""))
        base = {
            "score_ratio": 1.2,
            "sharpe_ratio": 1.2,
            "sharpe_threshold": 0.2,
            "sharpe_aligned": 0.4,
            "cost_ratio": 2.0,
            "spread_ratio": 2.0,
            "regime_strength": 0.6,
            "hawkes_ratio": 1.0,
            "heston_ratio": 1.0,
            "model_cohesion": 0.7,
        }
        if sym == "EURUSD":
            # Higher display confidence but fails execution confidence.
            base.update({"confidence": 92.0, "confidence_exec_base": 40.0})
        else:
            # Lower display confidence but execution-ready.
            base.update({"confidence": 70.0, "confidence_exec_base": 70.0})
        return base

    agent.score_symbol = fake_score
    agent._trade_confidence_metrics = fake_conf_metrics
    _ = agent.decisions(md, held_symbols=set())

    top = dict(agent.last_best_candidate)
    assert top.get("symbol") == "GBPUSD"
    assert bool(top.get("execution_ready", False)) is True
