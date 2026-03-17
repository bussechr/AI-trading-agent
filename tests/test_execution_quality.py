import pandas as pd
import pytest
from unittest.mock import patch

from src.agents.fx_el_hawkes_agent import Decision, FXELAgent
from src.run_fx import _resolve_synthetic_fill_steps


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


def test_execution_quality_soft_mode_bypasses_low_score_ratio_when_other_quality_is_strong():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": True,
            "execution_gate_mode": "soft",
            "entry_gate_mode": "soft",
            "exec_min_confidence": 40.0,
            "exec_min_score_ratio": 0.25,
            "exec_min_score_ratio_soft": 0.05,
            "exec_min_sharpe_ratio": 0.15,
        }
    )
    agent = FXELAgent(cfg)

    md = {"EURUSD": _market_df()}
    decision = Decision(symbol="EURUSD", side="SELL", score=0.3)
    agent.last_candidate_map = {
        ("EURUSD", "SELL"): {
            "confidence": 8.0,
            "confidence_raw": 55.0,
            "confidence_exec": 46.0,
            "score_ratio": 0.04,
            "score_ratio_exec": 0.06,
            "sharpe_ratio": 5.0,
            "cost_ratio": 30.0,
            "blocked_by": "low_score",
            "blocked_by_all": "low_score",
            "entry_ready": True,
            "exec_quality_ready": False,
            "execution_ready": False,
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
    assert agent.rejection_stats_cycle.get("exec_low_score_ratio", 0) == 0


def test_execution_quality_soft_mode_rejects_low_score_ratio_without_bypass():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": True,
            "execution_gate_mode": "soft",
            "entry_gate_mode": "soft",
            "exec_min_confidence": 40.0,
            "exec_min_score_ratio": 0.35,
            "exec_min_score_ratio_soft": 0.20,
            "exec_min_sharpe_ratio": 0.30,
        }
    )
    agent = FXELAgent(cfg)

    md = {"EURUSD": _market_df()}
    decision = Decision(symbol="EURUSD", side="SELL", score=0.3)
    agent.last_candidate_map = {
        ("EURUSD", "SELL"): {
            "confidence": 90.0,
            "confidence_raw": 90.0,
            "confidence_exec": 90.0,
            "score_ratio": 0.08,
            "score_ratio_exec": 0.10,
            "sharpe_ratio": 1.00,
            "cost_ratio": 0.70,  # bypass requires >= 1.0
            "blocked_by": "low_score",
            "blocked_by_all": "low_score",
            "entry_ready": True,
            "exec_quality_ready": False,
            "execution_ready": False,
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

    assert not mock_send.called
    assert agent.rejection_stats_cycle.get("exec_low_score_ratio", 0) >= 1


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


def test_startup_warmup_blocks_entries_even_with_valid_candidate():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": False,
            "startup_warmup_strategy": "live",
            "startup_warmup_min_live_bars": 24,
            "startup_warmup_min_tick_hours": 6,
        }
    )
    agent = FXELAgent(cfg)
    agent.activate_startup_warmup("EURUSD", gap_hours=48)

    md = {"EURUSD": _market_df()}
    md["EURUSD"].attrs["live_bars_since_startup"] = 0
    decision = Decision(symbol="EURUSD", side="BUY", score=0.8)
    agent.last_candidate_map = {
        ("EURUSD", "BUY"): {
            "confidence": 75.0,
            "confidence_exec": 75.0,
            "score_ratio": 1.2,
            "sharpe_ratio": 1.2,
            "cost_ratio": 2.5,
            "blocked_by": "none",
        }
    }

    with patch.object(agent, "decisions", return_value=[decision]):
        with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
            with patch("src.agents.fx_el_hawkes_agent.send") as mock_send:
                with patch("src.agents.fx_el_hawkes_agent.post_visuals"):
                    with patch("src.agents.fx_el_hawkes_agent.update_thought"):
                        with patch("src.agents.fx_el_hawkes_agent.post_decisions"):
                            with patch("src.agents.fx_el_hawkes_agent.bridge_client.get_metrics", return_value={}):
                                agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])

    assert not mock_send.called
    assert int(agent.rejection_stats_cycle.get("startup_warmup", 0)) >= 1


def test_backward_warmup_blocks_entries_while_backfill_pending():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": False,
            "startup_warmup_strategy": "backward_bridge",
            "startup_backfill_block_entries": True,
        }
    )
    agent = FXELAgent(cfg)

    md = {"EURUSD": _market_df()}
    md["EURUSD"].attrs["warmup_strategy"] = "backward_bridge"
    md["EURUSD"].attrs["startup_backfill_pending"] = True
    md["EURUSD"].attrs["startup_backfill_ready"] = False
    md["EURUSD"].attrs["startup_backfill_bars"] = 12
    md["EURUSD"].attrs["startup_backfill_retry_age_secs"] = 45.0
    md["EURUSD"].attrs["startup_backward_replay_done"] = False
    decision = Decision(symbol="EURUSD", side="SELL", score=0.8)

    with patch.object(agent, "decisions", return_value=[decision]):
        with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
            with patch("src.agents.fx_el_hawkes_agent.send") as mock_send:
                with patch("src.agents.fx_el_hawkes_agent.post_visuals"):
                    with patch("src.agents.fx_el_hawkes_agent.update_thought"):
                        with patch("src.agents.fx_el_hawkes_agent.post_decisions"):
                            with patch("src.agents.fx_el_hawkes_agent.bridge_client.get_metrics", return_value={}):
                                agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])

    assert not mock_send.called
    assert int(agent.rejection_stats_cycle.get("startup_backfill_pending", 0)) >= 1


def test_backward_replay_blocks_one_cycle_then_allows_entries():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": False,
            "startup_warmup_strategy": "backward_bridge",
            "startup_backfill_block_entries": True,
            "startup_backward_replay_bars": 24,
        }
    )
    agent = FXELAgent(cfg)
    agent.activate_startup_backward_warmup("EURUSD", gap_hours=48, backfill_bars=96, replay_bars=24)

    md = {"EURUSD": _market_df()}
    md["EURUSD"].attrs["warmup_strategy"] = "backward_bridge"
    md["EURUSD"].attrs["startup_backfill_pending"] = False
    md["EURUSD"].attrs["startup_backfill_ready"] = True
    md["EURUSD"].attrs["startup_backfill_bars"] = 96
    md["EURUSD"].attrs["startup_backfill_retry_age_secs"] = 0.0
    md["EURUSD"].attrs["startup_backward_replay_done"] = False
    decision = Decision(symbol="EURUSD", side="BUY", score=0.8)
    agent.last_candidate_map = {
        ("EURUSD", "BUY"): {
            "confidence": 75.0,
            "confidence_exec": 75.0,
            "score_ratio": 1.2,
            "sharpe_ratio": 1.2,
            "cost_ratio": 2.5,
            "blocked_by": "none",
            "warmup_strategy": "backward_bridge",
            "startup_backfill_pending": False,
            "startup_backfill_ready": True,
            "startup_backfill_bars": 96,
            "startup_backfill_retry_age_secs": 0.0,
            "startup_backward_replay_done": False,
        }
    }

    replay_calls: list[int] = []

    def _fake_score(df, _sym):
        replay_calls.append(len(df))
        return 0.0, {"score": 0.0}

    with patch.object(agent, "score_symbol", side_effect=_fake_score):
        with patch.object(agent, "decisions", return_value=[decision]):
            with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
                with patch("src.agents.fx_el_hawkes_agent.send") as mock_send:
                    with patch("src.agents.fx_el_hawkes_agent.post_visuals"):
                        with patch("src.agents.fx_el_hawkes_agent.update_thought"):
                            with patch("src.agents.fx_el_hawkes_agent.post_decisions"):
                                    with patch("src.agents.fx_el_hawkes_agent.bridge_client.get_metrics", return_value={}):
                                        with patch("src.agents.risk_utils.calculate_position_size", return_value=0.2):
                                            agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])
                                            first_cycle_replay_rejections = int(
                                                agent.rejection_stats_cycle.get("startup_backward_replay", 0)
                                            )
                                            agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])
                                            second_cycle_replay_rejections = int(
                                                agent.rejection_stats_cycle.get("startup_backward_replay", 0)
                                            )

    assert first_cycle_replay_rejections >= 1
    assert second_cycle_replay_rejections == 0
    assert len(replay_calls) > 0
    assert bool(agent.startup_backward_replay_state["EURUSD"]["replay_done"]) is True


def test_starvation_relaxation_activates_after_low_score_window():
    cfg = _base_cfg()
    cfg.update(
        {
            "starvation_window_cycles": 4,
            "starvation_step_cycles": 2,
            "starvation_relax_step": 0.03,
            "regime_score_mult_transition": 1.20,
            "starvation_transition_mult_floor": 0.98,
        }
    )
    agent = FXELAgent(cfg)

    for _ in range(4):
        agent.rejection_stats_cycle = {
            "low_score": 6,
            "soft_low_score": 2,
            "exec_low_score_ratio": 2,
            "soft_cost_gate": 1,
        }
        agent._update_starvation_state(0)

    assert agent.starvation_mode_active is True
    assert agent._dynamic_transition_mult() < 1.20
    assert agent.starvation_relax_level > 0.0


def test_starvation_transition_multiplier_never_below_floor():
    cfg = _base_cfg()
    cfg.update(
        {
            "regime_score_mult_transition": 1.20,
            "starvation_transition_mult_floor": 0.98,
        }
    )
    agent = FXELAgent(cfg)
    agent.starvation_mode_active = True
    agent.starvation_relax_level = 10.0
    assert agent._dynamic_transition_mult() == pytest.approx(0.98, abs=1e-9)


def test_neutral_fallback_risk_scale_is_capped():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_confidence_risk_sizing": True,
            "neutral_micro_fallback_risk_mult": 0.60,
            "conf_risk_floor": 0.65,
            "conf_risk_ceiling": 1.35,
            "conf_risk_power": 1.0,
        }
    )
    agent = FXELAgent(cfg)
    scale = agent._execution_risk_scale(
        {
            "confidence": 100.0,
            "confidence_exec": 100.0,
            "blocked_by": "none",
            "fallback_path": "neutral_micro_ai",
        }
    )
    assert float(scale) <= (0.60 + 1e-9)


def test_bridge_timeout_metrics_trigger_close_only_degrade():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": False,
            "bridge_safety_degrade_enabled": True,
            "bridge_safety_degrade_cycles": 10,
            "bridge_safety_ack_timeout_rate_max": 0.05,
            "bridge_safety_pending_oldest_secs_max": 60.0,
            "bridge_safety_pending_count_max": 50,
        }
    )
    agent = FXELAgent(cfg)
    md = {"EURUSD": _market_df()}
    decision = Decision(symbol="EURUSD", side="BUY", score=0.8)
    agent.last_candidate_map = {
        ("EURUSD", "BUY"): {
            "confidence": 80.0,
            "confidence_exec": 80.0,
            "score_ratio": 1.5,
            "sharpe_ratio": 1.2,
            "cost_ratio": 2.5,
            "blocked_by": "none",
        }
    }

    bridge_metrics = {
        "timeouts": {"ack_timeout_rate_5m": 0.12},
        "queue": {"pending_oldest_secs": 10.0, "pending_count": 1},
    }
    with patch.object(agent, "decisions", return_value=[decision]):
        with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
            with patch("src.agents.fx_el_hawkes_agent.send") as mock_send:
                with patch("src.agents.fx_el_hawkes_agent.post_visuals"):
                    with patch("src.agents.fx_el_hawkes_agent.update_thought"):
                        with patch("src.agents.fx_el_hawkes_agent.post_decisions"):
                            with patch("src.agents.fx_el_hawkes_agent.bridge_client.get_metrics", return_value=bridge_metrics):
                                agent.act(10000.0, md, all_symbols_catalog=["EURUSD"])

    assert not mock_send.called
    assert int(agent.rejection_stats_cycle.get("bridge_safety_close_only", 0)) >= 1


def test_entry_monitor_reports_open_proximity_from_gate_ratios():
    cfg = _base_cfg()
    cfg.update(
        {
            "use_execution_quality_gate": True,
            "exec_min_confidence": 40.0,
            "exec_min_score_ratio": 0.5,
            "exec_min_sharpe_ratio": 0.5,
            "execution_gate_mode": "hard",
        }
    )
    agent = FXELAgent(cfg)
    out = agent._build_entry_monitor(
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "confidence_exec": 28.0,
            "score_ratio": 0.80,
            "sharpe_ratio": 1.20,
            "cost_ratio": 3.00,
            "execution_ready": False,
            "blocked_by": "low_score",
        }
    )

    # Min component: execution confidence 28/40 = 0.70.
    assert float(out["open_proximity_pct"]) == pytest.approx(70.0, abs=1e-9)
    assert out["blocked_by"] == "low_score"


def test_close_position_monitor_hits_100pct_on_reversal_trigger():
    cfg = _base_cfg()
    agent = FXELAgent(cfg)

    sym = "EURUSD"
    entry_price = 1.1000
    now_price = 1.1000
    open_time = 1000.0
    now_ts = 2000.0
    agent.risk_manager.update_position_state(
        symbol=sym,
        current_price=now_price,
        entry_price=entry_price,
        side="BUY",
        vol=0.001,
        entry_time=open_time,
    )
    snap = agent._build_close_position_monitor(
        symbol=sym,
        side="BUY",
        score_now=-0.30,
        p_trend=0.55,
        current_price=now_price,
        open_price=entry_price,
        open_time=open_time,
        now_ts=now_ts,
        hold_policy={
            "min_hold_secs": 0.0,
            "time_limit_hours": 24.0,
            "stagnation_minutes": 60.0,
            "regime_exit_th": 0.0,
        },
        exit_score_threshold_eff=0.20,
    )

    assert float(snap["close_proximity_pct"]) == pytest.approx(100.0, abs=1e-9)
    assert snap["dominant_close_reason"] == "reversal_exit"


def test_synthetic_gap_fill_is_capped_when_bridge_history_is_unavailable():
    fill_steps, truncated = _resolve_synthetic_fill_steps(
        gap_hours=361,
        gap_recovery_enabled=True,
        max_synth_bars=3,
    )
    assert fill_steps == 3
    assert truncated is True


def test_zero_score_collapse_sets_explicit_candidate_rejection_reason():
    cfg = _base_cfg()
    cfg.update(
        {
            "entry_gate_mode": "soft",
            "execution_gate_mode": "soft",
            "use_execution_quality_gate": True,
            "score_zero_epsilon": 1e-6,
            "exec_min_raw_signal_ratio": 0.20,
        }
    )
    agent = FXELAgent(cfg)
    md = {"EURUSD": _market_df()}

    agent.score_symbol = lambda _df, _sym: (
        0.0,
        {
            "p_trend": 0.55,
            "vol": 0.001,
            "predictive_sharpe": 0.2,
            "hawkes_n": 1.0,
            "lppls_hazard": 0.0,
            "score": 0.0,
            "raw_signal": 0.0,
            "momentum_component": 0.0,
            "micro_component": 0.0,
            "ai_component": 0.0,
        },
    )

    _ = agent.decisions(md, held_symbols=set())
    top = dict(agent.last_best_candidate)
    assert top.get("blocked_by") == "zero_score_collapse"
    assert top.get("side") == "NONE"
    assert bool(top.get("execution_ready", True)) is False


def test_direction_abstain_gate_forces_none_on_weak_exec_strength():
    cfg = _base_cfg()
    cfg.update(
        {
            "entry_gate_mode": "soft",
            "execution_gate_mode": "soft",
            "use_execution_quality_gate": True,
            "exec_use_raw_signal_proxy": False,
            "direction_abstain_score_ratio": 0.35,
            "score_threshold": 0.2,
        }
    )
    agent = FXELAgent(cfg)
    md = {"EURUSD": _market_df()}

    agent.score_symbol = lambda _df, _sym: (
        0.30,
        {
            "p_trend": 0.60,
            "vol": 0.001,
            "predictive_sharpe": 0.5,
            "hawkes_n": 1.0,
            "lppls_hazard": 0.0,
            "score": 0.30,
            "raw_signal": 0.30,
            "momentum_component": 0.30,
            "micro_component": 0.0,
            "ai_component": 0.0,
        },
    )
    agent._trade_confidence_metrics = lambda **_kwargs: {
        "confidence": 80.0,
        "confidence_exec_base": 80.0,
        "score_ratio": 0.20,
        "sharpe_ratio": 1.1,
        "sharpe_threshold": 0.2,
        "sharpe_aligned": 0.5,
        "cost_ratio": 2.0,
        "spread_ratio": 2.0,
        "regime_strength": 0.8,
        "hawkes_ratio": 1.0,
        "heston_ratio": 1.0,
        "model_cohesion": 0.8,
    }

    _ = agent.decisions(md, held_symbols=set())
    top = dict(agent.last_best_candidate)
    assert top.get("side") == "NONE"
    assert bool(top.get("direction_abstain_triggered", False)) is True
    assert bool(top.get("execution_ready", True)) is False


def test_direction_side_dominance_guard_blocks_blind_one_way_drift():
    cfg = _base_cfg()
    cfg.update(
        {
            "entry_gate_mode": "soft",
            "execution_gate_mode": "soft",
            "use_execution_quality_gate": True,
            "exec_use_raw_signal_proxy": False,
            "direction_abstain_score_ratio": 0.0,  # isolate bias guard in this test
            "direction_bias_window": 20,
            "direction_bias_max_share": 0.85,
            "score_threshold": 0.2,
        }
    )
    agent = FXELAgent(cfg)
    agent.candidate_side_history.extend(["SELL"] * 20)
    md = {"EURUSD": _market_df()}

    agent.score_symbol = lambda _df, _sym: (
        -0.35,
        {
            "p_trend": 0.50,  # transition bucket -> no trend-justified override
            "vol": 0.001,
            "predictive_sharpe": 0.5,
            "hawkes_n": 1.0,
            "lppls_hazard": 0.0,
            "score": -0.35,
            "raw_signal": -0.35,
            "momentum_component": -0.35,
            "micro_component": 0.0,
            "ai_component": 0.0,
        },
    )
    agent._trade_confidence_metrics = lambda **_kwargs: {
        "confidence": 82.0,
        "confidence_exec_base": 82.0,
        "score_ratio": 1.2,
        "sharpe_ratio": 1.2,
        "sharpe_threshold": 0.2,
        "sharpe_aligned": 0.5,
        "cost_ratio": 2.5,
        "spread_ratio": 2.0,
        "regime_strength": 0.8,
        "hawkes_ratio": 1.0,
        "heston_ratio": 1.0,
        "model_cohesion": 0.8,
    }

    _ = agent.decisions(md, held_symbols=set())
    top = dict(agent.last_best_candidate)
    assert top.get("side") == "NONE"
    assert bool(top.get("direction_bias_guard_active", False)) is True
    assert str(top.get("direction_bias_justification", "none")) == "none"


def test_dashboard_payload_includes_rolling_edge_diagnostics_fields():
    cfg = _base_cfg()
    agent = FXELAgent(cfg)
    agent.last_diagnostics = {
        "score": 0.0,
        "pz": 0.0,
        "predictive_sharpe": 0.0,
        "p_trend": 0.5,
    }
    agent.side_share_buy_rolling = 0.12
    agent.side_share_sell_rolling = 0.88
    agent.abstain_rate_rolling = 0.33
    agent.edge_vs_random_hit_delta = 0.04
    agent.edge_vs_random_expectancy_delta = 0.002

    with patch("src.agents.fx_el_hawkes_agent.get_positions", return_value=[]):
        with patch("src.agents.fx_el_hawkes_agent.update_thought"):
            with patch("src.agents.fx_el_hawkes_agent.post_decisions") as mock_post:
                agent._post_decisions_to_dashboard(
                    decisions=[],
                    md={"EURUSD": _market_df()},
                    vol_now=0.001,
                    target_pct=0.01,
                )

    assert mock_post.called
    payload = dict(mock_post.call_args.kwargs.get("diagnostics", {}) or {})
    assert float(payload.get("side_share_buy_rolling", 0.0)) == pytest.approx(0.12, abs=1e-9)
    assert float(payload.get("side_share_sell_rolling", 0.0)) == pytest.approx(0.88, abs=1e-9)
    assert float(payload.get("abstain_rate_rolling", 0.0)) == pytest.approx(0.33, abs=1e-9)
    assert float(payload.get("edge_vs_random_hit_delta", 0.0)) == pytest.approx(0.04, abs=1e-9)
    assert float(payload.get("edge_vs_random_expectancy_delta", 0.0)) == pytest.approx(0.002, abs=1e-9)


def test_execution_quality_gate_uses_raw_signal_proxy_for_score_ratio():
    cfg = _base_cfg()
    cfg.update(
        {
            "entry_gate_mode": "soft",
            "execution_gate_mode": "soft",
            "use_execution_quality_gate": True,
            "exec_min_confidence": 55.0,
            "exec_min_score_ratio": 0.90,
            "exec_min_sharpe_ratio": 0.80,
            "exec_use_raw_signal_proxy": True,
            "score_threshold": 0.2,
        }
    )
    agent = FXELAgent(cfg)
    md = {"EURUSD": _market_df()}

    agent.score_symbol = lambda _df, _sym: (
        0.01,  # score_ratio=0.05 vs threshold 0.2
        {
            "p_trend": 0.75,
            "vol": 0.001,
            "predictive_sharpe": 0.4,
            "hawkes_n": 1.0,
            "lppls_hazard": 0.0,
            "score": 0.01,
            "raw_signal": 0.30,  # raw_signal_ratio=1.5
            "momentum_component": 0.30,
            "micro_component": 0.0,
            "ai_component": 0.0,
        },
    )

    def fake_conf_metrics(**_kwargs):
        return {
            "confidence": 80.0,
            "confidence_exec_base": 80.0,
            "score_ratio": 0.05,
            "sharpe_ratio": 1.2,
            "sharpe_threshold": 0.2,
            "sharpe_aligned": 0.4,
            "cost_ratio": 2.5,
            "spread_ratio": 2.0,
            "regime_strength": 0.8,
            "hawkes_ratio": 1.0,
            "heston_ratio": 1.0,
            "model_cohesion": 0.8,
        }

    agent._trade_confidence_metrics = fake_conf_metrics
    _ = agent.decisions(md, held_symbols=set())

    top = dict(agent.last_best_candidate)
    assert float(top.get("score_ratio", 0.0)) < 0.10
    assert float(top.get("score_ratio_exec", 0.0)) > 1.0
    assert top.get("exec_score_basis") == "proxy_max"
    assert bool(top.get("exec_quality_ready", False)) is True
    assert bool(top.get("execution_ready", False)) is True


def test_candidate_rows_include_startup_backfill_audit_fields():
    cfg = _base_cfg()
    cfg.update(
        {
            "entry_gate_mode": "soft",
            "execution_gate_mode": "soft",
            "use_execution_quality_gate": False,
            "startup_warmup_strategy": "backward_bridge",
        }
    )
    agent = FXELAgent(cfg)
    md = {"EURUSD": _market_df()}
    md["EURUSD"].attrs["warmup_strategy"] = "backward_bridge"
    md["EURUSD"].attrs["startup_backfill_pending"] = True
    md["EURUSD"].attrs["startup_backfill_ready"] = False
    md["EURUSD"].attrs["startup_backfill_bars"] = 10
    md["EURUSD"].attrs["startup_backfill_retry_age_secs"] = 33.0
    md["EURUSD"].attrs["startup_backward_replay_done"] = False

    agent.score_symbol = lambda _df, _sym: (
        0.25,
        {
            "p_trend": 0.75,
            "vol": 0.001,
            "predictive_sharpe": 0.4,
            "hawkes_n": 1.0,
            "lppls_hazard": 0.0,
            "score": 0.25,
            "raw_signal": 0.25,
            "momentum_component": 0.25,
            "micro_component": 0.0,
            "ai_component": 0.0,
        },
    )

    _ = agent.decisions(md, held_symbols=set())
    top = dict(agent.last_best_candidate)
    assert top.get("warmup_strategy") == "backward_bridge"
    assert bool(top.get("startup_backfill_pending", False)) is True
    assert bool(top.get("startup_backfill_ready", True)) is False
    assert int(top.get("startup_backfill_bars", 0)) == 10
    assert float(top.get("startup_backfill_retry_age_secs", 0.0)) == pytest.approx(33.0, abs=1e-9)
    assert bool(top.get("startup_backward_replay_done", True)) is False


def test_starvation_exec_min_score_ratio_respects_aggressive_floor():
    cfg = _base_cfg()
    cfg.update(
        {
            "exec_min_score_ratio": 0.25,
            "exec_min_score_ratio_floor_starvation": 0.10,
            "starvation_relax_step": 0.05,
        }
    )
    agent = FXELAgent(cfg)
    agent.starvation_mode_active = True
    agent.starvation_step_count = 10

    dyn = agent._dynamic_exec_min_score_ratio(
        confidence_exec=90.0,
        sharpe_ratio=1.5,
        cost_ratio=2.5,
    )
    assert dyn == pytest.approx(0.10, abs=1e-9)
