"""
High-level matching engine interface.

Provides a clean API for order submission, cancellation,
and market data retrieval across multiple tickers.

This is the main entry point for the trading system.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List

from trading.engine.order_book_manager import OrderBookManager
from trading.events.models import Order, Trade


class MatchingEngine:
    """High-level matching engine interface."""

    def __init__(self, tickers: List[str]):
        """
        Initialize matching engine with supported tickers.

        Args:
            tickers: List of ticker symbols to support (e.g., ['AAPL', 'MSFT', 'GOOGL'])
        """
        self.manager = OrderBookManager(tickers)
        self.order_registry: Dict[str, tuple[str, Order]] = {}  # order_id -> (ticker, order)
        self._registry_timestamps: Dict[str, datetime] = {}  # order_id -> registration time

    def submit_order(self, order: Order) -> List[Trade]:
        """
        Submit order and track it.

        Args:
            order: Order to submit

        Returns:
            List of trades generated

        Raises:
            ValueError: If ticker is not supported
        """
        trades = self.manager.submit_order(order)

        # Register order for cancellation/lookup if not complete
        if not order.is_complete():
            self.order_registry[order.order_id] = (order.ticker, order)
            self._registry_timestamps[order.order_id] = datetime.now(tz=timezone.utc)

        return trades

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel order by ID (auto-detects ticker).

        Args:
            order_id: Order ID to cancel

        Returns:
            True if order was found and canceled, False otherwise
        """
        if order_id not in self.order_registry:
            return False

        ticker, order = self.order_registry[order_id]
        success = self.manager.cancel_order(ticker, order_id)

        if success:
            del self.order_registry[order_id]
            self._registry_timestamps.pop(order_id, None)

        return success

    def evict_stale_registry(self, ttl_seconds: int = 3600) -> int:
        """
        Remove terminal orders from the registry older than ttl_seconds.

        Terminal orders are those in a FILLED, CANCELED, or REJECTED state
        (i.e., order.is_complete() returns True). Resting orders are never
        evicted regardless of age.

        Returns the number of entries evicted.
        """
        _epoch = datetime.min.replace(tzinfo=timezone.utc)
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=ttl_seconds)
        to_evict = [
            order_id
            for order_id, (_, order) in self.order_registry.items()
            if order.is_complete()
            and self._registry_timestamps.get(order_id, _epoch) < cutoff
        ]
        for order_id in to_evict:
            del self.order_registry[order_id]
            self._registry_timestamps.pop(order_id, None)
        return len(to_evict)

    def get_market_data(self, ticker: str) -> dict:
        """
        Get market data for ticker.

        Args:
            ticker: Ticker symbol

        Returns:
            Dictionary with best bid, best ask, and spread

        Raises:
            ValueError: If ticker is not supported
        """
        book = self.manager.get_order_book(ticker)

        return {
            "ticker": ticker,
            "best_bid": book.get_best_bid(),
            "best_ask": book.get_best_ask(),
            "spread": book.get_spread(),
        }

    def get_supported_tickers(self) -> List[str]:
        """
        Get supported tickers.

        Returns:
            Sorted list of ticker symbols
        """
        return self.manager.get_supported_tickers()
