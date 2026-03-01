"""
Stock Trading Platform — FastAPI entry point.

Architecture:
  HTTP Request → OrderRequest (Pydantic) → asyncio.Queue → Consumer Worker
  → MatchingEngine → List[Trade] → OrderResponse (Pydantic) → HTTP Response

The asyncio.Queue decouples HTTP ingestion from matching, enabling
future horizontal scaling of the consumer workers.

Startup sequence:
  1. init_app_state()          — create engine, queue, risk, event_log, snapshot_mgr
  2. snapshot_mgr.load()       — load latest snapshot (if any)
  3. snapshot_mgr.restore()    — rebuild resting orders from snapshot
  4. event_log.read_all()      — replay events after snapshot sequence
  5. run_consumer()            — start background order processor
  6. _periodic_snapshot()      — start background snapshot task

Shutdown sequence:
  1. Cancel periodic snapshot and consumer tasks
  2. snapshot_mgr.save()       — write final snapshot
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from fastapi import FastAPI

from trading.api import consumer
from trading.api.broadcaster import init_broadcaster
from trading.api.dependencies import init_app_state
from trading.api.routes import router
from trading.api.websocket import ws_router
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order, OrderSide, OrderStatus, OrderType
from trading.persistence.snapshot import SnapshotManager
from trading.persistence.event_log import EventLog

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup; clean up on shutdown."""
    engine, queue, risk, event_log, snapshot_mgr = init_app_state()
    broadcaster = init_broadcaster()

    # ── Startup: restore snapshot then replay event log ───────────────────────
    snapshot = await snapshot_mgr.load()
    replay_after = 0
    if snapshot:
        snapshot_mgr.restore(engine, snapshot)
        replay_after = snapshot["sequence"]
        logger.info("Restored from snapshot at sequence %d", replay_after)

    async for event in event_log.read_all(after_sequence=replay_after):
        _replay_event(engine, event)
    logger.info("Event log replay complete")

    # ── Background tasks ──────────────────────────────────────────────────────
    consumer_task = asyncio.create_task(
        consumer.run_consumer(engine, queue, broadcaster, risk, event_log),
        name="order-consumer",
    )
    snapshot_task = asyncio.create_task(
        _periodic_snapshot(engine, event_log, snapshot_mgr),
        name="snapshot",
    )

    yield  # Application runs here

    # ── Shutdown: cancel tasks then write final snapshot ──────────────────────
    snapshot_task.cancel()
    consumer_task.cancel()
    for task in [snapshot_task, consumer_task]:
        try:
            await task
        except asyncio.CancelledError:
            pass

    await snapshot_mgr.save(engine, event_log._sequence)
    logger.info("Final snapshot saved on shutdown")


def _replay_event(engine: MatchingEngine, event: dict) -> None:
    """
    Re-apply a single logged event to rebuild in-memory engine state.

    Only order_submitted and order_cancelled are replayed — trade_executed
    events are a consequence of submit_order() and are generated naturally
    during replay. Risk state is NOT updated here; it is rebuilt separately
    during Day 5 recovery (out of scope for this step).
    """
    if event["event"] == "order_submitted":
        od = event["order"]
        order = Order(
            order_id=od["order_id"],
            ticker=od["ticker"],
            side=OrderSide(od["side"]),
            order_type=OrderType(od["order_type"]),
            quantity=od["quantity"],
            price=Decimal(od["price"]) if od["price"] is not None else None,
            status=OrderStatus(od["status"]),
            filled_quantity=od["filled_quantity"],
            timestamp=datetime.fromisoformat(od["timestamp"]),
            account_id=od["account_id"],
        )
        engine.submit_order(order)

    elif event["event"] == "order_cancelled":
        engine.cancel_order(event["order_id"])


async def _periodic_snapshot(
    engine: MatchingEngine,
    event_log: EventLog,
    snapshot_mgr: SnapshotManager,
    interval_seconds: int = 300,
) -> None:
    """Write a snapshot every interval_seconds (default 5 minutes)."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await snapshot_mgr.save(engine, event_log._sequence)
        except asyncio.CancelledError:
            break


app = FastAPI(
    title="Stock Trading Platform",
    version="0.3.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(ws_router)
