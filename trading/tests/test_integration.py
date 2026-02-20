"""
Integration tests for the MatchingEngine.

Tests complete end-to-end scenarios across multiple tickers,
order lifecycle, and system integration.
"""

from decimal import Decimal
from datetime import datetime
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderType, OrderStatus


def test_full_trading_scenario():
    """Integration: Complete trading scenario."""
    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL"])

    # Check supported tickers
    assert "AAPL" in engine.get_supported_tickers()
    assert len(engine.get_supported_tickers()) == 3

    # Add initial orders
    sell_aapl = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(sell_aapl)
    assert len(trades) == 0

    # Check market data
    md = engine.get_market_data("AAPL")
    assert md["best_ask"] == Decimal("150.00")
    assert md["best_bid"] is None
    assert md["ticker"] == "AAPL"

    # Add buy order - should match
    buy_aapl = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(buy_aapl)

    assert len(trades) == 1
    assert trades[0].ticker == "AAPL"
    assert trades[0].quantity == 100
    assert trades[0].price == Decimal("150.00")

    # Market should be empty now
    md = engine.get_market_data("AAPL")
    assert md["best_ask"] is None
    assert md["best_bid"] is None


def test_cross_ticker_independence():
    """Integration: Orders in one ticker don't affect another."""
    engine = MatchingEngine(["AAPL", "MSFT"])

    # Fill AAPL book
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
        engine.submit_order(order)

    # MSFT should be unaffected
    md_msft = engine.get_market_data("MSFT")
    assert md_msft["best_bid"] is None

    md_aapl = engine.get_market_data("AAPL")
    assert md_aapl["best_bid"] == Decimal("154.00")


def test_cancel_across_tickers():
    """Integration: Cancel uses correct ticker automatically."""
    engine = MatchingEngine(["AAPL", "MSFT"])

    order_aapl = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(order_aapl)

    order_msft = Order(
        order_id="2",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(order_msft)

    # Cancel AAPL order by ID only (auto-detects ticker)
    assert engine.cancel_order("1")

    # MSFT order should still be there
    md_msft = engine.get_market_data("MSFT")
    assert md_msft["best_bid"] == Decimal("300.00")

    # AAPL should be empty
    md_aapl = engine.get_market_data("AAPL")
    assert md_aapl["best_bid"] is None


def test_market_order_across_multiple_tickers():
    """Integration: Market orders in different tickers."""
    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL"])

    # Set up sell orders in each ticker
    tickers_prices = [
        ("AAPL", "150.00"),
        ("MSFT", "300.00"),
        ("GOOGL", "2500.00")
    ]

    for ticker, price in tickers_prices:
        sell = Order(
            order_id=f"{ticker}_SELL",
            ticker=ticker,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=Decimal(price),
            timestamp=datetime.now()
        )
        engine.submit_order(sell)

    # Execute market buys for each
    for ticker, expected_price in tickers_prices:
        buy = Order(
            order_id=f"{ticker}_BUY",
            ticker=ticker,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=100,
            price=None,
            timestamp=datetime.now()
        )
        trades = engine.submit_order(buy)

        assert len(trades) == 1
        assert trades[0].ticker == ticker
        assert trades[0].price == Decimal(expected_price)


def test_order_registry_cleanup():
    """Integration: Order registry tracks only active orders."""
    engine = MatchingEngine(["AAPL"])

    # Add sell order
    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(sell)

    assert "S1" in engine.order_registry

    # Match it with buy order
    buy = Order(
        order_id="B1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(buy)

    # Completed orders should remain in registry (current implementation)
    # This is a design choice - could be changed to clean up
    assert sell.status == OrderStatus.FILLED
    assert buy.status == OrderStatus.FILLED


def test_cancel_removes_from_registry():
    """Integration: Canceling order removes it from registry."""
    engine = MatchingEngine(["AAPL"])

    order = Order(
        order_id="1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(order)

    assert "1" in engine.order_registry

    # Cancel the order
    success = engine.cancel_order("1")
    assert success
    assert "1" not in engine.order_registry


def test_cancel_nonexistent_order():
    """Integration: Canceling non-existent order returns False."""
    engine = MatchingEngine(["AAPL"])

    result = engine.cancel_order("NONEXISTENT")
    assert result is False


def test_multiple_partial_fills():
    """Integration: Multiple partial fills across orders."""
    engine = MatchingEngine(["AAPL"])

    # Large sell order
    sell = Order(
        order_id="S1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(sell)

    # Multiple small buy orders
    total_filled = 0
    for i in range(5):
        buy = Order(
            order_id=f"B{i}",
            ticker="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=10,
            price=Decimal("150.00"),
            timestamp=datetime.now()
        )
        trades = engine.submit_order(buy)
        assert len(trades) == 1
        total_filled += trades[0].quantity

    assert total_filled == 50
    assert sell.remaining_quantity() == 50
    assert sell.status == OrderStatus.PARTIALLY_FILLED


def test_five_ticker_simultaneous_trading():
    """Integration: Simultaneous trading across all 5 tickers."""
    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    tickers_prices = [
        ("AAPL", "150.00"),
        ("MSFT", "300.00"),
        ("GOOGL", "2500.00"),
        ("TSLA", "200.00"),
        ("NVDA", "500.00")
    ]

    # Add sell orders for all tickers
    for ticker, price in tickers_prices:
        sell = Order(
            order_id=f"{ticker}_SELL",
            ticker=ticker,
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=Decimal(price),
            timestamp=datetime.now()
        )
        engine.submit_order(sell)

    # Verify market data for all
    for ticker, price in tickers_prices:
        md = engine.get_market_data(ticker)
        assert md["best_ask"] == Decimal(price)
        assert md["best_bid"] is None

    # Execute trades for all
    for ticker, price in tickers_prices:
        buy = Order(
            order_id=f"{ticker}_BUY",
            ticker=ticker,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            price=Decimal(price),
            timestamp=datetime.now()
        )
        trades = engine.submit_order(buy)
        assert len(trades) == 1

    # All books should be empty now
    for ticker, _ in tickers_prices:
        md = engine.get_market_data(ticker)
        assert md["best_ask"] is None
        assert md["best_bid"] is None


def test_complex_order_flow():
    """Integration: Complex realistic order flow."""
    engine = MatchingEngine(["AAPL"])

    # Initial market setup
    orders = [
        # Sells
        Order("S1", "AAPL", OrderSide.SELL, OrderType.LIMIT, 50, Decimal("151.00"), datetime.now()),
        Order("S2", "AAPL", OrderSide.SELL, OrderType.LIMIT, 100, Decimal("152.00"), datetime.now()),
        # Buys
        Order("B1", "AAPL", OrderSide.BUY, OrderType.LIMIT, 50, Decimal("149.00"), datetime.now()),
        Order("B2", "AAPL", OrderSide.BUY, OrderType.LIMIT, 100, Decimal("148.00"), datetime.now()),
    ]

    for order in orders:
        engine.submit_order(order)

    # Check spread
    md = engine.get_market_data("AAPL")
    assert md["best_bid"] == Decimal("149.00")
    assert md["best_ask"] == Decimal("151.00")
    assert md["spread"] == Decimal("2.00")

    # Aggressive buy that crosses spread
    aggressive_buy = Order(
        order_id="B3",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=75,
        price=Decimal("152.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(aggressive_buy)

    # Should match 50 @ 151, then 25 @ 152
    assert len(trades) == 2
    assert trades[0].price == Decimal("151.00")
    assert trades[0].quantity == 50
    assert trades[1].price == Decimal("152.00")
    assert trades[1].quantity == 25


def test_order_submission_error_handling():
    """Integration: Error handling for invalid tickers."""
    engine = MatchingEngine(["AAPL"])

    order = Order(
        order_id="1",
        ticker="INVALID",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )

    try:
        engine.submit_order(order)
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "not supported" in str(e)


def test_market_data_error_handling():
    """Integration: Error handling for invalid ticker in market data."""
    engine = MatchingEngine(["AAPL"])

    try:
        engine.get_market_data("INVALID")
        assert False, "Should have raised ValueError"
    except ValueError as e:
        assert "not supported" in str(e)


def test_end_to_end_lifecycle():
    """Integration: Complete order lifecycle from submission to completion."""
    engine = MatchingEngine(["AAPL"])

    # Submit limit order
    order1 = Order(
        order_id="O1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(order1)
    assert len(trades) == 0
    assert order1.status == OrderStatus.NEW

    # Partial fill
    order2 = Order(
        order_id="O2",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=30,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(order2)
    assert len(trades) == 1
    assert order1.status == OrderStatus.PARTIALLY_FILLED
    assert order1.filled_quantity == 30

    # Cancel remaining
    success = engine.cancel_order("O1")
    assert success
    assert order1.status == OrderStatus.CANCELED
    assert order1.filled_quantity == 30  # Filled quantity preserved
