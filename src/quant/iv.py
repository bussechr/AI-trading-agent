"""
Black-Scholes / Garman-Kohlhagen pricing and implied volatility utilities.
"""
from __future__ import annotations
import math


def _Phi(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    a1 =  0.254829592
    a2 = -0.284496736
    a3 =  1.421413741
    a4 = -1.453152027
    a5 =  1.061405429
    p  =  0.3275911
    
    sign = 1.0 if x >= 0 else -1.0
    x = abs(x)
    
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5*t + a4)*t) + a3)*t + a2)*t + a1)*t * math.exp(-x*x/2.0)
    
    return 0.5 * (1.0 + sign * y)


def _phi(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def gk_price(F: float, K: float, T: float, rd: float, rf: float, vol: float, cp: int) -> float:
    """
    Garman-Kohlhagen (FX Black-Scholes) option price.
    
    Parameters
    ----------
    F : float
        Forward price (spot * exp((rd - rf)*T))
    K : float
        Strike price
    T : float
        Time to expiry in years
    rd : float
        Domestic risk-free rate
    rf : float
        Foreign risk-free rate
    vol : float
        Volatility (annual)
    cp : int
        +1 for call, -1 for put
    
    Returns
    -------
    float
        Option price (domestic currency per unit of foreign)
    """
    if T <= 0.0 or vol <= 0.0:
        return max(0.0, cp * (F * math.exp(-rf * T) - K * math.exp(-rd * T)))
    
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * sqrt_T)
    d2 = d1 - vol * sqrt_T
    
    if cp > 0:  # call
        return math.exp(-rf * T) * F * _Phi(d1) - math.exp(-rd * T) * K * _Phi(d2)
    else:  # put
        return math.exp(-rd * T) * K * _Phi(-d2) - math.exp(-rf * T) * F * _Phi(-d1)


def gk_vega(F: float, K: float, T: float, rd: float, rf: float, vol: float) -> float:
    """Vega of FX option (same for calls and puts)."""
    if T <= 0.0 or vol <= 0.0:
        return 0.0
    
    sqrt_T = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * sqrt_T)
    
    return F * math.exp(-rf * T) * _phi(d1) * sqrt_T


def implied_vol_newton(price: float, F: float, K: float, T: float, 
                       rd: float, rf: float, cp: int, 
                       initial_guess: float = 0.2, 
                       max_iter: int = 50, tol: float = 1e-6) -> float:
    """
    Compute implied volatility via Newton-Raphson.
    
    Returns
    -------
    float
        Implied volatility, or NaN if no convergence
    """
    vol = initial_guess
    
    for _ in range(max_iter):
        calc_price = gk_price(F, K, T, rd, rf, vol, cp)
        diff = calc_price - price
        
        if abs(diff) < tol:
            return vol
        
        vega = gk_vega(F, K, T, rd, rf, vol)
        if vega < 1e-10:
            return float('nan')
        
        vol -= diff / vega
        
        # Keep vol in reasonable bounds
        if vol < 0.001:
            vol = 0.001
        elif vol > 5.0:
            vol = 5.0
    
    return float('nan')
