#!/usr/bin/env python3
"""
Basic usage examples for the matching engine.

Demonstrates:
1. Setting up the engine with multiple tickers
2. Submitting limit orders
3. Matching orders and generating trades
4. Executing market orders
5. Canceling orders
6. Querying market data
"""

from decimal import Decimal
from datetime import datetime
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderType


def example_1_limit_order():
    """Example 1: Submit a limit order and check market data."""
    print("=== Example 1: Limit Order ===")

    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])
    print(f"Supported tickers: {engine.get_supported_tickers()}")

    sell_order = Order(
        order_id="SELL_1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(sell_order)
    print(f"Submitted sell order: {sell_order.order_id} | trades generated: {len(trades)}")

    md = engine.get_market_data("AAPL")
    print(f"Market data: best_bid={md['best_bid']} | best_ask={md['best_ask']} | spread={md['spread']}")
    print()


def example_2_matching_orders():
    """Example 2: Submit matching buy and sell orders."""
    print("=== Example 2: Matching Orders ===")

    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    # Sell order
    sell_order = Order(
        order_id="SELL_1",
        ticker="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(sell_order)

    # Matching buy order
    buy_order = Order(
        order_id="BUY_1",
        ticker="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=100,
        price=Decimal("150.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(buy_order)
    print(f"Submitted buy order: {buy_order.order_id} | trades generated: {len(trades)}")

    for trade in trades:
        print(f"  Trade: {trade.quantity} shares of {trade.ticker} @ ${trade.price}")
        print(f"  Buyer: {trade.buyer_order_id} | Seller: {trade.seller_order_id}")

    md = engine.get_market_data("AAPL")
    print(f"Market data after match: best_bid={md['best_bid']} | best_ask={md['best_ask']}")
    print()


def example_3_market_order():
    """Example 3: Execute a market order."""
    print("=== Example 3: Market Order ===")

    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    # Add some liquidity first
    sell_order = Order(
        order_id="SELL_1",
        ticker="MSFT",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=50,
        price=Decimal("300.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(sell_order)

    # Execute market buy
    market_buy = Order(
        order_id="MKT_BUY_1",
        ticker="MSFT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=50,
        price=None,
        timestamp=datetime.now()
    )
    trades = engine.submit_order(market_buy)
    print(f"Market buy executed: {len(trades)} trade(s)")

    for trade in trades:
        print(f"  Executed {trade.quantity} shares @ ${trade.price}")

    print(f"Market order status: {market_buy.status.value}")
    print()


def example_4_market_order_no_liquidity():
    """Example 4: Market order rejected when no liquidity."""
    print("=== Example 4: Market Order - No Liquidity ===")

    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    market_buy = Order(
        order_id="MKT_BUY_1",
        ticker="TSLA",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=100,
        price=None,
        timestamp=datetime.now()
    )
    trades = engine.submit_order(market_buy)
    print(f"Market buy trades: {len(trades)}")
    print(f"Market order status: {market_buy.status.value}")
    print()


def example_5_order_cancellation():
    """Example 5: Cancel a resting order."""
    print("=== Example 5: Order Cancellation ===")

    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    order = Order(
        order_id="CANCEL_ME",
        ticker="GOOGL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=10,
        price=Decimal("2500.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(order)
    print(f"Submitted order: {order.order_id} | status: {order.status.value}")

    md = engine.get_market_data("GOOGL")
    print(f"Before cancel: best_bid={md['best_bid']}")

    success = engine.cancel_order("CANCEL_ME")
    print(f"Cancellation successful: {success} | order status: {order.status.value}")

    md = engine.get_market_data("GOOGL")
    print(f"After cancel: best_bid={md['best_bid']}")
    print()


def example_6_partial_fill():
    """Example 6: Partial fill of a large order."""
    print("=== Example 6: Partial Fill ===")

    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    # Large sell order
    large_sell = Order(
        order_id="LARGE_SELL",
        ticker="NVDA",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=1000,
        price=Decimal("500.00"),
        timestamp=datetime.now()
    )
    engine.submit_order(large_sell)

    # Small buy order (partial fill)
    small_buy = Order(
        order_id="SMALL_BUY",
        ticker="NVDA",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=300,
        price=Decimal("500.00"),
        timestamp=datetime.now()
    )
    trades = engine.submit_order(small_buy)

    print(f"Trade: {trades[0].quantity} shares @ ${trades[0].price}")
    print(f"Sell order - status: {large_sell.status.value} | filled: {large_sell.filled_quantity} | remaining: {large_sell.remaining_quantity()}")
    print(f"Buy order  - status: {small_buy.status.value} | filled: {small_buy.filled_quantity}")
    print()


def example_7_multi_ticker_snapshot():
    """Example 7: Market data snapshot across all tickers."""
    print("=== Example 7: Multi-Ticker Market Snapshot ===")

    engine = MatchingEngine(["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"])

    # Set up order books for all tickers
    setup = [
        ("AAPL",  "149.50", "150.00"),
        ("MSFT",  "299.00", "300.00"),
        ("GOOGL", "2498.00", "2500.00"),
        ("TSLA",  "199.00", "200.00"),
        ("NVDA",  "499.00", "500.00"),
    ]

    for i, (ticker, bid, ask) in enumerate(setup):
        engine.submit_order(Order(f"B{i}", ticker, OrderSide.BUY, OrderType.LIMIT, 100, Decimal(bid), datetime.now()))
        engine.submit_order(Order(f"S{i}", ticker, OrderSide.SELL, OrderType.LIMIT, 100, Decimal(ask), datetime.now()))

    # Print snapshot
    print(f"{'Ticker':<8} {'Bid':>10} {'Ask':>10} {'Spread':>10}")
    print("-" * 42)
    for ticker, _, _ in setup:
        md = engine.get_market_data(ticker)
        print(f"{ticker:<8} {str(md['best_bid']):>10} {str(md['best_ask']):>10} {str(md['spread']):>10}")
    print()


if __name__ == "__main__":
    example_1_limit_order()
    example_2_matching_orders()
    example_3_market_order()
    example_4_market_order_no_liquidity()
    example_5_order_cancellation()
    example_6_partial_fill()
    example_7_multi_ticker_snapshot()
