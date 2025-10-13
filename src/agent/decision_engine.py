"""Decision Engine - AI-powered trading decision logic."""

import logging
from typing import Dict, Any, List
import numpy as np


class DecisionEngine:
    """
    AI Decision Engine for trading signals.
    
    Analyzes market data and generates trading decisions.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Decision Engine.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Strategy parameters
        self.rsi_oversold = config.get('rsi_oversold', 30)
        self.rsi_overbought = config.get('rsi_overbought', 70)
        self.min_confidence = config.get('min_confidence', 0.6)
        
    def make_decision(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make trading decision based on market analysis.
        
        Args:
            analysis: Market analysis dictionary
            
        Returns:
            Decision dictionary with action, confidence, and parameters
        """
        indicators = analysis.get('indicators', {})
        current_price = analysis.get('current_price', {})
        
        if not indicators or not current_price:
            return {"action": "HOLD", "confidence": 0.0, "reason": "Insufficient data"}
        
        # Calculate signals
        signals = self._analyze_signals(indicators, current_price)
        
        # Combine signals and make decision
        decision = self._combine_signals(signals, current_price, analysis['symbol'])
        
        return decision
    
    def _analyze_signals(self, indicators: Dict[str, Any], 
                        current_price: Dict[str, float]) -> Dict[str, float]:
        """
        Analyze individual signals from indicators.
        
        Args:
            indicators: Technical indicators
            current_price: Current bid/ask prices
            
        Returns:
            Dictionary of signal strengths (-1 to 1)
        """
        signals = {}
        
        # RSI Signal
        rsi = indicators.get('rsi')
        if rsi is not None:
            if rsi < self.rsi_oversold:
                signals['rsi'] = (self.rsi_oversold - rsi) / self.rsi_oversold  # Bullish
            elif rsi > self.rsi_overbought:
                signals['rsi'] = -(rsi - self.rsi_overbought) / (100 - self.rsi_overbought)  # Bearish
            else:
                signals['rsi'] = 0.0
        
        # Moving Average Signal
        sma_20 = indicators.get('sma_20')
        sma_50 = indicators.get('sma_50')
        
        if sma_20 is not None and sma_50 is not None:
            # Golden cross / Death cross
            ma_diff = (sma_20 - sma_50) / sma_50
            signals['ma_trend'] = np.clip(ma_diff * 10, -1, 1)  # Scale and clip
        
        # Price vs MA Signal
        if sma_20 is not None:
            current = current_price.get('bid', 0)
            if current > 0:
                price_vs_ma = (current - sma_20) / sma_20
                signals['price_position'] = np.clip(price_vs_ma * 20, -1, 1)
        
        # Volatility Signal (higher volatility = lower confidence)
        volatility = indicators.get('volatility')
        if volatility is not None:
            signals['volatility'] = -abs(volatility) * 0.1  # Penalty for high volatility
        
        return signals
    
    def _combine_signals(self, signals: Dict[str, float],
                        current_price: Dict[str, float],
                        symbol: str) -> Dict[str, Any]:
        """
        Combine multiple signals into a trading decision.
        
        Args:
            signals: Dictionary of signal strengths
            current_price: Current prices
            symbol: Trading symbol
            
        Returns:
            Trading decision
        """
        if not signals:
            return {"action": "HOLD", "confidence": 0.0, "reason": "No signals"}
        
        # Weighted average of signals
        weights = {
            'rsi': 0.3,
            'ma_trend': 0.4,
            'price_position': 0.2,
            'volatility': 0.1
        }
        
        weighted_signal = sum(
            signals.get(key, 0) * weight 
            for key, weight in weights.items()
        )
        
        # Calculate confidence
        confidence = abs(weighted_signal)
        
        # Determine action
        if confidence < self.min_confidence:
            action = "HOLD"
            reason = f"Low confidence ({confidence:.2%})"
        elif weighted_signal > 0:
            action = "BUY"
            reason = self._generate_reason(signals, bullish=True)
        else:
            action = "SELL"
            reason = self._generate_reason(signals, bullish=False)
        
        # Calculate stop loss and take profit
        current = current_price.get('ask' if action == 'BUY' else 'bid', 0)
        stop_loss, take_profit = self._calculate_levels(current, action)
        
        decision = {
            "action": action,
            "confidence": confidence,
            "reason": reason,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "signals": signals,
            "weighted_signal": weighted_signal
        }
        
        self.logger.info(f"{symbol}: {action} (Confidence: {confidence:.2%}) - {reason}")
        
        return decision
    
    def _generate_reason(self, signals: Dict[str, float], bullish: bool) -> str:
        """Generate human-readable reason for decision."""
        reasons = []
        
        rsi = signals.get('rsi', 0)
        if bullish and rsi > 0.3:
            reasons.append("RSI oversold")
        elif not bullish and rsi < -0.3:
            reasons.append("RSI overbought")
        
        ma_trend = signals.get('ma_trend', 0)
        if bullish and ma_trend > 0.2:
            reasons.append("Bullish MA crossover")
        elif not bullish and ma_trend < -0.2:
            reasons.append("Bearish MA crossover")
        
        if not reasons:
            reasons.append("Multiple technical signals")
        
        return ", ".join(reasons)
    
    def _calculate_levels(self, price: float, action: str,
                         atr_multiplier: float = 2.0) -> tuple:
        """
        Calculate stop loss and take profit levels.
        
        Args:
            price: Current price
            action: BUY or SELL
            atr_multiplier: ATR multiplier for levels
            
        Returns:
            Tuple of (stop_loss, take_profit)
        """
        # Simple fixed percentage for now
        # In production, should use ATR or other dynamic methods
        sl_pct = self.config.get('stop_loss_pct', 0.01)  # 1%
        tp_pct = self.config.get('take_profit_pct', 0.02)  # 2%
        
        if action == "BUY":
            stop_loss = price * (1 - sl_pct)
            take_profit = price * (1 + tp_pct)
        else:  # SELL
            stop_loss = price * (1 + sl_pct)
            take_profit = price * (1 - tp_pct)
        
        return stop_loss, take_profit
    
    def get_portfolio_status(self) -> Dict[str, Any]:
        """
        Get current portfolio status.
        
        Returns:
            Portfolio status dictionary
        """
        account = self.connector.get_account_info()
        positions = self.order_manager.get_positions()
        
        return {
            "timestamp": datetime.now().isoformat(),
            "account": account,
            "positions": [
                {
                    "ticket": pos.ticket,
                    "symbol": pos.symbol,
                    "type": pos.order_type,
                    "volume": pos.volume,
                    "profit": pos.profit,
                    "open_time": pos.open_time.isoformat()
                }
                for pos in positions
            ],
            "total_positions": len(positions),
            "total_profit": self.order_manager.get_total_profit()
        }
