
import pytest
import pandas as pd
import numpy as np
from src.agents.fx_el_hawkes_agent import FXELAgent

@pytest.fixture
def mock_config():
    """Basic configuration for testing."""
    return {
        "symbols_roots": ["EURUSD", "GBPUSD"],
        "mini_suffixes": [".MINI"],
        "el_window": 10,
        "el_ema_span": 5,
        "score_threshold": 0.2,
        "max_concurrent": 2,
        "corr_max": 0.9,
        "use_regime_filter": True,
        "use_hawkes": False,
        "use_lppls": False,
        "use_heston_guard": False,
        "vol_ref": 0.01,
        "target_base_pct": 0.01
    }

@pytest.fixture
def sample_price_data():
    """Generate 100 bars of synthetic price data with a trend."""
    np.random.seed(42)
    n = 300
    
    # Random walk with drift
    returns = np.random.normal(0.0001, 0.001, n)
    price = 1.1000 * np.exp(np.cumsum(returns))
    
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n, freq="h"),
        "open": price,
        "high": price * 1.001,
        "low": price * 0.999,
        "close": price,
        "volume": np.random.randint(100, 1000, n)
    })
    return df

    return df

@pytest.fixture
def mock_market_data(sample_price_data):
    """Return dict of market data as agent expects."""
    return {
        "EURUSD": sample_price_data,
        "EURUSD.MINI": sample_price_data
    }

@pytest.fixture
def agent_setup(mock_config):
    """Renamed for clarity in new tests."""
    return mock_config

@pytest.fixture
def agent(mock_config):
    """Instantiated agent with mock config."""
    return FXELAgent(mock_config)
