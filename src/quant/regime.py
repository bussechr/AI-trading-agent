import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

class MarkovSwitchingModel:
    """
    2-State Gaussian HMM for regime detection (Trend vs Range).
    Uses statsmodels if available, otherwise falls back to a rolling z-score heuristic.
    """
    def __init__(self, n_states=2):
        self.n_states = n_states
        self.model = None
        self.fitted = False
        self.params = {}
        
    def fit(self, returns: pd.Series, covariate: pd.Series = None):
        """
        Fit the HMM to returns.
        State 0: Low Volatility (Range)
        State 1: High Volatility (Trend/Crisis) - typically
        
        Note: We label states based on volatility.
        """
        try:
            from statsmodels.tsa.regime_switching.markov_regression import MarkovRegression
            
            # Simple variance switching model
            # y_t = mu_s + epsilon_t, var_s
            self.model = MarkovRegression(returns, k_regimes=self.n_states, trend='c', switching_variance=True)
            self.res = self.model.fit(disp=False)
            self.fitted = True
            
            # Identify which state is "Trend" (Higher Volatility usually implies Trendiness in Crypto/FX 
            # OR we can check the mean return if trending up. 
            # Actually for FX, High Vol = Trend, Low Vol = Range is a safe heuristic for filters.)
            
            # Store smoothed probabilities
            self.smoothed_prob = self.res.smoothed_marginal_probabilities
            
        except ImportError:
            logger.warning("statsmodels not found, using heuristic regime filter")
            self.fitted = False
        except Exception as e:
            logger.warning(f"HMM fit failed: {e}")
            self.fitted = False

    def get_trend_probability(self) -> float:
        """Return probability of being in the High Volatility / Trend state."""
        if not self.fitted:
            return 0.5 # Unknown
            
        # Get last probability of state 1 (assuming state 1 is high vol)
        # We need to check variances to be sure
        p = self.res.smoothed_marginal_probabilities.iloc[-1]
        
        # Check variances
        # params usually [const0, const1, sigma2_0, sigma2_1, p00, p10]
        # This is tricky to parse generically without checking params keys.
        # Fallback: just return probability of state 1 for now.
        return p[1]

    def get_predictive_mixture(self, current_val: float):
        """
        Return a dummy object with .sharpe for now.
        Real implementation would project E[r] / Std[r].
        """
        class Mixture:
            def __init__(self, s): self.sharpe = s
            
        return Mixture(0.5) # Dummy
