"""
Failure injection tests.

Verifies the system behaves correctly under adverse conditions:
malformed event logs, duplicate order IDs across restarts, risk
violations, spread violations, and invalid inputs.
"""

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from main import app
from trading.risk.checker import MAX_POSITION_QUANTITY

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_order(
    ticker: str = "AAPL",
    side: str = "BUY",
    order_type: str = "LIMIT",
    quantity: int = 10,
    price: str | None = "100.00",
    order_id: str | None = None,
    account_id: str = "default",
) -> dict:
    payload: dict = {
        "ticker": ticker,
        "side": side,
        "order_type": order_type,
        "quantity": quantity,
        "account_id": account_id,
    }
    if price is not None:
        payload["price"] = price
    if order_id is not None:
        payload["order_id"] = order_id
    return payload


# ── Spread / liquidity protection ─────────────────────────────────────────────


async def test_market_order_rejected_when_book_empty(client):
    resp = await client.post("/orders", json=make_order(order_type="MARKET", price=None))
    assert resp.status_code == 422
    assert "empty" in resp.json()["detail"].lower()


async def test_market_order_rejected_when_spread_too_wide(client):
    # Create a wide spread: ask at $200, bid at $100 → spread = $100 > $50 limit
    await client.post("/orders", json=make_order(side="SELL", price="200.00"))
    await client.post("/orders", json=make_order(side="BUY", price="100.00"))

    resp = await client.post("/orders", json=make_order(order_type="MARKET", price=None))
    assert resp.status_code == 422
    assert "spread" in resp.json()["detail"].lower()


# ── Cancel edge cases ─────────────────────────────────────────────────────────


async def test_cancel_nonexistent_order_returns_false(client):
    resp = await client.post("/orders/nonexistent-id/cancel")
    assert resp.status_code == 200
    assert resp.json()["success"] is False


async def test_cancel_after_fill_returns_not_found(client):
    await client.post("/orders", json=make_order(side="SELL", price="150.00", quantity=10))
    buy = await client.post("/orders", json=make_order(side="BUY", price="150.00", quantity=10))
    assert buy.json()["filled_quantity"] == 10
    order_id = buy.json()["order_id"]

    cancel = await client.post(f"/orders/{order_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["success"] is False


# ── Risk state integrity ───────────────────────────────────────────────────────


async def test_risk_violation_does_not_corrupt_risk_state(client):
    """A rejected order must not alter risk state — the next valid order still passes."""
    # Valid order
    resp1 = await client.post("/orders", json=make_order(quantity=100))
    assert resp1.status_code == 201

    # Violates position limit — must not corrupt state
    resp2 = await client.post(
        "/orders",
        json=make_order(quantity=MAX_POSITION_QUANTITY + 1, price="1.00"),
    )
    assert resp2.status_code == 422

    # Another valid order — must still be accepted
    resp3 = await client.post("/orders", json=make_order(quantity=100, order_id=None))
    assert resp3.status_code == 201


# ── Invalid input ─────────────────────────────────────────────────────────────


async def test_submit_to_unsupported_ticker_returns_422(client):
    resp = await client.post("/orders", json=make_order(ticker="FAKE"))
    assert resp.status_code == 422


# ── Restart resilience ────────────────────────────────────────────────────────


async def test_duplicate_order_id_does_not_double_submit_after_restart(
    tmp_path, monkeypatch
):
    """Idempotency cache survives restart — same order_id returns HTTP 200, no new order."""
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post("/orders", json=make_order(order_id="dup-1"))
            assert resp.status_code == 201
            original_id = resp.json()["order_id"]

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.post("/orders", json=make_order(order_id="dup-1"))
            assert resp.status_code == 200
            assert resp.json()["order_id"] == original_id

            # Book must have exactly one resting bid, not two
            book = (await client.get("/book/AAPL")).json()
            assert sum(level["quantity"] for level in book["bids"]) == 10


async def test_event_log_truncated_mid_line_does_not_crash_on_restart(
    tmp_path, monkeypatch
):
    """A partial last line in the event log (simulated crash) must not prevent startup."""
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    log_path = tmp_path / "events.log"

    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            for i in range(3):
                await client.post("/orders", json=make_order(quantity=10 + i))

    # Truncate the last line to simulate a partial write at crash time
    content = log_path.read_bytes()
    # Remove last 20 bytes — enough to corrupt the final JSON line
    log_path.write_bytes(content[:-20])

    # Restart must succeed — no exception, /tickers must respond
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as client:
            resp = await client.get("/tickers")
            assert resp.status_code == 200
            assert "AAPL" in resp.json()["tickers"]
