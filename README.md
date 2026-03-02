# Stock Trading Platform

A production-minded trading backend built with Python 3.13 + FastAPI + asyncio.

## Features

- Limit and market order matching (price-time priority)
- 5 US equity tickers: AAPL, MSFT, GOOGL, TSLA, NVDA
- Pre-trade risk checks: position limits, notional exposure per ticker, spread protection
- Append-only NDJSON event log + periodic snapshots for crash recovery
- Idempotent order submission (client-supplied `order_id`, 24h TTL persisted to event log)
- Per-ticker consumer sharding (concurrent matching across symbols)
- WebSocket market data: book updates, trades, order status
- Prometheus metrics: latency histogram, orders/sec, error rates, queue depth
- Structured logging with `structlog` (JSON in production, coloured console in dev)
- Health (`/health`) and readiness (`/ready`) endpoints

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        HTTP Clients                         │
└──────────────────┬────────────────────────┬─────────────────┘
                   │ REST                   │ WebSocket /ws
                   ▼                        ▼
          ┌────────────────┐    ┌───────────────────────┐
          │  FastAPI App   │    │  Broadcaster          │
          │  POST /orders  │    │  (per-ticker pub/sub) │
          │  GET  /book    │    └──────────▲────────────┘
          │  GET  /tickers │               │
          │  GET  /health  │               │ notify_book_update
          │  GET  /ready   │               │ notify_trade
          └───────┬────────┘               │ notify_order_status
                  │                        │
                  │ queues[ticker]         │
                  ▼                        │
        ┌──────────────────┐               │
        │  Per-Ticker      │───────────────┘
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

## Quick Start

### Prerequisites

- Python 3.13+
- [uv](https://github.com/astral-sh/uv)

### Install

```bash
git clone <repo-url>
cd stock_trading_platform
uv sync --all-groups
```

### Run

```bash
uvicorn main:app --port 8000
```

### Example

```bash
# Submit a limit buy order
curl -s -X POST http://localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"ticker":"AAPL","side":"BUY","order_type":"LIMIT","quantity":100,"price":"150.00"}' \
  | python -m json.tool

# Check the order book
curl -s http://localhost:8000/book/AAPL | python -m json.tool

# Check readiness
curl -s http://localhost:8000/ready | python -m json.tool

# Prometheus metrics
curl -s http://localhost:8000/metrics | head -20
```

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/orders` | Submit a limit or market order |
| `POST` | `/orders/{id}/cancel` | Cancel an open order |
| `GET` | `/book/{ticker}` | Order book snapshot |
| `GET` | `/tickers` | List supported tickers |
| `GET` | `/health` | Liveness probe |
| `GET` | `/ready` | Readiness probe |
| `GET` | `/metrics` | Prometheus metrics |
| `WS` | `/ws` | Real-time market data stream |

### WebSocket Protocol

```
Client → {"action": "subscribe", "tickers": ["AAPL", "MSFT"]}
Server → {"type": "subscribed", "tickers": ["AAPL", "MSFT"]}
Server → {"type": "book_update", "ticker": "AAPL", "best_bid": "149.50", "best_ask": "150.00", "spread": "0.50"}
Server → {"type": "trade", "ticker": "AAPL", "price": "150.00", "quantity": 100, ...}
Server → {"type": "order_status", "ticker": "AAPL", "order_id": "...", "status": "FILLED", ...}
```

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `EVENT_LOG_PATH` | `data/events.log` | Path to the NDJSON event log |
| `SNAPSHOT_PATH` | `data/snapshot.json` | Path to the periodic snapshot file |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FORMAT` | `console` | Log format: `console` (coloured) or `json` |

## Testing

```bash
uv run pytest trading/tests/ -q          # 215 tests
uv run pytest trading/tests/ --tb=short  # with tracebacks
```

Test suite covers:
- Order submission and matching (unit + integration)
- Risk check enforcement (position, notional, per-ticker, spread)
- Property-based invariants via Hypothesis
- Failure injection (truncated event log, duplicate order IDs, risk state integrity)
- WebSocket event streaming
- Prometheus metrics
- Crash recovery and restart simulation

## Benchmarks

See [benchmarks/README.md](benchmarks/README.md) for throughput and latency results.

Summary (live uvicorn server, Apple Silicon, Python 3.13):

| Scenario | Throughput | p50 | p99 |
|----------|-----------|-----|-----|
| Single-ticker (AAPL) | 1,025 orders/sec | 9.35 ms | 19.58 ms |
| Multi-ticker (5 tickers) | 974 orders/sec | 8.74 ms | 27.87 ms |
| Matched orders (trade per BUY) | 876 orders/sec | 11.86 ms | 24.07 ms |

## Design Decisions

See [week3_summary.md](week3_summary.md) and [week4_summary.md](week4_summary.md) for architecture rationale and weekly progress notes.

Key decisions:
- **Per-ticker asyncio queues** decouple HTTP ingestion from matching; one consumer task per ticker enables concurrent matching across symbols with no locking needed (cooperative multitasking guarantee).
- **Append-only event log + snapshots** provide crash recovery without a database. On restart: load snapshot → replay events after snapshot sequence → rebuild risk and idempotency state.
- **Idempotency via client-supplied `order_id`** persisted to the event log so duplicate detection survives restarts.
- **Price-time priority** implemented with `SortedDict` (O(log n) per level) and `deque` per price level (O(1) FIFO).
