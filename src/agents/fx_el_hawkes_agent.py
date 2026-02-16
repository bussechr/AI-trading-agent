from __future__ import annotations
import os, json, math
from dataclasses import dataclass
import numpy as np
import pandas as pd
import logging

from .risk_utils import el_pz, regime_tilt, dynamic_target_pct, realised_vol, cost_gate, low_corr_pick
from execution.mt4_bridge_client import send
import requests

from .regime_filter import MarkovSwitchingModel, RegimeMixture
from .hawkes_micro import BivariateHawkes, OFIProxy, HawkesSignal
from .lppls_guard import LPPLSDetector, LPPLSResult

MINI_SUFFIXES_DEFAULT = [".MINI", "-MINI", "m", ".m"]

logger = logging.getLogger(__name__)

@dataclass
class Decision:
    symbol: str
    side: str
    score: float

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
        self.score_th = float(cfg.get("score_threshold", 0.40))
        self.use_dyn_target = bool(cfg.get("use_dynamic_target", True))

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
        self.leverage = float(cfg.get("leverage", 30.0))
        self.max_margin_pct = float(cfg.get("max_margin_pct", 0.80))
        
        self.regime_model = MarkovSwitchingModel(n_states=2)
        self.regime_fitted = False
        
        self.use_hawkes = cfg.get("use_hawkes", False)
        self.hawkes_n_min = cfg.get("hawkes_n_min", 0.8)
        if self.use_hawkes:
            self.hawkes = BivariateHawkes()
            self.ofi_proxy = OFIProxy()
        
        self.use_lppls = cfg.get("use_lppls", False)
        self.lppls_threshold = cfg.get("lppls_threshold", 0.6)
        if self.use_lppls:
            self.lppls = LPPLSDetector(window=252)
        
        self.min_predictive_sharpe = cfg.get("min_predictive_sharpe", 0.3)
        
        # Strategy refinements
        self.use_adaptive_beta = cfg.get("use_adaptive_beta", True)
        self.beta_trend_boost = cfg.get("beta_trend_boost", 1.3)
        self.beta_range_boost = cfg.get("beta_range_boost", 1.3)
        self.use_session_filter = cfg.get("use_session_filter", True)
        self.use_graduated_heston = cfg.get("use_graduated_heston", True)
        
        # OFI history for z-score normalization
        self.ofi_history = []
        self.ofi_history_max = 50
        
        self.heston = None
        if cfg.get("use_heston_guard", False):
            self._init_heston_service(cfg)
        
        # Runtime validation tracking
        self.rejection_stats = {}
        self.decision_log = []

    def _init_heston_service(self, cfg: dict):
        """Initialize Heston service with configured provider."""
        from .heston_service import HestonService
        
        provider_type = cfg.get("heston_provider", "proxy")  # "http" or "proxy"
        
        if provider_type == "http":
            from marketdata.http_fx_options import HTTPFXOptionProvider
            provider = HTTPFXOptionProvider(
                url_template=cfg.get("options_url_template", "https://api.example.com/fx/chain?symbol={symbol}"),
                headers={"Authorization": f"Bearer {cfg.get('options_api_key', '')}"},
                field_map=cfg.get("options_field_map", {})
            )
            logger.info("Initialized HTTP options provider for Heston calibration")
        else:
            from marketdata.proxy_provider import ProxyOptionProvider
            
            def get_close_series(root):
                path = f"data/fx_minis/{root}.MINI.csv"
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
    def score_symbol(self, df: pd.DataFrame, symbol: str) -> tuple[float, dict]:
        """
        Full EL-Hawkes-Regime score with predictive mixture.
        
        score_t = β_p * pz_t * (2*P_t - 1) + β_m * m_t
        
        where:
        - pz_t: EL momentum (z-scored, EMA smoothed)
        - P_t: regime probability (trend state)
        - m_t: Hawkes drift proxy (optional)
        """
        close = df["close"]
        r = np.log(close).diff()
        
        if len(close) < max(252, self.cfg["el_window"] + 5):
            return 0.0, {"error": "insufficient_bars"}
        
        pz_series = el_pz(close, self.cfg["el_window"], self.cfg["el_ema_span"])
        
        if not self.regime_fitted and len(r) >= 252:
            try:
                self.regime_model.fit(r, pz_series)
                self.regime_fitted = True
                logger.info(f"Regime model fitted for {symbol}")
            except Exception as e:
                logger.warning(f"Regime fitting failed: {e}")
        
        pz_val = float(pz_series.iloc[-1])
        
        if self.regime_fitted:
            p_trend = self.regime_model.get_trend_probability()
        else:
            # Fallback to simple tilt
            tilt_series = regime_tilt(r)
            tilt_val = float(tilt_series.iloc[-1])
            p_trend = (tilt_val + 1.0) / 2.0  # Map [-1, 1] to [0, 1]
        
        # Get adaptive beta weights based on regime
        beta_p, beta_m = self._adaptive_beta_weights(p_trend)
        score = beta_p * pz_val * (2 * p_trend - 1)
        
        hawkes_drift = 0.0
        hawkes_n = 1.0
        if self.use_hawkes:
            # Use OFI proxy if no tick data
            if 'volume' in df.columns:
                hawkes_signal = self.ofi_proxy.get_signal(df.tail(100))
            else:
                # Fallback: no micro signal
                hawkes_signal = HawkesSignal(0.0, 1.0, 0.0, 0.0)
            
            hawkes_drift = hawkes_signal.drift
            hawkes_n = hawkes_signal.branching
            
            # Add micro drift to score with improved normalization
            drift_std = self._normalize_ofi_drift(hawkes_drift)
            score += beta_m * drift_std
        
        lppls_hazard = 0.0
        if self.use_lppls:
            lppls_hazard = self.lppls.get_hazard(close)
            if lppls_hazard > self.lppls_threshold and score > 0:
                # Reduce long exposure in high crash hazard
                logger.info(f"{symbol}: LPPLS hazard {lppls_hazard:.2f} > threshold, reducing long score")
                score *= (1.0 - lppls_hazard)
        
        # Apply session awareness adjustment
        if self.use_session_filter:
            import datetime
            utc_hour = datetime.datetime.utcnow().hour
            session_mult = self._session_adjustment(symbol, utc_hour)
            score *= session_mult
            if session_mult < 1.0:
                logger.debug(f"{symbol}: session hour {utc_hour} UTC → score scaled by {session_mult:.2f}")
        
        # Calculate simple historical Sharpe as fallback
        simple_rolling_sharpe = 0.0
        if len(r) >= 24:
            # Annualized Sharpe (approximate for H1 data: sqrt(24*252) ~ 77.7)
            # Using a smaller window for reactivity, e.g. 5 days (120 bars)
            window = min(len(r), 120)
            rolling_mean = r.rolling(window).mean().iloc[-1]
            rolling_std = r.rolling(window).std(ddof=0).iloc[-1]
            if rolling_std > 1e-9:
                # Annualize: mean * N / (std * sqrt(N)) -> mean/std * sqrt(N)
                simple_rolling_sharpe = (rolling_mean / rolling_std) * np.sqrt(252 * 24)
            
            logger.info(f"{symbol} DEBUG: Mean={rolling_mean:.6f}, Std={rolling_std:.6f}, Sharpe={simple_rolling_sharpe:.4f}, Bars={len(r)}")

        predictive_sharpe = simple_rolling_sharpe  # Start with historical
        if self.regime_fitted:
            try:
                mixture = self.regime_model.get_predictive_mixture(pz_val)
                model_sharpe = mixture.sharpe
                # Use whichever has larger magnitude
                if abs(model_sharpe) > abs(simple_rolling_sharpe):
                    predictive_sharpe = model_sharpe
            except Exception:
                pass  # Keep simple_rolling_sharpe
        
        diagnostics = {
            "pz": pz_val,
            "p_trend": p_trend,
            "score": score,
            "vol": float(r.rolling(96).std(ddof=0).iloc[-1]) if len(r) > 96 else 0.0,
            "hawkes_drift": hawkes_drift,
            "hawkes_n": hawkes_n,
            "lppls_hazard": lppls_hazard,
            "predictive_sharpe": predictive_sharpe,
            "beta_p": beta_p,  # Log adaptive weights for debugging
            "beta_m": beta_m
        }
        
        # Save for dashboard
        self.last_diagnostics = diagnostics
        
        return score, diagnostics

    # ---------- Decide and act ----------
    def decisions(self, md: dict[str, pd.DataFrame]) -> list[Decision]:
        raw: list[Decision] = []
        for sym, df in md.items():
            if df is None or df.empty or len(df) < max(252, self.cfg["el_window"]+5):
                self._log_rejection("insufficient_bars")
                continue
            
            sc, diag = self.score_symbol(df, sym)
            
            self.last_diagnostics = diag # Save for dashboard
            self._log_decision(sym, diag)
            
            if abs(sc) < self.score_th:
                logger.debug(f"{sym}: |score|={abs(sc):.3f} < threshold={self.score_th}, rejected")
                self._log_rejection("low_score")
                continue
            
            if diag.get("predictive_sharpe", 0) < self.min_predictive_sharpe:
                logger.info(f"{sym}: predictive Sharpe {diag['predictive_sharpe']:.3f} < min {self.min_predictive_sharpe}, rejected")
                self._log_rejection("low_predictive_sharpe")
                continue
            
            if self.use_hawkes and diag.get("hawkes_n", 1.0) < self.hawkes_n_min:
                logger.info(f"{sym}: Hawkes branching {diag['hawkes_n']:.3f} < min {self.hawkes_n_min}, rejected (low crowding)")
                self._log_rejection("hawkes_crowding")
                continue
            
            if self.heston is not None:
                symbol_root = self._extract_root(sym)
                vol_guard = self.heston.get_vol_guard(symbol_root)
                if vol_guard is not None:
                    current_vol = diag.get("vol", 0.0)
                    vol_ratio = current_vol / vol_guard if vol_guard > 0 else 1.0
                    
                    if self.use_graduated_heston:
                        # Graduated response: scale score down in high vol
                        if vol_ratio > 1.0:
                            vol_penalty = np.clip(1.0 - 0.5 * (vol_ratio - 1.0), 0.3, 1.0)
                            sc *= vol_penalty
                            logger.info(f"{sym}: vol_ratio={vol_ratio:.2f} → score scaled by {vol_penalty:.2f}")
                    else:
                        # Binary rejection (legacy)
                        if vol_ratio > 1.5:
                            logger.info(f"{sym}: rejected by Heston vol guard - current={current_vol:.4f} > guard={vol_guard:.4f}")
                            self._log_rejection("heston_vol_guard")
                            continue

            
            side = "BUY" if sc > 0 else "SELL"
            logger.info(f"{sym}: score={sc:.3f}, pz={diag.get('pz',0):.3f}, P(trend)={diag.get('p_trend',0):.3f}, pred_sharpe={diag.get('predictive_sharpe',0):.3f} → {side}")
            
            raw.append(Decision(symbol=sym, side=side, score=abs(sc)))

        if not raw: return []

        # correlation filter on H1 returns
        rets = {s: np.log(md[s]["close"]).diff().dropna() for s,_ in [(d.symbol, d.score) for d in raw]}
        corr = pd.DataFrame(rets).corr()

        # sort by |score| desc, pick low-corr set
        ranked = sorted([(d.symbol, d.score) for d in raw], key=lambda x: x[1], reverse=True)
        chosen_syms = low_corr_pick(ranked, corr, self.maxK, self.corr_max)

        # map back to full decision objects
        chosen = [d for d in raw if d.symbol in chosen_syms]
        return chosen

    def _extract_root(self, symbol: str) -> str:
        """Extract FX root from symbol (e.g., 'EURUSD.MINI' -> 'EURUSD')."""
        s_up = symbol.upper()
        for root in self.roots:
            if root in s_up:
                return root
        return symbol  # fallback

    def act(self, equity: float, market_data: dict[str, pd.DataFrame], *,
            all_symbols_catalog: list[str]) -> None:
        """
        Build mini-only universe, compute targets, apply cost gates, and send commands.
        """
        self.equity = equity  # Store for dashboard
        universe = self.build_universe(all_symbols_catalog)
        md = {s: market_data.get(s) for s in universe if s in market_data}

        # realised vol for dynamic target scaling — average across chosen minis
        try:
            vols = [realised_vol(np.log(df["close"]).diff()) for df in md.values() if df is not None]
            vol_now = float(np.nanmean(vols)) if vols else self.vol_ref
        except Exception:
            vol_now = self.vol_ref

        target_pct = dynamic_target_pct(vol_now, self.vol_ref, self.target_base) if self.use_dyn_target else self.target_base

        decs = self.decisions(md)
        
        # --- POSITION FILTER ---
        # Don't buy if we already hold it
        from execution.mt4_bridge_client import get_positions
        open_positions = get_positions()
        # [ {'symbol': 'EURUSD', 'lots': 0.1}, ... ]
        held_symbols = set(p['symbol'] for p in open_positions)
        
        # Filter decisions
        new_decs = []
        for d in decs:
            if d.symbol in held_symbols:
                logger.info(f"{d.symbol}: Skipping, already hold position.")
                self._log_rejection("already_held")
            else:
                new_decs.append(d)
        decs = new_decs
        # -----------------------

        # Post decisions to bridge for dashboard monitoring
        self._post_decisions_to_dashboard(decs, md, vol_now, target_pct)
        
        if not decs: return

        # Drawdown check
        # We need equity history. For now, we just use current equity vs high water mark if available.
        # Ideally, main loop should track history. Here we just do a point-in-time check if we had history.
        # Since we don't have history passed in, we skip for now or rely on main loop to kill agent.
        # But we will implement the sizing and SL logic.
        
        # expected move proxy from |score| scaled by small constant; adjust if you like
        for d in decs:
            df = md[d.symbol]
            current_price = float(df["close"].iloc[-1])
            
            # 1. Calculate Stop Loss distance (e.g. 2 * daily vol)
            # Vol is already in "vol_now" (daily sigma). 
            # Let's use individual vol if available in diagnostics, otherwise vol_now
            # Re-calculate individual vol here for precision
            local_vol = realised_vol(np.log(df["close"]).diff())
            sl_pips = (local_vol * 2.0) * current_price / (self.pip_value_per_lot/10.0) # approx conversion if needed, but easier to work in price
            
            # Simple ATR-based SL price
            sl_dist = current_price * local_vol * 2.0
            sl_price = current_price - sl_dist if d.side == "BUY" else current_price + sl_dist
            tp_dist = sl_dist * 2.0 # 2:1 Reward/Risk
            tp_price = current_price + tp_dist if d.side == "BUY" else current_price - tp_dist
            
            # 2. Position Sizing
            # Risk 1% of equity per trade
            # stop_loss_dist_pips ≈ sl_dist / 0.0001 (for non-JPY)
            # We use the utility function
            
            # Need to estimate pips. Standard pip=0.0001 usually.
            is_jpy = "JPY" in d.symbol
            pip_size = 0.01 if is_jpy else 0.0001
            sl_pips_scalar = sl_dist / pip_size
            
            # risk_utils.calculate_position_size(equity, risk_pct, stop_pips, pip_value)
            from .risk_utils import calculate_position_size
            lot_size = calculate_position_size(equity, 0.01, sl_pips_scalar, self.pip_value_per_lot)
            
            # --- LEVERAGE CAP ---
            # Enforce max margin usage per trade (simplified model assuming 1 lot = 100k units base)
            # Max Exposure = Equity * Leverage * max_margin_pct
            # Max Lots = Max Exposure / (Price * 100,000)
            max_exposure = equity * self.leverage * self.max_margin_pct
            max_lots_lev = max_exposure / (current_price * 100000.0)
            
            if lot_size > max_lots_lev:
                logger.info(f"{d.symbol}: capped size {lot_size:.2f} -> {max_lots_lev:.2f} due to leverage 1:{int(self.leverage)}")
                lot_size = max_lots_lev
            # --------------------
            
            if lot_size < 0.01:
                logger.info(f"{d.symbol}: rejected: position size {lot_size} too small (risk protection)")
                self._log_rejection("small_position")
                # update_thought(f"Skipping {d.symbol}: Position size too small") 
                continue

            # gate by expected move vs cost
            # For IG: lot_fraction = 0.10 (mini lot size) -> Now using REAL lot_size
            # We treat 1.0 lot as standard.
            
            expected_move = min(0.006, 0.003 + 0.004 * min(1.0, d.score))  # ~30-60 bps proxy
            
            # Cost gate check with logging
            # Cost ≈ spread * pip_value * lots
            
            # --- LIVE SPREAD CHECK ---
            real_spread = df.attrs.get("spread", self.avg_spread_pips)
            # -------------------------
            
            cost_cash = (real_spread * self.pip_value_per_lot * lot_size)
            # Exp gain ≈ exp_move * price * (pip_value/pip_size * lots) ... complicated.
            # Simplified: Expected Profit = Equity * exp_move * Leverage... 
            # Let's stick to the old cost gate fraction for continuity but using new size
            
            cost_fraction = cost_cash / max(equity, 1e-9)
            cost_threshold = 3.0 * cost_fraction
            
            if expected_move <= cost_threshold:
                logger.info(f"{d.symbol}: rejected: cost gate - exp_move={expected_move*100:.3f}% < 3×cost={cost_threshold*100:.3f}%")
                self._log_rejection("cost_gate")
                continue
            
            logger.info(f"{d.symbol}: SIGNAL {d.side} - score={d.score:.3f}, lots={lot_size:.2f}, SL={sl_price:.5f}, TP={tp_price:.5f}")
            
            # Send with calculated lots and SL/TP
            from execution.mt4_bridge_client import send, update_thought, post_visuals
            import time
            # Send order
            update_thought(f"SIGNAL: {d.side} {d.symbol} (Score: {d.score:.2f})")
            send(d.side, d.symbol, lots=lot_size, tp_cash=0, sl_price=sl_price, tp_price=tp_price)
            
            # VISUALS: Send Arrow
            visual_arrow = {
                "symbol": d.symbol,
                "type": "arrow",
                "side": d.side,
                "price": current_price,
                "time": int(time.time()),
                "text": f"Score: {d.score:.2f}"
            }
            post_visuals(visual_arrow)
            
            # VISUALS: Send Label (Regime)
            regime_txt = self.last_diagnostics.get("regime", "Unknown") if hasattr(self, "last_diagnostics") else "Unknown"
            visual_label = {
                "symbol": d.symbol,
                "type": "label",
                "price": current_price,
                "time": int(time.time()),
                "text": f"AI: {regime_txt} (Sc:{d.score:.2f})"
            }
            post_visuals(visual_label)
    
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
            if p_trend > 0.5:
                regime_lbl = f"{direction} Trend"
            else:
                regime_lbl = "Ranging"
            regime_line = f"REGIME: {regime_lbl} (Trend={p_trend*100:.0f}%)"
            
            # Metric 3: Hawkes (with drift and branching ratio)
            hawkes_n = last_diag.get("hawkes_n", 0.0)
            hawkes_drift = last_diag.get("hawkes_drift", 0.0)
            fragility = "UNSTABLE" if hawkes_n > 0.9 else ("Elevated" if hawkes_n > 0.7 else "Stable")
            hawkes_line = f"HAWKES: n={hawkes_n:.2f} ({fragility}) | Drift {hawkes_drift:.4f}"
            
            # Metric 4: LPPLS (with crash probability)
            lppls_h = last_diag.get("lppls_hazard", 0.0)
            crash_prob = lppls_h * 100
            crash_risk = "CRITICAL" if lppls_h > 0.8 else ("High" if lppls_h > 0.5 else "Low")
            lppls_line = f"LPPLS: Hazard {lppls_h:.3f} ({crash_risk}) | CrashP {crash_prob:.1f}%"
            
            # Metric 5: Volatility (with percentile and regime)
            vol_now_pct = vol_now * 100
            vol_pct_rank = last_diag.get("vol_percentile", 50)
            vol_regime = "HIGH" if vol_pct_rank > 80 else ("Low" if vol_pct_rank < 20 else "Normal")
            heston_line = f"VOLATILITY: {vol_now_pct:.2f}% (p{vol_pct_rank:.0f} {vol_regime}) | Tgt {target_pct*100:.1f}%" 
            
            # Metric 6: Risk & Exposure (with live spread and positions)
            lev = getattr(self, "leverage", 30.0)
            
            # Get live spread if available (from any symbol in md)
            live_spread = 0.0
            try:
                for sym_key, df_temp in md.items():
                    sp = df_temp.attrs.get("spread", 0.0)
                    if sp > 0:
                        live_spread = sp
                        break
            except:
                pass
            
            # Get position count (with fast timeout to avoid hangs)
            pos_count = 0
            total_lots = 0.0
            try:
                from execution.mt4_bridge_client import get_positions
                positions = get_positions(max_retries=1)
                pos_count = len(positions)
                total_lots = sum(p.get("lots", 0) for p in positions)
            except:
                pass
            
            risk_line = f"RISK: Lev 1:{int(lev)} | Spread {live_spread:.1f}p | Pos {pos_count} ({total_lots:.2f} lots)"
            
            # Action Summary (prepend with symbols)
            if decisions:
                # Show which symbols and sides
                signals_str = ", ".join([f"{d.symbol} {d.side}" for d in decisions[:2]])  # First 2
                if len(decisions) > 2:
                    signals_str += f" +{len(decisions)-2}"
                action_str = f"ACTION: {signals_str}"
            else:
                action_str = "ACTION: Scanning..."
                
            # Account Info
            eq_line = f"ACCOUNT: Equity ${self.equity:,.2f}"
                
            msg = f"{action_str}|{mom_line}|{regime_line}|{hawkes_line}|{lppls_line}|{heston_line}|{risk_line}|{eq_line}"
            
            # Limit length to prevent MT4 crashes (max 500 chars - sufficient for 8 lines)
            if len(msg) > 500:
                msg = msg[:497] + "..."
            
            from execution.mt4_bridge_client import update_thought
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
                        "price": close_price,
                        "target_pct": float(target_pct)
                    })
            
            requests.post(
                "http://127.0.0.1:5000/state/decisions",
                json={
                    "decisions": decisions_data, 
                    "vol": float(vol_now),
                    "diagnostics": {
                        "rejection_stats": dict(self.rejection_stats),
                        "recent_decisions": self.decision_log[-10:] if self.decision_log else []
                    }
                },
                timeout=1
            )
        except Exception:
            pass  # Don't fail trading on dashboard errors
    
    def _log_rejection(self, reason: str):
        """Track rejection reasons for diagnostics."""
        self.rejection_stats[reason] = self.rejection_stats.get(reason, 0) + 1
    
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
