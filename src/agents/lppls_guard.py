"""
Log-Periodic Power Law Singularity (LPPLS) crash hazard detector.
Identifies bubble-like price dynamics on daily timeframe.
"""
import numpy as np
import pandas as pd
from scipy.optimize import differential_evolution
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class LPPLSResult:
    """LPPLS fit result."""
    tc: float  # Critical time (crash time estimate)
    m: float  # Power law exponent
    omega: float  # Log-periodic frequency
    hazard: float  # Crash hazard score [0, 1]
    confidence: float  # Fit confidence
    days_to_tc: float  # Days until estimated crash

class LPPLSDetector:
    """
    LPPLS model: ln(p(t)) = A + B(tc - t)^m + C(tc - t)^m cos(ω ln(tc - t) + φ)
    Detects super-exponential growth with log-periodic oscillations.
    """
    
    def __init__(self, window: int = 252):
        self.window = window  # Lookback window (trading days)
        self.last_fit = None
        
    def fit(self, prices: pd.Series) -> LPPLSResult | None:
        """
        Fit LPPLS model to price series.
        Returns None if fit fails or insufficient data.
        """
        if len(prices) < self.window:
            return None
            
        # Use recent window
        p = prices.tail(self.window).values
        t = np.arange(len(p))
        
        # Log prices
        log_p = np.log(p)
        
        # Fit LPPLS (simplified - full implementation is complex)
        try:
            result = self._fit_lppls(t, log_p)
            self.last_fit = result
            return result
        except Exception as e:
            logger.warning(f"LPPLS fit failed: {e}")
            return None
    
    def _fit_lppls(self, t: np.ndarray, log_p: np.ndarray) -> LPPLSResult:
        """
        Fit LPPLS model using differential evolution.
        Simplified version - production would use more sophisticated fitting.
        """
        T = len(t)
        t_max = t[-1]
        
        # Parameter bounds: [tc, m, omega]
        # tc: critical time (must be > t_max)
        # m: power law exponent (0.1 to 0.9)
        # omega: log-periodic frequency (2 to 25)
        bounds = [
            (t_max + 5, t_max + 120),  # tc: 5-120 days ahead
            (0.1, 0.9),  # m
            (2.0, 25.0)  # omega
        ]
        
        def objective(params):
            tc, m, omega = params
            
            # Compute LPPLS prediction
            dt = tc - t
            if np.any(dt <= 0):
                return 1e10
            
            # Linear fit for A, B, C given tc, m, omega
            X = np.column_stack([
                np.ones(T),
                dt**m,
                dt**m * np.cos(omega * np.log(dt))
            ])
            
            try:
                coeffs = np.linalg.lstsq(X, log_p, rcond=None)[0]
                pred = X @ coeffs
                mse = np.mean((log_p - pred)**2)
                return mse
            except:
                return 1e10
        
        # Optimize
        result = differential_evolution(
            objective,
            bounds,
            maxiter=50,
            popsize=10,
            seed=42,
            workers=1
        )
        
        tc, m, omega = result.x
        
        # Compute hazard score
        days_to_tc = tc - t_max
        
        # Hazard increases as we approach tc and with steeper m
        hazard = np.clip(1.0 - (days_to_tc / 60.0), 0, 1) * (m / 0.9)
        
        # Confidence based on fit quality
        confidence = np.clip(1.0 - result.fun, 0, 1)
        
        return LPPLSResult(
            tc=float(tc),
            m=float(m),
            omega=float(omega),
            hazard=float(hazard),
            confidence=float(confidence),
            days_to_tc=float(days_to_tc)
        )
    
    def get_hazard(self, prices: pd.Series) -> float:
        """
        Get current crash hazard score [0, 1].
        Returns 0 if no fit available.
        """
        result = self.fit(prices)
        if result is None:
            return 0.0
        return result.hazard
    
    def should_reduce_exposure(self, prices: pd.Series, threshold: float = 0.6) -> bool:
        """
        Check if crash hazard warrants reducing exposure.
        """
        hazard = self.get_hazard(prices)
        return hazard > threshold
