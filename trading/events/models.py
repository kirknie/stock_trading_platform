"""
Core domain models for the trading system.

This module defines the fundamental data structures for orders and trades.
All models use immutable-style dataclasses for safety and clarity.
"""

from enum import Enum
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime
from typing import Optional


class OrderSide(Enum):
    """Side of the order: buy or sell."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """Type of order: limit (price specified) or market (immediate execution)."""
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(Enum):
    """Current status of an order in its lifecycle."""
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass
class Order:
    """
    Represents a trading order.

    Attributes:
        order_id: Unique identifier for the order
        ticker: Stock symbol (e.g., 'AAPL', 'MSFT')
        side: BUY or SELL
        order_type: LIMIT or MARKET
        quantity: Number of shares
        price: Limit price (None for market orders)
        timestamp: When the order was created
        status: Current order status
        filled_quantity: How many shares have been filled
        account_id: Account that placed the order
    """
    order_id: str
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[Decimal]
    timestamp: datetime
    status: OrderStatus = OrderStatus.NEW
    filled_quantity: int = 0
    account_id: str = "default"

    def remaining_quantity(self) -> int:
        """Calculate how many shares remain unfilled."""
        return self.quantity - self.filled_quantity

    def is_complete(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.REJECTED
        )


@dataclass
class Trade:
    """
    Represents a completed trade between two orders.

    Attributes:
        trade_id: Unique identifier for the trade
        ticker: Stock symbol
        buyer_order_id: Order ID of the buy side
        seller_order_id: Order ID of the sell side
        price: Execution price
        quantity: Number of shares traded
        timestamp: When the trade occurred
    """
    trade_id: str
    ticker: str
    buyer_order_id: str
    seller_order_id: str
    price: Decimal
    quantity: int
    timestamp: datetime
