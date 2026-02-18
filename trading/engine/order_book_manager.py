"""
Multi-ticker order book manager.

Routes orders to the appropriate ticker's order book.
Maintains isolated order books for each supported ticker.

Key features:
- Support for multiple tickers (3-5 equities)
- Order routing by ticker symbol
- Cross-ticker cancellation support
"""

from typing import Dict, List, Set
from trading.engine.order_book import OrderBook
from trading.events.models import Order, Trade, OrderType


class OrderBookManager:
    """Manages multiple order books for different tickers."""

    def __init__(self, supported_tickers: List[str]):
        """
        Initialize manager with supported tickers.

        Args:
            supported_tickers: List of ticker symbols to support (e.g., ['AAPL', 'MSFT'])
        """
        self.order_books: Dict[str, OrderBook] = {
            ticker: OrderBook(ticker) for ticker in supported_tickers
        }
        self.supported_tickers: Set[str] = set(supported_tickers)

    def submit_order(self, order: Order) -> List[Trade]:
        """
        Route order to appropriate book.

        Args:
            order: Order to submit

        Returns:
            List of trades generated

        Raises:
            ValueError: If ticker is not supported
        """
        if order.ticker not in self.supported_tickers:
            raise ValueError(f"Ticker {order.ticker} not supported")

        book = self.order_books[order.ticker]

        if order.order_type == OrderType.LIMIT:
            return book.add_limit_order(order)
        elif order.order_type == OrderType.MARKET:
            return book.execute_market_order(order)
        else:
            raise ValueError(f"Unknown order type: {order.order_type}")

    def cancel_order(self, ticker: str, order_id: str) -> bool:
        """
        Cancel order in specific ticker's book.

        Args:
            ticker: Ticker symbol
            order_id: Order ID to cancel

        Returns:
            True if order was found and canceled, False otherwise
        """
        if ticker not in self.supported_tickers:
            return False

        book = self.order_books[ticker]
        return book.cancel_order(order_id)

    def get_order_book(self, ticker: str) -> OrderBook:
        """
        Get order book for ticker.

        Args:
            ticker: Ticker symbol

        Returns:
            OrderBook instance

        Raises:
            ValueError: If ticker is not supported
        """
        if ticker not in self.supported_tickers:
            raise ValueError(f"Ticker {ticker} not supported")
        return self.order_books[ticker]

    def get_supported_tickers(self) -> List[str]:
        """
        Get list of supported tickers.

        Returns:
            Sorted list of ticker symbols
        """
        return sorted(self.supported_tickers)
