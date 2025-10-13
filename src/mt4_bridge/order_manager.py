"""Order Manager - Handles order lifecycle and risk management."""

import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
from .connector import MT4Connector, OrderType


@dataclass
class Position:
    """Represents a trading position."""
    ticket: int
    symbol: str
    order_type: str
    volume: float
    open_price: float
    current_price: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    profit: float
    open_time: datetime
    comment: str = ""


class OrderManager:
    """
    Manages order execution and position tracking.
    
    Provides high-level order management with risk controls.
    """
    
    def __init__(self, connector: MT4Connector, max_positions: int = 10,
                 max_risk_per_trade: float = 0.02):
        """
        Initialize Order Manager.
        
        Args:
            connector: MT4 connector instance
            max_positions: Maximum number of concurrent positions
            max_risk_per_trade: Maximum risk per trade as fraction of account
        """
        self.connector = connector
        self.max_positions = max_positions
        self.max_risk_per_trade = max_risk_per_trade
        self.logger = logging.getLogger(__name__)
        
        self._positions: Dict[int, Position] = {}
    
    def update_positions(self) -> None:
        """Update all open positions from MT4."""
        orders = self.connector.get_open_orders()
        
        self._positions.clear()
        for order in orders:
            position = Position(
                ticket=order['ticket'],
                symbol=order['symbol'],
                order_type=order['type'],
                volume=order['volume'],
                open_price=order['open_price'],
                current_price=order['current_price'],
                stop_loss=order.get('stop_loss'),
                take_profit=order.get('take_profit'),
                profit=order.get('profit', 0.0),
                open_time=datetime.fromisoformat(order['open_time']),
                comment=order.get('comment', '')
            )
            self._positions[order['ticket']] = position
    
    def get_positions(self) -> List[Position]:
        """
        Get all current positions.
        
        Returns:
            List of Position objects
        """
        self.update_positions()
        return list(self._positions.values())
    
    def get_position_by_ticket(self, ticket: int) -> Optional[Position]:
        """
        Get a specific position by ticket.
        
        Args:
            ticket: Order ticket number
            
        Returns:
            Position object or None if not found
        """
        self.update_positions()
        return self._positions.get(ticket)
    
    def calculate_position_size(self, symbol: str, risk_amount: float,
                               entry_price: float, 
                               stop_loss: float) -> float:
        """
        Calculate position size based on risk parameters.
        
        Args:
            symbol: Trading symbol
            risk_amount: Amount to risk in account currency
            entry_price: Entry price
            stop_loss: Stop loss price
            
        Returns:
            Position size in lots
        """
        # Calculate pip value and risk
        pip_risk = abs(entry_price - stop_loss)
        
        if pip_risk == 0:
            self.logger.warning("Stop loss equals entry price")
            return 0.0
        
        # Simple calculation (should be enhanced with contract size)
        # This is a basic implementation
        position_size = risk_amount / pip_risk
        
        # Round to 2 decimal places (0.01 lot minimum for most brokers)
        position_size = round(position_size, 2)
        
        return max(0.01, position_size)  # Minimum 0.01 lots
    
    def open_market_order(self, symbol: str, order_type: OrderType,
                         risk_pct: Optional[float] = None,
                         volume: Optional[float] = None,
                         stop_loss: Optional[float] = None,
                         take_profit: Optional[float] = None,
                         comment: str = "") -> Optional[int]:
        """
        Open a market order with risk management.
        
        Args:
            symbol: Trading symbol
            order_type: BUY or SELL
            risk_pct: Risk as percentage of account (overrides volume)
            volume: Order volume in lots
            stop_loss: Stop loss price
            take_profit: Take profit price
            comment: Order comment
            
        Returns:
            Order ticket number or None if error
        """
        # Check position limit
        self.update_positions()
        if len(self._positions) >= self.max_positions:
            self.logger.warning(f"Maximum positions ({self.max_positions}) reached")
            return None
        
        # Calculate volume based on risk if specified
        if risk_pct and stop_loss:
            balance = self.connector.get_balance()
            if balance:
                risk_amount = balance * min(risk_pct, self.max_risk_per_trade)
                price_data = self.connector.get_current_price(symbol)
                if price_data:
                    entry_price = price_data['ask'] if order_type == OrderType.BUY else price_data['bid']
                    volume = self.calculate_position_size(symbol, risk_amount, 
                                                         entry_price, stop_loss)
        
        if not volume or volume <= 0:
            self.logger.error("Invalid order volume")
            return None
        
        # Open order
        ticket = self.connector.open_order(
            symbol=symbol,
            order_type=order_type,
            volume=volume,
            stop_loss=stop_loss,
            take_profit=take_profit,
            comment=comment
        )
        
        if ticket:
            self.logger.info(f"Opened {order_type.name} order #{ticket} for {symbol}")
        else:
            self.logger.error(f"Failed to open order for {symbol}")
        
        return ticket
    
    def close_position(self, ticket: int) -> bool:
        """
        Close a position.
        
        Args:
            ticket: Order ticket number
            
        Returns:
            True if successful, False otherwise
        """
        success = self.connector.close_order(ticket)
        
        if success:
            self.logger.info(f"Closed position #{ticket}")
            if ticket in self._positions:
                del self._positions[ticket]
        else:
            self.logger.error(f"Failed to close position #{ticket}")
        
        return success
    
    def close_all_positions(self) -> int:
        """
        Close all open positions.
        
        Returns:
            Number of positions closed
        """
        self.update_positions()
        closed_count = 0
        
        for ticket in list(self._positions.keys()):
            if self.close_position(ticket):
                closed_count += 1
        
        return closed_count
    
    def get_total_exposure(self) -> float:
        """
        Calculate total exposure across all positions.
        
        Returns:
            Total exposure in account currency
        """
        self.update_positions()
        total = sum(pos.volume * pos.open_price for pos in self._positions.values())
        return total
    
    def get_total_profit(self) -> float:
        """
        Calculate total profit/loss across all positions.
        
        Returns:
            Total P&L
        """
        self.update_positions()
        return sum(pos.profit for pos in self._positions.values())
