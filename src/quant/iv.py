from __future__ import annotations
import math
from typing import Tuple

# --- Normal distribution helpers ---

def phi(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def n_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def phi_inv(p: float) -> float:
    """Acklam's inverse-normal approximation (accurate for p in (0,1))."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf

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
        return (((((c1*q + c2)*q + c3)*q + c4)*q + c5)*q + c6) / (((d1*q + d2)*q + d3)*q + d4)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c1*q + c2)*q + c3)*q + c4)*q + c5)*q + c6) / (((d1*q + d2)*q + d3)*q + d4)

    q = p - 0.5
    r = q * q
    return (((((a1*r + a2)*r + a3)*r + a4)*r + a5)*r + a6) * q / (((((b1*r + b2)*r + b3)*r + b4)*r + b5)*r + 1)


# --- Garman–Kohlhagen (FX Black) ---

def _d1_d2(F: float, K: float, T: float, vol: float) -> Tuple[float, float]:
    T = max(T, 1e-12)
    s = max(vol, 1e-12) * math.sqrt(T)
    if K <= 0 or F <= 0:
        return 0.0, 0.0
    m = math.log(F / K)
    d1 = m / s + 0.5 * s
    d2 = d1 - s
    return d1, d2


def gk_price(F: float, K: float, T: float, rd: float, rf: float, vol: float, cp_sign: int) -> float:
    """FX option price under Garman–Kohlhagen. cp_sign: +1 call, -1 put.
    F is the forward S0*exp((rd-rf)T).
    Return price in domestic currency.
    """
    df_d = math.exp(-rd * max(T, 0.0))
    d1, d2 = _d1_d2(F, K, T, vol)
    if cp_sign >= 0:
        return df_d * (F * phi(d1) - K * phi(d2))
    else:
        return df_d * (K * phi(-d2) - F * phi(-d1))


def gk_vega(F: float, K: float, T: float, rd: float, rf: float, vol: float) -> float:
    df_d = math.exp(-rd * max(T, 0.0))
    d1, _ = _d1_d2(F, K, T, vol)
    return df_d * F * n_pdf(d1) * math.sqrt(max(T, 1e-12))


def implied_vol_gk(F: float, K: float, T: float, rd: float, rf: float, price: float, cp_sign: int,
                   tol: float = 1e-7, max_iter: int = 100, v_min: float = 1e-6, v_max: float = 5.0) -> float:
    """Robust bisection-based implied vol solver for GK prices."""
    # Clamp inputs
    T = max(T, 1e-12)
    price = max(price, 0.0)
    df_d = math.exp(-rd * T)

    # Intrinsic value bounds
    if cp_sign >= 0:
        intrinsic = df_d * max(F - K, 0.0)
    else:
        intrinsic = df_d * max(K - F, 0.0)
    if price < intrinsic * 0.99999:
        return v_min

    low, high = v_min, v_max
    p_low = gk_price(F, K, T, rd, rf, low, cp_sign)
    p_high = gk_price(F, K, T, rd, rf, high, cp_sign)

    # Ensure monotonicity bracket
    for _ in range(10):
        if p_low <= price <= p_high:
            break
        high *= 2.0
        p_high = gk_price(F, K, T, rd, rf, high, cp_sign)
        if high > 50.0:
            break

    for _ in range(max_iter):
        mid = 0.5 * (low + high)
        p_mid = gk_price(F, K, T, rd, rf, mid, cp_sign)
        if abs(p_mid - price) < tol:
            return max(mid, v_min)
        if p_mid > price:
            high = mid
        else:
            low = mid
    return max(0.5 * (low + high), v_min)


__all__ = [
    "phi", "phi_inv", "n_pdf",
    "gk_price", "gk_vega", "implied_vol_gk",
]
