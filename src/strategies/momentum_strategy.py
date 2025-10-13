"""Momentum Strategy - Trend-following strategy based on momentum indicators."""

import numpy as np
from typing import Dict, Any
from .base_strategy import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """
    Momentum-based trading strategy.
    
    Uses RSI, moving averages, and price momentum to identify trends.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize Momentum Strategy."""
        super().__init__(config)
        
        self.rsi_period = config.get('rsi_period', 14)
        self.rsi_oversold = config.get('rsi_oversold', 30)
        self.rsi_overbought = config.get('rsi_overbought', 70)
        
        self.fast_ma_period = config.get('fast_ma_period', 20)
        self.slow_ma_period = config.get('slow_ma_period', 50)
        
    def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze market data using momentum indicators.
        
        Args:
            market_data: Market data with OHLC prices
            
        Returns:
            Analysis results
        """
        if 'close' not in market_data:
            return {"error": "Missing price data"}
        
        closes = np.array(market_data['close'])
        
        if len(closes) < max(self.slow_ma_period, self.rsi_period + 1):
            return {"error": "Insufficient data"}
        
        # Calculate indicators
        rsi = self._calculate_rsi(closes)
        fast_ma = np.mean(closes[-self.fast_ma_period:])
        slow_ma = np.mean(closes[-self.slow_ma_period:])
        
        # Price momentum
        momentum = (closes[-1] - closes[-10]) / closes[-10] if len(closes) >= 10 else 0
        
        # Trend strength
        ma_diff = (fast_ma - slow_ma) / slow_ma
        
        analysis = {
            "rsi": rsi,
            "fast_ma": fast_ma,
            "slow_ma": slow_ma,
            "momentum": momentum,
            "ma_diff": ma_diff,
            "current_price": closes[-1],
            "trend": "bullish" if ma_diff > 0 else "bearish"
        }
        
        return analysis
    
    def generate_signal(self, analysis: Dict[str, Any]) -> str:
        """
        Generate trading signal.
        
        Args:
            analysis: Analysis results
            
        Returns:
            Signal: 'BUY', 'SELL', or 'HOLD'
        """
        if "error" in analysis:
            return "HOLD"
        
        rsi = analysis['rsi']
        ma_diff = analysis['ma_diff']
        momentum = analysis['momentum']
        
        # Calculate confidence score
        confidence = self._calculate_confidence(analysis)
        
        # Generate signal
        if confidence < self.config.get('min_confidence', 0.6):
            return "HOLD"
        
        # Strong bullish signals
        if (rsi < self.rsi_oversold and ma_diff > 0) or \
           (ma_diff > 0.005 and momentum > 0.01):
            return "BUY"
        
        # Strong bearish signals
        if (rsi > self.rsi_overbought and ma_diff < 0) or \
           (ma_diff < -0.005 and momentum < -0.01):
            return "SELL"
        
        return "HOLD"
    
    def _calculate_rsi(self, prices: np.ndarray) -> float:
        """Calculate RSI indicator."""
        if len(prices) < self.rsi_period + 1:
            return 50.0
        
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-self.rsi_period:])
        avg_loss = np.mean(losses[-self.rsi_period:])
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def _calculate_confidence(self, analysis: Dict[str, Any]) -> float:
        """
        Calculate confidence score for the signal.
        
        Args:
            analysis: Analysis results
            
        Returns:
            Confidence score (0 to 1)
        """
        confidence_factors = []
        
        # RSI extremes increase confidence
        rsi = analysis['rsi']
        if rsi < self.rsi_oversold:
            confidence_factors.append((self.rsi_oversold - rsi) / self.rsi_oversold)
        elif rsi > self.rsi_overbought:
            confidence_factors.append((rsi - self.rsi_overbought) / (100 - self.rsi_overbought))
        
        # Strong trend increases confidence
        ma_diff = abs(analysis['ma_diff'])
        confidence_factors.append(min(ma_diff * 50, 1.0))
        
        # Strong momentum increases confidence
        momentum = abs(analysis['momentum'])
        confidence_factors.append(min(momentum * 20, 1.0))
        
        if not confidence_factors:
            return 0.0
        
        return np.mean(confidence_factors)
