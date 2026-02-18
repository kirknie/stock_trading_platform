"""
Tests for the OrderBookManager.

Tests multi-ticker routing, order isolation between tickers,
cancellation, and error handling.
"""

from decimal import Decimal
from datetime import datetime
import pytest
from trading.engine.order_book_manager import OrderBookManager
from trading.events.models import Order, OrderSide, OrderType, OrderStatus


def test_manager_initialization():
    """Test manager initializes with supported tickers."""
    tickers = ["AAPL", "MSFT", "GOOGL"]
    manager = OrderBookManager(tickers)

    assert set(manager.get_supported_tickers()) == set(tickers)
    assert len(manager.order_books) == 3


def test_submit_order_to_correct_book():
    """Test order is routed to correct ticker's book."""
    manager = OrderBookManager(["AAPL", "MSFT"])

    # Submit to AAPL
    order_aapl = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = manager.submit_order(order_aapl)

    assert len(trades) == 0
    assert manager.get_order_book("AAPL").get_best_bid() == Decimal("150.00")
    assert manager.get_order_book("MSFT").get_best_bid() is None


def test_submit_to_unsupported_ticker():
    """Test submitting order to unsupported ticker raises error."""
    manager = OrderBookManager(["AAPL"])

    order = Order(
        order_id="1",
        ticker="INVALID",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )

    with pytest.raises(ValueError, match="not supported"):
        manager.submit_order(order)


def test_multi_ticker_matching():
    """Test matching works independently for each ticker."""
    manager = OrderBookManager(["AAPL", "MSFT"])

    # Add orders for both tickers
    sell_aapl = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(sell_aapl)

    sell_msft = Order(
        order_id="2",
        ticker="MSFT",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(sell_msft)

    # Match AAPL
    buy_aapl = Order(
        order_id="3",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades_aapl = manager.submit_order(buy_aapl)

    # Match MSFT
    buy_msft = Order(
        order_id="4",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    trades_msft = manager.submit_order(buy_msft)

    assert len(trades_aapl) == 1
    assert trades_aapl[0].ticker == "AAPL"
    assert len(trades_msft) == 1
    assert trades_msft[0].ticker == "MSFT"


def test_cancel_in_correct_book():
    """Test cancellation works in correct ticker's book."""
    manager = OrderBookManager(["AAPL", "MSFT"])

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(order)

    # Cancel in wrong book
    assert not manager.cancel_order("MSFT", "1")

    # Cancel in correct book
    assert manager.cancel_order("AAPL", "1")
    assert order.status == OrderStatus.CANCELED


def test_order_isolation_between_tickers():
    """Test orders in one ticker don't affect another."""
    manager = OrderBookManager(["AAPL", "MSFT", "GOOGL"])

    # Fill AAPL with orders
    for i in range(5):
        order = Order(
            order_id=f"AAPL_{i}",
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=Decimal("150.00") + Decimal(i),
            timestamp=datetime.now()
        )
        manager.submit_order(order)

    # MSFT and GOOGL should be unaffected
    assert manager.get_order_book("MSFT").get_best_bid() is None
    assert manager.get_order_book("GOOGL").get_best_bid() is None
    assert manager.get_order_book("AAPL").get_best_bid() == Decimal("154.00")


def test_market_order_routing():
    """Test market orders are routed correctly."""
    manager = OrderBookManager(["AAPL", "MSFT"])

    # Add sell order in AAPL
    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(sell)

    # Market buy in AAPL
    market_buy = Order(
        order_id="M1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    trades = manager.submit_order(market_buy)

    assert len(trades) == 1
    assert trades[0].ticker == "AAPL"
    assert market_buy.status == OrderStatus.FILLED


def test_get_order_book_unsupported_ticker():
    """Test getting order book for unsupported ticker raises error."""
    manager = OrderBookManager(["AAPL"])

    with pytest.raises(ValueError, match="not supported"):
        manager.get_order_book("INVALID")


def test_cancel_unsupported_ticker():
    """Test canceling in unsupported ticker returns False."""
    manager = OrderBookManager(["AAPL"])

    result = manager.cancel_order("INVALID", "1")
    assert result is False


def test_same_order_id_different_tickers():
    """Test same order ID can exist in different tickers."""
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


def test_multiple_tickers_simultaneous():
    """Test simultaneous activity across multiple tickers."""
    manager = OrderBookManager(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    # Add buy orders for all tickers
    tickers_prices = [
        ("AAPL", "150.00"),
        ("MSFT", "300.00"),
        ("GOOGL", "2500.00"),
        ("TSLA", "200.00"),
        ("NVDA", "500.00")
    ]

    for ticker, price in tickers_prices:
        order = Order(
            order_id=f"{ticker}_BUY",
            ticker=ticker,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=Decimal(price),
            timestamp=datetime.now()
        )
        manager.submit_order(order)

    # Verify each book has correct bid
    for ticker, price in tickers_prices:
        assert manager.get_order_book(ticker).get_best_bid() == Decimal(price)


def test_get_supported_tickers_sorted():
    """Test supported tickers are returned sorted."""
    manager = OrderBookManager(["TSLA", "AAPL", "MSFT", "GOOGL", "NVDA"])

    tickers = manager.get_supported_tickers()
    assert tickers == ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]


def test_unknown_order_type():
    """Test submitting order with unknown type raises error."""
    manager = OrderBookManager(["AAPL"])

    # Create an order and manually set an invalid order type
    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    # Simulate an unknown order type by modifying it
    # (In practice this shouldn't happen, but test the error handling)
    order.order_type = None

    with pytest.raises((ValueError, AttributeError)):
        manager.submit_order(order)


def test_partial_fill_across_tickers():
    """Test partial fills work independently per ticker."""
    manager = OrderBookManager(["AAPL", "MSFT"])

    # Large sell orders in both tickers
    sell_aapl = Order(
        order_id="SA1", ticker="AAPL", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    sell_msft = Order(
        order_id="SM1", ticker="MSFT", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=100, price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    manager.submit_order(sell_aapl)
    manager.submit_order(sell_msft)

    # Small buy orders (partial fills)
    buy_aapl = Order(
        order_id="BA1", ticker="AAPL", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=30, price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    buy_msft = Order(
        order_id="BM1", ticker="MSFT", side=OrderSide.BUY,
        order_type=OrderType.LIMIT, quantity=40, price=Decimal("300.00"),
        timestamp=datetime.now()
    )

    trades_aapl = manager.submit_order(buy_aapl)
    trades_msft = manager.submit_order(buy_msft)

    assert len(trades_aapl) == 1
    assert trades_aapl[0].quantity == 30
    assert sell_aapl.remaining_quantity() == 70

    assert len(trades_msft) == 1
    assert trades_msft[0].quantity == 40
    assert sell_msft.remaining_quantity() == 60
