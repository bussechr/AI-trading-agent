from __future__ import annotations
import os, json, math, time
from dataclasses import dataclass
import numpy as np
import pandas as pd
import logging

from .risk_utils import el_pz, regime_tilt, dynamic_target_pct, realised_vol, cost_gate, low_corr_pick
from .risk_manager import RiskManager
try:
    from execution import mt4_bridge_client as bridge_client
except ImportError:  # Package mode: import via src.*
    from src.execution import mt4_bridge_client as bridge_client

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
        self.roots = cfg["symbols_roots"]
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
        self.exec_min_sharpe_ratio = float(cfg.get("exec_min_sharpe_ratio", 0.80))
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

        # Regime-conditional thresholding: stricter in transition, looser in clean trends.
        self.regime_trend_threshold = float(cfg.get("regime_trend_threshold", 0.62))
        self.regime_range_threshold = float(cfg.get("regime_range_threshold", 0.38))
        self.regime_score_mult_trend = float(cfg.get("regime_score_mult_trend", 0.95))
        self.regime_score_mult_range = float(cfg.get("regime_score_mult_range", 1.05))
        self.regime_score_mult_transition = float(cfg.get("regime_score_mult_transition", 1.30))
        self.regime_score_mult_trend_buy = float(cfg.get("regime_score_mult_trend_buy", 1.0))
        self.regime_score_mult_trend_sell = float(cfg.get("regime_score_mult_trend_sell", 1.0))
        self.regime_score_mult_range_buy = float(cfg.get("regime_score_mult_range_buy", 1.0))
        self.regime_score_mult_range_sell = float(cfg.get("regime_score_mult_range_sell", 1.0))
        self.regime_score_mult_transition_buy = float(cfg.get("regime_score_mult_transition_buy", 1.0))
        self.regime_score_mult_transition_sell = float(cfg.get("regime_score_mult_transition_sell", 1.0))
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
            np.clip(cfg.get("score_distribution_floor_mult", 0.25), 0.05, 1.00)
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
        
        # OFI history for z-score normalization
        self.ofi_history = []
        self.ofi_history_max = 50
        
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
        self.score_abs_history: dict[str, list[float]] = {}
        self.score_history_bar: dict[str, int] = {}
        self._last_direction_state_save = 0.0
        self.portfolio_risk_state: dict = {}
        self.governance_state: dict = {}
        try:
            self._load_direction_state()
        except AttributeError:
            logger.warning("Directional persistence loader missing; continuing without saved state")
        except Exception as exc:
            logger.warning("Directional persistence init failed: %s", exc)
        
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
        self.pending_entry_ttl_secs = float(np.clip(cfg.get("pending_entry_ttl_secs", 8.0), 1.0, 60.0))
        self.pending_entries: dict[tuple[str, str], float] = {}
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
            base_mult = float(self.regime_score_mult_transition)
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

    def _update_governance_state(self, equity: float) -> dict:
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

        reasons: list[str] = []
        risk_scale = 1.0
        hard_triggered = False
        if self.use_live_governance:
            if drawdown >= self.gov_hard_dd_pct:
                hard_triggered = True
                self.gov_pause_until_cycle = max(self.gov_pause_until_cycle, self.gov_cycle + self.gov_pause_cycles)
                reasons.append("hard_drawdown")

            edge_decay = (w >= self.gov_edge_window) and (rolling_edge < self.gov_min_edge)
            conf_decay = (w >= self.gov_edge_window) and (rolling_conf < self.gov_min_conf)
            if drawdown >= self.gov_soft_dd_pct:
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
            "cycle": int(self.gov_cycle),
            "equity_peak": float(self.gov_equity_peak),
            "drawdown_pct": float(drawdown),
            "soft_dd_pct": float(self.gov_soft_dd_pct),
            "hard_dd_pct": float(self.gov_hard_dd_pct),
            "recovery_dd_pct": float(self.gov_recovery_dd_pct),
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
            if not rows:
                continue
            clipped = list(rows)[-self.direction_calib_window :]
            history_payload[str(sym).upper()] = [[int(side), int(hit)] for side, hit in clipped]

        state_payload: dict[str, dict] = {}
        for sym, row in (self.direction_state or {}).items():
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
        
        # Strong trend regime (p_trend > 0.7) → boost momentum, dampen micro
        # Range regime (p_trend < 0.3) → dampen momentum, boost micro
        if p_trend > 0.7:
            beta_p_adj = beta_p * self.beta_trend_boost
            beta_m_adj = beta_m * (2.0 - self.beta_trend_boost)  # Inverse
        elif p_trend < 0.3:
            beta_p_adj = beta_p * (2.0 - self.beta_range_boost)  # Inverse
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
        direction_side_post = 1 if score >= 0 else -1
        direction_side_samples = int(
            direction_quality.get("buy_samples", 0) if direction_side_post > 0 else direction_quality.get("sell_samples", 0)
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
            "micro_coop_mult": float(micro_coop_mult),
            "raw_signal": float(momentum_component + micro_component),
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
        
        for sym, df in md.items():
            if df is None or df.empty or len(df) < max(252, self.el_window + 5):
                self._log_rejection("insufficient_bars")
                continue

            # Skip stale symbols when live tick timestamps are available.
            if self.require_fresh_ticks and ("last_tick_ts" in df.attrs):
                try:
                    tick_age = time.time() - float(df.attrs.get("last_tick_ts", 0.0))
                    if tick_age > self.tick_stale_secs:
                        self._log_rejection("stale_tick")
                        continue
                except Exception as exc:
                    logger.debug(f"{sym}: failed tick staleness check ({exc})")
            
            sc, diag = self.score_symbol(df, sym)
            sym_key = str(sym).upper()
            
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
            trade_side = "BUY" if sc >= 0 else "SELL"
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
            diag["regime_bucket"] = regime_bucket
            diag["score_threshold_regime"] = float(score_threshold_regime)
            diag["score_threshold_regime_raw"] = float(score_threshold_raw)
            diag["score_threshold_base_mult"] = float(th_parts["base_mult"])
            diag["score_threshold_side_mult"] = float(th_parts["side_mult"])
            diag["score_threshold_adapt_mult"] = float(th_parts["adapt_mult"])
            diag["score_threshold_total_mult"] = float(th_parts["total_mult"])
            diag["score_threshold_ref_q"] = float(score_threshold_ref_q)
            diag["score_threshold_ref_n"] = int(score_threshold_ref_n)

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
            if not score_ok:
                blockers.append("low_score")
            if not sharpe_ok:
                blockers.append("low_predictive_sharpe")
            if self.use_hawkes_gate and (not hawkes_ok):
                blockers.append("hawkes_crowding")
            if self.use_utility_objective and (not utility_ok) and self.utility_gate_mode == "hard":
                blockers.append("negative_utility")
            blocked_by = blockers[0] if blockers else "none"

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
                    gate_penalty *= float(np.clip(abs(sc) / max(score_threshold_regime, 1e-9), 0.35, 1.0))
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
                confidence_adj *= float(np.clip(gate_penalty, 0.10, 1.0))
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
            diag["blocked_by"] = blocked_by

            candidate_row = {
                "symbol": sym,
                "side": "BUY" if sc_effective >= 0 else "SELL",
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
                "raw_signal": float(diag.get("raw_signal", 0.0)),
                "lppls_factor": float(diag.get("lppls_factor", 1.0)),
                "session_mult": float(diag.get("session_mult", 1.0)),
                "direction_factor": float(diag.get("direction_factor", 1.0)),
                "direction_hit_rate": float(diag.get("direction_hit_rate", 0.5)),
                "direction_buy_hit_rate": float(diag.get("direction_buy_hit_rate", 0.5)),
                "direction_sell_hit_rate": float(diag.get("direction_sell_hit_rate", 0.5)),
                "direction_buy_samples": int(diag.get("direction_buy_samples", 0)),
                "direction_sell_samples": int(diag.get("direction_sell_samples", 0)),
                "direction_samples": int(diag.get("direction_samples", 0)),
                "heston_scale": float(heston_scale),
                "heston_ratio": float(heston_ratio),
                "gate_spread": bool(spread_ok),
                "gate_cost": bool(cost_ok),
                "gate_score": bool(score_ok),
                "gate_sharpe": bool(sharpe_ok),
                "gate_hawkes": bool(hawkes_ok),
                "gate_heston": bool(heston_ok),
                "gate_utility": bool(utility_ok),
                "utility": float(diag.get("utility", 0.0)),
                "utility_min": float(self.utility_min),
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
            if blockers and self.entry_gate_mode == "soft":
                priority *= 0.80
            candidate_row["priority"] = float(max(priority, 0.0))
            entry_ready = not (blockers and self.entry_gate_mode == "hard")
            exec_quality_ready = True
            if self.use_execution_quality_gate:
                exec_quality_ready = (
                    float(candidate_row.get("confidence_exec", 0.0)) >= float(self.exec_min_confidence)
                    and float(candidate_row.get("score_ratio", 0.0)) >= float(self.exec_min_score_ratio)
                    and float(candidate_row.get("sharpe_ratio", 0.0)) >= float(self.exec_min_sharpe_ratio)
                )
            exec_cost_ready = True
            if self.execution_gate_mode == "hard":
                exec_cost_ready = bool(cost_ok)
            execution_ready = bool(entry_ready and exec_quality_ready and exec_cost_ready)
            candidate_row["entry_ready"] = bool(entry_ready)
            candidate_row["exec_quality_ready"] = bool(exec_quality_ready)
            candidate_row["exec_cost_ready"] = bool(exec_cost_ready)
            candidate_row["execution_ready"] = bool(execution_ready)
            candidate_rows.append(candidate_row)

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

            if blockers:
                for reason_soft in blockers:
                    self._log_rejection(f"soft_{reason_soft}")

            side = "BUY" if sc_effective >= 0 else "SELL"
            
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
            logger.error(f"HUD Error: {e}")

    def _extract_root(self, symbol: str) -> str:
        """Extract FX root from symbol (e.g., 'EURUSD.MINI' -> 'EURUSD')."""
        s_up = symbol.upper()
        for root in self.roots:
            if root in s_up:
                return root
        return symbol  # fallback

    def _manage_exits(self, positions: list[dict], md: dict[str, pd.DataFrame]) -> None:
        """Check active positions for score reversals AND risk manager exits."""
        
        # Ensure RiskManager is initialized
        if not hasattr(self, "risk_manager"):
            from .risk_manager import RiskManager
            self.risk_manager = RiskManager(self.cfg)

        for pos in positions:
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
                        open_time = time.time() # Fail safe: assume new if unknown, to trigger grace period? 
                        # Actually if we assume new, we might hold forever if logic depends on mature.
                        # Check risk manager: if duration < 300, it skips stop. 
                        # So if we set open_time = now, duration=0, we are safe (held).
                        # But we risk holding a zombie trade. 
                        # Better to set open_time = time.time() so it gets a 5m grace period from *agent restart*.

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
                
                # Check Risk Exits
                should_close_risk, reason_risk = self.risk_manager.check_exit(
                    sym, current_price, current_vol, 
                    p_trend_val, now_ts
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

                    bridge_client.close_position(sym, magic=p_magic)
                    continue

                # --- SIGNAL REVERSAL EXIT ---
                # If we hold a position but the signal has flipped strongly against us, close it.
                # This catches V-reversals where the trend changes before the stop.
                
                # For SHORT (SELL) positions:
                # If New Score > +Threshold (Strong Buy Signal) -> Close Short
                if side == "SELL" and sc > exit_score_threshold:
                    logger.info(
                        f"REVERSAL EXIT {sym}: Score {sc:.2f} > {exit_score_threshold:.2f} (Bullish Reversal)"
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
                    bridge_client.close_position(sym, magic=p_magic)
                    continue
                
                # For LONG (BUY) positions:
                # If New Score < -Threshold (Strong Sell Signal) -> Close Long
                if side == "BUY" and sc < -exit_score_threshold:
                    logger.info(
                        f"REVERSAL EXIT {sym}: Score {sc:.2f} < -{exit_score_threshold:.2f} (Bearish Reversal)"
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
                    bridge_client.close_position(sym, magic=p_magic)
                    continue



            except Exception as e:
                logger.error(f"Error in risk update for {sym}: {e}")

            # --- COMPLETED RISK & OVERLORD CHECKS ---
            # Legacy Score Reversal removed to prevent churn.
            # We rely on Overlord (Regime Guard) and Risk Manager (R-Multiples).
            pass

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
            next_score_th = max(0.02, self.base_score_th * high_score_mult)
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
        self.rejection_stats_cycle = {}
        if not self.use_hawkes_gate:
            self.rejection_stats.pop("hawkes_crowding", None)
        universe = self.build_universe(all_symbols_catalog)
        md = {s: market_data.get(s) for s in universe if s in market_data}

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
        gov_state = self._update_governance_state(float(equity))
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
            decs_for_exec = []

        # 5. Execute New Trades (dashboard posted after execution gating for accurate live state)
        executed_decs: list[Decision] = []
        if not decs_for_exec:
            self._post_decisions_to_dashboard(executed_decs, md, vol_now, target_pct)
            self._persist_direction_state(force=False)
            return

        total_risk_used = float(self.portfolio_risk_state.get("total_risk_pct", 0.0))
        cluster_risk_used = dict(self.portfolio_risk_state.get("cluster_risk_pct", {}) or {})
        cluster_map = dict(self.portfolio_risk_state.get("cluster_map", {}) or {})
        for d in decs_for_exec:
            try:
                df = md[d.symbol]
                current_price = float(df["close"].iloc[-1])
                candidate_row = dict(candidate_map.get(self._candidate_key(d.symbol, d.side), {}) or {})
                conf_now = float(getattr(d, "confidence", 0.0))
                score_ratio_now = float(getattr(d, "score_ratio", 0.0))
                sharpe_ratio_now = 0.0
                if candidate_row:
                    conf_now = float(
                        candidate_row.get(
                            "confidence_exec",
                            candidate_row.get("confidence_raw", candidate_row.get("confidence", conf_now)),
                        )
                    )
                    score_ratio_now = float(candidate_row.get("score_ratio", score_ratio_now))
                    sharpe_ratio_now = float(candidate_row.get("sharpe_ratio", 0.0))
                    if self.use_execution_quality_gate:
                        if conf_now < float(self.exec_min_confidence):
                            self._log_rejection("exec_low_confidence")
                            continue
                        if score_ratio_now < float(self.exec_min_score_ratio):
                            self._log_rejection("exec_low_score_ratio")
                            continue
                        if sharpe_ratio_now < float(self.exec_min_sharpe_ratio):
                            self._log_rejection("exec_low_sharpe_ratio")
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
                        continue
                    if remaining_cluster <= 0:
                        self._log_rejection("cluster_risk_cap")
                        continue
                    allowed_risk_pct = min(base_trade_risk_pct, remaining_portfolio, remaining_cluster)
                    if allowed_risk_pct < self.portfolio_min_trade_risk_pct:
                        self._log_rejection("portfolio_risk_budget_thin")
                        continue
                elif allowed_risk_pct <= 0:
                    self._log_rejection("risk_scale_zero")
                    continue

                lot_size = calculate_position_size(
                    equity,
                    allowed_risk_pct,
                    sl_pips_scalar,
                    pip_value_symbol,
                )
                
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
                            continue
                        lot_size = max_lots_q
                    if lot_size < lot_floor_margin:
                        self._log_rejection("margin_level_target_unmet")
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
                        continue
                    if trade_risk_pct > max_allowed_now:
                        scale = max_allowed_now / max(trade_risk_pct, 1e-12)
                        lot_scaled = lot_size * scale
                        lot_scaled_q = math.floor(lot_scaled / self.lot_step_hint) * self.lot_step_hint
                        if lot_scaled_q < min_lot_effective:
                            self._log_rejection("portfolio_min_lot_exceeds_budget")
                            continue
                        lot_size = lot_scaled_q
                        trade_risk_pct = (
                            max(float(sl_pips_scalar), 0.0)
                            * max(float(pip_value_symbol), 1e-9)
                            * max(float(lot_size), 0.0)
                        ) / max(float(equity), 1e-9)
                        if trade_risk_pct > (max_allowed_now * 1.01):
                            self._log_rejection("portfolio_risk_requantize_fail")
                            continue
                        self._log_rejection("portfolio_risk_scaled")
                
                est_margin_used = (notional_per_lot * max(lot_size, 0.0)) / max(self.leverage, 1e-9)
                est_margin_level = (equity / max(est_margin_used, 1e-9)) * 100.0
                signal_kind = "ADD" if getattr(d, "is_add", False) else "SIGNAL"
                if self._has_pending_entry(d.symbol, d.side, time.time()):
                    self._log_rejection("pending_entry_sync")
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
        self._post_decisions_to_dashboard(executed_decs, md, vol_now, target_pct)
        self._persist_direction_state(force=False)
    
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

            top_candidate = self._sanitize_candidate(getattr(self, "last_best_candidate", {}) or {})
            if top_candidate:
                confidence_line = (
                    f"CONFIDENCE: {float(top_candidate.get('confidence', 0.0)):.0f}% | "
                    f"ScoreX {float(top_candidate.get('score_ratio', 0.0)):.2f} | "
                    f"SharpeX {float(top_candidate.get('sharpe_ratio', 0.0)):.2f} | "
                    f"CostX {float(top_candidate.get('cost_ratio', 0.0)):.2f} | "
                    f"DirHit {float(top_candidate.get('direction_hit_rate', 0.5))*100.0:.0f}% | "
                    f"DFac {float(top_candidate.get('direction_factor', 1.0)):.2f} | "
                    f"ThX {float(top_candidate.get('score_threshold_total_mult', 1.0)):.2f} | "
                    f"U {float(top_candidate.get('utility', 0.0)):+.4f} | "
                    f"Pri {float(top_candidate.get('priority', top_candidate.get('score', 0.0))):.2f} | "
                    f"Reg {str(top_candidate.get('regime_bucket', '?'))}"
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
                                 f"({conf_now:.0f}%)"
                             )
                         else:
                             action_line = (
                                 f"ACTION: Scanning... Pressure {blocker_all} | "
                                 f"Closest {top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                                 f"({conf_now:.0f}%)"
                             )
                     else:
                         action_line = (
                             f"ACTION: Scanning... Closest "
                             f"{top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                             f"({conf_now:.0f}%)"
                         )
                 else:
                     action_line = "ACTION: Scanning..."

            if bool(gov.get("enabled", False)) and bool(gov.get("paused", False)):
                reason_txt = ",".join([str(x) for x in list(gov.get("reasons", []))[:2]]) or "governance"
                if top_candidate:
                    action_line = (
                        f"ACTION: Paused ({reason_txt}) | Closest "
                        f"{top_candidate.get('symbol', '?')} {top_candidate.get('side', '?')} "
                        f"({float(top_candidate.get('confidence', 0.0)):.0f}%)"
                    )
                else:
                    action_line = f"ACTION: Paused ({reason_txt})"
            
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
                    },
                    "entry_gate_mode": self.entry_gate_mode,
                    "execution_gate_mode": self.execution_gate_mode,
                    "execution_quality": {
                        "enabled": bool(self.use_execution_quality_gate),
                        "min_confidence": float(self.exec_min_confidence),
                        "min_score_ratio": float(self.exec_min_score_ratio),
                        "min_sharpe_ratio": float(self.exec_min_sharpe_ratio),
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
                    "governance": dict(getattr(self, "governance_state", {}) or {}),
                    "top_candidate": top_candidate,
                    "top_candidates": top_candidates,
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
