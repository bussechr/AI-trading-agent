"""
Example integrations for Heston service with FX trading agent.

Two approaches:
1. Live options feed via HTTP (recommended)
2. Proxy options from spot data (fallback)
"""

# =============================================================================
# APPROACH 1: Live Options Feed (HTTP Provider)
# =============================================================================

def setup_agent_with_live_options(cfg: dict):
    """
    Configure agent with live options feed.
    
    Usage:
        agent = FXELAgent(cfg)
        agent.heston = setup_agent_with_live_options(cfg)
    """
    from .heston_service import HestonService
    from ..marketdata.http_fx_options import HTTPFXOptionProvider
    
    # Configure your options data provider
    # Replace with your actual API endpoint and credentials
    provider = HTTPFXOptionProvider(
        url_template="https://YOUR_OPTIONS_API/v1/fx/chain?symbol={symbol}",
        headers={"Authorization": "Bearer YOUR_TOKEN"},
        field_map={
            "K": "strike",           # strike price field name in API response
            "T": "tenor_years",       # time to expiry in years
            "cp": "cp",              # call/put flag
            "bid": "bid",            # bid price
            "ask": "ask",            # ask price
            "S0": "spot",            # spot price (optional if 'F' provided)
            "rd": "rd",              # domestic rate
            "rf": "rf",              # foreign rate
            "F": "forward"           # forward price (optional if 'S0' provided)
        }
    )
    
    return HestonService(
        outdir="data/heston",
        provider=provider,
        recalc_after_secs=18*3600  # once per trading day (~18 hours)
    )


# =============================================================================
# APPROACH 2: Proxy Options from Spot (Fallback)
# =============================================================================

def setup_agent_with_proxy_options(cfg: dict):
    """
    Configure agent with synthetic options built from spot statistics.
    
    Use only if you don't have access to live options quotes.
    
    Usage:
        agent = FXELAgent(cfg)
        agent.heston = setup_agent_with_proxy_options(cfg)
    """
    from .heston_service import HestonService
    from ..marketdata.proxy_provider import ProxyOptionProvider
    import pandas as pd
    import os
    
    # Callbacks to fetch spot data from your data source
    def get_close_series(symbol_root: str) -> pd.Series:
        """
        Fetch historical close prices for the symbol.
        Adjust path/logic to match your data storage.
        """
        # Example: CSV files in data/fx_minis/
        path = f"data/fx_minis/{symbol_root}.MINI.csv"
        if os.path.exists(path):
            df = pd.read_csv(path)
            return df["close"]
        
        # Fallback: return empty series
        return pd.Series(dtype=float)
    
    def get_spot(symbol_root: str) -> float:
        """Get current spot price (last close)."""
        series = get_close_series(symbol_root)
        if len(series) > 0:
            return float(series.iloc[-1])
        return 1.0  # fallback
    
    provider = ProxyOptionProvider(
        get_close=get_close_series,
        get_s0=get_spot,
        rd=0.05,  # domestic rate (USD), adjust as needed
        rf=0.03   # foreign rate, adjust as needed
    )
    
    return HestonService(
        outdir="data/heston",
        provider=provider,
        recalc_after_secs=6*3600  # can be more frequent since it's cheap
    )


# =============================================================================
# Integration with FXELAgent
# =============================================================================

def create_agent_with_heston(cfg: dict, use_live_options: bool = True):
    """
    Create FX agent with Heston service integrated.
    
    Parameters
    ----------
    cfg : dict
        Agent configuration
    use_live_options : bool
        If True, use HTTP provider (recommended).
        If False, use proxy provider (fallback).
    
    Returns
    -------
    FXELAgent
        Agent with heston service attached
    """
    from .fx_el_hawkes_agent import FXELAgent
    
    agent = FXELAgent(cfg)
    
    if use_live_options:
        agent.heston = setup_agent_with_live_options(cfg)
    else:
        agent.heston = setup_agent_with_proxy_options(cfg)
    
    return agent


# =============================================================================
# Using Heston in Trading Logic
# =============================================================================

def example_using_heston_guard(agent, symbol: str, raw_score: float) -> float:
    """
    Example: adjust trading score based on implied volatility regime.
    
    High IV → reduce size (expensive options, higher uncertainty)
    Low IV → potentially increase size (cheap protection, lower risk)
    """
    if not hasattr(agent, 'heston'):
        return raw_score  # no adjustment
    
    regime = agent.heston.get_vol_regime(symbol)
    
    if regime == 'high':
        # Scale down in high vol environments
        return raw_score * 0.5
    elif regime == 'low':
        # Could scale up in low vol (optional, be conservative)
        return raw_score * 1.0
    else:
        return raw_score


def example_using_heston_for_position_sizing(agent, symbol: str, 
                                             base_size: float) -> float:
    """
    Example: scale position size inversely with implied volatility.
    """
    if not hasattr(agent, 'heston'):
        return base_size
    
    iv = agent.heston.get_implied_vol_guard(symbol)
    if iv is None:
        return base_size
    
    # Scale inversely: higher vol → smaller size
    # Example: at 10% IV, scale=1.0; at 20% IV, scale=0.5
    vol_scale = min(2.0, 0.10 / max(iv, 0.05))
    
    return base_size * vol_scale
