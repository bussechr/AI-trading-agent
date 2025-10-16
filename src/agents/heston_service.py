from __future__ import annotations
import os, json, time, logging
from typing import Optional, Protocol
from dataclasses import dataclass, asdict
import numpy as np

logger = logging.getLogger(__name__)

class OptionProvider(Protocol):
    """Protocol for option chain providers (HTTP or proxy)."""
    def get_chain(self, symbol_root: str):
        """Return a Chain object with S0, rd, rf, and rows of OptionRow."""
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
    symbol_root: str

class HestonService:
    """
    Manages Heston model calibration for FX options.
    Fetches option chains via provider, calibrates parameters, caches to JSON.
    """
    def __init__(self, outdir: str, provider: OptionProvider, recalc_after_secs: float = 18*3600):
        self.outdir = outdir
        self.provider = provider
        self.recalc_after_secs = recalc_after_secs
        os.makedirs(outdir, exist_ok=True)
        self._cache: dict[str, HestonParams] = {}

    def get_params(self, symbol_root: str, force_recalc: bool = False) -> Optional[HestonParams]:
        """
        Get Heston parameters for symbol_root.
        Returns cached params if fresh, otherwise fetches chain and calibrates.
        """
        cache_path = os.path.join(self.outdir, f"{symbol_root}_heston.json")
        now = time.time()

        # Check memory cache first
        if not force_recalc and symbol_root in self._cache:
            params = self._cache[symbol_root]
            age = now - params.timestamp
            if age < self.recalc_after_secs:
                logger.debug(f"{symbol_root}: using cached Heston params (age={age/3600:.1f}h)")
                return params

        # Check disk cache
        if not force_recalc and os.path.exists(cache_path):
            try:
                with open(cache_path, 'r') as f:
                    data = json.load(f)
                age = now - data['timestamp']
                if age < self.recalc_after_secs:
                    params = HestonParams(**data)
                    self._cache[symbol_root] = params
                    logger.debug(f"{symbol_root}: loaded Heston params from disk (age={age/3600:.1f}h)")
                    return params
            except Exception as e:
                logger.warning(f"{symbol_root}: failed to load cached params: {e}")

        # Need fresh calibration
        try:
            logger.info(f"{symbol_root}: fetching option chain and calibrating Heston...")
            chain = self.provider.get_chain(symbol_root)
            params = self._calibrate(chain)
            params.timestamp = now
            params.symbol_root = symbol_root

            # Save to disk and memory
            with open(cache_path, 'w') as f:
                json.dump(asdict(params), f, indent=2)
            self._cache[symbol_root] = params
            
            logger.info(f"{symbol_root}: calibrated Heston - v0={params.v0:.4f}, theta={params.theta:.4f}, kappa={params.kappa:.2f}")
            return params

        except Exception as e:
            logger.error(f"{symbol_root}: Heston calibration failed: {e}")
            return None

    def _calibrate(self, chain) -> HestonParams:
        """
        Calibrate Heston model to option chain.
        This is a simplified calibration - in production you'd use scipy.optimize with proper loss function.
        """
        from ..quant.iv import implied_vol_newton
        
        # Extract IVs from chain
        ivs = []
        for row in chain.rows:
            mid_price = (row.bid + row.ask) / 2.0
            F = chain.S0 * np.exp((chain.rd - chain.rf) * row.T)
            cp = 1 if row.cp == 'C' else -1
            
            iv = implied_vol_newton(mid_price, F, row.K, row.T, chain.rd, chain.rf, cp)
            if iv is not None and 0.01 < iv < 2.0:
                ivs.append((row.T, row.K / F, iv))
        
        if not ivs:
            raise ValueError("No valid IVs extracted from chain")
        
        # Simple moment-matching calibration (placeholder for proper optimization)
        atm_ivs = [iv for T, m, iv in ivs if 0.95 < m < 1.05]
        avg_iv = np.mean(atm_ivs) if atm_ivs else np.mean([iv for _, _, iv in ivs])
        
        v0 = avg_iv ** 2
        theta = v0  # assume mean-reverting to current level
        kappa = 2.0  # moderate mean reversion
        sigma = 0.3  # moderate vol of vol
        rho = -0.5   # typical negative correlation for equity/FX
        
        return HestonParams(
            v0=float(v0),
            theta=float(theta),
            kappa=float(kappa),
            sigma=float(sigma),
            rho=float(rho),
            timestamp=0.0,  # will be set by caller
            symbol_root=""  # will be set by caller
        )

    def get_vol_guard(self, symbol_root: str) -> Optional[float]:
        """
        Get volatility guard level from Heston params.
        Returns sqrt(theta) as a simple vol estimate, or None if unavailable.
        """
        params = self.get_params(symbol_root)
        if params is None:
            return None
        return float(np.sqrt(params.theta))
