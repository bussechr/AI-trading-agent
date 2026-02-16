"""
Bivariate Hawkes process for self-exciting microstructure.
Captures order flow clustering and provides drift proxy + branching ratio.
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class HawkesSignal:
    """Hawkes microstructure signal."""
    drift: float  # m_t = λ+ - λ- (directional flow)
    branching: float  # n = (α+ + α-) / (β+ + β-) (crowding measure)
    lambda_buy: float  # Buy intensity
    lambda_sell: float  # Sell intensity

class BivariateHawkes:
    """
    Bivariate Hawkes process for buy/sell order flow.
    λ_i(t) = μ_i + Σ_j α_ij Σ_{t_k < t} exp(-β_ij(t - t_k))
    """
    
    def __init__(self):
        # Parameters: [μ+, μ-, α++, α+-, α-+, α--, β+, β-]
        self.params = np.array([0.1, 0.1, 0.3, 0.1, 0.1, 0.3, 1.0, 1.0])
        self.fitted = False
        
    def fit(self, trades_df: pd.DataFrame, max_iter: int = 100) -> None:
        """
        Fit Hawkes model to buy/sell timestamps using Maximum Likelihood Estimation.
        """
        if len(trades_df) < 50:
            logger.warning("Insufficient trades for Hawkes fitting")
            return
            
        buy_times = np.sort(trades_df[trades_df['side'] == 1]['time'].values)
        sell_times = np.sort(trades_df[trades_df['side'] == -1]['time'].values)
        
        T_start = min(buy_times[0], sell_times[0]) if len(buy_times) > 0 and len(sell_times) > 0 else 0
        T_end = max(buy_times[-1], sell_times[-1]) if len(buy_times) > 0 and len(sell_times) > 0 else 0
        duration = T_end - T_start
        
        if duration <= 0: return

        # Normalize times to [0, duration]
        buy_times = (buy_times - T_start).astype(float)
        sell_times = (sell_times - T_start).astype(float)
        
        def neg_log_likelihood(params):
            # params = [mu_p, mu_m, alpha_pp, alpha_pm, alpha_mp, alpha_mm, beta_p, beta_m]
            # Ensure positive constraints
            if np.any(params <= 1e-6): return 1e10
            
            mu_p, mu_m = params[0], params[1]
            a_pp, a_pm, a_mp, a_mm = params[2:6]
            b_p, b_m = params[6], params[7]
            
            # Recursive intensity calculation (Ogata's method linear time)
            ll = -mu_p * duration - mu_m * duration
            
            # Compensator term (integral of intensities)
            # Integral of recursive part: sum(alpha/beta * (1 - exp(-beta*(T-t_k))))
            ll -= np.sum((a_pp/b_p) * (1 - np.exp(-b_p * (duration - buy_times))))
            ll -= np.sum((a_pm/b_p) * (1 - np.exp(-b_p * (duration - sell_times))))
            ll -= np.sum((a_mp/b_m) * (1 - np.exp(-b_m * (duration - buy_times))))
            ll -= np.sum((a_mm/b_m) * (1 - np.exp(-b_m * (duration - sell_times))))
            
            # Log intensities at event times
            # This part is computationally expensive in pure Python, so we use a simplified
            # approximation or just fit the first order moments for speed in this demo.
            # For this audit fix, we will use a "Method of Moments" style proxy which is faster
            # than full MLE in pure Python but better than hardcoded values.
            # However, since the instruction requested MLE, we will implement a basic MLE loop 
            # but limit the history lookback for performance.
            
            def get_intensity(t, events, alpha, beta):
                # Sum_{tk < t} alpha * exp(-beta * (t - tk))
                # optimization: only look at recent 50 events
                mask = (events < t) & (events > t - (10.0/beta)) # decay cutoff
                nearby = events[mask]
                if len(nearby) == 0: return 0.0
                return np.sum(alpha * np.exp(-beta * (t - nearby)))

            # Sum log(lambda(ti))
            # Optimization: limit to subset of points to keep fit time < 1s
            sample_buys = buy_times[::max(1, len(buy_times)//100)] # sample 100 pts
            sample_sells = sell_times[::max(1, len(sell_times)//100)]

            term_p = 0.0
            for t in sample_buys:
                lam = mu_p + get_intensity(t, buy_times, a_pp, b_p) + get_intensity(t, sell_times, a_pm, b_p)
                term_p += np.log(max(1e-6, lam))
            
            term_m = 0.0
            for t in sample_sells:
                lam = mu_m + get_intensity(t, buy_times, a_mp, b_m) + get_intensity(t, sell_times, a_mm, b_m)
                term_m += np.log(max(1e-6, lam))
                
            # Scale up sample to full size
            ll += term_p * (len(buy_times) / len(sample_buys))
            ll += term_m * (len(sell_times) / len(sample_sells))
            
            return -ll

        # Initial guess
        x0 = [
             len(buy_times)/duration, len(sell_times)/duration, # mus
             0.2, 0.1, 0.1, 0.2, # alphas
             2.0, 2.0 # betas
        ]
        
        # Bounds
        bounds = [(1e-3, None)] * 8
        
        # Optimize
        res = minimize(neg_log_likelihood, x0, bounds=bounds, method='L-BFGS-B', 
                      options={'maxiter': 50, 'gtol': 1e-3})
        
        self.params = res.x
        self.fitted = True
        
    def compute_intensities(self, trades_df: pd.DataFrame, current_time: float) -> tuple[float, float]:
        """
        Compute current buy and sell intensities.
        Returns: (λ+, λ-)
        """
        if not self.fitted:
            return 0.1, 0.1
            
        mu_buy, mu_sell, a_pp, a_pm, a_mp, a_mm, beta_p, beta_m = self.params
        
        # Get recent trades
        recent = trades_df[trades_df['time'] < current_time].tail(100)
        
        # Compute excitation from recent buys
        buy_trades = recent[recent['side'] == 1]
        buy_excite_p = np.sum(a_pp * np.exp(-beta_p * (current_time - buy_trades['time'].values)))
        buy_excite_m = np.sum(a_mp * np.exp(-beta_m * (current_time - buy_trades['time'].values)))
        
        # Compute excitation from recent sells
        sell_trades = recent[recent['side'] == -1]
        sell_excite_p = np.sum(a_pm * np.exp(-beta_p * (current_time - sell_trades['time'].values)))
        sell_excite_m = np.sum(a_mm * np.exp(-beta_m * (current_time - sell_trades['time'].values)))
        
        lambda_buy = mu_buy + buy_excite_p + sell_excite_p
        lambda_sell = mu_sell + buy_excite_m + sell_excite_m
        
        return float(lambda_buy), float(lambda_sell)
    
    def get_signal(self, trades_df: pd.DataFrame, current_time: float) -> HawkesSignal:
        """
        Get current Hawkes signal for trading decision.
        """
        lambda_buy, lambda_sell = self.compute_intensities(trades_df, current_time)
        
        # Drift proxy: directional flow
        drift = lambda_buy - lambda_sell
        
        # Branching ratio: measure of self-excitation (crowding)
        a_pp, a_pm, a_mp, a_mm = self.params[2:6]
        beta_p, beta_m = self.params[6:8]
        branching = (a_pp + a_pm + a_mp + a_mm) / (beta_p + beta_m + 1e-12)
        
        return HawkesSignal(
            drift=float(drift),
            branching=float(branching),
            lambda_buy=float(lambda_buy),
            lambda_sell=float(lambda_sell)
        )

class OFIProxy:
    """
    Order Flow Imbalance proxy when tick data unavailable.
    Uses OHLCV bar data with volume-weighted tick rule to estimate directional flow.
    """
    
    def compute_ofi(self, bars: pd.DataFrame) -> pd.Series:
        """
        Compute OFI from OHLCV bars using volume-weighted tick rule.
        
        Tick Rule: 
        - If close > mid, classify as buy (+1)
        - If close < mid, classify as sell (-1)
        - If close == mid, use previous direction
        
        OFI = Σ(sign(ΔP) * V)
        """
        if 'volume' not in bars.columns:
            # Fallback: use simple price momentum
            return (bars['close'] - bars['open']) / bars['open']
        
        # Mid price = (high + low) / 2
        mid = (bars['high'] + bars['low']) / 2.0
        
        # Tick rule: sign based on close vs mid
        signs = np.sign(bars['close'] - mid)
        
        # Handle zero signs (close == mid): use previous sign
        signs = pd.Series(signs, index=bars.index).replace(0, np.nan).fillna(method='ffill').fillna(0)
        
        # Volume-weighted OFI
        ofi = signs * bars['volume']
        
        # Normalize by typical volume to make scale interpretable
        vol_ma = bars['volume'].rolling(20, min_periods=1).mean()
        ofi_normalized = ofi / (vol_ma + 1e-9)
        
        return ofi_normalized
    
    def get_signal(self, bars: pd.DataFrame) -> HawkesSignal:
        """
        Get Hawkes-like signal from OFI proxy.
        Uses EMA smoothing for drift and autocorrelation for branching.
        """
        ofi = self.compute_ofi(bars)
        
        # Drift: exponential moving average of recent OFI (faster reaction)
        # Use span=5 for recent trend
        ema_span = 5
        if len(ofi) >= ema_span:
            drift = float(ofi.ewm(span=ema_span, adjust=False).mean().iloc[-1])
        else:
            drift = float(ofi.mean())
        
        # Branching proxy: OFI autocorrelation at lag 1 (flow clustering measure)
        # Higher autocorr => stronger self-excitation (crowding)
        if len(ofi) > 20:
            branching = float(np.clip(abs(ofi.autocorr(lag=1)), 0, 1))
        else:
            branching = 0.5  # Neutral default
        
        # Map drift to buy/sell intensities
        # Positive drift => net buying pressure (lambda_buy > lambda_sell)
        baseline = 0.5
        lambda_buy = baseline + max(0, drift)
        lambda_sell = baseline + max(0, -drift)
        
        return HawkesSignal(
            drift=drift,
            branching=branching,
            lambda_buy=lambda_buy,
            lambda_sell=lambda_sell
        )
