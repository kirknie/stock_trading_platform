"""
Health and readiness endpoint tests.
"""


async def test_health_returns_200(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_ready_returns_200_when_initialised(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert "tickers" in body
    assert "AAPL" in body["tickers"]


async def test_ready_returns_queue_depths(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert "queue_depths" in body
    queue_depths = body["queue_depths"]
    for ticker in ("AAPL", "MSFT", "GOOGL", "TSLA", "NVDA"):
        assert ticker in queue_depths
        assert queue_depths[ticker] == 0
