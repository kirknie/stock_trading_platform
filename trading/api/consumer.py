"""
Async order consumer with broadcaster integration.

Reads (order, future) tuples from the queue and processes them
through the MatchingEngine. The future is used to return the
result back to the waiting HTTP handler.

Why a queue?
- Decouples HTTP ingestion rate from matching throughput
- Enables future batching or per-ticker consumer sharding
- Matching engine remains single-threaded (deterministic)
"""

import asyncio
import logging

from trading.api.broadcaster import Broadcaster
from trading.engine.matcher import MatchingEngine

logger = logging.getLogger(__name__)


async def run_consumer(
    engine: MatchingEngine,
    queue: asyncio.Queue,
    broadcaster: Broadcaster,
) -> None:
    """
    Continuously drain the order queue and process orders.

    Each item in the queue is a (Order, asyncio.Future) pair.
    The future is resolved with the list of trades generated.
    After each order, notifies the broadcaster of book and trade events.
    """
    logger.info("Order consumer started")
    while True:
        try:
            order, future = await queue.get()
            try:
                trades = engine.submit_order(order)
                future.set_result(trades)

                # Notify subscribers: book update for the ticker
                book = engine.manager.get_order_book(order.ticker)
                await broadcaster.notify_book_update(
                    ticker=order.ticker,
                    best_bid=book.get_best_bid(),
                    best_ask=book.get_best_ask(),
                    spread=book.get_spread(),
                )
                # Notify subscribers: one event per generated trade
                for trade in trades:
                    await broadcaster.notify_trade(
                        trade_id=trade.trade_id,
                        ticker=trade.ticker,
                        price=trade.price,
                        quantity=trade.quantity,
                    )

            except Exception as exc:
                future.set_exception(exc)
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info("Order consumer shutting down")
            break
        except Exception as exc:
            logger.error("Unexpected consumer error: %s", exc)
