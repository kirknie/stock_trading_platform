"""
Shared test fixtures.
"""

import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.fixture
async def client():
    """
    Async test client with full ASGI lifespan.

    LifespanManager triggers main.py's startup (init_app_state +
    consumer task) and shutdown (consumer task cancelled) for every
    test. Without this the queue has no consumer and POST /orders
    hangs forever awaiting the future.
    """
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as ac:
            yield ac
