"""Tests for Decision Engine."""

import pytest
import numpy as np
from src.agent.decision_engine import DecisionEngine


class TestDecisionEngine:
    """Test suite for DecisionEngine."""
    
    def setup_method(self):
        """Setup test fixtures."""
        self.config = {
            'rsi_oversold': 30,
            'rsi_overbought': 70,
            'min_confidence': 0.6,
            'stop_loss_pct': 0.01,
            'take_profit_pct': 0.02
        }
        self.engine = DecisionEngine(self.config)
    
    def test_initialization(self):
        """Test engine initialization."""
        assert self.engine.rsi_oversold == 30
        assert self.engine.rsi_overbought == 70
        assert self.engine.min_confidence == 0.6
    
    def test_make_decision_insufficient_data(self):
        """Test decision making with insufficient data."""
        analysis = {}
        decision = self.engine.make_decision(analysis)
        
        assert decision['action'] == 'HOLD'
        assert decision['confidence'] == 0.0
    
    def test_make_decision_hold_low_confidence(self):
        """Test HOLD decision for low confidence."""
        analysis = {
            'indicators': {
                'rsi': 50,
                'sma_20': 1.2000,
                'sma_50': 1.2010,
                'volatility': 0.001
            },
            'current_price': {'bid': 1.2005, 'ask': 1.2007},
            'symbol': 'EURUSD'
        }
        
        decision = self.engine.make_decision(analysis)
        
        # Should be HOLD due to neutral indicators
        assert decision['action'] == 'HOLD'
    
    def test_make_decision_buy_signal(self):
        """Test BUY decision for strong bullish signal."""
        analysis = {
            'indicators': {
                'rsi': 25,  # Oversold
                'sma_20': 1.2100,
                'sma_50': 1.2000,  # Bullish crossover
                'volatility': 0.001
            },
            'current_price': {'bid': 1.2105, 'ask': 1.2107},
            'symbol': 'EURUSD'
        }
        
        decision = self.engine.make_decision(analysis)
        
        # Should likely be BUY
        assert decision['action'] in ['BUY', 'HOLD']
        assert 'stop_loss' in decision
        assert 'take_profit' in decision
    
    def test_calculate_levels(self):
        """Test stop loss and take profit calculation."""
        sl, tp = self.engine._calculate_levels(1.2000, 'BUY')
        
        assert sl < 1.2000  # Stop loss below entry for BUY
        assert tp > 1.2000  # Take profit above entry for BUY
        
        sl, tp = self.engine._calculate_levels(1.2000, 'SELL')
        
        assert sl > 1.2000  # Stop loss above entry for SELL
        assert tp < 1.2000  # Take profit below entry for SELL


if __name__ == "__main__":
    pytest.main([__file__])
