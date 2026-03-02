"""
Append-only event log for the trading platform.

Every domain event is written as a JSON line (NDJSON) to a log file.
The log is the authoritative record for snapshot + replay recovery.

Event types:
  order_submitted      — recorded after risk checks pass, before submit_order()
  trade_executed       — recorded once per trade
  order_cancelled      — recorded after a successful cancellation
  idempotency_cached   — recorded when a new order_id is cached; includes expires_at
"""

import json
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiofiles

from trading.events.models import Order, Trade

logger = logging.getLogger(__name__)


class EventLog:
    """
    Async append-only event log backed by a flat file.

    One instance is shared for the lifetime of the application.
    All writes go through the append_* methods, which are called from
    the single consumer coroutine — no locking needed.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = (
            path
            if path is not None
            else Path(os.getenv("EVENT_LOG_PATH", "data/events.log"))
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._sequence: int = 0

    async def append_order_submitted(self, order: Order) -> int:
        """Log an order submission. Returns the sequence number assigned."""
        seq = self._next_seq()
        await self._write(
            {
                "event": "order_submitted",
                "seq": seq,
                "ts": _now(),
                "order": _order_to_dict(order),
            }
        )
        return seq

    async def append_trade_executed(
        self, trade: Trade, buyer_account: str, seller_account: str
    ) -> int:
        """Log a confirmed trade with account IDs. Returns the sequence number assigned."""
        seq = self._next_seq()
        await self._write(
            {
                "event": "trade_executed",
                "seq": seq,
                "ts": _now(),
                "trade": _trade_to_dict(trade),
                "buyer_account": buyer_account,
                "seller_account": seller_account,
            }
        )
        return seq

    async def append_order_cancelled(self, order_id: str, ticker: str) -> int:
        """Log a successful cancellation. Returns the sequence number assigned."""
        seq = self._next_seq()
        await self._write(
            {
                "event": "order_cancelled",
                "seq": seq,
                "ts": _now(),
                "order_id": order_id,
                "ticker": ticker,
            }
        )
        return seq

    async def append_idempotency_cached(
        self, order_id: str, response: dict[str, Any], ttl_hours: int = 24
    ) -> int:
        """
        Log a cached idempotency response. Returns the sequence number assigned.

        The expires_at field allows the restore path to drop entries that have
        already expired, preventing unbounded cache growth across restarts.
        """
        seq = self._next_seq()
        expires_at = (
            datetime.now(tz=timezone.utc) + timedelta(hours=ttl_hours)
        ).isoformat()
        await self._write(
            {
                "event": "idempotency_cached",
                "seq": seq,
                "ts": _now(),
                "order_id": order_id,
                "response": response,
                "expires_at": expires_at,
            }
        )
        return seq

    async def read_all(self, after_sequence: int = 0) -> AsyncIterator[dict[str, Any]]:
        """
        Yield all events with seq > after_sequence in file order.

        Used during recovery to replay events that occurred after a snapshot.
        Yields nothing if the log file does not exist.
        """
        if not self._path.exists():
            return
        async with aiofiles.open(self._path, "r") as f:
            async for line in f:
                line = line.strip()
                if line:
                    event = json.loads(line)
                    if event.get("seq", 0) > after_sequence:
                        yield event

    def _next_seq(self) -> int:
        self._sequence += 1
        return self._sequence

    async def _write(self, event: dict[str, Any]) -> None:
        async with aiofiles.open(self._path, "a") as f:
            await f.write(json.dumps(event, default=_json_default) + "\n")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _json_default(obj: Any) -> str:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not serializable: {type(obj)}")


def _order_to_dict(order: Order) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "ticker": order.ticker,
        "side": order.side.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "price": str(order.price) if order.price is not None else None,
        "timestamp": order.timestamp.isoformat(),
        "status": order.status.value,
        "filled_quantity": order.filled_quantity,
        "account_id": order.account_id,
    }


def _trade_to_dict(trade: Trade) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "ticker": trade.ticker,
        "buyer_order_id": trade.buyer_order_id,
        "seller_order_id": trade.seller_order_id,
        "price": str(trade.price),
        "quantity": trade.quantity,
        "timestamp": trade.timestamp.isoformat(),
    }
