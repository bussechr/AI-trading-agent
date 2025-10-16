"""
Markov-switching AR-GARCH with Student-t innovations for regime detection.
Provides filtered probability P_t(trend) and predictive mixture distribution.
"""
import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import minimize
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)

@dataclass
class RegimeState:
    """Single regime state parameters."""
    mu: float  # AR mean
    phi: float  # AR(1) coefficient
    omega: float  # GARCH constant
    alpha: float  # GARCH alpha (ARCH term)
    beta: float  # GARCH beta (GARCH term)
    nu: float  # Student-t degrees of freedom

@dataclass
class RegimeMixture:
    """Predictive mixture distribution."""
    mu_mix: float  # Mixed mean
    var_mix: float  # Mixed variance
    prob_trend: float  # P(trend state)
    sharpe: float  # Predictive Sharpe ratio

class MarkovSwitchingModel:
    """
    Two-state Markov-switching AR(1)-GARCH(1,1) with Student-t innovations.
    States: 0=range-bound, 1=trend
    """
    
    def __init__(self, n_states: int = 2):
        self.n_states = n_states
        self.states = [RegimeState(0, 0, 0.0001, 0.1, 0.8, 5.0) for _ in range(n_states)]
        self.transition_matrix = np.array([[0.95, 0.05], [0.05, 0.95]])  # Persistent states
        self.filtered_probs = None
        
    def fit(self, returns: pd.Series, el_momentum: pd.Series, max_iter: int = 50) -> None:
        """
        Fit the model using EM algorithm.
        el_momentum is used as a covariate in the mean equation.
        """
        r = returns.dropna().values
        p = el_momentum.loc[returns.dropna().index].values
        
        if len(r) < 100:
            logger.warning("Insufficient data for regime fitting")
            return
        
        # Initialize with simple heuristics
        vol = np.std(r)
        self.states[0] = RegimeState(mu=0.0, phi=0.0, omega=vol**2*0.5, alpha=0.1, beta=0.8, nu=5.0)
        self.states[1] = RegimeState(mu=0.0, phi=0.3, omega=vol**2*0.3, alpha=0.15, beta=0.75, nu=5.0)
        
        # Run simplified EM (full implementation would be more complex)
        self._em_iteration(r, p, max_iter)
        
    def _em_iteration(self, returns: np.ndarray, momentum: np.ndarray, max_iter: int):
        """Simplified EM iteration."""
        T = len(returns)
        
        # Initialize filtered probabilities
        probs = np.ones((T, self.n_states)) / self.n_states
        
        for iteration in range(max_iter):
            # E-step: Forward-backward algorithm (simplified)
            probs = self._filter_probs(returns, momentum)
            
            # M-step: Update parameters (simplified - just update means based on momentum)
            for s in range(self.n_states):
                weighted_returns = returns * probs[:, s]
                weighted_momentum = momentum * probs[:, s]
                
                if np.sum(probs[:, s]) > 10:
                    # Update mean as function of momentum
                    self.states[s].mu = np.sum(weighted_momentum) / np.sum(probs[:, s]) * 0.01
        
        self.filtered_probs = probs
        
    def _filter_probs(self, returns: np.ndarray, momentum: np.ndarray) -> np.ndarray:
        """
        Filter state probabilities using forward algorithm.
        Returns: (T, n_states) array of filtered probabilities.
        """
        T = len(returns)
        probs = np.zeros((T, self.n_states))
        probs[0, :] = 1.0 / self.n_states
        
        for t in range(1, T):
            # Predict
            pred_probs = probs[t-1] @ self.transition_matrix
            
            # Update with likelihood
            likelihoods = np.array([
                self._likelihood(returns[t], momentum[t], s) 
                for s in range(self.n_states)
            ])
            
            probs[t] = pred_probs * likelihoods
            probs[t] /= np.sum(probs[t]) + 1e-12
            
        return probs
    
    def _likelihood(self, ret: float, mom: float, state: int) -> float:
        """Compute likelihood of return given state."""
        s = self.states[state]
        
        # Mean depends on momentum
        mu = s.mu + s.phi * mom
        
        # Variance from GARCH (simplified - use constant vol)
        sigma = np.sqrt(s.omega / (1 - s.alpha - s.beta))
        
        # Student-t likelihood
        return stats.t.pdf(ret, df=s.nu, loc=mu, scale=sigma)
    
    def get_trend_probability(self) -> float:
        """Get current filtered probability of trend state."""
        if self.filtered_probs is None:
            return 0.5
        return float(self.filtered_probs[-1, 1])  # State 1 = trend
    
    def get_predictive_mixture(self, current_momentum: float) -> RegimeMixture:
        """
        Compute predictive mixture distribution for next return.
        Returns mixture mean, variance, and Sharpe ratio.
        """
        if self.filtered_probs is None:
            return RegimeMixture(0.0, 0.01**2, 0.5, 0.0)
        
        p_trend = self.get_trend_probability()
        probs = np.array([1 - p_trend, p_trend])
        
        # Compute mixture moments
        means = np.array([
            self.states[s].mu + self.states[s].phi * current_momentum
            for s in range(self.n_states)
        ])
        
        variances = np.array([
            self.states[s].omega / (1 - self.states[s].alpha - self.states[s].beta)
            for s in range(self.n_states)
        ])
        
        # Mixture mean
        mu_mix = np.sum(probs * means)
        
        # Mixture variance: E[Var] + Var[E]
        var_mix = np.sum(probs * (variances + means**2)) - mu_mix**2
        
        # Predictive Sharpe
        sharpe = mu_mix / (np.sqrt(var_mix) + 1e-12)
        
        return RegimeMixture(
            mu_mix=float(mu_mix),
            var_mix=float(var_mix),
            prob_trend=float(p_trend),
            sharpe=float(sharpe)
        )
