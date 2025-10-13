"""MT4 Connector - Handles connection and communication with MetaTrader 4."""

import zmq
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from enum import Enum


class OrderType(Enum):
    """MT4 Order Types."""
    BUY = 0
    SELL = 1
    BUY_LIMIT = 2
    SELL_LIMIT = 3
    BUY_STOP = 4
    SELL_STOP = 5


class MT4Connector:
    """
    Connector for MetaTrader 4 using ZeroMQ.
    
    This class provides a Python interface to communicate with MT4
    via ZeroMQ sockets. Requires MT4 EA with ZeroMQ implementation.
    """
    
    def __init__(self, host: str = "localhost", req_port: int = 5555, 
                 pull_port: int = 5556, timeout: int = 10000):
        """
        Initialize MT4 Connector.
        
        Args:
            host: MT4 server host
            req_port: Request/Reply port
            pull_port: Pull port for market data
            timeout: Connection timeout in milliseconds
        """
        self.host = host
        self.req_port = req_port
        self.pull_port = pull_port
        self.timeout = timeout
        
        self.context = None
        self.req_socket = None
        self.pull_socket = None
        self.connected = False
        
        self.logger = logging.getLogger(__name__)
        
    def connect(self) -> bool:
        """
        Establish connection to MT4.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            self.context = zmq.Context()
            
            # REQ socket for commands
            self.req_socket = self.context.socket(zmq.REQ)
            self.req_socket.setsockopt(zmq.RCVTIMEO, self.timeout)
            self.req_socket.setsockopt(zmq.SNDTIMEO, self.timeout)
            self.req_socket.connect(f"tcp://{self.host}:{self.req_port}")
            
            # PULL socket for market data
            self.pull_socket = self.context.socket(zmq.PULL)
            self.pull_socket.setsockopt(zmq.RCVTIMEO, self.timeout)
            self.pull_socket.connect(f"tcp://{self.host}:{self.pull_port}")
            
            self.connected = True
            self.logger.info(f"Connected to MT4 at {self.host}:{self.req_port}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to connect to MT4: {e}")
            self.connected = False
            return False
    
    def disconnect(self) -> None:
        """Close connection to MT4."""
        if self.req_socket:
            self.req_socket.close()
        if self.pull_socket:
            self.pull_socket.close()
        if self.context:
            self.context.term()
        
        self.connected = False
        self.logger.info("Disconnected from MT4")
    
    def _send_command(self, command: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Send command to MT4 and wait for response.
        
        Args:
            command: Command dictionary
            
        Returns:
            Response dictionary or None if error
        """
        if not self.connected:
            self.logger.error("Not connected to MT4")
            return None
        
        try:
            # Send command
            self.req_socket.send_string(json.dumps(command))
            
            # Wait for response
            response = self.req_socket.recv_string()
            return json.loads(response)
            
        except zmq.error.Again:
            self.logger.error("Request timeout")
            return None
        except Exception as e:
            self.logger.error(f"Error sending command: {e}")
            return None
    
    def get_account_info(self) -> Optional[Dict[str, Any]]:
        """
        Get account information.
        
        Returns:
            Dictionary with account info or None if error
        """
        command = {"action": "ACCOUNT_INFO"}
        return self._send_command(command)
    
    def get_balance(self) -> Optional[float]:
        """
        Get account balance.
        
        Returns:
            Account balance or None if error
        """
        info = self.get_account_info()
        return info.get("balance") if info else None
    
    def get_equity(self) -> Optional[float]:
        """
        Get account equity.
        
        Returns:
            Account equity or None if error
        """
        info = self.get_account_info()
        return info.get("equity") if info else None
    
    def get_market_data(self, symbol: str, timeframe: str = "M1", 
                       bars: int = 100) -> Optional[Dict[str, Any]]:
        """
        Get market data for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., "EURUSD")
            timeframe: Timeframe (e.g., "M1", "H1", "D1")
            bars: Number of bars to retrieve
            
        Returns:
            Market data dictionary or None if error
        """
        command = {
            "action": "GET_DATA",
            "symbol": symbol,
            "timeframe": timeframe,
            "bars": bars
        }
        return self._send_command(command)
    
    def get_current_price(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        Get current bid/ask prices for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Dictionary with 'bid' and 'ask' prices or None if error
        """
        command = {
            "action": "GET_PRICE",
            "symbol": symbol
        }
        return self._send_command(command)
    
    def open_order(self, symbol: str, order_type: OrderType, 
                   volume: float, price: Optional[float] = None,
                   stop_loss: Optional[float] = None, 
                   take_profit: Optional[float] = None,
                   comment: str = "") -> Optional[int]:
        """
        Open a new order.
        
        Args:
            symbol: Trading symbol
            order_type: Type of order (BUY, SELL, etc.)
            volume: Order volume in lots
            price: Order price (None for market orders)
            stop_loss: Stop loss price
            take_profit: Take profit price
            comment: Order comment
            
        Returns:
            Order ticket number or None if error
        """
        command = {
            "action": "OPEN_ORDER",
            "symbol": symbol,
            "type": order_type.value,
            "volume": volume,
            "price": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "comment": comment
        }
        
        response = self._send_command(command)
        return response.get("ticket") if response else None
    
    def close_order(self, ticket: int, volume: Optional[float] = None) -> bool:
        """
        Close an existing order.
        
        Args:
            ticket: Order ticket number
            volume: Partial close volume (None for full close)
            
        Returns:
            True if successful, False otherwise
        """
        command = {
            "action": "CLOSE_ORDER",
            "ticket": ticket,
            "volume": volume
        }
        
        response = self._send_command(command)
        return response.get("success", False) if response else False
    
    def modify_order(self, ticket: int, stop_loss: Optional[float] = None,
                    take_profit: Optional[float] = None) -> bool:
        """
        Modify an existing order.
        
        Args:
            ticket: Order ticket number
            stop_loss: New stop loss price
            take_profit: New take profit price
            
        Returns:
            True if successful, False otherwise
        """
        command = {
            "action": "MODIFY_ORDER",
            "ticket": ticket,
            "stop_loss": stop_loss,
            "take_profit": take_profit
        }
        
        response = self._send_command(command)
        return response.get("success", False) if response else False
    
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get all open orders.
        
        Returns:
            List of open orders
        """
        command = {"action": "GET_ORDERS"}
        response = self._send_command(command)
        return response.get("orders", []) if response else []
    
    def get_order_info(self, ticket: int) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific order.
        
        Args:
            ticket: Order ticket number
            
        Returns:
            Order information dictionary or None if error
        """
        command = {
            "action": "GET_ORDER",
            "ticket": ticket
        }
        return self._send_command(command)


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO)
    
    connector = MT4Connector()
    if connector.connect():
        print("Connected to MT4")
        
        # Get account info
        account = connector.get_account_info()
        print(f"Account: {account}")
        
        # Get price
        price = connector.get_current_price("EURUSD")
        print(f"EURUSD Price: {price}")
        
        connector.disconnect()
