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
        
    def fit(self, returns: pd.Series, el_momentum: pd.Series, max_iter: int = 50, tol: float = 1e-4) -> None:
        """
        Fit the model using EM algorithm.
        el_momentum is used as a covariate in the mean equation.
        """
        r = returns.dropna().values
        p = el_momentum.loc[returns.dropna().index].values
        
        if len(r) < 100:
            logger.warning("Insufficient data for regime fitting")
            return
        
        # Initialize with simple heuristics to avoid bad local optima
        vol = np.std(r)
        # State 0: Low vol, mean reversion (range)
        self.states[0] = RegimeState(mu=0.0, phi=0.0, omega=vol**2*0.4, alpha=0.05, beta=0.90, nu=10.0)
        # State 1: High vol, momentum/trend (trend)
        self.states[1] = RegimeState(mu=0.0, phi=0.5, omega=vol**2*0.8, alpha=0.10, beta=0.85, nu=5.0)
        
        self._em_iteration(r, p, max_iter, tol)
        
    def _em_iteration(self, returns: np.ndarray, momentum: np.ndarray, max_iter: int, tol: float):
        """EM iteration with convergence check."""
        T = len(returns)
        prev_ll = -np.inf
        
        for iteration in range(max_iter):
            # E-step: Filter probabilities
            probs, current_ll = self._filter_probs_and_ll(returns, momentum)
            
            # Check convergence
            delta = current_ll - prev_ll
            if iteration > 0 and 0 <= delta < tol:
                logger.debug(f"Regime filter converged at iter {iteration}, LL={current_ll:.4f}")
                break
            prev_ll = current_ll
            
            # M-step: Update parameters
            # Weighted regression for AR(1) mean: r_t = mu + phi * p_t + epsilon
            # We solve for each state separately
            for s in range(self.n_states):
                weights = probs[:, s]
                sum_w = np.sum(weights)
                
                if sum_w > 10: # Minimum effective sample size
                    # Weighted least squares for mu and phi
                    # Y = r_t, X = [1, p_t]
                    # This is slightly expensive, so we might stick to simplified update if speed is key.
                    # Simplified update: assume mu~0, just estimate phi?
                    # Let's try to update phi at least.
                    
                    # Centered moments
                    w_mean_r = np.sum(weights * returns) / sum_w
                    w_mean_p = np.sum(weights * momentum) / sum_w
                    
                    cov_rp = np.sum(weights * (returns - w_mean_r) * (momentum - w_mean_p))
                    var_p = np.sum(weights * (momentum - w_mean_p)**2)
                    
                    if var_p > 1e-9:
                        new_phi = cov_rp / var_p
                        self.states[s].phi = np.clip(new_phi, -0.9, 0.9)
                        
                        # Update mu: mean_r - phi * mean_p
                        self.states[s].mu = w_mean_r - self.states[s].phi * w_mean_p
                        
                        # Update variance/omega (simplified GARCH intercept update)
                        # resid = r - (mu + phi*p)
                        resid = returns - (self.states[s].mu + self.states[s].phi * momentum)
                        w_var = np.sum(weights * resid**2) / sum_w
                        
                        # Adjust omega to target long-run variance match
                        # Long-run var = omega / (1 - alpha - beta)
                        # omega = w_var * (1 - alpha - beta)
                        target_omega = w_var * (1 - self.states[s].alpha - self.states[s].beta)
                        self.states[s].omega = max(1e-7, target_omega)

        self.filtered_probs = probs

    def _filter_probs_and_ll(self, returns: np.ndarray, momentum: np.ndarray) -> tuple[np.ndarray, float]:
        """
        Filter probabilities and compute total log-likelihood.
        C3 FIX: propagate per-state GARCH(1,1) conditional variance h_t
        recursively instead of using the constant long-run variance.
        """
        T = len(returns)
        probs = np.zeros((T, self.n_states))
        probs[0, :] = 1.0 / self.n_states
        log_likelihood = 0.0

        # Initialise h_t to the unconditional (long-run) variance for each state.
        h = np.array([
            self.states[s].omega / max(1.0 - self.states[s].alpha - self.states[s].beta, 1e-6)
            for s in range(self.n_states)
        ], dtype=float)
        h = np.maximum(h, 1e-10)

        lik_buffer = np.zeros(self.n_states)

        for t in range(1, T):
            # Predict state probabilities
            pred_probs = probs[t - 1] @ self.transition_matrix

            # Evaluate state-conditional likelihoods using current h_t
            for s in range(self.n_states):
                lik_buffer[s] = self._likelihood_h(returns[t], momentum[t], s, h[s])

            numerator = pred_probs * lik_buffer
            denom = float(np.sum(numerator))
            if denom > 1e-20:
                probs[t] = numerator / denom
                log_likelihood += np.log(denom)
            else:
                probs[t] = pred_probs  # numerical fallback

            # Update h_{t+1} = ω + α·ε²_t + β·h_t  (per state, using state mean)
            for s in range(self.n_states):
                mu_s = self.states[s].mu + self.states[s].phi * momentum[t]
                eps2 = (returns[t] - mu_s) ** 2
                h[s] = (
                    self.states[s].omega
                    + self.states[s].alpha * eps2
                    + self.states[s].beta * h[s]
                )
                h[s] = max(h[s], 1e-10)

        return probs, log_likelihood
        
    def _filter_probs(self, returns: np.ndarray, momentum: np.ndarray) -> np.ndarray:
        """
        Filter state probabilities using forward algorithm.
        C3 FIX: propagates recursive GARCH(1,1) conditional variance h_t.
        Returns: (T, n_states) array of filtered probabilities.
        """
        T = len(returns)
        probs = np.zeros((T, self.n_states))
        probs[0, :] = 1.0 / self.n_states

        # Initialise h_t to unconditional variance per state.
        h = np.array([
            self.states[s].omega / max(1.0 - self.states[s].alpha - self.states[s].beta, 1e-6)
            for s in range(self.n_states)
        ], dtype=float)
        h = np.maximum(h, 1e-10)

        for t in range(1, T):
            pred_probs = probs[t - 1] @ self.transition_matrix

            likelihoods = np.array([
                self._likelihood_h(returns[t], momentum[t], s, h[s])
                for s in range(self.n_states)
            ])

            probs[t] = pred_probs * likelihoods
            denom = float(np.sum(probs[t]))
            probs[t] /= (denom + 1e-12)

            # Update h_{t+1} per state
            for s in range(self.n_states):
                mu_s = self.states[s].mu + self.states[s].phi * momentum[t]
                eps2 = (returns[t] - mu_s) ** 2
                h[s] = (
                    self.states[s].omega
                    + self.states[s].alpha * eps2
                    + self.states[s].beta * h[s]
                )
                h[s] = max(h[s], 1e-10)

        return probs
    
    def _likelihood(self, ret: float, mom: float, state: int) -> float:
        """Compute likelihood using the unconditional (long-run) variance.
        Used during initialisation only; _likelihood_h is used during filtering."""
        s = self.states[state]
        mu = s.mu + s.phi * mom
        long_run_var = s.omega / max(1.0 - s.alpha - s.beta, 1e-6)
        sigma = max(np.sqrt(long_run_var), 1e-8)
        return float(stats.t.pdf(ret, df=s.nu, loc=mu, scale=sigma))

    def _likelihood_h(self, ret: float, mom: float, state: int, h_t: float) -> float:
        """Compute likelihood using the current recursive conditional variance h_t.
        C3 FIX: replaces the constant sigma with the time-varying GARCH sigma."""
        s = self.states[state]
        mu = s.mu + s.phi * mom
        sigma = max(np.sqrt(max(h_t, 1e-10)), 1e-8)
        return float(stats.t.pdf(ret, df=s.nu, loc=mu, scale=sigma))
    
    def get_trend_probability(self) -> float:
        """Get current filtered probability of trend state."""
        if self.filtered_probs is None:
            return 0.5
        raw = float(self.filtered_probs[-1, 1])  # State 1 = trend
        # Soft clamp to [0.05, 0.95] — prevents overconfident extremes
        # from compounding evidence over hundreds of bars
        return 0.05 + 0.9 * raw
    
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
