"""Base Strategy - Abstract base class for trading strategies."""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseStrategy(ABC):
    """
    Abstract base class for trading strategies.
    
    All custom strategies should inherit from this class.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize strategy.
        
        Args:
            config: Strategy configuration
        """
        self.config = config
        self.name = self.__class__.__name__
    
    @abstractmethod
    def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze market data and generate signals.
        
        Args:
            market_data: Market data dictionary
            
        Returns:
            Analysis result with signals
        """
        pass
    
    @abstractmethod
    def generate_signal(self, analysis: Dict[str, Any]) -> str:
        """
        Generate trading signal from analysis.
        
        Args:
            analysis: Analysis results
            
        Returns:
            Signal: 'BUY', 'SELL', or 'HOLD'
        """
        pass
    
    def calculate_position_size(self, account_balance: float,
                               risk_pct: float) -> float:
        """
        Calculate position size based on risk.
        
        Args:
            account_balance: Account balance
            risk_pct: Risk percentage (0.01 = 1%)
            
        Returns:
            Position size in lots
        """
        risk_amount = account_balance * risk_pct
        # This is simplified - should include stop loss distance
        return round(risk_amount / 1000, 2)  # Simplified calculation
    
    def validate_signal(self, signal: str, confidence: float,
                       min_confidence: float = 0.6) -> bool:
        """
        Validate if signal meets criteria for execution.
        
        Args:
            signal: Trading signal
            confidence: Signal confidence
            min_confidence: Minimum confidence threshold
            
        Returns:
            True if signal is valid, False otherwise
        """
        if signal not in ['BUY', 'SELL']:
            return False
        
        return confidence >= min_confidence
