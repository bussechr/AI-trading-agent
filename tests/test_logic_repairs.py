
import pytest
import numpy as np
import pandas as pd
from src.agents.heston_service import HestonService, HestonParams
from src.agents.hawkes_micro import BivariateHawkes, HawkesSignal

# --- Mock Classes ---

class MockChain:
    def __init__(self, S0=1.0, rd=0.05, rf=0.03):
        self.S0 = S0
        self.rd = rd
        self.rf = rf
        self.rows = []

class MockRow:
    def __init__(self, T, K, bid, ask, cp='C', iv=None):
        self.T = T
        self.K = K
        self.bid = bid
        self.ask = ask
        self.cp = cp
        self.iv = iv

class MockProvider:
    def get_chain(self, root):
        c = MockChain()
        # Create synthetic chain consistent with ~20% vol
        # S0=1.0, r=0.02 diff.
        # ATM options approx price ~ 0.4 * vol * sqrt(T)
        vol = 0.20
        for T in [0.1, 0.5, 1.0]:
            for m in [0.9, 1.0, 1.1]:
                K = 1.0 * m
                # Mock IV smile: higher IV at wings
                mock_iv = vol + 0.1 * abs(m - 1.0)
                # We interpret "market_iv" as being attached to row for this test
                row = MockRow(T, K, 0.0, 0.0, 'C', iv=mock_iv)
                c.rows.append(row)
        return c

# --- Tests ---

def test_heston_calibration():
    """Test that Heston calibration runs and returns valid params."""
    service = HestonService(outdir=".", provider=MockProvider())
    
    # Needs to run _calibrate directly or via get_params
    # We'll use the public get_params but mock the cache/write to avoid file IO if possible
    # Actually get_params writes to disk, so we just check _calibrate logic directly if possible
    # or just let it write to tmp
    
    chain = MockProvider().get_chain("EURUSD")
    params = service._calibrate(chain)
    
    assert isinstance(params, HestonParams)
    assert params.v0 > 0
    assert params.theta > 0
    assert params.kappa > 0
    assert params.sigma > 0
    # rho usually negative for equity, can be anything for FX, but bound is [-1, 1]
    assert -1.0 <= params.rho <= 1.0

def test_hawkes_fitting():
    """Test that Hawkes process fits to trade data."""
    # Generate synthetic trades
    # Burst of buys, then burst of sells
    times = []
    sides = []
    
    t = 0.0
    # 50 buys in a cluster
    for i in range(50):
        t += np.random.exponential(1.0)
        times.append(t)
        sides.append(1)
        
    t += 100.0 # gap
    # 50 sells
    for i in range(50):
        t += np.random.exponential(1.0)
        times.append(t)
        sides.append(-1)
        
    df = pd.DataFrame({"time": times, "side": sides})
    
    hawkes = BivariateHawkes()
    hawkes.fit(df)
    
    assert hawkes.fitted
    # Check dimensions of params
    assert len(hawkes.params) == 8
    assert np.all(hawkes.params >= 0)
    
    # Check signal generation
    sig = hawkes.get_signal(df, t + 1.0)
    assert isinstance(sig, HawkesSignal)
    assert isinstance(sig.drift, float)
