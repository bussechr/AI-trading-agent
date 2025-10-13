"""Tests for Trading Strategies."""

import pytest
import numpy as np
from src.strategies.momentum_strategy import MomentumStrategy


class TestMomentumStrategy:
    """Test suite for MomentumStrategy."""
    
    def setup_method(self):
        """Setup test fixtures."""
        self.config = {
            'rsi_period': 14,
            'rsi_oversold': 30,
            'rsi_overbought': 70,
            'fast_ma_period': 20,
            'slow_ma_period': 50,
            'min_confidence': 0.6
        }
        self.strategy = MomentumStrategy(self.config)
    
    def test_initialization(self):
        """Test strategy initialization."""
        assert self.strategy.rsi_period == 14
        assert self.strategy.rsi_oversold == 30
        assert self.strategy.fast_ma_period == 20
    
    def test_analyze_insufficient_data(self):
        """Test analysis with insufficient data."""
        market_data = {'close': [1.2000, 1.2010, 1.2005]}
        
        result = self.strategy.analyze(market_data)
        
        assert 'error' in result
    
    def test_analyze_sufficient_data(self):
        """Test analysis with sufficient data."""
        # Generate sample price data
        prices = np.random.uniform(1.2000, 1.2100, 100).tolist()
        market_data = {'close': prices}
        
        result = self.strategy.analyze(market_data)
        
        assert 'error' not in result
        assert 'rsi' in result
        assert 'fast_ma' in result
        assert 'slow_ma' in result
        assert 'momentum' in result
    
    def test_generate_signal(self):
        """Test signal generation."""
        # Bullish scenario
        bullish_analysis = {
            'rsi': 25,  # Oversold
            'ma_diff': 0.01,  # Bullish trend
            'momentum': 0.02,  # Positive momentum
            'current_price': 1.2100
        }
        
        signal = self.strategy.generate_signal(bullish_analysis)
        
        assert signal in ['BUY', 'SELL', 'HOLD']
    
    def test_calculate_confidence(self):
        """Test confidence calculation."""
        analysis = {
            'rsi': 25,
            'ma_diff': 0.01,
            'momentum': 0.02
        }
        
        confidence = self.strategy._calculate_confidence(analysis)
        
        assert 0 <= confidence <= 1


if __name__ == "__main__":
    pytest.main([__file__])
