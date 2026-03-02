"""
Prometheus metrics for the trading platform.

Exposes:
  orders_submitted_total        — counter, labels: ticker, side, order_type
  orders_rejected_total         — counter, labels: ticker, reason
  trades_executed_total         — counter, labels: ticker
  order_processing_seconds      — histogram, labels: ticker
  order_queue_depth             — gauge, labels: ticker
  active_websocket_connections_total — gauge
"""

from prometheus_client import Counter, Gauge, Histogram

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
