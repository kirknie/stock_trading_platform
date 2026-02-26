"""
WebSocket broadcaster for real-time market data.

Maintains a registry of connected clients and their subscriptions.
The matching engine's consumer calls notify_* after each match
to push updates to subscribed clients.
"""

import asyncio
import json
import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class Broadcaster:
    """WebSocket broadcaster for market data events."""

    def __init__(self):
        # Map: ticker → set of client queues
        self._subscriptions: dict[str, set[asyncio.Queue]] = {}
        # Map: client queue → set of subscribed tickers (for cleanup)
        self._client_tickers: dict[asyncio.Queue, set[str]] = {}

    def subscribe(self, client_queue: asyncio.Queue, tickers: list[str]) -> None:
        """Subscribe a client to the given tickers."""
        self._client_tickers[client_queue] = set(tickers)
        for ticker in tickers:
            if ticker not in self._subscriptions:
                self._subscriptions[ticker] = set()
            self._subscriptions[ticker].add(client_queue)

    def unsubscribe(self, client_queue: asyncio.Queue) -> None:
        """Remove a client from all subscriptions."""
        tickers = self._client_tickers.pop(client_queue, set())
        for ticker in tickers:
            self._subscriptions.get(ticker, set()).discard(client_queue)

    async def broadcast(self, ticker: str, message: dict[str, Any]) -> None:
        """Send a message to all clients subscribed to the given ticker."""
        queues = self._subscriptions.get(ticker, set())
        payload = json.dumps(message, default=_decimal_default)
        for q in list(queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("Client queue full, dropping message for %s", ticker)

    async def notify_book_update(
        self,
        ticker: str,
        best_bid: Decimal | None,
        best_ask: Decimal | None,
        spread: Decimal | None,
    ) -> None:
        await self.broadcast(
            ticker,
            {
                "type": "book_update",
                "ticker": ticker,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
            },
        )

    async def notify_trade(
        self,
        trade_id: str,
        ticker: str,
        price: Decimal,
        quantity: int,
    ) -> None:
        await self.broadcast(
            ticker,
            {
                "type": "trade",
                "trade_id": trade_id,
                "ticker": ticker,
                "price": price,
                "quantity": quantity,
            },
        )


def _decimal_default(obj: Any) -> str:
    """JSON serializer for Decimal values."""
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


# Module-level singleton
_broadcaster: Broadcaster | None = None


def get_broadcaster() -> Broadcaster:
    global _broadcaster
    if _broadcaster is None:
        raise RuntimeError(
            "Broadcaster not initialized. Call init_broadcaster() first."
        )
    return _broadcaster


def init_broadcaster() -> Broadcaster:
    global _broadcaster
    _broadcaster = Broadcaster()
    return _broadcaster
