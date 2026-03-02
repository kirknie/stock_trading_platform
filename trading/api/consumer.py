"""
Async order consumer with risk checking and broadcaster integration.

Reads (order, future) tuples from the queue and processes them
through the MatchingEngine. The future is used to return the
result back to the waiting HTTP handler.

Why a queue?
- Decouples HTTP ingestion rate from matching throughput
- Enables future batching or per-ticker consumer sharding
- Matching engine remains single-threaded (deterministic)
"""

import asyncio

import structlog

from trading.api.broadcaster import Broadcaster
from trading.engine.matcher import MatchingEngine
from trading.events.models import OrderType
from trading.metrics.collector import queue_depth
from trading.persistence.event_log import EventLog
from trading.risk.checker import RiskChecker, RiskViolation

logger = structlog.get_logger(__name__)


async def run_consumer(
    engine: MatchingEngine,
    queue: asyncio.Queue,
    broadcaster: Broadcaster,
    risk: RiskChecker,
    event_log: EventLog,
) -> None:
    """
    Continuously drain the order queue and process orders.

    Flow per order:
      1. risk.check(order)             -> raises RiskViolation on breach
      2. risk.check_market_spread()    -> raises RiskViolation if spread too wide
      3. event_log.append_order_submitted() -> persist before matching
      4. risk.record_open_order(order) -> register in exposure tracking
      5. engine.submit_order(order)    -> matching engine
      6. event_log.append_trade_executed() -> persist each trade
      7. risk.record_fill(trade, ...)  -> update position state per trade
      8. risk.record_order_complete()  -> remove from open exposure if fully filled
      9. broadcaster.notify_*()        -> push WebSocket events
    """
    logger.info("Order consumer started")
    while True:
        try:
            order, future = await queue.get()
            queue_depth.labels(ticker=order.ticker).set(queue.qsize())
            try:
                risk.check(order)

                if order.order_type == OrderType.MARKET:
                    book = engine.manager.get_order_book(order.ticker)
                    risk.check_market_spread(
                        ticker=order.ticker,
                        best_bid=book.get_best_bid(),
                        best_ask=book.get_best_ask(),
                    )

                risk.record_open_order(order)
                await event_log.append_order_submitted(order)
                trades = engine.submit_order(order)

                # Persist and update risk state for each trade before
                # resolving the future — guarantees the log is written
                # before the HTTP response is returned to the caller.
                for trade in trades:
                    buyer_entry = engine.order_registry.get(trade.buyer_order_id)
                    seller_entry = engine.order_registry.get(trade.seller_order_id)
                    buyer_account = (
                        buyer_entry[1].account_id if buyer_entry else order.account_id
                    )
                    seller_account = (
                        seller_entry[1].account_id if seller_entry else order.account_id
                    )
                    await event_log.append_trade_executed(
                        trade, buyer_account, seller_account
                    )
                    risk.record_fill(trade, buyer_account, seller_account)

                if order.is_complete():
                    risk.record_order_complete(order)

                future.set_result(trades)

                # Notify subscribers: order lifecycle status
                await broadcaster.notify_order_status(
                    order_id=order.order_id,
                    ticker=order.ticker,
                    status=order.status.value,
                    filled_quantity=order.filled_quantity,
                    remaining_quantity=order.remaining_quantity(),
                )

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

            except RiskViolation as exc:
                future.set_exception(exc)
            except Exception as exc:
                future.set_exception(exc)
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info("Order consumer shutting down")
            break
        except Exception as exc:
            logger.error("Unexpected consumer error: %s", exc)
