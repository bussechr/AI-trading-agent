from __future__ import annotations
import pandas as pd
from typing import Callable
from .proxy_options import build_proxy_chain, Chain

class ProxyOptionProvider:
    """
    Builds a synthetic chain from spot closes supplied by a callback: get_close(symbol_root)->Series.
    """
    def __init__(self, get_close: Callable[[str], pd.Series], get_s0: Callable[[str], float],
                 rd: float = 0.0, rf: float = 0.0):
        self.get_close = get_close
        self.get_s0 = get_s0
        self.rd = rd
        self.rf = rf

    def get_chain(self, symbol_root: str) -> Chain:
        close = self.get_close(symbol_root)
        S0 = float(self.get_s0(symbol_root))
        return build_proxy_chain(symbol_root, S0, self.rd, self.rf, close)
