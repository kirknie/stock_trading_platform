"""
Tests for core domain models.

Tests the Order and Trade data classes and their helper methods.
"""

from decimal import Decimal
from datetime import datetime
from trading.events.models import Order, OrderSide, OrderType, OrderStatus, Trade


def test_order_creation():
    """Test basic order creation with all required fields."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    assert order.order_id == "1"
    assert order.ticker == "AAPL"
    assert order.side == OrderSide.BUY
    assert order.order_type == OrderType.LIMIT
    assert order.quantity == 100
    assert order.price == Decimal("150.00")
    assert order.status == OrderStatus.NEW
    assert order.filled_quantity == 0
    assert order.remaining_quantity() == 100
    assert not order.is_complete()


def test_order_partial_fill():
    """Test order behavior with partial fill."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    order.filled_quantity = 30
    order.status = OrderStatus.PARTIALLY_FILLED

    assert order.remaining_quantity() == 70
    assert order.filled_quantity == 30
    assert not order.is_complete()


def test_order_complete_fill():
    """Test order behavior when fully filled."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    order.filled_quantity = 100
    order.status = OrderStatus.FILLED

    assert order.remaining_quantity() == 0
    assert order.is_complete()


def test_market_order_no_price():
    """Test market order creation without price."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    assert order.price is None
    assert order.order_type == OrderType.MARKET


def test_order_canceled():
    """Test order in canceled state."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    order.status = OrderStatus.CANCELED

    assert order.is_complete()


def test_order_rejected():
    """Test order in rejected state."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    order.status = OrderStatus.REJECTED

    assert order.is_complete()


def test_sell_order_creation():
    """Test sell order creation."""
    order = Order(
        order_id="2",
        ticker="MSFT",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    assert order.side == OrderSide.SELL
    assert order.ticker == "MSFT"


def test_trade_creation():
    """Test trade creation with all fields."""
    trade = Trade(
        trade_id="T1",
        ticker="AAPL",
        buyer_order_id="B1",
        seller_order_id="S1",
        price=Decimal("150.00"),
        quantity=100,
        timestamp=datetime.now()
    )
    assert trade.trade_id == "T1"
    assert trade.ticker == "AAPL"
    assert trade.buyer_order_id == "B1"
    assert trade.seller_order_id == "S1"
    assert trade.price == Decimal("150.00")
    assert trade.quantity == 100


def test_order_with_custom_account():
    """Test order with custom account ID."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now(),
        account_id="ACC123"
    )
    assert order.account_id == "ACC123"


def test_order_default_account():
    """Test order uses default account if not specified."""
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    assert order.account_id == "default"
