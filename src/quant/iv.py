from __future__ import annotations
import math
from typing import Optional

def _Phi(x: float) -> float:
    """Standard normal CDF approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _phi(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def gk_price(F: float, K: float, T: float, rd: float, rf: float, vol: float, cp: int) -> float:
    """
    Garman-Kohlhagen (FX Black-Scholes) option price.
    cp: +1 for call, -1 for put
    """
    if T <= 0 or vol <= 0:
        return max(0.0, cp * (F - K))
    
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * sqrt_T)
    d2 = d1 - vol * sqrt_T
    
    df_d = math.exp(-rd * T)
    df_f = math.exp(-rf * T)
    
    if cp > 0:  # call
        return df_f * F * _Phi(d1) - df_d * K * _Phi(d2)
    else:  # put
        return df_d * K * _Phi(-d2) - df_f * F * _Phi(-d1)

def implied_vol_newton(price: float, F: float, K: float, T: float, rd: float, rf: float, 
                       cp: int, vol_guess: float = 0.20, max_iter: int = 50, tol: float = 1e-6) -> Optional[float]:
    """
    Newton-Raphson solver for implied volatility.
    Returns None if convergence fails.
    """
    vol = vol_guess
    for _ in range(max_iter):
        theo = gk_price(F, K, T, rd, rf, vol, cp)
        diff = theo - price
        
        if abs(diff) < tol:
            return vol
        
        # vega
        sqrt_T = math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * sqrt_T)
        vega = math.exp(-rf * T) * F * _phi(d1) * sqrt_T
        
        if vega < 1e-10:
            return None
        
        vol = vol - diff / vega
        
        if vol <= 0:
            return None
    
    return None
