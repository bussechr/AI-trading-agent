"""
Proxy option provider wrapper.

Builds synthetic option chains from spot price data via callbacks.
Use only as a fallback when real market quotes are unavailable.
"""
from __future__ import annotations
import pandas as pd
from typing import Callable

from .proxy_options import build_proxy_chain, Chain


class ProxyOptionProvider:
    """
    Builds a synthetic chain from spot closes supplied by callbacks.
    
    This is a fallback provider that generates synthetic option quotes
    from spot price statistics. It should NOT replace real market data
    in production.
    
    Example:
        def get_closes(symbol):
            df = pd.read_csv(f"data/fx/{symbol}.csv")
            return df["close"]
        
        def get_spot(symbol):
            return get_closes(symbol).iloc[-1]
        
        provider = ProxyOptionProvider(
            get_close=get_closes,
            get_s0=get_spot,
            rd=0.05,  # 5% domestic rate
            rf=0.03   # 3% foreign rate
        )
        
        chain = provider.get_chain("EURUSD")
    """
    
    def __init__(
        self,
        get_close: Callable[[str], pd.Series],
        get_s0: Callable[[str], float],
        rd: float = 0.0,
        rf: float = 0.0
    ):
        """
        Args:
            get_close: Callback returning historical close prices for a symbol
            get_s0: Callback returning current spot price for a symbol
            rd: Domestic interest rate (annualized)
            rf: Foreign interest rate (annualized)
        """
        self.get_close = get_close
        self.get_s0 = get_s0
        self.rd = rd
        self.rf = rf
    
    def get_chain(self, symbol_root: str) -> Chain:
        """
        Build synthetic option chain from spot statistics.
        
        Args:
            symbol_root: FX pair symbol (e.g., "EURUSD")
        
        Returns:
            Synthetic Chain with proxy options
        """
        close = self.get_close(symbol_root)
        S0 = float(self.get_s0(symbol_root))
        
        return build_proxy_chain(symbol_root, S0, self.rd, self.rf, close)
