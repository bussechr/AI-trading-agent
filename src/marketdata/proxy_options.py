"""
Proxy options chain builder from spot price statistics.

IMPORTANT: This produces SYNTHETIC quotes from realized volatility and 
spot statistics. It is NOT equivalent to market calibration and should 
only be used as a fallback when real options quotes are unavailable.

The synthetic chain builds ATM and 25-delta options at typical maturities
using volatility smile heuristics derived from skewness and kurtosis.
"""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import List

# Reuse dataclasses from http_fx_options
from .http_fx_options import OptionRow, Chain


def _atm_vol_from_realised(ret: pd.Series, ann: int = 252) -> float:
    """
    Estimate ATM volatility from realized log-returns.
    
    Args:
        ret: Log-returns series
        ann: Annualization factor (252 for daily data)
    
    Returns:
        Annualized volatility estimate
    """
    vol_d = float(ret.std(ddof=0))
    return max(1e-4, vol_d * math.sqrt(ann))


def _skew_kurt(ret: pd.Series) -> tuple[float, float]:
    """
    Compute sample skewness and kurtosis from returns.
    
    Args:
        ret: Log-returns series
    
    Returns:
        (skewness, kurtosis) tuple
    """
    r = ret.dropna()
    if len(r) < 30:
        return 0.0, 3.0
    
    m = float(r.mean())
    s = float(r.std(ddof=0)) or 1e-8
    
    skew = float(((r - m) ** 3).mean() / (s ** 3))
    kurt = float(((r - m) ** 4).mean() / (s ** 4))
    
    return skew, kurt


def _strike_from_delta(
    F: float,
    T: float,
    rd: float,
    rf: float,
    vol: float,
    delta_target: float,
    call: bool = True
) -> float:
    """
    Solve for strike K given a target delta in FX Black model.
    
    FX convention: Delta_call = exp(-rf*T) * N(d1)
    
    Args:
        F: Forward price
        T: Time to expiry (years)
        rd: Domestic rate
        rf: Foreign rate
        vol: Volatility
        delta_target: Target delta (e.g., 0.25 for 25-delta)
        call: True for call delta, False for put delta
    
    Returns:
        Strike price K
    """
    from ..quant.iv import PhiInv
    
    df_f = math.exp(-rf * T)
    
    # For put: delta_put = -exp(-rf*T) * N(-d1)
    if not call:
        delta_target = -delta_target
    
    # Solve: delta = df_f * N(d1) => d1 = Phi^{-1}(delta / df_f)
    d1 = PhiInv(abs(delta_target) / max(df_f, 1e-12))
    
    if delta_target < 0:
        d1 = -d1
    
    s = vol * math.sqrt(max(T, 1e-8))
    K = F / math.exp(d1 * s - 0.5 * s * s)
    
    return K


def build_proxy_chain(
    symbol_root: str,
    S0: float,
    rd: float,
    rf: float,
    close: pd.Series
) -> Chain:
    """
    Construct synthetic option chain from spot price statistics.
    
    This builds a volatility smile from realized vol, skewness, and kurtosis:
    - ATM vol from realized volatility
    - Risk reversal (25Δ call - 25Δ put vol) from skewness
    - Butterfly (25Δ strangle - ATM vol) from excess kurtosis
    
    Maturities: ~1 week, 1 month, 3 months
    
    WARNING: This is NOT market data. Use only as a fallback proxy when
    real options quotes are unavailable.
    
    Args:
        symbol_root: FX pair symbol (e.g., "EURUSD")
        S0: Current spot price
        rd: Domestic interest rate
        rf: Foreign interest rate
        close: Historical close prices (for vol estimation)
    
    Returns:
        Synthetic Chain object with ATM and 25Δ options
    """
    from ..quant.iv import gk_price
    
    # Compute realized statistics
    ret = np.log(close).diff()
    atm_annual = _atm_vol_from_realised(ret)
    skew, kurt = _skew_kurt(ret)
    
    # Crude smile approximation from moments
    # Risk reversal: call vol - put vol (driven by skewness)
    rr25 = 0.10 * skew
    
    # Butterfly: (call vol + put vol)/2 - ATM (driven by kurtosis)
    bf25 = 0.25 * max(kurt - 3.0, 0.0)
    
    # Standard FX option maturities (in years)
    maturities = [
        5 / 252,   # ~1 week
        21 / 252,  # ~1 month
        63 / 252   # ~3 months
    ]
    
    rows: List[OptionRow] = []
    
    for T in maturities:
        # Flat term structure (could use HAR for term structure if desired)
        atm = atm_annual
        
        # 25-delta smile points
        vol_25c = max(1e-4, atm + 0.5 * bf25 + 0.5 * rr25)
        vol_25p = max(1e-4, atm + 0.5 * bf25 - 0.5 * rr25)
        vol_atm = atm
        
        # Forward price
        F = S0 * math.exp((rd - rf) * T)
        
        # Strikes for 25-delta options
        Kc = _strike_from_delta(F, T, rd, rf, vol_25c, delta_target=0.25, call=True)
        Kp = _strike_from_delta(F, T, rd, rf, vol_25p, delta_target=0.25, call=False)
        Ka = F  # ATM-forward
        
        # Synthetic mid prices with tiny spreads
        pc = gk_price(F, Kc, T, rd, rf, vol_25c, +1)
        pp = gk_price(F, Kp, T, rd, rf, vol_25p, -1)
        pa = gk_price(F, Ka, T, rd, rf, vol_atm, +1)
        
        # Add options with small bid-ask spread
        rows += [
            OptionRow(K=float(Kc), T=float(T), cp="C", bid=pc * 0.99, ask=pc * 1.01),
            OptionRow(K=float(Kp), T=float(T), cp="P", bid=pp * 0.99, ask=pp * 1.01),
            OptionRow(K=float(Ka), T=float(T), cp="C", bid=pa * 0.99, ask=pa * 1.01),
        ]
    
    return Chain(
        symbol_root=symbol_root,
        S0=float(S0),
        rd=float(rd),
        rf=float(rf),
        rows=rows
    )
