"""
Heston calibration service for FX options.

Fetches option chains (live or proxy), calibrates Heston parameters,
caches results, and provides volatility guards/scalers for trading decisions.
"""
from __future__ import annotations
import os, json, time, logging
from typing import Optional, Protocol
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


class OptionProvider(Protocol):
    """Protocol for option chain providers (HTTP or Proxy)."""
    def get_chain(self, symbol_root: str):
        """Return a Chain object with S0, rd, rf, and rows (OptionRow list)."""
        ...


@dataclass
class HestonParams:
    """Calibrated Heston model parameters."""
    v0: float      # initial variance
    theta: float   # long-term variance
    kappa: float   # mean reversion speed
    sigma: float   # vol of vol
    rho: float     # correlation
    timestamp: float
    symbol: str


class HestonService:
    """
    Manages Heston calibration lifecycle:
    1. Fetch option chain from provider
    2. Calibrate Heston parameters
    3. Cache to JSON with timestamp
    4. Provide guards/scalers for agent
    """
    
    def __init__(self, outdir: str, provider: OptionProvider, 
                 recalc_after_secs: float = 18*3600):
        """
        Parameters
        ----------
        outdir : str
            Directory to store cached Heston parameter JSON files
        provider : OptionProvider
            Either HTTPFXOptionProvider or ProxyOptionProvider
        recalc_after_secs : float
            Recalibrate if cached params older than this (default 18h)
        """
        self.outdir = outdir
        self.provider = provider
        self.recalc_after_secs = recalc_after_secs
        os.makedirs(outdir, exist_ok=True)
        self._cache: dict[str, HestonParams] = {}
    
    def get_params(self, symbol_root: str, force_refresh: bool = False) -> Optional[HestonParams]:
        """
        Get cached or freshly calibrated Heston parameters for a symbol.
        
        Returns None if calibration fails or provider errors.
        """
        # Check memory cache first
        if not force_refresh and symbol_root in self._cache:
            params = self._cache[symbol_root]
            age = time.time() - params.timestamp
            if age < self.recalc_after_secs:
                logger.debug(f"{symbol_root}: using cached Heston params (age={age/3600:.1f}h)")
                return params
        
        # Check disk cache
        cache_path = os.path.join(self.outdir, f"{symbol_root}_heston.json")
        if not force_refresh and os.path.exists(cache_path):
            try:
                with open(cache_path, 'r') as f:
                    data = json.load(f)
                age = time.time() - data['timestamp']
                if age < self.recalc_after_secs:
                    params = HestonParams(**data)
                    self._cache[symbol_root] = params
                    logger.debug(f"{symbol_root}: loaded Heston from disk (age={age/3600:.1f}h)")
                    return params
            except Exception as e:
                logger.warning(f"{symbol_root}: failed to load cached params: {e}")
        
        # Need fresh calibration
        logger.info(f"{symbol_root}: fetching option chain and calibrating Heston")
        try:
            chain = self.provider.get_chain(symbol_root)
            params = self._calibrate(chain)
            
            # Cache to disk and memory
            with open(cache_path, 'w') as f:
                json.dump(asdict(params), f, indent=2)
            
            self._cache[symbol_root] = params
            logger.info(f"{symbol_root}: calibration complete - v0={params.v0:.4f}, theta={params.theta:.4f}, kappa={params.kappa:.2f}")
            return params
            
        except Exception as e:
            logger.error(f"{symbol_root}: calibration failed: {e}")
            return None
    
    def _calibrate(self, chain) -> HestonParams:
        """
        Calibrate Heston to option chain.
        
        For now, returns a simplified calibration using ATM vol as a proxy.
        A full implementation would use optimization to fit all parameters
        to the entire surface (strikes + maturities).
        """
        from ..quant.iv import implied_vol_newton
        import math
        
        # Extract ATM options (closest to forward)
        atm_vols = []
        for row in chain.rows:
            F = chain.S0 * math.exp((chain.rd - chain.rf) * row.T)
            if abs(row.K - F) / F < 0.05:  # within 5% of ATM
                mid_price = (row.bid + row.ask) / 2.0
                cp = +1 if row.cp == 'C' else -1
                iv = implied_vol_newton(mid_price, F, row.K, row.T, 
                                       chain.rd, chain.rf, cp)
                if math.isfinite(iv) and 0.01 < iv < 2.0:
                    atm_vols.append(iv)
        
        if not atm_vols:
            raise ValueError("No valid ATM options found for calibration")
        
        # Simple approximation: use ATM vol as starting point
        atm_vol = sum(atm_vols) / len(atm_vols)
        v0 = atm_vol * atm_vol  # variance
        
        # Heuristic defaults (institutional calibration would fit these)
        params = HestonParams(
            v0=v0,
            theta=v0,           # long-term = current (simplification)
            kappa=2.0,          # moderate mean reversion
            sigma=0.3,          # typical vol-of-vol for FX
            rho=-0.7,           # negative correlation (leverage effect)
            timestamp=time.time(),
            symbol=chain.symbol_root
        )
        
        return params
    
    def get_implied_vol_guard(self, symbol_root: str) -> Optional[float]:
        """
        Get current implied volatility level as a guard/scaler.
        Returns sqrt(v0) from calibrated params, or None if unavailable.
        """
        params = self.get_params(symbol_root)
        if params is None:
            return None
        return params.v0 ** 0.5
    
    def get_vol_regime(self, symbol_root: str, threshold_low: float = 0.08, 
                      threshold_high: float = 0.15) -> str:
        """
        Classify volatility regime: 'low', 'normal', 'high', or 'unknown'.
        Useful for position sizing and risk adjustments.
        """
        params = self.get_params(symbol_root)
        if params is None:
            return 'unknown'
        
        iv = params.v0 ** 0.5
        if iv < threshold_low:
            return 'low'
        elif iv > threshold_high:
            return 'high'
        else:
            return 'normal'
