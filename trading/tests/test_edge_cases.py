"""
Edge case tests for the order book.

Tests specific corner cases and boundary conditions that might not
be covered by random property tests.
"""

from decimal import Decimal
from datetime import datetime
from trading.engine.order_book import OrderBook
from trading.engine.order_book_manager import OrderBookManager
from trading.events.models import Order, OrderSide, OrderType, OrderStatus


def test_zero_quantity_order():
    """Edge: Zero quantity orders should still process."""
    book = OrderBook("AAPL")

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=0,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )

    # Should not add to book or generate trades
    trades = book.add_limit_order(order)
    assert len(trades) == 0
    assert book.get_best_bid() is None


def test_very_large_orders():
    """Edge: Handle very large order quantities."""
    book = OrderBook("AAPL")

    large_qty = 1_000_000
    sell = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=large_qty,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    buy = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=large_qty,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(buy)

    assert len(trades) == 1
    assert trades[0].quantity == large_qty


def test_many_price_levels():
    """Edge: Many price levels in book."""
    book = OrderBook("AAPL")

    # Add 100 different price levels
    for i in range(100):
        order = Order(
            order_id=str(i),
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal(str(100 + i)),
            timestamp=datetime.now()
        )
        book.add_limit_order(order)

    assert book.get_best_bid() == Decimal("199")
    assert len(book.bids) == 100


def test_same_order_id_different_tickers():
    """Edge: Same order ID across tickers (should be allowed)."""
    manager = OrderBookManager(["AAPL", "MSFT"])

    order1 = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )

    order2 = Order(
        order_id="1",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )

    manager.submit_order(order1)
    manager.submit_order(order2)

    # Both should be in their respective books
    assert manager.get_order_book("AAPL").get_best_bid() == Decimal("150.00")
    assert manager.get_order_book("MSFT").get_best_bid() == Decimal("300.00")


def test_rapid_order_cancellation():
    """Edge: Cancel order immediately after submission."""
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

    # Immediate cancel
    success = book.cancel_order("1")
    assert success
    assert order.status == OrderStatus.CANCELED
    assert book.get_best_bid() is None


def test_partial_fill_then_cancel():
    """Edge: Cancel partially filled order."""
    book = OrderBook("AAPL")

    large_sell = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(large_sell)

    # Partial fill
    small_buy = Order(
        order_id="2",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=30,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(small_buy)

    assert large_sell.filled_quantity == 30
    assert large_sell.remaining_quantity() == 70

    # Cancel remaining
    success = book.cancel_order("1")
    assert success
    assert large_sell.status == OrderStatus.CANCELED
    # Filled quantity should remain
    assert large_sell.filled_quantity == 30


def test_very_small_price():
    """Edge: Very small prices (penny stocks)."""
    book = OrderBook("AAPL")

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1000,
        price=Decimal("0.01"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(order)

    assert len(trades) == 0
    assert book.get_best_bid() == Decimal("0.01")


def test_very_large_price():
    """Edge: Very large prices (high-value stocks)."""
    book = OrderBook("AAPL")

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=1,
        price=Decimal("999999.99"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(order)

    assert len(trades) == 0
    assert book.get_best_ask() == Decimal("999999.99")


def test_many_orders_same_price():
    """Edge: Many orders at the same price level."""
    book = OrderBook("AAPL")

    # Add 1000 orders at same price
    for i in range(1000):
        order = Order(
            order_id=str(i),
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=1,
            price=Decimal("150.00"),
            timestamp=datetime.now()
        )
        book.add_limit_order(order)

    assert len(book.bids[Decimal("150.00")]) == 1000


def test_alternating_buys_sells():
    """Edge: Alternating buy/sell orders."""
    book = OrderBook("AAPL")

    # Alternate between buys and sells
    for i in range(10):
        if i % 2 == 0:
            order = Order(
                order_id=f"B{i}",
                ticker="AAPL",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=10,
                price=Decimal("149.00"),
                timestamp=datetime.now()
            )
        else:
            order = Order(
                order_id=f"S{i}",
                ticker="AAPL",
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=10,
                price=Decimal("151.00"),
                timestamp=datetime.now()
            )
        book.add_limit_order(order)

    # Should have 5 buys and 5 sells
    assert len(book.bids[Decimal("149.00")]) == 5
    assert len(book.asks[Decimal("151.00")]) == 5


def test_cancel_all_orders_at_price_level():
    """Edge: Cancel all orders at a specific price level."""
    book = OrderBook("AAPL")

    # Add multiple orders at same price
    for i in range(5):
        order = Order(
            order_id=str(i),
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("150.00"),
            timestamp=datetime.now()
        )
        book.add_limit_order(order)

    # Cancel all of them
    for i in range(5):
        book.cancel_order(str(i))

    # Price level should be removed
    assert book.get_best_bid() is None
    assert Decimal("150.00") not in book.bids


def test_market_order_partial_liquidity():
    """Edge: Market order with only partial liquidity available."""
    book = OrderBook("AAPL")

    # Add limited liquidity
    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    # Large market order
    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(buy)

    # Should fill 50, reject the rest
    assert len(trades) == 1
    assert trades[0].quantity == 50
    assert buy.filled_quantity == 50
    assert buy.status == OrderStatus.REJECTED


def test_exact_price_match():
    """Edge: Orders with exact same price."""
    book = OrderBook("AAPL")

    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = book.add_limit_order(buy)

    assert len(trades) == 1
    assert trades[0].price == Decimal("150.00")


def test_empty_book_spread():
    """Edge: Spread on empty book should be None."""
    book = OrderBook("AAPL")
    assert book.get_spread() is None


def test_one_sided_book_spread():
    """Edge: Spread with only bids or only asks."""
    book = OrderBook("AAPL")

    # Only bids
    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(buy)
    assert book.get_spread() is None

    # Only asks
    book2 = OrderBook("MSFT")
    sell = Order(
        order_id="S1",
        ticker="MSFT",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    book2.add_limit_order(sell)
    assert book2.get_spread() is None


def test_decimal_precision():
    """Edge: Decimal precision is maintained."""
    book = OrderBook("AAPL")

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.123"),  # 3 decimal places
        timestamp=datetime.now()
    )
    book.add_limit_order(order)

    assert book.get_best_bid() == Decimal("150.123")


def test_order_with_single_share():
    """Edge: Order with quantity of 1."""
    book = OrderBook("AAPL")

    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=1,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=1,
        price=None,
        timestamp=datetime.now()
    )
    trades = book.execute_market_order(buy)

    assert len(trades) == 1
    assert trades[0].quantity == 1
    assert buy.status == OrderStatus.FILLED


def test_wide_spread():
    """Edge: Very wide bid-ask spread."""
    book = OrderBook("AAPL")

    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("100.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(buy)

    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("200.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    assert book.get_spread() == Decimal("100.00")


def test_tight_spread():
    """Edge: Very tight bid-ask spread."""
    book = OrderBook("AAPL")

    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    book.add_limit_order(buy)

    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.01"),
        timestamp=datetime.now()
    )
    book.add_limit_order(sell)

    assert book.get_spread() == Decimal("0.01")
