from __future__ import annotations
import numpy as np
import pandas as pd

def el_pz(close: pd.Series, w: int, ema: int) -> pd.Series:
    """EL generalised momentum (display-friendly z-score, then EMA)."""
    r = np.log(close).diff()
    mu = r.rolling(w).mean()
    sig = r.rolling(w).std(ddof=0).replace(0, np.nan)
    pz = (r - mu) / sig
    return pz.ewm(span=ema, adjust=False).mean()

def regime_tilt(ret: pd.Series, w: int = 96) -> pd.Series:
    """
    Regime proxy: z-score of rolling mean of returns, squashed to [-1,1].
    If hmmlearn is installed, swap this for a proper 2-state HMM filter.
    """
    z = (ret.rolling(w).mean()) / (ret.rolling(w).std(ddof=0) + 1e-12)
    return np.tanh(2.0 * z).fillna(0.0)

def dynamic_target_pct(vol_now: float, vol_ref: float, base: float) -> float:
    """Scale 1% by sqrt(vol/vol_ref) so that cycles take similar time across regimes."""
    if not np.isfinite(vol_now) or vol_now <= 0: return base
    return float(base * np.sqrt(max(vol_now, 1e-12) / max(vol_ref, 1e-12)))

def realised_vol(ret: pd.Series, w: int = 96) -> float:
    """Rolling stdev as a simple realised volatility proxy."""
    return float(ret.rolling(w).std(ddof=0).iloc[-1])

def cost_gate(expected_move: float, spread_pips: float, pip_value_per_lot: float,
              equity: float, lot_fraction: float) -> bool:
    """
    Expected move (fraction of price) must exceed ~3x cost.
    Cost in equity terms ~ spread_pips * pip_value_per_lot * lot_fraction / equity
    """
    cost = (spread_pips * pip_value_per_lot * lot_fraction) / max(equity, 1e-9)
    return expected_move > 3.0 * cost

def low_corr_pick(cands: list[tuple[str, float]],
                  corr: pd.DataFrame, k: int, corr_max: float) -> list[str]:
    """
    Pick up to k symbols with pairwise correlation <= corr_max.
    cands: [(symbol, score_abs), ...] sorted desc by |score|
    """
    chosen: list[str] = []
    for sym, _ in cands:
        ok = True
        for s2 in chosen:
            if sym in corr.index and s2 in corr.columns:
                if abs(float(corr.loc[sym, s2])) > corr_max:
                    ok = False; break
        if ok:
            chosen.append(sym)
            if len(chosen) >= k: break
    return chosen

def check_drawdown_limit(equity_history: list[float], max_dd_pct: float = 0.10) -> bool:
    """
    Return False if drawdown from peak exceeds limit.
    equity_history: list of equity values over time.

    Note: this function scans the full list for the running peak on every call.
    For high-frequency use, callers should maintain a running peak variable instead
    of calling this on every cycle.
    """
    if not equity_history or len(equity_history) < 2:
        return True

    peak = max(equity_history)  # M1 FIX: was a manual O(N) for-loop; same complexity, simpler code
    current = equity_history[-1]
    if peak <= 0:
        return True

    dd = (peak - current) / peak
    return dd < max_dd_pct


def calculate_position_size(equity: float, risk_pct: float, stop_pips: float, 
                           pip_value: float, max_lots: float = 5.0) -> float:
    """
    Calculate position size based on fixed fractional risk.
    risk_amount = equity * risk_pct
    lot_size = risk_amount / (stop_pips * pip_value)
    """
    if stop_pips <= 0 or pip_value <= 0:
        return 0.0
        
    risk_amount = equity * risk_pct
    raw_lots = risk_amount / (stop_pips * pip_value)
    
    # Clip to max
    return min(raw_lots, max_lots)
