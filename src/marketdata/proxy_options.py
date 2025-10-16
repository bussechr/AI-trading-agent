from __future__ import annotations
import math, numpy as np, pandas as pd
from dataclasses import dataclass
from typing import List
from .http_fx_options import OptionRow, Chain
from ..quant.iv import gk_price

def _atm_vol_from_realised(ret: pd.Series, ann=252) -> float:
    vol_d = float(ret.std(ddof=0))
    return max(1e-4, vol_d * math.sqrt(ann))

def _skew_kurt(ret: pd.Series) -> tuple[float,float]:
    r = ret.dropna()
    if len(r) < 30: return 0.0, 3.0
    m = float(r.mean()); s = float(r.std(ddof=0)) or 1e-8
    skew = float(((r-m)**3).mean() / (s**3))
    kurt = float(((r-m)**4).mean() / (s**4))
    return skew, kurt

def _strike_from_delta(F: float, T: float, rd: float, rf: float, vol: float, delta_target: float, call: bool=True) -> float:
    """FX Black delta: Δ_call = exp(-rf T) N(d1). Solve for K."""
    from math import log, sqrt
    
    def PhiInv(p):
        """Acklam's inverse-normal approximation"""
        a1=-3.969683028665376e+01; a2=2.209460984245205e+02; a3=-2.759285104469687e+02
        a4=1.383577518672690e+02; a5=-3.066479806614716e+01; a6=2.506628277459239e+00
        b1=-5.447609879822406e+01; b2=1.615858368580409e+02; b3=-1.556989798598866e+02
        b4=6.680131188771972e+01; b5=-1.328068155288572e+01
        c1=-7.784894002430293e-03; c2=-3.223964580411365e-01; c3=-2.400758277161838e+00
        c4=-2.549732539343734e+00; c5=4.374664141464968e+00; c6=2.938163982698783e+00
        d1=7.784695709041462e-03; d2=3.224671290700398e-01; d3=2.445134137142996e+00; d4=3.754408661907416e+00
        plow=0.02425; phigh=1-plow
        if p<plow:
            q=math.sqrt(-2*math.log(p)); return (((((c1*q+c2)*q+c3)*q+c4)*q+c5)*q+c6)/((((d1*q+d2)*q+d3)*q+d4)*q+1)
        if p>phigh:
            q=math.sqrt(-2*math.log(1-p)); return -(((((c1*q+c2)*q+c3)*q+c4)*q+c5)*q+c6)/((((d1*q+d2)*q+d3)*q+d4)*q+1)
        q=p-0.5; r=q*q; return (((((a1*r+a2)*r+a3)*r+a4)*r+a5)*r+a6)*q/((((b1*r+b2)*r+b3)*r+b4)*r+b5)*r+1

    df_f = math.exp(-rf*T)
    d1 = PhiInv(delta_target / max(df_f, 1e-12))
    s  = vol*math.sqrt(max(T,1e-8))
    K  = F / math.exp(d1*s - 0.5*s*s)
    return K

def build_proxy_chain(symbol_root: str, S0: float, rd: float, rf: float, close: pd.Series) -> Chain:
    """
    Construct synthetic quotes at ATM and 25Δ smile from spot stats.
    This is *not* market data; it is a heuristic proxy to let the calibrator run.
    """
    ret = np.log(close).diff()
    atm_annual = _atm_vol_from_realised(ret)
    skew, kurt = _skew_kurt(ret)
    # crude smile from skew/kurt
    rr25 = 0.10 * skew         # 25Δ risk-reversal (vol pts)
    bf25 = 0.25 * max(kurt-3, 0.0)  # 25Δ butterfly
    maturities = [5/252, 21/252, 63/252]  # ~1w, 1m, 3m
    rows: List[OptionRow] = []
    for T in maturities:
        atm = atm_annual  # keep flat in this proxy; you can term-structure from HAR if you like
        vol_25c = max(1e-4, atm + 0.5*bf25 + 0.5*rr25)
        vol_25p = max(1e-4, atm + 0.5*bf25 - 0.5*rr25)
        vol_atm = atm
        F = S0*math.exp((rd-rf)*T)
        Kc = _strike_from_delta(F,T,rd,rf,vol_25c, delta_target=0.25, call=True)
        Kp = _strike_from_delta(F,T,rd,rf,vol_25p, delta_target=0.25, call=False)
        Ka = F  # ATM-forward
        # synth mid prices with tiny spreads
        pc = gk_price(F,Kc,T,rd,rf,vol_25c,+1); pp = gk_price(F,Kp,T,rd,rf,vol_25p,-1); pa = gk_price(F,Ka,T,rd,rf,vol_atm,+1)
        rows += [
            OptionRow(K=float(Kc), T=float(T), cp="C", bid=pc*0.99, ask=pc*1.01),
            OptionRow(K=float(Kp), T=float(T), cp="P", bid=pp*0.99, ask=pp*1.01),
            OptionRow(K=float(Ka), T=float(T), cp="C", bid=pa*0.99, ask=pa*1.01),
        ]
    return Chain(symbol_root=symbol_root, S0=float(S0), rd=float(rd), rf=float(rf), rows=rows)
