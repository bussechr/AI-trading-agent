
import pytest
import pandas as pd
import numpy as np
from src.agents.risk_utils import el_pz, regime_tilt, dynamic_target_pct, realised_vol, cost_gate, low_corr_pick

def test_el_pz(sample_price_data):
    """Test EL momentum calculation."""
    close = sample_price_data["close"]
    pz = el_pz(close, w=10, ema=5)
    
    assert len(pz) == len(close)
    assert not pz.isna().all()
    # Momentum should be somewhat correlated with returns
    ret = np.log(close).diff()
    assert pz.corr(ret) > 0

def test_regime_tilt():
    """Test regime tilt proxy."""
    # Create valid return series
    rets = pd.Series(np.random.normal(0, 0.01, 100))
    tilt = regime_tilt(rets, w=10)
    
    assert len(tilt) == 100
    assert tilt.min() >= -1.0
    assert tilt.max() <= 1.0

def test_dynamic_target_pct():
    """Test dynamic target scaling."""
    base = 0.01
    ref = 0.01
    
    # Higher vol -> higher target
    assert dynamic_target_pct(0.04, ref, base) > base
    # Lower vol -> lower target
    assert dynamic_target_pct(0.0025, ref, base) < base
    # Zero/negative vol -> base
    assert dynamic_target_pct(0.0, ref, base) == base

def test_cost_gate():
    """Test cost gate logic."""
    # High expected move vs low cost -> Pass
    assert cost_gate(expected_move=0.01, spread_pips=1.0, pip_value_per_lot=10, 
                     equity=10000, lot_fraction=0.1) == True
                     
    # Low expected move vs high cost -> Fail
    assert cost_gate(expected_move=0.0001, spread_pips=5.0, pip_value_per_lot=10,
                     equity=1000, lot_fraction=1.0) == False

def test_low_corr_pick():
    """Test correlation filter."""
    cands = [("A", 1.0), ("B", 0.9), ("C", 0.8)]
    
    # Highly correlated A & B
    corr = pd.DataFrame({
        "A": [1.0, 0.95, 0.1],
        "B": [0.95, 1.0, 0.1],
        "C": [0.1, 0.1, 1.0]
    }, index=["A", "B", "C"])
    
    # Should pick A (highest score) and C (uncorrelated), skip B (correlated to A)
    picked = low_corr_pick(cands, corr, k=3, corr_max=0.5)
    assert picked == ["A", "C"]
