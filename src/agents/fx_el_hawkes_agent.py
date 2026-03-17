from __future__ import annotations
import os, json, math, time
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
import logging

from .risk_utils import el_pz, regime_tilt, dynamic_target_pct, realised_vol, cost_gate, low_corr_pick
from .risk_manager import RiskManager
try:
    from execution import mt4_bridge_client as bridge_client
except ImportError:  # Package mode: import via src.*
    from src.execution import mt4_bridge_client as bridge_client
try:
    from src.trader.domain.risk_envelope import compute_adaptive_risk_envelope
except Exception:  # pragma: no cover - fallback if trader package unavailable
    compute_adaptive_risk_envelope = None

send = bridge_client.send
post_visuals = bridge_client.post_visuals
get_positions = bridge_client.get_positions
update_thought = bridge_client.update_thought
post_decisions = bridge_client.post_decisions

from .regime_filter import MarkovSwitchingModel
from .hawkes_micro import BivariateHawkes, OFIProxy, HawkesSignal
from .lppls_guard import LPPLSDetector

MINI_SUFFIXES_DEFAULT = [".MINI", "-MINI", "m", ".m"]

logger = logging.getLogger(__name__)

@dataclass
class Decision:
    symbol: str
    side: str
    score: float
    reason: str = ""
    priority: float = 0.0
    confidence: float = 0.0
    score_ratio: float = 0.0
    utility: float = 0.0
    blocked_by: str = "none"
    is_add: bool = False

class FXELAgent:
    """
    Full EL-Hawkes-Regime agent with:
    - EL momentum (generalized momentum oscillator)
    - Markov-switching regime filter with Student-t innovations
    - Hawkes microstructure (self-exciting flow)
    - LPPLS crash guard
    - Heston volatility surface guard
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mini_suffixes = cfg.get("mini_suffixes", MINI_SUFFIXES_DEFAULT)
        roots_raw = [str(r).strip().upper() for r in cfg.get("symbols_roots", []) if str(r).strip()]
        active_raw = [str(s).strip().upper() for s in (cfg.get("active_symbols", []) or []) if str(s).strip()]
        self.active_symbols_filter = set(active_raw)
        if active_raw:
            active_set = set(active_raw)
            roots = [r for r in roots_raw if r in active_set]
            missing = sorted(active_set - set(roots_raw))
            if missing:
                logger.warning(
                    "active_symbols contains %d symbol(s) not listed in symbols_roots: %s",
                    len(missing),
                    ", ".join(missing[:10]) + ("..." if len(missing) > 10 else ""),
                )
            if roots:
                self.roots = roots
            else:
                logger.warning("active_symbols matched no configured roots; falling back to symbols_roots")
                self.roots = roots_raw
        else:
            self.roots = roots_raw
        self.state_symbol_allow = set(self.active_symbols_filter) if self.active_symbols_filter else None
        self.maxK = int(cfg.get("max_concurrent", 4))
        self.corr_max = float(cfg.get("corr_max", 0.70))
        self.base_score_th = float(cfg.get("score_threshold", 0.40))
        self.score_th = self.base_score_th
        self.use_dyn_target = bool(cfg.get("use_dynamic_target", True))
        self.use_regime_filter = bool(cfg.get("use_regime_filter", True))
        self.account_ccy = str(cfg.get("account_currency", "USD")).upper()
        self.el_window = int(cfg.get("el_window", 48))
        self.el_ema_span = int(cfg.get("el_ema_span", 10))

        # LOG DASHBOARD VERSION
        import logging
        self.logger = logging.getLogger(__name__)
        self.logger.info("="*60)
        self.logger.info("FX EL HAWKES AGENT v2.1 - ENHANCED DASHBOARD LOADED 📊")
        self.logger.info("="*60)
        
        self.vol_ref = float(cfg.get("vol_ref", 0.010))
        self.target_base = float(cfg.get("target_base_pct", 0.010))
        self.avg_spread_pips = float(cfg.get("avg_spread_pips", 0.8))
        self.pip_value_per_lot = float(cfg.get("pip_value_per_lot", 10.0))
        self.ig_mini_lot = float(cfg.get("ig_mini_lot_size", 0.10))
        self.min_trade_lot = max(float(cfg.get("min_trade_lot", 0.01)), 0.01)
        self.use_equity_scaled_pip_target = bool(cfg.get("use_equity_scaled_pip_target", False))
        self.pip_value_target_pct_equity = float(
            np.clip(cfg.get("pip_value_target_pct_equity", 0.00004), 0.0, 0.05)
        )
        self.use_equity_scaled_min_lot = bool(cfg.get("use_equity_scaled_min_lot", False))
        self.equity_scaled_min_lot_per_unit = float(
            max(cfg.get("equity_scaled_min_lot_per_unit", 0.0), 0.0)
        )
        self.max_margin_level_per_trade_pct = float(cfg.get("max_margin_level_per_trade_pct", 0.0))
        self.lot_step_hint = max(float(cfg.get("lot_step_hint", 0.01)), 0.0001)
        self.leverage = float(cfg.get("leverage", 30.0))
        self.max_margin_pct = float(cfg.get("max_margin_pct", 0.80))
        
        # Per-symbol regime models: avoids cross-symbol contamination.
        self.regime_models: dict[str, MarkovSwitchingModel] = {}
        self.regime_fitted: dict[str, bool] = {}
        self.regime_last_fit_ts: dict[str, float] = {}
        self.regime_last_fit_n: dict[str, int] = {}
        self.regime_refit_secs = int(cfg.get("regime_refit_secs", 3600))
        self.regime_refit_bars = int(cfg.get("regime_refit_bars", 24))
        
        self.use_hawkes = cfg.get("use_hawkes", False)
        self.use_hawkes_gate = bool(cfg.get("use_hawkes_gate", False))
        self.hawkes_n_min = float(cfg.get("hawkes_n_min", 0.8))
        if self.use_hawkes:
            self.ofi_proxy = OFIProxy()
            self.hawkes_models: dict[str, BivariateHawkes] = {}
            self.hawkes_last_fit_ts: dict[str, float] = {}
            self.hawkes_last_fit_n: dict[str, int] = {}
            self.hawkes_refit_secs = int(cfg.get("hawkes_refit_secs", 900))
            self.hawkes_refit_bars = int(cfg.get("hawkes_refit_bars", 6))
        
        self.use_lppls = cfg.get("use_lppls", False)
        self.lppls_threshold = cfg.get("lppls_threshold", 0.6)
        if self.use_lppls:
            self.lppls = LPPLSDetector(window=252)
        self.lppls_refresh_secs = int(cfg.get("lppls_refresh_secs", 3600))
        self.lppls_min_points = int(cfg.get("lppls_min_points", 126))
        self._lppls_cache: dict[str, tuple[float, float]] = {}  # symbol -> (ts, hazard)
        
        self.min_predictive_sharpe = float(cfg.get("min_predictive_sharpe", 0.3))
        self.min_predictive_sharpe_trend_mult = float(
            np.clip(cfg.get("min_predictive_sharpe_trend_mult", 1.00), 0.0, 3.0)
        )
        self.min_predictive_sharpe_range_mult = float(
            np.clip(cfg.get("min_predictive_sharpe_range_mult", 0.45), 0.0, 3.0)
        )
        self.min_predictive_sharpe_transition_mult = float(
            np.clip(cfg.get("min_predictive_sharpe_transition_mult", 0.75), 0.0, 3.0)
        )
        self.entry_gate_mode = str(cfg.get("entry_gate_mode", "hard")).strip().lower()
        if self.entry_gate_mode not in {"hard", "soft"}:
            self.entry_gate_mode = "soft"
        self.execution_gate_mode = str(cfg.get("execution_gate_mode", self.entry_gate_mode)).strip().lower()
        if self.execution_gate_mode not in {"hard", "soft"}:
            self.execution_gate_mode = self.entry_gate_mode
        self.use_execution_quality_gate = bool(cfg.get("use_execution_quality_gate", True))
        self.exec_min_confidence = float(cfg.get("exec_min_confidence", 55.0))
        self.exec_min_score_ratio = float(cfg.get("exec_min_score_ratio", 0.90))
        self.exec_min_score_ratio_soft = float(
            np.clip(cfg.get("exec_min_score_ratio_soft", min(self.exec_min_score_ratio, 0.05)), 0.0, 1.50)
        )
        self.exec_min_sharpe_ratio = float(cfg.get("exec_min_sharpe_ratio", 0.80))
        self.score_zero_epsilon = float(max(0.0, cfg.get("score_zero_epsilon", 1e-6)))
        self.exec_use_raw_signal_proxy = bool(cfg.get("exec_use_raw_signal_proxy", True))
        self.exec_min_raw_signal_ratio = float(np.clip(cfg.get("exec_min_raw_signal_ratio", 0.20), 0.0, 5.0))
        self.soft_score_penalty_floor = float(np.clip(cfg.get("soft_score_penalty_floor", 0.05), 0.0, 1.0))
        self.use_confidence_risk_sizing = bool(cfg.get("use_confidence_risk_sizing", True))
        self.conf_risk_floor = float(np.clip(cfg.get("conf_risk_floor", 0.65), 0.05, 1.0))
        self.conf_risk_ceiling = float(np.clip(cfg.get("conf_risk_ceiling", 1.35), 1.0, 3.0))
        self.conf_risk_power = float(np.clip(cfg.get("conf_risk_power", 1.15), 0.25, 3.0))
        self.soft_blocked_risk_scale = float(np.clip(cfg.get("soft_blocked_risk_scale", 0.75), 0.10, 1.0))
        self.enable_winner_adds = bool(cfg.get("enable_winner_adds", False))
        self.max_adds_per_symbol = int(max(0, cfg.get("max_adds_per_symbol", 2)))
        self.winner_add_size_mult = float(np.clip(cfg.get("winner_add_size_mult", 0.50), 0.10, 1.00))
        self.winner_add_min_profit = float(cfg.get("winner_add_min_profit", 0.01))
        self.winner_add_min_r = float(cfg.get("winner_add_min_r", 0.50))
        self.winner_add_min_confidence = float(np.clip(cfg.get("winner_add_min_confidence", 70.0), 0.0, 100.0))
        self.winner_add_min_score_ratio = float(np.clip(cfg.get("winner_add_min_score_ratio", 1.20), 0.0, 10.0))
        self.winner_add_min_sharpe_ratio = float(np.clip(cfg.get("winner_add_min_sharpe_ratio", 1.00), 0.0, 10.0))
        self.winner_add_min_utility = float(cfg.get("winner_add_min_utility", 0.0))
        self.winner_add_min_trend_prob = float(np.clip(cfg.get("winner_add_min_trend_prob", 0.60), 0.50, 0.95))
        self.soft_reversal_exit_enabled = bool(cfg.get("soft_reversal_exit_enabled", self.entry_gate_mode == "soft"))
        self.soft_reversal_exit_score_ratio = float(
            np.clip(cfg.get("soft_reversal_exit_score_ratio", 0.03), 0.0, 2.0)
        )
        self.soft_reversal_exit_min_hold_hours = float(
            np.clip(cfg.get("soft_reversal_exit_min_hold_hours", 1.0), 0.0, 168.0)
        )
        self.soft_reversal_exit_min_aligned_sharpe = float(
            cfg.get("soft_reversal_exit_min_aligned_sharpe", 0.20)
        )
        self.soft_reversal_exit_persistence_cycles = int(
            max(1, cfg.get("soft_reversal_exit_persistence_cycles", 3))
        )
        self.soft_reversal_exit_loss_threshold = float(
            min(cfg.get("soft_reversal_exit_loss_threshold", -0.50), 0.0)
        )
        self.soft_reversal_persistence: dict[str, int] = {}

        # Directional model refinement: multi-horizon consensus + online side calibration.
        self.use_directional_ensemble = bool(cfg.get("use_directional_ensemble", True))
        self.dir_fast_window = int(max(8, cfg.get("dir_fast_window", max(8, self.el_window // 2))))
        self.dir_slow_window = int(max(self.dir_fast_window + 4, cfg.get("dir_slow_window", max(32, self.el_window * 2))))
        self.dir_base_weight = float(cfg.get("dir_base_weight", 1.0))
        self.dir_fast_weight = float(cfg.get("dir_fast_weight", 0.35))
        self.dir_slow_weight = float(cfg.get("dir_slow_weight", 0.25))
        self.use_directional_calibration = bool(cfg.get("use_directional_calibration", True))
        self.direction_calib_window = int(max(20, cfg.get("direction_calib_window", 200)))
        self.direction_min_samples = int(max(10, cfg.get("direction_min_samples", 40)))
        self.direction_calib_strength = float(np.clip(cfg.get("direction_calib_strength", 0.35), 0.0, 1.5))
        self.direction_bias_penalty = float(np.clip(cfg.get("direction_bias_penalty", 0.25), 0.0, 1.5))
        self.direction_factor_min = float(np.clip(cfg.get("direction_factor_min", 0.75), 0.4, 1.0))
        self.direction_factor_max = float(np.clip(cfg.get("direction_factor_max", 1.25), 1.0, 2.0))
        if self.direction_factor_max < self.direction_factor_min:
            self.direction_factor_max = self.direction_factor_min
        self.direction_recency_halflife = int(max(10, cfg.get("direction_recency_halflife", 80)))
        self.direction_learn_min_score_ratio = float(
            np.clip(cfg.get("direction_learn_min_score_ratio", 0.35), 0.0, 3.0)
        )
        self.direction_state_path = str(cfg.get("direction_state_path", "data/state/direction_state.json")).strip()
        self.direction_state_save_secs = int(max(5, cfg.get("direction_state_save_secs", 30)))
        self.direction_abstain_score_ratio = float(
            np.clip(cfg.get("direction_abstain_score_ratio", 0.35), 0.0, 5.0)
        )
        self.direction_bias_window = int(max(20, cfg.get("direction_bias_window", 120)))
        self.direction_bias_max_share = float(np.clip(cfg.get("direction_bias_max_share", 0.85), 0.50, 0.99))
        self.direction_bias_priority_penalty = float(
            np.clip(cfg.get("direction_bias_priority_penalty", 0.70), 0.10, 1.0)
        )
        self.use_ai_indicator_model = bool(cfg.get("use_ai_indicator_model", False))
        self.ai_score_weight = float(np.clip(cfg.get("ai_score_weight", 0.22), 0.0, 2.0))
        self.ai_confidence_floor = float(np.clip(cfg.get("ai_confidence_floor", 0.10), 0.0, 1.0))
        self.ai_min_signal = float(np.clip(cfg.get("ai_min_signal", 0.05), 0.0, 1.0))
        self.ai_learn_min_confidence = float(np.clip(cfg.get("ai_learn_min_confidence", 0.20), 0.0, 1.0))
        self.ai_indicator_calib_window = int(max(20, cfg.get("ai_indicator_calib_window", 200)))
        self.ai_indicator_min_samples = int(max(10, cfg.get("ai_indicator_min_samples", 40)))
        self.ai_indicator_calib_strength = float(np.clip(cfg.get("ai_indicator_calib_strength", 0.30), 0.0, 1.5))
        self.ai_indicator_bias_penalty = float(np.clip(cfg.get("ai_indicator_bias_penalty", 0.20), 0.0, 1.5))
        self.ai_indicator_factor_min = float(np.clip(cfg.get("ai_indicator_factor_min", 0.75), 0.4, 1.0))
        self.ai_indicator_factor_max = float(np.clip(cfg.get("ai_indicator_factor_max", 1.25), 1.0, 2.0))
        if self.ai_indicator_factor_max < self.ai_indicator_factor_min:
            self.ai_indicator_factor_max = self.ai_indicator_factor_min
        self.ai_indicator_recency_halflife = int(max(10, cfg.get("ai_indicator_recency_halflife", 80)))
        self.ai_indicator_state_path = str(
            cfg.get("ai_indicator_state_path", "data/state/ai_indicator_state.json")
        ).strip()
        self.ai_indicator_state_save_secs = int(max(5, cfg.get("ai_indicator_state_save_secs", 30)))
        self.use_horizon_hold_policy = bool(cfg.get("use_horizon_hold_policy", True))
        self.hold_horizon_hours = self._parse_horizon_hours(
            cfg.get("hold_horizon_hours", [2, 4, 8, 12, 24])
        )
        self.hold_default_horizon_hours = float(
            np.clip(
                cfg.get("hold_default_horizon_hours", 8.0),
                float(min(self.hold_horizon_hours)),
                float(max(self.hold_horizon_hours)),
            )
        )
        self.hold_policy_floor_mult = float(np.clip(cfg.get("hold_policy_floor_mult", 0.60), 0.20, 1.00))
        self.hold_policy_cap_mult = float(np.clip(cfg.get("hold_policy_cap_mult", 2.50), 1.00, 6.00))
        if self.hold_policy_cap_mult < self.hold_policy_floor_mult:
            self.hold_policy_cap_mult = self.hold_policy_floor_mult
        self.hold_policy_min_hold_gain = float(np.clip(cfg.get("hold_policy_min_hold_gain", 1.20), 0.0, 5.0))
        self.hold_policy_time_limit_gain = float(np.clip(cfg.get("hold_policy_time_limit_gain", 0.80), 0.0, 5.0))
        self.hold_policy_stagnation_gain = float(np.clip(cfg.get("hold_policy_stagnation_gain", 0.70), 0.0, 5.0))
        self.hold_policy_reversal_gain = float(np.clip(cfg.get("hold_policy_reversal_gain", 0.55), 0.0, 4.0))

        # Regime-conditional thresholding: stricter in transition, looser in clean trends.
        self.regime_trend_threshold = float(cfg.get("regime_trend_threshold", 0.62))
        self.regime_range_threshold = float(cfg.get("regime_range_threshold", 0.38))
        self.regime_score_mult_trend = float(cfg.get("regime_score_mult_trend", 0.95))
        self.regime_score_mult_range = float(cfg.get("regime_score_mult_range", 1.05))
        self.regime_score_mult_transition = float(cfg.get("regime_score_mult_transition", 1.30))
        self.regime_score_mult_transition_base = float(self.regime_score_mult_transition)
        self.regime_score_mult_trend_buy = float(cfg.get("regime_score_mult_trend_buy", 1.0))
        self.regime_score_mult_trend_sell = float(cfg.get("regime_score_mult_trend_sell", 1.0))
        self.regime_score_mult_range_buy = float(cfg.get("regime_score_mult_range_buy", 1.0))
        self.regime_score_mult_range_sell = float(cfg.get("regime_score_mult_range_sell", 1.0))
        self.regime_score_mult_transition_buy = float(cfg.get("regime_score_mult_transition_buy", 1.0))
        self.regime_score_mult_transition_sell = float(cfg.get("regime_score_mult_transition_sell", 1.0))
        self.starvation_window_cycles = int(max(1, cfg.get("starvation_window_cycles", 36)))
        self.starvation_reject_share_min = float(np.clip(cfg.get("starvation_reject_share_min", 0.60), 0.0, 1.0))
        self.starvation_relax_step = float(np.clip(cfg.get("starvation_relax_step", 0.03), 0.0, 0.30))
        self.starvation_transition_mult_floor = float(
            np.clip(cfg.get("starvation_transition_mult_floor", 0.98), 0.50, 2.50)
        )
        self.exec_min_score_ratio_floor_starvation = float(
            np.clip(cfg.get("exec_min_score_ratio_floor_starvation", 0.35), 0.10, 1.50)
        )
        self.starvation_step_cycles = int(max(1, cfg.get("starvation_step_cycles", 12)))
        self.starvation_symbol_only = str(cfg.get("starvation_symbol_only", "EURUSD")).strip().upper()
        self.neutral_micro_fallback_enabled = bool(cfg.get("neutral_micro_fallback_enabled", True))
        self.neutral_micro_fallback_risk_mult = float(
            np.clip(cfg.get("neutral_micro_fallback_risk_mult", 0.60), 0.10, 1.00)
        )
        self.neutral_micro_fallback_ptrend_low = float(
            np.clip(cfg.get("neutral_micro_fallback_ptrend_low", 0.45), 0.0, 1.0)
        )
        self.neutral_micro_fallback_ptrend_high = float(
            np.clip(cfg.get("neutral_micro_fallback_ptrend_high", 0.55), 0.0, 1.0)
        )
        if self.neutral_micro_fallback_ptrend_high < self.neutral_micro_fallback_ptrend_low:
            self.neutral_micro_fallback_ptrend_high = self.neutral_micro_fallback_ptrend_low
        self.neutral_micro_fallback_momentum_eps = float(
            np.clip(cfg.get("neutral_micro_fallback_momentum_eps", 0.03), 0.0, 1.0)
        )
        self.neutral_micro_fallback_threshold_mult = float(
            np.clip(cfg.get("neutral_micro_fallback_threshold_mult", 0.75), 0.10, 1.20)
        )
        self.use_side_threshold_adaptation = bool(cfg.get("use_side_threshold_adaptation", True))
        self.side_threshold_min_samples = int(
            max(10, cfg.get("side_threshold_min_samples", self.direction_min_samples))
        )
        self.side_threshold_hit_strength = float(
            np.clip(cfg.get("side_threshold_hit_strength", 0.35), 0.0, 2.0)
        )
        self.side_threshold_bias_strength = float(
            np.clip(cfg.get("side_threshold_bias_strength", 0.20), 0.0, 2.0)
        )
        self.side_threshold_min = float(np.clip(cfg.get("side_threshold_min", 0.70), 0.4, 1.0))
        self.side_threshold_max = float(np.clip(cfg.get("side_threshold_max", 1.40), 1.0, 2.5))
        if self.side_threshold_max < self.side_threshold_min:
            self.side_threshold_max = self.side_threshold_min
        self.use_score_distribution_adaptation = bool(cfg.get("use_score_distribution_adaptation", True))
        self.score_distribution_window = int(max(20, cfg.get("score_distribution_window", 240)))
        self.score_distribution_min_samples = int(max(10, cfg.get("score_distribution_min_samples", 60)))
        self.score_distribution_quantile = float(
            np.clip(cfg.get("score_distribution_quantile", 0.75), 0.50, 0.99)
        )
        self.score_distribution_mult = float(np.clip(cfg.get("score_distribution_mult", 1.05), 0.10, 5.0))
        self.score_distribution_floor_mult = float(
            np.clip(cfg.get("score_distribution_floor_mult", 0.25), 0.005, 1.00)
        )
        self.auto_tune_min_score_threshold = float(
            np.clip(cfg.get("auto_tune_min_score_threshold", 0.02), 0.001, 0.20)
        )

        # Unified utility objective gate. Disabled by default for backward compatibility.
        self.use_utility_objective = bool(cfg.get("use_utility_objective", False))
        self.utility_gate_mode = str(cfg.get("utility_gate_mode", "soft")).strip().lower()
        if self.utility_gate_mode not in {"hard", "soft"}:
            self.utility_gate_mode = "soft"
        self.utility_min = float(cfg.get("utility_min", 0.0))
        self.utility_lambda_var = float(cfg.get("utility_lambda_var", 0.35))
        self.utility_lambda_tail = float(cfg.get("utility_lambda_tail", 0.45))
        self.utility_lambda_cost = float(cfg.get("utility_lambda_cost", 1.00))
        self.utility_lambda_corr = float(cfg.get("utility_lambda_corr", 0.20))

        # Portfolio-level risk budget: cap aggregate and correlation-cluster exposure.
        risk_pct_cfg = float(cfg.get("risk_per_trade_pct", 0.01))
        self.use_portfolio_risk_budget = bool(cfg.get("use_portfolio_risk_budget", True))
        self.portfolio_risk_cap_pct = float(
            cfg.get("portfolio_risk_cap_pct", max(0.02, risk_pct_cfg * 2.5))
        )
        self.cluster_risk_cap_pct = float(
            cfg.get("cluster_risk_cap_pct", max(0.012, risk_pct_cfg * 1.5))
        )
        self.cluster_corr_threshold = float(cfg.get("cluster_corr_threshold", max(self.corr_max, 0.75)))
        self.portfolio_min_trade_risk_pct = float(cfg.get("portfolio_min_trade_risk_pct", 0.001))
        if self.cluster_risk_cap_pct > self.portfolio_risk_cap_pct:
            self.cluster_risk_cap_pct = self.portfolio_risk_cap_pct

        # Live governance: de-risk/pause entries on drawdown and edge decay.
        self.use_live_governance = bool(cfg.get("use_live_governance", True))
        self.gov_soft_dd_pct = float(cfg.get("gov_soft_dd_pct", 0.06))
        self.gov_hard_dd_pct = float(cfg.get("gov_hard_dd_pct", 0.10))
        if self.gov_hard_dd_pct < self.gov_soft_dd_pct:
            self.gov_hard_dd_pct = self.gov_soft_dd_pct
        self.gov_recovery_dd_pct = float(cfg.get("gov_recovery_dd_pct", 0.03))
        self.gov_soft_risk_scale = float(np.clip(cfg.get("gov_soft_risk_scale", 0.60), 0.05, 1.0))
        self.gov_edge_window = int(max(5, cfg.get("gov_edge_window", 24)))
        self.gov_min_edge = float(cfg.get("gov_min_edge", 0.00015))
        self.gov_min_conf = float(cfg.get("gov_min_conf", 40.0))
        self.gov_pause_cycles = int(max(1, cfg.get("gov_pause_cycles", 12)))
        self.daily_loss_breaker_pct = float(np.clip(cfg.get("daily_loss_breaker_pct", 0.03), 0.0, 0.25))
        self.use_adaptive_risk_envelope = bool(cfg.get("use_adaptive_risk_envelope", True))
        self.gov_soft_dd_min = float(cfg.get("gov_soft_dd_min", min(self.gov_soft_dd_pct, 0.06)))
        self.gov_soft_dd_max = float(cfg.get("gov_soft_dd_max", max(self.gov_soft_dd_pct, 0.09)))
        self.gov_hard_dd_min = float(cfg.get("gov_hard_dd_min", min(self.gov_hard_dd_pct, 0.10)))
        self.gov_hard_dd_max = float(cfg.get("gov_hard_dd_max", max(self.gov_hard_dd_pct, 0.12)))
        self.daily_breaker_min = float(cfg.get("daily_breaker_min", min(self.daily_loss_breaker_pct, 0.02)))
        self.daily_breaker_max = float(cfg.get("daily_breaker_max", max(self.daily_loss_breaker_pct, 0.03)))
        if self.gov_soft_dd_max < self.gov_soft_dd_min:
            self.gov_soft_dd_max = self.gov_soft_dd_min
        if self.gov_hard_dd_max < self.gov_hard_dd_min:
            self.gov_hard_dd_max = self.gov_hard_dd_min
        if self.daily_breaker_max < self.daily_breaker_min:
            self.daily_breaker_max = self.daily_breaker_min
        self.daily_loss_breaker_dynamic_pct = float(self.daily_loss_breaker_pct)
        self.latest_risk_envelope: dict = {}
        
        # Strategy refinements
        self.use_adaptive_beta = cfg.get("use_adaptive_beta", True)
        self.beta_trend_boost = cfg.get("beta_trend_boost", 1.3)
        self.beta_range_boost = cfg.get("beta_range_boost", 1.3)
        self.use_session_filter = cfg.get("use_session_filter", True)
        self.use_graduated_heston = cfg.get("use_graduated_heston", True)
        self.use_model_cohesion = bool(cfg.get("use_model_cohesion", True))
        self.micro_align_mult = float(np.clip(cfg.get("micro_align_mult", 1.10), 1.0, 1.8))
        self.micro_conflict_mult_trend = float(np.clip(cfg.get("micro_conflict_mult_trend", 0.35), 0.0, 1.0))
        self.micro_conflict_mult_range = float(np.clip(cfg.get("micro_conflict_mult_range", 0.75), 0.0, 1.2))
        self.model_cohesion_conf_weight = float(np.clip(cfg.get("model_cohesion_conf_weight", 0.05), 0.0, 0.20))
        if self.micro_conflict_mult_range < self.micro_conflict_mult_trend:
            self.micro_conflict_mult_range = self.micro_conflict_mult_trend
        
        # OFI history for z-score normalisation (M4 FIX: use deque to avoid O(N) pop(0))
        import collections
        self.ofi_history_max = 50
        self.ofi_history: collections.deque = collections.deque(maxlen=self.ofi_history_max)
        self.candidate_side_history: collections.deque = collections.deque(maxlen=self.direction_bias_window)

        self.heston = None
        if cfg.get("use_heston_guard", False):
            self._init_heston_service(cfg)
        
        # Runtime validation tracking
        self.rejection_stats = {}
        self.rejection_stats_cycle = {}
        self.decision_log = []
        self.last_candidates: list[dict] = []
        self.last_best_candidate: dict = {}
        self.last_candidate_map: dict[tuple[str, str], dict] = {}
        self.direction_history: dict[str, list[tuple[int, int]]] = {}
        self.direction_state: dict[str, dict] = {}
        self.ai_indicator_history: dict[str, list[tuple[int, int]]] = {}
        self.ai_indicator_state: dict[str, dict] = {}
        self.score_abs_history: dict[str, list[float]] = {}
        self.score_history_bar: dict[str, int] = {}
        self._last_direction_state_save = 0.0
        self._last_ai_indicator_state_save = 0.0
        self.portfolio_risk_state: dict = {}
        self.governance_state: dict = {}
        try:
            self._load_direction_state()
        except AttributeError:
            logger.warning("Directional persistence loader missing; continuing without saved state")
        except Exception as exc:
            logger.warning("Directional persistence init failed: %s", exc)
        if self.use_ai_indicator_model:
            try:
                self._load_ai_indicator_state()
            except AttributeError:
                logger.warning("AI indicator persistence loader missing; continuing without saved state")
            except Exception as exc:
                logger.warning("AI indicator persistence init failed: %s", exc)
        
        self.risk_manager = RiskManager(cfg)
        self.base_trailing_mult = float(self.risk_manager.trailing_mult)
        self.base_risk_per_trade = float(self.risk_manager.risk_per_trade)
        self.current_risk_per_trade = float(self.base_risk_per_trade)
        self.dynamic_risk_scale = 1.0
        self.gov_cycle = 0
        self.gov_pause_until_cycle = 0
        self.gov_equity_peak = 0.0
        self.gov_edge_history: list[float] = []
        self.gov_conf_history: list[float] = []
        
        self.last_optimization_time = 0
        self.vol_ref_runtime = self._normalize_vol_reference(self.vol_ref)

        # Track recent reversals for grace period
        # { symbol: timestamp }
        self.recent_reversals = {}

        self.tick_stale_secs = int(cfg.get("tick_stale_secs", 180))
        self.require_fresh_ticks = bool(cfg.get("require_fresh_ticks", False))
        self.execution_mode = str(cfg.get("execution_mode", "full_live")).strip().lower()
        if self.execution_mode not in {"full_live", "close_only", "read_only"}:
            self.execution_mode = "full_live"
        kill_switch = str(os.getenv("FX_AGENT_EXECUTION_MODE", "")).strip().lower()
        if kill_switch in {"full_live", "close_only", "read_only"}:
            self.execution_mode = kill_switch
        self.max_new_entries_per_minute = int(max(1, cfg.get("max_new_entries_per_minute", 12)))
        self.max_total_commands_per_minute = int(max(1, cfg.get("max_total_commands_per_minute", 60)))
        self._recent_entry_cmd_ts: list[float] = []
        self._recent_total_cmd_ts: list[float] = []
        self.stale_tick_rejections = 0
        self.gap_recovery_events = 0
        self.close_failures_by_error_code: dict[str, int] = {}
        self.pending_entry_ttl_secs = float(np.clip(cfg.get("pending_entry_ttl_secs", 8.0), 1.0, 600.0))
        self.pending_entries: dict[tuple[str, str], float] = {}
        self.pending_entry_last_expired: dict[str, float] = {}
        self.cycle_id = 0
        self.bridge_safety_degrade_enabled = bool(cfg.get("bridge_safety_degrade_enabled", True))
        self.bridge_safety_degrade_cycles = int(max(1, cfg.get("bridge_safety_degrade_cycles", 10)))
        self.bridge_safety_ack_timeout_rate_max = float(
            np.clip(cfg.get("bridge_safety_ack_timeout_rate_max", 0.05), 0.0, 1.0)
        )
        self.bridge_safety_pending_oldest_secs_max = float(
            max(0.0, cfg.get("bridge_safety_pending_oldest_secs_max", 60.0))
        )
        self.bridge_safety_pending_count_max = int(max(1, cfg.get("bridge_safety_pending_count_max", 50)))
        self.bridge_safety_close_only_until_cycle = 0
        self.bridge_safety_last_metrics: dict = {}
        self.audit_trace_enabled = bool(cfg.get("audit_trace_enabled", False))
        self.audit_trace_path = str(cfg.get("audit_trace_path", "data/state/audit/strategy_trace.jsonl")).strip()
        self.audit_sample_rate = float(np.clip(cfg.get("audit_sample_rate", 1.0), 0.0, 1.0))
        self.audit_replay_mode = str(cfg.get("audit_replay_mode", "offline")).strip().lower()
        if self.audit_replay_mode not in {"offline", "live_like"}:
            self.audit_replay_mode = "offline"
        self._audit_rows_pending: list[dict] = []
        self._audit_summary_path: Path | None = None
        self._audit_summary_parquet_path: Path | None = None
        if self.audit_trace_path:
            trace_path = Path(self.audit_trace_path)
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            self._audit_summary_path = trace_path.with_name(f"{trace_path.stem}_summary.csv")
            self._audit_summary_parquet_path = trace_path.with_name(f"{trace_path.stem}_summary.parquet")
        # Interop (Python <-> MT4) audit controls.
        self.interop_audit_enabled = bool(cfg.get("interop_audit_enabled", False))
        self.interop_audit_trace_path = str(
            cfg.get("interop_audit_trace_path", "data/state/audit/interop/transport_trace.jsonl")
        ).strip()
        self.interop_audit_sample_rate = float(np.clip(cfg.get("interop_audit_sample_rate", 1.0), 0.0, 1.0))
        self.interop_audit_mode = str(cfg.get("interop_audit_mode", "live_shadow")).strip().lower()
        if self.interop_audit_mode not in {"live_shadow", "replay_live_like", "replay_offline"}:
            self.interop_audit_mode = "live_shadow"
        self.interop_latency_buckets_ms = cfg.get("interop_latency_buckets_ms", [25, 50, 100, 250, 500, 1000, 1600])
        self.interop_compute_trace_path = str(
            cfg.get("interop_compute_trace_path", "data/state/audit/interop/compute_trace.jsonl")
        ).strip()
        self._interop_last_decisions_timing: dict[str, float] = {}
        self.startup_warmup_strategy = str(cfg.get("startup_warmup_strategy", "live")).strip().lower()
        if self.startup_warmup_strategy not in {"live", "backward_bridge"}:
            self.startup_warmup_strategy = "live"
        self.startup_blockers_enabled = bool(cfg.get("startup_blockers_enabled", True))
        self.startup_backward_replay_bars = int(max(24, cfg.get("startup_backward_replay_bars", 96)))
        self.startup_backfill_block_entries = bool(cfg.get("startup_backfill_block_entries", True))
        self.startup_warmup_min_live_bars = int(max(1, cfg.get("startup_warmup_min_live_bars", 24)))
        self.startup_warmup_min_tick_hours = float(max(0.1, cfg.get("startup_warmup_min_tick_hours", 6.0)))
        self.startup_major_gap_hours = int(max(1, cfg.get("startup_major_gap_hours", 24)))
        self.startup_warmup_active = False
        self.startup_warmup_started_ts = 0.0
        self.startup_warmup_symbol = ""
        self.startup_warmup_reason = ""
        self.startup_warmup_live_bars = 0
        self.startup_backfill_by_symbol: dict[str, dict] = {}
        self.startup_backfill_pending_active = False
        self.startup_backfill_ready_active = False
        self.startup_backfill_bars_active = 0
        self.startup_backfill_retry_age_secs_active = 0.0
        self.startup_backfill_symbol = ""
        self.startup_backward_replay_state: dict[str, dict] = {}
        self.startup_backward_replay_cycle_block = False
        self.startup_backward_replay_done_active = False
        self.model_state_degraded_symbols: set[str] = set()
        self.cycle_exec_history: list[dict] = []
        self.starvation_mode_active = False
        self.starvation_no_exec_cycles = 0
        self.starvation_relax_level = 0.0
        self.starvation_step_count = 0
        self.suppression_ratio_history: list[float] = []
        self.suppression_ratio_rolling = 0.0
        self.dominant_rejection_reason = "none"
        self.side_share_buy_rolling = 0.5
        self.side_share_sell_rolling = 0.5
        self.abstain_rate_rolling = 0.0
        self.edge_vs_random_hit_delta = 0.0
        self.edge_vs_random_expectancy_delta = 0.0
        self.daily_eq_anchor_day = ""
        self.daily_eq_anchor = 0.0
        self.daily_breaker_active = False
        self.monitor_close_positions: list[dict] = []
        self.monitor_last_cycle_ts = 0.0
        if self.interop_audit_enabled:
            try:
                if self.interop_audit_trace_path:
                    Path(self.interop_audit_trace_path).parent.mkdir(parents=True, exist_ok=True)
                if self.interop_compute_trace_path:
                    Path(self.interop_compute_trace_path).parent.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
            # Bridge client reads these env keys for transport-level trace annotations.
            os.environ["MT4_INTEROP_AUDIT_ENABLED"] = "1"
            os.environ["MT4_INTEROP_AUDIT_TRACE_PATH"] = str(self.interop_audit_trace_path)
            os.environ["MT4_INTEROP_AUDIT_SAMPLE_RATE"] = str(self.interop_audit_sample_rate)
            os.environ["MT4_INTEROP_AUDIT_MODE"] = str(self.interop_audit_mode)
            os.environ["MT4_INTEROP_LATENCY_BUCKETS_MS"] = ",".join(
                str(int(v))
                for v in list(self.interop_latency_buckets_ms or [])
                if str(v).strip()
            )
        # Infer pip-value lot reference safely:
        # - explicit pip_value_lot_reference wins
        # - low pip values (<=2) are usually mini-lot quotes (e.g. $1/pip @ 0.10 lot on IG)
        # - otherwise assume standard-lot quote.
        lot_ref_cfg = cfg.get("pip_value_lot_reference", None)
        if lot_ref_cfg is None:
            self.pip_value_lot_reference = self.ig_mini_lot if self.pip_value_per_lot <= 2.0 else 1.0
        else:
            self.pip_value_lot_reference = float(lot_ref_cfg)
        self.pip_value_standard_lot = self._normalize_pip_value_per_standard_lot(
            self.pip_value_per_lot, self.pip_value_lot_reference
        )
        self._missing_fx_conv_warned: set[str] = set()

    def _init_heston_service(self, cfg: dict):
        """Initialize Heston service with configured provider."""
        from .heston_service import HestonService
        
        provider_type = cfg.get("heston_provider", "proxy")  # "http" or "proxy"
        
        if provider_type == "http":
            try:
                from marketdata.http_fx_options import HTTPFXOptionProvider
            except ImportError:  # Package mode: import via src.*
                from src.marketdata.http_fx_options import HTTPFXOptionProvider
            provider = HTTPFXOptionProvider(
                url_template=cfg.get("options_url_template", "https://api.example.com/fx/chain?symbol={symbol}"),
                headers={"Authorization": f"Bearer {cfg.get('options_api_key', '')}"},
                field_map=cfg.get("options_field_map", {})
            )
            logger.info("Initialized HTTP options provider for Heston calibration")
        else:
            try:
                from marketdata.proxy_provider import ProxyOptionProvider
            except ImportError:  # Package mode: import via src.*
                from src.marketdata.proxy_provider import ProxyOptionProvider
            
            def get_close_series(root):
                candidates = [
                    f"data/fx_minis/{root}.csv",
                    f"data/fx_minis/{root}.MINI.csv",
                ]
                for path in candidates:
                    if os.path.exists(path):
                        df = pd.read_csv(path)
                        return df["close"]
                return pd.Series([])
            
            def get_spot(root):
                s = get_close_series(root)
                return float(s.iloc[-1]) if len(s) > 0 else 1.0
            
            provider = ProxyOptionProvider(
                get_close=get_close_series,
                get_s0=get_spot,
                rd=cfg.get("rd", 0.05),
                rf=cfg.get("rf", 0.03)
            )
            logger.info("Initialized proxy options provider for Heston calibration")
        
        self.heston = HestonService(
            outdir=cfg.get("heston_outdir", "data/heston"),
            provider=provider,
            recalc_after_secs=cfg.get("heston_recalc_secs", 18*3600)
        )

    def _normalize_pip_value_per_standard_lot(self, pip_value: float, lot_reference: float) -> float:
        lot_ref = max(float(lot_reference), 1e-9)
        return max(float(pip_value), 1e-9) / lot_ref

    def _normalize_vol_reference(self, vol_ref: float) -> float:
        """
        Normalize configured vol_ref to the same scale as H1 return std used in score diagnostics.
        If vol_ref is large (daily-like), map it down; if already H1-scale, keep it.
        """
        if vol_ref <= 0:
            return 1e-6
        return vol_ref / 38.0 if vol_ref > 0.002 else vol_ref

    def _expected_move_fraction(self, score_abs: float, vol_now: float) -> float:
        vol_term = max(float(vol_now), 0.0)
        score_term = max(float(score_abs), 0.0)
        return float(np.clip((2.0 * vol_term) * (0.5 + score_term), 0.0002, 0.0100))

    def _regime_bucket(self, p_trend: float) -> str:
        p = float(np.clip(p_trend, 0.0, 1.0))
        if p >= self.regime_trend_threshold:
            return "trend"
        if p <= self.regime_range_threshold:
            return "range"
        return "transition"

    def _regime_score_threshold(self, p_trend: float) -> float:
        parts = self._regime_threshold_components(p_trend)
        return float(parts["threshold"])

    def _regime_sharpe_threshold(self, bucket: str | None) -> float:
        """
        Regime-aware Sharpe floor.
        Range regimes are inherently lower-conviction on predictive Sharpe and should not be
        held to the same floor as clean trend regimes.
        """
        b = str(bucket or "").strip().lower()
        if b == "range":
            mult = self.min_predictive_sharpe_range_mult
        elif b == "transition":
            mult = self.min_predictive_sharpe_transition_mult
        else:
            mult = self.min_predictive_sharpe_trend_mult
        return max(0.0, float(self.min_predictive_sharpe) * float(mult))

    def _regime_side_multiplier(self, bucket: str, side: str | None = None) -> float:
        side_up = str(side or "").upper()
        if side_up not in {"BUY", "SELL"}:
            return 1.0
        if bucket == "trend":
            return float(self.regime_score_mult_trend_buy if side_up == "BUY" else self.regime_score_mult_trend_sell)
        if bucket == "range":
            return float(self.regime_score_mult_range_buy if side_up == "BUY" else self.regime_score_mult_range_sell)
        return float(
            self.regime_score_mult_transition_buy if side_up == "BUY" else self.regime_score_mult_transition_sell
        )

    def _side_threshold_factor(self, side: str | None, quality: dict | None = None) -> float:
        """
        Side-aware threshold multiplier:
        lowers threshold for empirically stronger side and raises it for weaker side.
        """
        if not self.use_side_threshold_adaptation:
            return 1.0
        side_up = str(side or "").upper()
        if side_up not in {"BUY", "SELL"}:
            return 1.0

        q = dict(quality or {})
        n_all = int(q.get("samples", 0))
        if n_all <= 0:
            return 1.0

        if side_up == "BUY":
            n_side = int(q.get("buy_samples", 0))
            side_hit_raw = float(q.get("buy_hit_rate", 0.5))
            side_imb = max(
                (int(q.get("buy_samples", 0)) - int(q.get("sell_samples", 0))) / max(n_all, 1),
                0.0,
            )
        else:
            n_side = int(q.get("sell_samples", 0))
            side_hit_raw = float(q.get("sell_hit_rate", 0.5))
            side_imb = max(
                (int(q.get("sell_samples", 0)) - int(q.get("buy_samples", 0))) / max(n_all, 1),
                0.0,
            )

        n_ref = max(int(self.side_threshold_min_samples), 1)
        blend = float(np.clip(n_side / n_ref, 0.0, 1.0))
        side_hit = 0.5 + blend * (float(np.clip(side_hit_raw, 0.0, 1.0)) - 0.5)
        hit_edge = float(np.clip(side_hit - 0.5, -0.25, 0.25))
        sample_conf = float(np.clip(n_all / n_ref, 0.0, 1.0))

        # Better side quality -> lower threshold; weaker side quality -> higher threshold.
        factor = 1.0 - (self.side_threshold_hit_strength * hit_edge)

        # Penalize persistent side concentration only when sample confidence is sufficient.
        factor += self.side_threshold_bias_strength * side_imb * sample_conf * 0.25

        return float(np.clip(factor, self.side_threshold_min, self.side_threshold_max))

    def _regime_threshold_components(
        self,
        p_trend: float,
        side: str | None = None,
        direction_quality: dict | None = None,
    ) -> dict:
        bucket = self._regime_bucket(p_trend)
        if bucket == "trend":
            base_mult = float(self.regime_score_mult_trend)
        elif bucket == "range":
            base_mult = float(self.regime_score_mult_range)
        else:
            base_mult = float(self._dynamic_transition_mult())
        side_mult = self._regime_side_multiplier(bucket, side)
        adapt_mult = self._side_threshold_factor(side, direction_quality)
        total_mult = max(1e-6, float(base_mult) * float(side_mult) * float(adapt_mult))
        threshold = max(1e-9, float(self.score_th) * total_mult)
        return {
            "bucket": bucket,
            "base_mult": float(base_mult),
            "side_mult": float(side_mult),
            "adapt_mult": float(adapt_mult),
            "total_mult": float(total_mult),
            "threshold": float(threshold),
        }

    def _record_score_magnitude(self, sym_key: str, score_now: float, bar_key: int | None = None) -> None:
        """Track recent absolute score magnitudes for adaptive thresholding (1 sample per bar)."""
        if not self.use_score_distribution_adaptation:
            return
        key = str(sym_key).upper()
        if bar_key is not None:
            last_bar = int(self.score_history_bar.get(key, -1))
            if int(bar_key) <= last_bar:
                return
        try:
            val = abs(float(score_now))
        except Exception:
            return
        if (not np.isfinite(val)) or val <= 0.0:
            return
        hist = self.score_abs_history.setdefault(key, [])
        hist.append(float(val))
        if len(hist) > self.score_distribution_window:
            del hist[:-self.score_distribution_window]
        if bar_key is not None:
            self.score_history_bar[key] = int(bar_key)

    def _adaptive_score_threshold(
        self,
        sym_key: str,
        threshold_now: float,
        *,
        exclude_latest: bool = True,
    ) -> tuple[float, float, int]:
        """
        Cap overly-strict score thresholds using the symbol's own recent score distribution.
        Returns (adapted_threshold, reference_quantile, sample_count).
        """
        base_th = max(float(threshold_now), 1e-9)
        if not self.use_score_distribution_adaptation:
            return base_th, 0.0, 0
        hist = list(self.score_abs_history.get(str(sym_key).upper(), []) or [])
        if exclude_latest and hist:
            hist = hist[:-1]
        n = len(hist)
        if n < self.score_distribution_min_samples:
            return base_th, 0.0, n
        arr = np.asarray(hist, dtype=float)
        arr = arr[np.isfinite(arr)]
        n_eff = int(arr.size)
        if n_eff < self.score_distribution_min_samples:
            return base_th, 0.0, n_eff
        ref_q = float(np.quantile(arr, self.score_distribution_quantile))
        if (not np.isfinite(ref_q)) or ref_q <= 0.0:
            return base_th, 0.0, n_eff
        adaptive_cap = max(
            float(self.base_score_th) * float(self.score_distribution_floor_mult),
            ref_q * float(self.score_distribution_mult),
        )
        adapted = min(base_th, max(adaptive_cap, 1e-9))
        return float(adapted), float(ref_q), n_eff

    def _symbol_corr_pressure(
        self,
        symbol: str,
        md: dict[str, pd.DataFrame],
        held_symbols_up: set[str],
    ) -> float:
        """Return [0,1] max absolute correlation to currently held symbols."""
        if not held_symbols_up:
            return 0.0
        if symbol not in md or md[symbol] is None or md[symbol].empty:
            return 0.0
        try:
            base_ret = np.log(md[symbol]["close"]).diff().dropna()
            if len(base_ret) < 24:
                return 0.0
            base_ret = base_ret.tail(192)
            best = 0.0
            for hs in held_symbols_up:
                match_sym = None
                if hs in md:
                    match_sym = hs
                else:
                    for k in md.keys():
                        if str(k).upper() == hs:
                            match_sym = k
                            break
                if match_sym is None or match_sym == symbol:
                    continue
                other_df = md.get(match_sym)
                if other_df is None or other_df.empty:
                    continue
                other_ret = np.log(other_df["close"]).diff().dropna().tail(192)
                if len(other_ret) < 24:
                    continue
                joined = pd.concat([base_ret, other_ret], axis=1, join="inner").dropna()
                if len(joined) < 24:
                    continue
                c = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
                if np.isfinite(c):
                    best = max(best, abs(c))
            return float(np.clip(best, 0.0, 1.0))
        except Exception as exc:
            logger.debug(f"{symbol}: failed correlation pressure calc ({exc})")
            return 0.0

    def _trade_utility(
        self,
        *,
        side: str,
        expected_move: float,
        vol_now: float,
        cost_fraction: float,
        lppls_hazard: float,
        corr_pressure: float,
    ) -> dict:
        """
        Unified utility: expected edge minus weighted penalties.
        Positive utility implies trade expectancy survives risk/cost controls.
        """
        edge = max(float(expected_move), 0.0)
        var_pen = max(float(vol_now), 0.0)
        tail_pen = max(float(lppls_hazard), 0.0)
        if str(side).upper() == "SELL":
            # LPPLS is primarily crash-upside hazard, so shorts carry lower tail penalty.
            tail_pen *= 0.35
        cost_pen = max(float(cost_fraction), 0.0)
        corr_pen = max(float(corr_pressure), 0.0)
        utility = (
            edge
            - self.utility_lambda_var * var_pen
            - self.utility_lambda_tail * tail_pen
            - self.utility_lambda_cost * cost_pen
            - self.utility_lambda_corr * corr_pen
        )
        return {
            "utility": float(utility),
            "utility_edge": float(edge),
            "utility_var_pen": float(var_pen),
            "utility_tail_pen": float(tail_pen),
            "utility_cost_pen": float(cost_pen),
            "utility_corr_pen": float(corr_pen),
        }

    def _resolve_md_symbol_key(self, symbol: str, md: dict[str, pd.DataFrame]) -> str | None:
        """Map broker/position symbol text to the closest market-data key."""
        if symbol in md:
            return symbol
        s_up = str(symbol).upper()
        for k in md.keys():
            if str(k).upper() == s_up:
                return k
        if s_up:
            for k in md.keys():
                if s_up in str(k).upper():
                    return k
        return None

    def _pending_entry_key(self, symbol: str, side: str) -> tuple[str, str]:
        return str(symbol).upper(), str(side).upper()

    def _cleanup_pending_entries(self, held_symbols_up: set[str], now_ts: float) -> None:
        ttl = max(1.0, float(self.pending_entry_ttl_secs))
        keep: dict[tuple[str, str], float] = {}
        for key, ts in dict(self.pending_entries).items():
            sym_up, _ = key
            if sym_up in held_symbols_up:
                continue
            try:
                age = float(now_ts) - float(ts)
            except Exception:
                age = ttl + 1.0
            if age < ttl:
                keep[key] = float(ts)
            else:
                self.pending_entry_last_expired[f"{sym_up}:{key[1]}"] = float(now_ts)
                self._log_rejection("pending_entry_ttl_expired")
                self._audit_emit_row(
                    {
                        "phase": "execution",
                        "symbol": str(sym_up),
                        "side": str(key[1]),
                        "rejection_reason": "pending_entry_ttl_expired",
                        "outcome": "rejected",
                    }
                )
        self.pending_entries = keep

    def _has_pending_entry(self, symbol: str, side: str, now_ts: float) -> bool:
        key = self._pending_entry_key(symbol, side)
        ts = self.pending_entries.get(key)
        if ts is None:
            return False
        ttl = max(1.0, float(self.pending_entry_ttl_secs))
        try:
            if float(now_ts) - float(ts) < ttl:
                return True
        except Exception:
            pass
        self.pending_entries.pop(key, None)
        return False

    def _mark_pending_entry(self, symbol: str, side: str, now_ts: float) -> None:
        sym_up = str(symbol).upper()
        self.pending_entries.pop((sym_up, "BUY"), None)
        self.pending_entries.pop((sym_up, "SELL"), None)
        self.pending_entries[(sym_up, str(side).upper())] = float(now_ts)

    def reset_symbol_model_state(self, symbol: str, reason: str = "") -> None:
        """Reset stale model internals after startup major-gap recovery."""
        sym_key = str(symbol or "").upper()
        if not sym_key:
            return
        self.regime_fitted[sym_key] = False
        self.regime_last_fit_ts[sym_key] = 0.0
        self.regime_last_fit_n[sym_key] = 0
        self.score_abs_history[sym_key] = []
        self.score_history_bar[sym_key] = -1
        if sym_key in self.hawkes_models:
            try:
                self.hawkes_models.pop(sym_key, None)
            except Exception:
                pass
        self.model_state_degraded_symbols.add(sym_key)
        self._log_rejection("startup_model_reset")
        self._audit_emit_row(
            {
                "phase": "state",
                "symbol": str(sym_key),
                "side": "",
                "rejection_reason": str(reason or "startup_model_reset"),
                "outcome": "state_reset",
                "warmup_mode": bool(self._warmup_mode_active()),
                "warmup_strategy": str(self.startup_warmup_strategy),
                "startup_backfill_pending": bool(self.startup_backfill_pending_active),
                "startup_backfill_ready": bool(self.startup_backfill_ready_active),
                "startup_backfill_bars": int(self.startup_backfill_bars_active),
                "startup_backfill_retry_age_secs": float(self.startup_backfill_retry_age_secs_active),
                "startup_backward_replay_done": bool(self.startup_backward_replay_done_active),
            }
        )

    def activate_startup_warmup(self, symbol: str, gap_hours: int) -> None:
        """Enable startup warmup guard after major gap recovery."""
        self.startup_warmup_active = True
        self.startup_warmup_started_ts = float(time.time())
        self.startup_warmup_symbol = str(symbol or "").upper()
        self.startup_warmup_reason = f"major_gap_{int(gap_hours)}h"
        self.startup_warmup_live_bars = 0
        if self.startup_warmup_symbol:
            self.model_state_degraded_symbols.add(self.startup_warmup_symbol)
        logger.warning(
            "Startup warmup activated for %s (%s)",
            self.startup_warmup_symbol or "UNKNOWN",
            self.startup_warmup_reason,
        )

    def _refresh_startup_warmup(self, md: dict[str, pd.DataFrame]) -> None:
        if not self.startup_warmup_active:
            return
        if self.startup_warmup_symbol:
            df = md.get(self.startup_warmup_symbol)
            if df is not None:
                try:
                    self.startup_warmup_live_bars = max(
                        int(self.startup_warmup_live_bars),
                        int(df.attrs.get("live_bars_since_startup", self.startup_warmup_live_bars)),
                    )
                except Exception:
                    pass
        elapsed = max(float(time.time()) - float(self.startup_warmup_started_ts), 0.0)
        bars_ok = int(self.startup_warmup_live_bars) >= int(self.startup_warmup_min_live_bars)
        time_ok = elapsed >= float(self.startup_warmup_min_tick_hours * 3600.0)
        if bars_ok or time_ok:
            self.startup_warmup_active = False
            if self.startup_warmup_symbol:
                self.model_state_degraded_symbols.discard(self.startup_warmup_symbol)
            logger.info(
                "Startup warmup completed (%s): bars=%d/%d elapsed=%.1fh",
                self.startup_warmup_symbol or "UNKNOWN",
                int(self.startup_warmup_live_bars),
                int(self.startup_warmup_min_live_bars),
                float(elapsed / 3600.0),
            )
            self.startup_warmup_symbol = ""
            self.startup_warmup_reason = ""
            self.startup_warmup_live_bars = 0

    def _warmup_mode_active(self) -> bool:
        if not bool(self.startup_blockers_enabled):
            return False
        if self.startup_warmup_strategy == "live":
            return bool(self.startup_warmup_active)
        if self.startup_backfill_block_entries and bool(self.startup_backfill_pending_active):
            return True
        if bool(self.startup_backward_replay_cycle_block):
            return True
        return False

    def _startup_entry_block_reason(self) -> str:
        if not bool(self.startup_blockers_enabled):
            return ""
        if self.startup_warmup_strategy == "live":
            return "startup_warmup" if bool(self.startup_warmup_active) else ""
        if self.startup_backfill_block_entries and bool(self.startup_backfill_pending_active):
            return "startup_backfill_pending"
        if bool(self.startup_backward_replay_cycle_block):
            return "startup_backward_replay"
        return ""

    def activate_startup_backward_warmup(
        self,
        symbol: str,
        gap_hours: int,
        backfill_bars: int = 0,
        replay_bars: int | None = None,
    ) -> None:
        """Arm one-shot backward replay warmup after bridge backfill is ready."""
        sym_key = str(symbol or "").upper()
        if not sym_key:
            return
        replay_n = int(max(24, replay_bars if replay_bars is not None else self.startup_backward_replay_bars))
        self.startup_backward_replay_state[sym_key] = {
            "active": True,
            "replay_done": False,
            "gap_hours": int(max(0, gap_hours)),
            "backfill_bars": int(max(0, backfill_bars)),
            "replay_bars": int(replay_n),
            "armed_ts": float(time.time()),
            "last_replay_ts": 0.0,
            "replay_steps": 0,
        }
        self.model_state_degraded_symbols.add(sym_key)
        logger.warning(
            "Startup backward warmup armed for %s (major_gap_%dh, backfill_bars=%d, replay_bars=%d)",
            sym_key,
            int(max(0, gap_hours)),
            int(max(0, backfill_bars)),
            int(replay_n),
        )
        self._audit_emit_row(
            {
                "phase": "state",
                "symbol": str(sym_key),
                "side": "",
                "rejection_reason": "startup_backward_replay",
                "outcome": "startup_backward_replay_armed",
                "warmup_mode": True,
                "warmup_strategy": str(self.startup_warmup_strategy),
                "startup_backfill_pending": bool(self.startup_backfill_pending_active),
                "startup_backfill_ready": bool(self.startup_backfill_ready_active),
                "startup_backfill_bars": int(max(0, backfill_bars)),
                "startup_backfill_retry_age_secs": float(self.startup_backfill_retry_age_secs_active),
                "startup_backward_replay_done": False,
            }
        )

    def _refresh_startup_backfill_state(self, md: dict[str, pd.DataFrame]) -> None:
        self.startup_backfill_by_symbol = {}
        self.startup_backfill_pending_active = False
        self.startup_backfill_ready_active = False
        self.startup_backfill_bars_active = 0
        self.startup_backfill_retry_age_secs_active = 0.0
        self.startup_backfill_symbol = ""
        self.startup_backward_replay_done_active = False
        if self.startup_warmup_strategy != "backward_bridge":
            return

        selected_pending: dict | None = None
        selected_ready: dict | None = None
        for sym, df in md.items():
            if df is None:
                continue
            attrs = getattr(df, "attrs", {}) or {}
            sym_key = str(sym).upper()
            pending = bool(attrs.get("startup_backfill_pending", False))
            ready = bool(attrs.get("startup_backfill_ready", False))
            bars = int(max(0, attrs.get("startup_backfill_bars", 0)))
            retry_age = float(max(0.0, attrs.get("startup_backfill_retry_age_secs", 0.0)))
            replay_state = dict(self.startup_backward_replay_state.get(sym_key, {}) or {})
            replay_done = bool(replay_state.get("replay_done", False)) or bool(
                attrs.get("startup_backward_replay_done", False)
            )
            row = {
                "symbol": str(sym_key),
                "pending": bool(pending),
                "ready": bool(ready),
                "bars": int(bars),
                "retry_age_secs": float(retry_age),
                "startup_backward_replay_done": bool(replay_done),
            }
            self.startup_backfill_by_symbol[sym_key] = row
            if pending:
                if selected_pending is None or float(row["retry_age_secs"]) >= float(
                    selected_pending.get("retry_age_secs", -1.0)
                ):
                    selected_pending = dict(row)
            elif ready:
                if selected_ready is None or int(row["bars"]) >= int(selected_ready.get("bars", -1)):
                    selected_ready = dict(row)

        selected = selected_pending if selected_pending is not None else selected_ready
        if selected:
            self.startup_backfill_symbol = str(selected.get("symbol", ""))
            self.startup_backfill_pending_active = bool(selected.get("pending", False))
            self.startup_backfill_ready_active = bool(selected.get("ready", False))
            self.startup_backfill_bars_active = int(selected.get("bars", 0))
            self.startup_backfill_retry_age_secs_active = float(selected.get("retry_age_secs", 0.0))
            self.startup_backward_replay_done_active = bool(selected.get("startup_backward_replay_done", False))

    def _run_startup_backward_replay(self, md: dict[str, pd.DataFrame]) -> None:
        self.startup_backward_replay_cycle_block = False
        if self.startup_warmup_strategy != "backward_bridge":
            return
        if not self.startup_backward_replay_state:
            return

        min_bars = int(max(252, self.el_window + 5))
        replayed_any = False
        for sym_key, state in list(self.startup_backward_replay_state.items()):
            if not bool(state.get("active", False)):
                continue
            md_key = self._resolve_md_symbol_key(sym_key, md)
            if md_key is None:
                continue
            df = md.get(md_key)
            if df is None or df.empty:
                continue
            sym_status = dict(self.startup_backfill_by_symbol.get(sym_key, {}) or {})
            if self.startup_backfill_block_entries and bool(sym_status.get("pending", False)):
                continue
            total_bars = int(len(df))
            if total_bars < min_bars:
                logger.warning(
                    "Startup backward replay skipped for %s (bars=%d < min=%d)",
                    sym_key,
                    total_bars,
                    min_bars,
                )
                state["active"] = False
                state["replay_done"] = True
                state["last_replay_ts"] = float(time.time())
                state["replay_steps"] = 0
                self.startup_backward_replay_state[sym_key] = state
                continue

            replayed_any = True
            self.startup_backward_replay_cycle_block = True
            replay_bars = int(max(1, min(total_bars, int(state.get("replay_bars", self.startup_backward_replay_bars)))))
            start_n = int(max(min_bars, total_bars - replay_bars + 1))
            steps = int(max(0, total_bars - start_n + 1))
            logger.info(
                "Startup backward replay start for %s (%d steps, bars=%d)",
                sym_key,
                steps,
                total_bars,
            )
            self._audit_emit_row(
                {
                    "phase": "state",
                    "symbol": str(sym_key),
                    "side": "",
                    "rejection_reason": "startup_backward_replay",
                    "outcome": "startup_backward_replay_start",
                    "warmup_mode": True,
                    "warmup_strategy": str(self.startup_warmup_strategy),
                    "startup_backfill_pending": bool(sym_status.get("pending", False)),
                    "startup_backfill_ready": bool(sym_status.get("ready", False)),
                    "startup_backfill_bars": int(sym_status.get("bars", state.get("backfill_bars", 0))),
                    "startup_backfill_retry_age_secs": float(sym_status.get("retry_age_secs", 0.0)),
                    "startup_backward_replay_done": False,
                }
            )
            try:
                for end_n in range(start_n, total_bars + 1):
                    self.score_symbol(df.iloc[:end_n], md_key)
            except Exception as exc:
                logger.warning("Startup backward replay failed for %s: %s", sym_key, exc)
            state["active"] = False
            state["replay_done"] = True
            state["last_replay_ts"] = float(time.time())
            state["replay_steps"] = int(steps)
            self.startup_backward_replay_state[sym_key] = state
            try:
                df.attrs["startup_backward_replay_done"] = True
            except Exception:
                pass
            self.model_state_degraded_symbols.discard(sym_key)
            self._audit_emit_row(
                {
                    "phase": "state",
                    "symbol": str(sym_key),
                    "side": "",
                    "rejection_reason": "",
                    "outcome": "startup_backward_replay_complete",
                    "warmup_mode": False,
                    "warmup_strategy": str(self.startup_warmup_strategy),
                    "startup_backfill_pending": bool(sym_status.get("pending", False)),
                    "startup_backfill_ready": bool(sym_status.get("ready", False)),
                    "startup_backfill_bars": int(sym_status.get("bars", state.get("backfill_bars", 0))),
                    "startup_backfill_retry_age_secs": float(sym_status.get("retry_age_secs", 0.0)),
                    "startup_backward_replay_done": True,
                }
            )

        if not replayed_any:
            self.startup_backward_replay_cycle_block = False

    def _update_starvation_state(self, executed_count: int) -> None:
        row = {
            "cycle": int(self.cycle_id),
            "executed_count": int(executed_count),
            "rejections": dict(self.rejection_stats_cycle),
        }
        self.cycle_exec_history.append(row)
        max_hist = max(self.starvation_window_cycles * 4, 200)
        if len(self.cycle_exec_history) > max_hist:
            self.cycle_exec_history = self.cycle_exec_history[-max_hist:]

        window = list(self.cycle_exec_history[-self.starvation_window_cycles :])
        if not window:
            self.starvation_mode_active = False
            self.starvation_no_exec_cycles = 0
            self.starvation_step_count = 0
            self.starvation_relax_level = 0.0
            return

        total_exec = int(sum(int(r.get("executed_count", 0)) for r in window))
        self.starvation_no_exec_cycles = int(sum(1 for r in window if int(r.get("executed_count", 0)) == 0))
        low_score_hits = 0
        total_rejections = 0
        hard_gate_bad = 0
        for r in window:
            rej = dict(r.get("rejections", {}) or {})
            total_rejections += int(sum(int(v) for v in rej.values()))
            low_score_hits += int(rej.get("low_score", 0))
            low_score_hits += int(rej.get("soft_low_score", 0))
            low_score_hits += int(rej.get("exec_low_score_ratio", 0))
            low_score_hits += int(rej.get("zero_score_collapse", 0))
            low_score_hits += int(rej.get("soft_zero_score_collapse", 0))
            hard_gate_bad += int(rej.get("stale_tick", 0))
            hard_gate_bad += int(rej.get("stale_tick_missing", 0))
            hard_gate_bad += int(rej.get("cost_gate", 0))
            hard_gate_bad += int(rej.get("spread", 0))

        low_score_share = float(low_score_hits / max(total_rejections, 1))
        hard_gates_healthy = hard_gate_bad == 0
        activate = (
            total_exec == 0
            and len(window) >= self.starvation_window_cycles
            and self.starvation_no_exec_cycles >= self.starvation_window_cycles
            and low_score_share >= float(self.starvation_reject_share_min)
            and hard_gates_healthy
        )
        self.starvation_mode_active = bool(activate)
        if not activate:
            self.starvation_step_count = 0
            self.starvation_relax_level = 0.0
            return

        extra = max(int(self.starvation_no_exec_cycles) - int(self.starvation_window_cycles), 0)
        self.starvation_step_count = int(1 + (extra // max(self.starvation_step_cycles, 1)))
        self.starvation_relax_level = float(self.starvation_step_count * self.starvation_relax_step)

    def _dynamic_transition_mult(self) -> float:
        if not self.starvation_mode_active:
            return float(self.regime_score_mult_transition_base)
        base = float(self.regime_score_mult_transition_base)
        return float(max(base - float(self.starvation_relax_level), self.starvation_transition_mult_floor))

    def _dynamic_exec_min_score_ratio(
        self,
        *,
        confidence_exec: float,
        sharpe_ratio: float,
        cost_ratio: float,
        blockers: str | None = None,
    ) -> float:
        base = float(self.exec_min_score_ratio)
        blockers_text = str(blockers or "").strip().lower()
        if (
            self.execution_gate_mode == "soft"
            and "low_score" in blockers_text
            and float(confidence_exec) >= float(self.exec_min_confidence)
            and float(sharpe_ratio) >= float(self.exec_min_sharpe_ratio)
            and float(cost_ratio) >= 1.0
        ):
            base = min(base, float(self.exec_min_score_ratio_soft))
        if not self.starvation_mode_active:
            return base
        if float(confidence_exec) < 45.0:
            return base
        if float(sharpe_ratio) < 0.8:
            return base
        if float(cost_ratio) < 2.0:
            return base
        relaxed = base - float(self.starvation_step_count * self.starvation_relax_step)
        return float(max(relaxed, self.exec_min_score_ratio_floor_starvation))

    def _allow_soft_exec_low_score_bypass(
        self,
        *,
        blockers: str | None,
        confidence_exec: float,
        sharpe_ratio: float,
        cost_ratio: float,
        score_ratio_exec: float,
    ) -> bool:
        """Allow soft-mode execution when only score-ratio is weak but other quality gates are healthy."""
        if str(self.execution_gate_mode) != "soft":
            return False
        blockers_text = str(blockers or "").strip().lower()
        if "low_score" not in blockers_text:
            return False
        if float(confidence_exec) < float(self.exec_min_confidence):
            return False
        if float(sharpe_ratio) < float(self.exec_min_sharpe_ratio):
            return False
        if float(cost_ratio) < 1.0:
            return False
        soft_floor = float(np.clip(min(self.exec_min_score_ratio_soft, self.exec_min_score_ratio), 0.0, 1.50))
        return float(score_ratio_exec) >= soft_floor

    def _update_daily_loss_breaker(self, equity: float) -> None:
        now = pd.Timestamp.now("UTC")
        day_key = str(now.date())
        eq = float(max(equity, 1e-9))
        threshold = float(
            np.clip(
                getattr(self, "daily_loss_breaker_dynamic_pct", self.daily_loss_breaker_pct),
                0.0,
                0.25,
            )
        )
        if self.daily_eq_anchor_day != day_key:
            self.daily_eq_anchor_day = day_key
            self.daily_eq_anchor = eq
            self.daily_breaker_active = False
            return
        if self.daily_eq_anchor <= 0:
            self.daily_eq_anchor = eq
            return
        dd = max(0.0, (self.daily_eq_anchor - eq) / max(self.daily_eq_anchor, 1e-9))
        if (not self.daily_breaker_active) and dd >= threshold:
            self.daily_breaker_active = True
            logger.warning(
                "Daily loss breaker activated: dd=%.2f%% threshold=%.2f%% anchor=%.2f eq=%.2f",
                dd * 100.0,
                threshold * 100.0,
                self.daily_eq_anchor,
                eq,
            )

    def _apply_bridge_safety_degrade(self) -> None:
        if not self.bridge_safety_degrade_enabled:
            return
        metrics = bridge_client.get_metrics(max_retries=1)
        if not metrics:
            return
        self.bridge_safety_last_metrics = dict(metrics)
        timeouts = dict(metrics.get("timeouts", {}) or {})
        queue = dict(metrics.get("queue", {}) or {})
        timeout_rate = float(timeouts.get("ack_timeout_rate_5m", 0.0))
        pending_oldest = float(queue.get("pending_oldest_secs", 0.0))
        pending_count = int(queue.get("pending_count", 0))
        trigger = (
            timeout_rate > float(self.bridge_safety_ack_timeout_rate_max)
            or (
                pending_oldest > float(self.bridge_safety_pending_oldest_secs_max)
                and pending_count > int(self.bridge_safety_pending_count_max)
            )
        )
        if trigger:
            self.bridge_safety_close_only_until_cycle = max(
                int(self.bridge_safety_close_only_until_cycle),
                int(self.cycle_id + self.bridge_safety_degrade_cycles),
            )
            self._log_rejection("bridge_safety_degrade")

    def _estimate_position_risk_pct(
        self,
        symbol: str,
        lots: float,
        md: dict[str, pd.DataFrame],
        equity: float,
    ) -> float:
        """
        Approximate open-position risk as stop-distance risk fraction of equity.
        Uses the same volatility-stop approximation as entry sizing.
        """
        if lots <= 0:
            return 0.0
        sym_key = self._resolve_md_symbol_key(symbol, md)
        if sym_key is None:
            return float(np.clip(self.risk_manager.risk_per_trade, 0.0, 0.20))
        df = md.get(sym_key)
        if df is None or df.empty:
            return float(np.clip(self.risk_manager.risk_per_trade, 0.0, 0.20))
        try:
            close = df["close"]
            current_price = float(close.iloc[-1])
            ret = np.log(close).diff().dropna()
            local_vol = float(ret.tail(96).std(ddof=0)) if len(ret) >= 24 else float(ret.std(ddof=0))
            if not np.isfinite(local_vol) or local_vol <= 0:
                local_vol = max(self.vol_ref_runtime, 1e-6)
            sl_dist = current_price * local_vol * 2.0
            pip_size = self._pip_size(sym_key)
            stop_pips = max(sl_dist / max(pip_size, 1e-9), 0.1)
            pip_value_symbol = self._pip_value_per_standard_lot(sym_key, current_price, md)
            risk_cash = stop_pips * max(pip_value_symbol, 1e-9) * max(float(lots), 0.0)
            risk_pct = risk_cash / max(float(equity), 1e-9)
            return float(np.clip(risk_pct, 0.0, 0.20))
        except Exception as exc:
            logger.debug(f"{symbol}: open-risk estimation fallback ({exc})")
            return float(np.clip(self.risk_manager.risk_per_trade, 0.0, 0.20))

    def _build_corr_clusters(
        self,
        md: dict[str, pd.DataFrame],
        threshold: float,
    ) -> dict[str, str]:
        """
        Build correlation-connected components from recent H1 returns.
        Symbols linked with |corr| >= threshold share a risk cluster.
        """
        threshold = float(np.clip(threshold, 0.0, 0.99))
        symbols = [s for s, df in md.items() if df is not None and (not df.empty)]
        if not symbols:
            return {}
        rets = {}
        for s in symbols:
            try:
                ret = np.log(md[s]["close"]).diff().dropna().tail(192)
                if len(ret) >= 24:
                    rets[s] = ret
            except Exception:
                continue
        if len(rets) <= 1:
            out = {}
            for idx, s in enumerate(symbols):
                out[s] = f"C{idx+1:02d}"
                out[str(s).upper()] = out[s]
            return out

        corr = pd.DataFrame(rets).corr().fillna(0.0)
        adjacency: dict[str, set[str]] = {s: set() for s in rets.keys()}
        for i, s1 in enumerate(rets.keys()):
            for j, s2 in enumerate(rets.keys()):
                if j <= i:
                    continue
                c = float(corr.loc[s1, s2]) if s1 in corr.index and s2 in corr.columns else 0.0
                if abs(c) >= threshold:
                    adjacency[s1].add(s2)
                    adjacency[s2].add(s1)

        seen: set[str] = set()
        clusters: dict[str, str] = {}
        cid = 0
        for s in rets.keys():
            if s in seen:
                continue
            cid += 1
            label = f"C{cid:02d}"
            stack = [s]
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                clusters[cur] = label
                clusters[str(cur).upper()] = label
                for nxt in adjacency.get(cur, set()):
                    if nxt not in seen:
                        stack.append(nxt)

        # Symbols without enough data become single-symbol clusters.
        for s in symbols:
            if s not in clusters:
                cid += 1
                label = f"C{cid:02d}"
                clusters[s] = label
                clusters[str(s).upper()] = label
        return clusters

    def _portfolio_risk_snapshot(
        self,
        open_positions: list[dict],
        md: dict[str, pd.DataFrame],
        equity: float,
        cluster_map: dict[str, str],
    ) -> dict:
        """
        Estimate aggregate and cluster-level open risk from current positions.
        """
        total_risk = 0.0
        cluster_risk: dict[str, float] = {}
        for pos in open_positions:
            sym = str(pos.get("symbol", "")).strip()
            if not sym:
                continue
            try:
                lots = float(pos.get("lots", self.min_trade_lot))
            except Exception:
                lots = float(self.min_trade_lot)
            if lots <= 0:
                continue
            sym_key = self._resolve_md_symbol_key(sym, md) or sym
            risk_pct = self._estimate_position_risk_pct(sym_key, lots, md, equity)
            total_risk += risk_pct
            cluster_key = cluster_map.get(sym_key, cluster_map.get(str(sym_key).upper(), f"SINGLE:{str(sym_key).upper()}"))
            cluster_risk[cluster_key] = cluster_risk.get(cluster_key, 0.0) + risk_pct

        return {
            "total_risk_pct": float(total_risk),
            "cluster_risk_pct": {str(k): float(v) for k, v in cluster_risk.items()},
            "cluster_map": dict(cluster_map),
            "portfolio_risk_cap_pct": float(self.portfolio_risk_cap_pct),
            "cluster_risk_cap_pct": float(self.cluster_risk_cap_pct),
        }

    def _update_governance_state(
        self,
        equity: float,
        *,
        volatility: float | None = None,
        trend_prob: float | None = None,
    ) -> dict:
        """
        Live governance layer:
        - Soft drawdown/edge decay -> de-risk via risk-scale.
        - Hard drawdown -> pause new entries for N cycles.
        """
        self.gov_cycle += 1
        eq = max(float(equity), 1e-9)
        if self.gov_equity_peak <= 0:
            self.gov_equity_peak = eq
        else:
            self.gov_equity_peak = max(self.gov_equity_peak, eq)

        drawdown = max(0.0, (self.gov_equity_peak - eq) / max(self.gov_equity_peak, 1e-9))
        top = dict(getattr(self, "last_best_candidate", {}) or {})
        p_trend_hint = float(top.get("p_trend", 0.5))
        if trend_prob is not None:
            try:
                p_trend_hint = float(trend_prob)
            except Exception:
                p_trend_hint = float(top.get("p_trend", 0.5))
        p_trend_hint = float(np.clip(p_trend_hint, 0.0, 1.0))
        vol_hint = float(max(0.0, volatility if volatility is not None else self.vol_ref_runtime))
        edge_now = float(top.get("utility", top.get("score_effective", top.get("score", 0.0)) * 1e-4))
        conf_now = float(top.get("confidence_exec", top.get("confidence_raw", top.get("confidence", 0.0))))

        self.gov_edge_history.append(edge_now)
        self.gov_conf_history.append(conf_now)
        max_hist = max(self.gov_edge_window * 4, 200)
        if len(self.gov_edge_history) > max_hist:
            self.gov_edge_history = self.gov_edge_history[-max_hist:]
        if len(self.gov_conf_history) > max_hist:
            self.gov_conf_history = self.gov_conf_history[-max_hist:]

        w = min(self.gov_edge_window, len(self.gov_edge_history))
        rolling_edge = float(np.mean(self.gov_edge_history[-w:])) if w > 0 else edge_now
        rolling_conf = float(np.mean(self.gov_conf_history[-w:])) if w > 0 else conf_now

        soft_dd_threshold = float(self.gov_soft_dd_pct)
        hard_dd_threshold = float(self.gov_hard_dd_pct)
        daily_breaker_threshold = float(self.daily_loss_breaker_pct)
        envelope_payload: dict = {}
        if self.use_adaptive_risk_envelope and compute_adaptive_risk_envelope is not None:
            try:
                envelope = compute_adaptive_risk_envelope(
                    volatility=vol_hint,
                    trend_prob=p_trend_hint,
                    soft_band=(float(self.gov_soft_dd_min), float(self.gov_soft_dd_max)),
                    hard_band=(float(self.gov_hard_dd_min), float(self.gov_hard_dd_max)),
                    daily_band=(float(self.daily_breaker_min), float(self.daily_breaker_max)),
                    now_ts=time.time(),
                )
                envelope_payload = envelope.to_dict()
                soft_dd_threshold = float(envelope.soft_dd_pct)
                hard_dd_threshold = float(envelope.hard_dd_pct)
                daily_breaker_threshold = float(envelope.daily_breaker_pct)
            except Exception as exc:
                logger.debug("Adaptive risk envelope update failed: %s", exc)
                envelope_payload = {}

        self.latest_risk_envelope = dict(envelope_payload or {})
        self.daily_loss_breaker_dynamic_pct = float(np.clip(daily_breaker_threshold, 0.0, 0.25))

        reasons: list[str] = []
        risk_scale = 1.0
        hard_triggered = False
        if self.use_live_governance:
            if drawdown >= hard_dd_threshold:
                hard_triggered = True
                self.gov_pause_until_cycle = max(self.gov_pause_until_cycle, self.gov_cycle + self.gov_pause_cycles)
                reasons.append("hard_drawdown")

            edge_decay = (w >= self.gov_edge_window) and (rolling_edge < self.gov_min_edge)
            conf_decay = (w >= self.gov_edge_window) and (rolling_conf < self.gov_min_conf)
            if drawdown >= soft_dd_threshold:
                risk_scale = min(risk_scale, self.gov_soft_risk_scale)
                reasons.append("soft_drawdown")
            if edge_decay:
                risk_scale = min(risk_scale, self.gov_soft_risk_scale)
                reasons.append("edge_decay")
            if conf_decay:
                risk_scale = min(risk_scale, self.gov_soft_risk_scale)
                reasons.append("confidence_decay")

            paused = self.gov_cycle < self.gov_pause_until_cycle
            # Allow early resume when regime recovers.
            if paused and drawdown <= self.gov_recovery_dd_pct and rolling_edge >= self.gov_min_edge:
                self.gov_pause_until_cycle = self.gov_cycle
                paused = False
            if paused:
                risk_scale = 0.0
                reasons.append("paused")
        else:
            paused = False

        self.dynamic_risk_scale = float(np.clip(risk_scale, 0.0, 1.0))
        self.governance_state = {
            "enabled": bool(self.use_live_governance),
            "adaptive_enabled": bool(self.use_adaptive_risk_envelope and compute_adaptive_risk_envelope is not None),
            "cycle": int(self.gov_cycle),
            "equity_peak": float(self.gov_equity_peak),
            "drawdown_pct": float(drawdown),
            "soft_dd_pct": float(soft_dd_threshold),
            "hard_dd_pct": float(hard_dd_threshold),
            "recovery_dd_pct": float(self.gov_recovery_dd_pct),
            "daily_breaker_pct": float(self.daily_loss_breaker_dynamic_pct),
            "edge_now": float(edge_now),
            "edge_rolling": float(rolling_edge),
            "edge_min": float(self.gov_min_edge),
            "confidence_now": float(conf_now),
            "confidence_rolling": float(rolling_conf),
            "confidence_min": float(self.gov_min_conf),
            "risk_scale": float(self.dynamic_risk_scale),
            "paused": bool(paused),
            "pause_until_cycle": int(self.gov_pause_until_cycle),
            "pause_cycles": int(self.gov_pause_cycles),
            "hard_triggered": bool(hard_triggered),
            "reasons": list(dict.fromkeys(reasons)),
            "daily_breaker_active": bool(self.daily_breaker_active),
            "startup_warmup_active": bool(self._warmup_mode_active()),
            "warmup_strategy": str(self.startup_warmup_strategy),
            "startup_backfill_pending": bool(self.startup_backfill_pending_active),
            "startup_backfill_ready": bool(self.startup_backfill_ready_active),
            "startup_backward_replay_done": bool(self.startup_backward_replay_done_active),
            "starvation_mode_active": bool(self.starvation_mode_active),
            "risk_envelope": dict(self.latest_risk_envelope),
            "risk_envelope_inputs": {
                "volatility": float(vol_hint),
                "trend_prob": float(p_trend_hint),
                "soft_band": [float(self.gov_soft_dd_min), float(self.gov_soft_dd_max)],
                "hard_band": [float(self.gov_hard_dd_min), float(self.gov_hard_dd_max)],
                "daily_band": [float(self.daily_breaker_min), float(self.daily_breaker_max)],
            },
        }
        return self.governance_state

    def _candidate_key(self, symbol: str, side: str) -> tuple[str, str]:
        return (str(symbol).upper(), str(side).upper())

    def _execution_risk_scale(self, candidate_row: dict | None) -> float:
        """
        Confidence-weighted risk scaling for entries:
        weak candidates run near floor; strong candidates can scale above base risk.
        """
        if (not self.use_confidence_risk_sizing) or (not candidate_row):
            return 1.0
        conf_src = float(
            candidate_row.get(
                "confidence_exec",
                candidate_row.get("confidence_raw", candidate_row.get("confidence", 0.0)),
            )
        )
        conf = float(np.clip(conf_src / 100.0, 0.0, 1.0))
        scale = self.conf_risk_floor + (self.conf_risk_ceiling - self.conf_risk_floor) * (conf ** self.conf_risk_power)
        blocker = str(candidate_row.get("blocked_by", "none"))
        if blocker and blocker != "none":
            scale *= self.soft_blocked_risk_scale
        if str(candidate_row.get("fallback_path", "none")) == "neutral_micro_ai":
            scale = min(scale, float(self.neutral_micro_fallback_risk_mult))
        return float(np.clip(scale, 0.0, self.conf_risk_ceiling))

    def _position_side_from_payload(self, pos: dict) -> str:
        """Normalize bridge position side into BUY/SELL/NONE."""
        p_type_raw = pos.get("type", -1)
        if isinstance(p_type_raw, str):
            pt = p_type_raw.upper()
            if pt in {"BUY", "SELL"}:
                return pt
            return "NONE"
        try:
            p_type_int = int(p_type_raw)
            return "BUY" if p_type_int == 0 else ("SELL" if p_type_int == 1 else "NONE")
        except Exception:
            return "NONE"

    def _summarize_positions_by_symbol(self, open_positions: list[dict]) -> dict[str, dict]:
        """
        Per-symbol summary used for winner-add checks.
        Assumes bridge positions are already filtered by EA magic.
        """
        out: dict[str, dict] = {}
        for pos in list(open_positions or []):
            sym_raw = str(pos.get("symbol", "") or "").strip()
            if not sym_raw:
                continue
            side = self._position_side_from_payload(pos)
            if side not in {"BUY", "SELL"}:
                continue
            sym_key = sym_raw.upper()
            row = out.setdefault(
                sym_key,
                {
                    "count": 0,
                    "buy_count": 0,
                    "sell_count": 0,
                    "profit": 0.0,
                },
            )
            row["count"] = int(row["count"]) + 1
            row["buy_count"] = int(row["buy_count"]) + (1 if side == "BUY" else 0)
            row["sell_count"] = int(row["sell_count"]) + (1 if side == "SELL" else 0)
            try:
                row["profit"] = float(row.get("profit", 0.0)) + float(pos.get("profit", 0.0))
            except Exception:
                pass

        for sym_key, row in out.items():
            buy_count = int(row.get("buy_count", 0))
            sell_count = int(row.get("sell_count", 0))
            if buy_count > 0 and sell_count == 0:
                side = "BUY"
            elif sell_count > 0 and buy_count == 0:
                side = "SELL"
            else:
                side = "MIXED"
            row["side"] = side
            row["winner"] = bool(float(row.get("profit", 0.0)) > float(self.winner_add_min_profit))
            out[sym_key] = row
        return out

    def _current_position_r(self, symbol: str, side: str, current_price: float) -> float:
        """Approximate current R for symbol from RiskManager state."""
        st = self.risk_manager.positions.get(symbol)
        if st is None:
            st = self.risk_manager.positions.get(str(symbol).upper())
        if st is None or float(getattr(st, "r_distance", 0.0)) <= 0:
            return 0.0
        entry = float(getattr(st, "entry_price", current_price))
        r_dist = float(getattr(st, "r_distance", 0.0))
        if r_dist <= 0:
            return 0.0
        if str(side).upper() == "BUY":
            pnl_dist = float(current_price) - entry
        else:
            pnl_dist = entry - float(current_price)
        return float(pnl_dist / max(r_dist, 1e-9))

    def _winner_add_check(
        self,
        *,
        decision: Decision,
        candidate_row: dict,
        pos_row: dict | None,
        current_price: float,
    ) -> tuple[bool, str]:
        """
        Winner-only continuation add gate.
        Returns (allowed, rejection_reason).
        """
        if not self.enable_winner_adds:
            return False, "already_held"
        if not pos_row:
            return False, "winner_add_missing_pos"
        if str(pos_row.get("side", "MIXED")) == "MIXED":
            return False, "winner_add_mixed_side"
        if str(pos_row.get("side", "NONE")) != str(decision.side).upper():
            return False, "winner_add_side_mismatch"
        if int(pos_row.get("count", 0)) >= int(self.max_adds_per_symbol + 1):
            return False, "winner_add_max_layers"
        if float(pos_row.get("profit", 0.0)) <= float(self.winner_add_min_profit):
            return False, "winner_add_not_profitable"

        current_r = self._current_position_r(decision.symbol, decision.side, current_price)
        if current_r < float(self.winner_add_min_r):
            return False, "winner_add_low_r"

        conf_now = float(candidate_row.get("confidence", getattr(decision, "confidence", 0.0)))
        if conf_now < float(self.winner_add_min_confidence):
            return False, "winner_add_low_conf"
        score_ratio_now = float(candidate_row.get("score_ratio", getattr(decision, "score_ratio", 0.0)))
        if score_ratio_now < float(self.winner_add_min_score_ratio):
            return False, "winner_add_low_score_ratio"
        sharpe_ratio_now = float(candidate_row.get("sharpe_ratio", 0.0))
        if sharpe_ratio_now < float(self.winner_add_min_sharpe_ratio):
            return False, "winner_add_low_sharpe_ratio"
        utility_now = float(candidate_row.get("utility", getattr(decision, "utility", 0.0)))
        if utility_now < float(self.winner_add_min_utility):
            return False, "winner_add_low_utility"

        p_trend = float(candidate_row.get("p_trend", 0.5))
        trend_th = float(self.winner_add_min_trend_prob)
        if str(decision.side).upper() == "BUY":
            if p_trend < trend_th:
                return False, "winner_add_weak_trend"
        else:
            if p_trend > (1.0 - trend_th):
                return False, "winner_add_weak_trend"

        return True, ""

    def _effective_min_trade_lot(self, *, equity: float, pip_value_symbol: float | None = None) -> float:
        """
        Dynamic minimum lot floor:
        - broker hard floor (`min_trade_lot`)
        - optional pip-value target as % of equity (e.g. 0.00004 => $0.40/pip at $10k)
        - optional direct lot-per-equity scaling fallback
        """
        floor = max(float(self.min_trade_lot), 0.01)
        eq = float(equity) if np.isfinite(equity) else 0.0
        if eq > 0:
            if self.use_equity_scaled_pip_target and self.pip_value_target_pct_equity > 0:
                pv = float(pip_value_symbol) if (pip_value_symbol is not None and np.isfinite(pip_value_symbol)) else float(
                    self.pip_value_standard_lot
                )
                if pv > 0:
                    target_pip_value = eq * float(self.pip_value_target_pct_equity)
                    floor = max(floor, target_pip_value / pv)
            if self.use_equity_scaled_min_lot and self.equity_scaled_min_lot_per_unit > 0:
                floor = max(floor, eq * float(self.equity_scaled_min_lot_per_unit))
        floor = math.ceil(floor / self.lot_step_hint) * self.lot_step_hint
        return float(max(floor, self.min_trade_lot, 0.01))

    def _symbol_allowed_for_state(self, symbol: str) -> bool:
        allow = getattr(self, "state_symbol_allow", None)
        if not allow:
            return True
        return str(symbol).upper() in allow

    def _consume_command_budget(self, is_entry: bool) -> tuple[bool, str]:
        now = time.time()
        self._recent_total_cmd_ts = [ts for ts in self._recent_total_cmd_ts if (now - ts) <= 60.0]
        self._recent_entry_cmd_ts = [ts for ts in self._recent_entry_cmd_ts if (now - ts) <= 60.0]
        if len(self._recent_total_cmd_ts) >= self.max_total_commands_per_minute:
            return False, "command_rate_total"
        if is_entry and len(self._recent_entry_cmd_ts) >= self.max_new_entries_per_minute:
            return False, "command_rate_entry"
        self._recent_total_cmd_ts.append(now)
        if is_entry:
            self._recent_entry_cmd_ts.append(now)
        return True, ""

    def _record_close_failure(self, code: str) -> None:
        key = str(code or "UNKNOWN")
        self.close_failures_by_error_code[key] = self.close_failures_by_error_code.get(key, 0) + 1

    def _audit_emit_row(self, row: dict | None) -> None:
        if (not self.audit_trace_enabled) or (not self.audit_trace_path):
            return
        if row is None:
            return
        if self.audit_sample_rate < 1.0 and float(np.random.random()) > self.audit_sample_rate:
            return

        out = dict(row)
        out.setdefault("ts", float(time.time()))
        out.setdefault("cycle_id", int(self.cycle_id))
        out.setdefault("phase", "unknown")
        out.setdefault("symbol", "")
        out.setdefault("score_raw", 0.0)
        out.setdefault("score_effective", 0.0)
        out.setdefault("gate_penalty", 1.0)
        out.setdefault("side_raw", "")
        out.setdefault("blockers", "none")
        out.setdefault("entry_ready", False)
        out.setdefault("exec_quality_ready", False)
        out.setdefault("execution_ready", False)
        out.setdefault("lot_pre_floor", 0.0)
        out.setdefault("lot_post_floor", 0.0)
        out.setdefault("rejection_reason", "")
        out.setdefault("outcome", "unknown")
        out.setdefault("raw_signal", 0.0)
        out.setdefault("score_ratio", 0.0)
        out.setdefault("score_ratio_exec", 0.0)
        out.setdefault("sharpe_ratio", 0.0)
        out.setdefault("cost_ratio", 0.0)
        out.setdefault("exec_min_score_ratio_dynamic", 0.0)
        out.setdefault("exec_score_basis", "score_ratio")
        out.setdefault("zero_score_collapse", False)
        out.setdefault("gap_recovery_source", "none")
        out.setdefault("gap_fill_truncated", False)
        out.setdefault("warmup_mode", bool(self._warmup_mode_active()))
        out.setdefault("warmup_strategy", str(self.startup_warmup_strategy))
        out.setdefault("startup_backfill_pending", bool(self.startup_backfill_pending_active))
        out.setdefault("startup_backfill_ready", bool(self.startup_backfill_ready_active))
        out.setdefault("startup_backfill_bars", int(self.startup_backfill_bars_active))
        out.setdefault("startup_backfill_retry_age_secs", float(self.startup_backfill_retry_age_secs_active))
        out.setdefault("startup_backward_replay_done", bool(self.startup_backward_replay_done_active))
        out.setdefault("starvation_mode", bool(self.starvation_mode_active))
        out.setdefault("relax_level", float(self.starvation_relax_level))
        out.setdefault("direction_abstain_triggered", False)
        out.setdefault("direction_bias_guard_active", False)
        out.setdefault("direction_bias_justification", "none")
        out.setdefault("side_share_buy_rolling", float(self.side_share_buy_rolling))
        out.setdefault("side_share_sell_rolling", float(self.side_share_sell_rolling))
        out.setdefault("abstain_rate_rolling", float(self.abstain_rate_rolling))
        out.setdefault("fallback_path", "none")
        self._audit_rows_pending.append(out)

    def _flush_audit_rows(self) -> None:
        if (not self.audit_trace_enabled) or (not self.audit_trace_path):
            self._audit_rows_pending = []
            return
        if not self._audit_rows_pending:
            return

        rows = list(self._audit_rows_pending)
        self._audit_rows_pending = []

        try:
            with open(self.audit_trace_path, "a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
        except Exception as exc:
            logger.warning("Failed writing audit trace '%s': %s", self.audit_trace_path, exc)
            return

        try:
            cand_rows = [r for r in rows if str(r.get("phase", "")).lower() == "candidate"]
            exec_rows = [r for r in rows if str(r.get("phase", "")).lower() == "execution"]
            exit_rows = [r for r in rows if str(r.get("phase", "")).lower() == "exit"]
            executed = sum(
                1 for r in exec_rows if str(r.get("outcome", "")).lower() in {"executed", "sent"}
            )
            rejected = sum(
                1 for r in exec_rows if str(r.get("outcome", "")).lower().startswith("rejected")
            )
            rejection_counts: dict[str, int] = {}
            for r in exec_rows:
                reason = str(r.get("rejection_reason", "") or "").strip()
                if not reason:
                    continue
                rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            top_reason = ""
            top_reason_count = 0
            if rejection_counts:
                top_reason, top_reason_count = sorted(
                    rejection_counts.items(), key=lambda kv: kv[1], reverse=True
                )[0]

            summary_row = {
                "ts": float(time.time()),
                "cycle_id": int(self.cycle_id),
                "candidate_rows": int(len(cand_rows)),
                "execution_rows": int(len(exec_rows)),
                "exit_rows": int(len(exit_rows)),
                "executed": int(executed),
                "rejected": int(rejected),
                "top_rejection_reason": str(top_reason),
                "top_rejection_count": int(top_reason_count),
            }
            summary_df = pd.DataFrame([summary_row])
            if self._audit_summary_path is not None:
                write_header = not self._audit_summary_path.exists()
                summary_df.to_csv(
                    self._audit_summary_path,
                    mode="a",
                    index=False,
                    header=write_header,
                )
            if self._audit_summary_parquet_path is not None:
                # Best-effort parquet mirror for downstream analytics.
                if self._audit_summary_parquet_path.exists():
                    try:
                        prev = pd.read_parquet(self._audit_summary_parquet_path)
                        summary_df = pd.concat([prev, summary_df], ignore_index=True)
                    except Exception:
                        pass
                try:
                    summary_df.to_parquet(self._audit_summary_parquet_path, index=False)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Failed writing audit summary: %s", exc)

    def _load_direction_state(self) -> None:
        """Load persisted directional history/state so calibration survives restarts."""
        path = str(getattr(self, "direction_state_path", "") or "").strip()
        if not path:
            return
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            hist_raw = dict(payload.get("history", {}) or {})
            state_raw = dict(payload.get("state", {}) or {})

            restored_hist: dict[str, list[tuple[int, int]]] = {}
            for sym, rows in hist_raw.items():
                sym_key = str(sym).upper()
                if not self._symbol_allowed_for_state(sym_key):
                    continue
                parsed: list[tuple[int, int]] = []
                for row in list(rows or []):
                    if not isinstance(row, (list, tuple)) or len(row) != 2:
                        continue
                    try:
                        side = int(row[0])
                        hit = int(row[1])
                    except Exception:
                        continue
                    if side not in (-1, 1):
                        continue
                    if hit not in (0, 1):
                        continue
                    parsed.append((side, hit))
                if parsed:
                    restored_hist[sym_key] = parsed[-self.direction_calib_window :]

            restored_state: dict[str, dict] = {}
            for sym, row in state_raw.items():
                if not isinstance(row, dict):
                    continue
                sym_key = str(sym).upper()
                if not self._symbol_allowed_for_state(sym_key):
                    continue
                st: dict = {}
                try:
                    close_val = float(row.get("close", 0.0))
                    if np.isfinite(close_val) and close_val > 0:
                        st["close"] = close_val
                except Exception:
                    pass
                try:
                    pending_side = int(row.get("pending_side", 0))
                    if pending_side in (-1, 1):
                        st["pending_side"] = pending_side
                except Exception:
                    pass
                for k in ("bar_key", "eval_bar_key"):
                    try:
                        st[k] = int(row.get(k, -1))
                    except Exception:
                        pass
                if st:
                    restored_state[sym_key] = st

            if restored_hist:
                self.direction_history.update(restored_hist)
            if restored_state:
                self.direction_state.update(restored_state)
            if restored_hist or restored_state:
                logger.info(
                    "Loaded directional state from %s (%d symbols hist, %d symbols pending)",
                    path,
                    len(restored_hist),
                    len(restored_state),
                )
        except Exception as exc:
            logger.warning("Failed to load directional state '%s': %s", path, exc)

    def _persist_direction_state(self, force: bool = False) -> None:
        """Persist directional state periodically for continuity across agent restarts."""
        path = str(getattr(self, "direction_state_path", "") or "").strip()
        if not path:
            return
        now = time.time()
        if (not force) and ((now - float(getattr(self, "_last_direction_state_save", 0.0))) < self.direction_state_save_secs):
            return

        history_payload: dict[str, list[list[int]]] = {}
        for sym, rows in (self.direction_history or {}).items():
            if not self._symbol_allowed_for_state(sym):
                continue
            if not rows:
                continue
            clipped = list(rows)[-self.direction_calib_window :]
            history_payload[str(sym).upper()] = [[int(side), int(hit)] for side, hit in clipped]

        state_payload: dict[str, dict] = {}
        for sym, row in (self.direction_state or {}).items():
            if not self._symbol_allowed_for_state(sym):
                continue
            if not row:
                continue
            out_row: dict = {}
            try:
                close_val = float(row.get("close", 0.0))
                if np.isfinite(close_val) and close_val > 0:
                    out_row["close"] = close_val
            except Exception:
                pass
            try:
                pending_side = int(row.get("pending_side", 0))
                if pending_side in (-1, 1):
                    out_row["pending_side"] = pending_side
            except Exception:
                pass
            for k in ("bar_key", "eval_bar_key"):
                try:
                    out_row[k] = int(row.get(k, -1))
                except Exception:
                    pass
            if out_row:
                state_payload[str(sym).upper()] = out_row

        payload = {
            "version": 1,
            "ts": int(now),
            "history": history_payload,
            "state": state_payload,
        }
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"), sort_keys=True)
            os.replace(tmp_path, path)
            self._last_direction_state_save = now
        except Exception as exc:
            logger.warning("Failed to persist directional state '%s': %s", path, exc)

    def _load_ai_indicator_state(self) -> None:
        """Load persisted AI indicator state so online calibration survives restarts."""
        path = str(getattr(self, "ai_indicator_state_path", "") or "").strip()
        if not path:
            return
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            hist_raw = dict(payload.get("history", {}) or {})
            state_raw = dict(payload.get("state", {}) or {})

            restored_hist: dict[str, list[tuple[int, int]]] = {}
            for sym, rows in hist_raw.items():
                sym_key = str(sym).upper()
                if not self._symbol_allowed_for_state(sym_key):
                    continue
                parsed: list[tuple[int, int]] = []
                for row in list(rows or []):
                    if not isinstance(row, (list, tuple)) or len(row) != 2:
                        continue
                    try:
                        side = int(row[0])
                        hit = int(row[1])
                    except Exception:
                        continue
                    if side not in (-1, 1):
                        continue
                    if hit not in (0, 1):
                        continue
                    parsed.append((side, hit))
                if parsed:
                    restored_hist[sym_key] = parsed[-self.ai_indicator_calib_window :]

            restored_state: dict[str, dict] = {}
            for sym, row in state_raw.items():
                if not isinstance(row, dict):
                    continue
                sym_key = str(sym).upper()
                if not self._symbol_allowed_for_state(sym_key):
                    continue
                st: dict = {}
                try:
                    close_val = float(row.get("close", 0.0))
                    if np.isfinite(close_val) and close_val > 0:
                        st["close"] = close_val
                except Exception:
                    pass
                try:
                    pending_side = int(row.get("pending_side", 0))
                    if pending_side in (-1, 1):
                        st["pending_side"] = pending_side
                except Exception:
                    pass
                for k in ("bar_key", "eval_bar_key"):
                    try:
                        st[k] = int(row.get(k, -1))
                    except Exception:
                        pass
                if st:
                    restored_state[sym_key] = st

            if restored_hist:
                self.ai_indicator_history.update(restored_hist)
            if restored_state:
                self.ai_indicator_state.update(restored_state)
            if restored_hist or restored_state:
                logger.info(
                    "Loaded AI indicator state from %s (%d symbols hist, %d symbols pending)",
                    path,
                    len(restored_hist),
                    len(restored_state),
                )
        except Exception as exc:
            logger.warning("Failed to load AI indicator state '%s': %s", path, exc)

    def _persist_ai_indicator_state(self, force: bool = False) -> None:
        """Persist AI indicator state for restart-safe online calibration."""
        path = str(getattr(self, "ai_indicator_state_path", "") or "").strip()
        if not path:
            return
        now = time.time()
        if (not force) and (
            (now - float(getattr(self, "_last_ai_indicator_state_save", 0.0)))
            < self.ai_indicator_state_save_secs
        ):
            return

        history_payload: dict[str, list[list[int]]] = {}
        for sym, rows in (self.ai_indicator_history or {}).items():
            if not self._symbol_allowed_for_state(sym):
                continue
            if not rows:
                continue
            clipped = list(rows)[-self.ai_indicator_calib_window :]
            history_payload[str(sym).upper()] = [[int(side), int(hit)] for side, hit in clipped]

        state_payload: dict[str, dict] = {}
        for sym, row in (self.ai_indicator_state or {}).items():
            if not self._symbol_allowed_for_state(sym):
                continue
            if not row:
                continue
            out_row: dict = {}
            try:
                close_val = float(row.get("close", 0.0))
                if np.isfinite(close_val) and close_val > 0:
                    out_row["close"] = close_val
            except Exception:
                pass
            try:
                pending_side = int(row.get("pending_side", 0))
                if pending_side in (-1, 1):
                    out_row["pending_side"] = pending_side
            except Exception:
                pass
            for k in ("bar_key", "eval_bar_key"):
                try:
                    out_row[k] = int(row.get(k, -1))
                except Exception:
                    pass
            if out_row:
                state_payload[str(sym).upper()] = out_row

        payload = {
            "version": 1,
            "ts": int(now),
            "history": history_payload,
            "state": state_payload,
        }
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"), sort_keys=True)
            os.replace(tmp_path, path)
            self._last_ai_indicator_state_save = now
        except Exception as exc:
            logger.warning("Failed to persist AI indicator state '%s': %s", path, exc)

    def _bar_key(self, df: pd.DataFrame) -> int:
        """Stable bar identity to avoid re-scoring direction outcomes inside the same H1 bar."""
        try:
            last_idx = df.index[-1]
            if isinstance(last_idx, pd.Timestamp):
                return int(last_idx.value)
            parsed = pd.to_datetime(last_idx, errors="coerce", utc=True)
            if pd.notna(parsed):
                return int(parsed.value)
        except Exception:
            pass
        try:
            return int(df.index[-1])
        except Exception:
            return int(len(df))

    def _update_direction_history(self, sym_key: str, current_close: float, bar_key: int) -> None:
        """
        Update realized directional hit stats from the prior bar forecast.
        Uses close-to-close sign and only evaluates once per new bar.
        """
        state = dict(self.direction_state.get(sym_key, {}) or {})
        prev_side = int(state.get("pending_side", 0))
        prev_bar = int(state.get("bar_key", -1))
        eval_bar = int(state.get("eval_bar_key", -1))
        prev_close = float(state.get("close", current_close))
        if prev_side not in (-1, 1):
            return
        if bar_key <= prev_bar:
            return
        if bar_key <= eval_bar:
            return
        if (not np.isfinite(prev_close)) or prev_close <= 0:
            return
        move = float(current_close) - float(prev_close)
        if abs(move) <= max(1e-10, abs(prev_close) * 1e-8):
            return
        realized = 1 if move > 0 else -1
        hit = 1 if realized == prev_side else 0
        hist = self.direction_history.setdefault(sym_key, [])
        hist.append((int(prev_side), int(hit)))
        if len(hist) > self.direction_calib_window:
            del hist[:-self.direction_calib_window]
        state["eval_bar_key"] = int(bar_key)
        self.direction_state[sym_key] = state
        self._persist_direction_state(force=False)

    def _update_ai_indicator_history(self, sym_key: str, current_close: float, bar_key: int) -> None:
        """
        Update realized hit-rate stats from the prior AI indicator forecast.
        Uses close-to-close sign and evaluates once per new bar.
        """
        state = dict(self.ai_indicator_state.get(sym_key, {}) or {})
        prev_side = int(state.get("pending_side", 0))
        prev_bar = int(state.get("bar_key", -1))
        eval_bar = int(state.get("eval_bar_key", -1))
        prev_close = float(state.get("close", current_close))
        if prev_side not in (-1, 1):
            return
        if bar_key <= prev_bar:
            return
        if bar_key <= eval_bar:
            return
        if (not np.isfinite(prev_close)) or prev_close <= 0:
            return
        move = float(current_close) - float(prev_close)
        if abs(move) <= max(1e-10, abs(prev_close) * 1e-8):
            return
        realized = 1 if move > 0 else -1
        hit = 1 if realized == prev_side else 0
        hist = self.ai_indicator_history.setdefault(sym_key, [])
        hist.append((int(prev_side), int(hit)))
        if len(hist) > self.ai_indicator_calib_window:
            del hist[:-self.ai_indicator_calib_window]
        state["eval_bar_key"] = int(bar_key)
        self.ai_indicator_state[sym_key] = state
        self._persist_ai_indicator_state(force=False)

    def _ai_indicator_quality_snapshot(self, sym_key: str) -> dict:
        """Return side-specific AI indicator hit rates and sample counts."""
        hist = list(self.ai_indicator_history.get(sym_key, []) or [])
        n_all = len(hist)
        if n_all <= 0:
            return {
                "samples": 0,
                "buy_samples": 0,
                "sell_samples": 0,
                "buy_hit_rate": 0.5,
                "sell_hit_rate": 0.5,
                "overall_hit_rate": 0.5,
            }

        sides = np.array([int(h[0]) for h in hist], dtype=float)
        hits = np.array([int(h[1]) for h in hist], dtype=float)
        idx = np.arange(n_all, dtype=float)
        hl = max(int(self.ai_indicator_recency_halflife), 1)
        w = np.power(0.5, (float(n_all - 1) - idx) / float(hl))
        w_sum = float(np.sum(w))

        mask_buy = sides > 0
        mask_sell = sides < 0
        n_buy = int(np.sum(mask_buy))
        n_sell = int(np.sum(mask_sell))

        if n_buy > 0:
            w_buy = w[mask_buy]
            buy_hit = float(np.dot(hits[mask_buy], w_buy) / max(float(np.sum(w_buy)), 1e-12))
        else:
            buy_hit = 0.5
        if n_sell > 0:
            w_sell = w[mask_sell]
            sell_hit = float(np.dot(hits[mask_sell], w_sell) / max(float(np.sum(w_sell)), 1e-12))
        else:
            sell_hit = 0.5
        overall_hit = float(np.dot(hits, w) / max(w_sum, 1e-12))
        return {
            "samples": int(n_all),
            "buy_samples": int(n_buy),
            "sell_samples": int(n_sell),
            "buy_hit_rate": float(np.clip(buy_hit, 0.0, 1.0)),
            "sell_hit_rate": float(np.clip(sell_hit, 0.0, 1.0)),
            "overall_hit_rate": float(np.clip(overall_hit, 0.0, 1.0)),
        }

    def _ai_indicator_calibration_factor(self, sym_key: str, side: int, quality: dict | None = None) -> float:
        """Convert AI indicator hit-rate diagnostics into a bounded score multiplier."""
        if side not in (-1, 1):
            return 1.0
        q = dict(quality or self._ai_indicator_quality_snapshot(sym_key))
        n_all = int(q.get("samples", 0))
        if n_all <= 0:
            return 1.0
        n_side = int(q.get("buy_samples", 0) if side > 0 else q.get("sell_samples", 0))
        n_other = int(q.get("sell_samples", 0) if side > 0 else q.get("buy_samples", 0))
        side_rate_raw = float(q.get("buy_hit_rate", 0.5) if side > 0 else q.get("sell_hit_rate", 0.5))
        other_rate_raw = float(q.get("sell_hit_rate", 0.5) if side > 0 else q.get("buy_hit_rate", 0.5))

        n_ref = max(self.ai_indicator_min_samples, 1)
        blend = float(np.clip(n_side / n_ref, 0.0, 1.0))
        side_rate = 0.5 + blend * (side_rate_raw - 0.5)
        other_rate = 0.5 + float(np.clip(n_other / n_ref, 0.0, 1.0)) * (other_rate_raw - 0.5)
        sample_conf = float(np.clip(n_all / n_ref, 0.0, 1.0))

        edge_term = float(np.clip(2.0 * (side_rate - 0.5), -0.35, 0.35))
        relative_term = float(np.clip(side_rate - other_rate, -0.25, 0.25))
        factor = 1.0 + self.ai_indicator_calib_strength * edge_term
        factor += 0.5 * self.ai_indicator_calib_strength * relative_term

        imbalance = float(
            (int(q.get("buy_samples", 0)) - int(q.get("sell_samples", 0)))
            / max(int(q.get("samples", 1)), 1)
        )
        if side > 0 and imbalance > 0:
            factor -= self.ai_indicator_bias_penalty * imbalance * sample_conf * 0.20
        elif side < 0 and imbalance < 0:
            factor -= self.ai_indicator_bias_penalty * abs(imbalance) * sample_conf * 0.20

        return float(np.clip(factor, self.ai_indicator_factor_min, self.ai_indicator_factor_max))

    def _register_ai_indicator_forecast(
        self,
        sym_key: str,
        current_close: float,
        side: int,
        bar_key: int,
        confidence: float | None = None,
        signal_abs: float | None = None,
    ) -> None:
        """Store the current AI directional forecast for next-bar evaluation."""
        state_prev = dict(self.ai_indicator_state.get(sym_key, {}) or {})
        if int(state_prev.get("bar_key", -1)) == int(bar_key):
            return

        conf_val = 0.0
        if confidence is not None:
            try:
                conf_val = float(np.clip(confidence, 0.0, 1.0))
            except Exception:
                conf_val = 0.0
        sig_val = 0.0
        if signal_abs is not None:
            try:
                sig_val = abs(float(signal_abs))
            except Exception:
                sig_val = 0.0

        pending_side = int(side) if side in (-1, 1) else 0
        if conf_val < float(self.ai_learn_min_confidence):
            pending_side = 0
        if sig_val < float(self.ai_min_signal):
            pending_side = 0

        new_state = {
            "close": float(current_close),
            "pending_side": int(pending_side),
            "bar_key": int(bar_key),
            "ts": float(time.time()),
        }
        if "eval_bar_key" in state_prev:
            try:
                new_state["eval_bar_key"] = int(state_prev.get("eval_bar_key", -1))
            except Exception:
                pass
        self.ai_indicator_state[sym_key] = new_state
        self._persist_ai_indicator_state(force=False)

    def _direction_quality_snapshot(self, sym_key: str) -> dict:
        """Return side-specific directional hit rates and sample counts."""
        hist = list(self.direction_history.get(sym_key, []) or [])
        n_all = len(hist)
        if n_all <= 0:
            return {
                "samples": 0,
                "buy_samples": 0,
                "sell_samples": 0,
                "buy_hit_rate": 0.5,
                "sell_hit_rate": 0.5,
                "overall_hit_rate": 0.5,
            }

        sides = np.array([int(h[0]) for h in hist], dtype=float)
        hits = np.array([int(h[1]) for h in hist], dtype=float)
        idx = np.arange(n_all, dtype=float)
        hl = max(int(self.direction_recency_halflife), 1)
        # Newer observations get higher weight; this adapts faster after regime shifts.
        w = np.power(0.5, (float(n_all - 1) - idx) / float(hl))
        w_sum = float(np.sum(w)) if n_all > 0 else 0.0

        mask_buy = sides > 0
        mask_sell = sides < 0
        n_buy = int(np.sum(mask_buy))
        n_sell = int(np.sum(mask_sell))

        if n_buy > 0:
            w_buy = w[mask_buy]
            buy_hit = float(np.dot(hits[mask_buy], w_buy) / max(float(np.sum(w_buy)), 1e-12))
        else:
            buy_hit = 0.5
        if n_sell > 0:
            w_sell = w[mask_sell]
            sell_hit = float(np.dot(hits[mask_sell], w_sell) / max(float(np.sum(w_sell)), 1e-12))
        else:
            sell_hit = 0.5
        overall_hit = float(np.dot(hits, w) / max(w_sum, 1e-12))
        return {
            "samples": int(n_all),
            "buy_samples": int(n_buy),
            "sell_samples": int(n_sell),
            "buy_hit_rate": float(np.clip(buy_hit, 0.0, 1.0)),
            "sell_hit_rate": float(np.clip(sell_hit, 0.0, 1.0)),
            "overall_hit_rate": float(np.clip(overall_hit, 0.0, 1.0)),
        }

    def _direction_calibration_factor(self, sym_key: str, side: int, quality: dict | None = None) -> float:
        """
        Convert side-specific hit-rate diagnostics into a bounded score multiplier.
        """
        if (not self.use_directional_calibration) or side not in (-1, 1):
            return 1.0
        q = dict(quality or self._direction_quality_snapshot(sym_key))
        n_all = int(q.get("samples", 0))
        if n_all <= 0:
            return 1.0
        n_side = int(q.get("buy_samples", 0) if side > 0 else q.get("sell_samples", 0))
        n_other = int(q.get("sell_samples", 0) if side > 0 else q.get("buy_samples", 0))
        side_rate_raw = float(q.get("buy_hit_rate", 0.5) if side > 0 else q.get("sell_hit_rate", 0.5))
        other_rate_raw = float(q.get("sell_hit_rate", 0.5) if side > 0 else q.get("buy_hit_rate", 0.5))

        # Shrink noisy side estimates toward 0.5 until minimum sample size is reached.
        n_ref = max(self.direction_min_samples, 1)
        blend = float(np.clip(n_side / n_ref, 0.0, 1.0))
        side_rate = 0.5 + blend * (side_rate_raw - 0.5)
        other_rate = 0.5 + float(np.clip(n_other / n_ref, 0.0, 1.0)) * (other_rate_raw - 0.5)
        sample_conf = float(np.clip(n_all / n_ref, 0.0, 1.0))

        edge_term = float(np.clip(2.0 * (side_rate - 0.5), -0.35, 0.35))
        relative_term = float(np.clip(side_rate - other_rate, -0.25, 0.25))
        factor = 1.0 + self.direction_calib_strength * edge_term + 0.5 * self.direction_calib_strength * relative_term

        # Penalize over-dominant side concentration to reduce persistent directional bias.
        imbalance = float(
            (int(q.get("buy_samples", 0)) - int(q.get("sell_samples", 0)))
            / max(int(q.get("samples", 1)), 1)
        )
        if side > 0 and imbalance > 0:
            factor -= self.direction_bias_penalty * imbalance * sample_conf * 0.20
        elif side < 0 and imbalance < 0:
            factor -= self.direction_bias_penalty * abs(imbalance) * sample_conf * 0.20

        return float(np.clip(factor, self.direction_factor_min, self.direction_factor_max))

    def _register_direction_forecast(
        self,
        sym_key: str,
        current_close: float,
        side: int,
        bar_key: int,
        score_abs: float | None = None,
    ) -> None:
        """Store the current directional forecast for evaluation on the next bar."""
        if side not in (-1, 1):
            return
        state_prev = dict(self.direction_state.get(sym_key, {}) or {})
        # Keep one forecast per bar; intra-bar loops should not rewrite baseline/side.
        if int(state_prev.get("bar_key", -1)) == int(bar_key):
            return

        score_val = None
        if score_abs is not None:
            try:
                score_val = abs(float(score_abs))
            except Exception:
                score_val = None
        learn_floor = max(float(self.score_th) * float(self.direction_learn_min_score_ratio), 0.0)
        pending_side = int(side)
        if score_val is not None and learn_floor > 0 and score_val < learn_floor:
            # Ignore weak/noisy directional calls for online calibration updates.
            pending_side = 0

        new_state = {
            "close": float(current_close),
            "pending_side": int(pending_side),
            "bar_key": int(bar_key),
            "ts": float(time.time()),
        }
        if "eval_bar_key" in state_prev:
            try:
                new_state["eval_bar_key"] = int(state_prev.get("eval_bar_key", -1))
            except Exception:
                pass
        self.direction_state[sym_key] = new_state

    def _ensemble_pz(self, close: pd.Series, pz_main: float) -> tuple[float, float, float]:
        """
        Multi-horizon EL momentum ensemble:
        blends fast/current/slow z-momentum to reduce single-window noise.
        """
        if not self.use_directional_ensemble:
            return float(pz_main), float(pz_main), float(pz_main)
        n = int(len(close))
        if n < 32:
            return float(pz_main), float(pz_main), float(pz_main)
        fast_w = int(np.clip(self.dir_fast_window, 8, max(8, n - 6)))
        slow_w = int(np.clip(self.dir_slow_window, fast_w + 2, max(fast_w + 2, n - 4)))
        fast_ema = max(3, int(self.el_ema_span * 0.7))
        slow_ema = max(3, int(self.el_ema_span * 1.6))
        try:
            pz_fast = float(el_pz(close, fast_w, fast_ema).iloc[-1])
        except Exception:
            pz_fast = float(pz_main)
        try:
            pz_slow = float(el_pz(close, slow_w, slow_ema).iloc[-1])
        except Exception:
            pz_slow = float(pz_main)
        wsum = (
            abs(float(self.dir_base_weight))
            + abs(float(self.dir_fast_weight))
            + abs(float(self.dir_slow_weight))
        )
        if wsum <= 1e-9:
            return float(pz_main), float(pz_fast), float(pz_slow)
        pz_blend = (
            float(self.dir_base_weight) * float(pz_main)
            + float(self.dir_fast_weight) * float(pz_fast)
            + float(self.dir_slow_weight) * float(pz_slow)
        ) / wsum
        return float(pz_blend), float(pz_fast), float(pz_slow)

    def _parse_horizon_hours(self, raw: object) -> list[int]:
        vals: list[int] = []
        if isinstance(raw, str):
            parts = [p.strip() for p in raw.replace(";", ",").split(",")]
            for part in parts:
                if not part:
                    continue
                try:
                    vals.append(int(float(part)))
                except Exception:
                    continue
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                try:
                    vals.append(int(float(item)))
                except Exception:
                    continue
        if not vals:
            vals = [2, 4, 8, 12, 24]
        vals = sorted({int(np.clip(v, 1, 240)) for v in vals if int(v) > 0})
        if not vals:
            vals = [2, 4, 8, 12, 24]
        return vals

    def _estimate_holding_horizon(
        self,
        close: pd.Series,
        score: float,
        p_trend: float,
        *,
        ai_signal: float = 0.0,
    ) -> dict:
        """
        Estimate which horizon currently carries the strongest directional edge.
        Returns a deterministic profile used to modulate hold patience on exits.
        """
        horizon_hours = list(getattr(self, "hold_horizon_hours", [2, 4, 8, 12, 24]))
        default_h = float(getattr(self, "hold_default_horizon_hours", horizon_hours[0]))
        out = {
            "primary_horizon_hours": float(default_h),
            "horizon_confidence": 0.0,
            "horizon_strength": 0.0,
            "horizon_side": "NONE",
            "horizon_scores": {},
        }
        if len(close) < (max(horizon_hours) + 8):
            return out
        try:
            c = close.astype(float)
            r = np.log(c).diff().dropna()
            if len(r) < max(24, max(horizon_hours) + 4):
                return out
            vol_ref = float(r.tail(min(len(r), 96)).std(ddof=0))
            if (not np.isfinite(vol_ref)) or vol_ref <= 0.0:
                vol_ref = float(r.std(ddof=0))
            vol_ref = max(vol_ref, 1e-6)

            score_side = 1 if float(score) >= 0.0 else -1
            trend_bias = float(np.clip(2.0 * np.clip(float(p_trend), 0.0, 1.0) - 1.0, -1.0, 1.0))
            ai_bias = float(np.clip(ai_signal, -1.0, 1.0))

            best_h = float(default_h)
            best_metric = -1e9
            best_conf = 0.0
            h_scores: dict[str, float] = {}
            for h in horizon_hours:
                if len(c) <= h:
                    continue
                prev_close = float(c.iloc[-(h + 1)])
                last_close = float(c.iloc[-1])
                if prev_close <= 0.0 or last_close <= 0.0:
                    continue
                ret_h = float(np.log(last_close / prev_close))
                z_h = float(np.clip(ret_h / max(vol_ref * math.sqrt(float(h)), 1e-9), -6.0, 6.0))
                align = float(score_side * z_h)

                step = r.tail(min(len(r), h)).to_numpy(dtype=float)
                if step.size <= 0:
                    consistency = 0.0
                elif score_side > 0:
                    consistency = float((step > 0.0).mean() * 2.0 - 1.0)
                else:
                    consistency = float((step < 0.0).mean() * 2.0 - 1.0)

                trend_align = float(np.sign(z_h) * np.sign(trend_bias)) if abs(trend_bias) > 1e-12 else 0.0
                ai_align = float(np.sign(z_h) * np.sign(ai_bias)) if abs(ai_bias) > 1e-12 else 0.0

                metric = (
                    0.72 * align
                    + 0.14 * consistency
                    + 0.09 * abs(trend_bias) * trend_align
                    + 0.05 * abs(ai_bias) * ai_align
                )
                conf_h = float(np.clip(abs(z_h) / 2.5, 0.0, 1.0))
                weighted_metric = float(metric * (0.40 + 0.60 * conf_h))

                h_scores[str(h)] = float(weighted_metric)
                if weighted_metric > best_metric:
                    best_metric = float(weighted_metric)
                    best_h = float(h)
                    best_conf = float(conf_h)

            if not h_scores:
                return out

            if best_metric > 0.0:
                out["primary_horizon_hours"] = float(best_h)
                out["horizon_strength"] = float(np.clip(best_metric, 0.0, 2.0))
                out["horizon_confidence"] = float(np.clip(best_conf * (0.55 + 0.45 * min(best_metric, 1.0)), 0.0, 1.0))
                out["horizon_side"] = "BUY" if score_side > 0 else "SELL"
            else:
                # No positive edge found: keep horizon short and confidence low.
                out["primary_horizon_hours"] = float(min(horizon_hours))
                out["horizon_strength"] = 0.0
                out["horizon_confidence"] = float(np.clip(best_conf * 0.35, 0.0, 0.40))
                out["horizon_side"] = "NONE"
            out["horizon_scores"] = h_scores
            return out
        except Exception:
            return out

    def _exit_hold_policy(self, diag: dict, side: str) -> dict:
        """
        Derive per-position hold overrides from horizon diagnostics.
        Keeps base behavior when horizon signal is weak.
        """
        side_up = str(side or "").upper()
        base_min_hold = float(getattr(self.risk_manager, "min_hold_secs", 0.0))
        base_time_limit = float(getattr(self.risk_manager, "time_limit_hours", 24.0))
        base_stagnation = float(getattr(self.risk_manager, "stagnation_minutes", 60.0))
        base_regime_th = float(getattr(self.risk_manager, "regime_exit_th", 0.0))
        out = {
            "min_hold_secs": float(max(base_min_hold, 0.0)),
            "time_limit_hours": float(max(base_time_limit, 0.1)),
            "stagnation_minutes": float(max(base_stagnation, 0.1)),
            "regime_exit_th": float(max(base_regime_th, 0.0)),
            "reversal_threshold_mult": 1.0,
        }
        if (not self.use_horizon_hold_policy) or side_up not in {"BUY", "SELL"}:
            return out
        try:
            h_hours = float(diag.get("primary_horizon_hours", self.hold_default_horizon_hours))
            h_conf = float(np.clip(diag.get("horizon_confidence", 0.0), 0.0, 1.0))
            h_strength = float(np.clip(diag.get("horizon_strength", 0.0), 0.0, 2.0))
            h_side = str(diag.get("horizon_side", "NONE")).upper()

            h_min = float(min(self.hold_horizon_hours))
            h_max = float(max(self.hold_horizon_hours))
            h_norm = float(np.clip((np.clip(h_hours, h_min, h_max) - h_min) / max(h_max - h_min, 1.0), 0.0, 1.0))
            strength_norm = float(np.clip(h_strength / 1.5, 0.0, 1.0))

            side_match = 0.5
            if h_side == side_up:
                side_match = 1.0
            elif h_side in {"BUY", "SELL"} and h_side != side_up:
                side_match = 0.0

            patience = float(np.clip((0.55 * h_conf + 0.45 * strength_norm) * (0.35 + 0.65 * h_norm), 0.0, 1.0))
            patience *= side_match if side_match > 0.0 else 0.25

            min_hold_mult = float(1.0 + self.hold_policy_min_hold_gain * patience)
            time_limit_mult = float(1.0 + self.hold_policy_time_limit_gain * patience)
            stagnation_mult = float(1.0 + self.hold_policy_stagnation_gain * patience)
            reversal_mult = float(1.0 + self.hold_policy_reversal_gain * patience)

            if side_match <= 0.0 and h_conf > 0.20:
                oppose = float(np.clip(h_conf * (0.50 + 0.50 * strength_norm), 0.0, 1.0))
                min_hold_mult *= (1.0 - 0.35 * oppose)
                time_limit_mult *= (1.0 - 0.30 * oppose)
                stagnation_mult *= (1.0 - 0.25 * oppose)
                reversal_mult *= (1.0 - 0.35 * oppose)

            floor = float(self.hold_policy_floor_mult)
            cap = float(self.hold_policy_cap_mult)
            min_hold_mult = float(np.clip(min_hold_mult, floor, cap))
            time_limit_mult = float(np.clip(time_limit_mult, floor, cap))
            stagnation_mult = float(np.clip(stagnation_mult, floor, cap))
            reversal_mult = float(np.clip(reversal_mult, floor, cap))

            out["min_hold_secs"] = float(max(base_min_hold * min_hold_mult, 0.0))
            out["time_limit_hours"] = float(max(base_time_limit * time_limit_mult, 0.1))
            out["stagnation_minutes"] = float(max(base_stagnation * stagnation_mult, 0.1))
            out["reversal_threshold_mult"] = float(max(reversal_mult, 0.1))
            return out
        except Exception:
            return out

    def _ai_indicator_signal(self, close: pd.Series, p_trend: float) -> dict:
        """
        Lightweight deterministic AI indicator:
        multi-horizon momentum + slope features normalized by recent volatility.
        """
        out = {
            "raw": 0.0,
            "signal": 0.0,
            "confidence": 0.0,
            "side": 0,
            "z_fast": 0.0,
            "z_med": 0.0,
            "z_slow": 0.0,
            "z_slope": 0.0,
        }
        if len(close) < max(32, self.el_window + 4):
            return out
        try:
            c = close.astype(float)
            r = np.log(c).diff().dropna()
            if len(r) < 24:
                return out

            vol_short = float(r.tail(min(len(r), 48)).std(ddof=0))
            vol_long = float(r.tail(min(len(r), 192)).std(ddof=0))
            vol_ref = max(vol_short, vol_long, 1e-6)

            mom_fast = float(r.tail(min(len(r), 6)).mean())
            mom_med = float(r.tail(min(len(r), 24)).mean())
            mom_slow = float(r.tail(min(len(r), 72)).mean())

            n_slope = min(len(c), 48)
            y = np.log(c.tail(n_slope).to_numpy(dtype=float))
            x = np.arange(n_slope, dtype=float)
            x0 = x - float(np.mean(x))
            y0 = y - float(np.mean(y))
            slope = float(np.dot(x0, y0) / max(float(np.dot(x0, x0)), 1e-12))

            z_fast = float(np.clip(mom_fast / vol_ref, -6.0, 6.0))
            z_med = float(np.clip(mom_med / vol_ref, -6.0, 6.0))
            z_slow = float(np.clip(mom_slow / vol_ref, -6.0, 6.0))
            z_slope = float(np.clip(slope / vol_ref, -6.0, 6.0))
            trend_bias = float(np.clip(2.0 * np.clip(float(p_trend), 0.0, 1.0) - 1.0, -1.0, 1.0))

            raw = (
                0.22 * z_fast
                + 0.32 * z_med
                + 0.34 * z_slow
                + 0.18 * z_slope
                + 0.10 * trend_bias * float(np.sign(z_med)) * min(abs(z_med), 3.0)
            )
            raw = float(np.clip(raw, -4.0, 4.0))
            signal = float(np.tanh(raw * 0.85))
            confidence = float(np.clip(abs(raw) / 2.5, 0.0, 1.0))
            side = 0
            if signal >= float(self.ai_min_signal):
                side = 1
            elif signal <= -float(self.ai_min_signal):
                side = -1

            out.update(
                {
                    "raw": raw,
                    "signal": signal,
                    "confidence": confidence,
                    "side": int(side),
                    "z_fast": z_fast,
                    "z_med": z_med,
                    "z_slow": z_slow,
                    "z_slope": z_slope,
                }
            )
            return out
        except Exception:
            return out

    def _directional_sharpe(self, sc: float, predictive_sharpe: float) -> float:
        """
        Map predictive sharpe into trade-direction space:
        positive means supportive of the proposed side (BUY if sc>=0, SELL if sc<0).
        """
        try:
            sh = float(predictive_sharpe)
        except Exception:
            sh = 0.0
        if not np.isfinite(sh):
            return 0.0
        return sh if float(sc) >= 0.0 else -sh

    def _trade_confidence_metrics(
        self,
        *,
        sc: float,
        diag: dict,
        spread_pips: float,
        expected_move: float,
        pip_value_per_lot: float,
        equity: float,
        lot_fraction: float,
        score_threshold: float | None = None,
        sharpe_threshold: float | None = None,
        heston_ratio: float = 1.0,
    ) -> dict:
        """
        Convert score + gate ratios into a continuous readiness/confidence view.
        Values around 100% imply all major gates are comfortably passing.
        """
        spread_lim = 2.0
        spread = max(float(spread_pips), 1e-9)
        cost = (spread * max(float(pip_value_per_lot), 1e-9) * max(float(lot_fraction), 1e-9)) / max(float(equity), 1e-9)
        cost_req = 3.0 * cost

        score_th = float(self.score_th if score_threshold is None else score_threshold)
        score_ratio = abs(float(sc)) / max(score_th, 1e-9)
        sharpe = float(diag.get("predictive_sharpe", 0.0))
        sharpe_aligned = float(diag.get("predictive_sharpe_aligned", self._directional_sharpe(sc, sharpe)))
        sharpe_th = float(self.min_predictive_sharpe if sharpe_threshold is None else sharpe_threshold)
        if sharpe_th > 0:
            sharpe_ratio = sharpe_aligned / max(sharpe_th, 1e-9)
        else:
            sharpe_ratio = 1.0
        cost_ratio = float(expected_move) / max(cost_req, 1e-12)
        spread_ratio = spread_lim / spread
        regime_strength = abs(2.0 * float(diag.get("p_trend", 0.5)) - 1.0)
        hawkes_ratio = (
            float(diag.get("hawkes_n", 1.0)) / max(float(self.hawkes_n_min), 1e-9)
            if self.use_hawkes and self.use_hawkes_gate
            else 1.0
        )
        heston_ratio_inv = 1.0 / max(float(heston_ratio), 1e-9)

        score_conf = float(np.clip(score_ratio, 0.0, 1.0))
        sharpe_conf = float(np.clip(max(sharpe_ratio, 0.0), 0.0, 1.0))
        cost_conf = float(np.clip(cost_ratio, 0.0, 1.0))
        spread_conf = float(np.clip(spread_ratio, 0.0, 1.0))
        regime_conf = float(np.clip(regime_strength, 0.0, 1.0))
        hawkes_conf = float(np.clip(hawkes_ratio, 0.0, 1.0))
        heston_conf = float(np.clip(heston_ratio_inv, 0.0, 1.0))
        cohesion_conf = float(np.clip(float(diag.get("model_cohesion", 0.5)), 0.0, 1.0))

        cohesion_weight = float(np.clip(self.model_cohesion_conf_weight, 0.0, 0.20))
        base_weight = 1.0 - cohesion_weight

        confidence = 100.0 * (
            base_weight
            * (
                0.35 * score_conf
                + 0.20 * sharpe_conf
                + 0.20 * cost_conf
                + 0.10 * spread_conf
                + 0.10 * regime_conf
                + 0.03 * hawkes_conf
                + 0.02 * heston_conf
            )
            + cohesion_weight * cohesion_conf
        )
        # Execution confidence intentionally excludes score/sharpe terms because those
        # are explicitly checked by execution-quality thresholds.
        confidence_exec_base = 100.0 * (
            base_weight
            * (
                0.45 * cost_conf
                + 0.20 * spread_conf
                + 0.20 * regime_conf
                + 0.10 * hawkes_conf
                + 0.05 * heston_conf
            )
            + cohesion_weight * cohesion_conf
        )

        return {
            "confidence": float(np.clip(confidence, 0.0, 100.0)),
            "confidence_exec_base": float(np.clip(confidence_exec_base, 0.0, 100.0)),
            "score_ratio": float(score_ratio),
            "sharpe_ratio": float(sharpe_ratio),
            "sharpe_threshold": float(sharpe_th),
            "sharpe_aligned": float(sharpe_aligned),
            "cost_ratio": float(cost_ratio),
            "spread_ratio": float(spread_ratio),
            "regime_strength": float(regime_strength),
            "hawkes_ratio": float(hawkes_ratio),
            "heston_ratio": float(heston_ratio),
            "model_cohesion": float(cohesion_conf),
        }

    @staticmethod
    def _clip01(value: float) -> float:
        try:
            return float(np.clip(float(value), 0.0, 1.0))
        except Exception:
            return 0.0

    def _rolling_side_metrics(self) -> dict[str, float]:
        hist = list(getattr(self, "candidate_side_history", []) or [])
        total = int(len(hist))
        if total <= 0:
            return {
                "window": int(self.direction_bias_window),
                "samples": 0,
                "buy_count": 0,
                "sell_count": 0,
                "abstain_count": 0,
                "buy_share": 0.5,
                "sell_share": 0.5,
                "abstain_rate": 0.0,
            }
        buy_n = int(sum(1 for s in hist if str(s).upper() == "BUY"))
        sell_n = int(sum(1 for s in hist if str(s).upper() == "SELL"))
        abstain_n = int(sum(1 for s in hist if str(s).upper() not in {"BUY", "SELL"}))
        directional_n = int(max(buy_n + sell_n, 0))
        if directional_n > 0:
            buy_share = float(buy_n / directional_n)
            sell_share = float(sell_n / directional_n)
        else:
            buy_share = 0.5
            sell_share = 0.5
        return {
            "window": int(self.direction_bias_window),
            "samples": int(total),
            "buy_count": int(buy_n),
            "sell_count": int(sell_n),
            "abstain_count": int(abstain_n),
            "buy_share": float(np.clip(buy_share, 0.0, 1.0)),
            "sell_share": float(np.clip(sell_share, 0.0, 1.0)),
            "abstain_rate": float(np.clip(abstain_n / max(total, 1), 0.0, 1.0)),
        }

    def _direction_bias_trend_justified(self, side: str, p_trend: float, regime_bucket: str) -> bool:
        side_up = str(side).upper()
        if side_up not in {"BUY", "SELL"}:
            return False
        if str(regime_bucket).lower() != "trend":
            return False
        p = float(np.clip(p_trend, 0.0, 1.0))
        if side_up == "BUY":
            return bool(p >= 0.65)
        return bool(p <= 0.35)

    def _rolling_edge_vs_random(self) -> tuple[float, float]:
        samples: list[int] = []
        for rows in (self.direction_history or {}).values():
            if not rows:
                continue
            tail = list(rows[-self.direction_bias_window :])
            for _, hit in tail:
                samples.append(int(hit))
        if not samples:
            return 0.0, 0.0
        hit_rate = float(np.clip(np.mean(samples), 0.0, 1.0))
        hit_delta = float(hit_rate - 0.5)
        expectancy_delta = float((2.0 * hit_rate) - 1.0)
        return hit_delta, expectancy_delta

    def _build_entry_monitor(self, top_candidate: dict | None) -> dict:
        """
        Convert top candidate gate state into a single open-trade proximity metric.
        100% means all required gates for execution are currently passing.
        """
        row = dict(top_candidate or {})
        if not row:
            return {
                "symbol": "",
                "side": "",
                "open_proximity_pct": 0.0,
                "execution_ready": False,
                "blocked_by": "none",
                "components_pct": {},
            }

        confidence_exec = float(row.get("confidence_exec", row.get("confidence", 0.0)))
        score_ratio = float(row.get("score_ratio", 0.0))
        score_ratio_exec = float(row.get("score_ratio_exec", score_ratio))
        sharpe_ratio = float(row.get("sharpe_ratio", 0.0))
        cost_ratio = float(row.get("cost_ratio", 0.0))
        dyn_score_floor = float(
            row.get(
                "exec_min_score_ratio_dynamic",
                self._dynamic_exec_min_score_ratio(
                    confidence_exec=confidence_exec,
                    sharpe_ratio=sharpe_ratio,
                    cost_ratio=cost_ratio,
                    blockers=str(row.get("blocked_by_all", row.get("blocked_by", "none"))),
                ),
            )
        )

        components = {
            "entry_score": self._clip01(score_ratio),
            "exec_confidence": (
                self._clip01(confidence_exec / max(float(self.exec_min_confidence), 1e-9))
                if self.use_execution_quality_gate
                else 1.0
            ),
            "exec_score_ratio": (
                self._clip01(score_ratio_exec / max(float(dyn_score_floor), 1e-9))
                if self.use_execution_quality_gate
                else 1.0
            ),
            "exec_sharpe_ratio": (
                self._clip01(sharpe_ratio / max(float(self.exec_min_sharpe_ratio), 1e-9))
                if self.use_execution_quality_gate
                else 1.0
            ),
            "exec_cost_ratio": (
                self._clip01(cost_ratio)
                if str(self.execution_gate_mode) == "hard"
                else 1.0
            ),
            "fresh_ticks": 1.0 if bool(row.get("fresh_ticks_ok", True)) else 0.0,
        }
        open_ratio = min(float(v) for v in components.values()) if components else 0.0

        return {
            "symbol": str(row.get("symbol", "")),
            "side": str(row.get("side", "")),
            "open_proximity_pct": float(100.0 * self._clip01(open_ratio)),
            "execution_ready": bool(row.get("execution_ready", False)),
            "entry_ready": bool(row.get("entry_ready", False)),
            "exec_quality_ready": bool(row.get("exec_quality_ready", False)),
            "exec_cost_ready": bool(row.get("exec_cost_ready", False)),
            "blocked_by": str(row.get("blocked_by", "none")),
            "blocked_by_all": str(row.get("blocked_by_all", row.get("blocked_by", "none"))),
            "components_pct": {k: float(v * 100.0) for k, v in components.items()},
            "thresholds": {
                "min_confidence": float(self.exec_min_confidence),
                "min_score_ratio_dynamic": float(dyn_score_floor),
                "min_sharpe_ratio": float(self.exec_min_sharpe_ratio),
            },
        }

    def _build_close_position_monitor(
        self,
        *,
        symbol: str,
        side: str,
        score_now: float,
        p_trend: float,
        current_price: float,
        open_price: float,
        open_time: float,
        now_ts: float,
        hold_policy: dict,
        exit_score_threshold_eff: float,
    ) -> dict:
        """
        Build exact per-position close telemetry from the same thresholds used by exit logic.
        """
        sym = str(symbol).upper()
        side_up = str(side).upper()
        st = self.risk_manager.positions.get(sym)
        r_dist = float(max(getattr(st, "r_distance", 0.0) if st else 0.0, 1e-9))
        peak_price = float(getattr(st, "peak_price", current_price) if st else current_price)
        trough_price = float(getattr(st, "trough_price", current_price) if st else current_price)
        elapsed_secs = float(max(float(now_ts) - float(open_time), 0.0))

        min_hold_secs = float(max(hold_policy.get("min_hold_secs", self.risk_manager.min_hold_secs), 0.0))
        hold_progress = 1.0 if min_hold_secs <= 0.0 else self._clip01(elapsed_secs / max(min_hold_secs, 1e-9))

        if side_up == "BUY":
            trailing_stop = float(peak_price - r_dist)
            trailing_distance = float(current_price - trailing_stop)
            hard_stop = float(open_price - r_dist)
            hard_distance = float(current_price - hard_stop)
            pnl_dist = float(current_price - open_price)
            adverse_score = float(-score_now)
        else:
            trailing_stop = float(trough_price + r_dist)
            trailing_distance = float(trailing_stop - current_price)
            hard_stop = float(open_price + r_dist)
            hard_distance = float(hard_stop - current_price)
            pnl_dist = float(open_price - current_price)
            adverse_score = float(score_now)

        trailing_ratio = self._clip01(1.0 - (trailing_distance / max(r_dist, 1e-9))) * hold_progress
        hard_ratio = self._clip01(1.0 - (hard_distance / max(r_dist, 1e-9))) * hold_progress

        regime_exit_th = float(max(hold_policy.get("regime_exit_th", self.risk_manager.regime_exit_th), 0.0))
        if regime_exit_th > 0.0:
            regime_ratio_raw = self._clip01(1.0 - max(float(p_trend) - regime_exit_th, 0.0) / max(regime_exit_th, 1e-9))
        else:
            regime_ratio_raw = 0.0
        regime_ratio = regime_ratio_raw * hold_progress

        time_limit_secs = float(max(hold_policy.get("time_limit_hours", self.risk_manager.time_limit_hours), 0.1) * 3600.0)
        stagnation_secs = float(max(hold_policy.get("stagnation_minutes", self.risk_manager.stagnation_minutes), 0.1) * 60.0)
        time_need_secs = float(max(time_limit_secs, stagnation_secs))
        time_ratio = self._clip01(elapsed_secs / max(time_need_secs, 1e-9))
        pnl_flat_limit = float(max(0.25 * r_dist, 1e-9))
        flat_ratio = self._clip01(1.0 - (abs(float(pnl_dist)) / pnl_flat_limit))
        time_stag_ratio = min(time_ratio, flat_ratio) * hold_progress

        reversal_th = float(max(exit_score_threshold_eff, 1e-9))
        reversal_ratio = self._clip01(float(adverse_score) / reversal_th)

        components = {
            "reversal_exit": float(reversal_ratio),
            "risk_trailing_stop": float(trailing_ratio),
            "risk_hard_stop": float(hard_ratio),
            "risk_regime_exit": float(regime_ratio),
            "risk_time_stagnation": float(time_stag_ratio),
        }
        dominant_reason = "none"
        close_ratio = 0.0
        if components:
            dominant_reason = max(components, key=components.get)
            close_ratio = float(max(components.values()))

        return {
            "symbol": sym,
            "side": side_up,
            "close_proximity_pct": float(100.0 * self._clip01(close_ratio)),
            "dominant_close_reason": str(dominant_reason if close_ratio > 0.0 else "none"),
            "hold_secs": float(elapsed_secs),
            "hold_progress_pct": float(100.0 * hold_progress),
            "current_price": float(current_price),
            "open_price": float(open_price),
            "components_pct": {k: float(v * 100.0) for k, v in components.items()},
            "thresholds": {
                "reversal_score_threshold": float(reversal_th),
                "regime_exit_th": float(regime_exit_th),
                "r_distance": float(r_dist),
                "trailing_stop_price": float(trailing_stop),
                "hard_stop_price": float(hard_stop),
                "time_limit_secs": float(time_limit_secs),
                "stagnation_secs": float(stagnation_secs),
                "min_hold_secs": float(min_hold_secs),
            },
            "signals": {
                "score_now": float(score_now),
                "p_trend": float(p_trend),
                "pnl_dist": float(pnl_dist),
            },
            "last_action": "hold",
        }

    def _find_symbol_close(self, pair: str, market_data: dict[str, pd.DataFrame] | None) -> float | None:
        if market_data is None:
            return None
        pair_up = pair.upper()
        for sym, df in market_data.items():
            sym_up = sym.upper()
            if pair_up in sym_up and df is not None and not df.empty:
                try:
                    return float(df["close"].iloc[-1])
                except Exception:
                    continue
        return None

    def _quote_to_usd_rate(self, quote_ccy: str, market_data: dict[str, pd.DataFrame] | None) -> float:
        q = quote_ccy.upper()
        if q == "USD":
            return 1.0
        direct = self._find_symbol_close(f"{q}USD", market_data)
        if direct is not None and direct > 0:
            return direct
        inverse = self._find_symbol_close(f"USD{q}", market_data)
        if inverse is not None and inverse > 0:
            return 1.0 / inverse
        if q not in self._missing_fx_conv_warned:
            logger.warning(f"Missing FX conversion path {q}->USD; using 1.0 fallback for notional estimate")
            self._missing_fx_conv_warned.add(q)
        return 1.0

    def _estimate_notional_per_lot(
        self, symbol: str, price: float, market_data: dict[str, pd.DataFrame] | None = None
    ) -> float:
        s = symbol.upper()
        if len(s) >= 6:
            base = s[:3]
            quote = s[3:6]
            if base == "USD":
                return 100000.0
            if quote == "USD":
                return 100000.0 * max(price, 1e-9)
            quote_to_usd = self._quote_to_usd_rate(quote, market_data)
            return 100000.0 * max(price, 1e-9) * max(quote_to_usd, 1e-9)
        return 100000.0 * max(price, 1e-9)

    def _pip_size(self, symbol: str) -> float:
        s = symbol.upper()
        if len(s) >= 6 and s[3:6] == "JPY":
            return 0.01
        return 0.0001

    def _pip_value_per_standard_lot(
        self, symbol: str, price: float, market_data: dict[str, pd.DataFrame] | None = None
    ) -> float:
        s = symbol.upper()
        if len(s) < 6:
            return self.pip_value_standard_lot

        quote = s[3:6]
        pip_quote = 100000.0 * self._pip_size(symbol)

        # Most deployments are USD account; keep account-currency conversion robust.
        if self.account_ccy == quote:
            return max(pip_quote, 1e-9)
        if self.account_ccy == "USD":
            return max(pip_quote * self._quote_to_usd_rate(quote, market_data), 1e-9)

        quote_to_usd = self._quote_to_usd_rate(quote, market_data)
        acct_to_usd = self._quote_to_usd_rate(self.account_ccy, market_data)
        return max(pip_quote * quote_to_usd / max(acct_to_usd, 1e-9), 1e-9)

    def _lot_floor_for_margin_level(
        self, equity: float, symbol: str, price: float, market_data: dict[str, pd.DataFrame] | None = None
    ) -> float:
        """
        Minimum lot needed so a single trade uses enough margin to keep margin level
        at or below configured threshold (e.g., <= 10000%).
        """
        target_ml = float(self.max_margin_level_per_trade_pct)
        if target_ml <= 0:
            return 0.0
        notional_per_lot = self._estimate_notional_per_lot(symbol, price, market_data)
        margin_per_lot = notional_per_lot / max(float(self.leverage), 1e-9)
        if margin_per_lot <= 0:
            return 0.0
        margin_needed = max(float(equity), 1e-9) * 100.0 / max(target_ml, 1e-9)
        return max(margin_needed / margin_per_lot, 0.0)

    def _build_hawkes_events(self, df: pd.DataFrame) -> pd.DataFrame:
        close = df["close"]
        if len(close) < 20:
            return pd.DataFrame(columns=["time", "side"])

        ret = close.diff()
        ret_abs = ret.abs().dropna()
        if ret_abs.empty:
            return pd.DataFrame(columns=["time", "side"])
        # Ignore near-zero bars; do not forward-fill side through flat periods.
        eps = max(1e-12, float(np.nanmedian(ret_abs.values)) * 1e-6)
        side = np.sign(ret)
        side = side[ret.abs() > eps]
        side = side[side != 0]
        if side.empty:
            return pd.DataFrame(columns=["time", "side"])

        if isinstance(side.index, pd.DatetimeIndex):
            tvals = side.index.view("int64") / 1e9
        else:
            tvals = np.arange(len(side), dtype=float)

        events = pd.DataFrame({"time": tvals, "side": side.values})
        return events.tail(500).reset_index(drop=True)

    
    # ---------- Strategy Refinement Helpers ----------
    def _session_adjustment(self, symbol: str, utc_hour: int) -> float:
        """Return liquidity multiplier [0.6, 1.0] based on FX session."""
        # London/NY overlap (13-17 UTC) = peak liquidity
        # London (7-12 UTC) / NY (18-22 UTC) = good liquidity
        # Asian (23-07 UTC) = depends on pair
        
        if 13 <= utc_hour <= 17:
            return 1.0  # Peak liquidity
        elif 7 <= utc_hour <= 12 or 18 <= utc_hour <= 22:
            return 0.85  # Good liquidity
        else:
            # Asian session - depends on pair
            if any(x in symbol for x in ["JPY", "AUD", "NZD"]):
                return 0.9  # Active pairs in Asian session
            return 0.6  # Low liquidity for EUR/GBP etc.
    
    def _adaptive_beta_weights(self, p_trend: float) -> tuple[float, float]:
        """Return adjusted (beta_p, beta_m) based on regime probability."""
        beta_p = self.cfg.get("beta_p", 1.0)
        beta_m = self.cfg.get("beta_m", 0.3)
        
        if not self.use_adaptive_beta:
            return beta_p, beta_m
        
        # Strong trend regime (p_trend > 0.7) -> boost momentum, dampen micro
        # Range regime (p_trend < 0.3) -> dampen momentum, boost micro
        # M2 FIX: clamp inverse multiplier to >= 0 so beta can never flip sign
        if p_trend > 0.7:
            beta_p_adj = beta_p * self.beta_trend_boost
            beta_m_adj = beta_m * max(0.0, 2.0 - self.beta_trend_boost)
        elif p_trend < 0.3:
            beta_p_adj = beta_p * max(0.0, 2.0 - self.beta_range_boost)
            beta_m_adj = beta_m * self.beta_range_boost
        else:
            beta_p_adj, beta_m_adj = beta_p, beta_m
        
        return beta_p_adj, beta_m_adj
    
    def _normalize_ofi_drift(self, hawkes_drift: float) -> float:
        """Z-score normalize OFI drift using rolling history."""
        # Update history
        self.ofi_history.append(hawkes_drift)
        if len(self.ofi_history) > self.ofi_history_max:
            self.ofi_history.pop(0)
        
        # Use z-score if enough history
        if len(self.ofi_history) >= 20:
            mean = np.mean(self.ofi_history)
            std = np.std(self.ofi_history) + 1e-6
            drift_z = (hawkes_drift - mean) / std
            # Clip to [-3, 3] and normalize to [-1, 1]
            return np.clip(drift_z, -3, 3) / 3.0
        else:
            # Fallback to simple sign-based normalization
            return hawkes_drift / (abs(hawkes_drift) + 0.01)
    
    # ---------- Universe (minis only) ----------
    def build_universe(self, all_symbols: list[str]) -> list[str]:
        """
        Keep only minis: names that end with or contain a configured mini suffix.
        Also require that the name contains one of the FX roots (EURUSD, etc.).
        
        For IG: no suffixes (empty mini_suffixes list), so we just filter by roots.
        All IG FX symbols are minis when traded at 0.10 lot size.
        """
        minis: list[str] = []
        
        # IG mode: no suffixes, all root symbols are valid (traded as minis via lot size)
        if not self.mini_suffixes:
            for s in all_symbols:
                s_up = s.upper()
                # Match any symbol that contains one of our roots
                if any(r in s_up for r in self.roots):
                    minis.append(s)
        else:
            # Generic mode: filter by suffixes
            lowers = [s.lower() for s in self.mini_suffixes]
            for s in all_symbols:
                s_up = s.upper()
                if not any(r in s_up for r in self.roots):  # must be a major root
                    continue
                s_low = s.lower()
                if any(s_low.endswith(suf.lower()) or (suf.lower() in s_low) for suf in lowers):
                    minis.append(s)
        
        return sorted(list(dict.fromkeys(minis)))  # unique, stable order

    # ---------- Core scoring ----------
    def _calculate_history_scores(self, r: pd.Series, pz: pd.Series, window: int = 30) -> str:
        """Calculate last N scores for HUD history using regime_tilt proxy (fast)."""
        if len(r) < window: return ""
        
        # Slice last N
        r_sub = r.iloc[-window:]
        pz_sub = pz.iloc[-window:]
        
        # Fast regime proxy for history
        tilt = regime_tilt(r_sub, w=min(96, len(r_sub)))
        p_trend = (tilt + 1.0) / 2.0
        
        scores = []
        beta_p_base = self.cfg.get("beta_p", 1.0)
        
        for i in range(len(r_sub)):
            pt = float(p_trend.iloc[i])
            pz_val = float(pz_sub.iloc[i])
            
            # Simplified adaptive beta (inline)
            bp = beta_p_base
            if pt > 0.7: bp *= self.beta_trend_boost
            elif pt < 0.3: bp *= (2.0 - self.beta_range_boost)
            
            sc = bp * pz_val * (2 * pt - 1)
            scores.append(f"{sc:.3f}")
            
        return ",".join(scores)

    def score_symbol(self, df: pd.DataFrame, symbol: str) -> tuple[float, dict]:
        """
        Full EL-Hawkes-Regime score with predictive mixture.
        """
        close = df["close"]
        r = np.log(close).diff()
        current_close = float(close.iloc[-1])
        # Evaluate directional hit-rate on the last completed bar, not the live partial bar.
        direction_eval_close = float(close.iloc[-2]) if len(close) >= 2 else current_close
        bar_key = self._bar_key(df)
        
        if len(close) < max(252, self.el_window + 5):
            return 0.0, {"error": "insufficient_bars"}
        
        pz_series = el_pz(close, self.el_window, self.el_ema_span)
        
        sym_key = symbol.upper()
        self._update_direction_history(sym_key, direction_eval_close, bar_key)
        if self.use_ai_indicator_model:
            self._update_ai_indicator_history(sym_key, direction_eval_close, bar_key)
        if sym_key not in self.regime_models:
            self.regime_models[sym_key] = MarkovSwitchingModel(n_states=2)
            self.regime_fitted[sym_key] = False
        regime_model = self.regime_models[sym_key]

        now_ts = time.time()
        if self.use_regime_filter:
            last_fit_ts = float(self.regime_last_fit_ts.get(sym_key, 0.0))
            last_fit_n = int(self.regime_last_fit_n.get(sym_key, 0))
            should_refit = (
                (not self.regime_fitted.get(sym_key, False))
                or (len(r) - last_fit_n >= self.regime_refit_bars)
                or (now_ts - last_fit_ts >= self.regime_refit_secs)
            )
            if should_refit and len(r) >= 252:
                try:
                    regime_model.fit(r, pz_series)
                    self.regime_fitted[sym_key] = True
                    self.regime_last_fit_ts[sym_key] = now_ts
                    self.regime_last_fit_n[sym_key] = len(r)
                    logger.info(f"Regime model fitted/refreshed for {symbol}")
                except Exception as e:
                    logger.warning(f"Regime fitting failed for {symbol}: {e}")
        
        pz_val = float(pz_series.iloc[-1])
        pz_blend, pz_fast, pz_slow = self._ensemble_pz(close, pz_val)
        
        if self.use_regime_filter and self.regime_fitted.get(sym_key, False):
            p_trend = regime_model.get_trend_probability()
        else:
            # Fallback to simple tilt
            tilt_series = regime_tilt(r)
            tilt_val = float(tilt_series.iloc[-1])
            p_trend = (tilt_val + 1.0) / 2.0  # Map [-1, 1] to [0, 1]
        
        # Get adaptive beta weights based on regime
        beta_p, beta_m = self._adaptive_beta_weights(p_trend)
        trend_tilt = (2 * p_trend - 1)
        momentum_component = beta_p * pz_blend * trend_tilt
        micro_component = 0.0
        micro_component_raw = 0.0
        micro_coop_mult = 1.0
        score = momentum_component
        
        hawkes_drift = 0.0
        hawkes_n = 1.0
        drift_std = 0.0
        if self.use_hawkes:
            hawkes_signal = None
            try:
                events = self._build_hawkes_events(df)
                if len(events) >= 50:
                    model = self.hawkes_models.get(sym_key)
                    if model is None:
                        model = BivariateHawkes()
                        self.hawkes_models[sym_key] = model
                    last_fit_ts = float(self.hawkes_last_fit_ts.get(sym_key, 0.0))
                    last_fit_n = int(self.hawkes_last_fit_n.get(sym_key, 0))
                    should_fit = (
                        (not model.fitted)
                        or (len(events) - last_fit_n >= self.hawkes_refit_bars)
                        or (now_ts - last_fit_ts >= self.hawkes_refit_secs)
                    )
                    if should_fit:
                        model.fit(events, max_iter=50)
                        self.hawkes_last_fit_ts[sym_key] = now_ts
                        self.hawkes_last_fit_n[sym_key] = len(events)
                    if model.fitted:
                        hawkes_signal = model.get_signal(events, float(events["time"].iloc[-1]) + 1e-6)
            except Exception as e:
                logger.warning(f"Hawkes fitting/signaling failed for {symbol}: {e}")

            # Fallback to OFI proxy if Hawkes is unavailable.
            if hawkes_signal is None:
                if 'volume' in df.columns:
                    hawkes_signal = self.ofi_proxy.get_signal(df.tail(100))
                else:
                    hawkes_signal = HawkesSignal(0.0, 1.0, 0.0, 0.0)
            
            hawkes_drift = hawkes_signal.drift
            hawkes_n = hawkes_signal.branching
            
            # Add micro drift to score with improved normalization
            drift_std = self._normalize_ofi_drift(hawkes_drift)

            micro_component_raw = beta_m * drift_std
            micro_component = micro_component_raw
            if self.use_model_cohesion and abs(micro_component_raw) > 1e-12 and abs(momentum_component) > 1e-12:
                trend_strength = abs(float(trend_tilt))
                mom_sign = float(np.sign(momentum_component))
                micro_sign = float(np.sign(micro_component_raw))
                if mom_sign == micro_sign:
                    # In clean trends, aligned micro flow should help momentum enter sooner.
                    micro_coop_mult = 1.0 + (float(self.micro_align_mult) - 1.0) * trend_strength
                else:
                    # When models disagree, dampen micro more in strong trends, less in range.
                    conflict_mult = float(self.micro_conflict_mult_range) - (
                        (float(self.micro_conflict_mult_range) - float(self.micro_conflict_mult_trend))
                        * trend_strength
                    )
                    micro_coop_mult = float(np.clip(conflict_mult, 0.0, 1.2))
                micro_component = micro_component_raw * micro_coop_mult
            score += micro_component
            
        lppls_hazard = 0.0
        lppls_factor = 1.0
        if self.use_lppls:
            cached = self._lppls_cache.get(sym_key)
            if cached is not None and (now_ts - cached[0]) < self.lppls_refresh_secs:
                lppls_hazard = cached[1]
            else:
                lppls_series = close
                if isinstance(close.index, pd.DatetimeIndex):
                    daily = close.resample("1D").last().dropna()
                    if len(daily) >= self.lppls_min_points:
                        lppls_series = daily
                elif len(close) >= (self.lppls_min_points * 24):
                    lppls_series = close.iloc[::24].dropna()
                lppls_hazard = self.lppls.get_hazard(lppls_series)
                self._lppls_cache[sym_key] = (now_ts, lppls_hazard)
            # CRITICAL FIX: LPPLS should only dampen BUYS.
            # If Sell (-score) and Hazard high, do not dampen.
            if lppls_hazard > self.lppls_threshold and score > 0:
                lppls_factor = (1.0 - lppls_hazard)
                score *= lppls_factor
            # Optional: Amplify Shorts if Hazard High?
            # if lppls_hazard > 0.8 and score < 0: score *= 1.5
        
        # Apply session awareness adjustment
        session_mult = 1.0
        if self.use_session_filter:
            import datetime
            utc_hour = datetime.datetime.now(datetime.timezone.utc).hour
            session_mult = self._session_adjustment(symbol, utc_hour)
            score *= session_mult

        direction_quality = self._direction_quality_snapshot(sym_key)
        direction_side_pre = 1 if score >= 0 else -1
        direction_factor = self._direction_calibration_factor(sym_key, direction_side_pre, direction_quality)
        score *= direction_factor

        ai_raw = 0.0
        ai_signal_base = 0.0
        ai_signal = 0.0
        ai_confidence = 0.0
        ai_factor = 1.0
        ai_component = 0.0
        ai_align_mult = 1.0
        ai_alignment_score = 0.0
        ai_alignment_trend = 0.0
        ai_side = 0
        ai_quality = {
            "samples": 0,
            "buy_samples": 0,
            "sell_samples": 0,
            "buy_hit_rate": 0.5,
            "sell_hit_rate": 0.5,
            "overall_hit_rate": 0.5,
        }
        if self.use_ai_indicator_model:
            ai_diag = self._ai_indicator_signal(close, p_trend)
            ai_raw = float(ai_diag.get("raw", 0.0))
            ai_signal_base = float(ai_diag.get("signal", 0.0))
            ai_confidence = float(ai_diag.get("confidence", 0.0))
            ai_side_pre = int(ai_diag.get("side", 0))
            ai_quality = self._ai_indicator_quality_snapshot(sym_key)
            if ai_side_pre in (-1, 1):
                ai_factor = self._ai_indicator_calibration_factor(sym_key, ai_side_pre, ai_quality)
            ai_signal = float(np.clip(ai_signal_base * ai_factor, -1.0, 1.0))
            if ai_signal >= float(self.ai_min_signal):
                ai_side = 1
            elif ai_signal <= -float(self.ai_min_signal):
                ai_side = -1
            if abs(ai_signal) > 1e-12:
                ai_alignment_score = float(np.sign(score) * np.sign(ai_signal))
                ai_alignment_trend = float(np.sign(trend_tilt) * np.sign(ai_signal))
                if ai_alignment_score < 0.0:
                    ai_align_mult = 0.0
                if abs(float(trend_tilt)) >= 0.35 and ai_alignment_trend < 0.0:
                    ai_align_mult = 0.0
                if abs(float(trend_tilt)) < 0.20:
                    ai_align_mult *= 0.35
                elif abs(float(trend_tilt)) < 0.35:
                    ai_align_mult *= 0.65
            if ai_confidence >= float(self.ai_confidence_floor):
                ai_component = (
                    float(self.ai_score_weight)
                    * ai_signal
                    * (0.40 + 0.60 * ai_confidence)
                    * float(np.clip(ai_align_mult, 0.10, 1.0))
                )
                score += ai_component
            self._register_ai_indicator_forecast(
                sym_key,
                direction_eval_close,
                ai_side,
                bar_key,
                confidence=ai_confidence,
                signal_abs=abs(ai_signal),
            )

        horizon_diag = self._estimate_holding_horizon(
            close,
            score,
            p_trend,
            ai_signal=ai_signal,
        )

        direction_side_post = 1 if score >= 0 else -1
        direction_side_samples = int(
            direction_quality.get("buy_samples", 0) if direction_side_post > 0 else direction_quality.get("sell_samples", 0)
        )
        ai_side_samples = int(
            ai_quality.get("buy_samples", 0) if ai_side > 0 else ai_quality.get("sell_samples", 0)
        )
        self._register_direction_forecast(
            sym_key,
            direction_eval_close,
            direction_side_post,
            bar_key,
            score_abs=abs(score),
        )
        self._record_score_magnitude(sym_key, score, bar_key=bar_key)
        
        # Calculate simple historical Sharpe (per-bar, not annualized) as fallback.
        simple_rolling_sharpe = 0.0
        if len(r) >= 24:
            window = min(len(r), 120)
            rolling_mean = r.rolling(window).mean().iloc[-1]
            rolling_std = r.rolling(window).std(ddof=0).iloc[-1]
            if rolling_std > 1e-9:
                simple_rolling_sharpe = (rolling_mean / rolling_std)

        predictive_sharpe = simple_rolling_sharpe
        predictive_sharpe_source = "rolling"
        if self.use_regime_filter and self.regime_fitted.get(sym_key, False):
            try:
                mixture = regime_model.get_predictive_mixture(pz_val)
                model_sharpe = mixture.sharpe
                if np.isfinite(model_sharpe):
                    # Prefer regime-model sharpe directly when available; it is state-aware.
                    predictive_sharpe = float(model_sharpe)
                    predictive_sharpe_source = "regime_model"
            except Exception as exc:
                logger.debug(f"{symbol}: predictive mixture unavailable, using rolling sharpe ({exc})")
        predictive_sharpe = float(np.clip(predictive_sharpe, -5.0, 5.0))
        sharpe_vote = float(np.sign(self._directional_sharpe(score, predictive_sharpe)))
        model_votes: list[float] = []
        if abs(float(momentum_component)) > 1e-12:
            model_votes.append(float(np.sign(momentum_component)))
        if abs(float(micro_component)) > 1e-12:
            model_votes.append(float(np.sign(micro_component)))
        if abs(float(ai_component)) > 1e-12:
            model_votes.append(float(np.sign(ai_component)))
        if sharpe_vote != 0.0:
            model_votes.append(sharpe_vote)
        if model_votes:
            # 1.0 => models reinforce each other, 0.5 => mixed, 0.0 => maximal conflict.
            model_cohesion = float(np.clip(0.5 + 0.5 * abs(sum(model_votes)) / len(model_votes), 0.0, 1.0))
        else:
            model_cohesion = 0.5
        
        # Calculate history string (last 30)
        hist_str = self._calculate_history_scores(r, pz_series, 30)

        diagnostics = {
            "pz": pz_val,
            "pz_blend": float(pz_blend),
            "pz_fast": float(pz_fast),
            "pz_slow": float(pz_slow),
            "p_trend": p_trend,
            "score": score,
            "vol": float(r.rolling(96).std(ddof=0).iloc[-1]) if len(r) > 96 else 0.0,
            "hawkes_drift": hawkes_drift,
            "hawkes_n": hawkes_n,
            "lppls_hazard": lppls_hazard,
            "predictive_sharpe": predictive_sharpe,
            "predictive_sharpe_simple": float(simple_rolling_sharpe),
            "predictive_sharpe_source": predictive_sharpe_source,
            "history": hist_str,
            "beta_p": beta_p,
            "beta_m": beta_m,
            "momentum_component": float(momentum_component),
            "micro_component_raw": float(micro_component_raw),
            "micro_component": float(micro_component),
            "ai_enabled": bool(self.use_ai_indicator_model),
            "ai_raw": float(ai_raw),
            "ai_signal_base": float(ai_signal_base),
            "ai_signal": float(ai_signal),
            "ai_confidence": float(ai_confidence),
            "ai_factor": float(ai_factor),
            "ai_align_mult": float(ai_align_mult),
            "ai_alignment_score": float(ai_alignment_score),
            "ai_alignment_trend": float(ai_alignment_trend),
            "ai_component": float(ai_component),
            "ai_side": "BUY" if ai_side > 0 else ("SELL" if ai_side < 0 else "NONE"),
            "ai_hit_rate": float(ai_quality.get("overall_hit_rate", 0.5)),
            "ai_buy_hit_rate": float(ai_quality.get("buy_hit_rate", 0.5)),
            "ai_sell_hit_rate": float(ai_quality.get("sell_hit_rate", 0.5)),
            "ai_buy_samples": int(ai_quality.get("buy_samples", 0)),
            "ai_sell_samples": int(ai_quality.get("sell_samples", 0)),
            "ai_samples": int(ai_quality.get("samples", 0)),
            "ai_side_samples": int(ai_side_samples),
            "micro_coop_mult": float(micro_coop_mult),
            "raw_signal_core": float(momentum_component + micro_component),
            "raw_signal": float(momentum_component + micro_component + ai_component),
            "model_cohesion": float(model_cohesion),
            "lppls_factor": float(lppls_factor),
            "session_mult": float(session_mult),
            "trend_tilt": float(trend_tilt),
            "drift_norm": float(drift_std),
            "direction_factor": float(direction_factor),
            "direction_side": "BUY" if direction_side_post > 0 else "SELL",
            "direction_hit_rate": float(direction_quality.get("overall_hit_rate", 0.5)),
            "direction_buy_hit_rate": float(direction_quality.get("buy_hit_rate", 0.5)),
            "direction_sell_hit_rate": float(direction_quality.get("sell_hit_rate", 0.5)),
            "direction_buy_samples": int(direction_quality.get("buy_samples", 0)),
            "direction_sell_samples": int(direction_quality.get("sell_samples", 0)),
            "direction_samples": int(direction_quality.get("samples", 0)),
            "direction_side_samples": int(direction_side_samples),
            "primary_horizon_hours": float(horizon_diag.get("primary_horizon_hours", self.hold_default_horizon_hours)),
            "horizon_confidence": float(horizon_diag.get("horizon_confidence", 0.0)),
            "horizon_strength": float(horizon_diag.get("horizon_strength", 0.0)),
            "horizon_side": str(horizon_diag.get("horizon_side", "NONE")),
            "horizon_scores": dict(horizon_diag.get("horizon_scores", {})),
            "model_state_degraded": bool(sym_key in self.model_state_degraded_symbols),
        }
        diagnostics["predictive_sharpe_aligned"] = float(
            self._directional_sharpe(score, predictive_sharpe)
        )
        diagnostics["signal_model_alignment"] = float(
            np.sign(score) * np.sign(predictive_sharpe)
        )
        
        self.last_diagnostics = diagnostics
        return score, diagnostics



    # ---------- Decide and act ----------
    def decisions(self, md: dict[str, pd.DataFrame], held_symbols: set | None = None) -> list[Decision]:
        """
        Score all symbols. 
        CRITICAL CHANGE: Only send HUD updates for HELD symbols to avoid spam/flicker.
        """
        held_symbols = held_symbols or set()
        held_symbols_up = {str(s).upper() for s in held_symbols}
        raw: list[Decision] = []
        equity_for_gate = float(getattr(self, "equity", 10000.0))
        
        # Track best candidate to show on HUD if no position is held
        best_candidate_diag = None
        best_candidate_score = 0.0
        best_candidate_sym = ""
        best_candidate_rank = (-1, -1.0, -1.0)
        candidate_rows: list[dict] = []
        score_symbol_ms_total = 0.0
        score_symbol_calls = 0
        
        for sym, df in md.items():
            if df is None or df.empty or len(df) < max(252, self.el_window + 5):
                self._log_rejection("insufficient_bars")
                continue

            # Strict freshness gate in live mode.
            fresh_ticks_ok = True
            tick_age = None
            if self.require_fresh_ticks:
                if "last_tick_ts" not in df.attrs:
                    self._log_rejection("stale_tick_missing")
                    self.stale_tick_rejections += 1
                    continue
                try:
                    tick_age = time.time() - float(df.attrs.get("last_tick_ts", 0.0))
                    if tick_age > self.tick_stale_secs:
                        self._log_rejection("stale_tick")
                        self.stale_tick_rejections += 1
                        fresh_ticks_ok = False
                        continue
                except Exception as exc:
                    logger.debug(f"{sym}: failed tick staleness check ({exc})")
                    self._log_rejection("stale_tick_parse")
                    self.stale_tick_rejections += 1
                    continue
            
            score_t0 = time.perf_counter()
            sc, diag = self.score_symbol(df, sym)
            score_symbol_ms_total += float((time.perf_counter() - score_t0) * 1000.0)
            score_symbol_calls += 1
            sym_key = str(sym).upper()
            diag["fresh_ticks_ok"] = bool(fresh_ticks_ok)
            diag["tick_age_secs"] = float(tick_age if tick_age is not None else -1.0)
            diag["gap_recovered"] = bool(df.attrs.get("gap_recovered", False))
            diag["gap_recovery_source"] = str(df.attrs.get("gap_recovery_source", "none"))
            diag["gap_fill_truncated"] = bool(df.attrs.get("gap_fill_truncated", False))
            diag["gap_hours_original"] = int(df.attrs.get("gap_hours_original", 0))
            diag["bar_integrity_ok"] = bool(df.attrs.get("bar_integrity_ok", True))
            warmup_strategy_now = str(df.attrs.get("warmup_strategy", self.startup_warmup_strategy))
            startup_backfill_pending_now = bool(df.attrs.get("startup_backfill_pending", False))
            startup_backfill_ready_now = bool(df.attrs.get("startup_backfill_ready", False))
            startup_backfill_bars_now = int(max(0, df.attrs.get("startup_backfill_bars", 0)))
            startup_backfill_retry_age_now = float(max(0.0, df.attrs.get("startup_backfill_retry_age_secs", 0.0)))
            replay_state_now = dict(self.startup_backward_replay_state.get(sym_key, {}) or {})
            startup_backward_replay_done_now = bool(replay_state_now.get("replay_done", False)) or bool(
                df.attrs.get("startup_backward_replay_done", False)
            )
            diag["warmup_strategy"] = str(warmup_strategy_now)
            diag["startup_backfill_pending"] = bool(startup_backfill_pending_now)
            diag["startup_backfill_ready"] = bool(startup_backfill_ready_now)
            diag["startup_backfill_bars"] = int(startup_backfill_bars_now)
            diag["startup_backfill_retry_age_secs"] = float(startup_backfill_retry_age_now)
            diag["startup_backward_replay_done"] = bool(startup_backward_replay_done_now)
            try:
                self.gap_recovery_events = max(
                    int(self.gap_recovery_events), int(df.attrs.get("gap_recovery_events", 0))
                )
            except Exception:
                pass
            
            # IS THIS SYMBOL HELD?
            if sym.upper() in held_symbols_up:
                # SEND HUD FOR HELD SYMBOL (Priority)
                self._send_hud(sym, sc, diag, df)
                self.last_diagnostics = diag # Keep active logic happy
            
            # Gate metrics for readiness/confidence view.
            spread_val_pips = float(df.attrs.get("spread", 0.0))
            spread_used = spread_val_pips if spread_val_pips > 0 else self.avg_spread_pips
            spread_ok = spread_val_pips <= 2.0

            expected_move = self._expected_move_fraction(abs(sc), float(diag.get("vol", 0.0)))
            current_price = float(df["close"].iloc[-1])
            pip_value_symbol = self._pip_value_per_standard_lot(sym, current_price, md)
            lot_fraction_gate = self._effective_min_trade_lot(
                equity=equity_for_gate,
                pip_value_symbol=pip_value_symbol,
            )
            if self.entry_gate_mode == "hard":
                # Hard mode should not clear cost gates purely because lot size is tiny.
                lot_fraction_gate = max(lot_fraction_gate, self.ig_mini_lot, 0.5)
            cost_ok_raw = cost_gate(
                expected_move=expected_move,
                spread_pips=spread_used,
                pip_value_per_lot=pip_value_symbol,
                equity=equity_for_gate,
                lot_fraction=lot_fraction_gate,
            )
            is_grace_period = False
            if sym in self.recent_reversals and (time.time() - self.recent_reversals[sym] < 60):
                is_grace_period = True
            cost_ok = bool(cost_ok_raw or is_grace_period)

            heston_ratio = 1.0
            heston_scale = 1.0
            heston_ok = True
            if self.heston is not None:
                symbol_root = self._extract_root(sym)
                vol_guard = self.heston.get_vol_guard(symbol_root)
                if vol_guard is not None:
                    current_vol = float(diag.get("vol", 0.0))
                    h1_guard = vol_guard / math.sqrt(252.0 * 24.0)  # annualized -> H1 stdev
                    heston_ratio = current_vol / h1_guard if h1_guard > 0 else 1.0

                    if self.use_graduated_heston:
                        if heston_ratio > 1.0:
                            heston_scale = float(np.clip(1.0 - 0.5 * (heston_ratio - 1.0), 0.3, 1.0))
                            sc *= heston_scale
                    elif heston_ratio > 1.5:
                        heston_ok = False
                    diag["heston_vol_ratio"] = float(heston_ratio)

            diag["heston_scale"] = float(heston_scale)
            diag["score"] = float(sc)

            hawkes_n_val = float(diag.get("hawkes_n", 1.0))
            if not np.isfinite(hawkes_n_val):
                hawkes_n_val = 1.0
            diag["hawkes_n"] = hawkes_n_val

            aligned_sharpe = self._directional_sharpe(sc, float(diag.get("predictive_sharpe", 0.0)))
            diag["predictive_sharpe_aligned"] = float(aligned_sharpe)
            diag["signal_model_alignment"] = float(
                np.sign(sc) * np.sign(float(diag.get("predictive_sharpe", 0.0)))
            )
            p_trend_val = float(diag.get("p_trend", 0.5))
            raw_signal_now = float(diag.get("raw_signal", 0.0))
            score_eps = float(max(self.score_zero_epsilon, 1e-12))
            if sc > score_eps:
                trade_side = "BUY"
            elif sc < -score_eps:
                trade_side = "SELL"
            elif raw_signal_now > score_eps:
                trade_side = "BUY"
            elif raw_signal_now < -score_eps:
                trade_side = "SELL"
            else:
                trade_side = "BUY"
            direction_quality = {
                "samples": int(diag.get("direction_samples", 0)),
                "buy_samples": int(diag.get("direction_buy_samples", 0)),
                "sell_samples": int(diag.get("direction_sell_samples", 0)),
                "buy_hit_rate": float(diag.get("direction_buy_hit_rate", 0.5)),
                "sell_hit_rate": float(diag.get("direction_sell_hit_rate", 0.5)),
            }
            th_parts = self._regime_threshold_components(
                p_trend_val,
                side=trade_side,
                direction_quality=direction_quality,
            )
            regime_bucket = str(th_parts["bucket"])
            score_threshold_raw = float(th_parts["threshold"])
            score_threshold_regime, score_threshold_ref_q, score_threshold_ref_n = self._adaptive_score_threshold(
                sym_key, score_threshold_raw, exclude_latest=True
            )
            fallback_path = "none"
            if self.neutral_micro_fallback_enabled:
                p_lo = float(self.neutral_micro_fallback_ptrend_low)
                p_hi = float(self.neutral_micro_fallback_ptrend_high)
                mom_abs = abs(float(diag.get("momentum_component", 0.0)))
                micro_ai_abs = abs(
                    float(diag.get("micro_component", 0.0)) + float(diag.get("ai_component", 0.0))
                )
                if (
                    p_lo <= p_trend_val <= p_hi
                    and mom_abs <= float(self.neutral_micro_fallback_momentum_eps)
                    and micro_ai_abs > 0.0
                ):
                    fallback_path = "neutral_micro_ai"
                    cap_th = float(self.score_th) * float(self.neutral_micro_fallback_threshold_mult)
                    floor_th = float(self.base_score_th) * float(self.score_distribution_floor_mult)
                    score_threshold_regime = min(score_threshold_regime, max(cap_th, floor_th, 1e-9))
            raw_signal_ratio = abs(raw_signal_now) / max(float(score_threshold_regime), 1e-9)
            zero_score_collapse = abs(float(sc)) <= float(self.score_zero_epsilon)
            directional_proxy_confident = bool(
                raw_signal_ratio >= float(self.exec_min_raw_signal_ratio)
                and abs(raw_signal_now) > float(self.score_zero_epsilon)
            )
            if zero_score_collapse:
                if raw_signal_now > score_eps and directional_proxy_confident:
                    trade_side = "BUY"
                elif raw_signal_now < -score_eps and directional_proxy_confident:
                    trade_side = "SELL"
                else:
                    trade_side = "NONE"
            diag["regime_bucket"] = regime_bucket
            diag["score_threshold_regime"] = float(score_threshold_regime)
            diag["score_threshold_regime_raw"] = float(score_threshold_raw)
            diag["score_threshold_base_mult"] = float(th_parts["base_mult"])
            diag["score_threshold_side_mult"] = float(th_parts["side_mult"])
            diag["score_threshold_adapt_mult"] = float(th_parts["adapt_mult"])
            diag["score_threshold_total_mult"] = float(th_parts["total_mult"])
            diag["score_threshold_ref_q"] = float(score_threshold_ref_q)
            diag["score_threshold_ref_n"] = int(score_threshold_ref_n)
            diag["fallback_path"] = str(fallback_path)
            diag["starvation_mode"] = bool(self.starvation_mode_active)
            diag["starvation_relax_level"] = float(self.starvation_relax_level)
            diag["raw_signal_ratio"] = float(raw_signal_ratio)
            diag["zero_score_collapse"] = bool(zero_score_collapse)
            diag["directional_proxy_confident"] = bool(directional_proxy_confident)

            # Range regimes are contrarian by construction; use magnitude support there.
            if regime_bucket == "range":
                aligned_sharpe = abs(aligned_sharpe)
                diag["predictive_sharpe_aligned"] = float(aligned_sharpe)

            sharpe_threshold_regime = self._regime_sharpe_threshold(regime_bucket)
            diag["min_predictive_sharpe_regime"] = float(sharpe_threshold_regime)
            score_ok = abs(sc) >= score_threshold_regime
            sharpe_ok = aligned_sharpe >= float(sharpe_threshold_regime)
            hawkes_ok = (
                (not self.use_hawkes)
                or (not self.use_hawkes_gate)
                or (hawkes_n_val >= float(self.hawkes_n_min))
            )

            cost_fraction = (
                spread_used
                * max(float(pip_value_symbol), 1e-9)
                * max(float(lot_fraction_gate), 1e-9)
            ) / max(float(equity_for_gate), 1e-9)
            corr_pressure = self._symbol_corr_pressure(sym, md, held_symbols_up)
            utility_diag = {
                "utility": 0.0,
                "utility_edge": float(expected_move),
                "utility_var_pen": float(diag.get("vol", 0.0)),
                "utility_tail_pen": float(diag.get("lppls_hazard", 0.0)),
                "utility_cost_pen": float(cost_fraction),
                "utility_corr_pen": float(corr_pressure),
            }
            utility_ok = True
            if self.use_utility_objective:
                utility_diag = self._trade_utility(
                    side=trade_side,
                    expected_move=expected_move,
                    vol_now=float(diag.get("vol", 0.0)),
                    cost_fraction=cost_fraction,
                    lppls_hazard=float(diag.get("lppls_hazard", 0.0)),
                    corr_pressure=corr_pressure,
                )
                utility_ok = float(utility_diag["utility"]) >= float(self.utility_min)
            diag.update(utility_diag)
            diag["utility_min"] = float(self.utility_min)
            diag["utility_ok"] = bool(utility_ok)
            diag["utility_gate_mode"] = self.utility_gate_mode if self.use_utility_objective else "off"

            blockers: list[str] = []
            if not spread_ok:
                blockers.append("spread")
            if not cost_ok:
                blockers.append("cost_gate")
            if not heston_ok:
                blockers.append("heston_vol_guard")
            if zero_score_collapse and (not directional_proxy_confident):
                blockers.append("zero_score_collapse")
            if not score_ok and (not (zero_score_collapse and (not directional_proxy_confident))):
                blockers.append("low_score")
            if not sharpe_ok:
                blockers.append("low_predictive_sharpe")
            if self.use_hawkes_gate and (not hawkes_ok):
                blockers.append("hawkes_crowding")
            if self.use_utility_objective and (not utility_ok) and self.utility_gate_mode == "hard":
                blockers.append("negative_utility")
            if not bool(diag.get("bar_integrity_ok", True)):
                blockers.append("bar_integrity")

            conf_metrics = self._trade_confidence_metrics(
                sc=sc,
                diag=diag,
                spread_pips=spread_used,
                expected_move=expected_move,
                pip_value_per_lot=pip_value_symbol,
                equity=equity_for_gate,
                lot_fraction=lot_fraction_gate,
                score_threshold=score_threshold_regime,
                sharpe_threshold=sharpe_threshold_regime,
                heston_ratio=heston_ratio,
            )
            diag["confidence"] = float(conf_metrics["confidence"])
            gate_penalty = 1.0
            if blockers:
                if not spread_ok:
                    gate_penalty *= float(np.clip(2.0 / max(spread_used, 1e-9), 0.35, 1.0))
                if not cost_ok:
                    gate_penalty *= float(np.clip(conf_metrics["cost_ratio"], 0.35, 1.0))
                if not score_ok:
                    score_floor = 0.35 if self.entry_gate_mode == "hard" else float(self.soft_score_penalty_floor)
                    gate_penalty *= float(
                        np.clip(abs(sc) / max(score_threshold_regime, 1e-9), score_floor, 1.0)
                    )
                if not sharpe_ok:
                    gate_penalty *= float(
                        np.clip(aligned_sharpe / max(float(sharpe_threshold_regime), 1e-9), 0.20, 1.0)
                    )
                if not heston_ok:
                    gate_penalty *= float(np.clip(1.0 / max(heston_ratio, 1e-9), 0.35, 1.0))
                if self.use_hawkes_gate and (not hawkes_ok):
                    gate_penalty *= float(
                        np.clip(hawkes_n_val / max(float(self.hawkes_n_min), 1e-9), 0.35, 1.0)
                    )
            if self.use_utility_objective and (not utility_ok) and self.utility_gate_mode == "soft":
                gate_penalty *= 0.60
            confidence_raw = float(conf_metrics["confidence"])
            confidence_exec_base = float(conf_metrics.get("confidence_exec_base", confidence_raw))
            confidence_adj = confidence_raw
            confidence_exec = confidence_exec_base
            if blockers:
                confidence_penalty_floor = (
                    0.10 if self.entry_gate_mode == "hard" else float(np.clip(self.soft_score_penalty_floor, 0.0, 0.10))
                )
                confidence_adj *= float(np.clip(gate_penalty, confidence_penalty_floor, 1.0))
                if self.entry_gate_mode == "hard":
                    confidence_adj = min(confidence_adj, confidence_raw * 0.5)
                # Execution gate already checks score_ratio/sharpe_ratio directly.
                # Keep execution confidence aligned to non-score blocker pressure so
                # soft low_score candidates are not double-penalized.
                exec_penalty = 1.0
                if not spread_ok:
                    exec_penalty *= float(np.clip(2.0 / max(spread_used, 1e-9), 0.35, 1.0))
                if not cost_ok:
                    exec_penalty *= float(np.clip(conf_metrics["cost_ratio"], 0.35, 1.0))
                if not heston_ok:
                    exec_penalty *= float(np.clip(1.0 / max(heston_ratio, 1e-9), 0.35, 1.0))
                if self.use_hawkes_gate and (not hawkes_ok):
                    exec_penalty *= float(
                        np.clip(hawkes_n_val / max(float(self.hawkes_n_min), 1e-9), 0.35, 1.0)
                    )
                if self.use_utility_objective and (not utility_ok) and self.utility_gate_mode == "soft":
                    exec_penalty *= 0.60
                confidence_exec *= float(np.clip(exec_penalty, 0.20, 1.0))
            confidence_adj = float(np.clip(confidence_adj, 0.0, 100.0))
            confidence_exec = float(np.clip(confidence_exec, 0.0, 100.0))
            conf_metrics["confidence_raw"] = float(confidence_raw)
            conf_metrics["confidence"] = float(confidence_adj)
            conf_metrics["confidence_exec"] = float(confidence_exec)
            diag["confidence_raw"] = float(confidence_raw)
            diag["confidence_exec_base"] = float(confidence_exec_base)
            diag["confidence"] = float(confidence_adj)
            diag["confidence_exec"] = float(confidence_exec)
            sc_effective = float(sc * gate_penalty)
            diag["score_effective"] = sc_effective
            diag["gate_penalty"] = float(gate_penalty)
            diag["confidence_penalty_floor"] = (
                0.10 if self.entry_gate_mode == "hard" else float(np.clip(self.soft_score_penalty_floor, 0.0, 0.10))
            )
            score_ratio_exec = float(conf_metrics.get("score_ratio", 0.0))
            exec_score_basis = "score_ratio"
            if self.exec_use_raw_signal_proxy:
                score_ratio_exec = float(max(score_ratio_exec, raw_signal_ratio))
                exec_score_basis = "proxy_max"

            if trade_side in {"BUY", "SELL"}:
                candidate_side = trade_side
            elif sc_effective > score_eps:
                candidate_side = "BUY"
            elif sc_effective < -score_eps:
                candidate_side = "SELL"
            else:
                candidate_side = "NONE"
            candidate_side_raw = str(candidate_side)

            side_metrics_pre = self._rolling_side_metrics()
            dominant_side = "NONE"
            dominant_share = 0.0
            direction_bias_guard_active = False
            direction_bias_justification = "none"
            direction_bias_penalty_applied = False
            direction_bias_priority_penalty = 1.0
            if candidate_side in {"BUY", "SELL"} and int(side_metrics_pre.get("samples", 0)) >= int(
                self.direction_bias_window
            ):
                buy_share_now = float(side_metrics_pre.get("buy_share", 0.5))
                sell_share_now = float(side_metrics_pre.get("sell_share", 0.5))
                if buy_share_now >= sell_share_now:
                    dominant_side = "BUY"
                    dominant_share = buy_share_now
                else:
                    dominant_side = "SELL"
                    dominant_share = sell_share_now
                if dominant_share > float(self.direction_bias_max_share) and candidate_side == dominant_side:
                    direction_bias_penalty_applied = True
                    direction_bias_priority_penalty = float(self.direction_bias_priority_penalty)
                    if self._direction_bias_trend_justified(candidate_side, p_trend_val, regime_bucket):
                        direction_bias_justification = "trend_regime"
                    else:
                        direction_bias_guard_active = True
                        blockers.append("direction_bias_guard")

            direction_abstain_triggered = bool(
                candidate_side in {"BUY", "SELL"}
                and float(score_ratio_exec) < float(self.direction_abstain_score_ratio)
            )
            if direction_abstain_triggered:
                blockers.append("direction_abstain")

            if direction_bias_guard_active or direction_abstain_triggered:
                candidate_side = "NONE"

            self.candidate_side_history.append(str(candidate_side))
            side_metrics_post = self._rolling_side_metrics()
            blocked_by = blockers[0] if blockers else "none"
            diag["blocked_by"] = str(blocked_by)

            candidate_row = {
                "symbol": sym,
                "side": str(candidate_side),
                "side_raw": str(candidate_side_raw),
                "score": float(abs(sc_effective)),
                "score_raw": float(sc),
                "score_effective": float(sc_effective),
                "gate_penalty": float(gate_penalty),
                "blocked_by": blocked_by if blocked_by else "none",
                "blocked_by_all": ",".join(blockers) if blockers else "none",
                "entry_gate_mode": self.entry_gate_mode,
                "utility_gate_mode": self.utility_gate_mode if self.use_utility_objective else "off",
                "spread_pips": float(spread_used),
                "expected_move": float(expected_move),
                "predictive_sharpe": float(diag.get("predictive_sharpe", 0.0)),
                "predictive_sharpe_aligned": float(aligned_sharpe),
                "predictive_sharpe_threshold": float(sharpe_threshold_regime),
                "p_trend": float(diag.get("p_trend", 0.5)),
                "regime_bucket": regime_bucket,
                "score_threshold_regime": float(score_threshold_regime),
                "score_threshold_base_mult": float(diag.get("score_threshold_base_mult", 1.0)),
                "score_threshold_side_mult": float(diag.get("score_threshold_side_mult", 1.0)),
                "score_threshold_adapt_mult": float(diag.get("score_threshold_adapt_mult", 1.0)),
                "score_threshold_total_mult": float(diag.get("score_threshold_total_mult", 1.0)),
                "hawkes_n": float(diag.get("hawkes_n", 1.0)),
                "beta_p": float(diag.get("beta_p", 0.0)),
                "beta_m": float(diag.get("beta_m", 0.0)),
                "momentum_component": float(diag.get("momentum_component", 0.0)),
                "micro_component": float(diag.get("micro_component", 0.0)),
                "ai_component": float(diag.get("ai_component", 0.0)),
                "ai_signal": float(diag.get("ai_signal", 0.0)),
                "ai_confidence": float(diag.get("ai_confidence", 0.0)),
                "ai_factor": float(diag.get("ai_factor", 1.0)),
                "ai_hit_rate": float(diag.get("ai_hit_rate", 0.5)),
                "ai_samples": int(diag.get("ai_samples", 0)),
                "raw_signal": float(diag.get("raw_signal", 0.0)),
                "raw_signal_ratio": float(raw_signal_ratio),
                "score_ratio_exec": float(score_ratio_exec),
                "direction_abstain_triggered": bool(direction_abstain_triggered),
                "direction_abstain_score_ratio": float(self.direction_abstain_score_ratio),
                "exec_score_basis": str(exec_score_basis),
                "direction_bias_guard_active": bool(direction_bias_guard_active),
                "direction_bias_justification": str(direction_bias_justification),
                "direction_bias_window": int(self.direction_bias_window),
                "direction_bias_max_share": float(self.direction_bias_max_share),
                "direction_bias_dominant_side": str(dominant_side),
                "direction_bias_dominant_share": float(dominant_share),
                "direction_bias_penalty_applied": bool(direction_bias_penalty_applied),
                "direction_bias_priority_penalty": float(direction_bias_priority_penalty),
                "side_share_buy_rolling": float(side_metrics_post.get("buy_share", 0.5)),
                "side_share_sell_rolling": float(side_metrics_post.get("sell_share", 0.5)),
                "abstain_rate_rolling": float(side_metrics_post.get("abstain_rate", 0.0)),
                "zero_score_collapse": bool(zero_score_collapse),
                "lppls_factor": float(diag.get("lppls_factor", 1.0)),
                "session_mult": float(diag.get("session_mult", 1.0)),
                "direction_factor": float(diag.get("direction_factor", 1.0)),
                "direction_hit_rate": float(diag.get("direction_hit_rate", 0.5)),
                "direction_buy_hit_rate": float(diag.get("direction_buy_hit_rate", 0.5)),
                "direction_sell_hit_rate": float(diag.get("direction_sell_hit_rate", 0.5)),
                "direction_buy_samples": int(diag.get("direction_buy_samples", 0)),
                "direction_sell_samples": int(diag.get("direction_sell_samples", 0)),
                "direction_samples": int(diag.get("direction_samples", 0)),
                "primary_horizon_hours": float(diag.get("primary_horizon_hours", self.hold_default_horizon_hours)),
                "horizon_confidence": float(diag.get("horizon_confidence", 0.0)),
                "horizon_strength": float(diag.get("horizon_strength", 0.0)),
                "horizon_side": str(diag.get("horizon_side", "NONE")),
                "heston_scale": float(heston_scale),
                "heston_ratio": float(heston_ratio),
                "fallback_path": str(fallback_path),
                "starvation_mode": bool(self.starvation_mode_active),
                "starvation_relax_level": float(self.starvation_relax_level),
                "gate_spread": bool(spread_ok),
                "gate_cost": bool(cost_ok),
                "gate_score": bool(score_ok),
                "gate_sharpe": bool(sharpe_ok),
                "gate_hawkes": bool(hawkes_ok),
                "gate_heston": bool(heston_ok),
                "gate_utility": bool(utility_ok),
                "utility": float(diag.get("utility", 0.0)),
                "utility_min": float(self.utility_min),
                "fresh_ticks_ok": bool(diag.get("fresh_ticks_ok", True)),
                "tick_age_secs": float(diag.get("tick_age_secs", -1.0)),
                "gap_recovered": bool(diag.get("gap_recovered", False)),
                "gap_recovery_source": str(diag.get("gap_recovery_source", "none")),
                "gap_fill_truncated": bool(diag.get("gap_fill_truncated", False)),
                "warmup_strategy": str(diag.get("warmup_strategy", self.startup_warmup_strategy)),
                "startup_backfill_pending": bool(diag.get("startup_backfill_pending", False)),
                "startup_backfill_ready": bool(diag.get("startup_backfill_ready", False)),
                "startup_backfill_bars": int(diag.get("startup_backfill_bars", 0)),
                "startup_backfill_retry_age_secs": float(diag.get("startup_backfill_retry_age_secs", 0.0)),
                "startup_backward_replay_done": bool(diag.get("startup_backward_replay_done", False)),
                "bar_integrity_ok": bool(diag.get("bar_integrity_ok", True)),
            }
            candidate_row.update(conf_metrics)
            priority = float(abs(sc_effective)) * (
                0.55 + 0.45 * float(np.clip(float(candidate_row.get("confidence", 0.0)) / 100.0, 0.0, 1.0))
            )
            if self.use_utility_objective:
                u_ref = max(abs(float(self.utility_min)), 1e-6)
                u_now = float(candidate_row.get("utility", 0.0))
                u_bonus = float(np.clip((u_now - float(self.utility_min)) / (3.0 * u_ref), -1.0, 2.0))
                priority *= float(np.clip(1.0 + 0.10 * u_bonus, 0.65, 1.35))
            if direction_bias_penalty_applied:
                priority *= float(np.clip(direction_bias_priority_penalty, 0.10, 1.0))
            if blockers and self.entry_gate_mode == "soft":
                priority *= 0.80
            candidate_row["priority"] = float(max(priority, 0.0))
            entry_ready = bool(
                str(candidate_row.get("side", "NONE")).upper() in {"BUY", "SELL"}
                and not (blockers and self.entry_gate_mode == "hard")
            )
            exec_quality_ready = True
            if self.use_execution_quality_gate:
                blockers_text = str(candidate_row.get("blocked_by_all", candidate_row.get("blocked_by", "none")))
                dyn_min_score_ratio = self._dynamic_exec_min_score_ratio(
                    confidence_exec=float(candidate_row.get("confidence_exec", 0.0)),
                    sharpe_ratio=float(candidate_row.get("sharpe_ratio", 0.0)),
                    cost_ratio=float(candidate_row.get("cost_ratio", 0.0)),
                    blockers=blockers_text,
                )
                score_ratio_exec_now = float(candidate_row.get("score_ratio_exec", candidate_row.get("score_ratio", 0.0)))
                score_ratio_ready = score_ratio_exec_now >= float(dyn_min_score_ratio)
                soft_score_bypass = False
                if not score_ratio_ready:
                    soft_score_bypass = self._allow_soft_exec_low_score_bypass(
                        blockers=blockers_text,
                        confidence_exec=float(candidate_row.get("confidence_exec", 0.0)),
                        sharpe_ratio=float(candidate_row.get("sharpe_ratio", 0.0)),
                        cost_ratio=float(candidate_row.get("cost_ratio", 0.0)),
                        score_ratio_exec=score_ratio_exec_now,
                    )
                    if soft_score_bypass:
                        score_ratio_ready = True
                        dyn_min_score_ratio = min(
                            float(dyn_min_score_ratio),
                            float(np.clip(self.exec_min_score_ratio_soft, 0.0, 1.50)),
                        )
                exec_quality_ready = (
                    float(candidate_row.get("confidence_exec", 0.0)) >= float(self.exec_min_confidence)
                    and bool(score_ratio_ready)
                    and float(candidate_row.get("sharpe_ratio", 0.0)) >= float(self.exec_min_sharpe_ratio)
                )
                candidate_row["exec_min_score_ratio_dynamic"] = float(dyn_min_score_ratio)
                candidate_row["exec_score_ratio_soft_bypass"] = bool(soft_score_bypass)
            exec_cost_ready = True
            if self.execution_gate_mode == "hard":
                exec_cost_ready = bool(cost_ok)
            execution_ready = bool(entry_ready and exec_quality_ready and exec_cost_ready)
            candidate_row["entry_ready"] = bool(entry_ready)
            candidate_row["exec_quality_ready"] = bool(exec_quality_ready)
            candidate_row["exec_cost_ready"] = bool(exec_cost_ready)
            candidate_row["execution_ready"] = bool(execution_ready)
            candidate_rows.append(candidate_row)
            self._audit_emit_row(
                {
                    "phase": "candidate",
                    "symbol": str(sym),
                    "side": str(candidate_row.get("side", "")),
                    "side_raw": str(candidate_row.get("side_raw", "")),
                    "score_raw": float(candidate_row.get("score_raw", 0.0)),
                    "score_effective": float(candidate_row.get("score_effective", 0.0)),
                    "gate_penalty": float(candidate_row.get("gate_penalty", 1.0)),
                    "blockers": str(candidate_row.get("blocked_by_all", "none")),
                    "entry_ready": bool(entry_ready),
                    "exec_quality_ready": bool(exec_quality_ready),
                    "execution_ready": bool(execution_ready),
                    "lot_pre_floor": 0.0,
                    "lot_post_floor": 0.0,
                    "rejection_reason": str(candidate_row.get("blocked_by", "none")),
                    "outcome": "candidate",
                    "confidence": float(candidate_row.get("confidence", 0.0)),
                    "confidence_exec": float(candidate_row.get("confidence_exec", 0.0)),
                    "raw_signal": float(candidate_row.get("raw_signal", 0.0)),
                    "score_ratio": float(candidate_row.get("score_ratio", 0.0)),
                    "score_ratio_exec": float(candidate_row.get("score_ratio_exec", candidate_row.get("score_ratio", 0.0))),
                    "exec_min_score_ratio_dynamic": float(candidate_row.get("exec_min_score_ratio_dynamic", 0.0)),
                    "exec_score_ratio_soft_bypass": bool(candidate_row.get("exec_score_ratio_soft_bypass", False)),
                    "exec_score_basis": str(candidate_row.get("exec_score_basis", "score_ratio")),
                    "sharpe_ratio": float(candidate_row.get("sharpe_ratio", 0.0)),
                    "cost_ratio": float(candidate_row.get("cost_ratio", 0.0)),
                    "zero_score_collapse": bool(candidate_row.get("zero_score_collapse", False)),
                    "gap_recovery_source": str(candidate_row.get("gap_recovery_source", "none")),
                    "gap_fill_truncated": bool(candidate_row.get("gap_fill_truncated", False)),
                    "warmup_mode": bool(self._warmup_mode_active()),
                    "warmup_strategy": str(candidate_row.get("warmup_strategy", self.startup_warmup_strategy)),
                    "startup_backfill_pending": bool(candidate_row.get("startup_backfill_pending", False)),
                    "startup_backfill_ready": bool(candidate_row.get("startup_backfill_ready", False)),
                    "startup_backfill_bars": int(candidate_row.get("startup_backfill_bars", 0)),
                    "startup_backfill_retry_age_secs": float(
                        candidate_row.get("startup_backfill_retry_age_secs", 0.0)
                    ),
                    "startup_backward_replay_done": bool(
                        candidate_row.get("startup_backward_replay_done", False)
                    ),
                    "starvation_mode": bool(self.starvation_mode_active),
                    "relax_level": float(self.starvation_relax_level),
                    "direction_abstain_triggered": bool(
                        candidate_row.get("direction_abstain_triggered", False)
                    ),
                    "direction_bias_guard_active": bool(
                        candidate_row.get("direction_bias_guard_active", False)
                    ),
                    "direction_bias_justification": str(
                        candidate_row.get("direction_bias_justification", "none")
                    ),
                    "side_share_buy_rolling": float(candidate_row.get("side_share_buy_rolling", 0.5)),
                    "side_share_sell_rolling": float(candidate_row.get("side_share_sell_rolling", 0.5)),
                    "abstain_rate_rolling": float(candidate_row.get("abstain_rate_rolling", 0.0)),
                    "fallback_path": str(candidate_row.get("fallback_path", "none")),
                }
            )

            candidate_rank = (
                1 if execution_ready else 0,
                float(candidate_row.get("priority", 0.0)),
                float(candidate_row.get("confidence", 0.0)),
            )
            if candidate_rank > best_candidate_rank:
                best_candidate_rank = candidate_rank
                best_candidate_score = float(sc_effective)
                best_candidate_diag = dict(diag)
                best_candidate_sym = sym

            if blockers and self.entry_gate_mode == "hard":
                self._log_rejection(blocked_by)
                continue

            if blockers and (not bool(candidate_row.get("execution_ready", False))):
                for reason_soft in blockers:
                    self._log_rejection(f"soft_{reason_soft}")

            side = str(candidate_row.get("side", "NONE")).upper()
            if side not in {"BUY", "SELL"}:
                if bool(candidate_row.get("direction_abstain_triggered", False)):
                    self._log_rejection("direction_abstain")
                elif bool(candidate_row.get("direction_bias_guard_active", False)):
                    self._log_rejection("direction_bias_guard")
                else:
                    self._log_rejection("zero_score_collapse")
                continue
            
            # --- DECISION GENERATION ---
            # Standard logic (Overlord Removed)
            gate_note = ",".join(blockers) if blockers else "pass"
            reason = (
                f"Score eff {abs(sc_effective):.2f} raw {abs(sc):.2f} | "
                f"SharpeA {aligned_sharpe:.2f} | Reg {regime_bucket} "
                f"th {score_threshold_regime:.2f} (x{float(diag.get('score_threshold_total_mult', 1.0)):.2f}) | "
                f"Gates {self.entry_gate_mode}:{gate_note}"
            )
            if self.use_utility_objective:
                reason += f" | U {float(diag.get('utility', 0.0)):+.4f}"
            if fallback_path != "none":
                reason += f" | Fallback {fallback_path}"
            if self.starvation_mode_active:
                reason += f" | Starve rL={self.starvation_relax_level:.2f}"
            if diag.get("lppls_hazard", 0) > self.lppls_threshold:
                 reason += " [LPPLS Caution]"
            
            raw.append(
                Decision(
                    symbol=sym,
                    side=side,
                    reason=reason,
                    score=float(abs(sc_effective)),
                    priority=float(candidate_row.get("priority", abs(sc_effective))),
                    confidence=float(candidate_row.get("confidence", 0.0)),
                    score_ratio=float(candidate_row.get("score_ratio", 0.0)),
                    utility=float(candidate_row.get("utility", 0.0)),
                    blocked_by=str(candidate_row.get("blocked_by", "none")),
                )
            )
            self._log_decision(sym, {
                "side": side,
                "score": float(abs(sc_effective)),
                "reason": reason,
                "confidence": float(conf_metrics["confidence"]),
                "p_trend": float(diag.get("p_trend", 0.5)),
                "predictive_sharpe": float(diag.get("predictive_sharpe", 0.0)),
                "predictive_sharpe_aligned": float(aligned_sharpe),
                "vol": float(diag.get("vol", 0.0)),
            })

        # Persist top candidate diagnostics for bridge/indicator HUDs.
        if candidate_rows:
            ready_n = int(sum(1 for r in candidate_rows if bool(r.get("execution_ready", False))))
            suppression = float(max(0.0, 1.0 - (ready_n / max(len(candidate_rows), 1))))
            self.suppression_ratio_history.append(float(suppression))
            if len(self.suppression_ratio_history) > 120:
                self.suppression_ratio_history = self.suppression_ratio_history[-120:]
            self.suppression_ratio_rolling = float(np.mean(self.suppression_ratio_history[-36:]))
        else:
            self.suppression_ratio_rolling = float(self.suppression_ratio_rolling)
        if self.rejection_stats_cycle:
            self.dominant_rejection_reason = str(
                max(self.rejection_stats_cycle, key=lambda k: int(self.rejection_stats_cycle.get(k, 0)))
            )
        else:
            self.dominant_rejection_reason = "none"
        side_metrics_live = self._rolling_side_metrics()
        self.side_share_buy_rolling = float(side_metrics_live.get("buy_share", 0.5))
        self.side_share_sell_rolling = float(side_metrics_live.get("sell_share", 0.5))
        self.abstain_rate_rolling = float(side_metrics_live.get("abstain_rate", 0.0))
        hit_delta, expectancy_delta = self._rolling_edge_vs_random()
        self.edge_vs_random_hit_delta = float(hit_delta)
        self.edge_vs_random_expectancy_delta = float(expectancy_delta)

        self.last_candidates = sorted(
            candidate_rows,
            key=lambda row: (
                1 if bool(row.get("execution_ready", False)) else 0,
                float(row.get("priority", 0.0)),
                float(row.get("confidence", 0.0)),
            ),
            reverse=True,
        )[:5]
        self.last_best_candidate = self.last_candidates[0] if self.last_candidates else {}
        candidate_map: dict[tuple[str, str], dict] = {}
        for row in candidate_rows:
            key = self._candidate_key(str(row.get("symbol", "")), str(row.get("side", "")))
            prev = candidate_map.get(key)
            if prev is None or float(row.get("priority", row.get("score", 0.0))) > float(
                prev.get("priority", prev.get("score", 0.0))
            ):
                candidate_map[key] = row
        self.last_candidate_map = candidate_map

        # HUD Update Logic
        # Priority: Held Symbol > Best Candidate
        target_hud_sym = None
        if held_symbols_up:
            md_lookup = {s.upper(): s for s in md.keys()}
            for hs in held_symbols_up:
                if hs in md_lookup:
                    target_hud_sym = md_lookup[hs]
                    break
            action_lbl = f"Managing {target_hud_sym}" if target_hud_sym else "Scanning..."
        if target_hud_sym is None and best_candidate_sym:
            target_hud_sym = best_candidate_sym
            action_lbl = "Scanning..."
            
        if target_hud_sym:
             md_hud = md.get(target_hud_sym)
             if md_hud is not None:
                 # Re-score or use existing? We need diag.
                 # If it was best candidate, we have best_candidate_diag.
                 # If it is held, we might need to retrieve diag from last run or re-calc.
                 # For simplicity, let's use the diag from the current loop if available, else re-score.
                 
                 hud_diag = {}
                 hud_score = 0
                 
                 if target_hud_sym == best_candidate_sym:
                     hud_diag = best_candidate_diag
                     hud_score = best_candidate_score
                 else:
                     # Re-score held symbol for display
                     try:
                         hud_score, hud_diag = self.score_symbol(md_hud, target_hud_sym)
                     except Exception as exc:
                         logger.debug(f"{target_hud_sym}: HUD rescore failed ({exc})")
                 
                 if hud_diag:
                     self._send_hud(target_hud_sym, hud_score, hud_diag, md_hud, action_label=action_lbl)
                     self.last_diagnostics = hud_diag

        self._interop_last_decisions_timing = {
            "score_symbol_ms_total": float(score_symbol_ms_total),
            "score_symbol_calls": float(score_symbol_calls),
            "score_symbol_ms_mean": float(score_symbol_ms_total / max(score_symbol_calls, 1)),
            "candidate_count": float(len(candidate_rows)),
            "decision_count": float(len(raw)),
        }

        if not raw: return []

        # correlation filter on H1 returns
        rets = {s: np.log(md[s]["close"]).diff().dropna() for s,_ in [(d.symbol, d.score) for d in raw]}
        corr = pd.DataFrame(rets).corr()

        # sort by execution priority (confidence/utility-weighted), pick low-corr set
        ranked = sorted(
            [(d.symbol, (d.priority if d.priority > 0 else d.score)) for d in raw],
            key=lambda x: x[1],
            reverse=True,
        )
        chosen_syms = low_corr_pick(ranked, corr, self.maxK, self.corr_max)

        # map back to full decision objects
        chosen = [d for d in raw if d.symbol in chosen_syms]
        return chosen
    
    def _send_hud(self, sym: str, sc: float, diag: dict, df: pd.DataFrame, action_label: str = "Scanning..."):
        import time
        try:
            spread_val = float(df.attrs.get("spread", 0))
            # Ensure all values are native Python types (no numpy)
            hud_data = {
                "symbol": str(sym),
                "type": "hud",
                "action": str(action_label), # NEW field for status
                "score": float(round(sc, 3)),
                "trend": float(round(diag.get("p_trend", 0.5), 2)),
                "regime": "Bull" if sc > 0 else "Bear", 
                "sharpe": float(round(diag.get("predictive_sharpe", 0), 2)),
                "vol": float(round(diag.get("vol", 0) * 10000, 1)),
                "hawkes": float(round(diag.get("hawkes_n", 0), 2)),
                "crash": float(round(diag.get("lppls_hazard", 0), 2)),
                "spread": float(round(spread_val, 1)),
                "pz": float(round(diag.get("pz", 0), 3)),
                "time": int(time.time())
            }
            # Low retry count, fail fast
            post_visuals(hud_data, max_retries=0)
        except Exception as e:
            logger.debug(f"HUD Error: {e}")  # L4 FIX: bridge timeouts should not flood the error log

    def _extract_root(self, symbol: str) -> str:
        """Extract FX root from symbol (e.g., 'EURUSD.MINI' -> 'EURUSD')."""
        s_up = symbol.upper()
        for root in self.roots:
            if root in s_up:
                return root
        return symbol  # fallback

    def _manage_exits(self, positions: list[dict], md: dict[str, pd.DataFrame]) -> None:
        """Check active positions for score reversals AND risk manager exits."""
        if self.execution_mode == "read_only":
            self.monitor_close_positions = []
            if positions:
                self._log_rejection("execution_mode_read_only")
            return
        close_monitor_positions: list[dict] = []
        open_syms = {
            str((pos or {}).get("symbol", "")).strip().upper()
            for pos in list(positions or [])
            if str((pos or {}).get("symbol", "")).strip()
        }
        for stale_sym in list(self.soft_reversal_persistence.keys()):
            if stale_sym not in open_syms:
                self.soft_reversal_persistence.pop(stale_sym, None)
        # L3 FIX: risk_manager is always created in __init__; hasattr guard was dead code.
        for pos in positions:
            close_monitor_row: dict | None = None
            raw_sym = pos.get("symbol")
            if not raw_sym: continue
            
            # CASE INSENSITIVE MATCHING
            # Try exact, then upper, then lower
            sym = raw_sym
            if sym not in md:
                if sym.upper() in md: sym = sym.upper()
                elif sym.lower() in md: sym = sym.lower()
                else: continue # Still not found
            
            # Re-score
            try:
                sc, diag = self.score_symbol(md[sym], sym)
            except Exception:
                continue
            p_trend_val = float(diag.get("p_trend", 0.5))
            reverse_side = "BUY" if float(sc) >= 0.0 else "SELL"
            direction_quality = {
                "samples": int(diag.get("direction_samples", 0)),
                "buy_samples": int(diag.get("direction_buy_samples", 0)),
                "sell_samples": int(diag.get("direction_sell_samples", 0)),
                "buy_hit_rate": float(diag.get("direction_buy_hit_rate", 0.5)),
                "sell_hit_rate": float(diag.get("direction_sell_hit_rate", 0.5)),
            }
            exit_th_parts = self._regime_threshold_components(
                p_trend_val,
                side=reverse_side,
                direction_quality=direction_quality,
            )
            exit_score_threshold_raw = float(exit_th_parts.get("threshold", self.score_th))
            exit_score_threshold, _, _ = self._adaptive_score_threshold(
                str(sym).upper(),
                exit_score_threshold_raw,
                exclude_latest=True,
            )

            def _emit_exit(outcome: str, rejection_reason: str = "", side_now: str = "") -> None:
                self._audit_emit_row(
                    {
                        "phase": "exit",
                        "symbol": str(sym),
                        "side": str(side_now),
                        "score_raw": float(sc),
                        "score_effective": float(sc),
                        "gate_penalty": 1.0,
                        "blockers": "none",
                        "entry_ready": True,
                        "exec_quality_ready": True,
                        "execution_ready": True,
                        "lot_pre_floor": float(pos.get("lots", 0.0) or 0.0),
                        "lot_post_floor": float(pos.get("lots", 0.0) or 0.0),
                        "rejection_reason": str(rejection_reason),
                        "outcome": str(outcome),
                    }
                )
            
            # --- UPDATE RISK STATE ---
            try:
                # MT4 positions have 'open_price', 'type' (0=buy,1=sell), 'open_time'
                p_type_raw = pos.get("type", -1)
                p_magic = int(pos.get("magic", 246810))
                if isinstance(p_type_raw, str):
                    pt = p_type_raw.upper()
                    if pt == "BUY":
                        side = "BUY"
                    elif pt == "SELL":
                        side = "SELL"
                    else:
                        side = "NONE"
                else:
                    p_type_int = int(p_type_raw)
                    side = "BUY" if p_type_int == 0 else ("SELL" if p_type_int == 1 else "NONE")
                # bridge uses string usually, or numeric. Let's assume numeric or try cast.
                # In bridge_client.py get_positions returns simple list. 
                # Let's assume bridge returns what MT4 provides: 'open_price'
                open_price = float(pos.get("open_price", 0.0))
                if open_price <= 0 or side == "NONE":
                    self._log_rejection("exit_pos_incomplete")
                    _emit_exit("rejected", "exit_pos_incomplete", side)
                    logger.warning(
                        "%s: skipping exit review due to incomplete position payload (side=%s open_price=%s)",
                        sym,
                        side,
                        pos.get("open_price"),
                    )
                    continue
                # Parsing open_time (MT4 is usually Unix timestamp or string)
                raw_time = pos.get("open_time", 0)
                open_time = 0.0
                try:
                    open_time = float(raw_time)
                except (TypeError, ValueError):
                    # Try parsing string format "2026.02.17 08:16:17"
                    try:
                        import datetime
                        # Adjust format to match MT4: YYYY.MM.DD HH:MM:SS
                        dt = datetime.datetime.strptime(str(raw_time), "%Y.%m.%d %H:%M:%S")
                        open_time = dt.timestamp()
                    except Exception as e:
                        logger.warning(f"Failed to parse open_time '{raw_time}' for {sym}: {e}")
                        self._log_rejection("exit_pos_open_time_invalid")
                        _emit_exit("rejected", "exit_pos_open_time_invalid", side)
                        continue

                current_price = float(md[sym]["close"].iloc[-1])
                current_vol = diag.get("vol", 0.01)
                
                # GRACE PERIOD LOGIC Moved to Overlord Prompt
                # We calculate hold duration and pass it to the review_position method.
                # TIME SKEW FIX: Use Market Data time (Broker Time) to match Position time
                # Local system time (time.time()) might mismatch Broker Server time by hours.
                try:
                    # md[sym] index is DatetimeIndex (from bridge_client)
                    now_ts = md[sym].index[-1].timestamp()
                except Exception:
                    now_ts = time.time()

                hold_policy = self._exit_hold_policy(diag, side)
                exit_score_threshold_eff = float(exit_score_threshold) * float(
                    hold_policy.get("reversal_threshold_mult", 1.0)
                )
                
                hold_duration = now_ts - open_time
                
                # Sanity Check
                if hold_duration < 0: hold_duration = 0 
                
                self.risk_manager.update_position_state(
                    symbol=sym,
                    current_price=current_price,
                    entry_price=open_price,
                    side=side,
                    vol=current_vol,
                    entry_time=open_time
                )
                close_monitor_row = self._build_close_position_monitor(
                    symbol=str(sym),
                    side=str(side),
                    score_now=float(sc),
                    p_trend=float(p_trend_val),
                    current_price=float(current_price),
                    open_price=float(open_price),
                    open_time=float(open_time),
                    now_ts=float(now_ts),
                    hold_policy=hold_policy,
                    exit_score_threshold_eff=float(exit_score_threshold_eff),
                )
                close_monitor_positions.append(close_monitor_row)
                
                # Check Risk Exits
                should_close_risk, reason_risk = self.risk_manager.check_exit(
                    sym,
                    current_price,
                    current_vol,
                    p_trend_val,
                    now_ts,
                    min_hold_secs_override=float(hold_policy.get("min_hold_secs", self.risk_manager.min_hold_secs)),
                    time_limit_hours_override=float(
                        hold_policy.get("time_limit_hours", self.risk_manager.time_limit_hours)
                    ),
                    stagnation_minutes_override=float(
                        hold_policy.get("stagnation_minutes", self.risk_manager.stagnation_minutes)
                    ),
                    regime_exit_th_override=float(
                        hold_policy.get("regime_exit_th", self.risk_manager.regime_exit_th)
                    ),
                )
                
                if should_close_risk:
                    logger.info(f"RISK EXIT {sym}: {reason_risk}")
                    
                    # VISUALS: Risk Exit Arrow
                    try:
                        post_visuals({
                            "symbol": sym,
                            "type": "arrow", 
                            "side": "EXIT", # Visualizer needs to handle EXIT (or use BUY/SELL opposite)
                            # Actually, let's use a distinct color or shape? 
                            # MQL4 Visualizer uses Wingdings. 
                            # Let's send "CLOSE" as side, and handle it in MQL4 to draw an "X" or similar.
                            "price": current_price,
                            "time": int(now_ts),
                            "color": "Orange",
                            "text": "Risk Exit"
                        })
                    except Exception as exc:
                        logger.warning(f"{sym}: failed to post risk-exit visual ({exc})")

                    cmd_ok, cmd_reason = self._consume_command_budget(is_entry=False)
                    if not cmd_ok:
                        self._log_rejection(cmd_reason)
                        _emit_exit("rejected", str(cmd_reason), side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = f"rejected:{cmd_reason}"
                        continue
                    close_ok = bool(bridge_client.close_position(sym, magic=p_magic))
                    if not close_ok:
                        self._record_close_failure("bridge_reject")
                        self._log_rejection("close_cmd_failed")
                        _emit_exit("rejected", "close_cmd_failed", side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = "rejected:close_cmd_failed"
                    else:
                        _emit_exit("sent", str(reason_risk), side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = f"close_sent:{reason_risk}"
                        self.soft_reversal_persistence[str(sym).upper()] = 0
                    continue

                # --- SIGNAL REVERSAL EXIT ---
                # If we hold a position but the signal has flipped strongly against us, close it.
                # This catches V-reversals where the trend changes before the stop.
                aligned_sharpe_exit = float(
                    self._directional_sharpe(sc, float(diag.get("predictive_sharpe", 0.0)))
                )
                soft_reversal_ratio = abs(float(sc)) / max(float(exit_score_threshold_eff), 1e-9)
                soft_reversal_hold_secs = float(max(self.soft_reversal_exit_min_hold_hours, 0.0) * 3600.0)
                soft_reversal_opposite = (
                    (side == "BUY" and sc < -float(self.score_zero_epsilon))
                    or (side == "SELL" and sc > float(self.score_zero_epsilon))
                )
                soft_reversal_target_side = "SELL" if side == "BUY" else "BUY"
                horizon_side_now = str(diag.get("horizon_side", "NONE")).upper()
                horizon_conf_now = float(np.clip(diag.get("horizon_confidence", 0.0), 0.0, 1.0))
                horizon_supports_reversal = bool(
                    (horizon_side_now == soft_reversal_target_side and horizon_conf_now >= 0.35)
                    or horizon_conf_now >= 0.80
                )
                unrealized_pnl_cash = float(pos.get("profit", 0.0) or 0.0)
                soft_reversal_loss_ok = bool(
                    unrealized_pnl_cash <= float(self.soft_reversal_exit_loss_threshold)
                )
                sym_key = str(sym).upper()
                prev_persist = int(self.soft_reversal_persistence.get(sym_key, 0))
                soft_reversal_persist_ok_now = bool(
                    soft_reversal_opposite
                    and hold_duration >= soft_reversal_hold_secs
                    and soft_reversal_ratio >= float(self.soft_reversal_exit_score_ratio)
                    and aligned_sharpe_exit >= float(self.soft_reversal_exit_min_aligned_sharpe)
                    and horizon_supports_reversal
                )
                if soft_reversal_persist_ok_now and soft_reversal_loss_ok:
                    persist_count = prev_persist + 1
                else:
                    persist_count = 0
                self.soft_reversal_persistence[sym_key] = int(persist_count)
                soft_reversal_persistence_ok = bool(
                    persist_count >= int(self.soft_reversal_exit_persistence_cycles)
                )
                if close_monitor_row is not None:
                    close_monitor_row["soft_reversal_persistence"] = int(persist_count)
                    close_monitor_row["soft_reversal_persistence_needed"] = int(
                        self.soft_reversal_exit_persistence_cycles
                    )
                    close_monitor_row["soft_reversal_loss_ok"] = bool(soft_reversal_loss_ok)
                    close_monitor_row["soft_reversal_unrealized_pnl"] = float(unrealized_pnl_cash)
                if (
                    self.soft_reversal_exit_enabled
                    and self.entry_gate_mode == "soft"
                    and soft_reversal_persistence_ok
                    and soft_reversal_loss_ok
                ):
                    logger.info(
                        f"SOFT REVERSAL EXIT {sym}: ratio={soft_reversal_ratio:.2f} "
                        f"(min {self.soft_reversal_exit_score_ratio:.2f}) "
                        f"sharpeX={aligned_sharpe_exit:.2f} hold={hold_duration/3600.0:.1f}h "
                        f"persist={persist_count}/{self.soft_reversal_exit_persistence_cycles} "
                        f"pnl={unrealized_pnl_cash:.2f}"
                    )
                    self.recent_reversals[sym] = time.time()
                    cmd_ok, cmd_reason = self._consume_command_budget(is_entry=False)
                    if not cmd_ok:
                        self._log_rejection(cmd_reason)
                        _emit_exit("rejected", str(cmd_reason), side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = f"rejected:{cmd_reason}"
                        continue
                    close_ok = bool(bridge_client.close_position(sym, magic=p_magic))
                    if not close_ok:
                        self._record_close_failure("bridge_reject")
                        self._log_rejection("close_cmd_failed")
                        _emit_exit("rejected", "close_cmd_failed", side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = "rejected:close_cmd_failed"
                    else:
                        _emit_exit("sent", "soft_reversal_exit", side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = "close_sent:soft_reversal_exit"
                    self.soft_reversal_persistence[sym_key] = 0
                    continue
                
                # For SHORT (SELL) positions:
                # If New Score > +Threshold (Strong Buy Signal) -> Close Short
                if side == "SELL" and sc > exit_score_threshold_eff:
                    logger.info(
                        f"REVERSAL EXIT {sym}: Score {sc:.2f} > {exit_score_threshold_eff:.2f} "
                        f"(Bullish Reversal, h={float(diag.get('primary_horizon_hours', 0.0)):.0f}h)"
                    )
                    
                    # VISUALS: Reversal Exit
                    try:
                        post_visuals({
                            "symbol": sym,
                            "type": "arrow",
                            "side": "EXIT",
                            "price": current_price,
                            "time": int(time.time()),
                            "color": "Blue",
                            "text": "Rev Exit"
                        })
                    except Exception as exc:
                        logger.warning(f"{sym}: failed to post reversal-exit visual ({exc})")
                    
                    self.recent_reversals[sym] = time.time()
                    cmd_ok, cmd_reason = self._consume_command_budget(is_entry=False)
                    if not cmd_ok:
                        self._log_rejection(cmd_reason)
                        _emit_exit("rejected", str(cmd_reason), side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = f"rejected:{cmd_reason}"
                        continue
                    close_ok = bool(bridge_client.close_position(sym, magic=p_magic))
                    if not close_ok:
                        self._record_close_failure("bridge_reject")
                        self._log_rejection("close_cmd_failed")
                        _emit_exit("rejected", "close_cmd_failed", side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = "rejected:close_cmd_failed"
                    else:
                        _emit_exit("sent", "reversal_exit", side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = "close_sent:reversal_exit"
                        self.soft_reversal_persistence[str(sym).upper()] = 0
                    continue
                
                # For LONG (BUY) positions:
                # If New Score < -Threshold (Strong Sell Signal) -> Close Long
                if side == "BUY" and sc < -exit_score_threshold_eff:
                    logger.info(
                        f"REVERSAL EXIT {sym}: Score {sc:.2f} < -{exit_score_threshold_eff:.2f} "
                        f"(Bearish Reversal, h={float(diag.get('primary_horizon_hours', 0.0)):.0f}h)"
                    )
                    
                    # VISUALS: Reversal Exit
                    try:
                        post_visuals({
                            "symbol": sym,
                            "type": "arrow",
                            "side": "EXIT",
                            "price": current_price,
                            "time": int(time.time()),
                            "color": "Blue",
                            "text": "Rev Exit"
                        })
                    except Exception as exc:
                        logger.warning(f"{sym}: failed to post reversal-exit visual ({exc})")
                    
                    self.recent_reversals[sym] = time.time()
                    cmd_ok, cmd_reason = self._consume_command_budget(is_entry=False)
                    if not cmd_ok:
                        self._log_rejection(cmd_reason)
                        _emit_exit("rejected", str(cmd_reason), side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = f"rejected:{cmd_reason}"
                        continue
                    close_ok = bool(bridge_client.close_position(sym, magic=p_magic))
                    if not close_ok:
                        self._record_close_failure("bridge_reject")
                        self._log_rejection("close_cmd_failed")
                        _emit_exit("rejected", "close_cmd_failed", side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = "rejected:close_cmd_failed"
                    else:
                        _emit_exit("sent", "reversal_exit", side)
                        if close_monitor_row is not None:
                            close_monitor_row["last_action"] = "close_sent:reversal_exit"
                        self.soft_reversal_persistence[str(sym).upper()] = 0
                    continue



            except Exception as e:
                logger.error(f"Error in risk update for {sym}: {e}")
                _emit_exit("rejected", "exit_exception")
                if close_monitor_row is not None:
                    close_monitor_row["last_action"] = "rejected:exit_exception"

            # --- COMPLETED RISK & OVERLORD CHECKS ---
            # Legacy Score Reversal removed to prevent churn.
            # We rely on Overlord (Regime Guard) and Risk Manager (R-Multiples).
            pass
        self.monitor_close_positions = sorted(
            close_monitor_positions,
            key=lambda row: float(row.get("close_proximity_pct", 0.0)),
            reverse=True,
        )
        self.monitor_last_cycle_ts = float(time.time())

    def _auto_tune_parameters(self, vol_now: float) -> None:
        """
        Adjusts Score Threshold and Trailing Stop based on Volatility Regime.
        """
        vol_1m_ref = self.vol_ref_runtime
        if not np.isfinite(vol_now) or vol_now <= 0:
            vol_now = vol_1m_ref

        high_score_mult = float(self.cfg.get("high_vol_score_mult", 0.85))
        low_score_mult = float(self.cfg.get("low_vol_score_mult", 1.25))
        high_trail_mult = float(
            self.cfg.get("high_vol_trailing_mult", max(1.5, self.base_trailing_mult * 0.75))
        )
        low_trail_mult = float(
            self.cfg.get("low_vol_trailing_mult", self.base_trailing_mult * 1.25)
        )

        if vol_now > (vol_1m_ref * 2.5):  # High volatility
            next_score_th = max(float(self.auto_tune_min_score_threshold), self.base_score_th * high_score_mult)
            next_trailing = high_trail_mult
            mode = "High Vol"
        elif vol_now < (vol_1m_ref * 0.8):  # Low volatility
            next_score_th = min(1.0, self.base_score_th * low_score_mult)
            next_trailing = low_trail_mult
            mode = "Low Vol"
        else:
            next_score_th = self.base_score_th
            next_trailing = self.base_trailing_mult
            mode = "Normal"

        if abs(self.score_th - next_score_th) > 1e-6:
            logger.info(
                f"Auto-Tune: {mode} ({vol_now*10000:.1f}bps). "
                f"score_th {self.score_th:.3f}->{next_score_th:.3f}, "
                f"trail {self.risk_manager.trailing_mult:.2f}->{next_trailing:.2f}"
            )
            self.score_th = float(next_score_th)
            self.risk_manager.trailing_mult = float(next_trailing)

    def act(self, equity: float, market_data: dict[str, pd.DataFrame], *,
            all_symbols_catalog: list[str]) -> None:
        """
        Build mini-only universe, compute targets, apply cost gates, and send commands.
        """
        import time
        
        self.equity = equity  # Store for dashboard
        self.cycle_id = int(self.cycle_id) + 1
        self.rejection_stats_cycle = {}
        if not self.use_hawkes_gate:
            self.rejection_stats.pop("hawkes_crowding", None)
        universe = self.build_universe(all_symbols_catalog)
        md = {s: market_data.get(s) for s in universe if s in market_data}
        if self.startup_warmup_strategy == "live":
            self._refresh_startup_warmup(md)
        self._refresh_startup_backfill_state(md)
        self._run_startup_backward_replay(md)
        self._refresh_startup_backfill_state(md)
        self._apply_bridge_safety_degrade()
        self._update_daily_loss_breaker(float(equity))
        effective_execution_mode = str(self.execution_mode)
        if (
            effective_execution_mode == "full_live"
            and int(self.bridge_safety_close_only_until_cycle) > int(self.cycle_id)
        ):
            effective_execution_mode = "close_only"

        # realisation vol for dynamic target scaling — average across chosen minis
        # Calculated early for Optimization
        vol_now = self.vol_ref
        try:
            # Need 'realised_vol' - assuming it's available or use std
            vols = []
            for df in md.values():
                if df is not None and not df.empty:
                    ret = np.log(df["close"]).diff()
                    vols.append(float(ret.std(ddof=0)))
            if vols:
                vol_now = float(np.nanmean(vols))
        except Exception as exc:
            logger.debug(f"Failed to compute volatility snapshot: {exc}")

        # --- AUTO-TUNE PARAMETERS ---
        now = time.time()
        if now - self.last_optimization_time > 60: # Check volatility every minute
            self._auto_tune_parameters(vol_now)
            self.last_optimization_time = now
        # -----------------------------

        target_pct = (
            dynamic_target_pct(vol_now, self.vol_ref_runtime, self.target_base)
            if self.use_dyn_target
            else self.target_base
        )

        # 1. Get Current Positions FIRST
        open_positions = get_positions()
        # [ {'symbol': 'eurusd', 'lots': 0.1, ...}, ... ]
        
        # Normalize held symbols to match MD keys (assumed uppercase or matched via build_universe)
        # But safest is to normalize both sides to upper for the check
        held_symbols_raw = {str(p.get("symbol")) for p in open_positions if p.get("symbol")}
        held_symbols_norm = set(s.upper() for s in held_symbols_raw)
        self._cleanup_pending_entries(held_symbols_norm, time.time())
        
        # 2. Score all symbols (pass held for HUD priority)
        # We pass normalized symbols to decisions so HUD lookup works with MD keys
        decs = self.decisions(md, held_symbols_norm) 
        
        # 3. Split decisions:
        # - fresh entries (not currently held)
        # - held symbols (eligible only for winner-add logic after exit management)
        fresh_entry_decs: list[Decision] = []
        held_symbol_decs: list[Decision] = []
        for d in decs:
            if d.symbol.upper() in held_symbols_norm:
                held_symbol_decs.append(d)
            else:
                if self._has_pending_entry(d.symbol, d.side, time.time()):
                    self._log_rejection("pending_entry_sync")
                    continue
                fresh_entry_decs.append(d)
        
        # 4. Manage Exits (Risk Manager + Reversals)
        self._manage_exits(open_positions, md)

        # Refresh positions after exits, then compute portfolio risk state.
        try:
            open_positions = get_positions(max_retries=1)
        except Exception:
            pass
        held_symbols_raw = {str(p.get("symbol")) for p in open_positions if p.get("symbol")}
        held_symbols_norm = set(s.upper() for s in held_symbols_raw)
        self._cleanup_pending_entries(held_symbols_norm, time.time())
        corr_clusters = self._build_corr_clusters(md, self.cluster_corr_threshold)
        self.portfolio_risk_state = self._portfolio_risk_snapshot(
            open_positions=open_positions,
            md=md,
            equity=float(equity),
            cluster_map=corr_clusters,
        )
        top_for_env = dict(getattr(self, "last_best_candidate", {}) or {})
        p_trend_hint = 0.5
        try:
            p_trend_hint = float(top_for_env.get("p_trend", 0.5))
        except Exception:
            p_trend_hint = 0.5
        gov_state = self._update_governance_state(
            float(equity),
            volatility=float(vol_now),
            trend_prob=float(np.clip(p_trend_hint, 0.0, 1.0)),
        )
        # Re-check daily breaker after adaptive envelope updates threshold bands.
        self._update_daily_loss_breaker(float(equity))
        if self.daily_breaker_active:
            gov_state = dict(gov_state)
            gov_state["paused"] = True
            gov_state["risk_scale"] = 0.0
            reasons = list(gov_state.get("reasons", []) or [])
            reasons.append("daily_loss_breaker")
            gov_state["reasons"] = list(dict.fromkeys(reasons))
            self.governance_state = dict(gov_state)
        self.current_risk_per_trade = float(self.base_risk_per_trade) * float(gov_state.get("risk_scale", 1.0))

        candidate_map = dict(getattr(self, "last_candidate_map", {}) or {})
        pos_summary = self._summarize_positions_by_symbol(open_positions)
        winner_add_decs: list[Decision] = []
        for d in held_symbol_decs:
            sym_key = str(d.symbol).upper()
            pos_row = pos_summary.get(sym_key)
            if not pos_row:
                self._log_rejection("winner_add_missing_pos")
                continue
            df = md.get(d.symbol)
            if df is None or df.empty:
                self._log_rejection("winner_add_missing_md")
                continue
            current_price = float(df["close"].iloc[-1])
            candidate_row = dict(candidate_map.get(self._candidate_key(d.symbol, d.side), {}) or {})
            allowed_add, reason_add = self._winner_add_check(
                decision=d,
                candidate_row=candidate_row,
                pos_row=pos_row,
                current_price=current_price,
            )
            if not allowed_add:
                self._log_rejection(reason_add)
                continue
            d.is_add = True
            if "WinnerAdd" not in d.reason:
                d.reason = f"{d.reason} | WinnerAdd"
            winner_add_decs.append(d)

        decs_for_exec = list(fresh_entry_decs) + winner_add_decs
        if bool(gov_state.get("paused", False)):
            if self.last_best_candidate:
                paused_row = dict(self.last_best_candidate)
                prev_blockers = str(paused_row.get("blocked_by_all", "none"))
                paused_row["blocked_by"] = "governance_pause"
                if prev_blockers and prev_blockers != "none":
                    paused_row["blocked_by_all"] = f"{prev_blockers},governance_pause"
                else:
                    paused_row["blocked_by_all"] = "governance_pause"
                paused_row["entry_gate_mode"] = "hard"
                self.last_best_candidate = paused_row
                if self.last_candidates:
                    self.last_candidates[0] = dict(paused_row)
            for _ in decs_for_exec:
                self._log_rejection("governance_pause")
            for d in decs_for_exec:
                self._audit_emit_row(
                    {
                        "phase": "execution",
                        "symbol": str(getattr(d, "symbol", "")),
                        "side": str(getattr(d, "side", "")),
                        "rejection_reason": "governance_pause",
                        "outcome": "rejected",
                    }
                )
            decs_for_exec = []
        startup_block_reason = self._startup_entry_block_reason()
        if startup_block_reason:
            if self.last_best_candidate:
                warmup_row = dict(self.last_best_candidate)
                prev_blockers = str(warmup_row.get("blocked_by_all", "none"))
                warmup_row["blocked_by"] = str(startup_block_reason)
                if prev_blockers and prev_blockers != "none":
                    warmup_row["blocked_by_all"] = f"{prev_blockers},{startup_block_reason}"
                else:
                    warmup_row["blocked_by_all"] = str(startup_block_reason)
                self.last_best_candidate = warmup_row
                if self.last_candidates:
                    self.last_candidates[0] = dict(warmup_row)
            for _ in decs_for_exec:
                self._log_rejection(str(startup_block_reason))
            for d in decs_for_exec:
                self._audit_emit_row(
                    {
                        "phase": "execution",
                        "symbol": str(getattr(d, "symbol", "")),
                        "side": str(getattr(d, "side", "")),
                        "rejection_reason": str(startup_block_reason),
                        "outcome": "rejected",
                        "warmup_mode": True,
                        "warmup_strategy": str(self.startup_warmup_strategy),
                        "startup_backfill_pending": bool(self.startup_backfill_pending_active),
                        "startup_backfill_ready": bool(self.startup_backfill_ready_active),
                        "startup_backfill_bars": int(self.startup_backfill_bars_active),
                        "startup_backfill_retry_age_secs": float(self.startup_backfill_retry_age_secs_active),
                        "startup_backward_replay_done": bool(self.startup_backward_replay_done_active),
                    }
                )
            decs_for_exec = []
        if effective_execution_mode == "close_only":
            for _ in decs_for_exec:
                if int(self.bridge_safety_close_only_until_cycle) > int(self.cycle_id):
                    self._log_rejection("bridge_safety_close_only")
                else:
                    self._log_rejection("execution_mode_close_only")
            for d in decs_for_exec:
                self._audit_emit_row(
                    {
                        "phase": "execution",
                        "symbol": str(getattr(d, "symbol", "")),
                        "side": str(getattr(d, "side", "")),
                        "rejection_reason": (
                            "bridge_safety_close_only"
                            if int(self.bridge_safety_close_only_until_cycle) > int(self.cycle_id)
                            else "execution_mode_close_only"
                        ),
                        "outcome": "rejected",
                    }
                )
            decs_for_exec = []
        if effective_execution_mode == "read_only":
            for _ in decs_for_exec:
                self._log_rejection("execution_mode_read_only")
            for d in decs_for_exec:
                self._audit_emit_row(
                    {
                        "phase": "execution",
                        "symbol": str(getattr(d, "symbol", "")),
                        "side": str(getattr(d, "side", "")),
                        "rejection_reason": "execution_mode_read_only",
                        "outcome": "rejected",
                    }
                )
            decs_for_exec = []

        # 5. Execute New Trades (dashboard posted after execution gating for accurate live state)
        executed_decs: list[Decision] = []
        if not decs_for_exec:
            self._update_starvation_state(0)
            self._post_decisions_to_dashboard(executed_decs, md, vol_now, target_pct)
            self._persist_direction_state(force=False)
            self._persist_ai_indicator_state(force=False)
            self._flush_audit_rows()
            return

        total_risk_used = float(self.portfolio_risk_state.get("total_risk_pct", 0.0))
        cluster_risk_used = dict(self.portfolio_risk_state.get("cluster_risk_pct", {}) or {})
        cluster_map = dict(self.portfolio_risk_state.get("cluster_map", {}) or {})
        for d in decs_for_exec:
            try:
                df = md[d.symbol]
                current_price = float(df["close"].iloc[-1])
                candidate_row = dict(candidate_map.get(self._candidate_key(d.symbol, d.side), {}) or {})
                entry_blockers = str(candidate_row.get("blocked_by_all", "none"))
                conf_now = float(getattr(d, "confidence", 0.0))
                score_ratio_now = float(getattr(d, "score_ratio", 0.0))
                score_ratio_exec_now = float(score_ratio_now)
                sharpe_ratio_now = 0.0
                cost_ratio_now = 0.0
                raw_signal_now = 0.0
                exec_score_basis_now = "score_ratio"
                zero_score_collapse_now = False
                gap_recovery_source_now = "none"
                gap_fill_truncated_now = False
                warmup_strategy_now = str(self.startup_warmup_strategy)
                startup_backfill_pending_now = False
                startup_backfill_ready_now = False
                startup_backfill_bars_now = 0
                startup_backfill_retry_age_now = 0.0
                startup_backward_replay_done_now = False
                dyn_min_score_ratio = float(self.exec_min_score_ratio)
                lot_pre_floor = 0.0
                lot_post_floor = 0.0

                def _emit_exec(outcome: str, rejection_reason: str = "") -> None:
                    self._audit_emit_row(
                        {
                            "phase": "execution",
                            "symbol": str(d.symbol),
                            "side": str(d.side),
                            "score_raw": float(candidate_row.get("score_raw", getattr(d, "score", 0.0))),
                            "score_effective": float(
                                candidate_row.get("score_effective", candidate_row.get("score", getattr(d, "score", 0.0)))
                            ),
                            "gate_penalty": float(candidate_row.get("gate_penalty", 1.0)),
                            "blockers": str(entry_blockers),
                            "entry_blockers": str(entry_blockers),
                            "entry_ready": bool(candidate_row.get("entry_ready", True)),
                            "exec_quality_ready": bool(candidate_row.get("exec_quality_ready", True)),
                            "execution_ready": bool(candidate_row.get("execution_ready", True)),
                            "lot_pre_floor": float(lot_pre_floor),
                            "lot_post_floor": float(lot_post_floor),
                            "rejection_reason": str(rejection_reason),
                            "outcome": str(outcome),
                            "confidence_exec": float(
                                candidate_row.get("confidence_exec", candidate_row.get("confidence", conf_now))
                            ),
                            "raw_signal": float(raw_signal_now),
                            "score_ratio": float(candidate_row.get("score_ratio", score_ratio_now)),
                            "score_ratio_exec": float(score_ratio_exec_now),
                            "exec_min_score_ratio_dynamic": float(dyn_min_score_ratio),
                            "exec_score_ratio_soft_bypass": bool(
                                candidate_row.get("exec_score_ratio_soft_bypass", False)
                            ),
                            "exec_score_basis": str(exec_score_basis_now),
                            "sharpe_ratio": float(candidate_row.get("sharpe_ratio", sharpe_ratio_now)),
                            "cost_ratio": float(candidate_row.get("cost_ratio", cost_ratio_now)),
                            "zero_score_collapse": bool(zero_score_collapse_now),
                            "gap_recovery_source": str(gap_recovery_source_now),
                            "gap_fill_truncated": bool(gap_fill_truncated_now),
                            "warmup_mode": bool(self._warmup_mode_active()),
                            "warmup_strategy": str(warmup_strategy_now),
                            "startup_backfill_pending": bool(startup_backfill_pending_now),
                            "startup_backfill_ready": bool(startup_backfill_ready_now),
                            "startup_backfill_bars": int(startup_backfill_bars_now),
                            "startup_backfill_retry_age_secs": float(startup_backfill_retry_age_now),
                            "startup_backward_replay_done": bool(startup_backward_replay_done_now),
                            "starvation_mode": bool(self.starvation_mode_active),
                            "relax_level": float(self.starvation_relax_level),
                            "fallback_path": str(candidate_row.get("fallback_path", "none")),
                            "is_add": bool(getattr(d, "is_add", False)),
                        }
                    )
                if candidate_row:
                    conf_now = float(
                        candidate_row.get(
                            "confidence_exec",
                            candidate_row.get("confidence_raw", candidate_row.get("confidence", conf_now)),
                        )
                    )
                    score_ratio_now = float(candidate_row.get("score_ratio", score_ratio_now))
                    score_ratio_exec_now = float(candidate_row.get("score_ratio_exec", score_ratio_now))
                    sharpe_ratio_now = float(candidate_row.get("sharpe_ratio", 0.0))
                    cost_ratio_now = float(candidate_row.get("cost_ratio", 0.0))
                    raw_signal_now = float(candidate_row.get("raw_signal", 0.0))
                    exec_score_basis_now = str(candidate_row.get("exec_score_basis", "score_ratio"))
                    zero_score_collapse_now = bool(candidate_row.get("zero_score_collapse", False))
                    gap_recovery_source_now = str(candidate_row.get("gap_recovery_source", "none"))
                    gap_fill_truncated_now = bool(candidate_row.get("gap_fill_truncated", False))
                    warmup_strategy_now = str(candidate_row.get("warmup_strategy", self.startup_warmup_strategy))
                    startup_backfill_pending_now = bool(candidate_row.get("startup_backfill_pending", False))
                    startup_backfill_ready_now = bool(candidate_row.get("startup_backfill_ready", False))
                    startup_backfill_bars_now = int(candidate_row.get("startup_backfill_bars", 0))
                    startup_backfill_retry_age_now = float(candidate_row.get("startup_backfill_retry_age_secs", 0.0))
                    startup_backward_replay_done_now = bool(candidate_row.get("startup_backward_replay_done", False))
                    if self.use_execution_quality_gate:
                        blockers_text = str(candidate_row.get("blocked_by_all", candidate_row.get("blocked_by", "none")))
                        dyn_min_score_ratio = self._dynamic_exec_min_score_ratio(
                            confidence_exec=conf_now,
                            sharpe_ratio=sharpe_ratio_now,
                            cost_ratio=cost_ratio_now,
                            blockers=blockers_text,
                        )
                        if conf_now < float(self.exec_min_confidence):
                            self._log_rejection("exec_low_confidence")
                            _emit_exec("rejected", "exec_low_confidence")
                            continue
                        if score_ratio_exec_now < float(dyn_min_score_ratio):
                            if self._allow_soft_exec_low_score_bypass(
                                blockers=blockers_text,
                                confidence_exec=conf_now,
                                sharpe_ratio=sharpe_ratio_now,
                                cost_ratio=cost_ratio_now,
                                score_ratio_exec=score_ratio_exec_now,
                            ):
                                dyn_min_score_ratio = min(
                                    float(dyn_min_score_ratio),
                                    float(np.clip(self.exec_min_score_ratio_soft, 0.0, 1.50)),
                                )
                                candidate_row["exec_score_ratio_soft_bypass"] = True
                                candidate_row["exec_min_score_ratio_dynamic"] = float(dyn_min_score_ratio)
                            else:
                                self._log_rejection("exec_low_score_ratio")
                                _emit_exec("rejected", "exec_low_score_ratio")
                                continue
                        if sharpe_ratio_now < float(self.exec_min_sharpe_ratio):
                            self._log_rejection("exec_low_sharpe_ratio")
                            _emit_exec("rejected", "exec_low_sharpe_ratio")
                            continue
                
                # Use Risk Utils for Sizing
                from .risk_utils import calculate_position_size
                
                local_vol = realised_vol(np.log(df["close"]).diff())
                if not np.isfinite(local_vol) or local_vol <= 0:
                    local_vol = max(self.vol_ref_runtime, 1e-5)

                pip_value_symbol = self._pip_value_per_standard_lot(d.symbol, current_price, md)
                min_lot_effective = self._effective_min_trade_lot(
                    equity=float(equity),
                    pip_value_symbol=pip_value_symbol,
                )
                
                # Stop Loss
                sl_dist = current_price * local_vol * 2.0
                
                # Estimate pip size
                is_jpy = "JPY" in d.symbol
                pip_size = 0.01 if is_jpy else 0.0001
                sl_pips_scalar = sl_dist / pip_size
                
                symbol_cluster = cluster_map.get(
                    d.symbol, cluster_map.get(str(d.symbol).upper(), f"SINGLE:{str(d.symbol).upper()}")
                )

                risk_scale_exec = self._execution_risk_scale(candidate_row)
                base_trade_risk_pct = float(max(self.current_risk_per_trade * risk_scale_exec, 0.0))
                if getattr(d, "is_add", False):
                    base_trade_risk_pct *= float(self.winner_add_size_mult)
                allowed_risk_pct = base_trade_risk_pct
                if self.use_portfolio_risk_budget:
                    remaining_portfolio = float(self.portfolio_risk_cap_pct) - float(total_risk_used)
                    remaining_cluster = float(self.cluster_risk_cap_pct) - float(
                        cluster_risk_used.get(symbol_cluster, 0.0)
                    )
                    if remaining_portfolio <= 0:
                        self._log_rejection("portfolio_risk_cap")
                        _emit_exec("rejected", "portfolio_risk_cap")
                        continue
                    if remaining_cluster <= 0:
                        self._log_rejection("cluster_risk_cap")
                        _emit_exec("rejected", "cluster_risk_cap")
                        continue
                    allowed_risk_pct = min(base_trade_risk_pct, remaining_portfolio, remaining_cluster)
                    if allowed_risk_pct < self.portfolio_min_trade_risk_pct:
                        self._log_rejection("portfolio_risk_budget_thin")
                        _emit_exec("rejected", "portfolio_risk_budget_thin")
                        continue
                elif allowed_risk_pct <= 0:
                    self._log_rejection("risk_scale_zero")
                    _emit_exec("rejected", "risk_scale_zero")
                    continue

                lot_size = calculate_position_size(
                    equity,
                    allowed_risk_pct,
                    sl_pips_scalar,
                    pip_value_symbol,
                )
                lot_pre_floor = float(max(lot_size, 0.0))
                
                # Leverage Cap
                max_exposure = equity * self.leverage * self.max_margin_pct
                notional_per_lot = self._estimate_notional_per_lot(d.symbol, current_price, md)
                max_lots_lev = max_exposure / max(notional_per_lot, 1e-9)
                
                if lot_size > max_lots_lev:
                    lot_size = max_lots_lev

                # Enforce minimum margin usage per trade via a max margin-level target.
                lot_floor_margin = self._lot_floor_for_margin_level(equity, d.symbol, current_price, md)
                if lot_floor_margin > 0 and lot_size < lot_floor_margin:
                    lot_size = lot_floor_margin
                    self._log_rejection("margin_level_floor")
                
                if lot_size < min_lot_effective:
                    lot_size = min_lot_effective
                    self._log_rejection("min_lot_floor")

                # Quantize up so EA-side floor rounding does not undershoot requested minimum.
                lot_size = math.ceil(lot_size / self.lot_step_hint) * self.lot_step_hint

                # Respect leverage cap after quantization/floors.
                if lot_size > max_lots_lev:
                    max_lots_q = math.floor(max_lots_lev / self.lot_step_hint) * self.lot_step_hint
                    if max_lots_q < min_lot_effective:
                        self._log_rejection("margin_cap")
                        _emit_exec("rejected", "margin_cap")
                        continue
                    lot_size = max_lots_q
                    self._log_rejection("margin_floor_clipped")

                # Cost Gate
                expected_move = self._expected_move_fraction(abs(d.score), local_vol)
                real_spread = float(df.attrs.get("spread", self.avg_spread_pips))
                exec_cost_ok = cost_gate(
                    expected_move=expected_move,
                    spread_pips=real_spread if real_spread > 0 else self.avg_spread_pips,
                    pip_value_per_lot=pip_value_symbol,
                    equity=float(equity),
                    lot_fraction=max(lot_size, min_lot_effective),
                )
                if not exec_cost_ok:
                    if self.execution_gate_mode == "hard":
                        self._log_rejection("cost_gate")
                        _emit_exec("rejected", "cost_gate")
                        continue
                    spread_used = real_spread if real_spread > 0 else self.avg_spread_pips
                    est_cost = (
                        spread_used
                        * max(float(pip_value_symbol), 1e-9)
                        * max(float(lot_size), 1e-9)
                    ) / max(float(equity), 1e-9)
                    est_cost_req = 3.0 * est_cost
                    exec_ratio = float(expected_move) / max(est_cost_req, 1e-12)
                    lot_size *= float(np.clip(exec_ratio, 0.35, 1.0))
                    if lot_size < min_lot_effective:
                        lot_size = min_lot_effective
                    lot_size = math.ceil(lot_size / self.lot_step_hint) * self.lot_step_hint
                    self._log_rejection("soft_cost_gate")

                # Final guard: if a lot was shrunk by later gates, re-apply margin-level floor.
                if lot_floor_margin > 0 and lot_size < lot_floor_margin:
                    lot_size = math.ceil(lot_floor_margin / self.lot_step_hint) * self.lot_step_hint
                    if lot_size > max_lots_lev:
                        max_lots_q = math.floor(max_lots_lev / self.lot_step_hint) * self.lot_step_hint
                        if max_lots_q < min_lot_effective:
                            self._log_rejection("margin_level_target_unmet")
                            _emit_exec("rejected", "margin_level_target_unmet")
                            continue
                        lot_size = max_lots_q
                    if lot_size < lot_floor_margin:
                        self._log_rejection("margin_level_target_unmet")
                        _emit_exec("rejected", "margin_level_target_unmet")
                        continue
                    self._log_rejection("margin_level_floor_reapplied")

                # Final portfolio-risk cap after all lot adjustments.
                trade_risk_pct = (
                    max(float(sl_pips_scalar), 0.0)
                    * max(float(pip_value_symbol), 1e-9)
                    * max(float(lot_size), 0.0)
                ) / max(float(equity), 1e-9)
                if self.use_portfolio_risk_budget:
                    remaining_portfolio = float(self.portfolio_risk_cap_pct) - float(total_risk_used)
                    remaining_cluster = float(self.cluster_risk_cap_pct) - float(
                        cluster_risk_used.get(symbol_cluster, 0.0)
                    )
                    max_allowed_now = min(base_trade_risk_pct, remaining_portfolio, remaining_cluster)
                    if max_allowed_now <= 0:
                        self._log_rejection("portfolio_risk_cap")
                        _emit_exec("rejected", "portfolio_risk_cap")
                        continue
                    if trade_risk_pct > max_allowed_now:
                        scale = max_allowed_now / max(trade_risk_pct, 1e-12)
                        lot_scaled = lot_size * scale
                        lot_scaled_q = math.floor(lot_scaled / self.lot_step_hint) * self.lot_step_hint
                        if lot_scaled_q < min_lot_effective:
                            self._log_rejection("portfolio_min_lot_exceeds_budget")
                            _emit_exec("rejected", "portfolio_min_lot_exceeds_budget")
                            continue
                        lot_size = lot_scaled_q
                        trade_risk_pct = (
                            max(float(sl_pips_scalar), 0.0)
                            * max(float(pip_value_symbol), 1e-9)
                            * max(float(lot_size), 0.0)
                        ) / max(float(equity), 1e-9)
                        if trade_risk_pct > (max_allowed_now * 1.01):
                            self._log_rejection("portfolio_risk_requantize_fail")
                            _emit_exec("rejected", "portfolio_risk_requantize_fail")
                            continue
                        self._log_rejection("portfolio_risk_scaled")
                
                est_margin_used = (notional_per_lot * max(lot_size, 0.0)) / max(self.leverage, 1e-9)
                est_margin_level = (equity / max(est_margin_used, 1e-9)) * 100.0
                signal_kind = "ADD" if getattr(d, "is_add", False) else "SIGNAL"
                lot_post_floor = float(max(lot_size, 0.0))
                if self._has_pending_entry(d.symbol, d.side, time.time()):
                    self._log_rejection("pending_entry_sync")
                    _emit_exec("rejected", "pending_entry_sync")
                    continue
                cmd_ok, cmd_reason = self._consume_command_budget(is_entry=True)
                if not cmd_ok:
                    self._log_rejection(cmd_reason)
                    _emit_exec("rejected", str(cmd_reason))
                    continue
                logger.info(
                    f"{signal_kind} {d.side} {d.symbol} (Sc:{d.score:.2f}) -> "
                    f"Sending lots={lot_size:.2f}, estML={est_margin_level:.0f}%, "
                    f"estRisk={trade_risk_pct*100.0:.2f}%, Conf={conf_now:.0f}%, "
                    f"ScoreX={score_ratio_now:.2f}, SharpeX={sharpe_ratio_now:.2f}, rScale={risk_scale_exec:.2f}"
                )
                
                # VISUALS: Send Entry Arrow
                try:
                    import time
                    visual_data = {
                        "symbol": d.symbol,
                        "type": "arrow",
                        "side": d.side, # BUY or SELL
                        "price": current_price,
                        "time": int(time.time()),
                        "color": "Green" if d.side == "BUY" else "Red",
                        "text": f"Entry {d.score:.2f}"
                    }
                    post_visuals(visual_data)
                except Exception as exc:
                    logger.warning(f"{d.symbol}: failed to post entry visual ({exc})")

                tp_dist_r = sl_dist * max(float(self.risk_manager.target_r), 1.0)
                tp_dist_target = current_price * max(float(target_pct), 0.0)
                tp_dist = max(tp_dist_r, tp_dist_target)
                sl_price = current_price - sl_dist if d.side == "BUY" else current_price + sl_dist
                tp_price = current_price + tp_dist if d.side == "BUY" else current_price - tp_dist
                send(
                    d.side,
                    d.symbol,
                    lots=lot_size,
                    tp_cash=0.0,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    magic=246810,
                )
                self._mark_pending_entry(d.symbol, d.side, time.time())

                update_thought(f"{signal_kind}: {d.side} {d.symbol}")
                executed_decs.append(d)
                _emit_exec("executed", "")

                if self.use_portfolio_risk_budget:
                    total_risk_used += float(trade_risk_pct)
                    cluster_risk_used[symbol_cluster] = float(
                        cluster_risk_used.get(symbol_cluster, 0.0) + float(trade_risk_pct)
                    )
                    self.portfolio_risk_state["total_risk_pct"] = float(total_risk_used)
                    self.portfolio_risk_state["cluster_risk_pct"] = dict(cluster_risk_used)
                
                # Also trigger HUD update immediately for this new trade
                diag = getattr(self, "last_diagnostics", {})
                if getattr(d, "is_add", False):
                    self._send_hud(d.symbol, d.score, diag, df, action_label=f"Executing ADD {d.side}...")
                else:
                    self._send_hud(d.symbol, d.score, diag, df, action_label=f"Executing {d.side}...")

            except Exception as e:
                logger.error(f"Error executing trade for {d.symbol}: {e}")
                self._audit_emit_row(
                    {
                        "phase": "execution",
                        "symbol": str(getattr(d, "symbol", "")),
                        "side": str(getattr(d, "side", "")),
                        "rejection_reason": "execution_exception",
                        "outcome": "rejected",
                    }
                )
        self._update_starvation_state(len(executed_decs))
        self._post_decisions_to_dashboard(executed_decs, md, vol_now, target_pct)
        self._persist_direction_state(force=False)
        self._persist_ai_indicator_state(force=False)
        self._flush_audit_rows()
    
    def _post_decisions_to_dashboard(self, decisions: list[Decision], 
                                     md: dict[str, pd.DataFrame],
                                     vol_now: float, target_pct: float) -> None:
        """Post current decisions to bridge for dashboard display."""
        
        # Collect State from internal object state (using last known symbol or global)
        # Note: These components are per-symbol. We will show metrics for the last processed symbol 
        # or an average/representative state to fit on one dashboard.
        
        last_diag = getattr(self, "last_diagnostics", {})
        
        try:
            # Metric 1: Momentum (with EL, Sharpe, and confidence)
            score = last_diag.get("score", 0.0)
            el_score = last_diag.get("pz", 0.0)
            sharpe = last_diag.get("predictive_sharpe", 0.0)
            readiness = min(100.0, abs(score) / self.score_th * 100.0)
            mom_line = f"MOMENTUM: EL {el_score:.3f} | Sharpe {sharpe:.2f} | Ready {readiness:.0f}%"
            
            # Metric 2: Regime (trend/range from HMM + direction from EL)
            p_trend = last_diag.get("p_trend", 0.5)
            direction = "Bullish" if el_score > 0 else "Bearish"
            # --- DASHBOARD FORMATTING v2.2 ---
            # 1. Strategy Mode
            mode_str = "Aggressive" if self.score_th < 0.10 else ("Strict" if self.score_th >= 0.20 else "Normal")
            algo_line = f"STRATEGY: Algorithmic v2.2 | Mode: {mode_str} (Th={self.score_th:.2f})"
            
            # 2. Volatility State
            vol_bps = vol_now * 10000
            vol_ref_1m = self.vol_ref_runtime
            vol_state = "High" if vol_now > (vol_ref_1m * 2.5) else ("Low" if vol_now < (vol_ref_1m * 0.8) else "Normal")
            vol_line = f"VOLATILITY: {vol_bps:.0f} bps ({vol_state}) | Ref {(vol_ref_1m*10000):.0f} bps"
            
            # 3. Market Regime
            p_t = last_diag.get("p_trend", 0.5)
            regime = "Trend" if p_t > 0.6 else ("Range" if p_t < 0.4 else "Transition")
            score_best = max([abs(d.score) for d in decisions]) if decisions else 0.0
            regime_line = f"MARKET: {regime} (P_Trend {p_t:.2f}) | Top Score: {score_best:.2f}"
            
            # 4. Risk Status
            # Get position count safe
            pos_count = 0
            try:
                positions = get_positions(max_retries=1)
                pos_count = len(positions)
            except Exception as exc:
                logger.debug(f"Unable to fetch positions for dashboard: {exc}")
            
            risk_line = (
                f"RISK: {pos_count} Pos | Stop: {self.risk_manager.trailing_mult}x Vol | "
                f"Risk {self.current_risk_per_trade*100.0:.2f}% | MinLot>={self.min_trade_lot:.2f}"
            )
            if self.use_equity_scaled_pip_target and self.pip_value_target_pct_equity > 0:
                pip_target_cash = float(self.equity) * float(self.pip_value_target_pct_equity)
                risk_line += f" | PipTarget ${pip_target_cash:.2f}"
            if self.max_margin_level_per_trade_pct > 0:
                risk_line += f" | ML<={self.max_margin_level_per_trade_pct:.0f}%"
            if self.use_portfolio_risk_budget:
                p_state = dict(getattr(self, "portfolio_risk_state", {}) or {})
                p_used = float(p_state.get("total_risk_pct", 0.0)) * 100.0
                p_cap = float(self.portfolio_risk_cap_pct) * 100.0
                c_map = dict(p_state.get("cluster_risk_pct", {}) or {})
                c_used = (max(c_map.values()) if c_map else 0.0) * 100.0
                c_cap = float(self.cluster_risk_cap_pct) * 100.0
                risk_line += f" | Port {p_used:.2f}/{p_cap:.2f}% | ClusterMax {c_used:.2f}/{c_cap:.2f}%"
            gov = dict(getattr(self, "governance_state", {}) or {})
            if bool(gov.get("enabled", False)):
                dd = float(gov.get("drawdown_pct", 0.0)) * 100.0
                scale = float(gov.get("risk_scale", 1.0))
                status = "PAUSED" if bool(gov.get("paused", False)) else "LIVE"
                risk_line += f" | Gov {status} rX{scale:.2f} DD {dd:.2f}%"
            if self.use_execution_quality_gate:
                risk_line += (
                    f" | QGate C>{self.exec_min_confidence:.0f}% "
                    f"Sx>{self.exec_min_score_ratio:.2f} Shx>{self.exec_min_sharpe_ratio:.2f}"
                )
            if self.startup_warmup_strategy == "live" and self.startup_warmup_active:
                risk_line += (
                    f" | Warmup {int(self.startup_warmup_live_bars)}/{int(self.startup_warmup_min_live_bars)}"
                )
            elif self.startup_warmup_strategy == "backward_bridge":
                if self.startup_backfill_pending_active and self.startup_backfill_block_entries:
                    risk_line += (
                        f" | Backfill {self.startup_backfill_bars_active}b "
                        f"age {self.startup_backfill_retry_age_secs_active:.0f}s"
                    )
                else:
                    replay_state = "Y" if self.startup_backward_replay_done_active else "N"
                    risk_line += f" | BackfillReady {self.startup_backfill_bars_active}b Replay {replay_state}"
            if self.starvation_mode_active:
                risk_line += f" | Starve rL={self.starvation_relax_level:.2f}"
            if self.daily_breaker_active:
                risk_line += " | DailyBreaker ON"

            top_candidate = self._sanitize_candidate(getattr(self, "last_best_candidate", {}) or {})
            model_age_secs = 0.0
            if top_candidate:
                top_sym = str(top_candidate.get("symbol", "")).upper()
                if top_sym:
                    last_fit_ts = float(self.regime_last_fit_ts.get(top_sym, 0.0))
                    model_age_secs = max(
                        0.0,
                        float(time.time() - last_fit_ts),
                    ) if last_fit_ts > 0 else 0.0
            if top_candidate:
                conf_adj = float(top_candidate.get("confidence", 0.0))
                conf_raw = float(top_candidate.get("confidence_raw", conf_adj))
                conf_exec = float(top_candidate.get("confidence_exec", conf_adj))
                gate_pen = float(top_candidate.get("gate_penalty", 1.0))
                confidence_line = (
                    f"CONFIDENCE: {conf_adj:.1f}% (Raw {conf_raw:.1f} Exec {conf_exec:.1f} Pen {gate_pen:.2f}) | "
                    f"ScoreX {float(top_candidate.get('score_ratio', 0.0)):.2f} | "
                    f"SharpeX {float(top_candidate.get('sharpe_ratio', 0.0)):.2f} | "
                    f"CostX {float(top_candidate.get('cost_ratio', 0.0)):.2f} | "
                    f"DirHit {float(top_candidate.get('direction_hit_rate', 0.5))*100.0:.0f}% | "
                    f"Hor {float(top_candidate.get('primary_horizon_hours', self.hold_default_horizon_hours)):.0f}h | "
                    f"HConf {float(top_candidate.get('horizon_confidence', 0.0))*100.0:.0f}% | "
                    f"DFac {float(top_candidate.get('direction_factor', 1.0)):.2f} | "
                    f"ThX {float(top_candidate.get('score_threshold_total_mult', 1.0)):.2f} | "
                    f"U {float(top_candidate.get('utility', 0.0)):+.4f} | "
                    f"Pri {float(top_candidate.get('priority', top_candidate.get('score', 0.0))):.2f} | "
                    f"Reg {str(top_candidate.get('regime_bucket', '?'))} | "
                    f"Suppr {float(self.suppression_ratio_rolling)*100.0:.0f}% | "
                    f"TopRej {str(self.dominant_rejection_reason)} | "
                    f"Warmup {'ON' if self._warmup_mode_active() else 'OFF'} ({self.startup_warmup_strategy}) | "
                    f"ModelAge {model_age_secs:.0f}s"
                )
            else:
                confidence_line = "CONFIDENCE: n/a"
            
            # 5. Action
            if decisions:
                 act_txt = ", ".join(
                     [f"{d.symbol} {d.side}{' +ADD' if getattr(d, 'is_add', False) else ''}" for d in decisions[:3]]
                 )
                 action_line = f"ACTION: {act_txt}"
            else:
                 if top_candidate:
                     blocker = str(top_candidate.get("blocked_by", "none"))
                     blocker_all = str(top_candidate.get("blocked_by_all", blocker))
                     gate_mode = str(top_candidate.get("entry_gate_mode", self.entry_gate_mode))
                     conf_now = float(top_candidate.get("confidence", 0.0))
                     if blocker and blocker != "none":
                         if gate_mode == "hard":
                             action_line = (
                                 f"ACTION: Scanning... Blocked by {blocker} | "
                                 f"Closest {top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                                 f"({conf_now:.1f}%)"
                             )
                         else:
                             action_line = (
                                 f"ACTION: Scanning... Pressure {blocker_all} | "
                                 f"Closest {top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                                 f"({conf_now:.1f}%)"
                             )
                     else:
                         action_line = (
                             f"ACTION: Scanning... Closest "
                             f"{top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                             f"({conf_now:.1f}%)"
                         )
                 else:
                     action_line = "ACTION: Scanning..."

            if bool(gov.get("enabled", False)) and bool(gov.get("paused", False)):
                reason_txt = ",".join([str(x) for x in list(gov.get("reasons", []))[:2]]) or "governance"
                if top_candidate:
                    action_line = (
                        f"ACTION: Paused ({reason_txt}) | Closest "
                        f"{top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                        f"({float(top_candidate.get('confidence', 0.0)):.1f}%)"
                    )
                else:
                    action_line = f"ACTION: Paused ({reason_txt})"
            elif int(self.bridge_safety_close_only_until_cycle) > int(self.cycle_id):
                if top_candidate:
                    action_line = (
                        f"ACTION: Close-only (bridge_qos) | Closest "
                        f"{top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                        f"({float(top_candidate.get('confidence', 0.0)):.1f}%)"
                    )
                else:
                    action_line = "ACTION: Close-only (bridge_qos)"
            
            msg = (
                f"{algo_line}\n{vol_line}\n{regime_line}\n{risk_line}\n"
                f"{confidence_line}\n{action_line}\nEquity: ${self.equity:,.2f}"
            )
            
            logger.info(f"Dashboard Msg: {msg.replace(chr(10), ' | ')}")
            update_thought(msg)
            
        except Exception as e:
            logger.error(f"Dashboard update failed: {e}")
        """Post current decisions to bridge for dashboard display."""
        


        try:
            decisions_data = []
            for d in decisions:
                df = md.get(d.symbol)
                if df is not None and not df.empty:
                    close_price = float(df["close"].iloc[-1])
                    decisions_data.append({
                        "symbol": d.symbol,
                        "side": d.side,
                        "score": float(d.score),
                        "priority": float(getattr(d, "priority", d.score)),
                        "confidence": float(getattr(d, "confidence", 0.0)),
                        "reason": str(d.reason),
                        "is_add": bool(getattr(d, "is_add", False)),
                        "price": close_price,
                        "target_pct": float(target_pct)
                    })
            
            top_candidate = self._sanitize_candidate(getattr(self, "last_best_candidate", {}) or {})
            top_candidates = [
                self._sanitize_candidate(row)
                for row in list(getattr(self, "last_candidates", [])[:3])
            ]
            entry_monitor = self._build_entry_monitor(top_candidate)
            close_positions = list(getattr(self, "monitor_close_positions", []) or [])
            close_top = dict(close_positions[0]) if close_positions else {}
            monitor_payload = {
                "updated_ts": float(time.time()),
                "cycle_id": int(getattr(self, "cycle_id", 0)),
                "entry": entry_monitor,
                "close": {
                    "close_proximity_pct": float(close_top.get("close_proximity_pct", 0.0)),
                    "dominant_close_reason": str(close_top.get("dominant_close_reason", "none"))
                    if close_top
                    else "none",
                    "positions_open": int(len(close_positions)),
                    "positions": close_positions[:10],
                },
                "warmup_mode": bool(self._warmup_mode_active()),
                "warmup_strategy": str(self.startup_warmup_strategy),
                "startup_backfill_pending": bool(self.startup_backfill_pending_active),
                "startup_backfill_ready": bool(self.startup_backfill_ready_active),
                "startup_backfill_bars": int(self.startup_backfill_bars_active),
                "startup_backfill_retry_age_secs": float(self.startup_backfill_retry_age_secs_active),
                "startup_backward_replay_done": bool(self.startup_backward_replay_done_active),
                "starvation_mode": bool(self.starvation_mode_active),
                "relax_level": float(self.starvation_relax_level),
                "daily_breaker_active": bool(self.daily_breaker_active),
            }
            rejection_stats = dict(getattr(self, "rejection_stats_cycle", {}) or {})
            if not self.use_hawkes_gate:
                rejection_stats.pop("hawkes_crowding", None)
            rejection_stats_total = dict(self.rejection_stats)
            if not self.use_hawkes_gate:
                rejection_stats_total.pop("hawkes_crowding", None)
            top_rejections = sorted(
                rejection_stats.items(),
                key=lambda kv: kv[1],
                reverse=True,
            )[:5]
            post_decisions(
                decisions=decisions_data,
                vol=float(vol_now),
                diagnostics={
                    "rejection_stats": rejection_stats,
                    "rejection_stats_total": rejection_stats_total,
                    "top_rejections": [{str(k): int(v)} for k, v in top_rejections],
                    "last_diag": {
                        "score": float(last_diag.get("score", 0.0)),
                        "score_effective": float(last_diag.get("score_effective", last_diag.get("score", 0.0))),
                        "pz": float(last_diag.get("pz", 0.0)),
                        "pz_blend": float(last_diag.get("pz_blend", last_diag.get("pz", 0.0))),
                        "pz_fast": float(last_diag.get("pz_fast", last_diag.get("pz", 0.0))),
                        "pz_slow": float(last_diag.get("pz_slow", last_diag.get("pz", 0.0))),
                        "hawkes_n": float(last_diag.get("hawkes_n", 0.0)),
                        "lppls_hazard": float(last_diag.get("lppls_hazard", 0.0)),
                        "p_trend": float(last_diag.get("p_trend", 0.5)),
                        "predictive_sharpe": float(last_diag.get("predictive_sharpe", 0.0)),
                        "predictive_sharpe_aligned": float(
                            last_diag.get(
                                "predictive_sharpe_aligned",
                                self._directional_sharpe(
                                    float(last_diag.get("score", 0.0)),
                                    float(last_diag.get("predictive_sharpe", 0.0)),
                                ),
                            )
                        ),
                        "vol": float(last_diag.get("vol", 0.0)),
                        "momentum_component": float(last_diag.get("momentum_component", 0.0)),
                        "micro_component": float(last_diag.get("micro_component", 0.0)),
                        "raw_signal": float(last_diag.get("raw_signal", 0.0)),
                        "lppls_factor": float(last_diag.get("lppls_factor", 1.0)),
                        "session_mult": float(last_diag.get("session_mult", 1.0)),
                        "direction_factor": float(last_diag.get("direction_factor", 1.0)),
                        "direction_side": str(last_diag.get("direction_side", "BUY")),
                        "direction_hit_rate": float(last_diag.get("direction_hit_rate", 0.5)),
                        "direction_buy_hit_rate": float(last_diag.get("direction_buy_hit_rate", 0.5)),
                        "direction_sell_hit_rate": float(last_diag.get("direction_sell_hit_rate", 0.5)),
                        "direction_buy_samples": int(last_diag.get("direction_buy_samples", 0)),
                        "direction_sell_samples": int(last_diag.get("direction_sell_samples", 0)),
                        "direction_samples": int(last_diag.get("direction_samples", 0)),
                        "direction_side_samples": int(last_diag.get("direction_side_samples", 0)),
                        "primary_horizon_hours": float(
                            last_diag.get("primary_horizon_hours", self.hold_default_horizon_hours)
                        ),
                        "horizon_confidence": float(last_diag.get("horizon_confidence", 0.0)),
                        "horizon_strength": float(last_diag.get("horizon_strength", 0.0)),
                        "horizon_side": str(last_diag.get("horizon_side", "NONE")),
                        "heston_scale": float(last_diag.get("heston_scale", 1.0)),
                        "gate_penalty": float(last_diag.get("gate_penalty", 1.0)),
                        "regime_bucket": str(last_diag.get("regime_bucket", "unknown")),
                        "score_threshold_regime": float(last_diag.get("score_threshold_regime", self.score_th)),
                        "score_threshold_base_mult": float(last_diag.get("score_threshold_base_mult", 1.0)),
                        "score_threshold_side_mult": float(last_diag.get("score_threshold_side_mult", 1.0)),
                        "score_threshold_adapt_mult": float(last_diag.get("score_threshold_adapt_mult", 1.0)),
                        "score_threshold_total_mult": float(last_diag.get("score_threshold_total_mult", 1.0)),
                        "utility": float(last_diag.get("utility", 0.0)),
                        "utility_edge": float(last_diag.get("utility_edge", 0.0)),
                        "utility_var_pen": float(last_diag.get("utility_var_pen", 0.0)),
                        "utility_tail_pen": float(last_diag.get("utility_tail_pen", 0.0)),
                        "utility_cost_pen": float(last_diag.get("utility_cost_pen", 0.0)),
                        "utility_corr_pen": float(last_diag.get("utility_corr_pen", 0.0)),
                        "utility_min": float(last_diag.get("utility_min", self.utility_min)),
                        "utility_ok": bool(last_diag.get("utility_ok", True)),
                        "beta_p": float(last_diag.get("beta_p", 0.0)),
                        "beta_m": float(last_diag.get("beta_m", 0.0)),
                        "side_share_buy_rolling": float(getattr(self, "side_share_buy_rolling", 0.5)),
                        "side_share_sell_rolling": float(getattr(self, "side_share_sell_rolling", 0.5)),
                        "abstain_rate_rolling": float(getattr(self, "abstain_rate_rolling", 0.0)),
                        "edge_vs_random_hit_delta": float(getattr(self, "edge_vs_random_hit_delta", 0.0)),
                        "edge_vs_random_expectancy_delta": float(
                            getattr(self, "edge_vs_random_expectancy_delta", 0.0)
                        ),
                    },
                    "entry_gate_mode": self.entry_gate_mode,
                    "execution_gate_mode": self.execution_gate_mode,
                    "execution_quality": {
                        "enabled": bool(self.use_execution_quality_gate),
                        "min_confidence": float(self.exec_min_confidence),
                        "min_score_ratio": float(self.exec_min_score_ratio),
                        "min_raw_signal_ratio": float(self.exec_min_raw_signal_ratio),
                        "min_sharpe_ratio": float(self.exec_min_sharpe_ratio),
                        "use_raw_signal_proxy": bool(self.exec_use_raw_signal_proxy),
                        "score_zero_epsilon": float(self.score_zero_epsilon),
                        "confidence_risk_sizing": bool(self.use_confidence_risk_sizing),
                        "conf_risk_floor": float(self.conf_risk_floor),
                        "conf_risk_ceiling": float(self.conf_risk_ceiling),
                        "conf_risk_power": float(self.conf_risk_power),
                        "soft_blocked_risk_scale": float(self.soft_blocked_risk_scale),
                        "equity_scaled_pip_target": bool(self.use_equity_scaled_pip_target),
                        "pip_value_target_pct_equity": float(self.pip_value_target_pct_equity),
                    },
                    "utility_gate_mode": self.utility_gate_mode if self.use_utility_objective else "off",
                    "portfolio_risk": {
                        "enabled": bool(self.use_portfolio_risk_budget),
                        "total_risk_pct": float(
                            (getattr(self, "portfolio_risk_state", {}) or {}).get("total_risk_pct", 0.0)
                        ),
                        "portfolio_risk_cap_pct": float(self.portfolio_risk_cap_pct),
                        "cluster_risk_cap_pct": float(self.cluster_risk_cap_pct),
                        "cluster_risk_pct": dict(
                            (getattr(self, "portfolio_risk_state", {}) or {}).get("cluster_risk_pct", {}) or {}
                        ),
                    },
                    "plugin_flags": {
                        "use_hawkes": bool(self.use_hawkes),
                        "use_lppls": bool(self.use_lppls),
                        "use_heston_guard": bool(self.heston is not None),
                        "use_ai_indicator_model": bool(self.use_ai_indicator_model),
                    },
                    "governance": dict(getattr(self, "governance_state", {}) or {}),
                    "risk_envelope": dict(getattr(self, "latest_risk_envelope", {}) or {}),
                    "runtime_metrics": {
                        "stale_tick_rejections": int(getattr(self, "stale_tick_rejections", 0)),
                        "gap_recovery_events": int(getattr(self, "gap_recovery_events", 0)),
                        "close_failures_by_error_code": dict(
                            getattr(self, "close_failures_by_error_code", {}) or {}
                        ),
                        "cycle_id": int(getattr(self, "cycle_id", 0)),
                        "audit_trace_enabled": bool(getattr(self, "audit_trace_enabled", False)),
                        "audit_trace_path": str(getattr(self, "audit_trace_path", "")),
                        "suppression_ratio_rolling": float(getattr(self, "suppression_ratio_rolling", 0.0)),
                        "dominant_rejection_reason": str(getattr(self, "dominant_rejection_reason", "none")),
                        "side_share_buy_rolling": float(getattr(self, "side_share_buy_rolling", 0.5)),
                        "side_share_sell_rolling": float(getattr(self, "side_share_sell_rolling", 0.5)),
                        "abstain_rate_rolling": float(getattr(self, "abstain_rate_rolling", 0.0)),
                        "edge_vs_random_hit_delta": float(getattr(self, "edge_vs_random_hit_delta", 0.0)),
                        "edge_vs_random_expectancy_delta": float(
                            getattr(self, "edge_vs_random_expectancy_delta", 0.0)
                        ),
                        "warmup_mode": bool(self._warmup_mode_active()),
                        "warmup_strategy": str(self.startup_warmup_strategy),
                        "startup_backfill_pending": bool(self.startup_backfill_pending_active),
                        "startup_backfill_ready": bool(self.startup_backfill_ready_active),
                        "startup_backfill_bars": int(self.startup_backfill_bars_active),
                        "startup_backfill_retry_age_secs": float(self.startup_backfill_retry_age_secs_active),
                        "startup_backward_replay_done": bool(self.startup_backward_replay_done_active),
                        "starvation_mode": bool(getattr(self, "starvation_mode_active", False)),
                        "relax_level": float(getattr(self, "starvation_relax_level", 0.0)),
                        "bridge_safety_close_only_until_cycle": int(
                            getattr(self, "bridge_safety_close_only_until_cycle", 0)
                        ),
                    },
                    "top_candidate": top_candidate,
                    "top_candidates": top_candidates,
                    "suppression_ratio_rolling": float(self.suppression_ratio_rolling),
                    "dominant_rejection_reason": str(self.dominant_rejection_reason),
                    "side_share_buy_rolling": float(self.side_share_buy_rolling),
                    "side_share_sell_rolling": float(self.side_share_sell_rolling),
                    "abstain_rate_rolling": float(self.abstain_rate_rolling),
                    "edge_vs_random_hit_delta": float(self.edge_vs_random_hit_delta),
                    "edge_vs_random_expectancy_delta": float(self.edge_vs_random_expectancy_delta),
                    "warmup_mode": bool(self._warmup_mode_active()),
                    "warmup_strategy": str(self.startup_warmup_strategy),
                    "startup_backfill_pending": bool(self.startup_backfill_pending_active),
                    "startup_backfill_ready": bool(self.startup_backfill_ready_active),
                    "startup_backfill_bars": int(self.startup_backfill_bars_active),
                    "startup_backfill_retry_age_secs": float(self.startup_backfill_retry_age_secs_active),
                    "startup_backward_replay_done": bool(self.startup_backward_replay_done_active),
                    "monitor": monitor_payload,
                    "model_age_secs": float(
                        max(
                            0.0,
                            time.time()
                            - float(
                                self.regime_last_fit_ts.get(
                                    str(top_candidate.get("symbol", "")).upper(),
                                    0.0,
                                )
                            ),
                        )
                    )
                    if (
                        top_candidate
                        and float(
                            self.regime_last_fit_ts.get(
                                str(top_candidate.get("symbol", "")).upper(),
                                0.0,
                            )
                        )
                        > 0.0
                    )
                    else 0.0,
                    "starvation_mode": bool(self.starvation_mode_active),
                    "relax_level": float(self.starvation_relax_level),
                    "recent_decisions": self.decision_log[-10:] if self.decision_log else []
                },
                max_retries=1,
            )
        except Exception as exc:
            logger.warning(f"Failed to post decisions to bridge: {exc}")

    def _sanitize_candidate(self, candidate: dict | None) -> dict:
        """Normalize blocker labels for HUD readability."""
        row = dict(candidate or {})
        blocker = str(row.get("blocked_by", "none"))
        if (not self.use_hawkes_gate) and blocker == "hawkes_crowding":
            blocker = "none"
        row["blocked_by"] = blocker
        return row
    
    def _log_rejection(self, reason: str):
        """Track rejection reasons for diagnostics."""
        if reason == "hawkes_crowding" and not self.use_hawkes_gate:
            return
        self.rejection_stats[reason] = self.rejection_stats.get(reason, 0) + 1
        self.rejection_stats_cycle[reason] = self.rejection_stats_cycle.get(reason, 0) + 1
    
    def _log_decision(self, symbol: str, diagnostics: dict):
        """Log decision data for audit trail."""
        from datetime import datetime
        self.decision_log.append({
            "time": datetime.now().isoformat(),
            "symbol": symbol,
            **diagnostics
        })
        # Keep last 1000 decisions
        if len(self.decision_log) > 1000:
            self.decision_log.pop(0)
