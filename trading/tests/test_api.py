"""
Tests for REST API endpoints.

Uses httpx.AsyncClient with FastAPI's ASGI transport to test
the full request/response cycle including the async consumer.
"""


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
    await client.post(
        "/orders",
        json={
            "ticker": "MSFT",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 50,
            "price": "300.00",
        },
    )

    response = await client.get("/book/MSFT")
    assert response.status_code == 200
    data = response.json()
    assert len(data["asks"]) == 1
    assert data["asks"][0]["price"] == "300.00"
    assert data["asks"][0]["quantity"] == 50
    assert data["best_ask"] == "300.00"


# ── POST /orders ──────────────────────────────────────────────────────────────


async def test_submit_limit_buy_no_match(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "GOOGL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "price": "2500.00",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["ticker"] == "GOOGL"
    assert data["status"] == "NEW"
    assert data["filled_quantity"] == 0
    assert data["trades"] == []


async def test_submit_limit_sell_no_match(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "TSLA",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 20,
            "price": "250.00",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "NEW"
    assert data["trades"] == []


async def test_submit_orders_that_match(client):
    # Sell side
    sell_resp = await client.post(
        "/orders",
        json={
            "ticker": "NVDA",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 100,
            "price": "500.00",
        },
    )
    assert sell_resp.status_code == 201

    # Buy side — should match immediately
    buy_resp = await client.post(
        "/orders",
        json={
            "ticker": "NVDA",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 100,
            "price": "500.00",
        },
    )
    assert buy_resp.status_code == 201
    data = buy_resp.json()
    assert data["status"] == "FILLED"
    assert data["filled_quantity"] == 100
    assert len(data["trades"]) == 1
    assert data["trades"][0]["price"] == "500.00"
    assert data["trades"][0]["quantity"] == 100


async def test_submit_market_order_with_liquidity(client):
    await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 75,
            "price": "155.00",
        },
    )

    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 75,
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "FILLED"
    assert len(data["trades"]) == 1
    assert data["trades"][0]["price"] == "155.00"


async def test_submit_market_order_no_liquidity(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 999,
        },
    )
    # Empty book is now caught by the spread protection check before matching
    assert response.status_code == 422
    assert "empty" in response.json()["detail"].lower()


async def test_submit_partial_fill(client):
    # Large sell
    await client.post(
        "/orders",
        json={
            "ticker": "MSFT",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 100,
            "price": "310.00",
        },
    )

    # Small buy — partial fill
    response = await client.post(
        "/orders",
        json={
            "ticker": "MSFT",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 40,
            "price": "310.00",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "FILLED"
    assert data["filled_quantity"] == 40
    assert len(data["trades"]) == 1

    # Remaining 60 should still be in book
    book = await client.get("/book/MSFT")
    asks = book.json()["asks"]
    ask_at_310 = next((a for a in asks if a["price"] == "310.00"), None)
    assert ask_at_310 is not None
    assert ask_at_310["quantity"] == 60


async def test_submit_order_invalid_ticker(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "INVALID",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "price": "100.00",
        },
    )
    assert response.status_code == 422


async def test_submit_limit_order_missing_price(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
        },
    )
    assert response.status_code == 422


async def test_submit_market_order_with_price_rejected(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 10,
            "price": "100.00",
        },
    )
    assert response.status_code == 422


async def test_submit_zero_quantity_rejected(client):
    response = await client.post(
        "/orders",
        json={
            "ticker": "AAPL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 0,
            "price": "150.00",
        },
    )
    assert response.status_code == 422


# ── POST /orders/{id}/cancel ──────────────────────────────────────────────────


async def test_cancel_open_order(client):
    submit = await client.post(
        "/orders",
        json={
            "ticker": "GOOGL",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 5,
            "price": "1.00",
        },
    )
    order_id = submit.json()["order_id"]

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
    await client.post(
        "/orders",
        json={
            "ticker": "TSLA",
            "side": "SELL",
            "order_type": "LIMIT",
            "quantity": 10,
            "price": "200.00",
        },
    )
    buy = await client.post(
        "/orders",
        json={
            "ticker": "TSLA",
            "side": "BUY",
            "order_type": "LIMIT",
            "quantity": 10,
            "price": "200.00",
        },
    )
    order_id = buy.json()["order_id"]

    cancel = await client.post(f"/orders/{order_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["success"] is False
