
import pytest
import pandas as pd
import numpy as np
from src.agents.risk_utils import check_drawdown_limit, calculate_position_size

def test_check_drawdown_limit():
    # No history
    assert check_drawdown_limit([]) == True
    
    # Rising equity
    history = [10000, 10100, 10200, 10300]
    assert check_drawdown_limit(history, 0.10) == True
    
    # Small drawdown
    history = [10000, 10500, 10400] # peak 10500, current 10400, dd ~1%
    assert check_drawdown_limit(history, 0.10) == True
    
    # Breach drawdown (peak 10000, current 8500 = 15% dd)
    history = [10000, 9500, 9000, 8500]
    assert check_drawdown_limit(history, 0.10) == False
    
def test_calculate_position_size():
    equity = 10000.0
    risk_pct = 0.01 # 1% = $100 risk
    pip_value = 10.0 # $10 per pip per lot
    
    # Case 1: 50 pips stop
    # Risk $100. Value per lot per pip = $10. 
    # Value per lot for 50 pips = $500.
    # Lots = 100 / 500 = 0.2
    lots = calculate_position_size(equity, risk_pct, 50.0, pip_value)
    assert lots == pytest.approx(0.2, abs=0.01)
    
    # Case 2: 20 pips stop -> 0.5 lots
    lots = calculate_position_size(equity, risk_pct, 20.0, pip_value)
    assert lots == pytest.approx(0.5, abs=0.01)
    
    # Case 3: Max lots cap
    # 1 pip stop -> 10 lots (capped at 5.0)
    lots = calculate_position_size(equity, risk_pct, 1.0, pip_value, max_lots=5.0)
    assert lots == 5.0

def test_agent_sizing_logic(agent_setup, mock_market_data):
    """Verify agent uses calculate_position_size in act() method."""
    from src.agents.fx_el_hawkes_agent import FXELAgent
    from unittest.mock import patch, MagicMock
    
    agent = FXELAgent(agent_setup)
    
    # Mock decision to force a trade
    with patch.object(agent, 'decisions', return_value=[MagicMock(symbol="EURUSD.MINI", side="BUY", score=0.8)]):
        with patch('src.agents.fx_el_hawkes_agent.send') as mock_send:
            agent.act(10000.0, mock_market_data, all_symbols_catalog=["EURUSD.MINI"])
            
            # Check if send was called with non-zero lots
            assert mock_send.called
            args, kwargs = mock_send.call_args
            
            # kwargs['lots'] should be > 0.01 and likely around 0.1-0.3 given mock vol
            lots = kwargs.get('lots', 0.0)
            sl_price = kwargs.get('sl_price')
            tp_price = kwargs.get('tp_price')
            
            assert lots > 0.01
            assert sl_price is not None
            assert tp_price is not None
            # mock price starts at 1.1000 and has small drift, so it should be close
            assert sl_price < 1.15  # SL below price for BUY (roughly)
            assert tp_price > 1.05  # TP above price for BUY
