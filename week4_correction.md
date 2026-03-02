# Correction Plan: Closing Gaps Between plan.md and Implementation

## Status Summary

Weeks 1–3 are fully complete. Week 4 in plan.md called for **Observability & Polish**
but was instead used to finish resilience work (risk recovery, eviction, persistent
idempotency, per-ticker consumers). The following gaps remain open.

---

## Gap Map

| # | Gap | Plan Reference | Priority |
|---|-----|----------------|----------|
| 1 | Per-ticker notional exposure risk check | "Max notional exposure per ticker" | High |
| 2 | Prometheus metrics (orders/sec, latency, error rate) | "Observability: Latency, Orders/sec, Error rates" | High |
| 3 | Structured logging with structlog | "Metrics & logging" | High |
| 4 | Failure injection tests | "Failure injection tests" | Medium |
| 5 | Health check + readiness endpoints | Implied by production observability | Medium |
| 6 | Run and document throughput benchmark | "Throughput benchmarked and documented" | Medium |
| 7 | WebSocket order status updates | "Order status updates" (WebSocket spec) | Medium |
| 8 | README + architecture diagram | "README + architecture diagram" | High |
| 9 | Resume bullets finalised | "Resume bullets finalized" | High |

---

## Gap 1: Per-Ticker Notional Exposure Risk Check

### Background

`plan.md` lists three pre-trade risk checks:
- Max position per account per ticker ✓ (done: 10,000 shares)
- **Max notional exposure per ticker** ✗ (missing)
- Total portfolio exposure across all tickers ✓ (done: $1M per account)

The missing check is **per-account, per-ticker notional exposure**: an account cannot
have more than a fixed open value (e.g., $500,000) in any single ticker at one time.
This prevents over-concentration in one name.

The current `_open_orders` in `RiskChecker` tracks total open value across all tickers.
A per-ticker version partitions that by `(account_id, ticker)`.

### Step 1.1 — Add constant and state to `trading/risk/checker.py`

Open `trading/risk/checker.py`. Add a new constant at the top alongside the existing ones:

```python
MAX_NOTIONAL_PER_TICKER: Decimal = Decimal("500000")  # $500k per account per ticker
```

Add a new state dict to `RiskChecker.__init__`:

```python
# Notional exposure per (account_id, ticker): sum of qty * price for open LIMIT orders
self._ticker_notional: dict[str, dict[str, Decimal]] = defaultdict(
    lambda: defaultdict(Decimal)
)
```

### Step 1.2 — Update `record_open_order` to track per-ticker notional

In `record_open_order(order)`, after updating `_open_orders`, add:

```python
if order.order_type == OrderType.LIMIT and order.price is not None:
    self._ticker_notional[order.account_id][order.ticker] += (
        Decimal(order.quantity) * order.price
    )
```

### Step 1.3 — Update `record_cancel` and `record_order_complete` to untrack

In `record_cancel(order)`:

```python
if order.order_type == OrderType.LIMIT and order.price is not None:
    self._ticker_notional[order.account_id][order.ticker] -= (
        Decimal(order.quantity) * order.price
    )
```

In `record_order_complete(order)`:

```python
if order.order_type == OrderType.LIMIT and order.price is not None:
    self._ticker_notional[order.account_id][order.ticker] -= (
        Decimal(order.remaining_quantity()) * order.price
    )
```

Note: use `remaining_quantity()` not `quantity` since partial fills have already reduced
the open exposure via `record_fill`.

### Step 1.4 — Update `record_fill` to reduce per-ticker notional

In `record_fill(trade, buyer_account, seller_account)`, after updating positions, reduce
per-ticker notional for both sides by `trade.quantity * trade.price`:

```python
self._ticker_notional[buyer_account][trade.ticker] = max(
    Decimal("0"),
    self._ticker_notional[buyer_account][trade.ticker] - Decimal(trade.quantity) * trade.price,
)
self._ticker_notional[seller_account][trade.ticker] = max(
    Decimal("0"),
    self._ticker_notional[seller_account][trade.ticker] - Decimal(trade.quantity) * trade.price,
)
```

### Step 1.5 — Add `get_ticker_notional` helper and check in `check(order)`

Add a getter:

```python
def get_ticker_notional(self, account_id: str, ticker: str) -> Decimal:
    return self._ticker_notional[account_id][ticker]
```

In `check(order)`, after the existing notional exposure check, add:

```python
if order.order_type == OrderType.LIMIT and order.price is not None:
    ticker_exposure = self.get_ticker_notional(order.account_id, order.ticker)
    order_value = Decimal(order.quantity) * order.price
    if ticker_exposure + order_value > MAX_NOTIONAL_PER_TICKER:
        raise RiskViolation(
            f"Per-ticker notional limit exceeded for {order.ticker}: "
            f"current={ticker_exposure}, order={order_value}, "
            f"limit={MAX_NOTIONAL_PER_TICKER}"
        )
```

### Step 1.6 — Update `_rebuild_risk` in `main.py`

`_rebuild_risk` already calls `risk.record_open_order()` for all resting orders (Phase 1)
and `risk.record_fill()` for all trades (Phase 2). Because Steps 1.2–1.4 add tracking
inside those same methods, `_rebuild_risk` needs no changes.

### Step 1.7 — Write tests in `trading/tests/test_risk.py`

Add to `test_risk.py`:

```
test_per_ticker_notional_rejected_when_limit_exceeded
  - Submit LIMIT BUY for 4,000 shares @ $100 in AAPL ($400k) → 201
  - Submit LIMIT BUY for 2,000 shares @ $100 in AAPL ($200k) → 422
    (total $600k > $500k per-ticker limit)
  - detail must contain "Per-ticker notional limit exceeded"

test_per_ticker_notional_independent_across_tickers
  - Submit LIMIT BUY 4,000 @ $100 in AAPL ($400k) → 201
  - Submit LIMIT BUY 4,000 @ $100 in MSFT ($400k) → 201
    (different tickers, each within $500k)

test_per_ticker_notional_freed_after_cancel
  - Submit LIMIT BUY 4,000 @ $100 in AAPL ($400k) → 201
  - Cancel it
  - Submit LIMIT BUY 4,000 @ $100 in AAPL again → 201
    (exposure back to $0 after cancel)

test_per_ticker_notional_reduced_after_fill
  - Submit SELL 2,000 @ $100 (resting)
  - Submit BUY 2,000 @ $100 ($200k) — fills both
  - Submit another BUY 4,000 @ $100 → 201
    (only $400k remaining, not $600k)
```

Run:
```bash
python -m pytest trading/tests/test_risk.py -v
python -m pytest trading/tests/ -q
```

---

## Gap 2: Prometheus Metrics

### Background

`plan.md` requires: "Throughput benchmarked and documented" and "Observability:
Latency, Orders/sec, Error rates". Prometheus is listed in the tech stack.

### Step 2.1 — Install dependency

Run:
```bash
uv add prometheus-client
```

This adds `prometheus-client` to `[project.dependencies]` in `pyproject.toml` automatically.

### Step 2.2 — Create `trading/metrics/collector.py`

Create `trading/metrics/` directory with `__init__.py`. Create `collector.py`:

```python
"""
Prometheus metrics for the trading platform.

Exposes:
  orders_submitted_total        — counter, labels: ticker, side, order_type
  orders_rejected_total         — counter, labels: ticker, reason
  trades_executed_total         — counter, labels: ticker
  order_processing_seconds      — histogram, labels: ticker
  queue_depth                   — gauge, labels: ticker
  active_websocket_connections  — gauge
"""

from prometheus_client import Counter, Gauge, Histogram, CollectorRegistry, REGISTRY

# ── Counters ──────────────────────────────────────────────────────────────────

orders_submitted = Counter(
    "orders_submitted_total",
    "Total orders submitted successfully",
    ["ticker", "side", "order_type"],
)

orders_rejected = Counter(
    "orders_rejected_total",
    "Total orders rejected (risk violations, invalid input)",
    ["ticker", "reason"],
)

trades_executed = Counter(
    "trades_executed_total",
    "Total trades executed",
    ["ticker"],
)

# ── Histograms ────────────────────────────────────────────────────────────────

order_processing_seconds = Histogram(
    "order_processing_seconds",
    "End-to-end order processing latency (queue enqueue to future resolved)",
    ["ticker"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
)

# ── Gauges ────────────────────────────────────────────────────────────────────

queue_depth = Gauge(
    "order_queue_depth",
    "Current number of orders waiting in the queue",
    ["ticker"],
)

active_ws_connections = Gauge(
    "active_websocket_connections_total",
    "Number of currently active WebSocket connections",
)
```

### Step 2.3 — Instrument `trading/api/routes.py`

In `submit_order`:

1. Before `await queues[order.ticker].put(...)`, record start time:
   ```python
   import time
   _start = time.perf_counter()
   ```

2. After `trades = await future` resolves successfully:
   ```python
   from trading.metrics.collector import orders_submitted, trades_executed, order_processing_seconds
   order_processing_seconds.labels(ticker=order.ticker).observe(
       time.perf_counter() - _start
   )
   orders_submitted.labels(
       ticker=order.ticker,
       side=order.side.value,
       order_type=order.order_type.value,
   ).inc()
   for _ in trades:
       trades_executed.labels(ticker=order.ticker).inc()
   ```

3. In the `except RiskViolation` block:
   ```python
   from trading.metrics.collector import orders_rejected
   orders_rejected.labels(ticker=request.ticker, reason="risk_violation").inc()
   ```

### Step 2.4 — Instrument queue depth in `consumer.py`

At the top of the `while True` loop, after `order, future = await queue.get()`:

```python
from trading.metrics.collector import queue_depth
queue_depth.labels(ticker=order.ticker).set(queue.qsize())
```

### Step 2.5 — Instrument WebSocket connections in `trading/api/websocket.py`

In the WebSocket handler, on connect:
```python
from trading.metrics.collector import active_ws_connections
active_ws_connections.inc()
```

On disconnect (in the `finally` block):
```python
active_ws_connections.dec()
```

### Step 2.6 — Expose `/metrics` endpoint in `main.py`

Add a route to expose Prometheus metrics:

```python
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

@app.get("/metrics", include_in_schema=False)
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

### Step 2.7 — Write tests in `trading/tests/test_metrics.py`

```
test_order_submission_increments_counter
  - Submit a LIMIT BUY order
  - orders_submitted_total{ticker="AAPL", side="BUY", order_type="LIMIT"} == 1

test_risk_violation_increments_rejected_counter
  - Submit order that exceeds position limit → 422
  - orders_rejected_total{reason="risk_violation"} >= 1

test_trade_increments_trade_counter
  - Submit matching SELL then BUY → trade
  - trades_executed_total{ticker="AAPL"} == 1

test_metrics_endpoint_returns_200
  - GET /metrics → 200
  - Content-Type: text/plain; version=0.0.4...
  - Body contains "orders_submitted_total"
```

Run:
```bash
python -m pytest trading/tests/test_metrics.py -v
python -m pytest trading/tests/ -q
```

---

## Gap 3: Structured Logging with structlog

### Background

Currently uses `logging.getLogger(__name__)` with stdlib format. `plan.md` lists
`structlog` as the logging tool.

### Step 3.1 — Install dependency

Run:
```bash
uv add structlog
```

This adds `structlog` to `[project.dependencies]` in `pyproject.toml` automatically.

### Step 3.2 — Create `trading/logging_config.py`

```python
"""
Structlog configuration for the trading platform.

Call configure_logging() once at application startup.

Output: JSON lines in production (LOG_FORMAT=json),
        coloured console output in development (default).
"""

import logging
import os
import structlog


def configure_logging() -> None:
    """Configure structlog with JSON or console renderer based on LOG_FORMAT env var."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = os.getenv("LOG_FORMAT", "console")  # "json" or "console"

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level, logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))
```

### Step 3.3 — Replace `logging.getLogger` calls

In every file that currently uses `logger = logging.getLogger(__name__)`:

```
trading/api/consumer.py
trading/api/broadcaster.py
trading/persistence/event_log.py
main.py
```

Replace:
```python
import logging
logger = logging.getLogger(__name__)
```

With:
```python
import structlog
logger = structlog.get_logger(__name__)
```

All existing `logger.info(...)`, `logger.error(...)` calls continue to work. Optionally
add structured key-value context to high-value log sites, for example in `consumer.py`:

```python
# Before:
logger.info("Order consumer started")

# After (add structured fields):
logger.info("order_consumer_started", ticker=queue_ticker)
```

And in the consumer order processing loop:
```python
logger.info(
    "order_processed",
    order_id=order.order_id,
    ticker=order.ticker,
    side=order.side.value,
    trade_count=len(trades),
)
```

### Step 3.4 — Call `configure_logging()` in `main.py`

At the top of `main.py`, before the `app = FastAPI(...)` line:

```python
from trading.logging_config import configure_logging
configure_logging()
```

### Step 3.5 — Add request correlation ID middleware

In `main.py`, add a middleware that injects a `request_id` into the structlog context
for every request, so all log lines for a request share the same ID:

```python
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

app.add_middleware(RequestIdMiddleware)
```

---

## Gap 4: Failure Injection Tests

### Background

`plan.md` requires "Failure injection tests". These verify the system behaves
correctly under adverse conditions without needing real failures.

### Step 4.1 — Create `trading/tests/test_failure_injection.py`

```
test_market_order_rejected_when_book_empty
  - Submit MARKET BUY with no resting sells
  - Expect 422 with "no liquidity" or spread-related message

test_market_order_rejected_when_spread_too_wide
  - Submit SELL LIMIT at $200, BUY LIMIT at $100 (spread = $100 > $50)
  - Submit MARKET BUY
  - Expect 422 with "spread" in detail

test_cancel_nonexistent_order_returns_false
  - POST /orders/nonexistent-id/cancel
  - Expect success=False (not an error)

test_duplicate_order_id_does_not_double_submit_after_restart
  [uses LifespanManager restart pattern from test_idempotency_persistence.py]
  - Submit order with order_id="dup-1" → 201
  - Restart
  - Submit same order_id → 200 (cached, no second order in book)

test_event_log_truncated_mid_line_does_not_crash_on_restart
  - Submit 3 orders (creates event log with 3+ lines)
  - Truncate the last line of events.log to simulate partial write
  - Restart via LifespanManager
  - Verify server starts cleanly (does not raise, returns 200 on /tickers)
  [requires EventLog._read_max_sequence and read_all to skip malformed lines —
   they already use try/except json.JSONDecodeError, so this should pass]

test_risk_violation_does_not_corrupt_risk_state
  - Submit a valid order (accepted)
  - Submit an order that violates position limit (rejected with 422)
  - Submit another valid order (must still be accepted)
  [verifies record_open_order is NOT called for rejected orders]

test_cancel_after_fill_returns_not_found
  - Submit and fill an order (submit matching buy + sell)
  - Attempt to cancel the filled order
  - Expect success=False

test_submit_to_unsupported_ticker_returns_422
  - POST /orders with ticker="FAKE"
  - Expect 422 with ticker not supported
```

Run:
```bash
python -m pytest trading/tests/test_failure_injection.py -v
python -m pytest trading/tests/ -q
```

---

## Gap 5: Health Check + Readiness Endpoints

### Background

Production services need `/health` (liveness) and `/ready` (readiness) endpoints for
orchestration systems (Kubernetes, load balancers). Not explicitly in `plan.md` but
required for the "production-minded" framing.

### Step 5.1 — Add health endpoints to `main.py` or a new `trading/api/health.py`

Create `trading/api/health.py`:

```python
"""
Health and readiness endpoints.

GET /health  — liveness probe: returns 200 if the process is running
GET /ready   — readiness probe: returns 200 if all singletons are initialised
               and the consumer tasks are alive
"""

from fastapi import APIRouter, Response, status
from trading.api.dependencies import get_engine, get_event_log, get_order_queues

health_router = APIRouter()


@health_router.get("/health", include_in_schema=False)
async def health() -> dict:
    """Liveness probe — always returns 200 if the process is up."""
    return {"status": "ok"}


@health_router.get("/ready", include_in_schema=False)
async def ready(response: Response) -> dict:
    """
    Readiness probe — returns 200 only if core singletons are initialised.
    Returns 503 if startup is incomplete (e.g., called before lifespan completes).
    """
    try:
        engine = get_engine()
        _ = get_event_log()
        queues = get_order_queues()
    except RuntimeError as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "reason": str(exc)}

    return {
        "status": "ready",
        "tickers": engine.get_supported_tickers(),
        "queue_depths": {ticker: q.qsize() for ticker, q in queues.items()},
    }
```

In `main.py`, register the router:

```python
from trading.api.health import health_router
app.include_router(health_router)
```

### Step 5.2 — Write tests

Add to `trading/tests/test_api.py` or a new `test_health.py`:

```
test_health_returns_200
  - GET /health → 200, body {"status": "ok"}

test_ready_returns_200_when_initialised
  - GET /ready → 200, body contains "status": "ready" and "tickers"

test_ready_returns_queue_depths
  - GET /ready → body contains "queue_depths" dict with all 5 tickers
```

---

## Gap 6: Run and Document Throughput Benchmark

### Background

`plan.md` requires "Throughput benchmarked and documented". A benchmark file
`benchmarks/throughput.py` already exists but has never been run and its results
have not been documented.

### Step 6.1 — Run the benchmark

Start the server:
```bash
uvicorn main:app --port 8000
```

In a second terminal, run the benchmark:
```bash
python benchmarks/throughput.py
```

Capture the output — record all numbers printed (orders/sec, p50/p95/p99 latency,
min/max).

### Step 6.2 — Verify the benchmark is complete

Read `benchmarks/throughput.py`. Verify it covers:
- Single-ticker throughput (all orders to AAPL)
- Multi-ticker throughput (orders spread across 5 tickers)
- Latency percentiles: p50, p95, p99, min, max

If any of these are missing, add them following the existing pattern.

### Step 6.3 — Add mixed load test (matched orders)

The existing benchmark likely submits only resting limit orders (no trades). Add a
scenario that generates actual trades:

```python
async def bench_matched_orders(client, n=500):
    """
    Submit alternating SELL then BUY at the same price — every pair generates a trade.
    Measures throughput when the matching engine is active (not just book insertion).
    """
    ...
```

### Step 6.4 — Document results in `benchmarks/README.md`

Create `benchmarks/README.md` with:

```markdown
# Throughput Benchmark Results

## Environment
- Machine: <your machine model>
- Python: 3.13.x
- OS: macOS 25.x
- Date: <date>

## Results

### Single-ticker (AAPL, limit orders, no matches)
| Metric | Value |
|--------|-------|
| Orders | 500 |
| Wall time | X.XXs |
| Throughput | XXX orders/sec |
| p50 latency | XX ms |
| p95 latency | XX ms |
| p99 latency | XX ms |
| Min / Max | X ms / X ms |

### Multi-ticker (5 tickers, limit orders, no matches)
...

### Matched orders (alternating buy/sell, trade generated per pair)
...

## Design Notes
- Latency is end-to-end HTTP round-trip (client → queue → consumer → response)
- Single uvicorn worker, single event loop
- Per-ticker consumers run concurrently; matching within a ticker is sequential
- Bottleneck is asyncio event loop overhead, not matching algorithm (O(log n) per level)
```

---

## Gap 7: WebSocket Order Status Updates

### Background

`plan.md` WebSocket spec includes "Order status updates" as a distinct event type.
Currently only `book_update` and `trade` events are broadcast. Clients cannot track
order lifecycle (NEW → PARTIALLY_FILLED → FILLED / CANCELED) via WebSocket.

### Step 7.1 — Add `notify_order_status` to `Broadcaster`

In `trading/api/broadcaster.py`, add:

```python
async def notify_order_status(
    self,
    order_id: str,
    ticker: str,
    status: str,
    filled_quantity: int,
    remaining_quantity: int,
) -> None:
    await self.broadcast(
        ticker,
        {
            "type": "order_status",
            "order_id": order_id,
            "ticker": ticker,
            "status": status,
            "filled_quantity": filled_quantity,
            "remaining_quantity": remaining_quantity,
        },
    )
```

### Step 7.2 — Emit order status from `consumer.py`

In `consumer.py`, after `future.set_result(trades)`, add an order status notification:

```python
await broadcaster.notify_order_status(
    order_id=order.order_id,
    ticker=order.ticker,
    status=order.status.value,
    filled_quantity=order.filled_quantity,
    remaining_quantity=order.remaining_quantity(),
)
```

Also emit on cancel in `routes.py` — in `cancel_order`, after `event_log.append_order_cancelled`:

```python
from trading.api.dependencies import get_broadcaster  # add this getter
broadcaster = get_broadcaster()
await broadcaster.notify_order_status(
    order_id=order_id,
    ticker=order.ticker,
    status="CANCELED",
    filled_quantity=order.filled_quantity,
    remaining_quantity=0,
)
```

Note: `get_broadcaster()` needs to be added to `dependencies.py` alongside the other
getters. The `Broadcaster` singleton is currently created in `main.py` via
`init_broadcaster()` but is not stored in `dependencies.py`. Either add it there or
pass it via dependency injection following the existing pattern.

### Step 7.3 — Update WebSocket test

In `trading/tests/test_websocket.py`, add:

```
test_order_status_event_received_on_fill
  - Connect to /ws, subscribe to AAPL
  - Submit matching SELL + BUY orders
  - Receive messages; verify one has type="order_status" and status="FILLED"

test_order_status_event_received_on_cancel
  - Connect to /ws, subscribe to AAPL
  - Submit LIMIT BUY (resting)
  - Cancel it
  - Receive messages; verify one has type="order_status" and status="CANCELED"
```

---

## Gap 8: README + Architecture Diagram

### Background

`plan.md` explicitly requires a README and architecture diagram. Neither exists.

### Step 8.1 — Create `README.md` in the repo root

Structure:

```markdown
# Stock Trading Platform

A production-minded trading backend built with Python 3.13 + FastAPI + asyncio.

## Features
- Limit and market order matching (price-time priority)
- 5 US equity tickers: AAPL, MSFT, GOOGL, TSLA, NVDA
- Pre-trade risk checks: position limits, notional exposure, spread protection
- Append-only event log + periodic snapshots for crash recovery
- Idempotent order submission (client-supplied order_id, 24h TTL)
- Per-ticker consumer sharding (concurrent matching across symbols)
- WebSocket market data: book updates, trades, order status
- Prometheus metrics: latency, throughput, queue depth, error rates

## Architecture

[ASCII diagram — see below]

## Quick Start

### Prerequisites
- Python 3.13+
- pip

### Install
git clone ...
cd stock_trading_platform
uv sync --all-groups

### Run
uvicorn main:app --port 8000

### Example
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"ticker":"AAPL","side":"BUY","order_type":"LIMIT","quantity":100,"price":"150.00"}' \
  | python -m json.tool

## API Reference
[table of endpoints]

## Configuration
[table of env vars: EVENT_LOG_PATH, SNAPSHOT_PATH, LOG_LEVEL, LOG_FORMAT]

## Testing
python -m pytest trading/tests/ -q          # 194 tests
python -m pytest trading/tests/ --tb=short  # with tracebacks

## Benchmarks
See benchmarks/README.md for throughput and latency results.

## Design Decisions
[link to week3_summary.md and week4_summary.md]
```

### Step 8.2 — ASCII architecture diagram

Include in `README.md`:

```
┌─────────────────────────────────────────────────────────────┐
│                        HTTP Clients                         │
└──────────────────┬────────────────────────┬────────────────-┘
                   │ REST                   │ WebSocket /ws
                   ▼                        ▼
          ┌────────────────┐    ┌───────────────────────┐
          │  FastAPI App   │    │  Broadcaster          │
          │  POST /orders  │    │  (per-ticker pub/sub) │
          │  GET  /book    │    └──────────▲────────────┘
          │  GET  /tickers │               │
          └───────┬────────┘               │ notify_book_update
                  │                        │ notify_trade
                  │ queues[ticker]         │ notify_order_status
                  ▼                        │
        ┌──────────────────┐              │
        │  Per-Ticker      │──────────────┘
        │  Order Queues    │
        │  (asyncio.Queue) │
        └──────┬───────────┘
               │  one consumer per ticker
       ┌───────▼──────────────────────────────────┐
       │  run_consumer (×5 tasks)                 │
       │  ├─ RiskChecker.check()                  │
       │  ├─ EventLog.append_order_submitted()    │
       │  ├─ MatchingEngine.submit_order()        │
       │  ├─ EventLog.append_trade_executed()     │
       │  └─ RiskChecker.record_fill()            │
       └───────┬──────────────────────────────────┘
               │
       ┌───────▼──────────────────────────────────┐
       │  MatchingEngine                          │
       │  └─ OrderBookManager                     │
       │     ├─ OrderBook[AAPL] (SortedDict bids) │
       │     ├─ OrderBook[MSFT]                   │
       │     └─ ... (GOOGL, TSLA, NVDA)           │
       └───────┬──────────────────────────────────┘
               │
       ┌───────▼──────────────────────────────────┐
       │  Persistence                             │
       │  ├─ data/events.log  (NDJSON append)     │
       │  └─ data/snapshot.json (periodic)        │
       └──────────────────────────────────────────┘
```

---

## Gap 9: Resume Bullets

`plan.md` says "Resume bullets finalized". These are already drafted in `week4_summary.md`
but need to be consolidated and refined. No code changes needed.

### Step 9.1 — Finalize bullets

Once all above gaps are closed, update `week4_summary.md` Resume Bullet to include
the new capabilities. Draft:

> "Built a production-grade Python asyncio trading backend: price-time priority limit/market
> order matching across 5 tickers, pre-trade risk controls (position, notional, per-ticker
> concentration), crash recovery via append-only NDJSON event log and periodic snapshots,
> idempotent order submission with 24h TTL persisted to the event log, per-ticker consumer
> sharding for concurrent matching, and Prometheus metrics (p99 latency, orders/sec, error
> rates). 194 tests including property-based invariants and full restart simulation. Achieved
> XXX orders/sec on a single event loop with p99 latency of XX ms."

The XXX values come from Step 6 benchmark results.

---

## Execution Order

Do these in order — each builds on the previous:

```
Gap 1  (risk check)        — ~2h — isolated to checker.py + tests
Gap 4  (failure tests)     — ~1h — no production code changes
Gap 5  (health endpoints)  — ~1h — additive, no changes to existing code
Gap 7  (WS order status)   — ~2h — small broadcaster + consumer change + tests
Gap 2  (Prometheus)        — ~3h — new module, instrument 3 files
Gap 3  (structlog)         — ~2h — replace logger calls, add middleware
Gap 6  (benchmark)         — ~1h — run existing script, document results
Gap 8  (README)            — ~2h — write docs only
Gap 9  (resume bullets)    — ~30m — update summary doc
```

---

## Validation Checklist (after all gaps closed)

```bash
uv run pytest trading/tests/ -q
# Expected: 210+ tests pass

uv run mypy trading/ --ignore-missing-imports
# Expected: no errors

uv run ruff check trading/ main.py
uv run black trading/ main.py --check

uv run uvicorn main:app --port 8000 &
curl -s http://localhost:8000/health | python -m json.tool
curl -s http://localhost:8000/ready | python -m json.tool
curl -s http://localhost:8000/metrics | head -20
uv run python benchmarks/throughput.py
```
