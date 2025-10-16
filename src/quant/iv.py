"""
Black-Scholes pricing and implied volatility utilities for FX options.
"""
from __future__ import annotations
import math
from typing import Optional


def _Phi(x: float) -> float:
    """Standard normal CDF approximation."""
    # Abramowitz & Stegun 26.2.17
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911
    
    sign = 1.0 if x >= 0 else -1.0
    x = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x)
    return 0.5 * (1.0 + sign * y)


def _phi(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def gk_price(F: float, K: float, T: float, rd: float, rf: float, vol: float, cp: int) -> float:
    """
    Garman-Kohlhagen (Black-76 for FX) option price.
    
    Args:
        F: Forward price (S0 * exp((rd-rf)*T))
        K: Strike price
        T: Time to expiry in years
        rd: Domestic risk-free rate
        rf: Foreign risk-free rate
        vol: Volatility (annualized)
        cp: +1 for call, -1 for put
    
    Returns:
        Option price in domestic currency
    """
    if T <= 0 or vol <= 0:
        return max(0.0, cp * (F * math.exp(-rf * T) - K * math.exp(-rd * T)))
    
    sqrtT = math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * sqrtT)
    d2 = d1 - vol * sqrtT
    
    df_d = math.exp(-rd * T)
    df_f = math.exp(-rf * T)
    
    if cp == 1:  # call
        return df_f * F * _Phi(d1) - df_d * K * _Phi(d2)
    else:  # put
        return df_d * K * _Phi(-d2) - df_f * F * _Phi(-d1)


def implied_vol(price: float, F: float, K: float, T: float, rd: float, rf: float, 
                cp: int, vol_guess: float = 0.15, max_iter: int = 50, 
                tol: float = 1e-6) -> Optional[float]:
    """
    Compute implied volatility via Newton-Raphson.
    
    Args:
        price: Market option price
        F: Forward price
        K: Strike price
        T: Time to expiry in years
        rd: Domestic risk-free rate
        rf: Foreign risk-free rate
        cp: +1 for call, -1 for put
        vol_guess: Initial volatility guess
        max_iter: Maximum iterations
        tol: Convergence tolerance
    
    Returns:
        Implied volatility or None if convergence fails
    """
    if T <= 0:
        return None
    
    vol = vol_guess
    for _ in range(max_iter):
        p = gk_price(F, K, T, rd, rf, vol, cp)
        diff = p - price
        
        if abs(diff) < tol:
            return vol
        
        # Vega for Newton step
        sqrtT = math.sqrt(T)
        d1 = (math.log(F / K) + 0.5 * vol * vol * T) / (vol * sqrtT)
        vega = F * math.exp(-rf * T) * _phi(d1) * sqrtT
        
        if vega < 1e-10:
            return None
        
        vol = vol - diff / vega
        
        # Clamp to reasonable range
        vol = max(0.01, min(vol, 5.0))
    
    return None


def PhiInv(p: float) -> float:
    """
    Inverse standard normal CDF (Acklam's approximation).
    
    Args:
        p: Probability (0 < p < 1)
    
    Returns:
        x such that Phi(x) = p
    """
    # Coefficients for rational approximation
    a1 = -3.969683028665376e+01
    a2 = 2.209460984245205e+02
    a3 = -2.759285104469687e+02
    a4 = 1.383577518672690e+02
    a5 = -3.066479806614716e+01
    a6 = 2.506628277459239e+00
    
    b1 = -5.447609879822406e+01
    b2 = 1.615858368580409e+02
    b3 = -1.556989798598866e+02
    b4 = 6.680131188771972e+01
    b5 = -1.328068155288572e+01
    
    c1 = -7.784894002430293e-03
    c2 = -3.223964580411365e-01
    c3 = -2.400758277161838e+00
    c4 = -2.549732539343734e+00
    c5 = 4.374664141464968e+00
    c6 = 2.938163982698783e+00
    
    d1 = 7.784695709041462e-03
    d2 = 3.224671290700398e-01
    d3 = 2.445134137142996e+00
    d4 = 3.754408661907416e+00
    
    plow = 0.02425
    phigh = 1 - plow
    
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c1*q + c2)*q + c3)*q + c4)*q + c5)*q + c6) / ((((d1*q + d2)*q + d3)*q + d4)*q + 1)
    
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c1*q + c2)*q + c3)*q + c4)*q + c5)*q + c6) / ((((d1*q + d2)*q + d3)*q + d4)*q + 1)
    
    q = p - 0.5
    r = q * q
    return (((((a1*r + a2)*r + a3)*r + a4)*r + a5)*r + a6)*q / (((((b1*r + b2)*r + b3)*r + b4)*r + b5)*r + 1)
