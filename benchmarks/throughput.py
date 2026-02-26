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
CONCURRENCY = 10  # concurrent requests in flight


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
    """Run a batch of orders concurrently and collect latency stats."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        # Warmup: let the server JIT-warm before measuring
        warmup = payloads[:WARMUP_ORDERS]
        await asyncio.gather(*[send_order(client, p) for p in warmup])

        # Benchmark: bounded concurrency via semaphore
        bench = payloads[WARMUP_ORDERS:]
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_send(payload: dict) -> float:
            async with semaphore:
                return await send_order(client, payload)

        start_wall = time.perf_counter()
        latencies: list[float] = await asyncio.gather(
            *[bounded_send(p) for p in bench]
        )
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


async def main() -> None:
    total = WARMUP_ORDERS + BENCHMARK_ORDERS

    # Benchmark 1: Single ticker — all orders go to the same book
    single_payloads = [
        make_limit_order(
            "AAPL",
            str(Decimal("150.00") + i),
            side="BUY" if i % 2 == 0 else "SELL",
        )
        for i in range(total)
    ]
    result1 = await run_benchmark("Single-ticker (AAPL)", single_payloads)

    # Benchmark 2: Multi-ticker — orders spread across all 5 books
    base_prices = {
        "AAPL": "150.00",
        "MSFT": "300.00",
        "GOOGL": "2500.00",
        "TSLA": "200.00",
        "NVDA": "500.00",
    }
    multi_payloads = [
        make_limit_order(
            TICKERS[i % 5],
            base_prices[TICKERS[i % 5]],
            side="BUY" if i % 2 == 0 else "SELL",
        )
        for i in range(total)
    ]
    result2 = await run_benchmark("Multi-ticker (5 tickers)", multi_payloads)

    # Print results table
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
