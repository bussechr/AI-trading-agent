"""AI Trading Agent - Main agent orchestrator."""

import logging
import time
from typing import Dict, List, Optional, Any
from datetime import datetime
import numpy as np

from ..mt4_bridge.connector import MT4Connector, OrderType
from ..mt4_bridge.order_manager import OrderManager
from .decision_engine import DecisionEngine


class TradingAgent:
    """
    Main AI Trading Agent.
    
    Orchestrates market analysis, decision making, and order execution.
    """
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize Trading Agent.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Initialize components
        self.connector = MT4Connector(
            host=config.get('mt4_host', 'localhost'),
            req_port=config.get('mt4_req_port', 5555),
            pull_port=config.get('mt4_pull_port', 5556)
        )
        
        self.order_manager = OrderManager(
            connector=self.connector,
            max_positions=config.get('max_positions', 5),
            max_risk_per_trade=config.get('max_risk_per_trade', 0.02)
        )
        
        self.decision_engine = DecisionEngine(config)
        
        self.symbols = config.get('symbols', ['EURUSD', 'GBPUSD', 'USDJPY'])
        self.timeframe = config.get('timeframe', 'H1')
        self.running = False
        
    def start(self) -> bool:
        """
        Start the trading agent.
        
        Returns:
            True if started successfully, False otherwise
        """
        if not self.connector.connect():
            self.logger.error("Failed to connect to MT4")
            return False
        
        self.running = True
        self.logger.info("Trading Agent started")
        return True
    
    def stop(self) -> None:
        """Stop the trading agent."""
        self.running = False
        self.connector.disconnect()
        self.logger.info("Trading Agent stopped")
    
    def analyze_market(self, symbol: str) -> Dict[str, Any]:
        """
        Analyze market conditions for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Analysis results dictionary
        """
        # Get market data
        market_data = self.connector.get_market_data(
            symbol=symbol,
            timeframe=self.timeframe,
            bars=200
        )
        
        if not market_data:
            return {"error": "Failed to get market data"}
        
        # Get current price
        current_price = self.connector.get_current_price(symbol)
        
        analysis = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "market_data": market_data,
            "current_price": current_price,
            "indicators": self._calculate_indicators(market_data)
        }
        
        return analysis
    
    def _calculate_indicators(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate technical indicators.
        
        Args:
            market_data: Market data dictionary
            
        Returns:
            Dictionary of calculated indicators
        """
        if 'close' not in market_data:
            return {}
        
        closes = np.array(market_data['close'])
        
        # Simple Moving Averages
        sma_20 = np.mean(closes[-20:]) if len(closes) >= 20 else None
        sma_50 = np.mean(closes[-50:]) if len(closes) >= 50 else None
        sma_200 = np.mean(closes[-200:]) if len(closes) >= 200 else None
        
        # RSI
        rsi = self._calculate_rsi(closes)
        
        # Volatility
        volatility = np.std(closes[-20:]) if len(closes) >= 20 else None
        
        return {
            "sma_20": sma_20,
            "sma_50": sma_50,
            "sma_200": sma_200,
            "rsi": rsi,
            "volatility": volatility
        }
    
    def _calculate_rsi(self, prices: np.ndarray, period: int = 14) -> Optional[float]:
        """
        Calculate Relative Strength Index.
        
        Args:
            prices: Price array
            period: RSI period
            
        Returns:
            RSI value or None if insufficient data
        """
        if len(prices) < period + 1:
            return None
        
        deltas = np.diff(prices)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def execute_trading_cycle(self) -> None:
        """Execute one trading cycle - analyze and make decisions."""
        try:
            # Get account info
            account = self.connector.get_account_info()
            if not account:
                self.logger.error("Failed to get account info")
                return
            
            self.logger.info(f"Account Balance: {account.get('balance', 'N/A')}, "
                           f"Equity: {account.get('equity', 'N/A')}")
            
            # Analyze each symbol
            for symbol in self.symbols:
                self._process_symbol(symbol, account)
                
        except Exception as e:
            self.logger.error(f"Error in trading cycle: {e}", exc_info=True)
    
    def _process_symbol(self, symbol: str, account: Dict[str, Any]) -> None:
        """
        Process a single symbol - analyze and execute if signal found.
        
        Args:
            symbol: Trading symbol
            account: Account information
        """
        # Analyze market
        analysis = self.analyze_market(symbol)
        
        if "error" in analysis:
            self.logger.error(f"Error analyzing {symbol}: {analysis['error']}")
            return
        
        # Get trading decision
        decision = self.decision_engine.make_decision(analysis)
        
        if decision['action'] == 'BUY':
            self._execute_buy(symbol, decision, account)
        elif decision['action'] == 'SELL':
            self._execute_sell(symbol, decision, account)
        elif decision['action'] == 'CLOSE':
            self._execute_close(symbol, decision)
        # else: HOLD - do nothing
    
    def _execute_buy(self, symbol: str, decision: Dict[str, Any], 
                    account: Dict[str, Any]) -> None:
        """Execute a buy order."""
        self.logger.info(f"BUY signal for {symbol} - Confidence: {decision.get('confidence', 0):.2%}")
        
        ticket = self.order_manager.open_market_order(
            symbol=symbol,
            order_type=OrderType.BUY,
            risk_pct=self.config.get('risk_per_trade', 0.01),
            stop_loss=decision.get('stop_loss'),
            take_profit=decision.get('take_profit'),
            comment=f"AI Agent - {decision.get('reason', '')}"
        )
        
        if ticket:
            self.logger.info(f"Successfully opened BUY order #{ticket} for {symbol}")
        else:
            self.logger.error(f"Failed to open BUY order for {symbol}")
    
    def _execute_sell(self, symbol: str, decision: Dict[str, Any],
                     account: Dict[str, Any]) -> None:
        """Execute a sell order."""
        self.logger.info(f"SELL signal for {symbol} - Confidence: {decision.get('confidence', 0):.2%}")
        
        ticket = self.order_manager.open_market_order(
            symbol=symbol,
            order_type=OrderType.SELL,
            risk_pct=self.config.get('risk_per_trade', 0.01),
            stop_loss=decision.get('stop_loss'),
            take_profit=decision.get('take_profit'),
            comment=f"AI Agent - {decision.get('reason', '')}"
        )
        
        if ticket:
            self.logger.info(f"Successfully opened SELL order #{ticket} for {symbol}")
        else:
            self.logger.error(f"Failed to open SELL order for {symbol}")
    
    def _execute_close(self, symbol: str, decision: Dict[str, Any]) -> None:
        """Execute close positions for symbol."""
        positions = self.order_manager.get_positions()
        
        for pos in positions:
            if pos.symbol == symbol:
                self.logger.info(f"CLOSE signal for {symbol} - Position #{pos.ticket}")
                self.order_manager.close_position(pos.ticket)
    
    def run(self, interval: int = 60) -> None:
        """
        Run the trading agent in a loop.
        
        Args:
            interval: Seconds between trading cycles
        """
        if not self.start():
            return
        
        self.logger.info(f"Trading agent running - checking every {interval} seconds")
        
        try:
            while self.running:
                self.execute_trading_cycle()
                time.sleep(interval)
                
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        finally:
            self.stop()
