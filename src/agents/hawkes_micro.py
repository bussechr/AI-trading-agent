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
        Fit Hawkes model to trade data.
        trades_df should have columns: time, side (1=buy, -1=sell)
        """
        if len(trades_df) < 50:
            logger.warning("Insufficient trades for Hawkes fitting")
            return
            
        # Simplified fitting - in production use proper MLE
        buy_times = trades_df[trades_df['side'] == 1]['time'].values
        sell_times = trades_df[trades_df['side'] == -1]['time'].values
        
        # Estimate base intensities
        T = trades_df['time'].max() - trades_df['time'].min()
        mu_buy = len(buy_times) / T
        mu_sell = len(sell_times) / T
        
        # Estimate excitation (simplified)
        self.params[0] = mu_buy
        self.params[1] = mu_sell
        self.params[2] = 0.3  # α++ (self-excitation)
        self.params[3] = 0.1  # α+- (cross-excitation)
        self.params[4] = 0.1  # α-+
        self.params[5] = 0.3  # α--
        self.params[6] = 1.0  # β+
        self.params[7] = 1.0  # β-
        
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
    Uses 1-minute bar data to estimate flow.
    """
    
    def compute_ofi(self, bars_1m: pd.DataFrame) -> pd.Series:
        """
        Compute OFI from 1-minute bars.
        OFI ≈ (close - open) * volume
        """
        if 'volume' not in bars_1m.columns:
            # Fallback: use price change only
            return (bars_1m['close'] - bars_1m['open']) / bars_1m['open']
        
        return (bars_1m['close'] - bars_1m['open']) * bars_1m['volume']
    
    def get_signal(self, bars_1m: pd.DataFrame) -> HawkesSignal:
        """
        Get Hawkes-like signal from OFI proxy.
        """
        ofi = self.compute_ofi(bars_1m)
        
        # Drift: recent OFI trend
        drift = float(ofi.tail(10).mean())
        
        # Branching proxy: OFI autocorrelation (clustering)
        if len(ofi) > 20:
            branching = float(abs(ofi.autocorr(lag=1)))
        else:
            branching = 0.5
        
        return HawkesSignal(
            drift=drift,
            branching=branching,
            lambda_buy=max(0, drift),
            lambda_sell=max(0, -drift)
        )
