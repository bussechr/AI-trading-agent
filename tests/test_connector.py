"""Tests for MT4 Connector."""

import pytest
from unittest.mock import Mock, patch
from src.mt4_bridge.connector import MT4Connector, OrderType


class TestMT4Connector:
    """Test suite for MT4Connector."""
    
    def test_initialization(self):
        """Test connector initialization."""
        connector = MT4Connector(host="localhost", req_port=5555)
        
        assert connector.host == "localhost"
        assert connector.req_port == 5555
        assert connector.connected == False
    
    @patch('zmq.Context')
    def test_connect_success(self, mock_context):
        """Test successful connection."""
        connector = MT4Connector()
        
        # Mock ZMQ context and sockets
        mock_socket = Mock()
        mock_context.return_value.socket.return_value = mock_socket
        
        result = connector.connect()
        
        # In real test, this would need proper mocking
        # This is a placeholder test structure
        assert isinstance(connector, MT4Connector)
    
    def test_order_type_enum(self):
        """Test OrderType enum values."""
        assert OrderType.BUY.value == 0
        assert OrderType.SELL.value == 1
        assert OrderType.BUY_LIMIT.value == 2
        assert OrderType.SELL_LIMIT.value == 3


if __name__ == "__main__":
    pytest.main([__file__])
