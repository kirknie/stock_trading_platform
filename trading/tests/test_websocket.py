"""
Tests for WebSocket market data streaming.
"""

import asyncio
import json
import pytest
import httpx
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from httpx_ws import aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from main import app


@pytest.fixture
async def managed_app(tmp_path, monkeypatch):
    """Run the full app lifespan (consumer + broadcaster) for the test."""
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    async with LifespanManager(app) as manager:
        yield manager.app


@pytest.fixture
async def client(managed_app):
    """HTTP client for submitting orders during WebSocket tests."""
    async with AsyncClient(
        transport=ASGITransport(app=managed_app), base_url="http://test"
    ) as ac:
        yield ac


async def test_websocket_subscribe_ack(managed_app, client):
    """Subscribing returns an acknowledgement with the requested tickers."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(
                    json.dumps({"action": "subscribe", "tickers": ["AAPL"]})
                )
                ack = json.loads(await ws.receive_text())
                assert ack["type"] == "subscribed"
                assert "AAPL" in ack["tickers"]


async def test_websocket_book_update_on_order(managed_app, client):
    """Submitting an order triggers a book_update for subscribed clients."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(
                    json.dumps({"action": "subscribe", "tickers": ["AAPL"]})
                )
                await ws.receive_text()  # ack

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

                # Collect messages until we find the book_update event
                book_msg = None
                for _ in range(5):
                    msg = json.loads(
                        await asyncio.wait_for(ws.receive_text(), timeout=2.0)
                    )
                    if msg["type"] == "book_update":
                        book_msg = msg
                        break

                assert book_msg is not None
                assert book_msg["type"] == "book_update"
                assert book_msg["ticker"] == "AAPL"
                assert book_msg["best_ask"] == "150.00"
                assert book_msg["best_bid"] is None


async def test_websocket_trade_event_on_match(managed_app, client):
    """Matching orders generates a trade event for subscribed clients."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(
                    json.dumps({"action": "subscribe", "tickers": ["MSFT"]})
                )
                await ws.receive_text()  # ack

                await client.post(
                    "/orders",
                    json={
                        "ticker": "MSFT",
                        "side": "SELL",
                        "order_type": "LIMIT",
                        "quantity": 10,
                        "price": "300.00",
                    },
                )
                await ws.receive_text()  # book_update from sell order

                await client.post(
                    "/orders",
                    json={
                        "ticker": "MSFT",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "quantity": 10,
                        "price": "300.00",
                    },
                )

                # Collect messages until we find the trade event
                trade_msg = None
                for _ in range(5):
                    msg = json.loads(
                        await asyncio.wait_for(ws.receive_text(), timeout=2.0)
                    )
                    if msg["type"] == "trade":
                        trade_msg = msg
                        break

                assert trade_msg is not None
                assert trade_msg["ticker"] == "MSFT"
                assert trade_msg["price"] == "300.00"
                assert trade_msg["quantity"] == 10


async def test_websocket_ticker_filtering(managed_app, client):
    """Clients only receive events for their subscribed tickers."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(
                    json.dumps({"action": "subscribe", "tickers": ["GOOGL"]})
                )
                await ws.receive_text()  # ack

                # Order on TSLA (not subscribed) — should produce no message
                await client.post(
                    "/orders",
                    json={
                        "ticker": "TSLA",
                        "side": "SELL",
                        "order_type": "LIMIT",
                        "quantity": 5,
                        "price": "200.00",
                    },
                )

                # Order on GOOGL (subscribed) — should produce a message
                await client.post(
                    "/orders",
                    json={
                        "ticker": "GOOGL",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "quantity": 5,
                        "price": "2500.00",
                    },
                )

                msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=2.0))
                assert msg["ticker"] == "GOOGL"


async def test_websocket_invalid_subscription(managed_app, client):
    """Invalid first message returns an error."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(json.dumps({"action": "invalid"}))
                msg = json.loads(await ws.receive_text())
                assert "error" in msg


async def test_order_status_event_received_on_fill(managed_app, client):
    """Filling an order emits an order_status event with status FILLED."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(
                    json.dumps({"action": "subscribe", "tickers": ["NVDA"]})
                )
                await ws.receive_text()  # ack

                # Place resting SELL
                await client.post(
                    "/orders",
                    json={
                        "ticker": "NVDA",
                        "side": "SELL",
                        "order_type": "LIMIT",
                        "quantity": 10,
                        "price": "500.00",
                    },
                )
                await ws.receive_text()  # order_status (OPEN)
                await ws.receive_text()  # book_update

                # Place matching BUY — triggers fill
                resp = await client.post(
                    "/orders",
                    json={
                        "ticker": "NVDA",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "quantity": 10,
                        "price": "500.00",
                    },
                )
                assert resp.json()["filled_quantity"] == 10

                # Collect messages until we find order_status with FILLED
                status_msg = None
                for _ in range(10):
                    msg = json.loads(
                        await asyncio.wait_for(ws.receive_text(), timeout=2.0)
                    )
                    if msg["type"] == "order_status" and msg["status"] == "FILLED":
                        status_msg = msg
                        break

                assert status_msg is not None
                assert status_msg["ticker"] == "NVDA"
                assert status_msg["filled_quantity"] == 10
                assert status_msg["remaining_quantity"] == 0


async def test_order_status_event_received_on_cancel(managed_app, client):
    """Cancelling a resting order emits an order_status event with status CANCELED."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(
                    json.dumps({"action": "subscribe", "tickers": ["TSLA"]})
                )
                await ws.receive_text()  # ack

                # Place a resting BUY
                resp = await client.post(
                    "/orders",
                    json={
                        "ticker": "TSLA",
                        "side": "BUY",
                        "order_type": "LIMIT",
                        "quantity": 5,
                        "price": "200.00",
                    },
                )
                order_id = resp.json()["order_id"]
                await ws.receive_text()  # order_status (OPEN)
                await ws.receive_text()  # book_update

                # Cancel it
                await client.post(f"/orders/{order_id}/cancel")

                msg = json.loads(
                    await asyncio.wait_for(ws.receive_text(), timeout=2.0)
                )
                assert msg["type"] == "order_status"
                assert msg["status"] == "CANCELED"
                assert msg["ticker"] == "TSLA"
                assert msg["order_id"] == order_id
