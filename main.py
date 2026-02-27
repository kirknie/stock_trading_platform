"""
Stock Trading Platform — FastAPI entry point.

Architecture:
  HTTP Request → OrderRequest (Pydantic) → asyncio.Queue → Consumer Worker
  → MatchingEngine → List[Trade] → OrderResponse (Pydantic) → HTTP Response

The asyncio.Queue decouples HTTP ingestion from matching, enabling
future horizontal scaling of the consumer workers.
"""

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI

from trading.api.routes import router
from trading.api.websocket import ws_router
from trading.api.dependencies import init_app_state
from trading.api.broadcaster import init_broadcaster
from trading.api import consumer


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup; clean up on shutdown."""
    engine, queue, risk = init_app_state()
    broadcaster = init_broadcaster()

    # Start background consumer that drains the order queue
    consumer_task = asyncio.create_task(
        consumer.run_consumer(engine, queue, broadcaster, risk), name="order-consumer"
    )

    yield  # Application runs here

    # Graceful shutdown: drain queue then cancel consumer
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Stock Trading Platform",
    version="0.2.0",
    lifespan=lifespan,
)

app.include_router(router)
app.include_router(ws_router)
