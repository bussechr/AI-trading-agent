from __future__ import annotations
import math
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import List, Tuple

from .http_fx_options import OptionRow, Chain  # reuse dataclasses
from quant.iv import gk_price, phi_inv


def _atm_vol_from_realised(ret: pd.Series, ann: int = 252) -> float:
    vol_d = float(ret.std(ddof=0))
    return max(1e-4, vol_d * math.sqrt(ann))


def _skew_kurt(ret: pd.Series) -> Tuple[float, float]:
    r = ret.dropna()
    if len(r) < 30:
        return 0.0, 3.0
    m = float(r.mean())
    s = float(r.std(ddof=0)) or 1e-8
    skew = float(((r - m) ** 3).mean() / (s ** 3))
    kurt = float(((r - m) ** 4).mean() / (s ** 4))
    return skew, kurt


def _strike_from_delta(F: float, T: float, rd: float, rf: float, vol: float,
                        delta_target: float, call: bool = True) -> float:
    # FX Black delta: Δ_call = exp(-rf T) N(d1). Solve for K.
    df_f = math.exp(-rf * T)
    d1 = phi_inv(delta_target / max(df_f, 1e-12))
    s = vol * math.sqrt(max(T, 1e-8))
    K = F / math.exp(d1 * s - 0.5 * s * s)
    return K


def build_proxy_chain(symbol_root: str, S0: float, rd: float, rf: float,
                      close: pd.Series) -> Chain:
    """
    Construct synthetic quotes at ATM and 25Δ smile from spot stats.
    This is *not* market data; it is a heuristic proxy to let the calibrator run.
    """
    close = pd.Series(close).dropna()
    ret = np.log(close).diff()
    atm_annual = _atm_vol_from_realised(ret)
    skew, kurt = _skew_kurt(ret)

    # crude smile from skew/kurt
    rr25 = 0.10 * skew  # 25Δ risk-reversal (vol pts)
    bf25 = 0.25 * max(kurt - 3, 0.0)  # 25Δ butterfly

    maturities = [5 / 252, 21 / 252, 63 / 252]  # ~1w, 1m, 3m
    rows: List[OptionRow] = []
    for T in maturities:
        atm = atm_annual  # flat term in this proxy
        vol_25c = max(1e-4, atm + 0.5 * bf25 + 0.5 * rr25)
        vol_25p = max(1e-4, atm + 0.5 * bf25 - 0.5 * rr25)
        vol_atm = atm
        F = S0 * math.exp((rd - rf) * T)
        Kc = _strike_from_delta(F, T, rd, rf, vol_25c, delta_target=0.25, call=True)
        Kp = _strike_from_delta(F, T, rd, rf, vol_25p, delta_target=0.25, call=False)
        Ka = F  # ATM-forward
        # synth mid prices with tiny spreads
        pc = gk_price(F, Kc, T, rd, rf, vol_25c, +1)
        pp = gk_price(F, Kp, T, rd, rf, vol_25p, -1)
        pa = gk_price(F, Ka, T, rd, rf, vol_atm, +1)
        rows += [
            OptionRow(K=float(Kc), T=float(T), cp="C", bid=pc * 0.99, ask=pc * 1.01),
            OptionRow(K=float(Kp), T=float(T), cp="P", bid=pp * 0.99, ask=pp * 1.01),
            OptionRow(K=float(Ka), T=float(T), cp="C", bid=pa * 0.99, ask=pa * 1.01),
        ]

    return Chain(symbol_root=symbol_root, S0=float(S0), rd=float(rd), rf=float(rf), rows=rows)
