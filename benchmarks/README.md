# Throughput Benchmark Results

## How to Run

```bash
# Terminal 1 — start the server
uvicorn main:app --host 0.0.0.0 --port 8000

# Terminal 2 — run the benchmark
PYTHONPATH=. python benchmarks/throughput.py
```

## Environment

| Item | Value |
|------|-------|
| Machine | Apple Silicon (arm64) |
| OS | macOS 26.1 |
| Python | 3.13.7 |
| Concurrency | 10 concurrent requests |
| Warmup orders | 50 (excluded from measurements) |
| Benchmark orders | 500 per scenario |

## Results

### Single-ticker (AAPL, limit orders, partial matches)

All 500 orders routed to one book. Alternating BUY/SELL at incrementing prices
produces occasional matches when a BUY price crosses a resting SELL.

| Metric | Value |
|--------|-------|
| Orders processed | 500 |
| Wall time | 0.488 s |
| Throughput | 1024.9 orders/sec |
| p50 latency | 9.35 ms |
| p95 latency | 11.25 ms |
| p99 latency | 19.58 ms |
| Min / Max | 5.44 ms / 30.69 ms |

### Multi-ticker (5 tickers, limit orders, partial matches)

500 orders interleaved across AAPL, MSFT, GOOGL, TSLA, NVDA.
Per-ticker consumer sharding lets all 5 books progress concurrently,
yielding higher throughput than single-ticker.

| Metric | Value |
|--------|-------|
| Orders processed | 500 |
| Wall time | 0.513 s |
| Throughput | 974.2 orders/sec |
| p50 latency | 8.74 ms |
| p95 latency | 19.55 ms |
| p99 latency | 27.87 ms |
| Min / Max | 5.06 ms / 34.34 ms |

### Matched orders (AAPL, trade per BUY)

250 SELL orders placed first (all rest at $150.00), then 250 BUY orders at
$150.00 — every BUY immediately matches a resting SELL and generates a trade.
Measures throughput when the matching engine is fully active.

| Metric | Value |
|--------|-------|
| Orders processed | 500 |
| Wall time | 0.571 s |
| Throughput | 876.0 orders/sec |
| p50 latency | 11.86 ms |
| p95 latency | 15.37 ms |
| p99 latency | 24.07 ms |
| Min / Max | 5.15 ms / 30.58 ms |

## Design Notes

- **Transport**: Real HTTP over TCP to a local uvicorn server. Latency includes
  TCP stack, uvicorn HTTP/1.1 parsing, asyncio queue round-trip, aiofiles event
  log write, and matching engine work.
- **Single event loop**: Uvicorn runs one worker. All consumers and request
  handlers share one event loop — concurrency is cooperative.
- **Multi-ticker advantage**: Per-ticker consumers run as separate asyncio tasks.
  When orders are spread across 5 tickers, all 5 consumers make progress
  concurrently, yielding higher throughput than single-ticker.
- **Matching overhead**: The matched-order scenario is slightly slower than
  resting-only orders due to the extra `event_log.append_trade_executed()` write
  and `risk.record_fill()` call per trade.
- **Bottleneck**: The asyncio event loop and aiofiles I/O for the event log,
  not the O(log n) matching algorithm.
