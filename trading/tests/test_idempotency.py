"""
Tests for idempotent order submission.

Covers:
  - Duplicate order_id returns cached response (HTTP 200)
  - Duplicate does not create a second resting order in the book
  - Different order_ids with same payload create separate orders
  - Orders without order_id are never cached (no dedup)
  - Idempotency key is order_id-specific, not payload-specific
  - Cached response fields match the original
"""


# ── Helpers ───────────────────────────────────────────────────────────────────


def limit_order(
    order_id: str | None = None,
    ticker: str = "AAPL",
    side: str = "BUY",
    quantity: int = 10,
    price: str = "150.00",
    account_id: str = "default",
) -> dict:
    payload: dict = {
        "ticker": ticker,
        "side": side,
        "order_type": "LIMIT",
        "quantity": quantity,
        "price": price,
        "account_id": account_id,
    }
    if order_id is not None:
        payload["order_id"] = order_id
    return payload


# ── Core deduplication ────────────────────────────────────────────────────────


async def test_first_submission_returns_201(client):
    resp = await client.post("/orders", json=limit_order(order_id="key-1"))
    assert resp.status_code == 201


async def test_duplicate_submission_returns_200(client):
    await client.post("/orders", json=limit_order(order_id="key-2"))
    resp2 = await client.post("/orders", json=limit_order(order_id="key-2"))
    assert resp2.status_code == 200


async def test_duplicate_returns_same_order_id(client):
    payload = limit_order(order_id="key-3")
    resp1 = await client.post("/orders", json=payload)
    resp2 = await client.post("/orders", json=payload)
    assert resp2.json()["order_id"] == resp1.json()["order_id"]


async def test_duplicate_returns_same_status(client):
    payload = limit_order(order_id="key-4")
    resp1 = await client.post("/orders", json=payload)
    resp2 = await client.post("/orders", json=payload)
    assert resp2.json()["status"] == resp1.json()["status"]


async def test_duplicate_returns_same_trades(client):
    # Submit a matching sell first so the buy generates a trade
    await client.post(
        "/orders",
        json=limit_order(ticker="AAPL", side="SELL", quantity=10, price="150.00"),
    )
    payload = limit_order(
        order_id="key-5", ticker="AAPL", side="BUY", quantity=10, price="150.00"
    )
    resp1 = await client.post("/orders", json=payload)
    resp2 = await client.post("/orders", json=payload)
    assert resp2.json()["trades"] == resp1.json()["trades"]


async def test_third_and_later_submissions_also_return_200(client):
    payload = limit_order(order_id="key-6")
    await client.post("/orders", json=payload)
    for _ in range(3):
        resp = await client.post("/orders", json=payload)
        assert resp.status_code == 200


# ── Book integrity ────────────────────────────────────────────────────────────


async def test_duplicate_does_not_add_second_resting_order(client):
    payload = limit_order(order_id="key-7", ticker="MSFT", price="299.00", quantity=20)
    await client.post("/orders", json=payload)
    await client.post("/orders", json=payload)  # duplicate

    book = await client.get("/book/MSFT")
    bids = book.json()["bids"]
    bid = next((b for b in bids if b["price"] == "299.00"), None)
    assert bid is not None
    assert bid["quantity"] == 20  # only one order, not two


# ── No dedup without order_id ─────────────────────────────────────────────────


async def test_order_without_id_always_creates_new_order(client):
    payload = limit_order(ticker="TSLA", price="200.00", quantity=5)
    resp1 = await client.post("/orders", json=payload)
    resp2 = await client.post("/orders", json=payload)
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["order_id"] != resp2.json()["order_id"]


async def test_order_without_id_adds_two_resting_orders(client):
    payload = limit_order(ticker="GOOGL", price="2500.00", quantity=5)
    await client.post("/orders", json=payload)
    await client.post("/orders", json=payload)

    book = await client.get("/book/GOOGL")
    bids = book.json()["bids"]
    bid = next((b for b in bids if b["price"] == "2500.00"), None)
    assert bid is not None
    assert bid["quantity"] == 10  # two separate orders of 5 each


# ── Key specificity ───────────────────────────────────────────────────────────


async def test_different_order_ids_create_separate_orders(client):
    base = {
        "ticker": "NVDA",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": 5,
        "price": "800.00",
    }
    resp1 = await client.post("/orders", json={**base, "order_id": "key-A"})
    resp2 = await client.post("/orders", json={**base, "order_id": "key-B"})
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["order_id"] != resp2.json()["order_id"]


async def test_idempotency_is_key_based_not_payload_based(client):
    """Same key, different payload → cached first response returned."""
    resp1 = await client.post(
        "/orders",
        json=limit_order(order_id="key-C", ticker="AAPL", quantity=10, price="100.00"),
    )
    # Same key, different price — should return cached first response
    resp2 = await client.post(
        "/orders",
        json=limit_order(order_id="key-C", ticker="AAPL", quantity=99, price="200.00"),
    )
    assert resp2.status_code == 200
    assert resp2.json()["order_id"] == resp1.json()["order_id"]
    assert resp2.json()["quantity"] == 10  # original quantity, not 99
