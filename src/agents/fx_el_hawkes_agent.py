from __future__ import annotations
import os, json, math
from dataclasses import dataclass
import numpy as np
import pandas as pd
import logging

from .risk_utils import el_pz, regime_tilt, dynamic_target_pct, realised_vol, cost_gate, low_corr_pick
from ..execution.mt4_bridge_client import send
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
        self.vol_ref = float(cfg.get("vol_ref", 0.010))
        self.target_base = float(cfg.get("target_base_pct", 0.010))
        self.avg_spread_pips = float(cfg.get("avg_spread_pips", 0.8))
        self.pip_value_per_lot = float(cfg.get("pip_value_per_lot", 10.0))
        self.ig_mini_lot = float(cfg.get("ig_mini_lot_size", 0.10))
        
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
            from ..marketdata.http_fx_options import HTTPFXOptionProvider
            provider = HTTPFXOptionProvider(
                url_template=cfg.get("options_url_template", "https://api.example.com/fx/chain?symbol={symbol}"),
                headers={"Authorization": f"Bearer {cfg.get('options_api_key', '')}"},
                field_map=cfg.get("options_field_map", {})
            )
            logger.info("Initialized HTTP options provider for Heston calibration")
        else:
            from ..marketdata.proxy_provider import ProxyOptionProvider
            
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
        
        beta_p = self.cfg.get("beta_p", 1.0)
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
            
            # Add micro drift to score
            beta_m = self.cfg.get("beta_m", 0.3)
            # Standardize drift
            drift_std = hawkes_drift / (abs(hawkes_drift) + 0.01)
            score += beta_m * drift_std
        
        lppls_hazard = 0.0
        if self.use_lppls:
            lppls_hazard = self.lppls.get_hazard(close)
            if lppls_hazard > self.lppls_threshold and score > 0:
                # Reduce long exposure in high crash hazard
                logger.info(f"{symbol}: LPPLS hazard {lppls_hazard:.2f} > threshold, reducing long score")
                score *= (1.0 - lppls_hazard)
        
        predictive_sharpe = 0.0
        if self.regime_fitted:
            mixture = self.regime_model.get_predictive_mixture(pz_val)
            predictive_sharpe = mixture.sharpe
        
        diagnostics = {
            "pz": pz_val,
            "p_trend": p_trend,
            "score": score,
            "vol": float(r.rolling(96).std(ddof=0).iloc[-1]) if len(r) > 96 else 0.0,
            "hawkes_drift": hawkes_drift,
            "hawkes_n": hawkes_n,
            "lppls_hazard": lppls_hazard,
            "predictive_sharpe": predictive_sharpe
        }
        
        return score, diagnostics

    # ---------- Decide and act ----------
    def decisions(self, md: dict[str, pd.DataFrame]) -> list[Decision]:
        raw: list[Decision] = []
        for sym, df in md.items():
            if df is None or df.empty or len(df) < max(252, self.cfg["el_window"]+5):
                self._log_rejection("insufficient_bars")
                continue
            
            sc, diag = self.score_symbol(df, sym)
            
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
                    if current_vol > vol_guard * 1.5:
                        logger.info(f"{sym}: rejected by Heston vol guard - current={current_vol:.4f} > guard={vol_guard:.4f}")
                        self._log_rejection("heston_vol_guard")
                        continue
            
            side = "BUY" if sc > 0 else "SELL"
            logger.info(f"{sym}: score={sc:.3f}, pz={diag.get('pz',0):.3f}, P(trend)={diag.get('p_trend',0):.3f}, pred_sharpe={diag.get('predictive_sharpe',0):.3f} → {side}")
            
            raw.append(Decision(sym=sym, side=side, score=abs(sc)))

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
        
        # Post decisions to bridge for dashboard monitoring
        self._post_decisions_to_dashboard(decs, md, vol_now, target_pct)
        
        if not decs: return

        # expected move proxy from |score| scaled by small constant; adjust if you like
        for d in decs:
            df = md[d.symbol]
            # gate by expected move vs cost
            # For IG: lot_fraction = 0.10 (mini lot size)
            lot_fraction = self.ig_mini_lot if not self.mini_suffixes else 0.01
            expected_move = min(0.006, 0.003 + 0.004 * min(1.0, d.score))  # ~30-60 bps proxy
            
            # Cost gate check with logging
            cost_fraction = (self.avg_spread_pips * self.pip_value_per_lot * lot_fraction) / max(equity, 1e-9)
            cost_threshold = 3.0 * cost_fraction
            
            if expected_move <= cost_threshold:
                logger.info(f"{d.symbol}: rejected: cost gate - exp_move={expected_move*100:.3f}% < 3×cost={cost_threshold*100:.3f}%")
                self._log_rejection("cost_gate")
                continue
            
            logger.info(f"{d.symbol}: SIGNAL {d.side} - score={d.score:.3f}, exp_move={expected_move*100:.2f}%, cost={cost_fraction*100:.4f}%, target={target_pct*100:.2f}%")
            
            # Send with lots=0.0 so EA enforces minimum (0.10 for IG minis)
            send(d.side, d.symbol, lots=0.0, tp_cash=equity * target_pct)
    
    def _post_decisions_to_dashboard(self, decisions: list[Decision], 
                                     md: dict[str, pd.DataFrame],
                                     vol_now: float, target_pct: float) -> None:
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
