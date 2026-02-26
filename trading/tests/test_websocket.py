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
async def managed_app():
    """Run the full app lifespan (consumer + broadcaster) for the test."""
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
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(json.dumps({"action": "subscribe", "tickers": ["AAPL"]}))
                ack = json.loads(await ws.receive_text())
                assert ack["type"] == "subscribed"
                assert "AAPL" in ack["tickers"]


async def test_websocket_book_update_on_order(managed_app, client):
    """Submitting an order triggers a book_update for subscribed clients."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(json.dumps({"action": "subscribe", "tickers": ["AAPL"]}))
                await ws.receive_text()  # ack

                await client.post("/orders", json={
                    "ticker": "AAPL",
                    "side": "SELL",
                    "order_type": "LIMIT",
                    "quantity": 50,
                    "price": "150.00",
                })

                msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=2.0))
                assert msg["type"] == "book_update"
                assert msg["ticker"] == "AAPL"
                assert msg["best_ask"] == "150.00"
                assert msg["best_bid"] is None


async def test_websocket_trade_event_on_match(managed_app, client):
    """Matching orders generates a trade event for subscribed clients."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(json.dumps({"action": "subscribe", "tickers": ["MSFT"]}))
                await ws.receive_text()  # ack

                await client.post("/orders", json={
                    "ticker": "MSFT",
                    "side": "SELL",
                    "order_type": "LIMIT",
                    "quantity": 10,
                    "price": "300.00",
                })
                await ws.receive_text()  # book_update from sell order

                await client.post("/orders", json={
                    "ticker": "MSFT",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "quantity": 10,
                    "price": "300.00",
                })

                # Collect messages until we find the trade event
                trade_msg = None
                for _ in range(5):
                    msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=2.0))
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
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(json.dumps({"action": "subscribe", "tickers": ["GOOGL"]}))
                await ws.receive_text()  # ack

                # Order on TSLA (not subscribed) — should produce no message
                await client.post("/orders", json={
                    "ticker": "TSLA",
                    "side": "SELL",
                    "order_type": "LIMIT",
                    "quantity": 5,
                    "price": "200.00",
                })

                # Order on GOOGL (subscribed) — should produce a message
                await client.post("/orders", json={
                    "ticker": "GOOGL",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "quantity": 5,
                    "price": "2500.00",
                })

                msg = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=2.0))
                assert msg["ticker"] == "GOOGL"


async def test_websocket_invalid_subscription(managed_app, client):
    """Invalid first message returns an error."""
    async with ASGIWebSocketTransport(app=managed_app) as transport:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ws_client:
            async with aconnect_ws("http://test/ws", ws_client) as ws:
                await ws.send_text(json.dumps({"action": "invalid"}))
                msg = json.loads(await ws.receive_text())
                assert "error" in msg
