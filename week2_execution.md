# Week 2 Execution Plan: API & Concurrency

## Overview

Build the async API layer on top of the matching engine from Week 1.

**Goal:** By end of week, you have a running web service that can:
- Accept orders via REST (`POST /orders`, `POST /orders/{id}/cancel`, `GET /book/{ticker}`, `GET /tickers`)
- Process orders asynchronously via `asyncio.Queue` (decouple HTTP from matching)
- Broadcast real-time market data via WebSocket with per-ticker subscriptions
- Produce a throughput benchmark documenting orders/sec

---

## Pre-Week Setup (30 minutes)

### Step 1: Install New Dependencies

```bash
uv add fastapi "uvicorn[standard]"
uv add --dev httpx pytest-asyncio
```

- `fastapi` — async web framework
- `uvicorn[standard]` — ASGI server with WebSocket support (includes `websockets`)
- `httpx` — async HTTP client for testing FastAPI endpoints
- `pytest-asyncio` — already installed, needed for async test cases

Verify `pyproject.toml` updated correctly:
```bash
uv sync
```

### Step 2: Extend Project Structure

```bash
mkdir -p trading/api
touch trading/api/__init__.py
touch trading/api/routes.py
touch trading/api/websocket.py
touch trading/api/dependencies.py
touch trading/api/schemas.py
```

Verify:
```bash
ls trading/api/
# __init__.py  dependencies.py  routes.py  schemas.py  websocket.py
```

### Step 3: Configure pytest-asyncio

Add the following to `pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

This makes all `async def test_*` functions run automatically without needing `@pytest.mark.asyncio` on every test.

---

## Day 1: Pydantic Schemas & API Foundation (3-4 hours)

### Goal
Define request/response schemas and wire up a minimal FastAPI app that runs.

---

### Step 1.1: Define Pydantic Schemas (45 min)
**File:** `trading/api/schemas.py`

These schemas are separate from the domain models in `trading/events/models.py`. They define what the API accepts and returns.

```python
"""
Pydantic schemas for REST API request and response bodies.

Keep these separate from domain models to allow independent evolution
of the API contract vs internal data structures.
"""

from decimal import Decimal
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, model_validator

from trading.events.models import OrderSide, OrderType, OrderStatus


# ── Request Schemas ──────────────────────────────────────────────────────────

class OrderRequest(BaseModel):
    """Request body for POST /orders."""
    ticker: str = Field(..., min_length=1, max_length=10, examples=["AAPL"])
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(..., gt=0, examples=[100])
    price: Optional[Decimal] = Field(
        None,
        gt=0,
        decimal_places=2,
        examples=["150.00"],
        description="Required for LIMIT orders. Omit for MARKET orders."
    )
    account_id: str = Field(default="default", min_length=1, max_length=50)

    @model_validator(mode="after")
    def price_required_for_limit(self) -> "OrderRequest":
        if self.order_type == OrderType.LIMIT and self.price is None:
            raise ValueError("price is required for LIMIT orders")
        if self.order_type == OrderType.MARKET and self.price is not None:
            raise ValueError("price must be omitted for MARKET orders")
        return self


# ── Response Schemas ─────────────────────────────────────────────────────────

class TradeResponse(BaseModel):
    """Single trade that occurred when an order matched."""
    trade_id: str
    ticker: str
    buyer_order_id: str
    seller_order_id: str
    price: Decimal
    quantity: int
    timestamp: datetime


class OrderResponse(BaseModel):
    """Response after submitting an order."""
    order_id: str
    ticker: str
    side: OrderSide
    order_type: OrderType
    quantity: int
    price: Optional[Decimal]
    status: OrderStatus
    filled_quantity: int
    trades: list[TradeResponse]


class CancelResponse(BaseModel):
    """Response after canceling an order."""
    order_id: str
    success: bool
    message: str


class OrderBookLevel(BaseModel):
    """One price level in the order book."""
    price: Decimal
    quantity: int


class OrderBookResponse(BaseModel):
    """Full order book snapshot for a ticker."""
    ticker: str
    bids: list[OrderBookLevel]   # sorted descending (best bid first)
    asks: list[OrderBookLevel]   # sorted ascending (best ask first)
    best_bid: Optional[Decimal]
    best_ask: Optional[Decimal]
    spread: Optional[Decimal]


class TickersResponse(BaseModel):
    """List of supported tickers."""
    tickers: list[str]
```

**Validation:**
```bash
python -c "from trading.api.schemas import OrderRequest, OrderResponse; print('OK')"
```

---

### Step 1.2: Create App Dependencies (30 min)
**File:** `trading/api/dependencies.py`

FastAPI uses dependency injection. This module creates and exposes the shared engine and queue instances.

```python
"""
Shared application state and dependency injection helpers.

The MatchingEngine and asyncio.Queue are created once at startup
and injected into route handlers via FastAPI's Depends() mechanism.
"""

import asyncio
from trading.engine.matcher import MatchingEngine

# Supported tickers — fixed set for this project
SUPPORTED_TICKERS = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]

# Module-level singletons initialized at startup
_engine: MatchingEngine | None = None
_order_queue: asyncio.Queue | None = None


def get_engine() -> MatchingEngine:
    """Return the shared MatchingEngine instance."""
    if _engine is None:
        raise RuntimeError("Engine not initialized. Call init_app_state() first.")
    return _engine


def get_order_queue() -> asyncio.Queue:
    """Return the shared async order queue."""
    if _order_queue is None:
        raise RuntimeError("Queue not initialized. Call init_app_state() first.")
    return _order_queue


def init_app_state() -> tuple[MatchingEngine, asyncio.Queue]:
    """
    Initialize shared application state.

    Called once during FastAPI lifespan startup.
    Returns (engine, queue) for use in the consumer task.
    """
    global _engine, _order_queue
    _engine = MatchingEngine(SUPPORTED_TICKERS)
    _order_queue = asyncio.Queue()
    return _engine, _order_queue
```

---

### Step 1.3: Wire Up a Minimal FastAPI App (45 min)
**File:** `main.py`

Replace the placeholder `main.py` with a minimal FastAPI application. We do **not** import `trading.api.consumer` or `trading.api.routes` yet — those files don't exist until Day 2 and would cause an `ImportError`. The full lifespan with the consumer will be wired in Step 2.0.

```python
"""
Stock Trading Platform — FastAPI entry point.

Architecture:
  HTTP Request → OrderRequest (Pydantic) → asyncio.Queue → Consumer Worker
  → MatchingEngine → List[Trade] → OrderResponse (Pydantic) → HTTP Response

The asyncio.Queue decouples HTTP ingestion from matching, enabling
future horizontal scaling of the consumer workers.
"""

from fastapi import FastAPI

app = FastAPI(
    title="Stock Trading Platform",
    version="0.2.0",
)
```

**Validation:**
```bash
python -c "from main import app; print('App OK')"
```

---

### Day 1 Checklist
- [ ] `trading/api/schemas.py` created with all request/response schemas
- [ ] `trading/api/dependencies.py` created with engine + queue singletons
- [ ] `main.py` created as minimal FastAPI app (no consumer yet)
- [ ] All imports resolve without errors
- [ ] No failing tests (run `pytest -v`)

---

## Day 2: REST Routes & Async Consumer (4-5 hours)

### Goal
Implement all REST endpoints and the background consumer that processes orders from the queue.

---

### Step 2.0: Complete main.py with Lifespan (15 min)
**File:** `main.py`

Now that the consumer will exist, update `main.py` to add the full lifespan, consumer task, and router registration.

```python
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
from trading.api import consumer


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize resources on startup; clean up on shutdown."""
    engine, queue = init_app_state()

    # Start background consumer that drains the order queue
    consumer_task = asyncio.create_task(
        consumer.run_consumer(engine, queue),
        name="order-consumer"
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
```

> **Note:** Do not validate `main.py` yet. It imports `trading.api.consumer` and
> `trading.api.routes` which are empty scaffold files until Steps 2.1 and 2.2 are done.
> Validation is at the end of Step 2.2.

---

### Step 2.1: Implement the Async Consumer (45 min)
**File:** `trading/api/consumer.py`

The consumer is a long-running coroutine that reads from the queue and calls the matching engine. It holds the result future so the HTTP handler can await the trade result.

```python
"""
Async order consumer.

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

from trading.engine.matcher import MatchingEngine
from trading.events.models import Order

logger = logging.getLogger(__name__)


async def run_consumer(engine: MatchingEngine, queue: asyncio.Queue) -> None:
    """
    Continuously drain the order queue and process orders.

    Each item in the queue is a (Order, asyncio.Future) pair.
    The future is resolved with the list of trades generated.
    """
    logger.info("Order consumer started")
    while True:
        try:
            order, future = await queue.get()
            try:
                trades = engine.submit_order(order)
                future.set_result(trades)
            except Exception as exc:
                future.set_exception(exc)
            finally:
                queue.task_done()
        except asyncio.CancelledError:
            logger.info("Order consumer shutting down")
            break
        except Exception as exc:
            logger.error("Unexpected consumer error: %s", exc)
```

---

### Step 2.2: Implement REST Routes (2-3 hours)
**File:** `trading/api/routes.py`

```python
"""
REST API routes for the trading platform.

Endpoints:
  POST   /orders              — Submit a new order
  POST   /orders/{id}/cancel  — Cancel an open order
  GET    /book/{ticker}       — Get order book snapshot
  GET    /tickers             — List supported tickers
"""

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status

from trading.api.dependencies import get_engine, get_order_queue
from trading.api.schemas import (
    CancelResponse,
    OrderBookLevel,
    OrderBookResponse,
    OrderRequest,
    OrderResponse,
    TickersResponse,
    TradeResponse,
)
from trading.engine.matcher import MatchingEngine
from trading.events.models import Order

router = APIRouter()


def _build_order_book_response(engine: MatchingEngine, ticker: str) -> OrderBookResponse:
    """Build a full OrderBookResponse from the live order book."""
    book = engine.manager.get_order_book(ticker)

    # Bids: sort descending (highest price first)
    bids = sorted(
        [
            OrderBookLevel(
                price=price,
                quantity=sum(o.remaining_quantity() for o in queue),
            )
            for price, queue in book.bids.items()
            if queue
        ],
        key=lambda x: x.price,
        reverse=True,
    )

    # Asks: sort ascending (lowest price first)
    asks = sorted(
        [
            OrderBookLevel(
                price=price,
                quantity=sum(o.remaining_quantity() for o in queue),
            )
            for price, queue in book.asks.items()
            if queue
        ],
        key=lambda x: x.price,
    )

    return OrderBookResponse(
        ticker=ticker,
        bids=bids,
        asks=asks,
        best_bid=book.get_best_bid(),
        best_ask=book.get_best_ask(),
        spread=book.get_spread(),
    )


@router.post(
    "/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a new order",
)
async def submit_order(
    request: OrderRequest,
    engine: MatchingEngine = Depends(get_engine),
    queue: asyncio.Queue = Depends(get_order_queue),
) -> OrderResponse:
    """
    Submit a limit or market order.

    The order is placed on the async queue and processed by the
    background consumer. The response includes any trades generated.
    """
    if request.ticker not in engine.get_supported_tickers():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Ticker '{request.ticker}' is not supported. "
                   f"Supported: {engine.get_supported_tickers()}",
        )

    order = Order(
        order_id=str(uuid.uuid4()),
        ticker=request.ticker,
        side=request.side,
        order_type=request.order_type,
        quantity=request.quantity,
        price=request.price,
        timestamp=datetime.now(tz=timezone.utc),
        account_id=request.account_id,
    )

    # Enqueue order and await result from consumer
    loop = asyncio.get_event_loop()
    future: asyncio.Future = loop.create_future()
    await queue.put((order, future))
    trades = await future

    return OrderResponse(
        order_id=order.order_id,
        ticker=order.ticker,
        side=order.side,
        order_type=order.order_type,
        quantity=order.quantity,
        price=order.price,
        status=order.status,
        filled_quantity=order.filled_quantity,
        trades=[
            TradeResponse(
                trade_id=t.trade_id,
                ticker=t.ticker,
                buyer_order_id=t.buyer_order_id,
                seller_order_id=t.seller_order_id,
                price=t.price,
                quantity=t.quantity,
                timestamp=t.timestamp,
            )
            for t in trades
        ],
    )


@router.post(
    "/orders/{order_id}/cancel",
    response_model=CancelResponse,
    summary="Cancel an open order",
)
async def cancel_order(
    order_id: str,
    engine: MatchingEngine = Depends(get_engine),
) -> CancelResponse:
    """
    Cancel an open order by its ID.

    Cancellation is synchronous (no queue) because it does not
    generate trades and must not be reordered with submissions.
    """
    success = engine.cancel_order(order_id)
    return CancelResponse(
        order_id=order_id,
        success=success,
        message="Order canceled" if success else "Order not found or already completed",
    )


@router.get(
    "/book/{ticker}",
    response_model=OrderBookResponse,
    summary="Get order book snapshot",
)
async def get_order_book(
    ticker: str,
    engine: MatchingEngine = Depends(get_engine),
) -> OrderBookResponse:
    """
    Return a snapshot of the current order book for the given ticker.

    Includes all visible bids and asks aggregated by price level.
    """
    if ticker not in engine.get_supported_tickers():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ticker '{ticker}' not found. "
                   f"Supported: {engine.get_supported_tickers()}",
        )
    return _build_order_book_response(engine, ticker)


@router.get(
    "/tickers",
    response_model=TickersResponse,
    summary="List supported tickers",
)
async def list_tickers(
    engine: MatchingEngine = Depends(get_engine),
) -> TickersResponse:
    """Return the list of all supported tickers."""
    return TickersResponse(tickers=engine.get_supported_tickers())
```

---

### Step 2.3: Validate main.py (5 min)

`websocket.py` is still a stub (full implementation is Day 4). Add a minimal stub so
`main.py` can import `ws_router` without error:

**File:** `trading/api/websocket.py` (stub — will be replaced in Day 4)
```python
"""
WebSocket endpoint for real-time market data.

Full implementation added in Day 4.
"""

from fastapi import APIRouter

ws_router = APIRouter()
```

Now validate the full app imports cleanly:

```bash
python -c "from main import app; print('App OK')"
```

---

### Step 2.4: Smoke Test the Server Manually (15 min)

Run the server:
```bash
uvicorn main:app --reload --port 8000
```

In a second terminal:
```bash
# List tickers
curl -s http://localhost:8000/tickers | python -m json.tool

# Submit a sell limit order
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"ticker": "AAPL", "side": "SELL", "order_type": "LIMIT", "quantity": 100, "price": "150.00"}' \
  | python -m json.tool

# Check the order book (note the order_id from above)
curl -s http://localhost:8000/book/AAPL | python -m json.tool

# Submit a matching buy order
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"ticker": "AAPL", "side": "BUY", "order_type": "LIMIT", "quantity": 100, "price": "150.00"}' \
  | python -m json.tool
# Expect: trades array with 1 trade at 150.00

# Submit an invalid order (expect 422)
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"ticker": "AAPL", "side": "BUY", "order_type": "LIMIT", "quantity": 100}' \
  | python -m json.tool
```

**Success criteria:**
- Server starts without errors
- `/tickers` returns 5 tickers
- Orders submit and match correctly
- Invalid requests return 422

---

### Day 2 Checklist
- [ ] `trading/api/consumer.py` created (Step 2.1)
- [ ] `trading/api/routes.py` created with all 4 endpoints (Step 2.2)
- [ ] `main.py` validates cleanly: `python -c "from main import app; print('App OK')"` (Step 2.3)
- [ ] Server starts with `uvicorn main:app --reload` (Step 2.4)
- [ ] Manual curl tests pass (Step 2.4)
- [ ] All existing tests still pass (`pytest -v`)

---

## Day 3: REST API Tests (3-4 hours)

### Goal
Write async tests for every REST endpoint using httpx's async test client.

---

### Step 3.1: Write REST API Tests (3-4 hours)
**File:** `trading/tests/test_api.py`

**Dependency:** `asgi-lifespan` is required to trigger the FastAPI lifespan
(which starts the consumer task) during tests. Without it, `POST /orders`
hangs forever because no consumer is draining the queue.

```bash
uv add --dev asgi-lifespan
```

The `client` fixture uses `LifespanManager` to start/stop the full app
lifespan (including the consumer task) for every test. This gives each
test a fresh, isolated engine with no shared state — no `conftest.py`
needed.

```python
"""
Tests for REST API endpoints.

Uses httpx.AsyncClient with FastAPI's ASGI transport to test
the full request/response cycle including the async consumer.
"""

import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.fixture
async def client():
    """
    Async test client with full ASGI lifespan.

    LifespanManager triggers main.py's startup (init_app_state +
    consumer task) and shutdown (consumer task cancelled) for every
    test. Without this the queue has no consumer and POST /orders
    hangs forever awaiting the future.
    """
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as ac:
            yield ac


# ── /tickers ──────────────────────────────────────────────────────────────────

async def test_list_tickers(client):
    response = await client.get("/tickers")
    assert response.status_code == 200
    data = response.json()
    assert "tickers" in data
    assert set(data["tickers"]) == {"AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"}


# ── /book/{ticker} ────────────────────────────────────────────────────────────

async def test_get_empty_order_book(client):
    response = await client.get("/book/AAPL")
    assert response.status_code == 200
    data = response.json()
    assert data["ticker"] == "AAPL"
    assert data["bids"] == []
    assert data["asks"] == []
    assert data["best_bid"] is None
    assert data["best_ask"] is None
    assert data["spread"] is None


async def test_get_order_book_unknown_ticker(client):
    response = await client.get("/book/FAKE")
    assert response.status_code == 404


async def test_order_book_shows_resting_orders(client):
    # Submit a sell limit order
    await client.post("/orders", json={
        "ticker": "MSFT",
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": 50,
        "price": "300.00",
    })

    response = await client.get("/book/MSFT")
    assert response.status_code == 200
    data = response.json()
    assert len(data["asks"]) == 1
    assert data["asks"][0]["price"] == "300.00"
    assert data["asks"][0]["quantity"] == 50
    assert data["best_ask"] == "300.00"


# ── POST /orders ──────────────────────────────────────────────────────────────

async def test_submit_limit_buy_no_match(client):
    response = await client.post("/orders", json={
        "ticker": "GOOGL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "2500.00",
    })
    assert response.status_code == 201
    data = response.json()
    assert data["ticker"] == "GOOGL"
    assert data["status"] == "NEW"
    assert data["filled_quantity"] == 0
    assert data["trades"] == []


async def test_submit_limit_sell_no_match(client):
    response = await client.post("/orders", json={
        "ticker": "TSLA",
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": 20,
        "price": "250.00",
    })
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "NEW"
    assert data["trades"] == []


async def test_submit_orders_that_match(client):
    # Sell side
    sell_resp = await client.post("/orders", json={
        "ticker": "NVDA",
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": 100,
        "price": "500.00",
    })
    assert sell_resp.status_code == 201

    # Buy side — should match immediately
    buy_resp = await client.post("/orders", json={
        "ticker": "NVDA",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 100,
        "price": "500.00",
    })
    assert buy_resp.status_code == 201
    data = buy_resp.json()
    assert data["status"] == "FILLED"
    assert data["filled_quantity"] == 100
    assert len(data["trades"]) == 1
    assert data["trades"][0]["price"] == "500.00"
    assert data["trades"][0]["quantity"] == 100


async def test_submit_market_order_with_liquidity(client):
    # Add sell-side liquidity
    await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": 75,
        "price": "155.00",
    })

    # Market buy should consume it
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 75,
    })
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "FILLED"
    assert len(data["trades"]) == 1
    assert data["trades"][0]["price"] == "155.00"


async def test_submit_market_order_no_liquidity(client):
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 999,
    })
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "REJECTED"
    assert data["trades"] == []


async def test_submit_partial_fill(client):
    # Large sell
    await client.post("/orders", json={
        "ticker": "MSFT",
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": 100,
        "price": "310.00",
    })

    # Small buy — partial fill
    response = await client.post("/orders", json={
        "ticker": "MSFT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 40,
        "price": "310.00",
    })
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "FILLED"
    assert data["filled_quantity"] == 40
    assert len(data["trades"]) == 1

    # Remaining 60 should still be in book
    book = await client.get("/book/MSFT")
    asks = book.json()["asks"]
    # Find the price level at 310.00
    ask_at_310 = next((a for a in asks if a["price"] == "310.00"), None)
    assert ask_at_310 is not None
    assert ask_at_310["quantity"] == 60


async def test_submit_order_invalid_ticker(client):
    response = await client.post("/orders", json={
        "ticker": "INVALID",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "100.00",
    })
    assert response.status_code == 422


async def test_submit_limit_order_missing_price(client):
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        # price intentionally omitted
    })
    assert response.status_code == 422


async def test_submit_market_order_with_price_rejected(client):
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 10,
        "price": "100.00",  # should not be present for MARKET
    })
    assert response.status_code == 422


async def test_submit_zero_quantity_rejected(client):
    response = await client.post("/orders", json={
        "ticker": "AAPL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 0,
        "price": "150.00",
    })
    assert response.status_code == 422


# ── POST /orders/{id}/cancel ──────────────────────────────────────────────────

async def test_cancel_open_order(client):
    # Submit an order that won't match
    submit = await client.post("/orders", json={
        "ticker": "GOOGL",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 5,
        "price": "1.00",  # far from market
    })
    order_id = submit.json()["order_id"]

    # Cancel it
    cancel = await client.post(f"/orders/{order_id}/cancel")
    assert cancel.status_code == 200
    data = cancel.json()
    assert data["success"] is True
    assert data["order_id"] == order_id

    # Book should be empty at that price
    book = await client.get("/book/GOOGL")
    bids = book.json()["bids"]
    assert not any(b["price"] == "1.00" for b in bids)


async def test_cancel_nonexistent_order(client):
    cancel = await client.post("/orders/does-not-exist/cancel")
    assert cancel.status_code == 200
    data = cancel.json()
    assert data["success"] is False


async def test_cancel_already_filled_order(client):
    # Create matching orders
    await client.post("/orders", json={
        "ticker": "TSLA",
        "side": "SELL",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "200.00",
    })
    buy = await client.post("/orders", json={
        "ticker": "TSLA",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 10,
        "price": "200.00",
    })
    order_id = buy.json()["order_id"]

    # Try to cancel a filled order
    cancel = await client.post(f"/orders/{order_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["success"] is False
```

**Run:**
```bash
pytest trading/tests/test_api.py -v
```

**Success criteria:** All 17 tests pass, 0 warnings.

---

### Step 3.2: Test Isolation
Test isolation is handled automatically by `LifespanManager`. Each test's
`client` fixture starts a fresh lifespan (calling `init_app_state()` which
creates a new `MatchingEngine` and `asyncio.Queue`), then shuts it down
after the test. No `conftest.py` is needed.

---

### Day 3 Checklist
- [ ] `asgi-lifespan` installed (`uv add --dev asgi-lifespan`)
- [ ] `trading/tests/test_api.py` created with 17 tests covering all 4 endpoints
- [ ] `client` fixture uses `LifespanManager` — consumer runs during every test
- [ ] All 17 API tests pass with 0 warnings
- [ ] All 99 tests pass (`pytest -v`)

---

## Day 4: WebSocket Market Data (3-4 hours)

### Goal
Broadcast real-time order book and trade updates to connected WebSocket clients with per-ticker subscription filtering.

---

### Step 4.1: Design the WebSocket Protocol

Clients connect to `ws://host/ws` and send a subscription message:

```json
// Client → Server: Subscribe
{"action": "subscribe", "tickers": ["AAPL", "MSFT"]}

// Server → Client: Top-of-book update (after any order activity)
{"type": "book_update", "ticker": "AAPL", "best_bid": "149.50", "best_ask": "150.00", "spread": "0.50"}

// Server → Client: Trade event
{"type": "trade", "ticker": "AAPL", "price": "150.00", "quantity": 100, "trade_id": "T0"}
```

Clients only receive events for their subscribed tickers.

---

### Step 4.2: Implement the Broadcaster (30 min)
**File:** `trading/api/broadcaster.py`

```python
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
    """Thread-safe WebSocket broadcaster for market data events."""

    def __init__(self):
        # Map: ticker → set of (websocket, asyncio.Queue) pairs
        self._subscriptions: dict[str, set[asyncio.Queue]] = {}
        # Map: websocket queue → set of subscribed tickers (for cleanup)
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
        await self.broadcast(ticker, {
            "type": "book_update",
            "ticker": ticker,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
        })

    async def notify_trade(
        self,
        trade_id: str,
        ticker: str,
        price: Decimal,
        quantity: int,
    ) -> None:
        await self.broadcast(ticker, {
            "type": "trade",
            "trade_id": trade_id,
            "ticker": ticker,
            "price": price,
            "quantity": quantity,
        })


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
        _broadcaster = Broadcaster()
    return _broadcaster


def init_broadcaster() -> Broadcaster:
    global _broadcaster
    _broadcaster = Broadcaster()
    return _broadcaster
```

---

### Step 4.3: Integrate Broadcaster Into Consumer and main.py (30 min)

**File:** `trading/api/consumer.py` — add `broadcaster` parameter and call `notify_*` after each match:

```python
"""
Async order consumer with broadcaster integration.
"""

import asyncio
import logging

from trading.engine.matcher import MatchingEngine
from trading.api.broadcaster import Broadcaster

logger = logging.getLogger(__name__)


async def run_consumer(
    engine: MatchingEngine,
    queue: asyncio.Queue,
    broadcaster: Broadcaster,
) -> None:
    """
    Drain the order queue and process orders through the matching engine.
    Notifies the broadcaster of book updates and trade events.
    """
    logger.info("Order consumer started")
    while True:
        try:
            order, future = await queue.get()
            try:
                trades = engine.submit_order(order)
                future.set_result(trades)

                # Notify after matching: book update + individual trades
                book = engine.manager.get_order_book(order.ticker)
                await broadcaster.notify_book_update(
                    ticker=order.ticker,
                    best_bid=book.get_best_bid(),
                    best_ask=book.get_best_ask(),
                    spread=book.get_spread(),
                )
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
```

**File:** `main.py` — call `init_broadcaster()` at startup and pass it to the consumer:

```python
from trading.api.routes import router
from trading.api.websocket import ws_router
from trading.api.dependencies import init_app_state
from trading.api.broadcaster import init_broadcaster
from trading.api import consumer


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine, queue = init_app_state()
    broadcaster = init_broadcaster()

    consumer_task = asyncio.create_task(
        consumer.run_consumer(engine, queue, broadcaster),
        name="order-consumer"
    )

    yield

    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass
```

**Validation:**
```bash
python -c "from main import app; print('OK')" && pytest -q
```

---

### Step 4.4: Implement the WebSocket Endpoint (1 hour)
**File:** `trading/api/websocket.py`

```python
"""
WebSocket endpoint for real-time market data.

Protocol:
  1. Client connects to /ws
  2. Client sends: {"action": "subscribe", "tickers": ["AAPL", ...]}
  3. Server streams: book_update and trade events for subscribed tickers
  4. Client disconnects: server cleans up subscription
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState

from trading.api.dependencies import get_broadcaster

logger = logging.getLogger(__name__)
ws_router = APIRouter()


@ws_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    broadcaster = get_broadcaster()
    client_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    subscribed = False

    try:
        # Wait for subscription message
        raw = await websocket.receive_text()
        msg = json.loads(raw)

        if msg.get("action") != "subscribe" or "tickers" not in msg:
            await websocket.send_text(json.dumps({
                "error": "First message must be: {\"action\": \"subscribe\", \"tickers\": [...]}"
            }))
            return

        tickers = msg["tickers"]
        broadcaster.subscribe(client_queue, tickers)
        subscribed = True
        logger.info("Client subscribed to: %s", tickers)

        await websocket.send_text(json.dumps({
            "type": "subscribed",
            "tickers": tickers,
        }))

        # Stream messages from broadcaster queue to client
        while True:
            # Use a short timeout to allow checking WebSocket state
            try:
                payload = await asyncio.wait_for(client_queue.get(), timeout=1.0)
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_text(payload)
            except asyncio.TimeoutError:
                # Heartbeat to detect disconnects
                if websocket.client_state != WebSocketState.CONNECTED:
                    break

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as exc:
        logger.error("WebSocket error: %s", exc)
    finally:
        if subscribed:
            broadcaster.unsubscribe(client_queue)
```

---

### Step 4.5: Write WebSocket Tests (1 hour)
**File:** `trading/tests/test_websocket.py`

```python
"""
Tests for WebSocket market data streaming.
"""

import asyncio
import json
import pytest
from httpx import AsyncClient, ASGITransport
from httpx_ws import aconnect_ws
from main import app


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


async def test_websocket_subscribe_and_receive_book_update(client):
    """Submitting an order triggers a book_update event for subscribed clients."""
    async with aconnect_ws("ws://test/ws", client) as ws:
        # Subscribe to AAPL
        await ws.send_text(json.dumps({"action": "subscribe", "tickers": ["AAPL"]}))
        ack = json.loads(await ws.receive_text())
        assert ack["type"] == "subscribed"
        assert "AAPL" in ack["tickers"]

        # Submit an order — should trigger book_update
        await client.post("/orders", json={
            "ticker": "AAPL",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 50,
            "price": "150.00",
        })

        # Should receive a book_update
        msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=2.0))
        assert msg["type"] == "book_update"
        assert msg["ticker"] == "AAPL"
        assert msg["best_ask"] == "150.00"


async def test_websocket_trade_event(client):
    """Matching orders generates a trade event for subscribed clients."""
    async with aconnect_ws("ws://test/ws", client) as ws:
        await ws.send_text(json.dumps({"action": "subscribe", "tickers": ["MSFT"]}))
        await ws.receive_text()  # ack

        # Create matching pair
        await client.post("/orders", json={
            "ticker": "MSFT",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 10,
            "price": "300.00",
        })
        await client.post("/orders", json={
            "ticker": "MSFT",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "price": "300.00",
        })

        # Collect messages until we see a trade
        trade_msg = None
        for _ in range(5):
            msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=2.0))
            if msg["type"] == "trade":
                trade_msg = msg
                break

        assert trade_msg is not None
        assert trade_msg["ticker"] == "MSFT"
        assert trade_msg["price"] == "300.00"
        assert trade_msg["quantity"] == 10


async def test_websocket_ticker_filtering(client):
    """Clients only receive updates for their subscribed tickers."""
    async with aconnect_ws("ws://test/ws", client) as ws:
        # Subscribe to GOOGL only
        await ws.send_text(json.dumps({"action": "subscribe", "tickers": ["GOOGL"]}))
        await ws.receive_text()  # ack

        # Submit order to TSLA (not subscribed)
        await client.post("/orders", json={
            "ticker": "TSLA",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 5,
            "price": "200.00",
        })

        # Submit order to GOOGL (subscribed)
        await client.post("/orders", json={
            "ticker": "GOOGL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 5,
            "price": "2500.00",
        })

        # Should receive update for GOOGL, not TSLA
        msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=2.0))
        assert msg["ticker"] == "GOOGL"


async def test_websocket_invalid_subscription(client):
    """Invalid subscription message returns an error."""
    async with aconnect_ws("ws://test/ws", client) as ws:
        await ws.send_text(json.dumps({"action": "invalid"}))
        msg = json.loads(await ws.receive_text())
        assert "error" in msg
```

**Note:** WebSocket tests require `httpx-ws`:
```bash
uv add --dev httpx-ws
```

**Run:**
```bash
pytest trading/tests/test_websocket.py -v
```

---

### Day 4 Checklist
- [ ] `trading/api/broadcaster.py` created (Step 4.2)
- [ ] `trading/api/consumer.py` updated with broadcaster integration; `main.py` updated to init broadcaster (Step 4.3)
- [ ] `trading/api/websocket.py` implemented (Step 4.4)
- [ ] WebSocket tests pass (Step 4.5)
- [ ] All tests still pass (`pytest -v`)

---

## Day 5: Throughput Benchmark & Documentation (3-4 hours)

### Goal
Measure orders/sec throughput, document results, and write the week summary.

---

### Step 5.1: Write the Benchmark Script (1.5 hours)
**File:** `benchmarks/throughput.py`

```bash
mkdir -p benchmarks
touch benchmarks/__init__.py
```

```python
#!/usr/bin/env python3
"""
Throughput benchmark for the trading platform REST API.

Measures:
1. Single-ticker orders/sec (AAPL only)
2. Multi-ticker orders/sec (all 5 tickers, interleaved)
3. Latency distribution (p50, p95, p99)

Run:
    # Start server first:
    uvicorn main:app --host 0.0.0.0 --port 8000

    # Then run benchmark:
    PYTHONPATH=. python benchmarks/throughput.py
"""

import asyncio
import statistics
import time
import uuid
from decimal import Decimal

import httpx

BASE_URL = "http://localhost:8000"
TICKERS = ["AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"]
WARMUP_ORDERS = 50
BENCHMARK_ORDERS = 500
CONCURRENCY = 10  # concurrent requests


def make_limit_order(ticker: str, price: str, side: str = "BUY") -> dict:
    return {
        "ticker": ticker,
        "side": side,
        "order_type": "LIMIT",
        "quantity": 100,
        "price": price,
        "account_id": str(uuid.uuid4()),
    }


async def send_order(client: httpx.AsyncClient, payload: dict) -> float:
    """Send one order and return latency in milliseconds."""
    start = time.perf_counter()
    resp = await client.post("/orders", json=payload)
    elapsed_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    return elapsed_ms


async def run_benchmark(
    name: str,
    payloads: list[dict],
    concurrency: int = CONCURRENCY,
) -> dict:
    """Run a batch of orders concurrently and collect stats."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # Warmup
        warmup = payloads[:WARMUP_ORDERS]
        warmup_tasks = [send_order(client, p) for p in warmup]
        await asyncio.gather(*warmup_tasks)

        # Benchmark
        bench = payloads[WARMUP_ORDERS:]
        semaphore = asyncio.Semaphore(concurrency)
        latencies = []

        async def bounded_send(payload):
            async with semaphore:
                return await send_order(client, payload)

        start_wall = time.perf_counter()
        tasks = [bounded_send(p) for p in bench]
        latencies = await asyncio.gather(*tasks)
        wall_time = time.perf_counter() - start_wall

    n = len(latencies)
    sorted_lat = sorted(latencies)

    return {
        "name": name,
        "total_orders": n,
        "wall_time_sec": round(wall_time, 3),
        "orders_per_sec": round(n / wall_time, 1),
        "latency_p50_ms": round(statistics.median(sorted_lat), 2),
        "latency_p95_ms": round(sorted_lat[int(n * 0.95)], 2),
        "latency_p99_ms": round(sorted_lat[int(n * 0.99)], 2),
        "latency_min_ms": round(sorted_lat[0], 2),
        "latency_max_ms": round(sorted_lat[-1], 2),
    }


async def main():
    total = WARMUP_ORDERS + BENCHMARK_ORDERS

    # Benchmark 1: Single ticker (AAPL)
    single_payloads = [
        make_limit_order("AAPL", str(Decimal("150.00") + i), side=("BUY" if i % 2 == 0 else "SELL"))
        for i in range(total)
    ]
    result1 = await run_benchmark("Single-ticker (AAPL)", single_payloads)

    # Benchmark 2: Multi-ticker (all 5, interleaved)
    prices = {"AAPL": "150.00", "MSFT": "300.00", "GOOGL": "2500.00", "TSLA": "200.00", "NVDA": "500.00"}
    multi_payloads = [
        make_limit_order(
            TICKERS[i % 5],
            prices[TICKERS[i % 5]],
            side=("BUY" if i % 2 == 0 else "SELL"),
        )
        for i in range(total)
    ]
    result2 = await run_benchmark("Multi-ticker (5 tickers)", multi_payloads)

    # Print results
    print("\n" + "=" * 60)
    print("THROUGHPUT BENCHMARK RESULTS")
    print("=" * 60)
    for result in [result1, result2]:
        print(f"\n{result['name']}")
        print(f"  Orders processed : {result['total_orders']}")
        print(f"  Wall time        : {result['wall_time_sec']}s")
        print(f"  Throughput       : {result['orders_per_sec']} orders/sec")
        print(f"  Latency p50      : {result['latency_p50_ms']} ms")
        print(f"  Latency p95      : {result['latency_p95_ms']} ms")
        print(f"  Latency p99      : {result['latency_p99_ms']} ms")
        print(f"  Min / Max        : {result['latency_min_ms']} / {result['latency_max_ms']} ms")
    print()


if __name__ == "__main__":
    asyncio.run(main())
```

**Run:**
```bash
# Terminal 1: start server
uvicorn main:app --host 0.0.0.0 --port 8000

# Terminal 2: run benchmark
PYTHONPATH=. python benchmarks/throughput.py
```

Record the output. You will paste results into the week summary.

---

### Step 5.2: Run the Full Test Suite (15 min)

```bash
# Full suite
pytest -v --tb=short

# With stats
pytest trading/tests/test_properties.py -v --hypothesis-show-statistics

# Check test count
pytest --collect-only
```

Expected: all previous 82 tests + new API/WebSocket tests passing.

---

### Step 5.3: Code Quality Check (15 min)

```bash
# Format
black trading/ benchmarks/ main.py

# Lint
ruff check trading/ benchmarks/ main.py

# Type check
mypy trading/ --ignore-missing-imports
```

Fix any errors before committing.

---

### Step 5.4: Create Week 2 Summary Document (1 hour)
**File:** `week2_summary.md`

Use the structure from `week1_summary.md`. Include:

1. **What Was Built** — API layer, consumer, WebSocket broadcaster
2. **Architecture** — request flow diagram (text)
3. **Endpoints** — table of all REST routes
4. **WebSocket Protocol** — subscribe/event message format
5. **Design Decisions** — why asyncio.Queue, why broadcaster pattern, sync vs async cancellation
6. **Throughput Results** — paste benchmark output
7. **Known Limitations** — no auth, no rate limiting, no reconnect on WebSocket
8. **Validation Checklist** — all tests passing, server runs, benchmark completed
9. **Resume Bullet** — draft updated bullet for Week 2 additions

---

### Step 5.5: Commit All Work

```bash
# Stage all new files
git add trading/api/ trading/tests/test_api.py trading/tests/test_websocket.py
git add trading/tests/conftest.py benchmarks/ main.py week2_summary.md week2_execution.md

# Verify no large/sensitive files accidentally included
git status

# Commit incrementally:
git commit -m "Add Pydantic schemas and FastAPI app skeleton"
git commit -m "Add async consumer and REST routes"
git commit -m "Add REST API tests"
git commit -m "Add WebSocket broadcaster and market data streaming"
git commit -m "Add WebSocket tests"
git commit -m "Add throughput benchmark"
git commit -m "Add week2 summary and documentation"
```

---

### Day 5 Checklist
- [ ] Benchmark script created and run
- [ ] Benchmark results recorded in `week2_summary.md`
- [ ] Full test suite passes
- [ ] Code formatted with `black` and linted with `ruff`
- [ ] `week2_summary.md` written
- [ ] All changes committed

---

## Week 2 Final Validation

### Must Pass:
```bash
# All tests pass
pytest -v

# Server starts cleanly
uvicorn main:app --port 8000

# Basic smoke test
curl http://localhost:8000/tickers
```

### Metrics to Record:
- Total test count: `pytest --collect-only`
- Throughput: from `benchmarks/throughput.py`
- New lines of code: `find trading/api/ -name "*.py" | xargs wc -l`

### Success Criteria:
✅ `POST /orders` accepts valid orders and returns trades
✅ `POST /orders/{id}/cancel` cancels open orders
✅ `GET /book/{ticker}` returns correct bids/asks after orders
✅ `GET /tickers` returns all 5 supported tickers
✅ WebSocket streams `book_update` and `trade` events per subscription
✅ Clients only receive events for their subscribed tickers
✅ Throughput benchmark documented
✅ All tests pass (REST, WebSocket, previous matching engine tests)
✅ Zero failing tests

---

## Architecture After Week 2

```
HTTP Client
    |
    | POST /orders  GET /book/{ticker}  GET /tickers
    ↓
FastAPI (Async)
    |
    | OrderRequest (Pydantic validated)
    ↓
asyncio.Queue  ←──────── HTTP handler awaits Future
    |
    | (order, future) tuple
    ↓
Consumer (run_consumer coroutine)
    |
    | engine.submit_order(order)
    ↓
MatchingEngine → OrderBookManager → OrderBook (per ticker)
    |
    | List[Trade]
    ↓
future.set_result(trades)  ──────→  HTTP handler returns OrderResponse
    |
    ↓
Broadcaster.notify_book_update()
Broadcaster.notify_trade()
    |
    ↓
Per-ticker subscription queues
    |
    ↓
WebSocket clients (filtered by ticker)
```

---

## Troubleshooting

### Server fails to start: `ImportError`
```bash
# Verify all packages installed
uv sync
python -c "import fastapi, uvicorn, httpx; print('OK')"
```

### Tests fail with `asyncio event loop` errors
```bash
# Ensure pyproject.toml has:
# [tool.pytest.ini_options]
# asyncio_mode = "auto"
```

### WebSocket tests fail with timeout
- Ensure the consumer is actually started during tests (lifespan runs)
- Check that `conftest.py` resets broadcaster as well as engine

### Benchmark shows low throughput
- Ensure server is running in non-debug mode: `uvicorn main:app --port 8000` (no `--reload`)
- The asyncio.Queue single-consumer design is intentionally single-threaded per the determinism requirement

### `ruff` reports lint errors
```bash
ruff check trading/ --fix
```

---

## Next Week Preview

Week 3 will add:
- **Pre-trade risk checks**: Max position per account per ticker, portfolio-wide notional exposure limit
- **Market order price protection**: Reject if spread is wider than a configured threshold
- **Event-sourced persistence**: Append-only log of all orders, trades, cancels
- **Snapshot + replay**: Dump book state periodically; restore identical state on restart
- **Idempotency**: Reject duplicate `order_id` submissions

---

**End of Week 2 Execution Plan**
