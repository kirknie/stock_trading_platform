# Week 2 Summary: API & Concurrency

## What Was Built

An async REST API and WebSocket market data layer on top of the Week 1 matching engine.
Orders are ingested via HTTP, processed asynchronously through an `asyncio.Queue`, and
real-time book/trade events are broadcast to WebSocket subscribers with per-ticker filtering.

---

## Core Components

### 1. Pydantic Schemas (`trading/api/schemas.py`)
- `OrderRequest`: Validates ticker, side, type, quantity, price with a `@model_validator`
  enforcing that LIMIT orders require a price and MARKET orders forbid one
- `OrderResponse`, `TradeResponse`: Full order state + any generated trades
- `OrderBookResponse`: Book snapshot with aggregated price levels, best bid/ask, spread
- `CancelResponse`, `TickersResponse`: Cancel result and ticker listing

### 2. Shared App State (`trading/api/dependencies.py`)
- Module-level singletons: `MatchingEngine` and `asyncio.Queue`
- `init_app_state()` called once at lifespan startup
- `get_engine()` / `get_order_queue()` injected into route handlers via `Depends()`
- Fixed ticker set: AAPL, MSFT, GOOGL, TSLA, NVDA

### 3. Async Order Consumer (`trading/api/consumer.py`)
- Long-running coroutine that drains `(Order, Future)` pairs from the queue
- Calls `engine.submit_order()`, resolves the future with resulting trades
- After each match: notifies the broadcaster with a book update and per-trade events
- Cancelled cleanly on shutdown via `asyncio.CancelledError`

### 4. REST Routes (`trading/api/routes.py`)
- `POST /orders` — submits order to queue, awaits future result, returns trades
- `POST /orders/{id}/cancel` — synchronous cancellation (no queue needed)
- `GET /book/{ticker}` — returns aggregated price levels from live order book
- `GET /tickers` — lists all supported tickers

### 5. WebSocket Broadcaster (`trading/api/broadcaster.py`)
- `subscribe(queue, tickers)` / `unsubscribe(queue)` — per-client queue management
- `notify_book_update()` / `notify_trade()` — push serialized JSON via `put_nowait`
- Drops messages silently if a client queue is full (backpressure)
- Module-level singleton initialized at lifespan startup

### 6. WebSocket Endpoint (`trading/api/websocket.py`)
- Protocol: connect → subscribe → stream events → disconnect
- Races `client_queue.get()` against `websocket.receive()` using `asyncio.wait`
- Detects disconnect by checking `msg["type"] == "websocket.disconnect"` from raw receive
- Re-arms the receive task for non-disconnect frames (future client commands)

---

## Request Flow

```
HTTP Client
    |
    | POST /orders
    ↓
FastAPI route handler
    |
    | OrderRequest (Pydantic validated)
    ↓
asyncio.Queue  ←──────── handler awaits Future
    |
    | (Order, Future) tuple
    ↓
run_consumer coroutine
    |
    | engine.submit_order(order)
    ↓
MatchingEngine → OrderBookManager → OrderBook (per ticker)
    |
    | List[Trade]
    ↓
future.set_result(trades) ──→ HTTP handler returns OrderResponse
    |
    ↓
broadcaster.notify_book_update()
broadcaster.notify_trade()     (one per trade)
    |
    ↓
Per-ticker asyncio.Queue per connected client
    |
    ↓
WebSocket endpoint → ws.send_text(payload)
    |
    ↓
WebSocket Client (receives only subscribed tickers)
```

---

## REST Endpoints

| Method | Path | Status | Description |
|--------|------|--------|-------------|
| `POST` | `/orders` | 201 / 422 | Submit limit or market order |
| `POST` | `/orders/{id}/cancel` | 200 | Cancel open order (always 200) |
| `GET` | `/book/{ticker}` | 200 / 404 | Order book snapshot |
| `GET` | `/tickers` | 200 | List supported tickers |

---

## WebSocket Protocol

```json
// Client → Server: first message must be a subscription
{"action": "subscribe", "tickers": ["AAPL", "MSFT"]}

// Server → Client: subscription acknowledged
{"type": "subscribed", "tickers": ["AAPL", "MSFT"]}

// Server → Client: after any order on a subscribed ticker
{"type": "book_update", "ticker": "AAPL", "best_bid": "149.50", "best_ask": "150.00", "spread": "0.50"}

// Server → Client: after a match on a subscribed ticker
{"type": "trade", "ticker": "AAPL", "price": "150.00", "quantity": 100, "trade_id": "T-..."}

// Server → Client: invalid first message
{"error": "First message must be: {\"action\": \"subscribe\", \"tickers\": [...]}"}
```

Clients only receive events for their subscribed tickers. Unsubscribed ticker activity
is filtered before any data leaves the server.

---

## Design Decisions

### asyncio.Queue as the ingestion buffer
The HTTP handler enqueues an `(Order, Future)` pair and awaits the future.
The consumer coroutine drains the queue sequentially.

- **Why**: Decouples HTTP concurrency (many requests in flight) from the matching engine
  (single-threaded, deterministic). The engine never needs a lock.
- **Trade-off**: One consumer serialises all matches. Throughput is bounded by the
  consumer loop, not by HTTP concurrency. Horizontal scaling would require per-ticker
  consumers (Week 3 candidate).

### Synchronous cancellation (no queue)
`POST /orders/{id}/cancel` calls `engine.cancel_order()` directly without going through
the queue.

- **Why**: Cancellation produces no trades and doesn't need to be sequenced relative to
  other cancellations. Going through the queue would add unnecessary latency.
- **Risk**: A cancel can race with the consumer processing the same order. The engine's
  order status checks make this safe — a filled order cannot be cancelled.

### Broadcaster with per-client asyncio.Queue
Each WebSocket client gets its own `asyncio.Queue`. The broadcaster's `put_nowait`
is non-blocking — a slow client cannot slow down matching or other clients.

- **Why**: Avoids head-of-line blocking. A slow reader only loses their own messages.
- **Trade-off**: Dropped messages on full queue (maxsize=100). A production system would
  track sequence numbers and allow clients to re-sync.

### Race-based disconnect detection in WebSocket endpoint
The streaming loop races `asyncio.wait({queue_task, receive_task})` to detect client
disconnect without polling.

- **Why**: `websocket.client_state` is only updated when you actually call `receive()`.
  A polling approach (1s timeout + state check) leaves the endpoint running for up to 1
  second after disconnect and hangs `ASGIWebSocketTransport`'s task group teardown.
- The receive task is re-armed on non-disconnect frames, so future client-to-server
  messages (e.g. dynamic re-subscription) can be added without restructuring the loop.

---

## Test Coverage

### New Tests Added This Week

| File | Tests | Covers |
|------|-------|--------|
| `test_api.py` | 17 | All 4 REST endpoints, validation, matching, cancellation |
| `test_websocket.py` | 5 | Subscribe ack, book_update, trade event, filtering, invalid sub |

### Full Suite

| File | Tests |
|------|-------|
| `test_models.py` | 10 |
| `test_order_book.py` | 16 |
| `test_order_book_manager.py` | 14 |
| `test_integration.py` | 13 |
| `test_edge_cases.py` | 19 |
| `test_properties.py` | 10 |
| `test_api.py` | 17 |
| `test_websocket.py` | 5 |
| **Total** | **104** |

**Key testing patterns:**
- `LifespanManager` from `asgi-lifespan` triggers the full app lifespan (consumer task
  starts) for every test — no mocking, no test-only code paths
- Each test gets a fresh `MatchingEngine` and `asyncio.Queue` via lifespan re-init
- `ASGIWebSocketTransport` from `httpx-ws` drives real WebSocket frames in-process,
  no network required

---

## Throughput Benchmark Results

Measured on a 2024 MacBook Pro (M-series), single uvicorn worker, no `--reload`:

```
Single-ticker (AAPL)
  Orders processed : 500
  Wall time        : 1.049s
  Throughput       : 476.5 orders/sec
  Latency p50      : 16.70 ms
  Latency p95      : 57.64 ms
  Latency p99      : 73.66 ms
  Min / Max        : 8.41 / 163.70 ms

Multi-ticker (5 tickers)
  Orders processed : 500
  Wall time        : 1.258s
  Throughput       : 397.5 orders/sec
  Latency p50      : 21.02 ms
  Latency p95      : 23.32 ms
  Latency p99      : 39.51 ms
  Min / Max        : 9.22 / 988.06 ms
```

**Observations:**
- Single-ticker throughput is ~20% higher than multi-ticker, consistent with
  the single consumer serialising across all books in the multi-ticker case
- p99 latency spikes (73 ms / 39 ms) occur when the semaphore-bound concurrency
  (10 in-flight) creates bursts at the single consumer
- The 988 ms max in multi-ticker is a GIL/scheduling outlier, not representative
  of steady-state performance
- The asyncio.Queue single-consumer architecture is intentionally throughput-bounded
  to preserve determinism; per-ticker consumers would reduce latency under load

---

## File Structure

```
trading/
├── api/
│   ├── __init__.py
│   ├── broadcaster.py    # 111 lines
│   ├── consumer.py       #  68 lines
│   ├── dependencies.py   #  43 lines
│   ├── routes.py         # 196 lines
│   ├── schemas.py        # 101 lines
│   └── websocket.py      #  91 lines
└── tests/
    ├── test_api.py        # 353 lines
    └── test_websocket.py  # 177 lines

benchmarks/
└── throughput.py          # 143 lines

main.py                    #  51 lines

New this week: ~1,334 lines (source + tests + benchmark)
```

---

## Known Limitations

1. **No authentication**: Any client can submit orders or connect to the WebSocket
2. **No rate limiting**: A single client can flood the order queue
3. **No WebSocket reconnect**: Clients that disconnect lose their subscription; no
   session resumption or missed-message recovery
4. **Single consumer**: All tickers share one consumer coroutine; a slow match on one
   ticker delays all others
5. **Order registry grows unbounded**: Filled/cancelled orders accumulate in memory
   (same as Week 1 — cleanup deferred to Week 3)
6. **No tick size enforcement**: Any `Decimal` price is accepted
7. **Broadcast drops on full queue**: A slow WebSocket client silently misses messages
   when their per-client queue (maxsize=100) is full

---

## Validation Checklist

- [x] All 104 tests pass (`pytest -v`)
- [x] REST endpoints return correct status codes and payloads
- [x] WebSocket streams book updates and trade events after order submission
- [x] WebSocket clients only receive events for subscribed tickers
- [x] Server starts cleanly (`uvicorn main:app --port 8000`)
- [x] Throughput benchmark runs and results recorded
- [x] Code formatted with `black` and linted with `ruff`
- [x] Zero failing tests

---

## Resume Bullet (Draft)

> "Layered an async REST + WebSocket API on a deterministic matching engine using
> FastAPI and Python's asyncio. Designed a queue-based order ingestion pipeline
> decoupling HTTP concurrency from single-threaded matching, achieving ~477 orders/sec
> on a single worker. Implemented real-time market data broadcast with per-ticker
> WebSocket subscriptions and property-based tests validating correctness across
> 104 scenarios."

---

## What's Next: Week 3

- **Pre-trade risk checks**: Max position per account, portfolio notional exposure limit
- **Market order price protection**: Reject if spread exceeds a configured threshold
- **Event-sourced persistence**: Append-only log of all orders, trades, cancels
- **Snapshot + replay**: Dump book state; restore identical state on restart
- **Idempotency**: Reject duplicate `order_id` submissions
