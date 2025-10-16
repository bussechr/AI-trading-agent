"""
HTTP provider for live FX option chains.

Fetches real market quotes from a REST API endpoint and maps fields
into a standard structure for Heston calibration.
"""
from __future__ import annotations
import math
import requests
from dataclasses import dataclass
from typing import List, Dict, Optional


@dataclass
class OptionRow:
    """Single option contract specification."""
    K: float      # Strike price
    T: float      # Time to expiry (years)
    cp: str       # 'C' for call, 'P' for put
    bid: float    # Bid price
    ask: float    # Ask price


@dataclass
class Chain:
    """Complete option chain with market data."""
    symbol_root: str    # Underlying symbol (e.g., "EURUSD")
    S0: float          # Spot price
    rd: float          # Domestic interest rate
    rf: float          # Foreign interest rate
    rows: List[OptionRow]  # Option contracts


class HTTPFXOptionProvider:
    """
    Pulls an option chain via REST and maps fields into a standard structure.
    
    The endpoint should return JSON in one of two formats:
    1. Dict with 'meta' + 'rows': {"meta": {...}, "rows": [...]}
    2. List of rows directly: [...]
    
    Each row must have: {K, T, cp, bid, ask}
    Meta should contain: S0, rd, rf (or F for forward price)
    
    Example:
        provider = HTTPFXOptionProvider(
            url_template="https://api.example.com/v1/fx/chain?symbol={symbol}",
            headers={"Authorization": "Bearer YOUR_TOKEN"},
            field_map={
                "K": "strike",
                "T": "tenor_years",
                "cp": "call_put",
                "bid": "bid_price",
                "ask": "ask_price",
                "S0": "spot",
                "rd": "domestic_rate",
                "rf": "foreign_rate",
                "F": "forward"
            }
        )
        chain = provider.get_chain("EURUSD")
    """
    
    def __init__(
        self,
        url_template: str,
        headers: Optional[dict] = None,
        field_map: Optional[dict] = None,
        timeout: float = 10.0
    ):
        """
        Args:
            url_template: URL with {symbol} placeholder
            headers: HTTP headers (e.g., auth tokens)
            field_map: Mapping from standard names to API field names
            timeout: Request timeout in seconds
        """
        self.url_template = url_template
        self.headers = headers or {}
        self.timeout = timeout
        
        # Default field mapping
        default_map = {
            "K": "K",
            "T": "T",
            "cp": "cp",
            "bid": "bid",
            "ask": "ask",
            "S0": "S0",
            "rd": "rd",
            "rf": "rf",
            "F": "F"
        }
        self.fm = field_map or default_map
    
    def get_chain(self, symbol_root: str) -> Chain:
        """
        Fetch option chain for a symbol.
        
        Args:
            symbol_root: FX pair symbol (e.g., "EURUSD")
        
        Returns:
            Chain object with spot, rates, and option rows
        
        Raises:
            requests.HTTPError: If API request fails
            ValueError: If required fields are missing
        """
        url = self.url_template.format(symbol=symbol_root)
        r = requests.get(url, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        
        # Parse response format
        if isinstance(data, dict) and "rows" in data:
            meta = data
            rows_json = data["rows"]
        else:
            meta = {}
            rows_json = data
        
        # Extract metadata
        S0 = meta.get(self.fm["S0"], None)
        rd = float(meta.get(self.fm["rd"], 0.0))
        rf = float(meta.get(self.fm["rf"], 0.0))
        F = meta.get(self.fm["F"], None)
        
        # Parse option rows
        rows: List[OptionRow] = []
        for row in rows_json:
            rows.append(OptionRow(
                K=float(row[self.fm["K"]]),
                T=float(row[self.fm["T"]]),
                cp=str(row[self.fm["cp"]]).upper()[0],
                bid=float(row[self.fm["bid"]]),
                ask=float(row[self.fm["ask"]]),
            ))
        
        # Derive S0 from forward if not provided
        if S0 is None:
            if F is None:
                raise ValueError(
                    f"Provider payload missing S0 (or F). "
                    f"Check field_map: {self.fm}"
                )
            # Back out S0 from forward at shortest maturity
            Tmin = min(rw.T for rw in rows) if rows else 0.0
            S0 = float(F) * math.exp(-(rd - rf) * Tmin)
        
        return Chain(
            symbol_root=symbol_root,
            S0=float(S0),
            rd=rd,
            rf=rf,
            rows=rows
        )
