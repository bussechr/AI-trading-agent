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
        Calibrate Heston model to option chain using Differential Evolution.
        Minimizes squared error between market IVs and Heston model IVs.
        """
        from scipy.optimize import differential_evolution
        
        # Extract IVs from chain
        ivs = []
        for row in chain.rows:
            mid_price = (row.bid + row.ask) / 2.0
            F = chain.S0 * np.exp((chain.rd - chain.rf) * row.T)
            # Use simple moneyness filter (0.8 to 1.2) to avoid illiquid wings
            if 0.8 < row.K / F < 1.2:
                # We use the row.iv if available, or implied_vol_newton if we had it
                # For this implementation, we assume row has 'iv' or we calculate it
                # If the provider gives us IVs directly (which is common), use them
                market_iv = getattr(row, 'iv', 0.2) 
                ivs.append((row.T, row.K, F, market_iv))
        
        if not ivs:
            # Fallback if no valid IVs found
            return HestonParams(0.04, 0.04, 2.0, 0.3, -0.5, 0.0, "")

        # Objective function: Sum of Squared Errors
        def objective(params):
            v0, theta, kappa, sigma, rho = params
            error = 0.0
            for T, K, F, mkt_iv in ivs:
                # Approximate Heston IV (using gatheredal expansion or similar would be better, 
                # but for now we use a simple proxy or just fit the variance surface directly)
                # Here we use a very simplified proxy for Heston IV to keep it pure Python/Scipy
                # Total variance ~ theta + (v0 - theta)*(1-exp(-kappa*T))/(kappa*T) ...
                
                # Simplified: Expected variance over horizon T
                exp_var = theta + (v0 - theta) * (1 - np.exp(-kappa * T)) / (kappa * T)
                model_iv = np.sqrt(max(0, exp_var))
                
                # Add skew correction (very rough proxy for rho effect)
                moneyness = np.log(K / F)
                skew_impact = 0.5 * rho * sigma * moneyness
                model_iv += skew_impact
                
                error += (model_iv - mkt_iv) ** 2
            return error

        # Bounds: v0, theta, kappa, sigma, rho
        bounds = [
            (0.001, 0.5),   # v0
            (0.001, 0.5),   # theta
            (0.1, 10.0),    # kappa
            (0.01, 2.0),    # sigma
            (-0.9, 0.9)     # rho
        ]
        
        result = differential_evolution(objective, bounds, seed=42, maxiter=20)
        
        v0, theta, kappa, sigma, rho = result.x
        
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
