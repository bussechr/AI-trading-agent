"""MT4 Bridge Module - Interface for MetaTrader 4 connectivity."""

from .connector import MT4Connector
from .order_manager import OrderManager

__all__ = ['MT4Connector', 'OrderManager']
