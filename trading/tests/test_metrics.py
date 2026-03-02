"""
Tests for Prometheus metrics instrumentation.

Counters are module-level singletons that accumulate across the process, so
each test captures a baseline before the action and asserts on the delta.
REGISTRY.get_sample_value() is the stable public API for reading metric values.
"""

from prometheus_client import REGISTRY

from trading.risk.checker import MAX_POSITION_QUANTITY


def _sample(name: str, labels: dict) -> float:
    """Return the current sample value for a labelled metric, defaulting to 0."""
    value = REGISTRY.get_sample_value(name, labels)
    return value if value is not None else 0.0


# ── Counter tests ─────────────────────────────────────────────────────────────


async def test_order_submission_increments_counter(client):
    labels = {"ticker": "AAPL", "side": "BUY", "order_type": "LIMIT"}
    before = _sample("orders_submitted_total", labels)

    resp = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "price": "150.00",
        },
    )
    assert resp.status_code == 201

    assert _sample("orders_submitted_total", labels) - before == 1.0


async def test_risk_violation_increments_rejected_counter(client):
    labels = {"ticker": "AAPL", "reason": "risk_violation"}
    before = _sample("orders_rejected_total", labels)

    resp = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": MAX_POSITION_QUANTITY + 1,
            "price": "1.00",
        },
    )
    assert resp.status_code == 422

    assert _sample("orders_rejected_total", labels) - before == 1.0


async def test_trade_increments_trade_counter(client):
    labels = {"ticker": "AAPL"}
    before = _sample("trades_executed_total", labels)

    await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 50,
            "price": "150.00",
        },
    )
    resp = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 50,
            "price": "150.00",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["filled_quantity"] == 50

    assert _sample("trades_executed_total", labels) - before == 1.0


# ── Endpoint test ─────────────────────────────────────────────────────────────


async def test_metrics_endpoint_returns_200(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    body = resp.text
    assert "orders_submitted_total" in body
    assert "orders_rejected_total" in body
    assert "trades_executed_total" in body
    assert "order_processing_seconds" in body
    assert "order_queue_depth" in body
