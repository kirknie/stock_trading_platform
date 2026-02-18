"""
Tests for the OrderBook matching engine.

Tests price-time priority matching, partial fills, market orders,
and order cancellation for a single ticker.
"""

from decimal import Decimal
from datetime import datetime
from trading.engine.order_book import OrderBook
from trading.events.models import Order, OrderSide, OrderType, OrderStatus


def test_empty_order_book():
    """Test empty order book has no bids/asks."""
    book = OrderBook("AAPL")
    assert book.ticker == "AAPL"
    assert book.get_best_bid() is None
    assert book.get_best_ask() is None
    assert book.get_spread() is None


def test_add_single_limit_buy():
    """Test adding a single buy limit order."""
    book = OrderBook("AAPL")
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(order)

    assert len(trades) == 0
    assert book.get_best_bid() == Decimal("150.00")
    assert book.get_best_ask() is None


def test_add_single_limit_sell():
    """Test adding a single sell limit order."""
    book = OrderBook("AAPL")
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("151.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(order)

    assert len(trades) == 0
    assert book.get_best_bid() is None
    assert book.get_best_ask() == Decimal("151.00")


def test_immediate_full_match():
    """Test immediate full match between buy and sell."""
    book = OrderBook("AAPL")

    # Add sell order
    sell_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_order)

    # Add matching buy order
    buy_order = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy_order)

    assert len(trades) == 1
    assert trades[0].quantity == 100
    assert trades[0].price == Decimal("150.00")
    assert buy_order.status == OrderStatus.FILLED
    assert sell_order.status == OrderStatus.FILLED
    assert book.get_best_ask() is None


def test_partial_fill():
    """Test partial fill of an order."""
    book = OrderBook("AAPL")

    # Add large sell order
    sell_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_order)

    # Add smaller buy order
    buy_order = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=30,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy_order)

    assert len(trades) == 1
    assert trades[0].quantity == 30
    assert buy_order.status == OrderStatus.FILLED
    assert sell_order.status == OrderStatus.PARTIALLY_FILLED
    assert sell_order.remaining_quantity() == 70


def test_price_time_priority():
    """Test price-time priority matching (FIFO at same price)."""
    book = OrderBook("AAPL")

    # Add two buy orders at same price
    order1 = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    order2 = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(order1)
    book.add_limit_order(order2)

    # Add sell order that matches one
    sell_order = Order(
        order_id="3",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(sell_order)

    # First order should match (time priority)
    assert len(trades) == 1
    assert trades[0].buyer_order_id == "1"
    assert order1.status == OrderStatus.FILLED
    assert order2.status == OrderStatus.NEW


def test_market_order_buy():
    """Test market buy order execution."""
    book = OrderBook("AAPL")

    # Add sell order
    sell_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_order)

    # Execute market buy
    market_order = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(market_order)

    assert len(trades) == 1
    assert trades[0].price == Decimal("150.00")
    assert market_order.status == OrderStatus.FILLED


def test_market_order_no_liquidity():
    """Test market order rejection when no liquidity."""
    book = OrderBook("AAPL")

    # Execute market buy with no sellers
    market_order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(market_order)

    assert len(trades) == 0
    assert market_order.status == OrderStatus.REJECTED


def test_cancel_order():
    """Test canceling an order by ID."""
    book = OrderBook("AAPL")

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(order)

    success = book.cancel_order("1")
    assert success
    assert order.status == OrderStatus.CANCELED
    assert book.get_best_bid() is None


def test_cancel_nonexistent_order():
    """Test canceling nonexistent order returns False."""
    book = OrderBook("AAPL")
    success = book.cancel_order("999")
    assert not success


def test_multiple_price_levels():
    """Test multiple price levels on both sides."""
    book = OrderBook("AAPL")

    # Add bids at different prices
    book.add_limit_order(Order(
        order_id="B1", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("150.00"),
        timestamp=datetime.now()
    ))
    book.add_limit_order(Order(
        order_id="B2", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("149.00"),
        timestamp=datetime.now()
    ))

    # Add asks at different prices
    book.add_limit_order(Order(
        order_id="S1", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("151.00"),
        timestamp=datetime.now()
    ))
    book.add_limit_order(Order(
        order_id="S2", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("152.00"),
        timestamp=datetime.now()
    ))

    assert book.get_best_bid() == Decimal("150.00")
    assert book.get_best_ask() == Decimal("151.00")
    assert book.get_spread() == Decimal("1.00")


def test_price_priority():
    """Test that better prices match first."""
    book = OrderBook("AAPL")

    # Add two sell orders at different prices
    sell_low = Order(
        order_id="S1", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=50, price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    sell_high = Order(
        order_id="S2", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=50, price=Decimal("151.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell_high)  # Add higher price first
    book.add_limit_order(sell_low)   # Add lower price second

    # Market buy should match lower price first
    buy = Order(
        order_id="B1", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=50, price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(buy)

    assert len(trades) == 1
    assert trades[0].price == Decimal("150.00")
    assert trades[0].seller_order_id == "S1"


def test_limit_order_no_match_due_to_price():
    """Test limit order doesn't match if price doesn't cross."""
    book = OrderBook("AAPL")

    # Add sell at 151
    sell = Order(
        order_id="S1", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("151.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    # Buy at 150 (doesn't cross)
    buy = Order(
        order_id="B1", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy)

    assert len(trades) == 0
    assert book.get_best_bid() == Decimal("150.00")
    assert book.get_best_ask() == Decimal("151.00")


def test_aggressive_limit_order():
    """Test aggressive limit order that crosses spread."""
    book = OrderBook("AAPL")

    # Add sell at 150
    sell = Order(
        order_id="S1", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    # Buy at 151 (crosses spread, should match at 150)
    buy = Order(
        order_id="B1", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("151.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy)

    assert len(trades) == 1
    assert trades[0].price == Decimal("150.00")  # Trade at resting order price


def test_partial_market_order():
    """Test market order partial fill."""
    book = OrderBook("AAPL")

    # Add small sell order
    sell = Order(
        order_id="S1", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=50, price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    # Large market buy
    buy = Order(
        order_id="B1", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=100, price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(buy)

    # Should fill 50, reject remaining 50
    assert len(trades) == 1
    assert trades[0].quantity == 50
    assert buy.filled_quantity == 50
    assert buy.status == OrderStatus.REJECTED  # Rejected because can't fill all


def test_sweep_multiple_levels():
    """Test order sweeping through multiple price levels."""
    book = OrderBook("AAPL")

    # Add multiple sell levels
    book.add_limit_order(Order(
        order_id="S1", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=50, price=Decimal("150.00"),
        timestamp=datetime.now()
    ))
    book.add_limit_order(Order(
        order_id="S2", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=50, price=Decimal("151.00"),
        timestamp=datetime.now()
    ))

    # Large buy order that sweeps both levels
    buy = Order(
        order_id="B1", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.MARKET, quantity=100, price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(buy)

    assert len(trades) == 2
    assert trades[0].price == Decimal("150.00")
    assert trades[0].quantity == 50
    assert trades[1].price == Decimal("151.00")
    assert trades[1].quantity == 50
    assert buy.status == OrderStatus.FILLED
