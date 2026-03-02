"""
Snapshot writer and loader for the order book state.

A snapshot captures all resting orders in every book at a point in time,
along with the event log sequence number at the moment it was taken.

On startup:
  1. Load the latest snapshot (if any) → restore resting orders into the engine
  2. Replay event log events with seq > snapshot["sequence"] → catch up to shutdown state

If no snapshot exists, replay the entire event log from seq 0.
"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

import aiofiles

from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderStatus, OrderType

logger = structlog.get_logger(__name__)


SNAPSHOT_VERSION = 1


class SnapshotManager:
    """Saves and restores full order book state to/from a JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = (
            path
            if path is not None
            else Path(os.getenv("SNAPSHOT_PATH", "data/snapshot.json"))
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)

    async def save(self, engine: MatchingEngine, sequence: int) -> None:
        """
        Write a snapshot of all resting orders to disk.

        Overwrites the previous snapshot — only the latest is ever needed.
        Called on graceful shutdown and periodically during operation.
        """
        snapshot: dict[str, Any] = {
            "version": SNAPSHOT_VERSION,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
            "sequence": sequence,
            "books": {},
        }

        for ticker in engine.get_supported_tickers():
            book = engine.manager.get_order_book(ticker)
            bids = [_order_to_dict(o) for queue in book.bids.values() for o in queue]
            asks = [_order_to_dict(o) for queue in book.asks.values() for o in queue]
            snapshot["books"][ticker] = {"bids": bids, "asks": asks}

        async with aiofiles.open(self._path, "w") as f:
            await f.write(json.dumps(snapshot, indent=2))

        logger.info("Snapshot saved at sequence %d to %s", sequence, self._path)

    async def load(self) -> dict[str, Any] | None:
        """
        Load the most recent snapshot from disk.

        Returns None if the file does not exist or the version doesn't match.
        """
        if not self._path.exists():
            return None
        async with aiofiles.open(self._path, "r") as f:
            content = await f.read()
        snapshot = json.loads(content)
        if snapshot.get("version") != SNAPSHOT_VERSION:
            logger.warning(
                "Snapshot version mismatch (got %s, expected %s), ignoring",
                snapshot.get("version"),
                SNAPSHOT_VERSION,
            )
            return None
        return snapshot

    def restore(self, engine: MatchingEngine, snapshot: dict[str, Any]) -> None:
        """
        Restore all resting orders from a snapshot into the engine.

        Called synchronously during startup before the consumer starts.
        Uses OrderBook._add_to_book() to place orders without triggering
        matching (snapshot orders were already non-matching at save time).
        """
        for ticker, book_data in snapshot.get("books", {}).items():
            book = engine.manager.get_order_book(ticker)
            for order_dict in book_data.get("bids", []) + book_data.get("asks", []):
                order = _dict_to_order(order_dict)
                book._add_to_book(order)
                engine.order_registry[order.order_id] = (ticker, order)

        logger.info(
            "Snapshot restored: sequence=%d, tickers=%s",
            snapshot.get("sequence", 0),
            list(snapshot.get("books", {}).keys()),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _order_to_dict(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "ticker": order.ticker,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "price": str(order.price) if order.price is not None else None,
        "status": order.status.value,
        "account_id": order.account_id,
        "timestamp": order.timestamp.isoformat(),
    }


def _dict_to_order(d: dict[str, Any]) -> Order:
    return Order(
        order_id=d["order_id"],
        ticker=d["ticker"],
        side=OrderSide(d["side"]),
        order_type=OrderType(d["order_type"]),
        quantity=d["quantity"],
        filled_quantity=d["filled_quantity"],
        price=Decimal(d["price"]) if d["price"] is not None else None,
        status=OrderStatus(d["status"]),
        account_id=d["account_id"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
    )
