"""
Shared test fixtures.
"""

import pytest
from asgi_lifespan import LifespanManager
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """
    Async test client with full ASGI lifespan.

    Redirects EVENT_LOG_PATH and SNAPSHOT_PATH to a per-test temp directory
    so each test starts with a clean engine — no replay bleed between tests.
    LifespanManager triggers main.py's startup (snapshot restore + event log
    replay + consumer task) and shutdown (final snapshot) for every test.
    """
    monkeypatch.setenv("EVENT_LOG_PATH", str(tmp_path / "events.log"))
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "snapshot.json"))
    async with LifespanManager(app) as manager:
        async with AsyncClient(
            transport=ASGITransport(app=manager.app), base_url="http://test"
        ) as ac:
            yield ac
